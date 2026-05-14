"""
Scripts/agents/analyst.py

Analyst Agent — institutional-grade draft author for the LangGraph pipeline.

Design pillars (aligned with the architect brief):
1. **Macro-Chain reasoning**: macro_context is injected into the system prompt
   using the Macro → Meso → Micro framework so the LLM reasons about
   transmission channels (e.g. GPR spike -> safe-haven flow -> GLD call demand)
   rather than isolated facts.
2. **IV Regime awareness** (Silver upgrade from "recommendation" to "arbitrage
   discovery"): before drafting, a deterministic helper classifies the current
   implied-vol regime from silver_context. The LLM is told the regime so its
   strategy recommendation is compatible (sell premium in high-IV, buy
   protection in low-IV).
3. **Strict data lineage**: every numeric / factual claim must carry either
   `[Silver: <preferred_anchor>]` or `[Gold: <bronze_ref>]`. Silver audit
   lineage refs remain available for provenance, but they are not the default
   inline citation form. Downstream the CheckerAgent enforces this.
4. **Revision-aware**: on every re-entry the agent sees the full append-only
   feedback log from state["critic_feedback"] (both Checker + Critic senders)
   and MUST address every unresolved Fatal error.
5. **Temporal decay hint**: the LLM is explicitly told to weight newer
   gold_context entries (fresh record_date) more heavily than stale ones.
6. **Graceful degradation**: LLM failures surface as a stub draft that
   explicitly says "INSUFFICIENT DATA" rather than a hallucinated report.
"""

from __future__ import annotations

import asyncio
import os
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import requests
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from Scripts.agents.prompts import render_revision_block
from Scripts.agents.state import RenderSafetyContract
from Scripts.core.financial_config import get_analyst_system_prompt
from Scripts.core.evidence_contracts import (
    build_silver_citation_registry,
    build_slot_evidence_contracts,
    canonical_query_family,
    evaluate_retrieval_slot_support,
    evaluate_slot_coverage,
    render_preferred_silver_citation,
    render_slot_evidence_contract_block,
)
from Scripts.core.posture_contract import (
    derive_posture_contract,
    render_posture_contract_block,
)
from Scripts.core.financial_ontology import (
    INSIDER_FLOW_QUERY_SLOTS,
    SEC_ACTION_TAXONOMY,
    describe_sec_action_direction,
    query_slots_for_family,
)
from Scripts.core.financial_narrative_contract import (
    build_narrative_brief,
    render_narrative_brief_block,
)
from Scripts.core.liquidity_policy import (
    available_ticker_prefixes as liquidity_available_ticker_prefixes,
    resolve_first_available_ticker_bundle,
    resolve_primary_ticker,
)
from Scripts.core.silver_context import preferred_silver_context_from_state
from Scripts.core.sec_contract import SECAnalysisBundle

logger = logging.getLogger(__name__)

_EXECUTABLE_LIQUIDITY_NOTE = (
    "Executable subset only (ask >= 0.50, spread <= 15%, DTE 7-60, tiered moneyness band)."
)

_LIVE_RECOMMENDATION_PATTERNS = (
    r"\bbest trade\b",
    r"\brecommended trade\b",
    r"\blive recommendation\b",
    r"\bcurrent best trade\b",
    r"\bbuy (?:this|the)\b",
    r"\bsell (?:this|the)\b",
    r"\benter (?:this|the)\b",
)

# Compiled once; covers all LLM placeholder variants that signal missing data
# leaking into the draft (None, null, N/A, n.a., undefined, nan).
_BAD_PLACEHOLDER_PAT = r"(?:none|null|n/a|n\.a\.|undefined|nan)"


def _normalize_openai_base_url(raw_base_url: Optional[str], ollama_host: Optional[str]) -> str:
    """
    Normalize Ollama OpenAI-compatible base URL to end with `/v1`.

    Accepts:
      - http://localhost:11434
      - http://localhost:11434/
      - http://localhost:11434/v1
      - http://localhost:11434/v1/
    Returns:
      - http://localhost:11434/v1
    """
    candidate = (raw_base_url or "").strip()
    if not candidate:
        candidate = (ollama_host or "").strip()
    if not candidate:
        candidate = "http://localhost:11434"

    base = candidate.rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _is_ollama_runner_500(exc: Exception) -> bool:
    """Return True for known Ollama terminal runner failures."""
    msg = str(exc).lower()
    return ("status code: 500" in msg) or ("runner process has terminated" in msg)


def _collect_valid_citation_ids(
    silver_ctx: Dict[str, Any],
    gold_ctx: List[Any],
    supplemental_news_ctx: Optional[List[Any]] = None,
) -> Dict[str, List[str]]:
    silver_ids: List[str] = []
    gold_ids: List[str] = []

    for a in (silver_ctx.get("lineage_anchors", []) if isinstance(silver_ctx, dict) else []) or []:
        s = str(a).strip()
        if s:
            silver_ids.append(s)

    for c in list(gold_ctx or []) + list(supplemental_news_ctx or []):
        br = getattr(c, "bronze_ref", None) or (c.get("bronze_ref") if isinstance(c, dict) else None)
        if br:
            gold_ids.append(str(br).strip())

    # Preserve order while deduping
    silver_ids = list(dict.fromkeys(silver_ids))
    gold_ids = list(dict.fromkeys(gold_ids))
    return {"silver_ids": silver_ids, "gold_ids": gold_ids}


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _source_type_value(chunk: Any) -> str:
    raw = _obj_get(chunk, "source_type", "")
    raw = getattr(raw, "value", raw)
    return str(raw or "").strip().lower()


def _sec_analysis_bundle(retrieval_outcome: Optional[Dict[str, Any]]) -> SECAnalysisBundle:
    outcome = retrieval_outcome if isinstance(retrieval_outcome, dict) else {}
    bundle = outcome.get("sec_analysis_bundle") or {}
    if isinstance(bundle, SECAnalysisBundle):
        return bundle
    if isinstance(bundle, dict) and bundle:
        try:
            return SECAnalysisBundle.model_validate(bundle)
        except Exception:
            pass
    compat_bundle = {
        "coverage": {
            "sec_forms_requested": list(outcome.get("sec_forms_requested") or []),
            "sec_forms_retrieved": list(outcome.get("sec_forms_retrieved") or []),
            "missing_forms": [form for form in list(outcome.get("sec_forms_requested") or []) if form not in set(outcome.get("sec_forms_retrieved") or [])],
            "coverage_mode": "partial" if outcome.get("sec_forms_retrieved") else "none",
            "sec_slot_hits": list(outcome.get("sec_slot_hits") or []),
            "sec_slot_missing": list(outcome.get("sec_slot_missing") or []),
        },
        "existence": {
            "sec_forms_requested": list(outcome.get("sec_forms_requested") or []),
            "sec_forms_retrieved": list(outcome.get("sec_forms_retrieved") or []),
            "sec_payload_context_by_form": dict(outcome.get("sec_payload_context_by_form") or {}),
            "sec_slot_hits": list(outcome.get("sec_slot_hits") or []),
            "sec_slot_missing": list(outcome.get("sec_slot_missing") or []),
        },
        "form4_features": [feature for feature in list(outcome.get("sec_analysis_features") or []) if str(feature.get("form_type") or "").upper() == "4"],
        "form8k_features": [feature for feature in list(outcome.get("sec_analysis_features") or []) if str(feature.get("form_type") or "").upper() == "8-K"],
        "form4_analysis_result": dict(outcome.get("form4_analysis_result") or {}),
        "form8k_analysis_result": dict(outcome.get("form8k_analysis_result") or {}),
        "missing_disclosure": {
            "missing_forms": [form for form in list(outcome.get("sec_forms_requested") or []) if form not in set(outcome.get("sec_forms_retrieved") or [])],
            "missing_slots": list(outcome.get("sec_slot_missing") or []),
            "coverage_mode": "partial" if outcome.get("sec_forms_retrieved") else "none",
            "has_missing": bool(outcome.get("sec_slot_missing") or []),
        },
    }
    try:
        return SECAnalysisBundle.model_validate(compat_bundle)
    except Exception:
        return SECAnalysisBundle()


