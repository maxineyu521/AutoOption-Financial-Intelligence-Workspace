"""
Fresh production runner for the multi-node financial RAG graph.

Unlike the legacy extraction path, this runner executes the graph directly and
writes detailed node-level audit artefacts that are designed for reuse by:

- Scripts/evaluation/agentic_eval.py
- Scripts/evaluation/agentic_node_eval.py

Primary artefacts:
    - production_results_full.jsonl
    - production_answers.jsonl
    - production_audit.jsonl
    - production_node_events.jsonl
    - production_summary.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from datetime import date, datetime
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

from pydantic import BaseModel  # noqa: E402

from Scripts.agents.router import build_financial_rag_graph  # noqa: E402

DEFAULT_TRUTH_FILE = PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_ground_truth_queries.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "agentic_eval"

logger = logging.getLogger("production_model")

APPEND_ONLY_KEYS = {"critic_feedback", "node_audit_log"}


def _to_jsonable(obj: Any, max_str: Optional[int] = None) -> Any:
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        if max_str and len(obj) > max_str:
            return obj[:max_str] + f"...(+{len(obj) - max_str} chars)"
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return _to_jsonable(obj.model_dump(), max_str=max_str)
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, max_str=max_str) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x, max_str=max_str) for x in obj]
    try:
        return str(obj)
    except Exception:
        return f"<unserializable {type(obj).__name__}>"


def _slugify(text: str, max_len: int = 48) -> str:
    import re

    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "unnamed"


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


def _as_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    return {}


def _normalise_source_name(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "macro_history": "macro",
        "macro": "macro",
        "options": "options",
        "sec": "sec",
        "gpr": "gpr",
        "news": "news",
    }
    return aliases.get(raw, raw)


def _merge_state(merged: Dict[str, Any], delta: Dict[str, Any]) -> None:
    for key, value in delta.items():
        if key in APPEND_ONLY_KEYS:
            current = merged.get(key) or []
            merged[key] = list(current) + list(value or [])
        else:
            merged[key] = value


def _serialise_feedback(items: Sequence[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, BaseModel):
            out.append(_to_jsonable(item))
        elif isinstance(item, dict):
            out.append(_to_jsonable(item))
        else:
            out.append({"raw": str(item)})
    return out


def _snapshot_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    card = _normalized_finalizer_input_card(state)
    return {
        "revision_count": state.get("revision_count"),
        "draft_len": len(str(state.get("draft_report") or "")),
        "critic_feedback_n": len(state.get("critic_feedback") or []),
        "checker_verdict": state.get("checker_verdict"),
        "critic_verdict": state.get("critic_verdict"),
        "recommendation_mode": card.get("recommendation_mode") or state.get("recommendation_mode"),
        "minor_edits_n": len(card.get("minor_edits") or []),
        "finalizer_card_keys": sorted(card.keys()) if card else [],
    }


def _normalized_revision_constraints(state: Dict[str, Any]) -> Dict[str, Any]:
    raw = _as_mapping(state.get("revision_constraints") or {})
    card = _as_mapping(state.get("finalizer_input_card") or {})
    card_constraints = _as_mapping(card.get("revision_constraints") or {})
    constraints = dict(raw)
    constraints.update(card_constraints)
    return constraints


def _normalized_finalizer_input_card(state: Dict[str, Any]) -> Dict[str, Any]:
    card = _as_mapping(state.get("finalizer_input_card") or {})
    normalized = dict(card)
    normalized["minor_edits"] = list(normalized.get("minor_edits") or [])
    return normalized


def _finalizer_plaintext_answer(final_report: Any, markdown: str = "", fallback: str = "") -> str:
    report = _as_mapping(final_report)
    convo = str(_obj_get(report, "conversation_reply", "") or "").strip()
    if convo:
        return convo

    parts: List[str] = []
    direct = str(_obj_get(report, "direct_conclusion", "") or "").strip()
    if direct:
        parts.append(direct)
    macro = str(_obj_get(report, "macro_summary", "") or "").strip()
    if macro:
        parts.append(f"Macro summary: {macro}")
    asset = str(_obj_get(report, "asset_read", "") or "").strip()
    if asset:
        parts.append(f"Asset / options read: {asset}")
    if parts:
        return " ".join(p for p in parts if p).strip()
    if markdown:
        return markdown.strip()
    return fallback.strip()


def _coerce_gold_texts(gold_context: Sequence[Any]) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    retrieved: List[str] = []
    raw_items: List[Dict[str, Any]] = []
    sources: List[str] = []
    for item in gold_context or []:
        if isinstance(item, dict):
            meta = _to_jsonable(item)
            text = str(item.get("content") or item.get("text") or item.get("page_content") or "")
            source = _normalise_source_name(item.get("source_type") or item.get("source"))
        else:
            meta = {"raw": str(item)}
            text = str(item)
            source = ""
        if text.strip():
            retrieved.append(text)
        if source:
            sources.append(source)
        raw_items.append(meta)
    return retrieved, raw_items, sources


def _coerce_silver_texts(silver_context: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    values = _as_mapping(_obj_get(silver_context, "values", {}) or {})
    retrieved: List[str] = []
    sources: set[str] = set()
    for key, value in values.items():
        retrieved.append(f"Silver metric: {key} = {value}")
        lowered = str(key).lower()
        if any(tok in lowered for tok in ("gpr",)):
            sources.add("gpr")
        elif any(tok in lowered for tok in ("vix", "gspc", "ixic", "dxy", "fedfunds", "cpiaucsl", "spot", "value", "change_pct")):
            sources.add("macro")
        else:
            sources.add("options")
    return retrieved, sorted(sources)


def _event_payload(
    *,
    case: Dict[str, Any],
    node: str,
    delta: Dict[str, Any],
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> Dict[str, Any]:
    node_audit_event = None
    for ev in delta.get("node_audit_log") or []:
        if isinstance(ev, dict) and ev.get("node") == node:
            node_audit_event = ev
    feedback = _serialise_feedback(delta.get("critic_feedback") or [])
    checker_feedback = [fb for fb in feedback if str(fb.get("sender", "")).lower() == "checker"]
    critic_feedback = [fb for fb in feedback if str(fb.get("sender", "")).lower() == "critic"]
    merged_delta_state = dict(before)
    _merge_state(merged_delta_state, delta)
    card_delta = _normalized_finalizer_input_card(merged_delta_state) if delta.get("finalizer_input_card") or delta.get("recommendation_mode") or delta.get("actionability_mode") or delta.get("structure_visibility_mode") or delta.get("revision_constraints") else {}
    card_after = _normalized_finalizer_input_card(after)
    return {
        "step": "node_update",
        "ts": datetime.now().isoformat(),
        "case_id": case.get("case_id"),
        "case_name": case.get("name"),
        "query": case.get("query"),
        "node": node,
        "delta_keys": sorted(delta.keys()),
        "before": _snapshot_summary(before),
        "after": _snapshot_summary(after),
        "node_audit_event": _to_jsonable(node_audit_event),
        "analyst_draft": delta.get("draft_report"),
        "checker_feedback": checker_feedback,
        "critic_feedback": critic_feedback,
        "checker_edit_suggestions": _to_jsonable(delta.get("checker_edit_suggestions")),
        "critic_edit_suggestions": _to_jsonable(delta.get("critic_edit_suggestions")),
        "critic_minor_suggestions": _to_jsonable(delta.get("critic_minor_suggestions")),
        "finalizer_input_card_delta": _to_jsonable(card_delta),
        "finalizer_input_card_after": _to_jsonable(card_after),
        "recommendation_mode_before": _normalized_finalizer_input_card(before).get("recommendation_mode") or before.get("recommendation_mode"),
        "recommendation_mode_after": card_after.get("recommendation_mode") or after.get("recommendation_mode"),
        "minor_edits_before_n": len(_normalized_finalizer_input_card(before).get("minor_edits") or []),
        "minor_edits_after_n": len(card_after.get("minor_edits") or []),
        "final_strategy_preview": _to_jsonable(delta.get("final_strategy"), max_str=4000),
    }


def _build_histories(node_events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    analyst_history = []
    checker_history = []
    critic_history = []
    finalizer_card_history = []
    adoption_trace = []
    for ev in node_events:
        node = ev.get("node")
        if node == "analyst":
            analyst_history.append(
                {
                    "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                    "draft": ev.get("analyst_draft"),
                    "after": ev.get("after"),
                }
            )
            if ev.get("finalizer_input_card_delta"):
                finalizer_card_history.append(
                    {
                        "node": "analyst",
                        "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                        "card": ev.get("finalizer_input_card_delta"),
                    }
                )
        elif node == "checker":
            checker_history.append(
                {
                    "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                    "verdict": _obj_get(ev.get("node_audit_event") or {}, "verdict"),
                    "checker_feedback": ev.get("checker_feedback"),
                    "checker_edit_suggestions": ev.get("checker_edit_suggestions"),
                    "before": ev.get("before"),
                    "after": ev.get("after"),
                }
            )
            adoption_trace.append(
                {
                    "node": "checker",
                    "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                    "verdict": _obj_get(ev.get("node_audit_event") or {}, "verdict"),
                    "feedback_n": len(ev.get("checker_feedback") or []),
                    "minor_edits_before_n": ev.get("minor_edits_before_n"),
                    "minor_edits_after_n": ev.get("minor_edits_after_n"),
                }
            )
        elif node == "critic":
            critic_history.append(
                {
                    "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                    "verdict": _obj_get(ev.get("node_audit_event") or {}, "verdict"),
                    "critic_feedback": ev.get("critic_feedback"),
                    "critic_minor_suggestions": ev.get("critic_minor_suggestions"),
                    "critic_edit_suggestions": ev.get("critic_edit_suggestions"),
                    "recommendation_mode_before": ev.get("recommendation_mode_before"),
                    "recommendation_mode_after": ev.get("recommendation_mode_after"),
                    "minor_edits_before_n": ev.get("minor_edits_before_n"),
                    "minor_edits_after_n": ev.get("minor_edits_after_n"),
                    "finalizer_input_card_after": ev.get("finalizer_input_card_after"),
                }
            )
            if ev.get("finalizer_input_card_after"):
                finalizer_card_history.append(
                    {
                        "node": "critic",
                        "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                        "card": ev.get("finalizer_input_card_after"),
                    }
                )
            adoption_trace.append(
                {
                    "node": "critic",
                    "revision_n": _obj_get(ev.get("node_audit_event") or {}, "revision_n"),
                    "verdict": _obj_get(ev.get("node_audit_event") or {}, "verdict"),
                    "feedback_n": len(ev.get("critic_feedback") or []),
                    "minor_suggestions_n": len(ev.get("critic_minor_suggestions") or []),
                    "recommendation_mode_before": ev.get("recommendation_mode_before"),
                    "recommendation_mode_after": ev.get("recommendation_mode_after"),
                    "minor_edits_before_n": ev.get("minor_edits_before_n"),
                    "minor_edits_after_n": ev.get("minor_edits_after_n"),
                }
            )
    return {
        "analyst_draft_history": analyst_history,
        "checker_feedback_history": checker_history,
        "critic_feedback_history_detailed": critic_history,
        "finalizer_input_card_history": finalizer_card_history,
        "adoption_trace": adoption_trace,
    }


def _compute_eval_batch_id(cases: Sequence[Dict[str, Any]], queries_file: Path) -> str:
    payload = {"queries_file": str(queries_file), "cases": [{"name": c.get("name"), "query": c.get("query")} for c in cases]}
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _load_cases(queries_file: Path, query: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str]:
    payload = _read_json(queries_file)
    cases = list(payload.get("cases", []))
    if query:
        cases = [case for case in cases if str(case.get("query")) == query]
    eval_batch_id = _compute_eval_batch_id(cases, queries_file)
    for case in cases:
        case["case_id"] = case.get("case_id") or f"{_slugify(case.get('name', 'case'))}_{hashlib.sha1(str(case.get('query')).encode('utf-8')).hexdigest()[:10]}"
        case["eval_batch_id"] = eval_batch_id
    return cases, eval_batch_id


class ProductionAuditLogger:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._audit_rows: List[Dict[str, Any]] = []
        self._node_rows: List[Dict[str, Any]] = []

    def log_audit(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now().isoformat())
        self._audit_rows.append(event)

    def log_node(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now().isoformat())
        self._node_rows.append(event)

    def flush(self) -> Dict[str, Path]:
        audit_path = self.run_dir / "production_audit.jsonl"
        node_path = self.run_dir / "production_node_events.jsonl"
        _write_jsonl(audit_path, self._audit_rows)
        _write_jsonl(node_path, self._node_rows)
        return {"audit": audit_path, "node_events": node_path}


class ProductionModel:
    def __init__(self) -> None:
        self.graph = build_financial_rag_graph()

    async def run_case(self, case: Dict[str, Any], audit: Optional[ProductionAuditLogger] = None) -> Dict[str, Any]:
        query = str(case.get("query") or "")
        merged_state: Dict[str, Any] = {"original_query": query}
        node_events: List[Dict[str, Any]] = []
        if audit:
            audit.log_audit({"step": "production_case_start", "case_name": case.get("name"), "case_id": case.get("case_id"), "query": query})
        async for update in self.graph.astream({"original_query": query}, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node, raw_delta in update.items():
                delta = _to_jsonable(raw_delta)
                before = dict(merged_state)
                _merge_state(merged_state, delta)
                after = dict(merged_state)
                event = _event_payload(case=case, node=str(node), delta=delta, before=before, after=after)
                node_events.append(event)
                if audit:
                    audit.log_node(event)
        row = _build_result_row(case, merged_state, node_events)
        if audit:
            audit.log_audit(
                {
                    "step": "production_case_complete",
                    "case_name": case.get("name"),
                    "case_id": case.get("case_id"),
                    "query": query,
                    "revision_count": row.get("revision_count"),
                    "finalizer_status": row.get("finalizer_status"),
                    "finalizer_trigger_node": row.get("finalizer_trigger_node"),
                    "trace_status": row.get("trace_status"),
                }
            )
        return row

    async def run_all_cases(
        self,
        *,
        queries_file: Path,
        query: Optional[str] = None,
        audit: Optional[ProductionAuditLogger] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        cases, eval_batch_id = _load_cases(queries_file, query=query)
        if audit:
            audit.log_audit(
                {
                    "step": "production_batch_start",
                    "queries_file": str(queries_file),
                    "n_cases": len(cases),
                    "eval_batch_id": eval_batch_id,
                }
            )
        results: List[Dict[str, Any]] = []
        for case in cases:
            try:
                results.append(await self.run_case(case, audit=audit))
            except Exception as exc:
                logger.exception("Production case failed: %s", case.get("name"))
                error_row = {
                    "case_id": case.get("case_id"),
                    "case_name": case.get("name"),
                    "query": case.get("query"),
                    "retrieved_contexts": [],
                    "answer": f"ERROR: {exc}",
                    "answer_for_eval": f"ERROR: {exc}",
                    "answer_rendered_markdown": "",
                    "analyst_draft": "",
                    "final_report": "",
                    "critic_feedback": [],
                    "checker_verdict": None,
                    "critic_verdict": None,
                    "checker_verdict_history": [],
                    "critic_verdict_history": [],
                    "critic_minor_suggestions": [],
                    "finalizer_status": None,
                    "finalizer_degraded_reason": None,
                    "finalizer_confidence": None,
                    "finalizer_trigger_node": None,
                    "revision_count": 0,
                    "time_range": {},
                    "time_window_label": None,
                    "retrieved_sources": [],
                    "gold_context": [],
                    "node_audit_log": [],
                    "node_snapshots": [],
                    "trace_status": "error",
                    "ground_truth": (case.get("ground_truth") or "").strip(),
                    "expected_sources": case.get("expected_sources", []),
                    "expected_time_window": case.get("expected_time_window"),
                    "case_meta": case.get("case_meta", {}),
                    "eval_batch_id": case.get("eval_batch_id"),
                    "model_type": "production",
                    "error": str(exc),
                }
                results.append(error_row)
                if audit:
                    audit.log_audit({"step": "production_case_error", "case_name": case.get("name"), "case_id": case.get("case_id"), "error": str(exc)})
        if audit:
            audit.log_audit(
                {
                    "step": "production_batch_complete",
                    "n_results": len(results),
                    "n_errors": sum(1 for row in results if row.get("error")),
                    "eval_batch_id": eval_batch_id,
                }
            )
        return results, {"queries_file": str(queries_file), "n_cases": len(cases), "eval_batch_id": eval_batch_id}


def _build_result_row(case: Dict[str, Any], state: Dict[str, Any], node_events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    fs = _as_mapping(state.get("final_strategy") or {})
    final_report_struct = _as_mapping(_obj_get(fs, "final_report", {}) or {})
    final_md = str(_obj_get(fs, "markdown", "") or "")
    draft = str(state.get("draft_report") or "")
    answer = _finalizer_plaintext_answer(final_report_struct, markdown=final_md, fallback=draft)
    gold_context = state.get("gold_context") or []
    gold_texts, gold_context_raw, gold_sources = _coerce_gold_texts(gold_context)
    silver_context = _as_mapping(state.get("silver_context_frozen") or state.get("silver_context") or {})
    silver_texts, silver_sources = _coerce_silver_texts(silver_context)
    retrieved_contexts = list(gold_texts) + list(silver_texts)
    metadata = _as_mapping(state.get("metadata") or {})
    retrieval_outcome = _as_mapping(state.get("retrieval_outcome") or {})
    outcome_sources = [_normalise_source_name(x) for x in (retrieval_outcome.get("strict_sources_hit") or []) + (retrieval_outcome.get("soft_sources_hit") or [])]
    retrieved_sources = sorted(set([src for src in outcome_sources + gold_sources + silver_sources if src]))
    requested_sources = [_normalise_source_name(x) for x in metadata.get("source_types") or []]
    actual_gold_sources = sorted(set(gold_sources))
    node_audit_log = _to_jsonable(state.get("node_audit_log") or [])
    node_counts: Dict[str, int] = {}
    for ev in node_audit_log:
        if isinstance(ev, dict) and ev.get("node"):
            node_counts[str(ev.get("node"))] = node_counts.get(str(ev.get("node")), 0) + 1
    last_pre_finalizer = None
    for ev in node_audit_log:
        if isinstance(ev, dict) and ev.get("node") and ev.get("node") != "finalizer":
            last_pre_finalizer = ev.get("node")
    histories = _build_histories(node_events)
    raw_finalizer_card = state.get("finalizer_input_card")
    normalized_card = _normalized_finalizer_input_card(state) if raw_finalizer_card is not None else None
    row = {
        "case_id": case.get("case_id"),
        "case_name": case.get("name", ""),
        "query": case.get("query", ""),
        "retrieved_contexts": retrieved_contexts,
        "answer": answer,
        "answer_for_eval": answer,
        "answer_rendered_markdown": final_md,
        "analyst_draft": draft,
        "analyst_draft_history": histories["analyst_draft_history"],
        "final_report": final_md,
        "final_report_struct": _to_jsonable(final_report_struct),
        "finalizer_input_card": _to_jsonable(normalized_card) if normalized_card is not None else None,
        "finalizer_input_card_history": histories["finalizer_input_card_history"],
        "critic_feedback": _to_jsonable(state.get("critic_feedback") or []),
        "checker_feedback_history": histories["checker_feedback_history"],
        "critic_feedback_history_detailed": histories["critic_feedback_history_detailed"],
        "checker_edit_suggestions": _to_jsonable(state.get("checker_edit_suggestions")),
        "critic_edit_suggestions": _to_jsonable(state.get("critic_edit_suggestions")),
        "critic_minor_suggestions": _to_jsonable(state.get("critic_minor_suggestions") or []),
        "checker_verdict": state.get("checker_verdict"),
        "critic_verdict": state.get("critic_verdict"),
        "checker_verdict_history": [str(ev.get("verdict")) for ev in node_audit_log if isinstance(ev, dict) and ev.get("node") == "checker" and ev.get("verdict")],
        "critic_verdict_history": [str(ev.get("verdict")) for ev in node_audit_log if isinstance(ev, dict) and ev.get("node") == "critic" and ev.get("verdict")],
        "finalizer_status": _obj_get(fs, "status"),
        "finalizer_degraded_reason": _obj_get(fs, "degraded_reason"),
        "finalizer_confidence": _obj_get(fs, "confidence_score"),
        "finalizer_trigger_node": last_pre_finalizer,
        "revision_count": state.get("revision_count"),
        "time_range": _to_jsonable(state.get("time_range") or {}),
        "time_window_label": _obj_get(state.get("time_range") or {}, "time_window_label"),
        "retrieved_sources": retrieved_sources,
        "requested_sources": requested_sources,
        "actual_gold_sources": actual_gold_sources,
        "gold_context": gold_context_raw,
        "gold_fallback_triggered": bool(state.get("is_fallback")),
        "gold_fallback_tier": "fallback" if state.get("is_fallback") else "strict",
        "gold_results_count": len(gold_context_raw),
        "scope_contract": _to_jsonable(state.get("scope_contract") or {}),
        "retrieval_outcome": _to_jsonable(retrieval_outcome),
        "node_audit_log": node_audit_log,
        "node_snapshots": node_events,
        "adoption_trace": histories["adoption_trace"],
        "node_counts": node_counts,
        "trace_status": "complete" if final_md or final_report_struct else "incomplete_no_finalizer",
        "ground_truth": (case.get("ground_truth") or "").strip(),
        "expected_sources": case.get("expected_sources", []),
        "expected_time_window": case.get("expected_time_window"),
        "case_meta": case.get("case_meta", {}),
        "eval_batch_id": case.get("eval_batch_id"),
        "model_type": "production",
    }
    if state.get("recommendation_mode") is not None:
        row["recommendation_mode"] = state.get("recommendation_mode")
    if state.get("actionability_mode") is not None:
        row["actionability_mode"] = state.get("actionability_mode")
    if state.get("structure_visibility_mode") is not None:
        row["structure_visibility_mode"] = state.get("structure_visibility_mode")
    if state.get("revision_constraints") is not None:
        row["revision_constraints"] = _to_jsonable(state.get("revision_constraints"))
    if not final_md and not final_report_struct:
        row["error"] = "no_finalizer_output"
    return row


def _answer_export_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "case_id": row.get("case_id"),
        "case_name": row.get("case_name"),
        "query": row.get("query"),
        "answer": row.get("answer"),
        "answer_for_eval": row.get("answer_for_eval"),
        "retrieved_contexts": row.get("retrieved_contexts"),
        "source_coverage": None,
        "time_window": row.get("time_window_label"),
        "trace_status": row.get("trace_status"),
        "finalizer_status": row.get("finalizer_status"),
        "revision_count": row.get("revision_count"),
        "answer_rendered_markdown": row.get("answer_rendered_markdown"),
        "finalizer_confidence": row.get("finalizer_confidence"),
        "gold_fallback_triggered": row.get("gold_fallback_triggered"),
        "requested_sources": row.get("requested_sources"),
        "actual_gold_sources": row.get("actual_gold_sources"),
    }


def write_results_bundle(run_dir: Path, results: Sequence[Dict[str, Any]], summary: Dict[str, Any], audit: Optional[ProductionAuditLogger] = None) -> Dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    full_path = run_dir / "production_results_full.jsonl"
    answers_path = run_dir / "production_answers.jsonl"
    summary_path = run_dir / "production_summary.json"
    _write_jsonl(full_path, results)
    _write_jsonl(answers_path, [_answer_export_row(row) for row in results])
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    outputs = {"results_full": full_path, "answers": answers_path, "summary": summary_path}
    if audit:
        outputs.update(audit.flush())
    return outputs


async def _async_main(query: Optional[str], queries_file: Path, run_dir: Path) -> Dict[str, Path]:
    audit = ProductionAuditLogger(run_dir)
    model = ProductionModel()
    results, summary = await model.run_all_cases(queries_file=queries_file, query=query, audit=audit)
    return write_results_bundle(run_dir, results, summary, audit=audit)


def run_production_bundle(
    *,
    queries_file: Path = DEFAULT_TRUTH_FILE,
    run_dir: Path,
    query: Optional[str] = None,
) -> Dict[str, Path]:
    """
    Fresh-run helper for the evaluation orchestrator.

    Produces:
      - production_results_full.jsonl
      - production_answers.jsonl
      - production_audit.jsonl
      - production_node_events.jsonl
      - production_summary.json
    """
    return asyncio.run(_async_main(query=query, queries_file=queries_file, run_dir=run_dir))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the production multi-node graph with detailed audit logging.")
    parser.add_argument("--queries-file", type=str, default=str(DEFAULT_TRUTH_FILE), help="Truth/query catalogue JSON.")
    parser.add_argument("--query", type=str, default=None, help="Run a single query exactly matching a case query.")
    parser.add_argument("--out-dir", type=str, default=None, help="Output directory. Default: logs/agentic_eval/YYYY-MM-DD/<timestamp>.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.out_dir:
        run_dir = Path(args.out_dir)
    else:
        day = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = DEFAULT_OUTPUT_ROOT / day / ts

    outputs = asyncio.run(_async_main(args.query, Path(args.queries_file), run_dir))
    print("Production evaluation run generated")
    print(f"  results: {outputs['results_full']}")
    print(f"  answers: {outputs['answers']}")
    print(f"  audit:   {outputs['audit']}")
    print(f"  nodes:   {outputs['node_events']}")


if __name__ == "__main__":
    main()
