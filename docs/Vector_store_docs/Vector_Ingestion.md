# Vector Ingestion - Institutional Gold Index Specification

## 1. Goal and Ingestion Control Objective

`Scripts/vector_store/ingestion.py` converts Gold-layer semantic JSONL artifacts into indexed Qdrant points for hybrid retrieval.

Control objectives:

- idempotent and replay-safe upsert behavior,
- dual-vector completeness (dense + sparse),
- payload index readiness for all retrieval filters,
- stable lineage fields for downstream citation and audit.

## 2. Architecture and Workflow

![Ingestion phase workflow](../../images/Ingestion_Phase_Workflow.svg)

## 3. Code Strategy

### 3.1 Dual-signal objective (what ingestion must support)

The Gold index must support both lexical-exact and semantic-abstract recall downstream:


| Information type  | Example                                                            | Signal                       |
| ----------------- | ------------------------------------------------------------------ | ---------------------------- |
| Lexical-exact     | `NVDA Form 4`, `action_direction=SELL`, `CPIAUCSL`                 | Tickers, SEC codes, acronyms |
| Semantic-abstract | "flight to safety", "IV crush regime", "geopolitical risk premium" | Conceptual proximity         |


A single channel (dense or sparse alone) is insufficient; ingestion therefore writes **named vectors `dense` + `sparse`** and rich payload fields so query-time hybrid fusion can combine them.

### 3.2 Retrieval-stack layers (ingestion-relevant slice)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        RETRIEVAL STACK — LAYER OVERVIEW                         │
├─────────────────────┬───────────────────────────────────────────────────────────┤
│  Layer              │  Components                                                │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Chunking            │  Per-source strategy: Zero-chunk (SEC), LLM-distilled     │
│                     │  summary unit (News/GPR), No-chunk (Options/Macro Silver) │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Encoding            │  Dense: BAAI/bge-base-en-v1.5 (768-dim, cosine, CPU)     │
│                     │  Sparse: prithivida/Splade_PP_en_v1 (SPLADE, ONNX, CPU)  │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Asymmetric input    │  Dense ← HyDE long paragraph (abstract intent)            │
│                     │  Sparse ← rerank_query short factual phrase (lexical)     │
│                     │  (query-time retriever only; not produced by ingestion.py) │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Fusion              │  Qdrant server-side Reciprocal Rank Fusion (RRF, k=60)   │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Reranking           │  BAAI/bge-reranker-v2-m3 CrossEncoder (top-K, CPU)       │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Pre-filter          │  Qdrant payload filter: ticker, source_type,              │
│                     │  action_direction, time window OR (unified_timestamp /    │
│                     │  publish_timestamp)                                        │
├─────────────────────┼───────────────────────────────────────────────────────────┤
│ Score gate          │  Drop candidates with score ≤ 1e-5 (noise floor)         │
└─────────────────────┴───────────────────────────────────────────────────────────┘
```

### 3.3 Gold artefacts → hybrid points (ingestion path)

```
Gold-layer JSONL / MD artefacts
        │
        ├─── SEC (Form 4, 8-K)
        │         │ Zero-chunking: 1 filing entry → 1 vector
        │         └── atomic self-contained sentence per record
        │
        ├─── News / GPR narratives
        │         │ LLM-distilled summary unit: ~150–300 tokens
        │         └── one coherent narrative block per Qdrant point
        │
        └─── Options / Macro (Silver Parquet)
                  │ NOT indexed in Qdrant — queried via DuckDB / SilverSQLTool
                  └── No chunking applies; time-windowed Parquet scan
                              │
                              ▼
              ┌───────────────────────────────┐
              │   QdrantHybridIngestor        │
              │   • Dense embed (bge-base)    │
              │   • Sparse embed (SPLADE)     │
              │   • Deterministic UUID        │
              │   • Batch upsert (size=100)   │
              └───────────────┬───────────────┘
                              │
                              ▼
                   ┌──────────────────┐
                   │   Qdrant Cloud   │
                   │  financial_rag_  │
                   │      gold        │
                   │ named vectors:   │
                   │  dense + sparse  │
                   └──────────────────┘
```

Query-time prefetch, RRF fusion, cross-encoder rerank, and the `0.01` score gate are implemented in the retriever layer (`Scripts/retrieval/qdrant_retriever.py`); see [Embedding, Chunking, and Retrieval Fusion Strategy](../Strategy_choices_docs/Embedding_Chunking_and_Retrieval_Fusion_Strategy.md) §V+.

### 3.4 Chunking strategy — per data source

Chunking is chosen per source so each Qdrant point maximises information density before dense/sparse encoding.

#### SEC (Form 4 / Form 8-K) — zero-chunking

**Policy:** one filing record → one Qdrant point.

```
Bronze JSONL (structured XML extract)
    ↓
