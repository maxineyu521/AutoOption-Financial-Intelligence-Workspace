"""
Data collection scheduler entrypoint.

Layered architecture:
1) Path layer: resolves project/data/log/script paths.
2) Job layer: defines cadence and execution policy.
3) Runner layer: executes scripts with retries and timeouts.
4) Scheduler layer: orchestrates due checks and state persistence.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List


def project_root() -> Path:
    """
    Resolve repository root from Scripts/data_collection/collect_data.py.
    """
    return Path(__file__).resolve().parents[2]


class Paths:
    """
    Centralized path resolver for scripts, data, logs and runtime state.
    """

    def __init__(self) -> None:
        self.root = project_root()
        self.scripts_data_collection = self.root / "Scripts" / "data_collection"
        self.scrapers = self.scripts_data_collection / "scrapers"
        self.processors = self.scripts_data_collection / "processors"
        self.data = self.root / "Data"
        self.logs = self.root / "logs" / "scheduler"
        self.runtime = self.root / "config" / "runtime"
        self.state_file = self.runtime / "collect_data_state.json"

    def ensure_dirs(self) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)

    def data_dir(self, *parts: str) -> Path:
        path = self.data.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_file(self) -> Path:
        return self.logs / "collect_data.log"


class Cadence(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    TRADING_DAILY = "trading_daily"


@dataclass(frozen=True)
class JobDefinition:
    name: str
    script_path: Path
    cadence: Cadence
    hour: int
    minute: int
    weekday: int | None = None  # Monday=0, Sunday=6 for weekly cadence.
    timeout_seconds: int = 60 * 30
    max_retries: int = 2


class StateStore:
    """
    Persistent state for idempotent scheduling (avoids duplicate runs).

    Thin adapter over :class:`Scripts.orchestration.run_state.RunState` so
    that the legacy ``SchedulerService`` daemon and the new CLI share the
    *same* ``config/runtime/collect_data_state.json``. All reads / writes
    delegate to the orchestration layer — atomic writes, tolerant reads,
    dataset-anchor mapping all come for free.
    """

    def __init__(self, state_file: Path) -> None:
        # Importing here (rather than at module top) keeps this file usable
        # as a standalone daemon even on environments where the full
        # orchestration package is still being rolled out.
        from Scripts.orchestration.run_state import RunState

        self.state_file = state_file
        self._run_state = RunState(state_path=state_file)

    # ---- back-compat surface (used by SchedulerService) --------------

    @property
    def state(self) -> Dict[str, Dict[str, str]]:
        """Mirror the pre-refactor ``.state`` dict for callers that touch
        it directly (``self.state["last_run_keys"][...]``)."""
        return {"last_run_keys": self._run_state.all_run_keys()}

    def save(self) -> None:
        # RunState writes atomically after every mutation, so this is a
        # no-op — kept so existing callers do not raise AttributeError.
        return None

    def get_last_run_key(self, job_name: str) -> str | None:
        return self._run_state.get_run_key(job_name)

    def set_last_run_key(self, job_name: str, run_key: str) -> None:
        self._run_state.write_run_key(job_name, run_key)


class JobRunner:
    """
    Script execution engine with retries and timeout controls.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def run(self, job: JobDefinition) -> bool:
        cmd = [sys.executable, str(job.script_path)]

        for attempt in range(1, job.max_retries + 2):
            self.logger.info("Running job=%s attempt=%s cmd=%s", job.name, attempt, cmd)
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=job.timeout_seconds,
                    cwd=str(project_root()),
                    check=False,
                )
                if completed.stdout:
                    self.logger.info("job=%s stdout:\n%s", job.name, completed.stdout.strip())
                if completed.stderr:
                    self.logger.warning("job=%s stderr:\n%s", job.name, completed.stderr.strip())

                if completed.returncode == 0:
                    self.logger.info("job=%s completed successfully", job.name)
                    return True

                self.logger.error(
                    "job=%s failed with return code=%s",
                    job.name,
                    completed.returncode,
                )
            except subprocess.TimeoutExpired:
                self.logger.exception("job=%s timeout after %ss", job.name, job.timeout_seconds)
            except Exception:
                self.logger.exception("job=%s failed with unexpected exception", job.name)

            if attempt <= job.max_retries:
                backoff = min(60, 10 * attempt)
                self.logger.info("job=%s retrying after %ss", job.name, backoff)
                time.sleep(backoff)

        return False


