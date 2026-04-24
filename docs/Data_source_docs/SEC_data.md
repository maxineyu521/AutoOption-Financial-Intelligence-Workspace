# SEC Regulatory Filings — Ingestion & Processing Pipeline

## 1. Goal

Ingest Form 4 (insider transactions) and Form 8-K (material corporate events) from SEC EDGAR, enrich them with rule-based and LLM-generated signals, and produce idempotent Qdrant-ready payloads for Gold-layer retrieval. The processed output is the exclusive source of insider-activity signals surfaced by the `QdrantRetriever` during agent analysis.

---

## 2. Architecture

The pipeline runs in two sequential phases: **Bronze ingestion** (raw scraping) and **Gold processing** (enrichment + vector preparation). A shared accession-number registry enforces idempotency across both phases.

```
SEC EDGAR REST API
        │
        ▼
[Phase A] sec_ingestion.py
        │  • CIK resolution from config/SEC_Ingestion/ticker_to_cik.json
        │  • Form 4 (XML) → structured transaction rows
        │  • Form 8-K (HTML) → markdownified, optionally item-chunked
        │  • 7-day rolling window; exponential-backoff on HTTP 429
        │
        ▼  Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/{TICKER}.jsonl
        │                                                  _SUMMARY.json
        │
        ▼
[Phase B] sec_processor.py
        │  • Dedup via global_processed_registry.json
        │  • Form 4  → rule-based tone score + C-suite / 10b5-1 adjustments
        │  • Form 8-K → options-expert-v1 / llama3 LLM (JSON mode) summary
        │  • ThreadPoolExecutor (max_workers=4) for LLM-bound concurrency
        │
        ▼  Data/3_Gold_Semantic/SEC_Insider_Trades/{YYYY-MM-DD}/qdrant_ready.jsonl
           config/SEC_Processing/global_processed_registry.json  (updated)
```

---

## 3. Code Strategy & Workflow

### Phase A — Ingestion (`sec_ingestion.py`)

| Step | Strategy |
|:---|:---|
| **Ticker universe** | Loaded from `config/SEC_Ingestion/SEC_tickers.json`; mapped to CIK via `config/SEC_Ingestion/ticker_to_cik.json` |
| **EDGAR query** | `GET data.sec.gov/submissions/CIK{cik}.json` → filter `filingDate` within last 7 days for Form 4 and 8-K |
| **Form 4 parse** | Fetch document XML, extract `reportingOwner`, `role`, `nonDerivativeTransaction[]` (shares, prices, codes, 10b5-1 flags) |
| **8-K parse** | Fetch HTML, `BeautifulSoup` strip → `markdownify` → optional split by `Item X.XX` headers into chunks |
| **Rate resilience** | Exponential backoff on HTTP 429/503; 1 s floor between requests |
| **Output** | Append one JSON object per filing to `{TICKER}.jsonl`; write cross-ticker `_SUMMARY.json` |

### Phase B — Processing (`sec_processor.py`)

| Step | Strategy |
|:---|:---|
| **Dedup guard** | Load `global_processed_registry.json` as a set; skip any row whose `accession_no` is already present |
| **Form 4 scoring** | Net-value tone (positive = buy signal); 10b5-1 planned-sale discount (−1); C-suite multipliers (+2 buy / −2 sell); zero-dollar vesting → `ACQUIRE/VEST` |
| **8-K LLM** | `ChatOllama(model="options-expert-v1:latest", format="json")` → strict JSON with `summary`, `transaction_date`, `tone_score`, `topics` |
| **Concurrency** | `ThreadPoolExecutor(max_workers=4)` — each future processes one Bronze row |
| **Registry update** | Append `accession_no` to registry file after each successful Gold write |

---

## 4. Output Data Schema & Paths

### Storage Paths

| Artifact | Path |
|:---|:---|
| **Bronze JSONL** | `Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/{TICKER}.jsonl` |
| **Bronze summary** | `Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/_SUMMARY.json` |
| **Gold JSONL** | `Data/3_Gold_Semantic/SEC_Insider_Trades/{YYYY-MM-DD}/qdrant_ready.jsonl` |
| **Registry** | `config/SEC_Processing/global_processed_registry.json` |
| **Ingestion log** | `logs/{YYYY-MM-DD}/SEC_Ingestion/ingestion_progress_{YYYY-MM-DD}.log` |

