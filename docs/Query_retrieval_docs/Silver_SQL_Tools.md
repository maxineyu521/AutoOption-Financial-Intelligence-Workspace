# Silver SQL Tool — `sql_tools.py`

## 1. Goal

`SilverSQLTool` is the institutional-grade DuckDB interface for the Silver layer. It translates LLM-extracted `MetadataExtraction` objects (tickers + metrics + time window) into parameterised SQL queries across three physical Parquet datasets — Options, Macro, and GPR — and returns structured `{values, lineage_anchors}` payloads that the Analyst agent can cite with full data lineage.

---

## 2. Architecture

```
MetadataExtraction  +  TimePredicate set
         │
         ▼
 query_parquet_by_metadata()
         │
         ├─ Ticker cap guard (max 5 tickers, env: SILVER_MAX_TICKERS)
         ├─ Fuzzy metric → canonical key (difflib, ontology whitelist)
         │
         ▼  for each ticker × each canonical metric:
 metric_dispatcher[canonical_key](ticker, metadata)
         │
         ├─► _handle_options_analysis      → Options_Market_Data parquet
         ├─► _handle_put_call_ratio        → Options_Market_Data parquet
         ├─► _handle_liquidity_analysis    → Options_Market_Data parquet
         ├─► _handle_pricing_spread        → Options_Market_Data parquet
         ├─► _handle_macro_analysis        → Macro_History parquet
         ├─► _handle_geopolitical_analysis → GPR_index parquet
         │
         ▼
 Aggregate: {values: {...}, lineage_anchors: [...]}
         │
         ▼
 Structured audit log → logs/Parquet_Query/{YYYY-MM-DD}/sql_retrieval_audit.log
```

---

## 3. Code Strategy & Workflow

### 3.1 Initialisation Sequence

```
SilverSQLTool.__init__()
  │
  ├─ Path resolution
  │     options_glob  = Data/2_Silver_Processed/Options_Market_Data/*/*.parquet
  │     macro_glob    = Data/2_Silver_Processed/Macro_History/*/*.parquet
  │     gpr_path      = Data/2_Silver_Processed/GPR_index/*.parquet
  │
  ├─ Runtime state load  →  config/runtime/collect_data_state.json
  │     Populates _get_anchor_date("options" | "macro" | "gpr" | "news")
  │
  ├─ Audit logger setup  →  logs/Parquet_Query/{YYYY-MM-DD}/sql_retrieval_audit.log
  │
  ├─ DuckDB in-memory connection
  │     SET threads = 4;  SET memory_limit = '2GB';
  │
  └─ _validate_parquet_contract()
        For each dataset: resolve glob → confirm ≥1 file → DuckDB DESCRIBE
        → assert required columns present → emit SCHEMA_CHECK audit line
```

### 3.2 Dynamic Time Anchor

`_get_anchor_date(dataset)` reads `collect_data_state.json` and returns the last successfully ingested date. This replaces `CURRENT_DATE` everywhere so backfills, weekends, and holidays are handled correctly.

| Dataset | State key | Anchor fallback |
|:---|:---|:---|
| Options | `options_daily` | `date.today()` |
| Macro | `macro_trading_daily` | `date.today()` |
| GPR | `gpr_monthly` | first of current month |

### 3.3 Metric Dispatch & Fuzzy Matching

When `query_parquet_by_metadata` receives a metric string from the LLM (e.g. `"IV"`, `"implied vol"`), it calls `_fuzzy_match_metric()`:

1. Exact match against `metric_dispatcher` keys → use directly.
2. `difflib.get_close_matches(metric, dispatcher_keys, n=1, cutoff=0.6)` → use best match, log `FUZZY_MATCH` event.
3. No match → log `UNAUTHORIZED_METRIC`, skip.

### 3.4 Per-Source Time Alignment

When `MasterRetriever` supplies a `time_predicates` dict (compiled by `time_adapter.compile_all`), each handler reads its source's `TimePredicate` via `_predicate_for(SourceTimeKey.SILVER_*)`. This ensures the SQL `WHERE` clause uses exactly the same (start_date, end_date) window that was logged in the time-range audit — eliminating the 3d-vs-2d drift that produced inconsistent PCR / IV-skew windows on the same report.

### 3.5 Handler Reference

