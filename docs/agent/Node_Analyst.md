# Node Specification: Analyst

## 1. Goal

Produce the strategy draft (`draft_report`) from prepared retrieval context with strict Macro→Meso→Micro logic and citation discipline.

---
## 2. Architecture
```mermaid
flowchart LR
    S[AgentState Inputs] --> R[IV Regime Inference + Pinning]
    R --> P[Prompt Assembly]
    P --> L[LLM Inference]
    L --> D[draft_report]
    D --> U[revision_count + verdict reset]
```
Node entrypoint:
- `Scripts/agents/router.py` → `analyst_node()`
- model executor: `Scripts/agents/analyst.py` → `AnalystAgent.generate_report()`
---
## 3. Code Strategy and Workflow
- **Pinned IV regime:** first pass computes and stores `iv_regime_pinned`; later revisions reuse it.
- **Revision-aware drafting:** consumes append-only `critic_feedback`.
- **Robust inference path:** OpenAI-compatible client + health check + retries + model fallback + degraded text fallback.
- **Routing contract:** only this node increments `revision_count`.
---

## 4. Output Data Schema (and Path)
| Output Key | Type | Notes | Path |
|---|---|---|---|
| `draft_report` | `str` | markdown strategy draft | `Scripts/agents/router.py` |
| `revision_count` | `int` | incremented each analyst pass | `Scripts/agents/router.py` |
| `checker_verdict` | `None` | reset for new cycle | `Scripts/agents/router.py` |
| `critic_verdict` | `None` | reset for new cycle | `Scripts/agents/router.py` |
| `iv_regime_pinned` | `Optional[Dict[str, Any]]` | only written on first pass | `Scripts/agents/router.py` |
| `node_audit_log` | `List[Dict[str, Any]]` | node latency and state IO | `Scripts/agents/router.py` |
---
## 5. How to Test
- **Single query:** `python -m Scripts query "Analyze GLD options under current macro regime."`
- **E2E regression:** `python Scripts/tests/test_router_e2e.py`
- **Compile check:** `python -m py_compile Scripts/agents/analyst.py`
---
## 6. Dependency Files and One-Line Install
Key files:
- `Scripts/agents/analyst.py`
- `Scripts/agents/prompts.py`
- `Scripts/agents/state.py`
- `Data/Agent_Context/latest_macro_context.md`
Install:
```bash
pip install -r requirements.txt
```