# Options data — `yfinance_options_history.py`

This documentation covers the `yfinance_options_history.py` script, designed for automated financial data ingestion and processing.

---

## 1. What this source provides (for downstream analysis)

- **Cross-sectional options snapshots** for liquid equity-index and metals ETFs: SPY, QQQ, IWM, GLD, SLV.
- **Per-contract fields** from Yahoo Finance: quotes, volume, open interest, implied volatility, ITM flag.
- **Derived features** for screening and LLM reasoning: days to expiration, moneyness %, bid–ask spread %, and a boolean **liquidity** flag.

---

## 2. Architecture and strategy workflow

### High-level sequence

1. **Initialization:** Resolve absolute paths for storage and configure a 1-second request delay.
2. **Market baseline fetching:** Current underlying price for the predefined symbol list (e.g., SPY, QQQ).
3. **Expiration discovery:** List all option expirations; process the nearest **10**.
4. **Chain extraction:** For each expiration, extract Call and Put dataframes.
5. **Vectorized enrichment:** `pandas` for moneyness, spreads, and liquidity across all contracts.
6. **Archival:** Save enriched data in a date-partitioned directory as Parquet.

### Implementation notes

1. **Paths and logging:** Under `Data/2_Silver_Processed/Options_Market_Data/` and `logs/{date}/`.
2. **Per symbol:** Fetch underlying quote → list expirations → for up to **10 nearest** expirations, pull full call/put chains (1 s delay between API calls).
3. **Normalize:** Flatten each contract to one row with `snapshot_date`, `underlying_price`, `option_type`, `dte`, etc.
4. **Vectorized metrics:** `moneyness_pct`, `spread_pct` (safe when `ask > 0`), `is_liquid` = volume ≥ 50, OI ≥ 100, and bid > 0.
5. **Persist:** One Parquet per symbol per day under a date subfolder.

---

## 3. Pipeline strategy (inputs, outputs, frequency)

| Item | Detail |
| :--- | :--- |
| **Inputs** | **In-script symbol list:** `["SPY", "QQQ", "IWM", "GLD", "SLV"]`. No external config file. **API:** `yfinance`. |
| **Outputs** | `Data/2_Silver_Processed/Options_Market_Data/{YYYY-MM-DD}/{SYMBOL}_options_{YYYY-MM-DD}.parquet` |
| **Logs** | `logs/{YYYY-MM-DD}/options_scraper_{YYYY-MM-DD}.log` |
| **Update frequency** | **Daily** per `collect_data.py` (07:20). |

---

## 4. Derived metrics and Parquet schema

### Derived metrics (logic reference)

| Metric | Calculation / logic | Purpose |
| :--- | :--- | :--- |
| **DTE** | `Expiration Date - Snapshot Date` | Measures time decay risk. |
| **Moneyness %** | `(abs(Strike - Price) / Price) * 100` | How far the strike is from the current price. |
| **Spread %** | `((Ask - Bid) / Ask) * 100` | Transaction cost and slippage risk. |
| **Is liquid** | `(Volume >= 50) & (OI >= 100) & (Bid > 0)` | Contracts realistically tradeable for LLM agents. |
| **In the money** | Boolean flag | Intrinsic value status. |

### Parquet columns (complete)

| Column | Type (logical) | Description |
| :--- | :--- | :--- |
| `snapshot_date` | string | `YYYY-MM-DD` |
| `symbol` | string | Underlying ticker |
| `underlying_price` | float | Spot/last used for moneyness |
| `contract_symbol` | string | OCC-style contract symbol |
| `option_type` | string | `call` or `put` |
| `strike` | float | Strike price |
| `expiration` | string | Expiration date string from Yahoo |
| `dte` | int | Calendar days to expiration |
| `moneyness_pct` | float | `abs(strike - underlying_price) / underlying_price * 100` |
| `last_price` | float | |
| `bid` | float | |
| `ask` | float | |
| `spread_pct` | float | `(ask - bid) / ask * 100` when `ask` is positive, else `0.0` |
| `volume` | int | |
| `open_interest` | int | |
| `implied_volatility` | float | |
| `in_the_money` | bool | From Yahoo |
| `is_liquid` | bool | See rule above |

There is **no separate metadata JSON**; filters use columns above.

---

## 5. Dependencies

```bash
pip install yfinance pandas numpy pyarrow
```
