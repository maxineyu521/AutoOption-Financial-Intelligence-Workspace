# Agentic Node Evaluation

## High Summary
The workflow is mostly keeping answers safe while still preserving useful non-actionable information under downgrade.

## Summary

- Cases: 20
- Mean revision count: 1.000
- Degraded case rate: 0.000
- Analyst -> Final truth gain avg: 0.217
- Analyst -> Final logic gain avg: 0.025
- Analyst -> Final information gain avg: -0.108
- Analyst -> Final intent gain avg: -0.025

## Metric Guide

### Workflow Overview

| Metric | Value | What it means |
| --- | --- | --- |
| n_cases | 20 | Number of paired production cases included in this node-evaluation run. |
| mean_revision_count | 1.000 | Average number of workflow passes needed before the answer reached its final state. |
| degraded_case_rate | 0.000 | Share of cases that exited through a degraded fallback path instead of a normal complete render. |
| checker_circuit_break_count | 0.000 | Count of cases where Checker findings effectively stopped the normal path and forced a fallback-style outcome. |
| completed_after_critic_count | 20.000 | Count of cases that completed normally after passing through Critic review. |

### Analyst

| Metric | Value | What it means |
| --- | --- | --- |
| analyst_logic_integrity_rate | 0.975 | How often the first Analyst draft avoided direction-changing financial logic errors. |
| analyst_truth_integrity_rate | 0.767 | How often the first Analyst draft stayed faithful to supported facts and honest missing-data disclosure. |
| analyst_slot_answer_rate | 0.908 | How often the Analyst either answered each requested slot or honestly disclosed that it could not. |
| analyst_semantic_evidence_carry_avg | 0.719 | How often the Analyst carried enough semantic evidence to support each answered slot. |
| analyst_intent_coverage_avg | 0.908 | How well the Analyst draft answered the user’s requested slots before later safety correction. |
| analyst_raw_metric_retention_avg | 0.255 | Legacy raw retention diagnostic: how much raw retrieval wording or numeric detail was repeated in the draft. |

### Checker

| Metric | Value | What it means |
| --- | --- | --- |
| checker_fix_adoption_avg | 0.775 | Whether Checker findings actually translated into cleaner final behavior, not just whether Checker ran. |

### Critic

| Metric | Value | What it means |
| --- | --- | --- |
| critic_actionability_downgrade_success_rate | 1.000 | How often Critic successfully pulled risky outputs back from live recommendation mode. |
| critic_risk_disclosure_success_rate | 1.000 | How often high-risk signals identified by Critic made it into the final answer. |
| critic_non_actionable_information_retention_delta_avg | -0.050 | Average semantic-information change from Analyst to Finalizer in non-actionable cases after Critic downgrade constraints. Near 0 means little compression; more negative values mean the answer was flattened. |

### Finalizer

| Metric | Value | What it means |
| --- | --- | --- |
| finalizer_logic_integrity_rate | 1.000 | How often the final answer avoided direction-changing financial logic mistakes after all node intervention. |
| finalizer_truth_integrity_rate | 0.983 | How often the final answer stayed grounded in supported facts and honest missing-data handling. |
| finalizer_slot_answer_rate | 0.883 | How often the final answer still answered each requested slot or honestly disclosed that it could not. |
| finalizer_semantic_evidence_retention_avg | 0.669 | How much semantic evidence survived into the final answer after downstream safety and rendering. |
| finalizer_disclosure_honesty_rate | 0.950 | How consistently the final answer preserved honest disclosure for slots that could not be answered from retrieval. |
| finalizer_information_value_avg | 0.792 | How complete and user-useful the final answer remains after safety and rendering constraints. |
| finalizer_risk_disclosure_pass_rate | 1.000 | How often the Finalizer explicitly preserved required risk language. |
| finalizer_illustrative_structure_rate | 0.429 | How often the Finalizer preserved a legal illustrative structure when the mode allowed it. |
| downgraded_answer_usefulness_floor_avg | 0.600 | Minimum usefulness floor for downgraded answers: 0.0 means required downgrade disclosure failed or no answer survived, 0.5 means information survived, 1.0 means information survived and the answer stayed query-first. |

### Outcome Quality

| Metric | Value | What it means |
| --- | --- | --- |
| critical_failure_case_rate | 0.000 | Share of cases containing direction-changing financial logic failures. |
| material_failure_case_rate | 0.050 | Share of cases containing fact-support or honest-disclosure failures. |
| compliance_failure_case_rate | 0.050 | Share of cases containing non-actionable boundary or illustrative-contract failures. |


