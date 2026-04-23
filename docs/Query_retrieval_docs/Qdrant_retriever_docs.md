# Gold-Layer Asymmetric Hybrid Retriever

_Scope: `Scripts/retrieval/qdrant_retriever.py`._

This document specifies the Gold-layer retrieval engine that turns a
`FullTransformationResult` into a ranked list of `RetrievedChunk`s.

---

## 1. Strategic Objective

Bridge the LLM's transformation payload (HyDE + metadata) and Qdrant
Cloud with three guarantees:

- **Temporal alignment** — all filters are built from
  `TimePredicate` objects produced upstream by `time_adapter`, not from
  wall-clock time. One anchor, one window, all sources.
- **Lexical precision** — SPLADE sparse retrieval against the raw
  query (or `rerank_query`) preserves rare tickers and acronyms that
  dense embeddings blur.
- **Post-fusion sharpness** — a cross-encoder reranker rescues the
  handful of true positives that RRF mixes with noise.

---

## 2. System Architecture

| Layer | Implementation |
| --- | --- |
| **Singleton resources** | `_instance` pattern caches Qdrant client + embedding models per process. |
| **Asymmetric vectorisation** | Dense ← `hyde_paragraph`, Sparse ← `rerank_query` (or raw query). |
| **Source-aware time filter** | Per-source `TimePredicate` → OR-joined `unified_timestamp` / `publish_timestamp` range conditions. |
| **Defensive filtering** | `models.Filter` pre-filter (ticker, source_type, action, form, sentiment). |
| **RRF fusion** | `Fusion.RRF` combines dense + sparse prefetch channels. |
| **Cross-encoder rerank** | `CrossEncoder(BAAI/bge-reranker-v2-m3)` rescores top-K. |

---

## 3. Execution Workflow

```text
[ FullTransformationResult + raw query + time_predicates ]
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 1 — Deterministic Filter Construction                  │
│  ├─► ticker match         (models.FieldCondition)           │
│  ├─► source_type match    (drops Silver-only source_types)  │
│  ├─► action_direction     (SEC)                             │
│  ├─► form_type            (SEC)                             │
│  ├─► sentiment range      (news)                            │
│  └─► per-source time OR   (unified_timestamp OR publish_ts) │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 2 — Asymmetric Vectorisation                           │
│  ├─► Dense : HuggingFace `BAAI/bge-base-en-v1.5`            │
│  │           (hyde_paragraph → dense vector)                │
│  └─► Sparse: fastembed `Splade_PP_en_v1`                    │
│              (rerank_query  → sparse vector)                │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 3 — Qdrant Prefetch + Fusion.RRF                       │
│  using="dense"  prefetch  + filter                          │
│  using="sparse" prefetch  + filter                          │
│  → RRF(fusion=RRF) top_k_pool candidates                    │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 4 — Cross-Encoder Rerank                               │
│  CrossEncoder(BAAI/bge-reranker-v2-m3)                      │
│  pairwise score([rerank_query, candidate_text]) → top_k     │
│  drop score ≤ 1e-5                                          │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
[ List[RetrievedChunk] ]
  └─► audit → logs/retrieval/<date>/retriever_audit_trail.jsonl
```

---

## 4. Temporal Alignment Contract

`retrieve_async` accepts an explicit
`time_predicates: Dict[SourceTimeKey, TimePredicate]` argument. If
present it is used directly; if absent the retriever falls back to the
single `time_window` value inside `FullTransformationResult`.

Two non-negotiable rules:

1. **OR over `unified_timestamp` and `publish_timestamp`.** Some
   collections only populate one of the two keys; the retriever
   must accept a hit on either. Without this, SEC/News filings
   written by the legacy ingestor are invisible.
2. **Business-day snapping.** Daily-grain predicates (for
   `source_type=options`) are computed by `trading_calendar.py` so
   that `"yesterday"` on a Monday resolves to the previous Friday —
   not an empty Sunday.

