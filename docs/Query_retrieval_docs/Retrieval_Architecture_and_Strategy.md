# Retrieval Architecture and Strategy - Institutional Blueprint

## 1. Goal and Operating Principle

Define the production retrieval contract that transforms a user query into a unified, auditable context payload for the multi-agent pipeline.  
The architecture must preserve deterministic numeric grounding (Silver), semantic breadth (Gold), and explicit degradation behavior under partial infrastructure failure.

## 2. Architecture and Workflow
![Retrieval workflow](../../images/Retrieval_workflow.svg)

## 3. Code Strategy 

Strategy highlights:

- Route-specific retrieval plans prevent over-fetching while preserving fallback observability.
- Gold and Silver run with independent timeout budgets and exception isolation.
- Per-source `TimePredicate` compilation ensures daily/monthly/event sources use physically valid windows.
- HyDE-derived novel tickers are isolated in compensation context to avoid contaminating primary numeric truth.
- Final payload includes explicit `status` (`success` or `partial_failure`) rather than silent empty returns.

## 4. Output Data Schema and Paths

| Field | Type | Description | Source | Path |
|---|---|---|---|---|
| `intent` | `str` | Chosen route (`sql_only`, `vector_only`, `hybrid_both`) | Router | `Scripts/retrieval/master_retriever.py` |
| `metadata` | `MetadataExtraction` | Structured query controls (tickers, metrics, windows, source hints) | Transformer Stage 1 | `Scripts/retrieval/query_transform.py` |
| `time_range` | `Dict[str, Any]` | Canonical window + serialized per-source predicates | Time adapter integration | `Scripts/retrieval/master_retriever.py` |
| `gold_context` | `List[RetrievedChunk]` | Gold semantic chunks after hybrid retrieval + rerank | Qdrant retriever | `Scripts/retrieval/qdrant_retriever.py` |
| `silver_context` | `Dict[str, Any]` | Deterministic numeric values and anchors | Silver SQL tool | `Scripts/retrieval/sql_tools.py` |
| `silver_context.compensation` | `Dict[str, Any]` | HyDE-triggered supplemental Silver retrieval | MasterRetriever compensation path | `Scripts/retrieval/master_retriever.py` |
| `hyde_anticipation` | `Dict[str, Any]` | HyDE paragraph, rerank query, entity extraction telemetry | Transformer + MasterRetriever | `Scripts/retrieval/query_transform.py` |
| `status` | `str` | Pipeline health (`success` or `partial_failure`) | MasterRetriever | `Scripts/retrieval/master_retriever.py` |
| `latency_stats` | `Dict[str, str]` | End-to-end timing summary | MasterRetriever | `Scripts/retrieval/master_retriever.py` |

## 5. How to Test

```bash
python Scripts/tests/test_router_e2e.py
python -m Scripts warmup
python -m Scripts query "Past month AAPL Form-4 selling signal and put positioning?"
```

Validation focus:

- each route (`sql_only`, `vector_only`, `hybrid_both`) produces expected context shape,
- `time_range.source_predicates` is populated and stable across retries,
- partial failures degrade with explicit `status` and error surfaces.

## 6. Dependency Files, Linked Docs, and One-Line Commands

### 6.1 Retrieval dependency files

- `Scripts/retrieval/master_retriever.py`
- `Scripts/retrieval/query_transform.py`
- `Scripts/retrieval/qdrant_retriever.py`
- `Scripts/retrieval/sql_tools.py`
- `Scripts/retrieval/time_adapter.py`
- `Scripts/retrieval/schema.py`

### 6.2 Linked documentation

- [Query Intent and Transformation](./Query_intent_docs.md)
- [Qdrant Retriever Docs](./Qdrant_retriever_docs.md)
- [Silver SQL Tools](./Silver_SQL_Tools.md)
- [Time Adapter](./Time_Adapter.md)
- [Observability](../Observability.md)



