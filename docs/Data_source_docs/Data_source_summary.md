# Data Source Summary Documentation

This documentation summarizes the multi-source data ecosystem integrated within the project.

## **I. Options Market Data (`yfinance_options_history.py`)**
* **Data Source:** Yahoo Finance API.
* **Data Content:** Full options chain snapshots for major market baselines (SPY, QQQ, IWM, GLD, SLV).
* **Metrics & Metadata:**
    * **Derived Metrics:** `moneyness_pct` (distance from strike), `spread_pct` (liquidity cost), `is_liquid` (Boolean flag based on Vol > 50 and OI > 100).
    * **Metadata:** `snapshot_date`, `symbol`, `strike`, `expiration`, `dte` (Days to Expiration), `implied_volatility`.
* **Storage:** Partitioned `.parquet` files organized by date.

## **II. Macroeconomic & Index Data (`macro_data_pipeline.py`)**
* **Data Source:** Yahoo Finance (Market Indices) and FRED (Federal Reserve Economic Data).
* **Data Content:** Daily action of S&P 500, VIX, Gold/Silver, and monthly updates for CPI, Unemployment, and Fed Funds Rate.
* **Metrics & Metadata:**
    * **Metrics:** `daily_change_pct`, `mom_change_pct` (Month-over-Month), `yoy_change_pct` (Year-over-Year).
    * **Metadata:** `observation_date`, `asset_class` (e.g., Equity Index, Inflation), `unit` (Index vs Percent).
* **Storage:** Hybrid output (Markdown for RAG context and Parquet for structured history).

## **III. SEC Regulatory Filings (`sec_ingestion.py` & `sec_processor.py`)**
* **Data Source:** SEC EDGAR REST API.
* **Data Content:** Corporate Form 4 (Insider Trading) and Form 8-K (Material Events).
* **Metrics & Metadata:**
    * **Enrichment:** LLM-generated summaries (Llama 3) and rule-based "Tone Scores" for insider activity.
    * **Metadata:** `ticker`, `accession_no`, `form_type`, `action_direction` (Buy/Sell/Vest), `tone_score` (-5 to +5).
* **Storage:** Qdrant-ready `.jsonl` files with deterministic UUIDs.

## **IV. Global News & Sentiment (`news_scraper.py`)**
* **Data Source:** GDELT 2.0 (Global Database of Events, Language, and Tone) and DuckDuckGo Search (fallback).
* **Data Content:** Scraped full-text articles filtered by macro topics (Central Banks, Geopolitics) and Metals action.
* **Metrics & Metadata:**
    * **Enrichment:** `llm_tone_score`, `volatility_implication` (Increase/Decrease), Entity extraction.
    * **Metadata:** `source_domain`, `publish_timestamp`, `impacted_assets`.
* **Storage:** Daily JSONL files categorized into Raw, Full-text, and Qdrant-processed layers.

## **V. Geopolitical Risk Index (`GPR_index.py`)**
* **Data Source:** Iacoviello GPR Index (Academic source).
* **Data Content:** Historical monthly geopolitical risk levels.
* **Metrics & Metadata:**
    * **Metrics:** `gpr_mom_pct`, `gpr_percentile` (Ranking against historical data).
    * **Metadata:** `topic` (macro_geopolitics_risk), `gpr_score`.
* **Storage:** Narrative Markdown corpus and structured vector input files.

