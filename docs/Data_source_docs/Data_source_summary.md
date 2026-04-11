# Data source summary

This documentation summarizes the multi-source data ecosystem integrated within the project.

## Pipelines at a glance

| Source | Script | What it provides for analysis | Scheduler cadence (local time) |
| :--- | :--- | :--- | :--- |
| **GPR Index** | `scrapers/GPR_index.py` | Monthly geopolitical risk level, momentum (MoM/YoY), historical percentile, RAG narratives tied to safe-haven metals context | Monthly, 06:05 |
| **Macro & market** | `scrapers/macro_data_pipeline.py` | Same-day equity/vol/FX/GLD-SLV levels plus FRED macro (Fed funds, CPI, unemployment) with daily vs MoM/YoY changes | Trading days, 06:30 |
| **News** | `scrapers/news_scraper.py` | GDELT-sourced articles by topic; full text; LLM tone (−5…+5), entities, volatility implication for metals/options context | Daily, 07:00 |
| **Options chains** | `scrapers/yfinance_options_history.py` | Per-underlying options snapshots: strikes, IV, liquidity flags, moneyness/spread for SPY/QQQ/IWM/GLD/SLV | Daily, 07:20 |
| **SEC ingestion** | `scrapers/sec_ingestion.py` | Raw parsed Form 4 (insider) and 8-K filings in a rolling 7-day window | Weekly (Sunday), 08:00 |
| **SEC processing** | `processors/sec_processor.py` | Rule-based + LLM summaries and Qdrant-ready payloads; dedup by accession | Weekly (Sunday), 09:00 |

The scheduler entrypoint is `Scripts/data_collection/collect_data.py` (poll interval 30s; jobs run when due per cadence above).

## Options market data (`yfinance_options_history.py`)

1. **Data source:** Yahoo Finance API.
2. **Data content:** Full options chain snapshots for major market baselines (SPY, QQQ, IWM, GLD, SLV).
3. **Metrics and metadata:**
   - **Derived metrics:** `moneyness_pct` (distance from strike), `spread_pct` (liquidity cost), `is_liquid` (Boolean flag based on Vol > 50 and OI > 100).
   - **Metadata:** `snapshot_date`, `symbol`, `strike`, `expiration`, `dte` (Days to Expiration), `implied_volatility`.
4. **Storage:** Partitioned `.parquet` files organized by date.

## Macroeconomic and index data (`macro_data_pipeline.py`)

1. **Data source:** Yahoo Finance (market indices) and FRED (Federal Reserve Economic Data).
2. **Data content:** Daily action of S&P 500, VIX, Gold/Silver, and monthly updates for CPI, Unemployment, and Fed Funds Rate.
3. **Metrics and metadata:**
   - **Metrics:** `daily_change_pct`, `mom_change_pct` (Month-over-Month), `yoy_change_pct` (Year-over-Year).
   - **Metadata:** `observation_date`, `asset_class` (e.g., Equity Index, Inflation), `unit` (Index vs Percent).
4. **Storage:** Hybrid output (Markdown for RAG context and Parquet for structured history).

## SEC regulatory filings (`sec_ingestion.py` and `sec_processor.py`)

1. **Data source:** SEC EDGAR REST API.
2. **Data content:** Corporate Form 4 (Insider Trading) and Form 8-K (Material Events).
3. **Metrics and metadata:**
   - **Enrichment:** LLM-generated summaries (Llama 3) and rule-based "Tone Scores" for insider activity.
   - **Metadata:** `ticker`, `accession_no`, `form_type`, `action_direction` (Buy/Sell/Vest), `tone_score` (-5 to +5).
4. **Storage:** Qdrant-ready `.jsonl` files with deterministic UUIDs.

## Global news and sentiment (`news_scraper.py`)

1. **Data source:** GDELT 2.0 (Global Database of Events, Language, and Tone) and DuckDuckGo Search (fallback).
2. **Data content:** Scraped full-text articles filtered by macro topics (Central Banks, Geopolitics) and Metals action.
3. **Metrics and metadata:**
   - **Enrichment:** `llm_tone_score`, `volatility_implication` (Increase/Decrease), Entity extraction.
   - **Metadata:** `source_domain`, `publish_timestamp`, `impacted_assets`.
4. **Storage:** Daily JSONL files categorized into Raw, Full-text, and Qdrant-processed layers.

## Geopolitical Risk Index (`GPR_index.py`)

1. **Data source:** Iacoviello GPR Index (Academic source).
2. **Data content:** Historical monthly geopolitical risk levels.
3. **Metrics and metadata:**
   - **Metrics:** `gpr_mom_pct`, `gpr_percentile` (Ranking against historical data).
   - **Metadata:** `topic` (macro_geopolitics_risk), `gpr_score`.
4. **Storage:** Narrative Markdown corpus and structured vector input files.

## Repository data layout

Medallion-style paths: `Data/1_Bronze_Raw/…`, `Data/2_Silver_Processed/…`, `Data/3_Gold_Semantic/…`, plus `Data/Agent_Context/` for agent-facing copies where applicable.

## Dependencies

**Combined Python dependencies (pip):**

```bash
pip install requests pandas numpy pyarrow xlrd yfinance fredapi python-dotenv beautifulsoup4 markdownify langchain-ollama newspaper3k duckduckgo-search fastembed qdrant-client pydantic rich nltk
```

**Services (not pip):** **Ollama** with `llama3` for `news_scraper.py`, `sec_processor.py`; environment variables **FRED_API_KEY**, **SEC_USER_AGENT** (see per-source docs).
