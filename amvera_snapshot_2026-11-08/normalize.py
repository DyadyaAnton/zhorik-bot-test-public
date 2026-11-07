"""Вытаскиваем вес/объём из названий и считаем цену за базовую единицу."""
import re

_MULT = re.compile(r"(\d+)\s*[xх×*]\s*(\d+[.,]?\d*)\s*(кг|г|гр|л|мл)\b", re.I)
_SIMPLE = re.compile(r"(\d+[.,]?\d*)\s*(кг|г|гр|л|мл|шт)\b", re.I)
_TO_BASE = {"кг": (1000, "g"), "г": (1, "g"), "гр": (1, "g"),
            "л": (1000, "ml"), "мл": (1, "ml"), "шт": (1, "pcs")}


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_pack(name: str):
    """'Гречка ядрица 800 г' -> (800.0, 'g'); '10 шт' -> (10, 'pcs'); '2 x 200 г' -> (400, 'g').
    Возвращает (None, None), если ничего не нашли — такие SKU уходят на ручную проверку."""
    m = _MULT.search(name)
    if m:
        k, unit = _TO_BASE[m.group(3).lower()]
        return int(m.group(1)) * _num(m.group(2)) * k, unit
    # если несколько совпадений (напр. «Яйцо С1 10 шт 60 г») — берём первое с весом, иначе штуки
    found = _SIMPLE.findall(name)
    for val, u in found:
        k, unit = _TO_BASE[u.lower()]
        if unit in ("g", "ml"):
            return _num(val) * k, unit
    for val, u in found:
        k, unit = _TO_BASE[u.lower()]
        return _num(val) * k, unit
    return None, None


def price_per_base(price: float, pack_amount: float):
    return price / pack_amount if pack_amount else None


def price_per_kg_or_l(price: float, pack_amount: float, unit: str):
    """Человекочитаемая цена за кг/л (для штучных — за штуку)."""
    p = price_per_base(price, pack_amount)
    if p is None:
        return None
    return p * 1000 if unit in ("g", "ml") else p


if __name__ == "__main__":
    tests = ["Гречка ядрица 800 г", "Молоко 2,5% 0,93 л", "Йогурт 4 x 125 г",
             "Яйцо куриное С1 10 шт", "Масло подсолнечное 1 л", "Сыр Российский"]
    for t in tests:
        print(f"{t!r:40} -> {parse_pack(t)}")
