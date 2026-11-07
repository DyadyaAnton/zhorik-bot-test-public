"""Atomic per-request token accounting; unknown usage charged conservatively."""
import os
import uuid
from datetime import datetime, timezone
from database import connect_db

def _positive_env_int(name, default, minimum):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError, OverflowError):
        return default

MONTH_LIMIT = _positive_env_int('LLM_LAB_MONTH_LIMIT', 1000000, 1)
RESERVE = _positive_env_int('LLM_LAB_RESERVE', 2500, 160)

def _period():
    return datetime.now(timezone.utc).strftime('%Y-%m')

def _init(db):
    db.execute("""CREATE TABLE IF NOT EXISTS zm_llm_usage (
        period TEXT PRIMARY KEY, reserved INTEGER NOT NULL DEFAULT 0,
        spent INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
        uncertain INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS zm_llm_reservations (
        reservation_id TEXT PRIMARY KEY, period TEXT NOT NULL,
        amount INTEGER NOT NULL, settled INTEGER NOT NULL DEFAULT 0)""")

def reserve():
    period = _period()
    rid = uuid.uuid4().hex
    with connect_db() as db:
        _init(db)
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO zm_llm_usage(period) VALUES (?)', (period,))
        row = db.execute('SELECT reserved,spent FROM zm_llm_usage WHERE period=?', (period,)).fetchone()
        if row['reserved'] + row['spent'] + RESERVE > MONTH_LIMIT:
            db.execute('COMMIT')
            return None
        db.execute('INSERT INTO zm_llm_reservations(reservation_id,period,amount) VALUES (?,?,?)', (rid,period,RESERVE))
        db.execute('UPDATE zm_llm_usage SET reserved=reserved+? WHERE period=?', (RESERVE,period))
        db.execute('COMMIT')
    return rid

def settle(reservation_id, actual=None):
    if not isinstance(reservation_id,str) or not reservation_id:
        raise ValueError('Reservation id required')
    with connect_db() as db:
        _init(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT period,amount,settled FROM zm_llm_reservations WHERE reservation_id=?',(reservation_id,)).fetchone()
        if not row or row['settled']:
            db.execute('ROLLBACK')
            raise ValueError('Reservation missing or already settled')
        amount = row['amount']
        valid = type(actual) is int and 0 <= actual <= amount
        charged = actual if valid else amount
        db.execute('UPDATE zm_llm_usage SET reserved=reserved-?,spent=spent+?,calls=calls+1,uncertain=uncertain+? WHERE period=?',
                   (amount,charged,int(not valid),row['period']))
        db.execute('UPDATE zm_llm_reservations SET settled=1 WHERE reservation_id=?',(reservation_id,))
        db.execute('COMMIT')
    return charged, not valid


def recover_unsettled_on_startup():
    """Conservatively account for calls interrupted by the previous process.

    Only call during single-instance startup, BEFORE accepting new LLM calls.
    An uncertain call is billed at its reserved maximum. Idempotent and atomic.
    """
    with connect_db() as db:
        _init(db)
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT reservation_id,period,amount FROM zm_llm_reservations WHERE settled=0').fetchall()
        for row in rows:
            db.execute('INSERT OR IGNORE INTO zm_llm_usage(period) VALUES (?)', (row['period'],))
            db.execute('UPDATE zm_llm_usage SET reserved=reserved-?,spent=spent+?,calls=calls+1,uncertain=uncertain+1 WHERE period=?',
                       (row['amount'], row['amount'], row['period']))
            db.execute('UPDATE zm_llm_reservations SET settled=1 WHERE reservation_id=?', (row['reservation_id'],))
        db.execute('COMMIT')
    return len(rows)

def stats():
    period = _period()
    with connect_db() as db:
        _init(db)
        row = db.execute('SELECT * FROM zm_llm_usage WHERE period=?',(period,)).fetchone()
    return {'period':period,'spent':row['spent'] if row else 0,
            'reserved':row['reserved'] if row else 0,'calls':row['calls'] if row else 0,
            'uncertain':row['uncertain'] if row else 0,'limit':MONTH_LIMIT}
