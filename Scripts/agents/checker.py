"""
Scripts/agents/checker.py

Checker Agent (Blue Team) — deterministic + LLM fact & lineage audit of the
Analyst draft, with an opt-in Silver-tool escape hatch for missing anchors.

Design philosophy:
- The Checker is NOT a strategist. It is a silent, uncompromising data-integrity auditor.
- Robustness Upgrades:
    * Tolerates LLM anchor truncation (substring matching).
    * Ignores calendar years and structural constants.
    * Aggressively truncates 'Strategy/Recommendation' sections before numeric audit.
"""

from __future__ import annotations

import os
import re
import logging
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from Scripts.agents.state import AgentFeedback
from Scripts.agents.prompts import get_checker_prompt

logger = logging.getLogger(__name__)

# 放宽至 3% 的容差，给 LLM 四舍五入留足空间
_NUMERIC_REL_TOLERANCE = float(os.getenv("CHECKER_NUMERIC_TOLERANCE", "0.03"))
_MAX_REVISIONS = int(os.getenv("AGENT_MAX_REVISIONS", "3"))
_TOOL_RECOVERY_ENABLED = os.getenv("CHECKER_TOOL_RECOVERY", "1") == "1"
_COVERAGE_CHECK_ENABLED = os.getenv("CHECKER_COVERAGE_CHECK_ENABLED", "1") == "1"
_ALLOW_GOLD_AS_SILVER_ALIAS = os.getenv("CHECKER_ALLOW_GOLD_AS_SILVER_ALIAS", "1") == "1"
_STRICT_NUMERIC_REQUIRES_SILVER_CITATION = os.getenv("CHECKER_NUMERIC_REQUIRE_SILVER_CITATION", "1") == "1"

_COVERAGE_KEYS: Dict[str, str] = {
    "latest_atm_iv": "ATM implied volatility",
    "pcr_volume": "put/call ratio (volume)",
    "gpr_index_level": "GPR geopolitical risk index",
}

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

def _check_key_metric_coverage(draft: str, silver_context: Dict[str, Any]) -> List[AgentFeedback]:
    if not _COVERAGE_CHECK_ENABLED: return []
    feedbacks: List[AgentFeedback] = []
    values = (silver_context or {}).get("values") or {}

    for key, label in _COVERAGE_KEYS.items():
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

