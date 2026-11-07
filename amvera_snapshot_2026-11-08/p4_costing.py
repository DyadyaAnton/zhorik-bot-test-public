"""Поставка 4А (патч 1): изолированное расчётное ядро. Не изменяет SQLite и не подтверждает MCP-цены.

Что исправлено относительно первой версии:
1. Несовместимые единицы (штуки/граммы, литры/граммы) больше не роняют всю смету:
   строка получает статус unit_incompatible. Пересчёт возможен только по ЯВНЫМ подсказкам
   (UnitHints: масса штуки, плотность), которые передаёт вызывающий код. Такие строки помечаются approximate.
2. Выбор цены сравнивает цены в одной единице (цена за требуемую единицу), а не «сырые» числа.
3. Упаковки считаются на уровне плана (estimate_plan) после объединения одинаковых продуктов;
   цена упаковки хранится отдельно (pack_price), поэтому 129 ₽ за 900 г — это ровно 129 ₽.
   Входные float округляются, чтобы не тащить шум вида 0.30000000000000004.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

UNITS = {'г': ('mass', Decimal('0.001')), 'кг': ('mass', Decimal('1')),
         'мл': ('volume', Decimal('0.001')), 'л': ('volume', Decimal('1')),
         'шт': ('count', Decimal('1'))}
SOURCE_PRIORITY = {'personal': 0, 'manual_store': 1, 'reference': 2, 'public_catalog': 3}
CENT = Decimal('0.01')


class UnitMismatch(ValueError):
    """Единицы несовместимы и нет подсказки для безопасного пересчёта."""


def decimal(value):
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        result = Decimal(repr(round(value, 10)))   # убираем двоичный шум float
    else:
        result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Не конечное число')
    return result


@dataclass(frozen=True)
class UnitHints:
    """Явные данные для пересчёта между видами единиц; сам Жорик их не выдумывает."""
    g_per_pcs: Decimal | None = None        # масса одной штуки, г (яйцо ~55)
    density_g_per_ml: Decimal | None = None  # плотность, г/мл (молоко ~1.03)

    def __post_init__(self):
        for name in ('g_per_pcs', 'density_g_per_ml'):
            value = getattr(self, name)
            if value is not None:
                value = decimal(value)
                if value <= 0:
                    raise ValueError(f'{name} должно быть положительным')
                object.__setattr__(self, name, value)


@dataclass(frozen=True)
class PriceQuote:
    product: str
    price: Decimal                 # цена за 1 quote.unit
    unit: str
    source: str
    observed_at: str
    store: str = ''
    confirmed: bool = False
    pack_quantity: Decimal | None = None
    pack_unit: str | None = None
    match_status: str = 'review_required'
    pack_price: Decimal | None = None   # цена целой упаковки (если известна точно)


def quote_from_pack(product, pack_price, pack_quantity, pack_unit, source, observed_at, **kwargs):
    """Котировка из «цена за упаковку»: 129 ₽ за 900 г. Цена за единицу выводится, цена упаковки хранится точно."""
    price_pack, qty = decimal(pack_price), decimal(pack_quantity)
    if price_pack <= 0 or qty <= 0:
        raise ValueError('Цена и размер упаковки должны быть положительными')
    return PriceQuote(product, price_pack / qty, pack_unit, source, observed_at,
                      pack_quantity=qty, pack_unit=pack_unit, pack_price=price_pack, **kwargs)


def convert(amount, source_unit, target_unit, hints: UnitHints | None = None):
    if source_unit not in UNITS or target_unit not in UNITS:
        raise ValueError('Неизвестная единица')
    kind_a, factor_a = UNITS[source_unit]
    kind_b, factor_b = UNITS[target_unit]
    value = decimal(amount)
    if kind_a == kind_b:
        return value * factor_a / factor_b
    h = hints or UnitHints()
    base = value * factor_a            # кг, л или шт
    pair = (kind_a, kind_b)
    if pair == ('count', 'mass') and h.g_per_pcs:
        converted = base * h.g_per_pcs / 1000
    elif pair == ('mass', 'count') and h.g_per_pcs:
        converted = base / (h.g_per_pcs / 1000)
    elif pair == ('volume', 'mass') and h.density_g_per_ml:
        converted = base * h.density_g_per_ml      # л × (кг/л) = кг
    elif pair == ('mass', 'volume') and h.density_g_per_ml:
        converted = base / h.density_g_per_ml
    else:
        raise UnitMismatch(f'Несовместимые единицы {source_unit}→{target_unit}: нужна явная подсказка (масса штуки/плотность)')
    return converted / factor_b


def _approximate(unit_a, unit_b):
    return UNITS[unit_a][0] != UNITS[unit_b][0]


def eligible(quote, today=None, max_age_days=14):
    if quote.source not in SOURCE_PRIORITY or not quote.confirmed:
        return False
    if quote.source == 'public_catalog' and quote.match_status != 'confirmed':
        return False
    if decimal(quote.price) <= 0 or quote.unit not in UNITS:
        return False
    try:
        observed = date.fromisoformat(quote.observed_at[:10])
    except (ValueError, TypeError):
        return False
    age = ((today or date.today()) - observed).days
    return 0 <= age <= max_age_days


def price_per(quote, unit, hints: UnitHints | None = None):
    """Цена за 1 `unit`, пересчитанная из единицы котировки (UnitMismatch, если нельзя)."""
    return decimal(quote.price) * convert(1, unit, quote.unit, hints)


def choose_price(product, quotes: Iterable[PriceQuote], today=None, max_age_days=14, stores=None,
                 need_unit=None, hints: UnitHints | None = None):
    """Только подтверждённые актуальные цены; приоритет personal > manual > reference > public,
    затем более свежая дата, затем самая дешёвая — сравнение всегда в одной единице.
    need_unit: единица, в которой нужен продукт; котировки, которые нельзя пересчитать, отбрасываются."""
    allowed = set(stores) if stores is not None else None
    candidates = []
    for q in quotes:
        if q.product.casefold() != product.casefold():
            continue
        if allowed is not None and q.store and q.store not in allowed:
            continue
        if not eligible(q, today, max_age_days):
            continue
        if need_unit is not None:
            try:
                comparable = price_per(q, need_unit, hints)
            except UnitMismatch:
                continue
            kind = ''
        else:
            kind, factor = UNITS[q.unit]
            comparable = decimal(q.price) / factor   # цена за кг / л / шт
        candidates.append(((SOURCE_PRIORITY[q.source], -date.fromisoformat(q.observed_at[:10]).toordinal(),
                            kind, comparable), q))
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1] if candidates else None


def cost_ingredient(amount, unit, quote: PriceQuote, hints: UnitHints | None = None):
    """Стоимость использованного количества и (для одной строки) целых упаковок.
    Для сметы всего меню используйте estimate_plan: упаковки нельзя суммировать по строкам."""
    quantity = decimal(amount)
    if quantity <= 0:
        raise ValueError('Количество должно быть положительным')
    usage_cost = convert(quantity, unit, quote.unit, hints) * decimal(quote.price)
    purchase_cost = packages = None
    if quote.pack_quantity is not None:
        packages, purchase_cost = _packs(quantity, unit, quote, hints)
    return {'usage_cost': usage_cost, 'purchase_cost': purchase_cost, 'packages': packages}


def _packs(quantity, unit, quote, hints):
    if not quote.pack_unit:
        raise ValueError('Для фасовки необходима единица')
    pack = decimal(quote.pack_quantity)
    if pack <= 0:
        raise ValueError('Фасовка должна быть положительной')
    packages = math.ceil(convert(quantity, unit, quote.pack_unit, hints) / pack)
    if quote.pack_price is not None:
        purchase = decimal(quote.pack_price) * packages
    else:
        purchase = convert(pack * packages, quote.pack_unit, quote.unit, hints) * decimal(quote.price)
    return packages, purchase


def _hints_for(product, hints: Mapping[str, UnitHints] | None):
    if not hints:
        return None
    return {k.casefold(): v for k, v in hints.items()}.get(product.casefold())


def estimate_ingredients(ingredients, quotes, today=None, max_age_days=14, stores=None, hints=None):
    """Оценка стоимости ИСПОЛЬЗОВАННЫХ количеств. ingredients: [{product, amount, unit}].
    Отсутствующая цена и несовместимые единицы не превращаются в ноль и не роняют смету.
    hints: {название продукта: UnitHints}. Закупку (упаковки) считает estimate_plan."""
    quotes = list(quotes)
    rows, missing, incompatible, usage_total = [], [], [], Decimal('0')
    for item in ingredients:
        product, unit = item['product'], item['unit']
        quantity = decimal(item['amount'])
        if quantity <= 0:
            raise ValueError('Количество должно быть положительным')
        h = _hints_for(product, hints)
        if choose_price(product, quotes, today, max_age_days, stores) is None:
            missing.append(product)
            rows.append({'product': product, 'status': 'missing_price'})
            continue
        quote = choose_price(product, quotes, today, max_age_days, stores, need_unit=unit, hints=h)
        if quote is None:
            incompatible.append(product)
            rows.append({'product': product, 'status': 'unit_incompatible'})
            continue
        cost = convert(quantity, unit, quote.unit, h) * decimal(quote.price)
        usage_total += cost
        rows.append({'product': product, 'status': 'priced', 'source': quote.source, 'store': quote.store,
                     'usage_cost': cost, 'approximate': _approximate(unit, quote.unit)})
    return {'rows': rows, 'known_usage_cost': usage_total.quantize(CENT, rounding=ROUND_HALF_UP),
            'missing_prices': sorted(set(missing)), 'unit_issues': sorted(set(incompatible)),
            'complete': not missing and not incompatible,
            'purchase_total': None, 'purchase_total_status': 'use_estimate_plan'}


def estimate_plan(ingredients, quotes, today=None, max_age_days=14, stores=None, hints=None):
    """Смета всего плана: одинаковые продукты объединяются, упаковки считаются один раз на продукт.
    purchase_total возвращается только если у ВСЕХ продуктов есть подходящая цена и фасовка."""
    quotes = list(quotes)
    groups = {}
    for item in ingredients:
        if decimal(item['amount']) <= 0:
            raise ValueError('Количество должно быть положительным')
        groups.setdefault(item['product'].casefold(), {'product': item['product'], 'items': []})['items'].append(item)

    rows, missing, incompatible, no_pack = [], [], [], []
    usage_total = purchase_total = Decimal('0')
    for group in groups.values():
        product, items = group['product'], group['items']
        h = _hints_for(product, hints)
        if choose_price(product, quotes, today, max_age_days, stores) is None:
            missing.append(product); rows.append({'product': product, 'status': 'missing_price'}); continue
        quote = choose_price(product, quotes, today, max_age_days, stores, need_unit=items[0]['unit'], hints=h)
        try:
            if quote is None:
                raise UnitMismatch(product)
            total = sum((convert(i['amount'], i['unit'], quote.unit, h) for i in items), Decimal('0'))
        except UnitMismatch:
            incompatible.append(product); rows.append({'product': product, 'status': 'unit_incompatible'}); continue
        usage = total * decimal(quote.price)
        usage_total += usage
        row = {'product': product, 'status': 'priced', 'source': quote.source, 'store': quote.store,
               'total_amount': total, 'unit': quote.unit, 'usage_cost': usage,
               'approximate': any(_approximate(i['unit'], quote.unit) for i in items),
               'packages': None, 'purchase_cost': None}
        if quote.pack_quantity is None:
            no_pack.append(product); row['status'] = 'priced_no_pack'
        else:
            packages, purchase = _packs(total, quote.unit, quote, h)
            row['packages'], row['purchase_cost'] = packages, purchase
            purchase_total += purchase
        rows.append(row)

    if missing or incompatible:
        status, total_value = 'incomplete_prices', None
    elif no_pack:
        status, total_value = 'missing_pack_info', None
    else:
        status, total_value = 'complete', purchase_total.quantize(CENT, rounding=ROUND_HALF_UP)
    return {'rows': rows, 'known_usage_cost': usage_total.quantize(CENT, rounding=ROUND_HALF_UP),
            'missing_prices': sorted(set(missing)), 'unit_issues': sorted(set(incompatible)),
            'no_pack_info': sorted(set(no_pack)),
            'complete': not missing and not incompatible,
            'purchase_total': total_value, 'purchase_total_status': status}
