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
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama

from Scripts.agents.state import AgentFeedback
from Scripts.agents.analyst import _infer_iv_regime, _format_silver, _format_gold
from Scripts.agents.prompts import get_critic_prompt

logger = logging.getLogger(__name__)

_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))
_INSIDER_SIGNAL_MIN_COUNT = int(os.getenv("INSIDER_SIGNAL_MIN_COUNT", "3"))


# ==========================================
# Structured LLM output
# ==========================================

class LogicIssue(BaseModel):
    severity: Literal["Fatal", "Minor"] = Field(
        description="Fatal = strategy contradicts regime/macro; Minor = could be tighter"
    )
    category: Literal[
        "iv_regime_fit",
        "macro_contradiction",
        "insider_signal_weakness",
        "risk_reward_imbalance",
        "other",
    ] = Field(description="Which logic axis fails")
    comment: str = Field(description="Concrete pushback with a suggested correction")


class CriticResult(BaseModel):
    is_passed: bool = Field(description="True iff every logic axis is acceptable")
    issues: List[LogicIssue] = Field(default_factory=list)


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


# ==========================================
# CriticAgent class
# ==========================================

class CriticAgent:
    """Red-team logic critic. Scoped strictly to strategy — facts are the Checker's job."""

    def __init__(self):
        self.model_name = os.getenv("OLLAMA_CRITIC_MODEL", "options-expert-v1:latest")
        temperature = float(os.getenv("CRITIC_TEMPERATURE", "0.0"))
        self.logic_llm = ChatOllama(
            model=self.model_name,
            temperature=temperature,
            format="json",
        ).with_structured_output(CriticResult)

        self.prompt = get_critic_prompt()

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
        silver_ctx = state.get("silver_context", {}) or {}
        gold_ctx = state.get("gold_context", []) or []
        macro_ctx = state.get("macro_context", "") or ""
        user_query = state.get("original_query", "") or ""

        # Deterministic pre-computes give the LLM an unambiguous ground truth.
        iv_regime_info = _infer_iv_regime(silver_ctx)
        insider_info = _compute_insider_confidence(gold_ctx)

        feedbacks: List[AgentFeedback] = []

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
        # If regime is HIGH and the draft is clearly long premium, or regime
        # is LOW and the draft is clearly short premium, raise a hard rule.
        regime = iv_regime_info.get("iv_regime")
        if regime == "HIGH" and any(
            kw in lowered_draft for kw in ("long straddle", "long strangle", "buy protective", "buy insurance")
        ):
            feedbacks.append(AgentFeedback(
                sender="Critic",
                error_type="Fatal",
                comment=(
                    f"[iv_regime_fit] HIGH IV regime (atm_iv={iv_regime_info['atm_iv']}) — long premium "
                    "structure is fighting the regime. Prefer credit spreads / iron condors."
                ),
                revision_index=revision_n,
            ))
        elif regime == "LOW" and any(
            kw in lowered_draft for kw in ("iron condor", "credit spread", "sell premium", "short strangle")
        ):
            feedbacks.append(AgentFeedback(
                sender="Critic",
                error_type="Fatal",
                comment=(
                    f"[iv_regime_fit] LOW IV regime (atm_iv={iv_regime_info['atm_iv']}) — selling premium "
                    "is under-paid for the risk. Prefer long optionality / debit spreads."
                ),
                revision_index=revision_n,
            ))

        # -------- LLM logic critique (best-effort) --------
        try:
            chain = self.prompt | self.logic_llm
            logger.info(
                f"CriticAgent: invoking logic LLM | iv_regime={iv_regime_info['iv_regime']} "
                f"| insider_verdict={insider_info['verdict']}"
            )
            result: CriticResult = await chain.ainvoke({
                "original_query": user_query,
                "macro_context": macro_ctx,
                "iv_regime": iv_regime_info,
                "insider_confidence": insider_info,
                "silver_block": _format_silver(silver_ctx),
                "gold_block": _format_gold(gold_ctx),
                "draft": draft,
            })
            if not result.is_passed:
                for issue in result.issues:
                    feedbacks.append(AgentFeedback(
                        sender="Critic",
                        error_type=issue.severity,
                        comment=f"[{issue.category}] {issue.comment}",
                        revision_index=revision_n,
                    ))
        except Exception as e:
            logger.warning(
                f"CriticAgent: logic LLM failed ({type(e).__name__}); "
                f"deterministic findings retained. Error: {e}"
            )

        verdict = self._compute_verdict(feedbacks)
        logger.info(f"CriticAgent: verdict={verdict} | total_findings={len(feedbacks)}")

        return {
            "critic_feedback": feedbacks,
            "critic_verdict": verdict,
        }

    @staticmethod
    def _compute_verdict(feedbacks: List[AgentFeedback]) -> str:
        if not feedbacks:
            return "pass"
        if any(fb.error_type.lower() == "fatal" for fb in feedbacks):
            return "fatal"
        return "minor"
