# SEC Patch & Maintenance Tooling

Three **ad hoc** utilities under `Scripts/tools/` support recovery, backfills, and operational hygiene for the SEC pipeline. They are **not** substitutes for the scheduled `sec_ingestion` / `sec_processor` pipelines; they operate on existing Bronze artefacts without re-scraping EDGAR.

---

## 1. Goal

| Utility | Primary role |
|:---|:---|
| `SEC_generate_cik_map.py` | Refresh the **ticker → CIK** map from SEC's public bulk endpoint into `config/SEC_Ingestion/ticker_to_cik.json` |
| `SEC_parsed_form4_8-k.py` | Re-fetch and re-parse Form 4 (XML) and 8-K (HTML) into `parsed_data` for Bronze rows that lack usable structured content; writes sidecar `patched_{TICKER}.jsonl` without mutating originals |
| `SEC_accession_no_depulicated.py` | Drop rows from Bronze JSONL whose `accession_no` already appears in `global_processed_registry.json` or is duplicated within the same file; rewrites in place |

---

## 2. Architecture

```
[1] SEC_generate_cik_map.py
       │  GET https://www.sec.gov/files/company_tickers.json
       │  Normalise CIK to 10-digit zero-padded string
       └─► config/SEC_Ingestion/ticker_to_cik.json

[2] SEC_parsed_form4_8-k.py
       │  Read Data/1_Bronze_Raw/SEC_Parsed_JSON/{date}/{TICKER}.jsonl
       │  Re-fetch Form 4 XML or 8-K HTML via original URL
       │  Parse via sec_ingestion parsers
       └─► Data/1_Bronze_Raw/SEC_Parsed_JSON/{date}/patched_{TICKER}.jsonl  (new file, originals unchanged)

[3] SEC_accession_no_depulicated.py
       │  Load config/SEC_Processing/global_processed_registry.json
       │  Scan Data/1_Bronze_Raw/SEC_Parsed_JSON/{date}/{TICKER}.jsonl
       │  Drop rows: missing accession | in registry | within-file duplicate
       └─► Data/1_Bronze_Raw/SEC_Parsed_JSON/{date}/{TICKER}.jsonl  (destructive rewrite)
```

---

## 3. Code Strategy & Workflow

### 3.1 `SEC_generate_cik_map.py`

1. HTTP GET `https://www.sec.gov/files/company_tickers.json` with SEC-compliant `User-Agent` header.
2. For each entry: normalise `cik_str` → 10-digit zero-padded string.
3. Persist full mapping as pretty-printed JSON.

**When to run:** after SEC bulk schema changes, or weekly if you rely on newly-listed symbols.

### 3.2 `SEC_parsed_form4_8-k.py`

1. Resolve `Data/1_Bronze_Raw/SEC_Parsed_JSON/{target_date}/`.
2. Enumerate source files: `{TICKER}.jsonl`, excluding `patched_*`, `_*`, and `*SUMMARY*` names.
3. For each source file, if `patched_{TICKER}.jsonl` does not already exist:
   - Stream JSON lines.
   - **Form 4** (`form_type == "4"`) with URL and empty `parsed_data`: fetch XML, parse transactions; set `raw_text = "XML_PARSED_SUCCESSFULLY"` on success.
   - **8-K** (`form_type == "8-K"`) with URL: re-parse when `parsed_data` is missing or `raw_text` is not yet `"8K_PARSED_INTO_MARKDOWN_CHUNKS"`.
   - Always ensure `parsed_data` key exists (empty dict if parse fails).
4. Write output to `patched_{TICKER}.jsonl` in the same date directory.

### 3.3 `SEC_accession_no_depulicated.py`

1. Load `config/SEC_Processing/global_processed_registry.json` as a set (missing file → empty set + warning).
2. Resolve `Data/1_Bronze_Raw/SEC_Parsed_JSON/{TARGET_DATE}/`.
3. For each `{TICKER}.jsonl`: classify each row:
   - **Drop** if `accession_no` is missing.
   - **Drop** if `accession_no` ∈ global registry.
   - **Drop** if `accession_no` already seen earlier in the same file.
   - **Keep** otherwise (first-occurrence ordering preserved).
