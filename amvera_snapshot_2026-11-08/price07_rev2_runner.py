"""PRICE07 REV2 source adapters and a shared scheduled/manual queue driver."""
from __future__ import annotations

import asyncio
import logging
import os

from database import connect_db
from price07_rev2_core import NETWORK_SOURCES, RESEARCH_SOURCES, claim, complete, status

log = logging.getLogger(__name__)
_lock = asyncio.Lock()


def auto_refresh_enabled():
    """Opt-in only until real retailer APIs and live deployment are accepted."""
    return os.getenv('PRICE07_REV2_AUTO_ENABLED', '').strip() == '1'


def _run_source(source, query):
    """Network operation. Called only after DB lease committed and closed."""
    if source == 'lenta':
        from lenta_public_source16 import search
        raw = os.getenv('PRICE07_LENTA_STORE_ID', '4515')
        if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
            raise ValueError('PRICE07_LENTA_STORE_ID required')
        return search(query, int(raw), limit=5)
    if source == 'vkusvill':
        from vkusvill_prices import search
        items = search(query, timeout=15)
        return [{'product_id': item['product_id'], 'title': item['name'],
                 'price_rub': item['price_rub'], 'observed_at': item['observed_at']}
                for item in items[:10]]
    if source == 'magnit':
        from price_sources_adapters import magnit_probe
        store = os.getenv('MAGNIT_STORE_CODE', '').strip()
        if not store:
            raise RuntimeError('MAGNIT_STORE_CODE is missing')
        result = magnit_probe(store, query)
        return [{'product_id': item.product_id, 'title': item.title,
                 'price_rub': item.price_rub, 'store_id': item.store_id,
                 'observed_at': item.observed_at} for item in result[:5]]
    if source == 'pyaterochka':
        from pyaterochka_sources_adapter import pyaterochka_probe
        store = os.getenv('PYATEROCHKA_SAP_CODE', '').strip()
        if not store:
            raise RuntimeError('PYATEROCHKA_SAP_CODE is missing')
        result = pyaterochka_probe(store, query)
        return [{'product_id': item.product_id, 'title': item.title,
                 'price_rub': item.price_rub, 'store_id': item.store_id,
                 'observed_at': item.observed_at} for item in result[:5]]
    if source == 'yarche':
        from yarche_public_probe import search_products
        return search_products(query, limit=5)
    raise ValueError('Unsupported retailer')


def _claim(source):
    with connect_db() as db:
        return claim(db, source)


def _settle(task, outcome, rows):
    with connect_db() as db:
        return complete(db, task, outcome=outcome, rows=rows)


async def one(source, *, fetch=None):
    """Single bounded operation with no network under a DB transaction."""
    if source == 'magnit' and fetch is None and not os.getenv('MAGNIT_STORE_CODE', '').strip():
        return {'source': source, 'status': 'missing_store_code'}
    if source == 'pyaterochka' and fetch is None and not os.getenv('PYATEROCHKA_SAP_CODE', '').strip():
        return {'source': source, 'status': 'missing_store_code'}
    task = await asyncio.to_thread(_claim, source)
    if not task:
        return {'source': source, 'status': 'idle'}
    try:
        rows = await asyncio.to_thread(fetch or _run_source, source, task['query'])
        outcome = 'success' if rows else 'empty'
    except Exception as exc:
        log.warning('PRICE07 source=%s problem=%s', source, type(exc).__name__)
        rows, outcome = [], 'error'
    result = await asyncio.to_thread(_settle, task, outcome, rows)
    return {'source': source, 'status': outcome, **result}


async def cycle(*, per_source=2, fetch=None):
    """Shared driver for scheduled loop and admin command; never overlap."""
    if not 1 <= per_source <= 5:
        raise ValueError('per-source limit')
    if _lock.locked():
        return {'status': 'busy', 'sources': []}
    async with _lock:
        outcomes = []
        # Each source is isolated: one unavailable retailer never blocks others.
        for source in NETWORK_SOURCES:
            for _ in range(per_source):
                item = await one(source, fetch=fetch)
                outcomes.append(item)
                if item['status'] in ('idle', 'missing_store_code', 'error'):
                    break
        return {'status': 'completed', 'sources': outcomes}


def report():
    with connect_db() as db:
        rows = status(db)
    for row in rows:
        if row['source'] in RESEARCH_SOURCES:
            row['status'] = 'adapter_unavailable'
        elif row['source'] == 'magnit' and not os.getenv('MAGNIT_STORE_CODE', '').strip():
            row['status'] = 'missing_store_code'
        elif row['source'] == 'pyaterochka' and not os.getenv('PYATEROCHKA_SAP_CODE', '').strip():
            row['status'] = 'missing_store_code'
    return rows


async def weekly_loop():
    """A small batch every hour throughout the week; resumes from SQLite."""
    while True:
        try:
            await cycle(per_source=4)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('PRICE07 REV2 cycle failed')
        await asyncio.sleep(3600)
