# Multi-Agent System — Architecture & Workflow

## 1. Goal

The agent layer is a **LangGraph-compiled multi-agent pipeline** that transforms a raw user financial question into a structured, fact-checked options strategy report. It orchestrates five specialised agents — Router, Analyst, Checker, Critic, and Finalizer — over an append-only shared state, with deterministic circuit-breakers preventing infinite revision loops.

---

## 2. System Architecture

### 2.1 Graph Topology

```
                    ┌──────────────────────────────────────┐
                    │          AgentState (shared)         │
                    └──────────────────────────────────────┘
                                      │
                    ┌─────────────────▼──────────────────┐
                    │       retrieval_master node         │
                    │  MasterRetriever.retrieve()         │
                    │  • Intent classification (router)   │
                    │  • QueryTransformer (2-stage LLM)   │
                    │  • Gold: Qdrant hybrid retrieval     │
                    │  • Silver: DuckDB parquet queries   │
                    │  • Macro + GPR always-on patches    │
                    │  • Writes silver_context_frozen     │
                    └─────────────────┬──────────────────┘
                                      │
                    ┌─────────────────▼──────────────────┐
                    │           analyst node              │
                    │  AnalystAgent.generate_report()     │
                    │  • Macro → Meso → Micro framework   │
                    │  • IV regime pinned on first pass   │
                    │  • Revision-aware (sees feedback)   │
                    │  • revision_count ++                │
                    └─────────────────┬──────────────────┘
                                      │
                    ┌─────────────────▼──────────────────┐
                    │           checker node              │
                    │  CheckerAgent.audit()               │
                    │  • Layer 1: deterministic regex     │
                    │    (uses silver_context_frozen)     │
                    │  • Layer 2: Silver rescue tool-call │
                    │  • Layer 3: LLM semantic audit      │
                    └────────────┬─────────┬─────────────┘
                                 │         │
                          fatal  │         │  pass / minor
                                 │         │
              ┌──────────────────▼──┐   ┌──▼──────────────────────┐
              │    analyst node     │   │      critic node         │
              │  (revision_count++) │   │  CriticAgent.audit()     │
              └──────────────────┬──┘   │  • IV regime fit         │
                                 │      │  • Macro contradiction   │
                                 │      │  • Insider signal check  │
                                 │      └──────────┬──────────────┘
                                 │                 │
                                 │          fatal  │  pass / minor
                                 │                 │
                                 └─────────────────┘
                                           │
                    ┌──────────────────────▼─────────────────────┐
                    │              finalizer node                 │
                    │  FinalizerAgent.format_and_clean()         │
                    │  • Markdown → FinalReport Pydantic schema  │
                    │  • Evidence pool construction              │
                    │  • confidence_score assignment             │
                    └────────────────────────────────────────────┘
```

### 2.2 Circuit-Breaker Logic

`revision_count` is incremented exclusively in `analyst_node`. Both `route_after_checker` and `route_after_critic` short-circuit to `finalizer` when `revision_count >= AGENT_MAX_REVISIONS` (env: `AGENT_MAX_REVISIONS`, default `3`).

```
route_after_checker(state) → str:
    if revision_count >= MAX_REVISIONS: return "finalizer"
    if checker_verdict == "fatal":      return "analyst"
    else:                               return "critic"

route_after_critic(state) → str:
    if revision_count >= MAX_REVISIONS: return "finalizer"
    if critic_verdict == "fatal":       return "analyst"
    else:                               return "finalizer"
```

### 2.3 Global Singletons (per process)

All heavy objects are initialised once at module load in `router.py` and shared across graph invocations:

| Singleton | Class | Cost |
|:---|:---|:---|
| `_MASTER_RETRIEVER` | `MasterRetriever` | Qdrant client + embedding models + DuckDB |
| `_SILVER_SQL_TOOL` | `SilverSQLTool` | Shared DuckDB connection + schema validation |
| `_ANALYST_AGENT` | `AnalystAgent` | `ChatOllama` client |
| `_CHECKER_AGENT` | `CheckerAgent` | `ChatOllama` + `SilverSQLTool` reference |
| `_CRITIC_AGENT` | `CriticAgent` | `ChatOllama` client |
| `_FINALIZER_AGENT` | `FinalizerAgent` | `ChatOllama` client |

---

## 3. Agent Profiles & Workflow

### 3.1 Analyst (`Scripts/agents/analyst.py`)

**Role:** Senior Options Strategist — drafts the Markdown strategy report.

