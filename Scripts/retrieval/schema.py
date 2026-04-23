"""
Scripts/retrieval/schema.py
Core Protocol for Financial Multi-Agent RAG.
Senior Architect Version: Two-Stage Pipeline (Extractor + HyDE)
"""

from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Optional, Dict, Any
from enum import Enum
from datetime import datetime

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
    val = getattr(tw, "value", tw)
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

class MetadataExtraction(BaseModel):
    """
    STAGE 1: Pure Metadata Extraction (The Extractor)
    Forces the LLM to output ONLY strict analytical parameters.
    """
    logical_reasoning: str = Field(..., description="Mandatory Analysis: Use 'Macro -> Meso -> Micro' framework. Analyze When, How, and Why before extraction.")
    tickers: List[str] = Field(..., description="Extracted stock symbols. Output empty list [] if none.")
    metrics: List[str] = Field(..., description="Quantitative metrics mentioned. Output empty list [] if none.")
    source_types: List[SourceType] = Field(..., description="Inferred data sources. MUST choose at least one.")
    action_direction: ActionDirection = Field(..., description="Insider trading action. MUST output 'NONE' if not applicable.")
    form_type: FormTypeFilter = Field(..., description="SEC form types. MUST output 'ALL' if not applicable.")
    sentiment_target: SentimentTarget = Field(..., description="User's sentiment bias. MUST output 'ANY' if not applicable.")
    event_keyword: str = Field(..., description="A 1-to-3 word keyword of the trigger. Output empty string '' if none.")
    time_window: TimeWindow = Field(..., description="Default to 'past_six_months' if not specified.")

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True, arbitrary_types_allowed=True)

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
        if v not in [item.value for item in TimeWindow]:
            return TimeWindow.PAST_SIX_MONTHS
        return v

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

class FullTransformationResult(BaseModel):
    """Aggregates the results of the Two-Stage Pipeline for the Router."""
    metadata: MetadataExtraction
    hyde: HyDEGeneration
    mapped_physical_columns: List[str] = Field(default_factory=list)

# ==========================================
# 3. ROUTING SCHEMAS (System State)
# ==========================================

class SECMetadata(BaseModel):
    ticker: str
    form_type: str
    accession_no: str
    filed_at: str
    transaction_date: Optional[str] = None
    action_direction: ActionDirection = ActionDirection.NONE
    tone_score: int
    url: str

class QueryIntent(BaseModel):
    primary_route: str = Field(description="One of: 'hybrid_both', 'gold_only', 'silver_only'")
    is_complex: bool = Field(default=False)
    search_top_k: int = Field(default=5)
    requires_hyde: bool = Field(default=True)

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

class AgentState(BaseModel):
    input_query: str
    macro_context: str = Field("", description="Daily market background injected into prompts")
    intent: Optional[QueryIntent] = None
    transformation_result: Optional[FullTransformationResult] = None
    raw_retrieved_chunks: List[RetrievedChunk] = []
    sql_context: Optional[SQLResult] = None