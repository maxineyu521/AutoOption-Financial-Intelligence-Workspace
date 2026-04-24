"""
Output tree (one run):

    logs/router_e2e/
        2026-04-21/
            20260421_103015_console.log
            20260421_103015_run_summary.json
            20260421_103015_01_happy_path_trace.jsonl
            20260421_103015_01_happy_path_final_state.json
            20260421_103015_01_happy_path_summary.json
            20260421_103015_02_critic_trap_trace.jsonl
            ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# -----------------------------------------------------------------------------
# Path bootstrap — this lets the test run standalone (python -m, pytest, or
# `python Scripts/tests/test_router_e2e.py`) without external PYTHONPATH.
# -----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import BaseModel  # noqa: E402

from Scripts.agents.router import build_financial_rag_graph  # noqa: E402
from Scripts.agents.state import AgentFeedback  # noqa: E402


# =============================================================================
# 0. Constants & log paths
# =============================================================================

_VALID_VERDICTS = {"pass", "fatal", "minor", None}
_VALID_SEVERITIES = {"Fatal", "Minor"}
_REQUIRED_TIMERANGE_KEYS = {
    "time_window_label", "window_days", "anchor_date",
    "start_date", "end_date", "is_default_window_applied",
}
_REQUIRED_HYDE_KEYS = {"paragraph", "novel_tickers", "whitelisted_tickers"}
_REQUIRED_FINAL_KEYS = {"status", "final_report", "evidence_links", "confidence_score"}
_MIN_DRAFT_LEN = 120   # below this the draft is suspicious

# Larger than the worst-case path (retrieval + analyst×3 + checker×3 + critic×3
# + finalizer ≈ 11) so the circuit-breaker fires before the recursion limit.
_RECURSION_LIMIT = 20

_DATE_STR = datetime.now().strftime("%Y-%m-%d")
_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")

LOG_ROOT = PROJECT_ROOT / "logs" / "router_e2e" / _DATE_STR
LOG_ROOT.mkdir(parents=True, exist_ok=True)


# =============================================================================
# 1. Logging — dual sink (console + file) attached to the root logger so every
#    agent / retriever INFO line shows up in the audit artefact.
# =============================================================================

def _configure_logging() -> Path:
    console_log_path = LOG_ROOT / f"{_RUN_TS}_console.log"
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove any pre-existing handlers (pytest reruns / REPL), then attach ours.
    for h in list(root.handlers):
        root.removeHandler(h)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream_handler)

    file_handler = logging.FileHandler(console_log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)

    # Silence over-noisy HTTP stack; we only care about agent/retriever signal.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return console_log_path


logger = logging.getLogger("RouterE2E")


# =============================================================================
# 2. Serialisation helpers (Pydantic-safe)
# =============================================================================

def _to_jsonable(obj: Any, max_str: int = 2000) -> Any:
    """Recursively convert Pydantic models / dates / sets into JSON-safe data.

    max_str truncates individual string fields to keep trace files readable —
    draft_report can be multi-KB and we only need a fingerprint in the trace.
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + f"...(+{len(obj) - max_str} chars)"
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return _to_jsonable(obj.model_dump(), max_str)
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, max_str) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x, max_str) for x in obj]
    # Fallback: best-effort repr — never raise.
    try:
        return str(obj)[:max_str]
    except Exception:
        return f"<unserialisable {type(obj).__name__}>"


