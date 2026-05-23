# Time Schema Audit — All Data Sources

> **Authoritative reference for every time column / payload key in the lake.**
> Updated: 2026-04-24 · Owner: RAG architecture
> Companion module: `Scripts/retrieval/time_adapter.py`
> Companion audit log: `logs/retrieval/<YYYY-MM-DD>/retriever_audit_trail.jsonl` (field `source_predicates`)

---

## 0. Why this document exists

A RAG system that mixes Qdrant (vector) and Parquet (SQL) cannot apply a single
`(start_ts, end_ts)` window to every query: each source stores time differently
and runs on a different cadence. Before this audit a "yesterday" query against
GPR returned `0` rows (monthly source, no daily data exists), and a "past week"
query against legacy SEC returned `0` hits (no numeric timestamp key in payload).

The fix is a **per-source Time Alignment Adapter** (`time_adapter.py`) that
compiles one `TimePredicate` per source, applying source-specific widening
rules. This document is the source of truth for what keys exist, their units,
and the widening policy applied in production.

---

## 1. Silver Layer (Parquet / DuckDB)

### 1.1 Options Market Data

| Attribute         | Value |
|-------------------|-------|
| Path (glob)       | `Data/2_Silver_Processed/Options_Market_Data/<YYYY-MM-DD>/<TICKER>_options_<YYYY-MM-DD>.parquet` |
| Writer            | `Scripts/data_collection/scrapers/yfinance_options_history.py` |
| Cadence           | **Daily** (trading days only; weekends/holidays missing) |
| Primary time col  | `snapshot_date` |
| Other time cols   | `expiration` (contract expiry, not snapshot) |
| Physical type     | `VARCHAR` (`YYYY-MM-DD`) |
| Timezone          | US market close (approx `America/New_York`); stored naive |
| Cast for BETWEEN  | `CAST(snapshot_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)` |
| Granularity enum  | `TimeGranularity.DAILY` |
| Min lookback      | `3 days` (survives weekend gap on Monday-anchored "yesterday") |
| `SourceTimeKey`   | `silver.options` |

### 1.2 Macro History

| Attribute         | Value |
|-------------------|-------|
| Path (glob)       | `Data/2_Silver_Processed/Macro_History/<YYYY-MM-DD>/macro_snapshot_<YYYY-MM-DD>.parquet` |
| Writer            | `Scripts/data_collection/scrapers/macro_data_pipeline.py` |
| Cadence           | **Mixed** — Daily (Yahoo Finance) + Monthly (FRED) interleaved |
| Primary time col  | `observation_date` (authoritative series date) |
| Other time cols   | `retrieval_date` (ingestion day) |
| Physical type     | `VARCHAR` (`YYYY-MM-DD`) |
| Timezone          | UTC (observation) / local ingest (retrieval) — stored naive |
| Cast for BETWEEN  | `CAST(observation_date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)` |
| Granularity enum  | `TimeGranularity.DAILY` (predominant; monthly rows survive via `ORDER BY observation_date DESC LIMIT 1`) |
| Min lookback      | `3 days` |
| `SourceTimeKey`   | `silver.macro` |

> **Frequency column:** rows also carry a `frequency ∈ {"Daily", "Monthly"}`
> field. Analyst can use it to disambiguate CPI / UNRATE (Monthly) from ^GSPC
> / VIX (Daily) without requiring separate partitions.

### 1.3 GPR Index (Silver)

| Attribute         | Value |
|-------------------|-------|
| Path              | `Data/2_Silver_Processed/GPR_index/gpr_monthly_enriched.parquet` (single file, overwritten in place) |
| Writer            | `Scripts/data_collection/scrapers/GPR_index.py` |
| Cadence           | **Monthly** (first-of-month rows) |
| Primary time col  | `date` |
| Other time cols   | `month` (duplicate of `date`) |
| Physical type     | `TIMESTAMP_NS` (pandas/arrow nanosecond timestamp) |
| Timezone          | UTC, stored naive |
| Cast for BETWEEN  | `date BETWEEN ? AND ?` (DuckDB coerces `DATE` → `TIMESTAMP_NS` automatically) |
| Granularity enum  | `TimeGranularity.MONTHLY` |
| Min lookback      | `35 days` (always widens to the full anchor month) |
| `SourceTimeKey`   | `silver.gpr` |

