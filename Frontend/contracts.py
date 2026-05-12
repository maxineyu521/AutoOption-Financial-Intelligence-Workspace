from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import PROJECT_ROOT

LEAKAGE_PATTERNS = [
    r"Automated pipeline degradation[^\n]*",
    r"Re-run the pipeline once upstream data \+ LLM availability is restored\.?",
    r"Full strategy payload",
    r"do not act on partial outputs\.?",
]


def to_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        dumped = obj.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(obj, "dict"):
        dumped = obj.dict()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def to_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def sanitize_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = text
    for pat in LEAKAGE_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


@lru_cache(maxsize=1)
def load_ticker_universe() -> set[str]:
    files = [
        PROJECT_ROOT / "config" / "universe" / "equity_single_name.json",
        PROJECT_ROOT / "config" / "universe" / "etf_broad_market.json",
        PROJECT_ROOT / "config" / "universe" / "etf_commodity.json",
    ]
    tickers: set[str] = set()
    for fp in files:
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                tickers |= {str(x).upper() for x in rows}
        except Exception:
            continue
    return tickers


def _extract_latest_update_date(anchors: List[Any]) -> str:
    parsed: List[datetime] = []
    for anchor in anchors:
        for token in re.findall(r"\d{4}-\d{2}-\d{2}", str(anchor)):
            try:
                parsed.append(datetime.strptime(token, "%Y-%m-%d"))
            except ValueError:
                continue
    return max(parsed).strftime("%Y-%m-%d") if parsed else "Unknown"


def _normalize_predicates(time_range: Dict[str, Any]) -> List[Dict[str, Any]]:
    preds = to_mapping(time_range.get("source_predicates"))
    rows: List[Dict[str, Any]] = []
    for source, payload in preds.items():
        p = to_mapping(payload)
        rows.append(
            {
                "source": source,
                "start_date": p.get("start_date"),
                "end_date": p.get("end_date"),
                "window_days": p.get("window_days"),
                "granularity": p.get("granularity"),
                "widened": bool(p.get("widened")),
                "widen_reason": p.get("widen_reason") or "-",
            }
        )
    return rows


def _collect_links(obj: Any, bag: Optional[set[str]] = None) -> set[str]:
    bag = bag or set()
    if isinstance(obj, dict):
        for _, v in obj.items():
            _collect_links(v, bag)
    elif isinstance(obj, list):
        for x in obj:
            _collect_links(x, bag)
    elif isinstance(obj, str):
        for match in re.findall(r"https?://[^\s\"'>]+", obj):
            bag.add(match)
    return bag


def normalize_state(raw_state: Dict[str, Any]) -> Dict[str, Any]:
    state = to_mapping(raw_state)
    metadata = to_mapping(state.get("metadata"))
    hyde = to_mapping(state.get("hyde_anticipation"))
    silver_context = to_mapping(state.get("silver_context"))
    gold_chunks = [to_mapping(x) for x in to_list(state.get("gold_context"))]
    final_strategy = to_mapping(state.get("final_strategy"))
    final_report = to_mapping(final_strategy.get("final_report"))
    iv_regime = to_mapping(state.get("iv_regime_pinned"))
    time_range = to_mapping(state.get("time_range"))
    scope_contract = to_mapping(state.get("scope_contract"))
    retrieval_outcome = to_mapping(state.get("retrieval_outcome"))
    critic_reasoning_profile = to_mapping(state.get("critic_reasoning_profile"))
    finalizer_input_card = to_mapping(state.get("finalizer_input_card"))

    silver_values = to_mapping(silver_context.get("values"))
    silver_links = sorted(_collect_links(silver_context))
    lineage_anchors = to_list(silver_context.get("lineage_anchors"))
    latest_update = _extract_latest_update_date(lineage_anchors)
    posture_trace = to_mapping(
        finalizer_input_card.get("posture_reasoning_trace")
        or critic_reasoning_profile.get("posture_reasoning_trace")
    )
    posture_label = (
        finalizer_input_card.get("posture_label")
        or critic_reasoning_profile.get("posture_label")
        or posture_trace.get("derived_label")
        or ""
    )
    posture_takeaway = (
        finalizer_input_card.get("posture_takeaway")
        or critic_reasoning_profile.get("posture_takeaway")
        or ""
    )
    posture_rationale = (
        finalizer_input_card.get("posture_rationale")
        or critic_reasoning_profile.get("posture_rationale")
        or ""
    )

    sec_sales: List[Dict[str, Any]] = []
    for chunk in gold_chunks:
        meta = to_mapping(chunk.get("metadata"))
        if str(meta.get("form_type", "")).strip() != "4":
            continue
        if str(meta.get("action_direction", "")).upper() != "SELL":
            continue
        content = str(chunk.get("content", ""))
        sec_sales.append(
            {
                "executive": content.split(" (")[0] if " (" in content else "N/A",
                "content": content,
                "filed_at": meta.get("filed_at"),
                "transaction_date": meta.get("transaction_date"),
                "accession_no": meta.get("accession_no"),
                "url": meta.get("url"),
            }
        )

    return {
        "original_query": state.get("original_query", ""),
        "metadata": metadata,
        "hyde_anticipation": hyde,
        "silver_context": silver_context,
        "silver_values": silver_values,
        "silver_links": silver_links,
        "lineage_anchors": lineage_anchors,
        "latest_update_date": latest_update,
        "gold_context": gold_chunks,
        "sec_sales": sec_sales,
        "time_range": time_range,
        "source_predicates": _normalize_predicates(time_range),
        "scope_contract": scope_contract,
        "retrieval_outcome": retrieval_outcome,
        "final_strategy": final_strategy,
        "final_report": final_report,
        "recommendation_mode": state.get("recommendation_mode"),
        "actionability_mode": state.get("actionability_mode"),
        "structure_visibility_mode": state.get("structure_visibility_mode"),
        "iv_regime_pinned": iv_regime,
        "critic_reasoning_profile": critic_reasoning_profile,
        "finalizer_input_card": finalizer_input_card,
        "posture_label": posture_label,
        "posture_takeaway": posture_takeaway,
        "posture_rationale": posture_rationale,
        "posture_reasoning_trace": posture_trace,
        "node_audit_log": to_list(state.get("node_audit_log")),
    }