def _sec_analysis_features(retrieval_outcome: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bundle = _sec_analysis_bundle(retrieval_outcome)
    return [
        *[feature.model_dump() for feature in list(bundle.form4_features or [])],
        *[feature.model_dump() for feature in list(bundle.form8k_features or [])],
    ]


def _fmt_money(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    abs_value = abs(float(value))
    if abs_value >= 1_000_000_000:
        return f"${abs_value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"${abs_value / 1_000_000:.2f}M"
    if abs_value >= 1_000:
        return f"${abs_value / 1_000:.1f}K"
    return f"${abs_value:,.0f}"


def _fmt_shares(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return f"{int(round(float(value))):,}"


def _sec_feature_line(feature: Dict[str, Any]) -> str:
    form_type = str(feature.get("form_type") or "").upper()
    if form_type == "8-K":
        content = str(feature.get("content") or "").strip()
        filed_at = str(feature.get("filed_at") or "").strip()
        return f"8-K filing on {filed_at}: {content}".strip(": ") if filed_at else (content or "8-K evidence was retrieved in this run")

    owner = str(feature.get("owner") or "An insider").strip()
    role = str(feature.get("role") or "").strip()
    action = str(feature.get("action_direction") or "NONE").upper()
    shares = _fmt_shares(feature.get("shares"))
    total_value = _fmt_money(feature.get("total_value"))
    remaining = _fmt_shares(feature.get("remaining_shares"))
    transaction_date = str(feature.get("transaction_date") or feature.get("filed_at") or "").strip()
    plan_flag = bool(feature.get("is_10b5_1_planned"))
    cluster_flag = bool(feature.get("is_cluster_trade"))
    action_text = {
        "SELL": "sold",
        "BUY": "bought",
        "ACQUIRE/VEST": "vested or acquired",
    }.get(action, "filed")

    lead = owner if not role else f"{owner} ({role})"
    details: List[str] = [action_text]
    if shares:
        details.append(f"{shares} shares")
    if total_value:
        details.append(f"worth about {total_value}")
    if transaction_date:
        details.append(f"on {transaction_date}")
    sentence = f"{lead} {', '.join(details)}".strip()
    tags: List[str] = []
    if plan_flag:
        tags.append("10b5-1 planned")
    if cluster_flag:
        tags.append("clustered insider window")
    if remaining:
        tags.append(f"remaining holdings about {remaining} shares")
    if tags:
        sentence += " | " + " | ".join(tags)
    return sentence


def _sec_analysis_summary(
    retrieval_outcome: Optional[Dict[str, Any]],
    gold_ctx: List[Any],
) -> str:
    bundle = _sec_analysis_bundle(retrieval_outcome)
    coverage = bundle.coverage
    summaries: List[str] = []
    form4_result = bundle.form4_analysis_result
    if form4_result.filing_count > 0:
        if form4_result.directional_read == "selling_pressure":
            summary = f"Form 4 flow leans bearish, with {form4_result.sell_count} selling filing(s)"
            if form4_result.total_value:
                summary += f" totaling about {_fmt_money(form4_result.total_value)}"
        elif form4_result.directional_read == "buying_support":
            summary = f"Form 4 flow leans constructive, with {form4_result.buy_count} buying filing(s)"
            if form4_result.total_value:
                summary += f" totaling about {_fmt_money(form4_result.total_value)}"
        elif form4_result.directional_read == "compensation_vesting":
            summary = "Form 4 flow is mostly compensation-driven vesting rather than discretionary open-market activity"
        else:
            summary = "Form 4 flow is mixed across selling, buying, and vesting activity"
        tags: List[str] = []
        if form4_result.planned_count:
            tags.append(f"{form4_result.planned_count} filing(s) flagged as 10b5-1 planned")
        if form4_result.cluster_count:
            tags.append(f"{form4_result.cluster_count} filing(s) arrived in a clustered insider window")
        if tags:
            summary += "; " + "; ".join(tags)
        summaries.append(summary)

    form8k_result = bundle.form8k_analysis_result
    if form8k_result.filing_count > 0:
        tone_text = {
            "negative": "the filing set skews negative",
            "positive": "the filing set skews constructive",
        }.get(form8k_result.dominant_tone, "the filing set looks broadly neutral")
        category_text = f" around {', '.join(form8k_result.categories[:3])}" if form8k_result.categories else ""
        base = f"8-K event evidence is present and {tone_text}{category_text}"
        if form8k_result.latest_filing_date and form8k_result.latest_filing_content:
            base += f"; latest filing on {form8k_result.latest_filing_date}: {form8k_result.latest_filing_content}"
        summaries.append(base)

    if summaries:
        return " ".join(summaries)

    if coverage.missing_forms:
        missing_forms = ", ".join(coverage.missing_forms)
        return f"SEC coverage is partial in this run; missing requested form(s): {missing_forms}"

    for chunk in gold_ctx or []:
        if _source_type_value(chunk) == "sec":
            return str(_obj_get(chunk, "content", "") or "").strip() or "SEC evidence was retrieved in this run"
    return ""


def _options_narrative_summary(
    *,
    silver_values: Dict[str, Any],
    iv_regime: Dict[str, Any],
    posture_contract: Optional[Dict[str, Any]] = None,
    options_ticker: Optional[str] = None,
    options_bundle: Optional[Dict[str, Any]] = None,
) -> str:
    posture = posture_contract if isinstance(posture_contract, dict) else {}
    trace = dict(posture.get("posture_reasoning_trace") or {})
    ticker_label = (options_ticker or "").strip().upper() or "the options board"

    pcr_state = str(trace.get("pcr_state") or "").strip().lower()
    iv_regime_state = str(trace.get("iv_regime_state") or iv_regime.get("iv_regime") or "").strip().upper()
    iv_richness_state = str(trace.get("iv_richness_state") or "").strip().lower()
    skew_state = str(trace.get("skew_state") or "").strip().lower()
    liquidity_state = str(trace.get("liquidity_state") or "").strip().lower()
    market_impact_risk = str(
        trace.get("market_impact_risk")
        or (options_bundle or {}).get("market_impact_risk")
        or ""
    ).strip()

    pcr_status = str(silver_values.get("pcr_status") or "").strip()
    pcr_volume = silver_values.get("pcr_volume")
    pcr_oi = silver_values.get("pcr_open_interest")
    atm_iv = silver_values.get("latest_atm_iv")
    iv_rank = silver_values.get("latest_atm_iv_rank_pct")
    iv_skew = silver_values.get("latest_iv_skew")

    has_flow_evidence = pcr_volume is not None or pcr_oi is not None or bool(pcr_status) or pcr_state not in {"", "unknown"}
    flow_sentence = ""
    if pcr_state == "protection_heavy":
        flow_sentence = "Flow leans defensive, with put demand signaling more active downside hedging."
    elif pcr_state == "call_skewed":
        flow_sentence = "Flow leans constructive, with call-side activity outweighing heavier downside protection."
    elif pcr_status:
        flow_sentence = f"Flow looks broadly {pcr_status.lower()}, rather than pointing to a one-way hedge chase."
    elif has_flow_evidence:
        flow_sentence = "Flow is not showing a one-way hedge chase from the available put/call evidence."
    if pcr_volume is not None:
        if not flow_sentence:
            flow_sentence = "Put/call flow is available."
        flow_sentence += f" PCR volume is {pcr_volume:.3f}"
        if pcr_oi is not None:
            flow_sentence += f" and PCR open interest is {pcr_oi:.3f}"
        flow_sentence += "."

    iv_sentence = ""
    if atm_iv is not None:
        richness_text = {
            "cheap": "still relatively inexpensive",
            "mid_range": "sitting in a middle-of-the-range premium regime",
            "firm": "already firm rather than cheap",
            "rich": "already rich",
        }.get(iv_richness_state, "")
        regime_text = {
            "LOW": "low-volatility",
            "NORMAL": "normal-volatility",
            "HIGH": "high-volatility",
        }.get(iv_regime_state, "current")
        iv_sentence = f"{ticker_label} ATM IV is {atm_iv:.4f}"
        if iv_rank is not None:
            iv_sentence += f" with IV rank at {iv_rank:.2f}%"
        if richness_text:
            iv_sentence += f", leaving premium {richness_text} in a {regime_text} backdrop"
        iv_sentence += "."

    skew_sentence = ""
    if iv_skew is not None:
        if skew_state == "positive_put_premium":
            skew_sentence = f"IV skew is {iv_skew:.4f}, which points to puts carrying a visible premium over calls."
        elif skew_state == "negative_call_premium":
            skew_sentence = f"IV skew is {iv_skew:.4f}, which points to calls carrying the richer premium."
        else:
            skew_sentence = f"IV skew is {iv_skew:.4f}, so put-versus-call volatility pricing looks fairly balanced."

    liquidity_sentence = ""
    bundle = options_bundle or {}
    liquid_contracts = bundle.get("liquid_contracts")
    spread_pct = bundle.get("avg_spread_pct")
    executable_oi = bundle.get("executable_open_interest")
    has_liquidity_evidence = liquid_contracts is not None or spread_pct is not None or executable_oi is not None
    has_known_market_impact = market_impact_risk and market_impact_risk.lower() != "unknown"
    if has_liquidity_evidence or has_known_market_impact:
        if liquidity_state == "fragile":
            liquidity_sentence = "Execution looks fragile"
        elif has_known_market_impact and market_impact_risk in {"Low", "Medium"}:
            liquidity_sentence = "Liquidity looks healthy"
        else:
            liquidity_sentence = "Liquidity is bounded by the retrieved executable-market evidence"
        detail_bits: List[str] = []
        if liquid_contracts is not None:
            detail_bits.append(f"{int(liquid_contracts)} executable contracts")
        if executable_oi is not None:
            detail_bits.append(f"{int(executable_oi)} executable open interest")
        if spread_pct is not None:
            detail_bits.append(f"{float(spread_pct):.3f}% weighted spread")
        if has_known_market_impact:
            detail_bits.append(f"{market_impact_risk.lower()} market-impact risk")
        if detail_bits:
            liquidity_sentence += " through " + ", ".join(detail_bits)
        liquidity_sentence += "."

    parts = [flow_sentence, iv_sentence, skew_sentence, liquidity_sentence]
    return " ".join(part.strip() for part in parts if part.strip())


def _preferred_silver_context(state: Dict[str, Any]) -> Dict[str, Any]:
    return preferred_silver_context_from_state(state)


def _derive_query_family(metadata: Any, original_query: str, silver_values: Dict[str, Any], gold_ctx: List[Any]) -> str:
    structured_family = canonical_query_family(str(_obj_get(metadata, "query_family", "") or ""))
    if structured_family:
        return structured_family
    structured_theme = str(_obj_get(metadata, "primary_theme", "") or "").strip().lower()
    structured_surface = str(_obj_get(metadata, "primary_surface", "") or "").strip().lower()
    if structured_theme == "insider":
        return "insider_flow_driven"
    if structured_theme == "geopolitics":
        return "geopolitical_options_read" if structured_surface == "options_surface" else "geopolitical_macro_read"
    if structured_theme == "cross_asset":
        return "cross_asset_regime"
    metrics = [str(m).lower() for m in (_obj_get(metadata, "metrics", []) or [])]
    source_types = [str(s).lower() for s in (_obj_get(metadata, "source_types", []) or [])]
    signals = {str(s).strip().lower() for s in (_obj_get(metadata, "signals", []) or []) if str(s).strip()}
    query_l = (original_query or "").lower()

    has_sec = "sec" in source_types or any(_source_type_value(c) == "sec" for c in gold_ctx or [])
    explicit_gpr_intent = (
        "gpr" in source_types
        or "gpr index" in " ".join(metrics)
        or "gpr context" in signals
    )
    explicit_macro_news_narrative = (
        "news" in source_types
        and "macro_history" in source_types
        and "gpr" not in source_types
        and "macro regime narrative" in signals
        and "news narrative" in signals
    )
    has_macro = "macro_history" in source_types or any(m in metrics for m in ("macro trend", "price change (%)")) or any(
        k in silver_values for k in ("VIX_value", "DXY_value", "GSPC_value", "IXIC_value")
    )
    if has_sec:
        return "insider_flow_driven"
    if explicit_macro_news_narrative:
        return "cross_asset_regime"
    if explicit_gpr_intent:
        if "options" in source_types or any(m in metrics for m in ("implied volatility (iv)", "iv skew", "put/call ratio", "options liquidity")):
            return "geopolitical_options_read"
        return "geopolitical_macro_read"
    if has_macro and any(t in query_l for t in ("vix", "dxy", "hedge", "qqq", "spy")):
        return "cross_asset_regime"
    return "options_microstructure"


def _scope_summary(scope_contract: Dict[str, Any]) -> Dict[str, Any]:
    scope = scope_contract if isinstance(scope_contract, dict) else {}
    return {
        "query_family": scope.get("query_family"),
        "asset_scope": scope.get("asset_scope"),
        "read_profile": scope.get("read_profile"),
        "primary_ticker": scope.get("primary_ticker"),
        "analysis_mode": scope.get("analysis_mode"),
        "coverage_basis": scope.get("coverage_basis"),
        "requires_catalyst_confirmation": scope.get("requires_catalyst_confirmation"),
        "gold_context_optional": scope.get("gold_context_optional"),
        "hard_data_sufficient_for_answer": scope.get("hard_data_sufficient_for_answer"),
        "scope_status": scope.get("scope_status"),
        "in_scope_tickers": list(scope.get("in_scope_tickers") or []),
        "out_of_scope_tickers": list(scope.get("out_of_scope_tickers") or []),
        "refusal_reason": scope.get("refusal_reason"),
        "strict_sources": list(scope.get("strict_sources") or []),
        "soft_context_sources": list(scope.get("soft_context_sources") or []),
        "output_mode_ceiling": scope.get("output_mode_ceiling"),
        "specificity_ceiling": scope.get("specificity_ceiling"),
        "required_disclosures": list(scope.get("required_disclosures") or []),
        "query_slots": dict(scope.get("query_slots") or {}),
        "sec_action_taxonomy": dict(scope.get("sec_action_taxonomy") or {}),
        "news_coverage_status": str(scope.get("news_coverage_status") or ""),
        "background_only_read": bool(scope.get("background_only_read")),
        "retrieved_news_count": int(scope.get("retrieved_news_count") or 0),
        "supplemental_news_status": str(scope.get("supplemental_news_status") or ""),
        "supplemental_news_count": int(scope.get("supplemental_news_count") or 0),
    }


def _retrieval_outcome_summary(retrieval_outcome: Dict[str, Any]) -> Dict[str, Any]:
    outcome = retrieval_outcome if isinstance(retrieval_outcome, dict) else {}
    return {
        "scope_status": outcome.get("scope_status"),
        "in_scope_tickers": list(outcome.get("in_scope_tickers") or []),
        "out_of_scope_tickers": list(outcome.get("out_of_scope_tickers") or []),
        "refusal_reason": outcome.get("refusal_reason"),
        "strict_sources_hit": list(outcome.get("strict_sources_hit") or []),
        "soft_sources_hit": list(outcome.get("soft_sources_hit") or []),
        "missing_strict_sources": list(outcome.get("missing_strict_sources") or []),
        "missing_query_slots": list(outcome.get("missing_query_slots") or []),
        "sec_forms_requested": list(outcome.get("sec_forms_requested") or []),
        "sec_forms_retrieved": list(outcome.get("sec_forms_retrieved") or []),
        "sec_slot_hits": list(outcome.get("sec_slot_hits") or []),
        "sec_slot_missing": list(outcome.get("sec_slot_missing") or []),
        "sec_index_presence_mismatch": bool(outcome.get("sec_index_presence_mismatch")),
        "news_coverage_status": str(outcome.get("news_coverage_status") or ""),
        "background_only_read": bool(outcome.get("background_only_read")),
        "retrieved_news_count": int(outcome.get("retrieved_news_count") or 0),
        "supplemental_news_status": str(outcome.get("supplemental_news_status") or ""),
        "supplemental_news_count": int(outcome.get("supplemental_news_count") or 0),
        "has_gold_evidence": bool(outcome.get("has_gold_evidence")),
        "has_silver_evidence": bool(outcome.get("has_silver_evidence")),
        "is_fallback": bool(outcome.get("is_fallback")),
        "time_window_extended": bool(outcome.get("time_window_extended")),
        "time_window_defaulted": bool(outcome.get("time_window_defaulted")),
    }


def _insider_slot_caveat_from_outcome(retrieval_outcome: Dict[str, Any]) -> str:
    bundle = _sec_analysis_bundle(retrieval_outcome)
    coverage = bundle.coverage
    missing_slots = list(coverage.sec_slot_missing or [])
    if not missing_slots and not coverage.sec_forms_requested:
        return ""
    parts: List[str] = []
    if "8-K" in coverage.sec_forms_requested and "8-K" in coverage.sec_forms_retrieved:
        parts.append("SEC/8-K event filing evidence was retrieved for this run")
    if "4" in coverage.sec_forms_requested and "4" in coverage.sec_forms_retrieved:
        parts.append("SEC/Form-4 evidence was retrieved for this run")
    if "sec_insider_signal" in missing_slots:
        parts.append("SEC/Form-4 evidence was not retrieved, so insider selling / buying / vesting cannot be assessed reliably")
    if "sec_event_signal" in missing_slots:
        parts.append("SEC/8-K event filing evidence was not retrieved, so the filing-driven event risk cannot be assessed reliably")
    outcome = retrieval_outcome if isinstance(retrieval_outcome, dict) else {}
    if "options_liquidity_posture" in [str(s) for s in (outcome.get("missing_query_slots") or [])]:
        parts.append("options liquidity posture cannot be assessed reliably from this run")
    return "; ".join(parts)


def _sec_direct_read(bundle: SECAnalysisBundle) -> str:
    coverage = bundle.coverage
    parts: List[str] = []
    if "4" in coverage.sec_forms_retrieved:
        form4 = bundle.form4_analysis_result
        if form4.directional_read == "selling_pressure":
            text = f"Form 4 flow shows selling pressure across {form4.sell_count} filing(s)"
        elif form4.directional_read == "buying_support":
            text = f"Form 4 flow shows buying support across {form4.buy_count} filing(s)"
        elif form4.directional_read == "compensation_vesting":
            text = "Form 4 flow is mostly compensation-driven vesting"
        else:
            text = "Form 4 flow is mixed across selling, buying, and vesting"
        if form4.total_value:
            text += f", totaling about {_fmt_money(form4.total_value)}"
        parts.append(text)
    if "8-K" in coverage.sec_forms_retrieved:
        form8k = bundle.form8k_analysis_result
        tone_text = {
            "negative": "the 8-K set skews negative",
            "positive": "the 8-K set skews constructive",
        }.get(form8k.dominant_tone, "the 8-K set looks broadly neutral")
        category_text = f" around {', '.join(form8k.categories[:3])}" if form8k.categories else ""
        parts.append(f"8-K event evidence is present and {tone_text}{category_text}")
    return ". ".join(part.strip().rstrip(".") for part in parts if part.strip()).strip()


def _sec_missing_note(bundle: SECAnalysisBundle) -> str:
    if not bundle.missing_disclosure.has_missing:
        return ""
    missing_forms = ", ".join(bundle.missing_disclosure.missing_forms)
    if not missing_forms:
        return ""
    return f"Requested SEC coverage is partial in this run; missing form(s): {missing_forms}."


def _actual_strict_sources_hit(metadata: Any, silver_values: Dict[str, Any], gold_ctx: List[Any]) -> List[str]:
    hits: List[str] = []
    if silver_values:
        if any(
            k in silver_values for k in (
                "latest_atm_iv", "pcr_volume", "pcr_open_interest", "SPY_executable_option_volume",
                "AAPL_executable_option_volume", "QQQ_underlying_price", "GLD_executable_option_volume",
                "SLV_executable_option_volume",
            )
        ):
            hits.append("options")
        if any(k in silver_values for k in ("VIX_value", "DXY_value", "GSPC_value", "IXIC_value", "FEDFUNDS_value", "CPIAUCSL_value")):
            hits.append("macro_history")
        if "gpr_index_level" in silver_values:
            hits.append("gpr")

    for chunk in gold_ctx or []:
        raw_src = _obj_get(chunk, "source_type", "")
        src = str(getattr(raw_src, "value", raw_src) or "").lower()
        if src in {"sec", "gpr", "news"} and src not in hits:
            hits.append(src)

    requested = [str(s).lower() for s in (_obj_get(metadata, "source_types", []) or [])]
    if "options" in requested and "options" not in hits and silver_values:
        hits.append("options")
    return hits


_TICKER_METRIC_SUFFIXES = {
    "executable_option_volume",
    "executable_open_interest",
    "liquid_contracts",
    "market_impact_risk",
    "avg_spread_pct",
    "underlying_price",
}


def _available_ticker_prefixes(silver_values: Dict[str, Any]) -> List[str]:
    return liquidity_available_ticker_prefixes(silver_values)


def _preferred_ticker_prefixes(
    metadata: Any,
    original_query: str,
    silver_values: Dict[str, Any],
) -> List[str]:
    preferred: List[str] = []
    available = _available_ticker_prefixes(silver_values)
    query_tokens = {
        token.strip(" \t\r\n,;:!?()[]{}'\"").upper()
        for token in str(original_query or "").split()
    }

    for ticker in (_obj_get(metadata, "tickers", []) or []):
        ticker_u = str(ticker).upper().strip()
        if ticker_u and ticker_u not in preferred:
            preferred.append(ticker_u)

    for ticker in available:
        if ticker.upper() in query_tokens and ticker not in preferred:
            preferred.append(ticker)

    for ticker in available:
        if ticker not in preferred:
            preferred.append(ticker)

    return preferred


def _resolve_ticker_metric_bundle(
    silver_values: Dict[str, Any],
    preferred_tickers: List[str],
    suffixes: List[str],
) -> tuple[Optional[str], Dict[str, Any]]:
    candidates = list(dict.fromkeys(preferred_tickers + _available_ticker_prefixes(silver_values)))
    return resolve_first_available_ticker_bundle(silver_values, candidates, suffixes)


def _ticker_metric_label(ticker: Optional[str]) -> str:
    return f"{ticker} " if ticker else ""


def _format_liquidity_summary(
    *,
    ticker: Optional[str],
    bundle: Dict[str, Any],
) -> Optional[str]:
    parts: List[str] = []
    if bundle.get("executable_option_volume") is not None:
        parts.append(f"Executable Option Volume: {bundle.get('executable_option_volume')}")
    if bundle.get("executable_open_interest") is not None:
        parts.append(f"Executable Open Interest: {bundle.get('executable_open_interest')}")
    if bundle.get("liquid_contracts") is not None:
        parts.append(f"Executable Contract Count: {bundle.get('liquid_contracts')}")
    if bundle.get("avg_spread_pct") is not None:
        parts.append(f"Executable Weighted Spread (%): {bundle.get('avg_spread_pct')}")
    if bundle.get("market_impact_risk"):
        parts.append(f"Market Impact Risk (Executable Subset): {bundle.get('market_impact_risk')}")
    if not parts:
        return None
    return f"{_ticker_metric_label(ticker)}" + " | ".join(parts) + f" | {_EXECUTABLE_LIQUIDITY_NOTE}"


def _primary_ticker_market_context_line(
    *,
    metadata: Any,
    silver_values: Dict[str, Any],
) -> Optional[str]:
    tickers = [str(ticker).upper().strip() for ticker in (getattr(metadata, "tickers", None) or []) if str(ticker).strip()]
    if not tickers:
        return None
    ticker = tickers[0]
    value_candidates = [
        f"{ticker}_value",
        f"{ticker}_SPOT_value",
    ]
    change_candidates = [
        f"{ticker}_change_pct",
        f"{ticker}_SPOT_change_pct",
    ]
    value = next((silver_values.get(key) for key in value_candidates if silver_values.get(key) is not None), None)
    change = next((silver_values.get(key) for key in change_candidates if silver_values.get(key) is not None), None)
    if value is None and change is None:
        return None
    parts: List[str] = []
    if value is not None:
        parts.append(f"{ticker} price: {value}")
    if change is not None:
        parts.append(f"move: {change}%")
    return " | ".join(parts)


def _news_summary_lines(gold_ctx: List[Any], *, limit: int = 2) -> List[str]:
    summaries: List[str] = []
    for chunk in gold_ctx or []:
        if _source_type_value(chunk) != "news":
            continue
        metadata = _obj_get(chunk, "metadata", {}) or {}
        title = str(metadata.get("title") or metadata.get("original_title") or "").strip()
        source = str(metadata.get("source") or "").strip()
        publish_date = str(metadata.get("publish_date") or metadata.get("record_date") or "").strip()[:10]
        tone_score = metadata.get("llm_tone_score", metadata.get("tone_score"))
        volatility = str(metadata.get("volatility_implication") or "").strip()
        entities = [str(entity).strip() for entity in (metadata.get("entities") or []) if str(entity).strip()]
        tone_label = "neutral"
        if isinstance(tone_score, (int, float)):
            if tone_score > 0:
                tone_label = "positive"
            elif tone_score < 0:
                tone_label = "negative"
        fragments: List[str] = []
        if title:
            fragments.append(title)
        content = " ".join(str(_obj_get(chunk, "content", "") or "").split()).strip()
        if not title and content:
            snippet = content[:140].rstrip()
            if len(content) > 140:
                snippet += "..."
            fragments.append(snippet)
        meta_bits: List[str] = []
        if publish_date:
            meta_bits.append(publish_date)
        if source:
            meta_bits.append(source)
        meta_bits.append(f"tone={tone_label}")
        if volatility:
            meta_bits.append(f"vol={volatility}")
        if entities:
            meta_bits.append("entities=" + ", ".join(entities[:2]))
        if meta_bits:
            fragments.append("(" + "; ".join(meta_bits) + ")")
        summary = " ".join(fragment for fragment in fragments if fragment).strip()
        if not summary:
            continue
        if summary not in summaries:
            summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries


def _supplemental_news_context(state: Dict[str, Any]) -> List[Any]:
    return list(state.get("supplemental_news_context", []) or [])


def _geopolitical_background_lines(
    *,
    silver_values: Dict[str, Any],
    metadata: Any = None,
) -> List[str]:
    lines: List[str] = []
    primary_context = _primary_ticker_market_context_line(metadata=metadata, silver_values=silver_values)
    if primary_context:
        lines.append(primary_context)

    if silver_values.get("gpr_index_level") is not None or silver_values.get("gpr_trend") is not None:
        gpr_parts: List[str] = []
        if silver_values.get("gpr_index_level") is not None:
            gpr_parts.append(f"GPR Index Level: {silver_values.get('gpr_index_level')}")
        if silver_values.get("gpr_percentile") is not None:
            gpr_parts.append(f"percentile: {silver_values.get('gpr_percentile')}")
        if silver_values.get("gpr_trend"):
            gpr_parts.append(f"trend: {silver_values.get('gpr_trend')}")
        lines.append(" | ".join(gpr_parts))

    ordered_metrics = [
        ("GLD price", "GLD_value", "GLD_change_pct"),
        ("SLV price", "SLV_value", "SLV_change_pct"),
        ("VIX level", "VIX_value", "VIX_change_pct"),
        ("DXY level", "DXY_value", "DXY_change_pct"),
        ("S&P 500", "GSPC_value", "GSPC_change_pct"),
    ]
    for label, value_key, change_key in ordered_metrics:
        value = silver_values.get(value_key)
        change = silver_values.get(change_key) if change_key else None
        if value is None and change is None:
            continue
        parts: List[str] = []
        if value is not None:
            parts.append(f"{label}: {value}")
        if change is not None:
            parts.append(f"move: {change}%")
        lines.append(" | ".join(parts))

    deduped: List[str] = []
    for line in lines:
        clean = " ".join(str(line).split()).strip()
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped


def _cross_asset_backdrop_lines(
    *,
    silver_values: Dict[str, Any],
    metadata: Any = None,
) -> List[str]:
    lines: List[str] = []
    primary_context = _primary_ticker_market_context_line(metadata=metadata, silver_values=silver_values)
    if primary_context:
        lines.append(primary_context)

    ordered_metrics = [
        ("SLV price", "SLV_value", "SLV_change_pct"),
        ("VIX level", "VIX_value", "VIX_change_pct"),
        ("DXY level", "DXY_value", "DXY_change_pct"),
        ("S&P 500", "GSPC_value", "GSPC_change_pct"),
    ]
    for label, value_key, change_key in ordered_metrics:
        value = silver_values.get(value_key)
        change = silver_values.get(change_key) if change_key else None
        if value is None and change is None:
            continue
        parts: List[str] = []
        if value is not None:
            parts.append(f"{label}: {value}")
        if change is not None:
            parts.append(f"move: {change}%")
        lines.append(" | ".join(parts))

    deduped: List[str] = []
    for line in lines:
        clean = " ".join(str(line).split()).strip()
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped


def _build_analyst_evidence_lines(
    query_family: str,
    silver_values: Dict[str, Any],
    gold_ctx: List[Any],
    iv_regime: Dict[str, Any],
    *,
    supplemental_news_ctx: Optional[List[Any]] = None,
    metadata: Any = None,
    original_query: str = "",
    retrieval_outcome: Optional[Dict[str, Any]] = None,
) -> List[str]:
    query_family = canonical_query_family(query_family)
    lines: List[str] = []
    preferred_tickers = _preferred_ticker_prefixes(metadata, original_query, silver_values)
    options_ticker, options_bundle = _resolve_ticker_metric_bundle(
        silver_values,
        preferred_tickers,
        ["executable_option_volume", "executable_open_interest", "liquid_contracts", "avg_spread_pct", "market_impact_risk"],
    )
    if query_family == "options_microstructure":
        if silver_values.get("pcr_volume") is not None:
            lines.append(
                f"PCR volume: {silver_values.get('pcr_volume')} | PCR open interest: {silver_values.get('pcr_open_interest')} | PCR status: {silver_values.get('pcr_status')}"
            )
        if silver_values.get("latest_atm_iv") is not None:
            lines.append(
                f"{_ticker_metric_label(options_ticker)}ATM IV: {silver_values.get('latest_atm_iv')} | IV Rank Percentile: {silver_values.get('latest_atm_iv_rank_pct')}"
            )
        liquidity_summary = _format_liquidity_summary(ticker=options_ticker, bundle=options_bundle)
        if liquidity_summary:
            lines.append(liquidity_summary)
        lines.extend(_news_summary_lines(supplemental_news_ctx or gold_ctx, limit=2))
    elif query_family == "insider_flow_driven":
        sec_summaries = [_sec_feature_line(feature) for feature in _sec_analysis_features(retrieval_outcome)[:3]]
        if not sec_summaries:
            for chunk in gold_ctx or []:
                if _source_type_value(chunk) != "sec":
                    continue
                chunk_metadata = _obj_get(chunk, "metadata", {}) or {}
                action = str(_obj_get(chunk_metadata, "action_direction", "") or "").upper()
                content = str(_obj_get(chunk, "content", "") or "").strip()
                if content:
                    taxonomy = describe_sec_action_direction(action) if action in SEC_ACTION_TAXONOMY else ""
                    sec_summaries.append(
                        f"{content} ({action or 'UNKNOWN'}: {taxonomy})".strip()
                    )
                if len(sec_summaries) >= 2:
                    break
        lines.extend(sec_summaries)
        liquidity_summary = _format_liquidity_summary(ticker=options_ticker, bundle=options_bundle) if str(getattr(metadata, "primary_surface", "") or "").strip().lower() == "options_surface" else ""
        if liquidity_summary:
            lines.append(liquidity_summary)
    elif query_family == "cross_asset_regime":
        lines.extend(_cross_asset_backdrop_lines(silver_values=silver_values, metadata=metadata))
        lines.extend(_news_summary_lines(supplemental_news_ctx or gold_ctx, limit=2))
    elif query_family in {"geopolitical_macro_read", "geopolitical_options_read"}:
        lines.extend(_geopolitical_background_lines(silver_values=silver_values, metadata=metadata))
        if (
            silver_values.get("gpr_percentile") is not None
            and not any("GPR Index Level:" in line for line in lines)
            and silver_values.get("gpr_index_level") is not None
        ):
            lines.append(
                f"GPR Index Level: {silver_values.get('gpr_index_level')} | GPR Percentile: {silver_values.get('gpr_percentile')}"
            )
        lines.extend(_news_summary_lines(gold_ctx, limit=2))
        if silver_values.get("latest_atm_iv") is not None:
            lines.append(f"{_ticker_metric_label(options_ticker)}Current ATM IV: {silver_values.get('latest_atm_iv')}")
        liquidity_summary = _format_liquidity_summary(ticker=options_ticker, bundle=options_bundle)
        if liquidity_summary:
            lines.append(liquidity_summary)
    else:
        if silver_values.get("latest_atm_iv") is not None:
            lines.append(
                f"{_ticker_metric_label(options_ticker)}ATM IV: {silver_values.get('latest_atm_iv')} | IV Rank Percentile: {silver_values.get('latest_atm_iv_rank_pct')}"
            )
    if iv_regime.get("iv_regime") and iv_regime.get("iv_regime") != "UNKNOWN":
        lines.append(f"IV Regime: {iv_regime.get('iv_regime')}")
    return lines


def _build_analyst_conclusion(
    original_query: str,
    query_family: str,
    silver_values: Dict[str, Any],
    gold_ctx: List[Any],
    supplemental_news_ctx: Optional[List[Any]],
    iv_regime: Dict[str, Any],
    retrieval_outcome: Optional[Dict[str, Any]] = None,
    *,
    metadata: Any = None,
    posture_contract: Optional[Dict[str, Any]] = None,
) -> str:
    query_family = canonical_query_family(query_family)
    topic = " ".join((original_query or "the requested topic").split())
    primary_surface = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
    preferred_tickers = _preferred_ticker_prefixes(metadata, original_query, silver_values)
    options_ticker, options_bundle = _resolve_ticker_metric_bundle(
        silver_values,
        preferred_tickers,
        ["executable_option_volume", "executable_open_interest", "liquid_contracts", "avg_spread_pct", "market_impact_risk"],
    )
    if query_family == "options_microstructure":
        summary = _options_narrative_summary(
            silver_values=silver_values,
            iv_regime=iv_regime,
            posture_contract=posture_contract,
            options_ticker=options_ticker,
            options_bundle=options_bundle,
        )
        news_summaries = _news_summary_lines(supplemental_news_ctx or gold_ctx, limit=2)
        if summary:
            if news_summaries:
                return f"For {topic}, {summary} Supplemental news: " + " | ".join(news_summaries) + "."
            return f"For {topic}, {summary} No supplemental news was retrieved in-window, so the read stays anchored to the structured options evidence."
        return f"For {topic}, the current read is anchored to the checked options evidence available in this run."
    if query_family == "insider_flow_driven":
        sec_bundle = _sec_analysis_bundle(retrieval_outcome)
        outcome = retrieval_outcome if isinstance(retrieval_outcome, dict) else {}
        slot_caveat = _insider_slot_caveat_from_outcome(outcome)
        wants_options_surface = primary_surface == "options_surface"
        liquidity_parts: List[str] = []
        if wants_options_surface and options_bundle.get("executable_option_volume") is not None:
            liquidity_parts.append(f"executable option volume {options_bundle.get('executable_option_volume')}")
        if wants_options_surface and options_bundle.get("executable_open_interest") is not None:
            liquidity_parts.append(f"executable open interest {options_bundle.get('executable_open_interest')}")
        if wants_options_surface and options_bundle.get("avg_spread_pct") is not None:
            liquidity_parts.append(f"executable weighted spread {options_bundle.get('avg_spread_pct')}%")
        if wants_options_surface and options_bundle.get("market_impact_risk"):
            liquidity_parts.append(f"market impact risk on the executable subset {options_bundle.get('market_impact_risk')}")
        options_summary = (
            f"{_ticker_metric_label(options_ticker)}options liquidity shows " + " | ".join(liquidity_parts)
            if liquidity_parts
            else ("options liquidity posture is limited in this run" if wants_options_surface else "")
        )
        if slot_caveat:
            if options_summary:
                return f"For {topic}, {slot_caveat}; {options_summary}."
            return f"For {topic}, {slot_caveat}."
        sec_lead = _sec_direct_read(sec_bundle) or _sec_analysis_summary(retrieval_outcome, gold_ctx) or "SEC evidence is limited in this run"
        notes: List[str] = []
        missing_note = _sec_missing_note(sec_bundle)
        if missing_note:
            notes.append(missing_note)
        if wants_options_surface:
            if options_summary:
                notes.append(options_summary)
            else:
                notes.append("options posture cannot be assessed from this run")
        summary = f"For {topic}, {sec_lead}."
        if notes:
            summary += " " + " ".join(note if note.endswith(".") else f"{note}." for note in notes)
        return summary
    if query_family == "cross_asset_regime":
        regime = iv_regime.get("iv_regime", "UNKNOWN")
        fragments: List[str] = []
        for line in _cross_asset_backdrop_lines(silver_values=silver_values, metadata=metadata)[:5]:
            if line.replace(" | ", "; ") not in fragments:
                fragments.append(line.replace(" | ", "; "))
        news_summaries = _news_summary_lines(supplemental_news_ctx or gold_ctx, limit=2)
        if news_summaries:
            return (
                f"For {topic}, "
                + "; ".join(fragments[:5])
                + ". Supplemental macro news: "
                + " | ".join(news_summaries)
                + "."
            )
        if fragments:
            return f"For {topic}, " + "; ".join(fragments[:5]) + f". No supplemental macro news was retrieved in-window, so this read stays anchored to the structured macro backdrop; the pinned regime reads {regime}."
        return f"For {topic}, the pinned regime reads {regime} based on the checked macro and cross-asset evidence in this run."
    if query_family in {"geopolitical_macro_read", "geopolitical_options_read"}:
        outcome = retrieval_outcome if isinstance(retrieval_outcome, dict) else {}
        background_only_read = bool(outcome.get("background_only_read"))
        news_coverage_status = str(outcome.get("news_coverage_status") or "").strip().lower()
        fragments = []
        for line in _geopolitical_background_lines(silver_values=silver_values, metadata=metadata)[:6]:
            fragments.append(line.replace(" | ", "; "))
        news_summaries = _news_summary_lines(supplemental_news_ctx or gold_ctx, limit=2)
        if news_summaries:
            return f"For {topic}, " + "; ".join(fragments[:6]) + ". Supplemental macro news: " + " | ".join(news_summaries) + "."
        elif background_only_read and news_coverage_status == "no_fresh_news_retrieved" and fragments:
            return (
                f"For {topic}, no fresh geopolitical news was retrieved in the requested window; "
                + "; ".join(fragments)
                + "; this remains a background-only geopolitical read."
            )
        if query_family == "geopolitical_options_read" and silver_values.get("latest_atm_iv") is not None:
            fragments.append(f"{_ticker_metric_label(options_ticker)}ATM IV is {silver_values.get('latest_atm_iv')}")
        if fragments:
            return f"For {topic}, " + "; ".join(fragments) + "."
    return f"For {topic}, the current read is anchored to the checked structured evidence in this run."


_LIQUIDITY_EVIDENCE_SUFFIXES = [
    "executable_option_volume",
    "executable_open_interest",
    "liquid_contracts",
    "avg_spread_pct",
    "market_impact_risk",
]


def _is_macro_news_surface(metadata: Any) -> bool:
    primary_surface = str(_obj_get(metadata, "primary_surface", "") or "").strip().lower()
    source_types = {
        str(getattr(source, "value", source) or "").strip().lower()
        for source in (_obj_get(metadata, "source_types", []) or [])
        if str(getattr(source, "value", source) or "").strip()
    }
    return primary_surface == "macro_news_surface" and bool(source_types & {"news", "macro_history"})


def _missing_options_surface_note(
    *,
    state: Dict[str, Any],
    metadata: Any,
    silver_values: Dict[str, Any],
    retrieval_outcome: Dict[str, Any],
) -> str:
    missing_slots = {
        str(slot).strip()
        for slot in (retrieval_outcome or {}).get("missing_query_slots", [])
        if str(slot).strip()
    }
    primary_ticker = resolve_primary_ticker(
        state=state,
        metadata=metadata,
        scope_contract=state.get("scope_contract") or {},
    )
    _, liquidity_bundle = resolve_first_available_ticker_bundle(
        silver_values,
        [primary_ticker] if primary_ticker else [],
        _LIQUIDITY_EVIDENCE_SUFFIXES,
    )
    has_iv_skew = silver_values.get("latest_iv_skew") is not None
    has_liquidity_evidence = any(
        liquidity_bundle.get(key) is not None
        for key in ("executable_option_volume", "executable_open_interest", "liquid_contracts", "avg_spread_pct")
    )
    has_known_market_impact = str(liquidity_bundle.get("market_impact_risk") or "").strip() not in {"", "Unknown"}

    missing_bits: List[str] = []
    if not has_iv_skew or "iv_skew_signal" in missing_slots:
        missing_bits.append("IV skew")
    if not (has_liquidity_evidence or has_known_market_impact) or "liquidity_signal" in missing_slots:
        missing_bits.append("options liquidity posture")
    if not missing_bits:
        return ""
    if len(missing_bits) == 1:
        subject = missing_bits[0]
        verb = "was"
    else:
        subject = " and ".join(missing_bits)
        verb = "were"
    return f"{subject} {verb} not retrieved cleanly enough to assess execution risk."


def build_finalizer_input_card(
    state: Dict[str, Any],
    draft: str,
    iv_regime: Dict[str, Any],
) -> Dict[str, Any]:
    metadata = state.get("metadata")
    scope_contract = state.get("scope_contract") or {}
    retrieval_outcome = state.get("retrieval_outcome") or {}
    gold_ctx = state.get("gold_context", []) or []
    supplemental_news_ctx = _supplemental_news_context(state)
    silver_ctx = _preferred_silver_context(state)
    silver_values = silver_ctx.get("values") or {}
    query_family = canonical_query_family(str(scope_contract.get("query_family") or "").strip()) or _derive_query_family(
        metadata,
        state.get("original_query", ""),
        silver_values,
        gold_ctx,
    )
    evidence_ids = _collect_valid_citation_ids(silver_ctx, gold_ctx, supplemental_news_ctx)
    query_slots = dict((scope_contract or {}).get("query_slots") or {})
    if not query_slots and query_family == "insider_flow_driven":
        query_slots = dict(INSIDER_FLOW_QUERY_SLOTS)
    sec_action_taxonomy = (
        dict((scope_contract or {}).get("sec_action_taxonomy") or {})
        if query_family == "insider_flow_driven"
        else {}
    )
    if query_family == "insider_flow_driven" and not sec_action_taxonomy:
        sec_action_taxonomy = dict(SEC_ACTION_TAXONOMY)
    actionability_mode = state.get("actionability_mode")
    structure_visibility_mode = state.get("structure_visibility_mode")
    revision_constraints = dict(state.get("revision_constraints") or {})
    posture_contract = derive_posture_contract(
        silver_values,
        iv_regime,
        scope_contract,
        state.get("recommendation_mode") or actionability_mode or "directional_watchlist",
        state=state,
        metadata=metadata,
    )
    sec_bundle = _sec_analysis_bundle(retrieval_outcome)
    analyst_evidence_lines = _build_analyst_evidence_lines(
        query_family,
        silver_values,
        gold_ctx,
        iv_regime,
        supplemental_news_ctx=supplemental_news_ctx,
        metadata=metadata,
        original_query=state.get("original_query", ""),
        retrieval_outcome=retrieval_outcome,
    )
    analyst_conclusion = _build_analyst_conclusion(
        state.get("original_query", ""),
        query_family,
        silver_values,
        gold_ctx,
        supplemental_news_ctx,
        iv_regime,
        retrieval_outcome,
        metadata=metadata,
        posture_contract=posture_contract,
    )
    narrative_brief = build_narrative_brief(
        query_family=query_family,
        original_query=state.get("original_query", ""),
        silver_values=silver_values,
        gold_context=gold_ctx,
        supplemental_news_context=supplemental_news_ctx,
        metadata=metadata,
        posture_contract=posture_contract,
        retrieval_outcome=retrieval_outcome,
    )
    narrative_brief_payload = narrative_brief.model_dump()
    sec_signal_summary = _sec_direct_read(sec_bundle)
    sec_missing_note = _sec_missing_note(sec_bundle)
    macro_news_surface = _is_macro_news_surface(metadata)
    missing_options_note = _missing_options_surface_note(
        state=state,
        metadata=metadata,
        silver_values=silver_values,
        retrieval_outcome=retrieval_outcome,
    )

    def _compact_sentence(text: str) -> str:
        return " ".join(str(text or "").split()).strip()

    def _topic_stripped(sentence: str) -> str:
        clean = _compact_sentence(sentence)
        topic = " ".join((state.get("original_query", "") or "the requested topic").split())
        prefix = f"For {topic}, "
        if clean.startswith(prefix):
            return clean[len(prefix):].strip()
        return clean

    def _render_safe_macro_summary_seed(values: Dict[str, Any]) -> str:
        if macro_news_surface or query_family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
            parts = [
                narrative_brief.news_driver,
                narrative_brief.macro_transmission,
                narrative_brief.game_theory_read,
            ]
            return " ".join(part for part in parts if part).strip()
        macro_bits: List[str] = []
        if bool((retrieval_outcome or {}).get("background_only_read")):
            for line in _geopolitical_background_lines(silver_values=values, metadata=metadata)[:6]:
                macro_bits.append(line.replace(" | ", "; "))
            news_bits = _news_summary_lines(supplemental_news_ctx, limit=2)
            if news_bits:
                macro_bits.append("supplemental macro news: " + " | ".join(news_bits))
        else:
            backdrop_lines = (
                _cross_asset_backdrop_lines(silver_values=values, metadata=metadata)
                if query_family == "cross_asset_regime"
                else _geopolitical_background_lines(silver_values=values, metadata=metadata)
            )
            for line in backdrop_lines[:5]:
                macro_bits.append(line.replace(" | ", "; "))
        if not macro_bits:
            return ""
        return "Broader market backdrop: " + "; ".join(macro_bits[:4]) + "."

    def _render_safe_asset_read_seed() -> str:
        if macro_news_surface or query_family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
            base = " ".join(
                part for part in (narrative_brief.asset_reaction, narrative_brief.volatility_setup) if part
            ).strip()
            return " ".join(part for part in (base, missing_options_note) if part).strip()
        fragments: List[str] = []
        posture_rationale = _compact_sentence(posture_contract.get("posture_rationale", ""))
        base_regime_read = _compact_sentence(posture_contract.get("base_regime_read", ""))
        if query_family == "options_microstructure":
            return " ".join(
                _compact_sentence(line).rstrip(".") + "."
                for line in analyst_evidence_lines[:3]
                if _compact_sentence(line)
            ).strip()
        if posture_rationale:
            fragments.append(posture_rationale)
        if base_regime_read:
            fragments.append(base_regime_read)
        if analyst_evidence_lines:
            line_limit = 5 if bool((retrieval_outcome or {}).get("background_only_read")) and query_family == "geopolitical_macro_read" else 3
            safe_lines = [
                _compact_sentence(line).rstrip(".") + "."
                for line in analyst_evidence_lines[:line_limit]
                if _compact_sentence(line)
            ]
            fragments.extend(safe_lines)
        return " ".join(fragment.strip() for fragment in fragments if fragment.strip()).strip()

    def _render_safe_asset_read_narrative_seed() -> str:
        if macro_news_surface:
            base = _compact_sentence(
                " ".join(part for part in (narrative_brief.asset_reaction, narrative_brief.volatility_setup) if part)
            )
            return " ".join(part for part in (base, missing_options_note) if part).strip()
        if query_family == "options_microstructure":
            preferred_tickers = _preferred_ticker_prefixes(metadata, state.get("original_query", ""), silver_values)
            options_ticker, options_bundle = _resolve_ticker_metric_bundle(
                silver_values,
                preferred_tickers,
                ["executable_option_volume", "executable_open_interest", "liquid_contracts", "avg_spread_pct", "market_impact_risk"],
            )
            return _compact_sentence(
                _options_narrative_summary(
                    silver_values=silver_values,
                    iv_regime=iv_regime,
                    posture_contract=posture_contract,
                    options_ticker=options_ticker,
                    options_bundle=options_bundle,
                )
            )
        if query_family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
            return _compact_sentence(
                " ".join(part for part in (narrative_brief.asset_reaction, narrative_brief.volatility_setup) if part)
            )
        return _render_safe_asset_read_seed()

    if macro_news_surface or query_family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
        direct_answer_seed = _compact_sentence(
            " ".join(
                part for part in (
                    narrative_brief.headline_read,
                    narrative_brief.risk_trigger or narrative_brief.risk_read,
                )
                if part
            )
        )
    else:
        direct_answer_seed = _compact_sentence(analyst_conclusion)
    posture_takeaway = _compact_sentence(posture_contract.get("posture_takeaway", ""))
    if (
        posture_takeaway
        and direct_answer_seed
        and not macro_news_surface
        and query_family not in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}
    ):
        direct_answer_seed = f"{posture_takeaway} {_topic_stripped(direct_answer_seed)}".strip()
    elif posture_takeaway and not macro_news_surface and query_family not in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
        direct_answer_seed = posture_takeaway

    render_safety_contract = RenderSafetyContract(
        direct_answer_seed=direct_answer_seed,
        direct_answer_includes_posture_takeaway=bool(posture_takeaway) and not macro_news_surface,
        direct_answer_includes_missing_slot_disclosure=bool(_insider_slot_caveat_from_outcome(retrieval_outcome or {}))
        or bool((retrieval_outcome or {}).get("background_only_read")),
        macro_summary_seed=_render_safe_macro_summary_seed(silver_values),
        asset_read_narrative_seed=_render_safe_asset_read_narrative_seed(),
        asset_read_seed=_render_safe_asset_read_seed(),
        risk_seed=_compact_sentence(
            " ".join(part for part in (narrative_brief.risk_trigger, narrative_brief.what_would_change) if part)
            if macro_news_surface or query_family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}
            else posture_contract.get("escalation_risk_read", "")
        ),
        summary_caveat_seed="",
        recommendation_mode_seed=str(
            state.get("recommendation_mode") or actionability_mode or "directional_watchlist"
        ).strip(),
        status_note="",
        status_note_severity="none",
        safe_for_frontend=True,
    ).model_dump()
    return {
        "query_family": query_family,
        "strict_sources_hit": list(retrieval_outcome.get("strict_sources_hit") or []) or _actual_strict_sources_hit(metadata, silver_values, gold_ctx),
        "key_numbers": dict(silver_values),
        "required_silver_anchors": evidence_ids["silver_ids"],
        "required_gold_refs": evidence_ids["gold_ids"],
        "supplemental_news_count": int(len(supplemental_news_ctx)),
        "supplemental_news_lines": _news_summary_lines(supplemental_news_ctx, limit=5),
        "sec_signal_summary": sec_signal_summary,
        "sec_missing_note": sec_missing_note,
        "asset_options_narrative": _render_safe_asset_read_narrative_seed(),
        "asset_options_evidence_lines": analyst_evidence_lines,
        "analyst_evidence_lines": analyst_evidence_lines,
        "analyst_conclusion": analyst_conclusion,
        "narrative_brief": narrative_brief_payload,
        "narrative_brief_block": render_narrative_brief_block(narrative_brief),
        "recommendation_mode": state.get("recommendation_mode"),
        "actionability_mode": actionability_mode,
        "structure_visibility_mode": structure_visibility_mode,
        "revision_constraints": revision_constraints,
        "minor_edits": [],
        "fallback_status": _retrieval_outcome_summary(retrieval_outcome),
        "time_window": state.get("time_range"),
        "macro_backdrop": state.get("macro_context", "") or "",
        "scope_contract_summary": _scope_summary(scope_contract),
        "retrieval_outcome_summary": _retrieval_outcome_summary(retrieval_outcome),
        "missing_query_slots": list((retrieval_outcome or {}).get("missing_query_slots") or []),
        "news_coverage_status": str((retrieval_outcome or {}).get("news_coverage_status") or ""),
        "background_only_read": bool((retrieval_outcome or {}).get("background_only_read")),
        "retrieved_news_count": int((retrieval_outcome or {}).get("retrieved_news_count") or 0),
        "query_slots": query_slots,
        "slot_evidence_contracts": dict((scope_contract or {}).get("slot_evidence_contracts") or {}),
        "sec_action_taxonomy": sec_action_taxonomy,
        "posture_label": posture_contract.get("posture_label", ""),
        "posture_takeaway": posture_contract.get("posture_takeaway", ""),
        "posture_rationale": posture_contract.get("posture_rationale", ""),
        "base_regime_read": posture_contract.get("base_regime_read", ""),
        "escalation_risk_archetype": posture_contract.get("escalation_risk_archetype", ""),
        "escalation_risk_read": posture_contract.get("escalation_risk_read", ""),
        "posture_reasoning_trace": dict(posture_contract.get("posture_reasoning_trace") or {}),
        "analyst_contract_audit": dict(state.get("analyst_contract_audit") or {}),
        "render_safety_contract": render_safety_contract,
    }


def _missing_strict_sources(state: Dict[str, Any]) -> List[str]:
    outcome = state.get("retrieval_outcome") or {}
    if not isinstance(outcome, dict):
        return []
    return [str(item).strip() for item in (outcome.get("missing_strict_sources") or []) if str(item).strip()]


def _missing_query_slots(state: Dict[str, Any]) -> List[str]:
    outcome = state.get("retrieval_outcome") or {}
    if not isinstance(outcome, dict):
        return []
    return [str(item).strip() for item in (outcome.get("missing_query_slots") or []) if str(item).strip()]


def _source_needles(source_name: str) -> List[str]:
    src = str(source_name or "").strip().lower()
    mapping = {
        "sec": ["sec", "form 4", "form-4"],
        "options": ["options", "options chain", "options board"],
        "macro_history": ["macro", "history", "macro history"],
        "gpr": ["gpr", "geopolitical risk"],
        "news": ["news"],
    }
    return mapping.get(src, [src.replace("_", " ")])


def _slot_label(slot_name: str, state: Dict[str, Any]) -> str:
    scope_contract = state.get("scope_contract") or {}
    query_slots = dict(scope_contract.get("query_slots") or {}) if isinstance(scope_contract, dict) else {}
    if query_slots.get(slot_name):
        return str(query_slots[slot_name])
    family_slots = query_slots_for_family(_current_query_family(state))
    if family_slots.get(slot_name):
        return str(family_slots[slot_name])
    if slot_name in INSIDER_FLOW_QUERY_SLOTS:
        return str(INSIDER_FLOW_QUERY_SLOTS[slot_name])
    return str(slot_name).replace("_", " ")


def _current_query_family(state: Dict[str, Any]) -> str:
    scope_contract = state.get("scope_contract") or {}
    if isinstance(scope_contract, dict) and scope_contract.get("query_family"):
        return str(scope_contract.get("query_family")).strip()
    silver_ctx = _preferred_silver_context(state)
    silver_values = silver_ctx.get("values") or {}
    return _derive_query_family(
        state.get("metadata"),
        state.get("original_query", ""),
        silver_values,
        state.get("gold_context", []) or [],
    )


def _first_available_value(silver_values: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = silver_values.get(key)
        if value is not None:
            return value
    return None


def _slot_evidence_contracts(state: Dict[str, Any]) -> Dict[str, Any]:
    scope_contract = state.get("scope_contract") or {}
    if isinstance(scope_contract, dict) and scope_contract.get("slot_evidence_contracts"):
        return dict(scope_contract.get("slot_evidence_contracts") or {})

    query_family = _current_query_family(state)
    scope_slots = dict(scope_contract.get("query_slots") or {}) if isinstance(scope_contract, dict) else {}
    capability_profile = dict(state.get("data_capability_profile") or {})
    return build_slot_evidence_contracts(
        query_family=query_family,
        query_slots=scope_slots,
        capability_profile=capability_profile,
    )


def _render_required_evidence_budget(state: Dict[str, Any]) -> str:
    slot_contracts = _slot_evidence_contracts(state)
    return render_slot_evidence_contract_block(slot_contracts)


def _section_body(draft: str, heading: str) -> str:
    if not draft:
        return ""
    lines = draft.splitlines()
    capture = False
    body: List[str] = []
    target = heading.strip().lower()
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.lower() == target:
            capture = True
            continue
        if capture and stripped.startswith("## "):
            break
        if capture:
            body.append(raw_line)
    return "\n".join(body).strip()


def _required_evidence_audit(draft: str, state: Dict[str, Any]) -> Dict[str, Any]:
    evidence_body = _section_body(draft, "## Key Evidence")
    scope_contract = state.get("scope_contract") or {}
    silver_ctx = _preferred_silver_context(state)
    retrieval_outcome = state.get("retrieval_outcome") or {}
    query_family = _current_query_family(state)
    slot_contracts = _slot_evidence_contracts(state)
    coverage = evaluate_slot_coverage(
        slot_contracts=slot_contracts,
        draft=evidence_body if evidence_body else (draft or ""),
        retrieval_outcome=retrieval_outcome if isinstance(retrieval_outcome, dict) else {},
        silver_values=dict(silver_ctx.get("values") or {}),
        gold_ctx=list(state.get("gold_context", []) or []),
    )
    retrieval_support = evaluate_retrieval_slot_support(
        slot_contracts=slot_contracts,
        retrieval_outcome=retrieval_outcome if isinstance(retrieval_outcome, dict) else {},
        silver_values=dict(silver_ctx.get("values") or {}),
        gold_ctx=list(state.get("gold_context", []) or []),
    )
    if (
        canonical_query_family(query_family) == "options_microstructure"
        and retrieval_support.get("hard_gate_pass")
        and not list((retrieval_outcome or {}).get("missing_query_slots") or [])
    ):
        retrieval_slot_status = dict(retrieval_support.get("slot_status") or {})
        merged_slot_status = dict(coverage.get("slot_status") or {})
        for slot_name, slot_state in retrieval_slot_status.items():
            if slot_state == "retrieved" and merged_slot_status.get(slot_name) in {"incomplete", "unsupported_by_retrieval"}:
                merged_slot_status[slot_name] = "answered"
        coverage["slot_status"] = merged_slot_status
        coverage["required_evidence_count_met"] = True
        coverage["hard_gate_pass"] = True
        coverage["required_evidence_keys_missing"] = []
        coverage["required_evidence_keys_present"] = list(merged_slot_status.keys())
        coverage["coverage_score"] = max(float(coverage.get("coverage_score") or 0.0), float(retrieval_support.get("retrieval_support_score") or 1.0))
    coverage["retrieval_slot_status"] = dict(retrieval_support.get("slot_status") or {})
    coverage["retrieval_hard_gate_pass"] = bool(retrieval_support.get("hard_gate_pass"))
    coverage["query_family"] = str(scope_contract.get("query_family") or query_family)
    return coverage


def _has_main_risk_fact(draft: str, state: Dict[str, Any]) -> bool:
    body = _section_body(draft, "## Main Risk")
    if body:
        return True
    risk_text = str((state.get("revision_constraints") or {}).get("main_risk_text") or "").strip().lower()
    draft_l = (draft or "").lower()
    if risk_text and risk_text in draft_l:
        return True
    return "market impact risk" in draft_l


def _render_analyst_output_contract_block(state: Dict[str, Any]) -> str:
    missing_sources = _missing_strict_sources(state)
    missing_slots = _missing_query_slots(state)
    silver_ctx = _preferred_silver_context(state)
    silver_values = dict(silver_ctx.get("values") or {})
    posture_contract = derive_posture_contract(
        silver_values,
        state.get("iv_regime_pinned") or {},
        state.get("scope_contract") or {},
        state.get("recommendation_mode") or state.get("actionability_mode") or "directional_watchlist",
        state=state,
        metadata=state.get("metadata"),
    )
    lines = [
        "=== ANALYST OUTPUT CONTRACT ===",
        "Use these exact section headings in this exact order:",
        "1. ## Direct Read",
        "2. ## Key Evidence",
        "3. ## Main Risk",
        "4. ## Missing Evidence / Limits",
        "5. ## Structure Hint (only if a structure hint is warranted)",
        "",
        "Hard obligations:",
        "- Key Evidence is the main payload: carry the most query-relevant supported numbers and anchors before adding interpretation.",
        "- In Key Evidence, include only supported evidence and never print placeholder values such as None, null, N/A-as-data, or unavailable-as-data.",
        "- Main Risk must state a concrete risk fact.",
        "- If any strict source is missing, Missing Evidence / Limits must explicitly name the missing source family.",
        "- If any query slot is missing, Missing Evidence / Limits must explicitly name what cannot be assessed reliably.",
        "- At most one structure hint is allowed, and it should stay analytical.",
    ]
    if posture_contract.get("requires_posture_takeaway"):
        lines.append("- Direct Read must state the final qualitative posture, not just list PCR / IV / skew / liquidity values.")
        lines.append("- Key Evidence should describe the current priced state from the core options metrics, and Main Risk should describe the next adverse transition from that state.")
    lines.append(_render_required_evidence_budget(state))
    if missing_sources:
        lines.append("Missing strict sources that must be named: " + ", ".join(missing_sources))
    if missing_slots:
        slot_labels = [_slot_label(slot, state) for slot in missing_slots]
        lines.append("Missing query slots that must be named: " + ", ".join(slot_labels))
    if posture_contract.get("requires_posture_takeaway"):
        lines.append(render_posture_contract_block(posture_contract))
    return "\n".join(lines)


def _has_missing_source_disclosure(draft: str, state: Dict[str, Any]) -> bool:
    missing_sources = _missing_strict_sources(state)
    if not missing_sources:
        return True
    draft_l = (draft or "").lower()
    for source_name in missing_sources:
        needles = _source_needles(source_name)
        if not any(needle in draft_l for needle in needles):
            return False
    return True


def _has_missing_slot_disclosure(draft: str, state: Dict[str, Any]) -> bool:
    missing_slots = _missing_query_slots(state)
    if not missing_slots:
        return True
    draft_l = (draft or "").lower()
    for slot_name in missing_slots:
        label = _slot_label(slot_name, state).lower()
        if label not in draft_l and slot_name.replace("_", " ").lower() not in draft_l:
            return False
    return True


def _has_none_placeholder_leak(draft: str) -> bool:
    draft_l = (draft or "").lower()
    patterns = (
        r":\s*" + _BAD_PLACEHOLDER_PAT + r"\b",
        r"\|\s*" + _BAD_PLACEHOLDER_PAT + r"\s*\|",
        r"\bnone-valued\b",
        r"\b(?:iv|pcr|volume|open\s+interest|liquid\s+contracts|spread)\b[^.\n]{0,40}\b"
        + _BAD_PLACEHOLDER_PAT + r"\b",
    )
    return any(re.search(pattern, draft_l) for pattern in patterns)


def _has_live_recommendation_phrase(draft: str) -> bool:
    draft_l = (draft or "").lower()
    return any(re.search(pattern, draft_l) for pattern in _LIVE_RECOMMENDATION_PATTERNS)


def _append_missing_limits_block(draft: str, state: Dict[str, Any]) -> str:
    missing_sources = _missing_strict_sources(state)
    missing_slots = _missing_query_slots(state)
    if not missing_sources and not missing_slots:
        return draft

    missing_lines: List[str] = []
    if missing_sources:
        missing_lines.append("Missing source(s): " + ", ".join(missing_sources) + ".")
    if missing_slots:
        slot_labels = [_slot_label(slot, state) for slot in missing_slots]
        missing_lines.append("Cannot assess reliably from this run: " + ", ".join(slot_labels) + ".")

    if not missing_lines:
        return draft

    if "## Missing Evidence / Limits" in draft:
        existing = draft
        for line in missing_lines:
            if line.lower() not in existing.lower():
                existing = existing.rstrip() + "\n" + line
        return existing

    return draft.rstrip() + "\n\n## Missing Evidence / Limits\n" + "\n".join(missing_lines)


def _normalize_analyst_draft(draft: str, state: Dict[str, Any]) -> str:
    if not draft:
        return draft

    heading_map = {
        "## quantitative evidence": "## Key Evidence",
        "## evidence": "## Key Evidence",
        "## key risk": "## Main Risk",
        "## risk": "## Main Risk",
        "## recommendation": "## Structure Hint",
        "## proposed strategy": "## Structure Hint",
        "## strategy": "## Structure Hint",
    }

    cleaned_lines: List[str] = []
    for raw_line in draft.splitlines():
        line = raw_line.rstrip()
        normalized_heading = heading_map.get(line.strip().lower())
        if normalized_heading:
            cleaned_lines.append(normalized_heading)
            continue
        lowered = line.lower()
        if re.search(r"\billustrative\s+only\b", lowered) or any(
            token in lowered for token in ("not a current recommendation", "not a live recommendation")
        ):
            # Accept any trailing punctuation after "illustrative only" (colon,
            # comma, dash, em-dash, en-dash, or bare whitespace) so LLM
            # reformulations like "Illustrative only —" or "Illustrative only,"
            # are normalised the same way as the canonical "Illustrative only:".
            line = re.sub(r"(?i)\billustrative\s+only\b[:\s,.\-\u2014\u2013]*", "Structure hint: ", line).strip()
            line = re.sub(r"(?i)\bnot a current recommendation\b", "", line).strip(" .;:-")
            line = re.sub(r"(?i)\bnot a live recommendation\b", "", line).strip(" .;:-")
            if not line:
                continue
        if (
            re.search(r":\s*" + _BAD_PLACEHOLDER_PAT + r"\b", lowered)
            or re.search(r"\|\s*" + _BAD_PLACEHOLDER_PAT + r"\s*\|", lowered)
        ):
            continue
        cleaned_lines.append(line)

    normalized = "\n".join(cleaned_lines).strip()
    normalized = _append_missing_limits_block(normalized, state)
    return normalized


def _validate_analyst_contract(draft: str, state: Dict[str, Any]) -> Dict[str, Any]:
    missing_source_ok = _has_missing_source_disclosure(draft, state)
    missing_slot_ok = _has_missing_slot_disclosure(draft, state)
    none_leak = _has_none_placeholder_leak(draft)
    main_risk_ok = _has_main_risk_fact(draft, state)
    live_phrase = _has_live_recommendation_phrase(draft)
    evidence_audit = _required_evidence_audit(draft, state)
    violations: List[str] = []
    if not missing_source_ok:
        violations.append("missing_source_disclosure_missing")
    if not missing_slot_ok:
        violations.append("missing_slot_disclosure_missing")
    if none_leak:
        violations.append("none_placeholder_leak")
    if not main_risk_ok:
        violations.append("main_risk_missing")
    if not evidence_audit.get("required_evidence_count_met", True):
        violations.append("required_evidence_incomplete")
    return {
        "missing_source_disclosure_present": missing_source_ok,
        "missing_slot_disclosure_present": missing_slot_ok,
        "none_placeholder_leak": none_leak,
        "main_risk_present": main_risk_ok,
        "live_recommendation_phrase": live_phrase,
        "violations": violations,
        "missing_sources_expected": _missing_strict_sources(state),
        "missing_slots_expected": _missing_query_slots(state),
        "query_family": evidence_audit.get("query_family"),
        "slot_status": dict(evidence_audit.get("slot_status") or {}),
        "required_evidence_keys_present": evidence_audit.get("required_evidence_keys_present", []),
        "required_evidence_keys_missing": evidence_audit.get("required_evidence_keys_missing", []),
        "required_evidence_count_met": evidence_audit.get("required_evidence_count_met", True),
        "coverage_score": evidence_audit.get("coverage_score", 1.0),
        "hard_gate_pass": evidence_audit.get("hard_gate_pass", True),
    }


def _render_revision_constraints_block(state: Dict[str, Any]) -> str:
    constraints = dict(state.get("revision_constraints") or {})
    if not constraints:
        return "(none)"

    lines = [
        f"actionability_mode: {constraints.get('actionability_mode') or state.get('recommendation_mode') or 'unknown'}",
        f"structure_visibility_mode: {constraints.get('structure_visibility_mode') or 'unknown'}",
    ]
    if constraints.get("forbid_actionable_recommendation"):
        lines.append("- Keep any structure mention analytical and evidence-backed rather than turning it into the centerpiece of the draft.")
    if constraints.get("allow_illustrative_structure"):
        lines.append("- You MAY keep at most one structure hint, but keep the draft evidence-first and save compliance phrasing for the Finalizer.")
    if constraints.get("must_disclose_risk"):
        lines.append("- You SHOULD identify the main risk clearly as a risk fact.")
    if constraints.get("must_disclose_missing_slots"):
        lines.append("- You MUST state which missing source or query slot prevents a reliable answer.")
    if constraints.get("illustrative_structure_hint"):
        lines.append(f"- Preferred structure hint only: {constraints.get('illustrative_structure_hint')}")
    if constraints.get("main_risk_text"):
        lines.append(f"- Main risk to preserve analytically: {constraints.get('main_risk_text')}")
    if constraints.get("what_must_change"):
        lines.append(f"- What would need to change analytically: {constraints.get('what_must_change')}")
    if constraints.get("why_not_now_text"):
        lines.append(f"- Why not now analytically: {constraints.get('why_not_now_text')}")
    return "\n".join(lines)


class AnalystResult:
    """Typed envelope for the analyst node's multi-field return.

    Using a tiny structured return instead of a raw string lets the router
    plumb `iv_regime_pinned` into AgentState without awkwardly breaking
    backwards-compat with the Markdown-only contract (a plain string is
    still accessible via `.draft`).
    """

    __slots__ = ("draft", "iv_regime", "used_fallback", "model_used", "contract_audit")

    def __init__(
        self,
        draft: str,
        iv_regime: Dict[str, Any],
        used_fallback: bool = False,
        model_used: str = "",
        contract_audit: Optional[Dict[str, Any]] = None,
    ):
        self.draft = draft
        self.iv_regime = iv_regime
        self.used_fallback = used_fallback
        self.model_used = model_used
        self.contract_audit = contract_audit or {}


# ==========================================
# IV Regime Classifier (deterministic, no LLM)
# ==========================================
# Percentile-based thresholds (cross-asset stable) from Silver iv-rank.
_IV_RANK_HIGH_PCT_DEFAULT = float(os.getenv("IV_RANK_HIGH_PCT", "70"))
_IV_RANK_LOW_PCT_DEFAULT = float(os.getenv("IV_RANK_LOW_PCT", "30"))


def _infer_iv_regime(
    silver_context: Dict[str, Any],
    high_pct: float = _IV_RANK_HIGH_PCT_DEFAULT,
    low_pct: float = _IV_RANK_LOW_PCT_DEFAULT,
) -> Dict[str, Any]:
    """Classify the current IV regime from silver_context.values.

    Inspects the `latest_atm_iv` + `latest_atm_iv_rank_pct` keys written by
    `Scripts/retrieval/sql_tools._handle_options_analysis`. Also surfaces
    Put/Call Ratio when present because PCR drives the regime narrative.

    Returns a stable dict the LLM prompt can consume verbatim:
        {
          "iv_regime": "HIGH" | "LOW" | "NORMAL" | "UNKNOWN",
          "atm_iv": float | None,
          "iv_rank_pct": float | None,
          "pcr_volume": float | None,
          "pcr_status": str | None,
          "thresholds": {"high_pct": ..., "low_pct": ...}
        }

    Classification rule:
      - HIGH   when iv_rank_pct >= high_pct
      - LOW    when iv_rank_pct <= low_pct
      - NORMAL otherwise
      - UNKNOWN if iv_rank_pct is absent/non-numeric
    """
    values = (silver_context or {}).get("values", {}) if isinstance(silver_context, dict) else {}
    atm_iv = values.get("latest_atm_iv")
    iv_rank_pct = values.get("latest_atm_iv_rank_pct")
    pcr_vol = values.get("pcr_volume")
    pcr_status = values.get("pcr_status")

    if not isinstance(iv_rank_pct, (int, float)):
        regime: Literal["HIGH", "LOW", "NORMAL", "UNKNOWN"] = "UNKNOWN"
    elif iv_rank_pct >= high_pct:
        regime = "HIGH"
    elif iv_rank_pct <= low_pct:
        regime = "LOW"
    else:
        regime = "NORMAL"

    return {
        "iv_regime": regime,
        "atm_iv": atm_iv,
        "iv_rank_pct": iv_rank_pct,
        "pcr_volume": pcr_vol,
        "pcr_status": pcr_status,
        "thresholds": {"high_pct": high_pct, "low_pct": low_pct},
    }


# ==========================================
# Context formatters (LLM-friendly, stable layout)
# ==========================================

def _format_silver(silver_context: Dict[str, Any]) -> str:
    """Flatten the Silver Layer payload into a deterministic table.

    The layout intentionally places every quantitative value on its own
    pipe-separated line so the Checker's regex-based number extraction is
    robust to whitespace.
    """
    if not silver_context:
        return "(no silver context)"
    values = silver_context.get("values") or {}
    lineage = silver_context.get("lineage_anchors") or []
    citation_contract = dict(silver_context.get("citation_contract") or {})
    citation_registry = build_silver_citation_registry(citation_contract, values, lineage)
    if not values:
        return "(silver returned no values — INSUFFICIENT DATA)"

    lines = ["| metric | value |", "|---|---|"]
    for k, v in values.items():
        lines.append(f"| {k} | {v} |")

    if citation_contract:
        lines.append("")
        lines.append(
            "PREFERRED INLINE SILVER CITATIONS — use the preferred_anchor exactly for inline quantitative claims. "
            "Do not use audit lineage refs as the default draft citation form."
        )
        lines.append("| metric | preferred_anchor | observed_at |")
        lines.append("|---|---|---|")
        for metric_key in values.keys():
            entry = dict(citation_contract.get(metric_key) or {})
            preferred_anchor = render_preferred_silver_citation(str(metric_key), citation_registry)
            observed_at = entry.get("observed_at") or "n/a"
            lines.append(f"| {metric_key} | {preferred_anchor} | {observed_at} |")

        lines.append("")
        lines.append("AUDIT LINEAGE REFERENCES — provenance only; not the preferred inline citation form.")
        lines.append("| metric | audit_lineage_anchors |")
        lines.append("|---|---|")
        referenced_audit: set[str] = set()
        for metric_key in values.keys():
            entry = dict(citation_contract.get(metric_key) or {})
            audit_refs = [str(a) for a in (entry.get("audit_lineage_anchors") or []) if a]
            referenced_audit.update(audit_refs)
            audit_text = ", ".join(audit_refs) if audit_refs else "(none)"
            lines.append(f"| {metric_key} | {audit_text} |")

        orphan_audit = [str(a) for a in lineage if str(a) not in referenced_audit]
        if orphan_audit:
            lines.append("")
            lines.append("ORPHAN AUDIT ANCHORS — kept for provenance completeness only:")
            for anchor in orphan_audit:
                lines.append(f"  - {anchor}")
    elif lineage:
        lines.append("")
        lines.append(
            "AUDIT LINEAGE REFERENCES — no structured citation contract was provided, so these anchors are shown "
            "for provenance only:"
        )
        for lid in lineage:
            lines.append(f"  - {lid}")
    return "\n".join(lines)


def _format_gold(gold_context: List[Any]) -> str:
    """Serialise RetrievedChunk objects (or plain dicts, as a fallback) into
    a numbered, citation-ready list.

    Each line exposes `bronze_ref` as the citation key so the LLM can emit
    `[Gold: <bronze_ref>]` verbatim.
    """
    if not gold_context:
        return "(no gold context)"

    lines = []
    for i, chunk in enumerate(gold_context, 1):
        # RetrievedChunk is a Pydantic model; fall back to dict for robustness.
        content = getattr(chunk, "content", None) or (chunk.get("content", "") if isinstance(chunk, dict) else "")
        bronze = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref", "UNKNOWN") if isinstance(chunk, dict) else "UNKNOWN")
        src = getattr(chunk, "source_type", None) or (chunk.get("source_type", "") if isinstance(chunk, dict) else "")
        meta = getattr(chunk, "metadata", None) or (chunk.get("metadata", {}) if isinstance(chunk, dict) else {})
        record_date = meta.get("record_date", "unknown") if isinstance(meta, dict) else "unknown"
        ticker = meta.get("ticker", "") if isinstance(meta, dict) else ""

        # Truncate long content to keep the prompt lean — 600 chars preserves
        # roughly one paragraph of SEC or news text, the reranker already
        # surfaced the most relevant portion.
        snippet = (content or "").replace("\n", " ").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"

        lines.append(
            f"[{i}] CITE_ID={bronze} | source={src} | ticker={ticker} | record_date={record_date}\n"
            f"    Use inline citation exactly as [Gold: {bronze}].\n"
            f"    {snippet}"
        )
    return "\n".join(lines)


def _load_macro_context(state_value: Optional[str]) -> str:
    """Prefer the macro context already in state, else fall back to the
    on-disk daily snapshot. A hard-coded fallback keeps the pipeline alive
    even when the data pipeline has not yet produced a macro snapshot."""
    if state_value and state_value.strip():
        return state_value

    try:
        project_root = Path(__file__).resolve().parents[2]
        fp = project_root / "Data" / "Agent_Context" / "latest_macro_context.md"
        if fp.exists():
            return fp.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"AnalystAgent: macro context fallback failed: {e}")

    return "Market conditions are currently stable. (fallback — no snapshot available)"


# ==========================================
# AnalystAgent class (router-compatible)
# ==========================================

class AnalystAgent:
    """LangGraph-compatible analyst node implementation.

    Public contract:
        agent = AnalystAgent()
        result = await agent.generate_report(state)   # AnalystResult
        draft_markdown = result.draft
        iv_regime_dict = result.iv_regime

    The router in Scripts/agents/router.py wraps this and writes
    `result.draft` into ``state["draft_report"]`` and (on the first pass
    only) ``result.iv_regime`` into ``state["iv_regime_pinned"]``.
    """

    def __init__(self):
        self.temperature = float(os.getenv("ANALYST_TEMPERATURE", "0.0"))
        self.max_tokens = int(os.getenv("ANALYST_MAX_TOKENS", "180"))
        self.timeout_s = float(os.getenv("ANALYST_TIMEOUT_SECONDS", "120"))
        self.max_retries = int(os.getenv("ANALYST_MAX_RETRIES", "2"))
        self.retry_backoff_s = float(os.getenv("ANALYST_RETRY_BACKOFF_SECONDS", "1.5"))
        self.healthcheck_enabled = os.getenv("ANALYST_HEALTHCHECK_ENABLED", "1") == "1"
        self.healthcheck_timeout_s = float(os.getenv("ANALYST_HEALTHCHECK_TIMEOUT_SECONDS", "4"))
        self.enable_model_fallback = os.getenv("ANALYST_ENABLE_MODEL_FALLBACK", "1") == "1"
        self.fast_fail_on_500 = os.getenv("ANALYST_FAST_FAIL_ON_OLLAMA_500", "1") == "1"
        self.fallback_switch_sla_s = float(os.getenv("ANALYST_FALLBACK_SWITCH_SLA_SECONDS", "1.0"))

        # RAG context window controls (prompt-size guardrails).
        self.max_macro_chars = int(os.getenv("ANALYST_MAX_MACRO_CHARS", "1600"))
        self.max_silver_lines = int(os.getenv("ANALYST_MAX_SILVER_LINES", "70"))
        self.max_gold_chars = int(os.getenv("ANALYST_MAX_GOLD_CHARS", "3200"))
        self.max_payload_chars = int(os.getenv("ANALYST_MAX_PAYLOAD_CHARS", "9000"))

        provider = os.getenv("ANALYST_PROVIDER", "").strip().lower()
        legacy_backend = os.getenv("ANALYST_LLM_BACKEND", "").strip().lower()
        if provider not in {"openai", "ollama"}:
            provider = legacy_backend if legacy_backend in {"openai", "ollama"} else "openai"
        self.provider = provider

        default_openai_model = os.getenv("ANALYST_PRIMARY_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
        default_ollama_model = os.getenv("OLLAMA_ANALYST_MODEL", "options-expert-v1:latest").strip() or "options-expert-v1:latest"
        configured_model = os.getenv("ANALYST_MODEL", "").strip()
        self.model_name = configured_model or (default_openai_model if self.provider == "openai" else default_ollama_model)

        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.openai_timeout_s = float(
            os.getenv("ANALYST_OPENAI_TIMEOUT_SECONDS", os.getenv("ANALYST_OPENAI_FALLBACK_TIMEOUT_SECONDS", "45"))
        )
        self.base_url = _normalize_openai_base_url(
            os.getenv("OLLAMA_OPENAI_BASE_URL"),
            os.getenv("OLLAMA_HOST"),
        )
        self.api_key = os.getenv("OLLAMA_OPENAI_API_KEY", "ollama")

        self.fallback_provider = "ollama" if self.provider == "openai" else "openai"
        self.fallback_model_name = (
            default_ollama_model
            if self.fallback_provider == "ollama"
            else (os.getenv("ANALYST_OPENAI_FALLBACK_MODEL", default_openai_model).strip() or default_openai_model)
        )

        self.llm = self._build_llm(self.provider, self.model_name)
        self.fallback_llm = None
        self.primary_healthy = True
        self.fallback_healthy = True

        self.ollama_fallback_enabled = (
            self.enable_model_fallback
            and os.getenv("ANALYST_OLLAMA_FALLBACK_ENABLED", "1") == "1"
        )
        self.openai_fallback_enabled = (
            self.enable_model_fallback
            and os.getenv("ANALYST_OPENAI_FALLBACK_ENABLED", "1") == "1"
        )
        self.fallback_enabled = (
            self.ollama_fallback_enabled if self.fallback_provider == "ollama" else self.openai_fallback_enabled
        )
        if self.fallback_enabled:
            self.fallback_llm = self._build_llm(self.fallback_provider, self.fallback_model_name)

        if self.healthcheck_enabled and self.provider == "ollama":
            self.primary_healthy = self._healthcheck_model(self.model_name)
        if self.healthcheck_enabled and self.fallback_llm is not None and self.fallback_provider == "ollama":
            ollama_ok = self._healthcheck_model(self.fallback_model_name)
            if not ollama_ok:
                self.fallback_healthy = False
                logger.info("AnalystAgent: Ollama alternative unavailable at startup; keeping primary only.")

        self.system_prompt = get_analyst_system_prompt()
        self.system_prompt = (
            f"{self.system_prompt}\n\n"
            "=== CITATION ENFORCEMENT (RUNTIME HARD RULE) ===\n"
            "Use ONLY IDs explicitly listed in the payload under VALID_SILVER_IDS / VALID_GOLD_IDS.\n"
            "Never invent, truncate, or normalize IDs. Never use placeholders like GOLD_CONTEXT,\n"
            "SILVER_CONTEXT, MACRO_CONTEXT. If no valid ID exists, write INSUFFICIENT DATA."
        )
        self.prompt_chain = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("human", "Here is the context and query:\n\n{payload}"),
        ])
        logger.info(
            "AnalystAgent: primary=%s (%s) | fallback=%s (%s) | retries=%s | fast_fail_500=%s",
            self.model_name,
            self.provider,
            self.fallback_model_name if self.fallback_llm is not None else "disabled",
            self.fallback_provider if self.fallback_llm is not None else "disabled",
            self.max_retries,
            self.fast_fail_on_500,
        )

    def _build_ollama_llm(self, model_name: str) -> ChatOpenAI:
        """Build a ChatOpenAI client pointed at Ollama's OpenAI-compatible API."""
        return ChatOpenAI(
            model=model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout_s,
        )

    def _build_openai_llm(self, model_name: str) -> ChatOpenAI:
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": self.openai_api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.openai_timeout_s,
        }
        if self.openai_base_url:
            kwargs["base_url"] = self.openai_base_url
        return ChatOpenAI(**kwargs)

    def _build_llm(self, provider: str, model_name: str) -> ChatOpenAI:
        if provider == "ollama":
            return self._build_ollama_llm(model_name)
        return self._build_openai_llm(model_name)

    def _models_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"

    def _healthcheck_model(self, model_name: str) -> bool:
        """Best-effort Ollama model healthcheck via OpenAI-compatible /models."""
        try:
            resp = requests.get(
                self._models_endpoint(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.healthcheck_timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
            model_ids = {str(item.get("id", "")) for item in payload.get("data", []) if isinstance(item, dict)}
            ready = model_name in model_ids
            if not ready:
                logger.warning(
                    "AnalystAgent: healthcheck ok but model missing | model=%s | available=%s",
                    model_name,
                    sorted([m for m in model_ids if m])[:10],
                )
            return ready
        except Exception as exc:
            logger.warning("AnalystAgent: healthcheck failed for model=%s | err=%s", model_name, exc)
            return False

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        s = (text or "").strip()
        if len(s) <= max_chars:
            return s
        return s[:max_chars] + "\n...(truncated)..."

    def _build_payload_text(
        self,
        user_query: str,
        macro_ctx: str,
        iv_regime_block: str,
        silver_ctx: Dict[str, Any],
        gold_ctx: List[Any],
        revision_block: str,
        revision_constraints_block: str,
        state: Dict[str, Any],
    ) -> str:
        ids = _collect_valid_citation_ids(silver_ctx, gold_ctx)
        valid_silver_ids = ", ".join(ids["silver_ids"][:80]) if ids["silver_ids"] else "(none)"
        valid_gold_ids = ", ".join(ids["gold_ids"][:80]) if ids["gold_ids"] else "(none)"
        macro = self._clip_text(macro_ctx, self.max_macro_chars)
        silver_full = _format_silver(silver_ctx)
        silver_lines = silver_full.splitlines()
        if len(silver_lines) > self.max_silver_lines:
            silver = "\n".join(silver_lines[: self.max_silver_lines]) + "\n...(silver truncated)..."
        else:
            silver = silver_full

        gold = self._clip_text(_format_gold(gold_ctx), self.max_gold_chars)
        payload = (
            "=== USER QUERY ===\n"
            f"{user_query}\n\n"
            "=== MACRO ENVIRONMENT ===\n"
            f"{macro}\n\n"
            "=== IV REGIME (deterministic) ===\n"
            f"{iv_regime_block}\n\n"
            "=== SILVER CONTEXT ===\n"
            f"{silver}\n\n"
            "=== GOLD CONTEXT ===\n"
            f"{gold}\n\n"
            "=== REVISION FEEDBACK ===\n"
            f"{revision_block or '(none)'}\n\n"
            "=== STRUCTURED REVISION CONSTRAINTS ===\n"
            f"{revision_constraints_block or '(none)'}\n\n"
            f"{_render_analyst_output_contract_block(state)}\n\n"
            "=== VALID CITE IDS (STRICT) ===\n"
            f"VALID_SILVER_IDS: {valid_silver_ids}\n"
            f"VALID_GOLD_IDS: {valid_gold_ids}\n\n"
            "=== OUTPUT POLICY ===\n"
            "Use only provided evidence. If missing data, explicitly say INSUFFICIENT DATA. "
            "Never output placeholder citation IDs."
        )
        return self._clip_text(payload, self.max_payload_chars)

    async def _invoke_with_retry(self, llm: ChatOpenAI, payload: Dict[str, Any], model_name: str):
        last_err: Optional[Exception] = None
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await (self.prompt_chain | llm).ainvoke(payload)
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "AnalystAgent: invoke failed | model=%s | attempt=%s/%s | err=%s",
                    model_name,
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                if self.fast_fail_on_500 and _is_ollama_runner_500(exc):
                    logger.warning(
                        "AnalystAgent: detected Ollama 500 runner error; trigger fallback immediately "
                        "(target_switch_sla=%.2fs).",
                        self.fallback_switch_sla_s,
                    )
                    raise exc
                if attempt < attempts:
                    await asyncio.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
        if last_err is not None:
            raise last_err
        raise RuntimeError("AnalystAgent retry loop failed without explicit exception.")

    async def generate_report(self, state: Dict[str, Any]) -> AnalystResult:
        """Run one analyst pass and return the draft Markdown + pinned IV regime.

        Returns
        -------
        AnalystResult
            ``.draft`` is the Markdown (existing contract), ``.iv_regime``
            is the dict the router writes into ``state["iv_regime_pinned"]``
            so subsequent revisions reuse the same regime instead of
            recomputing it from a Silver table that may have mutated under
            the Checker's rescue refresh.

        Note: `revision_count` increment is owned by the router's analyst_node,
        not by this method — keeps the SoC clean and lets the route functions
        rely on a single source of truth for "how many drafts have been tried".
        """
        user_query = state.get("original_query", "")
        macro_ctx = _load_macro_context(state.get("macro_context"))
        silver_ctx = _preferred_silver_context(state)
        gold_ctx = state.get("gold_context", []) or []
        feedback_log = state.get("critic_feedback", []) or []
        revision_constraints_block = _render_revision_constraints_block(state)
        revision_n = state.get("revision_count", 0)

        # Deterministic pre-compute: IV regime.
        # IV Regime Pinning (2026-04-22, docs/test/2026-04-22/
        # router_e2e_deep_analysis.md §6.5):
        #   The regime MUST be stable across revisions otherwise the
        #   strategy recommendation flips direction mid-pipeline (Test 4
        #   flipped NORMAL → LOW → NORMAL over three revisions because the
        #   Checker's rescue refreshed `latest_atm_iv` each time). We pin
        #   the regime on the very first pass and reuse it verbatim — the
        #   router writes it back onto AgentState.iv_regime_pinned.
        pinned = state.get("iv_regime_pinned")
        if isinstance(pinned, dict) and pinned.get("iv_regime"):
            iv_regime = pinned
            logger.info(
                f"AnalystAgent: reusing pinned IV regime={iv_regime['iv_regime']} "
                f"| atm_iv={iv_regime.get('atm_iv')} (revision={revision_n})"
            )
        else:
            iv_regime = _infer_iv_regime(silver_ctx)
            logger.info(
                f"AnalystAgent: pinning IV regime={iv_regime['iv_regime']} "
                f"| atm_iv={iv_regime['atm_iv']} (first pass; will be reused on revisions)"
            )

        iv_regime_block = (
            f"iv_regime={iv_regime['iv_regime']} | atm_iv={iv_regime['atm_iv']} "
            f"| iv_rank_pct={iv_regime.get('iv_rank_pct')} "
            f"| pcr_volume={iv_regime['pcr_volume']} | pcr_status={iv_regime['pcr_status']} "
            f"| thresholds={iv_regime['thresholds']}"
        )

        # Revision-aware prompting — empty on the first pass.
        revision_block = render_revision_block(feedback_log)

        try:
            logger.info(
                f"AnalystAgent: invoking LLM | revision={revision_n} | "
                f"iv_regime={iv_regime['iv_regime']} | gold_n={len(gold_ctx)} | "
                f"silver_values_n={len(silver_ctx.get('values', {}))}"
            )
            payload = {
                "payload": self._build_payload_text(
                    user_query=user_query,
                    macro_ctx=macro_ctx,
                    iv_regime_block=iv_regime_block,
                    silver_ctx=silver_ctx,
                    gold_ctx=gold_ctx,
                    revision_block=revision_block,
                    revision_constraints_block=revision_constraints_block,
                    state=state,
                )
            }
            response = await self._invoke_with_retry(self.llm, payload, self.model_name)

            draft = getattr(response, "content", str(response)).strip()
            if not draft:
                raise RuntimeError("LLM returned empty draft")
            draft = _normalize_analyst_draft(draft, state)
            contract_audit = _validate_analyst_contract(draft, state)
            if contract_audit["violations"]:
                logger.warning(
                    "AnalystAgent: contract violations detected | revision=%s | violations=%s",
                    revision_n,
                    ", ".join(contract_audit["violations"]),
                )
            return AnalystResult(
                draft=draft,
                iv_regime=iv_regime,
                used_fallback=False,
                model_used=self.model_name,
                contract_audit=contract_audit,
            )

        except Exception as e:
            if (
                self.enable_model_fallback
                and self.fallback_llm is not None
                and self.fallback_healthy
            ):
                try:
                    fallback_started = asyncio.get_running_loop().time()
                    logger.warning(
                        "AnalystAgent: primary %s path failed; switching to %s fallback model=%s",
                        self.provider,
                        self.fallback_provider,
                        self.fallback_model_name,
                    )
                    payload = {
                        "payload": self._build_payload_text(
                            user_query=user_query,
                            macro_ctx=macro_ctx,
                            iv_regime_block=iv_regime_block,
                            silver_ctx=silver_ctx,
                            gold_ctx=gold_ctx,
                            revision_block=revision_block,
                            revision_constraints_block=revision_constraints_block,
                            state=state,
                        )
                    }
                    response = await self._invoke_with_retry(self.fallback_llm, payload, self.fallback_model_name)
                    draft = getattr(response, "content", str(response)).strip()
                    if draft:
                        draft = _normalize_analyst_draft(draft, state)
                        contract_audit = _validate_analyst_contract(draft, state)
                        if contract_audit["violations"]:
                            logger.warning(
                                "AnalystAgent: fallback draft contract violations | revision=%s | violations=%s",
                                revision_n,
                                ", ".join(contract_audit["violations"]),
                            )
                        switch_elapsed = asyncio.get_running_loop().time() - fallback_started
                        if switch_elapsed > self.fallback_switch_sla_s:
                            logger.warning(
                                "AnalystAgent: backup switch exceeded SLA %.2fs (actual=%.2fs)",
                                self.fallback_switch_sla_s,
                                switch_elapsed,
                            )
                        return AnalystResult(
                            draft=draft,
                            iv_regime=iv_regime,
                            used_fallback=True,
                            model_used=self.fallback_model_name,
                            contract_audit=contract_audit,
                        )
                except Exception as fallback_err:
                    logger.exception("AnalystAgent: fallback model failed: %s", fallback_err)

            logger.exception(f"AnalystAgent: LLM call failed: {e}")
            fallback_draft = (
                "## Analyst Draft — DEGRADED MODE\n"
                "INSUFFICIENT DATA — the Analyst LLM call failed. No recommendation "
                "will be produced until the model is available again.\n\n"
                f"Diagnostic: {type(e).__name__}"
            )
            return AnalystResult(
                draft=fallback_draft,
                iv_regime=iv_regime,
                used_fallback=False,
                model_used="degraded",
                contract_audit={
                    "missing_source_disclosure_present": True,
                    "missing_slot_disclosure_present": True,
                    "none_placeholder_leak": False,
                    "live_recommendation_phrase": False,
                    "violations": ["llm_failure_degraded_mode"],
                    "missing_sources_expected": _missing_strict_sources(state),
                    "missing_slots_expected": _missing_query_slots(state),
                },
            )
