"""
Scripts/agents/checker.py

Checker Agent (Blue Team) — deterministic + LLM fact & lineage audit of the
Analyst draft, with an opt-in Silver-tool escape hatch for missing anchors.

Design philosophy:
- The Checker is NOT a strategist. It is a silent, uncompromising data-integrity auditor.
- Robustness Upgrades:
    * Uses the structured Silver citation contract instead of anchor-name guessing.
    * Ignores calendar years and structural constants.
    * Aggressively truncates 'Strategy/Recommendation' sections before numeric audit.
"""

from __future__ import annotations

import os
import re
import logging
import json
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from Scripts.agents.state import AgentFeedback, FinalizerEdit
from Scripts.agents.prompts import get_checker_prompt
from Scripts.core.evidence_contracts import (
    build_gold_citation_registry,
    build_silver_citation_registry,
    normalize_silver_citation_contract,
    parse_silver_inline_payload,
    resolve_gold_anchor_ref,
    resolve_silver_anchor_ref,
    semantic_slot_evidence_eval,
)
from Scripts.core.sec_contract import SECAnalysisBundle
from Scripts.core.silver_context import effective_silver_context

logger = logging.getLogger(__name__)

# 放宽至 3% 的容差，给 LLM 四舍五入留足空间
_NUMERIC_REL_TOLERANCE = float(os.getenv("CHECKER_NUMERIC_TOLERANCE", "0.03"))
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))
_TOOL_RECOVERY_ENABLED = os.getenv("CHECKER_TOOL_RECOVERY", "1") == "1"
_COVERAGE_CHECK_ENABLED = os.getenv("CHECKER_COVERAGE_CHECK_ENABLED", "1") == "1"
_ALLOW_GOLD_AS_SILVER_ALIAS = os.getenv("CHECKER_ALLOW_GOLD_AS_SILVER_ALIAS", "1") == "1"
_STRICT_NUMERIC_REQUIRES_SILVER_CITATION = os.getenv("CHECKER_NUMERIC_REQUIRE_SILVER_CITATION", "1") == "1"

_COVERAGE_KEYS: Dict[str, Dict[str, Any]] = {
    "latest_atm_iv": {
        "label": "ATM implied volatility",
        "query_terms": ("iv", "implied volatility", "atm iv", "volatility"),
        "metric_terms": ("implied volatility (iv)", "iv skew", "iv"),
        "source_terms": ("options",),
    },
    "pcr_volume": {
        "label": "put/call ratio (volume)",
        "query_terms": ("put-call ratio", "put/call ratio", "pcr"),
        "metric_terms": ("put/call ratio", "pcr"),
        "source_terms": ("options",),
    },
    "gpr_index_level": {
        "label": "GPR geopolitical risk index",
        "query_terms": ("gpr", "geopolitic", "geopolitical risk"),
        "metric_terms": ("gpr index", "geopolitical risk"),
        "source_terms": ("gpr",),
    },
}

# ==========================================
# Structured LLM output
# ==========================================
class CheckerViolation(BaseModel):
    severity: str = Field(description="'Fatal' for true factual mismatches or hallucinated sources; 'Minor' for citation-form issues or non-blocking precision notes")
    description: str = Field(description="What was hallucinated vs what the real data says")
    cited_silver_value: Optional[str] = Field(default=None, description="Ground-truth value from silver_context")
    draft_value: Optional[str] = Field(default=None, description="Value claimed by the draft")
    metric_key: Optional[str] = Field(default=None, description="Canonical Silver metric key when identifiable.")
    violation_kind: Optional[Literal["factual_mismatch", "citation_form", "precision_only", "source_hallucination", "qualitative_overstatement", "scope_contract_mismatch"]] = Field(
        default=None,
        description="Optional violation taxonomy hint. Checker revalidates this deterministically."
    )

class CheckerResult(BaseModel):
    is_passed: bool = Field(description="True iff every claim is grounded AND cited")
    violations: List[CheckerViolation] = Field(default_factory=list)

# ==========================================
# Regex pass (deterministic layer 1)
# ==========================================
_NUMBER_RE = re.compile(r"(?<![\[\w.])(-?\d[\d,]*(?:\.\d+)?)(%?)(?![\]\w])")
_CITATION_RE = re.compile(r"\[(?P<kind>Silver|Gold)\s*:\s*(?P<anchor>[^\]]+)\]", re.IGNORECASE)

_STRATEGY_VOCAB_RE = re.compile(
    r"\b(dte|delta|strike|strikes?|days?\s+to\s+expir\w*|expir\w+|"
    r"spread|otm|atm|itm|leg|wing|condor|straddle|strangle|collar|"
    r"debit|credit|wide|breakeven|break-?even|payout|"
    r"max\s+loss|max\s+gain|risk[- ]reward|theta|vega|gamma|rho)\b",
    re.IGNORECASE,
)
_STRATEGY_WINDOW = 60

_COMMON_NUMERIC_WHITELIST: Set[float] = {
    0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 25.0, 30.0, 45.0, 50.0, 70.0, 100.0, 500.0,
    # Deterministic IV thresholds frequently echoed from iv_regime_block.
    0.18, 0.35,
}
_FORBIDDEN_INTERNAL_SILVER_ANCHORS: Set[str] = {"iv_regime_block"}
_MONTH_NAMES: Set[str] = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
}

_SENTINEL_ANCHORS: frozenset = frozenset({
    "insufficient data", "no direct data available", "no data", "no data available",
    "not applicable", "n/a", "na", "none", "unknown", "tbd", "pending",
})
_PLACEHOLDER_ANCHORS: Set[str] = {
    "gold_context",
    "silver_context",
    "macro_context",
}

def _is_sentinel_anchor(anchor: str) -> bool:
    if not anchor: return False
    return " ".join(anchor.lower().split()) in _SENTINEL_ANCHORS

def _is_strategy_parameter(m: "re.Match[str]", text: str) -> bool:
    start = max(0, m.start() - _STRATEGY_WINDOW)
    end = min(len(text), m.end() + _STRATEGY_WINDOW)
    return bool(_STRATEGY_VOCAB_RE.search(text[start:end]))


def _line_for_pos(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos)
    end = text.find("\n", pos)
    start = 0 if start < 0 else start + 1
    end = len(text) if end < 0 else end
    return text[start:end]

def _numeric_equiv(draft_val: float, truth_val: float, rel_tol: float = _NUMERIC_REL_TOLERANCE) -> bool:
    if truth_val == 0: return abs(draft_val - truth_val) < 1e-9
    if abs(draft_val - truth_val) / abs(truth_val) <= rel_tol: return True
    if abs(draft_val - truth_val * 100) / abs(truth_val * 100 or 1) <= rel_tol: return True
    if abs(draft_val * 100 - truth_val) / abs(truth_val) <= rel_tol: return True
    return False

