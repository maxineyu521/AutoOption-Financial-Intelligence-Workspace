"""
Contract-first evaluation harness for the financial RAG stack.

This script replaces the old RAGAS-centered comparison with a contract-aware
benchmark focused on three priorities:

1. Do not say wrong things.
2. If evidence is missing, say so explicitly instead of hallucinating.
3. Compare whether the four-node pipeline reduces financial errors relative to
   the single-node baseline.

Default behavior compares:
    - baseline_results_full.jsonl
    - production_results_full.jsonl
against the structured truth catalogue in:
    Scripts/tests/router_e2e_ground_truth_queries.json
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:  # type: ignore[no-redef]
        return None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from Scripts.core.financial_ontology import (
    INSIDER_FLOW_QUERY_SLOTS,
    SEC_ACTION_TAXONOMY,
    describe_sec_action_direction,
    missing_slots_for_query_family,
    query_slots_for_family,
)
from Scripts.core.evidence_contracts import (
    build_slot_evidence_contracts,
    canonical_query_family,
    evaluate_truth_slot_semantics,
)

DEFAULT_TRUTH_FILE = PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_ground_truth_queries.json"
RAGAS_LOG_ROOT = PROJECT_ROOT / "logs" / "RAGAS"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "agentic_eval"

logger = logging.getLogger("agentic_eval")

QUERY_FAMILY_SLOTS: Dict[str, Dict[str, str]] = {
    "options_microstructure": {
        "pcr_signal": "Put/call ratio signal",
        "atm_iv_signal": "ATM implied volatility signal",
        "liquidity_signal": "Options liquidity / 30-DTE liquidity signal",
    },
    "single_name_options": {
        "iv_or_skew_signal": "Single-name IV / skew signal",
        "liquidity_signal": "Options liquidity signal",
    },
    "cross_asset_regime": {
        "iv_vs_vix_regime": "IV versus VIX regime signal",
        "hedging_implication": "Hedging implication",
    },
    "geopolitical_commodity": {
        "gpr_regime_signal": "GPR regime signal",
        "options_regime_implication": "Options regime implication",
    },
    "insider_flow_driven": dict(INSIDER_FLOW_QUERY_SLOTS),
}

SOURCE_ALIASES: Dict[str, str] = {
    "macro_history": "macro",
    "macro": "macro",
    "options": "options",
    "sec": "sec",
    "gpr": "gpr",
    "news": "news",
}

SHORT_PREMIUM_PATTERNS = [
    r"\bbull put spread\b",
    r"\bbear call spread\b",
    r"\bshort put\b",
    r"\bshort call\b",
    r"\bshort straddle\b",
    r"\bshort strangle\b",
    r"\bcredit spread\b",
    r"\biron condor\b",
]

LONG_PREMIUM_PATTERNS = [
    r"\blong straddle\b",
    r"\blong strangle\b",
    r"\blong call\b",
    r"\blong put\b",
    r"\bdebit spread\b",
    r"\bbull call spread\b",
    r"\bbear put spread\b",
]

CONCRETE_STRUCTURE_PATTERNS = SHORT_PREMIUM_PATTERNS + LONG_PREMIUM_PATTERNS + [
    r"\bsetup:\b",
    r"\bstrike\b",
    r"\bexpiration\b",
    r"\bexpiry\b",
    r"\b30-dte\b",
]

SLOT_PATTERNS: Dict[str, Dict[str, List[str]]] = {
    "pcr_signal": [r"\bpcr\b", r"put[-/ ]call ratio", r"put/call ratio"],
    "atm_iv_signal": [r"\batm iv\b", r"implied volatility", r"\biv\b"],
    "liquidity_signal": [r"\bliquidity\b", r"\bliquid\b", r"open interest", r"\bspread\b", r"\bcontracts?\b"],
    "iv_or_skew_signal": [r"\biv skew\b", r"\bskew\b", r"\batm iv\b", r"implied volatility"],
    "iv_vs_vix_regime": [r"\bvix\b", r"\biv\b", r"\bregime\b"],
    "hedging_implication": [r"\bhedg", r"\bwatchlist\b", r"\binformational\b", r"\bprotect"],
    "gpr_regime_signal": [r"\bgpr\b", r"geopolitical risk"],
    "options_regime_implication": [r"\boption", r"\biv\b", r"\bliquidity\b", r"\bhedg"],
    "sec_insider_signal": [r"\binsider\b", r"\bform-4\b", r"\bsec\b", r"\bvesting\b", r"\bbuy", r"\bsell"],
    "options_liquidity_posture": [r"\bliquidity\b", r"open interest", r"\bvolume\b", r"\bspread\b", r"\bcontracts?\b"],
}

DISCLOSURE_PATTERNS = {
    "sec": [r"sec/form-4 evidence was not retrieved", r"insider .* cannot be assessed reliably", r"form-4 .* cannot be assessed reliably"],
    "options": [r"options .* (?:was not retrieved|cannot be assessed reliably)", r"liquidity .* cannot be assessed reliably"],
    "macro": [r"macro .* (?:was not retrieved|cannot be assessed reliably)", r"vix .* cannot be assessed reliably"],
    "gpr": [r"gpr .* (?:was not retrieved|cannot be assessed reliably)"],
}

EVAL_DASHBOARD_CSS = """
:root {
  --bg: #f7f8fb;
  --panel: #ffffff;
  --ink: #1f2937;
  --muted: #667085;
  --line: #d9e0ea;
  --blue: #2563eb;
  --green: #16a34a;
  --amber: #d97706;
  --red: #dc2626;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: Inter, Segoe UI, Arial, sans-serif; }
.page { width: min(1180px, calc(100vw - 48px)); margin: 28px auto 56px; }
.hero, .panel, .metric-card, table { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 10px 24px rgba(15, 23, 42, 0.06); }
.hero, .panel { padding: 20px; }
h1 { margin: 0 0 8px; font-size: 28px; }
h2 { margin: 24px 0 12px; font-size: 18px; }
h3 { margin: 16px 0 8px; font-size: 15px; }
.muted { color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-top: 16px; }
.metric-card { padding: 14px; }
.label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
.value { margin-top: 8px; font-size: 24px; font-weight: 700; }
.desc { margin-top: 8px; color: var(--muted); font-size: 13px; }
table { width: 100%; border-collapse: collapse; overflow: hidden; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { background: #eef3fb; color: #334155; font-size: 12px; text-transform: uppercase; }
tr:last-child td { border-bottom: none; }
.good { color: var(--green); font-weight: 600; }
.warn { color: var(--amber); font-weight: 600; }
.bad { color: var(--red); font-weight: 600; }
.list { margin: 0; padding-left: 18px; }
.pill { display: inline-block; padding: 4px 8px; border-radius: 999px; border: 1px solid var(--line); background: #f8fafc; font-size: 12px; margin-right: 6px; margin-bottom: 6px; }
"""


def _safe_lower(text: Any) -> str:
    return str(text or "").strip().lower()


def _fmt_metric(value: Any, digits: int = 2) -> str:
    try:
        if value is None:
            return "N/A"
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _latest_ragas_run() -> Path:
    dated_dirs = sorted([p for p in RAGAS_LOG_ROOT.iterdir() if p.is_dir()], key=lambda p: p.name)
    if not dated_dirs:
        raise FileNotFoundError(f"No dated run directories found under {RAGAS_LOG_ROOT}")
    run_dirs: List[Path] = []
    for dated in dated_dirs:
        run_dirs.extend([p for p in dated.iterdir() if p.is_dir()])
    if not run_dirs:
        raise FileNotFoundError(f"No run directories found under {RAGAS_LOG_ROOT}")
    return sorted(run_dirs, key=lambda p: p.name)[-1]


def _normalize_source(source: str) -> str:
    return SOURCE_ALIASES.get(str(source or "").lower(), str(source or "").lower())


def _truth_map(truth_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(case["query"]): case for case in truth_payload.get("cases", [])}


def _extract_sources_from_context_metadata(row: Dict[str, Any]) -> List[str]:
    sources: List[str] = []
    for meta in row.get("context_metadata") or []:
        src = _normalize_source(str(meta.get("source") or meta.get("source_type") or ""))
        if src:
            sources.append(src)
    return sources


def _extract_retrieved_sources(row: Dict[str, Any]) -> List[str]:
    explicit = row.get("retrieved_sources")
    if isinstance(explicit, list) and explicit:
        return [_normalize_source(str(s)) for s in explicit if str(s).strip()]
    return _extract_sources_from_context_metadata(row)


def _structured_truth(case: Dict[str, Any]) -> Dict[str, Any]:
    structured = dict(case.get("structured_truth") or {})
    meta = case.get("case_meta") or {}
    family = canonical_query_family(str(structured.get("query_family") or meta.get("intent_family") or "unknown"))
    structured["query_family"] = family
    if not structured.get("intent_slots"):
        structured["intent_slots"] = query_slots_for_family(family) or QUERY_FAMILY_SLOTS.get(str(family), {})
    structured.setdefault("expected_sources", case.get("expected_sources") or meta.get("strict_expected_sources") or [])
    structured.setdefault("expected_mode_ceiling", "directional_watchlist")
    structured.setdefault("required_disclosures", [])
    structured.setdefault("forbidden_claims", [])
    structured.setdefault("financial_logic_expectations", [])
    structured.setdefault("coverage_allows_disclosure_pass", True)
    if family == "insider_flow_driven":
        actions: List[str] = []
        for action in re.findall(r"\baction ([A-Z/]+)\b", str(case.get("ground_truth") or "")):
            if action not in actions:
                actions.append(action)
        structured.setdefault(
            "sec_action_taxonomy_expectation",
            {"definitions": dict(SEC_ACTION_TAXONOMY), "observed_actions": actions},
        )
    return structured


def _extract_metric_map_from_contexts(contexts: Sequence[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for ctx in contexts or []:
        match = re.match(r"Silver metric:\s*([A-Za-z0-9_^]+)\s*=\s*(.+)$", str(ctx).strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _extract_numbers(text: str) -> List[float]:
    values: List[float] = []
    for raw in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:,\d{3})*(?:\.\d+)?", text or ""):
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return values


def _near_match(value: float, candidates: Iterable[float]) -> bool:
    for candidate in candidates:
        tolerance = max(0.02, abs(candidate) * 0.03)
        if abs(value - candidate) <= tolerance:
            return True
    return False


def _answer_body(row: Dict[str, Any]) -> str:
    answer = str(row.get("answer_for_eval") or row.get("answer") or "")
    query = str(row.get("query") or "").strip()
    if query and answer.startswith(f"For {query},"):
        return answer[len(f"For {query},"):].strip()
    return answer.strip()


def _resolved_mode(row: Dict[str, Any]) -> str:
    card = row.get("finalizer_input_card") or {}
    if isinstance(card, dict):
        for key in ("actionability_mode", "recommendation_mode"):
            value = str(card.get(key) or "").strip()
            if value:
                return value
    for key in ("actionability_mode", "recommendation_mode"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _infer_iv_regime(row: Dict[str, Any]) -> str:
    values = _extract_metric_map_from_contexts(row.get("retrieved_contexts") or [])
    iv_rank_raw = values.get("latest_atm_iv_rank_pct")
    try:
        iv_rank = float(str(iv_rank_raw).replace("%", ""))
    except (TypeError, ValueError):
        return "UNKNOWN"
    if iv_rank >= 75:
        return "HIGH"
    if iv_rank <= 25:
        return "LOW"
    return "NORMAL"


def _contains_any(patterns: Sequence[str], text: str) -> bool:
    lowered = _safe_lower(text)
    return any(re.search(pattern, lowered) for pattern in patterns)


def _expected_sources(case: Dict[str, Any], structured_truth: Dict[str, Any]) -> List[str]:
    raw = structured_truth.get("expected_sources") or case.get("expected_sources") or []
    return [_normalize_source(str(src)) for src in raw]


def _missing_sources(case: Dict[str, Any], row: Dict[str, Any], structured_truth: Dict[str, Any]) -> List[str]:
    expected = _expected_sources(case, structured_truth)
    actual = set(_extract_retrieved_sources(row))
    return [src for src in expected if src not in actual]


def _missing_query_slots(case: Dict[str, Any], row: Dict[str, Any], structured_truth: Dict[str, Any]) -> List[str]:
    family = canonical_query_family(str(structured_truth.get("query_family") or ""))
    return missing_slots_for_query_family(family, _missing_sources(case, row, structured_truth))


def _has_source_disclosure(answer: str, source: str) -> bool:
    patterns = DISCLOSURE_PATTERNS.get(_normalize_source(source), [])
    return _contains_any(patterns, answer)


def _coverage_disclosure_pass(case: Dict[str, Any], row: Dict[str, Any], structured_truth: Dict[str, Any]) -> bool:
    missing_sources = _missing_sources(case, row, structured_truth)
    missing_slots = _missing_query_slots(case, row, structured_truth)
    if not missing_sources and not missing_slots:
        return True

    intent = _intent_coverage(structured_truth, row, missing_slots)
    status_map = dict(intent.get("slot_status") or {})
    if missing_slots:
        for slot in missing_slots:
            if status_map.get(slot) != "disclosed_unanswerable":
                return False
    answer = str(row.get("answer_for_eval") or row.get("answer") or "")
    if missing_sources and not all(_has_source_disclosure(answer, src) for src in missing_sources):
        return False
    return True


def _runtime_slot_contracts(row: Dict[str, Any], structured_truth: Dict[str, Any]) -> Dict[str, Any]:
    card = row.get("finalizer_input_card") or {}
    if isinstance(card, dict):
        contracts = card.get("slot_evidence_contracts")
        if isinstance(contracts, dict) and contracts:
            return contracts
        query_slots = card.get("query_slots")
    else:
        query_slots = None
    scope_contract = row.get("scope_contract") or {}
    if isinstance(scope_contract, dict):
        contracts = scope_contract.get("slot_evidence_contracts")
        if isinstance(contracts, dict) and contracts:
            return contracts
        if query_slots is None:
            query_slots = scope_contract.get("query_slots")
        capability_profile = scope_contract.get("data_capability_profile")
    else:
        capability_profile = None
    family = canonical_query_family(str(structured_truth.get("query_family") or ""))
    return build_slot_evidence_contracts(family, query_slots or structured_truth.get("intent_slots"), capability_profile or {})


def _semantic_slot_evidence_eval(structured_truth: Dict[str, Any], row: Dict[str, Any], missing_slots: Sequence[str]) -> Dict[str, Any]:
    answer = str(row.get("answer_for_eval") or row.get("answer") or "")
    contracts = _runtime_slot_contracts(row, structured_truth)
    retrieval_outcome = dict(row.get("retrieval_outcome") or {})
    if missing_slots and not retrieval_outcome.get("missing_query_slots"):
        retrieval_outcome["missing_query_slots"] = list(missing_slots)
    silver_values = _extract_metric_map_from_contexts(row.get("retrieved_contexts") or [])
    card = row.get("finalizer_input_card") or {}
    if isinstance(card, dict):
        key_numbers = card.get("key_numbers")
        if isinstance(key_numbers, dict):
            merged = dict(key_numbers)
            merged.update(silver_values)
            silver_values = merged
    gold_ctx = list(row.get("gold_context") or [])
    return evaluate_truth_slot_semantics(
        structured_truth.get("intent_slots") or {},
        contracts,
        answer,
        retrieval_outcome,
        silver_values,
        gold_ctx,
    )


def _intent_coverage(structured_truth: Dict[str, Any], row: Dict[str, Any], missing_slots: Sequence[str]) -> Dict[str, Any]:
    answer = str(row.get("answer_for_eval") or row.get("answer") or "")
    analyst_draft = str(row.get("analyst_draft") or "")
    audit = row.get("analyst_contract_audit") or {}
    if answer and analyst_draft and answer.strip() == analyst_draft.strip() and isinstance(audit, dict):
        audit_slots = audit.get("slot_status")
        if isinstance(audit_slots, dict) and audit_slots:
            coverage = float(audit.get("coverage_score") or 0.0)
            statuses = {
                str(slot): (
                    "disclosed_unanswerable"
                    if str(status) == "not_reliably_answerable"
                    else str(status)
                )
                for slot, status in audit_slots.items()
            }
            total = len(statuses)
            answered = sum(1 for status in statuses.values() if status in {"answered", "disclosed_unanswerable"})
            return {
                "slot_status": statuses,
                "score": round(answered / total, 4) if total else 1.0,
                "slot_answer_rate": round(answered / total, 4) if total else 1.0,
                "slot_evidence_strength": {},
                "slot_disclosure_honesty": {},
                "semantic_evidence_carry_score": coverage,
                "contract_source": "analyst_contract_audit",
                "contract_slot_status": statuses,
                "contract_conflicts": {},
            }
    semantic = _semantic_slot_evidence_eval(structured_truth, row, missing_slots)
    semantic["score"] = semantic.get("slot_answer_rate", 1.0)
    return semantic


def _metric_sentences(answer: str) -> List[str]:
    pieces = re.split(r"(?<=[.!?])\s+", answer or "")
    out: List[str] = []
    for piece in pieces:
        lowered = _safe_lower(piece)
        if any(term in lowered for term in ("pcr", "put-call", "put/call", "iv", "vix", "gpr", "open interest", "liquid", "liquidity", "volume", "spread")):
            out.append(piece)
    return out


def _unsupported_fact_pass(row: Dict[str, Any]) -> Tuple[bool, List[str]]:
    answer = _answer_body(row)
    ctx_numbers = _extract_numbers(" ".join(row.get("retrieved_contexts") or []))
    problems: List[str] = []
    for sentence in _metric_sentences(answer):
        lowered = _safe_lower(sentence)
        if "none" in lowered:
            continue
        for value in _extract_numbers(sentence):
            if "form-4" in lowered and math.isclose(value, 4.0, abs_tol=1e-9):
                continue
            if value in {7.0, 30.0, 75.0, 80.0, 100.0, 180.0}:
                continue
            if not _near_match(value, ctx_numbers):
                problems.append(sentence.strip())
                break
    return (len(problems) == 0, problems)


def _sec_taxonomy_pass(structured_truth: Dict[str, Any], row: Dict[str, Any]) -> Tuple[bool, str]:
    if structured_truth.get("query_family") != "insider_flow_driven":
        return True, ""

    observed = set((structured_truth.get("sec_action_taxonomy_expectation") or {}).get("observed_actions") or [])
    answer = _safe_lower(_answer_body(row))
    if "cannot be assessed reliably" in answer:
        return True, ""
    if observed and observed.issubset({"ACQUIRE/VEST", "NONE"}):
        if "selling" in answer or re.search(r"\binsider sell", answer):
            return False, "Answer describes insider selling even though observed SEC actions are vesting/NONE only."
        if re.search(r"\bbuy\b", answer) and "vesting" not in answer:
            return False, "Answer describes insider buying without preserving ACQUIRE/VEST distinction."
    return True, ""


def _revision_constraints_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    card = row.get("finalizer_input_card") or {}
    if isinstance(card, dict):
        constraints = card.get("revision_constraints")
        if isinstance(constraints, dict):
            return constraints
    constraints = row.get("revision_constraints") or {}
    return constraints if isinstance(constraints, dict) else {}


def _row_text_blob(row: Dict[str, Any]) -> str:
    parts = [
        str(row.get("answer_for_eval") or row.get("answer") or ""),
        str(row.get("answer_rendered_markdown") or ""),
    ]
    return "\n".join(part for part in parts if part).strip()


def _risk_disclosure_present(text: str, constraints: Dict[str, Any]) -> bool:
    lowered = _safe_lower(text)
    main_risk = str(constraints.get("main_risk_text") or "").strip().lower()
    if main_risk and main_risk in lowered:
        return True
    risk_needles = [
        "market impact risk is high",
        "market impact risk high",
        "execution risk",
        "risk remains high",
        "liquidity risk",
    ]
    return any(needle in lowered for needle in risk_needles)


def _why_not_now_present(text: str, constraints: Dict[str, Any]) -> bool:
    lowered = _safe_lower(text)
    explicit = str(constraints.get("what_must_change") or "").strip().lower()
    if explicit and explicit in lowered:
        return True
    needles = [
        "conditions would need to improve",
        "if conditions improved",
        "not actionable now",
        "not suitable now",
        "execution conditions",
        "data does not support a live structure",
        "would need to improve",
    ]
    return any(needle in lowered for needle in needles)


def _risk_disclosure_pass(row: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    constraints = _revision_constraints_from_row(row)
    if not constraints.get("must_disclose_risk"):
        return None, ""
    answer = _row_text_blob(row)
    if _risk_disclosure_present(answer, constraints):
        return True, ""
    return False, "Required risk disclosure was not retained in the final answer."


def _illustrative_structure_allowed_pass(structured_truth: Dict[str, Any], row: Dict[str, Any]) -> Tuple[bool, str]:
    del structured_truth
    answer = _row_text_blob(row)
    mode = _resolved_mode(row)
    constraints = _revision_constraints_from_row(row)
    lowered = _safe_lower(answer)
    has_concrete_structure = _contains_any(CONCRETE_STRUCTURE_PATTERNS, lowered)
    if mode not in {"informational_only", "directional_watchlist"}:
        return True, ""
    if constraints.get("structure_visibility_mode") != "illustrative_structure":
        return True, ""
    if not has_concrete_structure:
        return True, ""

    disclaimer_needles = [
        "illustrative only",
        "not a current recommendation",
        "not a live recommendation",
    ]
    if not any(needle in lowered for needle in disclaimer_needles):
        return False, "Illustrative structure is present in non-actionable mode without an explicit non-recommendation disclaimer."
    if not _risk_disclosure_present(answer, constraints):
        return False, "Illustrative structure is present in non-actionable mode without an explicit risk warning."
    if not _why_not_now_present(answer, constraints):
        return False, "Illustrative structure is present in non-actionable mode without a why-not-now or what-must-change explanation."
    return True, ""


def _structure_support_pass(structured_truth: Dict[str, Any], row: Dict[str, Any]) -> Tuple[bool, str]:
    answer = _answer_body(row)
    mode = _resolved_mode(row)
    constraints = _revision_constraints_from_row(row)
    lowered = _safe_lower(answer)
    has_concrete_structure = _contains_any(CONCRETE_STRUCTURE_PATTERNS, lowered)
    if re.search(r"\bno strike-level options structure\b|\bdo not issue strike-level action\b|\bnot a trade recommendation\b", lowered):
        has_concrete_structure = False
    if mode in {"informational_only", "directional_watchlist"} and has_concrete_structure:
        if constraints.get("structure_visibility_mode") != "illustrative_structure":
            return False, f"Mode is {mode} but answer still contains a concrete structure outside the allowed structure-visibility boundary."
        illustrative_ok, illustrative_reason = _illustrative_structure_allowed_pass(structured_truth, row)
        if illustrative_ok:
            return True, ""
        return False, illustrative_reason or f"Mode is {mode} but answer still contains a concrete structure."
    return True, ""


def _regime_logic_pass(row: Dict[str, Any]) -> Tuple[bool, str]:
    answer = _answer_body(row)
    regime = _infer_iv_regime(row)
    lowered = _safe_lower(answer)
    if regime == "LOW" and _contains_any(SHORT_PREMIUM_PATTERNS, lowered):
        return False, "LOW IV regime answer promotes short-premium structure."
    if regime == "HIGH" and _contains_any(LONG_PREMIUM_PATTERNS, lowered) and "catalyst" not in lowered:
        return False, "HIGH IV regime answer promotes naked long-premium structure without catalyst justification."
    return True, ""


def _financial_common_sense_pass(structured_truth: Dict[str, Any], row: Dict[str, Any]) -> Tuple[bool, str]:
    answer = _safe_lower(_answer_body(row))
    family = structured_truth.get("query_family")
    if family == "cross_asset_regime" and ("pcr volume is none" in answer and "liquidity remains visible through none" in answer):
        return False, "Answer repeats None-valued microstructure placeholders instead of honest unavailability disclosure."
    if family == "geopolitical_commodity" and "atm iv is none" in answer:
        return False, "Answer presents ATM IV as None rather than explicitly stating missing options evidence."
    return True, ""


def _financial_truthfulness(case: Dict[str, Any], row: Dict[str, Any], structured_truth: Dict[str, Any]) -> Dict[str, Any]:
    def _append_unique(target: List[str], value: str) -> None:
        if value and value not in target:
            target.append(value)

    unsupported_ok, unsupported_issues = _unsupported_fact_pass(row)
    disclosure_ok = _coverage_disclosure_pass(case, row, structured_truth)
    common_ok, common_reason = _financial_common_sense_pass(structured_truth, row)
    regime_ok, regime_reason = _regime_logic_pass(row)
    sec_ok, sec_reason = _sec_taxonomy_pass(structured_truth, row)
    risk_ok, risk_reason = _risk_disclosure_pass(row)
    illustrative_ok, illustrative_reason = _illustrative_structure_allowed_pass(structured_truth, row)
    structure_ok, structure_reason = _structure_support_pass(structured_truth, row)

    details = {
        "unsupported_fact_pass": unsupported_ok,
        "missing_info_honesty_pass": disclosure_ok,
        "financial_common_sense_pass": common_ok,
        "regime_logic_pass": regime_ok,
        "sec_taxonomy_pass": sec_ok,
        "risk_disclosure_pass": risk_ok,
        "illustrative_structure_allowed_pass": illustrative_ok,
        "structure_support_pass": structure_ok,
    }
    critical_failures: List[str] = []
    material_failures: List[str] = []
    compliance_failures: List[str] = []

    material_failures.extend(unsupported_issues)
    if not common_ok and common_reason:
        _append_unique(material_failures, common_reason)
    if not regime_ok and regime_reason:
        _append_unique(critical_failures, regime_reason)
    if not sec_ok and sec_reason:
        _append_unique(critical_failures, sec_reason)
    if risk_ok is False and risk_reason:
        _append_unique(compliance_failures, risk_reason)
    if not illustrative_ok and illustrative_reason:
        _append_unique(compliance_failures, illustrative_reason)
    if not structure_ok and structure_reason:
        _append_unique(compliance_failures, structure_reason)
    if not disclosure_ok:
        _append_unique(material_failures, "Missing-source or missing-slot disclosure is absent or incomplete.")

    failures = critical_failures + material_failures + compliance_failures
    if critical_failures:
        highest_failure_severity = "critical"
    elif material_failures:
        highest_failure_severity = "material"
    elif compliance_failures:
        highest_failure_severity = "compliance"
    else:
        highest_failure_severity = None

    return {
        "pass": all(value for value in details.values() if value is not None),
        "checks": details,
        "failures": failures,
        "critical_failures": critical_failures,
        "material_failures": material_failures,
        "compliance_failures": compliance_failures,
        "highest_failure_severity": highest_failure_severity,
    }


def _extract_labeled_values(text: str) -> Dict[str, float]:
    patterns = {
        "pcr": r"put-?call ratio.*?[:=]\s*([-+]?\d+(?:\.\d+)?)",
        "atm_iv": r"atm (?:implied volatility|iv).*?[:=]\s*([-+]?\d+(?:\.\d+)?)",
        "vix": r"\bvix\b.*?[:=]\s*([-+]?\d+(?:\.\d+)?)",
        "gpr": r"\bgpr\b.*?[:=]\s*([-+]?\d+(?:\.\d+)?)",
        "liquid_contracts": r"liquid.*?[:=]\s*([-+]?\d+(?:,\d{3})*(?:\.\d+)?)",
    }
    out: Dict[str, float] = {}
    for label, pattern in patterns.items():
        match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        try:
            out[label] = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
    return out


def _label_retention_score(analyst_draft: str, final_text: str) -> float:
    labels = ["put-call ratio", "atm implied volatility", "iv rank percentile", "vix", "gpr", "open interest", "liquid", "volume"]
    required = [label for label in labels if label in _safe_lower(analyst_draft)]
    if not required:
        return 1.0
    retained = [label for label in required if label in _safe_lower(final_text)]
    return round(len(retained) / len(required), 4)


def _evidence_retention(row: Dict[str, Any]) -> Dict[str, Any]:
    analyst = str(row.get("analyst_draft") or "")
    final_text = str(row.get("answer_rendered_markdown") or row.get("answer_for_eval") or row.get("answer") or "")
    analyst_values = _extract_labeled_values(analyst)
    final_numbers = _extract_numbers(final_text)
    if analyst_values:
        retained = sum(1 for value in analyst_values.values() if _near_match(value, final_numbers))
        key_number_retention_rate = round(retained / len(analyst_values), 4)
    else:
        key_number_retention_rate = 1.0

    anchor_retention_rate = _label_retention_score(analyst, final_text)
    provenance_retention_pass = all(
        section in final_text
        for section in ("## Direct Conclusion", "## Macro / Event Backdrop", "## Asset / Options Read")
    )
    evidence_retention_score = round(
        (key_number_retention_rate + anchor_retention_rate + (1.0 if provenance_retention_pass else 0.0)) / 3.0,
        4,
    )
    return {
        "key_number_retention_rate": key_number_retention_rate,
        "anchor_retention_rate": anchor_retention_rate,
        "provenance_retention_pass": provenance_retention_pass,
        "evidence_retention_score": evidence_retention_score,
    }


def _contract_compliance_score(truthfulness: Dict[str, Any], coverage_disclosure_pass: bool) -> float:
    flags = list(truthfulness.get("checks", {}).values()) + [coverage_disclosure_pass]
    return round(sum(1.0 for flag in flags if flag) / len(flags), 4) if flags else 1.0


def _load_openai_client() -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - runtime dependency guard
        raise RuntimeError("openai package is required for LLM rubric judging.") from exc
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for LLM rubric judging.")
    return OpenAI()


def _llm_financial_rubric(
    *,
    client: Any,
    model: str,
    query: str,
    answer: str,
    contexts: Sequence[str],
    structured_truth: Dict[str, Any],
) -> Dict[str, Any]:
    context_preview = "\n".join(f"- {ctx}" for ctx in list(contexts)[:12])
    prompt = (
        "You are grading a financial RAG answer for logic quality only. "
        "Assume correctness gates already filtered hard factual failures. "
        "Score each category from 1 to 5.\n\n"
        f"Query:\n{query}\n\n"
        f"Answer:\n{answer}\n\n"
        f"Structured truth contract:\n{json.dumps(structured_truth, ensure_ascii=False)}\n\n"
        f"Retrieved context preview:\n{context_preview}\n\n"
        "Return JSON with keys regime_fit, directional_sanity, risk_averse_suitability, "
        "abstention_discipline, financial_common_sense, rationale. "
        "Use integers 1-5 for the score keys."
    )
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a strict financial evaluation judge."},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    scores = []
    for key in (
        "regime_fit",
        "directional_sanity",
        "risk_averse_suitability",
        "abstention_discipline",
        "financial_common_sense",
    ):
        raw = payload.get(key, 3)
        try:
            score = int(raw)
        except (TypeError, ValueError):
            score = 3
        score = max(1, min(5, score))
        payload[key] = score
        scores.append(score)
    payload["normalized_score"] = round((sum(scores) / len(scores) - 1.0) / 4.0, 4)
    return payload


def _financial_sanity_score(
    *,
    truthfulness: Dict[str, Any],
    llm_result: Optional[Dict[str, Any]],
) -> float:
    if not truthfulness.get("pass"):
        base = sum(1.0 for flag in truthfulness.get("checks", {}).values() if flag) / max(len(truthfulness.get("checks", {})), 1)
        return round(min(0.25, 0.25 * base), 4)
    if llm_result is None:
        return round(sum(1.0 for flag in truthfulness.get("checks", {}).values() if flag) / max(len(truthfulness.get("checks", {})), 1), 4)
    return float(llm_result.get("normalized_score", 0.0))


def _paired_delta(prod_value: float, base_value: float) -> float:
    if prod_value > base_value + 1e-9:
        return 1.0
    if math.isclose(prod_value, base_value, rel_tol=1e-9, abs_tol=1e-9):
        return 0.5
    return 0.0


def _evaluate_row(
    *,
    case: Dict[str, Any],
    row: Dict[str, Any],
    client: Any,
    judge_model: str,
    run_llm_judge: bool,
) -> Dict[str, Any]:
    structured = _structured_truth(case)
    missing_slots = _missing_query_slots(case, row, structured)
    coverage_disclosure_pass = _coverage_disclosure_pass(case, row, structured)
    truthfulness = _financial_truthfulness(case, row, structured)
    intent = _intent_coverage(structured, row, missing_slots)
    evidence = _evidence_retention(row)
    contract_score = _contract_compliance_score(truthfulness, coverage_disclosure_pass)

    llm_result = None
    if run_llm_judge:
        llm_result = _llm_financial_rubric(
            client=client,
            model=judge_model,
            query=str(case.get("query") or row.get("query") or ""),
            answer=str(row.get("answer_for_eval") or row.get("answer") or ""),
            contexts=row.get("retrieved_contexts") or [],
            structured_truth=structured,
        )

    financial_sanity_score = _financial_sanity_score(
        truthfulness=truthfulness,
        llm_result=llm_result,
    )

    weighted = (0.40 * financial_sanity_score) + (0.30 * intent["score"]) + (0.30 * evidence["evidence_retention_score"])
    overall_case_score = round(min(0.25, weighted) if not truthfulness.get("pass") else weighted, 4)

    return {
        "query_family": structured.get("query_family"),
        "expected_sources": _expected_sources(case, structured),
        "retrieved_sources": _extract_retrieved_sources(row),
        "missing_sources": _missing_sources(case, row, structured),
        "missing_query_slots": missing_slots,
        "financial_truthfulness_pass": bool(truthfulness.get("pass")),
        "financial_truthfulness_checks": truthfulness.get("checks"),
        "financial_truthfulness_failures": truthfulness.get("failures"),
        "highest_failure_severity": truthfulness.get("highest_failure_severity"),
        "critical_failures": truthfulness.get("critical_failures"),
        "material_failures": truthfulness.get("material_failures"),
        "compliance_failures": truthfulness.get("compliance_failures"),
        "coverage_disclosure_pass": coverage_disclosure_pass,
        "financial_sanity_score": financial_sanity_score,
        "financial_sanity_rubric": llm_result,
        "intent_coverage_score": intent["score"],
        "intent_slot_status": intent["slot_status"],
        "contract_compliance_score": contract_score,
        "evidence_retention": evidence,
        "recommendation_mode_resolved": _resolved_mode(row),
        "overall_case_score": overall_case_score,
    }


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {}
    def avg(key: str) -> float:
        vals = [float(r.get(key, 0.0)) for r in results]
        return round(sum(vals) / len(vals), 4)

    truthfulness_rate = round(sum(1 for r in results if r.get("financial_truthfulness_pass")) / len(results), 4)
    coverage_rate = round(sum(1 for r in results if r.get("coverage_disclosure_pass")) / len(results), 4)
    illustrative_rate = round(
        sum(
            1
            for r in results
            if (r.get("financial_truthfulness_checks") or {}).get("illustrative_structure_allowed_pass", True)
        ) / len(results),
        4,
    )
    risk_required_count = sum(
        1 for r in results
        if (r.get("financial_truthfulness_checks") or {}).get("risk_disclosure_pass") is not None
    )
    risk_disclosure_rate = (
        round(
            sum(
                1
                for r in results
                if (r.get("financial_truthfulness_checks") or {}).get("risk_disclosure_pass") is True
            ) / risk_required_count,
            4,
        )
        if risk_required_count
        else None
    )
    critical_rate = round(sum(1 for r in results if r.get("critical_failures")) / len(results), 4)
    material_rate = round(sum(1 for r in results if r.get("material_failures")) / len(results), 4)
    compliance_rate = round(sum(1 for r in results if r.get("compliance_failures")) / len(results), 4)
    return {
        "n_cases": len(results),
        "financial_truthfulness_pass_rate": truthfulness_rate,
        "coverage_disclosure_pass_rate": coverage_rate,
        "illustrative_structure_allowed_pass_rate": illustrative_rate,
        "risk_disclosure_pass_rate": risk_disclosure_rate,
        "critical_failure_case_rate": critical_rate,
        "material_failure_case_rate": material_rate,
        "compliance_failure_case_rate": compliance_rate,
        "financial_sanity_avg": avg("financial_sanity_score"),
        "intent_coverage_avg": avg("intent_coverage_score"),
        "contract_compliance_avg": avg("contract_compliance_score"),
        "evidence_retention_avg": round(sum(float(r["evidence_retention"]["evidence_retention_score"]) for r in results) / len(results), 4),
        "overall_case_score_avg": avg("overall_case_score"),
    }


def _metric_description_map() -> Dict[str, str]:
    return {
        "financial_truthfulness_pass_rate": "Share of final answers that stayed within hard financial and support boundaries.",
        "coverage_disclosure_pass_rate": "Share of cases where missing sources or slots were disclosed honestly instead of being papered over.",
        "critical_failure_case_rate": "Share of cases containing direction-changing financial logic failures.",
        "material_failure_case_rate": "Share of cases containing fact-support or honest-disclosure failures.",
        "compliance_failure_case_rate": "Share of cases containing non-actionable boundary or illustrative-contract failures.",
        "risk_disclosure_pass_rate": "Share of risk-required cases where the final answer preserved the required risk disclosure from structured state.",
        "illustrative_structure_allowed_pass_rate": "Share of cases where non-actionable illustrative structures, if present, were framed legally with disclaimer, risk, and why-not-now context.",
        "financial_sanity_avg": "Hybrid score for financial logic quality after hard truthfulness checks.",
        "intent_coverage_avg": "How completely the answer addressed the user’s requested slots without forcing coverage.",
        "agentic_delta_avg": "Average paired delta between baseline and the four-node finalizer.",
        "evidence_retention_avg": "How much key evidence survived into the final answer and report output.",
    }


def _high_summary(summary: Dict[str, Any], baseline_summary: Dict[str, Any], production_summary: Dict[str, Any]) -> str:
    truth_gain = (production_summary.get("financial_truthfulness_pass_rate", 0.0) or 0.0) - (baseline_summary.get("financial_truthfulness_pass_rate", 0.0) or 0.0)
    delta = summary.get("agentic_delta_avg", 0.0) or 0.0
    if delta >= 0.75 and truth_gain >= 0.2:
        return "The four-node system is materially safer than baseline, and most of that gain is coming from better final-stage contract control rather than broader answer coverage."
    if delta >= 0.5:
        return "The four-node system is ahead of baseline overall, but some of the gain still looks like conservative mode control rather than consistently stronger analytical answers."
    return "The current run is not clearly separating itself from baseline yet; the remaining weakness is likely in node handoff quality rather than basic retrieval."


def _quick_takeaways(summary: Dict[str, Any], paired_results: List[Dict[str, Any]]) -> List[str]:
    takeaways: List[str] = []
    if (summary.get("illustrative_structure_allowed_pass_rate", 1.0) or 1.0) < 1.0:
        takeaways.append("Illustrative structures are still being framed inconsistently; some cases are missing disclaimer, risk, or why-not-now context.")
    if (summary.get("risk_disclosure_pass_rate", 1.0) or 1.0) < 1.0:
        takeaways.append("Some structured high-risk requirements are still getting lost before the final answer is rendered.")
    if (summary.get("coverage_disclosure_pass_rate", 0.0) or 0.0) < 1.0:
        takeaways.append("Missing-source disclosure is still incomplete in part of the run.")
    if not takeaways:
        takeaways.append("The run is mostly respecting the updated non-actionable structure contract.")
    if paired_results:
        worst = min(paired_results, key=lambda row: float(row.get("agentic_delta_score", 0.0)))
        takeaways.append(f"Weakest paired case right now: {worst.get('case_name')} (delta {worst.get('agentic_delta_score', 0.0):.2f}).")
    return takeaways


def _render_report(
    *,
    summary: Dict[str, Any],
    paired_results: List[Dict[str, Any]],
    baseline_summary: Dict[str, Any],
    production_summary: Dict[str, Any],
) -> str:
    metric_descriptions = _metric_description_map()
    lines = [
        "# Contract-First Financial Evaluation",
        "",
        "## High Summary",
        _high_summary(summary, baseline_summary, production_summary),
        "",
        "## Quick Takeaways",
    ]
    for item in _quick_takeaways(summary, paired_results):
        lines.append(f"- {item}")
    lines.extend([
        "",
        "## Summary",
        f"- Cases: {summary.get('n_cases', 0)}",
        f"- financial_truthfulness_pass_rate: {_fmt_metric(summary.get('financial_truthfulness_pass_rate'))}",
        f"- coverage_disclosure_pass_rate: {_fmt_metric(summary.get('coverage_disclosure_pass_rate'))}",
        f"- critical_failure_case_rate: {_fmt_metric(summary.get('critical_failure_case_rate'))}",
        f"- material_failure_case_rate: {_fmt_metric(summary.get('material_failure_case_rate'))}",
        f"- compliance_failure_case_rate: {_fmt_metric(summary.get('compliance_failure_case_rate'))}",
        f"- risk_disclosure_pass_rate: {_fmt_metric(summary.get('risk_disclosure_pass_rate'))}",
        f"- illustrative_structure_allowed_pass_rate: {_fmt_metric(summary.get('illustrative_structure_allowed_pass_rate'))}",
        f"- financial_sanity_avg: {_fmt_metric(summary.get('financial_sanity_avg'))}",
        f"- intent_coverage_avg: {_fmt_metric(summary.get('intent_coverage_avg'))}",
        f"- agentic_delta_avg: {_fmt_metric(summary.get('agentic_delta_avg'))}",
        f"- evidence_retention_avg: {_fmt_metric(summary.get('evidence_retention_avg'))}",
        "",
        "## Metric Guide",
    ])
    for key, desc in metric_descriptions.items():
        lines.append(f"- `{key}`: {desc}")
    lines.extend([
        "",
        "## Baseline vs Finalizer",
        f"- Baseline truthfulness pass rate: {_fmt_metric(baseline_summary.get('financial_truthfulness_pass_rate'))}",
        f"- Finalizer truthfulness pass rate: {_fmt_metric(production_summary.get('financial_truthfulness_pass_rate'))}",
        f"- Baseline financial sanity avg: {_fmt_metric(baseline_summary.get('financial_sanity_avg'))}",
        f"- Finalizer financial sanity avg: {_fmt_metric(production_summary.get('financial_sanity_avg'))}",
        "",
        "## Per-case Delta",
    ])
    for row in paired_results:
        lines.extend([
            "",
            f"### {row['case_name']}",
            f"- agentic_delta_score: {row['agentic_delta_score']:.2f}",
            f"- baseline_overall: {row['baseline']['overall_case_score']:.2f}",
            f"- finalizer_overall: {row['production']['overall_case_score']:.2f}",
            f"- baseline_truthfulness_pass: {row['baseline']['financial_truthfulness_pass']}",
            f"- finalizer_truthfulness_pass: {row['production']['financial_truthfulness_pass']}",
            f"- baseline_missing_sources: {row['baseline']['missing_sources']}",
            f"- finalizer_missing_sources: {row['production']['missing_sources']}",
            f"- finalizer_mode: {row['production'].get('recommendation_mode_resolved')}",
        ])
        if row["production"].get("highest_failure_severity"):
            lines.append(f"- highest_failure_severity: {row['production']['highest_failure_severity']}")
        if row["production"].get("critical_failures"):
            lines.append(f"- critical_failures: {row['production']['critical_failures']}")
        if row["production"].get("material_failures"):
            lines.append(f"- material_failures: {row['production']['material_failures']}")
        if row["production"].get("compliance_failures"):
            lines.append(f"- compliance_failures: {row['production']['compliance_failures']}")
    return "\n".join(lines) + "\n"


def _dashboard_html(
    *,
    summary: Dict[str, Any],
    paired_results: List[Dict[str, Any]],
    baseline_summary: Dict[str, Any],
    production_summary: Dict[str, Any],
) -> str:
    metric_descriptions = _metric_description_map()
    cards = [
        ("Truth Pass", _fmt_metric(summary.get('financial_truthfulness_pass_rate')), metric_descriptions["financial_truthfulness_pass_rate"]),
        ("Disclosure Pass", _fmt_metric(summary.get('coverage_disclosure_pass_rate')), metric_descriptions["coverage_disclosure_pass_rate"]),
        ("Critical Fail", _fmt_metric(summary.get('critical_failure_case_rate')), metric_descriptions["critical_failure_case_rate"]),
        ("Material Fail", _fmt_metric(summary.get('material_failure_case_rate')), metric_descriptions["material_failure_case_rate"]),
        ("Compliance Fail", _fmt_metric(summary.get('compliance_failure_case_rate')), metric_descriptions["compliance_failure_case_rate"]),
        ("Risk Disclosure", _fmt_metric(summary.get('risk_disclosure_pass_rate')), metric_descriptions["risk_disclosure_pass_rate"]),
        ("Illustrative Legal", _fmt_metric(summary.get('illustrative_structure_allowed_pass_rate')), metric_descriptions["illustrative_structure_allowed_pass_rate"]),
        ("Financial Sanity", _fmt_metric(summary.get('financial_sanity_avg')), metric_descriptions["financial_sanity_avg"]),
        ("Intent Coverage", _fmt_metric(summary.get('intent_coverage_avg')), metric_descriptions["intent_coverage_avg"]),
    ]
    card_html = "".join(
        f"<div class='metric-card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='desc'>{html.escape(desc)}</div></div>"
        for label, value, desc in cards
    )
    takeaways = "".join(f"<li>{html.escape(item)}</li>" for item in _quick_takeaways(summary, paired_results))
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('case_name')))}</td>"
        f"<td>{row.get('agentic_delta_score', 0.0):.2f}</td>"
        f"<td>{html.escape(str(row['production'].get('financial_truthfulness_pass')))}</td>"
        f"<td>{html.escape(str(row['production'].get('highest_failure_severity') or '-'))}</td>"
        f"<td>{html.escape(str(row['production'].get('critical_failures') or []))}</td>"
        f"<td>{html.escape(str(row['production'].get('material_failures') or []))}</td>"
        f"<td>{html.escape(str(row['production'].get('compliance_failures') or []))}</td>"
        "</tr>"
        for row in paired_results
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Contract-First Evaluation Dashboard</title>
  <style>{EVAL_DASHBOARD_CSS}</style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <h1>Contract-First Evaluation Dashboard</h1>
      <div class="muted">{html.escape(_high_summary(summary, baseline_summary, production_summary))}</div>
      <div class="grid">{card_html}</div>
    </section>
    <section class="panel">
      <h2>Quick Takeaways</h2>
      <ul class="list">{takeaways}</ul>
      <div style="margin-top:16px">
        <span class="pill">Baseline truth pass: {baseline_summary.get('financial_truthfulness_pass_rate', 0):.2f}</span>
        <span class="pill">Finalizer truth pass: {production_summary.get('financial_truthfulness_pass_rate', 0):.2f}</span>
        <span class="pill">Baseline sanity: {baseline_summary.get('financial_sanity_avg', 0):.2f}</span>
        <span class="pill">Finalizer sanity: {production_summary.get('financial_sanity_avg', 0):.2f}</span>
      </div>
    </section>
    <section class="panel">
      <h2>Per-case Delta</h2>
      <table>
        <thead>
          <tr><th>Case</th><th>Delta</th><th>Truth Pass</th><th>Highest Severity</th><th>Critical</th><th>Material</th><th>Compliance</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""


def _pair_rows(
    truth_payload: Dict[str, Any],
    baseline_rows: List[Dict[str, Any]],
    production_rows: List[Dict[str, Any]],
    *,
    client: Any,
    judge_model: str,
    run_llm_judge: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    truth_by_query = _truth_map(truth_payload)
    baseline_by_query = {str(row.get("query")): row for row in baseline_rows}
    production_by_query = {str(row.get("query")): row for row in production_rows}

    paired: List[Dict[str, Any]] = []
    baseline_eval_rows: List[Dict[str, Any]] = []
    production_eval_rows: List[Dict[str, Any]] = []
    for query, case in truth_by_query.items():
        base_row = baseline_by_query.get(query)
        prod_row = production_by_query.get(query)
        if not base_row or not prod_row:
            logger.warning("Skipping unpaired query: %s", query)
            continue

        base_eval = _evaluate_row(
            case=case,
            row=base_row,
            client=client,
            judge_model=judge_model,
            run_llm_judge=run_llm_judge,
        )
        prod_eval = _evaluate_row(
            case=case,
            row=prod_row,
            client=client,
            judge_model=judge_model,
            run_llm_judge=run_llm_judge,
        )
        baseline_eval_rows.append({"case_name": case["name"], "query": query, **base_eval})
        production_eval_rows.append({"case_name": case["name"], "query": query, **prod_eval})

        delta_truthfulness = _paired_delta(float(prod_eval["financial_truthfulness_pass"]), float(base_eval["financial_truthfulness_pass"]))
        delta_contract = _paired_delta(prod_eval["contract_compliance_score"], base_eval["contract_compliance_score"])
        delta_sanity = _paired_delta(prod_eval["financial_sanity_score"], base_eval["financial_sanity_score"])
        delta_intent = _paired_delta(prod_eval["intent_coverage_score"], base_eval["intent_coverage_score"])
        agentic_delta_score = round((delta_truthfulness + delta_contract + delta_sanity + delta_intent) / 4.0, 4)
        paired.append({
            "case_name": case["name"],
            "query": query,
            "agentic_delta_score": agentic_delta_score,
            "delta_truthfulness": delta_truthfulness,
            "delta_contract_compliance": delta_contract,
            "delta_financial_sanity": delta_sanity,
            "delta_intent_coverage": delta_intent,
            "baseline": base_eval,
            "production": prod_eval,
        })
    return paired, baseline_eval_rows, production_eval_rows


def _resolve_results_path(explicit: Optional[str], kind: str, run_dir: Optional[Path]) -> Path:
    if explicit:
        return Path(explicit)
    base_dir = run_dir or _latest_ragas_run()
    filename = "baseline_results_full.jsonl" if kind == "baseline" else "production_results_full.jsonl"
    return base_dir / filename


def run_contract_eval(
    *,
    truth_file: Path = DEFAULT_TRUTH_FILE,
    baseline_results: Optional[Path] = None,
    production_results: Optional[Path] = None,
    run_dir: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    judge_model: str = "gpt-4o",
    skip_llm_judge: bool = False,
) -> Dict[str, Path]:
    truth_payload = _read_json(Path(truth_file))
    baseline_path = baseline_results or _resolve_results_path(None, "baseline", run_dir)
    production_path = production_results or _resolve_results_path(None, "production", run_dir)
    baseline_rows = _read_jsonl(Path(baseline_path))
    production_rows = _read_jsonl(Path(production_path))

    client = None
    run_llm_judge = not skip_llm_judge
    if run_llm_judge:
        client = _load_openai_client()

    paired, baseline_eval_rows, production_eval_rows = _pair_rows(
        truth_payload,
        baseline_rows,
        production_rows,
        client=client,
        judge_model=judge_model,
        run_llm_judge=run_llm_judge,
    )
    baseline_summary = _summarize(baseline_eval_rows)
    production_summary = _summarize(production_eval_rows)
    combined_summary = {
        **_summarize(production_eval_rows),
        "agentic_delta_avg": round(sum(row["agentic_delta_score"] for row in paired) / len(paired), 4) if paired else 0.0,
        "comparison_cases": len(paired),
        "baseline_results_file": str(baseline_path),
        "production_results_file": str(production_path),
        "truth_file": str(Path(truth_file)),
        "judge_model": None if skip_llm_judge else judge_model,
        "generated_at": datetime.now().isoformat(),
    }

    target_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir.mkdir(parents=True, exist_ok=True)

    cases_path = target_dir / "agentic_eval_cases.jsonl"
    with cases_path.open("w", encoding="utf-8") as fh:
        for row in paired:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary_path = target_dir / "agentic_eval_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "summary": combined_summary,
                "baseline_summary": baseline_summary,
                "production_summary": production_summary,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report_path = target_dir / "agentic_eval_report.md"
    report_path.write_text(
        _render_report(
            summary=combined_summary,
            paired_results=paired,
            baseline_summary=baseline_summary,
            production_summary=production_summary,
        ),
        encoding="utf-8",
    )
    dashboard_path = target_dir / "agentic_eval_dashboard.html"
    dashboard_path.write_text(
        _dashboard_html(
            summary=combined_summary,
            paired_results=paired,
            baseline_summary=baseline_summary,
            production_summary=production_summary,
        ),
        encoding="utf-8",
    )
    return {"cases": cases_path, "summary": summary_path, "report": report_path, "dashboard": dashboard_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="Contract-first financial evaluator.")
    parser.add_argument("--truth-file", default=str(DEFAULT_TRUTH_FILE), help="Full truth catalogue JSON.")
    parser.add_argument("--baseline-results", default=None, help="baseline_results_full.jsonl path.")
    parser.add_argument("--production-results", default=None, help="production_results_full.jsonl path.")
    parser.add_argument("--run-dir", default=None, help="Existing logs/RAGAS run directory to evaluate.")
    parser.add_argument("--out-dir", default=None, help="Output directory. Default logs/agentic_eval/YYYYMMDD_HHMMSS.")
    parser.add_argument("--judge-model", default="gpt-4o", help="OpenAI judge model for rubric scoring.")
    parser.add_argument("--skip-llm-judge", action="store_true", help="Skip gpt-4o financial rubric scoring.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    run_dir = Path(args.run_dir) if args.run_dir else None
    outputs = run_contract_eval(
        truth_file=Path(args.truth_file),
        baseline_results=Path(args.baseline_results) if args.baseline_results else None,
        production_results=Path(args.production_results) if args.production_results else None,
        run_dir=run_dir,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        judge_model=args.judge_model,
        skip_llm_judge=args.skip_llm_judge,
    )

    print("Contract-first financial evaluation complete")
    print(f"  cases:   {outputs['cases']}")
    print(f"  summary: {outputs['summary']}")
    print(f"  report:  {outputs['report']}")
    print(f"  dashboard: {outputs['dashboard']}")


if __name__ == "__main__":
    main()
