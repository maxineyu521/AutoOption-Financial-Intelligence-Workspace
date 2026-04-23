# Query Intent & Transformation Layer

_Scope: `Scripts/retrieval/query_transform.py` +
`Scripts/retrieval/master_retriever.py::router_llm`._

This document defines **how a raw user question becomes a pair of
deterministic structured artefacts** (routing decision + transformation
payload) that the rest of the retrieval pipeline treats as a hard
contract.

---

## 1. Strategic Objective

The intent layer is the **policy gate** of the retrieval subsystem. It
converts ambiguous natural language into:

1. a **route** (Gold-only, Silver-only, or hybrid), and
2. a **structured extraction** (tickers, metrics, time window, action
   direction) plus a HyDE expansion used for dense retrieval.

Without this layer, downstream retrievers would have to parse the
query themselves — a recipe for drift, duplicated logic, and
non-deterministic behaviour across agents.

---

## 2. Two Cooperating Sub-Components

| Sub-component | File | Model | Purpose |
| --- | --- | --- | --- |
| **Intent Router** | `master_retriever.py::MasterRetriever.router_llm` | `llama3:latest` (env `OLLAMA_ROUTER_MODEL`) | Pick `gold_only` / `silver_only` / `hybrid_both`. Short JSON output. |
| **Query Transformer** | `query_transform.py::QueryTransformer` | `options-expert-v1:latest` (env `OLLAMA_CUSTOM_MODEL_NAME`) | Two-stage metadata extraction + HyDE generation. |

> **Model rationale.** The router only classifies into three buckets —
> using the fine-tuned 70B here would burn 30–60 s per query for no
> accuracy gain. The transformer, in contrast, has to reason about
> options terminology, SEC form types, macro indicators, etc., so it
> runs on the fine-tuned model. See `docs/LLM_Pool.md` §1 for the full
> model inventory.

---

## 3. Execution Workflow

```text
[ Raw User Query ]
        │
        ▼
┌─────────────────────────────────────────────────────┐
│ 3.1  MasterRetriever.router_llm (llama3:latest)     │
│      → QueryIntent(primary_route=...)               │
└─────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────┐
│ 3.2  QueryTransformer.transform_for_dual_rag        │
│      (options-expert-v1:latest, 70B, 2 stages)      │
│                                                     │
│  Stage 1 — EXTRACTOR                                │
│    ├─► Ontology-constrained metadata                │
│    ├─► Ticker whitelist guardrail                   │
│    └─► TimeWindow enum normalization                │
│                                                     │
│  Stage 2 — HyDE                                     │
│    ├─► hyde_paragraph (dense channel)               │
│    └─► rerank_query   (sparse + rerank channel)     │
└─────────────────────────────────────────────────────┘
        │
        ▼
[ FullTransformationResult + QueryIntent ]
        │
        ▼
   handed to QdrantRetriever / SilverSQLTool
```

---

## 4. Time Anchoring Contract

The transformer emits a `TimeWindow` enum (`yesterday`, `past_week`,
`past_month`, …). These enum values are **not interpreted here** —
they are resolved downstream by `Scripts/retrieval/time_adapter.py`
against a fixed anchor:

- The anchor is **always** `RunState.latest_business_day("options")`,
  read from `config/runtime/collect_data_state.json`. It is *never*
  `datetime.now()`.
- `time_adapter.compile_all` returns a `Dict[SourceTimeKey,
  TimePredicate]` — one window per data source (SEC, options, macro,
  news). Each source may have different business-day semantics.
- The resulting predicates are serialised into `AgentState` so that
  re-queries from `CheckerAgent._rescue_missing_anchors` use the same
  window as the initial retrieval. This eliminates the `latest_atm_iv`
  oscillation observed in the 2026-04-22 router_e2e logs.

See `docs/Query_retrieval_docs/Time_Schema_Audit.md` for the full
time-schema matrix.

---

## 5. Output Schema — `FullTransformationResult`

