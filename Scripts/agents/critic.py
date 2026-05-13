"""
Scripts/agents/critic.py

Critic Agent (Red Team) — adversarial logic / regime / insider-signal review.

In the new topology, the router calls `checker_node` BEFORE `critic_node`, so
this agent no longer needs to invoke the Checker internally. Critic's job is
now narrowly scoped: challenge the STRATEGY LOGIC of an already fact-checked
draft.

Review axes (in strict priority order):
    1. IV Regime Fit       — long premium in HIGH IV / short in LOW IV = Fatal.
    2. Macro Contradiction — directional bias contradicting [MACRO ENVIRONMENT].
    3. Insider Weakness    — single-exec signal being treated as conviction.
    4. Risk/Reward Balance — expensive protection where a spread would suffice.

Deterministic backstops (no-LLM):
    - If draft recommends bullish calls but the Insider Confidence Index is
      BEARISH_SIGNAL (3+ execs selling) → Fatal (always raised).
    - If draft recommends bearish puts but the Insider Confidence Index is
      BULLISH_SIGNAL → Fatal (always raised).

State I/O:
  reads : state["draft_report"], state["silver_context"], state["gold_context"],
          state["macro_context"], state["original_query"], state["revision_count"]
  writes: {
      "critic_feedback": List[AgentFeedback] (append-only; sender="Critic"),
      "critic_verdict": "pass" | "fatal" | "minor",
  }
"""

from __future__ import annotations

import os
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from Scripts.agents.state import AgentFeedback, FinalizerEdit
from Scripts.agents.analyst import _infer_iv_regime, _format_silver, _format_gold
from Scripts.agents.prompts import get_critic_prompt
from Scripts.core.financial_reasoning_contract import (
    build_data_capability_profile,
    render_data_capability_profile,
    render_reasoning_contract,
)
from Scripts.core.liquidity_policy import (
    classify_market_impact_risk,
    resolve_primary_ticker,
    resolve_ticker_metric_bundle,
)
from Scripts.core.posture_contract import (
    derive_posture_contract,
    derive_posture_market_risk,
    render_posture_contract_block,
)
from Scripts.retrieval.schema import render_retrieval_outcome_block, render_scope_contract_block

logger = logging.getLogger(__name__)

_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))
_INSIDER_SIGNAL_MIN_COUNT = int(os.getenv("INSIDER_SIGNAL_MIN_COUNT", "3"))
_UNCERTAINTY_TOKENS = (
    "might", "could", "may", "possible", "possibly", "assume", "assumes",
    "without considering", "uncertain", "not clear",
)


# ==========================================
# Structured LLM output
# ==========================================

class LogicIssue(BaseModel):
    severity: Literal["Fatal", "Minor"] = Field(
        description=(
            "Fatal: strategy MUST be rewritten (direction contradicts regime/macro/insider signal). "
            "Minor: strategy is acceptable but could be polished — do NOT set is_passed=False for Minor only."
        )
    )
    category: Literal[
        "iv_regime_fit",
        "strategy_family_fit",
        "catalyst_horizon_fit",
        "evidence_sufficiency_and_abstention",
        "macro_contradiction",
        "insider_signal_weakness",
        "risk_reward_imbalance",
        "other",
    ] = Field(description="Which logic axis fails")
    comment: str = Field(description="Concrete pushback with a suggested correction (≤ 40 words)")


class CriticResult(BaseModel):
    is_passed: bool = Field(
        description=(
            "True iff there are NO Fatal issues. "
            "Minor-only findings MUST still set is_passed=True — they go to minor_suggestions. "
            "Only set is_passed=False when at least one Fatal issue exists."
        )
    )
    issues: List[LogicIssue] = Field(
        default_factory=list,
        description=(
            "Fatal-severity issues only. "
            "If is_passed=True this list MUST be empty. "
            "Minor-severity findings go into minor_suggestions instead."
        )
    )
    minor_suggestions: List[str] = Field(
        default_factory=list,
        description=(
            "Polish notes for the Finalizer — improvements that do NOT block approval. "
            "Each entry ≤ 30 words. Examples: tighten spread width, add hedge note, adjust DTE."
        )
    )
    recommendation_mode: Literal["actionable_options", "directional_watchlist", "informational_only"] = Field(
        default="directional_watchlist",
        description=(
            "Final output mode after logic review. "
            "actionable_options = concrete options structure is supportable; "
            "directional_watchlist = direction/watchlist is supportable but not strike-level action; "
            "informational_only = only context and caveats should be returned."
        )
    )


# ==========================================
# Insider Confidence Index (deterministic, no LLM)
# ==========================================

