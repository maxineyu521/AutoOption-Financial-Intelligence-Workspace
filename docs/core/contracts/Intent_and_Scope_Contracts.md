# Intent And Scope Contracts

## Purpose

Intent and scope contracts convert user language into a bounded financial problem. They determine the analytical family, required surfaces, data strictness, output ceilings, and refusal behavior before downstream agents begin interpretation.

These contracts classify the problem. They do not decide final recommendation strength.

## Contract Owners And Consumers

| Contract | Producer | Consumer |
|---|---|---|
| `MetadataExtraction` | Query transform | Master retrieval |
| `ScopeContract` | Master retrieval | Analyst, Checker, Critic, Finalizer |
| `RetrievalOutcome` scope fields | Master retrieval | Checker and Critic |

## MetadataExtraction Contract

`MetadataExtraction` is the structured intent payload. It captures the user request as machine-actionable routing inputs.

| Field | Role |
|---|---|
| `tickers` | Primary symbols extracted from the query |
| `metrics` | Requested business metrics such as IV, PCR, GPR, or insider activity |
| `source_types` | Requested source families such as `sec`, `news`, `gpr`, `options` |
| `primary_theme` | High-level analytical theme: `insider`, `geopolitics`, `cross_asset`, `options` |
| `primary_surface` | Primary evidence surface: `options_surface` or `macro_news_surface` |
| `asset_scope` | Asset shape: `single_name`, `benchmark`, `basket`, `unspecified` |
| `read_profile` | Answer-shaping profile: `board_state`, `posture_read`, `event_risk`, `structure_request` |
| `time_window` | Normalized retrieval window |

## ScopeContract Fields

`ScopeContract` is the upstream control surface compiled by retrieval.

| Field | Meaning |
|---|---|
| `query_family` | Canonical financial problem family |
| `strict_sources` | Sources required for the answer contract |
| `soft_context_sources` | Sources that may enrich but are not strict gates |
| `allowed_metrics` | Supported requested metrics |
| `unavailable_metrics` | Valid intent metrics not supported by current data |
| `query_slots` | Family-specific semantic slots |
| `slot_evidence_contracts` | Evidence sufficiency contract per slot |
| `analysis_mode` | Answer workflow pattern |
| `coverage_basis` | Truth basis for the answer |
| `market_analysis_only` | Whether a read-only market output is valid |
| `output_mode_ceiling` | Maximum permitted recommendation mode |
| `specificity_ceiling` | Maximum permitted structure specificity |
| `scope_status` | `in_scope` or `out_of_scope` |

## Canonical Modes

| Mode | Values | Interpretation |
|---|---|---|
| `analysis_mode` | `default_read`, `data_backed_read` | Whether the answer follows the default path or requires data-backed treatment |
| `coverage_basis` | `silver_only`, `silver_primary_with_soft_gold`, `hybrid_required` | Which evidence basis makes the answer admissible |
| `read_profile` | `board_state`, `posture_read`, `event_risk`, `structure_request` | The expected answer shape |
| `output_mode_ceiling` | `actionable_options`, `directional_watchlist`, `informational_only` | Hard ceiling on output actionability |
| `specificity_ceiling` | `structure_allowed`, `watchlist_only`, `no_structure` | Hard ceiling on options-structure specificity |

## Query Family Routing

Current canonical `query_family` values:

| Family | Purpose |
|---|---|
| `options_microstructure` | Options board, IV, skew, PCR, liquidity, and executable structure context |
| `insider_flow_driven` | SEC Form 4 / insider-flow-driven reads with optional options posture |
| `cross_asset_regime` | Macro, volatility, and cross-asset regime reads |
| `geopolitical_macro_read` | GPR/news/impact-basket geopolitical macro reads |
| `geopolitical_options_read` | Geopolitical risk connected to options volatility posture |

Compatibility aliases are resolved by core helpers, including `macro_regime -> cross_asset_regime`, `macro_geopolitics_risk -> geopolitical_macro_read`, and `geopolitical_commodity -> geopolitical_options_read`.

## Scope Status And Refusal Behavior

`scope_status = out_of_scope` means the system should not attempt a normal financial answer. The Finalizer may still render a scope-limited response using `refusal_reason`, `in_scope_tickers`, `out_of_scope_tickers`, and available boundary text.

`scope_status = in_scope` permits downstream analysis, subject to evidence contracts and output ceilings.

## Output Ceilings

`output_mode_ceiling` and `specificity_ceiling` constrain downstream governance. Even when the Critic sees enough evidence for a stronger answer, these ceilings prevent over-specific or over-actionable output.

Current posture activation truth:

Posture synthesis is activated by implementation logic when `read_profile == "posture_read"`, `query_family` is posture-capable, and `recommendation_mode != "actionable_options"`. Older prose may emphasize `market_analysis_only`; that field remains an answerability signal, but it is not the sole posture trigger in current code.

## Known Limits / Engineering Risks

- `ScopeContract` is strongly modeled in retrieval schema, but downstream state often carries it as a dictionary.
- `market_analysis_only`, `read_profile`, `analysis_mode`, and output ceilings all influence answer shape; ownership must remain explicit.
- Documentation must track implementation truth for posture activation to avoid contract drift.

## Source Of Truth

- `Scripts/retrieval/schema.py`
- `Scripts/retrieval/query_transform.py`
- `Scripts/retrieval/master_retriever.py`
- `Scripts/core/posture_contract.py`
- `Scripts/agents/state.py`

