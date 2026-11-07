"""Owner-scoped, read-only diagnostic of previously saved weekly menus.

No Qwen call, modification, nutrition claims, or historical menu migration.
"""
from __future__ import annotations

import json
from collections import Counter

from database import connect_db
from menu_generator import DAYS, MEALS


def list_menus(owner_id: int, *, page: int = 0, page_size: int = 8):
    if type(page) is not int or not 0 <= page <= 100000:
        raise ValueError('Invalid page')
    if not 1 <= page_size <= 20:
        raise ValueError('Invalid page size')
    with connect_db() as db:
        total = db.execute('SELECT COUNT(*) FROM menus WHERE owner_id=?', (owner_id,)).fetchone()[0]
        rows = db.execute('SELECT id FROM menus WHERE owner_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                          (owner_id, page_size, page * page_size)).fetchall()
    return total, [int(r['id']) for r in rows]


def audit_menu(owner_id: int, menu_id: int):
    """Return simple factual counts or None; not a safety certification."""
    if type(menu_id) is not int or menu_id < 1:
        return None
    with connect_db() as db:
        row = db.execute('SELECT result_json FROM menus WHERE owner_id=? AND id=?',
                         (owner_id, menu_id)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row['result_json'])
        stored = payload.get('menu') if isinstance(payload, dict) else None
        days = stored.get('menu', stored) if isinstance(stored, dict) else None
        stats = payload.get('stats', {}) if isinstance(payload, dict) else {}
        if not isinstance(days, dict):
            return None
        titles = Counter()
        products = Counter()
        breakfast = Counter()
        missing = []
        simple_dinners = []
        dish_count = 0
        for day in DAYS:
            daily = days.get(day)
            if not isinstance(daily, dict):
                missing.append(day)
                continue
            for meal in MEALS:
                dishes = daily.get(meal)
                if not isinstance(dishes, list) or not dishes:
                    missing.append(f'{day}: {meal}')
                    continue
                for dish in dishes:
                    if not isinstance(dish, dict):
                        missing.append(f'{day}: {meal} (формат)')
                        continue
                    name = dish.get('name')
                    items = dish.get('ingredients')
                    if not isinstance(name, str) or not name.strip() or not isinstance(items, list):
                        missing.append(f'{day}: {meal} (состав)')
                        continue
                    dish_count += 1
                    titles[name.strip()] += 1
                    ingredient_names = {i['product'].strip().casefold() for i in items
                                        if isinstance(i, dict) and isinstance(i.get('product'), str)
                                        and i['product'].strip()}
                    products.update(ingredient_names)
                    if meal == 'Завтрак':
                        breakfast[name.strip()] += 1
                    if meal == 'Ужин' and len(ingredient_names) <= 2:
                        simple_dinners.append(day)
        return {
            'menu_id': menu_id,
            'dish_count': dish_count,
            'missing': missing,
            'repeated_dishes': [(k, v) for k, v in titles.most_common() if v > 1][:5],
            'frequent_products': products.most_common(7),
            'simple_dinner_days': simple_dinners,
            'purchase_estimate': stats.get('purchase_estimate') if isinstance(stats, dict) else None,
            'known_kcal': stats.get('known_kcal') if isinstance(stats, dict) else None,
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def format_audit(audit):
    lines = [f'📚 Проверка сохранённого меню №{audit["menu_id"]}',
             f'Блюд: {audit["dish_count"]} (ожидается минимум 21).']
    if audit['missing']:
        lines.append('⚠️ Неполные дни/приёмы: '+', '.join(audit['missing'][:5]))
    if audit['repeated_dishes']:
        lines.append('Повторы блюд: '+', '.join(f'{name} ×{n}' for name, n in audit['repeated_dishes']))
    if audit['frequent_products']:
        lines.append('Частые ингредиенты: '+', '.join(f'{name} ×{n}' for name, n in audit['frequent_products'][:5]))
    if audit['simple_dinner_days']:
        lines.append('Простые ужины (до 2 ингредиентов): '+', '.join(audit['simple_dinner_days']))
    if type(audit['purchase_estimate']) in (int, float):
        lines.append(f'Сохранённая оценка закупки: {audit["purchase_estimate"]:.2f} ₽ (без пересчёта).')
    lines.append('ℹ️ Диагностика состава без Qwen. Новые цены и калории не вычислялись. Меню не изменено.')
    return '\n'.join(lines)[:3800]


def format_saved_menu(owner_id: int, menu_id: int):
    """Display historical dishes and grams, without recalculation or mutation.

    Returns Telegram-sized chunks; the owner filter is enforced by SQL.
    """
    with connect_db() as db:
        row = db.execute('SELECT result_json FROM menus WHERE owner_id=? AND id=?',
                         (owner_id, menu_id)).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row['result_json'])
        saved = payload['menu']
        days = saved.get('menu', saved)
        if not isinstance(days, dict):
            return []
        lines = [f'🍽 Сохранённое меню №{menu_id} · исторический снимок']
        for day in DAYS:
            lines.append('\n📅 '+day)
            daily = days.get(day, {})
            for meal in MEALS:
                lines.append(meal+':')
                dishes = daily.get(meal, []) if isinstance(daily, dict) else []
                if not isinstance(dishes, list):
                    dishes = []
                for dish in dishes[:12]:
                    if not isinstance(dish, dict):
                        continue
                    name = dish.get('name')
                    if not isinstance(name, str) or not name.strip():
                        continue
                    items = dish.get('ingredients', [])
                    grams = []
                    if isinstance(items, list):
                        for item in items[:35]:
                            if not isinstance(item, dict):
                                continue
                            product, amount, unit = item.get('product'), item.get('amount'), item.get('unit')
                            if isinstance(product, str) and type(amount) in (int, float) and amount >= 0 and unit in ('г', 'мл', 'шт'):
                                grams.append(f'{product[:35]} {amount:g} {unit}')
                    lines.append('• '+name[:120]+(' — '+', '.join(grams) if grams else ''))
                if not dishes:
                    lines.append('• Нет сохранённого блюда')
        lines.append('\nℹ️ Это исходное меню, не исправленное ИИ. Массы ингредиентов указаны как в сохранённой записи.')
        chunks = []
        current = ''
        for line in lines:
            # Never split a line mid-ingredient; truncate pathological legacy input.
            line = line[:1900]
            if len(current)+len(line)+1 > 3500:
                chunks.append(current)
                current = ''
            current += ('\n' if current else '')+line
        if current:
            chunks.append(current)
        return chunks
    except (ValueError, TypeError, KeyError, AttributeError):
        return []
