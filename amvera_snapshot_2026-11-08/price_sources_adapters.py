"""Ограниченные диагностические адаптеры публичных интерфейсов магазинов.

Неофициальные интерфейсы могут измениться. Нет обхода 403, cookies, браузерной
маскировки, повторов, пагинации и записи в рабочую БД. Один запрос на запуск.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
import requests
from price_sources_core import observation

MAX_RESPONSE = 1_000_000
ENDPOINTS = {
    'magnit': 'https://magnit.ru/webgate/v2/goods/search',
    'auchan': 'https://www.auchan.ru/v1/catalog/products',
}


def _post(store, payload):
    url = ENDPOINTS[store]
    # Endpoints are fixed in code; never accept an arbitrary destination or redirect.
    with requests.post(url, json=payload, timeout=(5, 20), allow_redirects=False,
                       headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
                       stream=True) as resp:
        if resp.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(f'{store}: редирект HTTP {resp.status_code}; переход отключён')
        if resp.status_code in (401, 403, 429):
            raise RuntimeError(f'{store}: HTTP {resp.status_code}; доступ ограничен, не повторяем')
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_content(16_384):
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise ValueError('Ответ превышает 1 МБ')
            chunks.append(chunk)
    return json.loads(b''.join(chunks).decode('utf-8-sig'))


def magnit_probe(store_code, search_term):
    """Один запрос на пять товаров по поисковой строке."""
    if not isinstance(store_code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', store_code):
        raise ValueError('Неверный код магазина')
    if not isinstance(search_term, str) or not search_term.strip() or len(search_term) > 100:
        raise ValueError('Неверный поисковый запрос')
    search_term = search_term.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in search_term):
        raise ValueError('Неверный поисковый запрос')
    payload = {'sort': {'order': 'desc', 'type': 'popularity'},
               'pagination': {'limit': 5, 'offset': 0},
               'includeAdultGoods': True, 'storeCode': store_code,
               'storeType': '1', 'catalogType': '1',
               'term': search_term, 'categories': []}
    data = _post('magnit', payload)
    if not isinstance(data, dict):
        raise ValueError('Неожиданный ответ Магнита')
    items = data.get('items', [])
    if not isinstance(items, list):
        raise ValueError('Неожиданный формат items')
    return [x for raw in items[:5] if (x := _magnit_row(raw, store_code))]



def magnit_categories_probe(store_code):
    """Один запрос категорий, как в исходном magnit_goods; без случайных ID."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', store_code):
        raise ValueError('Неверный код магазина')
    payload = {'sort': {'order': 'desc', 'type': 'popularity'},
               'pagination': {'limit': 5, 'offset': 0},
               'includeAdultGoods': True, 'storeCode': store_code,
               'storeType': '1', 'catalogType': '1', 'categories': []}
    data = _post('magnit', payload)
    if not isinstance(data, dict):
        raise ValueError('Неожиданный ответ Магнита')
    cat = data.get('category') or {}
    extended = cat.get('fastCategoriesExtended', []) if isinstance(cat, dict) else []
    items = extended or data.get('fastCategories', [])
    if not isinstance(items, list):
        raise ValueError('Неожиданный формат категорий')
    result = []
    for item in items[:5]:
        if isinstance(item, dict):
            result.append({'id': str(item.get('id') or item.get('categoryId') or ''),
                           'name': str(item.get('name') or item.get('title') or '')[:160]})
    return result



def magnit_structure_probe(store_code):
    """Single request; return only a bounded, redacted JSON shape, never values."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', store_code):
        raise ValueError('Неверный код магазина')
    payload = {'sort': {'order': 'desc', 'type': 'popularity'},
               'pagination': {'limit': 5, 'offset': 0},
               'includeAdultGoods': True, 'storeCode': store_code,
               'storeType': '1', 'catalogType': '1', 'categories': []}
    data = _post('magnit', payload)
    return _safe_shape(data)


def _safe_shape(data):
    """Report structural types and counts only; no raw API values or keys that look secret."""
    sensitive = re.compile(r'token|secret|password|cookie|session|auth|key|phone|email|address|user|personal|credential', re.I)
    def shape(value, depth=0):
        if depth >= 3:
            return type(value).__name__
        if isinstance(value, dict):
            keys = [k for k in value if isinstance(k, str) and not sensitive.search(k)]
            keys = sorted(keys)[:20]
            return {'type': 'object', 'fields': {k[:60]: shape(value[k], depth + 1) for k in keys},
                    'field_count': len(value), 'truncated': len(value) > len(keys)}
        if isinstance(value, list):
            return {'type': 'array', 'count': len(value),
                    'first_item_shape': shape(value[0], depth + 1) if value else None}
        return type(value).__name__
    return {'http_status': 200, 'response_shape': shape(data),
            'note': 'Только типы, имена несекретных полей и размеры массивов; значения скрыты.'}

def _magnit_row(raw, store_code):
    if not isinstance(raw, dict):
        return None
    # Magnit API fields may vary; never infer prices from arbitrary numeric fields.
    product = raw.get('product') if isinstance(raw.get('product'), dict) else raw
    price = product.get('price')
    if isinstance(price, dict):
        price = price.get('value') if price.get('value') is not None else price.get('current')
    # В исходном magnit_goods цена API выражена в копейках.
    # Отбрасываем неизвестные/нечисловые форматы, не угадываем валюту.
    if isinstance(price, bool) or not isinstance(price, (int, float, str)):
        return None
    try:
        kopecks = Decimal(str(price))
        if not kopecks.is_finite() or kopecks <= 0 or kopecks != kopecks.to_integral_value():
            return None
        price = str(kopecks / Decimal(100))
    except (InvalidOperation, ValueError):
        return None
    row = {'id': product.get('id') or product.get('productId'),
           'title': product.get('name') or product.get('title'), 'price': price}
    return observation('magnit', row, store_id=store_code, source_url=ENDPOINTS['magnit'])


def auchan_probe(category, merchant_id):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', category):
        raise ValueError('Неверная категория')
    if not isinstance(merchant_id, int) or not 1 <= merchant_id <= 100000:
        raise ValueError('Неверный merchantId')
    payload = {'filter': {'category': category, 'promo_only': False,
                          'active_only': False, 'cashback_only': False},
               'page': 1, 'perPage': 5, 'merchantId': merchant_id}
    data = _post('auchan', payload)
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError('Неожиданный формат ответа Ашана')
    rows = []
    for raw in data['items'][:5]:
        if not isinstance(raw, dict):
            continue
        code = raw.get('code')
        url = ('https://www.auchan.ru/product/' + str(code) + '/') if code and re.fullmatch(r'[\w-]{1,80}', str(code)) else ENDPOINTS['auchan']
        item = observation('auchan', raw, store_id=str(merchant_id), source_url=url)
        if item:
            rows.append(item)
    return rows


def pyaterochka_offline(raw_items, store_id):
    """Normalize a supplied JSON sample, without accessing protected endpoints."""
    if not isinstance(raw_items, list):
        raise ValueError('Ожидается массив JSON')
    result = []
    for raw in raw_items[:50]:
        if not isinstance(raw, dict):
            continue
        p = raw.get('product') if isinstance(raw.get('product'), dict) else raw
        price = p.get('price')
        if isinstance(price, dict):
            price = price.get('regular') or price.get('value')
        candidate = {'id': p.get('plu') or p.get('id'), 'title': p.get('name') or p.get('title'),
                     'price': price, 'oldPrice': p.get('old_price')}
        item = observation('pyaterochka', candidate, store_id=store_id,
                           source_url='https://5ka.ru/')
        if item:
            result.append(item)
    return result
