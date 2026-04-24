"""
Topology:
    retrieval_master  →  analyst  →  checker  →  [fatal? analyst : critic]
                                                          │
                                           critic →  [fatal? analyst : finalizer]
                                                          │
                         revision_count ≥ N  →  finalizer (hard circuit-breaker)

    职责:
    1. Graph orchestration: clearly define the six-step pipeline of Transform → Dual Retrieval → Analyst → Checker → Critic → Finalizer.
    Transform + Dual Retrieval are merged into `master_retrieval_node` internally,
    because the MasterRetriever facade has already atomically handled the intent classification + metadata extraction + Gold/Silver concurrency.
    2. Resource reuse: global singleton MasterRetriever + global singleton SilverSQLTool, avoid reloading LLM / DuckDB / Qdrant client each time the graph is entered.
    3. Macro preamble (Macro Preamble): read latest_macro_context.md into `state["macro_context"]` at the entry node, all downstream nodes share the same macro snapshot.
    4. Conditional routing: Checker and Critic have independent conditional edges, short-circuit fields `checker_verdict` / `critic_verdict` make the routing decision O(1) and do not depend on the time window inference of the append-only list.
    5. Circuit breaker: `revision_count >= AGENT_MAX_REVISIONS` hard-force entering Finalizer,
    Finalizer will mark the product as `status="degraded"`.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from langgraph.graph import StateGraph, END

from Scripts.agents.state import AgentState
from Scripts.retrieval import MasterRetriever
from Scripts.retrieval.sql_tools import SilverSQLTool
from Scripts.agents.analyst import AnalystAgent
from Scripts.agents.checker import CheckerAgent
from Scripts.agents.critic import CriticAgent
from Scripts.agents.finalizer import FinalizerAgent

logger = logging.getLogger(__name__)

# --- Hard circuit-breaker (shared constant across agents) ---
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))


# ==========================================
# 0a. Per-node audit log helper
# ==========================================

def _emit_node_audit(
    node: str,
    revision_n: int,
    t0: float,
    verdict: str | None = None,
    findings_n: int = 0,
    key_in: Dict[str, Any] | None = None,
    key_out: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Build a single node audit event and return it as a 1-element list.

    Returned as a list so it can be directly used as the `node_audit_log`
    value in the router node's return dict — LangGraph merges it via the
    ``operator.add`` reducer declared in AgentState.

    Schema (docs/test/2026-04-23/checker_strictness_v2_architecture.md §2.3):
        node, revision_n, t_start (ISO-8601), latency_ms, verdict,
        findings_n, key_state_in, key_state_out
    """
    latency_ms = round((time.monotonic() - t0) * 1000, 1)
    event: Dict[str, Any] = {
        "node": node,
        "revision_n": revision_n,
        "t_start": datetime.utcnow().isoformat(timespec="milliseconds"),
        "latency_ms": latency_ms,
        "verdict": verdict,
        "findings_n": findings_n,
        "key_state_in": key_in or {},
        "key_state_out": key_out or {},
    }
    logger.info(
        f"📋 [AuditLog:{node}] revision={revision_n} | latency={latency_ms}ms | "
        f"verdict={verdict} | findings={findings_n}"
    )
    return [event]

# --- Global singletons: loaded once per process, not per invocation ---
_MASTER_RETRIEVER = MasterRetriever()
# Share the same SilverSQLTool instance across Retriever + Checker so the
# Checker's tool-call escape hatch hits the same DuckDB connection that
# already cached the Parquet schema at startup.
_SILVER_SQL_TOOL: SilverSQLTool = _MASTER_RETRIEVER.sql_tool

# --- Agent singletons (ChatOllama clients are expensive to construct) ---
_ANALYST_AGENT = AnalystAgent()
_CHECKER_AGENT = CheckerAgent(silver_tool=_SILVER_SQL_TOOL)
_CRITIC_AGENT = CriticAgent()
_FINALIZER_AGENT = FinalizerAgent()


# ==========================================
# 0. Macro preamble helper (injected into state on first pass)
# ==========================================

