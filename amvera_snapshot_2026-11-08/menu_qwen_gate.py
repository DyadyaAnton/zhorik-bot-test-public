"""Bounded Qwen editorial review of a validated draft; never trusts LLM arithmetic.

This module does not alter recipes, prices, allergies, or the canonical database.
"""
import logging
import asyncio
import time

from llm_usage_async import reserve_cancellation_safe, settle_reliably
from llm_service import review_menu_json
from menu_generator import DAYS, MEALS

log = logging.getLogger(__name__)


def outline(menu):
    lines = []
    for day in DAYS:
        meals = menu['menu'][day]
        for meal in MEALS:
            names = [dish['name'][:90] for dish in meals[meal][:6]]
            lines.append(f'{day} / {meal}: '+', '.join(names))
    result = '\n'.join(lines)
    return result[:3900]


def needs_revision(review):
    """Only actual editorial issues (not generic suggestions) trigger a retry."""
    issues = review.get('issues', [])
    return bool(issues)


async def review(menu, prefs=None):
    """Return (status, issues). Failure is explicit; no fabricated Qwen approval.

    Reservations are settled even when network fails/coroutine is cancelled.
    """
    from perf_diagnostics import mark
    from menu_editorial_policy import validated_allergies
    # Fail closed before any paid reservation, including on this optional path.
    allergies = (prefs or {}).get('allergy', ())
    validated_allergies(allergies)
    started=time.perf_counter()
    # SQLite reservation can stall under contention; keep it off the event
    # loop so unrelated Telegram callbacks remain responsive.
    rid = await reserve_cancellation_safe()
    mark('qwen_reserve',time.perf_counter()-started)
    if not rid:
        return 'unavailable_limit', []
    tokens = None
    try:
        started=time.perf_counter()
        from menu_editorial_policy import screen_review
        result, tokens = await review_menu_json(outline(menu), allergies=allergies)
        result, rejected = screen_review(result, allergies)
        if rejected:
            log.warning('Qwen rejected_by_local_check: %d suggestion(s)', rejected)
        mark('qwen_network_parse',time.perf_counter()-started)
        return 'reviewed', result['issues']
    except Exception as exc:
        log.warning('Qwen menu review unavailable: %s', type(exc).__name__)
        return 'unavailable_error', []
    finally:
        # Reuse the original usage value on retry. Charging zero after a
        # failed commit would undercount actual LLM usage; retry only when the
        # database is temporarily busy. A persistent failure is not hidden.
        settle_started=time.perf_counter()
        await asyncio.shield(settle_reliably(rid, tokens))
        mark('qwen_settle',time.perf_counter()-settle_started)
