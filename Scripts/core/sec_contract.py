from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


SECFormType = Literal["4", "8-K"]
SECCoverageMode = Literal["full", "partial", "none"]
SECTone = Literal["negative", "neutral", "positive"]


class SECRequestedForms(BaseModel):
    sec_forms_requested: List[SECFormType] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


class SECCoverageContract(BaseModel):
    sec_forms_requested: List[SECFormType] = Field(default_factory=list)
    sec_forms_retrieved: List[SECFormType] = Field(default_factory=list)
    missing_forms: List[SECFormType] = Field(default_factory=list)
    coverage_mode: SECCoverageMode = Field(default="none")
    sec_slot_hits: List[str] = Field(default_factory=list)
    sec_slot_missing: List[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


class SECExistenceResult(BaseModel):
    sec_forms_requested: List[SECFormType] = Field(default_factory=list)
    sec_forms_retrieved: List[SECFormType] = Field(default_factory=list)
    sec_payload_context_by_form: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    sec_slot_hits: List[str] = Field(default_factory=list)
    sec_slot_missing: List[str] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True)


class Form4Feature(BaseModel):
    ticker: str = ""
    form_type: Literal["4"] = "4"
    accession_no: str = ""
    owner: str = ""
    role: str = ""
    action_direction: str = "NONE"
    transaction_date: str = ""
    filed_at: str = ""
    shares: Optional[float] = None
    price: Optional[float] = None
    total_value: Optional[float] = None
    remaining_shares: Optional[float] = None
    is_10b5_1_planned: bool = False
    is_cluster_trade: bool = False
    cluster_count: int = 1
    content: str = ""
    url: str = ""

    model_config = ConfigDict(frozen=True)


class Form8KFeature(BaseModel):
    ticker: str = ""
    form_type: Literal["8-K"] = "8-K"
    accession_no: str = ""
    filed_at: str = ""
    tone_score: Optional[float] = None
    topics: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    content: str = ""
    url: str = ""

    model_config = ConfigDict(frozen=True)


class Form4AnalysisContract(BaseModel):
    filing_count: int = 0
    sell_count: int = 0
    buy_count: int = 0
    vest_count: int = 0
    planned_count: int = 0
    cluster_count: int = 0
    total_value: Optional[float] = None
    directional_read: Literal["selling_pressure", "buying_support", "compensation_vesting", "mixed"] = "mixed"

    model_config = ConfigDict(frozen=True)


class Form8KAnalysisContract(BaseModel):
    filing_count: int = 0
    negative_count: int = 0
    positive_count: int = 0
    neutral_count: int = 0
    dominant_tone: SECTone = "neutral"
    categories: List[str] = Field(default_factory=list)
    event_pressure: Literal["elevated", "contained", "constructive", "mixed"] = "contained"
    repeat_pattern: Literal["isolated", "repeated"] = "isolated"
    latest_filing_date: str = ""
    latest_filing_content: str = ""

    model_config = ConfigDict(frozen=True)


class SECMissingDisclosure(BaseModel):
    missing_forms: List[SECFormType] = Field(default_factory=list)
    missing_slots: List[str] = Field(default_factory=list)
    coverage_mode: SECCoverageMode = Field(default="none")
    has_missing: bool = False

    model_config = ConfigDict(frozen=True)


class SECAnalysisBundle(BaseModel):
    coverage: SECCoverageContract = Field(default_factory=SECCoverageContract)
    existence: SECExistenceResult = Field(default_factory=SECExistenceResult)
    form4_features: List[Form4Feature] = Field(default_factory=list)
    form8k_features: List[Form8KFeature] = Field(default_factory=list)
    form4_analysis_result: Form4AnalysisContract = Field(default_factory=Form4AnalysisContract)
    form8k_analysis_result: Form8KAnalysisContract = Field(default_factory=Form8KAnalysisContract)
    missing_disclosure: SECMissingDisclosure = Field(default_factory=SECMissingDisclosure)

    model_config = ConfigDict(frozen=True)
