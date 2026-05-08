"""
RunState — the authoritative read/write handle for
``config/runtime/collect_data_state.json``.

This module is the *single source of truth* for three things that used to
be duplicated across the codebase:

1. **Cadence arithmetic** — turning a ``datetime`` into a ``run_key``
   (``"2026-04-21"`` / ``"2026-W16"`` / ``"2026-04"``) and vice versa.
2. **Idempotency check** — was job ``X`` already run this cadence window?
3. **Time-anchor lookup** — what is the latest successfully ingested
   partition date for dataset ``Y``? Agents (``Checker``, ``Finalizer``,
   ``time_adapter``, ``SilverSQLTool``) ask ``RunState.latest_anchor_date``
   instead of calling ``date.today()`` — surviving weekends, holidays and
   manual backfills.

Design choices
--------------
* **Atomic writes.** ``write_run_key`` stages to ``*.json.tmp`` then
  ``os.replace`` — on Windows this is the POSIX-equivalent atomic rename,
  so a Ctrl+C in the middle of a scheduler tick cannot corrupt the file
  that agents rely on for time anchoring.
* **No dependencies.** stdlib-only; safely importable from every layer
  including the CLI bootstrap before any heavyweight packages are loaded.
* **Lenient reads / strict writes.** If the file is missing or broken the
  readers return "unknown" sentinels so the agent layer degrades to
  ``date.today()`` rather than crashing. Writes always produce a valid
  JSON document.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    """Scripts/orchestration/run_state.py -> repo root (parents[2])."""
    return Path(__file__).resolve().parents[2]


def default_state_path() -> Path:
    """Return the canonical path to ``collect_data_state.json``."""
    return _project_root() / "config" / "runtime" / "collect_data_state.json"


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


class Cadence(str, Enum):
    """How often a pipeline stage should run.

    ``TRADING_DAILY`` is a specialization of ``DAILY`` that additionally
    refuses to run on Saturdays/Sundays — used by ``macro_trading_daily``
    where FRED / yfinance emit no new data on weekends.
    """

    DAILY = "daily"
    TRADING_DAILY = "trading_daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# ---------------------------------------------------------------------------
# run_key arithmetic — shared with the legacy `collect_data.py` scheduler
# ---------------------------------------------------------------------------


def run_key(cadence: Cadence, when: datetime) -> str:
    """Compute the idempotency key for a given moment + cadence.

    Examples
    --------
    >>> run_key(Cadence.DAILY, datetime(2026, 4, 21))
    '2026-04-21'
    >>> run_key(Cadence.WEEKLY, datetime(2026, 4, 21))   # ISO week
    '2026-W16'
    >>> run_key(Cadence.MONTHLY, datetime(2026, 4, 21))
    '2026-04'
    """
    if cadence in (Cadence.DAILY, Cadence.TRADING_DAILY):
        return when.strftime("%Y-%m-%d")
    if cadence == Cadence.WEEKLY:
        iso = when.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if cadence == Cadence.MONTHLY:
        return when.strftime("%Y-%m")
    raise ValueError(f"Unsupported cadence: {cadence!r}")


def run_key_to_date(key: str) -> Optional[date]:
    """Reverse ``run_key`` back to a representative ``date``.

    * Daily key  ``"2026-04-21"``  -> ``date(2026, 4, 21)``
    * Weekly key ``"2026-W16"``    -> Monday of that ISO week
    * Monthly key ``"2026-04"``    -> first day of that month

    Returns ``None`` when the string does not match any known pattern so
    callers can fall back to ``date.today()``.
    """
    if not key:
        return None
    try:
        if len(key) == 10 and key[4] == "-" and key[7] == "-":  # YYYY-MM-DD
            return datetime.strptime(key, "%Y-%m-%d").date()
        if len(key) == 7 and key[4] == "-":                       # YYYY-MM
            return datetime.strptime(key + "-01", "%Y-%m-%d").date()
        if "W" in key:                                            # YYYY-Www
            year_str, week_str = key.split("-W", 1)
            return date.fromisocalendar(int(year_str), int(week_str), 1)
    except (ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# Dataset → anchor-key mapping
# ---------------------------------------------------------------------------


# The authoritative map between a *logical dataset* used by agents and the
# *run_key field* where scrapers announce freshness. Keep additions to this
# table in sync with ``Scripts.data_collection.collect_data.build_jobs``.
DATASET_ANCHOR_KEYS: Dict[str, str] = {
    "options":  "options_daily",
    "macro":    "macro_trading_daily",
    "news":     "news_daily",
    "gpr":      "gpr_monthly",
    "sec":      "sec_processor_weekly",   # processor is the downstream-visible anchor
    "sec_raw":  "sec_ingestion_weekly",
}


# ---------------------------------------------------------------------------
# The state handle
# ---------------------------------------------------------------------------


@dataclass
class _StateDoc:
    """Typed view of the JSON payload — kept internal for clarity."""

    last_run_keys: Dict[str, str]
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {"last_run_keys": dict(self.last_run_keys)}
        if self.updated_at:
            doc["updated_at"] = self.updated_at
        return doc

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "_StateDoc":
        keys = raw.get("last_run_keys") or {}
        if not isinstance(keys, dict):
            keys = {}
        return cls(
            last_run_keys={str(k): str(v) for k, v in keys.items()},
            updated_at=raw.get("updated_at"),
        )


class RunState:
    """Thread-safe-ish (process-level) handle to the runtime state file.

    *Not* multi-process safe — the scheduler daemon and the interactive
    CLI should not both try to write the file at the same moment. In
    practice the scheduler owns writes; agents/CLIs only read. Readers
    tolerate half-written files by catching ``json.JSONDecodeError`` and
    returning an empty document.
    """

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self.state_path: Path = Path(state_path) if state_path else default_state_path()
        self._doc: _StateDoc = _StateDoc(last_run_keys={})
        self._loaded: bool = False

    # ---- low-level I/O -----------------------------------------------------

    def _load(self) -> None:
        if not self.state_path.exists():
            self._doc = _StateDoc(last_run_keys={})
            self._loaded = True
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
            self._doc = _StateDoc.from_dict(raw)
        except (OSError, json.JSONDecodeError):
            # Tolerate a half-written file — agents must never crash because
            # the scheduler was interrupted mid-write.
            self._doc = _StateDoc(last_run_keys={})
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()

    def reload(self) -> "RunState":
        """Force re-read from disk. Useful for long-lived agent processes
        that want to pick up a fresh daily run without restarting."""
        self._loaded = False
        self._load()
        return self

    def _atomic_write(self) -> None:
        self._doc.updated_at = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(self._doc.to_dict(), ensure_ascii=False, indent=2)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        # Use NamedTemporaryFile in the *same directory* so os.replace is a
        # rename within one filesystem — atomic on both Windows and POSIX.
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.state_path.name + ".",
            suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync is a best-effort durability hint; on some
                    # Windows filesystems it raises OSError(EINVAL). Swallow
                    # and rely on os.replace for atomicity.
                    pass
            os.replace(tmp_path, self.state_path)
        except Exception:
            # Clean up the tmp file if the rename failed.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---- public API --------------------------------------------------------

    # --- job-keyed reads -----------------------------------------------

    def get_run_key(self, job_name: str) -> Optional[str]:
        """Return the stored ``run_key`` for a named job, or ``None`` if
        the job has never recorded a successful run."""
        self._ensure_loaded()
        return self._doc.last_run_keys.get(job_name)

    def all_run_keys(self) -> Dict[str, str]:
        """Return a *copy* of the full ``last_run_keys`` mapping."""
        self._ensure_loaded()
        return dict(self._doc.last_run_keys)

    # --- idempotency ---------------------------------------------------

    def is_up_to_date(
        self,
        job_name: str,
        cadence: Cadence,
        when: Optional[datetime] = None,
    ) -> bool:
        """True if the most recently recorded ``run_key`` matches what we
        would compute now — i.e. the job does not need to run again this
        cadence window."""
        when = when or datetime.now()
        return self.get_run_key(job_name) == run_key(cadence, when)

    # --- writes (owned by scheduler / CLI ingest) ----------------------

    def mark_job_complete(
        self,
        job_name: str,
        cadence: Cadence,
        when: Optional[datetime] = None,
    ) -> str:
        """Record that ``job_name`` just finished successfully. Returns
        the ``run_key`` that was written so callers can log it."""
        self._ensure_loaded()
        when = when or datetime.now()
        key = run_key(cadence, when)
        self._doc.last_run_keys[job_name] = key
        self._atomic_write()
        return key

    def write_run_key(self, job_name: str, key: str) -> None:
        """Lower-level write for callers that already computed their own
        ``run_key`` (e.g. backfill scripts replaying historical dates)."""
        self._ensure_loaded()
        self._doc.last_run_keys[job_name] = str(key)
        self._atomic_write()

    # --- time anchor lookup (the single source of truth) ---------------

    def latest_anchor_date(
        self,
        dataset: str,
        *,
        fallback: Optional[date] = None,
    ) -> date:
        """Return the most recently ingested *data* date for ``dataset``.

        Parameters
        ----------
        dataset
            Logical dataset name — one of ``DATASET_ANCHOR_KEYS``'s keys
            (``"options"``, ``"macro"``, ``"news"``, ``"gpr"``, ``"sec"``,
            ``"sec_raw"``). Case-insensitive.
        fallback
            What to return when the state file is missing or the dataset
            has no recorded anchor yet. Defaults to ``date.today()``.

        Never raises — the agent layer is expected to always get *some*
        anchor so that ``yesterday`` can be computed deterministically.
        """
        self._ensure_loaded()
        key_field = DATASET_ANCHOR_KEYS.get(dataset.lower())
        if key_field:
            raw = self._doc.last_run_keys.get(key_field)
            anchor = run_key_to_date(raw or "")
            if anchor:
                return anchor
        return fallback or date.today()

    def latest_anchor_for_granularity(
        self,
        granularity: str,
        *,
        fallback: Optional[date] = None,
    ) -> date:
        """Convenience mirror of :meth:`latest_anchor_date` keyed by the
        ``TimeGranularity`` enum values used in ``time_adapter``:
        ``"daily"``, ``"event"``, ``"monthly"``, ``"weekly"``. Events
        anchor on news (the freshest event stream); weekly anchors on
        SEC; monthly on GPR.
        """
        mapping = {
            "daily":   "options",
            "event":   "news",
            "weekly":  "sec",
            "monthly": "gpr",
        }
        dataset = mapping.get(granularity.lower(), "options")
        return self.latest_anchor_date(dataset, fallback=fallback)

    # --- diagnostics ---------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a serialisable summary for ``cli.py status``."""
        self._ensure_loaded()
        return {
            "state_path": str(self.state_path),
            "exists":     self.state_path.exists(),
            "updated_at": self._doc.updated_at,
            "last_run_keys": dict(self._doc.last_run_keys),
            "dataset_anchor_keys": dict(DATASET_ANCHOR_KEYS),
            "anchors": {
                ds: self.latest_anchor_date(ds).isoformat()
                for ds in DATASET_ANCHOR_KEYS
            },
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_SINGLETON: Optional[RunState] = None


def get_run_state(state_path: Optional[Path] = None) -> RunState:
    """Process-wide singleton accessor.

    Agents that re-query the state repeatedly (``SilverSQLTool``,
    ``time_adapter.build_anchor``) should call this rather than
    instantiating ``RunState`` — avoids O(N) JSON parses per query.
    Pass an explicit ``state_path`` only in tests.
    """
    global _SINGLETON
    if state_path is not None:
        # Tests: bypass the cache so each test gets its own instance.
        return RunState(state_path)
    if _SINGLETON is None:
        _SINGLETON = RunState()
    return _SINGLETON


def reset_run_state_singleton() -> None:
    """Drop the process-wide cache — used by long-lived daemons that want
    to pick up a freshly rewritten state file without restarting."""
    global _SINGLETON
    _SINGLETON = None


__all__ = [
    "Cadence",
    "DATASET_ANCHOR_KEYS",
    "RunState",
    "default_state_path",
    "get_run_state",
    "reset_run_state_singleton",
    "run_key",
    "run_key_to_date",
]
