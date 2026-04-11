# Geopolitical Risk (GPR) Index — `GPR_index.py`

## 1. What this source provides (for downstream analysis)

- **Monthly global GPR level** from the Iacoviello academic series, usable as a macro stress / safe-haven demand driver alongside precious metals and volatility narratives.
- **Enriched series:** month-over-month and year-over-year change, 3-month moving average, full-sample percentile rank.
- **RAG outputs:** per-month markdown narratives and deterministic Qdrant JSONL payloads so retrieval stays idempotent across reruns.

---

## 2. Architecture and strategy workflow

### Step 1 — Extraction (fault-tolerant download)

1. Download the latest GPR `.xls` file directly from Matteo Iacoviello's academic repository.
2. Retry on failure (up to 3 attempts with 10-second delays) for network drops or site outages.
3. Parse legacy date formats (e.g., `1985M01`) into standard Python datetime objects.

### Step 2 — Transformation and statistical enrichment

1. Operate on the *entire* historical dataset so long-window metrics stay correct.
2. Compute:
   - **MoM %** (Month-over-Month change)
   - **YoY %** (Year-over-Year change)
   - **3-Month Moving Average** (trend smoothing)
   - **Historical Percentile** (ranks the current threat level against all historical data)

### Step 3 — Natural language generation (NLG)

1. Uses conditional logic based on MoM momentum to generate dynamic RAG-friendly markdown. For example, a >10% jump generates a "significant escalation" narrative, while a <-10% drop generates a "cooling off" narrative.
2. Hardcodes trading context into the text (e.g., explicitly mentioning the historical correlation between GPR spikes and Gold/Silver price action).

### Step 4 — Idempotent archival (load preparation)

1. **Truncation:** Keep post-2020 rows only so older regimes do not dominate the vector space.
2. **Deterministic UUIDs:** Qdrant document IDs use `uuid5` from a fixed string (e.g., `GPR_2024_04`) so reruns overwrite the same logical documents without duplicates.

---

## 3. Pipeline strategy (inputs, outputs, frequency)

| Item | Detail |
| :--- | :--- |
| **Input** | Remote URL only: `https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls` (no local input file). |
| **Outputs** | See table below. |
| **Update frequency** | **Monthly** per `collect_data.py` (06:05). Each successful run refreshes the full post-2020 slice in Parquet and Gold files. |

### Output paths (repo root relative)

| Layer | Path |
| :--- | :--- |
| Bronze (preview CSV, last 24 rows) | `Data/1_Bronze_Raw/GPR_index/{YYYY-MM-DD}/gpr_preview.csv` |
| Silver (canonical Parquet) | `Data/2_Silver_Processed/GPR_index/gpr_monthly_enriched.parquet` |
| Gold (narrative + Qdrant) | `Data/3_Gold_Semantic/GPR_index/{YYYY-MM-DD}/gpr_narrative_corpus.md`, `qdrant_gpr_input.jsonl` |
| Logs | `logs/{YYYY-MM-DD}/gpr_downloader.log` |

---

## 4. Data shapes and metadata

### Parquet: `gpr_monthly_enriched.parquet` (post-2020 only)

| Column | Type (logical) | Description |
| :--- | :--- | :--- |
| `month` | string | Original series month key |
| `gpr` | float | Raw GPR index |
| `date` | datetime | Parsed month-end style date |
| `gpr_mom_pct` | float | Month-over-month % change |
| `gpr_yoy_pct` | float | Year-over-year % change |
| `gpr_3m_ma` | float | 3-month rolling mean |
| `gpr_percentile` | float | Percentile rank (0–100) vs full history |

### JSONL: `qdrant_gpr_input.jsonl` (top-level)

| Field | Description | Type |
| :--- | :--- | :--- |
| `id` | A deterministic UUID v5 (e.g., hashed from "GPR_2024_04"). | String |
| `text` | The full, multi-paragraph markdown narrative detailing the month's metrics, historical context, and asset impact. | String |
| `metadata` | A nested dictionary for exact payload filtering in the vector database. | Object |

### `metadata` object structure

| Key | Description | Type |
| :--- | :--- | :--- |
| `topic` | Hardcoded routing tag (`"macro_geopolitics_risk"`). | String |
| `title` | Human-readable title (e.g., `"GPR Index Update 2024-04"`). | String |
| `publish_date` | Formatted string of the observation month (`"YYYY-MM-DD"`). | String |
| `publish_timestamp` | Integer Unix epoch for fast time-series filtering in Qdrant. | Integer |
| `gpr_score` | The raw Geopolitical Risk index float value. | Float |
| `gpr_percentile` | The calculated historical percentile (0-100) of the current score. | Float |

---

## 5. Dependencies

```bash
pip install requests pandas pyarrow xlrd
```

`xlrd` supports legacy `.xls` reads used by `pd.read_excel` on the downloaded file.
