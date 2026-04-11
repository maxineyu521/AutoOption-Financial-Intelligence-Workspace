# SEC data ingestion and processing

## 1. What this source provides (for downstream analysis)

- **Form 4 (insider activity):** Parsed transactions (codes, shares, prices, 10b5-1 hints) and structured metadata for recent filings.
- **Form 8-K (material events):** HTML → markdown (full doc or per-Item chunks) for event summarization.
- **Processed layer:** Rule-based **tone_score** and narrative for Form 4; **LLM (Llama 3 JSON)** summary and tone for 8-K; deduplication by **accession number** for idempotent Qdrant upserts.

---

## 2. Architecture and strategy workflow

The pipeline runs in two phases: **ingestion (scraping)** and **enrichment (processing)**.

### Phase A — Ingestion (`scrapers/sec_ingestion.py`)

#### Architecture workflow

1. **Target identification:** Load NASDAQ tickers and map to SEC CIK (Central Index Key) via a local file to limit API calls.
2. **Resilient querying:** SEC EDGAR submissions API for Form 4 and Form 8-K in the **past 7 days**; retries with exponential backoff for rate limits (HTTP 429) and network jitter.
3. **Document retrieval and parsing**
   - **Form 4 (XML):** Reporting owner, role, non-derivative transactions (shares, prices, codes, 10b5-1 flags).
   - **Form 8-K (HTML):** Strip noise, markdownify, optional chunking by SEC “Item” headers.
4. **Raw archival:** Daily JSONL per ticker plus a cross-ticker summary file.

#### Scraping strategy workflow

1. Load **`config/SEC_Ingestion/SEC_tickers.json`** (ticker list) and **`config/SEC_Ingestion/ticker_to_cik.json`** (CIK map).
2. For each ticker, call SEC **`data.sec.gov/submissions/CIK{cik}.json`**, filter **Form 4** and **8-K** with `filingDate` in the **last 7 days**.
3. **Form 4:** Fetch XML (normalize URL), parse transactions and owner metadata.
4. **8-K:** Fetch HTML, strip boilerplate, markdownify, optionally split by `Item X.XX` headers.
5. Append one JSON object per filing to **`Data/1_Bronze_Raw/SEC_Parsed_JSON/{date}/{ticker}.jsonl`**; write **`_SUMMARY.json`** across tickers.

### Phase B — Processing (`processors/sec_processor.py`)

#### Architecture workflow

1. **State management:** Global registry of processed accession numbers for idempotency and no duplicate embeddings.
2. **Concurrent execution:** `ThreadPoolExecutor` to overlap LLM-bound work.
3. **Contextual enrichment**
   - **Form 4 expert rules:** Tone score from net value, 10b5-1 planned sales (less negative), C-suite multipliers (+2 buys / −2 sells), neutral treatment for zero-dollar vesting-style flows.
   - **Form 8-K LLM analysis:** Markdown passed to local LLM (`ChatOllama`, `llama3`); strict JSON with summary, tone (−5 to +5), and topic tags.
4. **Vector DB preparation:** Single append-friendly `qdrant_ready.jsonl` output.

#### Scraping strategy workflow

1. Read all `*.jsonl` from the same date folder (CLI `--date`, default today).
2. Skip accession numbers present in **`config/SEC_Processing/global_processed_registry.json`**.
3. **Form 4:** Apply net buy/sell rules, C-suite and 10b5-1 adjustments; produce one-line summary and scores.
4. **8-K:** Call **ChatOllama** (`llama3`, JSON mode, `http://localhost:11434`) for summary, `transaction_date`, `tone_score`, `topics`.
5. Append **`text` + merged `metadata`** to **`qdrant_ready.jsonl`**; register accession after each successful write (thread pool, max 4 workers).

---

## 3. Pipeline strategy (inputs, outputs, frequency)

| Script | Inputs | Outputs | Frequency (`collect_data.py`) |
| :--- | :--- | :--- | :--- |
| **sec_ingestion.py** | `config/SEC_Ingestion/SEC_tickers.json`, `ticker_to_cik.json`; env **`SEC_USER_AGENT`** (required by SEC); SEC EDGAR APIs | `Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/{TICKER}.jsonl`, `_SUMMARY.json`; logs under `logs/{date}/SEC_Ingestion/` | Weekly **Sunday** 08:00 |
| **sec_processor.py** | Same-date Bronze JSONL; Ollama **`llama3`**; registry file | `Data/3_Gold_Semantic/SEC_Insider_Trades/{YYYY-MM-DD}/qdrant_ready.jsonl`; updates `config/SEC_Processing/global_processed_registry.json` | Weekly **Sunday** 09:00 |

**Ingestion log file:** `logs/{YYYY-MM-DD}/SEC_Ingestion/ingestion_progress_{YYYY-MM-DD}.log`

---

## 4. Data shapes and metadata

### Bronze JSONL (one object per line)

| Field | Description |
| :--- | :--- |
| `metadata` | See below (ingestion) |
| `parsed_data` | Form 4: `reporting_owner`, `role`, `transactions[]`. 8-K: `is_chunked`, `item_chunks` **or** `full_markdown`, etc. |
| `raw_text` | Placeholder string noting parse status |

#### `metadata` (ingestion — complete tags)

| Tag | Type | Description |
| :--- | :--- | :--- |
| `ticker` | string | Symbol |
| `form_type` | string | `"4"` or `"8-K"` |
| `filed_at` | string | SEC filing date `YYYY-MM-DD` |
| `accession_no` | string | SEC accession number |
| `url` | string | Primary document URL |
| `ingested_at` | string | ISO timestamp when written |

### Processed: `qdrant_ready.jsonl`

| Field | Description | Type |
| :--- | :--- | :--- |
| `text` | The human-readable summary. For Form 4, it's a generated sentence describing the trade. For 8-K, it's the LLM-generated summary. | String |
| `metadata` | A nested dictionary containing all structured data to be used as payload/filters in Qdrant. | Object |

#### `metadata` (processed — ingestion fields plus enrichment; complete tags)

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

## 5. Dependencies

**Ingestion:**

```bash
pip install requests beautifulsoup4 markdownify python-dotenv
```

**Processor (plus local Ollama with `llama3`):**

```bash
pip install langchain-ollama python-dotenv
```