def _slugify(text: str, max_len: int = 40) -> str:
    """Filesystem-safe slug for the test case name."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:max_len] or "unnamed"


# =============================================================================
# 3. Per-node audit functions
# -----------------------------------------------------------------------------
# Each function receives the delta for the node (what LangGraph merged into
# state from THAT node's return) and the cumulative merged state assembled by
# the harness. It returns:
#     (audit_dict, assertion_failures)
# where assertion_failures is a list[str] — empty == clean node.
# =============================================================================

def _audit_retrieval_master(delta: Dict[str, Any], merged: Dict[str, Any]):
    audit: Dict[str, Any] = {}
    fails: List[str] = []

    meta = delta.get("metadata")
    tr = delta.get("time_range") or {}
    he = delta.get("hyde_anticipation") or {}
    silver = delta.get("silver_context") or {}
    gold = delta.get("gold_context") or []
    macro = delta.get("macro_context") or ""

    audit["metadata_present"] = meta is not None
    audit["gold_chunks"] = len(gold)
    audit["silver_values_n"] = len(silver.get("values", {}) or {})
    audit["silver_source_channel"] = silver.get("source_channel")
    audit["silver_has_compensation"] = "compensation" in silver
    audit["time_range_keys"] = sorted(tr.keys())
    audit["time_range_label"] = tr.get("time_window_label")
    audit["time_range_days"] = tr.get("window_days")
    audit["time_range_start"] = tr.get("start_date")
    audit["time_range_end"] = tr.get("end_date")
    audit["default_window_applied"] = tr.get("is_default_window_applied")
    audit["hyde_paragraph_len"] = len(he.get("paragraph", "")) if isinstance(he, dict) else 0
    audit["hyde_whitelisted"] = he.get("whitelisted_tickers") if isinstance(he, dict) else None
    audit["hyde_novel_tickers"] = he.get("novel_tickers") if isinstance(he, dict) else None
    audit["hyde_source_channel"] = he.get("source_channel") if isinstance(he, dict) else None
    audit["macro_context_bytes"] = len(macro)
    audit["is_fallback"] = bool(delta.get("is_fallback", False))
    audit["revision_count_reset_to_zero"] = delta.get("revision_count") == 0

    if meta is None:
        fails.append("retrieval_master: metadata is None (transformer failed)")
    missing_tr = _REQUIRED_TIMERANGE_KEYS - set(tr.keys())
    if missing_tr:
        fails.append(f"retrieval_master: time_range missing keys {sorted(missing_tr)}")
    if not isinstance(he, dict):
        fails.append("retrieval_master: hyde_anticipation is not a dict")
    else:
        missing_he = _REQUIRED_HYDE_KEYS - set(he.keys())
        if missing_he:
            fails.append(f"retrieval_master: hyde_anticipation missing keys {sorted(missing_he)}")
    if silver.get("source_channel") not in ("primary", "hyde_expansion", None):
        fails.append(f"retrieval_master: unexpected silver source_channel={silver.get('source_channel')}")
    if delta.get("revision_count") != 0:
        fails.append("retrieval_master: revision_count not initialised to 0")
    if delta.get("checker_verdict") is not None:
        fails.append("retrieval_master: checker_verdict should be None at graph entry")
    if delta.get("critic_verdict") is not None:
        fails.append("retrieval_master: critic_verdict should be None at graph entry")

    return audit, fails


def _audit_analyst(delta: Dict[str, Any], merged: Dict[str, Any]):
    audit: Dict[str, Any] = {}
    fails: List[str] = []

    draft = delta.get("draft_report") or ""
    rev = delta.get("revision_count")

    audit["draft_len"] = len(draft)
    audit["draft_preview"] = draft[:240]
    audit["revision_count_after"] = rev
    audit["checker_verdict_reset"] = delta.get("checker_verdict") is None
    audit["critic_verdict_reset"] = delta.get("critic_verdict") is None

    if not isinstance(rev, int) or rev < 1:
        fails.append(f"analyst: revision_count must be >=1, got {rev!r}")
    if len(draft) < _MIN_DRAFT_LEN:
        fails.append(f"analyst: draft too short ({len(draft)} chars < {_MIN_DRAFT_LEN})")
    if delta.get("checker_verdict") is not None:
        fails.append("analyst: must reset checker_verdict to None for the new revision")
    if delta.get("critic_verdict") is not None:
        fails.append("analyst: must reset critic_verdict to None for the new revision")

    return audit, fails


def _audit_checker(delta: Dict[str, Any], merged: Dict[str, Any]):
    audit: Dict[str, Any] = {}
    fails: List[str] = []

    verdict = delta.get("checker_verdict")
    new_fb = delta.get("critic_feedback", []) or []

    audit["checker_verdict"] = verdict
    audit["new_feedback_items"] = len(new_fb)
    audit["silver_context_refreshed"] = "silver_context" in delta

    senders: List[str] = []
    severities: List[str] = []
    rev_indices: List[Any] = []
    for f in new_fb:
        if isinstance(f, BaseModel):
            d = f.model_dump()
        elif isinstance(f, dict):
            d = f
        else:
            fails.append(f"checker: feedback item of unexpected type {type(f).__name__}")
            continue
        senders.append(d.get("sender"))
        severities.append(d.get("error_type"))
        rev_indices.append(d.get("revision_index"))

    audit["feedback_senders"] = senders
    audit["feedback_severities"] = severities
    audit["feedback_revision_indices"] = rev_indices

    if verdict not in _VALID_VERDICTS:
        fails.append(f"checker: verdict={verdict!r} not in {_VALID_VERDICTS}")
    for sv in severities:
        if sv not in _VALID_SEVERITIES:
            fails.append(f"checker: feedback severity {sv!r} not in {_VALID_SEVERITIES}")
    # Every feedback this node emitted should be stamped by the Checker.
    if any(s != "Checker" for s in senders):
        fails.append(f"checker: found non-Checker senders in its delta: {senders}")
    cur_rev = merged.get("revision_count", 0)
    if any(ri is None for ri in rev_indices):
        fails.append("checker: at least one feedback item has revision_index=None")
    elif any(ri != cur_rev for ri in rev_indices):
        fails.append(
            f"checker: feedback revision_index {rev_indices} does not match current revision {cur_rev}"
        )
    # Sanity: if verdict=="fatal" there MUST be at least one Fatal feedback to
    # justify the routing decision.
    if verdict == "fatal" and not any(s == "Fatal" for s in severities):
        # Fatal verdict without any Fatal entry → router will re-run analyst
        # on empty ammo. Architectural smell.
        fails.append("checker: verdict=fatal but no Fatal severity in the delta feedback")

    return audit, fails


def _audit_critic(delta: Dict[str, Any], merged: Dict[str, Any]):
    audit: Dict[str, Any] = {}
    fails: List[str] = []

    verdict = delta.get("critic_verdict")
    new_fb = delta.get("critic_feedback", []) or []

    audit["critic_verdict"] = verdict
    audit["new_feedback_items"] = len(new_fb)

    senders: List[str] = []
    severities: List[str] = []
    rev_indices: List[Any] = []
    for f in new_fb:
        if isinstance(f, BaseModel):
            d = f.model_dump()
        elif isinstance(f, dict):
            d = f
        else:
            fails.append(f"critic: feedback item of unexpected type {type(f).__name__}")
            continue
        senders.append(d.get("sender"))
        severities.append(d.get("error_type"))
        rev_indices.append(d.get("revision_index"))

    audit["feedback_senders"] = senders
    audit["feedback_severities"] = severities
    audit["feedback_revision_indices"] = rev_indices

    if verdict not in _VALID_VERDICTS:
        fails.append(f"critic: verdict={verdict!r} not in {_VALID_VERDICTS}")
    for sv in severities:
        if sv not in _VALID_SEVERITIES:
            fails.append(f"critic: feedback severity {sv!r} not in {_VALID_SEVERITIES}")
    if any(s != "Critic" for s in senders):
        fails.append(f"critic: found non-Critic senders in its delta: {senders}")
    cur_rev = merged.get("revision_count", 0)
    if any(ri is None for ri in rev_indices):
        fails.append("critic: at least one feedback item has revision_index=None")
    elif any(ri != cur_rev for ri in rev_indices):
        fails.append(
            f"critic: feedback revision_index {rev_indices} does not match current revision {cur_rev}"
        )
    if verdict == "fatal" and not any(s == "Fatal" for s in severities):
        fails.append("critic: verdict=fatal but no Fatal severity in the delta feedback")

    return audit, fails


def _audit_finalizer(delta: Dict[str, Any], merged: Dict[str, Any]):
    audit: Dict[str, Any] = {}
    fails: List[str] = []

    final = delta.get("final_strategy") or {}
    audit["final_status"] = final.get("status")
    audit["confidence_score"] = final.get("confidence_score")
    audit["evidence_links_n"] = len(final.get("evidence_links", []) or [])
    audit["has_markdown"] = bool(final.get("markdown"))
    audit["final_report_keys"] = sorted((final.get("final_report") or {}).keys())

    missing = _REQUIRED_FINAL_KEYS - set(final.keys())
    if missing:
        fails.append(f"finalizer: final_strategy missing keys {sorted(missing)}")
    if final.get("status") not in ("complete", "degraded"):
        fails.append(f"finalizer: status={final.get('status')!r} not in {{complete, degraded}}")
    cs = final.get("confidence_score")
    if not isinstance(cs, (int, float)) or not (0.0 <= float(cs) <= 1.0):
        fails.append(f"finalizer: confidence_score={cs!r} out of [0,1]")
    return audit, fails


NODE_AUDITORS = {
    "retrieval_master": _audit_retrieval_master,
    "analyst": _audit_analyst,
    "checker": _audit_checker,
    "critic": _audit_critic,
    "finalizer": _audit_finalizer,
}


# =============================================================================
# 4. Merged-state accumulator
# -----------------------------------------------------------------------------
# LangGraph's stream_mode="updates" yields ONLY the per-node delta. To validate
# assertions that need whole-state context (e.g. revision_count at the moment
# Checker writes its verdict) we maintain our own merged view, honouring the
# AgentState `operator.add` semantics for `critic_feedback`.
# =============================================================================

def _merge_delta(merged: Dict[str, Any], delta: Dict[str, Any]) -> None:
    for k, v in delta.items():
        if k == "critic_feedback":
            prior = merged.get("critic_feedback") or []
            merged["critic_feedback"] = list(prior) + list(v or [])
        else:
            merged[k] = v


# =============================================================================
# 5. Single-test runner
# =============================================================================

async def run_single_test(
    graph,
    test_idx: int,
    test_name: str,
    query: str,
    case_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute ONE LangGraph query; write trace/final_state/summary artefacts.

    Returns a compact summary dict that the caller aggregates into the run-level
    summary. Exceptions are captured, not re-raised, so a broken test case
    cannot abort the rest of the suite.
    """
    slug = f"{test_idx:02d}_{_slugify(test_name)}"
    trace_path = LOG_ROOT / f"{_RUN_TS}_{slug}_trace.jsonl"
    final_state_path = LOG_ROOT / f"{_RUN_TS}_{slug}_final_state.json"
    summary_path = LOG_ROOT / f"{_RUN_TS}_{slug}_summary.json"

    banner = f"{'=' * 80}\n🚀 [TEST {test_idx}] {test_name}\n❓ Query: {query}\n{'=' * 80}"
    logger.info("\n" + banner)

    t_start = time.time()
    merged_state: Dict[str, Any] = {"original_query": query}
    node_executions: List[Dict[str, Any]] = []
    step = 0
    total_failures: List[str] = []

    try:
        with open(trace_path, "w", encoding="utf-8") as trace_fp:
            # Header line — makes the JSONL self-describing for later replay.
            trace_fp.write(json.dumps({
                "_meta": {
                    "test_idx": test_idx,
                    "test_name": test_name,
                    "query": query,
                    "run_ts": _RUN_TS,
                    "recursion_limit": _RECURSION_LIMIT,
                    "started_at": datetime.now().isoformat(),
                    # Catalogue metadata (expected_sources, expected_time_window,
                    # notes, …) is propagated verbatim so post-mortem tooling
                    # can diff actual vs. expected without re-reading the JSON.
                    "case_meta": case_meta or {},
                },
            }, ensure_ascii=False) + "\n")
            trace_fp.flush()

            initial_state = {"original_query": query}
            async for output in graph.astream(
                initial_state,
                config={"recursion_limit": _RECURSION_LIMIT},
                stream_mode="updates",
            ):
                for node_name, state_update in output.items():
                    step += 1
                    _merge_delta(merged_state, state_update)

                    auditor = NODE_AUDITORS.get(node_name)
                    if auditor:
                        audit, fails = auditor(state_update, merged_state)
                    else:
                        audit, fails = {"note": "no auditor registered"}, []

                    total_failures.extend(f"[step {step}][{node_name}] {m}" for m in fails)

                    # Per-node console line — bounded, no PII leak.
                    verdict_str = ""
                    if node_name == "checker":
                        verdict_str = f" | verdict={audit.get('checker_verdict')}"
                    elif node_name == "critic":
                        verdict_str = f" | verdict={audit.get('critic_verdict')}"
                    elif node_name == "finalizer":
                        verdict_str = f" | status={audit.get('final_status')}"
                    elif node_name == "analyst":
                        verdict_str = f" | rev={audit.get('revision_count_after')}"
                    elif node_name == "retrieval_master":
                        verdict_str = (
                            f" | gold={audit.get('gold_chunks')}"
                            f" silver_vals={audit.get('silver_values_n')}"
                            f" tr={audit.get('time_range_start')}→{audit.get('time_range_end')}"
                        )
                    fail_tag = f"  ⚠️ FAILS={len(fails)}" if fails else ""
                    logger.info(
                        f"🟢 [Step {step:02d}] Node={node_name.upper()}{verdict_str}{fail_tag}"
                    )
                    if fails:
                        for m in fails:
                            logger.warning(f"   ↳ {m}")

                    record = {
                        "step": step,
                        "timestamp": datetime.now().isoformat(),
                        "elapsed_s": round(time.time() - t_start, 3),
                        "node": node_name,
                        "delta_keys": sorted(state_update.keys()),
                        "audit": _to_jsonable(audit),
                        "assertion_failures": fails,
                        "delta_preview": _to_jsonable(state_update),
                    }
                    node_executions.append(record)
                    trace_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
                    trace_fp.flush()

        elapsed = round(time.time() - t_start, 3)
        final_state_json = _to_jsonable(merged_state)

        with open(final_state_path, "w", encoding="utf-8") as fp:
            json.dump(final_state_json, fp, ensure_ascii=False, indent=2)

        final_strategy = merged_state.get("final_strategy") or {}
        summary = {
            "test_idx": test_idx,
            "test_name": test_name,
            "query": query,
            "outcome": "ok",
            "steps": step,
            "elapsed_s": elapsed,
            "revision_count": merged_state.get("revision_count"),
            "is_fallback": merged_state.get("is_fallback"),
            "final_status": final_strategy.get("status"),
            "confidence_score": final_strategy.get("confidence_score"),
            "final_report_produced": bool(final_strategy.get("final_report")),
            "evidence_links_n": len(final_strategy.get("evidence_links", []) or []),
            "total_feedback": len(merged_state.get("critic_feedback", []) or []),
            "nodes_executed": [r["node"] for r in node_executions],
            "assertion_failures_total": len(total_failures),
            "assertion_failures": total_failures,
            "artefacts": {
                "trace": str(trace_path),
                "final_state": str(final_state_path),
            },
        }
        with open(summary_path, "w", encoding="utf-8") as fp:
            json.dump(summary, fp, ensure_ascii=False, indent=2)

        verdict_icon = "✅" if not total_failures and final_strategy.get("final_report") else (
            "⚠️" if final_strategy.get("final_report") else "❌"
        )
        logger.info(
            f"{verdict_icon} [TEST {test_idx}] Done in {elapsed}s | rev={summary['revision_count']} "
            f"| final_status={summary['final_status']} | assertion_fails={len(total_failures)} | "
            f"artefacts={summary_path.name}"
        )
        return summary

    except Exception as e:
        elapsed = round(time.time() - t_start, 3)
        tb = traceback.format_exc()
        logger.error(f"❌ [TEST {test_idx}] CRASHED after {elapsed}s: {type(e).__name__}: {e}")
        crash_summary = {
            "test_idx": test_idx,
            "test_name": test_name,
            "query": query,
            "outcome": "crashed",
            "steps": step,
            "elapsed_s": elapsed,
            "error_type": type(e).__name__,
            "error_msg": str(e),
            "traceback": tb,
            "nodes_executed_before_crash": [r["node"] for r in node_executions],
            "assertion_failures_before_crash": total_failures,
        }
        with open(summary_path, "w", encoding="utf-8") as fp:
            json.dump(crash_summary, fp, ensure_ascii=False, indent=2)
        return crash_summary


