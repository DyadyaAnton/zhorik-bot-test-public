"""Bounded, read-only Lenta public-API diagnostic; never persists market prices.

Endpoint contract researched from shndo1337/lenta-mcp (MIT). Availability and
permission to call the public endpoint must still be checked from Amvera.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = 'https://integration.api.lenta.com'
CHANNELS = frozenset(('lo', 'locc'))


class LentaSourceError(RuntimeError):
    pass


def _request(path, params, *, fetch=None):
    params = {'retailBrand': 'lo', **params}
    if fetch is not None:
        return fetch(path, params)
    url = API + path + '?' + urlencode(params)
    try:
        with urlopen(Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Jorik-price-probe/16B'}), timeout=12) as response:
            if response.status != 200:
                raise LentaSourceError(f'HTTP {response.status}')
            data = response.read(512_000)
        return json.loads(data)
    except (OSError, ValueError) as exc:
        raise LentaSourceError(type(exc).__name__) from exc


def _valid_store(store_id):
    if isinstance(store_id, bool) or not isinstance(store_id, int) or store_id <= 0:
        raise ValueError('store_id должен быть положительным числом из списка магазинов')
    return store_id


def stores_for_city(city='Москва', *, fetch=None):
    """Return IDs only when the API itself identifies their city; no Tomsk default."""
    city = city.strip().casefold()
    if not city or len(city) > 80:
        raise ValueError('Некорректный город')
    raw = _request('/v1/stores', {}, fetch=fetch)
    if not isinstance(raw, dict) or not isinstance(raw.get('stores'), list):
        raise LentaSourceError('Неожиданный формат списка магазинов')
    result = []
    for store in raw['stores']:
        if not isinstance(store, dict) or store.get('type') == 'zoo':
            continue
        if str(store.get('cityName', '')).strip().casefold() != city:
            continue
        try:
            sid = _valid_store(int(store['id']))
        except (KeyError, ValueError, TypeError):
            continue
        result.append({'id': sid, 'city': store['cityName'], 'address': str(store.get('address') or '')[:180]})
        if len(result) >= 20:
            break
    return result


def search(query, store_id, *, channel='lo', limit=5, fetch=None):
    """Only observations requiring review; no regional/price verification claims."""
    query = query.strip()
    if not query or len(query) > 80 or any(ord(ch) < 32 for ch in query):
        raise ValueError('Поисковый запрос: 1–80 печатных символов')
    _valid_store(store_id)
    if channel not in CHANNELS or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('Допустимые каналы: lo/locc; лимит 1–5')
    raw = _request('/catalog/v1/items', {'stores': store_id, 'channel': channel,
                                         'query': query, 'limit': limit, 'offset': 0}, fetch=fetch)
    if not isinstance(raw, dict) or not isinstance(raw.get('items', []), list):
        raise LentaSourceError('Неожиданный формат ответа API')
    observed_at = datetime.now(timezone.utc).isoformat()
    observations = []
    for item in raw.get('items', [])[:limit]:
        if not isinstance(item, dict):
            continue
        prices = item.get('prices') or {}
        if not isinstance(prices, dict):
            prices = {}
        kopecks = prices.get('price')
        amount = round(kopecks / 100, 2) if type(kopecks) is int and 0 < kopecks < 10**9 else None
        observations.append({'source': 'lenta_public_probe', 'query': query,
                             'product_id': str(item.get('id') or ''),
                             'title': str(item.get('name') or '')[:180],
                             'price_rub': amount, 'store_id': store_id,
                             'channel': channel, 'region': 'не подтверждён',
                             'review_status': 'review_required', 'observed_at': observed_at})
    return observations
