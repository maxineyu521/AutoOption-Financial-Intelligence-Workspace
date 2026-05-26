# Financial Contract Control Plane: Phase 2 Improvements

## Scope

This note tracks follow-up improvements for the financial contract control plane after the institutional documentation pass under `docs/core/contracts/`.

It is intentionally framed as a **Phase 2 engineering hardening roadmap**, not as a production incident or urgent defect list.

The current system already has a coherent contract model across:
- intent and scope,
- ontology and data capability,
- evidence coverage,
- posture and liquidity interpretation,
- narrative synthesis,
- critic governance,
- final rendering.

The remaining work is mostly about making those boundaries harder to drift, easier to test, and safer for future contributors.

## Priority Model

The earlier P0/P1 labels are too severe for the current state. Use this softer priority model instead:

| Phase 2 Priority | Meaning | GitHub Handling |
|---|---|---|
| Near-term hardening | Worth doing before major feature expansion because it reduces drift risk | Track in this markdown; no standalone issue required unless implementation starts |
| Medium-term cleanup | Improves maintainability and future correctness | Track here; create issue only when scoped for a sprint |
| Known limit | Acceptable tradeoff if documented and tested | Disclose in docs; no issue needed |

## Current State

### 1. Contract activation rules are now documented

Current state:
- the new contract docs explicitly describe posture activation from implementation truth
- current posture activation depends on:
  - `read_profile == "posture_read"`
  - eligible `query_family`
  - `recommendation_mode != "actionable_options"`
- `market_analysis_only` remains documented as an answerability signal rather than the sole posture trigger

Why this is not an urgent bug:
- the implementation is deterministic
- the risk was mainly documentation drift
- the new docs reduce the chance of future contributors using the older simplified mental model

Phase 2 improvement:
- add a small regression test that locks the posture activation rule
- keep the legacy control-plane doc aligned with the module docs

### 2. Cross-node handoff contracts are still dictionary-shaped

Current state:
- several important handoffs are carried as dictionaries:
  - `scope_contract`
  - `revision_constraints`
  - `finalizer_input_card`
  - `critic_reasoning_profile`
- this is flexible and compatible with older traces
- the main risk is silent field drift, not known broken behavior

Why this is not an urgent bug:
- many upstream schemas already exist
- current code paths populate the expected fields
- documentation now records field ownership and expected consumers

Phase 2 improvement:
- introduce lightweight validators or Pydantic models for the highest-value handoffs
- start with `revision_constraints` and `finalizer_input_card`
- keep backward-compatible dictionary serialization for existing traces

### 3. Recommendation and actionability modes overlap

Current state:
- `recommendation_mode` and `actionability_mode` currently share the same value space:
  - `actionable_options`
  - `directional_watchlist`
  - `informational_only`
- this overlap is intentional for now
- the separate field gives future room for recommendation wording to evolve independently from actionability governance

Why this is not an urgent bug:
- the value space is explicit
- Critic writes both fields consistently in the current path
- the new governance doc records the distinction

Phase 2 improvement:
- add a mode ownership test that confirms:
  - `recommendation_mode` is the render-level output mode
  - `actionability_mode` mirrors the current actionability ceiling
  - scope ceilings can only make the mode more conservative

### 4. Finalizer boundary is policy-enforced more than schema-enforced

Current state:
- Finalizer is expected to render from `finalizer_input_card` and `revision_constraints`
- it should not derive new financial logic from raw state
- this boundary is documented but not fully enforced by a single typed input schema

Why this is not an urgent bug:
- the architectural intent is already present in code comments and docs
- Critic supplies explicit section ownership
- current Finalizer logic has multiple guardrails for recommendation mode enforcement

Phase 2 improvement:
- define a render-safe finalizer input schema
- validate required governance fields before final rendering
- add a regression test that prevents unsupported structure promotion when `structure_visibility_mode = "no_structure"`

### 5. Evidence coverage still depends partly on answer wording