| Metric | Value |
| --- | --- |
| Analyst contribution avg | 0.842 |
| Checker contribution avg | 0.908 |
| Critic contribution avg | 0.960 |
| Finalizer contribution avg | 0.849 |
| Mean revision count | 1.000 |
| Degraded case rate | 0.000 |
| Checker fix adoption avg | 0.775 |
| Critic feedback adoption avg | 1.000 |
| Critic downgrade success rate | 1.000 |
| Critic risk disclosure success rate | 1.000 |
| Critic non-actionable information retention delta avg | -0.050 |
| Analyst slot answer rate | 0.908 |
| Analyst semantic evidence carry avg | 0.719 |
| Analyst raw metric retention avg | 0.255 |
| Critical failure case rate | 0.000 |
| Material failure case rate | 0.050 |
| Compliance failure case rate | 0.050 |
| Finalizer logic integrity rate | 1.000 |
| Finalizer truth integrity rate | 0.983 |
| Finalizer slot answer rate | 0.883 |
| Finalizer semantic evidence retention avg | 0.669 |
| Finalizer disclosure honesty rate | 0.950 |
| Finalizer informational completeness avg | 0.792 |
| Finalizer evidence retention avg | 0.657 |
| Finalizer risk disclosure pass rate | 1.000 |
| Finalizer illustrative structure rate | 0.429 |
| Downgraded answer usefulness floor avg | 0.600 |

## Revision / Degraded Path

- Finalizer status counts: `{'complete': 20}`
- Finalizer trigger counts: `{'critic': 20}`
- Checker circuit-break count: `0`
- Completed-after-critic count: `20`

## Node Contribution Analytics

| Node | Contribution | Key Metric | Value | What it means |
| --- | --- | --- | --- | --- |
| Analyst | 0.842 | Analyst Logic | 0.975 | First evidence-backed draft quality: does the Analyst avoid direction-changing logic mistakes while answering the requested slots? |
| Checker | 0.908 | Checker Fix Adoption | 0.775 | QA correction adoption: do Checker findings actually land downstream and clean up the final behavior? |
| Critic | 0.960 | Non-actionable info delta (0 is best) | -0.050 (Compression: mild) | Governance and compression control: this centered delta shows whether non-actionable answers preserved Analyst evidence or flattened it. |
| Finalizer | 0.849 | Finalizer Truth Integrity | 0.983 | Final answer truthfulness and rendering integrity after all safety constraints and revisions. |

- `Contribution` = normalized composite node score for overall run performance. It is roughly on a `0-1` scale and higher is better.
- `Value` = the raw headline metric shown for interpretability. These values do not all share the same scale and should not be compared across nodes as if all were `0-1` positive metrics.
- Critic uses a centered metric where `0` is best. Current run: `-0.050` (`Compression: mild`). Near `0` means little compression, more negative means stronger flattening, and more positive means downstream clarification.

## Per-case Node Matrix

| Case | Revisions | Finalizer | Analyst semantic evidence | Checker adoption | Critic adoption | Critic risk | Logic gain | Truth gain | Info gain |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Microstructure - SPY hedge posture | 1 | complete | directional_watchlist | 0.500 | 1.000 | 1.000 | N/A | 0.000 | 0.333 | -0.013 |
| Microstructure - QQQ downside hedge | 1 | complete | directional_watchlist | 0.500 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | -0.160 |
| Microstructure - IWM downside screen | 1 | complete | directional_watchlist | 0.750 | 1.000 | 1.000 | N/A | 0.000 | 0.000 | -0.110 |
| Microstructure - GLD volatility screen | 1 | complete | directional_watchlist | 0.750 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | -0.110 |
| Single-name options - AAPL IV skew posture | 1 | complete | directional_watchlist | 0.500 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | -0.160 |
| Single-name options - NVDA premium posture | 1 | complete | directional_watchlist | 0.500 | 1.000 | 1.000 | N/A | 0.000 | 0.000 | -0.160 |
| Single-name options - AMD skew setup | 1 | complete | directional_watchlist | 0.750 | 1.000 | 1.000 | N/A | 0.000 | 0.000 | -0.285 |
| Single-name options - TSLA downside liquidity | 1 | complete | directional_watchlist | 0.500 | 1.000 | 1.000 | N/A | 0.000 | 0.000 | -0.160 |
| Cross-asset regime - QQQ IV versus VIX | 1 | complete | informational_only | 1.000 | 0.500 | 1.000 | 1.000 | 0.000 | 0.333 | -0.060 |
| Cross-asset regime - SPY Fed and yields impact | 1 | complete | informational_only | 0.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | 0.180 |
| Cross-asset regime - SPY CPI and VIX | 1 | complete | informational_only | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | -0.075 |
| Cross-asset regime - IWM small-cap stress | 1 | complete | informational_only | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | 0.000 | -0.075 |
| Geopolitical commodity - GLD risk backdrop | 1 | complete | informational_only | 1.000 | 0.500 | 1.000 | N/A | 0.000 | 0.333 | -0.180 |
| Insider flow - AAPL selling signal | 1 | complete | informational_only | 1.000 | 0.500 | 1.000 | 1.000 | 0.500 | 0.333 | -0.087 |
| Insider flow - NVDA insider signal | 1 | complete | informational_only | 0.750 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | -0.050 |
| Insider flow - AMD buying versus vesting | 1 | complete | informational_only | 0.750 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | -0.050 |
| Insider flow - TSLA default window posture | 1 | complete | informational_only | 0.875 | 1.000 | 1.000 | 1.000 | 0.000 | 0.667 | -0.113 |
| Geopolitical commodity - GLD hedge posture | 1 | complete | directional_watchlist | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | -0.440 |
| Geopolitical commodity - SLV safe-haven posture | 1 | complete | directional_watchlist | 0.500 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | 0.060 |
| Geopolitical commodity - GLD volatility and risk | 1 | complete | directional_watchlist | 0.750 | 1.000 | 1.000 | 1.000 | 0.000 | 0.333 | -0.110 |