def _load_macro_snapshot() -> str:
    """Read Data/Agent_Context/latest_macro_context.md once at graph entry.

    Returns a safe fallback string when the snapshot file is absent so the
    pipeline never crashes on first-run or in CI. The Analyst / Critic both
    consume `state["macro_context"]` downstream.
    """
    try:
        project_root = Path(__file__).resolve().parents[2]
        fp = project_root / "Data" / "Agent_Context" / "latest_macro_context.md"
        if fp.exists():
            return fp.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"router: macro snapshot load failed: {e}")
    return (
        "Market conditions are currently stable. (fallback — "
        "latest_macro_context.md not yet produced by the macro pipeline.)"
    )


# ==========================================
# 1. Node: retrieval_master (Transform + Dual Retrieval + Macro preamble)
# ==========================================

# ------------------------------------------------------------------
# Canonical default shapes for the two audit channels.
# -----------------------------------------------------------------------------
# These keys match the contract declared in Scripts/agents/state.py (AgentState)
# AND the per-node assertion list in Scripts/tests/test_router_e2e.py
# (_REQUIRED_TIMERANGE_KEYS / _REQUIRED_HYDE_KEYS). Hard-coding the shape here
# guarantees the Analyst can always read state["time_range"]["start_date"]
# even when MasterRetriever degraded to the _empty_payload branch.
# -----------------------------------------------------------------------------

def _default_time_range() -> Dict[str, Any]:
    """Fallback time-range used when the retriever couldn't compute one."""
    today = date.today()
    # 180-day look-back mirrors the schema default so downstream Silver queries
    # still have a sensible window even in emergency mode.
    return {
        "time_window_label": "past_six_months",
        "window_days": 180,
        "anchor_date": today.isoformat(),
        "start_date": (today - timedelta(days=180)).isoformat(),
        "end_date": today.isoformat(),
        "is_default_window_applied": True,
    }


def _default_hyde_anticipation() -> Dict[str, Any]:
    """Fallback HyDE payload — zero-capacity but schema-compliant."""
    return {
        "paragraph": "",
        "rerank_query": "",
        "raw_candidates": [],
        "whitelisted_tickers": [],
        "novel_tickers": [],
        "source_channel": "reference",
    }


def _coerce_time_range(tr: Any) -> Dict[str, Any]:
    """Fill in any missing required keys with defaults so downstream nodes
    can safely `state["time_range"]["start_date"]` without guards.
    Accepts None, a partial dict, or a full dict; always returns a full dict.
    """
    base = _default_time_range()
    if isinstance(tr, dict):
        for k, v in tr.items():
            if v is not None:
                base[k] = v
    return base


def _coerce_hyde_anticipation(he: Any) -> Dict[str, Any]:
    base = _default_hyde_anticipation()
    if isinstance(he, dict):
        for k, v in he.items():
            if v is not None:
                base[k] = v
    return base