> **Ops note on Qdrant indexing.** `publish_timestamp` must be
> indexed on the collection, otherwise Qdrant raises
> `400 Index required but not found for "publish_timestamp"`. This
> is a one-time schema fix on the Qdrant side; see
> `Scripts/vector_store/ingestion.py` for the canonical index
> payload.

---

## 5. Output Schema — `RetrievedChunk`

| Field | Type | Source | Purpose |
| :--- | :--- | :--- | :--- |
| `chunk_id` | String | Qdrant `id` | Unique UUID — traceability. |
| `text` | String | Qdrant payload | Actual news / SEC / macro text. |
| `score` | Float | RRF + rerank | Post-rerank relevance. |
| `source_type` | String | Qdrant payload | `sec` / `news` / `macro` / `gpr`. |
| `timestamp` | Integer | `unified_timestamp` or `publish_timestamp` | Unix epoch. |
| `bronze_ref` | String | payload | Accession / URL / UUID fallback. |
| `metadata` | Dict | payload | Tickers, impacted_assets, form_type, tone_score, … |

---

## 6. Example Audit Entry

```json
{
  "timestamp": "2026-04-19T21:16:01.849708",
  "original_query": "What recent insider buying activity has there been for TSLA and how did the market react?",
  "rerank_query_used": "TSLA Form 4 insider buying executives past month",
  "filter_applied": {
    "must": [
      {"key": "ticker", "match": {"any": ["TSLA"]}},
      {"key": "source_type", "match": {"any": ["sec", "news"]}},
      {"key": "action_direction", "match": {"any": ["BUY", "ACQUIRE/VEST"]}},
      {"key": "form_type", "match": {"value": "4"}},
      {
        "should": [
          {"key": "unified_timestamp", "range": {"gte": 1774055761.0, "lte": 1776647761.0}},
          {"key": "publish_timestamp", "range": {"gte": 1774055761.0, "lte": 1776647761.0}}
        ]
      }
    ]
  },
  "fallback_triggered": false,
  "results_count": 1,
  "top_k_scores": [0.0145],
  "latency_sec": 0.857,
  "status": "SUCCESS"
}
```

---

## 7. Reliability Controls

| Control | Mechanism |
| --- | --- |
| Singleton resources | `__new__` + `@lru_cache` keeps heavy models loaded once per process. |
| Connection resilience | Qdrant client opens with `prefer_grpc=False`, `timeout=15s`, `retries=3 × 2s`. |
| Graceful failure | Any exception in `retrieve_async` logs a `FAILED` audit row and returns `[]` — agents never observe a crash. |
| Fallback tiers | `drop_ticker_180d` widens the window and drops the ticker filter when the first pass returns 0 chunks. |

---

## 8. Verification

```bash
python Scripts/retrieval/qdrant_retriever.py
pytest -xvs Scripts/tests/test_master_retriever.py
pytest -xvs Scripts/tests/test_router_e2e.py
```

Expected console shape:

1. `🔍 Stage 2: Qdrant Hybrid Retrieving...`
2. `✅ Final Retrieved: N chunks` followed by enumerated scores.

---

## 9. Environment Dependencies

```bash
pip install qdrant-client fastembed sentence-transformers python-dotenv asyncio
```

| Variable | Default | Effect |
| --- | --- | --- |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-base-en-v1.5` | Dense encoder |
| `EMBEDDING_DEVICE` | `cpu` | Dense encoder placement |
| `SPARSE_MODEL_NAME` | `prithivida/Splade_PP_en_v1` | Sparse encoder |
| `FASTEMBED_THREADS` | `6` | Sparse encoder threads |
| `RERANKER_MODEL_NAME` | `BAAI/bge-reranker-v2-m3` | Cross-encoder |
| `RETRIEVER_DEVICE` | `cpu` | Cross-encoder placement |
| `QDRANT_HOST` / `QDRANT_API_KEY` | required | Qdrant Cloud credentials |

_Depends on `Scripts.vector_store.connection` (clients) and
`Scripts.retrieval.schema` (Pydantic contracts)._