Current state:
- slot coverage evaluation uses evidence tokens, text aliases, and answer-section matching
- this makes the audit explainable and deterministic
- it also means wording changes can affect coverage scores

Why this is not an urgent bug:
- this is a known tradeoff of semantic carry checking
- evidence contracts are centralized
- the new evidence doc clearly separates evidence gaps from market risks

Phase 2 improvement:
- add a structured evidence-carry field to Analyst output where practical
- use text matching as a fallback audit channel, not the only signal
- expand alias coverage only when a concrete false negative appears

## Phase 2 Workstreams

### Workstream A: Contract Drift Guardrails

Goal:
- prevent implementation and documentation from diverging on activation rules and mode semantics

Proposed work:
- add targeted tests for posture activation
- add tests for scope ceiling conservatism
- update the legacy `Financial_Contracts_Control_Plane.md` if any wording conflicts with the new docs

Acceptance criteria:
- posture activation test covers eligible and ineligible `read_profile` values
- mode ceiling test proves `output_mode_ceiling` and `specificity_ceiling` cannot promote actionability
- docs consistently describe `read_profile == "posture_read"` as the current posture activation trigger

### Workstream B: Handoff Schema Hardening

Goal:
- reduce silent dictionary field drift in cross-node handoffs

Proposed work:
- define typed models or validators for:
  - `revision_constraints`
  - `finalizer_input_card`
- preserve dictionary compatibility at state boundaries
- start with warnings or validation helpers before making failures hard

Acceptance criteria:
- missing critical governance fields are detected before final render
- typed model serialization remains compatible with existing state dictionaries
- tests cover missing, partial, and valid handoff payloads

### Workstream C: Finalizer Render Boundary

Goal:
- ensure Finalizer remains a renderer of upstream contracts, not a new financial reasoning node

Proposed work:
- make section ownership explicit in render helpers
- validate that unsupported structures are removed or downgraded according to `structure_visibility_mode`
- keep evidence coverage notes out of market-risk sections

Acceptance criteria:
- `no_structure` mode cannot render trade ideas
- `illustrative_structure` mode labels examples as non-live
- `true_risk_text` is the source of the Risks section when provided

### Workstream D: Evidence Carry Robustness

Goal:
- reduce brittleness from answer text matching while preserving auditable coverage checks

Proposed work:
- add structured evidence-carry metadata to Analyst output for high-value slots
- retain current alias/text evaluation as a deterministic secondary audit
- add regression fixtures for common slot families

Acceptance criteria:
- slot satisfaction can be evaluated from structured carry metadata when present
- text alias matching remains available for backward compatibility
- evidence gaps still produce deterministic disclosure or downgrade behavior

## Non-Goals

This Phase 2 note does not propose:
- rewriting the multi-agent workflow,
- changing financial thresholds,
- replacing current retrieval logic,
- changing recommendation policy,
- creating urgent GitHub issues for every known limit.

## Known Limits To Keep As Documentation Only

These are acceptable as documented limits unless a concrete failure appears:

- SEC / Gold / Silver semantics require clear documentation but do not need immediate code changes.
- PCR, IV rank, skew, liquidity tier, and market-impact thresholds are deterministic and hard-coded by design.
- Large module size increases maintenance cost, but refactoring should wait until ownership boundaries are fully documented.

## Suggested GitHub Handling

Do not open a severe P0-style issue for this entire set.

Preferred handling:
1. Keep this markdown as the Phase 2 roadmap.
2. Open focused GitHub issues only when a workstream is ready to implement.
3. Use neutral labels such as `improvement`, `technical-debt`, or `contract-hardening`.
4. Avoid incident-style language unless a reproducible behavioral bug is found.

Suggested future issue titles:
- `Add regression tests for posture activation and mode ceilings`
- `Introduce validators for finalizer handoff contracts`
- `Harden Finalizer structure-visibility enforcement`
- `Add structured evidence-carry metadata for slot coverage`

