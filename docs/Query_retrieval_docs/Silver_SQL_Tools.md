# Silver SQL Tooling - Institutional Specification

## 1. Goal and Control Objective

`Scripts/retrieval/sql_tools.py` is the deterministic numeric retrieval engine for the Silver layer.  
Its control objective is to convert `MetadataExtraction` into auditable DuckDB outputs without LLM-side numeric fabrication risk.

Primary guarantees:

- Metric routing is whitelist-constrained and ontology-aware.
- Ticker matching is strict (no fuzzy ticker resolution).
- Time windows are anchored to ingestion reality (not wall-clock assumptions).
- Every SQL path emits lineage anchors and structured audit logs.

## 2. Architecture (Markdown Block)

```text
[MasterRetriever]
  └── passes MetadataExtraction + optional per-source TimePredicate map
      to SilverSQLTool.query_parquet_by_metadata()
            ├── metric normalization + supported/unsupported partition
            ├── ticker cap enforcement (SILVER_MAX_TICKERS)
            ├── dispatcher route by canonical metric
            │     ├── options handlers
            │     ├── macro handlers
            │     └── geopolitical (GPR) handler
            ├── DuckDB parquet execution (parameterized where applicable)
            ├── lineage anchor assembly
            └── status payload + SQL audit logging
```

## 3. Code Strategy and Workflow

### 3.1 End-to-End Retrieval Workflow

![Retrieval workflow](../../images/Retrieval_workflow.svg)

### 3.2 SQL Computation Logic (Detailed)

Core SQL computation logic is implemented in dedicated handlers:

- `put_call_ratio`: aggregates put/call `volume` and `open_interest` by `snapshot_date`, returns latest row in active window.
- `options_analysis`: computes ATM IV and IV-rank percentile using window functions (`ROW_NUMBER`, `PERCENT_RANK`) over the lookback series.
- `liquidity_analysis`: aggregates `SUM(volume)`, `SUM(open_interest)`, `AVG(spread_pct)`, and liquid contract count.
- `pricing_spread`: computes bid/ask/last/spread snapshots for liquid contracts in DTE range.
- `macro_analysis`: returns latest macro value and change field with ETF to macro alias translation.
- `geopolitical_analysis`: returns latest GPR index, percentile, and MoM trend classification.

### 3.3 Filter and Time-Window Logic (Detailed)

#### A) Metric and ticker filters

- Metric filter path:
  - exact dispatcher match
  - normalized exact match (punctuation/case-insensitive)
  - ontology-backed fuzzy match (`difflib`, cutoff 0.75)
- Unsupported metrics are surfaced through:
  - `status.unsupported_metrics`
  - `status.unsupported_metrics_message`
- Ticker logic:
  - strict upper/strip only
  - capped by `SILVER_MAX_TICKERS`
  - overflow is logged (`TICKER_CAP`)

#### B) Dynamic anchor strategy

- Anchor source: `config/runtime/collect_data_state.json` (`last_run_keys`).
- Dataset-specific keys:
  - `options` -> `options_daily`
  - `macro` -> `macro_trading_daily`
  - `gpr` -> `gpr_monthly`
- Fallback: `date.today()` if runtime state is missing/broken.

#### C) Per-source predicate integration

When `time_predicates` are supplied from `MasterRetriever`, handlers read compiled `TimePredicate` directly:

- `silver.options` supports weekend-safe widening for short windows.
- `silver.gpr` supports monthly widening semantics.
- If predicates are absent, handlers revert to local `time_window_to_days` policy.

#### D) Handler-specific SQL filters

- `put_call_ratio`
  - `WHERE symbol = ?`
  - `CAST(snapshot_date AS DATE) BETWEEN start AND anchor`
  - grouped by date; latest row only
- `options_analysis`
  - `is_liquid = true`
  - `dte BETWEEN 7 AND 45`
  - lookback bounded by `IV_RANK_LOOKBACK_DAYS`
- `liquidity_analysis`
  - latest available date snapshot, no strict time WHERE
- `pricing_spread`
  - `is_liquid = true AND dte BETWEEN 7 AND 45`
- `macro_analysis`
  - symbol aliasing (`SPY` -> `^GSPC`, `QQQ` -> `^IXIC`)
  - latest row per symbol
- `geopolitical_analysis`
  - latest monthly row from GPR parquet

#### E) Data contract safeguards

On initialization, `_validate_parquet_contract()` verifies:

- glob resolves to files (`NO_FILES` guard),
- required columns exist (`MISSING_COLUMNS` guard),
- parquet introspection is readable (`READ_FAILED` guard).

This prevents silent drift between ingestion schemas and retrieval SQL assumptions.

## 4. Output Data Schema and Paths

### 4.1 Runtime Output Schema

| Field | Type | Description | Produced By | Path |
|---|---|---|---|---|
| `values` | `Dict[str, Any]` | Final numeric/string metrics consumed by Analyst/Checker | `query_parquet_by_metadata()` + handlers | `Scripts/retrieval/sql_tools.py` |
| `lineage_anchors` | `List[str]` | Deterministic anchor IDs used for citation traceability | Handler return payloads | `Scripts/retrieval/sql_tools.py` |
| `status.unsupported_metrics` | `List[str]` | Raw metrics that failed canonical mapping | `_partition_requested_metrics()` | `Scripts/retrieval/sql_tools.py` |
| `status.unsupported_metrics_message` | `str` | User-facing explanation with supported examples | `_build_unsupported_metric_message()` | `Scripts/retrieval/sql_tools.py` |
| `status.error` | `str \| None` | Structured fatal status (e.g., `NO_SUPPORTED_METRICS`) | `query_parquet_by_metadata()` | `Scripts/retrieval/sql_tools.py` |

### 4.2 Audit Output Paths

| Artifact | Description | Path Pattern |
|---|---|---|
| SQL handler audit log | SQL range mode, anchors, latency, schema checks | `logs/Parquet_Query/<YYYY-MM-DD>/sql_retrieval_audit.log` |

## 5. How to Test

Recommended validation flow:

```bash
python -c "from Scripts.retrieval.sql_tools import SilverSQLTool; t=SilverSQLTool(); print('sql_tool_ok', bool(t.metric_dispatcher))"
python Scripts/tests/test_router_e2e.py
python -m Scripts query "Past week SPY put/call ratio and IV skew with liquidity risk"
```

What to validate:

- unsupported metrics produce deterministic status instead of crashes,
- ticker overflow is logged and bounded,
- `lineage_anchors` are present for each successful handler,
- SQL audit file includes `SQL_RANGE`, `START_QUERY`, `FINISH_QUERY`.

## 6. Dependency Files, Documentation Links, and One-Line Commands

### 6.1 Core dependency files

- `Scripts/retrieval/master_retriever.py` (upstream orchestration and predicate injection)
- `Scripts/retrieval/time_adapter.py` (compiled source-specific time semantics)
- `Scripts/retrieval/schema.py` (shared enums, `MetadataExtraction`, time policy)
- `Scripts/core/financial_ontology.py` (metric allowlist + metric-column mapping)

### 6.2 Linked retrieval docs

- [Retrieval Architecture and Strategy](./Retrieval_Architecture_and_Strategy.md)
- [Query Intent and Transformation](./Query_intent_docs.md)
- [Qdrant Retriever Docs](./Qdrant_retriever_docs.md)
- [Time Adapter](./Time_Adapter.md)



