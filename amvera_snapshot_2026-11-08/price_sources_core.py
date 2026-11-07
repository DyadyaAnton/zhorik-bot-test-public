"""Жорик: общая модель наблюдений цен (лаборатория, без записи в рабочую БД)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Observation:
    store: str
    product_id: str
    title: str
    price_rub: str
    old_price_rub: str
    quantity: str
    unit: str
    unit_price_rub: str
    store_id: str
    region: str
    source_url: str
    observed_at: str
    status: str = 'review_required'


def money(value):
    if isinstance(value, dict):
        value = value.get('value')
    if value is None or isinstance(value, bool):
        return ''
    try:
        d = Decimal(str(value).replace(' ', '').replace(',', '.'))
        if not d.is_finite() or d <= 0 or d > 1_000_000:
            return ''
        return str(d.quantize(Decimal('0.01')))
    except (ValueError, InvalidOperation):
        return ''


def package(title):
    """Conservative packaging guess: does not confuse grams with millilitres."""
    m = re.search(r'(?<!\d)(\d+(?:[.,]\d+)?)\s*(кг|г|гр|мл|л|шт)(?![а-яa-z])', title.lower())
    if not m:
        return '', ''
    return m.group(1).replace(',', '.'), {'гр': 'г'}.get(m.group(2), m.group(2))


def observation(store, raw, *, store_id='', region='', source_url=''):
    if not isinstance(raw, dict):
        return None
    title = str(raw.get('title') or raw.get('name') or raw.get('productName') or '').strip()[:300]
    price = money(raw.get('price'))
    if not title or not price:
        return None
    qty, unit = package(title)
    old = money(raw.get('oldPrice') or raw.get('old_price'))
    per_unit = ''
    if qty and unit in ('кг', 'г', 'л', 'мл'):
        amount = Decimal(qty) * (Decimal('0.001') if unit in ('г', 'мл') else 1)
        if amount > 0:
            per_unit = str((Decimal(price) / amount).quantize(Decimal('0.01')))
    return Observation(store, str(raw.get('id') or raw.get('code') or raw.get('plu') or ''), title,
                       price, old, qty, unit, per_unit, str(store_id), str(region or 'неизвестен'),
                       source_url, datetime.now(timezone.utc).isoformat())


def csv_bytes(rows):
    fields = list(Observation.__dataclass_fields__)
    f = io.StringIO(newline='')
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    for item in rows:
        data = asdict(item)
        for key, value in data.items():
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                data[key] = "'" + value
        writer.writerow(data)
    return f.getvalue().encode('utf-8-sig')
