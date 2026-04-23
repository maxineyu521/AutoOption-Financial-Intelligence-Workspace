"""
Scripts/agents/checker.py

Checker Agent (Blue Team) — deterministic + LLM fact & lineage audit of the
Analyst draft, with an opt-in Silver-tool escape hatch for missing anchors.

Design philosophy:
- The Checker is NOT a strategist. It is a silent, uncompromising data-integrity
  auditor. Its only question is: "does every number match the Silver Layer, and
  does every citation point at a known anchor?"
- Three defence layers:
    1. DETERMINISTIC regex pass — extracts every number from the draft and
       verifies it is present in silver_context (tolerant to percentage
       scale). Citations checked against known lineage anchors / bronze_refs.
    2. TOOL-CALL ESCAPE HATCH (opt-in via CHECKER_TOOL_RECOVERY=1) —
       when a citation anchor is unknown, Checker calls `SilverSQLTool.
       query_parquet_by_metadata(state["metadata"])` to re-query the Silver
       Layer. If the re-query surfaces the anchor / value, the finding is
       downgraded to Minor and the refreshed silver_context is returned so
       downstream nodes see the latest data. This realises the architect
       brief: "Retriever 预取 + Checker 缺失时动态 Tool call 回查库."
    3. LLM SEMANTIC pass — catches paraphrased fabrications (best-effort,
       non-fatal on LLM error).

State I/O:
  reads : state["draft_report"], state["silver_context"], state["gold_context"],
          state["metadata"], state["revision_count"]
  writes: {
      "critic_feedback": List[AgentFeedback] (append-only; sender="Checker"),
      "checker_verdict": "pass" | "fatal" | "minor",
      # optional, only when tool-call rescue refreshed the context:
      "silver_context": Dict[str, Any],
  }
"""

from __future__ import annotations

import os
import re
import logging
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama

from Scripts.agents.state import AgentFeedback
from Scripts.agents.prompts import get_checker_prompt

logger = logging.getLogger(__name__)

# Tolerance for numeric match — LLMs occasionally reformat "2.35%" as "2.4%".
# 2% relative tolerance is tight enough to catch real fabrications but loose
# enough to survive reasonable rounding. Tunable via env for strict regimes.
_NUMERIC_REL_TOLERANCE = float(os.getenv("CHECKER_NUMERIC_TOLERANCE", "0.02"))

# Hard cap on revisions — mirror the router constant.
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))

# Whether Checker is allowed to re-query Silver on a citation miss. Default on
# — failing closed (i.e. flag Fatal) is still safe, but allowing rescue lets
# us recover when Retriever happened to miss a metric on the first pass.
_TOOL_RECOVERY_ENABLED = os.getenv("CHECKER_TOOL_RECOVERY", "1") == "1"


# ==========================================
# Structured LLM output
# ==========================================

class CheckerViolation(BaseModel):
    severity: str = Field(description="'Fatal' for any number mismatch; 'Minor' for missing citations only")
    description: str = Field(description="What was hallucinated vs what the real data says")
    cited_silver_value: Optional[str] = Field(default=None, description="Ground-truth value from silver_context")
    draft_value: Optional[str] = Field(default=None, description="Value claimed by the draft")


class CheckerResult(BaseModel):
    is_passed: bool = Field(description="True iff every claim is grounded AND cited")
    violations: List[CheckerViolation] = Field(default_factory=list)


# ==========================================
# Regex pass (deterministic layer 1)
# ==========================================

# Capture standalone numbers; skip numbers living inside [Silver: ...] / [Gold: ...] citations.
_NUMBER_RE = re.compile(r"(?<![\[\w.])(-?\d[\d,]*(?:\.\d+)?)(%?)(?![\]\w])")
_CITATION_RE = re.compile(r"\[(?P<kind>Silver|Gold)\s*:\s*(?P<anchor>[^\]]+)\]", re.IGNORECASE)

