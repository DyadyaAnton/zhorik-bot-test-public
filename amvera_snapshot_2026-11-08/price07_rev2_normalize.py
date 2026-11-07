"""Conservative package normalization; never changes match/review status."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MULTI = re.compile(r'(?<!\d)(\d{1,3})\s*[xх×]\s*(\d+(?:[.,]\d+)?)\s*(кг|гр?|мл|л|шт)\b', re.I)
SINGLE = re.compile(r'(?<!\d)(\d+(?:[.,]\d+)?)\s*(кг|гр?|мл|л|шт)\b', re.I)
FACTOR = {'г': ('кг', Decimal('0.001')), 'гр': ('кг', Decimal('0.001')),
          'кг': ('кг', Decimal(1)), 'мл': ('л', Decimal('0.001')),
          'л': ('л', Decimal(1)), 'шт': ('шт', Decimal(1))}


def normalized_unit_price(title, price):
    if not isinstance(title, str) or not title.strip():
        return None
    try:
        amount = Decimal(str(price))
        if not amount.is_finite() or amount <= 0:
            return None
    except (ValueError, InvalidOperation, TypeError):
        return None
    matches = list(MULTI.finditer(title))
    singles = list(SINGLE.finditer(title))
    if matches:
        # The SINGLE regex also sees the size inside a multipack; any
        # *other* quantity means the product's total volume is ambiguous.
        if len(matches) != 1 or any(not (matches[0].start() <= s.start()
                                     and s.end() <= matches[0].end()) for s in singles):
            return None
        match = matches[0]
        count, size, unit = match.groups()
        count = Decimal(count)
        size = Decimal(size.replace(',', '.'))
    else:
        if len(singles) != 1:
            return None
        match = singles[0]
        size, unit = match.groups()
        size, count = Decimal(size.replace(',', '.')), Decimal(1)
    base, factor = FACTOR[unit.lower()]
    total = size * count * factor
    if total <= 0 or total > 10000:
        return None
    try:
        unit_price = amount / total
        # Corrupt/untrusted retailer payload must not crash settlement or
        # overflow float storage; implausible values remain unnormalized.
        if not unit_price.is_finite() or unit_price > Decimal('1e12'):
            return None
        unit_price = unit_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError):
        return None
    return {'base_unit': base, 'package_total': total, 'unit_price_rub': float(unit_price)}
