"""
Stage abstractions for the pipeline DAG.

Design
------
A *Stage* is one unit of work in the ingest pipeline — typically
"scrape source X and land it in Bronze/Silver". Stages are:

* **Idempotent** — asked to run twice in the same cadence window they
  skip the second time. Ownership of that decision lives in ``RunState``.
* **Declarative about cadence** — the stage says *"I run weekly"* and
  the scheduler / CLI decides whether today's the day.
* **Declarative about dependencies** — ``depends_on`` forms a DAG that
  :class:`Scripts.orchestration.pipeline.Pipeline` topologically sorts.
* **Robust about failure** — raising is allowed; the runner converts
  exceptions into ``StageResult(status="failed")`` and decides whether
  to continue based on ``critical``.

Two concrete Stage types are provided here:

* :class:`ScriptStage` — thin wrapper around :class:`subprocess.run`,
  fully compatible with your existing ``Scripts/data_collection/scrapers/*.py``
  scripts. No migration required.
* :class:`CallableStage` — for future in-process stages (e.g. the query
  pipeline, embedding re-indexers). No subprocess overhead.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from Scripts.orchestration.run_state import Cadence, RunState, run_key

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """Outcome of a single Stage invocation."""

    name: str
    status: str          # "ok" | "skipped" | "failed" | "degraded"
    run_key: Optional[str] = None
    latency_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Stage(ABC):
    """Abstract base for all pipeline stages.

    Subclasses must set the class-level ``name`` / ``cadence`` and
    implement :meth:`_execute`. Everything else (logging, timing,
    state bookkeeping) is handled here.
    """

    name: str
    cadence: Cadence
    depends_on: Tuple[str, ...] = ()
    critical: bool = True
    timeout_s: int = 60 * 30
    max_retries: int = 2
    retry_backoff_s: int = 10

    # ---- subclass hooks ----------------------------------------------------

    @abstractmethod
    def _execute(self, when: datetime) -> Tuple[str, str]:
        """Perform the actual work.

        Returns
        -------
        (stdout, stderr)
            Captured output (empty strings are fine). Raise on failure.
        """

    # ---- framework methods -------------------------------------------------

    def is_up_to_date(self, run_state: RunState, when: datetime) -> bool:
        """Default freshness check: compare stored ``run_key`` vs current."""
        return run_state.is_up_to_date(self.name, self.cadence, when)

    def run(
        self,
        run_state: RunState,
        when: Optional[datetime] = None,
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> StageResult:
        when = when or datetime.now()
        current_key = run_key(self.cadence, when)

        if not force and self.is_up_to_date(run_state, when):
            log.info(
                "stage=%s skipped (up-to-date, run_key=%s)", self.name, current_key
            )
            return StageResult(name=self.name, status="skipped", run_key=current_key)

        if dry_run:
            log.info("stage=%s dry_run (would execute run_key=%s)", self.name, current_key)
            return StageResult(
                name=self.name,
                status="skipped",
                run_key=current_key,
                metrics={"dry_run": True},
            )

        started = time.monotonic()
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 2):
            log.info(
                "stage=%s attempt=%d cadence=%s run_key=%s",
                self.name,
                attempt,
                self.cadence.value,
                current_key,
            )
            try:
                stdout, stderr = self._execute(when)
                run_state.mark_job_complete(self.name, self.cadence, when)
                latency = time.monotonic() - started
                log.info(
                    "stage=%s ok run_key=%s latency=%.2fs",
                    self.name,
                    current_key,
                    latency,
                )
                return StageResult(
                    name=self.name,
                    status="ok",
                    run_key=current_key,
                    latency_s=latency,
                    stdout=stdout,
                    stderr=stderr,
                )
            except Exception as exc:  # noqa: BLE001 — we want the full trap
                last_error = exc
                log.warning(
                    "stage=%s attempt=%d failed: %s",
                    self.name,
                    attempt,
                    exc,
                )
                if attempt <= self.max_retries:
                    backoff = min(60, self.retry_backoff_s * attempt)
                    time.sleep(backoff)

        latency = time.monotonic() - started
        return StageResult(
            name=self.name,
            status="failed",
            run_key=current_key,
            latency_s=latency,
            error=str(last_error) if last_error else "unknown error",
        )


# ---------------------------------------------------------------------------
# Concrete: subprocess adapter (today's default)
# ---------------------------------------------------------------------------


class ScriptStage(Stage):
    """Stage that shells out to a Python script via ``subprocess.run``.

    This is the zero-migration wrapper for your existing scrapers in
    ``Scripts/data_collection/scrapers/*.py`` and processors in
    ``Scripts/data_collection/processors/*.py``. Those scripts continue
    to run standalone ( ``python Scripts/...``) — the ScriptStage simply
    adds orchestration concerns (retries, timing, run_key accounting).
    """

    def __init__(
        self,
        *,
        name: str,
        script_path: Path,
        cadence: Cadence,
        depends_on: Tuple[str, ...] = (),
        critical: bool = True,
        timeout_s: int = 60 * 30,
        max_retries: int = 2,
        project_root: Optional[Path] = None,
        extra_args: Tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.cadence = cadence
        self.depends_on = depends_on
        self.critical = critical
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.script_path = Path(script_path)
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.extra_args = extra_args

        if not self.script_path.exists():
            raise FileNotFoundError(
                f"ScriptStage({name!r}): script not found at {self.script_path}"
            )

    def _execute(self, when: datetime) -> Tuple[str, str]:
        cmd = [sys.executable, str(self.script_path), *self.extra_args]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            cwd=str(self.project_root),
            check=False,
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if completed.returncode != 0:
            # Include stderr tail in the raised message so the StageResult
            # carries an actionable error without dumping the whole trace.
            tail = stderr[-500:] if stderr else "(no stderr)"
            raise RuntimeError(
                f"script={self.script_path.name} rc={completed.returncode}: {tail}"
            )
        return stdout, stderr


# ---------------------------------------------------------------------------
# Concrete: in-process callable (future-facing)
# ---------------------------------------------------------------------------


class CallableStage(Stage):
    """Stage that runs an in-process Python callable.

    Prefer this for new stages written from scratch — avoids the cost
    of spawning a Python subprocess and lets the callable accept typed
    state (``RunState``, ``run_id``). The callable signature is:

        fn(when: datetime) -> Dict[str, Any]

    and the returned dict is stored in ``StageResult.metrics``.
    """

    def __init__(
        self,
        *,
        name: str,
        fn: Callable[[datetime], Dict[str, Any]],
        cadence: Cadence,
        depends_on: Tuple[str, ...] = (),
        critical: bool = True,
        timeout_s: int = 60 * 30,
        max_retries: int = 2,
    ) -> None:
        self.name = name
        self.cadence = cadence
        self.depends_on = depends_on
        self.critical = critical
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._fn = fn

    def _execute(self, when: datetime) -> Tuple[str, str]:
        metrics = self._fn(when) or {}
        # We return metrics via stdout JSON so StageResult.metrics picks
        # it up; the Pipeline runner unpacks this for callers that care.
        import json as _json  # local to keep global imports minimal
        return _json.dumps(metrics, default=str), ""


__all__ = [
    "Stage",
    "StageResult",
    "ScriptStage",
    "CallableStage",
]
