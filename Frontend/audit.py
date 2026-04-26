from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROJECT_ROOT
from .contracts import normalize_state

try:
    from Scripts.observability.audit import audit_path, current_run_id
except Exception:  # pragma: no cover - frontend should still log in degraded envs
    audit_path = None
    current_run_id = None


def _day_dir() -> Path:
    day = datetime.utcnow().strftime("%Y-%m-%d")
    if audit_path is not None:
        try:
            p = audit_path("frontend_query", filename="placeholder.jsonl", scoped_by_run=False)
            return p.parent
        except Exception:
            pass
    log_dir = PROJECT_ROOT / "logs" / "frontend_query" / day
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_effective_run_id() -> str:
    if callable(current_run_id):
        try:
            rid = current_run_id()
            if rid:
                return str(rid)
        except Exception:
            pass
    return f"frontend_local_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"


def _slugify(text: str, max_len: int = 80) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower()).strip("_")
    if not base:
        return "query"
    return base[:max_len].strip("_")


def _derive_query_tag(query: str, final_state: Dict[str, Any]) -> str:
    metadata = (final_state or {}).get("metadata") or {}
    event_keyword = str(metadata.get("event_keyword", "") or "").strip().lower()
    if event_keyword:
        return _slugify(event_keyword)
    return _slugify(query, max_len=64)