def _compute_insider_confidence(gold_context: List[Any]) -> Dict[str, Any]:
    """Aggregate SEC-sourced Form-4 activity into a directional verdict.

    Single-exec transactions (tax, vesting) are noise. Signal requires
    _INSIDER_SIGNAL_MIN_COUNT aligned executives.
    """
    bullish = 0
    bearish = 0
    tone_scores: List[float] = []

    for chunk in gold_context or []:
        meta = getattr(chunk, "metadata", None) or (chunk.get("metadata", {}) if isinstance(chunk, dict) else {})
        src = getattr(chunk, "source_type", None) or (chunk.get("source_type", "") if isinstance(chunk, dict) else "")
        if str(src).lower() != "sec":
            continue

        action = str(meta.get("action_direction", "")).upper() if isinstance(meta, dict) else ""
        if action == "BUY":
            bullish += 1
        elif action == "SELL":
            bearish += 1

        tone = meta.get("tone_score") if isinstance(meta, dict) else None
        if isinstance(tone, (int, float)):
            tone_scores.append(float(tone))

    avg_tone = sum(tone_scores) / len(tone_scores) if tone_scores else None

    if bullish >= _INSIDER_SIGNAL_MIN_COUNT and bullish > bearish:
        verdict = "BULLISH_SIGNAL"
    elif bearish >= _INSIDER_SIGNAL_MIN_COUNT and bearish > bullish:
        verdict = "BEARISH_SIGNAL"
    else:
        verdict = "NOISE"

    return {
        "bullish_exec_count": bullish,
        "bearish_exec_count": bearish,
        "avg_tone_score": avg_tone,
        "verdict": verdict,
        "threshold": _INSIDER_SIGNAL_MIN_COUNT,
    }


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_iso_date(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    if len(text) >= 10:
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _extract_strategy_profile(draft: str, anchor_date: datetime | None) -> Dict[str, Any]:
    text = (draft or "").lower()

    def has_any(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    if has_any("iron condor", "credit spread", "short strangle", "short straddle", "sell premium"):
        family = "short_premium"
    elif has_any("call spread", "put spread", "debit spread", "bull call spread", "bear put spread"):
        family = "defined_risk_directional"
    elif has_any("long straddle", "long strangle"):
        family = "long_vol_event"
    elif has_any("long call", "buy call", "long put", "buy put", "protective put"):
        family = "naked_long_premium"
    else:
        family = "unknown"

    dte_days: int | None = None
    if match := re.search(r"(\d{1,3})\s*(?:-|to)\s*(\d{1,3})\s*dte", text):
        dte_days = round((int(match.group(1)) + int(match.group(2))) / 2)
    elif match := re.search(r"(\d{1,3})\s*dte", text):
        dte_days = int(match.group(1))
    elif match := re.search(r"(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*week", text):
        dte_days = round(((int(match.group(1)) + int(match.group(2))) / 2) * 7)
    elif match := re.search(r"(\d{1,2})\s*week", text):
        dte_days = int(match.group(1)) * 7
    elif match := re.search(r"(\d{4}-\d{2}-\d{2})", text):
        expiry_dt = _parse_iso_date(match.group(1))
        if expiry_dt and anchor_date:
            dte_days = max((expiry_dt.date() - anchor_date.date()).days, 0)

    return {
        "family": family,
        "is_concrete_options_recommendation": family != "unknown",
        "is_naked_long_premium": family == "naked_long_premium",
        "is_long_premium": family in {"naked_long_premium", "long_vol_event", "defined_risk_directional"},
        "is_short_premium": family == "short_premium",
        "is_defined_risk": family == "defined_risk_directional",
        "mentions_spread": "spread" in text,
        "dte_days": dte_days,
    }


def _gold_recency_profile(gold_context: List[Any], anchor_date: datetime | None) -> Dict[str, Any]:
    newest_age_days: int | None = None
    oldest_age_days: int | None = None

    for chunk in gold_context or []:
        meta = _obj_get(chunk, "metadata", {}) or {}
        record_dt = (
            _parse_iso_date(meta.get("record_date"))
            or _parse_iso_date(meta.get("publish_date"))
            or _parse_iso_date(meta.get("publish_timestamp"))
            or _parse_iso_date(meta.get("transaction_date"))
            or _parse_iso_date(meta.get("filed_at"))
        )
        if record_dt is None or anchor_date is None:
            continue
        age = max((anchor_date.date() - record_dt.date()).days, 0)
        newest_age_days = age if newest_age_days is None else min(newest_age_days, age)
        oldest_age_days = age if oldest_age_days is None else max(oldest_age_days, age)

    return {
        "newest_gold_age_days": newest_age_days,
        "oldest_gold_age_days": oldest_age_days,
    }


def _catalyst_profile(
    draft: str,
    metadata: Any,
    gold_context: List[Any],
    time_range: Dict[str, Any],
    anchor_date: datetime | None,
) -> Dict[str, Any]:
    draft_l = (draft or "").lower()
    event_keyword = str(_obj_get(metadata, "event_keyword", "") or "").lower()
    time_window = str(_obj_get(metadata, "time_window", "") or (time_range or {}).get("time_window_label", ""))
    recency = _gold_recency_profile(gold_context, anchor_date)

    catalyst_keywords = (
        "earnings", "cpi", "ppi", "fomc", "fed", "minutes",
        "payroll", "nfp", "guidance", "auction", "meeting",
    )
    has_event_language = bool(event_keyword) or any(k in draft_l for k in catalyst_keywords)
    if not has_event_language:
        for chunk in gold_context or []:
            text = str(_obj_get(chunk, "content", "") or "").lower()
            if any(k in text for k in catalyst_keywords):
                has_event_language = True
                break

    newest_age_days = recency.get("newest_gold_age_days")
    recency_threshold = 14 if time_window in {"today", "past_week"} else 30
    has_near_term_catalyst = bool(
        has_event_language and (newest_age_days is None or newest_age_days <= recency_threshold)
    )
    stale_event_evidence = bool(
        has_event_language and newest_age_days is not None and newest_age_days > 30
    )

    return {
        "time_window": time_window,
        "event_keyword": event_keyword,
        "has_event_language": has_event_language,
        "has_near_term_catalyst": has_near_term_catalyst,
        "stale_event_evidence": stale_event_evidence,
        **recency,
    }


def _render_time_range_block(time_range: Dict[str, Any], metadata: Any) -> str:
    tr = time_range or {}
    requested = _obj_get(metadata, "time_window", "") or tr.get("time_window_label", "")
    lines = [
        f"requested_time_window={requested or '(unknown)'}",
        f"window_days={tr.get('window_days')}",
        f"anchor_date={tr.get('anchor_date')}",
        f"start_date={tr.get('start_date')}",
        f"end_date={tr.get('end_date')}",
    ]
    return "\n".join(lines)


def _dedupe_suggestions(items: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for raw in items:
        item = (raw or "").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def _must_keep_keys_for_reply(metadata: Any, silver_ctx: Dict[str, Any]) -> List[str]:
    values = (silver_ctx or {}).get("values") or {}
    metrics = [str(m).lower() for m in (_obj_get(metadata, "metrics", []) or [])]
    required: List[str] = []
    metric_map = {
        "pcr_volume": ("put/call ratio", "pcr"),
        "latest_atm_iv": ("implied volatility", "iv", "atm iv"),
        "gpr_index_level": ("gpr", "geopolitical risk"),
        "vix_value": ("options pricing / spread", "macro trend", "implied volatility"),
    }
    for key, needles in metric_map.items():
        if key not in values:
            continue
        if any(any(needle in metric for needle in needles) for metric in metrics):
            required.append(key)
    if "latest_atm_iv" in values and "latest_atm_iv" not in required and any("iv" in m for m in metrics):
        required.append("latest_atm_iv")
    return required


def _make_critic_edit(
    *,
    edit_type: str,
    target_section: str,
    instruction: str,
    must_keep_keys: List[str] | None = None,
) -> FinalizerEdit:
    return {
        "source": "Critic",
        "edit_type": edit_type,
        "target_section": target_section,
        "instruction": instruction,
        "must_keep_keys": list(must_keep_keys or []),
        "must_keep_anchor_ids": [],
    }


def _dedupe_edits(items: List[FinalizerEdit]) -> List[FinalizerEdit]:
    seen: set[tuple[str, str, str]] = set()
    ordered: List[FinalizerEdit] = []
    for item in items:
        sig = (
            str(item.get("edit_type")),
            str(item.get("target_section")),
            str(item.get("instruction")),
        )
        if sig in seen:
            continue
        seen.add(sig)
        ordered.append(item)
    return ordered


def _market_impact_risk_value(
    *,
    silver_ctx: Dict[str, Any],
    metadata: Any,
    scope_contract: Dict[str, Any],
) -> str:
    values = (silver_ctx or {}).get("values") or {}
    primary_ticker = resolve_primary_ticker(
        metadata=metadata,
        scope_contract=scope_contract,
    )
    bundle = resolve_ticker_metric_bundle(
        values,
        primary_ticker,
        ["avg_spread_pct", "liquid_contracts"],
    )
    return classify_market_impact_risk(
        primary_ticker,
        bundle.get("avg_spread_pct"),
        bundle.get("liquid_contracts"),
    )


def _answerability_contract(scope_contract: Dict[str, Any]) -> Dict[str, Any]:
    scope = scope_contract if isinstance(scope_contract, dict) else {}
    return {
        "analysis_mode": str(scope.get("analysis_mode") or "default_read").strip() or "default_read",
        "requires_catalyst_confirmation": bool(scope.get("requires_catalyst_confirmation", True)),
        "gold_context_optional": bool(scope.get("gold_context_optional", False)),
        "hard_data_sufficient_for_answer": bool(scope.get("hard_data_sufficient_for_answer", False)),
        "market_analysis_only": bool(scope.get("market_analysis_only", False)),
    }


def _extract_structure_hint(draft: str, strategy_profile: Dict[str, Any]) -> str:
    text = draft or ""
    normalized = text.lower()
    patterns = [
        "bull put spread",
        "bear put spread",
        "bull call spread",
        "bear call spread",
        "call spread",
        "put spread",
        "debit spread",
        "credit spread",
        "long straddle",
        "long strangle",
        "long call",
        "long put",
        "protective put",
        "iron condor",
    ]
    label = ""
    for pattern in patterns:
        if pattern in normalized:
            label = pattern.title()
            break
    if not label:
        family = str(strategy_profile.get("family") or "")
        family_map = {
            "defined_risk_directional": "defined-risk spread",
            "long_vol_event": "long-volatility structure",
            "naked_long_premium": "long premium structure",
            "short_premium": "premium-selling structure",
        }
        label = family_map.get(family, "")
    strike_match = re.findall(r"\b\d+(?:\.\d+)?[CP]\b", text, flags=re.IGNORECASE)
    dte_match = re.search(r"\b\d{1,3}\s*(?:-|to\s*)?\d{0,3}\s*DTE\b", text, flags=re.IGNORECASE)
    pieces: List[str] = []
    if label:
        pieces.append(label)
    if strike_match:
        pieces.append(f"using {' / '.join(strike_match[:2])}")
    if dte_match:
        pieces.append(dte_match.group(0))
    return " ".join(pieces).strip()


def _structure_visibility_mode(
    strategy_profile: Dict[str, Any],
    recommendation_mode: str,
) -> str:
    if recommendation_mode == "actionable_options":
        return "recommended_structure"
    if strategy_profile.get("is_concrete_options_recommendation"):
        return "illustrative_structure"
    if recommendation_mode == "directional_watchlist":
        return "illustrative_structure"
    return "no_structure"


def _build_revision_constraints(
    *,
    recommendation_mode: str,
    strategy_profile: Dict[str, Any],
    silver_ctx: Dict[str, Any],
    retrieval_outcome: Dict[str, Any],
    data_capability_profile: Dict[str, Any],
    draft: str,
    metadata: Any,
    scope_contract: Dict[str, Any],
    iv_regime_pinned: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    answerability = _answerability_contract(scope_contract)
    values = dict((silver_ctx or {}).get("values") or {})
    market_impact_risk = _market_impact_risk_value(
        silver_ctx=silver_ctx,
        metadata=metadata,
        scope_contract=scope_contract,
    )
    high_risk = market_impact_risk.lower() == "high"
    structure_visibility_mode = _structure_visibility_mode(strategy_profile, recommendation_mode)
    missing_sources = [str(x) for x in (retrieval_outcome.get("missing_strict_sources") or []) if str(x).strip()]
    missing_slots = [str(x) for x in (retrieval_outcome.get("missing_query_slots") or []) if str(x).strip()]
    is_missing_hard_data = bool(missing_sources or missing_slots)
    market_read_only = recommendation_mode in {"informational_only", "directional_watchlist"} and not is_missing_hard_data
    if answerability["market_analysis_only"] and not is_missing_hard_data:
        market_read_only = True
    if (
        answerability["analysis_mode"] == "data_backed_read"
        and answerability["hard_data_sufficient_for_answer"]
        and recommendation_mode != "actionable_options"
    ):
        market_read_only = True
    illustrative_hint = _extract_structure_hint(draft, strategy_profile)
    has_core_options_posture_evidence = bool(
        (silver_ctx.get("values") or {}).get("latest_atm_iv") is not None
        and (
            (silver_ctx.get("values") or {}).get("pcr_volume") is not None
            or (silver_ctx.get("values") or {}).get("pcr_open_interest") is not None
        )
        and (
            (silver_ctx.get("values") or {}).get("liquid_contracts") is not None
            or (silver_ctx.get("values") or {}).get("executable_option_volume") is not None
            or (silver_ctx.get("values") or {}).get("executable_open_interest") is not None
        )
    )
    soft_coverage_gap = bool(
        answerability["market_analysis_only"]
        and has_core_options_posture_evidence
        and set(missing_slots).issubset({"iv_skew_signal"})
        and bool(missing_slots)
    )
    hard_gap = bool(is_missing_hard_data and not soft_coverage_gap)
    if soft_coverage_gap:
        evidence_coverage_note = (
            "Specific IV skew metric was not retrieved; posture is based on ATM IV, PCR, and liquidity."
        )
        evidence_coverage_severity = "soft_note"
    elif hard_gap:
        evidence_coverage_note = ""
        evidence_coverage_severity = "hard_gap"
    else:
        evidence_coverage_note = ""
        evidence_coverage_severity = "none"
    read_valid_despite_coverage_gap = not hard_gap
    if recommendation_mode == "actionable_options":
        mode_boundary_text = (
            "The current evidence is strong enough to discuss a concrete options structure, "
            "subject to the stated risk controls."
        )
    elif recommendation_mode == "directional_watchlist":
        mode_boundary_text = (
            "The current evidence supports a directional/watchlist read, not a strike-level options action."
        )
    elif market_read_only:
        mode_boundary_text = (
            "The current answer is a read-only market posture, not a live options escalation."
        )
    else:
        mode_boundary_text = (
            "The current evidence supports context and boundaries, but not a concrete options setup."
        )
    posture_contract = derive_posture_contract(
        values,
        iv_regime_pinned,
        scope_contract,
        recommendation_mode,
        metadata=metadata,
    )
    posture_market_risk = derive_posture_market_risk(
        values,
        iv_regime_pinned,
        posture_contract,
        scope_contract,
        metadata=metadata,
    )
    if high_risk:
        true_risk_text = f"Market impact risk is {market_impact_risk}."
    elif posture_market_risk:
        true_risk_text = posture_market_risk
    else:
        true_risk_text = "The current view should be revisited if fresher evidence materially changes the setup."
    if recommendation_mode == "actionable_options":
        why_not_now_text = ""
    elif hard_gap and answerability["market_analysis_only"]:
        why_not_now_text = "Some requested evidence was not retrieved cleanly enough for a full posture read."
    elif hard_gap:
        why_not_now_text = "Data insufficient for live analysis."
    elif market_read_only:
        why_not_now_text = ""
    elif high_risk:
        why_not_now_text = "Execution conditions are too weak for a live structure because market impact risk is High."
    elif missing_slots:
        why_not_now_text = "Some requested evidence is missing, so the setup cannot be promoted into a live structure yet."
    elif not data_capability_profile.get("can_support_concrete_option_structure"):
        why_not_now_text = "Current project data does not support a live strike-level options structure yet."
    else:
        why_not_now_text = "The current evidence supports context or direction, but not a live options structure yet."
    if recommendation_mode == "actionable_options":
        what_must_change = ""
    elif is_missing_hard_data and answerability["market_analysis_only"]:
        what_must_change = "Recover the missing evidence before upgrading this posture read."
    elif is_missing_hard_data:
        what_must_change = "Recover the missing source or query-slot evidence before promoting a live structure."
    elif market_read_only:
        what_must_change = ""
    elif high_risk:
        what_must_change = "Execution risk would need to improve materially before a live structure becomes appropriate."
    elif not data_capability_profile.get("can_support_concrete_option_structure"):
        what_must_change = "Stronger Silver options support or fresher confirming evidence would be needed before promoting a live structure."
    else:
        what_must_change = "Fresher or stronger confirming evidence would be needed before promoting a live structure."
    allow_illustrative_structure = (
        structure_visibility_mode == "illustrative_structure" and not is_missing_hard_data
    )
    if allow_illustrative_structure and illustrative_hint:
        if recommendation_mode == "directional_watchlist":
            illustrative_structure_text = (
                f"Illustrative only: {illustrative_hint} is the cleaner template to watch if conditions improve, "
                "but it is not a live recommendation."
            )
        else:
            illustrative_structure_text = (
                f"Illustrative only: {illustrative_hint} is a non-actionable example only, "
                "not a current or live recommendation."
            )
    else:
        illustrative_structure_text = ""
    must_explain_why_not_now = hard_gap
    if answerability["market_analysis_only"] and not is_missing_hard_data:
        must_explain_why_not_now = False
    if hard_gap and answerability["market_analysis_only"]:
        answer_status_note = (
            "This answer remains a constrained read because some required evidence is still missing in the current run."
        )
        answer_status_severity = "hard_boundary"
    elif hard_gap:
        answer_status_note = (
            "This answer is materially constrained because required evidence is missing in the current run."
        )
        answer_status_severity = "hard_boundary"
    elif soft_coverage_gap:
        answer_status_note = evidence_coverage_note
        answer_status_severity = "soft_note"
    else:
        answer_status_note = ""
        answer_status_severity = "none"
    return {
        "actionability_mode": recommendation_mode,
        "structure_visibility_mode": structure_visibility_mode,
        "forbid_actionable_recommendation": recommendation_mode != "actionable_options",
        "allow_illustrative_structure": allow_illustrative_structure,
        "must_explain_why_not_now": must_explain_why_not_now,
        "market_read_only": market_read_only,
        "must_disclose_risk": high_risk or hard_gap or bool(answerability["market_analysis_only"] and read_valid_despite_coverage_gap),
        "must_disclose_missing_slots": hard_gap,
        "main_risk_text": true_risk_text,
        "true_risk_text": true_risk_text,
        "evidence_coverage_note": evidence_coverage_note,
        "evidence_coverage_severity": evidence_coverage_severity,
        "read_valid_despite_coverage_gap": read_valid_despite_coverage_gap,
        "answer_status_note": answer_status_note,
        "answer_status_severity": answer_status_severity,
        "missing_metric_note": evidence_coverage_note,
        "missing_metric_severity": evidence_coverage_severity,
        "read_valid_despite_missing_metric": soft_coverage_gap,
        "mode_boundary_text": mode_boundary_text,
        "why_not_now_text": why_not_now_text,
        "what_must_change": what_must_change,
        "illustrative_structure_hint": illustrative_hint,
        "illustrative_structure_text": illustrative_structure_text,
        "section_ownership": {
            "direct_conclusion": "evidence_only",
            "asset_read": "evidence_recap_only",
            "recommendation_mode": "boundary_only",
            "risks": "true_risks_only",
        },
        "high_risk": high_risk,
        "market_impact_risk": market_impact_risk,
        "missing_hard_data": is_missing_hard_data,
        "analysis_mode": answerability["analysis_mode"],
        "requires_catalyst_confirmation": answerability["requires_catalyst_confirmation"],
        "gold_context_optional": answerability["gold_context_optional"],
        "hard_data_sufficient_for_answer": answerability["hard_data_sufficient_for_answer"],
        "market_analysis_only": answerability["market_analysis_only"],
        "posture_label": posture_contract.get("posture_label", ""),
        "posture_takeaway": posture_contract.get("posture_takeaway", ""),
        "posture_rationale": posture_contract.get("posture_rationale", ""),
        "base_regime_read": posture_contract.get("base_regime_read", ""),
        "escalation_risk_archetype": posture_contract.get("escalation_risk_archetype", ""),
        "escalation_risk_read": posture_contract.get("escalation_risk_read", ""),
        "posture_reasoning_trace": dict(posture_contract.get("posture_reasoning_trace") or {}),
    }


_MODE_ORDER = {
    "actionable_options": 2,
    "directional_watchlist": 1,
    "informational_only": 0,
}


def _more_conservative_mode(
    left: str,
    right: str,
) -> str:
    lmode = left if left in _MODE_ORDER else "directional_watchlist"
    rmode = right if right in _MODE_ORDER else "directional_watchlist"
    return lmode if _MODE_ORDER[lmode] <= _MODE_ORDER[rmode] else rmode


def _has_fatal_feedback(feedbacks: List[AgentFeedback]) -> bool:
    return any((fb.error_type or "").lower() == "fatal" for fb in feedbacks)


def _has_options_evidence(
    retrieval_outcome: Dict[str, Any],
    data_capability_profile: Dict[str, Any],
) -> bool:
    strict_hits = {
        str(src).strip().lower()
        for src in (retrieval_outcome.get("strict_sources_hit") or [])
        if str(src).strip()
    }
    if "options" in strict_hits:
        return True
    return bool(
        data_capability_profile.get("has_options_chain_support")
        or (
            data_capability_profile.get("has_options_source")
            and (
                data_capability_profile.get("has_iv_signal")
                or data_capability_profile.get("has_liquidity_signal")
                or data_capability_profile.get("has_strike_support")
                or data_capability_profile.get("has_dte_support")
            )
        )
    )


def _recommendation_gate_context(
    *,
    retrieval_outcome: Dict[str, Any],
    data_capability_profile: Dict[str, Any],
    scope_contract: Dict[str, Any],
    feedbacks: List[AgentFeedback],
) -> Dict[str, Any]:
    answerability = _answerability_contract(scope_contract)
    missing_strict_sources = [
        str(src).strip()
        for src in (retrieval_outcome.get("missing_strict_sources") or [])
        if str(src).strip()
    ]
    missing_query_slots = [
        str(slot).strip()
        for slot in (retrieval_outcome.get("missing_query_slots") or [])
        if str(slot).strip()
    ]
    gating_missing_sources = [] if answerability.get("gold_context_optional") else missing_strict_sources
    has_hard_missing = bool(gating_missing_sources or missing_query_slots)
    has_options_evidence = _has_options_evidence(retrieval_outcome, data_capability_profile)
    has_any_signal = bool(
        data_capability_profile.get("has_gold_evidence")
        or data_capability_profile.get("has_price_signal")
        or data_capability_profile.get("has_iv_signal")
        or data_capability_profile.get("has_options_chain_support")
    )
    scope = scope_contract if isinstance(scope_contract, dict) else {}
    output_mode_ceiling = str(scope.get("output_mode_ceiling") or "").strip()
    specificity_ceiling = str(scope.get("specificity_ceiling") or "").strip()
    can_support_concrete_option_structure = bool(
        data_capability_profile.get("can_support_concrete_option_structure")
    )
    has_fatal_feedback = _has_fatal_feedback(feedbacks)
    return {
        "missing_strict_sources": gating_missing_sources,
        "missing_query_slots": missing_query_slots,
        "has_hard_missing": has_hard_missing,
        "has_options_evidence": has_options_evidence,
        "has_any_signal": has_any_signal,
        "output_mode_ceiling": output_mode_ceiling,
        "specificity_ceiling": specificity_ceiling,
        "can_support_concrete_option_structure": can_support_concrete_option_structure,
        "has_fatal_feedback": has_fatal_feedback,
    }


def _apply_mode_ceiling(
    recommendation_mode: str,
    scope_contract: Dict[str, Any],
) -> str:
    scope = scope_contract if isinstance(scope_contract, dict) else {}
    output_ceiling = str(scope.get("output_mode_ceiling") or "").strip()
    mode = _more_conservative_mode(recommendation_mode, output_ceiling) if output_ceiling else recommendation_mode
    specificity_ceiling = str(scope.get("specificity_ceiling") or "").strip()
    if mode == "actionable_options" and specificity_ceiling == "watchlist_only":
        return "directional_watchlist"
    if mode == "actionable_options" and specificity_ceiling == "no_structure":
        return "informational_only"
    return mode


def _merge_recommendation_mode(
    *,
    deterministic_mode: str,
    llm_mode: str,
    gate_ctx: Dict[str, Any],
    scope_contract: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    """Merge deterministic and LLM modes without flattening posture-watchlist cases.

    The Critic remains conservative, but protected read-only posture cases should
    not be pushed down to informational_only when the retrieval contract is
    complete and options evidence is present.
    """
    llm_candidate = llm_mode if llm_mode in _MODE_ORDER else deterministic_mode
    merge_policy = "default_conservative_merge"
    protected_directional_floor_applied = False

    if gate_ctx.get("has_fatal_feedback"):
        final_mode = "informational_only"
        merge_policy = "fatal_floor"
    elif gate_ctx.get("has_hard_missing"):
        final_mode = "informational_only"
        merge_policy = "hard_missing_floor"
    elif deterministic_mode == "actionable_options":
        if llm_candidate == "informational_only":
            final_mode = "directional_watchlist"
        else:
            final_mode = llm_candidate if llm_candidate in {"actionable_options", "directional_watchlist"} else deterministic_mode
        merge_policy = "actionable_corridor_merge"
    elif (
        deterministic_mode == "directional_watchlist"
        and gate_ctx.get("has_options_evidence")
        and not gate_ctx.get("has_hard_missing")
    ):
        final_mode = "directional_watchlist"
        protected_directional_floor_applied = True
        merge_policy = "protected_directional_floor"
    else:
        final_mode = _more_conservative_mode(deterministic_mode, llm_candidate)

    final_mode = _apply_mode_ceiling(final_mode, scope_contract)
    diagnostics = {
        "llm_mode": llm_candidate,
        "final_mode": final_mode,
        "protected_directional_floor_applied": protected_directional_floor_applied,
        "merge_policy": merge_policy,
    }
    return final_mode, diagnostics


def _determine_recommendation_mode(
    *,
    data_capability_profile: Dict[str, Any],
    strategy_profile: Dict[str, Any],
    catalyst_info: Dict[str, Any],
    retrieval_outcome: Dict[str, Any],
    scope_contract: Dict[str, Any],
    feedbacks: List[AgentFeedback],
    revision_n: int,
) -> str:
    answerability = _answerability_contract(scope_contract)
    gate_ctx = _recommendation_gate_context(
        retrieval_outcome=retrieval_outcome,
        data_capability_profile=data_capability_profile,
        scope_contract=scope_contract,
        feedbacks=feedbacks,
    )
    if answerability["market_analysis_only"]:
        if gate_ctx["has_fatal_feedback"] or gate_ctx["has_hard_missing"] or not gate_ctx["has_any_signal"]:
            mode = "informational_only"
        else:
            mode = "directional_watchlist"
        return _apply_mode_ceiling(mode, scope_contract)
    if gate_ctx["has_fatal_feedback"]:
        mode = "informational_only"
    elif gate_ctx["has_hard_missing"]:
        mode = "informational_only"
    elif gate_ctx["output_mode_ceiling"] == "informational_only":
        mode = "informational_only"
    elif gate_ctx["has_options_evidence"] and gate_ctx["can_support_concrete_option_structure"]:
        if (
            answerability["requires_catalyst_confirmation"]
            and catalyst_info.get("stale_event_evidence")
            and strategy_profile.get("is_concrete_options_recommendation")
        ):
            mode = "directional_watchlist"
        else:
            mode = "actionable_options"
    elif gate_ctx["has_options_evidence"]:
        mode = "directional_watchlist"
    elif gate_ctx["has_any_signal"]:
        mode = "directional_watchlist"
    elif revision_n >= max(_MAX_REVISIONS - 1, 1):
        mode = "informational_only"
    else:
        mode = "informational_only"
    return _apply_mode_ceiling(mode, scope_contract)


# ==========================================
# CriticAgent class
# ==========================================

class CriticAgent:
    """Red-team logic critic. Scoped strictly to strategy — facts are the Checker's job."""

    def __init__(self):
        provider = os.getenv("CRITIC_PROVIDER", "").strip().lower()
        if provider not in {"openai", "ollama"}:
            provider = "openai"
        self.provider = provider

        default_openai_model = os.getenv("CRITIC_OPENAI_FALLBACK_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        default_ollama_model = os.getenv("OLLAMA_CRITIC_MODEL", "options-expert-v1:latest").strip() or "options-expert-v1:latest"
        self.model_name = os.getenv("CRITIC_MODEL", "").strip() or (
            default_openai_model if self.provider == "openai" else default_ollama_model
        )
        temperature = float(os.getenv("CRITIC_TEMPERATURE", "0.0"))
        self.logic_llm = self._build_llm(self.provider, self.model_name, temperature)

        self.fallback_enabled = os.getenv("CRITIC_ENABLE_MODEL_FALLBACK", "1") == "1"
        self.fallback_provider = "ollama" if self.provider == "openai" else "openai"
        self.fallback_model_name = (
            default_ollama_model
            if self.fallback_provider == "ollama"
            else default_openai_model
        )
        self.fallback_llm = (
            self._build_llm(self.fallback_provider, self.fallback_model_name, temperature)
            if self.fallback_enabled
            else None
        )

        self.prompt = get_critic_prompt()

    def _build_llm(self, provider: str, model_name: str, temperature: float):
        if provider == "ollama":
            return ChatOllama(
                model=model_name,
                temperature=temperature,
                format="json",
            ).with_structured_output(CriticResult)

        openai_kwargs: Dict[str, Any] = {
            "model": model_name,
            "temperature": temperature,
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "timeout": float(os.getenv("CRITIC_OPENAI_TIMEOUT_SECONDS", "45")),
        }
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        if openai_base_url:
            openai_kwargs["base_url"] = openai_base_url
        return ChatOpenAI(**openai_kwargs).with_structured_output(CriticResult)

    @staticmethod
    def _is_macro_fatal_evidence_strong(comment: str) -> bool:
        """
        Conservative macro-fatal gate:
        - Reject speculative language as Fatal.
        - Require explicit contradiction semantics to keep Fatal.
        """
        c = (comment or "").lower()
        if any(tok in c for tok in _UNCERTAINTY_TOKENS):
            return False
        return any(
            strong in c for strong in (
                "direct contradiction",
                "directly contradict",
                "clearly contradict",
                "inconsistent with",
            )
        )

    @staticmethod
    def _fatal_issue_is_actionable(
        issue: LogicIssue,
        iv_regime_info: Dict[str, Any],
        insider_info: Dict[str, Any],
        strategy_profile: Dict[str, Any],
        catalyst_info: Dict[str, Any],
        data_capability_profile: Dict[str, Any],
    ) -> bool:
        """
        Guardrail against LLM over-triggered Fatal findings.
        """
        category = issue.category
        if category == "iv_regime_fit":
            regime = iv_regime_info.get("iv_regime")
            if regime == "HIGH":
                return bool(
                    strategy_profile.get("is_naked_long_premium")
                    and not catalyst_info.get("has_near_term_catalyst")
                )
            if regime == "LOW":
                return bool(strategy_profile.get("is_short_premium"))
            return False
        if category == "insider_signal_weakness":
            # NOISE is explicitly non-fatal per policy.
            return insider_info.get("verdict") in {"BULLISH_SIGNAL", "BEARISH_SIGNAL"}
        if category == "catalyst_horizon_fit":
            # Horizon mismatch should normally be corrected via tighter framing,
            # not a hard rewrite.
            return False
        if category == "evidence_sufficiency_and_abstention":
            # Insufficient structure support should degrade to informational-only,
            # not force a Fatal rewrite.
            return False
        if category == "strategy_family_fit":
            if not strategy_profile.get("is_concrete_options_recommendation"):
                return False
            if not data_capability_profile.get("can_support_concrete_option_structure"):
                return False
            if iv_regime_info.get("iv_regime") == "HIGH":
                return bool(
                    strategy_profile.get("is_naked_long_premium")
                    and not catalyst_info.get("has_near_term_catalyst")
                )
            if iv_regime_info.get("iv_regime") == "LOW":
                return bool(strategy_profile.get("is_short_premium"))
            return False
        if category == "macro_contradiction":
            # Fatal only when contradiction language is strong and non-speculative.
            return CriticAgent._is_macro_fatal_evidence_strong(issue.comment)
        return True

    async def audit(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Return the Critic node payload for LangGraph.

        Shape:
            {
              "critic_feedback": List[AgentFeedback],
              "critic_verdict":  "pass" | "fatal" | "minor",
            }
        """
        revision_n = state.get("revision_count", 0)
        if revision_n >= _MAX_REVISIONS:
            logger.warning("CriticAgent: max revisions reached — skipping audit.")
            return {"critic_feedback": [], "critic_verdict": "pass"}

        draft = state.get("draft_report", "") or ""
        metadata = state.get("metadata")
        silver_ctx = state.get("silver_context", {}) or {}
        gold_ctx = state.get("gold_context", []) or []
        time_range = state.get("time_range", {}) or {}
        scope_contract = state.get("scope_contract", {}) or {}
        retrieval_outcome = state.get("retrieval_outcome", {}) or {}
        macro_ctx = state.get("macro_context", "") or ""
        user_query = state.get("original_query", "") or ""
        answerability = _answerability_contract(scope_contract)
        requires_catalyst_confirmation = answerability["requires_catalyst_confirmation"]
        hard_data_sufficient_for_answer = answerability["hard_data_sufficient_for_answer"]

        # Deterministic pre-computes give the LLM an unambiguous ground truth.
        iv_regime_info = _infer_iv_regime(silver_ctx)
        insider_info = _compute_insider_confidence(gold_ctx)
        anchor_date = _parse_iso_date(time_range.get("anchor_date"))
        strategy_profile = _extract_strategy_profile(draft, anchor_date)
        catalyst_info = _catalyst_profile(draft, metadata, gold_ctx, time_range, anchor_date)
        data_capability_profile = build_data_capability_profile(metadata, silver_ctx, gold_ctx, time_range)

        feedbacks: List[AgentFeedback] = []
        minor_suggestions: List[str] = []
        minor_edits: List[FinalizerEdit] = []
        required_reply_keys = _must_keep_keys_for_reply(metadata, silver_ctx)
        market_impact_risk = _market_impact_risk_value(
            silver_ctx=silver_ctx,
            metadata=metadata,
            scope_contract=scope_contract,
        )
        prompt_posture_contract = derive_posture_contract(
            dict((silver_ctx or {}).get("values") or {}),
            iv_regime_info,
            scope_contract,
            state.get("recommendation_mode") or state.get("actionability_mode") or "directional_watchlist",
            metadata=metadata,
        )
        high_market_impact_risk = market_impact_risk.lower() == "high"

        # -------- Deterministic backstop 1: insider/structure contradiction --------
        lowered_draft = draft.lower()
        if insider_info["verdict"] == "BEARISH_SIGNAL" and any(
            kw in lowered_draft for kw in ("long call", "buy call", "bull call")
        ):
            feedbacks.append(AgentFeedback(
                sender="Critic",
                error_type="Fatal",
                comment=(
                    f"[insider_signal_weakness] Recommended bullish call structure contradicts a "
                    f"BEARISH insider signal ({insider_info['bearish_exec_count']} execs selling, "
                    f"threshold={insider_info['threshold']}). "
                    "Either justify why the signal does not apply, or restructure."
                ),
                revision_index=revision_n,
            ))
        elif insider_info["verdict"] == "BULLISH_SIGNAL" and any(
            kw in lowered_draft for kw in ("long put", "buy put", "bear put")
        ):
            feedbacks.append(AgentFeedback(
                sender="Critic",
                error_type="Fatal",
                comment=(
                    f"[insider_signal_weakness] Recommended bearish put structure contradicts a "
                    f"BULLISH insider signal ({insider_info['bullish_exec_count']} execs buying, "
                    f"threshold={insider_info['threshold']})."
                ),
                revision_index=revision_n,
            ))

        # -------- Deterministic backstop 2: IV regime contradiction --------
        # IV-regime fit is still deterministic-first, but now distinguishes
        # between direct contradiction and a merely suboptimal structure family.
        regime = iv_regime_info.get("iv_regime")
        if regime == "HIGH" and strategy_profile.get("is_naked_long_premium"):
            feedbacks.append(AgentFeedback(
                sender="Critic",
                error_type=(
                    "Fatal"
                    if requires_catalyst_confirmation and not catalyst_info.get("has_near_term_catalyst")
                    else "Minor"
                ),
                comment=(
                    f"[strategy_family_fit] HIGH IV regime (atm_iv={iv_regime_info['atm_iv']}) with a naked long-premium "
                    "structure is only defensible around a specific near-term catalyst. Prefer a defined-risk spread "
                    "or downgrade the conviction."
                ),
                revision_index=revision_n,
            ))
        elif regime == "HIGH" and strategy_profile.get("is_long_premium") and strategy_profile.get("is_defined_risk"):
            minor_suggestions.append(
                "[strategy_family_fit] HIGH IV supports the directional thesis less efficiently in premium-rich structures; "
                "keep the defined-risk spread framing and avoid naked premium language."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="remove_unsupported_structure",
                target_section="asset_read",
                instruction="Keep the structure at defined-risk spread language and avoid naked long-premium framing in a HIGH IV regime.",
            ))
        elif regime == "LOW" and strategy_profile.get("is_short_premium"):
            feedbacks.append(AgentFeedback(
                sender="Critic",
                error_type="Fatal",
                comment=(
                    f"[iv_regime_fit] LOW IV regime (atm_iv={iv_regime_info['atm_iv']}) — selling premium "
                    "is under-paid for the risk. Prefer long optionality / debit spreads."
                ),
                revision_index=revision_n,
            ))

        # -------- Deterministic backstop 3: catalyst horizon / DTE fit --------
        window_days = time_range.get("window_days")
        dte_days = strategy_profile.get("dte_days")
        if (
            requires_catalyst_confirmation
            and
            strategy_profile.get("is_concrete_options_recommendation")
            and isinstance(dte_days, int)
            and dte_days > 60
            and catalyst_info.get("has_near_term_catalyst")
            and isinstance(window_days, int)
            and window_days <= 30
        ):
            minor_suggestions.append(
                "[catalyst_horizon_fit] The thesis is short-window / event-driven, but the recommended DTE is much longer than the evidence window. "
                "Tighten the expiration or explain the extra runway."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="tighten_horizon",
                target_section="direct_conclusion",
                instruction="Tighten the horizon framing so the conclusion matches the event window and does not imply a longer-dated thesis than the evidence supports.",
            ))

        if (
            requires_catalyst_confirmation
            and strategy_profile.get("is_concrete_options_recommendation")
            and catalyst_info.get("stale_event_evidence")
        ):
            minor_suggestions.append(
                "[catalyst_horizon_fit] The catalyst framing relies on stale Gold evidence relative to the requested window. "
                "Reduce conviction or shift to informational-only framing."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="tighten_horizon",
                target_section="direct_conclusion",
                instruction="State clearly that the catalyst framing is stale relative to the requested window and lower conviction accordingly.",
            ))

        # -------- Deterministic backstop 4: evidence sufficiency / abstention --------
        if (
            strategy_profile.get("is_concrete_options_recommendation")
            and not data_capability_profile.get("can_support_concrete_option_structure")
            and not (
                answerability["analysis_mode"] == "data_backed_read"
                and hard_data_sufficient_for_answer
            )
        ):
            minor_suggestions.append(
                "[evidence_sufficiency_and_abstention] Current project data does not provide enough options microstructure support "
                "for a concrete structure. Keep the answer at the directional watchlist level unless evidence weakens further."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="mode_downgrade",
                target_section="recommendation_mode",
                instruction="Downgrade the output to directional watchlist because current project data does not support a concrete options structure.",
            ))
        if (
            answerability["analysis_mode"] == "data_backed_read"
            and hard_data_sufficient_for_answer
            and not data_capability_profile.get("can_support_concrete_option_structure")
        ):
            minor_suggestions.append(
                "[evidence_sufficiency_and_abstention] Hard data is complete for a data-backed read, so preserve a market-read or watchlist framing instead of forcing a why-not-now downgrade."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="mode_downgrade",
                target_section="recommendation_mode",
                instruction="Keep the output at read-only market analysis or directional watchlist level; do not add a catalyst-driven why-not-now downgrade when hard data is sufficient.",
            ))

        unavailable_metrics = data_capability_profile.get("unavailable_metrics") or []
        if unavailable_metrics:
            minor_suggestions.append(
                "[evidence_sufficiency_and_abstention] Some requested metrics are unavailable in current project scope "
                f"({', '.join(unavailable_metrics)}). Add a caveat instead of inferring substitutes."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="clarify_risk",
                target_section="risks",
                instruction=f"Add a caveat that current project scope does not include: {', '.join(unavailable_metrics)}; do not infer substitutes.",
            ))

        if high_market_impact_risk:
            minor_suggestions.append(
                f"[risk_reward_imbalance] Market impact risk is {market_impact_risk}. Keep any structure non-actionable unless execution conditions improve, and surface that risk explicitly."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="must_disclose_risk",
                target_section="conversation_reply",
                instruction=f"Explicitly state that market impact risk is {market_impact_risk} and that it is a reason not to treat the structure as a live recommendation.",
            ))
            minor_edits.append(_make_critic_edit(
                edit_type="must_disclose_risk",
                target_section="risks",
                instruction=f"Add a risk line that market impact risk is {market_impact_risk} and that execution conditions would need to improve before a live structure is appropriate.",
            ))

        if (
            revision_n >= max(_MAX_REVISIONS - 1, 1)
            and strategy_profile.get("is_concrete_options_recommendation")
            and (
                catalyst_info.get("stale_event_evidence")
                or not data_capability_profile.get("can_support_concrete_option_structure")
            )
        ):
            minor_suggestions.append(
                "[evidence_sufficiency_and_abstention] Revision budget is nearly exhausted while structure confidence is still weak. "
                "Prefer directional watchlist or informational-only output over a forced trade recommendation."
            )
            minor_edits.append(_make_critic_edit(
                edit_type="mode_downgrade",
                target_section="recommendation_mode",
                instruction="Keep the output below strike-level action because revision budget is nearly exhausted and structure confidence is still weak.",
            ))

        if required_reply_keys:
            minor_edits.append(_make_critic_edit(
                edit_type="keep_numbers",
                target_section="conversation_reply",
                instruction="Preserve the query-required strict metrics in the first sentence of the conversation reply.",
                must_keep_keys=required_reply_keys,
            ))

        # -------- LLM logic critique (best-effort) --------
        # Minor suggestions are collected separately and forwarded to the Finalizer
        # as polish notes — they do NOT block the draft or trigger a revision.
        # Only Fatal issues set is_passed=False and go into critic_feedback.
        try:
            chain = self.prompt | self.logic_llm
            logger.info(
                f"CriticAgent: invoking logic LLM | provider={self.provider} | model={self.model_name} "
                f"| iv_regime={iv_regime_info['iv_regime']} | insider_verdict={insider_info['verdict']}"
            )
            invoke_payload = {
                "original_query": user_query,
                "macro_context": macro_ctx,
                "iv_regime": iv_regime_info,
                "insider_confidence": insider_info,
                "reasoning_contract_block": render_reasoning_contract(),
                "posture_contract_block": render_posture_contract_block(prompt_posture_contract),
                "scope_contract_block": render_scope_contract_block(scope_contract),
                "retrieval_outcome_block": render_retrieval_outcome_block(retrieval_outcome),
                "data_capability_block": render_data_capability_profile(data_capability_profile),
                "time_range_block": _render_time_range_block(time_range, metadata),
                "critic_reasoning_profile_block": str({
                    "analysis_mode": answerability.get("analysis_mode"),
                    "requires_catalyst_confirmation": answerability.get("requires_catalyst_confirmation"),
                    "gold_context_optional": answerability.get("gold_context_optional"),
                    "hard_data_sufficient_for_answer": answerability.get("hard_data_sufficient_for_answer"),
                    "market_analysis_only": answerability.get("market_analysis_only"),
                    "iv_regime": iv_regime_info.get("iv_regime"),
                    "strategy_family": strategy_profile.get("family"),
                    "dte_days": strategy_profile.get("dte_days"),
                    "has_near_term_catalyst": catalyst_info.get("has_near_term_catalyst"),
                    "stale_event_evidence": catalyst_info.get("stale_event_evidence"),
                    "newest_gold_age_days": catalyst_info.get("newest_gold_age_days"),
                    "can_support_concrete_option_structure": data_capability_profile.get("can_support_concrete_option_structure"),
                }),
                "silver_block": _format_silver(silver_ctx),
                "gold_block": _format_gold(gold_ctx),
                "draft": draft,
            }
            result: CriticResult = await chain.ainvoke(invoke_payload)

            # Minor suggestions: forward to Finalizer, do NOT block.
            if result.minor_suggestions:
                minor_suggestions.extend(result.minor_suggestions)
                logger.info(
                    f"CriticAgent: {len(result.minor_suggestions)} minor suggestions "
                    "forwarded to Finalizer (non-blocking)."
                )

            # Fatal issues only: add to critic_feedback → may trigger revision.
            if not result.is_passed:
                for issue in result.issues:
                    if issue.category == "catalyst_horizon_fit" and not requires_catalyst_confirmation:
                        minor_suggestions.append(f"[{issue.category}] {issue.comment}")
                        minor_edits.append(_make_critic_edit(
                            edit_type="tighten_horizon",
                            target_section="direct_conclusion",
                            instruction="Do not force a catalyst-horizon downgrade for this data-backed read; keep the answer anchored to current hard data instead.",
                        ))
                        continue
                    if issue.severity == "Fatal":
                        if self._fatal_issue_is_actionable(
                            issue,
                            iv_regime_info,
                            insider_info,
                            strategy_profile,
                            catalyst_info,
                            data_capability_profile,
                        ):
                            feedbacks.append(AgentFeedback(
                                sender="Critic",
                                error_type="Fatal",
                                comment=f"[{issue.category}] {issue.comment}",
                                revision_index=revision_n,
                            ))
                        else:
                            # Demote unsupported Fatal to a non-blocking polish note.
                            minor_suggestions.append(f"[{issue.category}] {issue.comment}")
                            if issue.category == "strategy_family_fit":
                                minor_edits.append(_make_critic_edit(
                                    edit_type="remove_unsupported_structure",
                                    target_section="asset_read",
                                    instruction=issue.comment,
                                ))
                            elif issue.category == "catalyst_horizon_fit":
                                minor_edits.append(_make_critic_edit(
                                    edit_type="tighten_horizon",
                                    target_section="direct_conclusion",
                                    instruction=issue.comment,
                                ))
                            elif issue.category == "evidence_sufficiency_and_abstention":
                                minor_edits.append(_make_critic_edit(
                                    edit_type="mode_downgrade",
                                    target_section="recommendation_mode",
                                    instruction=issue.comment,
                                ))
                            logger.info(
                                "CriticAgent: demoted non-actionable Fatal -> minor "
                                f"(category={issue.category}, iv_regime={iv_regime_info.get('iv_regime')}, "
                                f"insider={insider_info.get('verdict')})"
                            )
                    else:
                        # LLM incorrectly placed a Minor in issues — demote to suggestion.
                        minor_suggestions.append(f"[{issue.category}] {issue.comment}")
                        if issue.category == "strategy_family_fit":
                            minor_edits.append(_make_critic_edit(
                                edit_type="remove_unsupported_structure",
                                target_section="asset_read",
                                instruction=issue.comment,
                            ))
                        elif issue.category == "catalyst_horizon_fit":
                            minor_edits.append(_make_critic_edit(
                                edit_type="tighten_horizon",
                                target_section="direct_conclusion",
                                instruction=issue.comment,
                            ))
                        elif issue.category == "evidence_sufficiency_and_abstention":
                            minor_edits.append(_make_critic_edit(
                                edit_type="mode_downgrade",
                                target_section="recommendation_mode",
                                instruction=issue.comment,
                            ))
                        elif issue.category in {"risk_reward_imbalance", "macro_contradiction", "other"}:
                            minor_edits.append(_make_critic_edit(
                                edit_type="clarify_risk",
                                target_section="risks",
                                instruction=issue.comment,
                            ))
                        logger.debug(
                            f"CriticAgent: Minor issue demoted from issues→suggestions: "
                            f"{issue.comment[:60]}"
                        )

        except Exception as e:
            logger.warning(
                "CriticAgent: primary %s logic LLM failed (%s); deterministic findings retained.",
                self.provider,
                e,
            )
            if self.fallback_llm is not None:
                try:
                    logger.info(
                        "CriticAgent: retrying with %s fallback model=%s",
                        self.fallback_provider,
                        self.fallback_model_name,
                    )
                    result = await (self.prompt | self.fallback_llm).ainvoke(invoke_payload)
                    if result.minor_suggestions:
                        minor_suggestions.extend(result.minor_suggestions)
                    if not result.is_passed:
                        for issue in result.issues:
                            if issue.category == "catalyst_horizon_fit" and not requires_catalyst_confirmation:
                                minor_suggestions.append(f"[{issue.category}] {issue.comment}")
                                continue
                            if issue.severity == "Fatal":
                                if self._fatal_issue_is_actionable(
                                    issue,
                                    iv_regime_info,
                                    insider_info,
                                    strategy_profile,
                                    catalyst_info,
                                    data_capability_profile,
                                ):
                                    feedbacks.append(AgentFeedback(
                                        sender="Critic",
                                        error_type="Fatal",
                                        comment=f"[{issue.category}] {issue.comment}",
                                        revision_index=revision_n,
                                    ))
                                else:
                                    minor_suggestions.append(f"[{issue.category}] {issue.comment}")
                            else:
                                minor_suggestions.append(f"[{issue.category}] {issue.comment}")
                except Exception as fb_err:
                    logger.warning(
                        "CriticAgent: %s fallback LLM failed (%s); deterministic findings retained.",
                        self.fallback_provider,
                        fb_err,
                    )

        minor_suggestions = _dedupe_suggestions(minor_suggestions)
        deterministic_mode = _determine_recommendation_mode(
            data_capability_profile=data_capability_profile,
            strategy_profile=strategy_profile,
            catalyst_info=catalyst_info,
            retrieval_outcome=retrieval_outcome,
            scope_contract=scope_contract,
            feedbacks=feedbacks,
            revision_n=revision_n,
        )
        gate_ctx = _recommendation_gate_context(
            retrieval_outcome=retrieval_outcome,
            data_capability_profile=data_capability_profile,
            scope_contract=scope_contract,
            feedbacks=feedbacks,
        )
        llm_mode = (
            result.recommendation_mode
            if "result" in locals() and getattr(result, "recommendation_mode", None)
            else deterministic_mode
        )
        recommendation_mode, merge_trace = _merge_recommendation_mode(
            deterministic_mode=deterministic_mode,
            llm_mode=llm_mode,
            gate_ctx=gate_ctx,
            scope_contract=scope_contract,
        )
        posture_contract = derive_posture_contract(
            dict((silver_ctx or {}).get("values") or {}),
            iv_regime_info,
            scope_contract,
            recommendation_mode,
            metadata=metadata,
        )
        structure_visibility_mode = _structure_visibility_mode(strategy_profile, recommendation_mode)
        if recommendation_mode != "actionable_options":
            suggestion = (
                "[evidence_sufficiency_and_abstention] Output mode should be directional watchlist only: prioritize the underlying / ETF and the "
                "options board to monitor, but do not issue strike-level action."
                if recommendation_mode == "directional_watchlist"
                else "[evidence_sufficiency_and_abstention] Output mode should be informational only: explain the logic and caveats without "
                     "promoting an actionable options structure."
            )
            minor_suggestions.append(suggestion)
            minor_suggestions = _dedupe_suggestions(minor_suggestions)
            minor_edits.append(_make_critic_edit(
                edit_type="mode_downgrade",
                target_section="recommendation_mode",
                instruction=(
                    "Keep the output at directional watchlist level rather than strike-level action."
                    if recommendation_mode == "directional_watchlist"
                    else "Keep the output informational only and do not promote a concrete options setup."
                ),
            ))
            if structure_visibility_mode == "illustrative_structure":
                illustrative_hint = _extract_structure_hint(draft, strategy_profile)
                instruction = (
                    "Preserve at most one non-actionable illustrative structure example under the current mode boundary."
                )
                if illustrative_hint:
                    instruction += f" Preferred illustrative template: {illustrative_hint}."
                minor_edits.append(_make_critic_edit(
                    edit_type="preserve_illustrative_structure",
                    target_section="asset_read",
                    instruction=instruction,
                ))
        minor_edits = _dedupe_edits(minor_edits)
        revision_constraints = _build_revision_constraints(
            recommendation_mode=recommendation_mode,
            strategy_profile=strategy_profile,
            silver_ctx=silver_ctx,
            retrieval_outcome=retrieval_outcome,
            data_capability_profile=data_capability_profile,
            draft=draft,
            metadata=metadata,
            scope_contract=scope_contract,
            iv_regime_pinned=iv_regime_info,
        )
        critic_reasoning_profile = {
            "iv_regime": iv_regime_info.get("iv_regime"),
            "strategy_family": strategy_profile.get("family"),
            "dte_days": strategy_profile.get("dte_days"),
            "has_near_term_catalyst": catalyst_info.get("has_near_term_catalyst"),
            "stale_event_evidence": catalyst_info.get("stale_event_evidence"),
            "newest_gold_age_days": catalyst_info.get("newest_gold_age_days"),
            "has_hard_missing": gate_ctx.get("has_hard_missing"),
            "missing_strict_sources": list(gate_ctx.get("missing_strict_sources") or []),
            "missing_query_slots": list(gate_ctx.get("missing_query_slots") or []),
            "has_options_evidence": gate_ctx.get("has_options_evidence"),
            "can_support_concrete_option_structure": gate_ctx.get("can_support_concrete_option_structure"),
            "output_mode_ceiling": gate_ctx.get("output_mode_ceiling"),
            "specificity_ceiling": gate_ctx.get("specificity_ceiling"),
            "deterministic_mode": deterministic_mode,
            "llm_mode": merge_trace.get("llm_mode"),
            "final_mode": merge_trace.get("final_mode"),
            "protected_directional_floor_applied": merge_trace.get("protected_directional_floor_applied"),
            "merge_policy": merge_trace.get("merge_policy"),
            "recommendation_mode": recommendation_mode,
            "structure_visibility_mode": structure_visibility_mode,
            "market_impact_risk": market_impact_risk,
            "market_analysis_only": answerability.get("market_analysis_only"),
            "posture_label": posture_contract.get("posture_label", ""),
            "posture_takeaway": posture_contract.get("posture_takeaway", ""),
            "posture_rationale": posture_contract.get("posture_rationale", ""),
            "base_regime_read": posture_contract.get("base_regime_read", ""),
            "escalation_risk_archetype": posture_contract.get("escalation_risk_archetype", ""),
            "escalation_risk_read": posture_contract.get("escalation_risk_read", ""),
            "posture_reasoning_trace": dict(posture_contract.get("posture_reasoning_trace") or {}),
        }
        verdict = self._compute_verdict(feedbacks)
        logger.info(
            f"CriticAgent: verdict={verdict} | fatal={len(feedbacks)} | "
            f"minor_suggestions={len(minor_suggestions)} | deterministic_mode={deterministic_mode} "
            f"| llm_mode={merge_trace.get('llm_mode')} | recommendation_mode={recommendation_mode} "
            f"| merge_policy={merge_trace.get('merge_policy')}"
        )

        return {
            "critic_feedback": feedbacks,
            "critic_verdict": verdict,
            # Non-blocking polish notes forwarded to Finalizer via state.
            "critic_minor_suggestions": minor_suggestions,
            "critic_edit_suggestions": minor_edits,
            "data_capability_profile": data_capability_profile,
            "critic_reasoning_profile": critic_reasoning_profile,
            "recommendation_mode": recommendation_mode,
            "actionability_mode": recommendation_mode,
            "structure_visibility_mode": structure_visibility_mode,
            "revision_constraints": revision_constraints,
        }

    @staticmethod
    def _compute_verdict(feedbacks: List[AgentFeedback]) -> str:
        if not feedbacks:
            return "pass"
        if any(fb.error_type.lower() == "fatal" for fb in feedbacks):
            return "fatal"
        return "minor"