# =============================================================================
# 6. Test catalogue & main
# =============================================================================

# Default path for the externalised catalogue. Overridable via the
# ROUTER_E2E_QUERIES env var for CI / ad-hoc runs without editing source.
_DEFAULT_QUERY_CATALOGUE = PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_queries.json"

# Inline fallback — mirrors the externalised file so the harness still
# runs if the JSON is missing or corrupt. Keep this list short; the
# JSON file is the source of truth for day-to-day edits.
_FALLBACK_TEST_CASES: List[Dict[str, str]] = [
    {
        "name": "Happy Path - Quant Query",
        "query": "What is the current Put/Call Ratio and IV Skew for AAPL?",
    },
    {
        "name": "Minimal Time Default - Six Months Fallback",
        "query": "Show me the options sentiment for SPY.",
    },
]


def _load_test_cases(path: Optional[Path] = None) -> List[Dict[str, str]]:
    """Load the query catalogue from JSON (source of truth) with a silent
    fallback to the inline list.

    Contract expected of the JSON:
        {"cases": [{"name": str, "query": str, ...}, ...]}

    Extra metadata (``expected_sources``, ``expected_time_window``,
    ``notes``) is passed through untouched so future assertions can key
    on it without schema changes here.
    """
    path = path or Path(os.environ.get("ROUTER_E2E_QUERIES", _DEFAULT_QUERY_CATALOGUE))
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
        cases = payload.get("cases") or []
        # Minimal sanity-check — each case must carry at least a name + query.
        cleaned = [
            c for c in cases
            if isinstance(c, dict) and c.get("name") and c.get("query")
        ]
        if not cleaned:
            logger.warning(
                "Catalogue %s parsed but contained no valid cases — "
                "falling back to inline defaults.", path,
            )
            return _FALLBACK_TEST_CASES
        logger.info("📂 Loaded %d query cases from %s", len(cleaned), path)
        return cleaned
    except FileNotFoundError:
        logger.warning("Catalogue %s not found — using inline fallback.", path)
        return _FALLBACK_TEST_CASES
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Catalogue %s is unreadable (%s) — using inline fallback.",
            path, exc,
        )
        return _FALLBACK_TEST_CASES