def _numeric_equiv_abs(draft_val: float, truth_val: float, rel_tol: float = _NUMERIC_REL_TOLERANCE) -> bool:
    return _numeric_equiv(draft_val, truth_val, rel_tol) or _numeric_equiv(abs(draft_val), abs(truth_val), rel_tol)

def _extract_silver_numbers(silver_context: Dict[str, Any]) -> List[float]:
    out: List[float] = []
    values = (silver_context or {}).get("values") or {}
    for v in values.values():
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, str):
            try: out.append(float(v.replace(",", "").rstrip("%").strip()))
            except ValueError: continue
    return out


def _extract_gold_numbers(gold_context: List[Any]) -> List[float]:
    """Extract numeric tokens from Gold chunks as fallback factual whitelist."""
    out: List[float] = []
    seen: Set[str] = set()
    for chunk in gold_context or []:
        text = getattr(chunk, "content", None) or (chunk.get("content", "") if isinstance(chunk, dict) else "")
        if not isinstance(text, str) or not text:
            continue
        for m in _NUMBER_RE.finditer(text):
            raw = m.group(1)
            norm = raw.replace(",", "")
            if norm in seen:
                continue
            seen.add(norm)
            try:
                out.append(float(norm))
            except ValueError:
                continue
    return out


def _is_placeholder_anchor(anchor: str) -> bool:
    if not anchor:
        return False
    compact = " ".join(anchor.lower().split())
    return compact in _PLACEHOLDER_ANCHORS


def _should_suppress_llm_violation(description: str) -> bool:
    d = (description or "").lower()
    if not d:
        return False
    if "does not appear in gold context" in d and "gold_context" in d:
        return True
    if "draft cites" in d and any(k in d for k in _PLACEHOLDER_ANCHORS):
        return True
    return False


