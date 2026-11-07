"""Изолированная диагностика публичной HTML-категории Перекрёстка.

Независимая реализация: не использует код стороннего GPL-парсера.
Один GET, без cookies, авторизации, браузера, редиректов и записи в БД.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests
from price_sources_core import observation

BASE = 'https://www.perekrestok.ru'
MAX_BODY = 1_000_000


class PerekrestokProbeError(Exception):
    pass


class _StructuredData(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self._capture = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            a = dict(attrs)
            self._capture = a.get('type', '').lower() == 'application/ld+json'
            self._buf = []

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._capture:
            self.scripts.append(''.join(self._buf))
            self._capture = False
            self._buf = []


def _path(category):
    category = (category or '').strip()
    if category.startswith(BASE):
        parsed = urlsplit(category)
        if parsed.scheme != 'https' or parsed.netloc != 'www.perekrestok.ru' or parsed.query or parsed.fragment:
            raise ValueError('Разрешён только адрес www.perekrestok.ru без параметров')
        category = parsed.path
    if not re.fullmatch(r'/cat/c/\d+/[a-z0-9-]{1,100}/?', category):
        raise ValueError('Нужен путь категории /cat/c/ID/nazvanie (с сайта Перекрёстка)')
    return category


def _walk(node):
    if isinstance(node, list):
        for child in node:
            yield from _walk(child)
    elif isinstance(node, dict):
        if node.get('@type') == 'Product' or (isinstance(node.get('@type'), list) and 'Product' in node['@type']):
            yield node
        for key in ('@graph', 'itemListElement', 'item', 'mainEntity'):
            if key in node:
                yield from _walk(node[key])


def _rows_from_html(html, source_url):
    parser = _StructuredData()
    parser.feed(html)
    rows = []
    seen = set()
    for script in parser.scripts[:40]:
        try:
            payload = json.loads(script)
        except (ValueError, TypeError):
            continue
        for item in _walk(payload):
            offer = item.get('offers') or {}
            if isinstance(offer, list):
                offer = next((x for x in offer if isinstance(x, dict) and x.get('price')), {})
            if not isinstance(offer, dict):
                continue
            raw = {'id': item.get('sku') or item.get('productID') or '',
                   'name': item.get('name'), 'price': offer.get('price')}
            result = observation('perekrestok', raw, source_url=source_url)
            if result and (result.product_id, result.title) not in seen:
                seen.add((result.product_id, result.title))
                rows.append(result)
                if len(rows) >= 5:
                    return rows, len(parser.scripts)
    return rows, len(parser.scripts)


def perekrestok_probe(category):
    path = _path(category)
    url = BASE + path
    try:
        with requests.get(url, timeout=(7, 15), allow_redirects=False, stream=True,
                          headers={'Accept': 'text/html', 'User-Agent': 'ZhorikPriceLab/1.0'}) as response:
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                raise PerekrestokProbeError(f'HTTP {status}: редирект запрещён')
            if status != 200:
                raise PerekrestokProbeError(f'HTTP {status}: страница недоступна')
            content_type = response.headers.get('Content-Type', '').lower()
            if 'text/html' not in content_type:
                raise PerekrestokProbeError('Ответ не является HTML')
            chunks, total = [], 0
            for chunk in response.iter_content(16384):
                total += len(chunk)
                if total > MAX_BODY:
                    raise PerekrestokProbeError('HTML больше 1 МБ; остановлено')
                chunks.append(chunk)
            html = b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace')
    except requests.RequestException as exc:
        raise PerekrestokProbeError(f'Сетевая ошибка: {type(exc).__name__}') from None
    rows, scripts = _rows_from_html(html, url)
    return {'http_status': 200, 'html_bytes': total,
            'jsonld_blocks': scripts, 'offers_found': len(rows),
            'note': 'Нет товаров: возможно, каталог загружается JavaScript; обход не выполняется.' if not rows else 'Найдены товары в JSON-LD'}, rows