| Aspect | Detail |
|:---|:---|
| **Framework** | Macro → Meso → Micro (mandatory three-layer structure) |
| **IV regime** | Deterministic pre-compute from `silver_context["values"]["latest_atm_iv"]`; pinned to `iv_regime_pinned` on the first pass so it never flips across revisions |
| **Citation format** | `[Silver: <lineage_anchor>]` for numeric claims; `[Gold: <bronze_ref>]` for qualitative claims |
| **Revision mode** | On re-entry, injects full append-only `critic_feedback` log (both Checker and Critic findings) via `render_revision_block()` |
| **LLM** | `options-expert-v1:latest` (fine-tuned; env: `OLLAMA_ANALYST_MODEL`), temperature `0.1` |

**Key output sections (mandatory):**

```markdown
## 1. Macro Regime Snapshot
## 2. Transmission Channel (Meso)
## 3. IV Regime & Structural Choice
## 4. Trade Idea(s)
## 5. Key Catalysts & Invalidation Levels
```

### 3.2 Checker (`Scripts/agents/checker.py`)

**Role:** Blue-Team Data Integrity Auditor — verifies every number and citation against the Silver layer.

| Aspect | Detail |
|:---|:---|
| **Layer 1: Deterministic** | Regex extracts all numbers and `[Silver: ...]` / `[Gold: ...]` citations from the draft; verifies against `silver_context_frozen` (immutable snapshot, not rescue-modified) |
| **Numeric tolerance** | 2% relative (`CHECKER_NUMERIC_TOLERANCE` env); bridges `0.45` ↔ `45%` encoding |
| **Source-aware pools** | Numbers routed by prefix: `MACRO_*`/VIX → macro pool; `gpr_*` → GPR pool; others → options pool — prevents cross-pool false positives |
| **Sentinel anchors** | `"INSUFFICIENT DATA"`, `"N/A"`, etc. are accepted without verification (Analyst is correct to use them when data is absent) |
| **Layer 2: Rescue** | When an anchor is missing, re-queries `SilverSQLTool` with pinned `TimePredicate`; downgrades Fatal → Minor on hit; merges new values into `silver_context` (not `frozen`) |
| **Layer 3: LLM** | `options-expert-v1:latest` (env: `OLLAMA_CHECKER_MODEL`), structured output → `CheckerResult` Pydantic model; best-effort, non-fatal on error |
| **Verdict** | `"fatal"` (any Fatal finding) → `"minor"` (only Minor findings) → `"pass"` (no findings) |

### 3.3 Critic (`Scripts/agents/critic.py`)

**Role:** Red-Team Chief Risk Officer — challenges strategy logic, not numbers.