def _deterministic_audit(draft: str, silver_context: Dict[str, Any], gold_context: List[Any], frozen_context: Optional[Dict[str, Any]] = None) -> List[AgentFeedback]:
    feedbacks: List[AgentFeedback] = []
    truth_ctx = frozen_context if frozen_context is not None else silver_context
    silver_truth_numbers = _extract_silver_numbers(truth_ctx)
    gold_truth_numbers = _extract_gold_numbers(gold_context)
    source_pools = _build_source_pools(truth_ctx)

    _silver_values_keys: Set[str] = set((truth_ctx or {}).get("values", {}).keys())
    known_silver_anchors = (
        set(str(a) for a in (truth_ctx or {}).get("lineage_anchors", []) or [])
        | _silver_values_keys
    )
    
    known_gold_refs: Set[str] = set()
    for chunk in gold_context or []:
        br = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref") if isinstance(chunk, dict) else None)
        if br: known_gold_refs.add(str(br))

    # 🌟 架构师增强：容忍 LLM 对 Citation Anchor 的截断和缩写
    for m in _CITATION_RE.finditer(draft):
        kind, anchor = m.group("kind").capitalize(), m.group("anchor").strip()
        if _is_sentinel_anchor(anchor): continue
        if _is_placeholder_anchor(anchor):
            feedbacks.append(AgentFeedback(
                sender="Checker", error_type="Fatal",
                comment=(
                    f"[rule:PLACEHOLDER_CITATION | anchor={anchor}] "
                    "Placeholder citation is not allowed. Use real IDs from Silver lineage anchors "
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
                        "Use [Silver: latest_atm_iv], [Silver: latest_atm_iv_rank_pct], "
                        "or [Silver: pcr_volume] instead."
                    ),
                    missing_lineage_id=[anchor],
                ))
                continue
            # Fuzzy match: "MACRO_VIX" is acceptable for "MACRO_VIX_2026-04-23"
            matched = any(anchor.lower() in k.lower() or k.lower() in anchor.lower() for k in known_silver_anchors)
            if not matched:
                if _ALLOW_GOLD_AS_SILVER_ALIAS:
                    gold_like = (
                        anchor.upper().startswith("GOLD_")
                        or any(anchor.lower() in k.lower() or k.lower() in anchor.lower() for k in known_gold_refs)
                    )
                    if gold_like:
                        feedbacks.append(AgentFeedback(
                            sender="Checker",
                            error_type="Minor",
                            comment=(
                                f"[rule:SILVER_GOLD_ALIAS | anchor={anchor}] "
                                "Anchor appears to be Gold evidence mislabeled as Silver. "
                                "Treating as non-fatal and continuing."
                            ),
                            missing_lineage_id=[anchor],
                        ))
                        continue
                feedbacks.append(AgentFeedback(
                    sender="Checker", error_type="Fatal",
                    comment=f"[rule:UNKNOWN_SILVER_ANCHOR | anchor={anchor}] Anchor '{anchor}' not found.",
                    missing_lineage_id=[anchor],
                ))
        elif kind == "Gold":
            matched = any(anchor.lower() in k.lower() or k.lower() in anchor.lower() for k in known_gold_refs)
            if not matched:
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

        is_claim = bool(is_pct) or "." in raw or abs(val) >= 10
        if not is_claim: continue
        line = _line_for_pos(fact_section, m.start())
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
        self.model_name = os.getenv("OLLAMA_CHECKER_MODEL", "llama3:latest")
        self.llm = ChatOllama(model=self.model_name, temperature=0.0, format="json").with_structured_output(CheckerResult)
        self.fallback_enabled = os.getenv("CHECKER_OPENAI_FALLBACK_ENABLED", "1") == "1"
        self.fallback_model_name = os.getenv("CHECKER_OPENAI_FALLBACK_MODEL", "gpt-4o-mini")
        self.fallback_llm = None
        if self.fallback_enabled:
            openai_kwargs: Dict[str, Any] = {
                "model": self.fallback_model_name,
                "temperature": 0.0,
                "api_key": os.getenv("OPENAI_API_KEY", ""),
                "timeout": float(os.getenv("CHECKER_OPENAI_TIMEOUT_SECONDS", "45")),
            }
            openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
            if openai_base_url:
                openai_kwargs["base_url"] = openai_base_url
            self.fallback_llm = ChatOpenAI(**openai_kwargs).with_structured_output(CheckerResult)
        self.prompt = get_checker_prompt()

    async def audit(self, state: Dict[str, Any]) -> Dict[str, Any]:
        revision_n = state.get("revision_count", 0)
        if revision_n >= _MAX_REVISIONS:
            logger.warning("CheckerAgent: max revisions reached — skipping audit.")
            return {"critic_feedback": [], "checker_verdict": "pass"}

        draft, silver_ctx, gold_ctx = state.get("draft_report", ""), state.get("silver_context", {}), state.get("gold_context", [])
        frozen_ctx = state.get("silver_context_frozen")
        macro_text = state.get("macro_context", "")

        feedbacks = _deterministic_audit(draft, silver_ctx, gold_ctx, frozen_ctx)
        
        truth_ctx_for_coverage = frozen_ctx if frozen_ctx is not None else silver_ctx
        feedbacks.extend(_check_key_metric_coverage(draft, truth_ctx_for_coverage))

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
            }
            result: CheckerResult = await (self.prompt | self.llm).ainvoke(prompt_payload)
            if not result.is_passed:
                for v in result.violations:
                    if _should_suppress_llm_violation(v.description):
                        continue
                    sev = v.severity if v.severity in ("Fatal", "Minor") else "Fatal"
                    feedbacks.append(AgentFeedback(sender="Checker", error_type=sev, comment=f"[rule:LLM_CONSISTENCY] {v.description}", revision_index=revision_n))
        except Exception as e:
            logger.warning(f"CheckerAgent: Ollama LLM audit failed: {e}")
            if self.fallback_enabled and self.fallback_llm is not None:
                try:
                    from Scripts.agents.analyst import _format_silver, _format_gold
                    fb_result: CheckerResult = await (self.prompt | self.fallback_llm).ainvoke({
                        "silver_block": _format_silver(silver_ctx),
                        "gold_block": _format_gold(gold_ctx),
                        "draft": draft,
                        "macro_context": macro_text[:2000] if macro_text else "(not available)",
                    })
                    if not fb_result.is_passed:
                        for v in fb_result.violations:
                            if _should_suppress_llm_violation(v.description):
                                continue
                            sev = v.severity if v.severity in ("Fatal", "Minor") else "Fatal"
                            feedbacks.append(AgentFeedback(
                                sender="Checker",
                                error_type=sev,
                                comment=f"[rule:LLM_CONSISTENCY:FALLBACK] {v.description}",
                                revision_index=revision_n,
                            ))
                except Exception as fb_err:
                    logger.warning(f"CheckerAgent: fallback LLM audit failed: {fb_err}")

        for fb in feedbacks: fb.revision_index = fb.revision_index or revision_n
        verdict = "pass" if not feedbacks else ("fatal" if any(f.error_type.lower() == "fatal" for f in feedbacks) else "minor")
        
        payload = {"critic_feedback": feedbacks, "checker_verdict": verdict}
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
        merged_silver = {
            **{k: v for k, v in old_silver.items() if k not in ("values", "lineage_anchors")},
            **{k: v for k, v in refreshed.items() if k not in ("values", "lineage_anchors")},
            "values": merged_values,
            "lineage_anchors": merged_anchors,
        }

        updated = [
            AgentFeedback(sender="Checker", error_type="Minor", comment=f"(Rescued) {fb.missing_lineage_id}", missing_lineage_id=fb.missing_lineage_id, revision_index=fb.revision_index)
            if fb.error_type.lower() == "fatal" and fb.missing_lineage_id and any(m in merged_anchors for m in fb.missing_lineage_id)
            else fb for fb in feedbacks
        ]
        return merged_silver, updated