def _to_jsonable(obj: Any, max_str: int = 2000) -> Any:
    """Match Scripts/tests/test_router_e2e.py serialization semantics."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + f"...(+{len(obj) - max_str} chars)"
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, max_str) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x, max_str) for x in obj]
    try:
        return str(obj)[:max_str]
    except Exception:
        return f"<unserialisable {type(obj).__name__}>"


def _node_level_audit(node_name: str, merged_state: Dict[str, Any]) -> Dict[str, Any]:
    """Frontend mirror of node-level audit shape used in router_e2e traces."""
    final_strategy = (merged_state or {}).get("final_strategy") or {}
    final_report = final_strategy.get("final_report") or {}
    audit: Dict[str, Any] = {"node": node_name}
    if node_name == "retrieval_master":
        silver = (merged_state.get("silver_context") or {}).get("values", {}) if isinstance(merged_state, dict) else {}
        gold = merged_state.get("gold_context") or []
        tr = merged_state.get("time_range") or {}
        audit.update({
            "gold_chunks": len(gold),
            "silver_values_n": len(silver),
            "time_range_start": tr.get("start_date"),
            "time_range_end": tr.get("end_date"),
            "is_fallback": bool(merged_state.get("is_fallback", False)),
        })
    elif node_name == "analyst":
        draft = str(merged_state.get("draft_report", "") or "")
        audit.update({
            "draft_len": len(draft),
            "draft_preview": draft[:240],
            "revision_count_after": merged_state.get("revision_count"),
        })
    elif node_name == "checker":
        audit.update({
            "checker_verdict": merged_state.get("checker_verdict"),
            "total_feedback_after_checker": len(merged_state.get("critic_feedback") or []),
        })
    elif node_name == "critic":
        audit.update({
            "critic_verdict": merged_state.get("critic_verdict"),
            "total_feedback_after_critic": len(merged_state.get("critic_feedback") or []),
        })
    elif node_name == "finalizer":
        audit.update({
            "final_status": final_strategy.get("status"),
            "confidence_score": final_strategy.get("confidence_score"),
            "final_report_keys": sorted(final_report.keys()) if isinstance(final_report, dict) else [],
        })
    return audit


def _build_trace_records(
    *,
    query: str,
    test_name: str,
    run_ts: str,
    trace_events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    started = time.time()
    records.append({
        "_meta": {
            "test_idx": 1,
            "test_name": test_name,
            "query": query,
            "run_ts": run_ts,
            "started_at": datetime.utcnow().isoformat(),
            "source": "frontend_stream",
        }
    })

    step = 0
    for evt in trace_events:
        step += 1
        node = str(evt.get("node", "pipeline"))
        state = evt.get("state") or {}
        record = {
            "step": step,
            "timestamp": datetime.utcnow().isoformat(),
            "elapsed_s": round(time.time() - started, 3),
            "node": node,
            "event": evt.get("event"),
            "route": evt.get("route"),
            "delta_keys": sorted(state.keys()) if isinstance(state, dict) else [],
            "audit": _to_jsonable(_node_level_audit(node, state)),
            "assertion_failures": [],
            "delta_preview": _to_jsonable(state),
        }
        records.append(record)
    return records


def _build_summary_payload(
    query: str,
    final_state: Dict[str, Any],
    trace_records: List[Dict[str, Any]],
    base_name: str,
    test_name: str,
) -> Dict[str, Any]:
    normalized = normalize_state(final_state or {})
    final_strategy = (final_state or {}).get("final_strategy") or normalized.get("final_strategy") or {}
    final_report = final_strategy.get("final_report") or normalized.get("final_report") or {}
    confidence = final_report.get("confidence_score", final_strategy.get("confidence_score"))

    run_id = get_effective_run_id()
    nodes_executed = [r.get("node") for r in trace_records if isinstance(r, dict) and "node" in r]
    steps = len([r for r in trace_records if isinstance(r, dict) and r.get("step") is not None])
    elapsed_candidates = [
        float(r.get("elapsed_s", 0.0))
        for r in trace_records
        if isinstance(r, dict) and r.get("elapsed_s") is not None
    ]
    elapsed_s = round(max(elapsed_candidates), 3) if elapsed_candidates else 0.0

    return {
        "test_idx": 1,
        "test_name": test_name,
        "run_id": run_id,
        "query": query,
        "outcome": "ok" if final_state else "crashed",
        "steps": steps,
        "elapsed_s": elapsed_s,
        "revision_count": final_state.get("revision_count"),
        "is_fallback": final_state.get("is_fallback"),
        "final_status": final_strategy.get("status"),
        "confidence_score": confidence,
        "final_report_produced": bool(final_report),
        "evidence_links_n": len(final_strategy.get("evidence_links", []) or []),
        "total_feedback": len(final_state.get("critic_feedback", []) or []),
        "nodes_executed": nodes_executed,
        "assertion_failures_total": 0,
        "assertion_failures": [],
        "artefacts": {
            "trace": f"{base_name}_trace.jsonl",
            "final_state": f"{base_name}_final_state.json",
        },
        "frontend": {
            "query_tag": _derive_query_tag(query, final_state),
            "node_audit_count": len(normalized.get("node_audit_log") or []),
            "trace_record_count": len(trace_records),
        },
    }


def append_frontend_query_audit(event: Dict[str, Any]) -> None:
    log_dir = _day_dir()
    log_file = log_dir / "query_audit_trail.jsonl"

    payload = {
        "ts_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "run_id": event.get("run_id") or get_effective_run_id(),
        **event,
    }
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_frontend_query_bundle(
    *,
    query: str,
    final_state: Optional[Dict[str, Any]],
    trace_events: List[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Persist one frontend query bundle:
      - *_trace.jsonl
      - *_final_state.json
      - *_summary.json

    Returns file paths for caller-side telemetry.
    """
    log_dir = _day_dir()
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_state = final_state or {}
    tag = _derive_query_tag(query, safe_state)
    test_name = tag.replace("_", " ").title() or "Frontend Query"
    base_name = f"{ts}_01_{tag}"

    trace_fp = log_dir / f"{base_name}_trace.jsonl"
    final_state_fp = log_dir / f"{base_name}_final_state.json"
    summary_fp = log_dir / f"{base_name}_summary.json"

    trace_records = _build_trace_records(
        query=query,
        test_name=test_name,
        run_ts=ts,
        trace_events=trace_events,
    )
    with trace_fp.open("w", encoding="utf-8") as f:
        for rec in trace_records:
            f.write(json.dumps(_to_jsonable(rec), ensure_ascii=False) + "\n")

    with final_state_fp.open("w", encoding="utf-8") as f:
        json.dump(_to_jsonable(safe_state), f, ensure_ascii=False, indent=2)

    summary_payload = _build_summary_payload(
        query=query,
        final_state=safe_state,
        trace_records=trace_records,
        base_name=base_name,
        test_name=test_name,
    )
    with summary_fp.open("w", encoding="utf-8") as f:
        json.dump(summary_payload, f, ensure_ascii=False, indent=2)

    return {
        "trace": str(trace_fp),
        "final_state": str(final_state_fp),
        "summary": str(summary_fp),
    }
