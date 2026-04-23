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
   `[Silver: <lineage_anchor>]` or `[Gold: <bronze_ref>]`. Downstream the
   CheckerAgent enforces this.
4. **Revision-aware**: on every re-entry the agent sees the full append-only
   feedback log from state["critic_feedback"] (both Checker + Critic senders)
   and MUST address every unresolved Fatal error.
5. **Temporal decay hint**: the LLM is explicitly told to weight newer
   gold_context entries (fresh record_date) more heavily than stale ones.
6. **Graceful degradation**: LLM failures surface as a stub draft that
   explicitly says "INSUFFICIENT DATA" rather than a hallucinated report.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from langchain_ollama import ChatOllama

from Scripts.agents.prompts import get_analyst_prompt, render_revision_block

logger = logging.getLogger(__name__)


class AnalystResult:
    """Typed envelope for the analyst node's multi-field return.

    Using a tiny structured return instead of a raw string lets the router
    plumb `iv_regime_pinned` into AgentState without awkwardly breaking
    backwards-compat with the Markdown-only contract (a plain string is
    still accessible via `.draft`).
    """

    __slots__ = ("draft", "iv_regime")

    def __init__(self, draft: str, iv_regime: Dict[str, Any]):
        self.draft = draft
        self.iv_regime = iv_regime


# ==========================================
# IV Regime Classifier (deterministic, no LLM)
# ==========================================
# Threshold defaults are conservative; can be tuned via env overrides without touching code.
_IV_HIGH_DEFAULT = float(os.getenv("IV_REGIME_HIGH_THRESHOLD", "0.35"))
_IV_LOW_DEFAULT = float(os.getenv("IV_REGIME_LOW_THRESHOLD", "0.18"))


def _infer_iv_regime(
    silver_context: Dict[str, Any],
    high: float = _IV_HIGH_DEFAULT,
    low: float = _IV_LOW_DEFAULT,
) -> Dict[str, Any]:
    """Classify the current IV regime from silver_context.values.

    Inspects the `latest_atm_iv` key written by
    `Scripts/retrieval/sql_tools._handle_options_analysis`. Also surfaces
    Put/Call Ratio when present because PCR drives the regime narrative.

    Returns a stable dict the LLM prompt can consume verbatim:
        {
          "iv_regime": "HIGH" | "LOW" | "NORMAL" | "UNKNOWN",
          "atm_iv": float | None,
          "pcr_volume": float | None,
          "pcr_status": str | None,
          "thresholds": {"high": ..., "low": ...}
        }

    Note on "IV Rank": we use absolute IV thresholds here because the Silver
    layer does not yet expose a historical IV distribution per symbol. The
    TODO in `sql_tools.py` is to add an `iv_rank` column; once that exists
    this function should switch to percentile-based ranking (see README).
    """
    values = (silver_context or {}).get("values", {}) if isinstance(silver_context, dict) else {}
    atm_iv = values.get("latest_atm_iv")
    pcr_vol = values.get("pcr_volume")
    pcr_status = values.get("pcr_status")

    if not isinstance(atm_iv, (int, float)):
        regime: Literal["HIGH", "LOW", "NORMAL", "UNKNOWN"] = "UNKNOWN"
    elif atm_iv >= high:
        regime = "HIGH"
    elif atm_iv <= low:
        regime = "LOW"
    else:
        regime = "NORMAL"

    return {
        "iv_regime": regime,
        "atm_iv": atm_iv,
        "pcr_volume": pcr_vol,
        "pcr_status": pcr_status,
        "thresholds": {"high": high, "low": low},
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
    if not values:
        return "(silver returned no values — INSUFFICIENT DATA)"

    lines = ["| metric | value |", "|---|---|"]
    for k, v in values.items():
        lines.append(f"| {k} | {v} |")
    if lineage:
        lines.append("")
        lines.append("LINEAGE ANCHORS (use these as [Silver: <id>] citations):")
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
            f"[{i}] source={src} | ticker={ticker} | record_date={record_date} | "
            f"bronze_ref={bronze}\n    {snippet}"
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
        self.model_name = os.getenv("OLLAMA_ANALYST_MODEL", "options-expert-v1:latest")
        temperature = float(os.getenv("ANALYST_TEMPERATURE", "0.1"))
        self.llm = ChatOllama(model=self.model_name, temperature=temperature)
        # Prompt is now sourced from the single-source-of-truth prompts module.
        self.prompt = get_analyst_prompt()

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
        silver_ctx = state.get("silver_context", {}) or {}
        gold_ctx = state.get("gold_context", []) or []
        feedback_log = state.get("critic_feedback", []) or []
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
            f"| pcr_volume={iv_regime['pcr_volume']} | pcr_status={iv_regime['pcr_status']} "
            f"| thresholds={iv_regime['thresholds']}"
        )

        # Revision-aware prompting — empty on the first pass.
        revision_block = render_revision_block(feedback_log)

        try:
            chain = self.prompt | self.llm
            logger.info(
                f"AnalystAgent: invoking LLM | revision={revision_n} | "
                f"iv_regime={iv_regime['iv_regime']} | gold_n={len(gold_ctx)} | "
                f"silver_values_n={len(silver_ctx.get('values', {}))}"
            )
            response = await chain.ainvoke({
                "original_query": user_query,
                "macro_context": macro_ctx,
                "iv_regime_block": iv_regime_block,
                "silver_block": _format_silver(silver_ctx),
                "gold_block": _format_gold(gold_ctx),
                "revision_block": revision_block,
            })
            draft = getattr(response, "content", str(response)).strip()
            if not draft:
                raise RuntimeError("LLM returned empty draft")
            return AnalystResult(draft=draft, iv_regime=iv_regime)

        except Exception as e:
            logger.exception(f"AnalystAgent: LLM call failed: {e}")
            fallback_draft = (
                "## Analyst Draft — DEGRADED MODE\n"
                "INSUFFICIENT DATA — the Analyst LLM call failed. No recommendation "
                "will be produced until the model is available again.\n\n"
                f"Diagnostic: {type(e).__name__}"
            )
            return AnalystResult(draft=fallback_draft, iv_regime=iv_regime)
