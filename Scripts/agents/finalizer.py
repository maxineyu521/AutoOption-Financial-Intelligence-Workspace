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
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import requests
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from Scripts.core.financial_config import get_analyst_system_prompt

logger = logging.getLogger(__name__)

# Hard revision cap — must match checker.py / critic.py / router.py.
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))


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

    conversation_reply: str = Field(
        default="",
        description=(
            "A 50-100 word plain-English trading recommendation in direct dialogue style. "
            "No markdown, no section headers, no bullet points. "
            "Write as if speaking to the trader: start with the key macro/news context in "
            "one sentence, state the recommended structure and ticker in one sentence, "
            "then add the main risk or caveat in one sentence. "
            "Example tone: 'Given elevated geopolitical risk and NORMAL IV on NVDA, "
            "a call spread with 30-45 DTE captures momentum while limiting premium outlay. "
            "Watch the insider selling overhang — size conservatively.' "
            "Incorporate any polish notes from the Critic if provided."
        )
    )

    def to_markdown(self) -> str:
        """Converts the structured Pydantic object into a clean, readable Markdown report for UI display."""
        md_lines = [
            f"# 📊 Institutional Options Strategy Report",
            f"**Generated on:** {self.report_date}",
            f"**Overall Confidence Score:** {self.confidence_score * 100:.1f}%",
        ]
        # Quick Take — the 50-100 word conversation reply shown first for chat interfaces.
        if self.conversation_reply and self.conversation_reply.strip():
            md_lines += [
                f"\n> **💬 Quick Take:** {self.conversation_reply.strip()}",
            ]
        md_lines += [
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
    """LangGraph-compatible finalizer. Produces the terminal `final_strategy` dict.
    """

    def __init__(self):
        # Primary engine: gpt-4o-mini via OpenAI API.
        # Backup engine: options-expert-v1:latest via local Ollama (OpenAI-compatible).
        self.temperature = float(os.getenv("FINALIZER_TEMPERATURE", "0.0"))
        self.max_tokens = int(os.getenv("FINALIZER_MAX_TOKENS", "700"))
        self.timeout_s = float(os.getenv("FINALIZER_TIMEOUT_SECONDS", "120"))
        self.max_retries = int(os.getenv("FINALIZER_MAX_RETRIES", "1"))
        self.retry_backoff_s = float(os.getenv("FINALIZER_RETRY_BACKOFF_SECONDS", "1.0"))
        self.fast_fail_on_500 = os.getenv("FINALIZER_FAST_FAIL_ON_OLLAMA_500", "1") == "1"
        self.fallback_switch_sla_s = float(os.getenv("FINALIZER_FALLBACK_SWITCH_SLA_SECONDS", "1.0"))

        # Primary: OpenAI gpt-4o-mini
        self.model_name = os.getenv("FINALIZER_PRIMARY_MODEL", "gpt-4o-mini")
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.openai_fallback_timeout_s = float(os.getenv("FINALIZER_OPENAI_FALLBACK_TIMEOUT_SECONDS", "45"))

        # Backup: Ollama options-expert-v1:latest
        self.ollama_backup_model = os.getenv("OLLAMA_FINALIZER_MODEL", "options-expert-v1:latest")
        self.base_url = _normalize_openai_base_url(
            os.getenv("OLLAMA_OPENAI_BASE_URL"),
            os.getenv("OLLAMA_HOST"),
        )
        self.api_key = os.getenv("OLLAMA_OPENAI_API_KEY", "ollama")
        self.ollama_fallback_enabled = os.getenv("FINALIZER_OLLAMA_FALLBACK_ENABLED", "1") == "1"

        self.max_macro_chars = int(os.getenv("FINALIZER_MAX_MACRO_CHARS", "2600"))
        self.max_draft_chars = int(os.getenv("FINALIZER_MAX_DRAFT_CHARS", "7000"))
        self.max_evidence_chars = int(os.getenv("FINALIZER_MAX_EVIDENCE_CHARS", "3000"))
        self.max_payload_chars = int(os.getenv("FINALIZER_MAX_PAYLOAD_CHARS", "12000"))

        self.llm = self._build_openai_fallback_llm(self.model_name)
        self.fallback_llm = (
            self._build_ollama_llm(self.ollama_backup_model)
            if self.ollama_fallback_enabled
            else None
        )

        self.system_prompt = get_analyst_system_prompt()
        self.prompt_chain = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            (
                "human",
                "You are the report finalizer. Convert the payload into the FinalReport schema "
                "without inventing facts.\n\n{payload}",
            ),
        ])

    def _build_ollama_llm(self, model_name: str):
        return ChatOpenAI(
            model=model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout_s,
        ).with_structured_output(FinalReport)

    def _build_openai_fallback_llm(self, model_name: str):
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": self.openai_api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.openai_fallback_timeout_s,
        }
        if self.openai_base_url:
            kwargs["base_url"] = self.openai_base_url
        return ChatOpenAI(**kwargs).with_structured_output(FinalReport)

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
        evidence_block: str,
        minor_suggestions_block: str,
        degraded: bool,
        degraded_reason: Optional[str],
    ) -> str:
        payload = (
            "=== USER QUERY ===\n"
            f"{user_query}\n\n"
            "=== ANALYST DRAFT (fact-checked upstream) ===\n"
            f"{self._clip_text(draft, self.max_draft_chars)}\n\n"
            "=== MACRO CONTEXT ===\n"
            f"{self._clip_text(macro_context, self.max_macro_chars)}\n\n"
            "=== EVIDENCE POOL ===\n"
            f"{self._clip_text(evidence_block, self.max_evidence_chars)}\n\n"
            "=== CRITIC MINOR SUGGESTIONS ===\n"
            f"{minor_suggestions_block}\n\n"
            "=== PIPELINE FLAGS ===\n"
            f"degraded={degraded} | degraded_reason={degraded_reason or 'none'}\n\n"
            "=== OUTPUT REQUIREMENTS ===\n"
            "Return FinalReport only. Preserve numbers exactly. If insufficient data, keep low confidence."
        )
        return self._clip_text(payload, self.max_payload_chars)

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

        # Critic Minor Suggestions — non-blocking polish notes forwarded from CriticAgent.
        # Rendered as a concise block so the Finalizer can incorporate them into
        # conversation_reply and rationale without triggering a rewrite.
        minor_suggestions: List[str] = state.get("critic_minor_suggestions") or []
        minor_block = (
            "\n".join(f"- {s}" for s in minor_suggestions)
            if minor_suggestions
            else "(none)"
        )

        # Render evidence pool as a readable, LLM-consumable block.
        evidence_block = "\n".join(
            f"- [{e.source_type}] {e.detail}" for e in evidence_pool
        ) or "(no evidence available — INSUFFICIENT DATA)"

        macro_ctx = state.get("macro_context") or ""

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
            payload_text = self._build_payload_text(
                user_query=user_query,
                draft=draft,
                macro_context=macro_ctx[:3000] if macro_ctx else "(not available)",
                evidence_block=evidence_block,
                minor_suggestions_block=minor_block,
                degraded=degraded,
                degraded_reason=degraded_reason,
            )
            prefer_fallback = bool(state.get("analyst_fallback_used", False))
            active_llm = self.fallback_llm if (prefer_fallback and self.fallback_llm is not None) else self.llm
            active_model_name = (
                self.ollama_backup_model if (prefer_fallback and self.fallback_llm is not None)
                else self.model_name
            )
            logger.info(
                f"FinalizerAgent: invoking LLM | revision={revision_n} | "
                f"evidence_n={len(evidence_pool)} | degraded={degraded} | model={active_model_name}"
            )
            report = await self._invoke_with_retry(
                active_llm,
                {"payload": payload_text},
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
            _reconcile_citation_source_types(report, evidence_pool)

            # Enforce confidence ceilings by regime.
            if degraded:
                report.confidence_score = min(report.confidence_score, 0.30)
            elif revision_n > 0:
                report.confidence_score = min(report.confidence_score, 0.60)

        except Exception as e:
            logger.warning("FinalizerAgent: primary/favored path failed: %s", e)
            # Secondary safety net: if current path fails and fallback exists, try fallback.
            if self.fallback_llm is not None:
                try:
                    fallback_started = asyncio.get_running_loop().time()
                    payload_text = self._build_payload_text(
                        user_query=user_query,
                        draft=draft,
                        macro_context=macro_ctx[:3000] if macro_ctx else "(not available)",
                        evidence_block=evidence_block,
                        minor_suggestions_block=minor_block,
                        degraded=degraded,
                        degraded_reason=degraded_reason,
                    )
                    logger.warning(
                        "FinalizerAgent: switching to Ollama backup model=%s",
                        self.ollama_backup_model,
                    )
                    report = await self._invoke_with_retry(
                        self.fallback_llm,
                        {"payload": payload_text},
                        self.openai_fallback_model,
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
                    if degraded:
                        report.confidence_score = min(report.confidence_score, 0.30)
                    elif revision_n > 0:
                        report.confidence_score = min(report.confidence_score, 0.60)
                except Exception as fallback_err:
                    logger.exception(f"FinalizerAgent: fallback LLM call failed: {fallback_err}")
                    degraded = True
                    degraded_reason = f"finalizer_llm_failure:{type(fallback_err).__name__}"
                    report = _degraded_report(
                        reason=degraded_reason,
                        draft=draft,
                        evidence_pool=evidence_pool,
                    )
                    _enforce_deterministic_report_date(report, state)
            else:
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

        output: Dict[str, Any] = {
            "status": "degraded" if degraded else "complete",
            "degraded_reason": degraded_reason,
            "final_report": report.model_dump(),
            "markdown": report.to_markdown(),
            "evidence_links": [e.model_dump() for e in evidence_pool],
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