> **Widening example:** a `"yesterday"` query (2d base) is widened to
> `[first-of-month(anchor), anchor]` (≥28 d) so the latest GPR row for the
> anchor month is always inside the window. Without this, monthly data is
> effectively invisible to short-horizon queries.

---

## 2. Gold Layer (Qdrant vector store)

All Gold payloads share a **canonical time key**: `unified_timestamp`
(Unix seconds, integer, UTC). Legacy payloads that predate the
2026-04-22 ingestion fix may still be missing this key — the retriever
supports either via a nested `Filter(should=[...])` on `unified_timestamp`
OR `publish_timestamp` (see §4).

### 2.1 News — `source_type == "news"`

| Key                     | Physical type | Unit        | Required? | Notes |
|-------------------------|---------------|-------------|-----------|-------|
| `unified_timestamp`     | `int`         | epoch_s     | Post-fix  | Mirror of `publish_timestamp` on ingest |
| `publish_timestamp`     | `int`         | epoch_s     | Yes       | Event time, written by `news_scraper.py` |
| `publish_date`          | `str`         | ISO-8601 Z  | Yes       | Human-readable companion (e.g. `2026-04-19T16:29:12Z`) |

| Attribute       | Value |
|-----------------|-------|
| Cadence         | Event (sub-daily) |
| Granularity enum| `TimeGranularity.EVENT` |
| Min lookback    | `1 day` |
| `SourceTimeKey` | `gold.news` |

### 2.2 SEC Insider Trades — `source_type == "sec"`

| Key                     | Physical type | Unit           | Required?     | Notes |
|-------------------------|---------------|----------------|---------------|-------|
| `unified_timestamp`     | `int`         | epoch_s        | Post-fix only | Derived from `transaction_date` (preferred), else `filed_at`, else `ingested_at`. **Absent on pre-2026-04-22 ingests.** |
| `filed_at`              | `str`         | ISO-8601 + TZ  | Yes           | e.g. `2026-04-13T18:30:45-04:00` (America/New_York) |
| `transaction_date`      | `str`         | ISO date       | Yes           | Actual trade date, no TZ (e.g. `2026-04-13`) |
| `ingested_at`           | `str`         | ISO datetime   | Yes           | Bronze-layer ingestion timestamp (naive, local) |
| `processed_at`          | `str`         | ISO datetime   | Yes           | Gold-layer processing timestamp |

| Attribute       | Value |
|-----------------|-------|
| Cadence         | Event (sub-daily, but bursty around market close) |
| Granularity enum| `TimeGranularity.EVENT` |
| Min lookback    | `3 days` (filings often clump on Monday post-weekend; 3-day floor keeps recall non-zero) |
| `SourceTimeKey` | `gold.sec` |

> **⚠️ Pre-reingest risk.** Because legacy SEC payloads have **neither
> `unified_timestamp` nor `publish_timestamp`**, a Qdrant numeric Range
> filter on these docs matches nothing. The retriever mitigation is
> documented in §4 ("Source-type logic check"), and the permanent fix is
> running `Scripts/vector_store/ingestion.py` to backfill `unified_timestamp`.

### 2.3 GPR (Gold) — `source_type == "gpr"`

| Key                     | Physical type | Unit   | Required? | Notes |
|-------------------------|---------------|--------|-----------|-------|
| `unified_timestamp`     | `int`         | epoch_s| Post-fix  | Mirror of `publish_timestamp` |
| `publish_timestamp`     | `int`         | epoch_s| Yes       | First-of-month epoch seconds |
| `publish_date`          | `str`         | ISO date| Yes      | e.g. `2020-01-01` |

| Attribute       | Value |
|-----------------|-------|
| Cadence         | **Monthly** (one point per month) |
| Granularity enum| `TimeGranularity.MONTHLY` |
| Min lookback    | `35 days` |
| `SourceTimeKey` | `gold.gpr` |

---

## 3. Registry summary (single glance)

