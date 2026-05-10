# Agentic Evaluation Iteration Review
> Latest Update: 2026-05-09
> Version: 3.0
- [Agentic Node Report 3.0](./logs/agentic_eval/2026-05-09/20260509_180947/reeval_node/agentic_node_report.md)
- [Agentic Node Dashboard 3.0](./logs/agentic_eval/2026-05-09/20260509_180947/reeval_node/agentic_node_dashboard.html)
- [Agentic Node Result 3.0](.logs/agentic_eval/2026-05-09/20260509_180947/production_results_full.jsonl)

This document is a current-version review of the four-node workflow in:

- `Analyst`
- `Checker`
- `Critic`
- `Finalizer`

---

## Part 1. Node Capability High Summary

### Overall read

The current workflow is already strong on safety and truth control. The four-node chain is not mainly adding more content; it is adding governance, correction, and mode discipline. The tradeoff is that some semantic richness is still compressed by the time the answer reaches the final user-facing layer.

### Core node matrix

| Node | Contribution / Role Signal | Current Metric | What it means in practice |
| --- | --- | --- | --- |
| Analyst | Initial evidence-backed synthesis | `0.842` contribution | Strong first-pass framing. The Analyst is already very good at financial logic (`0.975`) and slot answering (`0.908`), but still leaks truth / disclosure issues in some hard cases (`0.767` truth integrity). |
| Checker | Correction and factual clean-up | `0.908` contribution | The Checker is the main error-dampener between first draft and governed answer. `0.775` fix adoption is solid, but still leaves room for stronger downstream absorption of corrections. |
| Critic | Governance, downgrade, and boundary control | `0.960` contribution | This is the strongest node in the current stack. Downgrade success is `1.000`, risk-disclosure success is `1.000`, and the workflow now cleanly prevents live-action drift in non-actionable cases. |
| Finalizer | Final rendering and disclosure integrity | `0.849` contribution | The Finalizer is very strong on final truth (`0.983`) and logic (`1.000`). Its main limitation is not correctness, but compression: semantic evidence retention lands at `0.669`, below the Analyst draft. |

### What the matrix says about current node strengths

- **Analyst** is already capable of producing a financially coherent first read. The main issue is not raw logic quality; it is unsupported inference around missing SEC evidence, disclosure gaps, and occasional illustrative leakage.
- **Checker** is the stabilizer for factual discipline. It does not need to be perfect to be useful; even at `0.775` adoption, it materially reduces downstream drift.
- **Critic** is the key reason the system now behaves like a governed financial assistant instead of an eager structure generator. It protects the boundary between `directional_watchlist` and real options action.
- **Finalizer** is where the workflow becomes production-usable. It consistently preserves truth and disclosure rules, but it still trims some analyst-level texture in order to stay safe.

### Headline current-version metrics

| Metric | Value |
| --- | --- |
| Final truth pass rate | `0.900` |
| Final truth integrity rate | `0.983` |
| Final informational completeness avg | `0.792` |
| Critical failure case rate | `0.000` |
| Material failure case rate | `0.050` |
| Compliance failure case rate | `0.050` |
| Downgraded answer usefulness floor avg | `0.600` |

Bottom line: the current system is already strong at staying financially safe and fact-grounded. The main remaining bottleneck is not logic or hallucination control; it is preserving enough semantic richness after the safety layers intervene.

---

## Part 2. Four Common Dimensions

The most useful way to read the current workflow is through the four common dimensions used in the node evaluation lens:

1. financial logic continuity
2. hallucination control
3. slot coverage
4. semantic evidence retention

### Dimension matrix

| Dimension | Analyst | Finalizer | Delta | Supporting outcome metric | Read |
| --- | --- | --- | --- | --- | --- |
| Financial logic continuity | `0.975` | `1.000` | `+0.025` | Critical failure rate `0.000` | Logic is already strong at draft time and becomes effectively clean by the final answer. |
| Hallucination control | `0.767` truth integrity | `0.983` truth integrity | `+0.216` | Material failure rate `0.050`; disclosure honesty `0.950` | This is the biggest quality gain across the chain. |
| Slot coverage | `0.908` | `0.883` | `-0.025` | Query-first usefulness floor `0.600` | The workflow gives up a small amount of direct slot coverage to preserve safer boundaries. |
| Semantic evidence retention | `0.719` | `0.669` | `-0.050` | Analyst -> Final information gain `-0.108`; Critic non-actionable info delta `-0.050` | This is the current main weakness: safety is achieved partly by compressing the answer. |

### 1. Financial logic continuity

This dimension is already in very good shape.

- Analyst logic integrity is `0.975`
- Finalizer logic integrity is `1.000`
- Analyst -> Final logic gain is `+0.025`
- Critical failure rate is `0.000`

