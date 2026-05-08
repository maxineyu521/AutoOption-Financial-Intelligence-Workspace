"""
Pipeline — compose Stages into a DAG, run them once or forever.

Two entry points:

* :meth:`Pipeline.run_once` — ad-hoc execution ("run today's ingest now",
  or "only the ``options_daily`` stage"). Called by the CLI ``ingest``
  subcommand and by tests.
* :meth:`Pipeline.run_forever` — long-lived scheduler loop. Replaces
  ``Scripts/data_collection/collect_data.py::SchedulerService.run_forever``
  while keeping the same cadence / state semantics.

Dependency handling
-------------------
Stages declare ``depends_on``; the runner topologically sorts the graph
and skips downstream stages whose upstreams failed (unless ``force=True``).
Cycles raise ``ValueError`` at *pipeline build time* — not at run time —
so misconfigurations are caught fast.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from Scripts.orchestration.run_state import Cadence, RunState, get_run_state
from Scripts.orchestration.stages import (
    ScriptStage,
    Stage,
    StageResult,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def _toposort(stages: Sequence[Stage]) -> List[Stage]:
    """Kahn's algorithm — stable w.r.t. insertion order for ties so the
    CLI output is reproducible."""
    by_name: Dict[str, Stage] = {s.name: s for s in stages}
    if len(by_name) != len(stages):
        all_names = [s.name for s in stages]
        dup = sorted({n for n in all_names if all_names.count(n) > 1})
        raise ValueError(f"Duplicate stage name(s): {dup}")

    incoming: Dict[str, int] = {n: 0 for n in by_name}
    outgoing: Dict[str, List[str]] = {n: [] for n in by_name}
    for s in stages:
        for dep in s.depends_on:
            if dep not in by_name:
                raise ValueError(
                    f"Stage {s.name!r} depends on unknown stage {dep!r}"
                )
            incoming[s.name] += 1
            outgoing[dep].append(s.name)

    ready: List[str] = [n for n, c in incoming.items() if c == 0]
    ordered: List[Stage] = []
    while ready:
        ready.sort()  # determinism
        current = ready.pop(0)
        ordered.append(by_name[current])
        for child in outgoing[current]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)

    if len(ordered) != len(stages):
        remaining = [n for n, c in incoming.items() if c > 0]
        raise ValueError(f"Stage dependency cycle involving: {remaining}")
    return ordered


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """A topologically-ordered collection of :class:`Stage`s."""

    def __init__(
        self,
        stages: Sequence[Stage],
        *,
        run_state: Optional[RunState] = None,
    ) -> None:
        self._stages: List[Stage] = _toposort(stages)
        self._by_name: Dict[str, Stage] = {s.name: s for s in self._stages}
        self.run_state: RunState = run_state or get_run_state()

    # ---- introspection -----------------------------------------------------

    @property
    def stage_names(self) -> List[str]:
        return [s.name for s in self._stages]

    def stage(self, name: str) -> Stage:
        return self._by_name[name]

    def describe(self) -> List[Dict[str, Any]]:
        return [stage.describe() for stage in self._stages]

    # ---- execution ---------------------------------------------------------

    def run_once(
        self,
        *,
        only: Optional[Iterable[str]] = None,
        force: bool = False,
        dry_run: bool = False,
        when: Optional[datetime] = None,
    ) -> List[StageResult]:
        """Run the pipeline exactly once.

        Parameters
        ----------
        only
            If provided, execute *only* these stage names (plus any of
            their upstreams that are not up-to-date). Unknown names are
            a ``ValueError``.
        force
            Ignore ``is_up_to_date`` — useful for manual backfills.
        dry_run
            Log what *would* run without executing.
        when
            Override the wall-clock. Used by tests and backfills.
        """
        when = when or datetime.now()
        targets = self._resolve_targets(only)

        results: List[StageResult] = []
        failed: set[str] = set()

        for stage in self._stages:
            if stage.name not in targets:
                continue

            # Short-circuit downstream work when a critical upstream failed.
            upstream_failed = {d for d in stage.depends_on if d in failed}
            if upstream_failed:
                log.warning(
                    "stage=%s skipped — upstream failed: %s",
                    stage.name,
                    sorted(upstream_failed),
                )
                results.append(
                    StageResult(
                        name=stage.name,
                        status="skipped",
                        error=f"upstream_failed={sorted(upstream_failed)}",
                    )
                )
                failed.add(stage.name)  # propagate to deeper dependents
                continue

            result = stage.run(
                self.run_state, when=when, force=force, dry_run=dry_run
            )
            results.append(result)
            if not result.ok and stage.critical:
                failed.add(stage.name)

        return results

    def run_forever(
        self,
        *,
        poll_seconds: int = 30,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """Long-lived scheduler loop. Calls :meth:`run_once` on every
        tick — ``is_up_to_date`` guarantees each stage runs at most once
        per cadence window.

        This replaces ``SchedulerService.run_forever`` in
        ``Scripts/data_collection/collect_data.py`` but is fully backward
        compatible because they share the same ``RunState`` on disk.
        """
        now = now_provider or datetime.now
        log.info(
            "Pipeline.run_forever starting — %d stages, poll=%ds, state=%s",
            len(self._stages),
            poll_seconds,
            self.run_state.state_path,
        )
        while True:
            try:
                self.run_once(when=now())
            except Exception:
                log.exception("unexpected tick failure — continuing")
            time.sleep(poll_seconds)

    # ---- internals ---------------------------------------------------------

    def _resolve_targets(self, only: Optional[Iterable[str]]) -> set[str]:
        """Expand ``only`` to include all upstream dependencies so partial
        runs respect the DAG."""
        if only is None:
            return set(self._by_name)

        requested = list(only)
        unknown = [n for n in requested if n not in self._by_name]
        if unknown:
            raise ValueError(
                f"Unknown stage name(s): {unknown}. "
                f"Known: {sorted(self._by_name)}"
            )

        needed: set[str] = set()
        stack = list(requested)
        while stack:
            current = stack.pop()
            if current in needed:
                continue
            needed.add(current)
            stack.extend(self._by_name[current].depends_on)
        return needed


# ---------------------------------------------------------------------------
# Default factory — the 6 stages your project already runs
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_default_pipeline(
    *,
    run_state: Optional[RunState] = None,
    project_root: Optional[Path] = None,
) -> Pipeline:
    """Build the production pipeline from the 5 scrapers + 1 processor
    already present in ``Scripts/data_collection``.

    The DAG mirrors the reality of the data layer:
        (gpr_monthly, macro_trading_daily, news_daily, options_daily)
                                 │
                                 ▼
                         sec_ingestion_weekly
                                 │
                                 ▼
                         sec_processor_weekly

    None of the daily sources depend on each other — they hit different
    external endpoints and can run in parallel if you later switch the
    runner to threads/async. ``sec_processor`` depends on ``sec_ingestion``
    because it parses the artefacts ingestion just wrote to Bronze.
    """
    root = Path(project_root) if project_root else _project_root()
    scrapers = root / "Scripts" / "data_collection" / "scrapers"
    processors = root / "Scripts" / "data_collection" / "processors"

    stages: List[Stage] = [
        ScriptStage(
            name="gpr_monthly",
            script_path=scrapers / "GPR_index.py",
            cadence=Cadence.MONTHLY,
            project_root=root,
        ),
        ScriptStage(
            name="macro_trading_daily",
            script_path=scrapers / "macro_data_pipeline.py",
            cadence=Cadence.TRADING_DAILY,
            project_root=root,
        ),
        ScriptStage(
            name="news_daily",
            script_path=scrapers / "news_scraper.py",
            cadence=Cadence.DAILY,
            project_root=root,
        ),
        ScriptStage(
            name="options_daily",
            script_path=scrapers / "yfinance_options_history.py",
            cadence=Cadence.DAILY,
            project_root=root,
        ),
        ScriptStage(
            name="sec_ingestion_weekly",
            script_path=scrapers / "sec_ingestion.py",
            cadence=Cadence.WEEKLY,
            timeout_s=60 * 45,
            project_root=root,
        ),
        ScriptStage(
            name="sec_processor_weekly",
            script_path=processors / "sec_processor.py",
            cadence=Cadence.WEEKLY,
            depends_on=("sec_ingestion_weekly",),
            timeout_s=60 * 45,
            project_root=root,
        ),
    ]

    return Pipeline(stages, run_state=run_state)


def default_stage_manifest(
    *,
    run_state: Optional[RunState] = None,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return the serialisable manifest for the default ingest DAG."""
    return build_default_pipeline(
        run_state=run_state,
        project_root=project_root,
    ).describe()


__all__ = [
    "Pipeline",
    "build_default_pipeline",
    "default_stage_manifest",
]