| Handler | Parquet | Key SQL Logic | Output Keys |
|:---|:---|:---|:---|
| `_handle_options_analysis` | Options | ATM IV from nearest-to-ATM liquid calls; skew from OTM puts; `dte BETWEEN 7 AND 45`, `is_liquid = true`, `mode = latest_only` | `latest_atm_iv`, `{T}_iv_skew_{D}d`, `{T}_skew_direction` |
| `_handle_put_call_ratio` | Options | Aggregate `put/call` volume + OI by snapshot date, window or latest | `pcr_volume`, `pcr_open_interest`, `pcr_status` |
| `_handle_liquidity_analysis` | Options | Count liquid call/put contracts; top volume strike | `total_liquid_calls`, `total_liquid_puts`, `top_volume_strike_call/put` |
| `_handle_pricing_spread` | Options | AVG bid/ask/spread for liquid, near-term options | `{T}_underlying_price`, `{T}_avg_bid`, `{T}_avg_ask`, `{T}_avg_spread_pct` |
| `_handle_macro_analysis` | Macro | Latest value + change for a given symbol (ETF→index alias applied) | `{T}_last_value`, `{T}_mom_change` or `{T}_daily_change` |
| `_handle_geopolitical_analysis` | GPR | Latest monthly GPR level, percentile, trend | `gpr_index_level`, `gpr_percentile`, `gpr_trend` |

**ETF → Macro alias:** `SPY → ^GSPC`, `QQQ → ^IXIC`, `IWM → ^RUT`, etc. (defined in `Scripts/core/financial_ontology.py::ETF_TO_MACRO_ALIAS`). Applied only in `_handle_macro_analysis`.

---

## 4. Output Data Schema

### Return Payload

```python
{
  "values": {
      "latest_atm_iv": 0.384,
      "AAPL_iv_skew_30d": -0.04,
      "AAPL_skew_direction": "put_skew",
      "pcr_volume": 1.23,
      "gpr_index_level": 148.2,
      "gpr_percentile": "72%",
      ...
  },
  "lineage_anchors": [
      "IV_AAPL_2026-04-22",
      "GPR_202604",
      "MACRO_VIX_2026-04-22",
      ...
  ]
}
```

### Lineage Anchor Naming Convention

| Source | Format | Example |
|:---|:---|:---|
| Options IV/skew | `IV_{TICKER}_{date}` | `IV_AAPL_2026-04-22` |
| Options PCR | `PCR_{TICKER}_{date}` | `PCR_SPY_2026-04-22` |
| Options liquidity | `LIQ_{TICKER}_{date}` | `LIQ_SPY_2026-04-22` |
| Options pricing | `PX_{TICKER}_{date}` | `PX_SPY_2026-04-22` |
| Macro series | `MACRO_{TICKER}[_AS_{ALIAS}]_{date}` | `MACRO_SPY_AS_GSPC_2026-04-22` |
| GPR | `GPR_{YYYYMM}` | `GPR_202604` |

### Audit Log Structure

File: `logs/Parquet_Query/{YYYY-MM-DD}/sql_retrieval_audit.log`

```
SCHEMA_CHECK | dataset=options | glob=... | files=108 | columns=18 | missing=[] | status=OK
START_QUERY  | Tickers: ['AAPL'] | Requested: ['Implied Volatility (IV)', 'IV Skew']
SQL_RANGE    | handler=options_analysis | ticker=AAPL | mode=latest_only | time_window=yesterday | ...
FINISH_QUERY | Latency: 0.071s | Anchors_Found: 4
UNAUTHORIZED_METRIC: Insider Trading
FUZZY_MATCH  | 'implied vol' -> 'Implied Volatility (IV)'
```

---

## 5. How to Test

### Contract validation (startup)

```bash
python -c "from Scripts.retrieval.sql_tools import SilverSQLTool; t = SilverSQLTool(); print('OK')"
# Expected: three SCHEMA_CHECK | status=OK lines in audit log
```

### Direct handler call

```python
import asyncio
from Scripts.retrieval.sql_tools import SilverSQLTool
from Scripts.retrieval.schema import MetadataExtraction, TimeWindow

tool = SilverSQLTool()
meta = MetadataExtraction(
    tickers=["AAPL"],
    metrics=["Implied Volatility (IV)", "IV Skew"],
    time_window=TimeWindow.YESTERDAY,
)
result = asyncio.run(tool.query_parquet_by_metadata(meta))
print(result["values"])
print(result["lineage_anchors"])
```

### Master Retriever integration test

```bash
python -m Scripts.tests.test_master_retriever
# Runs all retriever test cases including sql_only route
```

### Audit log inspection

```bash
cat logs/Parquet_Query/$(date +%Y-%m-%d)/sql_retrieval_audit.log
```

---

## 6. Dependencies

| Library | Purpose |
|:---|:---|
| `duckdb` | In-memory SQL engine for Parquet queries |
| `python-dotenv` | `.env` loading for `DATA_LAKE_ROOT` |
| `pydantic` | `MetadataExtraction` contract validation |

```bash
pip install duckdb python-dotenv pydantic
```

**Internal dependencies:**
- `Scripts/core/financial_ontology.py` — `ALLOWED_METRICS`, `METRIC_TO_COLUMN_MAPPING`, `ETF_TO_MACRO_ALIAS`
- `Scripts/retrieval/schema.py` — `MetadataExtraction`, `TimeWindow`, `time_window_to_days`
- `Scripts/retrieval/time_adapter.py` — `SourceTimeKey`, `TimePredicate`
- `config/runtime/collect_data_state.json` — dynamic anchor dates
