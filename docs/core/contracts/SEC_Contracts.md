# SEC Contracts

## Purpose

SEC contracts separate insider transaction evidence from event filing evidence. Form 4 and 8-K records serve different analytical roles and must not be treated as interchangeable Gold evidence.

## SEC Form Coverage Model

| Contract | Role |
|---|---|
| `SECRequestedForms` | Records which SEC forms were requested |
| `SECExistenceResult` | Records which requested forms were retrieved and where payload chunks live |
| `SECCoverageContract` | Converts retrieval existence into `full`, `partial`, or `none` coverage |
| `SECMissingDisclosure` | Creates the disclosure payload for missing forms or slots |
| `SECAnalysisBundle` | Aggregates coverage, existence, feature extraction, analysis, and missing disclosure |

Canonical form and coverage modes:

| Mode | Values |
|---|---|
| `SECFormType` | `4`, `8-K` |
| `SECCoverageMode` | `full`, `partial`, `none` |
| `SECTone` | `negative`, `neutral`, `positive` |

## Form 4 Feature Contract

`Form4Feature` represents parsed insider-transaction evidence.

| Field | Meaning |
|---|---|
| `ticker` | Issuer ticker |
| `owner` | Reporting owner |
| `role` | Insider role when available |
| `action_direction` | `SELL`, `BUY`, `ACQUIRE/VEST`, or `NONE` |
| `transaction_date` | Transaction date |
| `filed_at` | Filing date |
| `shares` | Aggregated transaction shares |
| `price` | Weighted average price when available |
| `total_value` | Aggregated transaction value |
| `remaining_shares` | Post-transaction holdings when available |
| `is_10b5_1_planned` | Planned-trade indicator |
| `is_cluster_trade` | Same-ticker same-date cluster indicator |

## Form 4 Analysis Contract

`Form4AnalysisContract` summarizes insider activity.

| Field | Meaning |
|---|---|
| `filing_count` | Number of Form 4 features |
| `sell_count` | Count of sell-directed filings |
| `buy_count` | Count of buy-directed filings |
| `vest_count` | Count of compensation vesting/acquisition filings |
| `planned_count` | Count of 10b5-1 planned trades |
| `cluster_count` | Count of clustered trades |
| `total_value` | Aggregate value where available |
| `directional_read` | `selling_pressure`, `buying_support`, `compensation_vesting`, or `mixed` |

Compensation vesting is not equivalent to open-market buying or selling.

## Form 8-K Feature Contract

`Form8KFeature` represents event filing evidence.

| Field | Meaning |
|---|---|
| `ticker` | Issuer ticker |
| `accession_no` | Filing accession number |
| `filed_at` | Filing date |
| `tone_score` | Numeric tone signal when available |
| `topics` | Event categories |
| `entities` | Extracted entities |
| `content` | Retrieved content summary |
| `url` | Filing URL |

## Form 8-K Analysis Contract

`Form8KAnalysisContract` summarizes event filing pressure.

| Field | Meaning |
|---|---|
| `filing_count` | Number of 8-K features |
| `negative_count` | Negative tone count |
| `positive_count` | Positive tone count |
| `neutral_count` | Neutral tone count |
| `dominant_tone` | `negative`, `neutral`, or `positive` |
| `categories` | Distinct event categories |
| `event_pressure` | `elevated`, `contained`, `constructive`, or `mixed` |
| `repeat_pattern` | `isolated` or `repeated` |
| `latest_filing_date` | Most recent filing date |

## Missing Disclosure Contract

`SECMissingDisclosure` carries:

- `missing_forms`,
- `missing_slots`,
- `coverage_mode`,
- `has_missing`.

When requested SEC forms are not retrieved, downstream answers must disclose the missing SEC subtype rather than generalize the gap as missing Gold context.

## Common Misclassification Risks

- Treating Form 4 vesting as insider buying.
- Treating 8-K tone as insider-flow evidence.
- Treating any SEC hit as satisfying all SEC slots.
- Collapsing `partial` coverage into `full` coverage.

## Source Of Truth

- `Scripts/core/sec_contract.py`
- `Scripts/core/sec_analysis.py`
- `Scripts/core/financial_ontology.py`
- `Scripts/core/evidence_contracts.py`

