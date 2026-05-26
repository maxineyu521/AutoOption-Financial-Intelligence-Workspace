# Narrative Contracts

## Purpose

Narrative contracts convert macro, news, geopolitical, and cross-asset evidence into structured render fields. They prevent the final answer from becoming an unrestricted prose synthesis over retrieved chunks.

## NewsSemanticProfile

`NewsSemanticProfile` is the deterministic search and routing profile for news-oriented reads.

| Field | Meaning |
|---|---|
| `tickers` | Primary tickers or assets |
| `canonical_topics` | Canonical ontology topics |
| `expanded_topics` | Related topics used for recall |
| `news_search_terms` | Combined asset and driver terms |
| `news_asset_terms` | Asset-specific search language |
| `news_driver_terms` | Macro or event driver language |
| `dense_context_terms` | Phrase-level semantic context for dense retrieval |
| `impacted_asset_aliases` | Human asset labels used in news payloads |
| `impact_basket` | Related tickers that define the transmission basket |

## NarrativeBrief

`NarrativeBrief` is the frontend-safe narrative handoff.

| Field | Render Role |
|---|---|
| `headline_read` | Direct narrative conclusion |
| `news_driver` | Primary retrieved news driver |
| `macro_transmission` | Rates, dollar, volatility, growth, or geopolitical channel |
| `asset_reaction` | Asset or basket response anchored to retrieved evidence |
| `game_theory_read` | Strategic interaction or policy-path framing |
| `volatility_setup` | Options-volatility setup when supported |
| `risk_read` | Narrative risk statement |
| `risk_trigger` | Concrete trigger that changes the read |
| `what_would_change` | Evidence or market condition that would revise the view |

## Topic Expansion And Impact Baskets

Topic expansion maps canonical topics to adjacent retrieval topics. Impact baskets map macro/news topics to assets that should be considered in the read.

Examples:

| Topic Family | Typical Impact Basket |
|---|---|
| `macro_geopolitics_risk` | GLD, SLV, VIX, S&P, DXY |
| `macro_central_banks` | GLD, SLV, DXY, VIX, S&P |
| `asset_precious_metals_spot` | GLD, SLV, DXY, VIX |

## Dense Versus Sparse Retrieval Language

Dense retrieval may use phrase-level semantic context such as `real yields` or `Fed policy path`. Sparse retrieval should use a smaller set of high-frequency terms to avoid noisy expansion.

The narrative contract owns this distinction so retrieval can remain precise without pushing topic logic into prompts.

## Narrative Field Ownership

Narrative fields are deterministic render inputs. Analyst may use them to build a draft and Finalizer may place them in sections, but neither should treat them as permission to invent unsupported macro causality.

Gold/news evidence can support narrative, catalyst, and transmission. It cannot create options strike precision without Silver support.

## Failure And Fallback Behavior

| Condition | Expected Behavior |
|---|---|
| Fresh news found | Anchor `news_driver` to the retrieved headline or content |
| No fresh news retrieved | Disclose background-only or lean on structured evidence |
| No asset-specific terms | Fall back to generic macro transmission language |
| Options posture metric unavailable | Avoid options-specific volatility setup |

## Known Limits / Engineering Risks

- Narrative profiles intentionally encode domain assumptions about topic adjacency and impact baskets.
- These assumptions should be reviewed when ingestion topics or asset coverage changes.
- Narrative text is structured, but still depends on retrieved metadata quality.

## Source Of Truth

- `Scripts/core/financial_narrative_contract.py`
- `Scripts/core/financial_ontology.py`
- `Scripts/retrieval/qdrant_retriever.py`
- `Scripts/agents/analyst.py`