### Bronze JSONL Schema (per line)

| Field | Type | Description |
|:---|:---|:---|
| `metadata.ticker` | `string` | Stock symbol (e.g. `"AAPL"`) |
| `metadata.form_type` | `string` | `"4"` or `"8-K"` |
| `metadata.filed_at` | `string` | SEC filing date `YYYY-MM-DD` |
| `metadata.accession_no` | `string` | Unique SEC identifier |
| `metadata.url` | `string` | Primary document URL on EDGAR |
| `metadata.ingested_at` | `string` | ISO timestamp of ingestion write |
| `parsed_data` | `object` | Form 4: `reporting_owner`, `role`, `transactions[]`. 8-K: `is_chunked`, `item_chunks` or `full_markdown` |
| `raw_text` | `string` | Sentinel string (`"XML_PARSED_SUCCESSFULLY"`, `"8K_PARSED_INTO_MARKDOWN_CHUNKS"`, or error note) |

### Gold `qdrant_ready.jsonl` Schema (per line)

| Field | Type | Description |
|:---|:---|:---|
| `text` | `string` | Human-readable summary sentence (Form 4) or LLM-generated summary (8-K) |
| `metadata.ticker` | `string` | Stock symbol |
| `metadata.form_type` | `string` | `"4"` or `"8-K"` |
| `metadata.filed_at` | `string` | SEC filing date |
| `metadata.accession_no` | `string` | Dedup key |
| `metadata.url` | `string` | Source document link |
| `metadata.transaction_date` | `string` | Actual event date (Form 4 trade date; 8-K event date from LLM) |
| `metadata.tone_score` | `int` | −5 … +5 (rule-based for Form 4; LLM for 8-K) |
| `metadata.action_direction` | `string` | `"BUY"`, `"SELL"`, `"ACQUIRE/VEST"`, or `"NONE"` |
| `metadata.topics` | `array[string]` | E.g. `["Insider Trading", "Form 4"]` |
| `metadata.source_type` | `string` | `"sec"` (required by QdrantRetriever filter) |
| `metadata.unified_timestamp` | `float` | Unix-seconds epoch derived from `transaction_date` (enables numeric Qdrant Range filter) |
| `metadata.processed_at` | `string` | ISO 8601 timestamp of Gold write |

> **Note on `unified_timestamp`**: Added post-2026-04-22 to support the `TimeAdapter` numeric Range binding. Legacy payloads without this field still have `filed_at` / `transaction_date` ISO strings for fallback matching.

---

## 5. How to Test

### Validate Bronze output

```bash
python Scripts/data_collection/scrapers/sec_ingestion.py
# Expected: Data/1_Bronze_Raw/SEC_Parsed_JSON/{today}/{TICKER}.jsonl for each ticker
```

### Validate Gold processing

```bash
python Scripts/data_collection/processors/sec_processor.py
# Expected: Data/3_Gold_Semantic/SEC_Insider_Trades/{today}/qdrant_ready.jsonl
```

### Inspect accession registry

```python
import json
reg = json.load(open("config/SEC_Processing/global_processed_registry.json"))
print(f"Registry size: {len(reg)} accessions")
```

### Qdrant retrieval smoke test (post-ingestion)

```bash
# Run after Qdrant_Ingestion.py has upserted the Gold JSONL
python -m Scripts.tests.test_master_retriever
# Query: "Any recent AAPL insider trades?"
# Expected: gold_chunks > 0, source_type="sec", form_type="4"
```

---

## 6. Dependencies

| Library | Phase | Purpose |
|:---|:---|:---|
| `requests` | A | EDGAR HTTP calls |
| `beautifulsoup4` | A | HTML stripping for 8-K |
| `markdownify` | A | HTML → Markdown conversion |
| `python-dotenv` | A + B | `.env` loading (`SEC_USER_AGENT`) |
| `langchain-ollama` | B | LLM 8-K summarisation |

```bash
# Phase A
pip install requests beautifulsoup4 markdownify python-dotenv

# Phase B (plus local Ollama with options-expert-v1 or llama3)
pip install langchain-ollama python-dotenv
```

**Required environment variable:** `SEC_USER_AGENT` — e.g. `"CompanyName contact@email.com"` (SEC fair-access policy requires a valid identifier in every request header).
