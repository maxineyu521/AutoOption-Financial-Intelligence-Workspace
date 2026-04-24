"""
Scripts/agents/state.py
Role: "Global State Protocol" for the financial multi-agent system.
Design philosophy:
1. Traceability (Traceability): force data ID to be passed through.
2. Resilience (Resilience): prevent memory loss through accumulator.
3. Determinism (Determinism): use Pydantic to constrain all intermediate products.
"""

import operator
from typing import TypedDict, Annotated, List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field

# ==========================================
# 1. Data lineage and source structure (Lineage)
# ==========================================

class EvidenceLink(BaseModel):
    """Evidence link anchor"""
    source_id: str = Field(..., description="original data ID from Gold or Silver layer (e.g. UUID or Row_Index)")
    content_summary: str = Field(..., description="the key numerical or factual summary supported by this evidence")
    reliability_score: float = Field(default=1.0, description="reliability score of the data source")

# ==========================================
# 2. Feedback object structure (solve deadlock and forgetfulness)
# ==========================================

class AgentFeedback(BaseModel):
    """Agent interaction feedback object"""
    sender: str = Field(..., description="the name of the agent that sent the feedback (Checker/Critic)")
    error_type: str = Field(..., description="Fatal (must be corrected) or Minor (suggestions for optimization)")
    comment: str = Field(..., description="specific rejection reason or modification suggestion")
    missing_lineage_id: Optional[List[str]] = Field(None, description="the missing or incorrect evidence ID")
    revision_index: Optional[int] = Field(
        default=None,
        description="the revision index of the feedback (used for audit replay; locate the新增 in the append-only list)")


# ==========================================
# 3. Structured parameter extraction (solve SQL hallucination)
# ==========================================

class QueryMetadata(BaseModel):
    """
    Structured intent extracted by Transform.
    For backend Python code to directly perform parameterized SQL拼接。
    """
    tickers: List[str] = Field(default_factory=list)
    metrics: List[str] = Field(default_factory=list, description="IV, Skew, Vol, Insider_Flow etc.")
    date_range: Dict[str, str] = Field(
        default_factory=dict, 
        description="YYYY-MM-DD format containing start_date and end_date"
    )
    logical_constraints: List[str] = Field(
        default_factory=list, 
        description="e.g. 'only look at options above 5% OTM'"
    )

# ==========================================
# 4. Global AgentState (core contract)
# ==========================================

class AgentState(TypedDict):
    """
    LangGraph global state: the "living dictionary" of the system.
    """
    # --- 原始输入 ---
    original_query: str
    macro_context: str  # latest macro snapshot content injected from external source

    # --- Retrieval layer data (with lineage) ---
    metadata: QueryMetadata
    gold_context: List[Dict[str, Any]]  # Qdrant returns, must contain {'id': ..., 'text': ...}
    # silver_context example: {'values': {...}, 'lineage_anchors': [...], 'source_channel': 'primary'}
    silver_context: Dict[str, Any]

    # 🔒 Immutable Silver snapshot — written ONCE by retrieval_master at exit,
    # never modified by rescue or revision passes.
    # Checker uses this as the deterministic numeric ground truth so that
    # Rescue-driven updates to `silver_context` (which are additive and used
    # by the NEXT analyst revision) cannot shift the audit baseline mid-round
    # and cause the IV-regime / numeric-match oscillation documented in
    # docs/test/2026-04-22/router_e2e_deep_analysis.md §6.5 (Test 4).
    # Structure mirrors silver_context: {values: Dict, lineage_anchors: List[str]}
    silver_context_frozen: Optional[Dict[str, Any]]

    # 🌟 Time audit: Retriever uniformly computes the retrieval interval, Analyst must declare at the end
    # structure: {time_window_label, window_days, anchor_date, start_date, end_date,
    #        is_default_window_applied}
    time_range: Optional[Dict[str, Any]]

    # 🌟 HyDE semantic hedge (HE = Hypothetical Expansion)
    # structure: {paragraph, rerank_query, novel_tickers, whitelisted, source_channel}
    # sql_only route as LLM prior knowledge supplement; vector_only route drives Silver reverse compensation query.
    hyde_anticipation: Optional[Dict[str, Any]]

    # 🌟 Pinned IV regime — computed ONCE by the Analyst on the first draft,
    # reused verbatim on every revision so the strategy recommendation does
    # not flip direction mid-pipeline (see docs/test/2026-04-22/
    # router_e2e_deep_analysis.md §6.5 — Test 4 flipped NORMAL→LOW→NORMAL
    # across three revisions because Silver rescues refreshed atm_iv).
    # Structure: {"iv_regime": str, "atm_iv": float|None,
    #             "pcr_volume": float|None, "pcr_status": str|None,
    #             "thresholds": {"high": float, "low": float}}
    iv_regime_pinned: Optional[Dict[str, Any]]

    # --- Inference interaction process ---
    draft_report: str
    
    # 🌟 Core: Append-only feedback list (solve Risk 2)
    # use operator.add to implement state accumulation, Analyst always sees the "error set"
    critic_feedback: Annotated[List[AgentFeedback], operator.add]

    # 📝 Critic Minor Suggestions — non-blocking polish notes for Finalizer.
    # Unlike critic_feedback (Fatal → analyst revision), these are style/structure
    # improvements that do NOT warrant a full rewrite.  Finalizer reads them
    # and incorporates where possible into conversation_reply and rationale.
    # Written by critic_node each pass (NOT append-only — replaced each time).
    critic_minor_suggestions: Optional[List[str]]

    # 🌟 Short-circuit decision fields: only read these two fields to decide the next jump,
    # avoid time window inference of "who reported the error in the last round" in the append-only list.
    # value domain: "pass" | "fatal" | "minor" | None(not yet audited)
    checker_verdict: Optional[Literal["pass", "fatal", "minor"]]
    critic_verdict: Optional[Literal["pass", "fatal", "minor"]]

    # --- Flow control (solve Risk 3) ---
    revision_count: int  # number of drafts produced by Analyst; only Analyst node is responsible for increment
    is_fallback: bool    # whether the fallback logic is triggered
    
    # --- Final product ---
    # force final_strategy to contain evidence_links to implement data lineage
    final_strategy: Dict[str, Any]

    # 🔍 Per-node audit trail — append-only, never reset between revisions.
    # Each node wrapper in router.py appends one Dict per call:
    #   {"node", "revision_n", "t_start", "latency_ms", "verdict", "findings_n",
    #    "key_state_in", "key_state_out"}
    # Enables full replay of the pipeline history from a single state dump.
    node_audit_log: Annotated[List[Dict[str, Any]], operator.add]