| SourceTimeKey        | Layer  | Cadence  | Time keys (priority order)                              | Units                                        | Min lookback |
|----------------------|--------|----------|----------------------------------------------------------|----------------------------------------------|--------------|
| `gold.news`          | Gold   | Event    | `unified_timestamp`, `publish_timestamp`                 | `epoch_s`, `epoch_s`                         | 1 d          |
| `gold.sec`           | Gold   | Event    | `unified_timestamp`, `filed_at`, `transaction_date`      | `epoch_s`, `iso_datetime`, `iso_date`        | 3 d          |
| `gold.gpr`           | Gold   | Monthly  | `unified_timestamp`, `publish_timestamp`                 | `epoch_s`, `epoch_s`                         | 35 d         |
| `silver.options`     | Silver | Daily    | `snapshot_date`                                          | `iso_date`                                   | 3 d          |
| `silver.macro`       | Silver | Daily    | `observation_date`, `retrieval_date`                     | `iso_date`, `iso_date`                       | 3 d          |
| `silver.gpr`         | Silver | Monthly  | `date`, `month`                                          | `ts_ns`, `ts_ns`                             | 35 d         |

This table is the machine-readable registry `SOURCE_SPECS` in
`Scripts/retrieval/time_adapter.py`. Keep them in sync.

---

## 4. Source-type logic check: `source_types = ["sec", "news"]`

**Question:** does simultaneously matching `unified_timestamp` OR
`publish_timestamp` across mixed source types create a logic problem?

**Answer:** no, provided re-ingestion has run. In detail:

| Payload family       | `unified_timestamp` present? | `publish_timestamp` present? | Behaviour under nested `should` filter |
|----------------------|------------------------------|------------------------------|----------------------------------------|
| News (any vintage)   | Yes (post-fix) / No (legacy) | **Yes**                      | Always matches via `publish_timestamp` |
| GPR  (any vintage)   | Yes (post-fix) / No (legacy) | **Yes**                      | Always matches via `publish_timestamp` |
| SEC  (post-fix)      | **Yes**                      | No                           | Matches via `unified_timestamp`        |
| SEC  (legacy)        | No                           | No                           | **Does not match — invisible to time filter** |

The `Filter(should=[...])` clause is satisfied when **at least one** timestamp key lands in range. So mixing `["sec", "news"]`:
- News docs always pass (they carry `publish_timestamp`).
- Post-reingest SEC docs pass via `unified_timestamp`.
- Legacy SEC docs are silently excluded — operationally indistinguishable from "no SEC data exists for this window", which is a **correctness
  win** (do not emit false SEC anchors) at the cost of **recall** (missing docs whose timestamp existed only as an ISO string).

### 4.1 Operational mitigation

1. **Re-ingest SEC via `Scripts/vector_store/ingestion.py`.** The ingestion enricher (patched 2026-04-22) now parses `transaction_date` → Unix
   seconds and writes `unified_timestamp` for every SEC document. This permanently removes the legacy-blind-spot.
2. **Audit visibility.** Every retriever run writes `source_predicates` + `filter_applied` to `logs/retrieval/<date>/retriever_audit_trail.jsonl`. Operators can query for `fallback_tier != "strict" AND source_type_contains("sec")` to quantify the legacy SEC gap over time.

### 4.2 Why this design is still the right one

Alternatives considered and rejected:

| Alternative                                | Why rejected                                                                 |
|--------------------------------------------|------------------------------------------------------------------------------|
| Require only `unified_timestamp`           | Hard breaking change; blanks out ALL legacy news/gpr docs too                |
| Add `filed_at` as a string Range condition | Qdrant `Range` is numeric-only; string ranges need `DatetimeRange` (1.8+)    |
| Duplicate every doc into multiple keys     | Index bloat; cannot backfill without re-vectorising                          |
| Per-source sub-queries joined at app layer | 3× Qdrant round-trips; breaks RRF fusion because each source has its own topk|

Nested `should` on `{unified_timestamp, publish_timestamp}` gives us one Qdrant call, backward compat, and a clean migration path. The Gold retriever (`qdrant_retriever._build_smart_filter`) implements it.

---

## 5. Robust time handling — recommendations

These are the patterns this codebase now uses; keep them as guardrails.

### 5.1 Compile once, execute everywhere
`MasterRetriever._compute_time_range` calls `time_adapter.compile_all(...)` and publishes the `{SourceTimeKey → TimePredicate}` dict on `self._current_predicate_set`. Both Gold (`qdrant_retriever`) and Silver (`sql_tools`) read from that compiled set — no retriever ever re-derives the window from raw `metadata.time_window`. This is the single invariant that kills off cross-layer drift.

### 5.2 Anchor = latest successful ingestion, not `date.today()`
Anchor dates come from `config/runtime/collect_data_state.json`
(`SilverSQLTool._get_anchor_date`). Weekends, holidays, and backfills all
land on the last known-good partition. Wall-clock-anchored queries are a
pre-2026-04-04 anti-pattern; see the "Dynamic Time Anchor" note in
`sql_tools.py`.

