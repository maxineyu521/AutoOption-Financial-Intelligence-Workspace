# Data Architecture

```text
📦 Data/
┣ 📂 1_Bronze_Raw/                         # 🥉 Bronze: scrape output, raw text, inputs for debug and traceability
┃
┃ ┣ 📂 SEC_Parsed_JSON/                    # SEC EDGAR parsed output (JSONL, not raw HTML/XML)
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┣ 📜 AAPL.jsonl
┃ ┃   ┣ 📜 MSFT.jsonl
┃ ┃   ┗ 📜 _SUMMARY.json
┃ ┃   💡 Notes:
┃ ┃   - Produced by `sec_ingestion.py`
┃ ┃   - Fetch and parse are done, but this layer is still Bronze
┃ ┃   - Structured raw parse for Form 4 / 8-K
┃ ┃   - No final vectorization yet; not loaded directly into Qdrant
┃
┃ ┣ 📂 News_Scrapes/                       # News scrape (Bronze)
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┣ 📂 Raw/
┃ ┃   ┃ ┣ 📜 raw_gdelt_macro_central_banks.jsonl
┃ ┃   ┃ ┣ 📜 raw_gdelt_macro_inflation_employment.jsonl
┃ ┃   ┃ ┣ 📜 raw_gdelt_macro_yields_dollar.jsonl
┃ ┃   ┃ ┣ 📜 raw_gdelt_macro_geopolitics_risk.jsonl
┃ ┃   ┃ ┣ 📜 raw_gdelt_asset_precious_metals_spot.jsonl
┃ ┃   ┃ ┗ 📜 raw_gdelt_asset_metals_derivatives.jsonl
┃ ┃   ┗ 📂 Full_text/
┃ ┃     ┣ 📜 full_text_macro_central_banks.jsonl
┃ ┃     ┣ 📜 full_text_macro_inflation_employment.jsonl
┃ ┃     ┣ 📜 full_text_macro_yields_dollar.jsonl
┃ ┃     ┣ 📜 full_text_macro_geopolitics_risk.jsonl
┃ ┃     ┣ 📜 full_text_asset_precious_metals_spot.jsonl
┃ ┃     ┗ 📜 full_text_asset_metals_derivatives.jsonl
┃ ┃   💡 Notes:
┃ ┃   - `Raw/` holds raw GDELT responses
┃ ┃   - `Full_text/` holds full-article scrape results
┃ ┃   - Bronze already dedupes and fetches full text; not yet final semantic input
┃ ┃   - Optional TTL on raw scrapes for debug / replay
┃
┃ ┗ 📂 GPR_index/                          # GPR raw preview
┃   ┗ 📂 2026-04-09/
┃     ┗ 📜 gpr_preview.csv
┃   💡 Notes:
┃   - Short-window preview for quick manual checks
┃   - Not the main analysis base; Silver / Gold hold that
┃
┣ 📂 2_Silver_Processed/                   # 🥈 Silver: structured layer for Pandas / numeric work / strategies
┃
┃ ┣ 📂 Options_Market_Data/                # Options chains, IV, liquidity, spreads, term structure
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┣ 📜 SPY_options_2026-04-09.parquet
┃ ┃   ┣ 📜 QQQ_options_2026-04-09.parquet
┃ ┃   ┣ 📜 IWM_options_2026-04-09.parquet
┃ ┃   ┣ 📜 GLD_options_2026-04-09.parquet
┃ ┃   ┗ 📜 SLV_options_2026-04-09.parquet
┃ ┃   💡 Notes:
┃ ┃   - From `yfinance_options_history.py`
┃ ┃   - Cleaned + derived fields: `dte`, `moneyness_pct`, `spread_pct`, `is_liquid`
┃ ┃   - Core quant track; not ingested into Qdrant
┃ ┃   - Suited for Pandas / DataFrame agents / tool calls
┃
┃ ┣ 📂 Macro_History/                      # Structured macro + market snapshot history
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┗ 📜 macro_snapshot_2026-04-09.parquet
┃ ┃   💡 Notes:
┃ ┃   - From `macro_data_pipeline.py`
┃ ┃   - Daily market + monthly macro stitched
┃ ┃   - Includes `daily_change_pct`, `mom_change_pct`, `yoy_change_pct`
┃ ┃   - Numeric analysis and history; not loaded directly into Qdrant
┃
┃ ┗ 📂 GPR_index/
┃   ┗ 📜 gpr_monthly_enriched.parquet
┃   💡 Notes:
┃   - From `GPR_index.py`
┃   - MoM / YoY / 3M MA / historical quantiles, etc.
┃   - Canonical structured history; overwritten on refresh
┃   - Structured analysis; not the main vector-store input
┃
┣ 📂 3_Gold_Semantic/                      # 🥇 Gold: semantic layer for LLM / RAG / Qdrant
┃
┃ ┣ 📂 SEC_Insider_Trades/                 # SEC: high-density semantic summaries after processing
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┗ 📜 qdrant_ready.jsonl
┃ ┃   💡 Notes:
┃ ┃   - From `sec_processor.py`
┃ ┃   - Form 4: rule-based summary (tone / action_direction / summary)
┃ ┃   - Form 8-K: local Ollama `llama3` JSON summary + sentiment
┃ ┃   - One of the files ingested directly into Qdrant
┃
┃ ┣ 📂 News_Qdrant/                        # News: semantic refinement for vectors
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┣ 📜 qdrant_macro_central_banks_processed.jsonl
┃ ┃   ┣ 📜 qdrant_macro_inflation_employment_processed.jsonl
┃ ┃   ┣ 📜 qdrant_macro_yields_dollar_processed.jsonl
┃ ┃   ┣ 📜 qdrant_macro_geopolitics_risk_processed.jsonl
┃ ┃   ┣ 📜 qdrant_asset_precious_metals_spot_processed.jsonl
┃ ┃   ┗ 📜 qdrant_asset_metals_derivatives_processed.jsonl
┃ ┃   💡 Notes:
┃ ┃   - From `news_scraper.py`
┃ ┃   - Refined with local Ollama `llama3`: English title, dense summary, tone, entities, affected assets, vol impact
┃ ┃   - Filters junk (cookies, 404s, noisy text)
┃ ┃   - One of the files ingested directly into Qdrant
┃
┃ ┣ 📂 Macro_Narratives/                   # Macro: semantic context (Markdown)
┃ ┃ ┗ 📂 2026-04-09/
┃ ┃   ┗ 📜 macro_context_2026-04-09.md
┃ ┃   💡 Notes:
┃ ┃   - From `macro_data_pipeline.py`
┃ ┃   - Not Ollama-generated; rule-templated RAG Markdown
┃ ┃   - Primarily LLM context documents today
┃ ┃   - `Qdrant_Ingestion.py` only scans `*.jsonl`, so `.md` is not ingested by default
┃
┃ ┗ 📂 GPR_index/                          # GPR: semantic narrative layer
┃   ┗ 📂 2026-04-09/
┃     ┣ 📜 gpr_narrative_corpus.md
┃     ┗ 📜 qdrant_gpr_input.jsonl
┃   💡 Notes:
┃   - From `GPR_index.py`
┃   - Narrative markdown is rule-templated, not Ollama-summarized
┃   - `qdrant_gpr_input.jsonl` is ready-to-ingest semantic input for Qdrant
┃   - Deterministic UUIDs for idempotent upserts across reruns
┃
┗ 📂 Agent_Context/                        # Stable context entry point for agents
  ┗ 📜 latest_macro_context.md
  💡 Notes:
  - Overwritten daily by `macro_data_pipeline.py`
  - Stable path without date partitions for agent / system-prompt injection
```

