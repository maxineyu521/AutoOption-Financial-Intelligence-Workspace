# External Model Comparison Dashboard

## Summary
This comparison reviews 4 node-evaluation runs of the same financial RAG workflow. The goal is to show which changes improved slot coverage, semantic evidence carry, safety boundaries, final-answer reliability, and mode-taxonomy correctness.

The four-run sequence shows a clear evolution in three phases:

1. **Rendering quality improved first**
   - the `Finalizer gpt-4o Baseline` improved final answer polish, but the workflow still over-compressed non-actionable answers.
2. **Disclosure semantics improved second**
   - `Hard-Missing vs Market-Read Split` better separated true hard-missing cases from read-only market-posture cases, mainly improving compliance tone and usefulness floor.
3. **Mode taxonomy and watchlist governance improved last**
   - `Watchlist Gate v1` sharply improved truth and reduced material failure.
   - `Protected Watchlist Floor + Re-eval` then corrected the overuse of `informational_only` and moved the system toward a more product-correct `directional_watchlist` split.

The final run is therefore not just the highest-scoring run. It is the first run where truth, compliance, and answer-mode taxonomy all point in the same direction.

## Run Lineup
- **Finalizer gpt-4o Baseline**: `logs\agentic_eval\2026-05-09\20260509_143613`
- **Hard-Missing vs Market-Read Split**: `logs\agentic_eval\2026-05-09\20260509_164803`
- **Watchlist Gate v1**: `logs\agentic_eval\2026-05-09\20260509_173314`
- **Protected Watchlist Floor + Re-eval**: `logs\agentic_eval\2026-05-09\20260509_180947\reeval_node`

## Iteration Deltas
### Finalizer gpt-4o Baseline -> Hard-Missing vs Market-Read Split
- Improved: Analyst Truth Integrity (+0.050), Critic Non-actionable Info Delta (+0.006), Final Truth Pass Rate (+0.050), Final Informational Completeness (+0.001), Final Illustrative Structure Rate (+0.095)
- Regressed: Analyst Semantic Evidence Carry (-0.006), Checker Fix Adoption (-0.125)

Interpretation:

- this iteration was mainly a **boundary-and-wording** improvement,
- it made non-actionable answers less apologetic and more usable,
- but it did not solve the deeper classification-shape issue.

### Hard-Missing vs Market-Read Split -> Watchlist Gate v1
- Improved: Analyst Truth Integrity (+0.017), Checker Fix Adoption (+0.025), Critic Non-actionable Info Delta (+0.019), Final Truth Pass Rate (+0.250), Final Truth Integrity (+0.067)
- Regressed: Final Illustrative Structure Rate (-0.267)

Interpretation:

- this is the largest step-change in pure answer quality,
- truth and material-failure behavior improved sharply,
- but the mode split still remained too compressed toward `informational_only`.

### Watchlist Gate v1 -> Protected Watchlist Floor + Re-eval
- Improved: Final Truth Pass Rate (+0.050), Final Informational Completeness (+0.045), Final Illustrative Structure Rate (+0.029), Compliance Failure Rate (-0.050)
- Regressed: Analyst Truth Integrity (-0.017), Critic Non-actionable Info Delta (-0.006), Final Semantic Evidence Retention (-0.006)

Interpretation:

- this is the most important product-shape iteration,
- runtime behavior improved through the protected watchlist floor,
- evaluator semantics also became more faithful to the intended watchlist contract,
- the tradeoff is still mild semantic compression downstream.