sec_processor.py (semantic rewrite + tone scoring)
    ↓
Gold JSONL (one natural-language sentence per transaction):
"ROCHE VINCENT (Chair & CEO) of ADI acquired (via RSU vesting)
 19,712 shares worth $0.00."  [metadata: ticker=ADI, action_direction=ACQUIRE/VEST, ...]
    ↓
One vector point — no further splitting
```

**Form 4 — deterministic transaction intelligence**

- Parsing basis: XML fields from `nonDerivativeTransaction` extracted as structured facts in `sec_ingestion.py`.
- `sec_processor.py` applies rule-based signal shaping: transaction code polarity (`P` / `S` / `M` / `F`), 10b5-1 plan discount for pre-scheduled sales, C-suite weighting via `role` / `officerTitle`, post-transaction holdings awareness.

**Form 8-K — semantic chunking before LLM compression**

- Normalisation in `sec_ingestion.py`: strip non-semantic HTML (`script` / `style` / `head` / `meta`), HTML→Markdown, split on `Item X.XX` boundaries when present.
- Gold synthesis in `sec_processor.py`: ingestion-role LLM (`OLLAMA_INGESTION_MODEL`) in strict JSON mode returns compact fields (`summary`, `transaction_date`, `tone_score`, `topics`).

**Metadata contract**

SEC Gold points combine **embedding text** with **payload metadata**: `source_type`, `form_type`, `action_direction`, `tone_score`, `accession_no`, `transaction_date`, `unified_timestamp` (and related keys) for filters, time bounds, and traceability.

#### News and GPR — LLM-distilled summary units

**Policy:** one scraper-summarised narrative block → one Qdrant point.

```
Raw article HTML (Bronze)
    ↓
news_scraper.py / GPR_index.py (LLM-summarise + sentiment score)
    ↓
Gold JSONL (one coherent narrative per article or monthly GPR block):
"Escalating Middle East tensions drove gold prices 2.4% higher this week
 as investors rotated into safe-haven assets, with ETF inflows hitting
 a 6-month high."  [metadata: topics=[macro_geopolitics_risk], tone_score=+0.7, ...]
    ↓
