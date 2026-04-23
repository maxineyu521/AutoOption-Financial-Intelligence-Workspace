# Retrieval Architecture, Strategy, and Workflow

_This document is the authoritative spec of the retrieval subsystem —
how raw user questions become ranked evidence that the agent graph
consumes. It supersedes all prior retrieval READMEs._

---

## I. Mission Profile and System Boundary

The retrieval subsystem is implemented across:

- `Scripts/core/financial_ontology.py`
- `Scripts/core/intent_router_prompt_templates.py`
- `Scripts/core/prompt_templates.py`
- `Scripts/core/few_shot_intent.py` / `few_shot_config.py`
- `Scripts/core/trading_calendar.py`
- `Scripts/retrieval/schema.py`
- `Scripts/retrieval/query_transform.py`
- `Scripts/retrieval/time_adapter.py`
- `Scripts/retrieval/master_retriever.py`
- `Scripts/retrieval/qdrant_retriever.py`
- `Scripts/retrieval/sql_tools.py`
- `Scripts/vector_store/connection.py`

It is a **two-stage transformation pipeline** followed by a
**dual-track retrieval-and-merge pipeline** (Gold semantic + Silver
structured), all anchored on a single `RunState`-owned business-day
timestamp.

---

## II. Institutional-Grade Architectural Topology

### A. Knowledge Governance Layer (`Scripts/core`)

- **Ontology governance** (`financial_ontology.py`)
  - Controlled vocabularies: `ALLOWED_SOURCES`, `ALLOWED_CATEGORIES`,
    `ALLOWED_METRICS`.
  - Deterministic metric→physical-column map
    (`METRIC_TO_COLUMN_MAPPING`).
  - Prevents free-form extraction drift.
- **Prompt governance** (`prompt_templates.py`,
  `intent_router_prompt_templates.py`)
  - Stage 1 extractor prompt, Stage 2 HyDE prompt, and the cheap
    intent-router prompt used by `MasterRetriever.router_llm`.
- **Few-shot calibration** (`few_shot_intent.py`)
  - Injects examples for SEC, macro/news, options-style queries.
- **Trading calendar** (`trading_calendar.py`)
  - Dependency-free business-day arithmetic (`previous_business_day`,
    `n_business_days_back`, `clamp_to_business_day`).

### B. Contract and Type-Safety Layer (`Scripts/retrieval/schema.py`)

Pydantic models serve as hard contracts:

- `MetadataExtraction`
- `HyDEGeneration`
- `FullTransformationResult`
- `RetrievedChunk`
- `QueryIntent`
- `SourceCitation`
- Enums for `TimeWindow`, `SourceType`, `ActionDirection`, `FormType`,
  `SentimentTarget`.

### C. Intent Routing Layer (`Scripts/retrieval/master_retriever.py::router_llm`)

- Cheap vanilla `llama3:latest` decides `gold_only` / `silver_only` /
  `hybrid_both`. See `docs/LLM_Pool.md` for the two-tier model
  rationale.

### D. Transformation Intelligence Layer (`Scripts/retrieval/query_transform.py`)

- Two-stage asynchronous LLM transformation on the fine-tuned 70B:
  1. **Extractor** (`OLLAMA_CUSTOM_MODEL_NAME`, `format=json`)
  2. **HyDE writer** (`OLLAMA_CUSTOM_MODEL_NAME`, higher temp)
- Post-LLM guardrails: ticker whitelist, ontology mapping, audit JSONL.

### E. Time Alignment Layer (`Scripts/retrieval/time_adapter.py`)

Single source of truth for retrieval time windows.

- `compile_all(time_window, anchor_date)` →
  `Dict[SourceTimeKey, TimePredicate]` — one window *per data source*
  (`SEC`, `OPTIONS`, `NEWS`, `MACRO`).
- Daily-grain sources are **business-day-aware** via
  `trading_calendar`. `"yesterday"` on a Monday resolves to Friday.
- `TimePredicate` is **serialisable** — it is persisted in
  `AgentState["time_range"]["source_predicates"]` so that the
  `CheckerAgent` rescue step re-queries Silver with the same window
  as the initial retrieval. This fixes the `latest_atm_iv` oscillation
  diagnosed in `docs/test/2026-04-22/router_e2e_deep_analysis.md`.