Interpretation:

- the workflow is not suffering from systemic financial-reasoning collapse,
- the Analyst is already mostly coherent on regime logic,
- the extra nodes are mainly polishing edge cases rather than rescuing a broken first draft.

This is important because it means the chain is not spending most of its effort fixing fundamental reasoning mistakes. It is spending its effort on governance and truth discipline.

### 2. Hallucination control

This is where the four-node design helps the most.

- Analyst truth integrity is `0.767`
- Finalizer truth integrity is `0.983`
- Analyst -> Final truth gain is `+0.217`
- Material failure rate is only `0.050`
- Final disclosure honesty is `0.950`

Metric definition:

- `truth integrity` is the mean of three binary checks in `agentic_node_eval.py`:
  - `unsupported_fact_pass`
  - `missing_info_honesty_pass`
  - `financial_common_sense_pass`
- So a value of `0.983` means the final answers passed almost all of those fact-boundary and honest-missing-information checks across the case set.
- `final disclosure honesty` is the aggregated `slot_disclosure_honesty_rate` coming from the slot-evidence contract layer. At the slot level, the evaluator checks whether a slot that cannot be answered from retrieval is explicitly disclosed rather than silently improvised. The final metric is the mean rate of honest slot-level disclosure across cases.

Interpretation:

- unsupported statements, missing-source disclosure gaps, and overconfident wording are still visible in some Analyst drafts,
- by the time the answer reaches Finalizer, those issues are mostly removed,
- the stack is therefore much better at suppressing hallucination than a single-pass answerer would be.

The strongest evidence is that truth improves much more than logic. That means the architecture is not merely making the answer smoother; it is materially cleaning the fact boundary.

### 3. Slot coverage

Slot coverage is strong, but it is not monotonic across the chain.

- Analyst slot answer rate is `0.908`
- Finalizer slot answer rate is `0.883`
- Analyst -> Final intent gain is `-0.025`

Interpretation:

- the Analyst is very willing to answer the requested slots,
- the governed workflow keeps most of that coverage,
- but some direct slot answering is deliberately sacrificed when the later nodes decide that a safer downgrade or narrower framing is required.

This is a healthy tradeoff, not a pure regression. In financial RAG, slight loss of slot-directness is acceptable if it prevents unsupported structure-level output.

### 4. Semantic evidence retention

This is the current version's main performance gap.

- Analyst semantic evidence carry is `0.719`
- Finalizer semantic evidence retention is `0.669`
- Analyst -> Final information gain is `-0.108`
- Critic non-actionable information retention delta is `-0.050`
- Downgraded answer usefulness floor is `0.600`

Metric definition:

- `semantic evidence carry` is the evaluator's `semantic_evidence_carry_score`.
- It is computed from slot-level evidence contracts: for each required query slot, the evaluator checks whether the answer preserves enough semantically relevant evidence tokens or evidence groups to support that slot.
- The slot-level scores are then averaged:
  - `1.0` means strong evidence carry for the relevant slots,
  - `0.5` means partial carry,
  - `0.0` means the required semantic support is effectively absent.
- In this document, Analyst semantic evidence carry (`0.719`) is the average of the Analyst draft's slot-level evidence support, while Finalizer semantic evidence retention (`0.669`) is the same contract applied to the final answer after downstream governance and rendering.

Interpretation:

- the workflow is good at keeping the answer safe,
- but some analyst-level semantic texture is still lost downstream,
- the compression is mild rather than catastrophic, but it is still the largest remaining quality cost in the system.

This is exactly why the current version feels strong on truth and mode governance, yet sometimes only moderate on richness after downgrade.

---

## Part 3. Four-Node Evolution, Delta, and Query Pattern Analysis

### Why the four-node workflow is better than a single-pass answer

The cleanest summary is:

- the Analyst gives the system speed and financial framing,
- the Checker removes unsupported or under-disclosed statements,
- the Critic enforces actionability and risk boundaries,
- the Finalizer turns the governed state into a clean user answer.

The aggregate delta across the workflow is:

| Analyst -> Final aggregate delta | Value |
| --- | --- |
| Logic gain | `+0.025` |
| Truth gain | `+0.217` |
| Information gain | `-0.108` |
| Intent / slot gain | `-0.025` |

Interpretation:

- the four-node chain clearly improves truth,
- it slightly improves logic,
- it slightly reduces direct slot aggressiveness,
- and it pays for that safety with some information compression.

That is still a good trade. In a financial product, this is the correct direction of bias: better to lose some richness than to preserve unsupported actionability.

### Node-by-node evolution inside the current workflow

#### Analyst -> Checker