- `Analyst` column = `semantic_evidence_carry_score`: `0.0` means almost no required semantic evidence survived in the draft, `0.5` means partial carry, and `1.0` means strong carry across the requested slots.
- `Checker` column = `fix_adoption_score`: `0.0` means checker findings did not translate into cleaner final behavior, `0.5` means partial or mixed adoption, and `1.0` means clean adoption or no fix needed.
- `Critic` column = `feedback_adoption_score`: `0.0` means critic governance feedback did not land downstream, `0.5` means partial or mixed influence, and `1.0` means downgrade / risk / governance feedback was adopted cleanly.
- These three per-case node columns are all normalized `0-1` node-influence scores. They are not the same thing as the `Value` column in Node Contribution Analytics.

## Downgrade Diagnostics

| Case | Mode | Risk required | Risk retained | Illustrative applicable | Illustrative present |
| --- | --- | --- | --- | --- | --- |
| Microstructure - SPY hedge posture | directional_watchlist | False | None | True | True |
| Microstructure - QQQ downside hedge | directional_watchlist | True | True | True | True |
| Microstructure - IWM downside screen | directional_watchlist | False | None | True | True |
| Microstructure - GLD volatility screen | directional_watchlist | True | True | True | True |
| Single-name options - AAPL IV skew posture | directional_watchlist | True | True | True | False |
| Single-name options - NVDA premium posture | directional_watchlist | False | None | True | False |
| Single-name options - AMD skew setup | directional_watchlist | False | None | True | False |
| Single-name options - TSLA downside liquidity | directional_watchlist | False | None | True | False |
| Cross-asset regime - QQQ IV versus VIX | informational_only | True | True | True | False |
| Cross-asset regime - SPY Fed and yields impact | informational_only | True | True | True | False |
| Cross-asset regime - SPY CPI and VIX | informational_only | True | True | False | None |
| Cross-asset regime - IWM small-cap stress | informational_only | True | True | False | None |
| Geopolitical commodity - GLD risk backdrop | informational_only | False | None | True | True |
| Insider flow - AAPL selling signal | informational_only | True | True | False | None |
| Insider flow - NVDA insider signal | informational_only | True | True | False | None |
| Insider flow - AMD buying versus vesting | informational_only | True | True | False | None |
| Insider flow - TSLA default window posture | informational_only | True | True | False | None |
| Geopolitical commodity - GLD hedge posture | directional_watchlist | True | True | True | False |
| Geopolitical commodity - SLV safe-haven posture | directional_watchlist | True | True | True | False |
| Geopolitical commodity - GLD volatility and risk | directional_watchlist | True | True | True | True |

- `informational_only`: read-only answer. The workflow concluded that the evidence or governance contract does not support a live recommendation.
- `directional_watchlist`: directional or regime read is supportable, but not a live trade recommendation. One clearly non-live illustrative structure hint is allowed when the contract permits it.
- `actionable_options`: live options recommendation path. The system has enough options-layer support to justify an options-specific actionable answer, potentially including structure-level or contract-level guidance.