### F. Retrieval Execution Layer

Two coordinated retrievers, both orchestrated by `MasterRetriever`:

- `qdrant_retriever.py::FinancialHybridRetriever` — Gold semantic
  (Dense + Sparse + Rerank).
- `sql_tools.py::SilverSQLTool` — Silver structured (Parquet + metric
  dispatcher; consumes the same `TimePredicate` objects).

### G. Infrastructure Layer (`Scripts/vector_store/connection.py`)

- Cached singletons for Qdrant client and embedding model.
- `prefer_grpc=False` for firewall resilience.
- All credentials loaded from `.env`.

---

## III. Retrieval Strategy Blueprint

### A. Asymmetric Hybrid — Why Two Texts Feed Two Channels

| Channel | Input | Model | Rationale |
| --- | --- | --- | --- |
| Dense | `hyde_paragraph` (long, synthetic) | `BAAI/bge-base-en-v1.5` | Captures abstract semantic intent ("flight to safety", "IV crush"). |
| Sparse | `rerank_query` (short, factual) | `prithivida/Splade_PP_en_v1` | Enforces exact lexical match for tickers and form types. |

### B. Deterministic Pre-Filtering

`_build_smart_filter` narrows search space *before* vector math:

- `ticker`, `source_type` (with Silver-only types stripped)
- SEC: `action_direction`, `form_type`
- News: `tone_score > 0` / `< 0`
- Time: OR over `unified_timestamp` **and** `publish_timestamp`,
  bounded by the per-source `TimePredicate`.

### C. Post-Fusion Precision

After `Fusion.RRF` merges dense and sparse prefetch pools,
`BAAI/bge-reranker-v2-m3` rescores top-K. Chunks with score ≤ 1e-5 are
dropped.

### D. Macro as a First-Class Silver Anchor

`latest_macro_context.md` (written by the macro pipeline) is parsed
in `master_retriever.py::_parse_macro_snapshot` and folded into
`silver_context` as `MACRO_*` anchors. This closes the "macro citation
trap" where the Analyst cited unattributable macro values.

---

## IV. End-to-End Workflow (Execution Graph)

```text
[ User Query ]
    │
    ▼
[ MasterRetriever.router_llm (llama3:latest) ]            ← route decision
    │
    ▼
[ QueryTransformer (options-expert-v1:latest, 70B) ]      ← 2-stage transform
    │                                                     ─ EXTRACT (JSON)
    │                                                     ─ HyDE
    ▼
[ time_adapter.compile_all(anchor_date from RunState) ]   ← per-source windows
    │
    ├──────────────┬─────────────────────────────────────┐
    ▼              ▼                                     ▼
[ Gold ]       [ Silver SQL ]                      [ Macro snapshot ]
FinancialHybrid  sql_tools.SilverSQLTool           parse latest_macro_context.md
  Retriever       ├─ handler dispatcher (metrics) → MACRO_* anchors
  ├─ Dense        ├─ metadata windowed Parquet   │
  ├─ Sparse       └─ anchors[]                   │
  ├─ RRF fuse                                    │
  ├─ Rerank                                      │
  └─ drop score ≤ 1e-5                           │
    │                                            │
    └──────────────┬──────────────────────────────┘
                   ▼
            [ MasterRetriever merges → MasterRetrievalResult ]
                   │
                   ▼
              LangGraph Agents (Router → Analyst → Checker → Finalizer)
```

---

## V. Model Registry

This section mirrors `docs/ARCHITECTURE.md` §5. Treat that document as
canonical; this table is here for reader convenience.

### A. Ollama LLMs

| Role | Model | Env | Notes |
| --- | --- | --- | --- |
| Intent router | `llama3:latest` | `OLLAMA_ROUTER_MODEL` | `MasterRetriever.router_llm`. Small JSON output. |
| Metadata extractor | `options-expert-v1:latest` | `OLLAMA_CUSTOM_MODEL_NAME` | `QueryTransformer` Stage 1. Fine-tuned Llama-3.3-70B-Q4. |
| HyDE writer | `options-expert-v1:latest` | `OLLAMA_CUSTOM_MODEL_NAME` | `QueryTransformer` Stage 2. |

