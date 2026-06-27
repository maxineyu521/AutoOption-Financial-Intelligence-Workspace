# ADLC Worksheet — Finalizer Structure Visibility Enforcement

## 1. Scope

**Goal**: Fix the two-directional leak in the Finalizer's `structure_visibility_mode` enforcement so that:
- `no_structure` mode cannot render trade ideas or concrete options structure
- `illustrative_structure` mode preserves examples labeled "not a live recommendation" (currently silently dropped at 0.429 pass rate)
- `recommended_structure` mode passes trade ideas through unchanged

**Boundaries**: The fix is limited to the Finalizer rendering node (`Scripts/agents/finalizer.py`). It must NOT modify Critic mode decision logic, Analyst draft generation, or evidence retrieval.

**Success criteria**: `finalizer_illustrative_structure_rate` increases from baseline, with no regression on `evidence_retention_avg`, `finalizer_information_value_avg`, `risk_disclosure_pass_rate`, `material_failure_case_rate`, or `compliance_failure_case_rate`.

**Source**: Phase 2 improvement roadmap, Item 4 / Workstream C (`docs/Improvement/Financial_Contract_Control_Plane_Phase2.md`).

## 2. Design

**Root cause**: Two-directional leak in structure visibility enforcement:
1. `_enforce_recommendation_mode` unconditionally clears `trade_ideas = []` for all non-actionable modes, destroying structure before it can be labeled illustrative
2. `_illustrative_structure_text` silently returns `""` when `allow_illustrative_structure` is False or `illustrative_structure_hint` is empty
3. No deterministic gate prevents structure leaking through in `no_structure` mode

**Solution**: Add a deterministic post-LLM enforcement gate (`_enforce_structure_visibility_mode`) that runs AFTER `_enforce_recommendation_mode` and `_apply_render_safety_contract`. Three-way gate:

| Mode | Behavior |
|---|---|
| `no_structure` | Strip `trade_ideas`, clear illustrative text, regex-strip concrete structure from text |
| `illustrative_structure` | Clear `trade_ideas` but build/preserve illustrative framing with "not a live recommendation" |
| `recommended_structure` | Pass through unchanged |

**Key design decision**: Capture LLM-generated `trade_ideas` BEFORE `_enforce_recommendation_mode` wipes them. This enables building illustrative text from actual structure the LLM produced, even when the Critic's `illustrative_structure_hint` is empty.

## 3. Build

**Branch**: `improvement-in-event`
**Commit range**: All commits after `baseline-pre-event` (tag at `76fb0a9`)

### Changes Made

1. **`Scripts/agents/finalizer.py`** (+189 lines)
   - Added `FinalizerRenderGuard` Pydantic schema with `resolved_illustrative_text()` method
   - Added `_enforce_structure_visibility_mode` deterministic gate function
   - Added `_strip_concrete_structure_text` regex engine for `no_structure` text cleaning
   - Wired enforcement into all three render paths (primary, fallback, degraded)
   - Added `captured_trade_ideas = list(report.trade_ideas)` before mode wipe

2. **`Scripts/tests/test_finalizer_structure_guard.py`** (NEW, 407 lines)
   - 24 drift-guard regression tests covering all three modes and edge cases
   - No LLM required — all tests use deterministic Pydantic models

3. **`Scripts/tests/router_e2e_queries.json`** (+49 lines)
   - Expanded from 5 → 12 cases with 7 new structure-visibility-specific cases

4. **`.agents/skills/finalizer-render-guard/SKILL.md`** (NEW, 117 lines)
   - Spec-conforming skill with YAML frontmatter, gotchas, output template

5. **`docs/model_selection_rationale.md`** (NEW)
   - Pillar 4 deliverable: model assignments and cost/latency/quality justification per node

## 4. Evaluate

### Baseline (Before) — `main` at `baseline-pre-event`

_Pending: fresh baseline eval currently running. Will be populated when results land._

| Metric | Baseline |
|---|---|
| `finalizer_illustrative_structure_rate` | _TBD_ |
| `finalizer_information_value_avg` | _TBD_ |
| `finalizer_semantic_evidence_retention_avg` | _TBD_ |
| `finalizer_risk_disclosure_pass_rate` | _TBD_ |
| `finalizer_query_first_pass_rate` | _TBD_ |
| `material_failure_case_rate` | _TBD_ |
| `compliance_failure_case_rate` | _TBD_ |

### After — `improvement-in-event` with fix applied

_Pending: will run identical eval after baseline completes._

| Metric | Baseline | After | Delta |
|---|---|---|---|
| `finalizer_illustrative_structure_rate` | _TBD_ | _TBD_ | _TBD_ |
| `finalizer_information_value_avg` | _TBD_ | _TBD_ | _TBD_ |
| `finalizer_semantic_evidence_retention_avg` | _TBD_ | _TBD_ | _TBD_ |
| `finalizer_risk_disclosure_pass_rate` | _TBD_ | _TBD_ | _TBD_ |
| `finalizer_query_first_pass_rate` | _TBD_ | _TBD_ | _TBD_ |
| `material_failure_case_rate` | _TBD_ | _TBD_ | _TBD_ |
| `compliance_failure_case_rate` | _TBD_ | _TBD_ | _TBD_ |

### Drift-Guard Regression Tests

```bash
python -m pytest Scripts/tests/test_finalizer_structure_guard.py -v
```

**Result**: 24/24 passed (28.59s, no LLM required).

## 5. Deploy

- **Branch**: `improvement-in-event`
- **Baseline tag**: `baseline-pre-event` at commit `76fb0a9` (untouched)
- **No infrastructure changes**: Fix is pure application logic in `finalizer.py`
- **Backward compatible**: No new env vars, no new dependencies, no schema changes to `FinalReport`

## 6. Observe

### What to monitor after deployment

- `finalizer_illustrative_structure_rate` — the primary metric this fix targets
- `finalizer_information_value_avg` — ensure the fix doesn't over-compress answer content
- `finalizer_semantic_evidence_retention_avg` — ensure evidence isn't lost by text stripping
- `material_failure_case_rate` / `compliance_failure_case_rate` — ensure no new failures

### Skill for future agents

The `.agents/skills/finalizer-render-guard/SKILL.md` documents the fix's gotchas and verification steps, enabling future agents to maintain the enforcement logic correctly.

## 7. Iterate

If the evaluation shows unexpected regressions:
1. Identify which specific case(s) failed
2. Check whether `_strip_concrete_structure_text` was too aggressive (over-stripping non-structure text)
3. Adjust `_NO_STRUCTURE_STRIP_PATTERNS` regex patterns or `resolved_illustrative_text` fallback logic
4. Re-run eval and verify all metrics remain at or above baseline

If the fix passes but the delta is smaller than expected:
1. Check whether the Critic's `structure_visibility_mode` assignment matches expectations for each case
2. Consider whether upstream hint extraction (`_extract_structure_hint` in `critic.py`) needs tuning

## Provenance

- Baseline tag: `baseline-pre-event` at commit `76fb0a9`
- All in-event work on branch: `improvement-in-event`
- No baseline modifications made
- License: MIT (present at `LICENSE`)
