# Macro & Market Data Pipeline Documentation

This documentation details the `macro_data_pipeline.py` script, which automates the collection of global market data and macroeconomic indicators for financial analysis and RAG (Retrieval-Augmented Generation) applications.

## 🎯 Project Purpose
The pipeline bridges the gap between high-frequency market data (Stock indices, Volatility) and low-frequency economic indicators (Inflation, Interest rates). By structuring this data into LLM-friendly Markdown and machine-readable Parquet formats, it enables AI agents to ground their financial reasoning in current economic reality.

---

## 🔄 Strategy Workflow
The script executes a multi-source ingestion process:

1.  **Environment Setup:** Loads API keys (FRED) and dynamically constructs directory hierarchies for logs and data.
2.  **Market Data Extraction (YFinance):** Downloads the last 5 days of price action for major indices and commodities to calculate the most recent daily percentage change.
3.  **Economic Data Extraction (FRED):** Queries the Federal Reserve Economic Data API for key monthly indicators.
4.  **Statistical Enrichment:** * Calculates **Daily Change %** for market assets.
    * Calculates **Month-over-Month (MoM)** and **Year-over-Year (YoY)** changes for economic indicators.
5.  **Dual-Stream Output:** * Generates a **Natural Language Summary** (Markdown) for immediate LLM context.
    * Generates a **Structured Dataset** (Parquet) for historical analysis and quantitative queries.

---

## 📥 Input
* **External APIs:** * `yfinance`: For S&P 500, NASDAQ, VIX, DXY, GLD, and SLV.
    * `fredapi`: For Federal Funds Rate, CPI (Inflation), and Unemployment Rate.
* **Environment Variables:** Requires `FRED_API_KEY` stored in a `.env` file.

---

## 📤 Output Files
The script organizes outputs into specific sub-directories:

* **Markdown Contexts:** `Data/Macro_History/Context_Briefs/macro_context_{YYYY-MM-DD}.md` (and a `latest_macro_context.md` copy for easy access).
* **Structured Data:** `Data/Macro_History/macro_snapshot_{YYYY-MM-DD}.parquet`.
* **Logs:** `logs/{YYYY-MM-DD}/macro_pipeline.log`.

---

## 📊 Data Schema & Metadata
Each record in the pipeline contains the following fields:

| Field | Description | Type |
| :--- | :--- | :--- |
| `retrieval_date` | The timestamp when the script was executed. | String |
| `observation_date` | The actual date the data point was recorded by the exchange/agency. | String |
| `symbol` | Ticker (e.g., ^GSPC) or Series ID (e.g., CPIAUCSL). | String |
| `asset_class` | Category (Equity Index, Inflation, Labor Market, etc.). | String |
| `value` | The raw price or index level. | Float |
| `unit` | Units of measurement (Points, USD, %, or Index). | String |
| `frequency` | Data update cadence (Daily or Monthly). | String |
| `daily_change_pct` | Day-to-day volatility (Market data only). | Float |
| `mom_change_pct` | Monthly momentum (Macro data only). | Float |
| `yoy_change_pct` | Annualized growth/inflation (Macro data only). | Float |

---

## 🛠 Technical Dependencies
* **yfinance:** Multi-threaded market data retrieval.
* **fredapi:** Interface for the St. Louis Fed's economic database.
* **pandas:** Core engine for data manipulation and change-rate calculations.
* **python-dotenv:** Secure management of API credentials.
* **pyarrow/fastparquet:** Backend engines for optimized Parquet storage.