# `config/` — Configuration single source of truth

This tree is intentionally **semantic-layered**, not pipeline-layered: files
live next to *what they describe* (the asset universe, reference maps,
per-pipeline parameters), not next to the script that happens to consume them.
All access goes through **`Scripts/core/universe.py`** — callers should never
`open()` files in this tree directly.

## Layout

```
config/
├── universe/                          # WHAT we track
│   ├── _manifest.json                 # schema + role metadata (owner, coverage, flags)
│   ├── equity_single_name.json        # 48 Nasdaq-100 single-names
│   ├── etf_broad_market.json          # ["SPY", "QQQ", "IWM"]
│   └── etf_commodity.json             # ["GLD", "SLV"]
│
├── reference/                         # STATIC lookup tables (derived from external sources)
│   └── ticker_to_cik.json             # ticker -> SEC CIK (~10K entries; written by SEC_generate_cik_map.py)
│
├── pipeline/                          # PER-PIPELINE parameters (no business logic)
│   └── options_history.json           # yfinance options scraper: universe_role, storage strategy, …
│
└── runtime/                           # MUTABLE runtime state
    ├── collect_data_state.json        # data-collection checkpoint
    └── sec_processed_registry.json    # SEC accession de-dup ledger (moved from SEC_Processing/ on 2026-04-22)
```

> Retired on **2026-04-22**: ``config/SEC_Ingestion/`` and ``config/SEC_Processing/``.
> All of their content now lives under ``universe/``, ``reference/``, or ``runtime/``
> and is reached through ``Scripts.core.universe`` — **never** by opening a hard-coded
> path.

## Why semantic layering

| Old pipeline-layered layout                         | New semantic layout                        |
|------------------------------------------------------|---------------------------------------------|
| `config/SEC_Ingestion/SEC_tickers.json`              | `config/universe/equity_single_name.json`   |
| `config/Option_chain/target_symbols.json` (proposed) | `config/universe/etf_broad_market.json`     |
| Duplicated ticker lists across pipelines             | **One list per role**, unioned via manifest |

The universe knows nothing about SEC or yfinance; it just declares *which
tickers are equities, which are broad-market ETFs, which are commodity ETFs*.
Pipelines then **ask for a role** (`sec.filers`, `options.scrape`) and the
manifest resolves the union.

## Access pattern

```python
from Scripts.core.universe import universe, pipelines, paths

universe.get("sec.filers")           # ['AAPL', 'ADBE', …]
universe.get("options.scrape")       # 53 tickers = single_names ∪ ETFs
universe.cik_map()                   # {'AAPL': '0000320193', …}
universe.asset_class_of("SPY")       # 'etf'

pipelines.load("options_history")    # dict with scrape / storage / …

paths.options_parquet_path("SPY", "2026-04-22")
#   legacy     -> Data/2_Silver_Processed/Options_Market_Data/2026-04-22/SPY_options_2026-04-22.parquet
#   hive_v1    -> Data/2_Silver_Processed/Options_Market_Data/snapshot_date=2026-04-22/asset_class=etf/SPY.parquet
#   monthly_rollup
#              -> Data/2_Silver_Processed/Options_Market_Data/raw/snapshot_date=2026-04-22/SPY.parquet
```

The active strategy is `storage.strategy` in `pipeline/options_history.json`.
**Default is `legacy`**, which produces paths byte-identical to the
pre-migration code so `Scripts/retrieval/sql_tools.py` read-side globs keep
working unchanged.

---

## Storage scaling — the 13K-files-per-year problem

With the universe now at **53 tickers** (`options.scrape` role), the scraper
writes **53 parquet / day → ~13,356 parquet / year**. The tree below is the
recommended migration path. None of it requires touching the scraper's
business logic.

### Phase 0 — today (`storage.strategy = legacy`) ✅ default

```
Data/2_Silver_Processed/Options_Market_Data/
└── 2026-04-22/
    ├── SPY_options_2026-04-22.parquet
    ├── QQQ_options_2026-04-22.parquet
    └── …
```

* Pros: zero-risk drop-in for the existing `*/*.parquet` glob in
  `sql_tools.py:183`.
* Cons: No partition pruning metadata on disk; just a flat date folder.
* Verdict: **fine up to ~5K–10K files**. DuckDB will still happily glob that
  many parquets; the real cost is only in S3-backed setups with per-object
  latency.

