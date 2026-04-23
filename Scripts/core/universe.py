"""Asset universe & pipeline-config single-source-of-truth.

This module is the only place the rest of the codebase should go through when
it needs:

  * ticker lists (``universe.get("equity.single_name")`` …)
  * the SEC ticker→CIK map (``universe.cik_map()``)
  * pipeline parameters (``universe.pipeline("options_history")``)
  * well-formed output paths for parquet snapshots
    (``universe.paths.options_parquet_path(symbol, snapshot_date)``)

Design goals
------------
* **Zero logic change** for legacy callers. Path-helpers default to the
  ``legacy`` storage strategy, which reproduces the exact file layout
  ``Scripts/data_collection/scrapers/yfinance_options_history.py`` has been
  writing since day one. Read-side globs in ``Scripts/retrieval/sql_tools.py``
  keep working unmodified.
* **Forward-compat**. A ``hive_v1`` and a ``monthly_rollup`` strategy are
  declared in ``config/pipeline/options_history.json``; flipping the
  ``storage.strategy`` field re-routes all writers without touching code.
* **Single source of truth for paths**. ``cik_map()`` reads
  ``config/reference/ticker_to_cik.json``. The retired
  ``config/SEC_Ingestion/`` folder is no longer consulted.

No third-party dependency (no ``pyyaml``, no ``pydantic``).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Scripts/core/universe.py → repo root is parents[2]
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
CONFIG_ROOT: Path = PROJECT_ROOT / "config"
DATA_ROOT: Path = PROJECT_ROOT / "Data"


# ============================================================================
# 1. UniverseLoader — tickers + reference maps
# ============================================================================

class UniverseLoader:
    """Read ticker roles from ``config/universe/_manifest.json`` and resolve
    composite roles by set union. Results are memoised per process.
    """

    _MANIFEST_PATH = CONFIG_ROOT / "universe" / "_manifest.json"

    # Canonical location for the static ticker -> CIK mapping. Written by
    # Scripts/tools/SEC_generate_cik_map.py.
    _CIK_MAP_PATH = CONFIG_ROOT / "reference" / "ticker_to_cik.json"

    # --------------------------------------------------------------- manifest
    @lru_cache(maxsize=1)
    def _manifest(self) -> Dict:
        if not self._MANIFEST_PATH.exists():
            raise FileNotFoundError(
                f"Universe manifest missing: {self._MANIFEST_PATH}. "
                "Create config/universe/_manifest.json before using UniverseLoader."
            )
        return json.loads(self._MANIFEST_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- role list
    @lru_cache(maxsize=64)
    def get(self, role: str) -> List[str]:
        """Resolve a role (leaf or composite) to a sorted, de-duplicated ticker list.

        Leaf roles map 1:1 to a JSON file in ``config/universe/`` via the
        ``roles`` section of the manifest. Composite roles are declared under
        ``composite_roles`` as a union of other roles.
        """
        mf = self._manifest()
        if role in mf.get("roles", {}):
            rel = mf["roles"][role]["file"]
            path = self._MANIFEST_PATH.parent / rel
            tickers = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(tickers, list):
                raise ValueError(f"Ticker file {path} is not a JSON list.")
            return sorted({str(t).upper() for t in tickers})

        if role in mf.get("composite_roles", {}):
            members = mf["composite_roles"][role]["union_of"]
            out: set = set()
            for m in members:
                out.update(self.get(m))
            return sorted(out)

        raise KeyError(
            f"Unknown universe role '{role}'. "
            f"Known leaf roles: {list(mf.get('roles', {}).keys())}; "
            f"composite: {list(mf.get('composite_roles', {}).keys())}."
        )

    # ------------------------------------------------------------- metadata
    def role_metadata(self, role: str) -> Dict:
        """Return the manifest entry for a leaf role (asset_class, has_options, …)."""
        mf = self._manifest()
        if role in mf.get("roles", {}):
            return dict(mf["roles"][role])
        if role in mf.get("composite_roles", {}):
            return dict(mf["composite_roles"][role])
        raise KeyError(role)

    def role_of(self, ticker: str) -> Optional[str]:
        """Reverse-lookup: which leaf role does ``ticker`` belong to?"""
        ticker = str(ticker).upper()
        for role in self._manifest().get("roles", {}):
            if ticker in self.get(role):
                return role
        return None

    def asset_class_of(self, ticker: str) -> str:
        """Return the ``asset_class`` declared on the leaf role owning ``ticker``.
        Falls back to ``"unknown"`` so callers never crash on an un-tagged ticker.
        """
        role = self.role_of(ticker)
        if role is None:
            return "unknown"
        meta = self.role_metadata(role)
        return str(meta.get("asset_class", "unknown"))

    # ------------------------------------------------------------- cik map
    @lru_cache(maxsize=1)
    def cik_map(self) -> Dict[str, str]:
        """Load the static ticker -> CIK mapping from the canonical
        ``config/reference/ticker_to_cik.json``. Returns an empty dict and
        logs an error if the file is missing — run
        ``Scripts/tools/SEC_generate_cik_map.py`` to (re)generate it.
        """
        if self._CIK_MAP_PATH.exists():
            mapping = json.loads(self._CIK_MAP_PATH.read_text(encoding="utf-8"))
            logger.info(
                f"✅ cik_map loaded from {self._CIK_MAP_PATH} ({len(mapping)} entries)"
            )
            return mapping
        logger.error(
            f"❌ ticker_to_cik.json not found at {self._CIK_MAP_PATH}. "
            "Run Scripts/tools/SEC_generate_cik_map.py to generate it."
        )
        return {}


# ============================================================================
# 2. PipelineConfig — non-ticker parameters per pipeline
# ============================================================================

class PipelineConfig:
    """Lazy loader for ``config/pipeline/<name>.json`` files."""

    _DIR = CONFIG_ROOT / "pipeline"

    @lru_cache(maxsize=16)
    def load(self, name: str) -> Dict:
        path = self._DIR / f"{name}.json"
        if not path.exists():
            logger.warning(f"PipelineConfig: {path} not found, returning empty dict.")
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


# ============================================================================
# 3. UniversePaths — storage-strategy aware output paths
# ============================================================================

class UniversePaths:
    """Strategy-aware output-path resolver.

    The active strategy is read from ``config/pipeline/options_history.json``
    (key ``storage.strategy``). An explicit ``strategy=`` kwarg always wins over
    the config default — useful for A/B-running Hive writes alongside legacy.

    The default (``legacy``) path reproduces exactly what the existing
    ``snapshot_daily_options_chain`` writes today, so migrating callers to
    this helper is a no-op on disk.
    """

    def __init__(
        self,
        loader: Optional[UniverseLoader] = None,
        pipeline: Optional[PipelineConfig] = None,
        data_root: Optional[Path] = None,
    ) -> None:
        self.loader = loader or UniverseLoader()
        self.pipeline = pipeline or PipelineConfig()
        self.data_root = Path(data_root) if data_root else DATA_ROOT

    # -------------------------------------------------- options parquet path
    def options_parquet_path(
        self,
        symbol: str,
        snapshot_date: str,
        strategy: Optional[str] = None,
    ) -> Path:
        """Return the parquet path for an options-chain snapshot.

        Parameters
        ----------
        symbol
            Ticker, e.g. ``"SPY"``. Case-insensitive; written uppercase on disk.
        snapshot_date
            ISO date string ``YYYY-MM-DD``.
        strategy
            Override the config-declared ``storage.strategy``. One of
            ``"legacy"``, ``"hive_v1"``, ``"monthly_rollup"``. ``None`` uses
            the config default.

        Notes
        -----
        * ``legacy`` is the default and is byte-identical to the pre-migration
          layout (``Options_Market_Data/<date>/<SYMBOL>_options_<date>.parquet``).
        * ``hive_v1`` inserts ``snapshot_date=<date>/asset_class=<class>/``
          before the file name. Intended for DuckDB ``hive_partitioning=1``.
        * ``monthly_rollup`` writes the per-day file under ``raw/``; roll-up
          compaction is a separate job (see options_history.json comments).
        """
        sym = symbol.upper()
        base = self.data_root / "2_Silver_Processed" / "Options_Market_Data"

        active = strategy or self._active_options_strategy()

        if active == "legacy":
            return base / snapshot_date / f"{sym}_options_{snapshot_date}.parquet"

        if active == "hive_v1":
            asset_class = self.loader.asset_class_of(sym)
            return (
                base
                / f"snapshot_date={snapshot_date}"
                / f"asset_class={asset_class}"
                / f"{sym}.parquet"
            )

        if active == "monthly_rollup":
            # Raw-tier only; rollup is produced by a downstream compaction job.
            return base / "raw" / f"snapshot_date={snapshot_date}" / f"{sym}.parquet"

        raise ValueError(
            f"Unknown storage strategy '{active}'. "
            "Allowed: legacy | hive_v1 | monthly_rollup."
        )

    def _active_options_strategy(self) -> str:
        cfg = self.pipeline.load("options_history")
        return (cfg.get("storage") or {}).get("strategy", "legacy")


# ============================================================================
# 4. Module-level singletons
# ============================================================================

universe: UniverseLoader = UniverseLoader()
pipelines: PipelineConfig = PipelineConfig()
paths: UniversePaths = UniversePaths(loader=universe, pipeline=pipelines)

__all__ = [
    "UniverseLoader",
    "PipelineConfig",
    "UniversePaths",
    "universe",
    "pipelines",
    "paths",
    "PROJECT_ROOT",
    "CONFIG_ROOT",
    "DATA_ROOT",
]
