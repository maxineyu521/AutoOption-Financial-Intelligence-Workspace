"""
Scripts/retrieval/sql_tools.py

Institutional-grade quantitative retrieval tool for the Silver Layer.

Design pillars:
1. Ontology decoupling: metric whitelist and mapping come from Scripts.core.financial_ontology.
2. Fuzzy metric resolution: LLM-extracted metric names are canonicalised via difflib-backed
   ontology matching. Ticker symbols are NEVER fuzzy-matched — they remain strict.
3. Dynamic Time Anchor: CURRENT_DATE usage is replaced with anchors derived from
   config/runtime/collect_data_state.json (last_run_keys), keeping SQL correct during
   backfills, weekends and holidays.
4. Aggregation engine: computes derived metrics such as Put/Call Ratio (PCR).
5. Auditability: daily-partitioned structured logs capture SQL, latency and lineage anchors.
6. Path robustness: expands '~' and resolves absolute paths on start-up.
"""

import os
import glob as _glob_mod
import json
import difflib
import duckdb
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Set
from dotenv import load_dotenv

# Core ontology (metrics whitelist + metric -> physical column map).
# Categories are for Gold/Qdrant prompts, not Silver SQL routing.
from Scripts.core.financial_ontology import (
    ALLOWED_METRICS,
    METRIC_TO_COLUMN_MAPPING,
    ETF_TO_MACRO_ALIAS,
)
# Shared retrieval contract:
# - MetadataExtraction: LLM structured output consumed by every handler.
# - time_window_to_days / TIME_WINDOW_DAYS: global TimeWindow policy, same one
#   used by qdrant_retriever.py (Gold Layer). Single source of truth.
from Scripts.retrieval.schema import MetadataExtraction, TIME_WINDOW_DAYS, time_window_to_days
# Per-source time predicates (landed 2026-04-22). The Silver handlers can
# optionally consume a pre-compiled `TimePredicate` from master_retriever so
# monthly GPR queries get month-widened windows and daily sources get
# weekend-safe windows — without re-deriving the policy per handler.
from Scripts.retrieval.time_adapter import SourceTimeKey, TimePredicate

# Load environment variables
load_dotenv()