# Prompt-sanctioned sentinel strings the Analyst is explicitly allowed to use
# when a claim cannot be cited (see prompts.py:DATA_LINEAGE_DIRECTIVE §3 and
# Scripts/agents/prompts.py:MACRO_CHAIN_DIRECTIVE). Treating these as "real"
# anchor lookups caused every degraded revision in the 2026-04-22 router-e2e
# run to fail the deterministic audit even though the draft was doing the
# right thing (admitting data gaps). They are therefore normalised to
# lower-case and matched as NON-CLAIMS — skipped by both the anchor check
# and, by extension, any number that sits inside such a citation.
_SENTINEL_ANCHORS: frozenset = frozenset({
    "insufficient data",
    "no direct data available",
    "no data",
    "no data available",
    "not applicable",
    "n/a",
    "na",
    "none",
    "unknown",
    "tbd",
    "pending",
})


def _is_sentinel_anchor(anchor: str) -> bool:
    """True iff the citation anchor is a whitelisted 'I have nothing to cite' sentinel.

    Normalised: case-insensitive, punctuation collapsed. This check is used
    by `_deterministic_audit` to short-circuit both anchor-existence and
    numeric-drift audits inside the citation — which matches the Analyst
    prompt's explicit instruction to use `INSUFFICIENT DATA` when a claim
    cannot be cited.
    """
    if not anchor:
        return False
    normalised = " ".join(anchor.lower().split())
    return normalised in _SENTINEL_ANCHORS


def _numeric_equiv(draft_val: float, truth_val: float, rel_tol: float = _NUMERIC_REL_TOLERANCE) -> bool:
    """Percent-tolerant equality. Handles 0.45 vs 45% encoding ambiguity."""
    if truth_val == 0:
        return abs(draft_val - truth_val) < 1e-9
    if abs(draft_val - truth_val) / abs(truth_val) <= rel_tol:
        return True
    # scale bridges
    if abs(draft_val - truth_val * 100) / abs(truth_val * 100 or 1) <= rel_tol:
        return True
    if abs(draft_val * 100 - truth_val) / abs(truth_val) <= rel_tol:
        return True
    return False


def _extract_silver_numbers(silver_context: Dict[str, Any]) -> List[float]:
    """Flatten silver_context['values'] into a list of floats."""
    out: List[float] = []
    values = (silver_context or {}).get("values") or {}
    for v in values.values():
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, str):
            s = v.replace(",", "").rstrip("%").strip()
            try:
                out.append(float(s))
            except ValueError:
                continue
    return out


def _extract_known_anchors(silver_context: Dict[str, Any], gold_context: List[Any]) -> Set[str]:
    """Union of Silver lineage_anchors ∪ Gold bronze_refs for citation verification."""
    known: Set[str] = set()
    for a in (silver_context or {}).get("lineage_anchors", []) or []:
        known.add(str(a))
    for chunk in gold_context or []:
        br = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref") if isinstance(chunk, dict) else None)
        if br:
            known.add(str(br))
    return known