def quick_query_quality(query: str) -> Dict[str, Any]:
    q = query or ""
    q_upper = q.upper()
    universe = load_ticker_universe()

    tickers = sorted({m for m in re.findall(r"\b[A-Z]{2,5}\b", q_upper) if m in universe})
    has_ticker = bool(tickers)
    has_time = bool(re.search(r"\b(today|yesterday|past week|past month|month|week|6 months|six months)\b", q.lower()))
    has_metric = bool(re.search(r"\b(iv|put-call|put call|pcr|skew|liquidity|open interest|vix|gpr|yield)\b", q.lower()))
    has_intent = bool(re.search(r"\b(buy|sell|hedge|position|strategy|signal|impact)\b", q.lower()))

    score = 0
    score += 25 if has_ticker else 0
    score += 25 if has_time else 0
    score += 25 if has_metric else 0
    score += 25 if has_intent else 0

    suggestions: List[str] = []
    if not has_ticker:
        suggestions.append("Start with a covered asset, such as AAPL, SPY, QQQ, GLD, or SLV.")
    if not has_time:
        suggestions.append("Add a time window like today, past week, or past month.")
    if not has_metric:
        suggestions.append("Name the signal you care about: IV, skew, PCR, VIX, GPR, liquidity, Form 4, or insider selling.")
    if not has_intent:
        suggestions.append("Say the goal: options posture, SEC filing risk, geopolitics narrative, or macro regime.")

    return {
        "score": score,
        "tickers": tickers,
        "suggestions": suggestions,
    }


def state_query_quality(normalized_state: Dict[str, Any]) -> Dict[str, Any]:
    metadata = to_mapping(normalized_state.get("metadata"))
    universe = load_ticker_universe()
    tickers = [str(t).upper() for t in to_list(metadata.get("tickers"))]
    in_universe = [t for t in tickers if t in universe]
    metrics = to_list(metadata.get("metrics"))
    time_window = str(metadata.get("time_window", "")).strip()

    score = 0
    score += 30 if tickers else 0
    score += 25 if time_window else 0
    score += 25 if metrics else 0
    score += 20 if tickers and len(in_universe) == len(tickers) else 0

    suggestions: List[str] = []
    if not tickers:
        suggestions.append("Add the asset first so retrieval can anchor the evidence.")
    elif len(in_universe) != len(tickers):
        miss = [t for t in tickers if t not in universe]
        suggestions.append(f"{', '.join(miss)} is outside the tracked universe; try a covered ticker or ask a macro-only question.")
    if not time_window:
        suggestions.append("Add a clear window such as today, past week, or past month.")
    if not metrics:
        suggestions.append("Add explicit signal words such as IV, skew, PCR, VIX, GPR, liquidity, Form 4, or insider selling.")

    return {
        "score": score,
        "tickers": tickers,
        "in_universe": in_universe,
        "time_window": time_window or "N/A",
        "metrics_count": len(metrics),
        "suggestions": suggestions,
    }


def evidence_blend_summary(normalized_state: Dict[str, Any]) -> str:
    latest_update = str(normalized_state.get("latest_update_date", "Unknown"))
    sec_sales = to_list(normalized_state.get("sec_sales"))
    gold = to_list(normalized_state.get("gold_context"))
    gold_scores = [float(to_mapping(x).get("score")) for x in gold if to_mapping(x).get("score") is not None]
    avg_gold = (sum(gold_scores) / len(gold_scores)) if gold_scores else None

    freshness = "unknown freshness"
    if latest_update != "Unknown":
        try:
            delta_days = (date.today() - datetime.strptime(latest_update, "%Y-%m-%d").date()).days
            if delta_days <= 2:
                freshness = f"fresh structured data snapshot ({latest_update})"
            elif delta_days <= 7:
                freshness = f"moderately fresh structured data snapshot ({latest_update})"
            else:
                freshness = f"stale structured data snapshot ({latest_update})"
        except ValueError:
            pass

    gold_phrase = f"narrative confidence avg {avg_gold:.3f}" if avg_gold is not None else "narrative confidence unavailable"
    sec_dates = [s.get("filed_at") for s in sec_sales if s.get("filed_at")]
    sec_recency = max(sec_dates) if sec_dates else "n/a"
    sec_phrase = f"{len(sec_sales)} Form-4 SELL filings, latest filed {sec_recency}" if sec_sales else "no recent Form-4 SELL filing evidence"

    return f"Evidence blend: {freshness}; {gold_phrase}; {sec_phrase}."
