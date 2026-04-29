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
        │  • CIK resolution via Scripts.core.universe (reference mapping)
        │  • Form 4 (XML) → structured transaction rows
        │  • Form 8-K (HTML) → markdownified, optionally item-chunked
        │  • 7-day rolling window; exponential-backoff on HTTP 429
        │
        ▼  Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/{TICKER}.jsonl
        │                                                  _SUMMARY.json
        │
        ▼
[Phase B] sec_processor.py
        │  • Dedup via config/runtime/sec_processed_registry.json
        │  • Form 4  → rule-based tone score + C-suite / 10b5-1 adjustments
        │  • Form 8-K → ingestion-role Ollama model (JSON mode) summary
        │  • ThreadPoolExecutor (max_workers=4) for LLM-bound concurrency
        │
        ▼  Data/3_Gold_Semantic/SEC_Insider_Trades/{YYYY-MM-DD}/qdrant_ready.jsonl
           config/runtime/sec_processed_registry.json  (updated)
```

---

## 3. Code Strategy & Workflow

### Phase A — Ingestion (`sec_ingestion.py`)

| Step | Strategy |
|:---|:---|
| **Ticker universe** | Loaded from `Scripts.core.universe` role `sec.filers`; CIK mapping resolved from reference map via UniverseLoader |
| **EDGAR query** | `GET data.sec.gov/submissions/CIK{cik}.json` → filter `filingDate` within last 7 days for Form 4 and 8-K |
| **Form 4 parse** | Fetch document XML, extract `reportingOwner`, `role`, `nonDerivativeTransaction[]` (shares, prices, codes, 10b5-1 flags) |
| **8-K parse** | Fetch HTML, `BeautifulSoup` strip → `markdownify` → optional split by `Item X.XX` headers into chunks |
| **Rate resilience** | Exponential backoff on HTTP 429/503; 1 s floor between requests |
| **Output** | Append one JSON object per filing to `{TICKER}.jsonl`; write cross-ticker `_SUMMARY.json` |

### Phase B — Processing (`sec_processor.py`)

| Step | Strategy |
|:---|:---|
| **Dedup guard** | Load `config/runtime/sec_processed_registry.json` as a set; skip any row whose `accession_no` is already present |
| **Form 4 scoring** | Net-value tone (positive = buy signal); 10b5-1 planned-sale discount (−1); C-suite multipliers (+2 buy / −2 sell); zero-dollar vesting → `ACQUIRE/VEST` |
| **8-K LLM** | `ChatOllama(model=OLLAMA_INGESTION_MODEL, format="json")` with env fallback (`OLLAMA_ROUTER_MODEL` or `llama3:latest`) → strict JSON with `summary`, `transaction_date`, `tone_score`, `topics` |
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
| **Registry** | `config/runtime/sec_processed_registry.json` |
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

## 5. Strategy Selection Rationale (Why This Architecture Works)

### 5.1 Why 8-K Requires a Different Parsing Strategy
Form 8-K is a high-noise document class: legal boilerplate, dense HTML tables, signature blocks, and repetitive headers can dilute the actual event signal. A naive `soup.get_text()` flattening step tends to:
- destroy table semantics,
- mix legal disclaimers with event text,
- increase hallucination risk in downstream LLM summarization.

The chosen strategy favors **signal isolation before generation**:
- keep meaningful structure in Markdown,
- reduce noise before model inference,
- preserve event granularity for retrieval.

### 5.2 8-K Normalization Standards
1. **Semantic item chunking (`Item X.XX`)**
   - 8-K filings are legally organized by event class (`Item 1.01`, `Item 2.02`, `Item 8.01`, etc.).
   - `sec_ingestion.py` splits markdown text with item-aware regex and stores per-item chunks when available.
   - Why: lowers token waste, improves event-local summaries, and enables more precise retrieval filtering.

2. **Table-aware content preservation**
   - 8-K often embeds event-critical numbers in HTML tables.
   - The pipeline converts HTML to Markdown to retain row/column readability for model understanding.
   - Why: preserves relational meaning that plain text flattening would destroy.

3. **Boilerplate suppression by structure**
   - Script/style/head and non-semantic blocks are removed before markdown conversion.
   - Item-based slicing naturally deprioritizes repetitive filing headers/footers and signature tails.
   - Why: reduces legal-noise contamination and improves summary-to-signal ratio.

### 5.3 Why Form 4 Uses Rule-First Scoring
Insider transactions have explicit machine-readable fields in XML. For this domain, deterministic rules are more reliable than free-form generation for directional labeling.

The rule-first design in `sec_processor.py` uses:
- transaction-code polarity (`P` buy vs `S` sell),
- 10b5-1 planned-trade adjustments,
- C-suite role weighting,
- neutral handling for vesting/tax mechanics.

Why: better calibration, lower variance, and auditability aligned with compliance-style workflows.

---

## 6. Metadata-to-Signal Design (How Trade Intelligence Is Constructed)

### 6.1 Metadata Is Not Storage Overhead; It Is the Signal Interface
Gold-layer SEC records use **text synthesis + metadata payload** as a dual-channel representation:
- **Text** captures natural-language event context for semantic retrieval.
- **Metadata** captures machine-enforceable facts for filtering, weighting, and temporal grounding.

Without metadata constraints, retrieval quality degrades under mixed-source RAG because regulatory, macro, and options data compete in the same vector space.

### 6.2 Core Transaction Fields and Why They Matter
1. **`transactionCode` (Primary directional key)**
   - `P` (open-market purchase): strongest insider bullish evidence.
   - `S` (open-market sale): strongest insider bearish evidence.
   - `M` / `F`: typically vesting/tax mechanics; often operationally neutral.
   - Rationale: not all "acquisitions" or "sales" carry equal predictive value.

2. **10b5-1 plan detection (`is_10b5_1_planned`)**
   - Extracted from Form 4 footnotes.
   - If sale is preplanned, negative interpretation is discounted in scoring.
   - Rationale: separates discretionary selling from scheduled liquidity events.

3. **Post-transaction ownership (`post_transaction_shares`)**
   - Captures holdings remaining after execution.
   - Rationale: "sold shares" is incomplete without "what remains"; residual ownership changes conviction.

4. **Officer seniority (`role` / `officerTitle`)**
   - CEO/CFO/C-suite actions receive higher directional weight than lower-tier officers.
   - Rationale: information depth and signaling impact are role-dependent.

### 6.3 Retrieval-Critical Metadata for Production RAG
- **`source_type="sec"`**: enables hard source filtering in the retriever.
- **`form_type`**: separates insider flow (`4`) from event disclosures (`8-K`).
- **`accession_no`**: deduplication and lineage-safe replay.
- **`transaction_date` + `unified_timestamp`**: supports natural-language time intent and numeric range filters.
- **`topics`, `tone_score`, `action_direction`**: compact downstream features for routing and evidence ranking.

### 6.4 Design Principle for Gold Layer
The SEC Gold contract intentionally combines:
- **Natural-language synthesis** for context richness,
- **Structured metadata payload** for deterministic control.

This hybrid contract is the reason SEC signals remain both interpretable to users and controllable by retrieval logic.