async def master_retrieval_node(state: AgentState) -> Dict[str, Any]:
    """Transform → Dual Retrieval → Macro Preamble, atomically.

    Writes into state:
        intent, metadata, gold_context, silver_context, macro_context,
        time_range, hyde_anticipation,
        revision_count=0, is_fallback, checker_verdict=None, critic_verdict=None

    Contract guarantee (NEW): time_range and hyde_anticipation are ALWAYS
    non-None and always carry the full set of required keys. This closes the
    2026-04-22 production bug where retrieve() returned an _empty_payload and
    the Analyst then crashed on `state["time_range"]["start_date"]`. Defaults
    are coerced via `_coerce_time_range` / `_coerce_hyde_anticipation` so a
    partial retriever response still produces a schema-valid state delta.
    """
    t0 = time.monotonic()
    logger.info("🔄 [Node:retrieval_master] Entering Master Retrieval + Macro preamble...")

    retrieval_results = await _MASTER_RETRIEVER.retrieve(state["original_query"])
    is_fallback = retrieval_results.get("status") != "success"
    if is_fallback:
        logger.warning("⚠️ [Node:retrieval_master] Retrieval partial_failure. Activating fallback state.")

    macro_ctx = _load_macro_snapshot()

    time_range = _coerce_time_range(retrieval_results.get("time_range"))
    hyde_anticipation = _coerce_hyde_anticipation(retrieval_results.get("hyde_anticipation"))

    gold_n = len(retrieval_results.get("gold_context") or [])
    silver_n = len((retrieval_results.get("silver_context") or {}).get("values") or {})

    return {
        "metadata": retrieval_results.get("metadata"),
        "gold_context": retrieval_results.get("gold_context", []),
        "silver_context": retrieval_results.get("silver_context", {}),
        # 🌟 Audit channels produced by MasterRetriever.retrieve() — now
        # guaranteed non-None and fully-shaped even in degraded modes:
        #   - time_range         : (anchor, start, end, window_days, label,
        #                          is_default_window_applied). Analyst emits
        #                          this in the compliance footer of every
        #                          final report.
        #   - hyde_anticipation  : HyDE paragraph + novel tickers. Checker
        #                          relaxes numeric audit when silver_context
        #                          carries `source_channel=silver_layer_via_hyde_expansion`.
        "time_range": time_range,
        "hyde_anticipation": hyde_anticipation,
        "macro_context": macro_ctx,
        "revision_count": 0,
        "is_fallback": is_fallback,
        # Reset verdicts on every new graph entry (safe for re-runs via checkpointer).
        "checker_verdict": None,
        "critic_verdict": None,
        # Reset the pinned IV regime: the first Analyst pass of this new
        # graph entry must recompute from the freshly-retrieved Silver,
        # not inherit a regime from the previous run under a checkpointer.
        "iv_regime_pinned": None,
        # Per-node audit log (v2 — append-only via operator.add).
        "node_audit_log": _emit_node_audit(
            node="retrieval_master",
            revision_n=0,
            t0=t0,
            verdict="fallback" if is_fallback else "success",
            findings_n=0,
            key_in={"query": (state.get("original_query") or "")[:80]},
            key_out={"gold_n": gold_n, "silver_n": silver_n, "is_fallback": is_fallback},
        ),
    }


# ==========================================
# 2. Node: analyst (single place that increments revision_count)
# ==========================================

async def analyst_node(state: AgentState) -> Dict[str, Any]:
    """Produce (or re-produce) the Markdown draft.

    IMPORTANT: Analyst is the SOLE owner of `revision_count` — each time the
    Analyst runs, it has produced one more draft, so we increment here. Checker
    and Critic no longer touch the counter. This makes the route functions'
    `revision_count >= N` circuit-breaker a clean "how many drafts have we
    tried so far" semantic.

    Also pins `iv_regime_pinned` on the first pass so subsequent revisions
    never see a flipped regime (was §6.5 of the 2026-04-22 deep analysis).
    """
    t0 = time.monotonic()
    revision_n = state.get("revision_count", 0)
    logger.info(f"✍️ [Node:analyst] Drafting (revision={revision_n + 1})...")

    result = await _ANALYST_AGENT.generate_report(state)

    # Pin the IV regime on the very first draft. On re-entry the analyst has
    # already read it back from state, so the pinned value round-trips
    # unchanged across revisions — guaranteed strategy-direction stability.
    delta: Dict[str, Any] = {
        "draft_report": result.draft,
        "revision_count": revision_n + 1,
        "checker_verdict": None,
        "critic_verdict": None,
        "node_audit_log": _emit_node_audit(
            node="analyst",
            revision_n=revision_n + 1,
            t0=t0,
            verdict=None,
            findings_n=0,
            key_in={
                "revision_n": revision_n,
                "feedback_n": len(state.get("critic_feedback") or []),
            },
            key_out={
                "draft_len": len(result.draft),
                "iv_regime": (result.iv_regime or {}).get("iv_regime"),
            },
        ),
    }
    if state.get("iv_regime_pinned") is None and isinstance(result.iv_regime, dict):
        delta["iv_regime_pinned"] = result.iv_regime
    return delta


# ==========================================
# 3. Node: checker (Blue team, fact + lineage)
# ==========================================