## Node Metrics
| Metric | Best | Finalizer gpt-4o Baseline | Hard-Missing vs Market-Read Split | Watchlist Gate v1 | Protected Watchlist Floor + Re-eval |
| --- | --- | --- | --- | --- | --- |
| Analyst Logic Integrity | Finalizer gpt-4o Baseline | 0.975 | 0.975 (0.000) | 0.975 (0.000) | 0.975 (0.000) |
| Analyst Truth Integrity | Watchlist Gate v1 | 0.717 | 0.767 (+0.050) | 0.783 (+0.017) | 0.767 (-0.017) |
| Analyst Slot Answer Rate | Finalizer gpt-4o Baseline | 0.908 | 0.908 (0.000) | 0.908 (0.000) | 0.908 (0.000) |
| Analyst Semantic Evidence Carry | Finalizer gpt-4o Baseline | 0.725 | 0.719 (-0.006) | 0.719 (0.000) | 0.719 (0.000) |
| Checker Fix Adoption | Finalizer gpt-4o Baseline | 0.875 | 0.750 (-0.125) | 0.775 (+0.025) | 0.775 (0.000) |
| Critic Feedback Adoption | Finalizer gpt-4o Baseline | 1.000 | 1.000 (0.000) | 1.000 (0.000) | 1.000 (0.000) |
| Critic Non-actionable Info Delta | Watchlist Gate v1 | -0.069 | -0.062 (+0.006) | -0.044 (+0.019) | -0.050 (-0.006) |
| Critic Downgrade Success | Finalizer gpt-4o Baseline | 1.000 | 1.000 (0.000) | 1.000 (0.000) | 1.000 (0.000) |
| Critic Risk Disclosure | Finalizer gpt-4o Baseline | 1.000 | 1.000 (0.000) | 1.000 (0.000) | 1.000 (0.000) |
| Final Truth Pass Rate | Protected Watchlist Floor + Re-eval | 0.550 | 0.600 (+0.050) | 0.850 (+0.250) | 0.900 (+0.050) |
| Final Truth Integrity | Watchlist Gate v1 | 0.917 | 0.917 (0.000) | 0.983 (+0.067) | 0.983 (0.000) |
| Final Slot Answer Rate | Watchlist Gate v1 | 0.858 | 0.858 (0.000) | 0.883 (+0.025) | 0.883 (0.000) |
| Final Semantic Evidence Retention | Watchlist Gate v1 | 0.656 | 0.656 (0.000) | 0.675 (+0.019) | 0.669 (-0.006) |
| Final Disclosure Honesty | Watchlist Gate v1 | 0.925 | 0.925 (0.000) | 0.950 (+0.025) | 0.950 (0.000) |
| Final Informational Completeness | Protected Watchlist Floor + Re-eval | 0.722 | 0.723 (+0.001) | 0.747 (+0.024) | 0.792 (+0.045) |
| Final Risk Disclosure | Finalizer gpt-4o Baseline | 1.000 | 1.000 (0.000) | 1.000 (0.000) | 1.000 (0.000) |
| Final Illustrative Structure Rate | Hard-Missing vs Market-Read Split | 0.571 | 0.667 (+0.095) | 0.400 (-0.267) | 0.429 (+0.029) |
| Downgraded Answer Usefulness Floor | Hard-Missing vs Market-Read Split | 0.475 | 0.600 (+0.125) | 0.600 (0.000) | 0.600 (0.000) |
| Critical Failure Rate | Finalizer gpt-4o Baseline | 0.000 | 0.000 (0.000) | 0.000 (0.000) | 0.000 (0.000) |
| Material Failure Rate | Watchlist Gate v1 | 0.250 | 0.250 (0.000) | 0.050 (-0.200) | 0.050 (0.000) |
| Compliance Failure Rate | Protected Watchlist Floor + Re-eval | 0.250 | 0.150 (-0.100) | 0.100 (-0.050) | 0.050 (-0.050) |

