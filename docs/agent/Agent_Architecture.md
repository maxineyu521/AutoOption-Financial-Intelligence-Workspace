# Institutional Agent Architecture Blueprint

## 1. Goal

Define a production-grade, auditable multi-agent workflow that turns one user query into a fact-checked, risk-reviewed, schema-constrained options strategy output.

---

## 2. Architecture and Workflow
![Multi-agent and frontend workflow](../../images/Muti-agent_Frontend_workflow.svg)

## 3. Code Strategy

- **Orchestration:** `Scripts/agents/router.py` compiles LangGraph topology and conditional routing.
- **State discipline:** each node writes deterministic deltas into `AgentState` so every hop is replayable from one frozen envelope.
- **Governance:** Checker (facts/lineage) gates Critic (logic/risk), then Finalizer structures output.

### 3.1 Deterministic envelope (before the reasoning loop)

- **Retrieval Master (`MasterRetriever` + router entry):** builds the run’s evidence envelope—Silver metrics as the numeric system of record and Gold chunks as semantic narrative—then materialises it into `AgentState` for all downstream nodes.
- **Temporal grounding:** when the query lacks an explicit time anchor, per-source `TimePredicate` fallbacks and router defaults bound retrieval so numbers and narratives stay on comparable windows.
- **Global macro baseline:** `macro_context` is populated from the latest snapshot (`Data/Agent_Context/latest_macro_context.md`) so Analyst/Critic/Finalizer share one narrative macro backdrop instead of free-form model memory. Separately, `MasterRetriever` may **merge** macro-derived rows into `silver_context` via `build_macro_silver_patch` (Markdown fallback when Parquet macro is thin) so headline macro also appears as **citable `MACRO_*` anchors** inside the same Silver audit surface as DuckDB metrics.

### 3.2 Silver anti-drift (Checker “blue team”)

- **Frozen Silver ground truth:** after retrieval, `silver_context` is treated as an immutable audit snapshot for the Checker pass. The Checker does **not** re-query DuckDB or refresh Silver on rewrite cycles; it audits draft numerics and lineage claims only against the Silver values and anchors already in state—eliminating cross-revision **data shift** when SQL results would otherwise move under the draft.
- **Citation whitelist:** Analyst prompts carry explicit valid Silver/Gold ID pools; the Checker’s deterministic path enforces anchors against that whitelist (and related rules) so fabricated IDs cannot masquerade as evidence.

### 3.3 Analyst and IV pinning

- **First-pass regime lock:** Analyst computes implied-vol regime from Silver on the first pass and persists `iv_regime_pinned` so later passes do not flip regime when rescue logic or timing would change raw Silver rows.
- **Revision memory:** append-only `critic_feedback` (Checker + Critic) is surfaced to Analyst so each rewrite must address outstanding blocking findings; `revision_count` is incremented on Analyst entry and drives the breaker below.

### 3.4 Critic (“red team”) and false-fatal containment

- **Separation of duties:** Checker owns numeric/citation integrity; Critic assumes those checks passed and stresses strategy, regime fit, and risk posture.
- **Deterministic gates:** `critic.py` demotes non-actionable LLM “Fatal” labels (e.g. benign `UNKNOWN` IV or `NOISE` insider labels where policy says not blocking) so the graph does not spin on spurious fatals—blocking vs polish is explicit in state.

### 3.5 Revision cap and circuit breaker

- **Hard stop:** `AGENT_MAX_REVISIONS` (default `3` across router/checker/critic/finalizer) caps loop depth. When the cap is hit, routing forces **Finalizer** so the user always receives a bounded-latency terminal payload rather than an unbounded revise loop.

### 3.6 Finalizer and delivery contract

- **Structured output:** Finalizer maps the approved (or breaker-shortened) Markdown draft into the canonical `FinalReport` JSON shape plus rendered markdown, evidence pool linkage, and confidence ceilings that reflect revision depth and pipeline degradation flags.

### 3.7 Model strategy

- **Analyst / Finalizer:** both default to `gpt-4o-mini` (OpenAI API) as primary; `options-expert-v1:latest` (Ollama OpenAI-compatible) is the automatic backup on primary failure. `analyst_fallback_used` can bias Finalizer toward the same provider family for consistency.

---

## 4. Output Data Schema and Paths
| Layer | Contract | Primary Path |
|---|---|---|
| Global state | `AgentState` (`TypedDict`) | `Scripts/agents/state.py` |
| Feedback item | `AgentFeedback` (`BaseModel`) | `Scripts/agents/state.py` |
| Final payload | `final_strategy` dict (`FinalReport.model_dump()`) | `Scripts/agents/finalizer.py` |
| Runtime traces | node-level audit events (`node_audit_log`) | `Scripts/agents/router.py` |

---

## 5. How to Test
- **Graph e2e:** `python Scripts/tests/test_router_e2e.py`
- **Single query path:** `python -m Scripts query "What is the best risk-defined setup for QQQ this week?"`
- **Warmup:** `python -m Scripts warmup`

---

## 6. Dependence Files and One-Line Command
### Inputs
- `Scripts/agents/router.py`
- `Scripts/agents/state.py`
- `Scripts/agents/analyst.py`
- `Scripts/agents/checker.py`
- `Scripts/agents/critic.py`
- `Scripts/agents/finalizer.py`

### Outputs
- schema-constrained final strategy payload;
- auditable node verdict and revision lineage for each query run.

### Related Docs
- [Backend System Blueprint](../modular_guide/Backend_README.md)
- [System Topology](../modular_guide/ARCHITECTURE.md)
- [Orchestration Runtime](../modular_guide/Orchestration.md)
- [Observability Contracts](../modular_guide/Observability.md)
- [User Query Policy](../modular_guide/User_Query_Guide.md)

