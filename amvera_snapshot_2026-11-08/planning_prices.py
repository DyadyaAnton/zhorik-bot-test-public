"""Indicative, non-verified ingredient price layer for menu planning.

The 258 canonical entries are ALL addressable. A planning fallback is an
assumption, never a claimed retailer quote, Rosstat statistic or measured
market average. Existing real user prices take priority in menu_generator.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from price_b_catalog import seeds

CENT = Decimal("0.01")
MAX_CANDIDATE_RUB = Decimal("100000")
LEVELS = {"economy": Decimal("0.80"), "standard": Decimal("1"),
          "comfort": Decimal("1.30")}

# These are transparent *planning assumptions* (RUB per kg/l/pc), NOT
# observations. Specific commodity families override their broad category.
BASE_ASSUMPTIONS = {
    "Молоко и молочные": 360, "Мясо и птица": 540,
    "Рыба и морепродукты": 610, "Крупы и макароны": 175,
    "Овощи": 160, "Фрукты и ягоды": 230,
    "Яйца и бобовые": 220, "Хлеб и мука": 190,
    "Масла и приправы": 320, "Напитки и прочее": 480,
}

SPECIAL = (
    (("молоко ",), 115), (("кефир ", "ряженка", "простокваша"), 135),
    (("йогурт питьевой",), 240), (("йогурт",), 390),
    (("сыр ", "моцарелла", "фета", "брынза"), 850),
    (("творог",), 440), (("масло сливочное",), 1050),
    (("сметана",), 340), (("сливки",), 350),
    (("куриное филе", "куриная грудка"), 430),
    (("курица целая",), 260), (("куриное", "куриная"), 350),
    (("говядина", "говяж"), 790), (("свинина", "свиной"), 440),
    (("картофель", "морковь", "свекла", "лук репчатый", "капуста белокочанная"), 105),
    (("помидоры", "огурцы", "перец сладкий"), 255),
    (("яблоки", "бананы", "апельсины", "мандарины"), 170),
    (("рис ", "гречневая", "овсяные", "макароны", "спагетти"), 145),
    (("хлеб", "батон"), 170),
    (("яйца куриные",), 13), (("яйца перепелиные",), 6),
    (("вода ",), 55), (("масло подсолнечное",), 155),
)

# The original 30-item ingredient vocabulary must continue to work. These
# are explicit planning aliases, not proof that a particular SKU matches.
LEGACY_ALIASES = {
    "молоко": "Молоко 2,5%", "кефир": "Кефир 2,5%",
    "творог": "Творог 5%", "сметана": "Сметана 20%",
    "масло сливочное": "Масло сливочное 82,5%",
    "рис": "Рис круглозерный", "гречка": "Гречневая крупа",
    "макароны": "Макароны из твердых сортов",
    "яйца": "Яйца куриные C1", "говядина": "Говядина мякоть",
    "свинина": "Свинина лопатка", "сыр твердый": "Сыр твердый",
    "лук": "Лук репчатый", "хлеб белый": "Хлеб пшеничный",
    "овсяные хлопья": "Овсяные хлопья",
}


def _key(value):
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _rounded(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# Цены, утверждённые владельцем вручную (точное совпадение названия). Мука: 03.11.2026, по накопленным наблюдениям
# (медиана 9 наблюдений ВкусВилла и Магнита около 69 ₽/кг) вместо запасной цены категории «Напитки и прочее» (480 ₽/кг).
APPROVED_PRICES = {"мука": 70, "мука пшеничная высший сорт": 70}


def assumption(name, category):
    key = _key(name)
    if key in APPROVED_PRICES:
        return Decimal(APPROVED_PRICES[key])
    for prefixes, amount in SPECIAL:
        if any(key.startswith(_key(prefix)) for prefix in prefixes):
            return Decimal(amount)
    return Decimal(BASE_ASSUMPTIONS[category])


def _observed_candidates(db):
    """Conservative, read-only observations; no candidate gets approved here.

    Price candidates may be used solely as clearly labeled provisional
    planning estimates after >=3 distinct product IDs, likely_match and an
    exact CP suggestion. This never alters review_status or verified_links.
    """
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zb_candidates'").fetchone()
    if not has_table:
        return {}
    by_code = defaultdict(dict)
    rows = db.execute("""SELECT suggested_code,retailer,product_id,unit_price_rub,unit_basis
                         FROM zb_candidates WHERE match_status='likely_match'
                         AND suggested_code IS NOT NULL""")
    units = {code: unit for code, _, _, unit in seeds()}
    for code, retailer, product_id, value, basis in rows:
        if units.get(code) != basis:
            continue
        try:
            price = Decimal(str(value))
        except (ValueError, ArithmeticError):
            continue
        if not price.is_finite() or not 0 < price < MAX_CANDIDATE_RUB:
            continue
        if not product_id:
            continue
        by_code[code].setdefault((retailer, product_id), price)
    return {code: (statistics.median(list(vals.values())), len(vals))
            for code, vals in by_code.items() if len(vals) >= 3}


def verified_market_prices(db, *, city=None, selected_shops=(), max_age_days=14):
    """Read only *verified* PRICE02 observations for the user's real region.

    Fail closed on missing scope, stale quotes, SKU identity, or incompatible
    units. Older installations without PRICE02 simply return no quotes. The
    returned keys are canonical product codes; this never promotes a price.
    """
    if not city or not str(city).strip():
        return {}
    if not 1 <= max_age_days <= 90:
        raise ValueError('Invalid quote freshness window')
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('pb_products','pb_skus','pb_observations')")}
    if len(tables) != 3:
        return {}
    shops = {str(s).casefold().strip() for s in selected_shops if str(s).strip()}
    # A user's selected retailer set is a restriction, never an instruction to
    # substitute an unavailable shop or infer a regional match.
    normalized_shops = {
        'вкусвилл': 'vkusvill', 'магнит': 'magnit',
        'лента': 'lenta', 'ашан': 'auchan',
        'пятерочка': 'pyaterochka', 'пятёрочка': 'pyaterochka',
        'перекресток': 'perekrestok', 'перекрёсток': 'perekrestok',
        'дикси': 'dixy', 'ярче!': 'yarche', 'ярче': 'yarche',
    }
    retailer_filter = {normalized_shops[s] for s in shops if s in normalized_shops}
    # No explicit user-selected shop means there is no retailer scope in which
    # we may claim a quoted menu price. Empty is not "all retailers".
    if not retailer_filter:
        return {}
    observed = db.execute('''
        SELECT p.code, p.base_unit, s.retailer, o.unit_price_rub,
               o.observed_at, o.id
        FROM pb_observations o
        JOIN pb_skus s ON s.id = o.sku_id
        JOIN pb_products p ON p.code = s.product_code
        WHERE o.review_status IN ('auto_verified', 'manually_verified')
          AND s.match_status IN ('auto_verified', 'manually_verified')
          AND o.region_confirmed = 1 AND o.region = ?
          AND o.base_unit = p.base_unit
          AND o.unit_price_rub > 0
          AND julianday(o.observed_at) IS NOT NULL
          AND julianday(o.observed_at) >= julianday('now', ?)
          AND julianday(o.observed_at) <= julianday('now', '+5 minutes')
        ORDER BY o.observed_at DESC, o.id DESC
    ''', (str(city).strip(), f'-{max_age_days} days'))
    result = {}
    for code, unit, retailer, amount, observed_at, row_id in observed:
        if retailer not in retailer_filter:
            continue
        if code not in result:
            result[code] = (float(amount), unit, retailer, observed_at, row_id)
    return result


def catalog_prices(db, level="standard"):
    """Return 258 planning entries, each with provenance and quality flags."""
    if level not in LEVELS:
        raise ValueError("Неизвестный ценовой режим")
    observed = _observed_candidates(db)
    result = {}
    for code, name, category, unit in seeds():
        if code in observed:
            base, count = observed[code]
            quality = "candidate_unverified"
            source = "Лабораторные кандидаты; товарное соответствие не подтверждено"
            # A scenario multiplier must never rewrite an observed median.
            # It is a planning discount/surcharge, not an observed price.
            price = base
            applied_level = "observed_median"
        else:
            base, count = assumption(name, category), 0
            quality = "planning_assumption"
            source = "Условная стартовая оценка, не рыночная котировка"
            price = base * LEVELS[level]
            applied_level = level
        result[code] = {
            "code": code, "name": name, "category": category, "unit": unit,
            "price": _rounded(price), "quality": quality,
            "observations": count, "source": source, "level": applied_level,
        }
    return result


def prices_for_menu(db, level="standard", *, city=None, selected_shops=(), include_provenance=False):
    """Return base prices keyed by name/unit, plus estimate identity set."""
    catalog = catalog_prices(db, level)
    values = {(_key(row["name"]), row["unit"]): float(row["price"])
              for row in catalog.values()}
    estimates = set(values)
    provenance = {(_key(row['name']),row['unit']):
                  {'quality': 'planning_assumption', 'source': row['source']}
                  for row in catalog.values()}
    # A real confirmed price replaces its planning assumption for the same
    # canonical product and aliases; every unverified candidate stays provisional.
    confirmed = verified_market_prices(db, city=city, selected_shops=selected_shops)
    names_by_code = {r['code']: r['name'] for r in catalog.values()}
    for code, (amount, unit, retailer, observed_at, row_id) in confirmed.items():
        if code in names_by_code:
            key = (_key(names_by_code[code]), unit)
            values[key] = amount
            estimates.discard(key)
            provenance[key] = {'quality': 'verified_market','retailer': retailer,
                               'region': str(city).strip(), 'observed_at': observed_at,
                               'observation_id': row_id, 'source': 'PRICE02 verified observation'}
    for alias, canonical in LEGACY_ALIASES.items():
        match = next((r for r in catalog.values() if r["name"] == canonical), None)
        if match:
            key = (_key(alias), match["unit"])
            canonical_key = (_key(canonical), match['unit'])
            values[key] = values[canonical_key]
            provenance[key] = dict(provenance[canonical_key])
            if canonical_key in estimates:
                estimates.add(key)
            else:
                estimates.discard(key)
    if include_provenance:
        return values, estimates, provenance
    return values, estimates


def package_quantity(name, unit):
    """Indicative supermarket pack size, NEVER a confirmed SKU package."""
    key = _key(name)
    if unit == "шт":
        return Decimal(10 if key.startswith("яйца") else 1)
    if unit == "л":
        return Decimal("1")
    if any(word in key for word in ("сыр", "масло сливочное", "колбас", "ветчин")):
        return Decimal("0.2")
    if any(word in key for word in ("творог", "сметана", "йогурт")):
        return Decimal("0.4")
    if any(word in key for word in ("рис", "крупа", "гречка", "макароны", "хлопья")):
        return Decimal("0.9")
    return Decimal("1")


# Исторический классификатор срока хранения сохранён для совместимости.
# REV7 считает ВСЕ продукты по расходу, независимо от срока хранения и упаковок.
LONG_LIFE_EXACT = {
    "мука", "мука пшеничная", "мука пшеничная высший сорт", "сахар", "соль",
    "масло подсолнечное", "масло оливковое",
    "гречка", "гречневая крупа", "овсяные хлопья", "пшено", "перловая крупа", "манная крупа", "булгур", "кускус",
    "ячневая крупа", "кукурузная крупа", "киноа",
    "макароны", "спагетти", "вермишель",
    "картофель", "морковь", "свекла", "лук", "лук репчатый", "чеснок",
    "капуста", "капуста белокочанная", "капуста краснокочанная",
}
LONG_LIFE_PREFIX = ("рис ", "макароны ", "мука ")  # все виды муки: пшеничная, ржаная, рисовая и др.


def is_long_life(name):
    key = _key(name)
    return key in LONG_LIFE_EXACT or key == "рис" or any(key.startswith(p) for p in LONG_LIFE_PREFIX)


# Классификатор «продаётся на вес» используется как описательная характеристика;
# расчёт стоимости в REV7 НЕ зависит от него: все позиции считаются по расходу.
BY_WEIGHT_EXACT = {
    # мясо и птица
    "курица целая", "куриное филе", "куриная грудка", "куриное бедро", "бедро куриное", "голень куриная", "куриный фарш", "фарш куриный",
    "куриная печень", "индейка", "индейка филе", "индейка бедро", "говядина", "говядина мякоть", "говяжий фарш", "говяжья печень", "печень",
    "свинина", "свинина лопатка", "свинина вырезка", "баранина",
    # рыба и морепродукты
    "минтай", "горбуша", "скумбрия", "треска", "хек", "сельдь", "семга", "форель", "пикша", "кальмар", "креветки", "рыбный фарш",
    # свежие овощи, зелень, грибы
    "огурцы", "помидоры", "перец сладкий", "кабачки", "баклажаны", "брокколи", "капуста цветная", "капуста пекинская", "капуста брюссельская",
    "редис", "репа", "сельдерей корневой", "сельдерей стеблевой", "салат", "шпинат", "руккола", "петрушка", "укроп", "кинза", "лук зеленый",
    "зелень", "грибы", "грибы шампиньоны", "грибы вешенки", "белые грибы", "тыква", "фасоль стручковая", "зеленый горошек замороженный",
    # фрукты и ягоды
    "яблоки", "бананы", "груши", "апельсины", "мандарины", "лимоны", "грейпфрут", "персики", "абрикосы", "сливы", "виноград", "киви", "гранат",
    "ананас", "авокадо", "арбуз", "дыня", "клубника", "малина", "черника", "клюква", "вишня", "черешня", "ягоды замороженные",
}
BY_WEIGHT_PREFIX = ()  # REV7 REV2: no generic prefix may classify a prepared food as raw meat/fish.


def is_by_weight(name):
    """Только известные продукты, не созвучные товарные наименования.

    Например, «куриные наггетсы» и «рыбные палочки» не классифицируются
    по широким префиксам «курин»/«рыб» как сырое мясо или рыба.
    """
    key = _key(name)
    return is_long_life(name) or key in BY_WEIGHT_EXACT or any(key.startswith(p) for p in BY_WEIGHT_PREFIX)


def purchase_estimate(ingredients, prices):
    """Стоимость всего *израсходованного* количества, без округления до упаковок.

    Даже яйцо с ценой за штуку оценивается пропорционально граммам через
    unit_conversions.EGG_GRAMS. Это цена расхода, не сумма кассового чека.
    """
    total = Decimal(0)
    unknown = []
    consumption_based = []
    for (name, unit), amount in ingredients.items():
        normalized_unit = {"г": "кг", "мл": "л"}.get(unit, unit)
        amount_base = Decimal(str(amount)) / (1000 if unit in ("г", "мл") else 1)
        value = prices.get((_key(name), normalized_unit))
        if value is None and _key(name) == "молоко" and unit == "г":
            # Explicitly approximate MILK ONLY at 1.03 kg/l, never general
            # density conversion for unrelated liquids.
            normalized_unit = "л"
            amount_base = amount_base / Decimal("1.03")
            value = prices.get((_key(name), normalized_unit))
        if value is None and unit == "г":
            # Яйца (цена за штуку), масло и кефир (цена за литр): пересчёт граммов, см. unit_conversions.
            import unit_conversions
            conv = unit_conversions.conversion_for(_key(name), prices)
            if conv:
                normalized_unit, grams = conv
                amount_base = Decimal(str(amount)) / Decimal(str(grams))
                value = prices.get((_key(name), normalized_unit))
        if value is None:
            unknown.append(name)
            continue
        consumption_based.append(name)
        total += Decimal(str(value)) * amount_base
    return {"purchase_estimate": _rounded(total),
            "unpriced": sorted(set(unknown)),
            "packaging_assumed": False,
            "consumption_based": sorted(set(consumption_based))}