One vector point — no further splitting
```

#### Options chains and macro — not in Qdrant

**Policy:** structured numeric data is never ingested into Qdrant.

Parquet under `Data/2_Silver_Processed/Options_Market_Data/` and `Data/2_Silver_Processed/Macro_History/` is queried only via `SilverSQLTool` (DuckDB): no embedding, no chunking, no Qdrant storage for those series.

### 3.5 Embedding models (ingestion encoders)

#### Dense — `BAAI/bge-base-en-v1.5`


| Attribute        | Value                           |
| ---------------- | ------------------------------- |
| Vector dimension | 768                             |
| Distance metric  | Cosine (L2-normalised)          |
| Inference device | CPU (`EMBEDDING_DEVICE=cpu`)    |
| HuggingFace ID   | `BAAI/bge-base-en-v1.5`         |
| Env var          | `EMBEDDING_MODEL_NAME`          |
| Singleton        | `@lru_cache` in `connection.py` |


Normalisation: `encode_kwargs={"normalize_embeddings": True}` aligns with Qdrant `Distance.COSINE`.

#### Sparse — `prithivida/Splade_PP_en_v1`


| Attribute          | Value                                          |
| ------------------ | ---------------------------------------------- |
| Architecture       | SPLADE (sparse lexical expansion)              |
| Runtime            | ONNX via `fastembed`                           |
| Inference device   | CPU (`FASTEMBED_THREADS` controls parallelism) |
| Env var            | `SPARSE_MODEL_NAME`                            |
| Qdrant vector name | `sparse` (dot product)                         |


#### Cross-encoder reranker — `BAAI/bge-reranker-v2-m3` (query-time, not in `ingestion.py`)


| Attribute        | Value                                           |
| ---------------- | ----------------------------------------------- |
| Architecture     | Cross-encoder (query ⊕ document joint encoding) |
| Library          | `sentence-transformers` `CrossEncoder`          |
| Inference device | CPU (`RETRIEVER_DEVICE=cpu`)                    |
| Env var          | `RERANKER_MODEL_NAME`                           |
| Applied to       | Top-K after RRF fusion, before score gate       |


### 3.6 `ingestion.py` implementation notes

- `init_collection_with_indexes()` always reconciles index schema (not only at initial collection creation).
- `_REQUIRED_INDEXES` includes both `unified_timestamp` and `publish_timestamp` for Gold time-range filtering.
- SEC records use deterministic `uuid5(accession_no)` IDs; non-SEC records fall back to native ID or generated UUID.
- Batch upsert (`batch_size=100`) balances throughput and failure isolation.
- `run_pipeline(full_refresh=False)` supports watermark-driven incremental ingestion via `collect_data_state.json`.

## 4. Output Data Schema and Paths

### 4.1 Qdrant point schema


| Field                         | Type           | Description                                            | Produced In                             | Path                                |
| ----------------------------- | -------------- | ------------------------------------------------------ | --------------------------------------- | ----------------------------------- |
| `id`                          | `str`          | Point identifier (`uuid5` for SEC or generated/native) | `process_and_upsert_file()`             | `Scripts/vector_store/ingestion.py` |
| `vector.dense`                | `List[float]`  | Dense semantic embedding                               | `get_embedding_model().embed_query()`   | `Scripts/vector_store/ingestion.py` |
| `vector.sparse`               | `SparseVector` | Sparse lexical embedding (`indices`, `values`)         | `SparseTextEmbedding.embed()`           | `Scripts/vector_store/ingestion.py` |
| `payload.text`                | `str`          | Retrieval text body                                    | source record                           | `Scripts/vector_store/ingestion.py` |
| `payload.source_type`         | `str`          | Gold source channel (`sec`, `news`, `gpr`)             | ingestion mapping                       | `Scripts/vector_store/ingestion.py` |
| `payload.unified_timestamp`   | `int`          | Canonical numeric time key for filtering               | `_to_unix_timestamp()` + fallback logic | `Scripts/vector_store/ingestion.py` |
| `payload.publish_timestamp`   | `int/str`      | Legacy or native publish key retained in metadata      | source record passthrough               | `Scripts/vector_store/ingestion.py` |
| `payload.ticker`              | `str`          | Ticker filter field (`NONE` fallback)                  | metadata normalization                  | `Scripts/vector_store/ingestion.py` |
| `payload.form_type`           | `str`          | SEC form filter field                                  | metadata normalization                  | `Scripts/vector_store/ingestion.py` |
| `payload.action_direction`    | `str`          | SEC action filter field                                | metadata normalization                  | `Scripts/vector_store/ingestion.py` |
| `payload.topic/topics`        | `str/list`     | News topical filter fields                             | metadata normalization                  | `Scripts/vector_store/ingestion.py` |
| `payload.has_bronze_evidence` | `bool`         | Whether source has deterministic bronze anchor         | ingestion rule                          | `Scripts/vector_store/ingestion.py` |


### 4.2 Operational outputs


| Artifact          | Description                                          | Path Pattern                      |
| ----------------- | ---------------------------------------------------- | --------------------------------- |
| Ingestion run log | upsert progress, errors, index reconciliation traces | `logs/<YYYY-MM-DD>/ingestion.log` |


## 5. How to Test

```bash
python Scripts/vector_store/ingestion.py --indexes-only
python Scripts/vector_store/ingestion.py --no-full-refresh
python Scripts/tests/test_router_e2e.py
```

Validation focus:

- collection exists and index reconciliation completes,
- ingestion writes non-zero upserts for current watermark files,
- retrieval tests pass without Qdrant index-missing errors.

## 6. Dependency Files, Linked Docs, and One-Line Commands

### 6.1 Core dependency files

- `Scripts/vector_store/ingestion.py`
- `Scripts/vector_store/connection.py`
- `Scripts/retrieval/qdrant_retriever.py`
- `config/runtime/collect_data_state.json`

### 6.2 Linked documentation

- [Qdrant Connection](./Qdrant_connection.md)
- [Embedding, Chunking, and Retrieval Fusion Strategy](../Strategy_choices_docs/Embedding_Chunking_and_Retrieval_Fusion_Strategy.md)
- [Retrieval Architecture and Strategy](../Query_retrieval_docs/Retrieval_Architecture_and_Strategy.md)
- [Qdrant Retriever Docs](../Query_retrieval_docs/Qdrant_retriever_docs.md)
- [Observability](../modular_guide/Observability.md)