# Retrieval Architecture, Strategy, and Workflow

## I. Mission Profile and System Boundary

This document defines the production retrieval architecture implemented across:

- `Scripts/core/financial_ontology.py`
- `Scripts/core/prompt_templates.py`
- `Scripts/core/few_shot_config.py`
- `Scripts/retrieval/schema.py`
- `Scripts/retrieval/query_transform.py`
- `Scripts/retrieval/qdrant_retriever.py`
- `Scripts/vector_store/connection.py`

The retrieval subsystem is designed as a **two-stage query transformation pipeline** followed by an **asymmetric hybrid retrieval and reranking pipeline**. Its objective is to convert ambiguous user language into deterministic filters and high-signal retrieval vectors for Qdrant Cloud.

---

## II. Institutional-Grade Architectural Topology

### A. Knowledge Governance Layer (`Scripts/core`)

- **Ontology governance** (`financial_ontology.py`):
  - Defines controlled vocabularies: `ALLOWED_SOURCES`, `ALLOWED_CATEGORIES`, `ALLOWED_METRICS`.
  - Provides deterministic semantic-to-physical mapping via `METRIC_TO_COLUMN_MAPPING`.
  - Prevents free-form extraction drift by anchoring output fields to approved values.
- **Prompt governance** (`prompt_templates.py`):
  - Stage 1 prompt: strict extractor with explicit output constraints.
  - Stage 2 prompt: HyDE writer plus concise `rerank_query` generation.
- **Few-shot calibration** (`few_shot_config.py`):
  - Injects examples for SEC, macro/news, and options-style queries when available.

### B. Contract and Type-Safety Layer (`Scripts/retrieval/schema.py`)

- Uses Pydantic models as strict contracts for:
  - `MetadataExtraction`
  - `HyDEGeneration`
  - `FullTransformationResult`
  - `RetrievedChunk`
  - `QueryIntent`
- Uses enums to enforce standardized values (for source, action, form type, time window, sentiment).

### C. Transformation Intelligence Layer (`Scripts/retrieval/query_transform.py`)

- Executes asynchronous two-stage LLM transformation:
  1. **Extractor LLM** for structured metadata.
  2. **HyDE writer LLM** for retrieval-oriented synthetic paragraph and rerank phrase.
- Applies post-LLM guardrails:
  - ticker whitelist cleanup
  - mapped physical column derivation from ontology
  - audit logging to `logs/query_transform/<date>/query_audit_trail.jsonl`

### D. Retrieval Execution Layer (`Scripts/retrieval/qdrant_retriever.py`)

- Performs hybrid retrieval with three components:
  - Dense embedding retrieval (semantic channel)
  - Sparse SPLADE retrieval (lexical channel)
  - Cross-encoder reranking (precision channel)
- Applies metadata/time filters before fusion.
- Uses Qdrant `Fusion.RRF` to combine dense + sparse prefetch channels.
- Produces standardized `List[RetrievedChunk]` and writes retrieval audit logs to `logs/retrieval/<date>/retriever_audit_trail.jsonl`.

### E. Infrastructure and Connectivity Layer (`Scripts/vector_store/connection.py`)

- Provides cached singletons for:
  - Qdrant Cloud client (`get_qdrant_client`)
  - Embedding provider (`get_embedding_model`)
- Enforces REST/HTTP path (`prefer_grpc=False`) for firewall resilience.
- Loads all key runtime settings from `.env`.

---

## III. Retrieval Strategy Blueprint

### A. Why This Is an Asymmetric Hybrid Strategy

The system deliberately uses **different texts for different vector channels**:

- **Dense channel input:** `hyde_paragraph` (high-context semantic expansion)
- **Sparse channel input:** `rerank_query` (short factual lexical target)

This asymmetry balances:
- semantic recall for abstract phrasing, and
- exact precision for tickers, forms, and event keywords.

### B. Deterministic Filtering Strategy

`_build_smart_filter(...)` applies institutional filtering logic before retrieval:

- ticker matching (`ticker`)
- source-type matching (`source_type`)
- SEC action/form constraints (`action_direction`, `form_type`)
- News sentiment constraints (`tone_score > 0` or `< 0`)
- time-range bounds via `unified_timestamp`

This design reduces false positives before vector scoring begins.

### C. Post-Fusion Precision Strategy

After RRF fusion:

- candidate texts are rescored by a cross-encoder reranker,
- low-score noise is dropped (`score > 0.00001`),
- top-k finalized records are normalized to `RetrievedChunk`.

---

## IV. End-to-End Workflow (Execution Graph)