## Per-case Comparison
| Case | Finalizer gpt-4o Baseline | Hard-Missing vs Market-Read Split | Watchlist Gate v1 | Protected Watchlist Floor + Re-eval |
| --- | --- | --- | --- | --- |
| Cross-asset regime - IWM small-cap stress | Fail / material / A 1.000 / F 1.000 | Fail / material / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 |
| Cross-asset regime - QQQ IV versus VIX | Fail / material / A 1.000 / F 1.000 | Fail / material / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 |
| Cross-asset regime - SPY CPI and VIX | Pass / - / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 | Pass / - / A 1.000 / F 1.000 |
| Cross-asset regime - SPY Fed and yields impact | Pass / - / A 0.000 / F 0.000 | Pass / - / A 0.000 / F 0.000 | Pass / - / A 0.000 / F 0.000 | Pass / - / A 0.000 / F 0.000 |
| Geopolitical commodity - GLD hedge posture | Fail / material / A 0.875 / F 0.500 | Fail / material / A 1.000 / F 0.500 | Pass / - / A 1.000 / F 0.500 | Pass / - / A 1.000 / F 0.500 |
| Geopolitical commodity - GLD risk backdrop | Pass / - / A 1.000 / F 1.000 | Fail / compliance / A 1.000 / F 1.000 | Fail / compliance / A 1.000 / F 1.000 | Fail / compliance / A 1.000 / F 1.000 |
| Geopolitical commodity - GLD volatility and risk | Pass / - / A 0.750 / F 0.750 | Fail / compliance / A 0.750 / F 0.750 | Fail / compliance / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 |
| Geopolitical commodity - SLV safe-haven posture | Fail / material / A 0.500 / F 0.500 | Fail / material / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 |
| Insider flow - AAPL selling signal | Fail / material / A 1.000 / F 1.000 | Fail / material / A 1.000 / F 1.000 | Fail / material / A 1.000 / F 1.000 | Fail / material / A 1.000 / F 0.875 |
| Insider flow - AMD buying versus vesting | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 |
| Insider flow - NVDA insider signal | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 |
| Insider flow - TSLA default window posture | Pass / - / A 0.875 / F 0.375 | Pass / - / A 0.875 / F 0.375 | Pass / - / A 0.875 / F 0.750 | Pass / - / A 0.875 / F 0.750 |
| Microstructure - GLD volatility screen | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 |
| Microstructure - IWM downside screen | Fail / compliance / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 | Pass / - / A 0.750 / F 0.750 |
| Microstructure - QQQ downside hedge | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 |
| Microstructure - SPY hedge posture | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 |
| Single-name options - AAPL IV skew posture | Pass / - / A 0.500 / F 0.500 | Fail / compliance / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 |
| Single-name options - AMD skew setup | Fail / compliance / A 1.000 / F 0.500 | Pass / - / A 0.750 / F 0.500 | Pass / - / A 0.750 / F 0.500 | Pass / - / A 0.750 / F 0.500 |
| Single-name options - NVDA premium posture | Fail / compliance / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 |
| Single-name options - TSLA downside liquidity | Fail / compliance / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 | Pass / - / A 0.500 / F 0.500 |

## Current Version Assessment (Protected Watchlist Floor + Re-eval)

### Strengths
- Final-answer truth pass is now `0.900`, the strongest in the four-run sequence.
- Final truth integrity is `0.983`, and compliance failure is down to `0.050`.
- Final informational completeness reaches `0.792`, also the best in the sequence.
- The watchlist taxonomy is finally materially corrected:
  - `directional_watchlist`: `11`
  - `informational_only`: `9`
- This is the first run where the workflow looks both safer **and** more product-correct.

### Limitations
- Critic and Finalizer still compress non-actionable answers; the centered non-actionable delta remains negative at `-0.050`.
- Final semantic evidence retention (`0.669`) is slightly below `Watchlist Gate v1` (`0.675`).
- Downgraded answers still sit at a usefulness floor of `0.600`, which is acceptable but not yet rich.
- `Geopolitical commodity - GLD risk backdrop` remains the standing compliance outlier.
- `Insider flow - AAPL selling signal` remains the standing material-failure outlier because the strict SEC signal is still missing at runtime.

## Executive Readout
- Across these 4 iterations, final truth pass improved from `0.550` to `0.900`.
- Final truth integrity improved from `0.917` to `0.983`.
- Compliance failure rate improved from `0.250` to `0.050`.
- The decisive architectural shift was not just "better wording" or "better rendering". It was the move from overusing `informational_only` toward a controlled `directional_watchlist` floor.
- `Watchlist Gate v1` was the strongest pure truth-and-safety jump.
- `Protected Watchlist Floor + Re-eval` is the best overall run because it combines:
  - strongest truth pass,
  - strongest final informational completeness,
  - cleanest compliance profile,
  - and the first materially correct answer-mode split.
- The main remaining weakness is no longer core truth. It is post-downgrade information compression.
