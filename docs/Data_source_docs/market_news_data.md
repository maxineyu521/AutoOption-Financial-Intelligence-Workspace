# News Data & LLM Enrichment Pipeline Documentation

This documentation details the `news_scraper.py` script, an advanced automated pipeline designed to fetch, parse, and semantically enrich global macro-financial news for vector database (Qdrant) ingestion.

## 🎯 Project Purpose
The pipeline acts as a real-time intelligence gathering engine for a Gold/Silver options trading desk. It aggregates global news from the GDELT project, scrapes the full article text, and utilizes a local Large Language Model (Llama 3) to translate foreign text, filter out noise, and generate quantitative metrics (like sentiment/tone scores and volatility implications) to feed into an AI-driven trading or RAG system.

---

## 🏗 Architecture & Strategy Workflow
The system operates through a robust 4-stage architecture:

1. **Topic Definition & Querying (Ingestion):**
   * Uses predefined boolean search logic to track macroeconomic drivers (e.g., Central Banks, Inflation, Geopolitics) and asset-specific news (Gold/Silver Spot and Derivatives).
   * Queries the GDELT 2.0 API to fetch the latest global article metadata for the past 48 hours.
   
2. **Resilient Scraping & Deduplication:**
   * Deduplicates incoming articles by generating unique signatures based on URL slugs and cleaned titles.
   * Uses `newspaper3k` to scrape the full raw text of the articles.
   * If direct scraping fails, it falls back to a DuckDuckGo Search (DDGS) targeting Yahoo Finance to retrieve an alternative text source.

3. **LLM Extraction & Scoring (Enrichment):**
   * Passes the scraped text to a local Llama 3 model (`ChatOllama`) operating under a strict zero-temperature quantitative analyst prompt.
   * Translates non-English content to English.
   * Filters out invalid content (cookie walls, 404s).
   * Extracts dense summaries, identified entities, and market impact metrics.

4. **Formatting & Archival (Export):**
   * Structures the final enriched data into a Qdrant-compatible JSON format.
   * Saves raw data, full-text backups, and vector-ready payloads into dynamically created daily directories.

---

## 📥 Inputs
* **Search Topics:** Hardcoded boolean strings mapping to key macro/asset concepts (`ALL_TOPICS` dictionary).
* **External APIs:** GDELT Project API (News Metadata) and DuckDuckGo Search API (Fallback routing).
* **Local LLM Engine:** Ollama running the `llama3` model on `localhost`.

---

## 📤 Output Files
The pipeline dynamically creates a daily folder hierarchy (`Data/news/{YYYY-MM-DD}/`) and outputs three streams of data per topic:

* **Raw Metadata:** `Raw/raw_gdelt_{topic_name}.jsonl` (Direct GDELT output).
* **Text Backups:** `Full_text/full_text_{topic_name}.jsonl` (URL and scraped article text).
* **Processed Payload (Target):** `Qdrant_Input/qdrant_{topic_name}_processed.jsonl` (Fully enriched Qdrant-ready vectors).
* **Logs:** `logs/{YYYY-MM-DD}/scraper_{YYYY-MM-DD}.log` (Detailed execution logs).

---

## 📊 Data Schema & Metadata
The final data stored in the `Qdrant_Input` JSONL files is structured specifically for vector embeddings. 

### Final Output Schema
| Field | Description | Type |
| :--- | :--- | :--- |
| `id` | A unique UUID v5 generated deterministically from the article URL. | String |
| `text` | The clean string to be embedded. Format: `"{english_title}. {dense_summary}"`. | String |
| `metadata` | A nested dictionary containing structured tags for Qdrant payload filtering. | Object |

### `metadata` Object Structure
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

## 🛠 Technical Dependencies
* **Data Gathering:** `requests`, `newspaper3k` (HTML parsing), `duckduckgo_search` (fallback search).
* **LLM Integration:** `langchain_ollama` (Local Llama 3 execution).
* **Data Processing:** `datetime`, `re` (Regex matching for LLM outputs), `uuid` (Deterministic ID generation).