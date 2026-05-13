"""
Scripts/retrieval/schema.py
Core Protocol for Financial Multi-Agent RAG.
Senior Architect Version: Two-Stage Pipeline (Extractor + HyDE)
"""

from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Optional, Dict, Any, Literal
from enum import Enum
from datetime import datetime

from Scripts.core.sec_contract import SECAnalysisBundle

# ==========================================
# 1. ENUMS FOR STRICT VALIDATION (Retained)
# ==========================================

class SourceType(str, Enum):
    SEC = "sec"
    NEWS = "news"
    GPR = "gpr"
    MACRO_HISTORY = "macro_history" 
    OPTIONS = "options"

class ActionDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    ACQUIRE = "ACQUIRE/VEST"
    NONE = "NONE"

class SearchMode(str, Enum):
    VECTOR_ONLY = "vector_only"
    HYBRID = "hybrid"
    METADATA_ONLY = "metadata_only"

class TimeWindow(str, Enum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    PAST_WEEK = "past_week"
    PAST_MONTH = "past_month"
    PAST_SIX_MONTHS = "past_six_months"
    ALL = "all"


_TIME_WINDOW_ALIASES: Dict[str, str] = {
    TimeWindow.TODAY.value: TimeWindow.TODAY.value,
    "current_day": TimeWindow.TODAY.value,
    TimeWindow.YESTERDAY.value: TimeWindow.YESTERDAY.value,
    "previous_day": TimeWindow.YESTERDAY.value,
    TimeWindow.PAST_WEEK.value: TimeWindow.PAST_WEEK.value,
    "past_7_days": TimeWindow.PAST_WEEK.value,
    "last_week": TimeWindow.PAST_WEEK.value,
    "week": TimeWindow.PAST_WEEK.value,
    TimeWindow.PAST_MONTH.value: TimeWindow.PAST_MONTH.value,
    "past_30_days": TimeWindow.PAST_MONTH.value,
    "last_month": TimeWindow.PAST_MONTH.value,
    "month": TimeWindow.PAST_MONTH.value,
    TimeWindow.PAST_SIX_MONTHS.value: TimeWindow.PAST_SIX_MONTHS.value,
    "past_6_months": TimeWindow.PAST_SIX_MONTHS.value,
    "last_6_months": TimeWindow.PAST_SIX_MONTHS.value,
    "last_six_months": TimeWindow.PAST_SIX_MONTHS.value,
    "six_months": TimeWindow.PAST_SIX_MONTHS.value,
    "6_months": TimeWindow.PAST_SIX_MONTHS.value,
    TimeWindow.ALL.value: TimeWindow.ALL.value,
    "all_history": TimeWindow.ALL.value,
    "full_history": TimeWindow.ALL.value,
}


def normalize_time_window_value(value: Any, *, default: str = TimeWindow.PAST_SIX_MONTHS.value) -> str:
    """Normalize structured time aliases onto the canonical TimeWindow values.

    This is the only compatibility shim for structured callers. Free-form
    language should still be resolved by the extractor; structured builder
    payloads should pass through this map before validation.
    """
    raw = getattr(value, "value", value)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    normalized = text.lower().replace(" ", "_").replace("-", "_")
    return _TIME_WINDOW_ALIASES.get(normalized, normalized)

# ---------------------------------------------------------------------
# Global TimeWindow -> day-count policy (single source of truth).
#
# Rationale:
#   - Kept in schema.py (not master_retriever.py) so the Gold retriever
#     (qdrant_retriever.py) and the Silver retriever (sql_tools.py) can
#     both consume it without creating a circular import against the
#     Master Retriever facade.
#   - master_retriever.py re-exports `TIME_WINDOW_DAYS` and
#     `time_window_to_days` at module-level, so downstream callers can
#     still treat master_retriever as the canonical global access point.
#   - `ALL` is set to 365 days: treated as the hard ceiling for "all history"
#     requests. Historical-data checks without an explicit time signal fall
#     back to PAST_SIX_MONTHS (180 days) via the MetadataExtraction validator
#     below — NOT to ALL — so default recall doesn't silently scan a year of
#     data. Gold time filtering can still override via `fallback_days`.
#   - Options spread fields follow the Silver-layer percentage-point contract:
#     `spread_pct = 2.5` means a 2.5% spread, not 0.025. Retrieval/display
#     layers should preserve that unit and must not multiply it by 100 again.
# ---------------------------------------------------------------------
TIME_WINDOW_DAYS: Dict[str, int] = {
    TimeWindow.TODAY.value:           1,
    # "yesterday" = 2 days so that Friday's anchor still covers Monday queries
    # (weekend gap). Downstream SQL handlers that pick `ORDER BY DESC LIMIT 1`
    # will naturally grab the most recent available trading day.
    TimeWindow.YESTERDAY.value:       2,
    TimeWindow.PAST_WEEK.value:       7,
    TimeWindow.PAST_MONTH.value:      30,
    TimeWindow.PAST_SIX_MONTHS.value: 180,
    TimeWindow.ALL.value:             365,
}


def time_window_to_days(tw: Any, default: int = 30) -> int:
    """
    Translate a metadata.time_window value (str or TimeWindow enum) into
    a day count using the global `TIME_WINDOW_DAYS` policy.

    Returns `default` when the input is missing or unrecognised. This is
    the ONLY function both Gold and Silver layers should use to reason
    about lookback windows — do not hard-code day counts elsewhere.
    """
    val = normalize_time_window_value(getattr(tw, "value", tw), default="")
    if not isinstance(val, str):
        return default
    return TIME_WINDOW_DAYS.get(val.lower(), default)

class FormTypeFilter(str, Enum):
    FORM_4 = "4"
    FORM_8K = "8-K"
    ALL = "ALL"

class SentimentTarget(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    ANY = "ANY"

# ==========================================
# 2. TWO-STAGE LLM OUTPUT SCHEMAS
# ==========================================

class NewsSemanticProfilePayload(BaseModel):
    """Closed JSON-schema payload for deterministic news semantic expansion."""

    tickers: List[str] = Field(default_factory=list)
    canonical_topics: List[str] = Field(default_factory=list)
    expanded_topics: List[str] = Field(default_factory=list)
    news_search_terms: List[str] = Field(default_factory=list)
    news_asset_terms: List[str] = Field(default_factory=list)
    news_driver_terms: List[str] = Field(default_factory=list)
    dense_context_terms: List[str] = Field(default_factory=list)
    impacted_asset_aliases: List[str] = Field(default_factory=list)
    impact_basket: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

class MetadataExtraction(BaseModel):
    """
    STAGE 1: Pure Metadata Extraction (The Extractor)
    Forces the LLM to output ONLY strict analytical parameters.
    """
    logical_reasoning: str = Field(..., description="Mandatory Analysis: Use 'Macro -> Meso -> Micro' framework. Analyze When, How, and Why before extraction.")
    tickers: List[str] = Field(..., description="Extracted stock symbols. Output empty list [] if none.")
    signals: List[str] = Field(default_factory=list, description="Structured builder-selected signals, when available.")
    metrics: List[str] = Field(..., description="Quantitative metrics mentioned. Output empty list [] if none.")
    source_types: List[SourceType] = Field(..., description="Inferred data sources. MUST choose at least one.")
    action_direction: ActionDirection = Field(..., description="Insider trading action. MUST output 'NONE' if not applicable.")
    form_type: FormTypeFilter = Field(..., description="SEC form types. MUST output 'ALL' if not applicable.")
    requested_sec_forms: List[FormTypeFilter] = Field(
        default_factory=list,
        description="Structured SEC filing subtypes explicitly requested by the caller. Use this to preserve mixed SEC asks like 8-K plus Form 4."
    )
    sentiment_target: SentimentTarget = Field(..., description="User's sentiment bias. MUST output 'ANY' if not applicable.")
    event_keyword: str = Field(..., description="A 1-to-3 word keyword of the trigger. Output empty string '' if none.")
    goal_contract: str = Field(
        default="",
        description="Canonical internal goal contract supplied by structured callers; backend should prefer this over free-form goal labels when present."
    )
    time_window: TimeWindow = Field(..., description="Default to 'past_six_months' if not specified.")
    primary_theme: Literal["insider", "geopolitics", "cross_asset", "options"] = Field(
        default="options",
        description="Primary analytical theme inferred from structured intent."
    )
    primary_surface: Literal["options_surface", "macro_news_surface"] = Field(
        default="macro_news_surface",
        description="Primary analytical surface required by the answer contract."
    )
    canonical_news_topics: List[str] = Field(
        default_factory=list,
        description="Canonical ontology news topics required for Gold news retrieval."
    )
    primary_news_topic: str = Field(
        default="",
        description="Primary canonical news topic that anchors the narrative intent."
    )
    expanded_news_topics: List[str] = Field(
        default_factory=list,
        description="Expanded canonical topic scope used for Gold news retrieval when adjacent ontology buckets are relevant."
    )
    news_search_terms: List[str] = Field(
        default_factory=list,
        description="Deterministic news-language terms used to retrieve asset-relevant articles without requiring ticker payloads."
    )
    news_asset_terms: List[str] = Field(
        default_factory=list,
        description="Asset words commonly used by news sources for the requested tickers, e.g. gold/bullion for GLD."
    )
    news_driver_terms: List[str] = Field(
        default_factory=list,
        description="Macro driver words used by news sources for this narrative, e.g. real yields, dollar, Fed."
    )
<<<<<<< Updated upstream
<<<<<<< Updated upstream
    news_semantic_profile: Dict[str, Any] = Field(
        default_factory=dict,
=======
=======
>>>>>>> Stashed changes
    dense_context_terms: List[str] = Field(
        default_factory=list,
        description="Dense-only semantic expansion terms. These must not be passed to sparse keyword retrieval."
    )
    news_semantic_profile: NewsSemanticProfilePayload = Field(
        default_factory=NewsSemanticProfilePayload,
<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        description="Serializable NewsSemanticProfile from financial_narrative_contract."
    )
    analysis_surfaces: List[str] = Field(
        default_factory=list,
        description="Structured analytical layers to cover, such as insider_signal, options_surface, macro_context, geopolitical_context, benchmark_context."
    )
    comparison_targets: List[str] = Field(
        default_factory=list,
        description="Secondary tickers or indices used for benchmark context rather than primary family selection."
    )
    asset_scope: Literal["single_name", "benchmark", "basket", "unspecified"] = Field(
        default="unspecified",
        description="Structured asset scope for the requested read."
    )
    read_profile: Literal["board_state", "posture_read", "event_risk", "structure_request"] = Field(
        default="board_state",
        description="Structured answer-shaping profile used by retrieval and reporting."
    )

    model_config = ConfigDict(
        use_enum_values=True,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    @field_validator('tickers')
    @classmethod
    def normalize_tickers(cls, v: List[str]) -> List[str]:
        return [ticker.upper().strip() for ticker in v]

    @field_validator('time_window', mode='before')
    @classmethod
    def handle_empty_window(cls, v: Any) -> TimeWindow:
        # Time-window fallback policy (single source of truth):
        #   - User says "all history" / explicit "all"            -> TimeWindow.ALL     (365 days)
        #   - User omits time / LLM hallucinates an invalid value -> PAST_SIX_MONTHS    (180 days)
        #   - All other canonical windows (today / past_week / past_month / past_six_months)
        #     are passed through unchanged.
        # Matches the extractor prompt ("Default to 6 months if unspecified") and
        # the Silver/Gold retrievers that read TIME_WINDOW_DAYS.
        normalized = normalize_time_window_value(v)
        if normalized not in [item.value for item in TimeWindow]:
            return TimeWindow.PAST_SIX_MONTHS
        return normalized

class HyDEGeneration(BaseModel):
    """
    STAGE 2: Semantic Paragraph Generation (The Writer)
    Generates a rich causal chain for Vector Search.
    """
    hyde_paragraph: str = Field(..., description="A 100 word high-density financial simulation based strictly on the metadata.")
    rerank_query: str = Field(
        ..., 
        description=(
            "A clean, factual search query stripped of analytical noise (like 'impact', 'skew', 'why'). "
            "Focus ONLY on the core entities and events to be found in the database. "
            "Example: If user asks 'How did AAPL Form 4 offloading impact IV skew?', "
            "you MUST output exactly 'AAPL executive insider selling Form 4 transactions'."
        )
        )

<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
    model_config = ConfigDict(extra="forbid")

>>>>>>> Stashed changes
=======
    model_config = ConfigDict(extra="forbid")

>>>>>>> Stashed changes
class BuilderQueryContract(BaseModel):
    """Structured frontend-builder contract carried alongside the free-text query."""

    query: str = Field(..., description="Human-readable query preview shown in the UI.")
    signals: List[str] = Field(default_factory=list, description="Exact builder-selected signals.")
    source_types: List[SourceType] = Field(default_factory=list)
    metrics: List[str] = Field(default_factory=list)
    requested_sec_forms: List[FormTypeFilter] = Field(default_factory=list)
    tickers: List[str] = Field(default_factory=list)
    time_window: TimeWindow = Field(default=TimeWindow.PAST_WEEK)

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True, arbitrary_types_allowed=True)

    @field_validator('time_window', mode='before')
    @classmethod
    def normalize_time_window(cls, v: Any) -> TimeWindow:
        normalized = normalize_time_window_value(v, default=TimeWindow.PAST_WEEK.value)
        if normalized not in [item.value for item in TimeWindow]:
            return TimeWindow.PAST_WEEK
        return normalized

class FullTransformationResult(BaseModel):
    """Aggregates the results of the Two-Stage Pipeline for the Router."""
    metadata: MetadataExtraction
    hyde: HyDEGeneration
    mapped_physical_columns: List[str] = Field(default_factory=list)
    in_scope_tickers: List[str] = Field(default_factory=list)
    out_of_scope_tickers: List[str] = Field(default_factory=list)
    supporting_contracts: List[Dict[str, Any]] = Field(default_factory=list)

# ==========================================
# 3. ROUTING SCHEMAS (System State)
# ==========================================

class SECMetadata(BaseModel):
    ticker: str
    form_type: str
    accession_no: str
    filed_at: str
    filed_at_epoch_s: Optional[int] = None
    transaction_date: Optional[str] = None
    transaction_date_epoch_s: Optional[int] = None
    action_direction: ActionDirection = ActionDirection.NONE
    tone_score: int
    url: str

class QueryIntent(BaseModel):
    primary_route: str = Field(description="One of: 'hybrid_both', 'gold_only', 'silver_only'")
    is_complex: bool = Field(default=False)
    search_top_k: int = Field(default=5)
    requires_hyde: bool = Field(default=True)


class TimeContract(BaseModel):
    """Read-only summary of the requested versus effective time window."""

    requested_window: str = Field(default=TimeWindow.PAST_SIX_MONTHS.value)
    effective_window: str = Field(default=TimeWindow.PAST_SIX_MONTHS.value)
    window_days: int = Field(default=180)
    is_default_window_applied: bool = Field(default=False)
    is_extended_window: bool = Field(default=False)

    model_config = ConfigDict(frozen=True)


class SourceCoverageContract(BaseModel):
    """Read-only source-coverage summary produced after retrieval completes."""

    strict_sources_expected: List[str] = Field(default_factory=list)
    strict_sources_hit: List[str] = Field(default_factory=list)
    soft_sources_expected: List[str] = Field(default_factory=list)
    soft_sources_hit: List[str] = Field(default_factory=list)
    missing_strict_sources: List[str] = Field(default_factory=list)
    missing_query_slots: List[str] = Field(default_factory=list)
    news_coverage_status: Literal["not_applicable", "fresh_news_found", "no_fresh_news_retrieved"] = Field(default="not_applicable")
    background_only_read: bool = Field(default=False)
    retrieved_news_count: int = Field(default=0)
    supplemental_news_status: Literal["not_applicable", "supplemental_news_found", "supplemental_news_missing"] = Field(default="not_applicable")
    supplemental_news_count: int = Field(default=0)

    model_config = ConfigDict(frozen=True)


class ScopeContract(BaseModel):
    """Runtime scope contract compiled once by MasterRetriever."""

    query_family: Literal[
        "options_microstructure",
        "insider_flow_driven",
        "cross_asset_regime",
        "geopolitical_macro_read",
        "geopolitical_options_read",
    ] = Field(default="options_microstructure")
    strict_sources: List[str] = Field(default_factory=list)
    soft_context_sources: List[str] = Field(default_factory=list)
    allowed_metrics: List[str] = Field(default_factory=list)
    unavailable_metrics: List[str] = Field(default_factory=list)
    supported_tickers: List[str] = Field(default_factory=list)
    requested_time_window: str = Field(default=TimeWindow.PAST_SIX_MONTHS.value)
    effective_time_window: str = Field(default=TimeWindow.PAST_SIX_MONTHS.value)
    output_mode_ceiling: Literal[
        "actionable_options",
        "directional_watchlist",
        "informational_only",
    ] = Field(default="directional_watchlist")
    specificity_ceiling: Literal[
        "structure_allowed",
        "watchlist_only",
        "no_structure",
    ] = Field(default="watchlist_only")
    required_disclosures: List[str] = Field(default_factory=list)
    query_slots: Dict[str, str] = Field(default_factory=dict)
    slot_evidence_contracts: Dict[str, Any] = Field(default_factory=dict)
    sec_action_taxonomy: Dict[str, str] = Field(default_factory=dict)
    sec_analysis_contract: Dict[str, Any] = Field(default_factory=dict)
    primary_ticker: str = Field(default="")
    analysis_mode: Literal["default_read", "data_backed_read"] = Field(default="default_read")
    coverage_basis: Literal[
        "silver_only",
        "silver_primary_with_soft_gold",
        "hybrid_required",
    ] = Field(default="silver_only")
    requires_catalyst_confirmation: bool = Field(default=True)
    gold_context_optional: bool = Field(default=False)
    hard_data_sufficient_for_answer: bool = Field(default=False)
    market_analysis_only: bool = Field(default=False)
    scope_status: Literal["in_scope", "out_of_scope"] = Field(default="in_scope")
    in_scope_tickers: List[str] = Field(default_factory=list)
    out_of_scope_tickers: List[str] = Field(default_factory=list)
    refusal_reason: str = Field(default="")
    primary_theme: Literal["insider", "geopolitics", "cross_asset", "options"] = Field(default="options")
    primary_surface: Literal["options_surface", "macro_news_surface"] = Field(default="macro_news_surface")
    canonical_news_topics: List[str] = Field(default_factory=list)
    primary_news_topic: str = Field(default="")
    expanded_news_topics: List[str] = Field(default_factory=list)
    news_search_terms: List[str] = Field(default_factory=list)
    news_asset_terms: List[str] = Field(default_factory=list)
    news_driver_terms: List[str] = Field(default_factory=list)
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
    dense_context_terms: List[str] = Field(default_factory=list)
>>>>>>> Stashed changes
=======
    dense_context_terms: List[str] = Field(default_factory=list)
>>>>>>> Stashed changes
    news_semantic_profile: Dict[str, Any] = Field(default_factory=dict)
    analysis_surfaces: List[str] = Field(default_factory=list)
    comparison_targets: List[str] = Field(default_factory=list)
    compensation_targets: List[str] = Field(default_factory=list)
    requested_sec_forms: List[str] = Field(default_factory=list)
    asset_scope: Literal["single_name", "benchmark", "basket", "unspecified"] = Field(default="unspecified")
    read_profile: Literal["board_state", "posture_read", "event_risk", "structure_request"] = Field(default="board_state")
    news_coverage_status: Literal["not_applicable", "fresh_news_found", "no_fresh_news_retrieved"] = Field(default="not_applicable")
    background_only_read: bool = Field(default=False)
    retrieved_news_count: int = Field(default=0)
    supplemental_news_status: Literal["not_applicable", "supplemental_news_found", "supplemental_news_missing"] = Field(default="not_applicable")
    supplemental_news_count: int = Field(default=0)

    model_config = ConfigDict(frozen=True)


class RetrievalOutcome(BaseModel):
    """Read-only retrieval outcome summary shared with downstream agents."""

    strict_sources_hit: List[str] = Field(default_factory=list)
    soft_sources_hit: List[str] = Field(default_factory=list)
    missing_strict_sources: List[str] = Field(default_factory=list)
    missing_query_slots: List[str] = Field(default_factory=list)
    sec_forms_requested: List[str] = Field(default_factory=list)
    sec_forms_retrieved: List[str] = Field(default_factory=list)
    sec_slot_hits: List[str] = Field(default_factory=list)
    sec_slot_missing: List[str] = Field(default_factory=list)
    sec_payload_context_by_form: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    sec_analysis_bundle: SECAnalysisBundle = Field(default_factory=SECAnalysisBundle)
    sec_analysis_features: List[Dict[str, Any]] = Field(default_factory=list)
    form4_analysis_result: Dict[str, Any] = Field(default_factory=dict)
    form8k_analysis_result: Dict[str, Any] = Field(default_factory=dict)
    sec_index_presence_mismatch: bool = Field(default=False)
    news_coverage_status: Literal["not_applicable", "fresh_news_found", "no_fresh_news_retrieved"] = Field(default="not_applicable")
    background_only_read: bool = Field(default=False)
    retrieved_news_count: int = Field(default=0)
    supplemental_news_status: Literal["not_applicable", "supplemental_news_found", "supplemental_news_missing"] = Field(default="not_applicable")
    supplemental_news_count: int = Field(default=0)
    has_gold_evidence: bool = Field(default=False)
    has_silver_evidence: bool = Field(default=False)
    is_fallback: bool = Field(default=False)
    time_window_extended: bool = Field(default=False)
    time_window_defaulted: bool = Field(default=False)
    time_contract: TimeContract = Field(default_factory=TimeContract)
    source_coverage: SourceCoverageContract = Field(default_factory=SourceCoverageContract)
    scope_status: Literal["in_scope", "out_of_scope"] = Field(default="in_scope")
    in_scope_tickers: List[str] = Field(default_factory=list)
    out_of_scope_tickers: List[str] = Field(default_factory=list)
    refusal_reason: str = Field(default="")

    model_config = ConfigDict(frozen=True)

class RetrievedChunk(BaseModel):
    content: str
    source_type: SourceType
    score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)
    bronze_ref: Optional[str] = "UNKNOWN"

class SQLResult(BaseModel):
    df_json: str
    query_executed: str
    summary: str


def _as_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    for method_name in ("model_dump", "dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                dumped = method()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                continue
    return {}


def render_scope_contract_block(scope_contract: Any) -> str:
    scope = _as_mapping(scope_contract)
    if not scope:
        return "(scope contract unavailable)"
    lines = [
        "=== SCOPE CONTRACT ===",
        f"query_family={scope.get('query_family')}",
        f"strict_sources={scope.get('strict_sources')}",
        f"soft_context_sources={scope.get('soft_context_sources')}",
        f"coverage_basis={scope.get('coverage_basis')}",
        f"allowed_metrics={scope.get('allowed_metrics')}",
        f"unavailable_metrics={scope.get('unavailable_metrics')}",
        f"supported_tickers={scope.get('supported_tickers')}",
        f"requested_time_window={scope.get('requested_time_window')}",
        f"effective_time_window={scope.get('effective_time_window')}",
        f"output_mode_ceiling={scope.get('output_mode_ceiling')}",
        f"specificity_ceiling={scope.get('specificity_ceiling')}",
        f"required_disclosures={scope.get('required_disclosures')}",
        f"query_slots={scope.get('query_slots')}",
        f"slot_evidence_contracts={list((scope.get('slot_evidence_contracts') or {}).keys())}",
        f"sec_action_taxonomy={scope.get('sec_action_taxonomy')}",
        f"sec_analysis_contract={scope.get('sec_analysis_contract')}",
        f"primary_ticker={scope.get('primary_ticker')}",
        f"analysis_mode={scope.get('analysis_mode')}",
        f"requires_catalyst_confirmation={scope.get('requires_catalyst_confirmation')}",
        f"gold_context_optional={scope.get('gold_context_optional')}",
        f"hard_data_sufficient_for_answer={scope.get('hard_data_sufficient_for_answer')}",
        f"market_analysis_only={scope.get('market_analysis_only')}",
        f"scope_status={scope.get('scope_status')}",
        f"in_scope_tickers={scope.get('in_scope_tickers')}",
        f"out_of_scope_tickers={scope.get('out_of_scope_tickers')}",
        f"refusal_reason={scope.get('refusal_reason') or '(none)'}",
        f"news_coverage_status={scope.get('news_coverage_status')}",
        f"background_only_read={scope.get('background_only_read')}",
        f"retrieved_news_count={scope.get('retrieved_news_count')}",
        f"supplemental_news_status={scope.get('supplemental_news_status')}",
        f"supplemental_news_count={scope.get('supplemental_news_count')}",
    ]
    return "\n".join(lines)


def render_retrieval_outcome_block(retrieval_outcome: Any) -> str:
    outcome = _as_mapping(retrieval_outcome)
    if not outcome:
        return "(retrieval outcome unavailable)"
    lines = [
        "=== RETRIEVAL OUTCOME ===",
        f"strict_sources_hit={outcome.get('strict_sources_hit')}",
        f"soft_sources_hit={outcome.get('soft_sources_hit')}",
        f"missing_strict_sources={outcome.get('missing_strict_sources')}",
        f"missing_query_slots={outcome.get('missing_query_slots')}",
        f"sec_forms_requested={outcome.get('sec_forms_requested')}",
        f"sec_forms_retrieved={outcome.get('sec_forms_retrieved')}",
        f"sec_payload_context_forms={list((outcome.get('sec_payload_context_by_form') or {}).keys())}",
        f"sec_coverage_mode={((outcome.get('sec_analysis_bundle') or {}).get('coverage') or {}).get('coverage_mode')}",
        f"sec_analysis_features_n={len(outcome.get('sec_analysis_features') or [])}",
        f"has_gold_evidence={outcome.get('has_gold_evidence')}",
        f"has_silver_evidence={outcome.get('has_silver_evidence')}",
        f"is_fallback={outcome.get('is_fallback')}",
        f"time_window_extended={outcome.get('time_window_extended')}",
        f"time_window_defaulted={outcome.get('time_window_defaulted')}",
        f"scope_status={outcome.get('scope_status')}",
        f"in_scope_tickers={outcome.get('in_scope_tickers')}",
        f"out_of_scope_tickers={outcome.get('out_of_scope_tickers')}",
        f"refusal_reason={outcome.get('refusal_reason') or '(none)'}",
        f"news_coverage_status={outcome.get('news_coverage_status')}",
        f"background_only_read={outcome.get('background_only_read')}",
        f"retrieved_news_count={outcome.get('retrieved_news_count')}",
        f"supplemental_news_status={outcome.get('supplemental_news_status')}",
        f"supplemental_news_count={outcome.get('supplemental_news_count')}",
    ]
    return "\n".join(lines)


def render_time_contract_block(time_contract: Any) -> str:
    contract = _as_mapping(time_contract)
    if not contract:
        return "(time contract unavailable)"
    lines = [
        "=== TIME CONTRACT ===",
        f"requested_window={contract.get('requested_window')}",
        f"effective_window={contract.get('effective_window')}",
        f"window_days={contract.get('window_days')}",
        f"is_default_window_applied={contract.get('is_default_window_applied')}",
        f"is_extended_window={contract.get('is_extended_window')}",
    ]
    return "\n".join(lines)

class AgentState(BaseModel):
    input_query: str
    macro_context: str = Field("", description="Daily market background injected into prompts")
    intent: Optional[QueryIntent] = None
    transformation_result: Optional[FullTransformationResult] = None
    raw_retrieved_chunks: List[RetrievedChunk] = []
    sql_context: Optional[SQLResult] = None