- `Risk required`: the governance contract said the final answer must explicitly carry risk language.
- `Risk retained`: that required risk language survived into the final answer.
- `Illustrative applicable`: the finalizer contract allowed an illustrative non-live structure in this case.
- `Illustrative present`: a non-live illustrative structure actually appeared in the final answer when it was allowed.
- `informational_only` stays stricter: if `must_explain_why_not_now=True`, the answer must explain why-not-now, and any illustrative wording must remain clearly degraded and non-live.
- `directional_watchlist` may legally include one illustrative structure hint, but only if it stays monitoring-oriented and does not cross into active trade wording.

## Node Contribution Analytics Notes

- `Contribution` = normalized composite node score for overall run performance. It is roughly on a `0-1` scale and higher is better.
- `Value` = the raw headline metric shown for interpretability. These values do not all share the same scale and should not be compared across nodes as if they were all `0-1` positive metrics.
- Critic uses a centered metric: `Non-actionable info delta (0 is best)`. Current run: `-0.050` (Compression: mild). Near `0` means little compression, more negative means stronger flattening, and more positive means downstream clarification.

### Microstructure - SPY hedge posture

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['## Key Evidence\n- **Put-Call Ratio (PCR) Volume**: 0.983, indicating a Bullish/Neutral stance [Silver: PCR_AGG_SPY_2026-05-08].', 'Illustrative structure is present in non-actionable mode without an explicit risk warning.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Microstructure - QQQ downside hedge

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['## Key Evidence\n- **Put-Call Ratio Signal**: The put-call ratio for QQQ is 1.203, indicating a bearish sentiment in the market [Silver: PCR_AGG_QQQ_2026-05-08].', '- **Options Liquidity Posture**: QQQ has 829 liquid contracts, suggesting a moderate level of liquidity for options trading [Silver: LIQ_QQQ_2026-05-09].']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['risk_reward_imbalance', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Microstructure - IWM downside screen

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['Illustrative structure is present in non-actionable mode without an explicit risk warning.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Microstructure - GLD volatility screen

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `[]`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['risk_reward_imbalance', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Single-name options - AAPL IV skew posture

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['risk_reward_imbalance', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Single-name options - NVDA premium posture

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['iv_regime_fit', 'strategy_family_fit', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Single-name options - AMD skew setup

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Single-name options - TSLA downside liquidity

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['iv_regime_fit', 'macro_contradiction', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Cross-asset regime - QQQ IV versus VIX

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['# QQQ ATM IV vs VIX Divergence Hedge Signal Analysis\n\n## Executive Summary\n- **Confidence Level:** 70%\n- **Primary Driver:** Technical\n\n## Key Evidence\n- **At-the-Money Implied Volatility (ATM IV):** 0.2043 [Silver: IVRANK_QQQ_2026-05-09 00:00:00]\n- **IV Rank Percentile:** 42.86% [Silver: IVRANK_QQQ_2026-05-09 00:00:00]\n- **VIX Value:** 17.19 [Silver: MACRO_VIX_2026-05-09]\n- **VIX Change:** +0.64% [Silver: MACRO_VIX_2026-05-09]\n\n## Structure Hint\n- **Setup:** Iron Condor\n- **R\n\n## Missing Evidence / Limits\nMissing source(s): news.', 'Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Cross-asset regime - SPY Fed and yields impact

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['# SPY Puts Analysis\n\n## Executive Summary\n- **Confidence Level:** 70%\n- **Primary Driver:** Macro\n\n## Key Evidence\n- **VIX Value:** 17.27, indicating moderate market volatility [Silver: MACRO_VIX_2026-05-09].', '- **SPY Daily Option Volume:** 2,512,869 contracts, suggesting high liquidity [Silver: LIQ_SPY_2026-05-09].', 'Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention', 'risk_reward_imbalance', 'iv_regime_fit', 'macro_contradiction', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Cross-asset regime - SPY CPI and VIX

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `[]`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Cross-asset regime - IWM small-cap stress

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `[]`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Geopolitical commodity - GLD risk backdrop

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['# GLD Analysis\n\n## Executive Summary\n- **Confidence Level:** 85%\n- **Primary Driver:** Macro\n\n## Key Evidence\n- **ATM Implied Volatility (IV):** 0.2424 [Silver: IVRANK_GLD_2026-05-09 00:00:00]\n- **IV Rank Percentile:** 6.25% [Silver: IVRANK_GLD_2026-05-09 00:00:00]\n- **Geopolitical Risk Index Level:** 230.77, 97.58th percentile, trend falling [Silver: GPR_202604]\n\n## Structure Hint\n- **Setup:** Long Straddle\n- **Rationale:** With a low IV rank and a high GPR index, a long volatility strategy like a straddle could capitalize on potential volatility spikes due to geopolitical uncertainties', 'Illustrative structure is present in non-actionable mode without an explicit risk warning.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['catalyst_horizon_fit', 'evidence_sufficiency_and_abstention', 'iv_regime_fit', 'macro_contradiction', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `compliance`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `['Informational_only answer carried market_read_only posture that belongs in directional_watchlist.']`

### Insider flow - AAPL selling signal

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `['sec']`
- Missing slots: `['sec_insider_signal']`
- Analyst truthfulness issues: `['Answer describes insider selling even though observed SEC actions are vesting/NONE only.', 'The market impact risk is categorized as high, and there are 332 liquid contracts [Silver: LIQ_AAPL_2026-05-09].', 'Missing-source or missing-slot disclosure is absent or incomplete.']`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['risk_reward_imbalance', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `material`
- Critical failures: `[]`
- Material failures: `['Missing-source or missing-slot disclosure is absent or incomplete.']`
- Compliance failures: `[]`

### Insider flow - NVDA insider signal

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `['sec']`
- Missing slots: `['sec_insider_signal']`
- Analyst truthfulness issues: `['Missing-source or missing-slot disclosure is absent or incomplete.']`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['insider_signal_weakness', 'evidence_sufficiency_and_abstention', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Insider flow - AMD buying versus vesting

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `['sec']`
- Missing slots: `['sec_insider_signal']`
- Analyst truthfulness issues: `['Missing-source or missing-slot disclosure is absent or incomplete.']`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Insider flow - TSLA default window posture

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:minor -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `['sec']`
- Missing slots: `['sec_insider_signal']`
- Analyst truthfulness issues: `['## Key Evidence\n- **Options Liquidity Posture**:\n  - Daily Option Volume: 1,746,393 contracts [Silver: LIQ_TSLA_2026-05-09]\n  - Open Interest: 2,398,071 contracts [Silver: LIQ_TSLA_2026-05-09]\n  - Market Impact Risk: High [Silver: LIQ_TSLA_2026-05-09]\n  - Liquid Contracts: 712 [Silver: LIQ_TSLA_2026-05-09]\n\n## Main Risk\nThe high market impact risk indicates potential difficulties in executing large trades without affecting the market price significantly.', 'Missing-source or missing-slot disclosure is absent or incomplete.']`
- Checker verdict counts: `{'minor': 1}`
- Critic categories: `['evidence_sufficiency_and_abstention', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `informational_only`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Geopolitical commodity - GLD hedge posture

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['# GLD Geopolitical Risk and Options Hedge Posture Analysis\n\n## Executive Summary\n- **Confidence Level:** 85%\n- **Primary Driver:** Macro\n\n## Key Evidence\n- **Geopolitical Risk Index:** The GPR Index for April 2026 is 230.77, placing it in the 97.6th percentile, indicating high geopolitical risk [Silver: GPR_202604].', '- **GLD Options Market:** The GLD daily option volume is 219,210 with an open interest of 1,930,753, and the average spread is 30.35% [Silver: MACRO_GLD_SPOT_2026-05-09].', 'Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['risk_reward_imbalance', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Geopolitical commodity - SLV safe-haven posture

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['# SLV Geopolitical Risk and Options Hedge Posture\n\n## Direct Read\nThe geopolitical risk index (GPR) for April 2026 is at 230.77, indicating a cooling off from previous highs but still at a significant level [Silver: GPR_202604].', '## Key Evidence\n- **Geopolitical Risk Index**: 230.77, 97.58th percentile, trend falling [Silver: GPR_202604].', '- **SLV Daily Option Volume**: 385,004 contracts [Silver: MACRO_SLV_SPOT_2026-05-09].', 'Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer.']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['risk_reward_imbalance', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`

### Geopolitical commodity - GLD volatility and risk

- Revision path: `retrieval_master@r0:success -> analyst@r1 -> checker@r1:pass -> critic@r1:pass -> finalizer@r1:complete`
- Missing sources: `[]`
- Missing slots: `[]`
- Analyst truthfulness issues: `['- **Geopolitical Risk Posture:** The Geopolitical Risk (GPR) Index is at 230.77, placing it in the 97.6th percentile, suggesting elevated geopolitical tensions [Silver: GPR_202604].']`
- Checker verdict counts: `{'pass': 1}`
- Critic categories: `['catalyst_horizon_fit', 'evidence_sufficiency_and_abstention', 'risk_reward_imbalance', 'iv_regime_fit', 'macro_contradiction', 'evidence_sufficiency_and_abstention']`
- Finalizer status: `complete` | mode: `directional_watchlist`
- Highest failure severity: `None`
- Critical failures: `[]`
- Material failures: `[]`
- Compliance failures: `[]`
