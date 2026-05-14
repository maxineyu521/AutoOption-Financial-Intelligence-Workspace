"""
Scripts/agents/finalizer.py

Finalizer Agent — converts the fact-checked + logic-approved Markdown draft
into the deterministic Pydantic `FinalReport` that the UI / downstream tools
consume. Preserves the original schema contract (FinalReport, TradeIdea,
SourceCitation) so any existing Streamlit/JSON consumer keeps working.

Design pillars:
1. **Structured output, not prose**  — uses ChatOllama.with_structured_output(FinalReport)
   so the LLM cannot return a free-form string. If the structured parse fails,
   we fall back to a safe skeleton report instead of throwing.
2. **Traceability (Audit Trail)**  — exposes canonical Silver preferred anchors
   and Gold bronze refs to the LLM while keeping raw audit lineage refs in
   provenance/debug state only.
3. **Confidence score reflects data, not vibes**  — if Gold or Silver layers
   returned nothing / errored, the confidence score is capped. If the
   revision counter hit the hard limit, we annotate the report as "degraded".
4. **LangGraph-friendly**  — `FinalizerAgent.format_and_clean(state)` returns
   the dict that the router writes into `state["final_strategy"]`.
"""

from __future__ import annotations

import os
import logging
import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import requests
from pydantic import BaseModel, Field, PrivateAttr, ValidationError
from langchain_openai import ChatOpenAI

from Scripts.agents.prompts import get_finalizer_prompt
from Scripts.agents.state import RenderSafetyContract
from Scripts.core.evidence_contracts import (
    build_silver_citation_registry,
    canonical_query_family,
    evaluate_retrieval_slot_support,
    render_preferred_silver_citation,
)
from Scripts.core.financial_reasoning_contract import render_data_capability_profile
from Scripts.core.financial_ontology import INSIDER_FLOW_QUERY_SLOTS, SEC_ACTION_TAXONOMY
from Scripts.core.financial_narrative_contract import render_narrative_brief_block
from Scripts.core.liquidity_policy import (
    resolve_first_available_ticker_bundle,
    resolve_primary_ticker,
)
from Scripts.core.sec_contract import SECAnalysisBundle
from Scripts.core.silver_context import preferred_silver_context_from_state
from Scripts.retrieval.schema import render_scope_contract_block, render_time_contract_block

logger = logging.getLogger(__name__)

# Hard revision cap — must match checker.py / critic.py / router.py.
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))
_EXECUTABLE_LIQUIDITY_NOTE = (
    "Liquidity metrics here are computed on the executable subset only: ask >= 0.50, "
    "spread <= 15%, DTE 7-60, tiered moneyness band"
)


def _normalize_openai_base_url(raw_base_url: Optional[str], ollama_host: Optional[str]) -> str:
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
    msg = str(exc).lower()
    return ("status code: 500" in msg) or ("runner process has terminated" in msg)


# ==========================================
# Pydantic contracts (PRESERVED — downstream UI depends on these)
# ==========================================

class SourceCitation(BaseModel):
    """RAG Traceability: Forces the LLM to cite its sources to reduce hallucination."""
    source_type: Literal["Macro Data", "SEC Filing", "Global News", "GPR Index", "Options Market Data"] = Field(
        description="The category of the data source used."
    )
    detail: str = Field(
        description="Brief description of the source (e.g., 'SEC Form 4 for GOOG filed on 2026-04-03')"
    )


class TradeIdea(BaseModel):
    """Structured representation of a single actionable options trade."""

    ticker: str = Field(description="The underlying asset ticker, e.g., SPY, AAPL, GLD, SLV")

    asset_class: Literal["Equity", "Index ETF", "Commodity ETF", "Currency"] = Field(
        description="Broad classification of the underlying asset."
    )

    market_outlook: Literal["Bullish", "Bearish", "Neutral", "Highly Volatile"] = Field(
        description="The directional bias for this underlying asset."
    )

    option_strategy: Literal[
        "Long Call", "Long Put", "Straddle/Strangle",
        "Call Spread", "Put Spread", "Iron Condor"
    ] = Field(
        description="The specific options strategy recommended."
    )

    strike_details: str = Field(
        description="Recommended strike price(s). Use string to accommodate spreads (e.g., 'Buy 150C / Sell 155C' or '150')."
    )

    expiration_date: str = Field(
        description="Recommended expiration date, formatted as YYYY-MM-DD or specific timeframe (e.g., '30-45 DTE')."
    )

    rationale: str = Field(
        description="Detailed, logical explanation combining macro trends, news, or insider activity."
    )

    catalysts: List[str] = Field(
        description="Upcoming events driving this trade (e.g., 'CPI release next week', 'Earnings call')."
    )

    risk_profile: Literal["Low", "Medium", "High", "Speculative"] = Field(
        description="Assessed risk level of this specific trade structure."
    )

    supporting_evidence: List[SourceCitation] = Field(
        default_factory=list,
        description="Crucial citations from the RAG context that justify this trade idea."
    )


class FinalReport(BaseModel):
    """The overarching schema for the LLM's final response."""

    _render_recommendation_mode: str = PrivateAttr(default="")
    _render_mode_boundary_text: str = PrivateAttr(default="")
    _render_illustrative_structure_text: str = PrivateAttr(default="")
    _render_asset_read_text: str = PrivateAttr(default="")

    report_date: str = Field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d"),
        description="Date of the report generation."
    )

    macro_summary: str = Field(
        description="High-level synthesis of current macro environment (VIX, GPR, Broad Markets)."
    )

    trade_ideas: List[TradeIdea] = Field(
        description="List of top actionable options trades based on the retrieved context."
    )

    key_risks_and_hedges: List[str] = Field(
        description="True risks or invalidation conditions only; not a transport channel for recommendation-mode boundary text."
    )

    confidence_score: float = Field(
        ge=0.0, le=1.0,
        description="Overall confidence in the report's conclusions based on data alignment."
    )

    conversation_reply: str = Field(
        default="",
        description=(
            "A plain-English direct answer capped at 100 words. "
            "No markdown, no section headers, no bullet points. "
            "This field is only for answering the user's query: sentence 1 gives the strict-evidence conclusion, "
            "and the remaining sentence(s) give the main caveat or risk. "
            "Do not open with macro background or report framing. "
            "The full macro narrative belongs in macro_summary / markdown sections, not here."
        )
    )
    evidence_coverage_note: str = Field(
        default="",
        description=(
            "Optional answerability / truthness disclosure. "
            "Use only for lightweight coverage caveats when the read still stands. "
            "This is not a risk field, not a recommendation-boundary field, and not an asset-analysis field."
        ),
    )
    status_note: str = Field(
        default="",
        description=(
            "Optional runtime or governance note for frontend-safe disclosure. "
            "This is not a risk field and must never contain prompt scaffolding, raw draft excerpts, or diagnostics."
        ),
    )

    def to_markdown(self) -> str:
        """Converts the structured Pydantic object into a clean, readable Markdown report for UI display."""
        md_lines = [
            f"# 📊 Institutional Options Strategy Report",
            f"**Generated on:** {self.report_date}",
            f"**Overall Confidence Score:** {self.confidence_score * 100:.1f}%",
        ]
        direct_conclusion = _trim_to_word_limit(self.conversation_reply.strip(), 100) if self.conversation_reply else ""
        status_note = _trim_to_word_limit((self.status_note or "").strip(), 60)
        macro_backdrop = _trim_to_word_limit((self.macro_summary or "").strip(), 150)
        asset_options_read = _trim_to_word_limit(_build_asset_options_section_text(self), 150)
        recommendation_mode = _trim_to_word_limit(_build_recommendation_mode_text(self), 150)
        risks_text = _trim_to_word_limit(_build_risks_section_text(self), 150)

        if direct_conclusion:
            md_lines += [f"\n## Direct Conclusion", direct_conclusion]
        if status_note:
            md_lines += [f"\n> Note: {status_note}"]
        if macro_backdrop:
            md_lines += [f"\n## Macro / Event Backdrop", macro_backdrop]
        if asset_options_read:
            md_lines += [f"\n## Asset / Options Read", asset_options_read]
        if recommendation_mode:
            md_lines += [f"\n## Recommendation Mode", recommendation_mode]
        if risks_text:
            md_lines += [f"\n## Risks / What Would Change the View", risks_text]

        return "\n".join(md_lines)


# ==========================================
# Helpers
# ==========================================

def _collect_evidence_pool(state: Dict[str, Any]) -> List[SourceCitation]:
    """Build a deterministic evidence pool from the retrieval state.

    The LLM is asked to draw TradeIdea citations from this pool. If the LLM
    misses any, the pool is also used as a final fallback so the compliance
    audit trail is never empty when data exists.
    """
    pool: List[SourceCitation] = []

    silver_ctx = _preferred_silver_context(state)
    silver_values = dict(silver_ctx.get("values") or {})
    citation_registry = build_silver_citation_registry(
        silver_ctx.get("citation_contract") or {},
        silver_values,
        silver_ctx.get("lineage_anchors") or [],
    )
    citation_contract = dict(citation_registry.get("contract") or {})
    for metric_key in silver_values:
        metric_s = str(metric_key)
        preferred = render_preferred_silver_citation(metric_s, citation_registry)
        entry = dict(citation_contract.get(metric_s) or {})
        observed_at = str(entry.get("observed_at") or "").strip()
        source_channel = str(entry.get("source_channel") or "primary").strip()
        metric_l = metric_s.lower()
        if metric_s.startswith("gpr_"):
            st: Literal["Macro Data", "SEC Filing", "Global News", "GPR Index", "Options Market Data"] = "GPR Index"
        elif metric_s.startswith(("GSPC_", "IXIC_", "VIX_", "DXY_", "DX_Y_NYB_", "GLD_SPOT_", "SLV_SPOT_", "FEDFUNDS_", "CPIAUCSL_", "UNRATE_")):
            st = "Macro Data"
        elif any(token in metric_l for token in ("iv", "spread", "liquid", "option", "open_interest", "pcr")):
            st = "Options Market Data"
        else:
            st = "Macro Data"
        bits = [f"Silver preferred anchor: {preferred}", f"metric={metric_s}"]
        if observed_at:
            bits.append(f"observed_at={observed_at}")
        if source_channel:
            bits.append(f"source_channel={source_channel}")
        pool.append(SourceCitation(source_type=st, detail=" | ".join(bits)))

    for chunk in list(state.get("gold_context", []) or []) + list(state.get("supplemental_news_context", []) or []):
        src = getattr(chunk, "source_type", None) or (chunk.get("source_type", "") if isinstance(chunk, dict) else "")
        meta = getattr(chunk, "metadata", None) or (chunk.get("metadata", {}) if isinstance(chunk, dict) else {})
        br = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref", "UNKNOWN") if isinstance(chunk, dict) else "UNKNOWN")
        record_date = (
            meta.get("record_date")
            or meta.get("publish_date")
            or meta.get("filed_at")
            or "unknown"
        ) if isinstance(meta, dict) else "unknown"

        src_l = str(getattr(src, "value", src) or "").lower()
        if src_l == "sec":
            ui_src: Literal["Macro Data", "SEC Filing", "Global News", "GPR Index", "Options Market Data"] = "SEC Filing"
        elif src_l == "gpr":
            ui_src = "GPR Index"
        else:
            ui_src = "Global News"

        pool.append(SourceCitation(
            source_type=ui_src,
            detail=f"{ui_src} @ {record_date} | ref={br}",
        ))

    deduped: List[SourceCitation] = []
    seen: set[tuple[str, str]] = set()
    for item in pool:
        key = (item.source_type, item.detail)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _enforce_deterministic_report_date(
    report: "FinalReport",
    state: Dict[str, Any],
) -> None:
    """Overwrite `report.report_date` with the pipeline-known anchor date.

    The LLM was previously hallucinating this field as a literal string
    ("current date"), as the first gold-chunk's record_date, or as
    "INSUFFICIENT DATA" — none of which are useful to a compliance reader.
    The pipeline's authoritative anchor lives at
    ``state["time_range"]["anchor_date"]`` (set by MasterRetriever from
    ``config/runtime/collect_data_state.json:options_daily``). When that
    is present we simply clobber the LLM's guess. When absent (e.g. the
    retriever hard-failed) we leave the LLM value alone so a downstream
    auditor can still see something plausible.
    """
    tr = state.get("time_range") or {}
    anchor = tr.get("anchor_date")
    if isinstance(anchor, str) and len(anchor) == 10 and anchor[4] == "-" and anchor[7] == "-":
        # basic ISO-8601 shape check — avoids writing a bogus anchor
        report.report_date = anchor