4. Rewrite the same `{TICKER}.jsonl` in place with kept rows only.

---

## 4. Output Data Schema & Paths

### Artifact Locations

```
config/SEC_Ingestion/
  ├── SEC_tickers.json               # Curated ticker list (not written by these tools)
  └── ticker_to_cik.json             # Written by SEC_generate_cik_map.py

config/SEC_Processing/
  └── global_processed_registry.json # Read (not written) by SEC_accession_no_depulicated.py
                                      # Written by sec_processor.py after Gold writes

Data/1_Bronze_Raw/SEC_Parsed_JSON/{YYYY-MM-DD}/
  ├── {TICKER}.jsonl                 # Source; rewritten in-place by accession dedupe
  ├── patched_{TICKER}.jsonl         # Written by SEC_parsed_form4_8-k.py (sidecar)
  └── _SUMMARY.json                  # Cross-ticker summary (not modified by patch tools)

logs/{YYYY-MM-DD}/
  ├── SEC_bronze_accession_dedupe_{YYYY-MM-DD}.log
  └── SEC_Ingestion/
        ├── ingestion_progress_{YYYY-MM-DD}.log
        └── patch_form4_8k_{YYYY-MM-DD}.log
```

### `patched_{TICKER}.jsonl` Schema

Identical to the Bronze JSONL schema in `docs/Data_source_docs/SEC_data.md`. Fields that were absent or empty in the original (`parsed_data`, `raw_text`) are populated; all other fields are copied verbatim.

### Dedupe Log Structure (per ticker file)

```
[TICKER.jsonl] input=47 output=32  kept=32  dropped_no_accession=0
               dropped_registry_hit=10  dropped_within_file_dup=5
               sample_registry_hits: [0001234567-26-000001, ...]
               sample_within_file_dups: [0001234567-26-000002, ...]
```

---

## 5. How to Test

### CIK map refresh

```bash
python Scripts/tools/SEC_generate_cik_map.py
# Expected: config/SEC_Ingestion/ticker_to_cik.json updated; AAPL → "0000320193"
python -c "import json; d=json.load(open('config/SEC_Ingestion/ticker_to_cik.json')); print(d.get('AAPL'))"
```

### Patch validation

```bash
# Run on a specific date
python Scripts/tools/SEC_parsed_form4_8-k.py --date 2026-04-23
# Check output
ls Data/1_Bronze_Raw/SEC_Parsed_JSON/2026-04-23/patched_*.jsonl
# Verify parsed_data populated
python -c "
import json
for line in open('Data/1_Bronze_Raw/SEC_Parsed_JSON/2026-04-23/patched_AAPL.jsonl'):
    r = json.loads(line)
    print(r['metadata']['form_type'], bool(r.get('parsed_data')))
"
```

### Dedupe smoke check

```bash
python Scripts/tools/SEC_accession_no_depulicated.py
# Check log
cat logs/$(date +%Y-%m-%d)/SEC_bronze_accession_dedupe_$(date +%Y-%m-%d).log
```

---

## 6. Recommended Sequencing & Governance

```
[1] SEC_generate_cik_map.py
        │  Refreshes ticker_to_cik.json (ingestion prerequisite)
        ▼
[2] SEC_parsed_form4_8-k.py          (optional — only if parsed_data / 8-K chunks missing)
        │  Produces patched_{TICKER}.jsonl alongside originals
        ▼
[3] SEC_accession_no_depulicated.py  (optional hygiene before Gold)
        │  Trims rows already in global_processed_registry.json
        ▼
    sec_processor.py  →  Gold JSONL  →  Qdrant upsert
```

**Governance notes:**
- Step 3 is **destructive**: rewrites `{TICKER}.jsonl` in place. Back up the date folder before running if in doubt.
- Step 2 does **not** overwrite originals; promote or merge `patched_*` content through your own pipeline rules before relying on Bronze alone.
- `global_processed_registry.json` is the authoritative "already processed" source of truth, written by `sec_processor.py`. Do not edit manually.

---

## 7. Dependencies

```bash
pip install requests beautifulsoup4 markdownify python-dotenv
```

**Required env variable:** `SEC_USER_AGENT` — e.g. `"CompanyName contact@email.com"` (SEC EDGAR fair-access policy).