class SilverSQLTool:
    # -----------------------------------------------------------------
    # Dataset -> last_run_keys mapping (see config/runtime/collect_data_state.json).
    # Used by _get_anchor_date() to build a Dynamic Time Anchor instead of
    # relying on CURRENT_DATE — keeps SQL correct during backfills, weekends,
    # and holidays when wall-clock and actual data-latest diverge.
    # -----------------------------------------------------------------
    _ANCHOR_KEY: Dict[str, str] = {
        "options": "options_daily",
        "macro":   "macro_trading_daily",
        "gpr":     "gpr_monthly",
        "news":    "news_daily",
    }

    # -----------------------------------------------------------------
    # Silver-Layer physical contract.
    # Every dataset here is validated at startup via _validate_parquet_contract():
    #   1. The glob resolves to >= 1 parquet file on disk.
    #   2. The actual parquet schema contains every column this tool consumes.
    # Drift (rename, missing column, moved folder) is surfaced in the daily
    # audit log as SCHEMA_CHECK | status=MISSING_COLUMNS / NO_FILES / READ_FAILED
    # so wrong-path regressions (e.g. GPR_History vs GPR_index) no longer
    # degrade into silent empty results.
    # -----------------------------------------------------------------
    _EXPECTED_SCHEMA: Dict[str, Dict[str, Any]] = {
        "options": {
            "glob_attr": "options_glob",
            "required": {
                "snapshot_date", "symbol", "option_type", "volume",
                "open_interest", "implied_volatility", "moneyness_pct",
                "spread_pct", "dte", "is_liquid", "contract_symbol",
            },
        },
        "macro": {
            "glob_attr": "macro_glob",
            "required": {
                "observation_date", "symbol", "value",
                "daily_change_pct", "mom_change_pct",
            },
        },
        "gpr": {
            "glob_attr": "gpr_path",
            "required": {"date", "gpr", "gpr_percentile", "gpr_mom_pct"},
        },
    }

    def __init__(self):
        # 1) Robust path resolution (expands ~, resolves env).
        raw_root = os.getenv("DATA_LAKE_ROOT", "./Data")
        self.data_root = Path(raw_root).expanduser().resolve()
        self._init_paths()

        # 2) Runtime data-freshness state (dynamic time anchors).
        self.runtime_state = self._load_runtime_state()

        # 3) Rolling audit logger (daily partitioned).
        self.audit_logger = self._setup_audit_logger()

        # 4) High-performance in-memory DuckDB session.
        self.conn = duckdb.connect(database=':memory:')
        self.conn.execute("SET threads TO 4; SET memory_limit='2GB';")

        # 4b) Deep Silver-Layer contract validation (path + column schema).
        #     Non-fatal — dev environments with partial data still boot, but
        #     misalignments are loudly logged so they can't rot silently.
        self._validate_parquet_contract()

        # 5) Metric dispatch table.
        # Keys MUST exist as-is (or resolve via fuzzy match) in
        # Scripts.core.financial_ontology.METRIC_TO_COLUMN_MAPPING.
        # Grouped by data domain for readability.
        self.metric_dispatcher = {
            # --- Options: IV-centric -----------------------------------
            "IV Skew":                 self._handle_options_analysis,
            "Implied Volatility (IV)": self._handle_options_analysis,
            "Implied Volatility":      self._handle_options_analysis,

            # --- Options: liquidity / flow -----------------------------
            # All three share the same physical columns (volume, OI,
            # is_liquid, spread_pct) so they route to the same handler.
            # Keeping distinct keys helps the Analyst prompt reason about
            # intent even though SQL is identical.
            "Options Liquidity":   self._handle_liquidity_analysis,
            "Open Interest":       self._handle_liquidity_analysis,
            "Options Volume":      self._handle_liquidity_analysis,
            "Institutional Flows": self._handle_liquidity_analysis,  # legacy alias
            "Institutional Flow":  self._handle_liquidity_analysis,  # legacy alias

            # --- Options: pricing / spread -----------------------------
            # Dedicated handler so quote/spread queries don't hijack the
            # IV-centric _handle_options_analysis result shape.
            "Options Pricing / Spread": self._handle_pricing_spread,
            "Underlying Price":         self._handle_pricing_spread,

            # --- Options: aggregates -----------------------------------
            "Put/Call Ratio": self._handle_put_call_ratio,

            # --- Macro series ------------------------------------------
            # "Price" / "Price Change (%)" apply to both macro symbols
            # (^GSPC, FEDFUNDS, …) and options underlyings. The handler
            # applies the ETF_TO_MACRO_ALIAS translation so SPY/QQQ land
            # on ^GSPC/^IXIC — see _handle_macro_analysis below.
            "Macro Trend":       self._handle_macro_analysis,
            "Price":             self._handle_macro_analysis,
            "Price Change (%)":  self._handle_macro_analysis,
            "Daily Change (%)":  self._handle_macro_analysis,
            "Monthly Change (%)":self._handle_macro_analysis,
            "Yearly Change (%)": self._handle_macro_analysis,

            # --- GPR ----------------------------------------------------
            "GPR Index":         self._handle_geopolitical_analysis,
            "GPR Threats":       self._handle_geopolitical_analysis,
            "GPR Acts":          self._handle_geopolitical_analysis,
            "GPR Components":    self._handle_geopolitical_analysis,
            "GPR Country-level": self._handle_geopolitical_analysis,
        }

    def _init_paths(self):
        """Build robust Parquet globs and warn when DATA_LAKE_ROOT is missing.

        Folder names below MUST match the Silver-Layer writers exactly:
          - Options  : Scripts/data_collection/scrapers/yfinance_options_history.py
                       writes <DATA_LAKE_ROOT>/2_Silver_Processed/Options_Market_Data/
                               <YYYY-MM-DD>/<SYMBOL>_options_<YYYY-MM-DD>.parquet
          - Macro    : Scripts/data_collection/scrapers/macro_data_pipeline.py
                       writes <DATA_LAKE_ROOT>/2_Silver_Processed/Macro_History/
                               <YYYY-MM-DD>/macro_snapshot_<YYYY-MM-DD>.parquet
          - GPR      : Scripts/data_collection/scrapers/GPR_index.py
                       writes <DATA_LAKE_ROOT>/2_Silver_Processed/GPR_index/
                               gpr_monthly_enriched.parquet   (NOTE: lowercase 'index',
                               single canonical file — writer overwrites in place).
        Any drift is surfaced loudly by _validate_parquet_contract().
        """
        self.options_glob = str(self.data_root / "2_Silver_Processed/Options_Market_Data/*/*.parquet")
        self.macro_glob = str(self.data_root / "2_Silver_Processed/Macro_History/*/*.parquet")
        self.gpr_path = str(self.data_root / "2_Silver_Processed/GPR_index/*.parquet")

        # Startup sanity check (robust against missing data lake).
        if not self.data_root.exists():
            print(f"⚠️ Warning: DATA_LAKE_ROOT {self.data_root} does not exist.")

    def _setup_audit_logger(self):
        """Daily-partitioned audit log at logs/Parquet_Query/YYYY-MM-DD/sql_retrieval_audit.log."""
        today = datetime.now().strftime("%Y-%m-%d")
        log_dir = Path(f"logs/Parquet_Query/{today}")
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "sql_retrieval_audit.log"

        logger = logging.getLogger("SilverSQLAudit")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            fh = logging.FileHandler(log_file, encoding='utf-8')
            # Structured line format, friendly for ELK / downstream log analytics.
            formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        return logger

    # =========================================================
    # Startup contract validation (Silver-Layer deep alignment check)
    # =========================================================
    def _validate_parquet_contract(self) -> None:
        """
        Deep path + column alignment check against _EXPECTED_SCHEMA.

        For every Silver dataset this tool consumes, verify:
          1. The resolved glob expands to at least one parquet file on disk
             (catches folder renames such as 'GPR_History' vs 'GPR_index').
          2. The physical parquet schema contains every column the handler
             SQL references (catches upstream writer schema drift).

        Emitted audit lines (one per dataset):
          SCHEMA_CHECK | dataset=<name> | glob=<...> | files=<n>
                       | columns=<n> | missing=<list> | status=<OK|...>

        Statuses:
          OK                 — path resolves, all required columns present.
          NO_FILES           — glob matches zero parquet files.
          MISSING_COLUMNS    — physical schema is missing expected columns.
          READ_FAILED        — DuckDB could not probe the parquet (corrupt
                               / unreadable / bad path).
        This method is NON-FATAL by design: dev environments without a full
        data lake must still boot. Any failure is loud in the audit log.
        """
        for dataset, spec in self._EXPECTED_SCHEMA.items():
            glob_pattern = getattr(self, spec["glob_attr"], None)
            required: Set[str] = set(spec["required"])

            if not glob_pattern:
                self.audit_logger.error(
                    f"SCHEMA_CHECK | dataset={dataset} | status=NO_GLOB_ATTR"
                )
                continue

            files = _glob_mod.glob(glob_pattern)
            if not files:
                self.audit_logger.error(
                    f"SCHEMA_CHECK | dataset={dataset} | glob={glob_pattern} "
                    f"| files=0 | status=NO_FILES"
                )
                continue

            try:
                # DESCRIBE is cheap: it reads only Parquet footer metadata.
                desc_df = self.conn.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{glob_pattern}') LIMIT 0"
                ).df()
                actual_cols = set(desc_df["column_name"].tolist())
                missing = required - actual_cols
                status = "OK" if not missing else "MISSING_COLUMNS"
                self.audit_logger.info(
                    f"SCHEMA_CHECK | dataset={dataset} | glob={glob_pattern} "
                    f"| files={len(files)} | columns={len(actual_cols)} "
                    f"| missing={sorted(missing)} | status={status}"
                )
            except Exception as e:
                self.audit_logger.error(
                    f"SCHEMA_CHECK | dataset={dataset} | glob={glob_pattern} "
                    f"| files={len(files)} | status=READ_FAILED "
                    f"| error={str(e)[:200]}"
                )

    # =========================================================
    # Structured SQL time-range audit (one line per handler invocation)
    # =========================================================
    def _audit_sql_range(
        self,
        handler: str,
        ticker: Optional[str],
        meta: MetadataExtraction,
        mode: str,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        window_days: Optional[int] = None,
        extra: Optional[str] = None,
    ) -> None:
        """
        Emit a structured SQL_RANGE line *before* executing a handler query.

        Two modes captured:
          - mode=window       : explicit WHERE filter on a date column.
                                start / end / window_days are the filter params.
          - mode=latest_only  : ORDER BY <date> DESC LIMIT 1 (no WHERE on date).
                                end = anchor / latest-known data date; window_days
                                is logged for traceability but NOT in the SQL.

        Format (pipe-separated for log analytics):
          SQL_RANGE | handler=<> | ticker=<> | mode=<> | time_window=<enum>
                    | start=<> | end=<> | window_days=<> | extra=<>
        """
        tw_val = getattr(meta.time_window, "value", meta.time_window) if meta else None
        parts = ["SQL_RANGE", f"handler={handler}"]
        if ticker is not None:
            parts.append(f"ticker={ticker}")
        parts.append(f"mode={mode}")
        parts.append(f"time_window={tw_val}")
        if start is not None:
            parts.append(f"start={start}")
        if end is not None:
            parts.append(f"end={end}")
        if window_days is not None:
            parts.append(f"window_days={window_days}")
        if extra:
            parts.append(f"extra={extra}")
        self.audit_logger.info(" | ".join(parts))

    # =========================================================
    # Dynamic Time-Anchor helpers (replaces CURRENT_DATE semantics)
    # =========================================================
    def _load_runtime_state(self) -> Dict[str, Any]:
        """
        Load dataset-freshness state from config/runtime/collect_data_state.json.
        Returns the raw dict. If the file is missing/broken this is NON-FATAL:
        _get_anchor_date() falls back to date.today() so the tool stays usable
        in dev environments that don't have the runtime state file yet.
        """
        try:
            # project root = Scripts/retrieval/sql_tools.py -> parents[2]
            project_root = Path(__file__).resolve().parents[2]
            state_path = project_root / "config" / "runtime" / "collect_data_state.json"
            if state_path.exists():
                with open(state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            # Audit logger may not exist yet at __init__ time — keep this as print.
            print(f"⚠️ SilverSQLTool: failed to load runtime state: {e}")
        return {}

    def _get_anchor_date(self, dataset: str) -> date:
        """
        Return the most recently verified data date for `dataset`, derived from
        config/runtime/collect_data_state.json. This is the institutional
        replacement for CURRENT_DATE — it survives weekends, holidays and
        backfill runs (wall-clock != last successful data ingestion).
        Accepts both 'YYYY-MM-DD' daily keys and 'YYYY-MM' monthly keys.
        Falls back to today() if no state key is available.
        """
        key = self._ANCHOR_KEY.get(dataset)
        last_keys = (self.runtime_state or {}).get("last_run_keys", {})
        raw = last_keys.get(key) if key else None
        if raw:
            try:
                if len(raw) == 7:   # Monthly: "YYYY-MM"
                    return datetime.strptime(raw + "-01", "%Y-%m-%d").date()
                if len(raw) == 10:  # Daily:   "YYYY-MM-DD"
                    return datetime.strptime(raw, "%Y-%m-%d").date()
            except Exception:
                pass
        return date.today()

    def _time_window_to_days(self, tw: Any, default: int = 5) -> int:
        """
        Translate metadata.time_window (str | TimeWindow) into a day count via the
        global TIME_WINDOW_DAYS policy in Scripts.retrieval.schema. Thin wrapper
        so every handler reads through the same API — never hard-code day counts.
        """
        return time_window_to_days(tw, default=default)

    # =========================================================
    # Metric fuzzy matching (ontology-aware; ticker is NEVER fuzzy)
    # =========================================================
    @staticmethod
    def _normalize(s: str) -> str:
        """Lowercase + strip non-alphanumerics to collapse punctuation/space variants."""
        return "".join(ch for ch in str(s).lower() if ch.isalnum())

    def _fuzzy_match_metric(self, metric: str) -> Optional[str]:
        """
        Map the LLM-extracted metric label to a canonical dispatcher key.
        Resolution order:
          1. Exact match on dispatcher key.
          2. Case/punctuation-insensitive exact match.
          3. difflib close-match against dispatcher keys + ontology
             (ALLOWED_METRICS and METRIC_TO_COLUMN_MAPPING keys).
        Ticker symbols NEVER pass through this path — they remain strict
        .upper().strip() lookups to prevent SQL executing against the wrong symbol.
        """
        if not metric:
            return None
        if metric in self.metric_dispatcher:
            return metric

        norm_target = self._normalize(metric)
        # Normalized exact match (covers "implied volatility" vs "Implied Volatility (IV)").
        for key in self.metric_dispatcher.keys():
            if self._normalize(key) == norm_target:
                return key

        # Ontology-aware fuzzy candidates — union of every known metric surface
        # form so LLM-side aliases still route to a valid handler.
        candidates = (
            list(self.metric_dispatcher.keys())
            + list(ALLOWED_METRICS)
            + list(METRIC_TO_COLUMN_MAPPING.keys())
        )
        norm_map: Dict[str, str] = {}
        for k in candidates:
            norm_map.setdefault(self._normalize(k), k)

        match = difflib.get_close_matches(
            norm_target, list(norm_map.keys()), n=1, cutoff=0.75
        )
        if match:
            canonical = norm_map[match[0]]
            # Only return if we can actually dispatch it — ontology members
            # without a handler must surface as UNAUTHORIZED_METRIC, not silently drop.
            return canonical if canonical in self.metric_dispatcher else None
        return None

    def _partition_requested_metrics(
        self, metrics: List[str]
    ) -> tuple[List[tuple[str, str]], List[str]]:
        """Split user-requested metrics into supported and unsupported buckets.

        Returns
        -------
        supported
            List of `(raw_metric, canonical_metric_key)` pairs that can be
            dispatched to a SQL handler.
        unsupported
            Raw metric labels that cannot be mapped to a whitelisted handler.
        """
        supported: List[tuple[str, str]] = []
        unsupported: List[str] = []
        for raw_metric in metrics or []:
            canonical = self._fuzzy_match_metric(raw_metric)
            if canonical is None:
                unsupported.append(raw_metric)
            else:
                supported.append((raw_metric, canonical))
        return supported, unsupported

    @staticmethod
    def _build_unsupported_metric_message(unsupported: List[str]) -> str:
        if not unsupported:
            return ""
        listed = ", ".join(sorted({str(m) for m in unsupported}))
        return (
            "Unsupported metric(s) requested: "
            f"{listed}. Supported examples: Put/Call Ratio, Implied Volatility (IV), "
            "IV Skew, Options Liquidity, Macro Trend, Daily/Monthly/Yearly Change (%), GPR Index."
        )

    # Cap on tickers actually dispatched. Even if the Transformer produces
    # N tickers, we execute at most this many SQL round-trips per request
    # to keep Silver latency bounded (each ticker = 1 DuckDB read per
    # metric). Excess tickers are logged and dropped LOUDLY.
    _MAX_TICKERS_PER_QUERY = int(os.getenv("SILVER_MAX_TICKERS", "5"))

    # Per-call predicate cache. Set at the top of query_parquet_by_metadata
    # so handlers can look up their source's window via
    # `self._current_predicates.get(SourceTimeKey.SILVER_*)`. Cleared at the
    # end of the call so the tool stays thread-ish-safe for sequential awaits
    # (DuckDB connection is already single-threaded).
    _current_predicates: Optional[Dict[SourceTimeKey, TimePredicate]] = None

    def _predicate_for(self, key: SourceTimeKey) -> Optional[TimePredicate]:
        """Return the compiled predicate for a Silver source, or None when the
        caller didn't supply `time_predicates`. Handlers that want per-source
        alignment should branch on `None` and fall back to the legacy
        `_time_window_to_days` path for backward compatibility.
        """
        if not self._current_predicates:
            return None
        return self._current_predicates.get(key)

    async def query_parquet_by_metadata(
        self,
        metadata: MetadataExtraction,
        time_predicates: Optional[Dict[SourceTimeKey, TimePredicate]] = None,
    ) -> Dict[str, Any]:
        """Main entry: route extracted metrics to DuckDB handlers and aggregate results.

        Fixes landed here (2026-04-22):
          1. **Iterates ALL tickers** (capped at _MAX_TICKERS_PER_QUERY) instead
             of silently using only tickers[0]. Today's Q3 audit shows 57
             tickers in metadata but anchors for only 1 — root cause.
          2. Results carry per-ticker lineage so the Analyst can cite
             "data anchor for SPY" vs "data anchor for QQQ" precisely.
          3. **Per-source time alignment** — when `time_predicates` is
             provided by MasterRetriever, handlers read a pre-compiled
             `TimePredicate` that already handled MONTHLY-widening for GPR
             and weekend-safety for DAILY sources. Handlers that don't need
             it are a no-op.
        """
        start_ts = datetime.now()
        results: Dict[str, Any] = {
            "values": {},
            "lineage_anchors": [],
            "status": {
                "unsupported_metrics": [],
                "unsupported_metrics_message": "",
                "error": None,
            },
        }
        # Publish predicates on the instance so handlers can pull them
        # without changing every handler signature. Cleared in the finally.
        self._current_predicates = time_predicates

        supported_metrics, unsupported_metrics = self._partition_requested_metrics(metadata.metrics or [])
        if unsupported_metrics:
            msg = self._build_unsupported_metric_message(unsupported_metrics)
            results["status"]["unsupported_metrics"] = unsupported_metrics
            results["status"]["unsupported_metrics_message"] = msg
            self.audit_logger.warning(f"UNSUPPORTED_METRICS | metrics={unsupported_metrics}")

        if not supported_metrics:
            # Fast-fail: no SQL should run when every metric is outside the
            # Silver whitelist. Return an explicit user-facing message so the
            # caller can surface a deterministic explanation.
            results["status"]["error"] = "NO_SUPPORTED_METRICS"
            if not results["status"]["unsupported_metrics_message"]:
                results["status"]["unsupported_metrics_message"] = (
                    "No supported metric found in the request."
                )
            self._current_predicates = None
            return results

        if not metadata.tickers:
            self.audit_logger.warning("EMPTY_TICKERS | Skipping SQL execution.")
            self._current_predicates = None
            return results

        # Ticker MUST stay exact — we only ever .upper() + .strip() it, no fuzzy match.
        tickers_all = [t.upper().strip() for t in metadata.tickers if t and t.strip()]
        tickers = tickers_all[: self._MAX_TICKERS_PER_QUERY]
        if len(tickers_all) > len(tickers):
            self.audit_logger.warning(
                f"TICKER_CAP | received={len(tickers_all)} executing={len(tickers)} "
                f"dropped={tickers_all[len(tickers):]}"
            )
        self.audit_logger.info(
            f"START_QUERY | Tickers: {tickers} | Requested: {metadata.metrics} | "
            f"Supported: {[cm for _, cm in supported_metrics]}"
        )

        for ticker in tickers:
            for metric, canonical_key in supported_metrics:
                if canonical_key != metric:
                    self.audit_logger.info(f"FUZZY_MATCH | '{metric}' -> '{canonical_key}'")

                handler = self.metric_dispatcher.get(canonical_key)
                if not handler:
                    continue

                try:
                    res = handler(ticker, metadata)
                    if res:
                        results["values"].update(res.get("values", {}))
                        results["lineage_anchors"].extend(res.get("lineage_anchors", []))
                except Exception as e:
                    self.audit_logger.error(
                        f"EXECUTION_ERROR | Ticker: {ticker} | Metric: {metric} | Error: {str(e)}"
                    )

        latency = (datetime.now() - start_ts).total_seconds()
        self.audit_logger.info(
            f"FINISH_QUERY | Latency: {latency:.3f}s | Anchors_Found: {len(results['lineage_anchors'])}"
        )
        # Always clear — predicates are per-call and must not leak to the
        # next metadata invocation.
        self._current_predicates = None
        return results

    # =========================================================
    # Business handlers — deep mapping of schema metrics.
    # =========================================================

    def _handle_put_call_ratio(self, ticker: str, meta: MetadataExtraction) -> Dict[str, Any]:
        """
        Put/Call Ratio with a Dynamic Time Anchor.
        - Anchor date is sourced from config/runtime/collect_data_state.json (options_daily),
          replacing CURRENT_DATE so backfills, weekends, and holidays are handled correctly.
        - Lookback window honors metadata.time_window via the global TIME_WINDOW_DAYS policy.
          Explicit short windows (today / yesterday) are HONOURED verbatim so PCR and IV-skew
          anchor on the SAME day — production audit on 2026-04-22 showed a previous
          `max(5, …)` floor caused `yesterday` to return a 5-day average while IV Skew
          returned a 2-day snapshot, producing inconsistent time semantics in one report.
          The 5-day floor is still applied ONLY when the user gave no time signal at all
          (raw time_window empty / unknown), preserving the original "short-sample noise"
          guard for under-specified queries.
        - Per-source time alignment (2026-04-22): when MasterRetriever supplied a
          pre-compiled `TimePredicate` for `silver.options`, we honour its
          (start_date, end_date) directly — this is weekend-safe (the adapter
          widens DAILY sources to a 3-day floor) so Monday-anchored "yesterday"
          queries automatically reach back to Friday without per-handler logic.
        - Parameters are SQL-bound (no string interpolation) — ticker stays strict.
        """
        anchor = self._get_anchor_date("options")
        predicate = self._predicate_for(SourceTimeKey.SILVER_OPTIONS)
        tw_val = getattr(meta.time_window, "value", meta.time_window)

        if predicate is not None:
            # Adapter already resolved weekend-safety + min-lookback; use it.
            start_date = predicate.start_date
            anchor = predicate.end_date  # source-aligned anchor (same day by default)
            window_days = predicate.window_days
        else:
            # Legacy path — honour explicit short-window intents, apply the
            # 5-day floor only when the user gave no time signal at all.
            explicit_short = isinstance(tw_val, str) and tw_val.lower() in {"today", "yesterday", "past_week"}
            if explicit_short:
                window_days = self._time_window_to_days(meta.time_window, default=2)
            else:
                window_days = max(5, self._time_window_to_days(meta.time_window, default=5))
            start_date = anchor - timedelta(days=window_days)

        # Explicit SQL time-range audit: this handler applies a real WHERE filter,
        # so start/end/window_days are logged verbatim for traceability.
        self._audit_sql_range(
            handler="put_call_ratio", ticker=ticker, meta=meta,
            mode="window", start=start_date, end=anchor, window_days=window_days,
        )

        query = f"""
            SELECT
                snapshot_date,
                SUM(CASE WHEN option_type = 'put'  THEN volume        ELSE 0 END) as put_vol,
                SUM(CASE WHEN option_type = 'call' THEN volume        ELSE 0 END) as call_vol,
                SUM(CASE WHEN option_type = 'put'  THEN open_interest ELSE 0 END) as put_oi,
                SUM(CASE WHEN option_type = 'call' THEN open_interest ELSE 0 END) as call_oi
            FROM read_parquet('{self.options_glob}')
            WHERE symbol = ?
              AND CAST(snapshot_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            GROUP BY snapshot_date
            ORDER BY snapshot_date DESC
            LIMIT 1
        """
        df = self.conn.execute(query, [ticker, start_date, anchor]).df()
        if df.empty: return None

        row = df.iloc[0]
        pcr_vol = row['put_vol'] / row['call_vol'] if row['call_vol'] > 0 else 0
        pcr_oi = row['put_oi'] / row['call_oi'] if row['call_oi'] > 0 else 0

        return {
            "values": {
                "pcr_volume": round(pcr_vol, 3),
                "pcr_open_interest": round(pcr_oi, 3),
                "pcr_status": "Bearish Sentiment" if pcr_vol > 1.0 else "Bullish/Neutral"
            },
            "lineage_anchors": [f"PCR_AGG_{ticker}_{row['snapshot_date']}"]
        }

    def _handle_options_analysis(self, ticker: str, meta: MetadataExtraction) -> Dict[str, Any]:
        """Surface latest ATM IV + IV rank percentile + freshness.

        IV rank is computed as a rolling historical percentile over daily ATM IV
        observations for the same ticker. This makes regime classification
        stable across assets (percentile space) instead of using hard IV levels.
        """
        # This handler applies no WHERE filter on snapshot_date; it picks the
        # latest available row. Log the anchor + requested window so the audit
        # trail still captures intended temporal context (useful when debugging
        # "why did the model see data older than expected").
        anchor = self._get_anchor_date("options")
        window_days = self._time_window_to_days(meta.time_window, default=180)
        iv_rank_lookback_days = int(os.getenv("IV_RANK_LOOKBACK_DAYS", "180"))
        rank_start = anchor - timedelta(days=iv_rank_lookback_days)
        self._audit_sql_range(
            handler="options_analysis", ticker=ticker, meta=meta,
            mode="latest_only", end=anchor, window_days=window_days,
            extra=f"filter=is_liquid=true;dte_in_[7,45];iv_rank_lookback={iv_rank_lookback_days}d",
        )

        query = f"""
            WITH raw AS (
                SELECT
                    CAST(snapshot_date AS DATE) AS snapshot_date,
                    contract_symbol,
                    implied_volatility,
                    ABS(moneyness_pct) AS abs_moneyness,
                    spread_pct
                FROM read_parquet('{self.options_glob}')
                WHERE symbol = ?
                  AND is_liquid = true
                  AND dte BETWEEN 7 AND 45
                  AND CAST(snapshot_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            ),
            atm_daily AS (
                SELECT
                    snapshot_date,
                    contract_symbol,
                    implied_volatility AS atm_iv,
                    ROW_NUMBER() OVER (
                        PARTITION BY snapshot_date
                        ORDER BY abs_moneyness ASC, spread_pct ASC NULLS LAST
                    ) AS rn
                FROM raw
            ),
            series AS (
                SELECT snapshot_date, contract_symbol, atm_iv
                FROM atm_daily
                WHERE rn = 1
            ),
            ranked AS (
                SELECT
                    snapshot_date,
                    contract_symbol,
                    atm_iv,
                    CASE
                        WHEN COUNT(*) OVER () >= 2
                        THEN ROUND(100.0 * PERCENT_RANK() OVER (ORDER BY atm_iv), 2)
                        ELSE NULL
                    END AS iv_rank_pct
                FROM series
            )
            SELECT snapshot_date, contract_symbol, atm_iv, iv_rank_pct
            FROM ranked
            ORDER BY snapshot_date DESC
            LIMIT 1
        """
        df = self.conn.execute(query, [ticker, rank_start, anchor]).df()
        if df.empty:
            return None

        latest_date = df["snapshot_date"].iloc[0]
        atm_iv = df["atm_iv"].iloc[0]
        iv_rank_pct = df["iv_rank_pct"].iloc[0]
        contract_symbol = df["contract_symbol"].iloc[0]
        
        iv_rank_val = None
        if iv_rank_pct is not None:
            try:
                iv_rank_f = float(iv_rank_pct)
                iv_rank_val = round(iv_rank_f, 2) if iv_rank_f == iv_rank_f else None
            except Exception:
                iv_rank_val = None

        return {
            "values": {
                "latest_atm_iv": round(atm_iv, 4),
                "latest_atm_iv_rank_pct": iv_rank_val,
                "iv_rank_lookback_days": iv_rank_lookback_days,
                "data_freshness": str(latest_date),
            },
            "lineage_anchors": [
                str(contract_symbol),
                f"IVRANK_{ticker}_{latest_date}",
            ],
        }

    def _handle_liquidity_analysis(self, ticker: str, meta: MetadataExtraction) -> Dict[str, Any]:
        """Liquidity & execution-risk snapshot.

        Upgrade note (2026-04-22):
          - Now surfaces OPEN INTEREST and LIQUID_CONTRACT_COUNT so the
            "Options Liquidity" / "Open Interest" dispatches carry the data
            the Analyst expects (previously only total_vol + avg_spread).
          - Still `latest_only` — picks the most recent snapshot_date with
            rows for this symbol so the result survives weekends/holidays.
        """
        anchor = self._get_anchor_date("options")
        window_days = self._time_window_to_days(meta.time_window, default=180)
        self._audit_sql_range(
            handler="liquidity_analysis", ticker=ticker, meta=meta,
            mode="latest_only", end=anchor, window_days=window_days,
        )

        query = f"""
            SELECT
                snapshot_date,
                SUM(volume)                               AS total_vol,
                SUM(open_interest)                        AS total_oi,
                AVG(spread_pct)                           AS avg_spread,
                SUM(CASE WHEN is_liquid THEN 1 ELSE 0 END) AS liquid_contracts
            FROM read_parquet('{self.options_glob}')
            WHERE symbol = ?
            GROUP BY snapshot_date
            ORDER BY snapshot_date DESC
            LIMIT 1
        """
        res = self.conn.execute(query, [ticker]).fetchone()
        if not res:
            return None

        total_vol = int(res[1] or 0)
        total_oi  = int(res[2] or 0)
        avg_spread = float(res[3] or 0.0)
        liquid_ct = int(res[4] or 0)

        return {
            "values": {
                f"{ticker}_daily_option_volume":  total_vol,
                f"{ticker}_open_interest":        total_oi,
                f"{ticker}_avg_spread_pct":       round(avg_spread * 100, 3),
                f"{ticker}_liquid_contracts":     liquid_ct,
                f"{ticker}_market_impact_risk":   "High" if avg_spread > 0.02 else "Low",
            },
            "lineage_anchors": [f"LIQ_{ticker}_{res[0]}"],
        }

    def _handle_pricing_spread(self, ticker: str, meta: MetadataExtraction) -> Dict[str, Any]:
        """Pricing / spread / underlying-price snapshot.

        Returns the latest-snapshot ATM-adjacent bid/ask mid so queries like
        "what's the current SPY option spread" land on quote data instead
        of the IV-centric options-analysis handler.
        """
        anchor = self._get_anchor_date("options")
        window_days = self._time_window_to_days(meta.time_window, default=180)
        self._audit_sql_range(
            handler="pricing_spread", ticker=ticker, meta=meta,
            mode="latest_only", end=anchor, window_days=window_days,
            extra="filter=is_liquid=true;dte_in_[7,45]",
        )

        query = f"""
            SELECT
                snapshot_date,
                underlying_price,
                AVG(bid)                  AS avg_bid,
                AVG(ask)                  AS avg_ask,
                AVG(spread_pct)           AS avg_spread_pct,
                AVG(last_price)           AS avg_last_price
            FROM read_parquet('{self.options_glob}')
            WHERE symbol = ?
              AND is_liquid = true
              AND dte BETWEEN 7 AND 45
            GROUP BY snapshot_date, underlying_price
            ORDER BY snapshot_date DESC
            LIMIT 1
        """
        res = self.conn.execute(query, [ticker]).fetchone()
        if not res:
            return None

        return {
            "values": {
                f"{ticker}_underlying_price": float(res[1] or 0),
                f"{ticker}_avg_bid":          round(float(res[2] or 0), 3),
                f"{ticker}_avg_ask":          round(float(res[3] or 0), 3),
                f"{ticker}_avg_spread_pct":   round(float(res[4] or 0) * 100, 3),
                f"{ticker}_avg_last_price":   round(float(res[5] or 0), 3),
            },
            "lineage_anchors": [f"PX_{ticker}_{res[0]}"],
        }

    def _handle_macro_analysis(self, ticker: str, meta: MetadataExtraction) -> Dict[str, Any]:
        """Macro-series latest-value read.

        Storage vs. query reconciliation (the ETF→index alias fix):
          - Our Macro_History parquet stores canonical *indices* (^GSPC,
            ^IXIC, FEDFUNDS, …). Users and the LLM write *ETFs* (SPY, QQQ).
          - ETF_TO_MACRO_ALIAS (config/financial_ontology.py) translates
            the common ETF tickers to the index the table actually holds.
          - Translation is LOCAL to this handler; options / SEC handlers
            keep the ETF symbol because their tables store ETFs.

        Schema note (Scripts/data_collection/scrapers/macro_data_pipeline.py):
          - Yahoo Finance / daily series  -> daily_change_pct populated, mom_change_pct = NULL.
          - FRED / monthly macro series   -> mom_change_pct populated, daily_change_pct = NULL.
          - BOTH change columns are stored ALREADY as percentage points
            (e.g. 2.35 means +2.35%). Do NOT multiply by 100 again here.
        Defensive fallback:
          - If mom_change is NULL, prefer daily_change; if both are NULL, emit 'n/a'.
        """
        anchor = self._get_anchor_date("macro")
        window_days = self._time_window_to_days(meta.time_window, default=180)

        # ETF → underlying-index translation (handler-local, audit-logged).
        query_ticker = ETF_TO_MACRO_ALIAS.get(ticker, ticker)
        alias_applied = query_ticker != ticker

        self._audit_sql_range(
            handler="macro_analysis", ticker=ticker, meta=meta,
            mode="latest_only", end=anchor, window_days=window_days,
            extra=(f"alias={ticker}->{query_ticker}" if alias_applied else None),
        )

        query = f"""
            SELECT observation_date, value, daily_change_pct, mom_change_pct
            FROM read_parquet('{self.macro_glob}')
            WHERE symbol = ?
            ORDER BY observation_date DESC LIMIT 1
        """
        res = self.conn.execute(query, [query_ticker]).fetchone()
        if not res: return None

        obs_date, last_value, daily_chg, mom_chg = res[0], res[1], res[2], res[3]
        # Lineage always records BOTH the user-facing ticker and the queried
        # symbol so the Analyst can explain the alias in the compliance footer.
        lineage_prefix = f"MACRO_{ticker}" if not alias_applied else f"MACRO_{ticker}_AS_{query_ticker}"

        if mom_chg is not None:
            change_label, change_val = f"{ticker}_mom_change", mom_chg
        elif daily_chg is not None:
            change_label, change_val = f"{ticker}_daily_change", daily_chg
        else:
            change_label, change_val = f"{ticker}_change", None

        change_str = f"{round(change_val, 2)}%" if change_val is not None else "n/a"

        return {
            "values": {
                f"{ticker}_last_value": last_value,
                change_label: change_str,
            },
            "lineage_anchors": [f"{lineage_prefix}_{obs_date}"]
        }

    def _handle_geopolitical_analysis(self, ticker: str, meta: MetadataExtraction) -> Dict[str, Any]:
        """GPR regime snapshot using gpr_percentile + gpr_mom_pct per GPR_Index schema.

        GPR is monthly; anchor resolves to the first-of-month of the last
        successfully ingested month (collect_data_state.json -> gpr_monthly).
        No WHERE filter is applied here — we always return the latest month —
        but the anchor is logged so the audit trail is still decisive.
        """
        anchor = self._get_anchor_date("gpr")
        window_days = self._time_window_to_days(meta.time_window, default=180)
        self._audit_sql_range(
            handler="geopolitical_analysis", ticker=ticker, meta=meta,
            mode="latest_only", end=anchor, window_days=window_days,
            extra="cadence=monthly",
        )

        query = f"""
            SELECT date, gpr, gpr_percentile, gpr_mom_pct
            FROM read_parquet('{self.gpr_path}')
            ORDER BY date DESC LIMIT 1
        """
        res = self.conn.execute(query).fetchone()
        if not res: return None

        mom = res[3]
        trend = "Rising" if (mom is not None and mom > 0) else ("Falling" if mom is not None else "Unknown")
        return {
            "values": {
                "gpr_index_level": res[1],
                "gpr_percentile": f"{res[2]}%",
                "gpr_trend": trend
            },
            "lineage_anchors": [f"GPR_{res[0].strftime('%Y%m')}"]
        }