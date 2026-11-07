"""Жорик: безопасная однократная диагностика публичного каталога Пятёрочки.

Не получает cookies, не запускает браузер и не пишет в базу данных.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import requests

from price_sources_core import observation

_BASE = 'https://5d.5ka.ru/api/catalog/v3/stores'
_MAX_BYTES = 1_048_576


class PyaterochkaProbeError(RuntimeError):
    pass


def _extract_products(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        raise PyaterochkaProbeError('Неожиданный тип ответа API')
    for key in ('products', 'items', 'data'):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            for inner in ('products', 'items', 'results'):
                if isinstance(value.get(inner), list):
                    return [x for x in value[inner] if isinstance(x, dict)]
    return []


def _positive_price(value):
    from price_sources_core import money
    return money(value)


def _row(raw, store_id):
    prices = raw.get('prices') if isinstance(raw.get('prices'), dict) else {}
    # Не смешиваем цену по карте, акционную и обычную: выбор требует проверки.
    # Предпочитаем обычную, если она есть; скидки фиксируем отдельно.
    regular = prices.get('regular')
    discount = prices.get('discount')
    price = discount if _positive_price(discount) else regular
    if price is None:
        price = raw.get('price')
    if not _positive_price(price):
        return None
    old = regular if _positive_price(discount) and _positive_price(regular) and float(regular) > float(discount) else None
    clean = {
        'id': raw.get('plu') or raw.get('id') or '',
        'name': raw.get('name') or raw.get('title') or '',
        'price': price,
        'oldPrice': old,
    }
    return observation('pyaterochka', clean, store_id=store_id, region='неизвестен',
                       source_url=f'{_BASE}/{quote(store_id)}/search')


def pyaterochka_probe(store_id: str, search_term: str):
    """Один GET, максимум 5 товаров; не выполняет повторные попытки."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', store_id):
        raise ValueError('Неверный код магазина: нужен SAP-код, например S105')
    search_term = search_term.strip()
    if not search_term or len(search_term) > 100 or any(ord(c) < 32 for c in search_term):
        raise ValueError('Неверная поисковая фраза')
    url = f'{_BASE}/{quote(store_id)}/search'
    params = {'q': search_term, 'mode': 'store', 'offset': 0, 'limit': 5, 'include_restrict': 'true'}
    headers = {'Accept': 'application/json', 'User-Agent': 'ZhorikPriceLab/1.0'}
    try:
        with requests.get(url, params=params, headers=headers, timeout=(5, 15),
                          allow_redirects=False, stream=True) as response:
            if response.is_redirect:
                raise PyaterochkaProbeError('Сервер перенаправил запрос; переход запрещён')
            if response.status_code in (401, 403):
                raise PyaterochkaProbeError(f'HTTP {response.status_code}: доступ ограничен; браузерный обход не выполняется')
            if response.status_code != 200:
                raise PyaterochkaProbeError(f'HTTP {response.status_code}: запрос не принят')
            content_type = response.headers.get('Content-Type', '').lower()
            if 'json' not in content_type:
                raise PyaterochkaProbeError('Ответ не JSON; возможно, требуется сессия сайта')
            data = bytearray()
            for chunk in response.iter_content(16384):
                data.extend(chunk)
                if len(data) > _MAX_BYTES:
                    raise PyaterochkaProbeError('Ответ превышает лимит 1 МБ')
            import json
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise PyaterochkaProbeError('Некорректный JSON') from exc
    except requests.RequestException as exc:
        raise PyaterochkaProbeError(f'Ошибка сети: {type(exc).__name__}') from exc
    result = []
    for item in _extract_products(payload)[:5]:
        row = _row(item, store_id)
        if row is not None:
            result.append(row)
    return result
