"""Conservative read-only SKU identity assessment for human review.

This module NEVER changes canonical product links or verification flags.
An equal product name is a review candidate, not proof of region, price,
packaging, nutrition or allergens.
"""
from __future__ import annotations

import re
from decimal import Decimal


def _text(value):
    return ' '.join(str(value or '').casefold().replace('ё', 'е').split())


def _percent(value):
    match = re.search(r'(?<!\d)(\d{1,2}(?:[.,]\d+)?)\s*%', _text(value))
    return Decimal(match.group(1).replace(',', '.')) if match else None


def _package(value):
    s = _text(value)
    match = re.search(r'(?<!\d)(\d+)\s*[xх×]\s*(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл|шт)\b', s)
    if match:
        count, size, unit = int(match.group(1)), Decimal(match.group(2).replace(',', '.')), match.group(3)
        return (count * size * (1000 if unit in ('кг', 'л') else 1), 'г' if unit in ('кг', 'г') else 'мл' if unit in ('л', 'мл') else 'шт')
    match = re.search(r'(?<!\d)(\d+(?:[.,]\d+)?)\s*(кг|г|л|мл|шт)\b', s)
    if not match:
        return None
    size, unit = Decimal(match.group(1).replace(',', '.')), match.group(2)
    return (size * (1000 if unit in ('кг', 'л') else 1), 'г' if unit in ('кг', 'г') else 'мл' if unit in ('л', 'мл') else 'шт')


def package_basis(title):
    """Infer only from an explicit quantity; missing/ambiguous is UNKNOWN."""
    units = re.findall(r'(?<!\d)\d+(?:[.,]\d+)?\s*(кг|г|л|мл|шт)\b', _text(title))
    bases = {{'кг':'кг','г':'кг','л':'л','мл':'л','шт':'шт'}[u] for u in units}
    if len(bases) != 1:
        return None
    return next(iter(bases))


def assess(raw_title, canonical_title):
    """Return auditable review evidence; never authorize automatic linkage."""
    raw, canon = _text(raw_title), _text(canonical_title)
    if not raw or not canon:
        return {'status': 'insufficient', 'confidence': 0, 'reasons': ['missing_title'], 'auto_verified': False}
    reasons = []
    if _percent(raw) is not None and _percent(canon) is not None and _percent(raw) != _percent(canon):
        reasons.append('fat_percent_conflict')
    raw_pack, canon_pack = _package(raw), _package(canon)
    if raw_pack and canon_pack and raw_pack != canon_pack:
        reasons.append('package_conflict')
    for marker in ('безлактозн', 'безглютенов'):
        if (marker in raw) != (marker in canon):
            reasons.append('attribute_conflict_' + marker)
    if reasons:
        return {'status': 'conflict', 'confidence': 0, 'reasons': reasons, 'auto_verified': False}
    if raw == canon:
        return {'status': 'review_required', 'confidence': 0.5, 'reasons': ['exact_title_only'], 'auto_verified': False}
    return {'status': 'insufficient', 'confidence': 0, 'reasons': ['distinct_title_or_missing_attributes'], 'auto_verified': False}


def inspect_sku(db, sku_id):
    """Bounded read-only inspection; NEVER mutate pb_skus or observations."""
    if type(sku_id) is not int or not 1 <= sku_id <= 2**63 - 1:
        raise ValueError('Invalid SKU ID')
    row = db.execute('SELECT retailer,external_id,title,match_status,product_code FROM pb_skus WHERE id=?',
                     (sku_id,)).fetchone()
    if row is None:
        return None
    retailer, external_id, title, current_status, current_code = row
    matches = []
    catalog_rows = db.execute('SELECT code,name FROM pb_products ORDER BY code LIMIT 5001').fetchall()
    for code, name in catalog_rows[:5000]:
        result = assess(title, name)
        if result['status'] == 'review_required':
            matches.append({'code': code, 'name': name, 'confidence': result['confidence']})
    return {'retailer': retailer, 'external_id': external_id, 'title': title,
            'current_status': current_status, 'current_code': current_code,
            'suggestions': matches[:5], 'truncated': len(matches) > 5,
            'catalog_truncated': len(catalog_rows) > 5000,
            'auto_verified': False}
