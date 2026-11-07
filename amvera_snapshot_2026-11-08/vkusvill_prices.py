"""Read-only public VkusVill MCP adapter. Candidates never become verified prices."""
import json
import logging
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from vkusvill_normalize import clean_name, normalize_candidate
from vkusvill_match import assess_match

ENDPOINT = 'https://mcp.vkusvill.ru/mcp'
_LOG = logging.getLogger(__name__)

def _shape(value, depth=0):
    """Only structural metadata; never log prices, names, tokens or raw responses."""
    if depth >= 5:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k)[:50]: _shape(v, depth + 1) for k, v in list(value.items())[:16]}
    if isinstance(value, list):
        return {'type': 'list', 'length': len(value), 'first': _shape(value[0], depth + 1) if value else None}
    return type(value).__name__



def _unpack(data):
    if not isinstance(data, dict):
        raise ValueError('Unexpected MCP response')
    if data.get('isError'):
        raise ValueError('MCP tool returned error')
    structured = data.get('structuredContent')
    if isinstance(structured, dict):
        return _unpack(structured)
    for block in data.get('content') or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            try:
                value = json.loads(block.get('text', ''))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return _unpack(value)
    return data


def _parse_response(raw):
    raw = raw.lstrip('\ufeff \t\r\n')
    if not raw:
        raise ValueError('Empty MCP response')
    if not raw.startswith('event:') and not raw.startswith('data:') and '\ndata:' not in raw:
        return json.loads(raw)
    events = []
    chunks = raw.replace('\r\n', '\n').split('\n\n')
    for chunk in chunks:
        data_lines = [line[5:].lstrip(' ') for line in chunk.split('\n') if line.startswith('data:')]
        if not data_lines:
            continue
        payload = '\n'.join(data_lines)
        if payload == '[DONE]':
            continue
        try:
            events.append(json.loads(payload))
        except ValueError:
            continue
    for event in events:
        if isinstance(event, dict) and (event.get('id') == 1 or event.get('id') == '1') and ('result' in event or 'error' in event):
            return event
    for event in events:
        if isinstance(event, dict) and ('result' in event or 'error' in event):
            return event
    raise ValueError('No MCP RPC result in SSE stream')


def _request(payload, timeout=15, attempts=3):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(ENDPOINT, body, headers={
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'User-Agent': 'ZhorikPriceSandbox/1.1',
    }, method='POST')
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('MCP response too large')
            parsed = _parse_response(raw.decode('utf-8'))
            if not isinstance(parsed, dict):
                raise ValueError('Unexpected RPC response')
            if 'error' in parsed:
                raise ValueError('MCP: ' + str(parsed['error'])[:250])
            if 'result' not in parsed:
                raise ValueError('MCP response missing result')
            return _unpack(parsed['result'])
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
            try:
                delay = min(5.0, max(0.0, float(retry_after)))
            except ValueError:
                delay = min(3.0, 0.5 * 2 ** attempt)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(min(3.0, 0.5 * 2 ** attempt))
    raise RuntimeError('MCP retries exhausted')


def _products(result):
    """Recognize the observed public catalog envelope: data.items.

    Keep older products envelopes for compatibility; fail closed otherwise.
    """
    original_result = result
    for _ in range(5):
        if not isinstance(result, dict):
            break
        for key in ('products', 'items'):
            if isinstance(result.get(key), list):
                return result[key]
        next_value = next((result[k] for k in ('data', 'result', 'response')
                           if isinstance(result.get(k), dict)), None)
        if next_value is None:
            break
        result = next_value
    _LOG.warning('VkusVill MCP unexpected product structure: %s', _shape(original_result))
    raise ValueError('MCP response has no products/items list')


def _price_amount(product):
    """Read only documented/observed current-price fields, never old prices."""
    price = product.get('price', product.get('цена'))
    if isinstance(price, dict):
        price = price.get('current', price.get('текущий'))
    try:
        amount = Decimal(str(price))
        if amount.is_finite() and amount > 0:
            return amount
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def search(query, timeout=15):
    query = query.strip()
    if not 2 <= len(query) <= 100:
        raise ValueError('Поисковая фраза: от 2 до 100 символов')
    result = _request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'vkusvill_products_search', 'arguments': {'q': query, 'page': 1, 'mode': 'short', 'limit': 10}
    }}, timeout=timeout)
    products = _products(result)
    rows = []
    for p in products[:10]:
        if not isinstance(p, dict):
            continue
        amount = _price_amount(p)
        if amount is None:
            continue
        name = clean_name(p.get('name'))
        product_id = str(p.get('xml_id') or p.get('id') or '').strip()
        if not name or not product_id:
            continue
        row = {'query': query, 'name': name, 'product_id': product_id,
                     'price_rub': float(amount), 'weight': str(p.get('weight') or ''),
                     'unit': str(p.get('unit') or ''), 'url': str(p.get('url') or ''),
                     'observed_at': datetime.now(timezone.utc).isoformat(),
                     'source': 'vkusvill_public_mcp', 'availability_confirmed': False}
        row.update(normalize_candidate(row))
        row.update(assess_match(query, name))
        rows.append(row)
    return rows


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS vkusvill_price_candidates (
        product_id TEXT PRIMARY KEY, query TEXT NOT NULL, name TEXT NOT NULL,
        price_rub REAL NOT NULL CHECK(price_rub>0), weight TEXT, unit TEXT,
        url TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL,
        availability_confirmed INTEGER NOT NULL DEFAULT 0)''')


def save(db, rows):
    init(db)
    for r in rows:
        db.execute('''INSERT INTO vkusvill_price_candidates
            (product_id,query,name,price_rub,weight,unit,url,observed_at,source,availability_confirmed)
            VALUES (:product_id,:query,:name,:price_rub,:weight,:unit,:url,:observed_at,:source,:availability_confirmed)
            ON CONFLICT(product_id) DO UPDATE SET query=excluded.query,name=excluded.name,
            price_rub=excluded.price_rub,weight=excluded.weight,unit=excluded.unit,
            url=excluded.url,observed_at=excluded.observed_at,source=excluded.source,
            availability_confirmed=excluded.availability_confirmed''', r)
    return len(rows)
