# Macro and market data — `macro_data_pipeline.py`

The pipeline bridges the gap between high-frequency market data (Stock indices, Volatility) and low-frequency economic indicators (Inflation, Interest rates).

This documentation details the `macro_data_pipeline.py` script, which automates the collection of global market data and macroeconomic indicators for financial analysis and RAG (Retrieval-Augmented Generation) applications.

---

## 1. What this source provides (for downstream analysis)

- **Market snapshot:** Latest levels and **day-over-day % change** for major indices and ETFs (^GSPC, ^IXIC, ^VIX, DX-Y.NYB, GLD, SLV) to anchor regime, risk-on/off, and precious-metals proxies.
- **Macro snapshot:** Latest **FRED** monthly series (Fed funds, CPI, unemployment) with **MoM** and **YoY** % changes for inflation and labor momentum.
- **Dual delivery:** One dated Parquet table for quant work; Markdown brief for RAG and a stable `latest_macro_context.md` for agents.

---

## 2. Architecture and strategy workflow

### High-level flow (architecture)

1. **Environment setup:** Load API keys (FRED) and build directory hierarchies for logs and data.
2. **Market data extraction (YFinance):** Download the last 5 days of price action for major indices and commodities; compute the most recent daily percentage change.
3. **Economic data extraction (FRED):** Query the Federal Reserve Economic Data API for key monthly indicators.
4. **Statistical enrichment:**
   - **Daily change %** for market assets.
   - **Month-over-Month (MoM)** and **Year-over-Year (YoY)** changes for economic indicators.
5. **Dual-stream output:**
   - **Natural language summary** (Markdown) for immediate LLM context.
   - **Structured dataset** (Parquet) for historical analysis and quantitative queries.

### Implementation steps (scraping and assembly)

1. **Config:** Resolve `Data/`, `logs/`, `Data/Agent_Context/`; load `.env` and require `FRED_API_KEY`.
2. **Yahoo Finance:** `yf.download` for configured tickers (`period="5d"`, `threads=False`); take last two closes to compute `daily_change_pct`; emit one row per market ticker with `frequency="Daily"`.
3. **FRED:** For each series ID, take latest point and compute MoM (vs prior month) and YoY (vs 12 months prior); `daily_change_pct` is null for macro rows.
4. **Merge:** Concatenate all rows; write Markdown report (daily block + macro block); write Parquet; copy full Markdown to `Data/Agent_Context/latest_macro_context.md`.

---

## 3. Pipeline strategy (inputs, outputs, frequency)

| Item | Detail |
| :--- | :--- |
| **Inputs** | **Environment:** `FRED_API_KEY` in `.env`. **No local input files.** APIs: Yahoo Finance (`yfinance`), FRED (`fredapi`). |
| **Outputs** | See table below. |
| **Update frequency** | **Every trading day** per `collect_data.py` (06:30). Monday–Friday only (scheduler does not exclude US exchange holidays). |

### Output paths

| Artifact | Path |
| :--- | :--- |
| Parquet | `Data/2_Silver_Processed/Macro_History/{YYYY-MM-DD}/macro_snapshot_{YYYY-MM-DD}.parquet` |
| RAG Markdown (dated) | `Data/3_Gold_Semantic/Macro_Narratives/{YYYY-MM-DD}/macro_context_{YYYY-MM-DD}.md` |
| Agent “latest” copy | `Data/Agent_Context/latest_macro_context.md` |
| Log | `logs/{YYYY-MM-DD}/macro_pipeline.log` |

---

## 4. Data shapes and metadata

### Parquet schema (`macro_snapshot_{date}.parquet`)

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

**Series coverage (from code):** `^GSPC`, `^IXIC`, `^VIX`, `DX-Y.NYB`, `GLD`, `SLV`; FRED: `FEDFUNDS`, `CPIAUCSL`, `UNRATE`.

---

## 5. Dependencies

```bash
pip install yfinance pandas fredapi python-dotenv pyarrow
```
