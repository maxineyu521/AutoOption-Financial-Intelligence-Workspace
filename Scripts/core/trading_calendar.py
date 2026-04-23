"""
Scripts/core/trading_calendar.py

Minimal dependency-free trading-day calendar.

WHY THIS MODULE EXISTS
----------------------
The production router-e2e run on 2026-04-22 exposed a *semantic* mismatch
between the way the RAG pipeline asks "yesterday" and the way the data
layers interpret it (see docs/test/2026-04-22/router_e2e_deep_analysis.md
lines 120–133). Concretely:

  - `collect_data_state.json:options_daily` already records the most recent
    successful ingestion day (e.g. 2026-04-21 Tue). This is the anchor.
  - Semantic "yesterday" from the LLM was being translated into
    `anchor − 2 calendar days = Sun 2026-04-19`, which is a weekend gap —
    the Parquet has no row there, so the handler returned either zero
    values or (worse) a different row across revisions.
  - What a financial user actually means by "yesterday" is "the previous
    trading day against the most recent ingestion date". On a Tuesday
    anchor that is Monday; on a Monday anchor that is Friday.

Scope
-----
This module only knows about the US weekend pattern (Sat/Sun off).
US federal / NYSE holidays are NOT encoded here to keep the module
dependency-free and stable across branches. Upgrading to
`pandas_market_calendars` or `exchange_calendars.NYSE` is a drop-in
replacement: provide `is_business_day(d)` returning False for any
observed NYSE holiday and all downstream consumers (time_adapter,
sql_tools) keep working.

Public API
----------
    is_business_day(d)              -> bool
    previous_business_day(d)        -> date
    next_business_day(d)            -> date
    n_business_days_back(d, n)      -> date
    business_days_between(a, b)     -> int   (inclusive of a, exclusive of b)
    clamp_to_business_day(d, mode)  -> date  (mode='prev' | 'next')
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

__all__ = [
    "is_business_day",
    "previous_business_day",
    "next_business_day",
    "n_business_days_back",
    "business_days_between",
    "clamp_to_business_day",
]


# ---------------------------------------------------------------------------
# Core predicates
# ---------------------------------------------------------------------------


def is_business_day(d: date) -> bool:
    """Return True iff `d` is Mon–Fri.

    Holidays are intentionally not handled — swap this single function for
    an NYSE-aware implementation (e.g. `exchange_calendars.NYSE.is_session`)
    if the surrounding data pipeline starts tracking holidays.
    """
    # Python weekday(): Mon=0 … Sun=6. Weekdays 0–4 are trading days.
    return d.weekday() < 5


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def previous_business_day(d: date) -> date:
    """Most recent business day STRICTLY before `d`.

    - previous_business_day(Tue) == Mon
    - previous_business_day(Mon) == Fri   (weekend skip)
    - previous_business_day(Sun) == Fri
    - previous_business_day(Sat) == Fri
    """
    cur = d - timedelta(days=1)
    while not is_business_day(cur):
        cur -= timedelta(days=1)
    return cur


def next_business_day(d: date) -> date:
    """Earliest business day STRICTLY after `d`. Mirror of `previous_business_day`."""
    cur = d + timedelta(days=1)
    while not is_business_day(cur):
        cur += timedelta(days=1)
    return cur


def n_business_days_back(d: date, n: int) -> date:
    """Walk `n` business days backwards from `d`.

    If `n == 0`, returns `d` clamped to the previous business day when `d`
    itself is a weekend (so the result is always a trading day).
    For `n > 0`, the walk excludes `d` on step 1 (matches "n-th previous
    trading day" semantics commonly used in financial APIs).
    """
    if n < 0:
        raise ValueError("n_business_days_back requires n >= 0")
    if n == 0:
        return clamp_to_business_day(d, mode="prev")
    cur = d
    for _ in range(n):
        cur = previous_business_day(cur)
    return cur


def business_days_between(a: date, b: date) -> int:
    """Count trading days in the half-open interval [a, b).

    Used by handlers that need to report "N trading days back" in audit logs
    even when the calendar span crosses weekends. Guaranteed to be ≤ abs(b-a).
    """
    if a == b:
        return 0
    step = 1 if b > a else -1
    cur = a
    count = 0
    while cur != b:
        if is_business_day(cur):
            count += 1
        cur += timedelta(days=step)
    return count if step == 1 else -count


def clamp_to_business_day(
    d: date,
    mode: Literal["prev", "next"] = "prev",
) -> date:
    """Snap a weekend date onto the adjacent business day.

    - mode='prev' (default): Sat/Sun → previous Fri. Mon–Fri → unchanged.
    - mode='next':           Sat/Sun → next Mon.     Mon–Fri → unchanged.

    Use 'prev' when reading historical data (end-of-window snap) and 'next'
    when scheduling forward-looking deliveries.
    """
    if is_business_day(d):
        return d
    if mode == "prev":
        return previous_business_day(d + timedelta(days=1))
    if mode == "next":
        return next_business_day(d - timedelta(days=1))
    raise ValueError(f"Unknown clamp mode: {mode}")
