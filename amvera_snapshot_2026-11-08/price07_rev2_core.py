"""PRICE07 REV2: incremental multi-source price queue and discovery registry.

No I/O to retailer endpoints under SQLite transactions. All market prices are
unverified until region, SKU identity and unit conversion have evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

NETWORK_SOURCES = ('lenta', 'vkusvill', 'magnit', 'pyaterochka', 'yarche')
RESEARCH_SOURCES = ('perekrestok', 'dixy')
ALL_SOURCES = NETWORK_SOURCES + RESEARCH_SOURCES

SCHEMA = '''
CREATE TABLE IF NOT EXISTS p7_source_progress (
  source TEXT PRIMARY KEY,
  week TEXT NOT NULL DEFAULT '',
  last_code TEXT NOT NULL DEFAULT '',
  completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
  lease TEXT,
  lease_at TEXT,
  processed INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  retry_after TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS p7_discovered_products (
  code TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  title_fingerprint TEXT NOT NULL UNIQUE,
  review_status TEXT NOT NULL DEFAULT 'unverified',
  kbju_verified INTEGER NOT NULL DEFAULT 0 CHECK(kbju_verified IN (0,1)),
  allergens_verified INTEGER NOT NULL DEFAULT 0 CHECK(allergens_verified IN (0,1)),
  first_retailer TEXT NOT NULL,
  first_sku_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK(review_status IN ('unverified','verified','rejected'))
);
CREATE TABLE IF NOT EXISTS p7_attempts (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  week TEXT NOT NULL,
  product_code TEXT NOT NULL,
  query TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('success','empty','error')),
  observations INTEGER NOT NULL,
  observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS p7_attempts_week_source ON p7_attempts(week,source);
'''


def init(db):
    """Additive-only schema; caller must take a durable backup first."""
    # PRICE-02 owns pb_products / pb_skus / pb_observations. They must exist
    # before PRICE-07 starts querying or staging, including on a fresh DB.
    from price02_pipeline import ensure
    ensure(db)
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute('PRAGMA table_info(p7_source_progress)')}
    if 'consecutive_errors' not in columns:
        db.execute('ALTER TABLE p7_source_progress ADD COLUMN consecutive_errors INTEGER NOT NULL DEFAULT 0')
    if 'retry_after' not in columns:
        db.execute('ALTER TABLE p7_source_progress ADD COLUMN retry_after TEXT')
    db.executemany('''INSERT OR IGNORE INTO p7_source_progress(source) VALUES(?)''',
                   ((src,) for src in ALL_SOURCES))


def week_key(now=None):
    year, week, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f'{year}-W{week:02}'


def _key(value):
    return ' '.join(str(value or '').strip().casefold().replace('ё', 'е').split())


def query_catalog(db, after='', limit=1):
    """Stable query order; unreviewed SKU titles cannot cause search fanout."""
    if not 1 <= limit <= 20:
        raise ValueError('query limit')
    audited = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('p7_nutrition_review','p7_nutrition_review_audit')")}
    if len(audited)==2:
        return [tuple(r) for r in db.execute('''
            SELECT code,name FROM pb_products WHERE code>?
            UNION ALL SELECT d.code,d.canonical_name FROM p7_discovered_products d
              JOIN p7_nutrition_review n ON n.product_code=d.code
              WHERE d.code>? AND d.review_status='verified'
                AND d.kbju_verified=1 AND d.allergens_verified=1
                AND EXISTS (SELECT 1 FROM p7_nutrition_review_audit a
                            WHERE a.product_code=d.code AND a.actor_id=n.actor_id
                              AND a.event='owner_verified')
            ORDER BY code LIMIT ?''', (after, after, limit))]
    return [tuple(r) for r in db.execute('''SELECT code,name FROM pb_products
        WHERE code>? ORDER BY code LIMIT ?''',(after,limit))]


def _expired(timestamp, now):
    if not timestamp:
        return True
    try:
        dt = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        return now - dt > timedelta(minutes=30)
    except ValueError:
        return True


def claim(db, source, *, now=None):
    """Acquire a durable exclusive lease for one source/query."""
    if source not in NETWORK_SOURCES:
        raise ValueError('Unavailable source')
    current = now or datetime.now(timezone.utc)
    key = week_key(current)
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('''SELECT week,last_code,completed,lease,lease_at,processed,found,errors,
            consecutive_errors,retry_after
            FROM p7_source_progress WHERE source=?''', (source,)).fetchone()
        if not row:
            raise RuntimeError('PRICE07 schema not initialized')
        (previous_week, last_code, done, lease, lease_at, processed, found,
         errors, failure_streak, retry_after) = row
        if previous_week != key:
            last_code, done, lease, lease_at = '', 0, None, None
            processed = found = errors = 0
            failure_streak, retry_after = 0, None
        if retry_after and previous_week == key:
            try:
                retry_dt = datetime.fromisoformat(retry_after)
                if retry_dt.tzinfo is None:
                    retry_dt = retry_dt.replace(tzinfo=timezone.utc)
                if current < retry_dt:
                    db.commit()
                    return None
            except ValueError:
                pass
        if lease and not _expired(lease_at, current):
            db.commit()
            return None
        if done:
            db.commit()
            return None
        candidates = query_catalog(db, last_code)
        if not candidates:
            db.execute('''UPDATE p7_source_progress SET week=?, completed=1,
                lease=NULL,lease_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE source=?''',
                (key, source))
            db.commit()
            return None
        token = secrets.token_hex(12)
        code, query = candidates[0]
        db.execute('''UPDATE p7_source_progress SET week=?, last_code=?,completed=0,
            lease=?,lease_at=?,processed=?,found=?,errors=?,consecutive_errors=?,retry_after=?,updated_at=CURRENT_TIMESTAMP
            WHERE source=?''',
            (key, last_code, token, current.replace(tzinfo=None).isoformat(),
             processed, found, errors, failure_streak, retry_after, source))
        db.commit()
        return {'week': key, 'source': source, 'code': code, 'query': query[:80],
                'token': token}
    except BaseException:
        db.rollback()
        raise


def _discovered(db, retailer, rows):
    """Add new product candidates automatically, never approve meal eligibility."""
    new = 0
    existing = {_key(r[0]) for r in db.execute('SELECT name FROM pb_products')}
    for row in rows:
        title = str(row.get('title') or row.get('name') or '').strip()
        external = str(row.get('product_id') or '').strip()
        if not title or not external or _key(title) in existing:
            continue
        fingerprint = hashlib.sha256(_key(title).encode('utf-8')).hexdigest()
        code = 'NP' + fingerprint[:20].upper()
        cursor = db.execute('''INSERT OR IGNORE INTO p7_discovered_products
            (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
            VALUES(?,?,?,?,?)''', (code, title[:180], fingerprint, retailer, external[:200]))
        new += cursor.rowcount
    return new


def complete(db, task, *, outcome, rows=(), source_name='price07_rev2'):
    """Atomic stage+cursor. Reject stale completion without modifying prices."""
    if outcome not in ('success', 'empty', 'error'):
        raise ValueError('outcome')
    if not isinstance(rows, (tuple, list)) or len(rows) > 10:
        raise ValueError('row bound')
    if outcome != 'success' and rows:
        raise ValueError('failed task cannot stage rows')
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT week,lease FROM p7_source_progress WHERE source=?',
                         (task['source'],)).fetchone()
        if not row or tuple(row) != (task['week'], task['token']):
            db.rollback()
            return {'stale': True}
        staged = 0
        discovered = 0
        if outcome == 'success':
            from price02_pipeline import stage_public_candidates
            from price07_rev2_normalize import normalized_unit_price
            result = stage_public_candidates(db, task['source'], rows,
                                             source=source_name, limit=10)
            staged = result['inserted_observations']
            accepted = []
            for candidate in rows:
                valid = db.execute('''SELECT 1 FROM pb_observations o
                    JOIN pb_skus s ON s.id=o.sku_id
                    WHERE s.retailer=? AND s.external_id=?
                      AND o.source=? AND o.observed_at=? LIMIT 1''',
                    (task['source'], str(candidate.get('product_id') or ''),
                     source_name, candidate.get('observed_at'))).fetchone()
                if valid:
                    accepted.append(candidate)
            discovered = _discovered(db, task['source'], accepted)
            for candidate in accepted:
                normalized = normalized_unit_price(
                    candidate.get('title') or candidate.get('name'), candidate.get('price_rub'))
                if not normalized:
                    continue
                db.execute('''UPDATE pb_observations SET unit_price_rub=?,base_unit=?
                    WHERE review_status='unverified' AND base_unit IS NULL
                      AND source=? AND observed_at=?
                      AND sku_id IN (SELECT id FROM pb_skus
                         WHERE retailer=? AND external_id=?)''',
                    (normalized['unit_price_rub'], normalized['base_unit'],
                     source_name, candidate.get('observed_at'), task['source'],
                     str(candidate.get('product_id') or '')))
        db.execute('''INSERT INTO p7_attempts(source,week,product_code,query,outcome,observations)
            VALUES(?,?,?,?,?,?)''', (task['source'],task['week'],task['code'],task['query'],outcome,staged))
        # A temporary HTTP failure must not silently drop a product for
        # the entire week. Release lease; next scheduled batch retries.
        failure = outcome == 'error'
        streak = db.execute('''SELECT consecutive_errors FROM p7_source_progress
            WHERE source=?''', (task['source'],)).fetchone()[0]
        delay = min(6*3600, 15*60*(2**min(streak, 6))) if failure else 0
        retry_at = (datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat() if failure else None
        db.execute('''UPDATE p7_source_progress SET last_code=CASE WHEN ?='error'
            THEN last_code ELSE ? END, lease=NULL,
            lease_at=NULL, processed=processed+1, found=found+?,
            errors=errors+?,consecutive_errors=?,retry_after=?,
            updated_at=CURRENT_TIMESTAMP WHERE source=?''',
            (outcome, task['code'], staged, int(failure), streak+1 if failure else 0,
             retry_at, task['source']))
        db.commit()
        return {'stale': False, 'observations': staged, 'discovered': discovered}
    except BaseException:
        db.rollback()
        raise


def status(db):
    rows = db.execute('''SELECT source,week,last_code,completed,processed,found,errors,
                         consecutive_errors,retry_after
                         FROM p7_source_progress ORDER BY source''')
    return [dict(zip(('source','week','cursor','completed','processed','found','errors',
                      'consecutive_errors','retry_after'), row))
            for row in rows]


def eligible_for_menu(db, code):
    # Legacy boolean flags cannot prove nutritional evidence on their own.
    has_review=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='p7_nutrition_review'").fetchone()
    if not has_review:
        return False
    row = db.execute('''SELECT kbju_verified,allergens_verified,review_status
        FROM p7_discovered_products WHERE code=?''', (code,)).fetchone()
    if not (row and row[0] and row[1] and row[2] == 'verified'):
        return False
    return db.execute('''SELECT 1 FROM p7_nutrition_review r
        JOIN p7_nutrition_review_audit a ON a.product_code=r.product_code
          AND a.event='owner_verified' AND a.actor_id=r.actor_id
        WHERE r.product_code=? AND r.kcal_100g IS NOT NULL
        AND r.protein_100g IS NOT NULL AND r.fat_100g IS NOT NULL
        AND r.carbs_100g IS NOT NULL AND r.allergens_json IS NOT NULL
        LIMIT 1''',(code,)).fetchone() is not None
