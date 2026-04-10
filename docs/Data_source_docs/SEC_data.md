# SEC Data Ingestion and Processing Pipeline Documentation

This documentation details the SEC data pipeline, comprising `sec_scraper.py` and `sec_processor.py`. This system is designed to automatically ingest, parse, and semantically enrich SEC Form 4 (Insider Trading) and Form 8-K (Current Reports) filings, preparing them for vector database (Qdrant) integration and RAG applications.

## 🎯 Project Purpose
The pipeline transforms raw, complex SEC regulatory filings into structured, actionable insights. By applying expert financial rules to insider trades and utilizing Large Language Models (LLMs) to summarize corporate events, it provides quantitative "tone scores" and summaries suitable for AI-driven financial analysis.

---

## 🏗 Architecture & Strategy Workflow

The pipeline operates in a robust, two-phase architecture: **Ingestion (Scraping)** and **Enrichment (Processing)**.

### Phase 1: Ingestion & Parsing (`sec_scraper.py`)
1. **Target Identification:** Loads target NASDAQ tickers and maps them to SEC CIK (Central Index Key) numbers using a local dictionary to minimize API overhead.
2. **Resilient Querying:** Queries the SEC EDGAR submissions API for recent Form 4 and Form 8-K filings (past 7 days). Uses an industrial-grade retry decorator with exponential backoff to handle rate limits (HTTP 429) and network jitter.
3. **Document Retrieval & Parsing:**
   * **Form 4 (XML):** Extracts reporting owner details, roles, and itemized non-derivative transactions (shares, prices, acquisition/disposition codes, 10b5-1 plan flags).
   * **Form 8-K (HTML):** Strips unnecessary HTML tags, converts the core text to Markdown, and chunks the document based on SEC "Item" headers.
4. **Raw Archival:** Saves the parsed structural data into daily JSONL files and generates a statistical summary report.

### Phase 2: Enrichment & LLM Processing (`sec_processor.py`)
1. **State Management:** Loads a global registry of previously processed accession numbers to ensure idempotency and prevent duplicate vector embeddings.
2. **Concurrent Execution:** Utilizes a `ThreadPoolExecutor` to process multiple filings simultaneously, significantly reducing the bottleneck caused by LLM inference times.
3. **Contextual Enrichment:**
   * **Form 4 Expert Rules:** Applies a rule-based engine to calculate a "Tone Score". It weighs the net transaction value, flags 10b5-1 planned sales (less negative), and applies multipliers if the insider is C-suite (+2 for buys, -2 for sells). Zero-dollar share changes are classified as neutral vesting events.
   * **Form 8-K LLM Analysis:** Passes the Markdown chunks to a local LLM (`ChatOllama` running `llama3`). The LLM is prompted to return strict JSON containing a concise summary, a tone score (-5 to +5), and relevant topic tags.
4. **Vector DB Preparation:** Outputs the fully enriched data into a single `qdrant_ready.jsonl` file.

---

## 📥 Inputs
* **Config Files:** `SEC_tickers.json` (target companies) and `ticker_to_cik.json` (CIK mapping).
* **Environment:** `SEC_USER_AGENT` (Required by SEC guidelines to prevent IP blocking) and LLM configuration parameters.
* **External APIs:** SEC EDGAR REST APIs.
* **Local Service:** Ollama service running the `llama3` model on `localhost:11434`.

---

## 📤 Output Files
The pipeline generates files organized by execution date:

* **Raw Data:** `Data/SEC/Raw_SEC/{YYYY-MM-DD}/raw_{ticker}.jsonl`
* **Daily Statistics:** `Data/SEC/Raw_SEC/{YYYY-MM-DD}/_SUMMARY.json`
* **Processed Data (Target):** `Data/SEC/Qdrant_SEC/{YYYY-MM-DD}/qdrant_ready.jsonl`
* **State tracking:** `Data/SEC/global_processed_registry.json`
* **Logs:** Stored in `logs/` and `logs/SEC/{YYYY-MM-DD}/`.

---

## 📊 Data Schema & Metadata

The final output in `qdrant_ready.jsonl` is structured specifically for vector database ingestion. Each line is a JSON object representing a single filing:

### Final Output Schema

| Field | Description | Type |
| :--- | :--- | :--- |
| `text` | The human-readable summary. For Form 4, it's a generated sentence describing the trade. For 8-K, it's the LLM-generated summary. | String |
| `metadata` | A nested dictionary containing all structured data to be used as payload/filters in Qdrant. | Object |

### `metadata` Object Structure

| Key | Description | Type |
| :--- | :--- | :--- |
| `ticker` | The stock symbol (e.g., AAPL). | String |
| `form_type` | The SEC filing type ("4" or "8-K"). | String |
| `filed_at` | The date the document was filed with the SEC. | String |
| `accession_no` | The unique SEC document identifier. | String |
| `url` | Direct link to the source document on EDGAR. | String |
| `transaction_date` | The actual date the event or trade occurred. | String |
| `tone_score` | Quantitative sentiment score. Form 4 uses custom rules; 8-K uses LLM evaluation (-5 to +5). | Integer |
| `action_direction` | Categorization of the event (e.g., "BUY", "SELL", "ACQUIRE/VEST", or "NONE"). | String |
| `topics` | Array of relevant tags (e.g., `["Insider Trading", "Form 4"]`). | Array of Strings |
| `processed_at` | Timestamp of when the pipeline finished processing the record. | String (ISO 8601) |

---

## 🛠 Technical Dependencies
* **Network & Parsing:** `requests`, `BeautifulSoup` (bs4), `xml.etree.ElementTree`, `markdownify`.
* **Concurrency:** `concurrent.futures.ThreadPoolExecutor`.
* **AI/LLM:** `langchain_ollama.ChatOllama` (Requires local Ollama instance).
* **Configuration:** `python-dotenv` for environment variable management.