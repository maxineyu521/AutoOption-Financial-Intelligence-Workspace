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
2. **Traceability (Audit Trail)**  — populates every TradeIdea.supporting_evidence
   with real lineage IDs from state["silver_context"]["lineage_anchors"] and
   state["gold_context"][*].bronze_ref. This satisfies the financial-compliance
   requirement called out in the original docstring.
3. **Confidence score reflects data, not vibes**  — if Gold or Silver layers
   returned nothing / errored, the confidence score is capped. If the
   revision counter hit the hard limit, we annotate the report as "degraded".
4. **LangGraph-friendly**  — `FinalizerAgent.format_and_clean(state)` returns
   the dict that the router writes into `state["final_strategy"]`.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama

from Scripts.agents.prompts import get_finalizer_prompt

logger = logging.getLogger(__name__)

# Hard revision cap — must match checker.py / critic.py / router.py.
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))


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
        description="Systemic risks that could invalidate these trade ideas and suggested hedges."
    )

    confidence_score: float = Field(
        ge=0.0, le=1.0,
        description="Overall confidence in the report's conclusions based on data alignment."
    )

    def to_markdown(self) -> str:
        """Converts the structured Pydantic object into a clean, readable Markdown report for UI display."""
        md_lines = [
            f"# 📊 Institutional Options Strategy Report",
            f"**Generated on:** {self.report_date}",
            f"**Overall Confidence Score:** {self.confidence_score * 100:.1f}%",
            f"\n## 🌍 Macro Context Summary",
            f"{self.macro_summary}",
            f"\n## 💡 Top Trade Ideas"
        ]

        for idx, trade in enumerate(self.trade_ideas, 1):
            md_lines.extend([
                f"\n### Idea {idx}: {trade.option_strategy} on {trade.ticker} ({trade.market_outlook})",
                f"- **Asset Class:** {trade.asset_class}",
                f"- **Target Strike(s):** {trade.strike_details}",
                f"- **Expiration:** {trade.expiration_date}",
                f"- **Risk Profile:** {trade.risk_profile}",
                f"- **Catalysts:** {', '.join(trade.catalysts)}",
                f"\n**Investment Rationale:**",
                f"{trade.rationale}",
            ])

            if trade.supporting_evidence:
                md_lines.append("\n**Supporting Evidence (RAG Context):**")
                for evidence in trade.supporting_evidence:
                    md_lines.append(f"  - *{evidence.source_type}:* {evidence.detail}")

            md_lines.append("\n---")  # Separator between trades

        md_lines.append(f"\n## ⚠️ Systemic Risks & Hedging Considerations")
        for risk in self.key_risks_and_hedges:
            md_lines.append(f"- {risk}")

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

    silver_ctx = state.get("silver_context", {}) or {}
    for anchor in silver_ctx.get("lineage_anchors", []) or []:
        anchor_s = str(anchor)
        # Route Silver anchors by prefix into the closest UI category. The
        # anchor prefix convention comes from sql_tools.py lineage writes
        # (e.g. "PCR_AGG_SPY_2026-04-19", "MACRO_VIX_...", "GPR_202604").
        if anchor_s.startswith("GPR_"):
            st: Literal["Macro Data", "SEC Filing", "Global News", "GPR Index", "Options Market Data"] = "GPR Index"
        elif anchor_s.startswith("MACRO_"):
            st = "Macro Data"
        else:
            st = "Options Market Data"
        pool.append(SourceCitation(source_type=st, detail=f"Silver lineage anchor: {anchor_s}"))

    for chunk in state.get("gold_context", []) or []:
        src = getattr(chunk, "source_type", None) or (chunk.get("source_type", "") if isinstance(chunk, dict) else "")
        meta = getattr(chunk, "metadata", None) or (chunk.get("metadata", {}) if isinstance(chunk, dict) else {})
        br = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref", "UNKNOWN") if isinstance(chunk, dict) else "UNKNOWN")
        record_date = meta.get("record_date", "unknown") if isinstance(meta, dict) else "unknown"

        src_l = str(src).lower()
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

    return pool


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
    draft: str,
    evidence_pool: List[SourceCitation],
    cap_confidence: float = 0.20,
) -> FinalReport:
    """Safe fallback when the Finalizer LLM fails or the pipeline is degraded.

    We never throw from this node — that would strand the entire graph.
    Instead we return a FinalReport that explicitly declares degradation,
    carries whatever evidence we have, and sets a low confidence score.
    """
    return FinalReport(
        macro_summary=(
            "DEGRADED MODE — finalizer could not produce a structured report. "
            f"Reason: {reason}. Raw analyst draft preserved below (first 400 chars):\n\n"
            f"{(draft or '')[:400]}"
        ),
        trade_ideas=[],
        key_risks_and_hedges=[
            "Automated pipeline degradation — do not act on partial outputs.",
            "Re-run the pipeline once upstream data + LLM availability is restored.",
        ],
        confidence_score=cap_confidence,
    )


# ==========================================
# FinalizerAgent class
# ==========================================

class FinalizerAgent:
    """LangGraph-compatible finalizer. Produces the terminal `final_strategy` dict."""

    def __init__(self):
        self.model_name = os.getenv("OLLAMA_FINALIZER_MODEL", "options-expert-v1:latest")
        temperature = float(os.getenv("FINALIZER_TEMPERATURE", "0.0"))
        self.llm = ChatOllama(
            model=self.model_name,
            temperature=temperature,
            format="json",
        ).with_structured_output(FinalReport)

        # Prompt comes from the single source of truth; variables expected by the
        # template are: original_query, draft, evidence_block.
        self.prompt = get_finalizer_prompt()

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

        # Render evidence pool as a readable, LLM-consumable block.
        evidence_block = "\n".join(
            f"- [{e.source_type}] {e.detail}" for e in evidence_pool
        ) or "(no evidence available — INSUFFICIENT DATA)"

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

        report: FinalReport
        try:
            chain = self.prompt | self.llm
            logger.info(
                f"FinalizerAgent: invoking LLM | revision={revision_n} | "
                f"evidence_n={len(evidence_pool)} | degraded={degraded}"
            )
            report = await chain.ainvoke({
                "original_query": user_query,
                "draft": draft,
                "evidence_block": evidence_block,
            })

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
            _reconcile_citation_source_types(report, evidence_pool)

            # Enforce confidence ceilings by regime.
            if degraded:
                report.confidence_score = min(report.confidence_score, 0.30)
            elif revision_n > 0:
                report.confidence_score = min(report.confidence_score, 0.60)

        except Exception as e:
            logger.exception(f"FinalizerAgent: LLM call failed: {e}")
            degraded = True
            degraded_reason = f"finalizer_llm_failure:{type(e).__name__}"
            report = _degraded_report(
                reason=degraded_reason,
                draft=draft,
                evidence_pool=evidence_pool,
            )
            # Keep the report_date deterministic even on the degraded path.
            _enforce_deterministic_report_date(report, state)

        return {
            "status": "degraded" if degraded else "complete",
            "degraded_reason": degraded_reason,
            "final_report": report.model_dump(),
            "markdown": report.to_markdown(),
            "evidence_links": [e.model_dump() for e in evidence_pool],
            "confidence_score": report.confidence_score,
        }


# ==========================================
# LangGraph node wrapper — drop-in for router.py
# ==========================================

async def finalizer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node: writes the structured final_strategy into state."""
    agent = FinalizerAgent()
    final_strategy = await agent.format_and_clean(state)
    return {"final_strategy": final_strategy}