### 5.3 Per-source widening is source-spec-driven, not query-spec-driven
Widening rules (monthly → month-wide, daily → 3-day weekend-safe) live
on `SourceTimeSpec.min_lookback_days`. Never embed them in handler
branches — add a spec row instead.

### 5.4 Always store **both** a numeric epoch **and** a human ISO
Every new ingestion writer must emit `unified_timestamp` (Unix seconds,
integer, UTC) AND a readable ISO date companion (`publish_date` /
`transaction_date` / `observation_date`). The epoch key is what filters
bind to; the ISO string is what humans and audit logs cite. Do not make
operators reverse-engineer epoch offsets during an incident.

### 5.5 Timezone normalisation at ingest, not at query
The ingestion enricher converts all timestamps to UTC before storing.
Downstream code can treat every Unix epoch as UTC. If a new source
writes local-time ISO strings, fix it at the ingest boundary, not in
the retriever.

### 5.6 Weekend-safe "end-of-day" bounds
`time_adapter._to_epoch_bounds` pushes `end_date` to `23:59:59 UTC` so
a same-day query (e.g. `today`) still matches events that occurred
during the day. This is the small detail that makes `yesterday` queries
not empty when the anchor is "today".

### 5.7 Hard filter + soft fallback, never hard filter alone
The Gold retriever has three tiers: strict (per-source aligned window),
soft (180-day + ticker-OR-topic), drop (180-day no ticker). The first
non-empty tier wins, and the tier label is persisted in the audit log
(`fallback_tier`). Monitor "tier != strict" rates as a data-freshness
signal.

### 5.8 Mandatory audit keys
Every retriever call must emit:
- `source_predicates`: serialised per-source TimePredicates
- `fallback_tier`: `strict | soft_ticker_180d | drop_ticker_180d | error`
- `filter_applied`: the Qdrant Filter object (Pydantic dump)
- `window_days`, `start_date`, `end_date`

Any retriever that omits these is a regression.

---

## 6. Implementation cross-reference

| Concern                     | File                                          | Anchor                          |
|-----------------------------|-----------------------------------------------|---------------------------------|
| Semantic `TimeWindow` enum  | `Scripts/retrieval/schema.py`                 | `class TimeWindow` + `TIME_WINDOW_DAYS` |
| Per-source compiler         | `Scripts/retrieval/time_adapter.py`           | `compile_predicate`, `compile_all`, `SOURCE_SPECS` |
| Orchestration / wiring      | `Scripts/retrieval/master_retriever.py`       | `_compute_time_range`, `_current_predicate_set` |
| Gold retriever binding      | `Scripts/retrieval/qdrant_retriever.py`       | `_build_smart_filter`, `_select_gold_predicates`, `retrieve_async(time_predicates=...)` |
| Silver retriever binding    | `Scripts/retrieval/sql_tools.py`              | `query_parquet_by_metadata(time_predicates=...)`, `_predicate_for`, `_handle_put_call_ratio` |
| Ingestion-side guarantees   | `Scripts/vector_store/ingestion.py`           | `QdrantHybridIngestor.process_and_upsert_file` (normalizes source payloads and timestamps before upsert) |
| Anchor (calendar truth)     | `config/runtime/collect_data_state.json`      | `last_run_keys.{options_daily, macro_trading_daily, gpr_monthly, news_daily}` |

---

## 7. Document Boundary and Folder Policy

### 7.1 Functional difference between the two documents

| Document | Scope | Primary audience | Update trigger |
|---|---|---|---|
| `Time_Adapter.md` | Time-window compilation logic (`TimeWindow -> TimePredicate`) and widening behavior | Retrieval/orchestration engineers | Any change to `compile_predicate`, widening policy, or source predicate wiring |
| `Time_Schema_Audit.md` | Physical source-of-truth for time columns/keys/units across Silver and Gold datasets | Data platform and retrieval engineers | Any schema/cadence/time-key change in ingestion or storage layers |

- `Time_Adapter.md` in `docs/Query_retrieval_docs/` because it documents retrieval-time query compilation behavior.
- `Time_Schema_Audit.md` to `docs/Data_source_docs/` when you want clear ownership separation between retrieval logic and physical data contracts.