```text
[User Query]
    |
    v
[QueryIntent (upstream router/orchestrator)]
    |
    v
[Stage 1: Metadata Extraction | ChatOllama + Pydantic]
    |- ontology-constrained fields
    |- ticker whitelist guardrail
    |- enum-normalized metadata
    |
    v
[Stage 2: HyDE + Rerank Query Generation | ChatOllama + Pydantic]
    |- hyde_paragraph (semantic carrier)
    |- rerank_query (lexical carrier)
    |
    v
[Hybrid Vectorization]
    |- Dense: embed_query(hyde_paragraph)
    |- Sparse: SPLADE(query_embed(rerank_query))
    |
    v
[Qdrant Prefetch + Fusion.RRF]
    |- dense prefetch (using="dense")
    |- sparse prefetch (using="sparse")
    |- metadata/time smart filter
    |
    v
[Cross-Encoder Rerank]
    |- pairwise scoring: [rerank_query, candidate_text]
    |- sort descending
    |
    v
[RetrievedChunk[] Output]
    |- bronze_ref (accession/url/id fallback)
    |- source_type, score, metadata
    |
    v
[Audit Logs + downstream agent consumption]
```

---

## V. Model Registry and Compute Placement (CPU/GPU)

### A. Query Transformation Models

1. **Extractor LLM**
   - Class: `ChatOllama(...).with_structured_output(MetadataExtraction)`
   - Env key: `OLLAMA_CUSTOM_MODEL_NAME`
   - Default: `options-expert-v1:latest`
   - Purpose: strict metadata extraction and reasoning chain
   - Compute: managed by Ollama runtime (CPU/GPU selection depends on host Ollama configuration)

2. **HyDE Writer LLM**
   - Class: `ChatOllama(...).with_structured_output(HyDEGeneration)`
   - Env key: `OLLAMA_CUSTOM_MODEL_NAME`
   - Default: `options-expert-v1:latest`
   - Purpose: generate `hyde_paragraph` and `rerank_query`
   - Compute: managed by Ollama runtime (CPU/GPU depends on Ollama deployment)

### B. Retrieval Models

1. **Dense Embedding Model**
   - Source factory: `get_embedding_model()` in `connection.py`
   - Provider env: `EMBEDDING_PROVIDER` (default `huggingface`)
   - Model env: `EMBEDDING_MODEL_NAME` (default `BAAI/bge-large-en-v1.5`)
   - Device env: `EMBEDDING_DEVICE` (default `cpu`)
   - Compute:
     - HuggingFace mode: explicit `EMBEDDING_DEVICE` control (`cpu`, `cuda`, `mps`, etc.)
     - Ollama embedding mode: compute managed by Ollama server (`OLLAMA_HOST`)

2. **Sparse Embedding Model**
   - Class: `SparseTextEmbedding`
   - Env key: `SPARSE_MODEL_NAME`
   - Default: `prithivida/Splade_PP_en_v1`
   - Threads env: `FASTEMBED_THREADS` (default `4`)
   - Compute: CPU-oriented thread execution

3. **Reranker Model**
   - Class: `CrossEncoder`
   - Env key: `RERANKER_MODEL_NAME`
   - Default: `BAAI/bge-reranker-v2-m3`
   - Device env: `RETRIEVER_DEVICE` (default `cpu`)
   - Compute: explicit PyTorch device control via `RETRIEVER_DEVICE` (CPU or GPU-enabled if available)

### C. Data Plane / Database Runtime

- **Vector database:** Qdrant Cloud
- **Client:** `QdrantClient`
- **Connection mode:** HTTP/HTTPS (`prefer_grpc=False`)
- **Timeout:** `15.0s`
- **Retry policy:** up to `3` attempts with `2s` delay

---

## VI. Runtime Configuration Matrix (`.env`)

| Key | Default | Function |
| --- | --- | --- |
| `QDRANT_HOST` | required | Qdrant Cloud endpoint |
| `QDRANT_API_KEY` | required | Qdrant authentication |
| `EMBEDDING_PROVIDER` | `huggingface` | Dense embedding backend |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-large-en-v1.5` | Dense model selection |
| `EMBEDDING_DEVICE` | `cpu` | Dense model device placement |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama embedding server endpoint |
| `OLLAMA_CUSTOM_MODEL_NAME` | `options-expert-v1:latest` | Query-transform LLM for both stages |
| `SPARSE_MODEL_NAME` | `prithivida/Splade_PP_en_v1` | Sparse lexical model |
| `FASTEMBED_THREADS` | `4` | Sparse model thread count |
| `RERANKER_MODEL_NAME` | `BAAI/bge-reranker-v2-m3` | Cross-encoder model |
| `RETRIEVER_DEVICE` | `cpu` | Cross-encoder device placement |

---

## VII. Reliability and Auditability Controls

- **Singleton/caching controls:**
  - `@lru_cache` for embedding model and Qdrant client
  - singleton retriever instance via `__new__`
- **Traceability controls:**
  - transformation audit JSONL
  - retrieval audit JSONL with scores, latency, filters, fallback flag
- **Failure controls:**
  - retrying Qdrant connection policy
  - exception-safe retrieval returns empty list with logged diagnostics

---

## VIII. Practical Invocation Sequence

```python
# Pseudocode-level flow
intent = QueryIntent(primary_route="hybrid_both")
transform_result = await QueryTransformer().transform_for_dual_rag(query, intent)
chunks = await FinancialHybridRetriever().retrieve_async(query, transform_result, top_k=5)
```

This sequence represents the canonical retrieval path used by downstream agents.

