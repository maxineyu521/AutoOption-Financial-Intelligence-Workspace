# Options Market Data — `yfinance_options_history.py`

## 1. Goal

Produce a daily, date-partitioned Silver-layer Parquet snapshot of the full options chains for a fixed universe of liquid equity-index and metals ETFs. The snapshot feeds two downstream consumers:

1. **`SilverSQLTool`** (`Scripts/retrieval/sql_tools.py`) — queried at agent runtime via DuckDB for IV skew, put/call ratio, liquidity analysis, and options pricing spread.
2. **`MasterRetriever` always-on GPR patch** (`Scripts/retrieval/master_retriever.py`) — colocated ingestion ensures the IV-regime classifier always has a same-day anchor.

---

## 2. Architecture

```
yfinance API
     │
     ▼
[1] Underlying price fetch (per symbol)
     │
     ▼
[2] Expiration discovery → nearest 10 expirations
     │
     ▼
[3] Call + Put chain extraction (1 s delay between API calls)
     │
     ▼
[4] Vectorised enrichment (pandas)
     │  • dte          = expiration − snapshot_date
     │  • moneyness_pct = |strike − underlying_price| / underlying_price × 100
     │  • spread_pct   = (ask − bid) / ask × 100  [0.0 when ask ≤ 0]
     │  • is_liquid    = volume ≥ 50 AND open_interest ≥ 100 AND bid > 0
     │
     ▼
[5] Persist → Parquet (date-partitioned, per symbol)
```

---

## 3. Code Strategy & Workflow

| Step | Implementation | Notes |
|:---|:---|:---|
| **Path resolution** | `UniverseLoader` (`Scripts/core/universe.py`) provides symbol list and resolves absolute output path via `universe.paths.options_parquet_path(symbol, date)` | Path contract: `Data/2_Silver_Processed/Options_Market_Data/{YYYY-MM-DD}/{SYMBOL}_options_{YYYY-MM-DD}.parquet` |
| **Rate limiting** | `time.sleep(1)` between every `yf.Ticker.option_chain()` call | Prevents Yahoo Finance throttle |
| **Expiration cap** | `[:10]` slice on sorted expiration list | Covers ≈ 7–60 DTE range; further expirations are illiquid |
| **Moneyness filter** | No server-side filter; all strikes retained | SilverSQLTool applies `dte BETWEEN 7 AND 45` and `is_liquid = true` filters at query time |
| **Parquet engine** | `pyarrow` (`df.to_parquet(..., engine="pyarrow")`) | Columnar compression; DuckDB reads natively |
| **Schema validation** | `SilverSQLTool._validate_parquet_contract()` checks required columns on every process startup | Emits `SCHEMA_CHECK` audit line; fails loudly on column drift |

---

## 4. Output Data Schema & Paths

### Storage Paths

| Artifact | Path |
|:---|:---|
| **Silver Parquet** | `Data/2_Silver_Processed/Options_Market_Data/{YYYY-MM-DD}/{SYMBOL}_options_{YYYY-MM-DD}.parquet` |
| **DuckDB glob** | `Data/2_Silver_Processed/Options_Market_Data/*/*.parquet` (used by `SilverSQLTool`) |
| **Run log** | `logs/{YYYY-MM-DD}/options_scraper_{YYYY-MM-DD}.log` |

### Parquet Schema

| Column | Type | Description |
|:---|:---|:---|
| `snapshot_date` | `string` (ISO date) | Trading day the snapshot was taken |
| `symbol` | `string` | Underlying ETF ticker (SPY, QQQ, IWM, GLD, SLV) |
| `underlying_price` | `float64` | Spot price used for moneyness calculation |
| `contract_symbol` | `string` | OCC-style full contract code |
| `option_type` | `string` | `"call"` or `"put"` |
| `strike` | `float64` | Strike price |
| `expiration` | `string` (ISO date) | Expiration date |
| `dte` | `int64` | Calendar days to expiration at snapshot time |
| `moneyness_pct` | `float64` | `abs(strike − underlying_price) / underlying_price × 100` |
| `last_price` | `float64` | Last traded price |
| `bid` | `float64` | Best bid |
| `ask` | `float64` | Best ask |
| `spread_pct` | `float64` | `(ask − bid) / ask × 100`; `0.0` when `ask ≤ 0` |
| `volume` | `int64` | Daily volume |
| `open_interest` | `int64` | Open interest |
| `implied_volatility` | `float64` | Annualised IV from Yahoo (decimal, e.g. `0.38` = 38%) |
| `in_the_money` | `bool` | Intrinsic value flag from Yahoo |
| `is_liquid` | `bool` | `volume ≥ 50 AND open_interest ≥ 100 AND bid > 0` |

**Required columns verified at startup by `SilverSQLTool._validate_parquet_contract()`:**
`snapshot_date`, `symbol`, `option_type`, `volume`, `open_interest`, `implied_volatility`, `moneyness_pct`, `spread_pct`, `dte`, `is_liquid`, `contract_symbol`

### SQL Handlers that consume this schema

| Handler | Key filters | Output keys |
|:---|:---|:---|
| `_handle_options_analysis` | `is_liquid = true`, `dte BETWEEN 7 AND 45` | `latest_atm_iv`, `{TICKER}_iv_skew_{DTE}d`, `{TICKER}_skew_direction` |
| `_handle_put_call_ratio` | `symbol = ?`, date window | `pcr_volume`, `pcr_open_interest`, `pcr_status` |
| `_handle_liquidity_analysis` | `is_liquid = true` | `total_liquid_calls`, `total_liquid_puts`, `top_volume_strike_call/put` |
| `_handle_pricing_spread` | `is_liquid = true`, `dte BETWEEN 7 AND 45` | `{TICKER}_underlying_price`, `{TICKER}_avg_bid/ask/spread_pct` |

---

## 5. How to Test

### Schema contract check (runs on every process start)

```bash
python -c "from Scripts.retrieval.sql_tools import SilverSQLTool; SilverSQLTool()"
# Look for: SCHEMA_CHECK | dataset=options | status=OK
```

### Manual single-symbol snapshot

```bash
python Scripts/data_collection/scrapers/yfinance_options_history.py
# Writes Data/2_Silver_Processed/Options_Market_Data/{today}/{SYMBOL}_options_{today}.parquet
```

### DuckDB spot query

```python
import duckdb
con = duckdb.connect()
df = con.execute("""
    SELECT snapshot_date, symbol, COUNT(*) as contracts,
           AVG(implied_volatility) as avg_iv
    FROM read_parquet('Data/2_Silver_Processed/Options_Market_Data/*/*.parquet')
    WHERE is_liquid = true AND dte BETWEEN 7 AND 45
    GROUP BY 1, 2 ORDER BY 1 DESC, 2
""").df()
print(df)
```

### End-to-end retrieval test

```bash
python -m Scripts.tests.test_master_retriever
# Test case: "What is the current IV skew for SPY?"
```

---

## 6. Dependencies

| Library | Purpose |
|:---|:---|
| `yfinance` | Options chain API |
| `pandas` | Vectorised enrichment |
| `numpy` | Numerical operations |
| `pyarrow` | Parquet serialisation |

```bash
pip install yfinance pandas numpy pyarrow
```

**Runtime prerequisite:** `collect_data_state.json` must have a valid `options_daily` key so `SilverSQLTool._get_anchor_date("options")` returns a current business day.
