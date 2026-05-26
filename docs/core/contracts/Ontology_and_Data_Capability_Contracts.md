# Ontology And Data Capability Contracts

## Purpose

Ontology and data capability contracts define what the system can safely know. They bridge business intent, physical data schemas, and downstream actionability.

The ontology admits valid financial requests while preventing unsupported metrics from being silently substituted or hallucinated.

## Financial Ontology Boundary

The financial ontology defines:

| Surface | Contract |
|---|---|
| Source whitelist | `ALLOWED_SOURCES` and source aliases |
| Category whitelist | `ALLOWED_CATEGORIES` |
| Metric whitelist | `ALLOWED_METRICS` |
| Physical schema | `DATASET_PHYSICAL_SCHEMA` |
| Metric mapping | `METRIC_TO_COLUMN_MAPPING` |
| Family slots | `QUERY_FAMILY_SLOTS` |

The ontology is the single source of truth for whether a metric is supported, reserved, or physically mapped.

## Metric-To-Column Mapping

`METRIC_TO_COLUMN_MAPPING` maps business metrics to physical columns. Non-empty mappings identify available or derivable data. Empty mappings identify valid intent that is not currently backed by ingested columns.

| Mapping Shape | Meaning | Required Behavior |
|---|---|---|
| `["implied_volatility"]` | Direct physical support | Retrieve and cite the value |
| `["option_type", "volume", "open_interest"]` | Derived metric support | Compute from source columns |
| `[]` | Valid intent but unsupported data | Disclose or downgrade; do not invent |

Examples of reserved unsupported metrics include `Greeks (Delta/Gamma)`, `Yield Spread`, and `Institutional Flows`.

## Available Versus Unavailable Metrics

`available_metrics` are requested metrics with registered physical support. `unavailable_metrics` are requested metrics that either are not registered or map to an empty column list.

Unavailable metrics are not fatal by themselves. They become governance constraints when the answer would require those metrics for a stronger claim or concrete options structure.

## DataCapabilityProfile

`DataCapabilityProfile` is a compact downstream profile built from metadata, Silver context, Gold context, and time range.

| Field | Meaning |
|---|---|
| `requested_metrics` | Metrics requested by the user |
| `requested_sources` | Source families requested by the user |
| `available_metrics` | Requested metrics supported by ontology |
| `unavailable_metrics` | Requested metrics not supported by current data |
| `has_options_source` | User/request scope includes options |
| `has_options_chain_support` | Options evidence has at least one options-board signal |
| `has_iv_signal` | IV, IV rank, skew, or implied-volatility signal is present |
| `has_liquidity_signal` | Liquidity, spread, OI, volume, or market-impact signal is present |
| `has_strike_support` | Strike, moneyness, or underlying price support is present |
| `has_dte_support` | Expiration or DTE support is present |
| `has_price_signal` | Underlying or price-change signal is present |
| `has_gold_evidence` | Gold evidence was retrieved |
| `can_support_concrete_option_structure` | Data can support strike-level options structure discussion |

## Concrete Options Structure Gate

Concrete options structures require Silver-layer options support. Gold/news/SEC evidence may support direction, catalyst, or context, but it cannot create strike-level precision.

Minimum support for `can_support_concrete_option_structure` requires:

- options source requested,
- options chain support,
- IV signal,
- liquidity or price signal,
- strike support.

If all requested metrics are unavailable, concrete structure support is forced false.

## Failure And Downgrade Behavior

When capability is incomplete:

| Capability Gap | Expected Behavior |
|---|---|
| Unsupported metric | Disclose or avoid unsupported claim |
| Missing strike support | Avoid strike-level recommendation |
| Missing IV signal | Avoid volatility-regime structure claims |
| Missing liquidity signal | Avoid executable structure confidence |
| Gold evidence only | Permit narrative or catalyst read, not structure precision |

## Known Limits / Engineering Risks

- Metric support is centralized, but downstream code must consistently honor unavailable metrics.
- Empty mappings are intentionally valid ontology entries; new code must not treat them as accidental omissions.
- Capability profile is dictionary-shaped downstream, so schema hardening would reduce silent drift.

## Source Of Truth

- `Scripts/core/financial_ontology.py`
- `Scripts/core/financial_reasoning_contract.py`
- `Scripts/retrieval/sql_tools.py`
- `Scripts/agents/critic.py`

