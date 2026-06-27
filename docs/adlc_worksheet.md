# ADLC Worksheet — Finalizer Structure Visibility Enforcement

## 1. Problem Identification

**Source**: Phase 2 improvement roadmap, Item 4 / Workstream C
**Document**: `docs/Improvement/Financial_Contract_Control_Plane_Phase2.md`

**Observation**: The `finalizer_illustrative_structure_rate` metric reads **0.429** in the cached May 2026 evaluation (20 cases). This means only 3 out of 7 applicable cases preserved a legal illustrative structure when the mode allowed it.

**Root Cause**: Two-directional leak in structure visibility enforcement:
1. `_enforce_recommendation_mode` unconditionally clears `trade_ideas = []` for all non-actionable modes, destroying structure before it can be labeled illustrative
2. `_illustrative_structure_text` silently returns `""` when `allow_illustrative_structure` is False or `illustrative_structure_hint` is empty
3. No deterministic gate prevents structure leaking through in `no_structure` mode

## 2. Baseline Evaluation (Before)

**Evaluation command** (deterministic, no LLM):
```bash
python Scripts/evaluation/run_agentic_benchmark.py \
  --reuse-run-dir logs/agentic_eval/2026-05-09/20260509_180947 \
  --skip-llm-judge --skip-baseline --skip-production
```

**Baseline metrics** (cached May run, 20 cases on `main` branch):

| Metric | Baseline Value |
|---|---|
| `finalizer_illustrative_structure_rate` | **0.429** |
| `material_failure_case_rate` | 0.050 |
| `compliance_failure_case_rate` | 0.050 |
| `finalizer_truth_integrity_rate` | 0.983 |
| `finalizer_logic_integrity_rate` | 1.000 |
| `critical_failure_case_rate` | 0.000 |
| `risk_disclosure_pass_rate` | 1.000 |

> **Note**: These are from the cached May run. The "After" column must be measured on the SAME case set after the fix.

## 3. Fix Applied

**Branch**: `improvement-in-event`
**Commit range**: All commits after `baseline-pre-event` (tag at `76fb0a9`)

### Changes Made

1. **`Scripts/agents/finalizer.py`** — Added `FinalizerRenderGuard` typed schema and `_enforce_structure_visibility_mode` deterministic gate function. Wired into all three render paths (primary, fallback, degraded).

2. **`Scripts/tests/test_finalizer_structure_guard.py`** — New: 15 drift-guard regression tests covering all three modes and edge cases.

3. **`Scripts/tests/router_e2e_queries.json`** — Expanded from 5 → 12 cases with 7 new cases specifically exercising structure visibility transitions.

4. **`.agents/skills/finalizer-render-guard/SKILL.md`** — Spec-conforming skill packaging.

### Fix Details

The `_enforce_structure_visibility_mode` function runs AFTER `_enforce_recommendation_mode` and `_apply_render_safety_contract` as a final deterministic gate:

- **`no_structure`**: Strips `trade_ideas`, clears illustrative text, removes concrete structure patterns from text fields
- **`illustrative_structure`**: Clears `trade_ideas` but builds/preserves illustrative framing text with "Illustrative only" and "not a live recommendation" labeling — even when `allow_illustrative_structure` is False or hint is empty (falls back to captured LLM trade ideas)
- **`recommended_structure`**: Passes through unchanged

## 4. Re-Evaluation (After)

**Re-evaluation command**:
```bash
python Scripts/evaluation/run_agentic_benchmark.py \
  --reuse-run-dir logs/agentic_eval/2026-05-09/20260509_180947 \
  --skip-llm-judge --skip-baseline --skip-production
```

**After metrics** (to be filled after re-run):

| Metric | Baseline | After | Delta |
|---|---|---|---|
| `finalizer_illustrative_structure_rate` | 0.429 | _TBD_ | _TBD_ |
| `material_failure_case_rate` | 0.050 | _TBD_ | _TBD_ |
| `compliance_failure_case_rate` | 0.050 | _TBD_ | _TBD_ |
| `finalizer_truth_integrity_rate` | 0.983 | _TBD_ | _TBD_ |
| `finalizer_logic_integrity_rate` | 1.000 | _TBD_ | _TBD_ |
| `critical_failure_case_rate` | 0.000 | _TBD_ | _TBD_ |
| `risk_disclosure_pass_rate` | 1.000 | _TBD_ | _TBD_ |

## 5. Regression Check

The fix must NOT degrade any existing metric. All other pass-rates must be ≥ baseline.

### Drift-Guard Regression Tests
```bash
python -m pytest Scripts/tests/test_finalizer_structure_guard.py -v
```

Expected: All 15 tests pass.

## 6. Iterate

If the re-evaluation shows unexpected regressions:
1. Identify which specific case(s) failed
2. Check whether the fix's text stripping was too aggressive
3. Adjust `_NO_STRUCTURE_STRIP_PATTERNS` or `resolved_illustrative_text` fallback
4. Re-run and verify

## 7. Provenance

- Baseline tag: `baseline-pre-event` at commit `76fb0a9`
- All in-event work on branch: `improvement-in-event`
- No baseline modifications made
- License: MIT (present at `LICENSE`)
