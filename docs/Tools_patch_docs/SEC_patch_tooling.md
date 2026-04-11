# SEC Patch & Maintenance Tooling

This document describes three **ad hoc** utilities under `Scripts/tools/` used to refresh SEC-related configuration, enrich Bronze JSONL with structured filing parses, and align Bronze files with the global processing registry. They are **not** substitutes for the scheduled `sec_ingestion` / `sec_processor` pipelines; they support recovery, backfills, and operational hygiene.

---

## 1 — Scope and Role of Each Utility

| Utility | Primary role |
| :--- | :--- |
| **`SEC_generate_cik_map.py`** | Refresh the **ticker → CIK** map from SEC’s public `company_tickers.json` into the repo config tree. |
| **`SEC_parsed_form4_8-k.py`** | **Sidecar patch:** for a given calendar folder under Bronze, re-fetch and parse **Form 4 (XML)** and **8-K (HTML)** into `parsed_data`, writing **`patched_{TICKER}.jsonl`** without mutating originals. |
| **`SEC_accession_no_depulicated.py`** | **In-place hygiene:** for each `{TICKER}.jsonl` on a target date, drop rows whose **accession** is already in the global registry or duplicated within the file, then rewrite the same file. |

---

## 2 — `SEC_generate_cik_map.py`

### 2.1 — Purpose

Ensures `sec_ingestion` can resolve **NASDAQ-style tickers** to **10-digit zero-padded CIKs** using a local JSON file, avoiding repeated calls to SEC bulk endpoints during routine ingestion.

### 2.2 — Workflow strategy

1. **HTTP GET** `https://www.sec.gov/files/company_tickers.json` with a compliant `User-Agent` (replace placeholder email with your own per SEC guidance).
2. **Normalize** each `cik_str` to a 10-digit string.
3. **Persist** the full mapping as pretty-printed JSON next to the curated ticker list used by ingestion.

### 2.3 — Inputs and outputs

| Item | Path / detail |
| :--- | :--- |
| **Input** | SEC-hosted JSON (network). |
| **Output file** | `config/SEC_Ingestion/ticker_to_cik.json` — object mapping `{ "TICKER": "0001234567", ... }`. |
| **Related config** | `config/SEC_Ingestion/SEC_tickers.json` — curated universe (not written by this script; presence is logged only). |

**Suggested run cadence:** after SEC bulk schema changes, or periodically (e.g. weekly) if you rely on newly listed symbols.

---

## 3 — `SEC_parsed_form4_8-k.py`

### 3.1 — Purpose

Some Bronze rows may still lack usable **`parsed_data`** after the initial crawl (Form 4 often needs a second-pass **XML** parse; 8-K benefits from **HTML cleanup** and **item-level markdown chunks**). This script reuses **`Scripts.data_collection.scrapers.sec_ingestion`** parsers to fill that gap and emit **parallel** patch files.

### 3.2 — Workflow strategy

1. Resolve **`Data/1_Bronze_Raw/SEC_Parsed_JSON/{target_date}/`**.
2. Enumerate source files: **`{TICKER}.jsonl`**, excluding `patched_*`, `_*` prefixes, and `*SUMMARY*` names.
3. For each source file, if **`patched_{TICKER}.jsonl`** does not already exist:
   - Stream JSON lines.
   - For **Form 4** (`form_type == "4"`) with URL and empty `parsed_data`: fetch and parse XML; set `raw_text` sentinel to `XML_PARSED_SUCCESSFULLY` on success.
   - For **8-K** (`form_type == "8-K"`) with URL: re-parse when `parsed_data` is missing or `raw_text` is not yet `8K_PARSED_INTO_MARKDOWN_CHUNKS`.
   - Always ensure `parsed_data` key exists (empty dict if parse fails).
4. Write output to **`patched_{TICKER}.jsonl`** in the **same** date directory.

### 3.3 — Inputs, outputs, and logs

| Item | Path / detail |
| :--- | :--- |
| **Input directory** | `Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/` |
| **Input files** | `{TICKER}.jsonl` (per-ticker Bronze). |
| **Output files** | `patched_{TICKER}.jsonl` (same folder; originals unchanged). |
| **Log directory** | `logs/{YYYY-MM-DD}/SEC_Ingestion/` (aligned with ingestion / processor). |
| **Log file** | `patch_form4_8k_{YYYY-MM-DD}.log` |