| Aspect | Detail |
|:---|:---|
| **Review axes** | (1) IV regime fit, (2) macro contradiction, (3) insider signal weakness, (4) risk/reward imbalance |
| **Hard constraint** | Never challenges numeric values (Checker's domain); never requests more data |
| **LLM** | `options-expert-v1:latest`, structured output → `CriticResult` Pydantic model |
| **Verdict** | Same `"fatal"` / `"minor"` / `"pass"` convention as Checker |

### 3.4 Finalizer (`Scripts/agents/finalizer.py`)

**Role:** Convert the fact-checked Markdown draft into the deterministic `FinalReport` Pydantic schema.

| Aspect | Detail |
|:---|:---|
| **Task** | Copy numbers verbatim; assemble `evidence_pool` from `lineage_anchors` + Gold `bronze_ref`s; assign `confidence_score` |
| **Confidence scoring** | Full Gold + Silver + no revision → ≤ 0.90; partial / revision loop → ≤ 0.60; degraded → ≤ 0.30 |
| **LLM** | `options-expert-v1:latest`, structured output → `FinalReport` Pydantic model |

### 3.5 Prompts (`Scripts/agents/prompts.py`)

Single source of truth for all agent prompts. Global guardrails defined once and composed into every agent's system template:

| Directive | Applied to |
|:---|:---|
| `DATA_LINEAGE_DIRECTIVE` | Analyst, Checker |
| `MACRO_CHAIN_DIRECTIVE` | Analyst |
| `IV_REGIME_DIRECTIVE` | Analyst, Critic |
| `INSIDER_DISCIPLINE_DIRECTIVE` | Analyst, Critic |
| `TEMPORAL_DECAY_DIRECTIVE` | Analyst |
| `REVISION_INJECTION_TEMPLATE` | Analyst (revision passes only) |

---

## 4. AgentState Contract (`Scripts/agents/state.py`)

`AgentState` is the LangGraph `TypedDict` that every node reads and writes. It is the system's authoritative shared memory.

| Field | Type | Owner | Description |
|:---|:---|:---|:---|
| `original_query` | `str` | CLI / entrypoint | Raw user question |
| `macro_context` | `str` | `retrieval_master` | Contents of `Data/Agent_Context/latest_macro_context.md` |
| `metadata` | `MetadataExtraction` | `retrieval_master` | LLM-extracted tickers, metrics, time window, intent |
| `gold_context` | `List[Dict]` | `retrieval_master` | Qdrant chunks (`bronze_ref`, `content`, `source_type`, …) |
| `silver_context` | `Dict` | `retrieval_master` (+ rescue) | `{values, lineage_anchors, source_channel, compensation}` — mutable; updated by Checker rescue |
| `silver_context_frozen` | `Dict \| None` | `retrieval_master` | Immutable snapshot written once at retrieval exit; Checker's deterministic audit baseline |
| `time_range` | `Dict \| None` | `retrieval_master` | `{time_window_label, window_days, anchor_date, start_date, end_date, source_predicates}` |
| `hyde_anticipation` | `Dict \| None` | `retrieval_master` | HyDE paragraph, rerank query, novel tickers |
| `iv_regime_pinned` | `Dict \| None` | `analyst` (first pass only) | `{iv_regime, atm_iv, pcr_volume, thresholds}` — never overwritten after first draft |
| `draft_report` | `str` | `analyst` | Current Markdown draft |
| `critic_feedback` | `List[AgentFeedback]` | `checker` + `critic` | **Append-only** (LangGraph `operator.add` reducer); full audit trail across all revisions |
| `checker_verdict` | `str \| None` | `checker` | `"pass"`, `"fatal"`, `"minor"`, or `None` — short-circuit routing signal |
| `critic_verdict` | `str \| None` | `critic` | Same vocabulary as `checker_verdict` |
| `revision_count` | `int` | `analyst` | Number of completed drafts; exclusively incremented by `analyst_node` |
| `is_fallback` | `bool` | `retrieval_master` | True when retrieval degraded gracefully |
| `final_strategy` | `Dict` | `finalizer` | `FinalReport` Pydantic dict |

### `AgentFeedback` Schema

| Field | Type | Description |
|:---|:---|:---|
| `sender` | `str` | `"Checker"` or `"Critic"` |
| `error_type` | `str` | `"Fatal"` (must fix) or `"Minor"` (advisory) |
| `comment` | `str` | Specific finding description |
| `missing_lineage_id` | `List[str] \| None` | Anchor(s) that were missing |
| `revision_index` | `int \| None` | Which draft produced this finding |

---

## 5. How to Test

### End-to-end graph execution

```bash
# Single query via CLI
python -m Scripts --cmd query --question "What is the current IV skew for AAPL?"

# With model warm-up
python -m Scripts --cmd query --question "NVDA insider activity last week?" --warmup
```

### E2E harness (industrial-grade with per-node assertions)

```bash
python Scripts/tests/test_router_e2e.py
# Outputs: logs/router_e2e/{YYYY-MM-DD}/
#   {RUN_TS}_console.log
#   {RUN_TS}_run_summary.json
#   {RUN_TS}_{N}_{test_slug}_trace.jsonl
#   {RUN_TS}_{N}_{test_slug}_final_state.json
```

### Unit test: Checker deterministic audit

```python
from Scripts.agents.checker import _deterministic_audit

silver = {"values": {"latest_atm_iv": 0.38}, "lineage_anchors": ["IV_AAPL_2026-04-22"]}
draft = "AAPL ATM IV is at 38% [Silver: IV_AAPL_2026-04-22]."
feedbacks = _deterministic_audit(draft, silver, [], frozen_context=silver)
assert feedbacks == []  # pass — number matches, anchor known
```

### Inspect run artefacts

```bash
# Latest run summary
cat logs/router_e2e/$(date +%Y-%m-%d)/*_run_summary.json | python -m json.tool

# Per-node trace for first test
cat logs/router_e2e/$(date +%Y-%m-%d)/*_01_*_trace.jsonl | head -20
```

---

## 6. Dependencies

| Library | Purpose |
|:---|:---|
| `langgraph` | Graph compilation, state management, conditional edges |
| `langchain-ollama` | `ChatOllama` client for all LLM agents |
| `pydantic` | `AgentState`, `AgentFeedback`, `FinalReport` contracts |
| `python-dotenv` | Env variable loading (`AGENT_MAX_REVISIONS`, model names) |

```bash
pip install langgraph langchain-ollama pydantic python-dotenv
```

**Ollama models required:**
- `options-expert-v1:latest` — Analyst, Checker, Critic, Finalizer
- `llama3:latest` — Intent router (lightweight, `num_predict=20`)

```bash
ollama pull llama3
# options-expert-v1 is a custom fine-tune — see docs/LLM_Pool.md
```

**File dependencies:**

| File | Required by |
|:---|:---|
| `Data/Agent_Context/latest_macro_context.md` | `retrieval_master` (macro preamble) |
| `config/runtime/collect_data_state.json` | `SilverSQLTool` (dynamic anchors) |
| `Scripts/agents/state.py` | All nodes |
| `Scripts/agents/prompts.py` | All agent LLM prompts |
| `Scripts/retrieval/master_retriever.py` | `retrieval_master` node |
