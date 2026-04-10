# Geopolitical Risk (GPR) Index Pipeline Documentation

This documentation details the `GPR_index.py` script, an automated ETL (Extract, Transform, Load) pipeline designed to ingest, enrich, and format the global Geopolitical Risk (GPR) Index for integration into a Vector Database (Qdrant) and RAG applications.

## 🎯 Project Purpose
The pipeline transforms raw, historical macroeconomic Excel data into semantic, LLM-friendly narratives. By calculating momentum metrics (MoM, YoY) and converting data points into natural language paragraphs, it allows AI agents to easily reason about geopolitical tensions and their historical impact on safe-haven assets like Gold and Silver.

---

## 🏗 Architecture & Strategy Workflow
The system uses a linear, fault-tolerant ETL architecture designed for safe daily execution:

1. **Extraction (Fault-Tolerant Download):**
   * Attempts to download the latest GPR `.xls` file directly from Matteo Iacoviello's academic repository.
   * Features an industrial retry mechanism (up to 3 attempts with 10-second delays) to gracefully handle network drops or site outages.
   * Parses legacy date formats (e.g., `1985M01`) into standard Python datetime objects.

2. **Transformation & Statistical Enrichment:**
   * Operates on the *entire* historical dataset to calculate accurate long-term metrics:
     * **MoM %** (Month-over-Month change)
     * **YoY %** (Year-over-Year change)
     * **3-Month Moving Average** (Trend smoothing)
     * **Historical Percentile** (Ranks the current threat level against all historical data).

3. **Natural Language Generation (NLG):**
   * Uses conditional logic based on MoM momentum to generate dynamic RAG-friendly markdown. For example, a >10% jump generates a "significant escalation" narrative, while a <-10% drop generates a "cooling off" narrative.
   * Hardcodes trading context into the text (e.g., explicitly mentioning the historical correlation between GPR spikes and Gold/Silver price action).

4. **Idempotent Archival (Load Preparation):**
   * **Truncation:** Filters the dataset to only include post-2020 data to prevent legacy noise (like the 90s/Cold War) from polluting the modern Vector DB space.
   * **Deterministic UUIDs:** Generates Qdrant document IDs using `uuid5` based on a fixed string (e.g., `GPR_2024_04`). This guarantees that re-running the script safely overwrites existing database records without creating duplicates (Idempotency).

---

## 📥 Inputs
* **Source URL:** `https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls`
* **Data Format:** Binary Excel (`.xls`) read into memory via `io.BytesIO`.

---

## 📤 Output Files
The pipeline dynamically creates a daily folder hierarchy (`Data/GPR_index/{YYYY-MM-DD}/`) and outputs three files:

* **Vector Payload (Target):** `qdrant_gpr_input.jsonl` (Structured JSON objects ready for Qdrant ingestion).
* **Narrative Corpus:** `gpr_narrative_corpus.md` (A pure markdown file of all generated text, useful for testing or simple RAG setups).
* **Data Preview:** `gpr_preview.csv` (A standard CSV containing the last 24 months of enriched data for quick human verification).
* **Logs:** `logs/{YYYY-MM-DD}/gpr_downloader.log`.

---

## 📊 Output Data Schema & Metadata

The target output file (`qdrant_gpr_input.jsonl`) contains line-delimited JSON objects structured specifically for vector embeddings.

### Final Qdrant Payload Schema
| Field | Description | Type |
| :--- | :--- | :--- |
| `id` | A deterministic UUID v5 (e.g., hashed from "GPR_2024_04"). | String |
| `text` | The full, multi-paragraph markdown narrative detailing the month's metrics, historical context, and asset impact. | String |
| `metadata` | A nested dictionary for exact payload filtering in the vector database. | Object |

### `metadata` Object Structure
| Key | Description | Type |
| :--- | :--- | :--- |
| `topic` | Hardcoded routing tag (`"macro_geopolitics_risk"`). | String |
| `title` | Human-readable title (e.g., `"GPR Index Update 2024-04"`). | String |
| `publish_date` | Formatted string of the observation month (`"YYYY-MM-DD"`). | String |
| `publish_timestamp` | Integer Unix epoch for fast time-series filtering in Qdrant. | Integer |
| `gpr_score` | The raw Geopolitical Risk index float value. | Float |
| `gpr_percentile` | The calculated historical percentile (0-100) of the current score. | Float |

---

## 🛠 Technical Dependencies
* **Network / I/O:** `requests`, `io` (for in-memory binary processing).
* **Data Manipulation:** `pandas` (for datetime parsing, rolling averages, and percentage calculations).
* **System / Architecture:** `uuid` (for deterministic ID generation), `datetime`, `os`.