---
name: finalizer-render-guard
description: |
  Deterministic enforcement of structure_visibility_mode in the Finalizer agent's
  financial report rendering. Prevents the two-directional leak where the Finalizer
  either drops legally-allowed illustrative structure or promotes structure when the
  mode forbids it.

  Use this skill when:
    1. Modifying Finalizer rendering logic or trade idea visibility.
    2. Adding or changing recommendation mode enforcement.
    3. Debugging missing illustrative structure in non-actionable outputs.
    4. Working on Workstream C (Finalizer Render Boundary) from the Phase 2 roadmap.
    5. Adding new structure_visibility_mode values or enforcement rules.

  Do NOT use when:
    1. Modifying Critic mode decision logic (see critic.py directly).
    2. Changing the Analyst draft generation.
    3. Working on evidence carry or retrieval changes (Workstream D).
license: MIT
metadata:
  version: v1
  publisher: AutoOptions Team
---

# Finalizer Render Guard

## Problem Statement

The Finalizer agent is the terminal rendering node in the AutoOptions multi-agent pipeline. It converts the Analyst's fact-checked draft into a deterministic `FinalReport` Pydantic model. However, it has a **two-directional leak** on `structure_visibility_mode`:

### Leak Direction 1: Drops allowed illustrative structure
When `structure_visibility_mode == "illustrative_structure"`, the Finalizer's `_enforce_recommendation_mode` unconditionally clears `report.trade_ideas = []` for all non-actionable modes. The fallback `_illustrative_structure_text` silently returns `""` when:
- `allow_illustrative_structure` is `False` (due to `is_missing_hard_data` in the Critic)
- `illustrative_structure_hint` is empty (LLM draft didn't contain extractable structure)

### Leak Direction 2: Can promote structure when forbidden
When `structure_visibility_mode == "no_structure"`, no deterministic gate prevents the LLM from generating structure. Enforcement relies entirely on prompt guardrails.

## Fix: `_enforce_structure_visibility_mode`

A deterministic, post-LLM enforcement function that runs AFTER `_enforce_recommendation_mode` and `_apply_render_safety_contract`:

### Three-way gate

| Mode | Behavior |
|---|---|
| `no_structure` | Strip `trade_ideas`, clear illustrative text, remove concrete structure patterns from text fields |
| `illustrative_structure` | Clear `trade_ideas` but preserve illustrative framing text with "Illustrative only" and "not a live recommendation" labeling |
| `recommended_structure` | Pass through unchanged |

### Key implementation details

1. **Capture before wipe**: LLM-generated `trade_ideas` are captured BEFORE `_enforce_recommendation_mode` clears them. This allows building illustrative text from the actual structure the LLM produced.

2. **Fallback hint construction**: When neither the Critic's `illustrative_structure_hint` nor `illustrative_structure_text` is available, the guard builds the hint from captured trade ideas (strategy, ticker, strikes, expiry).

3. **Text stripping for no_structure**: Regex patterns matching concrete options structures (Long/Short Call/Put, spreads, condors, strike references) are stripped from `conversation_reply`.

4. **true_risk_text sourcing**: When `true_risk_text` is provided in revision constraints, it becomes the authoritative source for the Risks section.

## Gotchas

> [!WARNING]
> **`allow_illustrative_structure` vs `structure_visibility_mode`**: These are NOT the same thing. `structure_visibility_mode` is the Critic's decision about what visibility level is appropriate. `allow_illustrative_structure` is a secondary gate that can suppress illustration when `is_missing_hard_data` is True. The render guard must handle the case where the mode is `illustrative_structure` but `allow` is False — in this case, the guard still produces illustrative text using captured trade ideas.

> [!WARNING]
> **Hint extraction dependency**: The Critic's `_extract_structure_hint` parses structure from the Analyst draft using regex. If the LLM changes its output format, the hint can be empty. The render guard's fallback to captured trade ideas mitigates this.

> [!WARNING]
> **Ordering matters**: The enforcement runs AFTER `_apply_render_safety_contract`, which can overwrite `key_risks_and_hedges`. The render guard's `true_risk_text` enforcement must be the last write to that field.

## Output Template: FinalizerRenderGuard Schema

```python
class FinalizerRenderGuard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    structure_visibility_mode: Literal[
        "recommended_structure", "illustrative_structure", "no_structure"
    ] = "no_structure"
    recommendation_mode: str = "informational_only"
    allow_illustrative_structure: bool = False
    illustrative_structure_hint: str = ""
    illustrative_structure_text: str = ""
    true_risk_text: str = ""
    forbid_actionable_recommendation: bool = True

    def resolved_illustrative_text(
        self, *, captured_trade_ideas: List[TradeIdea]
    ) -> str:
        """Deterministically builds illustrative framing text."""
        ...
```

## Verification

Run the drift-guard regression tests (no LLM required):

```bash
python -m pytest Scripts/tests/test_finalizer_structure_guard.py -v
```

Key assertions:
- `no_structure` cannot render trade ideas
- `illustrative_structure` labels examples non-live
- `recommended_structure` passes through unchanged
- `true_risk_text` is sourced for the Risks section

## Related Files

- `Scripts/agents/finalizer.py` — the enforcement implementation
- `Scripts/agents/critic.py` — where `structure_visibility_mode` is decided
- `Scripts/agents/state.py` — `RenderSafetyContract` definition
- `Scripts/evaluation/agentic_node_eval.py` — `finalizer_illustrative_structure_rate` metric
- `Scripts/evaluation/agentic_eval.py` — `illustrative_structure_allowed_pass_rate` metric
- `docs/Improvement/Financial_Contract_Control_Plane_Phase2.md` — Item 4, Workstream C
