"""PRICE-07: bounded weekly Lenta candidate collection, never verified pricing.

The SQLite lease is acquired before any network calls; each query's network
work happens outside transactions. A failed job may be retried explicitly.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from lenta_public_source16 import search
from price02_pipeline import stage_public_candidates

CATEGORY = 'price07_weekly_lenta'
DEFAULT_QUERIES = ('молоко', 'гречка', 'рис', 'яйца', 'курица', 'картофель',
                   'морковь', 'яблоки', 'кефир', 'творог', 'хлеб', 'масло')
MAX_QUERIES = 30


def week_key(now=None):
    now = now or datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f'{year}-W{week:02d}'


def configured_store():
    raw = os.getenv('PRICE07_LENTA_STORE_ID', '4515')
    if not raw.isascii() or not raw.isdecimal() or not 0 < int(raw) < 10**9:
        raise ValueError('PRICE07_LENTA_STORE_ID must be a positive store ID')
    return int(raw)


def _runs(db, key):
    return db.execute('''SELECT id,status,started_at,finished_at FROM pb_refresh_runs
        WHERE category=? AND retailer='lenta' AND json_extract(report_json,'$.week')=?
        ORDER BY id DESC LIMIT 1''', (CATEGORY, key)).fetchone()


def reserve(db, key, *, trigger='scheduled'):
    """Reserve one job per week; never start a second concurrent job."""
    if trigger not in ('scheduled', 'manual'):
        raise ValueError('trigger')
    required = {'pb_skus', 'pb_observations', 'pb_refresh_runs'}
    actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= actual:
        raise RuntimeError('PRICE07 requires existing price schema; automatic migrations forbidden')
    db.execute('BEGIN IMMEDIATE')
    try:
        prior = _runs(db, key)
        if prior:
            if prior[1] == 'completed':
                db.commit()
                return None
            age = datetime.now(timezone.utc) - datetime.fromisoformat(prior[2]).replace(tzinfo=timezone.utc)
            attempts = db.execute('''SELECT COUNT(*) FROM pb_refresh_runs
                WHERE category=? AND retailer='lenta' AND json_extract(report_json,'$.week')=?''',
                (CATEGORY, key)).fetchone()[0]
            if attempts >= 3 or (prior[1] == 'started' and age < timedelta(hours=1)) or (
                    trigger == 'scheduled' and age < timedelta(hours=1)):
                db.commit()
                return None
        run_id = db.execute('''INSERT INTO pb_refresh_runs
            (trigger_type,retailer,category,status,report_json)
            VALUES(?,'lenta',?,'started',?)''',
            (trigger, CATEGORY, json.dumps({'week': key}, ensure_ascii=False))).lastrowid
        db.commit()
        return run_id
    except BaseException:
        db.rollback()
        raise


def run(db_factory, *, trigger='scheduled', now=None, fetch=None,
        queries=DEFAULT_QUERIES, store_id=None):
    """Run one bounded weekly batch. All source results remain unverified."""
    if not isinstance(queries, (tuple, list)) or not 1 <= len(queries) <= MAX_QUERIES:
        raise ValueError('query count')
    if any(not isinstance(q, str) or not q.strip() or len(q) > 80 for q in queries):
        raise ValueError('query')
    store_id = configured_store() if store_id is None else store_id
    if type(store_id) is not int or store_id <= 0:
        raise ValueError('store ID')
    key = week_key(now)
    with db_factory() as db:
        run_id = reserve(db, key, trigger=trigger)
    if run_id is None:
        return {'status': 'already_run', 'week': key}
    report = {'week': key, 'store_id': store_id, 'queries': len(queries),
              'success': 0, 'failed': 0, 'empty': 0, 'new_skus': 0,
              'new_observations': 0, 'review_required': 0}
    try:
        for query in queries:
            try:
                # Network access is outside all SQLite transactions.
                rows = search(query, store_id, fetch=fetch)
                if not rows:
                    report['empty'] += 1
                    continue
                with db_factory() as db:
                    outcome = stage_public_candidates(
                        db, 'lenta', rows, source='lenta_public_price07', limit=5)
                report['success'] += 1
                report['new_skus'] += outcome['inserted_skus']
                report['new_observations'] += outcome['inserted_observations']
                report['review_required'] += outcome['review_required']
            except Exception:
                report['failed'] += 1
        status = ('completed' if not report['failed'] and not report['empty'] else
                  'partial' if report['success'] else 'failed')
    except BaseException:
        status = 'failed'
        raise
    finally:
        with db_factory() as db:
            db.execute('''UPDATE pb_refresh_runs SET status=?,finished_at=CURRENT_TIMESTAMP,
                report_json=? WHERE id=? AND status='started' ''',
                (locals().get('status', 'failed'), json.dumps(report, ensure_ascii=False), run_id))
    return {'status': status, **report}


def latest(db):
    row = db.execute('''SELECT status,finished_at,report_json FROM pb_refresh_runs
        WHERE retailer='lenta' AND category=? ORDER BY id DESC LIMIT 1''',
        (CATEGORY,)).fetchone()
    if not row:
        return None
    return {'status': row[0], 'finished_at': row[1], **json.loads(row[2])}
