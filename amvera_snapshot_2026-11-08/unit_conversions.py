"""Пересчёт единиц цены в граммы для продуктов, цена которых задана не за кг.

Рецепты хранят количество в граммах, а цена на ряд продуктов задана за штуку или за литр.
Без пересчёта такие рецепты (все яичные) не получали цену и никогда не попадали в меню.

Коэффициенты (граммов в одной единице цены):
  яйцо: 60 г. Решение владельца от 03.11.2026; середина диапазона 55,0-64,9 г для яиц первой категории
        по ГОСТ 31654-2012. Считается масса, указанная в рецепте (без отдельного учёта скорлупы).
  масло подсолнечное: 920 г/л, масло оливковое: 915 г/л, кефир: 1030 г/л, молоко: 1030 г/л.
        Справочные значения плотности; кроме молока (значение уже было в коде) не проверены по источнику.
Ключи заданы в нормализованном виде (casefold, «ё» → «е», одинарные пробелы).
"""
EGG_GRAMS = 60.0

GRAMS_PER_PRICE_UNIT = {
    'яйца': {'шт': EGG_GRAMS},
    'яйца куриные c1': {'шт': EGG_GRAMS},
    'молоко': {'л': 1030.0},
    'кефир': {'л': 1030.0},
    'масло подсолнечное': {'л': 920.0},
    'масло оливковое': {'л': 915.0},
}


def conversion_for(name_key, prices):
    """(единица цены, граммов в единице) для первой найденной цены, либо None."""
    for price_unit, grams in GRAMS_PER_PRICE_UNIT.get(name_key, {}).items():
        if (name_key, price_unit) in prices:
            return price_unit, grams
    return None


def price_via_conversion(name_key, amount_grams, prices):
    """Стоимость amount_grams граммов через пересчёт, либо None, если пересчёт неприменим."""
    conv = conversion_for(name_key, prices)
    if conv is None:
        return None
    price_unit, grams = conv
    return prices[(name_key, price_unit)] * amount_grams / grams


def used_price_key(name_key, unit, prices):
    """Ключ цены, который реально использован для ингредиента (нужно для пометки «оценка»)."""
    basis = {'г': 'кг', 'мл': 'л'}.get(unit, unit)
    if (name_key, basis) in prices:
        return (name_key, basis)
    if unit == 'г':
        conv = conversion_for(name_key, prices)
        if conv:
            return (name_key, conv[0])
    return (name_key, basis)