async def checker_node(state: AgentState) -> Dict[str, Any]:
    """Deterministic regex audit + coverage check + LLM consistency audit + Silver rescue."""
    t0 = time.monotonic()
    revision_n = state.get("revision_count", 0)
    logger.info(f"🧐 [Node:checker] Auditing draft (revision={revision_n}) for factual + lineage accuracy...")
    result = await _CHECKER_AGENT.audit(state)
    # audit() returns {"critic_feedback": [...], "checker_verdict": ..., optional silver_context}
    verdict = result.get("checker_verdict", "pass")
    findings_n = len(result.get("critic_feedback") or [])

    # Log structured rejection reasons for Fatal findings (aids Analyst revision).
    if verdict == "fatal":
        fatal_comments = [
            fb.comment for fb in (result.get("critic_feedback") or [])
            if fb.error_type.lower() == "fatal"
        ]
        for fc in fatal_comments:
            logger.warning(f"🚫 [Checker:Fatal] {fc}")

    result["node_audit_log"] = _emit_node_audit(
        node="checker",
        revision_n=revision_n,
        t0=t0,
        verdict=verdict,
        findings_n=findings_n,
        key_in={"revision_n": revision_n, "draft_len": len(state.get("draft_report") or "")},
        key_out={"verdict": verdict, "findings_n": findings_n},
    )
    return result


# ==========================================
# 4. Node: critic (Red team, logic + regime + insider)
# ==========================================

async def critic_node(state: AgentState) -> Dict[str, Any]:
    """Red-team logic critique; runs only if Checker produced no Fatal findings."""
    t0 = time.monotonic()
    revision_n = state.get("revision_count", 0)
    logger.info(f"🛡️ [Node:critic] Challenging strategy logic / regime fit / insider signal (revision={revision_n})...")
    result = await _CRITIC_AGENT.audit(state)
    verdict = result.get("critic_verdict", "pass")
    findings_n = len(result.get("critic_feedback") or [])

    # Log structured rejection reasons for Fatal findings.
    if verdict == "fatal":
        fatal_comments = [
            fb.comment for fb in (result.get("critic_feedback") or [])
            if fb.error_type.lower() == "fatal"
        ]
        for fc in fatal_comments:
            logger.warning(f"🚫 [Critic:Fatal] {fc}")

    result["node_audit_log"] = _emit_node_audit(
        node="critic",
        revision_n=revision_n,
        t0=t0,
        verdict=verdict,
        findings_n=findings_n,
        key_in={"revision_n": revision_n},
        key_out={"verdict": verdict, "findings_n": findings_n},
    )
    return result


# ==========================================
# 5. Node: finalizer (structured output)
# ==========================================

async def finalizer_node(state: AgentState) -> Dict[str, Any]:
    """Convert Markdown draft → FinalReport Pydantic dict."""
    t0 = time.monotonic()
    revision_n = state.get("revision_count", 0)
    logger.info("✨ [Node:finalizer] Structuring FinalReport...")
    final_strategy = await _FINALIZER_AGENT.format_and_clean(state)
    status = final_strategy.get("status", "unknown")
    return {
        "final_strategy": final_strategy,
        "node_audit_log": _emit_node_audit(
            node="finalizer",
            revision_n=revision_n,
            t0=t0,
            verdict=status,
            findings_n=0,
            key_in={"revision_n": revision_n},
            key_out={
                "status": status,
                "confidence": final_strategy.get("confidence_score"),
            },
        ),
    }


# ==========================================
# 6. Conditional edges — the routing brain
# ==========================================

def route_after_checker(state: AgentState) -> str:
    """After Checker: fatal → analyst, else → critic.  Hard cap → finalizer."""
    revision_n = state.get("revision_count", 0)
    if revision_n >= _MAX_REVISIONS:
        # Structured: [rule:CIRCUIT_BREAK | node=checker | revision_n=N | limit=N]
        logger.warning(
            f"🛑 [Route:after_checker] [rule:CIRCUIT_BREAK | node=checker | "
            f"revision_n={revision_n} | limit={_MAX_REVISIONS}] "
            "Forcing finalizer (degraded output)."
        )
        return "finalizer"

    verdict = state.get("checker_verdict")
    if verdict == "fatal":
        # Emit which specific findings caused the reroute so the Analyst log
        # has machine-parseable rejection context adjacent to the route decision.
        fatal_items = [
            fb.comment for fb in (state.get("critic_feedback") or [])
            if fb.error_type.lower() == "fatal" and fb.sender == "Checker"
        ]
        logger.info(
            f"🔄 [Route:after_checker] [rule:CHECKER_FATAL_REROUTE | revision_n={revision_n} | "
            f"fatal_count={len(fatal_items)}] Routing back to Analyst. "
            f"First fatal: {fatal_items[0][:120] if fatal_items else 'n/a'}"
        )
        return "analyst"

    # "pass" or "minor" or None → move forward to Critic.
    # Minor findings are delivered to Analyst via the append-only feedback log
    # only if the Critic also pushes back; alone they do not trigger a rewrite.
    logger.info(
        f"✅ [Route:after_checker] verdict={verdict or 'pass'} — forwarding to Critic."
    )
    return "critic"