### Phase 1 — Hive partitioning (`storage.strategy = hive_v1`)

```
Data/2_Silver_Processed/Options_Market_Data/
├── snapshot_date=2026-04-22/
│   ├── asset_class=etf/
│   │   ├── SPY.parquet
│   │   ├── QQQ.parquet
│   │   └── …
│   └── asset_class=equity/
│       ├── AAPL.parquet
│       ├── MSFT.parquet
│       └── …
```

* DuckDB reads with partition pushdown:
  ```sql
  SELECT * FROM read_parquet(
      '.../Options_Market_Data/**/*.parquet', hive_partitioning=1
  )
  WHERE snapshot_date = '2026-04-22' AND asset_class = 'etf'
  ```
  → only touches the 5 ETF files, not all 53.
* **Still ~13K files/year** — but with cheap column-free filtering.
* Migration cost: (a) update `options_glob` to `**/*.parquet` and add
  `hive_partitioning=1`; (b) optional one-off backfill script to copy legacy
  files into the new tree; (c) flip `storage.strategy` to `hive_v1`.

### Phase 2 — Monthly roll-up compaction (`storage.strategy = monthly_rollup`)

```
Data/2_Silver_Processed/Options_Market_Data/
├── raw/                                     # hot tier: last N days only
│   └── snapshot_date=2026-04-22/
│       ├── SPY.parquet
│       └── …
└── rollup/                                  # cold tier: 1 compacted file / month
    ├── snapshot_month=2026-03/
    │   └── options_chain.parquet            # ALL tickers × ALL days of March
    └── snapshot_month=2026-04/
        └── options_chain.parquet
```

* Writer side is unchanged — it keeps dropping per-ticker-per-day files into
  `raw/`. A **separate compaction job** (e.g. monthly cron, not included in
  this PR) runs:
  ```python
  df = duckdb.sql(f"""
      SELECT *, snapshot_date
      FROM read_parquet('.../raw/snapshot_date=2026-03-*/*.parquet',
                        hive_partitioning=1)
  """).to_df()
  df.to_parquet('.../rollup/snapshot_month=2026-03/options_chain.parquet',
                partition_cols=None, compression='zstd', row_group_size=100_000)
  # After rollup is verified, the raw/ tier is pruned for that month.
  ```
* **File count collapses from ~1100/month to 1/month** → 12 rollup files +
  ~30–60 days of raw hot files = well under 100 files/year on disk.
* Silver SQL then reads: `rollup/**/*.parquet UNION ALL raw/**/*.parquet`
  with a date-range filter — DuckDB merges the views.

### When to escalate

| Universe size | Recommendation                       |
|---------------|--------------------------------------|
| ≤ 20 tickers  | `legacy` is fine. Don't over-engineer. |
| 20–80 tickers | Move to `hive_v1` when convenient.   |
| 80+ tickers, multi-year horizon | Implement Phase 2 rollup; prune `raw/` after compaction. |

---

## Adding a new ticker

1. Append the ticker to the right leaf file under `universe/` (e.g.
   `universe/equity_single_name.json`).
2. If it requires SEC filings, run
   `python Scripts/tools/SEC_generate_cik_map.py` to refresh
   `config/reference/ticker_to_cik.json`.
3. No code change. Both `sec_ingestion.py` and `yfinance_options_history.py`
   pick up the new ticker on the next run via `universe.get(role)`.

## Migration log (2026-04-22)

| Retired location                                    | New location                                      |
|------------------------------------------------------|----------------------------------------------------|
| `config/SEC_Ingestion/SEC_tickers.json`              | `config/universe/equity_single_name.json` (role `sec.filers`) |
| `config/SEC_Ingestion/ticker_to_cik.json`            | `config/reference/ticker_to_cik.json`             |
| `config/SEC_Processing/global_processed_registry.json` | `config/runtime/sec_processed_registry.json`    |

Callers updated in the same commit:
`Scripts/retrieval/query_transform.py`, `Scripts/tools/SEC_generate_cik_map.py`,
`Scripts/tools/SEC_Accession_No_depulicated.py`,
`Scripts/data_collection/scrapers/sec_ingestion.py`,
`Scripts/data_collection/processors/sec_processor.py`,
`Scripts/core/universe.py`.