def _deterministic_audit(
    draft: str,
    silver_context: Dict[str, Any],
    gold_context: List[Any],
) -> List[AgentFeedback]:
    """Regex-only audit. Always runs, zero LLM dependency."""
    feedbacks: List[AgentFeedback] = []

    silver_truth_numbers = _extract_silver_numbers(silver_context)
    known_silver_anchors = set(str(a) for a in (silver_context or {}).get("lineage_anchors", []) or [])
    known_gold_refs: Set[str] = set()
    for chunk in gold_context or []:
        br = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref") if isinstance(chunk, dict) else None)
        if br:
            known_gold_refs.add(str(br))

    # (b) Citation anchor existence first (highest severity).
    #    Prompt-sanctioned sentinels ("INSUFFICIENT DATA", "N/A", …) are
    #    *deliberately* accepted here — the Analyst prompt instructs the
    #    model to use them when a claim cannot be cited, so flagging them
    #    as Fatal creates an unrecoverable revision loop (was root cause D
    #    of docs/test/2026-04-22/router_e2e_deep_analysis.md).
    for m in _CITATION_RE.finditer(draft):
        kind = m.group("kind").capitalize()
        anchor = m.group("anchor").strip()
        if _is_sentinel_anchor(anchor):
            continue
        if kind == "Silver" and anchor not in known_silver_anchors:
            feedbacks.append(AgentFeedback(
                sender="Checker",
                error_type="Fatal",
                comment=f"Citation references unknown Silver lineage anchor '{anchor}'. "
                        f"Known anchors: {sorted(known_silver_anchors) or '[]'}",
                missing_lineage_id=[anchor],
            ))
        elif kind == "Gold" and anchor not in known_gold_refs:
            feedbacks.append(AgentFeedback(
                sender="Checker",
                error_type="Fatal",
                comment=f"Citation references unknown Gold bronze_ref '{anchor}'. "
                        f"Known refs (first 5): {list(known_gold_refs)[:5] or '[]'}",
                missing_lineage_id=[anchor],
            ))

    # (a) Number audit — skip claims inside recognised citations.
    draft_without_citations = _CITATION_RE.sub("", draft)
    for m in _NUMBER_RE.finditer(draft_without_citations):
        raw, is_pct = m.group(1), m.group(2)
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        # Tiny integers (1, 2, 3) are almost always section numbers or trade
        # idea counts, not claims. Any 2-digit+ number, decimal, or % qualifies.
        is_claim = bool(is_pct) or "." in raw or abs(val) >= 10
        if not is_claim:
            continue
        if any(_numeric_equiv(val, t) for t in silver_truth_numbers):
            continue
        feedbacks.append(AgentFeedback(
            sender="Checker",
            error_type="Fatal",
            comment=(
                f"Draft cites numeric value '{raw}{is_pct}' that does not match any "
                f"Silver value within {int(_NUMERIC_REL_TOLERANCE * 100)}% tolerance. "
                f"Either cite Silver/Gold or remove the claim."
            ),
        ))

    return feedbacks


# ==========================================
# CheckerAgent class
# ==========================================