---

## 4 — `SEC_accession_no_depulicated.py`

### 4.1 — Purpose

Keeps per-ticker Bronze JSONL consistent with **downstream idempotency**: rows whose **`metadata.accession_no`** already appears in **`global_processed_registry.json`** should not be re-sent to Gold processing. The script also removes **within-file** duplicate accessions (first occurrence wins).

### 4.2 — Workflow strategy

1. Load **`config/SEC_Processing/global_processed_registry.json`** as a **set** of accession strings (if missing, treat as empty and log a warning).
2. Resolve **`Data/1_Bronze_Raw/SEC_Parsed_JSON/{TARGET_DATE}/`** (module-level date constant in script).
3. For each `{TICKER}.jsonl` candidate, scan all non-empty lines and classify each row:
   - **Drop** if `accession_no` is missing.
   - **Drop** if `accession_no` ∈ global registry.
   - **Drop** if `accession_no` already seen earlier in the same file.
   - **Keep** otherwise, preserving order of first occurrence.
4. **Rewrite** the same `{TICKER}.jsonl` in place with the kept rows only.

### 4.3 — Inputs, outputs, and logs

| Item | Path / detail |
| :--- | :--- |
| **Input / output** | Same path: `Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/{TICKER}.jsonl` (**destructive** rewrite). |
| **Registry input** | `config/SEC_Processing/global_processed_registry.json` (JSON array of accession strings). |
| **Log directory** | `logs/{YYYY-MM-DD}/` |
| **Log file** | `SEC_bronze_accession_dedupe_{YYYY-MM-DD}.log` |

The log records, **per ticker file**: input line count, output line count, counts for each removal reason, and **sample lists** (capped) of accessions removed for registry hits and within-file duplicates, plus a run header (paths, registry size, candidate file count).

---

## 5 — Recommended sequencing (operational view)

Use this order when repairing a **single** Bronze date after ingestion issues or registry updates:

```text
[1] SEC_generate_cik_map.py
        │
        ▼   Refreshes ticker_to_cik.json (ingestion prerequisite)
[2] SEC_parsed_form4_8-k.py   (optional, if parsed_data / 8-K chunks missing)
        │
        ▼   Produces patched_{TICKER}.jsonl alongside originals
[3] SEC_accession_no_depulicated.py   (optional hygiene before Gold)
        │
        ▼   Trims rows already in global_processed_registry.json
```

**Caution:** Step **3** mutates `{TICKER}.jsonl` in place. Step **2** does not overwrite originals but adds patch files; promote or merge patched content through your own pipeline rules before relying on Bronze alone.

---

## 6 — Quick reference — artifact locations

```text
config/SEC_Ingestion/
  ├── SEC_tickers.json              # Curated ticker list (ingestion)
  └── ticker_to_cik.json            # Written by SEC_generate_cik_map.py

config/SEC_Processing/
  └── global_processed_registry.json # Read by SEC_accession_no_depulicated.py

Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/
  ├── {TICKER}.jsonl                 # Source; rewritten by accession dedupe
  └── patched_{TICKER}.jsonl         # Written by SEC_parsed_form4_8-k.py

logs/{YYYY-MM-DD}/
  ├── SEC_bronze_accession_dedupe_{YYYY-MM-DD}.log
  └── SEC_Ingestion/
        ├── ingestion_progress_{YYYY-MM-DD}.log   # sec_ingestion (reference)
        └── patch_form4_8k_{YYYY-MM-DD}.log       # SEC_parsed_form4_8-k.py
```

---

## 7 — Governance notes

- **SEC fair access:** set a real contact in `User-Agent` for both CIK download and filing fetches (see `.env` / `SEC_USER_AGENT` where applicable).
- **Auditability:** retain dedupe and patch logs under `logs/{date}/` for traceability of what was removed or enriched.
- **Registry authority:** `global_processed_registry.json` is updated by **`sec_processor`** after successful Gold processing; dedupe tooling assumes it is the source of truth for “already processed” accessions.