def _reconcile_citation_source_types(
    report: "FinalReport",
    evidence_pool: List["SourceCitation"],
) -> None:
    """Clobber LLM-invented `source_type` values against the deterministic pool.

    Scenario (§5.2 of the 2026-04-22 deep analysis): the evidence pool is
    built correctly from Gold chunks (source_type=sec → "SEC Filing"), but
    the LLM re-wrote them all to "Global News" when producing
    `trade_ideas[].supporting_evidence`. The pool already contains the
    correct mapping; we match by `detail` substring and force the canonical
    `source_type` when the LLM matches a real pool item.
    """
    if not evidence_pool or not report.trade_ideas:
        return

    # Index the pool on two keys: exact detail and a lowercased prefix so
    # minor whitespace/punctuation drift from the LLM still matches.
    pool_exact: Dict[str, SourceCitation] = {p.detail: p for p in evidence_pool}
    pool_prefix: List[tuple] = [
        (p.detail.lower()[:60], p) for p in evidence_pool if p.detail
    ]

    for idea in report.trade_ideas:
        for citation in idea.supporting_evidence:
            if not citation.detail:
                continue
            exact = pool_exact.get(citation.detail)
            if exact is not None:
                if exact.source_type != citation.source_type:
                    citation.source_type = exact.source_type
                continue
            # Fuzzy prefix match — handles LLM line breaks / truncations.
            key = citation.detail.lower()[:60]
            for pkey, p in pool_prefix:
                if key == pkey:
                    if p.source_type != citation.source_type:
                        citation.source_type = p.source_type
                    break


def _degraded_report(
    reason: str,
    user_query: str,
    evidence_pool: List[SourceCitation],
    cap_confidence: float = 0.20,
) -> FinalReport:
    """Safe fallback when the Finalizer LLM fails or the pipeline is degraded.

    We never throw from this node — that would strand the entire graph.
    Instead we return a FinalReport that explicitly declares degradation,
    carries whatever evidence we have, and sets a low confidence score.
    """
    topic = (user_query or "the requested topic").strip()
    return FinalReport(
        macro_summary=(
            "DEGRADED MODE — this run is limited to informational output only. "
            f"Reason: {reason}. The pipeline could not safely support a constrained, actionable "
            f"options recommendation for {topic} in this run."
        ),
        trade_ideas=[],
        key_risks_and_hedges=[
            "Informational mode only — no explicit options trade recommendation should be actioned from this output.",
            "Wait for a clean rerun or fresher evidence before upgrading this topic into a directional options signal.",
        ],
        confidence_score=cap_confidence,
        conversation_reply=(
            f"For {topic}, the current run supports an informational read only, not a trade recommendation. "
            "There may be a general direction worth watching, but the evidence quality or revision state is not strong enough "
            "to call a clear options setup right now."
        ),
        status_note=_runtime_status_note(degraded=True, degraded_reason=reason),
    )


def _dedupe_preserve_order(items: List[str]) -> List[str]:
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


def _typed_edits_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    edits = _finalizer_card(state).get("minor_edits") or []
    if not isinstance(edits, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in edits:
        if isinstance(item, dict):
            normalized.append(item)
    return normalized


def _revision_constraints(state: Dict[str, Any]) -> Dict[str, Any]:
    card = _finalizer_card(state)
    constraints = card.get("revision_constraints") or state.get("revision_constraints") or {}
    return constraints if isinstance(constraints, dict) else {}


def _market_impact_risk_value(state: Dict[str, Any]) -> str:
    values = _silver_values(state)
    primary_ticker = resolve_primary_ticker(
        state=state,
        metadata=state.get("metadata"),
        scope_contract=state.get("scope_contract") or {},
    )


def _out_of_scope_report(
    *,
    state: Dict[str, Any],
    user_query: str,
) -> FinalReport:
    tickers = _out_of_scope_tickers(state)
    refusal_reason = _refusal_reason(state) or "The requested ticker is outside the local covered universe."
    ticker_text = ", ".join(tickers) if tickers else "the requested ticker"
    topic = re.sub(r"\s+", " ", (user_query or "the requested topic").strip())
    return FinalReport(
        macro_summary=(
            f"Out of scope: {ticker_text} is outside the local covered universe for this pipeline. "
            f"{refusal_reason}"
        ),
        trade_ideas=[],
        key_risks_and_hedges=[refusal_reason],
        confidence_score=0.05,
        conversation_reply=(
            f"I can’t answer {topic} from this pipeline because {ticker_text} is outside the local covered universe. "
            f"{refusal_reason}"
        ),
    )
    ticker, bundle = resolve_first_available_ticker_bundle(
        values,
        [primary_ticker] if primary_ticker else [],
        ["market_impact_risk"],
    )
    value = bundle.get("market_impact_risk") if ticker else None
    return str(value).strip() if value not in (None, "") else ""


def _render_typed_edit_instruction(edit: Dict[str, Any], state: Dict[str, Any]) -> str:
    instruction = str(edit.get("instruction", "") or "").strip()
    if not instruction:
        return ""
    if edit.get("edit_type") == "keep_numbers":
        key_numbers = dict(_finalizer_card(state).get("key_numbers") or {})
        must_keep = list(edit.get("must_keep_keys") or [])
        keep_fragments = []
        for key in must_keep:
            if key in key_numbers:
                keep_fragments.append(f"{key}={key_numbers[key]}")
        if keep_fragments:
            instruction = f"{instruction} Required metrics: {', '.join(keep_fragments)}."
    return instruction


def _build_report_provenance(state: Dict[str, Any]) -> Dict[str, List[str]]:
    card = _finalizer_card(state)
    silver_ids = [str(x) for x in (card.get("required_silver_anchors") or [])]
    gold_ids = [str(x) for x in (card.get("required_gold_refs") or [])]
    return {
        "direct_conclusion_evidence": silver_ids[:],
        "macro_summary_evidence": gold_ids[:],
        "asset_read_evidence": silver_ids[:] + gold_ids[:],
    }


def _collect_current_revision_minor_suggestions(state: Dict[str, Any]) -> List[str]:
    """Collect only typed edits from the finalizer input card.

    Finalizer intentionally does NOT read prose suggestions from critic_feedback
    or critic_minor_suggestions. Those remain audit / debug channels only.
    """
    collected: List[str] = []
    typed_edits = _typed_edits_from_state(state)
    if typed_edits:
        collected.extend(
            _render_typed_edit_instruction(edit, state)
            for edit in typed_edits
        )
    return _dedupe_preserve_order(collected)


def _build_revision_guardrails_block(
    *,
    minor_suggestions: List[str],
    degraded: bool,
    degraded_reason: Optional[str],
    revision_constraints: Optional[Dict[str, Any]] = None,
) -> str:
    constraints = revision_constraints or {}
    lines = [
        "=== FINALIZER REVISION BOUNDARY ===",
        "You are doing constrained normalization, not open-ended generation.",
        "No new facts: do not add any new ticker, metric, catalyst, strike, expiry, or number.",
        "No new reasoning: do not derive a fresh thesis from raw retrieval state.",
        "Only reframe: reuse Analyst-owned conclusions, apply Critic constraints, and fill the schema.",
        "Only make edits that are strictly required to satisfy the listed minor suggestions.",
        "Everything else must remain semantically unchanged from the analyst draft.",
        "Do not add new tickers, strikes, DTE windows, catalysts, macro claims, or directional views unless they already appear in the draft or evidence pool.",
        "If the minor suggestions list is empty, perform schema extraction only.",
    ]
    if degraded:
        lines.extend([
            f"Pipeline degraded: {degraded_reason or 'unknown_reason'}.",
            "Do not emit a trade recommendation. Keep trade_ideas empty and make the reply informational only.",
        ])
    if constraints.get("must_explain_why_not_now"):
        lines.extend([
            "Informational-only degraded mode: explicitly explain why a live structure should not be advanced now.",
            "Use the provided why-not-now reason directly rather than generic market-posture language.",
        ])
    elif constraints.get("market_read_only"):
        lines.extend([
            "Confident analysis mode: treat informational_only as a read-only market posture, not as an apology template.",
            "Lead with the evidence-backed posture and keep any structure mention theoretical and clearly non-actionable.",
        ])
    if minor_suggestions:
        lines.append("Allowed modifications:")
        lines.extend(f"- {item}" for item in minor_suggestions)
    else:
        lines.append("Allowed modifications: none beyond faithful schema conversion.")
    return "\n".join(lines)


def _build_informational_reply(
    *,
    user_query: str,
    reason: Optional[str] = None,
) -> str:
    topic = (user_query or "the requested topic").strip()
    topic = re.sub(r"\s+", " ", topic)
    reason_clause = f" because {reason}" if reason else ""
    return (
        f"For {topic}, the current evidence supports context only{reason_clause}. "
        "There may be a broader directional theme worth monitoring, but there is not enough support for a clean options setup. "
        "Use this as information and risk framing, not as a live trade signal."
    )


def _build_watchlist_reply(
    *,
    user_query: str,
    reason: Optional[str] = None,
) -> str:
    topic = (user_query or "the requested topic").strip()
    topic = re.sub(r"\s+", " ", topic)
    reason_clause = f" because {reason}" if reason else ""
    return (
        f"For {topic}, the clearest read is a directional watchlist{reason_clause}. "
        "Focus on the primary underlying or ETF and the nearby options board, but do not treat this as strike-level action yet. "
        "Wait for stronger options-chain confirmation before promoting a concrete structure."
    )


def _first_sentence(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return ""
    match = re.search(r"(.+?[.!?])(?:\s|$)", clean)
    return (match.group(1) if match else clean).strip()


def _trim_to_word_limit(text: str, max_words: int = 100) -> str:
    words = re.findall(r"\S+", (text or "").strip())
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(" ,;:-") + "."


def _preferred_silver_context(state: Dict[str, Any]) -> Dict[str, Any]:
    return preferred_silver_context_from_state(state)


def _silver_values(state: Dict[str, Any]) -> Dict[str, Any]:
    silver = _preferred_silver_context(state)
    values = silver.get("values") or {}
    return values if isinstance(values, dict) else {}


def _finalizer_card(state: Dict[str, Any]) -> Dict[str, Any]:
    card = state.get("finalizer_input_card") or {}
    return card if isinstance(card, dict) else {}


def _render_safety_contract(state: Dict[str, Any]) -> RenderSafetyContract:
    raw_contract = _finalizer_card(state).get("render_safety_contract") or {}
    if not isinstance(raw_contract, dict):
        return RenderSafetyContract()
    try:
        return RenderSafetyContract.model_validate(raw_contract)
    except ValidationError as exc:
        logger.warning("FinalizerAgent: invalid render_safety_contract; falling back to defaults | error=%s", exc)
        return RenderSafetyContract()


def _direct_answer_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).direct_answer_seed.strip()


def _direct_answer_includes_posture_takeaway(state: Dict[str, Any]) -> bool:
    return _render_safety_contract(state).direct_answer_includes_posture_takeaway


def _direct_answer_includes_missing_slot_disclosure(state: Dict[str, Any]) -> bool:
    return _render_safety_contract(state).direct_answer_includes_missing_slot_disclosure


def _macro_summary_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).macro_summary_seed.strip()