## Architecture overview

### 1. Bronze layer

- Holds raw scrape output, raw parses, and full-text intermediates.
- Optimized for traceability and debugging, not LLM ergonomics.
- `News_Scrapes` and `SEC_Parsed_JSON` are still intermediates, not final semantic inputs.

### 2. Silver layer

- Parquet suited to structured analysis.
- Primarily powers quant logic, tabular analysis, filtering, and strategy code.
- `Options_Market_Data`, `Macro_History`, and `GPR_index` should not be the main direct inputs to Qdrant.

### 3. Gold layer

- High-density semantic artifacts for RAG / LLM.
- What `Qdrant_Ingestion.py` auto-discovers and ingests is `3_Gold_Semantic/*/*/*.jsonl`.
- What actually enters Qdrant today is mainly:
- `SEC_Insider_Trades/{date}/qdrant_ready.jsonl`
- `News_Qdrant/{date}/qdrant_*_processed.jsonl`
- `GPR_index/{date}/qdrant_gpr_input.jsonl`

### 4. Agent_Context layer

- Not part of classic Bronze/Silver/Gold, but important for agents.
- Holds a “latest stable snapshot” so agents do not have to infer date folders every time.

## Processing status summary


| Data source         | Bronze        | Silver | Gold | Ollama / LLM                                     | Qdrant           |
| ------------------- | ------------- | ------ | ---- | ------------------------------------------------ | ---------------- |
| SEC Form 4 / 8-K    | Yes           | No     | Yes  | Yes. Form 4: rules; 8-K: Ollama `llama3` summary | Yes              |
| News / GDELT        | Yes           | No     | Yes  | Yes. Ollama `llama3` refinement and labels       | Yes              |
| Macro / Market      | No            | Yes    | Yes  | No. Template-built Markdown today                | No by default    |
| GPR Index           | Yes (preview) | Yes    | Yes  | No. Rule-generated narrative                     | Yes (JSONL only) |
| Options market data | No            | Yes    | No   | No                                               | No               |


## Important notes

- The stable agent context path in this project is `Data/Agent_Context/`, not `3_Gold_Semantic/Agent_Context/`.
- `Qdrant_Ingestion.py` scans `3_Gold_Semantic / <source> / <date> / *.jsonl`, so only Gold JSONL is auto-ingested into the vector store.
- `Macro_Narratives/*.md` lives under Gold but is not ingested by the current script unless `.md` ingestion is added later.

