"""Cancellation-safe bridge between asyncio and durable LLM accounting."""
import asyncio
import logging
import sqlite3
import threading
import time

from llm_usage import reserve, settle

log = logging.getLogger(__name__)


def _settle_with_retry(rid, tokens=None):
    for attempt in range(5):
        try:
            return settle(rid, tokens)
        except sqlite3.OperationalError:
            if attempt == 4:
                log.exception('LLM settlement deferred to startup reconciliation')
                raise
            time.sleep(0.15 * (attempt + 1))


async def settle_reliably(rid, tokens=None):
    return await asyncio.to_thread(_settle_with_retry, rid, tokens)


async def reserve_cancellation_safe():
    """The worker owns a reservation until the awaiting coroutine receives it.

    On cancellation the worker charges orphaned reservations conservatively.
    If SQLite is unavailable, startup recovery handles the persisted reservation.
    """
    lock = threading.Lock()
    state = {'cancelled': False, 'rid': None}

    def worker():
        rid = reserve()
        with lock:
            cancelled = state['cancelled']
            if not cancelled:
                state['rid'] = rid
        if cancelled and rid:
            try:
                _settle_with_retry(rid, None)
            except sqlite3.OperationalError:
                pass  # Durable startup reconciliation is the last resort.
        return rid

    try:
        return await asyncio.to_thread(worker)
    except asyncio.CancelledError:
        with lock:
            state['cancelled'] = True
            rid = state['rid']
            state['rid'] = None
        if rid:
            try:
                await asyncio.shield(settle_reliably(rid, None))
            except sqlite3.OperationalError:
                pass
        raise