def _asset_read_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).asset_read_seed.strip()


def _asset_read_narrative_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).asset_read_narrative_seed.strip()


def _risk_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).risk_seed.strip()


def _summary_caveat_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).summary_caveat_seed.strip()


def _status_note_seed(state: Dict[str, Any]) -> str:
    return _render_safety_contract(state).status_note.strip()


def _narrative_brief_block(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    block = str(card.get("narrative_brief_block") or "").strip()
    if block:
        return block
    return render_narrative_brief_block(card.get("narrative_brief") or {})


def _scope_contract_for_rendering(state: Dict[str, Any]) -> Dict[str, Any]:
    card = _finalizer_card(state)
    scope = card.get("scope_contract_summary") or state.get("scope_contract") or {}
    return scope if isinstance(scope, dict) else {}


def _scope_status(state: Dict[str, Any]) -> str:
    return str(_scope_contract_for_rendering(state).get("scope_status") or "in_scope").strip().lower()


def _out_of_scope_tickers(state: Dict[str, Any]) -> List[str]:
    scope = _scope_contract_for_rendering(state)
    return [str(t).strip() for t in (scope.get("out_of_scope_tickers") or []) if str(t).strip()]


def _refusal_reason(state: Dict[str, Any]) -> str:
    return str(_scope_contract_for_rendering(state).get("refusal_reason") or "").strip()


def _clean_analyst_evidence_line(text: str) -> str:
    clean = (text or "").strip()
    for marker in ("[Silver:", "[Gold:"):
        if marker in clean:
            clean = clean.split(marker, 1)[0].rstrip()
    clean = clean.replace("**", "").replace("__", "")
    if clean.startswith("-"):
        clean = clean[1:].strip()
    clean = clean.replace(" | ", "; ")
    return re.sub(r"\s+", " ", clean).strip(" -")


def _extract_analyst_evidence_lines(draft: str) -> List[str]:
    lines: List[str] = []
    in_quant_section = False
    for raw in (draft or "").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            section = line.strip().lower()
            if in_quant_section and section != "## quantitative evidence":
                break
            in_quant_section = section == "## quantitative evidence"
            continue
        if not in_quant_section:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("-"):
            cleaned = _clean_analyst_evidence_line(stripped)
            if cleaned:
                lines.append(cleaned)
    return lines


def _render_scope_contract_for_finalizer(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    summary = card.get("scope_contract_summary") or state.get("scope_contract") or {}
    return render_scope_contract_block(summary)


def _render_data_capability_for_finalizer(state: Dict[str, Any]) -> str:
    return render_data_capability_profile(state.get("data_capability_profile") or {})


def _render_time_contract_for_finalizer(state: Dict[str, Any]) -> str:
    outcome = state.get("retrieval_outcome") or {}
    time_contract = outcome.get("time_contract") if isinstance(outcome, dict) else None
    if time_contract:
        return render_time_contract_block(time_contract)
    time_range = dict(_finalizer_card(state).get("time_window") or state.get("time_range") or {})
    requested_window = str(((state.get("scope_contract") or {}).get("requested_time_window")) or time_range.get("time_window_label") or "past_six_months")
    fallback_contract = {
        "requested_window": requested_window,
        "effective_window": time_range.get("time_window_label") or requested_window,
        "window_days": time_range.get("window_days"),
        "is_default_window_applied": time_range.get("is_default_window_applied"),
        "is_extended_window": bool(state.get("is_fallback")),
    }
    return render_time_contract_block(fallback_contract)


def _fmt_value(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}%"
    return str(value)


def _missing_query_slots(state: Dict[str, Any]) -> List[str]:
    card = _finalizer_card(state)
    outcome = card.get("retrieval_outcome_summary") or state.get("retrieval_outcome") or {}
    if not isinstance(outcome, dict):
        return []
    return [str(slot) for slot in (outcome.get("missing_query_slots") or []) if str(slot).strip()]


def _retrieval_outcome_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    card = _finalizer_card(state)
    outcome = card.get("retrieval_outcome_summary") or state.get("retrieval_outcome") or {}
    return outcome if isinstance(outcome, dict) else {}


def _slot_evidence_contracts_for_finalizer(state: Dict[str, Any]) -> Dict[str, Any]:
    scope = _scope_contract_for_rendering(state)
    contracts = scope.get("slot_evidence_contracts") if isinstance(scope, dict) else {}
    return dict(contracts or {}) if isinstance(contracts, dict) else {}


def _compute_retrieval_confidence(state: Dict[str, Any]) -> float:
    if _scope_status(state) == "out_of_scope":
        return 0.05

    scope = _scope_contract_for_rendering(state)
    retrieval_outcome = _retrieval_outcome_summary(state)
    silver_values = _silver_values(state)
    gold_ctx = list(state.get("gold_context", []) or [])
    slot_contracts = _slot_evidence_contracts_for_finalizer(state)
    slot_support = evaluate_retrieval_slot_support(
        slot_contracts=slot_contracts,
        retrieval_outcome=retrieval_outcome,
        silver_values=silver_values,
        gold_ctx=gold_ctx,
    )

    has_silver = bool(silver_values)
    strict_missing = [
        str(src).strip()
        for src in (retrieval_outcome.get("missing_strict_sources") or [])
        if str(src).strip()
    ]
    gold_optional = bool(scope.get("gold_context_optional", False))
    required_sources_ok = gold_optional or not strict_missing
    slot_pass = bool(slot_support.get("hard_gate_pass", False))
    slot_support_rate = float(slot_support.get("slot_support_rate", 0.0) or 0.0)
    retrieval_support_score = float(slot_support.get("retrieval_support_score", 0.0) or 0.0)
    is_fallback = bool(retrieval_outcome.get("is_fallback", False) or state.get("is_fallback", False))

    if has_silver and slot_pass and required_sources_ok:
        score = 0.92
    elif has_silver and required_sources_ok and (slot_support_rate >= 0.66 or retrieval_support_score >= 0.66):
        score = 0.78
    elif has_silver and (slot_support_rate > 0.0 or retrieval_support_score > 0.0):
        score = 0.52
    elif has_silver:
        score = 0.30
    else:
        score = 0.12 if gold_ctx else 0.08

    if is_fallback:
        score = min(score, 0.45)

    return round(max(0.05, min(score, 0.95)), 2)


def _query_slots_map(state: Dict[str, Any]) -> Dict[str, str]:
    card = _finalizer_card(state)
    scope = card.get("scope_contract_summary") or state.get("scope_contract") or {}
    if isinstance(scope, dict) and scope.get("query_slots"):
        return dict(scope.get("query_slots") or {})
    if canonical_query_family(str(card.get("query_family", "") or "").lower()) == "insider_flow_driven":
        return dict(INSIDER_FLOW_QUERY_SLOTS)
    return {}


def _sec_action_taxonomy_map(state: Dict[str, Any]) -> Dict[str, str]:
    card = _finalizer_card(state)
    scope = card.get("scope_contract_summary") or state.get("scope_contract") or {}
    if isinstance(scope, dict) and scope.get("sec_action_taxonomy"):
        return dict(scope.get("sec_action_taxonomy") or {})
    if canonical_query_family(str(card.get("query_family", "") or "").lower()) == "insider_flow_driven":
        return dict(SEC_ACTION_TAXONOMY)
    return {}


def _sec_analysis_bundle(state: Dict[str, Any]) -> SECAnalysisBundle:
    outcome = state.get("retrieval_outcome") or {}
    bundle = outcome.get("sec_analysis_bundle") or {}
    if isinstance(bundle, SECAnalysisBundle):
        return bundle
    if isinstance(bundle, dict) and bundle:
        try:
            return SECAnalysisBundle.model_validate(bundle)
        except Exception:
            pass
    return SECAnalysisBundle()


def _missing_slot_disclosure_sentence(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    if canonical_query_family(str(card.get("query_family", "") or "").lower()) == "insider_flow_driven":
        analyst_missing = str(card.get("sec_missing_note") or "").strip()
        if analyst_missing:
            return analyst_missing.rstrip(".") + "."
        bundle = _sec_analysis_bundle(state)
        if bundle.missing_disclosure.has_missing and bundle.missing_disclosure.missing_forms:
            return (
                "Requested SEC coverage is partial in this run; missing form(s): "
                + ", ".join(bundle.missing_disclosure.missing_forms)
                + "."
            )
    outcome = state.get("retrieval_outcome") or {}
    news_coverage_status = str(outcome.get("news_coverage_status") or "").strip().lower()
    background_only_read = bool(outcome.get("background_only_read"))
    slots = _missing_query_slots(state)
    requested_forms = [str(s).upper().strip() for s in (outcome.get("sec_forms_requested") or []) if str(s).strip()]
    retrieved_forms = {str(s).upper().strip() for s in (outcome.get("sec_forms_retrieved") or []) if str(s).strip()}
    if background_only_read and news_coverage_status == "no_fresh_news_retrieved":
        return "No fresh geopolitical news was retrieved in the requested window, so this answer stays at background-only geopolitical context."
    if not slots and not requested_forms:
        return ""
    slot_map = _query_slots_map(state)
    parts: List[str] = []
    if "8-K" in requested_forms and "8-K" in retrieved_forms:
        parts.append("SEC/8-K evidence was retrieved in this run")
    if "4" in requested_forms and "4" in retrieved_forms:
        parts.append("SEC/Form-4 evidence was retrieved in this run")
    if "sec_insider_signal" in slots:
        parts.append(
            "SEC/Form-4 evidence was not retrieved, so "
            + slot_map.get("sec_insider_signal", "the insider signal")
            + " cannot be assessed reliably"
        )
    if "sec_event_signal" in slots:
        parts.append(
            "SEC/8-K evidence was not retrieved, so "
            + slot_map.get("sec_event_signal", "the SEC event filing signal")
            + " cannot be assessed reliably"
        )
    if "options_liquidity_posture" in slots:
        parts.append(
            slot_map.get("options_liquidity_posture", "options liquidity posture")
            + " cannot be assessed reliably from this run"
        )
    return "; ".join(parts) + "." if parts else ""


def _sec_taxonomy_sentence(state: Dict[str, Any]) -> str:
    taxonomy = _sec_action_taxonomy_map(state)
    if not taxonomy:
        return ""
    return (
        "This pipeline treats SELL as insider disposition, BUY as open-market purchase, "
        "and ACQUIRE/VEST as vesting-related acquisition rather than open-market buying or selling."
    )


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


def _extract_sec_signal_summary(state: Dict[str, Any], query: str) -> str:
    card = _finalizer_card(state)
    analyst_summary = str(card.get("sec_signal_summary") or "").strip()
    if analyst_summary:
        return analyst_summary
    bundle = _sec_analysis_bundle(state)
    coverage = bundle.coverage
    parts: List[str] = []
    if "4" in coverage.sec_forms_retrieved:
        form4_result = bundle.form4_analysis_result
        if form4_result.directional_read == "selling_pressure":
            summary = f"Form 4 flow shows net selling pressure across {form4_result.sell_count} filing(s)"
        elif form4_result.directional_read == "buying_support":
            summary = f"Form 4 flow shows buying support across {form4_result.buy_count} filing(s)"
        elif form4_result.directional_read == "compensation_vesting":
            summary = "Form 4 flow is mostly compensation-driven vesting rather than discretionary open-market activity"
        else:
            summary = "Form 4 flow is mixed across selling, buying, and vesting activity"
        if form4_result.total_value:
            summary += f", totaling about {_fmt_money(form4_result.total_value)}"
        parts.append(summary)
    if "8-K" in coverage.sec_forms_retrieved:
        form8k = bundle.form8k_analysis_result
        summary = f"8-K event evidence is present and reads as {form8k.event_pressure or 'neutral_event_pressure'}"
        if form8k.categories:
            summary += f" around {', '.join(form8k.categories[:3])}"
        if form8k.latest_filing_date and form8k.latest_filing_content:
            summary += f"; latest filing on {form8k.latest_filing_date}: {form8k.latest_filing_content}"
        parts.append(summary)
    if parts:
        return " ".join(parts)

    if not (state.get("gold_context") or []):
        disclosure = _missing_slot_disclosure_sentence(state)
        if disclosure:
            return disclosure.rstrip(".")
        if "selling" in (query or "").lower():
            return "No matched in-scope Form 4 selling evidence was retrieved in this run"
        return "No matched in-scope Form 4 flow evidence was retrieved in this run"
    return "SEC evidence was retrieved, but the structured insider-flow summary is limited in this run"


def _build_analyst_owned_evidence_sentence(
    *,
    state: Dict[str, Any],
    draft: str,
    user_query: str,
) -> str:
    direct_answer_seed = _direct_answer_seed(state)
    if direct_answer_seed:
        return direct_answer_seed
    card = _finalizer_card(state)
    analyst_conclusion = str(card.get("analyst_conclusion") or "").strip()
    if analyst_conclusion:
        return analyst_conclusion
    analyst_lines = [str(line).strip() for line in (card.get("analyst_evidence_lines") or []) if str(line).strip()]
    if analyst_lines:
        return analyst_lines[0].rstrip(".") + "."
    topic = re.sub(r"\s+", " ", (user_query or "the requested topic").strip())
    return f"For {topic}, the checked Analyst framing remains read-only at the current evidence level."


def _posture_takeaway(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    return str(card.get("posture_takeaway") or "").strip()


def _posture_rationale(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    return str(card.get("posture_rationale") or "").strip()


def _base_regime_read(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    return str(card.get("base_regime_read") or "").strip()


def _escalation_risk_read(state: Dict[str, Any]) -> str:
    card = _finalizer_card(state)
    return str(card.get("escalation_risk_read") or "").strip()


def _strip_topic_prefix(sentence: str) -> str:
    clean = re.sub(r"\s+", " ", (sentence or "").strip())
    if not clean.lower().startswith("for "):
        return clean
    if ", " not in clean:
        return clean
    return clean.split(", ", 1)[1].strip()


def _build_strict_evidence_sentence(
    *,
    state: Dict[str, Any],
    draft: str,
    user_query: str,
) -> str:
    card = _finalizer_card(state)
    analyst_conclusion = str(card.get("analyst_conclusion", "") or "").strip()
    if analyst_conclusion:
        return analyst_conclusion

    topic = re.sub(r"\s+", " ", (user_query or "the requested topic").strip())
    intent_family = str(card.get("query_family", "") or "").lower()
    values = dict(card.get("key_numbers") or {}) if card.get("key_numbers") else _silver_values(state)
    analyst_lines = list(card.get("analyst_evidence_lines") or [])

    atm_iv = values.get("latest_atm_iv")
    iv_rank = values.get("latest_atm_iv_rank_pct")
    iv_skew = values.get("latest_iv_skew")
    pcr_volume = values.get("pcr_volume")
    pcr_oi = values.get("pcr_open_interest")
    pcr_status = values.get("pcr_status")
    vix_value = values.get("VIX_value")
    vix_change = values.get("VIX_change_pct")
    dxy_value = values.get("DXY_value")
    dxy_change = values.get("DXY_change_pct")
    gpr_level = values.get("gpr_index_level")
    gpr_percentile = values.get("gpr_percentile")
    primary_ticker = resolve_primary_ticker(
        state=state,
        metadata=state.get("metadata"),
        scope_contract=state.get("scope_contract") or {},
    )
    options_ticker, options_bundle = resolve_first_available_ticker_bundle(
        values,
        [primary_ticker] if primary_ticker else [],
        ["executable_option_volume", "executable_open_interest", "liquid_contracts", "avg_spread_pct", "market_impact_risk"],
    )
    executable_option_volume = options_bundle.get("executable_option_volume")
    executable_open_interest = options_bundle.get("executable_open_interest")
    liquid_contracts = options_bundle.get("liquid_contracts")
    avg_spread_pct = options_bundle.get("avg_spread_pct")
    market_impact_risk = options_bundle.get("market_impact_risk")

    if intent_family == "options_microstructure":
        fragments: List[str] = []
        if pcr_volume is not None:
            pcr_fragment = f"PCR volume is {_fmt_value(pcr_volume, 3)}"
            if pcr_oi is not None:
                pcr_fragment += f" and PCR open interest is {_fmt_value(pcr_oi, 3)}"
            if pcr_status:
                pcr_fragment += f" ({pcr_status})"
            fragments.append(pcr_fragment)
        if atm_iv is not None:
            iv_fragment = f"ATM IV is {_fmt_value(atm_iv, 4)}"
            if iv_rank is not None:
                iv_fragment += f" with IV rank {_fmt_pct(iv_rank, 2)}"
            fragments.append(iv_fragment)
        if iv_skew is not None:
            fragments.append(f"IV skew reads {_fmt_value(iv_skew, 4)}")
        liquidity_bits: List[str] = []
        if liquid_contracts is not None:
            liquidity_bits.append(f"{_fmt_value(liquid_contracts, 0)} executable contracts")
        if executable_option_volume is not None:
            liquidity_bits.append(f"executable option volume {_fmt_value(executable_option_volume, 0)}")
        if executable_open_interest is not None:
            liquidity_bits.append(f"executable open interest {_fmt_value(executable_open_interest, 0)}")
        if avg_spread_pct is not None:
            liquidity_bits.append(f"executable weighted spread {_fmt_pct(avg_spread_pct, 3)}")
        if liquidity_bits:
            fragments.append("Liquidity shows " + ", ".join(liquidity_bits) + f". {_EXECUTABLE_LIQUIDITY_NOTE}")
        if fragments:
            return f"For {topic}, " + "; ".join(fragments) + "."

    if intent_family == "insider_flow_driven":
        sec_summary = _extract_sec_signal_summary(state, user_query)
        scope = state.get("scope_contract") or {}
        wants_options_surface = str(scope.get("primary_surface") or "").strip().lower() == "options_surface"
        liquidity_bits: List[str] = []
        if wants_options_surface and executable_option_volume is not None:
            liquidity_bits.append(f"executable option volume {_fmt_value(executable_option_volume, 0)}")
        if wants_options_surface and executable_open_interest is not None:
            liquidity_bits.append(f"executable open interest {_fmt_value(executable_open_interest, 0)}")
        if wants_options_surface and market_impact_risk:
            liquidity_bits.append(f"market impact risk on the executable subset {market_impact_risk}")
        liquidity_summary = "; ".join(liquidity_bits[:3])
        taxonomy_sentence = _sec_taxonomy_sentence(state)
        parts = [f"For {topic}, {sec_summary}."]
        if liquidity_summary:
            parts.append(f"Options liquidity shows {liquidity_summary}. {_EXECUTABLE_LIQUIDITY_NOTE}")
        missing_note = _missing_slot_disclosure_sentence(state)
        if missing_note and missing_note not in sec_summary:
            parts.append(missing_note)
        if taxonomy_sentence:
            parts.append(taxonomy_sentence)
        return " ".join(part.strip() for part in parts if part.strip())

    if intent_family == "cross_asset_regime":
        fragments: List[str] = []
        if atm_iv is not None:
            ticker_label = f"{options_ticker} " if options_ticker else ""
            iv_fragment = f"{ticker_label}ATM IV is {_fmt_value(atm_iv, 4)}"
            if iv_rank is not None:
                iv_fragment += f" with IV rank {_fmt_pct(iv_rank, 2)}"
            fragments.append(iv_fragment)
        if iv_skew is not None:
            fragments.append(f"IV skew reads {_fmt_value(iv_skew, 4)}")
        if vix_value is not None:
            vix_fragment = f"VIX is {_fmt_value(vix_value, 2)}"
            if vix_change is not None:
                vix_fragment += f" ({_fmt_pct(vix_change, 2)} move)"
            fragments.append(vix_fragment)
        if dxy_value is not None:
            dxy_fragment = f"DXY is {_fmt_value(dxy_value, 2)}"
            if dxy_change is not None:
                dxy_fragment += f" ({_fmt_pct(dxy_change, 2)} move)"
            fragments.append(dxy_fragment)
        if gpr_level is not None:
            fragments.append(f"GPR is {_fmt_value(gpr_level, 2)}")
        regime = str(((state.get("iv_regime_pinned") or {}).get("iv_regime")) or "").upper()
        regime_clause = ""
        if regime == "LOW":
            regime_clause = "This still reads as a lower-volatility regime than a stressed hedge regime."
        elif regime == "HIGH":
            regime_clause = "This still reads as a stressed, higher-premium hedge regime."
        elif regime == "NORMAL":
            regime_clause = "This still reads as a middle-of-the-range volatility regime."
        if fragments:
            body = f"For {topic}, " + "; ".join(fragments) + "."
            return f"{body} {regime_clause}".strip()

    if intent_family in {"geopolitical_macro_read", "geopolitical_options_read"}:
        fragments: List[str] = []
        if gpr_level is not None:
            gpr_fragment = f"GPR is {_fmt_value(gpr_level, 2)}"
            if gpr_percentile is not None:
                gpr_fragment += f" at the {_fmt_value(gpr_percentile)} percentile"
            fragments.append(gpr_fragment)
        if intent_family == "geopolitical_options_read" and atm_iv is not None:
            fragments.append(f"ATM IV is {_fmt_value(atm_iv, 4)}")
        if intent_family == "geopolitical_options_read" and liquid_contracts is not None:
            fragments.append(f"liquidity shows {_fmt_value(liquid_contracts, 0)} executable contracts")
        if fragments:
            return f"For {topic}, " + "; ".join(fragments) + "."

    if analyst_lines:
        return f"For {topic}, " + "; ".join(analyst_lines[:3]) + "."
    return f"For {topic}, the current conclusion is anchored to the checked in-scope evidence from this run."


def _build_mode_boundary_sentence(recommendation_mode: str) -> str:
    if recommendation_mode == "actionable_options":
        return "The current evidence is strong enough to discuss a concrete options structure, subject to the stated risk controls."
    if recommendation_mode == "directional_watchlist":
        return "The current evidence supports a directional/watchlist read, not a strike-level options action."
    return "The current evidence supports context and boundaries, but not a concrete options setup."


def _build_iv_regime_read(state: Dict[str, Any], values: Dict[str, Any]) -> str:
    atm_iv = values.get("latest_atm_iv")
    iv_rank = values.get("latest_atm_iv_rank_pct")
    regime = str(((state.get("iv_regime_pinned") or {}).get("iv_regime")) or "").upper()
    if atm_iv is None and iv_rank is None and not regime:
        return ""
    lead = f"The IV regime is {regime.lower()}" if regime in {"LOW", "NORMAL", "HIGH"} else "The volatility regime"
    metric_bits: List[str] = []
    if atm_iv is not None:
        metric_bits.append(f"ATM IV at {_fmt_value(atm_iv, 4)}")
    if iv_rank is not None:
        metric_bits.append(f"IV rank at {_fmt_pct(iv_rank, 2)}")
    if metric_bits:
        lead = f"{lead}, with " + " and ".join(metric_bits)
    if regime == "LOW":
        implication = "suggesting comparatively cheaper optionality and a muted premium backdrop."
    elif regime == "HIGH":
        implication = "suggesting elevated premium and a more stressed volatility backdrop."
    elif regime == "NORMAL":
        implication = "suggesting limited volatility expansion potential unless the regime shifts."
    else:
        implication = "suggesting a balanced, non-extreme volatility backdrop."
    return f"{lead}, {implication}"


def _build_liquidity_posture_read(
    *,
    values: Dict[str, Any],
    options_ticker: str,
    options_bundle: Dict[str, Any],
) -> str:
    liquid_contracts = options_bundle.get("liquid_contracts")
    executable_option_volume = options_bundle.get("executable_option_volume")
    executable_open_interest = options_bundle.get("executable_open_interest")
    avg_spread_pct = options_bundle.get("avg_spread_pct")
    market_impact_risk = options_bundle.get("market_impact_risk")
    if all(
        metric is None
        for metric in (
            liquid_contracts,
            executable_option_volume,
            executable_open_interest,
            avg_spread_pct,
            market_impact_risk,
        )
    ):
        return ""
    label = f"{options_ticker} " if options_ticker else ""
    bits: List[str] = []
    if liquid_contracts is not None:
        bits.append(f"{_fmt_value(liquid_contracts, 0)} executable contracts")
    if executable_option_volume is not None:
        bits.append(f"executable option volume {_fmt_value(executable_option_volume, 0)}")
    if executable_open_interest is not None:
        bits.append(f"executable open interest {_fmt_value(executable_open_interest, 0)}")
    if avg_spread_pct is not None:
        bits.append(f"executable weighted spread {_fmt_pct(avg_spread_pct, 3)}")
    sentence = f"{label}liquidity remains supported by " + ", ".join(bits)
    if market_impact_risk:
        sentence += f", while executable-subset market impact risk reads {market_impact_risk}"
    return f"{sentence}. {_EXECUTABLE_LIQUIDITY_NOTE}."


def _build_asset_read_evidence_recap(
    *,
    state: Dict[str, Any],
    draft: str,
    user_query: str,
) -> str:
    card = _finalizer_card(state)
    intent_family = canonical_query_family(str(card.get("query_family", "") or "").lower())
    narrative_seed = _asset_read_narrative_seed(state)
    support_seed = _asset_read_seed(state)
    if narrative_seed:
        return narrative_seed
    if support_seed and intent_family != "options_microstructure":
        return support_seed
    posture_rationale = _posture_rationale(state)
    base_regime_read = _base_regime_read(state)
    values = dict(card.get("key_numbers") or {}) if card.get("key_numbers") else _silver_values(state)
    pcr_volume = values.get("pcr_volume")
    pcr_oi = values.get("pcr_open_interest")
    pcr_status = values.get("pcr_status")
    iv_skew = values.get("latest_iv_skew")
    primary_ticker = resolve_primary_ticker(
        state=state,
        metadata=state.get("metadata"),
        scope_contract=state.get("scope_contract") or {},
    )
    options_ticker, options_bundle = resolve_first_available_ticker_bundle(
        values,
        [primary_ticker] if primary_ticker else [],
        ["executable_option_volume", "executable_open_interest", "liquid_contracts", "avg_spread_pct", "market_impact_risk"],
    )

    if intent_family == "options_microstructure":
        fragments: List[str] = []
        if posture_rationale:
            fragments.append(posture_rationale)
        if base_regime_read:
            fragments.append(base_regime_read)
        if pcr_volume is not None:
            pcr_fragment = f"PCR volume is {_fmt_value(pcr_volume, 3)}"
            if pcr_oi is not None:
                pcr_fragment += f" and PCR open interest is {_fmt_value(pcr_oi, 3)}"
            if pcr_status:
                pcr_fragment += f" ({pcr_status})"
            fragments.append(pcr_fragment + ".")
        iv_regime_read = _build_iv_regime_read(state, values)
        if iv_regime_read and not base_regime_read:
            fragments.append(iv_regime_read)
        if iv_skew is not None:
            fragments.append(f"IV skew reads {_fmt_value(iv_skew, 4)}, which helps frame the current put-versus-call volatility asymmetry.")
        liquidity_read = _build_liquidity_posture_read(
            values=values,
            options_ticker=options_ticker,
            options_bundle=options_bundle,
        )
        if liquidity_read:
            fragments.append(liquidity_read)
        if fragments:
            return " ".join(fragment.strip() for fragment in fragments if fragment.strip())

    if intent_family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
        strict_sentence = _build_strict_evidence_sentence(
            state=state,
            draft=draft,
            user_query=user_query,
        ).strip()
        outcome = state.get("retrieval_outcome") or {}
        if (
            intent_family == "geopolitical_macro_read"
            and bool(outcome.get("background_only_read"))
            and str(outcome.get("news_coverage_status") or "").strip().lower() == "no_fresh_news_retrieved"
        ):
            return _strip_topic_prefix(strict_sentence)
        if posture_rationale and base_regime_read:
            return f"{posture_rationale} {base_regime_read} {_strip_topic_prefix(strict_sentence)}".strip()
        if posture_rationale:
            return f"{posture_rationale} {_strip_topic_prefix(strict_sentence)}".strip()
        topic = re.sub(r"\s+", " ", (user_query or "the requested topic").strip())
        prefix = f"For {topic}, "
        if strict_sentence.startswith(prefix):
            return strict_sentence[len(prefix):].strip()
        return strict_sentence

    analyst_lines = list(card.get("analyst_evidence_lines") or [])
    if analyst_lines:
        return " ".join(str(line).strip().rstrip(".") + "." for line in analyst_lines[:2] if str(line).strip())
    if support_seed:
        return support_seed
    return ""


def _evidence_coverage_severity(state: Dict[str, Any]) -> str:
    constraints = _revision_constraints(state)
    return str(
        constraints.get("evidence_coverage_severity")
        or constraints.get("missing_metric_severity")
        or "none"
    ).strip().lower()


def _coverage_note_value(
    report: FinalReport,
    *,
    state: Dict[str, Any],
) -> str:
    note = str(report.evidence_coverage_note or "").strip()
    if note:
        return note
    constraints = _revision_constraints(state)
    return str(
        constraints.get("evidence_coverage_note")
        or constraints.get("missing_metric_note")
        or ""
    ).strip()


def _runtime_status_note(*, degraded: bool, degraded_reason: Optional[str]) -> str:
    if not degraded:
        return ""
    reason = str(degraded_reason or "").strip().lower()
    if reason.startswith("revision_cap_hit"):
        return "This answer is being delivered from the last reviewed state after the revision limit was reached."
    if reason == "retrieval_fallback_active":
        return "This answer is being delivered under retrieval fallback conditions, so it remains more constrained than a normal run."
    if reason.startswith("finalizer_llm_failure"):
        return "This answer is being rendered from checked structured fields after final formatting fallback engaged."
    return "This answer is being delivered under a constrained runtime state."


def _status_note_severity(*, state: Dict[str, Any], degraded: bool, degraded_reason: Optional[str]) -> str:
    if degraded and degraded_reason:
        return "soft_note"
    constraints = _revision_constraints(state)
    return str(constraints.get("answer_status_severity") or "none").strip().lower()


def _status_note_value(
    report: FinalReport,
    *,
    state: Dict[str, Any],
    degraded: bool,
    degraded_reason: Optional[str],
) -> str:
    note = str(report.status_note or "").strip()
    if note:
        return note
    safe_note = _status_note_seed(state)
    if safe_note:
        return safe_note
    constraints = _revision_constraints(state)
    critic_note = str(constraints.get("answer_status_note") or "").strip()
    if critic_note:
        return critic_note
    return _runtime_status_note(degraded=degraded, degraded_reason=degraded_reason)


def _safe_macro_summary_seed(state: Dict[str, Any]) -> str:
    seeded = _macro_summary_seed(state)
    family = canonical_query_family(str(_finalizer_card(state).get("query_family", "") or "").lower())
    supplemental_news_lines = [
        str(line).strip()
        for line in (_finalizer_card(state).get("supplemental_news_lines") or [])
        if str(line).strip()
    ]
    if seeded:
        if family in {"cross_asset_regime", "geopolitical_macro_read"} and supplemental_news_lines:
            return f"{seeded} Supplemental macro news: {' | '.join(supplemental_news_lines[:2])}."
        return seeded
    if family in {"cross_asset_regime", "geopolitical_macro_read"}:
        if supplemental_news_lines:
            return "Supplemental macro news: " + " | ".join(supplemental_news_lines[:2]) + "."
        return "No supplemental macro news was retrieved for this narrative window."
    return "No separate macro catalyst was required to support the current answer."


def _narrative_family(state: Dict[str, Any]) -> str:
    return canonical_query_family(str(_finalizer_card(state).get("query_family", "") or "").lower())


def _family_safe_mode_boundary_sentence(state: Dict[str, Any], recommendation_mode: str) -> str:
    family = _narrative_family(state)
    if family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
        if recommendation_mode == "directional_watchlist":
            return "The current evidence supports a directional macro/geopolitical watchlist read, not a live trade escalation."
        if recommendation_mode == "informational_only":
            return "The current evidence supports an informational macro/geopolitical read, not a live trade escalation."
    return _build_mode_boundary_sentence(recommendation_mode)


def _apply_render_safety_contract(
    report: FinalReport,
    *,
    state: Dict[str, Any],
    degraded: bool,
    degraded_reason: Optional[str],
    recommendation_mode: str,
) -> None:
    report.status_note = _status_note_value(
        report,
        state=state,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )
    report.macro_summary = _safe_macro_summary_seed(state)
    report._render_asset_read_text = _build_asset_read_evidence_recap(
        state=state,
        draft="",
        user_query=state.get("original_query", "") or "",
    )
    mandatory_risk = _mandatory_risk_sentence(state)
    report.key_risks_and_hedges = [mandatory_risk] if mandatory_risk else []


def _soft_missing_metric_tail_note(report: FinalReport, *, state: Dict[str, Any]) -> str:
    if _evidence_coverage_severity(state) != "soft_note":
        return ""
    note = _coverage_note_value(report, state=state)
    if not note:
        return ""
    return f"(Note: {note})"


def _build_asset_options_section_text(report: FinalReport) -> str:
    cached = getattr(report, "_render_asset_read_text", "") or ""
    if cached:
        return cached
    if not report.trade_ideas:
        return ""
    lead = report.trade_ideas[0]
    catalysts = ", ".join(lead.catalysts[:3]) if lead.catalysts else "no explicit near-term catalyst confirmed in context"
    evidence_bits = []
    if lead.option_strategy:
        evidence_bits.append(f"Structure: {lead.option_strategy}")
    if lead.ticker:
        evidence_bits.append(f"Underlying: {lead.ticker}")
    if lead.strike_details:
        evidence_bits.append(f"Strikes: {lead.strike_details}")
    if lead.expiration_date:
        evidence_bits.append(f"Expiry: {lead.expiration_date}")
    if lead.risk_profile:
        evidence_bits.append(f"Risk: {lead.risk_profile}")
    rationale = re.sub(r"\s+", " ", (lead.rationale or "").strip())
    lead_in = ". ".join(evidence_bits)
    return f"{lead_in}. Catalyst view: {catalysts}. {rationale}".strip()


def _build_recommendation_mode_text(report: FinalReport) -> str:
    explicit_mode = getattr(report, "_render_recommendation_mode", "") or ""
    boundary_text = getattr(report, "_render_mode_boundary_text", "") or ""
    illustrative_text = getattr(report, "_render_illustrative_structure_text", "") or ""
    if explicit_mode == "directional_watchlist":
        body = boundary_text or _build_mode_boundary_sentence(explicit_mode)
        if illustrative_text:
            body = f"{body} {illustrative_text}"
        return f"Directional watchlist: {body}"
    if explicit_mode == "informational_only":
        body = boundary_text or _build_mode_boundary_sentence(explicit_mode)
        if illustrative_text:
            body = f"{body} {illustrative_text}"
        return f"Informational only: {body}"
    if explicit_mode == "actionable_options":
        return (
            "Actionable options: "
            f"{boundary_text or _build_mode_boundary_sentence(explicit_mode)}"
        )
    if report.trade_ideas:
        return (
            "Actionable options: the current evidence is strong enough to present a concrete structure, "
            "but it should still be read with the stated risk controls and data-window caveats."
        )
    return (
        "Informational only: the report provides context and boundaries for the topic, "
        "but does not support a concrete options setup at the current evidence level."
    )


def _build_risks_section_text(report: FinalReport) -> str:
    if not report.key_risks_and_hedges:
        return "No additional market-risk escalation note was required for this answer."
    filtered = [str(r).strip() for r in report.key_risks_and_hedges if str(r).strip()]
    if not filtered:
        return "No additional market-risk escalation note was required for this answer."
    return " ".join(filtered)


def _mandatory_risk_sentence(state: Dict[str, Any]) -> str:
    risk_seed = _risk_seed(state)
    if risk_seed:
        return risk_seed
    escalation_risk = _escalation_risk_read(state)
    if escalation_risk:
        return escalation_risk
    constraints = _revision_constraints(state)
    if constraints.get("must_disclose_risk") and constraints.get("true_risk_text"):
        return str(constraints.get("true_risk_text")).strip()
    if constraints.get("must_disclose_risk") and constraints.get("main_risk_text"):
        return str(constraints.get("main_risk_text")).strip()
    return ""


def _market_read_only_flag(state: Dict[str, Any]) -> bool:
    return bool(_revision_constraints(state).get("market_read_only"))


def _why_not_now_sentence(state: Dict[str, Any]) -> str:
    constraints = _revision_constraints(state)
    if _evidence_coverage_severity(state) != "hard_gap":
        return ""
    if not constraints.get("must_explain_why_not_now"):
        return ""
    text = str(
        constraints.get("why_not_now_text")
        or constraints.get("what_must_change")
        or ""
    ).strip()
    if not text:
        return ""
    return text if text.endswith(".") else f"{text}."


def _illustrative_structure_text(
    *,
    state: Dict[str, Any],
    recommendation_mode: str,
) -> str:
    constraints = _revision_constraints(state)
    text = str(constraints.get("illustrative_structure_text") or "").strip()
    if text:
        return text
    if not constraints.get("allow_illustrative_structure"):
        return ""
    if constraints.get("structure_visibility_mode") != "illustrative_structure":
        return ""
    hint = str(constraints.get("illustrative_structure_hint") or "").strip()
    if not hint:
        return ""
    if recommendation_mode == "directional_watchlist":
        return f"Illustrative only: {hint} is the cleaner template to watch if conditions improve, but it is not a live recommendation."
    return f"Illustrative only: {hint} is a non-actionable example only, not a current or live recommendation."


def _build_non_actionable_risk_sentence(report: FinalReport, *, state: Dict[str, Any]) -> str:
    severity = _evidence_coverage_severity(state)
    disclosure_sentence = ""
    if severity == "hard_gap" and not _direct_answer_includes_missing_slot_disclosure(state):
        disclosure_sentence = _missing_slot_disclosure_sentence(state)
    why_not_now = _why_not_now_sentence(state)
    summary_caveat = _summary_caveat_seed(state)
    risk_bits = [bit for bit in (disclosure_sentence, why_not_now, summary_caveat) if bit]
    return " ".join(dict.fromkeys(risk_bits)).strip()


def _render_informational_only_reply(
    *,
    evidence_sentence: str,
    risk_sentence: str,
) -> str:
    body = f"{evidence_sentence}"
    if risk_sentence:
        body += f" {risk_sentence}"
    return _trim_to_word_limit(body)


def _render_market_read_only_reply(
    *,
    state: Dict[str, Any],
    evidence_sentence: str,
    risk_sentence: str,
) -> str:
    family = _narrative_family(state)
    if family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
        posture = ""
    elif family == "insider_flow_driven":
        posture = "Insider read: treat this as an informational SEC/insider-flow summary rather than a live options setup."
    else:
        posture = "Market posture: use this as a read-only options setup anchored to the current regime, IV, and liquidity read."
    body = f"{evidence_sentence} {posture}".strip()
    if risk_sentence:
        body += f" {risk_sentence}"
    return _trim_to_word_limit(body)


def _render_directional_watchlist_reply(
    *,
    evidence_sentence: str,
    risk_sentence: str,
) -> str:
    body = f"{evidence_sentence}"
    if risk_sentence:
        body += f" {risk_sentence}"
    return _trim_to_word_limit(body)


def _build_query_first_reply(
    report: FinalReport,
    *,
    state: Dict[str, Any],
    draft: str,
    user_query: str,
    recommendation_mode: str,
    reason: Optional[str] = None,
) -> str:
    if _scope_status(state) == "out_of_scope" or recommendation_mode != "actionable_options":
        evidence_sentence = _build_analyst_owned_evidence_sentence(
            state=state,
            draft=draft,
            user_query=user_query,
        )
    else:
        evidence_sentence = _build_strict_evidence_sentence(
            state=state,
            draft=draft,
            user_query=user_query,
        )
    risk_sentence = _build_non_actionable_risk_sentence(report, state=state)
    posture_takeaway = _posture_takeaway(state)
    direct_answer_seed_present = bool(_direct_answer_seed(state))
    direct_answer_includes_posture = _direct_answer_includes_posture_takeaway(state)
    if posture_takeaway and not direct_answer_seed_present and not direct_answer_includes_posture:
        evidence_sentence = f"{posture_takeaway} {_strip_topic_prefix(evidence_sentence)}".strip()
    reason_sentence = (
        f" Main caveat: {reason}."
        if reason and not _market_read_only_flag(state)
        else ""
    )

    if recommendation_mode == "actionable_options" and report.trade_ideas:
        lead = report.trade_ideas[0]
        structure = f"{lead.option_strategy} on {lead.ticker}".strip()
        structure_bits: List[str] = []
        if lead.strike_details:
            structure_bits.append(f"strikes {lead.strike_details}")
        if lead.expiration_date:
            structure_bits.append(f"expiry {lead.expiration_date}")
        structure_clause = (
            f" using {'; '.join(structure_bits)}" if structure_bits else ""
        )
        body = (
            f"{evidence_sentence} "
            f"The current structure favored in this run is {structure}{structure_clause}. "
            f"{risk_sentence or 'Main risk: keep sizing conservative because the setup can weaken if the evidence shifts.'}"
        )
        return _trim_to_word_limit(body)

    if recommendation_mode == "directional_watchlist":
        reply = _render_directional_watchlist_reply(
            evidence_sentence=evidence_sentence,
            risk_sentence=risk_sentence or "Main caution: the evidence is not strong enough to promote a clean trade idea.",
        )
    else:
        if _market_read_only_flag(state):
            reply = _render_market_read_only_reply(
                state=state,
                evidence_sentence=evidence_sentence,
                risk_sentence=risk_sentence or "Main risk: define the invalidation level before upgrading the posture.",
            )
        else:
            reply = _render_informational_only_reply(
                evidence_sentence=evidence_sentence,
                risk_sentence=risk_sentence or "Main caution: the evidence is not strong enough to promote a clean trade idea.",
            )
    soft_note = _soft_missing_metric_tail_note(report, state=state)
    if soft_note:
        reply = _trim_to_word_limit(f"{reply} {soft_note}")
    if reason_sentence:
        reply = _trim_to_word_limit(f"{reply} {reason_sentence.strip()}")
    return reply


def _enforce_recommendation_mode(
    report: FinalReport,
    *,
    state: Dict[str, Any],
    draft: str,
    recommendation_mode: str,
    user_query: str,
    reason: Optional[str],
    confidence_cap: float,
) -> None:
    """Apply the Critic-decided output mode to the final report."""
    constraints = _revision_constraints(state)
    report._render_recommendation_mode = recommendation_mode
    report._render_mode_boundary_text = str(
        constraints.get("mode_boundary_text") or _family_safe_mode_boundary_sentence(state, recommendation_mode)
    ).strip()
    report._render_illustrative_structure_text = _illustrative_structure_text(
        state=state,
        recommendation_mode=recommendation_mode,
    )
    report._render_asset_read_text = _build_asset_read_evidence_recap(
        state=state,
        draft=draft,
        user_query=user_query,
    )
    report.evidence_coverage_note = _coverage_note_value(report, state=state)
    if recommendation_mode == "actionable_options":
        report.conversation_reply = _build_query_first_reply(
            report,
            state=state,
            draft=draft,
            user_query=user_query,
            recommendation_mode=recommendation_mode,
        )
        return

    mandatory_risk = _mandatory_risk_sentence(state)
    market_read_only = _market_read_only_flag(state)
    report.trade_ideas = []
    ordered_risks: List[str] = []
    for item in (mandatory_risk,):
        if item and item not in ordered_risks:
            ordered_risks.append(item)
    report.key_risks_and_hedges = ordered_risks

    taxonomy = _sec_taxonomy_sentence(state)
    if taxonomy and taxonomy.lower() not in (report.macro_summary or "").lower():
        report.macro_summary = (
            f"{report.macro_summary.strip()}\n\n"
            f"{taxonomy}"
        ).strip()
    report.conversation_reply = _build_query_first_reply(
        report,
        state=state,
        draft=draft,
        user_query=user_query,
        recommendation_mode=recommendation_mode,
        reason=None if market_read_only else reason,
    )


# ==========================================
# FinalizerAgent class
# ==========================================

class FinalizerAgent:
    """LangGraph-compatible finalizer. Produces the terminal `final_strategy` dict.
    """

    def __init__(self):
        self.temperature = float(os.getenv("FINALIZER_TEMPERATURE", "0.0"))
        self.max_tokens = int(os.getenv("FINALIZER_MAX_TOKENS", "700"))
        self.timeout_s = float(os.getenv("FINALIZER_TIMEOUT_SECONDS", "120"))
        self.max_retries = int(os.getenv("FINALIZER_MAX_RETRIES", "1"))
        self.retry_backoff_s = float(os.getenv("FINALIZER_RETRY_BACKOFF_SECONDS", "1.0"))
        self.fast_fail_on_500 = os.getenv("FINALIZER_FAST_FAIL_ON_OLLAMA_500", "1") == "1"
        self.fallback_switch_sla_s = float(os.getenv("FINALIZER_FALLBACK_SWITCH_SLA_SECONDS", "1.0"))

        provider = os.getenv("FINALIZER_PROVIDER", "").strip().lower()
        if provider not in {"openai", "ollama"}:
            provider = "openai"
        self.provider = provider

        default_openai_model = os.getenv("FINALIZER_PRIMARY_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
        default_ollama_model = os.getenv("OLLAMA_FINALIZER_MODEL", "options-expert-v1:latest").strip() or "options-expert-v1:latest"
        self.model_name = os.getenv("FINALIZER_MODEL", "").strip() or (
            default_openai_model if self.provider == "openai" else default_ollama_model
        )
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.openai_timeout_s = float(
            os.getenv("FINALIZER_OPENAI_TIMEOUT_SECONDS", os.getenv("FINALIZER_OPENAI_FALLBACK_TIMEOUT_SECONDS", "45"))
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
            else (os.getenv("FINALIZER_OPENAI_FALLBACK_MODEL", default_openai_model).strip() or default_openai_model)
        )
        self.fallback_enabled = os.getenv(
            "FINALIZER_ENABLE_MODEL_FALLBACK",
            os.getenv("FINALIZER_OLLAMA_FALLBACK_ENABLED", "1"),
        ) == "1"

        self.max_macro_chars = int(os.getenv("FINALIZER_MAX_MACRO_CHARS", "2600"))
        self.max_draft_chars = int(os.getenv("FINALIZER_MAX_DRAFT_CHARS", "7000"))
        self.max_evidence_chars = int(os.getenv("FINALIZER_MAX_EVIDENCE_CHARS", "3000"))
        self.max_payload_chars = int(os.getenv("FINALIZER_MAX_PAYLOAD_CHARS", "12000"))

        self.llm = self._build_llm(self.provider, self.model_name)
        self.fallback_llm = (
            self._build_llm(self.fallback_provider, self.fallback_model_name)
            if self.fallback_enabled
            else None
        )

        self.prompt_chain = get_finalizer_prompt()

    def _build_ollama_llm(self, model_name: str):
        return ChatOpenAI(
            model=model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout_s,
        ).with_structured_output(FinalReport)

    def _build_openai_llm(self, model_name: str):
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": self.openai_api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.openai_timeout_s,
        }
        if self.openai_base_url:
            kwargs["base_url"] = self.openai_base_url
        return ChatOpenAI(**kwargs).with_structured_output(FinalReport)

    def _build_llm(self, provider: str, model_name: str):
        if provider == "ollama":
            return self._build_ollama_llm(model_name)
        return self._build_openai_llm(model_name)

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        s = (text or "").strip()
        if len(s) <= max_chars:
            return s
        return s[:max_chars] + "\n...(truncated)..."

    def _build_payload_text(
        self,
        *,
        user_query: str,
        draft: str,
        macro_context: str,
        narrative_brief_block: str,
        evidence_block: str,
        scope_contract_block: str,
        data_capability_block: str,
        time_range_block: str,
        minor_suggestions_block: str,
        revision_guardrails_block: str,
        degraded: bool,
        degraded_reason: Optional[str],
        revision_constraints: Optional[Dict[str, Any]],
    ) -> Dict[str, str]:
        constraints = revision_constraints or {}
        pipeline_flags = (
            f"degraded={degraded} | degraded_reason={degraded_reason or 'none'}"
            f" | market_read_only={bool(constraints.get('market_read_only'))}"
            f" | must_explain_why_not_now={bool(constraints.get('must_explain_why_not_now'))}"
        )
        return {
            "original_query": user_query,
            "draft": self._clip_text(draft, self.max_draft_chars),
            "macro_context": self._clip_text(macro_context, self.max_macro_chars),
            "narrative_brief_block": self._clip_text(narrative_brief_block, 1800),
            "evidence_block": self._clip_text(evidence_block, self.max_evidence_chars),
            "scope_contract_block": self._clip_text(scope_contract_block, 2200),
            "data_capability_block": self._clip_text(data_capability_block, 2200),
            "time_range_block": self._clip_text(time_range_block, 1200),
            "minor_suggestions_block": self._clip_text(minor_suggestions_block, 2000),
            "revision_guardrails_block": self._clip_text(revision_guardrails_block, 2500),
            "pipeline_flags": pipeline_flags,
        }

    async def _invoke_with_retry(self, llm, payload: Dict[str, Any], model_name: str):
        attempts = self.max_retries + 1
        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return await (self.prompt_chain | llm).ainvoke(payload)
            except Exception as exc:
                last_err = exc
                if self.fast_fail_on_500 and _is_ollama_runner_500(exc):
                    logger.warning(
                        "FinalizerAgent: detected Ollama 500 runner error; fail fast to fallback "
                        "(target_switch_sla=%.2fs).",
                        self.fallback_switch_sla_s,
                    )
                    raise exc
                if attempt < attempts:
                    await asyncio.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
        if last_err is not None:
            raise last_err
        raise RuntimeError("FinalizerAgent retry loop failed without explicit exception.")

    async def format_and_clean(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Convert the Analyst draft into a FinalReport dict.

        Returns a dict (not the Pydantic model) so it survives serialisation
        at the LangGraph state boundary. Shape::

            {
              "status": "complete" | "degraded",
              "final_report": <FinalReport.model_dump()>,
              "markdown": <FinalReport.to_markdown()>,
              "evidence_links": [ ... SourceCitation.model_dump ... ],
              "confidence_score": float,
            }
        """
        draft = state.get("draft_report", "") or ""
        user_query = state.get("original_query", "") or ""
        revision_n = state.get("revision_count", 0)
        is_fallback = bool(state.get("is_fallback", False))
        evidence_pool = _collect_evidence_pool(state)
        recommendation_mode = (
            _finalizer_card(state).get("recommendation_mode")
            or state.get("recommendation_mode")
            or (state.get("critic_reasoning_profile") or {}).get("recommendation_mode")
            or "directional_watchlist"
        )
        retrieval_confidence = _compute_retrieval_confidence(state)

        # Current-pass minor suggestions only. This keeps the Finalizer aligned
        # with the latest reviewer guidance instead of reopening stale history.
        minor_suggestions = _collect_current_revision_minor_suggestions(state)
        minor_block = (
            "\n".join(f"- {s}" for s in minor_suggestions)
            if minor_suggestions
            else "(none)"
        )

        # Render evidence pool as a readable, LLM-consumable block.
        evidence_block = "\n".join(
            f"- [{e.source_type}] {e.detail}" for e in evidence_pool
        ) or "(no evidence available — INSUFFICIENT DATA)"

        macro_ctx = _finalizer_card(state).get("macro_backdrop") or state.get("macro_context") or ""
        narrative_brief_block = _narrative_brief_block(state)
        scope_contract_block = _render_scope_contract_for_finalizer(state)
        data_capability_block = _render_data_capability_for_finalizer(state)
        time_range_block = _render_time_contract_for_finalizer(state)

        if _scope_status(state) == "out_of_scope":
            report = _out_of_scope_report(state=state, user_query=user_query)
            report.status_note = _status_note_value(
                report,
                state=state,
                degraded=False,
                degraded_reason=None,
            )
            _enforce_deterministic_report_date(report, state)
            return {
                "status": "complete",
                "degraded_reason": None,
                "final_report": report.model_dump(),
                "markdown": report.to_markdown(),
                "evidence_links": [e.model_dump() for e in evidence_pool],
                "report_provenance": _build_report_provenance(state),
                "confidence_score": report.confidence_score,
            }

        degraded = False
        degraded_reason: Optional[str] = None

        # If the pipeline already timed out at the revision cap OR the retrieval
        # layer degraded, we surface it rather than pretending everything is fine.
        if revision_n >= _MAX_REVISIONS:
            degraded = True
            degraded_reason = f"revision_cap_hit (n={revision_n})"
        elif is_fallback:
            degraded = True
            degraded_reason = "retrieval_fallback_active"

        if degraded:
            report = _degraded_report(
                reason=degraded_reason or "pipeline_degraded",
                user_query=user_query,
                evidence_pool=evidence_pool,
            )
            report.confidence_score = retrieval_confidence
            mandatory_risk = _mandatory_risk_sentence(state)
            if mandatory_risk and mandatory_risk not in report.key_risks_and_hedges:
                report.key_risks_and_hedges = [mandatory_risk] + list(report.key_risks_and_hedges or [])
            degraded_constraints = _revision_constraints(state)
            report._render_recommendation_mode = "informational_only"
            report._render_mode_boundary_text = str(
                degraded_constraints.get("mode_boundary_text") or _build_mode_boundary_sentence("informational_only")
            ).strip()
            report._render_illustrative_structure_text = _illustrative_structure_text(
                state=state,
                recommendation_mode="informational_only",
            )
            report._render_asset_read_text = _build_asset_read_evidence_recap(
                state=state,
                draft=draft,
                user_query=user_query,
            )
            report.conversation_reply = _build_query_first_reply(
                report,
                state=state,
                draft=draft,
                user_query=user_query,
                recommendation_mode="informational_only",
                reason=None,
            )
            _apply_render_safety_contract(
                report,
                state=state,
                degraded=True,
                degraded_reason=degraded_reason,
                recommendation_mode="informational_only",
            )
            _enforce_deterministic_report_date(report, state)
            output: Dict[str, Any] = {
                "status": "degraded",
                "degraded_reason": degraded_reason,
                "final_report": report.model_dump(),
                "markdown": report.to_markdown(),
                "evidence_links": [e.model_dump() for e in evidence_pool],
                "report_provenance": _build_report_provenance(state),
                "confidence_score": report.confidence_score,
            }
            return output

        report: FinalReport
        try:
            revision_constraints = _revision_constraints(state)
            revision_guardrails = _build_revision_guardrails_block(
                minor_suggestions=minor_suggestions,
                degraded=degraded,
                degraded_reason=degraded_reason,
                revision_constraints=revision_constraints,
            )
            payload_dict = self._build_payload_text(
                user_query=user_query,
                draft=draft,
                macro_context=macro_ctx[:3000] if macro_ctx else "(not available)",
                narrative_brief_block=narrative_brief_block,
                evidence_block=evidence_block,
                scope_contract_block=scope_contract_block,
                data_capability_block=data_capability_block,
                time_range_block=time_range_block,
                minor_suggestions_block=minor_block,
                revision_guardrails_block=revision_guardrails,
                degraded=degraded,
                degraded_reason=degraded_reason,
                revision_constraints=revision_constraints,
            )
            prefer_fallback = bool(state.get("analyst_fallback_used", False))
            active_llm = self.fallback_llm if (prefer_fallback and self.fallback_llm is not None) else self.llm
            active_model_name = (
                self.fallback_model_name if (prefer_fallback and self.fallback_llm is not None)
                else self.model_name
            )
            logger.info(
                f"FinalizerAgent: invoking LLM | revision={revision_n} | "
                f"evidence_n={len(evidence_pool)} | degraded={degraded} | model={active_model_name}"
            )
            report = await self._invoke_with_retry(
                active_llm,
                payload_dict,
                active_model_name,
            )
            if report is None:
                raise RuntimeError("Finalizer LLM returned empty report")

            # Post-hoc traceability guarantee: if the LLM produced trade ideas
            # but attached zero citations, backfill from the evidence pool
            # (conservative cap of 3 citations per idea).
            if report.trade_ideas and evidence_pool:
                for idea in report.trade_ideas:
                    if not idea.supporting_evidence:
                        idea.supporting_evidence = evidence_pool[:3]

            # Deterministic overrides — eliminate LLM drift on fields the
            # pipeline already knows exactly (docs/test/2026-04-22/
            # router_e2e_deep_analysis.md §5.1, §5.2).
            _enforce_deterministic_report_date(report, state)
            report.confidence_score = retrieval_confidence
            _reconcile_citation_source_types(report, evidence_pool)
            report.confidence_score = retrieval_confidence

            effective_mode = (
                "directional_watchlist"
                if recommendation_mode == "actionable_options" and not report.trade_ideas
                else recommendation_mode
            )
            _enforce_recommendation_mode(
                report,
                state=state,
                draft=draft,
                recommendation_mode=effective_mode,
                user_query=user_query,
                reason=None,
                confidence_cap=0.40 if effective_mode == "directional_watchlist" else (0.35 if revision_n == 0 else 0.30),
            )
            _apply_render_safety_contract(
                report,
                state=state,
                degraded=degraded,
                degraded_reason=degraded_reason,
                recommendation_mode=effective_mode,
            )

        except Exception as e:
            logger.warning("FinalizerAgent: primary/favored path failed: %s", e)
            # Secondary safety net: if current path fails and fallback exists, try fallback.
            if self.fallback_llm is not None:
                try:
                    revision_constraints = _revision_constraints(state)
                    fallback_started = asyncio.get_running_loop().time()
                    revision_guardrails = _build_revision_guardrails_block(
                        minor_suggestions=minor_suggestions,
                        degraded=degraded,
                        degraded_reason=degraded_reason,
                        revision_constraints=revision_constraints,
                    )
                    payload_dict = self._build_payload_text(
                        user_query=user_query,
                        draft=draft,
                        macro_context=macro_ctx[:3000] if macro_ctx else "(not available)",
                        narrative_brief_block=narrative_brief_block,
                        evidence_block=evidence_block,
                        scope_contract_block=scope_contract_block,
                        data_capability_block=data_capability_block,
                        time_range_block=time_range_block,
                        minor_suggestions_block=minor_block,
                        revision_guardrails_block=revision_guardrails,
                        degraded=degraded,
                        degraded_reason=degraded_reason,
                        revision_constraints=revision_constraints,
                    )
                    logger.warning(
                        "FinalizerAgent: switching to %s fallback model=%s",
                        self.fallback_provider,
                        self.fallback_model_name,
                    )
                    report = await self._invoke_with_retry(
                        self.fallback_llm,
                        payload_dict,
                        self.fallback_model_name,
                    )
                    switch_elapsed = asyncio.get_running_loop().time() - fallback_started
                    if switch_elapsed > self.fallback_switch_sla_s:
                        logger.warning(
                            "FinalizerAgent: fallback switch exceeded SLA %.2fs (actual=%.2fs)",
                            self.fallback_switch_sla_s,
                            switch_elapsed,
                        )
                    if report.trade_ideas and evidence_pool:
                        for idea in report.trade_ideas:
                            if not idea.supporting_evidence:
                                idea.supporting_evidence = evidence_pool[:3]
                    _enforce_deterministic_report_date(report, state)
                    _reconcile_citation_source_types(report, evidence_pool)
                    report.confidence_score = retrieval_confidence
                    effective_mode = (
                        "directional_watchlist"
                        if recommendation_mode == "actionable_options" and not report.trade_ideas
                        else recommendation_mode
                    )
                    _enforce_recommendation_mode(
                        report,
                        state=state,
                        draft=draft,
                        recommendation_mode=effective_mode,
                        user_query=user_query,
                        reason=None,
                        confidence_cap=0.40 if effective_mode == "directional_watchlist" else (0.35 if revision_n == 0 else 0.30),
                    )
                    _apply_render_safety_contract(
                        report,
                        state=state,
                        degraded=degraded,
                        degraded_reason=degraded_reason,
                        recommendation_mode=effective_mode,
                    )
                except Exception as fallback_err:
                    logger.exception(f"FinalizerAgent: fallback LLM call failed: {fallback_err}")
                    degraded = True
                    degraded_reason = f"finalizer_llm_failure:{type(fallback_err).__name__}"
                    report = _degraded_report(
                        reason=degraded_reason,
                        user_query=user_query,
                        evidence_pool=evidence_pool,
                    )
                    report.confidence_score = retrieval_confidence
                    _apply_render_safety_contract(
                        report,
                        state=state,
                        degraded=True,
                        degraded_reason=degraded_reason,
                        recommendation_mode="informational_only",
                    )
                    _enforce_deterministic_report_date(report, state)
            else:
                logger.exception(f"FinalizerAgent: LLM call failed: {e}")
                degraded = True
                degraded_reason = f"finalizer_llm_failure:{type(e).__name__}"
                report = _degraded_report(
                    reason=degraded_reason,
                    user_query=user_query,
                    evidence_pool=evidence_pool,
                )
                report.confidence_score = retrieval_confidence
                _apply_render_safety_contract(
                    report,
                    state=state,
                    degraded=True,
                    degraded_reason=degraded_reason,
                    recommendation_mode="informational_only",
                )
                # Keep the report_date deterministic even on the degraded path.
                _enforce_deterministic_report_date(report, state)

        output: Dict[str, Any] = {
            "status": "degraded" if degraded else "complete",
            "degraded_reason": degraded_reason,
            "final_report": report.model_dump(),
            "markdown": report.to_markdown(),
            "evidence_links": [e.model_dump() for e in evidence_pool],
            "report_provenance": _build_report_provenance(state),
            "confidence_score": report.confidence_score,
        }
        return output


# ==========================================
# LangGraph node wrapper — drop-in for router.py
# ==========================================

async def finalizer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: writes the structured final_strategy into state."""
    agent = FinalizerAgent()
    final_strategy = await agent.format_and_clean(state)
    return {"final_strategy": final_strategy}
