# Options Data Pipeline Documentation

This documentation covers the `yfinance_client.py` script, designed for automated financial data ingestion and processing.

## 🔄 Strategy Workflow
The script follows a sequential, rate-limited execution logic to ensure data integrity and prevent IP blocking:

1.  **Initialization:** Resolves absolute paths for storage and configures a 1-second request delay.
2.  **Market Baseline Fetching:** Retrieves the current underlying price for a pre-defined list of market symbols (e.g., SPY, QQQ).
3.  **Expiration Discovery:** Identifies all available option expiration dates and selects the nearest 10 for processing.
4.  **Chain Extraction:** Iterates through every expiration date to extract both Call and Put dataframes.
5.  **Vectorized Enrichment:** Uses `pandas` to calculate advanced metrics (moneyness, spreads, liquidity) across the entire dataset simultaneously.
6.  **Archival:** Saves the enriched data into a timestamped directory structure using the high-performance Parquet format.

---

## 📥 Input
The script operates on the following primary inputs defined within the `__main__` block:
* **Target Symbols:** A list of tickers representing market baselines (`["SPY", "QQQ", "IWM", "GLD", "SLV"]`).
* **API Source:** Real-time data streams from the `yfinance` library.

---

## 📤 Output Files
The script generates two types of outputs organized by date:

* **Data Snapshots:** Stored in `Data/Options_History/{YYYY-MM-DD}/{SYMBOL}_options_{YYYY-MM-DD}.parquet`.
* **Execution Logs:** Stored in `logs/options_scraper_{YYYY-MM-DD}.log`, detailing the number of liquid contracts found and any API errors.

---

## 📊 Metadata & Derived Metrics
Beyond standard price data, the script generates specific metadata to aid automated reasoning:

| Metric | Calculation / Logic | Purpose |
| :--- | :--- | :--- |
| **DTE** | `Expiration Date - Snapshot Date` | Measures time decay risk. |
| **Moneyness %** | `(abs(Strike - Price) / Price) * 100` | Identifies how "deep" or "far" an option is from the current price. |
| **Spread %** | `((Ask - Bid) / Ask) * 100` | Quantifies the transaction cost and slippage risk. |
| **Is Liquid** | `(Volume >= 50) & (OI >= 100) & (Bid > 0)` | Filters for contracts that are realistically tradeable for LLM agents. |
| **In The Money** | Boolean flag | Identifies intrinsic value status. |

---

## 📊 Data Schema

The generated Parquet files include the following columns:

| Column | Description |
| :--- | :--- |
| `snapshot_date` | Date the data was captured |
| `underlying_price` | Price of the stock at snapshot time |
| `strike` | Option strike price |
| `dte` | Days to Expiration |
| `moneyness_pct` | `abs(strike - price) / price * 100` |
| `spread_pct` | Bid-Ask spread percentage |
| `is_liquid` | Boolean flag (Volume >= 50 & OI >= 100) |
| `implied_volatility` | IV as reported by Yahoo Finance |

---
## 🛠 Technical Dependencies
* **yfinance:** Market data retrieval.
* **pandas/numpy:** Vectorized data processing and metric calculation.
* **pyarrow/fastparquet:** Efficient storage of large-scale financial time-series data.

---

## 📝 Logging
Logs are generated daily in the `/logs` folder, tracking API success rates, liquid contract counts, and any connection errors.