def route_after_critic(state: AgentState) -> str:
    """After Critic: fatal → analyst, else → finalizer.  Hard cap → finalizer."""
    revision_n = state.get("revision_count", 0)
    if revision_n >= _MAX_REVISIONS:
        logger.warning(
            f"🛑 [Route:after_critic] [rule:CIRCUIT_BREAK | node=critic | "
            f"revision_n={revision_n} | limit={_MAX_REVISIONS}] "
            "Forcing finalizer (degraded output)."
        )
        return "finalizer"

    verdict = state.get("critic_verdict")
    if verdict == "fatal":
        fatal_items = [
            fb.comment for fb in (state.get("critic_feedback") or [])
            if fb.error_type.lower() == "fatal" and fb.sender == "Critic"
        ]
        logger.info(
            f"🔄 [Route:after_critic] [rule:CRITIC_FATAL_REROUTE | revision_n={revision_n} | "
            f"fatal_count={len(fatal_items)}] Routing back to Analyst. "
            f"First fatal: {fatal_items[0][:120] if fatal_items else 'n/a'}"
        )
        return "analyst"

    logger.info(
        f"✅ [Route:after_critic] verdict={verdict or 'pass'} — forwarding to Finalizer."
    )
    return "finalizer"


# ==========================================
# 7. Graph assembly
# ==========================================

def build_financial_rag_graph():
    """Compile the industrial-grade workflow.

    Contract consumed by `main.py` / `Scripts.Generation_Phase`:
        graph = build_financial_rag_graph()
        final_state = await graph.ainvoke({"original_query": user_input})
    """
    workflow = StateGraph(AgentState)

    # 1. Nodes
    workflow.add_node("retrieval_master", master_retrieval_node)
    workflow.add_node("analyst", analyst_node)
    workflow.add_node("checker", checker_node)
    workflow.add_node("critic", critic_node)
    workflow.add_node("finalizer", finalizer_node)

    # 2. Linear edges
    workflow.set_entry_point("retrieval_master")
    workflow.add_edge("retrieval_master", "analyst")
    workflow.add_edge("analyst", "checker")

    # 3. Conditional edges: Checker -> {analyst | critic | finalizer(circuit-break)}
    workflow.add_conditional_edges(
        "checker",
        route_after_checker,
        {
            "analyst": "analyst",
            "critic": "critic",
            "finalizer": "finalizer",
        },
    )

    # 4. Conditional edges: Critic -> {analyst | finalizer}
    workflow.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "analyst": "analyst",
            "finalizer": "finalizer",
        },
    )

    # 5. Terminal edge
    workflow.add_edge("finalizer", END)

    logger.info(
        "🛠️ Financial RAG Graph compiled. Topology: retrieval_master → analyst → "
        "checker → {analyst|critic|finalizer} ; critic → {analyst|finalizer}."
    )
    return workflow.compile()


# Public alias. Generic LangGraph tutorials and the orchestration CLI
# refer to the entry point as ``build_graph``; exposing it here removes
# an entire class of cross-module naming-drift bugs (see
# docs/test/2026-04-22/ingestion_and_query_runtime_failures.md §2.5).
build_graph = build_financial_rag_graph

__all__ = [
    "build_financial_rag_graph",
    "build_graph",
    "master_retrieval_node",
    "analyst_node",
    "checker_node",
    "critic_node",
    "finalizer_node",
    "route_after_checker",
    "route_after_critic",
]
