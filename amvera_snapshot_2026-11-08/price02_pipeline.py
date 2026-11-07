"""PRICE 02: bounded retailer probes and fail-closed staging of observations.

Only a confirmed canonical identity, comparable unit and explicit evidence can
ever be auto-verified. Unknown region is never silently called Moscow.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from price_b_catalog import seeds
from price_base_v1 import init_schema, upgrade_retailer_scope
from vkusvill_prices import search as vkusvill_search

MAX_QUERIES = 5
CATALOG = {name.casefold().replace('ё', 'е'): (code, name, category, unit)
           for code, name, category, unit in seeds()}


def _money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(amount) if amount.is_finite() and 0 < amount <= 1_000_000 else None


def _identity(query, title, unit, status):
    """Extremely conservative matching, never based on an LLM guess.

    PRICE 02 uses exact normalized title only: branded and packaged product
    names require later review even if their search words happen to match.
    """
    key = ' '.join(str(query).casefold().replace('ё', 'е').split())
    found = CATALOG.get(key)
    if not found or status not in ('likely_match', 'exact_match'):
        return None
    title_key = ' '.join(str(title).casefold().replace('ё', 'е').split())
    if title_key != key or found[3] != unit:
        return None
    return found


def stage(db, retailer, query, rows, *, region=None, region_confirmed=False, store_code=None):
    """Atomic savepoint even when invoked without an outer transaction."""
    db.execute('SAVEPOINT price02_stage')
    try:
        result = _stage_impl(db, retailer, query, rows, region=region,
                             region_confirmed=region_confirmed, store_code=store_code)
        db.execute('RELEASE SAVEPOINT price02_stage')
        return result
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT price02_stage')
        db.execute('RELEASE SAVEPOINT price02_stage')
        raise


def _stage_impl(db, retailer, query, rows, *, region=None, region_confirmed=False, store_code=None):
    """Stage raw external candidates without modifying existing production prices.

    Do not assert a region based solely on an IP address or store name.
    Caller must create tables on a dedicated connection before this function.
    """
    if retailer not in ('vkusvill', 'magnit'):
        raise ValueError('Unsupported retailer')
    if not isinstance(rows, list) or len(rows) > 10:
        raise ValueError('Maximum ten observations per request')
    if region_confirmed and not region:
        raise ValueError('Confirmed region must have evidence')
    region_key = str(region).strip() if region_confirmed else 'unknown'
    store_key = str(store_code) if store_code is not None else 'unknown'
    result = {'received': len(rows), 'staged': 0, 'verified': 0, 'review_required': 0, 'skipped': 0}
    for r in rows:
        if retailer == 'vkusvill':
            external_id, title = str(r.get('product_id') or ''), str(r.get('name') or '')
            price = _money(r.get('price_rub'))
            unit_price = _money(r.get('price_per_base_unit'))
            unit = r.get('base_unit')
            match = r.get('match_status', 'review_required')
            source = 'vkusvill_public_mcp'
            observed = r.get('observed_at')
        else:
            external_id, title = str(r.product_id or ''), str(r.title or '')
            price = _money(r.price_rub)
            unit_price = _money(r.unit_price_rub)
            unit = {'г': 'кг', 'мл': 'л'}.get(r.unit, r.unit)
            match = 'review_required'
            source = 'magnit_public_probe'
            observed = r.observed_at
        if not external_id or not title.strip() or not price or not isinstance(observed, str):
            result['skipped'] += 1
            continue
        # No auto-verification without exact canonical match and normalized units.
        identity = _identity(query, title, unit, match)
        # Exact identity may be auto-confirmed; the *price type* cannot.
        # Both APIs return a current price, not guaranteed regular/non-promo.
        verified_sku = identity is not None and unit_price is not None and bool(region_confirmed)
        code = identity[0] if identity else None
        existing = db.execute('SELECT id,match_status FROM pb_skus WHERE retailer=? AND external_id=?',
                              (retailer, external_id)).fetchone()
        if existing is None:
            sku_id = db.execute('''INSERT INTO pb_skus
                (retailer,external_id,title,product_code,match_status,match_evidence)
                VALUES(?,?,?,?,?,?)''', (retailer, external_id, title[:220], code,
                  'auto_verified' if verified_sku else 'review_required',
                  'exact_title_unit_region' if verified_sku else None)).lastrowid
        else:
            sku_id = existing[0]
            # Never downgrade a previously reviewed canonical identity or
            # overwrite it based on a new ambiguous search result.
            if existing[1] == 'review_required' and verified_sku:
                db.execute('''UPDATE pb_skus SET product_code=?,match_status='auto_verified',
                             match_evidence='exact_title_unit_region' WHERE id=?''', (code, sku_id))
        # Promotions are not inferred from price deltas or unknown old-price fields.
        status = 'unverified'
        try:
            db.execute('''INSERT OR IGNORE INTO pb_observations
                (sku_id,region,region_confirmed,store_code,region_key,store_key,
                 current_price_rub,unit_price_rub,base_unit,observed_at,source,review_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (sku_id,region if region_confirmed else None,
                  int(region_confirmed),store_code,region_key,store_key,price,unit_price,
                  unit if unit in ('кг','л','шт') else None,observed,source,status))
        except (ValueError, TypeError):
            result['skipped'] += 1
            continue
        result['staged'] += 1
        result['verified' if status == 'auto_verified' else 'review_required'] += 1
    return result


async def probe(query, *, magnit_store_code=None):
    """Bounded public read-only requests, isolated from Telegram event loop."""
    query = ' '.join(query.strip().split())
    if not 2 <= len(query) <= 80 or any(ord(c) < 32 for c in query):
        raise ValueError('Введите название продукта (2–80 символов)')
    calls = {'vkusvill': asyncio.to_thread(vkusvill_search, query)}
    if magnit_store_code:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', magnit_store_code):
            raise ValueError('Неверный код магазина Магнит')
        # Lazy import allows offline unit tests without the optional requests
        # dependency; Amvera installs it from requirements.txt.
        from price_sources_adapters import magnit_probe
        calls['magnit'] = asyncio.to_thread(magnit_probe, magnit_store_code, query)
    responses = await asyncio.gather(*calls.values(), return_exceptions=True)
    return {key: value for key, value in zip(calls, responses)}


def ensure(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_refresh_runs'").fetchone():
        init_schema(db)
    else:
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_skus'").fetchone()
        if ddl and "'lenta'" not in ddl[0]:
            if db.in_transaction:
                raise RuntimeError('Retailer schema upgrade must run before opening a transaction')
            upgrade_retailer_scope(db)
    db.executemany('INSERT OR IGNORE INTO pb_products(code,name,category,base_unit) VALUES (?,?,?,?)', seeds())


def stage_public_candidates(db, retailer, rows, *, source, limit=10):
    """Stage bounded public retailer results; never mark them as verified.

    For PRICE03–05 adapters, a raw retailer result is not evidence of a
    matching catalog product, trustworthy location, or confirmed shelf price.
    Caller must call ensure() first on a dedicated connection. This function
    never overwrites historical observations or source tables.
    """
    allowed = {'magnit', 'vkusvill', 'lenta', 'auchan', 'pyaterochka',
               'perekrestok', 'dixy', 'yarche'}
    if retailer not in allowed or not isinstance(source, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{3,80}', source):
        raise ValueError('Invalid retailer or source')
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError('Invalid limit')
    if not isinstance(rows, (tuple, list)) or len(rows) > limit:
        raise ValueError('Candidate batch exceeds explicit limit')

    def field(row, name):
        return row.get(name) if isinstance(row, dict) else getattr(row, name, None)

    report = {'received': len(rows), 'inserted_skus': 0,
              'inserted_observations': 0, 'duplicates': 0, 'skipped': 0,
              'review_required': 0}
    db.execute('SAVEPOINT price_public_stage')
    try:
        for row in rows:
            external_id = str(field(row, 'product_id') or '').strip()
            title = str(field(row, 'title') or field(row, 'name') or '').strip()
            price = _money(field(row, 'price_rub'))
            observed = field(row, 'observed_at')
            try:
                timestamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
                valid_date = timestamp.tzinfo is not None and timestamp.utcoffset() is not None
                valid_date = valid_date and timestamp <= datetime.now(timezone.utc) + timedelta(minutes=5)
            except (ValueError, AttributeError, TypeError):
                valid_date = False
            if (not external_id or len(external_id) > 200 or not title or
                    not price or not valid_date or any(ord(c) < 32 for c in external_id)):
                report['skipped'] += 1
                continue
            existing = db.execute('SELECT id FROM pb_skus WHERE retailer=? AND external_id=?',
                                  (retailer, external_id)).fetchone()
            if existing:
                sku_id = existing[0]
            else:
                sku_id = db.execute('''INSERT INTO pb_skus
                    (retailer,external_id,title,match_status) VALUES(?,?,?,'review_required')''',
                    (retailer, external_id, title[:220])).lastrowid
                report['inserted_skus'] += 1
            # Preserve source's claimed store ID, but *never* infer a region.
            store = field(row, 'store_id')
            store = str(store).strip()[:100] if store is not None and str(store).strip() else None
            cursor = db.execute('''INSERT OR IGNORE INTO pb_observations
                (sku_id,region,region_confirmed,store_code,region_key,store_key,
                 current_price_rub,observed_at,source,review_status)
                VALUES(?,NULL,0,?,'unknown',?,?,?,?,'unverified')''',
                (sku_id, store, store or 'unknown', price, observed, source))
            if cursor.rowcount:
                report['inserted_observations'] += 1
            else:
                report['duplicates'] += 1
            report['review_required'] += 1
        db.execute('RELEASE SAVEPOINT price_public_stage')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT price_public_stage')
        db.execute('RELEASE SAVEPOINT price_public_stage')
        raise
    return report


def rollback_if_open(db):
    """A failed BEGIN IMMEDIATE has nothing to roll back (SQLite BUSY)."""
    if db.in_transaction:
        db.rollback()


def import_legacy_candidates(db, *, dry_run=True, limit=2500):
    """Bounded, idempotent backfill of historical Magnit/VkusVill candidates.

    All rows stay UNVERIFIED and region UNKNOWN. The legacy table is not
    modified or removed. Call ensure(db) before importing on a dedicated
    connection; never execute this from a Telegram callback by default.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError('Invalid backfill limit')
    has_legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zb_candidates'").fetchone()
    report = {'scanned': 0, 'valid': 0, 'inserted_skus': 0,
              'inserted_observations': 0, 'skipped': 0, 'dry_run': bool(dry_run),
              'total_available': 0, 'truncated': False}
    if not has_legacy:
        return report
    report['total_available'] = db.execute('SELECT COUNT(*) FROM zb_candidates').fetchone()[0]
    report['truncated'] = report['total_available'] > limit
    if report['truncated'] and not dry_run:
        raise ValueError('Legacy candidate import exceeds limit; raise the explicit bound')
    rows = db.execute('''SELECT id,retailer,product_id,title,price_rub,
                               unit_price_rub,unit_basis,observed_at
                        FROM zb_candidates ORDER BY id LIMIT ?''', (limit,)).fetchall()
    prepared = []
    for legacy_id, retailer, external_id, title, price_text, unit_text, unit, observed in rows:
        report['scanned'] += 1
        if (retailer not in ('vkusvill', 'magnit') or not external_id or not title or
                not observed or len(str(external_id)) > 120):
            report['skipped'] += 1
            continue
        try:
            datetime.fromisoformat(str(observed).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            report['skipped'] += 1
            continue
        price = _money(price_text)
        if price is None:
            report['skipped'] += 1
            continue
        unit_price = _money(unit_text)
        if unit not in ('кг', 'л', 'шт'):
            unit, unit_price = None, None
        prepared.append((legacy_id, retailer, str(external_id), str(title)[:220],
                         price, unit_price, unit, str(observed)))
        report['valid'] += 1
    if dry_run or not prepared:
        return report
    # Keep the entire historical import atomic. Never modify legacy sources.
    db.execute('SAVEPOINT price02_legacy_backfill')
    try:
        for legacy_id, retailer, external_id, title, price, unit_price, unit, observed in prepared:
            sku_cursor = db.execute('''INSERT OR IGNORE INTO pb_skus
                         (retailer,external_id,title,match_status)
                         VALUES (?,?,?,'review_required')''',
                       (retailer, external_id, title))
            report['inserted_skus'] += sku_cursor.rowcount
            sku_id = db.execute('SELECT id FROM pb_skus WHERE retailer=? AND external_id=?',
                                (retailer, external_id)).fetchone()[0]
            cursor = db.execute('''INSERT OR IGNORE INTO pb_observations
                 (sku_id,region_confirmed,region_key,store_key,current_price_rub,
                  unit_price_rub,base_unit,observed_at,source,review_status)
                 VALUES (?,0,'unknown','unknown',?,?,?,?,?,'unverified')''',
                                (sku_id, price, unit_price, unit, observed,
                                 f'legacy_zb_candidates:{legacy_id}'))
            report['inserted_observations'] += cursor.rowcount
        db.execute('RELEASE SAVEPOINT price02_legacy_backfill')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT price02_legacy_backfill')
        db.execute('RELEASE SAVEPOINT price02_legacy_backfill')
        raise
    return report