Main effect:

- factual clean-up and disclosure correction

Key metric:

- Checker fix adoption `0.775`

Interpretation:

- the Checker is not decorative,
- it is responsible for a meaningful part of the truth uplift,
- but it is not yet a perfect guarantee that every issue found upstream will be fully absorbed downstream.

#### Checker -> Critic

Main effect:

- actionability control and safe downgrade

Key metrics:

- Critic feedback adoption `1.000`
- Critic downgrade success `1.000`
- Critic risk disclosure success `1.000`

Interpretation:

- this is the architectural center of gravity in the current system,
- the Critic is the node that turns "financially plausible" into "product-safe and policy-safe",
- it is also the main reason the workflow now handles `directional_watchlist` much better than earlier versions.

#### Critic -> Finalizer

Main effect:

- governed answer rendering

Key metrics:

- Finalizer truth integrity `0.983`
- Finalizer informational completeness `0.792`
- Finalizer illustrative structure rate `0.429`
- Finalizer semantic evidence retention `0.669`

Interpretation:

- the Finalizer is not the source of the governance policy, but it is the layer that operationalizes it consistently,
- it succeeds on truth and disclosure,
- it still leaves some room to improve semantic richness and useful illustrative framing under non-actionable modes.

### Query families where the current model performs better

The current workflow performs best on queries where:

- the strict source is present,
- the answer can stay at `directional_watchlist` rather than forcing live structure,
- Silver options evidence is sufficient to anchor a regime or posture read.

Strong query patterns:

- **Microstructure watchlist-style queries**
  - `Microstructure - SPY hedge posture`
  - `Microstructure - QQQ downside hedge`
  - `Microstructure - IWM downside screen`
  - `Microstructure - GLD volatility screen`
- **Single-name options posture queries**
  - `Single-name options - AAPL IV skew posture`
  - `Single-name options - NVDA premium posture`
  - `Single-name options - AMD skew setup`
  - `Single-name options - TSLA downside liquidity`
- **Geopolitical hedge posture queries**
  - `Geopolitical commodity - GLD hedge posture`
  - `Geopolitical commodity - SLV safe-haven posture`
  - `Geopolitical commodity - GLD volatility and risk`
- **Cross-asset regime reads**
  - `Cross-asset regime - QQQ IV versus VIX`
  - `Cross-asset regime - IWM small-cap stress`

Why these are stronger:

- they fit the current product ceiling well,
- they benefit from strong Silver evidence,
- and the `directional_watchlist` mode gives the workflow enough room to remain useful without overcommitting.

### Query patterns where the current model is weaker

The weakest cases share one of two profiles:

1. the query requires a strict source that is missing at runtime
2. the answer sits on a boundary between `informational_only` and `directional_watchlist`

Main weaker cases:

- **Insider flow - AAPL selling signal**
  - this remains the main material-failure outlier,
  - `sec` is missing,
  - the required `sec_insider_signal` slot is missing,
  - the workflow still struggles to stay fully explicit and clean when the user asks for a concrete insider-selling read but the strict SEC evidence is absent.
  - more specifically, this is not only a generic "missing SEC" problem. The ontology defines insider-flow coverage with an explicit taxonomy:
    - `SELL` = open-market or planned disposition / selling activity
    - `BUY` = open-market purchase / affirmative buying activity
    - `ACQUIRE/VEST` = compensation-related vesting or award-linked acquisition, and is **not** equivalent to open-market buying or selling
  - the query slot itself is therefore defined as `Form-4 insider selling / buying / vesting signal`, not a collapsed single insider label. In the AAPL case, the coverage problem comes from the workflow not having strict SEC support for the specific insider-action category the user asked about, while the evaluator also refuses to count `ACQUIRE/VEST` as interchangeable with `SELL`.
- **Geopolitical commodity - GLD risk backdrop**
  - this remains the main compliance outlier,
  - the node report explicitly flags that an `informational_only` answer carried a market-read posture that belongs in `directional_watchlist`,
  - this is still the best standing regression test for mode-boundary correctness.
- **Cross-asset regime - SPY Fed and yields impact**
  - the case passes, but it is thinner than the stronger watchlist cases,
  - the draft shows more macro narration and less slot-specific evidence density,
  - it is a useful reminder that passing is not the same as being maximally information-rich.

### Final read on current-version quality

The current workflow is already strong enough to support a low-barrier financial intelligence product:

- logic is stable,
- hallucination control is strong,
- slot coverage remains high,
- the watchlist boundary is much better governed than earlier versions.

The remaining improvement target is now narrow and clear:

- preserve more semantic richness after Critic and Finalizer intervention,
- especially in downgraded answers,
- without reopening the door to unsupported actionability.