def _normalise_silver_citation_contract(silver_context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    values = (silver_context or {}).get("values") or {}
    lineage = [str(a) for a in ((silver_context or {}).get("lineage_anchors") or []) if a is not None]
    raw_contract = dict((silver_context or {}).get("citation_contract") or {})
    return normalize_silver_citation_contract(raw_contract, values, lineage)


def _build_silver_anchor_sets(silver_context: Dict[str, Any]) -> Dict[str, Any]:
    values = (silver_context or {}).get("values") or {}
    lineage = [str(a) for a in ((silver_context or {}).get("lineage_anchors") or []) if a is not None]
    raw_contract = dict((silver_context or {}).get("citation_contract") or {})
    registry = build_silver_citation_registry(raw_contract, values, lineage)
    contract = dict(registry.get("contract") or {})
    preferred_anchors: Set[str] = set((registry.get("preferred_anchor_to_metric") or {}).keys())
    legacy_alias_to_preferred: Dict[str, str] = {}
    audit_lineage_anchors: Set[str] = set((registry.get("audit_anchor_to_metrics") or {}).keys())
    for alias, metric_key in dict(registry.get("legacy_alias_to_metric") or {}).items():
        preferred_anchor = str((contract.get(metric_key) or {}).get("preferred_anchor") or metric_key)
        legacy_alias_to_preferred[str(alias)] = preferred_anchor

    return {
        "contract": contract,
        "preferred_anchors": preferred_anchors,
        "legacy_alias_to_preferred": legacy_alias_to_preferred,
        "audit_lineage_anchors": audit_lineage_anchors,
        "registry": registry,
    }


def _llm_violation_is_anchor_form_only(
    description: str,
    anchor_sets: Dict[str, Any],
) -> bool:
    lower = str(description or "").lower()
    if "missing citation" in lower or "lacks citation" in lower:
        return True
    if "citation" not in lower:
        return False
    if "silver shows" in lower and "which is correct" in lower:
        return True
    legacy_aliases = {
        str(alias).lower() for alias in (anchor_sets.get("legacy_alias_to_preferred") or {}).keys()
    }
    preferred_anchors = {
        str(anchor).lower() for anchor in (anchor_sets.get("preferred_anchors") or set())
    }
    return bool(
        legacy_aliases
        and any(f"[silver: {alias}]" in lower for alias in legacy_aliases)
        and any(f"[silver: {anchor}]" in lower for anchor in preferred_anchors)
    )


def _payload_has_repeated_silver_label(payload_text: str) -> bool:
    return str(payload_text or "").lower().count("silver:") > 0


def _coerce_truth_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").rstrip("%").strip())
        except ValueError:
            return None
    return None


def _metric_truth_numbers(metric_key: str, truth_ctx: Dict[str, Any]) -> List[float]:
    values = (truth_ctx or {}).get("values") or {}
    value = values.get(metric_key)
    coerced = _coerce_truth_number(value)
    return [coerced] if coerced is not None else []


def _metric_numeric_equiv(
    draft_val: float,
    truth_val: float,
    *,
    comparison_mode: str,
) -> bool:
    mode = str(comparison_mode or "raw_decimal").strip().lower()
    if mode == "display_percent_ratio":
        candidates = {truth_val}
        if abs(truth_val) <= 1.0:
            candidates.add(truth_val * 100.0)
        else:
            candidates.add(truth_val / 100.0)
        for candidate in candidates:
            if _numeric_equiv(draft_val, candidate):
                return True
            if abs(draft_val - round(candidate, 2)) <= 0.01:
                return True
        return False
    if mode == "display_percent_points":
        if _numeric_equiv(draft_val, truth_val):
            return True
        rounded_truth = round(truth_val, 2)
        if abs(draft_val - rounded_truth) <= 0.01:
            return True
        return False
    if mode == "ratio_or_percent":
        return _numeric_equiv(draft_val, truth_val)
    if mode == "signed_decimal":
        return _numeric_equiv(draft_val, truth_val)
    return _numeric_equiv(draft_val, truth_val)


def _extract_numbers_from_text(text: str) -> List[float]:
    values: List[float] = []
    for match in _NUMBER_RE.finditer(str(text or "")):
        try:
            values.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return values


def _llm_violation_is_precision_only(description: str, violation_kind: Optional[str]) -> bool:
    lower = str(description or "").lower()
    kind = str(violation_kind or "").strip().lower()
    if kind == "precision_only":
        return True
    return (
        "mismatch in precision" in lower
        or "which is correct" in lower
        or "precision-only" in lower
    )


def _llm_violation_has_repeated_supported_number(description: str) -> bool:
    numbers = _extract_numbers_from_text(description)
    if len(numbers) < 2:
        return False
    lead = numbers[0]
    for candidate in numbers[1:]:
        if _numeric_equiv(lead, candidate):
            return True
    return False


def _llm_violation_is_supported_claim_with_citation_issue(
    violation: CheckerViolation,
    anchor_sets: Dict[str, Any],
) -> bool:
    lower = str(violation.description or "").lower()
    kind = str(violation.violation_kind or "").strip().lower()
    citation_markers = (
        "citation",
        "cited",
        "source hallucination",
        "valid citation",
        "missing citation",
        "malformed citation",
    )
    support_markers = (
        "which is correct",
        "is correct",
        "matches",
        "supported",
    )
    if not any(marker in lower for marker in citation_markers):
        return False
    if kind not in {"citation_form", "source_hallucination", "scope_contract_mismatch", "factual_mismatch", ""}:
        return False
    if _llm_violation_numeric_equiv(violation, anchor_sets):
        return True
    return any(marker in lower for marker in support_markers) and _llm_violation_has_repeated_supported_number(violation.description)


def _llm_violation_is_qualitative_overstatement(description: str, violation_kind: Optional[str]) -> bool:
    kind = str(violation_kind or "").strip().lower()
    if kind == "qualitative_overstatement":
        return True
    lower = str(description or "").lower()
    markers = (
        "notable",
        "significant",
        "meaningful",
        "elevated",
        "constructive",
        "defensive",
        "overstated",
        "salience",
        "qualitative",
    )
    return any(marker in lower for marker in markers)


def _slot_disclosure_supported(
    retrieval_slot_eval: Dict[str, Any],
    slot_name: str,
) -> bool:
    slot_status = dict((retrieval_slot_eval or {}).get("slot_status") or {})
    return str(slot_status.get(slot_name) or "") == "disclosed_unanswerable"


def _requested_sec_forms_from_metadata(metadata_dict: Dict[str, Any]) -> List[str]:
    forms: List[str] = []
    for raw in list(metadata_dict.get("requested_sec_forms") or []):
        form = str(getattr(raw, "value", raw) or "").strip().upper()
        if form in {"8-K", "4"} and form not in forms:
            forms.append(form)
    if forms:
        return forms
    form_type = str(metadata_dict.get("form_type") or "").strip().upper()
    if form_type in {"8-K", "4"}:
        return [form_type]
    return []


def _sec_analysis_bundle_from_outcome(retrieval_outcome: Dict[str, Any]) -> SECAnalysisBundle:
    bundle = dict(retrieval_outcome or {}).get("sec_analysis_bundle") or {}
    if isinstance(bundle, SECAnalysisBundle):
        return bundle
    if isinstance(bundle, dict) and bundle:
        try:
            return SECAnalysisBundle.model_validate(bundle)
        except Exception:
            pass
    return SECAnalysisBundle()


def _llm_violation_is_valid_missing_source_disclosure(
    violation: CheckerViolation,
    *,
    retrieval_slot_eval: Dict[str, Any],
    metadata_dict: Dict[str, Any],
    retrieval_outcome: Optional[Dict[str, Any]] = None,
) -> bool:
    lower = str(violation.description or "").lower()
    kind = str(violation.violation_kind or "").strip().lower()
    if kind not in {"source_hallucination", "scope_contract_mismatch", "factual_mismatch", ""}:
        return False
    sec_bundle = _sec_analysis_bundle_from_outcome(retrieval_outcome or {})
    coverage = sec_bundle.coverage
    requested_forms = _requested_sec_forms_from_metadata(metadata_dict)
    if "8-K" in requested_forms and (
        "8-K" in list(coverage.missing_forms or []) or _slot_disclosure_supported(retrieval_slot_eval, "sec_event_signal")
    ):
        markers = ("8-k", "8k", "sec filing data", "sec filing", "provided in the context", "lacks any mention")
        if any(marker in lower for marker in markers):
            return True
    if "4" in requested_forms and (
        "4" in list(coverage.missing_forms or []) or _slot_disclosure_supported(retrieval_slot_eval, "sec_insider_signal")
    ):
        markers = ("form 4", "form-4", "insider", "provided in the context", "lacks any mention")
        if any(marker in lower for marker in markers):
            return True
    return False


def _llm_violation_is_valid_macro_news_options_boundary(
    violation: CheckerViolation,
    *,
    retrieval_slot_eval: Dict[str, Any],
    metadata_dict: Dict[str, Any],
    retrieval_outcome: Optional[Dict[str, Any]] = None,
) -> bool:
    primary_surface = str(metadata_dict.get("primary_surface") or "").strip().lower()
    if primary_surface != "macro_news_surface":
        return False

    lower = str(violation.description or "").lower()
    option_markers = (
        "systematic options",
        "iv regime",
        "implied volatility",
        "detailed option",
        "option metrics",
        "iv skew",
        "liquidity posture",
        "options liquidity",
    )
    if not any(marker in lower for marker in option_markers):
        return False

    outcome = retrieval_outcome if isinstance(retrieval_outcome, dict) else {}
    missing_sources = {str(item).strip().lower() for item in (outcome.get("missing_strict_sources") or [])}
    missing_slots = {str(item).strip() for item in (outcome.get("missing_query_slots") or [])}
    slot_status = dict((retrieval_slot_eval or {}).get("slot_status") or {})
    boundary_slots = {"iv_skew_signal", "liquidity_signal", "atm_iv_signal", "iv_or_skew_signal"}
    if "options" in missing_sources:
        return True
    if missing_slots & boundary_slots:
        return True
    return any(slot_status.get(slot) == "not_reliably_answerable" for slot in boundary_slots)


def _is_calendar_date_number(match: "re.Match[str]", text: str) -> bool:
    raw = str(match.group(1) or "").replace(",", "")
    if "." in raw:
        return False
    try:
        value = int(raw)
    except ValueError:
        return False
    if value < 1 or value > 31:
        return False

    start = max(0, match.start() - 24)
    end = min(len(text), match.end() + 24)
    window = text[start:end].lower()
    chars: List[str] = []
    for ch in window:
        chars.append(ch if ch.isalpha() else " ")
    words = set("".join(chars).split())
    if words & _MONTH_NAMES:
        return True

    before = text[match.start() - 1] if match.start() > 0 else ""
    after = text[match.end()] if match.end() < len(text) else ""
    return before in {"-", "/"} or after in {"-", "/"}


def _llm_violation_numbers(violation: CheckerViolation) -> tuple[Optional[float], Optional[float]]:
    draft_num = _coerce_truth_number(violation.draft_value)
    truth_num = _coerce_truth_number(violation.cited_silver_value)
    if draft_num is not None and truth_num is not None:
        return draft_num, truth_num
    description_numbers = _extract_numbers_from_text(violation.description)
    if draft_num is None and description_numbers:
        draft_num = description_numbers[0]
    if truth_num is None and len(description_numbers) >= 2:
        truth_num = description_numbers[1]
    return draft_num, truth_num


def _llm_violation_comparison_modes(
    violation: CheckerViolation,
    anchor_sets: Dict[str, Any],
) -> List[str]:
    contract = dict(anchor_sets.get("contract") or {})
    metric_key = str(violation.metric_key or "").strip()
    if metric_key and metric_key in contract:
        return [str((contract.get(metric_key) or {}).get("comparison_mode") or "raw_decimal")]
    return [
        "raw_decimal",
        "signed_decimal",
        "display_percent_points",
        "display_percent_ratio",
        "ratio_or_percent",
    ]


def _llm_violation_numeric_equiv(
    violation: CheckerViolation,
    anchor_sets: Dict[str, Any],
) -> bool:
    draft_num, truth_num = _llm_violation_numbers(violation)
    if draft_num is None or truth_num is None:
        return False
    for mode in _llm_violation_comparison_modes(violation, anchor_sets):
        if _metric_numeric_equiv(draft_num, truth_num, comparison_mode=mode):
            return True
    return False


def _checker_feedback_from_llm_violation(
    violation: CheckerViolation,
    *,
    anchor_sets: Dict[str, Any],
    retrieval_slot_eval: Dict[str, Any],
    metadata_dict: Dict[str, Any],
    retrieval_outcome: Optional[Dict[str, Any]],
    revision_n: int,
    fallback_suffix: str = "",
) -> Optional[AgentFeedback]:
    if _should_suppress_llm_violation(violation.description):
        return None
    if _llm_violation_is_valid_missing_source_disclosure(
        violation,
        retrieval_slot_eval=retrieval_slot_eval,
        metadata_dict=metadata_dict,
        retrieval_outcome=retrieval_outcome,
    ):
        return None
    if _llm_violation_is_valid_macro_news_options_boundary(
        violation,
        retrieval_slot_eval=retrieval_slot_eval,
        metadata_dict=metadata_dict,
        retrieval_outcome=retrieval_outcome,
    ):
        return None
    if _llm_violation_is_anchor_form_only(violation.description, anchor_sets):
        return AgentFeedback(
            sender="Checker",
            error_type="Minor",
            comment=f"[rule:LLM_CONSISTENCY{fallback_suffix}:LEGACY_SILVER_ALIAS] {violation.description}",
            revision_index=revision_n,
        )
    if _llm_violation_is_supported_claim_with_citation_issue(violation, anchor_sets):
        return AgentFeedback(
            sender="Checker",
            error_type="Minor",
            comment=f"[rule:LLM_CONSISTENCY{fallback_suffix}:CITATION_FORM] {violation.description}",
            revision_index=revision_n,
        )
    if _llm_violation_is_qualitative_overstatement(violation.description, violation.violation_kind):
        return AgentFeedback(
            sender="Checker",
            error_type="Minor",
            comment=f"[rule:LLM_CONSISTENCY{fallback_suffix}:QUALITATIVE_OVERSTATEMENT] {violation.description}",
            revision_index=revision_n,
        )
    if _llm_violation_is_precision_only(violation.description, violation.violation_kind):
        if _llm_violation_numeric_equiv(violation, anchor_sets):
            return None
    sev = violation.severity if violation.severity in ("Fatal", "Minor") else "Fatal"
    if sev == "Fatal" and _llm_violation_numeric_equiv(violation, anchor_sets):
        sev = "Minor" if "citation" in str(violation.description or "").lower() else "Minor"
    return AgentFeedback(
        sender="Checker",
        error_type=sev,
        comment=f"[rule:LLM_CONSISTENCY{fallback_suffix}] {violation.description}",
        revision_index=revision_n,
    )


def _resolved_silver_metric_keys_for_line(line: str, registry: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for match in _CITATION_RE.finditer(line or ""):
        if str(match.group("kind") or "").strip().lower() != "silver":
            continue
        payload = str(match.group("anchor") or "")
        for ref in parse_silver_inline_payload(payload):
            resolved = resolve_silver_anchor_ref(ref, registry)
            metric_key = str(resolved.get("resolved_metric_key") or "").strip()
            if metric_key and metric_key not in keys:
                keys.append(metric_key)
    return keys

def _build_source_pools(frozen: Dict[str, Any]) -> Dict[str, List[float]]:
    _MACRO_PREFIXES = ("MACRO_", "VIX", "GSPC", "IXIC", "DXY", "GLD_SPOT", "SLV_SPOT", "FEDFUNDS", "CPIAUCSL", "UNRATE", "TNX", "BAML")
    values = (frozen or {}).get("values") or {}
    macro_nums, gpr_nums, other_nums = [], [], []

    for k, v in values.items():
        if isinstance(v, (int, float)): fv = float(v)
        elif isinstance(v, str):
            try: fv = float(v.replace(",", "").rstrip("%").strip())
            except ValueError: continue
        else: continue

        if any(k.startswith(p) for p in _MACRO_PREFIXES): macro_nums.append(fv)
        elif k.startswith("gpr"): gpr_nums.append(fv)
        else: other_nums.append(fv)
    return {"macro": macro_nums, "gpr": gpr_nums, "options": other_nums}

def _extract_fact_section(draft: str) -> str:
    """🌟 架构师增强：极度强化的策略区截断，防止误杀参数"""
    if not draft: return ""
    stop_re = re.compile(
        r"(?im)^(?:#+)?\s*(?:\d+\.?\s*)?(?:Trade Idea|Options Strategy|Recommendation|Positioning|Execution|Trade Execution)\b"
    )
    m = stop_re.search(draft)
    return draft[: m.start()] if m else draft

def _coverage_rule_applies(
    key: str,
    *,
    query_text: str,
    metrics: List[str],
    source_types: List[str],
) -> bool:
    rule = _COVERAGE_KEYS.get(key, {})
    query_lower = (query_text or "").lower()
    metric_blob = " | ".join(str(m).lower() for m in metrics or [])
    source_set = {str(s).strip().lower() for s in source_types or []}

    if any(term in query_lower for term in rule.get("query_terms", ())):
        return True
    if any(term in metric_blob for term in rule.get("metric_terms", ())):
        return True
    if source_set & set(rule.get("source_terms", ())):
        return True
    return False


def _check_key_metric_coverage(
    draft: str,
    silver_context: Dict[str, Any],
    *,
    query_text: str = "",
    metrics: Optional[List[str]] = None,
    source_types: Optional[List[str]] = None,
) -> List[AgentFeedback]:
    if not _COVERAGE_CHECK_ENABLED: return []
    feedbacks: List[AgentFeedback] = []
    values = (silver_context or {}).get("values") or {}
    metrics = metrics or []
    source_types = source_types or []

    for key, rule in _COVERAGE_KEYS.items():
        if not _coverage_rule_applies(
            key,
            query_text=query_text,
            metrics=metrics,
            source_types=source_types,
        ):
            continue
        label = str(rule.get("label") or key)
        sv = values.get(key)
        if sv is None: continue
        try: sv_float = float(sv)
        except (ValueError, TypeError): continue

        draft_nums = [
            float(m.group(1).replace(",", ""))
            for m in _NUMBER_RE.finditer(draft)
            if m.group(1).replace(",", "").replace(".", "").lstrip("-").isdigit() or "." in m.group(1)
        ]
        if not any(_numeric_equiv(dn, sv_float) for dn in draft_nums):
            feedbacks.append(AgentFeedback(
                sender="Checker", error_type="Minor",
                comment=f"[rule:COVERAGE_MISS | metric={key}] Metric '{label}' ({sv_float}) missing from draft."
            ))
    return feedbacks


def _must_keep_keys_from_metadata(
    metadata_dict: Dict[str, Any],
    query_text: str,
    truth_ctx: Dict[str, Any],
) -> List[str]:
    values = (truth_ctx or {}).get("values") or {}
    metrics = list(metadata_dict.get("metrics") or [])
    source_types = list(metadata_dict.get("source_types") or [])
    required: List[str] = []
    for key in _COVERAGE_KEYS:
        if key not in values:
            continue
        if _coverage_rule_applies(
            key,
            query_text=query_text,
            metrics=metrics,
            source_types=source_types,
        ):
            required.append(key)
    return required


def _checker_typed_edits(
    *,
    state: Dict[str, Any],
    draft: str,
    truth_ctx: Dict[str, Any],
    metadata_dict: Dict[str, Any],
    feedbacks: List[AgentFeedback],
) -> List[FinalizerEdit]:
    values = (truth_ctx or {}).get("values") or {}
    anchors = [str(a) for a in ((truth_ctx or {}).get("lineage_anchors") or [])]
    query_text = str(state.get("original_query", "") or "")
    retrieval_outcome = state.get("retrieval_outcome") or {}
    missing_strict_sources = list(retrieval_outcome.get("missing_strict_sources") or [])
    required_keys = _must_keep_keys_from_metadata(metadata_dict, query_text, truth_ctx)
    typed: List[FinalizerEdit] = []

    if required_keys:
        missing_keys: List[str] = []
        draft_nums = [
            float(m.group(1).replace(",", ""))
            for m in _NUMBER_RE.finditer(draft or "")
            if m.group(1)
        ]
        for key in required_keys:
            sv = values.get(key)
            try:
                sv_float = float(sv)
            except (TypeError, ValueError):
                continue
            if not any(_numeric_equiv(dn, sv_float) for dn in draft_nums):
                missing_keys.append(key)
        if missing_keys:
            typed.append({
                "source": "Checker",
                "edit_type": "keep_numbers",
                "target_section": "direct_conclusion",
                "instruction": "Preserve the strict Silver metrics required by the query in the direct conclusion.",
                "must_keep_keys": missing_keys,
                "must_keep_anchor_ids": anchors,
            })

    if any("coverage_miss" in str(fb.comment).lower() for fb in feedbacks) or missing_strict_sources:
        source_clause = ""
        if missing_strict_sources:
            source_clause = f" Missing strict sources: {', '.join(missing_strict_sources)}."
        typed.append({
            "source": "Checker",
            "edit_type": "clarify_risk",
            "target_section": "risks",
            "instruction": (
                "State that some requested source or metric coverage is incomplete and the read should be treated cautiously."
                + source_clause
            ),
            "must_keep_keys": [],
            "must_keep_anchor_ids": anchors,
        })

    time_range = state.get("time_range") or {}
    if bool(state.get("is_fallback")) or bool(time_range.get("is_default_window_applied")):
        typed.append({
            "source": "Checker",
            "edit_type": "tighten_horizon",
            "target_section": "direct_conclusion",
            "instruction": "Align time-sensitive wording to the actual retrieved window and avoid overstating recency because the retrieval window was extended or defaulted.",
            "must_keep_keys": [],
            "must_keep_anchor_ids": anchors,
        })

    if re.search(r"\b(strike|strikes|dte|expiration|expiry)\b", draft or "", re.IGNORECASE):
        source_types = {str(s).lower() for s in (metadata_dict.get("source_types") or [])}
        if "options" not in source_types:
            typed.append({
                "source": "Checker",
                "edit_type": "remove_unsupported_structure",
                "target_section": "asset_read",
                "instruction": "Remove unsupported strike, expiration, or options-structure specificity that is not justified by the requested source scope.",
                "must_keep_keys": [],
                "must_keep_anchor_ids": anchors,
            })

    deduped: List[FinalizerEdit] = []
    seen: set[tuple[str, str]] = set()
    for item in typed:
        sig = (str(item.get("edit_type")), str(item.get("target_section")))
        if sig in seen:
            continue
        seen.add(sig)
        deduped.append(item)
    return deduped

def _deterministic_audit(draft: str, silver_context: Dict[str, Any], gold_context: List[Any], frozen_context: Optional[Dict[str, Any]] = None) -> List[AgentFeedback]:
    feedbacks: List[AgentFeedback] = []
    truth_ctx = frozen_context if frozen_context is not None else silver_context
    silver_truth_numbers = _extract_silver_numbers(truth_ctx)
    gold_truth_numbers = _extract_gold_numbers(gold_context)
    source_pools = _build_source_pools(truth_ctx)
    silver_anchor_sets = _build_silver_anchor_sets(truth_ctx)
    silver_registry = dict(silver_anchor_sets.get("registry") or {})
    preferred_silver_anchors: Set[str] = set(silver_anchor_sets["preferred_anchors"])
    legacy_alias_to_preferred: Dict[str, str] = dict(silver_anchor_sets["legacy_alias_to_preferred"])
    audit_lineage_anchors: Set[str] = set(silver_anchor_sets["audit_lineage_anchors"])
    
    gold_registry = build_gold_citation_registry(gold_context)
    known_gold_refs: Set[str] = set(gold_registry.get("known_refs") or set())

    # 🌟 架构师增强：容忍 LLM 对 Citation Anchor 的截断和缩写
    for m in _CITATION_RE.finditer(draft):
        kind, anchor = m.group("kind").capitalize(), m.group("anchor").strip()
        if _is_sentinel_anchor(anchor):
            continue
        if _is_placeholder_anchor(anchor):
            feedbacks.append(AgentFeedback(
                sender="Checker", error_type="Fatal",
                comment=(
                    f"[rule:PLACEHOLDER_CITATION | anchor={anchor}] "
                    "Placeholder citation is not allowed. Use canonical Silver preferred anchors "
                    "or Gold bronze_ref values only."
                ),
                missing_lineage_id=[anchor],
            ))
            continue

        if kind == "Silver":
            if anchor.lower() in _FORBIDDEN_INTERNAL_SILVER_ANCHORS:
                feedbacks.append(AgentFeedback(
                    sender="Checker", error_type="Fatal",
                    comment=(
                        f"[rule:FORBIDDEN_INTERNAL_ANCHOR | anchor={anchor}] "
                        "Internal control blocks cannot be used as Silver citations. "
                        "Use the canonical preferred Silver anchor for the cited metric instead."
                    ),
                    missing_lineage_id=[anchor],
                ))
                continue
            parsed_refs = parse_silver_inline_payload(anchor)
            if not parsed_refs:
                feedbacks.append(AgentFeedback(
                    sender="Checker", error_type="Fatal",
                    comment=f"[rule:UNKNOWN_SILVER_ANCHOR | anchor={anchor}] Anchor '{anchor}' not found.",
                    missing_lineage_id=[anchor],
                ))
                continue
            resolved_refs = [resolve_silver_anchor_ref(ref, silver_registry) for ref in parsed_refs]
            unresolved = [ref for ref in resolved_refs if ref.get("resolution_status") == "unresolved"]
            audit_refs = [ref for ref in resolved_refs if ref.get("resolution_status") == "audit_lineage"]
            legacy_refs = [ref for ref in resolved_refs if ref.get("resolution_status") == "legacy_alias"]
            repeated_label = _payload_has_repeated_silver_label(anchor)

            if unresolved:
                if _ALLOW_GOLD_AS_SILVER_ALIAS:
                    unresolved_anchors = [str(ref.get("anchor") or "").strip() for ref in unresolved if str(ref.get("anchor") or "").strip()]
                    if unresolved_anchors and all(
                        item.upper().startswith("GOLD_") or item in known_gold_refs for item in unresolved_anchors
                    ):
                        feedbacks.append(AgentFeedback(
                            sender="Checker",
                            error_type="Minor",
                            comment=(
                                f"[rule:SILVER_GOLD_ALIAS | anchor={', '.join(unresolved_anchors)}] "
                                "Anchor appears to be Gold evidence mislabeled as Silver. "
                                "Treating as non-fatal and continuing."
                            ),
                            missing_lineage_id=unresolved_anchors,
                        ))
                        continue
                unresolved_anchors = [str(ref.get("anchor") or "").strip() for ref in unresolved if str(ref.get("anchor") or "").strip()]
                feedbacks.append(AgentFeedback(
                    sender="Checker", error_type="Fatal",
                    comment=(
                        f"[rule:UNKNOWN_SILVER_ANCHOR | anchor={', '.join(unresolved_anchors)}] "
                        f"Anchor(s) '{', '.join(unresolved_anchors)}' not found."
                    ),
                    missing_lineage_id=unresolved_anchors,
                ))
                continue
            if audit_refs:
                bad_anchors = [str(ref.get("anchor") or "").strip() for ref in audit_refs if str(ref.get("anchor") or "").strip()]
                feedbacks.append(AgentFeedback(
                    sender="Checker",
                    error_type="Fatal",
                    comment=(
                        f"[rule:AUDIT_LINEAGE_USED_AS_INLINE_CITATION | anchor={', '.join(bad_anchors)}] "
                        "Audit lineage anchors are provenance-only. Use the metric's preferred Silver anchor instead."
                    ),
                    missing_lineage_id=bad_anchors,
                ))
                continue
            if repeated_label:
                feedbacks.append(AgentFeedback(
                    sender="Checker",
                    error_type="Minor",
                    comment=(
                        f"[rule:MALFORMED_MULTI_ANCHOR_SILVER_CITATION | anchor={anchor}] "
                        "Silver multi-anchor citation was normalized successfully, but the inline form should use a single "
                        "Silver label followed by comma-separated preferred anchors."
                    ),
                ))
            for ref in legacy_refs:
                alias = str(ref.get("anchor") or "").strip()
                preferred_anchor = str(ref.get("preferred_anchor") or "").strip()
                feedbacks.append(AgentFeedback(
                    sender="Checker",
                    error_type="Minor",
                    comment=(
                        f"[rule:LEGACY_SILVER_ALIAS | anchor={alias}] "
                        f"Legacy Silver alias used; canonical inline citation is "
                        f"[Silver: {preferred_anchor}]."
                    ),
                ))
            if not repeated_label and not legacy_refs:
                continue
        elif kind == "Gold":
            resolved_gold_ref = resolve_gold_anchor_ref(anchor, gold_registry)
            if resolved_gold_ref.get("resolution_status") == "alias":
                feedbacks.append(AgentFeedback(
                    sender="Checker",
                    error_type="Minor",
                    comment=(
                        f"[rule:GOLD_ALIAS_USED | anchor={anchor}] "
                        f"Gold citation used a source/date alias; canonical inline citation is "
                        f"[Gold: {resolved_gold_ref.get('resolved_ref')}]."
                    ),
                ))
            elif resolved_gold_ref.get("resolution_status") == "ambiguous":
                feedbacks.append(AgentFeedback(
                    sender="Checker", error_type="Fatal",
                    comment=(
                        f"[rule:AMBIGUOUS_GOLD_REF | anchor={anchor}] "
                        "Gold source/date alias maps to multiple retrieved chunks; use the exact bronze_ref."
                    ),
                    missing_lineage_id=[anchor],
                ))
            elif resolved_gold_ref.get("resolution_status") != "canonical":
                feedbacks.append(AgentFeedback(
                    sender="Checker", error_type="Fatal",
                    comment=f"[rule:UNKNOWN_GOLD_REF | anchor={anchor}] Ref '{anchor}' not found.",
                    missing_lineage_id=[anchor],
                ))

    # Number audit
    fact_section = _extract_fact_section(draft)
    # Never audit numbers that live inside citation anchors. Otherwise dates in
    # anchors like [Silver: MACRO_GSPC_2026-04-24] can be misread as draft facts.
    citation_spans = [m.span() for m in _CITATION_RE.finditer(fact_section)]

    def _inside_citation(pos: int) -> bool:
        for s, e in citation_spans:
            if s <= pos < e:
                return True
        return False

    for m in _NUMBER_RE.finditer(fact_section):
        if _inside_citation(m.start()):
            continue
        raw, is_pct = m.group(1), m.group(2)
        try: val = float(raw.replace(",", ""))
        except ValueError: continue
        
        # 🌟 架构师增强：免疫年份 (2020-2030) 和 常见自然数
        if abs(val) in _COMMON_NUMERIC_WHITELIST: continue
        if val >= 2020 and val <= 2030 and "." not in raw: continue
        if _is_calendar_date_number(m, fact_section): continue

        is_claim = bool(is_pct) or "." in raw or abs(val) >= 10
        if not is_claim: continue
        line = _line_for_pos(fact_section, m.start())
        line_metric_keys = _resolved_silver_metric_keys_for_line(line, silver_registry)
        line_has_silver = bool(re.search(r"\[Silver\s*:", line, re.IGNORECASE))
        line_has_gold = bool(re.search(r"\[Gold\s*:", line, re.IGNORECASE))
        if _STRICT_NUMERIC_REQUIRES_SILVER_CITATION and (not line_has_silver):
            # Gold-backed qualitative lines often contain counts/ratios not expected
            # to match Silver numeric tables exactly.
            if line_has_gold:
                continue
            # Keep existing behavior for uncited numbers: handled by other checks.
            continue
        if _is_strategy_parameter(m, fact_section): continue

        metric_specific_match = False
        for metric_key in line_metric_keys:
            contract_entry = dict((silver_anchor_sets.get("contract") or {}).get(metric_key) or {})
            comparison_mode = str(contract_entry.get("comparison_mode") or "raw_decimal")
            for truth_val in _metric_truth_numbers(metric_key, truth_ctx):
                if _metric_numeric_equiv(val, truth_val, comparison_mode=comparison_mode):
                    metric_specific_match = True
                    break
            if metric_specific_match:
                break
        if metric_specific_match:
            continue

        if any(_numeric_equiv_abs(val, t) for t in silver_truth_numbers): continue
        if any(_numeric_equiv_abs(val, t) for t in gold_truth_numbers): continue
        
        all_sub = source_pools["macro"] + source_pools["gpr"] + source_pools["options"]
        if any(_numeric_equiv_abs(val, t) for t in all_sub): continue

        feedbacks.append(AgentFeedback(
            sender="Checker", error_type="Fatal",
            comment=f"[rule:NUMERIC_MISMATCH | draft={raw}{is_pct}] Number not in Silver data."
        ))

    return feedbacks


# ==========================================
# CheckerAgent class
# ==========================================
class CheckerAgent:
    def __init__(self, silver_tool: Optional[Any] = None):
        self.silver_tool = silver_tool
        self.enable_rescue = _TOOL_RECOVERY_ENABLED and silver_tool is not None
        provider = os.getenv("CHECKER_PROVIDER", "").strip().lower()
        if provider not in {"openai", "ollama"}:
            provider = "openai"
        self.provider = provider

        default_openai_model = os.getenv("CHECKER_OPENAI_FALLBACK_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        default_ollama_model = os.getenv("OLLAMA_CHECKER_MODEL", "llama3:latest").strip() or "llama3:latest"
        self.model_name = os.getenv("CHECKER_MODEL", "").strip() or (
            default_openai_model if self.provider == "openai" else default_ollama_model
        )
        self.llm = self._build_llm(self.provider, self.model_name)

        self.fallback_enabled = os.getenv(
            "CHECKER_ENABLE_MODEL_FALLBACK",
            os.getenv("CHECKER_OPENAI_FALLBACK_ENABLED", "1"),
        ) == "1"
        self.fallback_provider = "ollama" if self.provider == "openai" else "openai"
        self.fallback_model_name = (
            default_ollama_model
            if self.fallback_provider == "ollama"
            else default_openai_model
        )
        self.fallback_llm = None
        if self.fallback_enabled:
            self.fallback_llm = self._build_llm(self.fallback_provider, self.fallback_model_name)
        self.prompt = get_checker_prompt()

    def _build_llm(self, provider: str, model_name: str):
        if provider == "ollama":
            return ChatOllama(
                model=model_name,
                temperature=0.0,
                format="json",
            ).with_structured_output(CheckerResult)

        openai_kwargs: Dict[str, Any] = {
            "model": model_name,
            "temperature": 0.0,
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "timeout": float(os.getenv("CHECKER_OPENAI_TIMEOUT_SECONDS", "45")),
        }
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        if openai_base_url:
            openai_kwargs["base_url"] = openai_base_url
        return ChatOpenAI(**openai_kwargs).with_structured_output(CheckerResult)

    async def audit(self, state: Dict[str, Any]) -> Dict[str, Any]:
        revision_n = state.get("revision_count", 0)
        if revision_n >= _MAX_REVISIONS:
            logger.warning("CheckerAgent: max revisions reached — skipping audit.")
            return {"critic_feedback": [], "checker_verdict": "pass"}

        draft, raw_silver_ctx, gold_ctx = state.get("draft_report", ""), state.get("silver_context", {}), state.get("gold_context", [])
        raw_frozen_ctx = state.get("silver_context_frozen")
        silver_ctx = effective_silver_context(raw_silver_ctx if isinstance(raw_silver_ctx, dict) else {})
        frozen_ctx = effective_silver_context(raw_frozen_ctx) if isinstance(raw_frozen_ctx, dict) else raw_frozen_ctx
        truth_ctx_for_anchor_contract = frozen_ctx if frozen_ctx is not None else silver_ctx
        silver_anchor_sets = _build_silver_anchor_sets(truth_ctx_for_anchor_contract)
        macro_text = state.get("macro_context", "")
        metadata = state.get("metadata")
        metadata_dict = {}
        if metadata is not None:
            if isinstance(metadata, dict):
                metadata_dict = metadata
            else:
                for method_name in ("model_dump", "dict"):
                    method = getattr(metadata, method_name, None)
                    if callable(method):
                        try:
                            dumped = method()
                            if isinstance(dumped, dict):
                                metadata_dict = dumped
                                break
                        except Exception:
                            pass

        feedbacks = _deterministic_audit(draft, silver_ctx, gold_ctx, frozen_ctx)
        
        truth_ctx_for_coverage = frozen_ctx if frozen_ctx is not None else silver_ctx
        scope_contract = state.get("scope_contract") if isinstance(state.get("scope_contract"), dict) else {}
        retrieval_outcome = state.get("retrieval_outcome") if isinstance(state.get("retrieval_outcome"), dict) else {}
        retrieval_slot_eval = semantic_slot_evidence_eval(
            scope_contract.get("slot_evidence_contracts") or {},
            draft,
            retrieval_outcome,
            dict((truth_ctx_for_coverage or {}).get("values") or {}),
            gold_ctx or [],
        )
        feedbacks.extend(
            _check_key_metric_coverage(
                draft,
                truth_ctx_for_coverage,
                query_text=str(state.get("original_query", "") or ""),
                metrics=list(metadata_dict.get("metrics") or []),
                source_types=list(metadata_dict.get("source_types") or []),
            )
        )

        refreshed_silver = None
        if self.enable_rescue and any(fb.error_type.lower() == "fatal" and fb.missing_lineage_id for fb in feedbacks):
            refreshed_silver, feedbacks = await self._rescue_missing_anchors(feedbacks, state)
            if refreshed_silver: silver_ctx = refreshed_silver

        try:
            from Scripts.agents.analyst import _format_silver, _format_gold
            prompt_payload = {
                "silver_block": _format_silver(silver_ctx),
                "gold_block": _format_gold(gold_ctx),
                "draft": draft,
                "macro_context": macro_text[:2000] if macro_text else "(not available)",
                "retrieval_outcome_block": json.dumps(retrieval_outcome, ensure_ascii=False, default=str)[:3000] if retrieval_outcome else "(not available)",
            }
            result: CheckerResult = await (self.prompt | self.llm).ainvoke(prompt_payload)
            if not result.is_passed:
                for v in result.violations:
                    feedback = _checker_feedback_from_llm_violation(
                        v,
                        anchor_sets=silver_anchor_sets,
                        retrieval_slot_eval=retrieval_slot_eval,
                        metadata_dict=metadata_dict,
                        retrieval_outcome=retrieval_outcome,
                        revision_n=revision_n,
                    )
                    if feedback is not None:
                        feedbacks.append(feedback)
        except Exception as e:
            logger.warning(
                "CheckerAgent: primary %s LLM audit failed (%s)",
                self.provider,
                e,
            )
            if self.fallback_enabled and self.fallback_llm is not None:
                try:
                    from Scripts.agents.analyst import _format_silver, _format_gold
                    fb_result: CheckerResult = await (self.prompt | self.fallback_llm).ainvoke({
                        "silver_block": _format_silver(silver_ctx),
                        "gold_block": _format_gold(gold_ctx),
                        "draft": draft,
                        "macro_context": macro_text[:2000] if macro_text else "(not available)",
                        "retrieval_outcome_block": json.dumps(retrieval_outcome, ensure_ascii=False, default=str)[:3000] if retrieval_outcome else "(not available)",
                    })
                    if not fb_result.is_passed:
                        for v in fb_result.violations:
                            feedback = _checker_feedback_from_llm_violation(
                                v,
                                anchor_sets=silver_anchor_sets,
                                retrieval_slot_eval=retrieval_slot_eval,
                                metadata_dict=metadata_dict,
                                retrieval_outcome=retrieval_outcome,
                                revision_n=revision_n,
                                fallback_suffix=":FALLBACK",
                            )
                            if feedback is not None:
                                feedbacks.append(feedback)
                except Exception as fb_err:
                    logger.warning(
                        "CheckerAgent: %s fallback LLM audit failed: %s",
                        self.fallback_provider,
                        fb_err,
                    )

        checker_edits = _checker_typed_edits(
            state=state,
            draft=draft,
            truth_ctx=truth_ctx_for_coverage,
            metadata_dict=metadata_dict,
            feedbacks=feedbacks,
        )
        for fb in feedbacks:
            fb.revision_index = fb.revision_index or revision_n
        analyst_feedbacks = [
            fb for fb in feedbacks
            if str(fb.error_type or "").lower() == "fatal"
        ]
        verdict = "pass"
        if analyst_feedbacks:
            verdict = "fatal"
        elif checker_edits:
            verdict = "minor"
        payload = {
            "critic_feedback": analyst_feedbacks,
            "checker_verdict": verdict,
            "checker_edit_suggestions": checker_edits,
        }
        if refreshed_silver: payload["silver_context"] = refreshed_silver
        return payload

    async def _rescue_missing_anchors(self, feedbacks: List[AgentFeedback], state: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], List[AgentFeedback]]:
        metadata = state.get("metadata")
        if not metadata: return None, feedbacks

        live_predicates = None
        if serialised_preds := (state.get("time_range") or {}).get("source_predicates"):
            try:
                from Scripts.retrieval.time_adapter import predicates_from_serialised
                live_predicates = predicates_from_serialised(serialised_preds)
            except: pass

        try: refreshed = await self.silver_tool.query_parquet_by_metadata(metadata, time_predicates=live_predicates)
        except Exception: return None, feedbacks

        old_silver = state.get("silver_context", {}) or {}
        merged_values = {**(old_silver.get("values", {})), **(refreshed.get("values", {}))}
        merged_anchors = sorted(
            set(str(a) for a in old_silver.get("lineage_anchors", []))
            | set(str(a) for a in refreshed.get("lineage_anchors", []))
        )
        merged_citation_contract = {
            **dict(old_silver.get("citation_contract", {}) or {}),
            **dict(refreshed.get("citation_contract", {}) or {}),
        }
        merged_citation_map = {
            **dict(old_silver.get("citation_anchor_map", {}) or {}),
            **dict(refreshed.get("citation_anchor_map", {}) or {}),
            **{
                str(metric_key): str((entry or {}).get("preferred_anchor"))
                for metric_key, entry in merged_citation_contract.items()
                if (entry or {}).get("preferred_anchor")
            },
        }
        merged_silver = {
            **{k: v for k, v in old_silver.items() if k not in ("values", "lineage_anchors", "citation_anchor_map", "citation_contract")},
            **{k: v for k, v in refreshed.items() if k not in ("values", "lineage_anchors", "citation_anchor_map", "citation_contract")},
            "values": merged_values,
            "lineage_anchors": merged_anchors,
            "citation_contract": merged_citation_contract,
            "citation_anchor_map": merged_citation_map,
        }

        updated = [
            AgentFeedback(sender="Checker", error_type="Minor", comment=f"(Rescued) {fb.missing_lineage_id}", missing_lineage_id=fb.missing_lineage_id, revision_index=fb.revision_index)
            if fb.error_type.lower() == "fatal" and fb.missing_lineage_id and any(m in merged_anchors for m in fb.missing_lineage_id)
            else fb for fb in feedbacks
        ]
        return merged_silver, updated
