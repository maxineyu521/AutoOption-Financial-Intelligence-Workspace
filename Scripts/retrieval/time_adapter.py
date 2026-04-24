"""
    Different data sources use different time types and granularities:

        Gold.News   : `unified_timestamp` / `publish_timestamp`  (Unix seconds, event-level)
        Gold.SEC    : `unified_timestamp` / `filed_at` / `transaction_date`
                      (Unix seconds + ISO-8601 strings, event-level)
        Gold.GPR    : `unified_timestamp` / `publish_timestamp`  (Unix seconds, MONTHLY)
        Silver.Options : `snapshot_date`          (VARCHAR ISO date, trading-day)
        Silver.Macro   : `observation_date` / `retrieval_date`  (VARCHAR ISO date, daily)
        Silver.GPR     : `date` / `month`         (TIMESTAMP_NS, MONTHLY)

    Applying a single (start_ts, end_ts) window to all of them is wrong:
      - "yesterday" against a MONTHLY source = 0 rows (should widen to the
        current month so GPR always returns its most recent observation).
      - "yesterday" against a DAILY source on a Monday = 0 rows (weekend
        gap; we must widen at least to Friday).
      - A naive Unix-seconds filter against SEC returns 0 because legacy
        SEC payloads only carry ISO-8601 strings, not an epoch key.

    This adapter compiles **one `TimePredicate` per source**, pre-widened
    for the source's granularity, so every downstream retriever receives
    semantics that match its physical schema without branching logic
    sprinkled across the codebase.

Design:
  - Stateless.
  - No Qdrant / DuckDB dependency (this is the *contract*, not the
    execution). Both layers consume a `TimePredicate` but decide how
    to bind it to their physical APIs.
  - Fully auditable: every predicate serialises via `.to_dict()` for the
    retriever audit trail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from Scripts.core.trading_calendar import is_business_day, previous_business_day
from Scripts.retrieval.schema import TIME_WINDOW_DAYS, TimeWindow, time_window_to_days

logger = logging.getLogger(__name__)

__all__ = [
    "TimeGranularity",
    "SourceTimeKey",
    "SourceTimeSpec",
    "TimePredicate",
    "SOURCE_SPECS",
    "compile_predicate",
    "compile_all",
    "predicates_from_serialised",
    "union_epoch_range",
]


# Semantic labels for which DAILY sources should use business-day semantics
# rather than calendar-day widening. Matches the user-visible TimeWindow
# values so routing is legible in logs.
_DAILY_BUSDAY_LABELS: set = {"today", "yesterday"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TimeGranularity(str, Enum):
    """Physical cadence of a data source.

    Determines how a sub-granularity semantic window (e.g. "yesterday")
    must be widened so the source has any chance of returning data.
    """

    EVENT = "event"      # News, SEC — sub-daily individual timestamps.
    DAILY = "daily"      # Options snapshots, macro daily series.
    MONTHLY = "monthly"  # GPR, FRED monthly macro series.


class SourceTimeKey(str, Enum):
    """Logical handle used by upstream callers to reference a data source.

    Strings chosen to be stable over refactors so audit logs stay diffable.
    """

    GOLD_NEWS      = "gold.news"
    GOLD_SEC       = "gold.sec"
    GOLD_GPR       = "gold.gpr"
    SILVER_OPTIONS = "silver.options"
    SILVER_MACRO   = "silver.macro"
    SILVER_GPR     = "silver.gpr"


@dataclass(frozen=True)
class SourceTimeSpec:
    """Physical time contract for one data source.

    Attributes
    ----------
    granularity
        Event / Daily / Monthly — drives widening policy.
    time_keys
        Ordered tuple of physical payload/column names that carry a parseable
        instant. Retrievers should attempt them in order (most specific first).
        For Qdrant this is the payload key; for Parquet this is the column.
    key_units
        Physical type, aligned 1:1 with `time_keys`. One of:
          - "epoch_s"       : Unix seconds (int/float)
          - "epoch_ms"      : Unix milliseconds (int/float)
          - "iso_date"      : 'YYYY-MM-DD'
          - "iso_datetime"  : ISO-8601 (may carry timezone offset or 'Z')
          - "ts_ns"         : pandas/arrow TIMESTAMP_NS (nanoseconds since epoch)
    min_lookback_days
        Floor on the widening when the semantic window is narrower than the
        source's cadence. MONTHLY sources take 35 days so GPR's first-of-
        month row is always inside the window. DAILY sources take 3 days to
        survive weekend gaps.
    """

    granularity: TimeGranularity
    time_keys: Tuple[str, ...]
    key_units: Tuple[str, ...]
    min_lookback_days: int = 1


# Authoritative registry. Keep this in sync with
# docs/Data_source_docs/Time_Schema_Audit.md.
SOURCE_SPECS: Dict[SourceTimeKey, SourceTimeSpec] = {
    SourceTimeKey.GOLD_NEWS: SourceTimeSpec(
        granularity=TimeGranularity.EVENT,
        # `unified_timestamp` is the post-2026-04-22 standard; `publish_timestamp`
        # is the legacy key that news_scraper has always written. Both carry
        # Unix seconds, so Qdrant `Range` binds against either transparently.
        time_keys=("unified_timestamp", "publish_timestamp"),
        key_units=("epoch_s", "epoch_s"),
        min_lookback_days=1,
    ),
    SourceTimeKey.GOLD_SEC: SourceTimeSpec(
        granularity=TimeGranularity.EVENT,
        # Legacy SEC payloads only carry ISO strings. Post-2026-04-22
        # ingestion stamps a numeric `unified_timestamp` derived from
        # `transaction_date` → the Qdrant numeric Range now binds.
        time_keys=("unified_timestamp", "filed_at", "transaction_date"),
        key_units=("epoch_s", "iso_datetime", "iso_date"),
        # SEC filings land in bursts; a 3-day floor keeps weekend-edge
        # "yesterday" queries non-empty without widening the semantic intent.
        min_lookback_days=3,
    ),
    SourceTimeKey.GOLD_GPR: SourceTimeSpec(
        granularity=TimeGranularity.MONTHLY,
        time_keys=("unified_timestamp", "publish_timestamp"),
        key_units=("epoch_s", "epoch_s"),
        # 35 days = "always catches the latest first-of-month anchor",
        # regardless of which calendar day the user queried on.
        min_lookback_days=35,
    ),
    SourceTimeKey.SILVER_OPTIONS: SourceTimeSpec(
        granularity=TimeGranularity.DAILY,
        time_keys=("snapshot_date",),
        key_units=("iso_date",),
        min_lookback_days=3,
    ),
    SourceTimeKey.SILVER_MACRO: SourceTimeSpec(
        granularity=TimeGranularity.DAILY,
        # `observation_date` is the authoritative series date; `retrieval_date`
        # is the ingestion day — kept second so callers can fall back.
        time_keys=("observation_date", "retrieval_date"),
        key_units=("iso_date", "iso_date"),
        min_lookback_days=3,
    ),
    SourceTimeKey.SILVER_GPR: SourceTimeSpec(
        granularity=TimeGranularity.MONTHLY,
        time_keys=("date", "month"),
        key_units=("ts_ns", "ts_ns"),
        min_lookback_days=35,
    ),
}


# ---------------------------------------------------------------------------
# Compiled predicate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimePredicate:
    """A physically-executable time window, pre-aligned for one source.

    A predicate is the *compiled* artefact of (semantic TimeWindow + anchor +
    source spec). Retrievers must never re-derive these values: they should
    consume `start_epoch_s` / `end_epoch_s` for Qdrant numeric Ranges, or
    `start_date` / `end_date` for Parquet/DuckDB BETWEEN filters.
    """

    source: SourceTimeKey
    label: str
    granularity: TimeGranularity
    start_date: date
    end_date: date
    start_epoch_s: int
    end_epoch_s: int
    window_days: int
    widened: bool = False
    widen_reason: Optional[str] = None
    # The physical keys the retriever should build range filters on. Copied
    # off `SourceTimeSpec` at compile time so a consumer can operate on just
    # the predicate, without another registry lookup.
    time_keys: Tuple[str, ...] = field(default_factory=tuple)
    key_units: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.value,
            "label": self.label,
            "granularity": self.granularity.value,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "start_epoch_s": self.start_epoch_s,
            "end_epoch_s": self.end_epoch_s,
            "window_days": self.window_days,
            "widened": self.widened,
            "widen_reason": self.widen_reason,
            "time_keys": list(self.time_keys),
            "key_units": list(self.key_units),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TimePredicate":
        """Rebuild a live predicate from the serialised `to_dict()` form.

        This is the round-trip counterpart used by agents that pick the
        predicate set off AgentState (e.g. the Checker's rescue path):
        the master retriever serialises once into
        `state["time_range"]["source_predicates"]`, and every downstream
        consumer rehydrates via this classmethod so it never has to
        recompute the window. Guaranteed to be predicate-identical to the
        one that produced the serialisation — this is what makes the
        initial-vs-rescue windows match in lockstep (was the 3d/2d drift
        in docs/test/2026-04-22/router_e2e_deep_analysis.md).
        """
        return cls(
            source=SourceTimeKey(d["source"]),
            label=str(d.get("label", "")),
            granularity=TimeGranularity(d["granularity"]),
            start_date=date.fromisoformat(d["start_date"]),
            end_date=date.fromisoformat(d["end_date"]),
            start_epoch_s=int(d["start_epoch_s"]),
            end_epoch_s=int(d["end_epoch_s"]),
            window_days=int(d["window_days"]),
            widened=bool(d.get("widened", False)),
            widen_reason=d.get("widen_reason"),
            time_keys=tuple(d.get("time_keys", ()) or ()),
            key_units=tuple(d.get("key_units", ()) or ()),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _to_epoch_bounds(start: date, end: date) -> Tuple[int, int]:
    """Convert a (start_date, end_date) pair to Unix-seconds bounds in UTC.

    End-inclusive: we push `end` to 23:59:59 so a same-day predicate
    (e.g. "today") still covers every event that occurred during that day.
    """
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_predicate(
    time_window: Any,
    source: SourceTimeKey,
    anchor: date,
    *,
    label: Optional[str] = None,
) -> TimePredicate:
    """Compile a single source-specific `TimePredicate`.

    Parameters
    ----------
    time_window
        A `TimeWindow` enum, its string value, or anything `time_window_to_days`
        accepts. Missing/unknown inputs fall back to PAST_SIX_MONTHS (180d)
        via `time_window_to_days(default=180)`.
    source
        The `SourceTimeKey` that identifies the data source.
    anchor
        The "end of window" date — usually the most recent successful
        ingestion date (Silver's `_get_anchor_date`), NOT `date.today()`.
        This is what makes the predicate robust over weekends/holidays.
    label
        Human-readable label for audit logs. Defaults to the semantic value
        of `time_window`.

    Returns
    -------
    TimePredicate
        Ready-to-execute predicate. For MONTHLY sources with a sub-month
        request, the start_date is snapped to the first of the anchor month.
    """
    spec = SOURCE_SPECS[source]

    base_days = time_window_to_days(time_window, default=180)
    resolved_label = label or (
        getattr(time_window, "value", None) or (str(time_window) if time_window is not None else "past_six_months")
    )
    label_norm = str(resolved_label).lower()

    # Anchor is usually the most recent ingestion day (options_daily from
    # config/runtime/collect_data_state.json). If — for any reason — that
    # value lands on a weekend (e.g. a manual backfill wrote a Saturday key)
    # snap the end of the window onto the previous trading day so DAILY
    # sources do not anchor on a non-existent partition. EVENT and MONTHLY
    # sources leave the anchor alone because their physical data is calendar-
    # day based (news can publish on weekends; GPR is first-of-month).
    end: date = anchor
    if spec.granularity == TimeGranularity.DAILY and not is_business_day(anchor):
        end = previous_business_day(anchor)

    start: date = end - timedelta(days=base_days)
    widened = False
    widen_reason: Optional[str] = None

    # --- Source-specific widening rules --------------------------------------
    # Branch A: DAILY + explicit business-day semantics ("today" / "yesterday").
    # This is the fix for docs/test/2026-04-22/router_e2e_deep_analysis.md
    # lines 120-133: a 2-calendar-day window on a Tuesday anchor silently
    # sampled Sunday (no partition), which caused `latest_atm_iv` to flip
    # between revisions. Business-day semantics pin the window to the actual
    # trading day before `end`, so rescue re-queries return the same row.
    if (
        spec.granularity == TimeGranularity.DAILY
        and label_norm in _DAILY_BUSDAY_LABELS
    ):
        if label_norm == "today":
            start = end
            widened = False
            widen_reason = "daily_today_busday (start=end=anchor)"
        else:  # yesterday
            start = previous_business_day(end)
            widened = True
            widen_reason = (
                f"daily_yesterday_busday "
                f"(prev_business_day({end.isoformat()})={start.isoformat()})"
            )
    elif base_days < spec.min_lookback_days:
        if spec.granularity == TimeGranularity.MONTHLY:
            # Always include the full anchor month so the source's first-of-
            # month row is inside the window. If the user asked for a span
            # wider than one month (e.g. PAST_SIX_MONTHS=180d) this branch
            # doesn't fire and the natural window is used.
            new_start = _first_of_month(end)
            start = min(start, new_start)
            widened = True
            widen_reason = (
                f"monthly_widen_to_{(end - start).days}d "
                f"(base={base_days}d < min={spec.min_lookback_days}d)"
            )
        elif spec.granularity == TimeGranularity.DAILY:
            # Non-YESTERDAY short windows (PAST_WEEK etc.) still get the
            # calendar-day floor so the window survives weekends without
            # forcing a full business-day walk.
            start = end - timedelta(days=spec.min_lookback_days)
            widened = True
            widen_reason = f"daily_min_{spec.min_lookback_days}d (base={base_days}d)"
        elif spec.granularity == TimeGranularity.EVENT:
            start = end - timedelta(days=spec.min_lookback_days)
            widened = True
            widen_reason = f"event_min_{spec.min_lookback_days}d (base={base_days}d)"

    start_epoch, end_epoch = _to_epoch_bounds(start, end)
    window_days = (end - start).days

    predicate = TimePredicate(
        source=source,
        label=resolved_label,
        granularity=spec.granularity,
        start_date=start,
        end_date=end,
        start_epoch_s=start_epoch,
        end_epoch_s=end_epoch,
        window_days=window_days,
        widened=widened,
        widen_reason=widen_reason,
        time_keys=spec.time_keys,
        key_units=spec.key_units,
    )

    if widened:
        logger.info(
            f"🕒 [TimeAdapter] {source.value}: widened → "
            f"{start.isoformat()}..{end.isoformat()} ({window_days}d) | "
            f"reason={widen_reason}"
        )
    return predicate


def compile_all(
    time_window: Any,
    anchor: date,
    *,
    label: Optional[str] = None,
) -> Dict[SourceTimeKey, TimePredicate]:
    """Compile predicates for every registered source in one call.

    Convenience for upstream orchestration (master_retriever) so the
    full per-source predicate set is materialised once, attached to the
    run payload for audit, and dispatched to Gold/Silver without
    re-derivation.
    """
    return {
        src: compile_predicate(time_window, src, anchor, label=label)
        for src in SOURCE_SPECS
    }


def predicates_from_serialised(
    serialised: Dict[str, Any],
) -> Dict[SourceTimeKey, TimePredicate]:
    """Inverse of `{key.value: pred.to_dict() for …}` — round-trips predicates.

    Accepts the dict shape attached to
    `state["time_range"]["source_predicates"]` by `MasterRetriever.
    _compute_time_range` and returns the live `{SourceTimeKey: TimePredicate}`
    mapping that `SilverSQLTool.query_parquet_by_metadata` and
    `QdrantRetriever.retrieve_async` already consume.

    Silently skips keys that fail to deserialise so a partially-broken
    audit payload never crashes a rescue call — the caller can still fall
    back to its own anchor math for the missing sources.
    """
    out: Dict[SourceTimeKey, TimePredicate] = {}
    if not isinstance(serialised, dict):
        return out
    for k, v in serialised.items():
        if not isinstance(v, dict):
            continue
        try:
            key = SourceTimeKey(k)
        except ValueError:
            logger.debug(f"predicates_from_serialised: unknown source_key '{k}' — skipped.")
            continue
        try:
            out[key] = TimePredicate.from_dict(v)
        except Exception as e:
            logger.warning(
                f"predicates_from_serialised: failed to rehydrate {k}: "
                f"{type(e).__name__}: {e} — skipped."
            )
    return out


def union_epoch_range(predicates: List[TimePredicate]) -> Tuple[int, int]:
    """Return the `(min_start, max_end)` epoch-second bounds over predicates.

    Used by the Gold retriever to build ONE numeric Range on
    `unified_timestamp` / `publish_timestamp` that covers every selected
    Gold source (news/sec/gpr). Each source's own widening is preserved:
    a mixed {news, gpr} query still has a window large enough for GPR.

    Returns `(0, 0)` on empty input so callers can skip the filter.
    """
    if not predicates:
        return (0, 0)
    start = min(p.start_epoch_s for p in predicates)
    end = max(p.end_epoch_s for p in predicates)
    return start, end