TEST_CASES: List[Dict[str, str]] = _load_test_cases()


async def main():
    console_log = _configure_logging()
    logger.info(f"📝 Run log directory: {LOG_ROOT}")
    logger.info(f"📝 Console mirror: {console_log}")
    logger.info(f"📝 Run timestamp: {_RUN_TS}")
    logger.info(
        "📂 Query catalogue: %s (%d cases)",
        os.environ.get("ROUTER_E2E_QUERIES", str(_DEFAULT_QUERY_CATALOGUE)),
        len(TEST_CASES),
    )

    # Build the graph ONCE — singleton agents are already cached inside router.
    graph = build_financial_rag_graph()

    run_summaries: List[Dict[str, Any]] = []
    for i, tc in enumerate(TEST_CASES, start=1):
        # Strip the two mandatory keys so `case_meta` only carries extras
        # (expected_sources, expected_time_window, notes, …) — keeps the
        # JSONL trace clean and the diff against the catalogue obvious.
        extras = {k: v for k, v in tc.items() if k not in ("name", "query")}
        s = await run_single_test(
            graph=graph,
            test_idx=i,
            test_name=tc["name"],
            query=tc["query"],
            case_meta=extras,
        )
        run_summaries.append(s)

    aggregate = {
        "run_ts": _RUN_TS,
        "date": _DATE_STR,
        "n_tests": len(run_summaries),
        "n_ok": sum(1 for s in run_summaries if s.get("outcome") == "ok"),
        "n_crashed": sum(1 for s in run_summaries if s.get("outcome") == "crashed"),
        "n_with_assertion_failures": sum(
            1 for s in run_summaries if s.get("assertion_failures_total", 0) > 0
        ),
        "tests": run_summaries,
        "log_root": str(LOG_ROOT),
    }
    aggregate_path = LOG_ROOT / f"{_RUN_TS}_run_summary.json"
    with open(aggregate_path, "w", encoding="utf-8") as fp:
        json.dump(_to_jsonable(aggregate), fp, ensure_ascii=False, indent=2)

    logger.info("=" * 80)
    logger.info(
        f"🏁 RUN COMPLETE | ok={aggregate['n_ok']} | crashed={aggregate['n_crashed']} "
        f"| with_assertion_failures={aggregate['n_with_assertion_failures']} | "
        f"aggregate={aggregate_path.name}"
    )
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
