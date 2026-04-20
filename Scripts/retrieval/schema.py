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
    PAST_WEEK = "past_week"
    PAST_MONTH = "past_month"
    ALL = "all"

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
        if v not in [item.value for item in TimeWindow]:
            return TimeWindow.ALL
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