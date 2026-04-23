"""
Unified logging + audit-path helpers.

Motivation
----------
Before this module the project wrote logs into a zoo of top-level
folders (``logs/retrieval/``, ``logs/query_transform/``, ``logs/router_e2e/``,
``logs/Parquet_Query/``, ``logs/scheduler/``, …). Investigating a single
production incident meant stitching together four timestamp-matched
files by hand.

This module converges everything on two well-known layouts:

1. **Run-scoped logs** (for *one* orchestrator invocation / CLI session)

   ::

        logs/runs/{YYYY-MM-DD}/{run_id}/
            orchestrator.log         # root logger, human-readable
            retrieval.jsonl          # per-module audit trail
            query_transform.jsonl
            router_e2e.jsonl
            sql_range.jsonl

   Every orchestrator invocation gets its own ``run_id`` folder, making
   "what happened between 15:09 and 15:12 on 2026-04-22?" trivial to
   answer with ``ls -R``.

2. **Daemon / long-lived logs** (for the scheduler that does not have a
   single-shot boundary)

   ::

        logs/scheduler/collect_data.log      # existing, back-compat
        logs/daemon/{YYYY-MM-DD}.log         # rolled daily

Migration path
--------------
* ``audit_path(module, anchor)`` returns the new unified path; existing
  modules can switch one call-site at a time.
* ``get_audit_logger(name, module)`` wraps ``logging.getLogger`` and
  attaches a ``FileHandler`` pointed at the unified path. Call it once
  per module and reuse the returned logger.
* Legacy paths still work — nothing under ``logs/`` is deleted.

Design constraints
------------------
* **Stdlib only** — imported during CLI / daemon bootstrap before any
  heavy dependencies.
* **Windows-safe** — file handles are opened with ``encoding="utf-8"``
  and ``delay=True`` so that transient permission issues do not crash
  the scheduler mid-tick.
* **Idempotent** — calling ``configure_root_logger()`` twice replaces
  handlers instead of stacking them (important for Jupyter / pytest).
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Path layout
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    # Scripts/observability/audit.py -> parents[2]
    return Path(__file__).resolve().parents[2]


def _logs_root() -> Path:
    return _project_root() / "logs"


# ---------------------------------------------------------------------------
# run_id — one per orchestrator / CLI invocation
# ---------------------------------------------------------------------------


_RUN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "audit_run_id", default=None
)


def start_run(tag: str = "session") -> str:
    """Mint and register a ``run_id`` for this process.

    Returns
    -------
    str
        ``"{YYYYMMDD}_{HHMMSS}_{tag}_{short-uuid}"``. Short enough to be
        a filesystem-safe folder name, long enough to be unique under
        millisecond-scale re-runs.
    """
    rid = f"{datetime.now():%Y%m%d_%H%M%S}_{tag}_{uuid.uuid4().hex[:6]}"
    _RUN_ID.set(rid)
    return rid


def current_run_id() -> str:
    """Return the active ``run_id``. Starts a fresh one if nothing has
    been registered yet — this keeps ad-hoc scripts (notebooks, tests)
    from needing to remember to call ``start_run`` first.
    """
    rid = _RUN_ID.get()
    if rid:
        return rid
    return start_run(tag="adhoc")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def audit_path(
    module: str,
    anchor: Optional[date] = None,
    *,
    filename: Optional[str] = None,
    scoped_by_run: bool = True,
) -> Path:
    """Return the canonical path for a module's audit file.

    Parameters
    ----------
    module
        Logical module name — ``"retrieval" / "query_transform" /
        "router_e2e" / "sql_range"``. Used as both the leaf filename
        (``<module>.jsonl``) and a short tag.
    anchor
        The *data* date this audit entry pertains to. Defaults to today.
        Using an explicit anchor is recommended for agent-side audits so
        backfills land in the correct folder.
    filename
        Override the default ``<module>.jsonl`` leaf filename. Mostly
        useful for sub-files like ``retriever_audit_trail.jsonl`` that
        existing consumers already expect.
    scoped_by_run
        When ``True`` (default), place the file under the current
        ``run_id`` so that everything produced by one CLI invocation
        lives together. When ``False``, use the flat
        ``logs/{module}/{date}/<filename>`` layout that predates
        ``run_id`` and is still read by legacy offline tools.

    The parent folder is created as a side-effect.
    """
    anchor = anchor or date.today()
    date_str = anchor.isoformat()
    leaf = filename or f"{module}.jsonl"

    if scoped_by_run:
        folder = _logs_root() / "runs" / date_str / current_run_id() / module
    else:
        folder = _logs_root() / module / date_str

    folder.mkdir(parents=True, exist_ok=True)
    return folder / leaf


# ---------------------------------------------------------------------------
# Logger configuration
# ---------------------------------------------------------------------------


_ROOT_CONFIGURED = False


def configure_root_logger(
    *,
    level: str = "INFO",
    also_to_file: bool = True,
) -> logging.Logger:
    """Install a sane stdout + file handler on the root logger.

    Safe to call multiple times — subsequent calls replace existing
    handlers so pytest / Jupyter do not double-print.
    """
    global _ROOT_CONFIGURED

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if also_to_file:
        run_folder = _logs_root() / "runs" / date.today().isoformat() / current_run_id()
        run_folder.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            run_folder / "orchestrator.log",
            encoding="utf-8",
            delay=True,  # open on first emit — survives read-only CI
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    _ROOT_CONFIGURED = True
    return root


def get_audit_logger(
    name: str,
    module: str,
    *,
    anchor: Optional[date] = None,
    level: str = "INFO",
) -> logging.Logger:
    """Return a logger whose output goes to the unified audit path.

    The returned logger has a single ``FileHandler`` attached at
    ``audit_path(module, anchor, filename=f"{name}.log")``. Console
    output continues to flow through the root logger (if configured).

    Typical usage — one call near the top of a module::

        log = get_audit_logger("QdrantRetriever", module="retrieval")
        log.info("search filter=%s", filt)
    """
    anchor = anchor or date.today()
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    target = audit_path(module, anchor, filename=f"{name}.log")

    # Avoid stacking handlers on repeat calls (pytest, Jupyter).
    for h in list(logger.handlers):
        if getattr(h, "_audit_path", None) == str(target):
            return logger

    handler = logging.FileHandler(target, encoding="utf-8", delay=True)
    handler._audit_path = str(target)  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


__all__ = [
    "audit_path",
    "configure_root_logger",
    "current_run_id",
    "get_audit_logger",
    "start_run",
]