class CheckerAgent:
    """Blue-team fact-auditor. Directly wired into the router's dedicated checker_node."""

    def __init__(self, silver_tool: Optional[Any] = None):
        """Args:
            silver_tool: Optional injected `SilverSQLTool`. The router shares its
                singleton instance so the Checker's rescue tool-call reuses the
                same DuckDB connection that already cached Parquet schemas.
                When None, tool-call recovery is disabled.
        """
        self.silver_tool = silver_tool
        self.enable_rescue = _TOOL_RECOVERY_ENABLED and silver_tool is not None

        self.model_name = os.getenv("OLLAMA_CHECKER_MODEL", "options-expert-v1:latest")
        self.llm = ChatOllama(
            model=self.model_name,
            temperature=0.0,
            format="json",
        ).with_structured_output(CheckerResult)

        self.prompt = get_checker_prompt()

    # ----------------------------------------
    # Public entry point (LangGraph contract)
    # ----------------------------------------
    async def audit(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Run all three defence layers and return the Checker node payload.

        Returns a dict matching `AgentState` keys so the LangGraph checkpointer
        merges it cleanly:
            {
              "critic_feedback": List[AgentFeedback],   # append-only
              "checker_verdict": "pass" | "fatal" | "minor",
              "silver_context": Dict[str, Any],         # optional refresh
            }
        """
        revision_n = state.get("revision_count", 0)
        if revision_n >= _MAX_REVISIONS:
            logger.warning("CheckerAgent: max revisions reached — skipping audit.")
            return {"critic_feedback": [], "checker_verdict": "pass"}

        draft = state.get("draft_report", "") or ""
        silver_ctx = state.get("silver_context", {}) or {}
        gold_ctx = state.get("gold_context", []) or []

        # ---- Layer 1: deterministic pass ----
        feedbacks: List[AgentFeedback] = _deterministic_audit(draft, silver_ctx, gold_ctx)
        logger.info(f"CheckerAgent: deterministic hits={len(feedbacks)}")

        # ---- Layer 2: tool-call escape hatch (opt-in) ----
        refreshed_silver: Optional[Dict[str, Any]] = None
        if self.enable_rescue and self._should_trigger_rescue(feedbacks):
            refreshed_silver, rescued_feedbacks = await self._rescue_missing_anchors(
                feedbacks=feedbacks,
                state=state,
            )
            feedbacks = rescued_feedbacks
            # If rescue succeeded, also refresh the silver-truth numbers
            # for the LLM pass below so it sees the latest data.
            if refreshed_silver:
                silver_ctx = refreshed_silver

        # ---- Layer 3: LLM semantic audit (best-effort) ----
        try:
            # Reuse Analyst formatters to keep the Silver/Gold rendering
            # identical across prompts (no silent schema drift between agents).
            from Scripts.agents.analyst import _format_silver, _format_gold
            silver_block = _format_silver(silver_ctx)
            gold_block = _format_gold(gold_ctx)

            chain = self.prompt | self.llm
            logger.info(f"CheckerAgent: invoking LLM audit | deterministic_hits={len(feedbacks)}")
            result: CheckerResult = await chain.ainvoke({
                "silver_block": silver_block,
                "gold_block": gold_block,
                "draft": draft,
            })
            if not result.is_passed:
                for v in result.violations:
                    sev = v.severity if v.severity in ("Fatal", "Minor") else "Fatal"
                    feedbacks.append(AgentFeedback(
                        sender="Checker",
                        error_type=sev,
                        comment=v.description + (
                            f" (draft: {v.draft_value} vs silver: {v.cited_silver_value})"
                            if v.draft_value or v.cited_silver_value else ""
                        ),
                        revision_index=revision_n,
                    ))
        except Exception as e:
            logger.warning(
                f"CheckerAgent: LLM audit failed ({type(e).__name__}); "
                f"falling back to deterministic only. Error: {e}"
            )

        # Stamp revision index on every feedback entry for audit replayability.
        for fb in feedbacks:
            if fb.revision_index is None:
                fb.revision_index = revision_n

        verdict = self._compute_verdict(feedbacks)
        logger.info(f"CheckerAgent: verdict={verdict} | total_findings={len(feedbacks)}")

        payload: Dict[str, Any] = {
            "critic_feedback": feedbacks,
            "checker_verdict": verdict,
        }
        if refreshed_silver is not None:
            payload["silver_context"] = refreshed_silver
        return payload

    # ----------------------------------------
    # Rescue helpers
    # ----------------------------------------

    @staticmethod
    def _should_trigger_rescue(feedbacks: List[AgentFeedback]) -> bool:
        """Rescue only when we have missing-anchor Fatal findings (not pure numeric drift)."""
        return any(
            fb.error_type.lower() == "fatal" and fb.missing_lineage_id
            for fb in feedbacks
        )

    async def _rescue_missing_anchors(
        self,
        feedbacks: List[AgentFeedback],
        state: Dict[str, Any],
    ) -> tuple[Optional[Dict[str, Any]], List[AgentFeedback]]:
        """Re-query Silver via the precomputed metadata and downgrade feedback on hit.

        Rationale: the architect brief wants Checker to dynamically tool-call
        the Silver Layer when Retriever missed an anchor — not to let the LLM
        re-plan the query (which would re-open the hallucination surface).
        We therefore reuse `state["metadata"]` (already LLM-extracted by
        QueryTransformer) and call SilverSQLTool directly.
        """
        metadata = state.get("metadata")
        if metadata is None:
            logger.info("CheckerAgent: rescue skipped — no metadata on state.")
            return None, feedbacks

        # Rebuild the same per-source `TimePredicate` set MasterRetriever
        # already published to `state["time_range"]["source_predicates"]`.
        # Without this, the rescue call would land on the legacy branch of
        # `_handle_put_call_ratio` / `_handle_options_analysis` and silently
        # compute a *different* window than the initial retrieval (the 3d→2d
        # drift documented in docs/test/2026-04-22/router_e2e_deep_analysis.md
        # lines 120–133), making `latest_atm_iv` oscillate across revisions
        # and the Checker unable to converge against its own refreshed truth.
        live_predicates = None
        tr = state.get("time_range") or {}
        serialised_preds = tr.get("source_predicates")
        if isinstance(serialised_preds, dict) and serialised_preds:
            try:
                # Imported lazily to avoid a cross-module import cycle at
                # module load time; checker.py must not pull in the full
                # retrieval package when a consumer only needs auditing.
                from Scripts.retrieval.time_adapter import predicates_from_serialised
                live_predicates = predicates_from_serialised(serialised_preds)
                if live_predicates:
                    logger.info(
                        "CheckerAgent: rescue will reuse pinned TimePredicate set "
                        f"({len(live_predicates)} sources) from state.time_range."
                    )
            except Exception as e:
                logger.warning(
                    f"CheckerAgent: could not rehydrate time predicates ({type(e).__name__}: {e}); "
                    "rescue will fall back to handler-local window math."
                )

        try:
            logger.info("CheckerAgent: triggering Silver rescue query (missing anchor).")
            # Pass the pinned predicate set so the Silver handlers hit exactly
            # the same (start_date, end_date) window as the initial retrieval.
            refreshed = await self.silver_tool.query_parquet_by_metadata(
                metadata, time_predicates=live_predicates,
            )
        except Exception as e:
            logger.warning(f"CheckerAgent: rescue query failed ({type(e).__name__}): {e}")
            return None, feedbacks

        refreshed_values = (refreshed or {}).get("values") or {}
        refreshed_anchors = set(str(a) for a in (refreshed or {}).get("lineage_anchors", []) or [])

        # Merge with the in-state silver so we don't lose values from the first pass.
        old_silver = state.get("silver_context", {}) or {}
        old_values = old_silver.get("values", {}) or {}
        old_anchors = set(str(a) for a in old_silver.get("lineage_anchors", []) or [])

        merged_values = {**old_values, **refreshed_values}
        merged_anchors = sorted(old_anchors | refreshed_anchors)
        merged_silver = {
            "values": merged_values,
            "lineage_anchors": merged_anchors,
            # preserve any other side-info like engine_error
            **{k: v for k, v in (refreshed or {}).items() if k not in ("values", "lineage_anchors")},
        }

        # Downgrade Fatal missing-anchor findings whose anchor is now known.
        updated: List[AgentFeedback] = []
        for fb in feedbacks:
            if (
                fb.error_type.lower() == "fatal"
                and fb.missing_lineage_id
                and any(mid in merged_anchors for mid in fb.missing_lineage_id)
            ):
                updated.append(AgentFeedback(
                    sender="Checker",
                    error_type="Minor",
                    comment=(
                        f"(Rescued) Citation anchor {fb.missing_lineage_id} was missing "
                        "from the initial retrieval pass but is now present after a "
                        "Silver re-query. Analyst should keep the citation — no rewrite needed."
                    ),
                    missing_lineage_id=fb.missing_lineage_id,
                    revision_index=fb.revision_index,
                ))
            else:
                updated.append(fb)

        logger.info(
            f"CheckerAgent: rescue merged {len(refreshed_anchors)} new anchors "
            f"({len(refreshed_values)} new values); downgraded Fatal → Minor where applicable."
        )
        return merged_silver, updated

    # ----------------------------------------
    # Verdict computation
    # ----------------------------------------

    @staticmethod
    def _compute_verdict(feedbacks: List[AgentFeedback]) -> str:
        """Short-circuit verdict used by the router's conditional edge."""
        if not feedbacks:
            return "pass"
        if any(fb.error_type.lower() == "fatal" for fb in feedbacks):
            return "fatal"
        return "minor"
