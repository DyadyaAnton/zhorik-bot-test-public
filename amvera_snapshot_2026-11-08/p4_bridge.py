"""Поставка 4Б/1: read-only adapter of existing menu and personal prices.

No migrations, no external calls, no automatic approval of retailer candidates.
"""
import json
import os
from datetime import date
from p4_costing import PriceQuote, UnitHints, estimate_plan, decimal, quote_from_pack
from menu_generator import MILK_DENSITY_G_PER_ML

# Единое окно свежести личных цен для диагностики Поставки 4 (дни).
# Меняется переменной окружения P4_PRICE_MAX_AGE_DAYS; по умолчанию 14.
def price_window_days():
    try:
        value = int(os.environ.get('P4_PRICE_MAX_AGE_DAYS', '14'))
    except ValueError:
        return 14
    return value if 1 <= value <= 365 else 14


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def quotes_for_user(db, uid):
    """User-entered prices are personal observations, not store-verified prices.

    They can estimate consumed quantities, but cannot certify the cost of buying packs.
    """
    if not _table_exists(db, 'user_prices'):
        return []
    result = []
    for row in db.execute('SELECT product,unit,price,observed_at,shop FROM user_prices WHERE user_id=?', (uid,)):
        if row['unit'] not in ('г','кг','мл','л','шт'):
            continue
        try:
            price = decimal(row['price'])
            if price <= 0:
                continue
            result.append(PriceQuote(product=row['product'], price=price, unit=row['unit'],
                                     source='personal', observed_at=row['observed_at'],
                                     store=row['shop'] or '', confirmed=True,
                                     match_status='confirmed'))
        except (ValueError, TypeError, ArithmeticError):
            continue
    # Stage 0: identities and observations are separate; legacy is migrated once.
    from p4_approval_core import init, linked_quotes
    init(db)
    for r in linked_quotes(db, uid):
        try:
            result.append(quote_from_pack(r['product'],r['pack_price'],r['pack_amount'],r['base_unit'],
                source='public_catalog',observed_at=r['observed_at'],store='ВкусВилл (публичный каталог)',
                confirmed=True,match_status='confirmed'))
        except (ValueError,TypeError,ArithmeticError):
            continue
    return result


def ingredients_from_saved(menu_json, people):
    """Read actual recipe quantities; no dependence on potentially stale stats cache."""
    data = json.loads(menu_json) if isinstance(menu_json, str) else menu_json
    plan = data['menu']['menu']
    items = []
    for daily in plan.values():
        for dishes in daily.values():
            for dish in dishes:
                for ingredient in dish['ingredients']:
                    items.append({'product': ingredient['product'],
                                  'amount': decimal(ingredient['amount']) * int(people),
                                  'unit': ingredient['unit']})
    return items


def estimate_saved_menu(db, uid, menu_id, today=None, max_age_days=None):
    row = db.execute('SELECT id,people_count,budget,result_json FROM menus WHERE id=? AND owner_id=?',
                     (menu_id, uid)).fetchone()
    if row is None:
        return None
    window = max_age_days or price_window_days()
    items = ingredients_from_saved(row['result_json'], row['people_count'])
    quotes = quotes_for_user(db, uid)
    # No hard-coded milk density: conversion hints must be confirmed in a future catalog.
    # Align with the menu generator's milk-only conversion. Approximate density,
    # never applied to other liquids or inferred from retailer pack labels.
    hints = {'молоко': UnitHints(density_g_per_ml=decimal(MILK_DENSITY_G_PER_ML))}
    result = estimate_plan(items, quotes, today=today or date.today(), max_age_days=window, hints=hints) if items else {
        'rows': [], 'known_usage_cost': decimal('0'), 'missing_prices': [],
        'unit_issues': [], 'no_pack_info': [], 'complete': False,
        'purchase_total': None, 'purchase_total_status': 'empty_plan'}
    have_any = {q.product.casefold() for q in quotes}
    # «Нет цены вообще» и «цена есть, но устарела» — разные причины, их нужно различать.
    result['stale_prices'] = [p for p in result['missing_prices'] if p.casefold() in have_any]
    result['no_price_at_all'] = [p for p in result['missing_prices'] if p.casefold() not in have_any]
    result['budget'] = decimal(row['budget'] or 0)
    result['ingredients_count'] = len({i['product'].casefold() for i in items})
    result['priced_count'] = sum(1 for r in result['rows'] if r['status'].startswith('priced'))
    result['personal_quotes'] = sum(q.source == 'personal' for q in quotes)
    result['approved_public_quotes'] = sum(q.source == 'public_catalog' for q in quotes)
    result['window_days'] = window
    return result


def format_diagnostic(result):
    if result is None:
        return 'Меню не найдено или оно принадлежит другому пользователю.'
    status = result['purchase_total_status']
    total, priced = result['ingredients_count'], result.get('priced_count', 0)
    window = result.get('window_days', 14)
    full_coverage = total > 0 and priced == total
    lines = ['🧮 Поставка 4Б · проверка сметы',
             f'Продуктов в меню: {total}',
             f'Личных ценовых наблюдений: {result["personal_quotes"]} (свежими считаются цены не старше {window} дн.)',
             f'Подтверждённых котировок публичного каталога: {result.get("approved_public_quotes", 0)}',
             f'Покрытие ценами: {priced} из {total} продуктов']
    if total == 0:
        lines.append('План пуст: сумма не рассчитывается.')
        lines.append(f'Бюджет: {result["budget"]:.2f} ₽')
    elif full_coverage:
        lines.append(f'Стоимость использованных продуктов: {result["known_usage_cost"]:.2f} ₽')
        lines.append(f'Бюджет: {result["budget"]:.2f} ₽')
    else:
        lines.append(f'Частичная сумма по {priced} из {total} продуктов: {result["known_usage_cost"]:.2f} ₽ '
                     '(это НЕ стоимость меню)')
        lines.append(f'Бюджет: {result["budget"]:.2f} ₽. Сравнение невозможно: цены есть не для всех продуктов.')
    lines += [f'Нет цены: {len(result.get("no_price_at_all", []))}',
              f'Цена устарела: {len(result.get("stale_prices", []))}',
              f'Несовместимые единицы: {len(result["unit_issues"])}',
              f'Нет подтверждённой фасовки: {len(result["no_pack_info"])}']
    if status == 'complete':
        lines.append(f'Стоимость закупки упаковками: {result["purchase_total"]:.2f} ₽')
    else:
        lines.append('⚠️ Полная стоимость закупки НЕ подтверждена.')
        lines.append('Причина: ' + {'empty_plan': 'пустой план',
                                   'incomplete_prices': 'нет цен или несовместимые единицы',
                                   'missing_pack_info': 'нет сведений о фасовке'}.get(status, status))
    for title, key in (('Нет цены', 'no_price_at_all'), ('Цена устарела', 'stale_prices'), ('Единицы', 'unit_issues')):
        values = result.get(key) or []
        if values:
            lines.append(f'{title}: ' + ', '.join(values[:20]) + (f' и ещё {len(values) - 20}' if len(values) > 20 else ''))
    if any(r.get('approximate') for r in result.get('rows', [])):
        lines.append('⚠️ Пересчёт массы молока в объём приблизительный (1,03 г/мл); это не проверенная фасовка по адресу.')
    lines.append('Кандидаты ВкусВилла используются только после ручного сопоставления; цены публичного каталога ориентировочные.')
    return '\n'.join(lines)[:3900]