| Component | Field | Type | Purpose |
| :--- | :--- | :--- | :--- |
| **Metadata** | `logical_reasoning` | String | Stage-1 CoT; audit-only. |
| **Metadata** | `tickers` | List[str] | Guardrail-cleaned tickers. |
| **Metadata** | `metrics` | List[str] | Canonical metric names from `financial_ontology.ALLOWED_METRICS`. |
| **Metadata** | `source_types` | List[str] | `sec` / `news` / `options` / `macro`. |
| **Metadata** | `action_direction` | Enum | `BUY` / `SELL` / `NEUTRAL` / `ANY`. |
| **Metadata** | `form_type` | Enum | `4`, `8-K`, `13F`, `ANY`. |
| **Metadata** | `sentiment_target` | Enum | `POSITIVE` / `NEGATIVE` / `ANY`. |
| **Metadata** | `event_keyword` | String | Short lexical hook for SPLADE. |
| **Metadata** | `time_window` | Enum | Raw token — resolved downstream. |
| **HyDE** | `hyde_paragraph` | String | Semantic carrier for dense retrieval. |
| **HyDE** | `rerank_query` | String | Factual short phrase for sparse + rerank. |
| **Mapped** | `mapped_physical_columns` | List[str] | Derived via `METRIC_TO_COLUMN_MAPPING`. |

---

## 6. Example Output

```json
{
  "timestamp": "2026-04-19T21:16:00.977910",
  "latency_seconds": 107.96,
  "original_query": "What recent insider buying activity has there been for TSLA and how did the market react?",
  "primary_route": "hybrid_both",
  "transformation_result": {
    "metadata": {
      "logical_reasoning": "Macro Step: Insider activity. Meso: Market reaction. Micro: TSLA insiders. Action: BUY.",
      "tickers": ["TSLA"],
      "metrics": ["Insider Trading", "Price Change (%)"],
      "source_types": ["sec", "news"],
      "action_direction": "BUY",
      "form_type": "4",
      "sentiment_target": "ANY",
      "event_keyword": "insider_buying",
      "time_window": "past_month"
    },
    "hyde": {
      "hyde_paragraph": "TSLA insiders have recently filed Form 4s indicating buying activity...",
      "rerank_query": "TSLA Form 4 insider buying executives past month"
    },
    "mapped_physical_columns": ["mom_change_pct", "insider_net_flow", "daily_change_pct"]
  }
}
```

---

## 7. Reliability Controls

- **Ontology-locked output.** Every free-form LLM field is post-checked
  against `financial_ontology`. Unknown tickers are dropped, not
  silently persisted.
- **Ticker-explosion guardrail.** Queries that extract more than 8
  tickers are truncated to the top 5 to keep Qdrant filters tractable.
- **Audit trail.** Every transformation is appended to
  `logs/query_transform/<date>/query_audit_trail.jsonl` with the raw
  query, mapped columns, chosen route, and latency.
- **Failure semantics.** Either stage may raise — the upstream caller
  (`MasterRetriever.retrieve`) converts exceptions into a degraded
  route (`silver_only` with empty metadata) rather than bubbling up
  and killing the agent graph.

---

## 8. Verification

```bash
# Exercise the transformer alone (no Qdrant required)
python Scripts/retrieval/query_transform.py

# Exercise the full MasterRetriever (router + transform + retrieval)
pytest -xvs Scripts/tests/test_master_retriever.py
pytest -xvs Scripts/tests/test_router_e2e.py
```

Expected STDOUT shape for `query_transform.py`:

1. `[STAGE 1]` — reasoning, tickers, metrics, time window.
2. `[STAGE 2]` — HyDE paragraph + rerank query.
3. Final JSON serialisation suitable for pasting into issue reports.

---

## 9. Environment Dependencies

```bash
pip install langchain-core langchain-ollama pydantic python-dotenv
```

Required env entries (all present in the standard `.env`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_CUSTOM_MODEL_NAME` | `options-expert-v1:latest` | Transformer (both stages) |
| `OLLAMA_ROUTER_MODEL` | `llama3:latest` | Intent router in `MasterRetriever` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint |
