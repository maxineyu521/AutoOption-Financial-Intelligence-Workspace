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
from pydantic import BaseModel, Field, ConfigDict

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


class FinalizerEdit(TypedDict, total=False):
    """Typed, non-blocking edit contract consumed by the Finalizer only."""
    source: Literal["Checker", "Critic"]
    edit_type: Literal[
        "mode_downgrade",
        "clarify_risk",
        "must_disclose_risk",
        "keep_numbers",
        "remove_unsupported_structure",
        "preserve_illustrative_structure",
        "tighten_horizon",
    ]
    target_section: Literal[
        "conversation_reply",
        "direct_conclusion",
        "macro_summary",
        "asset_read",
        "recommendation_mode",
        "risks",
    ]
    instruction: str
    must_keep_keys: List[str]
    must_keep_anchor_ids: List[str]


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
    logical_reasoning: str = Field(
        default="",
        description="Query-transform reasoning trace in Macro -> Meso -> Micro form."
    )
    source_types: List[str] = Field(
        default_factory=list,
        description="Requested source families inferred by query_transform, e.g. sec/news/gpr/options."
    )
    primary_theme: Literal["insider", "geopolitics", "cross_asset", "options"] = Field(
        default="options",
        description="Primary analytical theme resolved upstream for canonical family routing."
    )
    primary_surface: Literal["options_surface", "macro_news_surface"] = Field(
        default="macro_news_surface",
        description="Primary analytical surface requested by the query."
    )
    canonical_news_topics: List[str] = Field(
        default_factory=list,
        description="Canonical ontology-backed news topics selected during transform."
    )
    analysis_surfaces: List[str] = Field(
        default_factory=list,
        description="Structured analytical layers the answer must cover."
    )
    comparison_targets: List[str] = Field(
        default_factory=list,
        description="Secondary tickers or benchmarks used for supporting context only."
    )
    asset_scope: Literal["single_name", "benchmark", "basket", "unspecified"] = Field(
        default="unspecified",
        description="Structured asset scope resolved upstream."
    )
    read_profile: Literal["board_state", "posture_read", "event_risk", "structure_request"] = Field(
        default="board_state",
        description="Structured answer-shaping profile resolved upstream."
    )
    action_direction: str = Field(
        default="NONE",
        description="Directional extraction from query_transform, when available."
    )
    form_type: str = Field(
        default="ALL",
        description="SEC form subtype filter when query_transform inferred one."
    )
    sentiment_target: str = Field(
        default="ANY",
        description="Optional sentiment-target extraction from query_transform."
    )
    event_keyword: str = Field(
        default="",
        description="Compact event trigger extracted by query_transform."
    )
    goal_contract: str = Field(
        default="",
        description="Canonical internal goal contract when supplied by a structured caller."
    )
    time_window: str = Field(
        default="past_six_months",
        description="Normalized retrieval window label inferred upstream."
    )


class RenderSafetyContract(BaseModel):
    """Validated, frontend-safe render seeds consumed by the Finalizer."""

    model_config = ConfigDict(extra="ignore")

    direct_answer_seed: str = ""
    direct_answer_includes_posture_takeaway: bool = Field(default=False, strict=True)
    direct_answer_includes_missing_slot_disclosure: bool = Field(default=False, strict=True)
    macro_summary_seed: str = ""
    asset_read_narrative_seed: str = ""
    asset_read_seed: str = ""
    risk_seed: str = ""
    summary_caveat_seed: str = ""
    recommendation_mode_seed: Literal["actionable_options", "directional_watchlist", "informational_only"] = "directional_watchlist"
    status_note: str = ""
    status_note_severity: Literal["none", "soft_note", "hard_boundary"] = "none"
    safe_for_frontend: bool = Field(default=True, strict=True)

# ==========================================
# 4. Global AgentState (core contract)
# ==========================================

class AgentState(TypedDict):
    """
    LangGraph global state: the "living dictionary" of the system.
    """
    # --- 原始输入 ---
    original_query: str
    query_builder_contract: Optional[Dict[str, Any]]
    macro_context: str  # latest macro snapshot content injected from external source

    # --- Retrieval layer data (with lineage) ---
    metadata: QueryMetadata
    gold_context: List[Dict[str, Any]]  # Qdrant returns, must contain {'id': ..., 'text': ...}
    supplemental_news_context: List[Dict[str, Any]]  # Supplemental macro-news lane for narrative families.
    # silver_context example:
    # {
    #   'values': {...},
    #   'lineage_anchors': [...],                 # audit/provenance refs
    #   'citation_contract': {...},               # authoritative raw Silver citation payload written by retrieval
    #   'citation_anchor_map': {...},             # deprecated metric -> preferred anchor shim
    #   'source_channel': 'primary'
    # }
    silver_context: Dict[str, Any]

    # 🔒 Immutable Silver snapshot — written ONCE by retrieval_master at exit,
    # never modified by rescue or revision passes.
    # Checker uses this as the deterministic numeric ground truth so that
    # Rescue-driven updates to `silver_context` (which are additive and used
    # by the NEXT analyst revision) cannot shift the audit baseline mid-round
    # and cause the IV-regime / numeric-match oscillation documented in
    # docs/test/2026-04-22/router_e2e_deep_analysis.md §6.5 (Test 4).
    # Structure mirrors silver_context:
    # {
    #   values: Dict,
    #   lineage_anchors: List[str],
    #   citation_contract: Dict[str, Dict[str, Any]],   # raw payload; agents normalize it via the shared core contract
    #   citation_anchor_map: Dict[str, str],
    # }
    silver_context_frozen: Optional[Dict[str, Any]]

    # 🌟 Time audit: Retriever uniformly computes the retrieval interval, Analyst must declare at the end
    # structure: {time_window_label, window_days, anchor_date, start_date, end_date,
    #        is_default_window_applied}
    time_range: Optional[Dict[str, Any]]
    scope_contract: Optional[Dict[str, Any]]
    retrieval_outcome: Optional[Dict[str, Any]]

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
    # Internal working draft only.
    # This field may contain revision scaffolding, fallback copy, or other
    # non-user-safe text. It is never a frontend-renderable field and must
    # not be echoed into final_report / markdown.
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
    checker_edit_suggestions: Optional[List[FinalizerEdit]]
    critic_edit_suggestions: Optional[List[FinalizerEdit]]

    # 🌟 Short-circuit decision fields: only read these two fields to decide the next jump,
    # avoid time window inference of "who reported the error in the last round" in the append-only list.
    # value domain: "pass" | "fatal" | "minor" | None(not yet audited)
    checker_verdict: Optional[Literal["pass", "fatal", "minor"]]
    critic_verdict: Optional[Literal["pass", "fatal", "minor"]]

    # Critic-side reasoning contracts and recommendation mode. These are
    # optional because older traces / callers may not populate them.
    data_capability_profile: Optional[Dict[str, Any]]
    critic_reasoning_profile: Optional[Dict[str, Any]]
    recommendation_mode: Optional[Literal["actionable_options", "directional_watchlist", "informational_only"]]
    actionability_mode: Optional[Literal["actionable_options", "directional_watchlist", "informational_only"]]
    structure_visibility_mode: Optional[Literal["recommended_structure", "illustrative_structure", "no_structure"]]
    # revision_constraints may additionally carry finalizer render ownership:
    # {
    #   mode_boundary_text: str,
    #   illustrative_structure_text: str,
    #   true_risk_text: str,
    #   evidence_coverage_note: str,
    #   evidence_coverage_severity: "none" | "soft_note" | "hard_gap",
    #   read_valid_despite_coverage_gap: bool,
    #   section_ownership: {
    #       direct_conclusion: "evidence_only",
    #       asset_read: "evidence_recap_only",
    #       recommendation_mode: "boundary_only",
    #       risks: "true_risks_only",
    #   }
    # }
    revision_constraints: Optional[Dict[str, Any]]
    analyst_contract_audit: Optional[Dict[str, Any]]

    # Finalizer-only input card. This is the single structured handoff the
    # Finalizer should prefer over re-reading mixed upstream state fields.
    # Ownership boundary:
    #   - Analyst contributes evidence / risk / structure-hint context
    #     plus posture synthesis when the read-only posture contract is active
    #     (posture_label, posture_takeaway, posture_rationale,
    #      posture_reasoning_trace)
    #   - Analyst also contributes `render_safety_contract`, the user-safe
    #     content surface that may be rendered in any runtime mode. This
    #     contract must never be populated by copying `draft_report`.
    #     Notable ownership flags:
    #       - direct_answer_seed: already composed answer-first prose
    #       - direct_answer_includes_posture_takeaway: whether the seed already
    #         contains the posture prelude, so Finalizer must not prepend it
    #       - summary_caveat_seed: optional short summary-only caveat, distinct
    #         from the full risk sentence owned by key_risks_and_hedges
    #   - Critic contributes governance constraints / output mode /
    #     render ownership bundle (mode_boundary_text, illustrative_structure_text,
    #     true_risk_text, evidence_coverage_note, answer_status_note,
    #     section_ownership)
    #   - Finalizer consumes this card for final wording only
    finalizer_input_card: Optional[Dict[str, Any]]

    # --- Flow control (solve Risk 3) ---
    revision_count: int  # number of drafts produced by Analyst; only Analyst node is responsible for increment
    is_fallback: bool    # whether the fallback logic is triggered
    analyst_fallback_used: Optional[bool]  # whether Analyst switched to OpenAI fallback in this run
    
    # --- Final product ---
    # force final_strategy to contain evidence_links to implement data lineage
    final_strategy: Dict[str, Any]

    # 🔍 Per-node audit trail — append-only, never reset between revisions.
    # Each node wrapper in router.py appends one Dict per call:
    #   {"node", "revision_n", "t_start", "latency_ms", "verdict", "findings_n",
    #    "key_state_in", "key_state_out"}
    # Enables full replay of the pipeline history from a single state dump.
    node_audit_log: Annotated[List[Dict[str, Any]], operator.add]