class SchedulePolicy:
    """
    Time and cadence policy. Uses simple local-time checks.
    """

    @staticmethod
    def is_trading_day(dt: datetime) -> bool:
        # Robust baseline: Monday-Friday. Integrate exchange holidays if needed.
        return dt.weekday() < 5

    @staticmethod
    def run_key(job: JobDefinition, now: datetime) -> str:
        if job.cadence in (Cadence.DAILY, Cadence.TRADING_DAILY):
            return now.strftime("%Y-%m-%d")
        if job.cadence == Cadence.WEEKLY:
            iso = now.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        if job.cadence == Cadence.MONTHLY:
            return now.strftime("%Y-%m")
        raise ValueError(f"Unsupported cadence: {job.cadence}")

    @staticmethod
    def is_due(job: JobDefinition, now: datetime, last_run_key: str | None) -> bool:
        if now.hour < job.hour or (now.hour == job.hour and now.minute < job.minute):
            return False

        if job.cadence == Cadence.TRADING_DAILY and not SchedulePolicy.is_trading_day(now):
            return False

        if job.cadence == Cadence.WEEKLY:
            if job.weekday is None:
                return False
            if now.weekday() != job.weekday:
                return False

        current_key = SchedulePolicy.run_key(job, now)
        return current_key != last_run_key


class SchedulerService:
    """
    Long-running scheduler loop with persistent state.
    """

    def __init__(
        self,
        jobs: List[JobDefinition],
        paths: Paths,
        state: StateStore,
        runner: JobRunner,
        logger: logging.Logger,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.jobs = jobs
        self.paths = paths
        self.state = state
        self.runner = runner
        self.logger = logger
        self.now_provider = now_provider or datetime.now

    def tick(self) -> None:
        now = self.now_provider()
        for job in self.jobs:
            last_key = self.state.get_last_run_key(job.name)
            if not SchedulePolicy.is_due(job, now, last_key):
                continue

            run_key = SchedulePolicy.run_key(job, now)
            self.logger.info(
                "job=%s due at %s (last_key=%s, run_key=%s)",
                job.name,
                now.isoformat(timespec="seconds"),
                last_key,
                run_key,
            )

            ok = self.runner.run(job)
            if ok:
                self.state.set_last_run_key(job.name, run_key)
            else:
                self.logger.error("job=%s exhausted retries, state not advanced", job.name)

    def run_forever(self, poll_seconds: int = 30) -> None:
        self.logger.info("Scheduler started with poll_seconds=%s", poll_seconds)
        while True:
            try:
                self.tick()
            except Exception:
                self.logger.exception("Unexpected scheduler tick failure")
            time.sleep(poll_seconds)


def build_jobs(paths: Paths) -> List[JobDefinition]:
    """
    Required schedule:
    - GPR_index.py: monthly.
    - macro_data_pipeline.py: every trading day.
    - news_scraper.py: daily.
    - yfinance_options_history.py: daily.
    - sec_ingestion.py: weekly.
    - sec_processor.py: weekly.
    """
    return [
        JobDefinition(
            name="gpr_monthly",
            script_path=paths.scrapers / "GPR_index.py",
            cadence=Cadence.MONTHLY,
            hour=6,
            minute=5,
        ),
        JobDefinition(
            name="macro_trading_daily",
            script_path=paths.scrapers / "macro_data_pipeline.py",
            cadence=Cadence.TRADING_DAILY,
            hour=6,
            minute=30,
        ),
        JobDefinition(
            name="news_daily",
            script_path=paths.scrapers / "news_scraper.py",
            cadence=Cadence.DAILY,
            hour=7,
            minute=0,
        ),
        JobDefinition(
            name="options_daily",
            script_path=paths.scrapers / "yfinance_options_history.py",
            cadence=Cadence.DAILY,
            hour=7,
            minute=20,
        ),
        JobDefinition(
            name="sec_ingestion_weekly",
            script_path=paths.scrapers / "sec_ingestion.py",
            cadence=Cadence.WEEKLY,
            hour=8,
            minute=0,
            weekday=6,  # Sunday
            timeout_seconds=60 * 45,
        ),
        JobDefinition(
            name="sec_processor_weekly",
            script_path=paths.processors / "sec_processor.py",
            cadence=Cadence.WEEKLY,
            hour=9,
            minute=0,
            weekday=6,  # Sunday
            timeout_seconds=60 * 45,
        ),
    ]


def configure_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("collect_data_scheduler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def validate_jobs(jobs: List[JobDefinition]) -> None:
    missing = [str(job.script_path) for job in jobs if not job.script_path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing job scripts: {missing}")


def main() -> None:
    paths = Paths()
    paths.ensure_dirs()

    logger = configure_logging(paths.log_file())
    logger.info("project_root=%s", paths.root)
    logger.info("data_root=%s", paths.data)
    logger.info("log_file=%s", paths.log_file())

    jobs = build_jobs(paths)
    validate_jobs(jobs)

    state = StateStore(paths.state_file)
    runner = JobRunner(logger)
    scheduler = SchedulerService(
        jobs=jobs,
        paths=paths,
        state=state,
        runner=runner,
        logger=logger,
    )
    scheduler.run_forever(poll_seconds=30)


if __name__ == "__main__":
    main()