### B. Retrieval models (not Ollama)

| Purpose | Artefact | Env | Device |
| --- | --- | --- | --- |
| Dense encoder | `BAAI/bge-base-en-v1.5` | `EMBEDDING_MODEL_NAME` | `EMBEDDING_DEVICE` (default `cpu`) |
| Sparse (SPLADE) | `prithivida/Splade_PP_en_v1` | `SPARSE_MODEL_NAME` | CPU, `FASTEMBED_THREADS` |
| Cross-encoder reranker | `BAAI/bge-reranker-v2-m3` | `RERANKER_MODEL_NAME` | `RETRIEVER_DEVICE` |

### C. Data plane

- Vector DB: **Qdrant Cloud** (`QDRANT_HOST`, `QDRANT_API_KEY`)
- Connection: HTTP/HTTPS, `timeout=15s`, `retries=3 × 2s`.

---

## VI. Runtime Configuration Matrix (`.env`)

| Key | Default | Function |
| --- | --- | --- |
| `QDRANT_HOST` | required | Qdrant Cloud endpoint |
| `QDRANT_API_KEY` | required | Qdrant authentication |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-base-en-v1.5` | Dense encoder |
| `EMBEDDING_DEVICE` | `cpu` | Dense encoder device |
| `SPARSE_MODEL_NAME` | `prithivida/Splade_PP_en_v1` | Sparse encoder |
| `FASTEMBED_THREADS` | `6` | Sparse encoder threads |
| `RERANKER_MODEL_NAME` | `BAAI/bge-reranker-v2-m3` | Cross-encoder |
| `RETRIEVER_DEVICE` | `cpu` | Cross-encoder device |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_CUSTOM_MODEL_NAME` | `options-expert-v1:latest` | Query transform + all agents |
| `OLLAMA_ROUTER_MODEL` | `llama3:latest` | `MasterRetriever` router |

---

## VII. Reliability and Auditability Controls

- **Singleton caching** — `@lru_cache` for embeddings, `__new__` for
  retriever, `llm_pool` for Ollama clients.
- **Transformation audit** — `logs/query_transform/<date>/query_audit_trail.jsonl`
- **Retrieval audit** — `logs/retrieval/<date>/retriever_audit_trail.jsonl`
- **SilverSQL audit** — `logs/Parquet_Query/<date>/*` via the
  `SilverSQLAudit` logger.
- **Graceful failure** — any retriever that raises returns `[]` +
  logged diagnostics; agents never observe an exception.
- **Time determinism** — `TimePredicate`s serialised into `AgentState`
  guarantee that Checker rescue re-queries use an identical window to
  the original retrieval.

---

## VIII. Practical Invocation Sequence

```python
# Orchestrator-level flow (simplified)
from Scripts.retrieval.master_retriever import MasterRetriever

retriever = MasterRetriever()
result = await retriever.retrieve_async(
    raw_query="What is AAPL's IV skew yesterday and are insiders active?",
)
# result.gold_context   — List[RetrievedChunk]
# result.silver_context — List[AnchorRecord] (including MACRO_* anchors)
# result.time_range     — {anchor_date, source_predicates: [...]}
```

`result` is the canonical input for `Scripts.agents.router.router_node`.

---

## IX. Change Log

| Date | Change | Rationale |
| --- | --- | --- |
| 2026-04-22 | `time_adapter.TimePredicate` serialised into `AgentState` | Eliminates `latest_atm_iv` oscillation across Checker rescue. |
| 2026-04-22 | Macro snapshot parsed into first-class Silver anchors | Closes the macro citation trap. |
| 2026-04-22 | Dual time-key OR (`unified_timestamp` OR `publish_timestamp`) | Makes legacy SEC/news filings discoverable. |
| 2026-04-22 | `MasterRetriever.router_llm` env renamed to `OLLAMA_ROUTER_MODEL` | Separates router tier from expert tier per `docs/LLM_Pool.md`. |
| 2026-04-22 | Business-day snapping for daily-grain predicates | `"yesterday"` on a Monday → previous Friday. |
