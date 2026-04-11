# News data and LLM enrichment — `news_scraper.py`

This documentation details the `news_scraper.py` script, an advanced automated pipeline designed to fetch, parse, and semantically enrich global macro-financial news for vector database (Qdrant) ingestion.

---

## Project purpose

The pipeline acts as a real-time intelligence gathering engine for a Gold/Silver options trading desk. It aggregates global news from the GDELT project, scrapes the full article text, and utilizes a local Large Language Model (Llama 3) to translate foreign text, filter out noise, and generate quantitative metrics (like sentiment/tone scores and volatility implications) to feed into an AI-driven trading or RAG system.

---

## 1. What this source provides (for downstream analysis)

- **Topic-bucketed news** (central banks, inflation/employment, yields/USD, geopolitics, precious metals spot, metals derivatives) via **GDELT** queries over the last **48 hours**.
- **Full article text** where scraping succeeds (`newspaper3k`), with **DuckDuckGo → Yahoo Finance** fallback for alternate URLs.
- **LLM-enriched signals** (local **Llama 3** via `ChatOllama`): English title, dense summary, tone score for gold/silver (−5…+5), entities, impacted asset classes, and VIX/volatility implication.

---

## 2. Architecture and strategy workflow

### High-level flow (architecture)

1. **Topic definition and querying (ingestion)**
   - Predefined boolean search logic for macro drivers (Central Banks, Inflation, Geopolitics) and asset-specific news (Gold/Silver spot and derivatives).
   - GDELT 2.0 API for the latest global article metadata over the past 48 hours.

2. **Resilient scraping and deduplication**
   - Dedup via URL slugs and cleaned titles.
   - `newspaper3k` for full raw text.
   - On failure, DuckDuckGo Search (DDGS) targeting Yahoo Finance for an alternate URL.

3. **LLM extraction and scoring (enrichment)**
   - Local Llama 3 (`ChatOllama`) with a strict zero-temperature quantitative analyst prompt.
   - Non-English content translated to English.
   - Invalid content filtered (cookie walls, 404s).
   - Dense summaries, entities, and market impact metrics extracted.

4. **Formatting and archival (export)**
   - Qdrant-compatible JSON structure.
   - Raw data, full-text backups, and vector-ready payloads under daily directories.

### Implementation steps (scraping strategy)

1. **Topic loop:** For each entry in `ALL_TOPICS` (macro + asset queries), call GDELT `doc` API (`mode=artlist`, `maxrecords=10`, `sort=DateDesc`).
2. **Dedup:** Signatures from URL slug or cleaned title to avoid duplicate articles.
3. **Raw append:** Write GDELT article JSON lines to Bronze raw file.
4. **Scrape:** For each article, fetch full text; on failure skip (no full-text / Qdrant line).
5. **LLM:** Structured prompt extracts fields; invalid or junk content discarded when parser finds no valid `SUMMARY`.
6. **Export:** Append full-text JSONL and Qdrant JSONL per topic.

---

## 3. Pipeline strategy (inputs, outputs, frequency)

| Item | Detail |
| :--- | :--- |
| **Inputs** | **Hardcoded queries** in `MACRO_TOPICS` and `ASSET_TOPICS`. **APIs:** GDELT `https://api.gdeltproject.org/api/v2/doc/doc`. **Local:** Ollama `llama3` (default `ChatOllama` endpoint). No JSON config file on disk. |
| **Update frequency** | **Daily** per `collect_data.py` (07:00). |

### Output paths

| Stream | Path |
| :--- | :--- |
| Raw GDELT | `Data/1_Bronze_Raw/News_Scrapes/{YYYY-MM-DD}/Raw/raw_gdelt_{topic_name}.jsonl` |
| Full text | `Data/1_Bronze_Raw/News_Scrapes/{YYYY-MM-DD}/Full_text/full_text_{topic_name}.jsonl` |
| Qdrant-ready | `Data/3_Gold_Semantic/News_Qdrant/{YYYY-MM-DD}/qdrant_{topic_name}_processed.jsonl` |
| Logs | `logs/{YYYY-MM-DD}/scraper_{YYYY-MM-DD}.log` |

**Topic keys (`topic_name`):** `macro_central_banks`, `macro_inflation_employment`, `macro_yields_dollar`, `macro_geopolitics_risk`, `asset_precious_metals_spot`, `asset_metals_derivatives`.

---

## 4. Data shapes and metadata

### Bronze raw (`raw_gdelt_*.jsonl`)

Each line is the **JSON object returned by GDELT** for an article (fields such as `url`, `title`, `domain`, `urldatetime` as provided by the API).

### Full text (`full_text_*.jsonl`)

| Field | Description |
| :--- | :--- |
| `url` | Article URL |
| `title` | Title from GDELT |
| `publish_date` | From `urldatetime` or fallback ISO time |
| `full_text` | Scraped body text |

### Qdrant (`qdrant_{topic}_processed.jsonl`) — top-level

| Field | Description | Type |
| :--- | :--- | :--- |
| `id` | A unique UUID v5 generated deterministically from the article URL. | String |
| `text` | The clean string to be embedded. Format: `"{english_title}. {dense_summary}"`. | String |
| `metadata` | A nested dictionary containing structured tags for Qdrant payload filtering. | Object |

### `metadata` (complete tags)

| Key | Description | Type |
| :--- | :--- | :--- |
| `topic` | The internal category that triggered the fetch (e.g., `macro_central_banks`). | String |
| `title` | The English-translated article title. | String |
| `original_title` | The native/original title fetched from GDELT. | String |
| `publish_date` | The ISO-8601 formatted publication timestamp. | String |
| `publish_timestamp` | Integer Unix epoch for fast time-range filtering in the vector DB. | Integer |
| `source` | The domain name of the publisher. | String |
| `url` | Direct link to the source article. | String |
| `entities` | Array of 3-5 explicitly mentioned key entities/organizations. | Array of Strings |
| `impacted_assets` | Broad asset classes affected by the news (e.g., USD, Gold, Equities). | Array of Strings |
| `volatility_implication` | Estimated impact on market fear/VIX (`Increase`, `Decrease`, `Neutral`). | String |
| `llm_tone_score` | Quantitative sentiment score on Gold/Silver from -5 (Bearish) to +5 (Bullish). | Integer |

---

## 5. Dependencies

```bash
pip install requests newspaper3k duckduckgo-search langchain-ollama
```

Also requires a running **Ollama** server with the **`llama3`** model available.
