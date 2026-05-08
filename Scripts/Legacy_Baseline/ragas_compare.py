"""
Scripts/Legacy_Baseline/ragas_compare.py

RAGAS Benchmarking Harness — Baseline vs Finalizer / Agentic Node Evaluation.

Measures the value-add of the full multi-agent Medallion pipeline against
the legacy single-pass baseline. The model-level comparison is intentionally
BaselineModel output vs the production Finalizer output, while the node-level
report quantifies Analyst, Checker, Critic, and Finalizer separately.

Metrics (README.md §5 — reference-free, no ground-truth required):
    ┌───────────────────────────────────────────┬────────────────────────────────────────────────┐
    │ Metric                                    │ What it captures                               │
    ├───────────────────────────────────────────┼────────────────────────────────────────────────┤
    │ Faithfulness                              │ Are all factual claims traceable to retrieved   │
    │                                           │ context? (hallucination guard)                  │
    ├───────────────────────────────────────────┼────────────────────────────────────────────────┤
    │ AnswerRelevancy                           │ Is the answer on-topic with the user's query?  │
    ├───────────────────────────────────────────┼────────────────────────────────────────────────┤
    │ LLMContextPrecisionWithoutReference       │ Are the most relevant chunks ranked first?     │
    │ (Context Precision — no ground truth)     │ (temporal relevance / metadata signal quality) │
    └───────────────────────────────────────────┴────────────────────────────────────────────────┘

Usage:
    # Full run: baseline fresh + production from latest logs
    python -m Scripts.Legacy_Baseline.ragas_compare

    # Skip re-running baseline, reuse existing results file
    python -m Scripts.Legacy_Baseline.ragas_compare \\
        --baseline-results logs/baseline/20260423_120000/baseline_results.jsonl

    # Run fresh for both (production via LangGraph)
    python -m Scripts.Legacy_Baseline.ragas_compare --mode fresh

Output:
    logs/ragas_eval/YYYYMMDD_HHMMSS/
        comparison_metrics.json    — BaselineModel vs Finalizer metric table
        comparison_report.md       — human-readable BaselineModel vs Finalizer summary
        ragas_audit.jsonl          — detailed per-case evaluation audit trail
        agentic_workflow_audit.jsonl   — node-level audit rows (JSONL)
        agentic_workflow_metrics.json  — aggregate node metrics
        agentic_workflow_report.md       — human-readable node summary
        agentic_workflow_eval_detailed.csv — per-query spreadsheet

Agentic workflow evaluation:
    Requires ``OPENAI_API_KEY`` and ``pip install langchain-openai openai``.
    Judge model defaults to ``gpt-4o`` (override with ``RAGAS_OPENAI_JUDGE_MODEL``).
    Optional per-case ``ground_truth`` in ``Scripts/tests/router_e2e_queries.json`` enables
    RAGAS AnswerCorrectness; otherwise Faithfulness + AnswerRelevancy are used for drafts.
    Status fields + ``node_audit_log`` are the fast path; trace logs remain the
    reproducible source of truth for ``--mode logs``.

    Use ``--agentic-status-only`` for the fast status path, or
    ``--no-agentic-nodes`` to skip node evaluation entirely.

Dependencies:
    pip install ragas langchain-ollama langchain-huggingface qdrant-client
    # Agentic node eval (optional): pip install langchain-openai openai
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Lazy dependency check
# ---------------------------------------------------------------------------
warnings.filterwarnings(
    "ignore",
    message=".*LangchainLLMWrapper.*deprecated.*",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message=".*LangchainEmbeddingsWrapper.*deprecated.*",
    category=DeprecationWarning,
)
try:
    from dotenv import load_dotenv
    from langchain_ollama import ChatOllama, OllamaEmbeddings
    from ragas import EvaluationDataset, evaluate, SingleTurnSample
    from ragas.metrics import (
        Faithfulness,
        AnswerRelevancy,
        LLMContextPrecisionWithoutReference,
    )
    try:
        from ragas.metrics import AnswerCorrectness  # type: ignore
    except ImportError:  # older ragas builds
        AnswerCorrectness = None  # type: ignore[misc, assignment]
    from ragas.llms import LangchainLLMWrapper
    from ragas.embeddings import LangchainEmbeddingsWrapper
except ImportError as _e:
    raise SystemExit(
        f"Missing dependency: {_e}\n"
        "Run: pip install ragas langchain-ollama langchain-huggingface"
    )

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_EVAL_LOG_ROOT = _PROJECT_ROOT / "logs" / "RAGAS"
_BASELINE_LOG_DIR = _PROJECT_ROOT / "logs" / "baseline"
_E2E_LOG_DIR = _PROJECT_ROOT / "logs" / "router_e2e"
_RETRIEVAL_LOG_ROOT = _PROJECT_ROOT / "logs" / "retrieval"
_QUERIES_FILE = _PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_queries.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("ragas_compare")


# ---------------------------------------------------------------------------
# RAGAS boundary adapters
# ---------------------------------------------------------------------------

def _as_mapping(obj: Any) -> Dict[str, Any]:
    """Best-effort mapping view for dicts, Pydantic models, and dataclasses."""
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    for method_name in ("model_dump", "dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                dumped = method()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                pass
    raw = getattr(obj, "__dict__", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict-like object without assuming ``.get`` exists."""
    mapping = _as_mapping(obj)
    if key in mapping:
        return mapping.get(key, default)
    return getattr(obj, key, default)


def _stringify_context_object(obj: Any) -> str:
    """Convert a retriever chunk or model object into the plain text RAGAS expects."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()

    mapping = _as_mapping(obj)
    for key in ("content", "text", "page_content", "chunk_text", "summary"):
        val = mapping.get(key) if mapping else getattr(obj, key, None)
        if val:
            return str(val).strip()

    # Some chunk models wrap the text under a nested document/payload field.
    for key in ("document", "payload"):
        nested = mapping.get(key) if mapping else getattr(obj, key, None)
        nested_text = _stringify_context_object(nested)
        if nested_text:
            return nested_text

    if mapping:
        try:
            return json.dumps(mapping, ensure_ascii=False, default=str).strip()
        except Exception:
            return str(mapping).strip()
    return str(obj).strip()


def _coerce_ragas_contexts(raw_contexts: Any) -> List[str]:
    """Return a clean ``List[str]`` suitable for SingleTurnSample.retrieved_contexts."""
    if raw_contexts is None:
        return []
    if isinstance(raw_contexts, (str, bytes)):
        items: List[Any] = [raw_contexts.decode("utf-8", errors="ignore") if isinstance(raw_contexts, bytes) else raw_contexts]
    elif isinstance(raw_contexts, dict):
        items = [raw_contexts]
    else:
        try:
            items = list(raw_contexts)
        except TypeError:
            items = [raw_contexts]

    out: List[str] = []
    for item in items:
        if isinstance(item, (list, tuple, set)):
            out.extend(_coerce_ragas_contexts(item))
            continue
        text = _stringify_context_object(item)
        if text:
            out.append(text)
    return out


def _source_from_chunk(chunk: Any) -> Optional[str]:
    meta = _obj_get(chunk, "metadata", {}) or {}
    return (
        _obj_get(chunk, "source_type")
        or _obj_get(meta, "source_type")
        or _obj_get(meta, "source")
        or _obj_get(chunk, "source")
    )


def _silver_values_from_context(silver_context: Any) -> Dict[str, Any]:
    values = _obj_get(silver_context, "values", {}) or {}
    values = _as_mapping(values) or values
    return values if isinstance(values, dict) else {}


def _finalizer_plaintext_answer(final_report: Any, markdown: str = "", fallback: str = "") -> str:
    """Build a judge-friendly plain-text answer from Finalizer structured output.

    RAGAS metrics should evaluate the semantic answer, not the frontend markdown
    chrome. Prefer the structured FinalReport fields when present, and fall back
    to markdown / analyst draft only if the structured fields are absent.
    """
    report = _as_mapping(final_report)
    parts: List[str] = []

    convo = str(_obj_get(report, "conversation_reply", "") or "").strip()
    if convo:
        return convo

    macro = str(_obj_get(report, "macro_summary", "") or "").strip()
    if macro:
        parts.append(f"Macro summary: {macro}")

    trade_ideas = _obj_get(report, "trade_ideas", []) or []
    if isinstance(trade_ideas, list):
        for idx, raw_idea in enumerate(trade_ideas, 1):
            idea = _as_mapping(raw_idea)
            if not idea:
                continue
            segments = [
                f"Idea {idx}:",
                str(_obj_get(idea, "option_strategy", "") or "").strip(),
                f"on {str(_obj_get(idea, 'ticker', '') or '').strip()}",
            ]
            market_outlook = str(_obj_get(idea, "market_outlook", "") or "").strip()
            if market_outlook:
                segments.append(f"({market_outlook})")
            detail = " ".join(s for s in segments if s).strip()
            if detail:
                parts.append(detail)

            strike = str(_obj_get(idea, "strike_details", "") or "").strip()
            expiry = str(_obj_get(idea, "expiration_date", "") or "").strip()
            risk_profile = str(_obj_get(idea, "risk_profile", "") or "").strip()
            if strike or expiry or risk_profile:
                attrs = []
                if strike:
                    attrs.append(f"strike={strike}")
                if expiry:
                    attrs.append(f"expiration={expiry}")
                if risk_profile:
                    attrs.append(f"risk={risk_profile}")
                parts.append("Trade details: " + ", ".join(attrs))

            rationale = str(_obj_get(idea, "rationale", "") or "").strip()
            if rationale:
                parts.append(f"Rationale: {rationale}")

            catalysts = _obj_get(idea, "catalysts", []) or []
            if isinstance(catalysts, list) and catalysts:
                catalyst_text = ", ".join(str(c).strip() for c in catalysts if str(c).strip())
                if catalyst_text:
                    parts.append(f"Catalysts: {catalyst_text}")

    risks = _obj_get(report, "key_risks_and_hedges", []) or []
    if isinstance(risks, list) and risks:
        risk_text = "; ".join(str(r).strip() for r in risks if str(r).strip())
        if risk_text:
            parts.append(f"Risks and hedges: {risk_text}")

    confidence = _obj_get(report, "confidence_score")
    if confidence is not None:
        try:
            parts.append(f"Confidence score: {float(confidence):.2f}")
        except (TypeError, ValueError):
            pass

    plain = "\n".join(p for p in parts if p).strip()
    if plain:
        return plain
    if markdown:
        return markdown.strip()
    return (fallback or "").strip()


def _silver_metric_contexts(values: Dict[str, Any], max_items: int = 20) -> List[str]:
    contexts: List[str] = []
    for idx, (key, value) in enumerate(values.items()):
        if idx >= max_items:
            break
        contexts.append(f"Silver metric: {key} = {value}")
    return contexts


def _load_retrieval_audit_index() -> Dict[str, Dict[str, Any]]:
    latest_dir: Optional[Path] = None
    if _RETRIEVAL_LOG_ROOT.exists():
        dated_dirs = [p for p in _RETRIEVAL_LOG_ROOT.iterdir() if p.is_dir()]
        if dated_dirs:
            latest_dir = sorted(dated_dirs)[-1]
    if latest_dir is None:
        return {}

    log_path = latest_dir / "retriever_audit_trail.jsonl"
    if not log_path.exists():
        return {}

    index: Dict[str, Dict[str, Any]] = {}
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                query = str(row.get("original_query") or "").strip()
                if not query:
                    continue
                index[query] = row
    except OSError:
        return {}
    return index


def _requested_sources_from_filter(filter_payload: Any) -> List[str]:
    out: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            key = str(node.get("key") or "")
            match = node.get("match")
            if key == "source_type" and isinstance(match, dict):
                any_values = match.get("any")
                if isinstance(any_values, list):
                    for value in any_values:
                        out.append(_normalise_source_name(value))
                elif match.get("value") is not None:
                    out.append(_normalise_source_name(match.get("value")))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(filter_payload)
    return sorted(set(x for x in out if x))


def enrich_rows_with_retrieval_audit(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    audit_index = _load_retrieval_audit_index()
    if not audit_index:
        return rows

    enriched: List[Dict[str, Any]] = []
    for row in rows:
        query = str(row.get("query") or "").strip()
        audit_row = audit_index.get(query) or {}
        merged = dict(row)
        if audit_row:
            filter_payload = audit_row.get("filter_applied")
            requested_sources = _requested_sources_from_filter(filter_payload)
            actual_gold_sources = sorted(set(
                _normalise_source_name(_source_from_chunk(chunk))
                for chunk in _as_list(row.get("gold_context"))
                if _source_from_chunk(chunk)
            ))
            if not actual_gold_sources:
                actual_gold_sources = sorted(
                    set(
                        src for src in (
                            _normalise_source_name(x) for x in _as_list(row.get("retrieved_sources"))
                        )
                        if src in {"sec", "news", "gpr"}
                    )
                )
            merged["gold_fallback_triggered"] = bool(audit_row.get("fallback_triggered"))
            merged["gold_fallback_tier"] = audit_row.get("fallback_tier")
            merged["gold_results_count"] = audit_row.get("results_count")
            merged["requested_sources"] = requested_sources
            merged["actual_gold_sources"] = actual_gold_sources
        enriched.append(merged)
    return enriched


def _slugify_case_label(text: str, max_len: int = 64) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return cleaned[:max_len] or "case"


def _stable_case_id(name: str, query: str) -> str:
    base = f"{(name or '').strip()}||{(query or '').strip()}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"{_slugify_case_label(name or query, max_len=32)}_{digest}"


def _compute_eval_batch_id(test_cases: List[Dict[str, Any]], queries_file: Path) -> str:
    payload = {
        "queries_file": str(queries_file),
        "cases": [
            {
                "case_id": c.get("case_id"),
                "name": c.get("name"),
                "query": c.get("query"),
            }
            for c in test_cases
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _ensure_fresh_event_loop() -> None:
    """Heal libraries that call ``asyncio.get_event_loop`` after a closed loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Event loop is closed")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _evaluate_with_event_loop_recovery(*, dataset: Any, metrics: List[Any]) -> Any:
    """Run RAGAS evaluate, retrying once if the active event loop was closed."""
    _ensure_fresh_event_loop()
    try:
        return evaluate(dataset=dataset, metrics=metrics)
    except RuntimeError as exc:
        if "event loop is closed" not in str(exc).lower():
            raise
        logger.warning("RAGAS evaluate hit a closed event loop; resetting loop and retrying once.")
        asyncio.set_event_loop(asyncio.new_event_loop())
        return evaluate(dataset=dataset, metrics=metrics)


def _run_async_fresh(coro: Any) -> Any:
    """Run an async coroutine and leave a usable loop for sync RAGAS calls."""
    result = asyncio.run(coro)
    asyncio.set_event_loop(asyncio.new_event_loop())
    return result


def _wrap_langchain_llm(llm: Any) -> Any:
    """Use the newest available RAGAS LangChain wrapper, falling back safely."""
    try:
        from ragas.integrations.langchain import LangchainLLMWrapper as NewLLMWrapper  # type: ignore

        return NewLLMWrapper(llm)
    except Exception:
        return LangchainLLMWrapper(llm)


def _wrap_langchain_embeddings(embeddings: Any) -> Any:
    """Use the newest available RAGAS LangChain embeddings wrapper when present."""
    try:
        from ragas.integrations.langchain import LangchainEmbeddingsWrapper as NewEmbWrapper  # type: ignore

        return NewEmbWrapper(embeddings)
    except Exception:
        return LangchainEmbeddingsWrapper(embeddings)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JUDGE_PROVIDER   = os.getenv("RAGAS_JUDGE_PROVIDER", "ollama").strip().lower()
JUDGE_LLM_MODEL  = os.getenv("RAGAS_JUDGE_MODEL",   "llama3:latest")
JUDGE_EMBED_MODEL = os.getenv("RAGAS_EMBED_MODEL",  "nomic-embed-text")
OLLAMA_BASE_URL  = os.getenv("OLLAMA_BASE_URL",      "http://localhost:11434")

# OpenAI judge (Analyst vs Finalizer / Checker metrics) — authoritative scoring path
OPENAI_JUDGE_MODEL    = os.getenv("RAGAS_OPENAI_JUDGE_MODEL", "gpt-4o")
OPENAI_EMBED_MODEL    = os.getenv("RAGAS_OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Medallion-style citation tags produced after Checker/Finalizer pressure
_CITATION_TAG_RE = re.compile(r"\[(?:Silver|Gold):[^\]]*\]", re.IGNORECASE)

# Metric thresholds for colour-coding
_PASS_THRESHOLD  = 0.60
_WARN_THRESHOLD  = 0.40


# ===========================================================================
# Section 1 — Audit Logger
# ===========================================================================

class AuditLogger:
    """Writes JSONL audit events to the run directory."""

    def __init__(self, run_dir: Path):
        run_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = run_dir
        self._audit_fh = open(run_dir / "ragas_audit.jsonl", "w", encoding="utf-8")
        logger.info(f"AuditLogger: writing to {run_dir}")

    def log(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self._audit_fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._audit_fh.flush()

    def close(self) -> None:
        self._audit_fh.close()


# ===========================================================================
# Section 2 — Production Model Result Extractor (from router_e2e trace logs)
# ===========================================================================

class ProductionLogExtractor:
    """
    Reads router_e2e trace JSONL files and extracts the data needed for RAGAS:
        - question             : from _meta.query
        - answer               : plain-text evaluation answer derived from FinalReport fields
        - analyst_draft        : last analyst draft captured immediately before the latest finalizer
        - final_report         : final_strategy markdown (empty if finalizer never ran)
        - critic_feedback      : merged append-only feedback (Checker + Critic) from all deltas
        - node_audit_log       : per-node status/latency/verdict events
        - retrieved_contexts   : gold_context texts + silver value summary

    Log path pattern:
        logs/router_e2e/YYYYMMDD/<run_ts>_<idx>_<case_slug>_trace.jsonl
    """

    @staticmethod
    def _slug_from_path(path: Path) -> str:
        parts = path.stem.replace("_trace", "").split("_")
        slug_parts = []
        skip_count = 0
        for part in parts:
            if skip_count < 2:
                skip_count += 1
                continue
            slug_parts.append(part)
        return "_".join(slug_parts)

    @staticmethod
    def _trace_has_finalizer(path: Path) -> bool:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        if json.loads(line).get("node") == "finalizer":
                            return True
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return False
        return False

    def find_latest_logs(self) -> List[Path]:
        """
        Find all candidate trace logs, newest first.
        """
        pattern = "**/*_trace.jsonl"
        all_logs = sorted(_E2E_LOG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        logger.info(f"ProductionLogExtractor: found {len(all_logs)} trace files under {_E2E_LOG_DIR}")
        return all_logs

    def _parse_trace(self, path: Path) -> Optional[Dict[str, Any]]:
        """Parse one JSONL trace file → result dict for RAGAS."""
        try:
            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception as exc:
            logger.warning(f"ProductionLogExtractor: failed to parse {path}: {exc}")
            return None

        if not lines:
            return None

        meta   = lines[0].get("_meta", {})
        query  = meta.get("query", "")
        case_name = meta.get("test_name", path.stem)
        case_id = meta.get("case_id") or _stable_case_id(case_name, query)
        eval_batch_id = meta.get("eval_batch_id")

        # Collect contexts from the first retrieval step
        gold_texts: List[str] = []
        silver_contexts: List[str] = []
        gold_context_raw: List[Any] = []

        merged_critic_feedback: List[Any] = []
        last_analyst_draft: str = ""
        analyst_before_final: str = ""
        final_markdown: str = ""
        final_plain_answer: str = ""
        node_audit_log: List[Dict[str, Any]] = []
        checker_verdict_history: List[str] = []
        critic_verdict_history: List[str] = []
        critic_minor_suggestions: List[str] = []
        finalizer_status: Optional[str] = None
        finalizer_degraded_reason: Optional[str] = None
        finalizer_confidence: Optional[float] = None
        finalizer_trigger_node: Optional[str] = None
        last_non_final_node: Optional[str] = None
        revision_count: Optional[int] = None
        time_range: Dict[str, Any] = {}
        retrieved_sources: List[str] = []

        for line in lines[1:]:
            node  = line.get("node", "")
            delta = _as_mapping(line.get("delta_preview", {}) or {})

            for ev in _obj_get(delta, "node_audit_log", []) or []:
                if isinstance(ev, dict):
                    node_audit_log.append(ev)

            checker_verdict = _obj_get(delta, "checker_verdict")
            if checker_verdict:
                checker_verdict_history.append(str(checker_verdict))

            critic_verdict = _obj_get(delta, "critic_verdict")
            if critic_verdict:
                critic_verdict_history.append(str(critic_verdict))

            if _obj_get(delta, "revision_count") is not None:
                try:
                    revision_count = int(_obj_get(delta, "revision_count"))
                except (TypeError, ValueError):
                    pass

            new_fb = _obj_get(delta, "critic_feedback")
            if new_fb:
                merged_critic_feedback.extend(list(new_fb))

            minor_notes = _obj_get(delta, "critic_minor_suggestions")
            if isinstance(minor_notes, list):
                critic_minor_suggestions = [str(x) for x in minor_notes]

            if node == "retrieval_master":
                gold_ctx = _obj_get(delta, "gold_context", [])
                gold_context_raw = list(gold_ctx or [])
                for g in gold_ctx:
                    src = _source_from_chunk(g)
                    if src:
                        retrieved_sources.append(str(src))
                    text = _stringify_context_object(g)
                    if text:
                        gold_texts.append(text)
                # Summarise silver values as a compact context string
                silver = _obj_get(delta, "silver_context", {})
                vals = _silver_values_from_context(silver)
                if vals:
                    retrieved_sources.append("options")
                    retrieved_sources.append("macro")
                    silver_contexts = _silver_metric_contexts(vals)
                if _as_mapping(_obj_get(delta, "time_range")):
                    time_range = _as_mapping(_obj_get(delta, "time_range"))

            if node == "analyst":
                draft = _obj_get(delta, "draft_report", "")
                if draft:
                    last_analyst_draft = draft

            if node == "finalizer":
                fs = _as_mapping(_obj_get(delta, "final_strategy", {}) or {})
                final_report = _as_mapping(_obj_get(fs, "final_report", {}) or {})
                md = _obj_get(fs, "markdown") or _obj_get(final_report, "macro_summary", "")
                plain = _finalizer_plaintext_answer(final_report, markdown=str(md or ""), fallback=last_analyst_draft)
                if md:
                    final_markdown = md
                    analyst_before_final = last_analyst_draft
                if plain:
                    final_plain_answer = plain
                finalizer_status = _obj_get(fs, "status") or finalizer_status
                finalizer_degraded_reason = _obj_get(fs, "degraded_reason") or finalizer_degraded_reason
                finalizer_confidence = _safe_float(_obj_get(fs, "confidence_score"))
                finalizer_trigger_node = last_non_final_node

            if node and node != "finalizer":
                last_non_final_node = node

        answer = final_plain_answer or final_markdown or last_analyst_draft

        # Assemble retrieved_contexts list for RAGAS
        retrieved_contexts: List[str] = []
        for t in _coerce_ragas_contexts(gold_texts):
            if t.strip():
                retrieved_contexts.append(t)
        retrieved_contexts.extend(silver_contexts)

        if not answer and not last_analyst_draft:
            logger.warning(f"ProductionLogExtractor: no answer found in {path.name}")
            return None

        analyst_for_nodes = analyst_before_final or last_analyst_draft
        node_counts = _node_counts(node_audit_log)
        trace_status = "complete" if final_markdown else "incomplete_no_finalizer"

        row = {
            "case_id":            case_id,
            "case_name":          case_name,
            "query":              query,
            "retrieved_contexts": retrieved_contexts,
            "answer":             answer,
            "answer_for_eval":    answer,
            "answer_rendered_markdown": final_markdown,
            "analyst_draft":      analyst_for_nodes,
            "final_report":       final_markdown,
            "critic_feedback":    merged_critic_feedback,
            "checker_verdict":    checker_verdict_history[-1] if checker_verdict_history else None,
            "critic_verdict":     critic_verdict_history[-1] if critic_verdict_history else None,
            "checker_verdict_history": checker_verdict_history,
            "critic_verdict_history":  critic_verdict_history,
            "critic_minor_suggestions": critic_minor_suggestions,
            "finalizer_status":   finalizer_status,
            "finalizer_degraded_reason": finalizer_degraded_reason,
            "finalizer_confidence": finalizer_confidence,
            "finalizer_trigger_node": finalizer_trigger_node,
            "revision_count":     revision_count,
            "time_range":         time_range,
            "time_window_label":  time_range.get("time_window_label"),
            "retrieved_sources":  sorted(set(_normalise_source_name(x) for x in retrieved_sources)),
            "gold_context":       gold_context_raw,
            "node_audit_log":     node_audit_log,
            "node_counts":        node_counts,
            "trace_status":       trace_status,
            "model_type":         "production",
            "log_file":           str(path),
            "trace_path":         str(path),
            "eval_batch_id":      eval_batch_id,
        }
        if not final_markdown:
            row["error"] = "no_finalizer_output"
        return row

    def load_all(self, test_cases: List[Dict], eval_batch_id: str, audit: Optional[AuditLogger] = None) -> List[Dict[str, Any]]:
        """Load production results for all test cases, preferring case_id + eval_batch_id."""
        case_logs = self.find_latest_logs()
        results: List[Dict[str, Any]] = []
        legacy_fallbacks = 0

        for case in test_cases:
            name = case.get("name", "")
            case_id = case.get("case_id")
            slug = _slugify_case_label(name, max_len=40)
            matched_path: Optional[Path] = None
            matched_reason = ""

            exact_candidates: List[Tuple[bool, float, Path]] = []
            legacy_candidates: List[Tuple[bool, float, Path]] = []
            for log_path in case_logs:
                try:
                    first = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
                except Exception:
                    continue
                meta = first.get("_meta", {}) if isinstance(first, dict) else {}
                log_case_id = meta.get("case_id")
                log_eval_batch_id = meta.get("eval_batch_id")
                log_test_name = str(meta.get("test_name") or "")
                log_slug = self._slug_from_path(log_path)
                candidate = (self._trace_has_finalizer(log_path), log_path.stat().st_mtime, log_path)

                if log_case_id == case_id and log_eval_batch_id == eval_batch_id:
                    exact_candidates.append(candidate)
                    continue

                if log_case_id == case_id and not log_eval_batch_id:
                    legacy_candidates.append(candidate)
                    continue

                if log_test_name == name:
                    legacy_candidates.append(candidate)
                    continue

                if slug in log_slug or log_slug in slug:
                    legacy_candidates.append(candidate)
                    continue

                slug_words = set(slug.split("_"))
                log_words  = set(log_slug.split("_"))
                if len(slug_words & log_words) >= 2:
                    legacy_candidates.append(candidate)

            if exact_candidates:
                exact_candidates = sorted(exact_candidates, key=lambda r: (r[0], r[1]), reverse=True)
                matched_path = exact_candidates[0][2]
                matched_reason = "case_id+eval_batch_id"
            elif legacy_candidates:
                legacy_candidates = sorted(legacy_candidates, key=lambda r: (r[0], r[1]), reverse=True)
                matched_path = legacy_candidates[0][2]
                matched_reason = "legacy_slug_fallback"
                legacy_fallbacks += 1

            if matched_path is None:
                logger.warning(
                    f"ProductionLogExtractor: no trace log matched case '{name}' (case_id='{case_id}'). "
                    "Skipping — run the E2E harness first or use --mode fresh."
                )
                results.append({
                    "case_id":            case_id,
                    "case_name":          name,
                    "query":              case.get("query", ""),
                    "retrieved_contexts": [],
                    "answer":             "MISSING — no trace log found for this case",
                    "analyst_draft":     "",
                    "final_report":       "",
                    "critic_feedback":    [],
                    "ground_truth":       (case.get("ground_truth") or "").strip(),
                    "expected_sources":   case.get("expected_sources", []),
                    "expected_time_window": case.get("expected_time_window"),
                    "case_meta":          case.get("case_meta", {}),
                    "model_type":         "production",
                    "eval_batch_id":      eval_batch_id,
                    "error":              "no_trace_log",
                })
                continue

            parsed = self._parse_trace(matched_path)
            if parsed is None:
                results.append({
                    "case_id":       case_id,
                    "case_name":   name,
                    "query":       case.get("query", ""),
                    "retrieved_contexts": [],
                    "answer":      "PARSE_ERROR",
                    "analyst_draft":  "",
                    "final_report":   "",
                    "critic_feedback": [],
                    "ground_truth":   (case.get("ground_truth") or "").strip(),
                    "expected_sources": case.get("expected_sources", []),
                    "expected_time_window": case.get("expected_time_window"),
                    "case_meta":      case.get("case_meta", {}),
                    "model_type":  "production",
                    "eval_batch_id": eval_batch_id,
                    "error":       "parse_failed",
                })
            else:
                logger.info(f"  ✅ Loaded production result for '{name}' from {matched_path.name} [{matched_reason}]")
                parsed["case_id"] = case_id
                parsed["eval_batch_id"] = parsed.get("eval_batch_id") or eval_batch_id
                parsed["ground_truth"] = (case.get("ground_truth") or "").strip()
                parsed["expected_sources"] = case.get("expected_sources", [])
                parsed["expected_time_window"] = case.get("expected_time_window")
                parsed["case_meta"] = case.get("case_meta", {})
                results.append(parsed)

        if audit and legacy_fallbacks:
            audit.log({
                "step": "production_extract_legacy_fallback",
                "count": legacy_fallbacks,
                "reason": "trace_meta_missing_case_id_or_eval_batch_id",
            })
        return results


# ===========================================================================
# Section 3 — RAGAS Evaluator
# ===========================================================================

class RAGASEvaluator:
    """
    Wraps the RAGAS evaluate() call with Ollama-based judge LLM & embeddings.
    Handles per-case granularity so each case's scores can be audit-logged.
    """

    def __init__(self):
        logger.info(
            "RAGASEvaluator: provider=%s | judge_llm=%s | judge_embed=%s",
            JUDGE_PROVIDER,
            JUDGE_LLM_MODEL,
            JUDGE_EMBED_MODEL,
        )

        if JUDGE_PROVIDER == "openai":
            try:
                from langchain_openai import ChatOpenAI, OpenAIEmbeddings
            except ImportError as exc:
                raise RuntimeError(
                    "RAGAS_JUDGE_PROVIDER=openai requires langchain-openai and openai. "
                    "Install: pip install langchain-openai openai"
                ) from exc
            if not os.getenv("OPENAI_API_KEY", "").strip():
                raise RuntimeError("RAGAS_JUDGE_PROVIDER=openai requires OPENAI_API_KEY.")
            _llm = ChatOpenAI(model=JUDGE_LLM_MODEL, temperature=0)
            _emb = OpenAIEmbeddings(model=JUDGE_EMBED_MODEL)
        else:
            _llm = ChatOllama(
                model=JUDGE_LLM_MODEL,
                base_url=OLLAMA_BASE_URL,
                temperature=0,
                num_predict=1024,
            )
            _emb = OllamaEmbeddings(
                model=JUDGE_EMBED_MODEL,
                base_url=OLLAMA_BASE_URL,
            )

        self._judge_llm  = _wrap_langchain_llm(_llm)
        self._judge_emb  = _wrap_langchain_embeddings(_emb)

        self._base_metric_names = [
            "faithfulness",
            "answer_relevancy",
            "llm_context_precision_without_reference",
        ]
        self._all_metric_names = [*self._base_metric_names, "answer_correctness"]

    def _metrics_for_case(self, has_reference: bool) -> Tuple[List[Any], List[str]]:
        """Build metric instances per case so reference-aware metrics only run when valid."""
        metrics: List[Any] = [
            Faithfulness(llm=self._judge_llm),
            AnswerRelevancy(llm=self._judge_llm, embeddings=self._judge_emb),
            LLMContextPrecisionWithoutReference(llm=self._judge_llm),
        ]
        names = list(self._base_metric_names)
        if has_reference and AnswerCorrectness is not None:
            try:
                metrics.append(AnswerCorrectness(llm=self._judge_llm))
            except TypeError:
                metrics.append(AnswerCorrectness(llm=self._judge_llm, embeddings=self._judge_emb))
            names.append("answer_correctness")
        return metrics, names

    def evaluate_batch(
        self,
        results: List[Dict[str, Any]],
        model_label: str,
        audit: AuditLogger,
    ) -> List[Dict[str, Any]]:
        """
        Run RAGAS on a batch of model results.

        Returns list of per-case score dicts:
            {case_name, query, faithfulness, answer_relevancy,
             llm_context_precision_without_reference, error}
        """
        logger.info(f"RAGASEvaluator: evaluating {len(results)} cases for model='{model_label}'")
        audit.log({
            "step": "ragas_batch_start",
            "model": model_label,
            "n_cases": len(results),
            "judge_provider": JUDGE_PROVIDER,
            "judge_llm": JUDGE_LLM_MODEL,
            "judge_embed": JUDGE_EMBED_MODEL,
            "metrics": self._all_metric_names,
            "reference_metric_enabled": AnswerCorrectness is not None,
        })

        scored: List[Dict[str, Any]] = []

        for i, res in enumerate(results):
            case_id   = res.get("case_id")
            case_name = res.get("case_name", f"case_{i+1}")
            query     = res.get("query", "")
            answer    = res.get("answer", "")
            contexts  = _coerce_ragas_contexts(res.get("retrieved_contexts", []))
            ground_truth = (res.get("ground_truth") or "").strip()
            contract_audit = evaluate_case_contract(res)

            logger.info(f"  [{i+1}/{len(results)}] RAGAS evaluating: '{case_name}' | model={model_label}")

            # Skip cases with missing data
            if res.get("error") or not answer or answer.startswith("MISSING") or answer.startswith("ERROR"):
                row = {
                    "case_id": case_id,
                    "case_name": case_name,
                    "query":     query,
                    "model":     model_label,
                    "faithfulness":                              None,
                    "answer_relevancy":                         None,
                    "llm_context_precision_without_reference":  None,
                    "answer_correctness":                       None,
                    "contract_audit": contract_audit,
                    "error": res.get("error", "empty_answer"),
                }
                scored.append(row)
                audit.log({"step": "ragas_case_skipped", "case": case_name, "model": model_label,
                           "reason": row["error"]})
                continue

            # Guard: RAGAS requires at least one non-empty context
            if not contexts:
                contexts = ["(no context retrieved)"]

            t0 = time.monotonic()
            try:
                has_reference = bool(ground_truth)
                metrics, metric_names = self._metrics_for_case(has_reference)
                sample_kwargs: Dict[str, Any] = {
                    "user_input": query,
                    "response": answer,
                    "retrieved_contexts": contexts,
                }
                if has_reference:
                    sample_kwargs["reference"] = ground_truth
                sample = SingleTurnSample(**sample_kwargs)
                dataset = EvaluationDataset(samples=[sample])
                result  = _evaluate_with_event_loop_recovery(dataset=dataset, metrics=metrics)
                scores_df = result.to_pandas()
                row_scores = scores_df.iloc[0].to_dict() if not scores_df.empty else {}

                latency_ms = (time.monotonic() - t0) * 1000

                row = {
                    "case_id": case_id,
                    "case_name": case_name,
                    "query":     query,
                    "model":     model_label,
                    "faithfulness":
                        _safe_float(row_scores.get("faithfulness")),
                    "answer_relevancy":
                        _safe_float(row_scores.get("answer_relevancy")),
                    "llm_context_precision_without_reference":
                        _safe_float(row_scores.get("llm_context_precision_without_reference")),
                    "answer_correctness":
                        _safe_float(row_scores.get("answer_correctness")),
                    "latency_ragas_ms": round(latency_ms, 1),
                    "context_count":    len(contexts),
                    "answer_len":       len(answer),
                    "ground_truth_present": has_reference,
                    "ground_truth_len": len(ground_truth),
                    "contract_audit": contract_audit,
                    "gold_fallback_triggered": res.get("gold_fallback_triggered"),
                    "gold_fallback_tier": res.get("gold_fallback_tier"),
                    "gold_results_count": res.get("gold_results_count"),
                    "requested_sources": res.get("requested_sources"),
                    "actual_gold_sources": res.get("actual_gold_sources"),
                    "error": None,
                }
                scored.append(row)

                audit.log({
                    "step":       "ragas_case_complete",
                    "case":       case_name,
                    "model":      model_label,
                    "scores":     {k: row.get(k) for k in self._all_metric_names},
                    "metrics_run": metric_names,
                    "contract_audit": contract_audit,
                    "latency_ms": round(latency_ms, 1),
                    "context_n":  len(contexts),
                    "answer_len": len(answer),
                    "answer_preview": answer[:200],
                    "contexts_preview": [c[:80] for c in contexts[:3]],
                })

                logger.info(
                    f"    faithfulness={_fmt(row['faithfulness'])} | "
                    f"relevancy={_fmt(row['answer_relevancy'])} | "
                    f"ctx_prec={_fmt(row['llm_context_precision_without_reference'])} | "
                    f"correctness={_fmt(row['answer_correctness'])} | "
                    f"latency={latency_ms:.0f}ms"
                )

            except Exception as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                logger.error(f"    ❌ RAGAS failed for '{case_name}': {exc}")
                row = {
                    "case_id": case_id,
                    "case_name": case_name,
                    "query":     query,
                    "model":     model_label,
                    "faithfulness":                              None,
                    "answer_relevancy":                         None,
                    "llm_context_precision_without_reference":  None,
                    "answer_correctness":                       None,
                    "latency_ragas_ms": round(latency_ms, 1),
                    "ground_truth_present": bool(ground_truth),
                    "ground_truth_len": len(ground_truth),
                    "contract_audit": contract_audit,
                    "error": str(exc),
                }
                scored.append(row)
                audit.log({"step": "ragas_case_error", "case": case_name, "model": model_label,
                           "error": str(exc), "latency_ms": round(latency_ms, 1)})

        audit.log({
            "step":    "ragas_batch_complete",
            "model":   model_label,
            "n_ok":    sum(1 for r in scored if r["error"] is None),
            "n_error": sum(1 for r in scored if r["error"] is not None),
        })
        return scored


# ===========================================================================
# Section 3b — Agentic workflow node metrics
# ===========================================================================


def count_medallion_citations(text: str) -> int:
    """Count ``[Silver: …]`` / ``[Gold: …]`` citation tags (case-insensitive)."""
    if not text:
        return 0
    return len(_CITATION_TAG_RE.findall(text))


def _feedback_sender(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("sender", "") or "")
    return str(getattr(item, "sender", "") or "")


def _feedback_error_type(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("error_type", "") or "")
    return str(getattr(item, "error_type", "") or "")


def _feedback_comment(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("comment", "") or "")
    return str(getattr(item, "comment", "") or "")


def _feedback_revision(item: Any) -> Optional[int]:
    raw = item.get("revision_index") if isinstance(item, dict) else getattr(item, "revision_index", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _feedback_items(feedback: Any) -> List[Any]:
    if not feedback:
        return []
    return list(feedback) if isinstance(feedback, (list, tuple)) else []


def _feedback_metrics(feedback: Any) -> Dict[str, Any]:
    """Split append-only feedback into Checker/Critic and Fatal/Minor counts."""
    items = _feedback_items(feedback)
    out: Dict[str, Any] = {
        "total": len(items),
        "checker_total": 0,
        "checker_fatal": 0,
        "checker_minor": 0,
        "critic_total": 0,
        "critic_fatal": 0,
        "critic_minor": 0,
        "other_total": 0,
        "first_checker_comment": None,
        "first_critic_comment": None,
        "feedback_revisions": sorted({r for r in (_feedback_revision(it) for it in items) if r is not None}),
    }
    for item in items:
        sender = _feedback_sender(item).strip().lower()
        severity = _feedback_error_type(item).strip().lower()
        comment = _feedback_comment(item)
        sender_key = sender if sender in ("checker", "critic") else "other"
        out[f"{sender_key}_total"] += 1
        if sender_key in ("checker", "critic"):
            if severity == "fatal":
                out[f"{sender_key}_fatal"] += 1
            elif severity == "minor":
                out[f"{sender_key}_minor"] += 1
            if comment and out[f"first_{sender_key}_comment"] is None:
                out[f"first_{sender_key}_comment"] = comment[:240]
    return out


def _node_counts(node_audit_log: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not isinstance(node_audit_log, list):
        return counts
    for ev in node_audit_log:
        if not isinstance(ev, dict):
            continue
        node = str(ev.get("node") or "")
        if node:
            counts[node] = counts.get(node, 0) + 1
    return counts


def _node_latency_summary(node_audit_log: Any) -> Dict[str, Dict[str, Optional[float]]]:
    buckets: Dict[str, List[float]] = {}
    if not isinstance(node_audit_log, list):
        return {}
    for ev in node_audit_log:
        if not isinstance(ev, dict):
            continue
        node = str(ev.get("node") or "")
        lat = _safe_float(ev.get("latency_ms"))
        if node and lat is not None:
            buckets.setdefault(node, []).append(lat)
    return {
        node: {
            "count": len(vals),
            "avg_latency_ms": round(sum(vals) / len(vals), 3) if vals else None,
            "max_latency_ms": round(max(vals), 3) if vals else None,
        }
        for node, vals in sorted(buckets.items())
    }


def _verdict_counts(values: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    vals = values if isinstance(values, list) else []
    for v in vals:
        key = str(v or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _finalizer_path_label(row: Dict[str, Any]) -> str:
    trigger = row.get("finalizer_trigger_node") or "unknown"
    checker_v = row.get("checker_verdict")
    critic_v = row.get("critic_verdict")
    status = row.get("finalizer_status") or "missing"
    revision_n = row.get("revision_count")
    return (
        f"trigger={trigger}; checker={checker_v or 'n/a'}; "
        f"critic={critic_v or 'n/a'}; finalizer={status}; revisions={revision_n if revision_n is not None else 'n/a'}"
    )


class AgenticNodeEvaluator:
    """
    Node-level evaluation.

    Fast path: status fields + node_audit_log quantify Analyst, Checker,
    Critic, and Finalizer without calling a judge model. Optional judge path:
    RAGAS on Analyst draft vs Finalizer report using OpenAI ``gpt-4o`` by
    default, plus Checker/Critic correction counts and citation lift.
    """

    def __init__(self, status_only: bool = False) -> None:
        self._status_only = status_only
        self._judge_llm = None
        self._judge_emb = None
        if status_only:
            logger.info("AgenticNodeEvaluator: status-only mode (no OpenAI judge calls).")
            return

        try:
            from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        except ImportError as exc:
            raise RuntimeError(
                "Agentic node evaluation requires langchain-openai. "
                "Install: pip install langchain-openai openai"
            ) from exc

        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY is not set.")

        _ollm = ChatOpenAI(model=OPENAI_JUDGE_MODEL, temperature=0)
        _oemb = OpenAIEmbeddings(model=OPENAI_EMBED_MODEL)
        self._judge_llm = _wrap_langchain_llm(_ollm)
        self._judge_emb = _wrap_langchain_embeddings(_oemb)
        logger.info(
            f"AgenticNodeEvaluator: OpenAI judge={OPENAI_JUDGE_MODEL} | embeddings={OPENAI_EMBED_MODEL}"
        )

    def _metrics_for_case(self, has_reference: bool) -> Tuple[List[Any], List[str]]:
        faith = Faithfulness(llm=self._judge_llm)
        if has_reference and AnswerCorrectness is not None:
            try:
                ac = AnswerCorrectness(llm=self._judge_llm)
            except TypeError:
                ac = AnswerCorrectness(llm=self._judge_llm, embeddings=self._judge_emb)
            return [faith, ac], ["faithfulness", "answer_correctness"]
        ar = AnswerRelevancy(llm=self._judge_llm, embeddings=self._judge_emb)
        return [faith, ar], ["faithfulness", "answer_relevancy"]

    def _evaluate_one_response(
        self,
        query: str,
        answer: str,
        contexts: List[str],
        ground_truth: str,
    ) -> Tuple[Dict[str, Optional[float]], List[str], Optional[str]]:
        if self._status_only:
            return {}, [], None
        has_ref = bool((ground_truth or "").strip())
        metrics, names = self._metrics_for_case(has_ref)
        ctx = _coerce_ragas_contexts(contexts)
        if not ctx:
            ctx = ["(no context retrieved)"]
        try:
            kw: Dict[str, Any] = {
                "user_input": query,
                "response": answer,
                "retrieved_contexts": ctx,
            }
            if has_ref and "answer_correctness" in names:
                kw["reference"] = ground_truth.strip()
            sample = SingleTurnSample(**kw)
            ds = EvaluationDataset(samples=[sample])
            result = _evaluate_with_event_loop_recovery(dataset=ds, metrics=metrics)
            scores_df = result.to_pandas()
            row_scores = scores_df.iloc[0].to_dict() if not scores_df.empty else {}
            out = {n: _safe_float(row_scores.get(n)) for n in names}
            return out, names, None
        except Exception as exc:
            return {n: None for n in names}, names, str(exc)

    def run(
        self,
        production_rows: List[Dict[str, Any]],
        run_dir: Path,
        audit: AuditLogger,
    ) -> Dict[str, Any]:
        agentic_jsonl = run_dir / "agentic_workflow_audit.jsonl"
        detailed: List[Dict[str, Any]] = []
        n_queries = len(production_rows)
        total_feedback_items = 0
        total_checker_items = 0
        total_critic_items = 0
        total_checker_fatal = 0
        total_checker_minor = 0
        total_critic_fatal = 0
        total_critic_minor = 0
        total_critic_minor_suggestions = 0
        faith_delta_vals: List[float] = []
        secondary_delta_vals: List[float] = []
        secondary_metric_name: Optional[str] = None
        citation_gain_vals: List[int] = []
        node_counts_total: Dict[str, int] = {}
        checker_verdict_counts: Dict[str, int] = {}
        critic_verdict_counts: Dict[str, int] = {}
        finalizer_status_counts: Dict[str, int] = {}
        finalizer_trigger_counts: Dict[str, int] = {}
        revision_counts: List[int] = []

        with open(agentic_jsonl, "w", encoding="utf-8") as ag_fh:
            hdr = {
                "step": "agentic_workflow_header",
                "openai_judge_model": OPENAI_JUDGE_MODEL,
                "openai_embed_model": OPENAI_EMBED_MODEL,
                "n_production_rows": n_queries,
                "status_only": self._status_only,
            }
            ag_fh.write(json.dumps(hdr, ensure_ascii=False) + "\n")
            audit.log(hdr)

            for i, row in enumerate(production_rows):
                case_name = row.get("case_name", f"case_{i+1}")
                query = row.get("query", "")
                contexts = _coerce_ragas_contexts(row.get("retrieved_contexts") or [])
                gt = (row.get("ground_truth") or "").strip()
                analyst = (row.get("analyst_draft") or "").strip()
                final_rep = (row.get("final_report") or "").strip()
                feedback = row.get("critic_feedback") or []
                fbm = _feedback_metrics(feedback)
                total_checker_items += int(fbm["checker_total"])
                total_critic_items += int(fbm["critic_total"])
                total_feedback_items += int(fbm["total"])
                total_checker_fatal += int(fbm["checker_fatal"])
                total_checker_minor += int(fbm["checker_minor"])
                total_critic_fatal += int(fbm["critic_fatal"])
                total_critic_minor += int(fbm["critic_minor"])

                cit_a = count_medallion_citations(analyst)
                cit_f = count_medallion_citations(final_rep)
                node_counts = row.get("node_counts") or _node_counts(row.get("node_audit_log"))
                for node, cnt in node_counts.items():
                    node_counts_total[node] = node_counts_total.get(node, 0) + int(cnt)

                for k, v in _verdict_counts(row.get("checker_verdict_history")).items():
                    checker_verdict_counts[k] = checker_verdict_counts.get(k, 0) + v
                for k, v in _verdict_counts(row.get("critic_verdict_history")).items():
                    critic_verdict_counts[k] = critic_verdict_counts.get(k, 0) + v

                finalizer_status = row.get("finalizer_status") or "missing"
                finalizer_status_counts[finalizer_status] = finalizer_status_counts.get(finalizer_status, 0) + 1
                trigger = row.get("finalizer_trigger_node") or "missing"
                finalizer_trigger_counts[trigger] = finalizer_trigger_counts.get(trigger, 0) + 1
                if row.get("revision_count") is not None:
                    try:
                        revision_counts.append(int(row.get("revision_count")))
                    except (TypeError, ValueError):
                        pass
                minor_suggestions_n = len(row.get("critic_minor_suggestions") or [])
                total_critic_minor_suggestions += minor_suggestions_n

                base_rec: Dict[str, Any] = {
                    "step": "agentic_row",
                    "case_name": case_name,
                    "query": query,
                    "trace_status": row.get("trace_status"),
                    "node_counts": node_counts,
                    "node_latency": _node_latency_summary(row.get("node_audit_log")),
                    "revision_count": row.get("revision_count"),
                    "checker_verdict": row.get("checker_verdict"),
                    "critic_verdict": row.get("critic_verdict"),
                    "finalizer_status": finalizer_status,
                    "finalizer_trigger_node": row.get("finalizer_trigger_node"),
                    "finalizer_path": _finalizer_path_label(row),
                    "finalizer_confidence": row.get("finalizer_confidence"),
                    "finalizer_degraded_reason": row.get("finalizer_degraded_reason"),
                    "errors_caught_total": fbm["total"],
                    "checker_feedback_total": fbm["checker_total"],
                    "checker_fatal": fbm["checker_fatal"],
                    "checker_minor": fbm["checker_minor"],
                    "critic_feedback_total": fbm["critic_total"],
                    "critic_fatal": fbm["critic_fatal"],
                    "critic_minor": fbm["critic_minor"],
                    "critic_minor_suggestions_n": minor_suggestions_n,
                    "first_checker_comment": fbm["first_checker_comment"],
                    "first_critic_comment": fbm["first_critic_comment"],
                    "analyst_citations": cit_a,
                    "final_citations": cit_f,
                }

                if row.get("error"):
                    rec = {
                        **base_rec,
                        "skip_reason": row.get("error"),
                        "faithfulness_gain": None,
                        "analyst_faithfulness": None,
                        "final_faithfulness": None,
                    }
                    ag_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    audit.log({**rec, "step": "agentic_case_skipped"})
                    detailed.append(rec)
                    continue

                if not analyst or not final_rep:
                    rec = {
                        **base_rec,
                        "skip_reason": "missing_analyst_or_final",
                        "faithfulness_gain": None,
                        "analyst_faithfulness": None,
                        "final_faithfulness": None,
                    }
                    ag_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    audit.log({**rec, "step": "agentic_case_skipped"})
                    detailed.append(rec)
                    continue

                scores_a: Dict[str, Optional[float]] = {}
                scores_f: Dict[str, Optional[float]] = {}
                names_a: List[str] = []
                err_a: Optional[str] = None
                err_f: Optional[str] = None
                if not self._status_only:
                    scores_a, names_a, err_a = self._evaluate_one_response(query, analyst, contexts, gt)
                    scores_f, _, err_f = self._evaluate_one_response(query, final_rep, contexts, gt)

                if secondary_metric_name is None and names_a:
                    for nm in names_a:
                        if nm != "faithfulness":
                            secondary_metric_name = nm
                            break

                fa = scores_a.get("faithfulness")
                ff = scores_f.get("faithfulness")
                f_gain: Optional[float] = None
                if fa is not None and ff is not None:
                    f_gain = round(float(ff) - float(fa), 6)
                    faith_delta_vals.append(float(ff) - float(fa))

                sec_gain: Optional[float] = None
                if secondary_metric_name:
                    sa = scores_a.get(secondary_metric_name)
                    sf = scores_f.get(secondary_metric_name)
                    if sa is not None and sf is not None:
                        sec_gain = round(float(sf) - float(sa), 6)
                        secondary_delta_vals.append(float(sf) - float(sa))

                cit_gain = cit_f - cit_a
                citation_gain_vals.append(cit_gain)

                rec = {
                    **base_rec,
                    "skip_reason": None,
                    "ground_truth_present": bool(gt),
                    "analyst_ragas": scores_a,
                    "final_ragas": scores_f,
                    "analyst_faithfulness": fa,
                    "final_faithfulness": ff,
                    "faithfulness_gain": f_gain,
                    "secondary_metric": secondary_metric_name,
                    "secondary_gain": sec_gain,
                    "citation_density_gain": cit_gain,
                    "analyst_ragas_error": err_a,
                    "final_ragas_error": err_f,
                }
                ag_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                ag_fh.flush()
                audit.log({**rec, "step": "agentic_case_complete"})
                detailed.append(rec)

        n_pairs = sum(1 for d in detailed if d.get("skip_reason") is None)
        finalizer_after_critic = sum(1 for d in detailed if d.get("finalizer_trigger_node") == "critic")
        finalizer_after_checker = sum(1 for d in detailed if d.get("finalizer_trigger_node") == "checker")
        finalizer_after_critic_minor = sum(
            1 for d in detailed
            if d.get("finalizer_trigger_node") == "critic" and d.get("critic_verdict") == "minor"
        )
        finalizer_after_critic_pass = sum(
            1 for d in detailed
            if d.get("finalizer_trigger_node") == "critic" and d.get("critic_verdict") == "pass"
        )

        summary: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "openai_judge_model": OPENAI_JUDGE_MODEL,
            "openai_embed_model": OPENAI_EMBED_MODEL,
            "status_only": self._status_only,
            "n_queries": n_queries,
            "n_agentic_pairs_evaluated": n_pairs,
            "node_run_counts": node_counts_total,
            "checker_verdict_counts": checker_verdict_counts,
            "critic_verdict_counts": critic_verdict_counts,
            "finalizer_status_counts": finalizer_status_counts,
            "finalizer_trigger_counts": finalizer_trigger_counts,
            "finalizer_after_critic_count": finalizer_after_critic,
            "finalizer_after_checker_circuit_break_count": finalizer_after_checker,
            "finalizer_after_critic_minor_count": finalizer_after_critic_minor,
            "finalizer_after_critic_pass_count": finalizer_after_critic_pass,
            "mean_revision_count": (
                round(sum(revision_counts) / len(revision_counts), 6) if revision_counts else None
            ),
            "correction_rate_total_feedback_per_query": (
                round(total_feedback_items / n_queries, 6) if n_queries else None
            ),
            "correction_rate_checker_feedback_per_query": (
                round(total_checker_items / n_queries, 6) if n_queries else None
            ),
            "correction_rate_critic_feedback_per_query": (
                round(total_critic_items / n_queries, 6) if n_queries else None
            ),
            "checker_fatal_total": total_checker_fatal,
            "checker_minor_total": total_checker_minor,
            "critic_fatal_total": total_critic_fatal,
            "critic_minor_total": total_critic_minor,
            "critic_minor_suggestions_total": total_critic_minor_suggestions,
            "mean_faithfulness_gain_final_minus_analyst": (
                round(sum(faith_delta_vals) / len(faith_delta_vals), 6) if faith_delta_vals else None
            ),
            "mean_secondary_gain_final_minus_analyst": (
                round(sum(secondary_delta_vals) / len(secondary_delta_vals), 6)
                if secondary_delta_vals else None
            ),
            "secondary_metric_name": secondary_metric_name,
            "mean_citation_density_gain": (
                round(sum(citation_gain_vals) / len(citation_gain_vals), 6)
                if citation_gain_vals else None
            ),
        }

        metrics_path = run_dir / "agentic_workflow_metrics.json"
        with open(metrics_path, "w", encoding="utf-8") as fp:
            json.dump({"summary": summary, "per_case": detailed}, fp, indent=2, ensure_ascii=False)

        csv_path = run_dir / "agentic_workflow_eval_detailed.csv"
        if detailed:
            keys = sorted({k for d in detailed for k in d.keys()})
            with open(csv_path, "w", newline="", encoding="utf-8") as fp:
                w = csv.DictWriter(fp, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                for d in detailed:
                    flat = dict(d)
                    for kk, vv in list(flat.items()):
                        if isinstance(vv, (dict, list)):
                            flat[kk] = json.dumps(vv, ensure_ascii=False)
                    w.writerow(flat)

        md_path = run_dir / "agentic_workflow_report.md"
        md_path.write_text(_render_agentic_markdown(summary, detailed), encoding="utf-8")

        audit.log({
            "step": "agentic_workflow_eval_complete",
            "metrics_file": str(metrics_path),
            "audit_jsonl": str(agentic_jsonl),
            "csv_file": str(csv_path) if detailed else None,
        })
        logger.info("Agentic workflow artefacts written to %s", run_dir)
        return summary


def _render_agentic_markdown(summary: Dict[str, Any], detailed: List[Dict[str, Any]]) -> str:
    ts = summary.get("generated_at", "")[:19].replace("T", " ")
    lines = [
        "# Agentic Workflow — Node Effectiveness",
        f"> Generated: {ts} UTC",
        f"> Status-only: `{summary.get('status_only')}` | OpenAI judge: `{summary.get('openai_judge_model')}` "
        f"| embeddings: `{summary.get('openai_embed_model')}`",
        "",
        "## Architecture Readout",
        "",
        "- Model-level comparison is BaselineModel output vs Finalizer output.",
        "- Analyst effectiveness is measured by revision count, citation density, and draft-to-final RAGAS gain when judge mode is enabled.",
        "- Checker and Critic are separated by sender, verdict, severity, and trigger path.",
        "- Finalizer effectiveness is measured only on terminal output and its status/degraded path.",
        "",
        "## Aggregate",
        "",
        "| Key | Value |",
        "|-----|-------|",
    ]
    for k, v in summary.items():
        if k in ("generated_at",):
            continue
        lines.append(f"| `{k}` | {v} |")

    lines += [
        "",
        "---",
        "",
        "## Per case",
        "",
        "| Case | Revisions | Checker | Critic | Finalizer Path | Faithfulness Δ | Citation Δ |",
        "|------|-----------|---------|--------|----------------|----------------|------------|",
    ]
    for d in detailed:
        nm = d.get("case_name", "")
        fg = d.get("faithfulness_gain")
        checker_cell = (
            f"{d.get('checker_verdict') or 'n/a'} "
            f"(F:{d.get('checker_fatal', 0)}, M:{d.get('checker_minor', 0)})"
        )
        critic_cell = (
            f"{d.get('critic_verdict') or 'n/a'} "
            f"(F:{d.get('critic_fatal', 0)}, M:{d.get('critic_minor', 0)}, "
            f"S:{d.get('critic_minor_suggestions_n', 0)})"
        )
        lines.append(
            f"| {nm} | {d.get('revision_count', '—')} | {checker_cell} | {critic_cell} | "
            f"{d.get('finalizer_path', '—')} | {fg if fg is not None else '—'} | "
            f"{d.get('citation_density_gain', '—')} |"
        )

    lines += ["", "---", "", "## Raw rows (JSON)", ""]
    for d in detailed:
        lines.append(f"### {d.get('case_name', '')}")
        lines.append("```json")
        lines.append(json.dumps(d, indent=2, ensure_ascii=False, default=str))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def run_agentic_node_evaluation_optional(
    production_rows: List[Dict[str, Any]],
    run_dir: Path,
    audit: AuditLogger,
    enabled: bool,
    status_only: bool,
) -> Optional[Dict[str, Any]]:
    if not enabled:
        audit.log({"step": "agentic_workflow_eval_skipped", "reason": "disabled_cli_flag"})
        return None
    try:
        ev = AgenticNodeEvaluator(status_only=status_only)
    except RuntimeError as exc:
        logger.warning("Agentic workflow evaluation skipped: %s", exc)
        audit.log({"step": "agentic_workflow_eval_skipped", "reason": str(exc)})
        return None

    audit.log({"step": "agentic_workflow_eval_start"})
    t0 = time.monotonic()
    summary = ev.run(production_rows, run_dir, audit)
    audit.log({"step": "agentic_workflow_eval_done", "elapsed_s": round(time.monotonic() - t0, 1)})
    return summary


# ===========================================================================
# Section 4 — Comparison & Reporting
# ===========================================================================

def _safe_float(v: Any, default: float = float("nan")) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "  N/A "
    if v >= _PASS_THRESHOLD:
        return f"✅{v:.3f}"
    if v >= _WARN_THRESHOLD:
        return f"⚠️{v:.3f}"
    return f"❌{v:.3f}"


def _avg(vals: List[Optional[float]]) -> Optional[float]:
    valid = [v for v in vals if v is not None]
    return sum(valid) / len(valid) if valid else None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _case_meta(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("case_meta") or {}
    return meta if isinstance(meta, dict) else {}


def _normalise_source_name(src: str) -> str:
    s = str(src or "").strip().lower()
    if "." in s:
        head, tail = s.split(".", 1)
        if head in {"sourcetype", "source_type"} and tail:
            s = tail
    return {
        "macro_history": "macro",
        "options": "options",
        "sec": "sec",
        "news": "news",
        "gpr": "gpr",
    }.get(s, s)


def evaluate_case_contract(row: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate non-RAGAS benchmark contract fields from generated ground truth."""
    meta = _case_meta(row)
    expected_sources = [
        _normalise_source_name(x)
        for x in _as_list(row.get("expected_sources") or meta.get("expected_sources"))
    ]
    retrieved_sources = [
        _normalise_source_name(x)
        for x in _as_list(row.get("retrieved_sources") or meta.get("retrieved_sources"))
    ]

    if not retrieved_sources:
        for item in _as_list(row.get("context_metadata")):
            if isinstance(item, dict) and item.get("source"):
                retrieved_sources.append(_normalise_source_name(str(item.get("source"))))

    if not retrieved_sources:
        contexts = " ".join(str(c).lower() for c in _as_list(row.get("retrieved_contexts")))
        inferred: List[str] = []
        if "silver layer values" in contexts:
            inferred.append("options")
            inferred.append("macro")
        if "form-4" in contexts or "accession" in contexts or "insider" in contexts:
            inferred.append("sec")
        if "news" in contexts or "volatility implication" in contexts:
            inferred.append("news")
        retrieved_sources = sorted(set(inferred))

    expected_time = row.get("expected_time_window") or meta.get("expected_time_window")
    time_meta = meta.get("time_window") if isinstance(meta.get("time_window"), dict) else {}
    actual_time = (
        row.get("time_window_label")
        or (row.get("time_range") or {}).get("time_window_label")
        or time_meta.get("requested_label")
    )

    node_focus = _as_list(meta.get("node_focus"))
    ontology_metrics = _as_list(meta.get("ontology_metrics_validated") or meta.get("metrics"))
    validation_errors = _as_list(meta.get("validation_errors"))
    ground_truth = (row.get("ground_truth") or "").strip()

    source_hits = sorted(set(expected_sources) & set(retrieved_sources))
    source_coverage = (
        round(len(source_hits) / len(set(expected_sources)), 6)
        if expected_sources else None
    )

    time_match = None
    if expected_time and actual_time:
        time_match = str(expected_time).lower() == str(actual_time).lower()

    required_truth_sections = [
        "Scope:",
        "Expected reasoning path:",
        "Truth-answer boundary:",
    ]
    truth_sections_present = {
        section.rstrip(":").lower(): section in ground_truth
        for section in required_truth_sections
    }

    return {
        "ground_truth_present": bool(ground_truth),
        "expected_sources": expected_sources,
        "retrieved_sources": retrieved_sources,
        "source_coverage": source_coverage,
        "source_hits": source_hits,
        "missing_sources": sorted(set(expected_sources) - set(retrieved_sources)),
        "expected_time_window": expected_time,
        "actual_time_window": actual_time,
        "time_window_match": time_match,
        "time_window_observational": time_match,
        "node_focus": node_focus,
        "node_focus_count": len(node_focus),
        "ontology_metrics": ontology_metrics,
        "ontology_metric_count": len(ontology_metrics),
        "ground_truth_sections_present": truth_sections_present,
        "ground_truth_contract_complete": (
            bool(ground_truth)
            and all(truth_sections_present.values())
            and not validation_errors
        ),
        "validation_errors": validation_errors,
    }


def _contract_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    audits = [r.get("contract_audit") or {} for r in rows]
    n = len(audits)
    valid_source = [a.get("source_coverage") for a in audits if a.get("source_coverage") is not None]
    time_vals = [a.get("time_window_match") for a in audits if a.get("time_window_match") is not None]
    return {
        "n_rows": n,
        "ground_truth_present_rate": (
            round(sum(1 for a in audits if a.get("ground_truth_present")) / n, 6) if n else None
        ),
        "mean_source_coverage": (
            round(sum(valid_source) / len(valid_source), 6) if valid_source else None
        ),
        "time_window_observational_rate": (
            round(sum(1 for v in time_vals if v) / len(time_vals), 6) if time_vals else None
        ),
        "time_window_match_rate": (
            round(sum(1 for v in time_vals if v) / len(time_vals), 6) if time_vals else None
        ),
        "ground_truth_contract_complete_rate": (
            round(sum(1 for a in audits if a.get("ground_truth_contract_complete")) / n, 6) if n else None
        ),
        "rows_with_validation_errors": sum(1 for a in audits if a.get("validation_errors")),
    }


def build_comparison(
    baseline_scores: List[Dict[str, Any]],
    production_scores: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Merge baseline and production scores by stable ``case_id``.
    Aggregate metrics are computed on paired rows only.
    Returns a comparison dict ready for JSON export + Markdown rendering.
    """
    METRICS = [
        "faithfulness",
        "answer_relevancy",
        "llm_context_precision_without_reference",
        "answer_correctness",
    ]

    def _row_key(row: Dict[str, Any]) -> str:
        return str(row.get("case_id") or _stable_case_id(row.get("case_name", ""), row.get("query", "")))

    base_by_case = {_row_key(r): r for r in baseline_scores}
    prod_by_case = {_row_key(r): r for r in production_scores}

    paired_ids = sorted(set(base_by_case) & set(prod_by_case))
    unmatched_baseline_ids = sorted(set(base_by_case) - set(prod_by_case))
    unmatched_production_ids = sorted(set(prod_by_case) - set(base_by_case))

    per_case: List[Dict[str, Any]] = []
    for case_id in paired_ids:
        b = base_by_case.get(case_id, {})
        p = prod_by_case.get(case_id, {})
        row: Dict[str, Any] = {
            "case_id": case_id,
            "case_name": b.get("case_name") or p.get("case_name") or case_id,
            "baseline_present": True,
            "production_present": True,
        }
        for m in METRICS:
            bv = b.get(m)
            pv = p.get(m)
            delta = (pv - bv) if (pv is not None and bv is not None) else None
            row[m] = {
                "baseline":   bv,
                "production": pv,
                "delta":      round(delta, 4) if delta is not None else None,
                "winner":     (
                    "production" if delta is not None and delta > 0.01 else
                    "baseline"   if delta is not None and delta < -0.01 else
                    "tie"
                ),
            }
        row["contract_audit"] = {
            "baseline": b.get("contract_audit") or {},
            "finalizer": p.get("contract_audit") or {},
        }
        row["retrieval_audit"] = {
            "gold_fallback_triggered": p.get("gold_fallback_triggered"),
            "gold_fallback_tier": p.get("gold_fallback_tier"),
            "gold_results_count": p.get("gold_results_count"),
            "requested_sources": p.get("requested_sources") or [],
            "actual_gold_sources": p.get("actual_gold_sources") or [],
        }
        per_case.append(row)

    aggregate: Dict[str, Any] = {}
    for m in METRICS:
        base_avg = _avg([base_by_case.get(c, {}).get(m) for c in paired_ids])
        prod_avg = _avg([prod_by_case.get(c, {}).get(m) for c in paired_ids])
        delta    = (prod_avg - base_avg) if (prod_avg is not None and base_avg is not None) else None
        aggregate[m] = {
            "baseline_avg":   round(base_avg, 4) if base_avg is not None else None,
            "production_avg": round(prod_avg, 4) if prod_avg is not None else None,
            "delta_avg":      round(delta, 4) if delta is not None else None,
            "winner":         (
                "production" if delta is not None and delta > 0.01 else
                "baseline"   if delta is not None and delta < -0.01 else
                "tie"
            ),
        }

    unmatched_baseline = [
        {
            "case_id": case_id,
            "case_name": base_by_case[case_id].get("case_name"),
            "query": base_by_case[case_id].get("query"),
            "reason": "missing_production_match",
        }
        for case_id in unmatched_baseline_ids
    ]
    unmatched_production = [
        {
            "case_id": case_id,
            "case_name": prod_by_case[case_id].get("case_name"),
            "query": prod_by_case[case_id].get("query"),
            "reason": "missing_baseline_match",
        }
        for case_id in unmatched_production_ids
    ]

    paired_production_rows = [prod_by_case[c] for c in paired_ids]
    strict_hit_cases = [
        r for r in paired_production_rows
        if r.get("gold_fallback_triggered") is False or r.get("gold_fallback_tier") == "strict"
    ]
    fallback_cases = [
        r for r in paired_production_rows
        if r.get("gold_fallback_triggered") is True and r.get("gold_fallback_tier") not in (None, "strict")
    ]

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "n_catalog_cases":  len(set(base_by_case) | set(prod_by_case)),
        "n_paired_cases":   len(paired_ids),
        "n_unmatched_baseline": len(unmatched_baseline),
        "n_unmatched_production": len(unmatched_production),
        "judge_llm":        JUDGE_LLM_MODEL,
        "judge_provider":   JUDGE_PROVIDER,
        "judge_embed_model": JUDGE_EMBED_MODEL,
        "baseline_label":    "BaselineModel",
        "candidate_label":   "Finalizer",
        "metrics_evaluated": METRICS,
        "per_case":         per_case,
        "coverage": {
            "paired_case_ids": paired_ids,
            "unmatched_baseline": unmatched_baseline,
            "unmatched_production": unmatched_production,
        },
        "aggregate":        aggregate,
        "contract_summary": {
            "baseline": _contract_summary([base_by_case[c] for c in paired_ids]),
            "finalizer": _contract_summary([prod_by_case[c] for c in paired_ids]),
        },
        "retrieval_summary": {
            "strict_hit_cases": len(strict_hit_cases),
            "fallback_cases": len(fallback_cases),
            "strict_hit_case_ids": [r.get("case_id") for r in strict_hit_cases],
            "fallback_case_ids": [r.get("case_id") for r in fallback_cases],
        },
    }


def render_markdown(comparison: Dict[str, Any]) -> str:
    """Render the comparison dict to a Markdown report."""
    METRICS = comparison["metrics_evaluated"]
    now_str = comparison["generated_at"][:19].replace("T", " ")

    lines = [
        "# RAGAS Benchmarking Report — BaselineModel vs Finalizer",
        f"> Generated: {now_str} UTC  |  Judge LLM: `{comparison['judge_llm']}`  "
        f"|  Paired Cases: {comparison['n_paired_cases']} / {comparison['n_catalog_cases']}",
        "",
        "---",
        "",
        "## 1. Aggregate Results",
        "",
        "Aggregate metrics are computed on paired baseline/finalizer cases only.",
        "",
        "| Metric | BaselineModel | Finalizer | Δ Delta | Winner |",
        "|--------|----------|------------|---------|--------|",
    ]
    for m in METRICS:
        agg = comparison["aggregate"][m]
        b   = f"{agg['baseline_avg']:.3f}"   if agg["baseline_avg"]   is not None else "N/A"
        p   = f"{agg['production_avg']:.3f}" if agg["production_avg"] is not None else "N/A"
        d   = (f"+{agg['delta_avg']:.3f}" if agg["delta_avg"] >= 0 else f"{agg['delta_avg']:.3f}") \
              if agg["delta_avg"] is not None else "N/A"
        w   = "🏆 Finalizer" if agg["winner"] == "production" else \
              "🏆 Baseline"   if agg["winner"] == "baseline"   else "—"
        short = _short_metric(m)
        lines.append(f"| **{short}** | {b} | {p} | {d} | {w} |")

    lines += [
        "",
        "---",
        "",
        "## 2. Coverage and Missing Matches",
        "",
        f"- Paired cases: `{comparison['n_paired_cases']}`",
        f"- Baseline-only unmatched cases: `{comparison['n_unmatched_baseline']}`",
        f"- Finalizer-only unmatched cases: `{comparison['n_unmatched_production']}`",
        f"- Strict-hit production cases: `{comparison.get('retrieval_summary', {}).get('strict_hit_cases', 0)}`",
        f"- Fallback production cases: `{comparison.get('retrieval_summary', {}).get('fallback_cases', 0)}`",
        "",
    ]
    for label, rows in (
        ("Baseline-only unmatched", comparison.get("coverage", {}).get("unmatched_baseline", [])),
        ("Finalizer-only unmatched", comparison.get("coverage", {}).get("unmatched_production", [])),
    ):
        lines.append(f"### {label}")
        lines.append("")
        if not rows:
            lines.append("- None")
        else:
            for row in rows:
                lines.append(f"- `{row.get('case_id')}` | {row.get('case_name')} | {row.get('reason')}")
        lines.append("")

    lines += [
        "---",
        "",
        "## 3. Ground-Truth Contract Coverage",
        "",
        "| Model | GT Present | Source Coverage | Contract Complete | Validation Errors | Time Audit (observational) |",
        "|-------|------------|-----------------|-------------------|-------------------|----------------------------|",
    ]
    for label, summary in (comparison.get("contract_summary") or {}).items():
        lines.append(
            f"| {label} | {summary.get('ground_truth_present_rate')} | "
            f"{summary.get('mean_source_coverage')} | "
            f"{summary.get('ground_truth_contract_complete_rate')} | "
            f"{summary.get('rows_with_validation_errors')} | "
            f"{summary.get('time_window_observational_rate')} |"
        )

        lines += [
        "",
        "---",
        "",
        "## 4. Paired Per-Case Breakdown",
        "",
    ]

    for row in comparison["per_case"]:
        lines.append(f"### Case: {row['case_name']}")
        lines.append("")
        lines.append(f"`case_id`: `{row.get('case_id')}`")
        lines.append("")
        lines.append("| Metric | BaselineModel | Finalizer | Δ | Winner |")
        lines.append("|--------|----------|------------|---|--------|")
        for m in METRICS:
            cell = row.get(m, {})
            b    = f"{cell['baseline']:.3f}"   if cell.get("baseline")   is not None else "N/A"
            p    = f"{cell['production']:.3f}" if cell.get("production") is not None else "N/A"
            d_v  = cell.get("delta")
            d    = (f"+{d_v:.3f}" if d_v >= 0 else f"{d_v:.3f}") if d_v is not None else "N/A"
            w    = "🏆 Final" if cell.get("winner") == "production" else \
                   "🏆 Base" if cell.get("winner") == "baseline"   else "—"
            short = _short_metric(m)
            lines.append(f"| {short} | {b} | {p} | {d} | {w} |")
        contract = row.get("contract_audit", {}).get("finalizer", {})
        retrieval = row.get("retrieval_audit", {})
        if contract:
            lines.append("")
            lines.append(
                f"Contract audit: source coverage `{contract.get('source_coverage')}`, "
                f"missing sources `{contract.get('missing_sources')}`, "
                f"time audit (observational) `{contract.get('time_window_match')}`."
            )
        if retrieval:
            lines.append(
                f"Retrieval audit: fallback `{retrieval.get('gold_fallback_triggered')}`, "
                f"tier `{retrieval.get('gold_fallback_tier')}`, "
                f"gold results `{retrieval.get('gold_results_count')}`, "
                f"requested `{retrieval.get('requested_sources')}`, "
                f"actual gold `{retrieval.get('actual_gold_sources')}`."
            )
        lines.append("")

    lines += [
        "---",
        "",
        "## 5. Metric Definitions",
        "",
        "| Metric | Definition | Requirement |",
        "|--------|------------|-------------|",
        "| **Faithfulness** | Fraction of claims in the answer that are verifiably grounded in the "
        "retrieved context. Measures hallucination risk. | Reference-free |",
        "| **Answer Relevancy** | Semantic similarity between the generated answer and the question. "
        "High scores mean the answer stays on-topic. | Reference-free |",
        "| **Context Precision** | Fraction of retrieved chunks that are judged relevant to the question. "
        "Measures retrieval signal quality. | Reference-free |",
        "| **Answer Correctness** | Agreement between the answer and generated English ground truth. "
        "Measures whether the final answer follows facts, time limits, and reasoning boundaries. | Requires `ground_truth` |",
        "",
        "---",
        "",
        "## 6. Interpretation Guide",
        "",
        f"- ✅ Score ≥ {_PASS_THRESHOLD}  → Acceptable",
        f"- ⚠️ Score {_WARN_THRESHOLD}–{_PASS_THRESHOLD} → Needs attention",
        f"- ❌ Score < {_WARN_THRESHOLD} → Failing",
        "",
        "A positive Δ (Finalizer – BaselineModel) indicates the terminal multi-agent",
        "answer outperforms the legacy single-pass baseline on that metric.",
        "",
        "> *Audit details: see `ragas_audit.jsonl` in this directory.*",
        "",
    ]
    return "\n".join(lines)


def _short_metric(m: str) -> str:
    return {
        "faithfulness":                             "Faithfulness",
        "answer_relevancy":                         "Answer Relevancy",
        "llm_context_precision_without_reference":  "Context Precision",
        "answer_correctness":                       "Answer Correctness",
    }.get(m, m)


# ===========================================================================
# Section 5 — Orchestrator
# ===========================================================================

def load_test_cases(path: Optional[Path] = None) -> List[Dict]:
    source = path or _QUERIES_FILE
    with open(source, encoding="utf-8") as f:
        raw_cases = json.load(f).get("cases", [])
    cases: List[Dict[str, Any]] = []
    for case in raw_cases:
        copied = dict(case)
        copied["case_id"] = copied.get("case_id") or _stable_case_id(
            copied.get("name", ""),
            copied.get("query", ""),
        )
        cases.append(copied)
    return cases


def enrich_results_with_case_contract(
    results: List[Dict[str, Any]],
    test_cases: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach generated ground-truth metadata to model rows before scoring."""
    by_name = {case.get("name"): case for case in test_cases}
    enriched: List[Dict[str, Any]] = []
    for row in results:
        case = by_name.get(row.get("case_name"), {})
        merged = dict(row)
        if case:
            merged.setdefault("case_id", case.get("case_id"))
            merged.setdefault("ground_truth", (case.get("ground_truth") or "").strip())
            merged.setdefault("expected_sources", case.get("expected_sources", []))
            merged.setdefault("expected_time_window", case.get("expected_time_window"))
            merged.setdefault("case_meta", case.get("case_meta", {}))
        enriched.append(merged)
    return enriched


def write_queries_snapshot(run_dir: Path, *, queries_file: Path, test_cases: List[Dict[str, Any]], eval_batch_id: str) -> Path:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queries_file": str(queries_file),
        "eval_batch_id": eval_batch_id,
        "n_cases": len(test_cases),
        "cases": test_cases,
    }
    path = run_dir / "queries_snapshot.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def _answer_export_row(row: Dict[str, Any], *, include_production_fields: bool) -> Dict[str, Any]:
    contract = evaluate_case_contract(row)
    out = {
        "case_id": row.get("case_id"),
        "case_name": row.get("case_name"),
        "query": row.get("query"),
        "answer": row.get("answer"),
        "answer_for_eval": row.get("answer_for_eval") or row.get("answer"),
        "retrieved_contexts": _coerce_ragas_contexts(row.get("retrieved_contexts")),
        "source_coverage": contract.get("source_coverage"),
        "time_window": row.get("time_window_label")
            or (row.get("time_range") or {}).get("time_window_label")
            or contract.get("actual_time_window"),
    }
    if include_production_fields:
        out.update({
            "trace_path": row.get("trace_path") or row.get("log_file"),
            "finalizer_status": row.get("finalizer_status"),
            "revision_count": row.get("revision_count"),
            "answer_rendered_markdown": row.get("answer_rendered_markdown") or row.get("final_report"),
            "finalizer_confidence": row.get("finalizer_confidence"),
            "recommendation_mode": (row.get("critic_reasoning_profile") or {}).get("recommendation_mode")
                or row.get("recommendation_mode"),
            "gold_fallback_triggered": row.get("gold_fallback_triggered"),
            "gold_fallback_tier": row.get("gold_fallback_tier"),
            "gold_results_count": row.get("gold_results_count"),
            "requested_sources": row.get("requested_sources"),
            "actual_gold_sources": row.get("actual_gold_sources"),
        })
    return out


def write_answers_jsonl(run_dir: Path, *, filename: str, rows: List[Dict[str, Any]], include_production_fields: bool) -> Path:
    path = run_dir / filename
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(
                _answer_export_row(row, include_production_fields=include_production_fields),
                ensure_ascii=False,
            ) + "\n")
    return path


def write_results_jsonl(run_dir: Path, *, filename: str, rows: List[Dict[str, Any]]) -> Path:
    path = run_dir / filename
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def write_scores_jsonl(run_dir: Path, *, filename: str, rows: List[Dict[str, Any]]) -> Path:
    path = run_dir / filename
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def load_baseline_from_file(path: Path) -> List[Dict[str, Any]]:
    logger.info(f"Loading baseline results from: {path}")
    results = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(json.loads(line))
    logger.info(f"  Loaded {len(results)} baseline results")
    return results


def load_results_from_file(path: Path) -> List[Dict[str, Any]]:
    logger.info(f"Loading results from: {path}")
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    logger.info(f"  Loaded {len(rows)} rows")
    return rows


def run_baseline_fresh(test_cases: List[Dict], run_dir: Path, queries_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Run the baseline model on all test cases."""
    from Scripts.Legacy_Baseline.baseline_model import BaselineModel, AuditLogger as BL_AuditLogger

    bl_run_dir = _BASELINE_LOG_DIR / run_dir.name
    bl_audit   = BL_AuditLogger(bl_run_dir)
    model      = BaselineModel()
    try:
        results = model.run_all_cases(queries_file=queries_file, audit=bl_audit)
    finally:
        bl_audit.close()
    return results


def production_row_from_graph_state(case: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """Shape LangGraph ``AgentState`` into the same dict layout as ``ProductionLogExtractor``."""
    state = _as_mapping(state)
    gold_context = state.get("gold_context") or []
    gold_texts = [
        _stringify_context_object(g)
        for g in gold_context
    ]
    retrieved_sources = []
    for g in gold_context:
        src = _source_from_chunk(g)
        if src:
            retrieved_sources.append(str(src))
    silver_contexts: List[str] = []
    vals = _silver_values_from_context(state.get("silver_context") or {})
    if vals:
        retrieved_sources.extend(["options", "macro"])
        silver_contexts = _silver_metric_contexts(vals)
    retrieved: List[str] = _coerce_ragas_contexts(gold_texts)
    retrieved.extend(silver_contexts)

    draft = state.get("draft_report") or ""
    fs = _as_mapping(state.get("final_strategy") or {})
    final_md = _obj_get(fs, "markdown", "") or ""
    final_report_struct = _as_mapping(_obj_get(fs, "final_report", {}) or {})
    answer = _finalizer_plaintext_answer(final_report_struct, markdown=str(final_md or ""), fallback=draft)
    node_audit_log = state.get("node_audit_log") or []
    node_counts = _node_counts(node_audit_log)
    last_pre_finalizer = None
    for ev in node_audit_log:
        if isinstance(ev, dict) and ev.get("node") and ev.get("node") != "finalizer":
            last_pre_finalizer = ev.get("node")

    row = {
        "case_id":            case.get("case_id"),
        "case_name":          case.get("name", ""),
        "query":              case.get("query", ""),
        "retrieved_contexts": retrieved,
        "answer":             answer,
        "answer_for_eval":    answer,
        "answer_rendered_markdown": final_md,
        "analyst_draft":      draft,
        "final_report":       final_md,
        "critic_feedback":    state.get("critic_feedback") or [],
        "checker_verdict":    state.get("checker_verdict"),
        "critic_verdict":     state.get("critic_verdict"),
        "checker_verdict_history": [
            str(ev.get("verdict")) for ev in node_audit_log
            if isinstance(ev, dict) and ev.get("node") == "checker" and ev.get("verdict")
        ],
        "critic_verdict_history": [
            str(ev.get("verdict")) for ev in node_audit_log
            if isinstance(ev, dict) and ev.get("node") == "critic" and ev.get("verdict")
        ],
        "critic_minor_suggestions": state.get("critic_minor_suggestions") or [],
        "finalizer_status":   _obj_get(fs, "status"),
        "finalizer_degraded_reason": _obj_get(fs, "degraded_reason"),
        "finalizer_confidence": _safe_float(_obj_get(fs, "confidence_score")),
        "finalizer_trigger_node": last_pre_finalizer,
        "revision_count":     state.get("revision_count"),
        "time_range":         _as_mapping(state.get("time_range") or {}),
        "time_window_label":  _obj_get(state.get("time_range") or {}, "time_window_label"),
        "retrieved_sources":  sorted(set(_normalise_source_name(x) for x in retrieved_sources)),
        "gold_context":       gold_context,
        "node_audit_log":     node_audit_log,
        "node_counts":        node_counts,
        "trace_status":       "complete" if final_md else "incomplete_no_finalizer",
        "ground_truth":       (case.get("ground_truth") or "").strip(),
        "expected_sources":   case.get("expected_sources", []),
        "expected_time_window": case.get("expected_time_window"),
        "case_meta":          case.get("case_meta", {}),
        "eval_batch_id":      case.get("eval_batch_id"),
        "model_type":         "production",
    }
    if not final_md:
        row["error"] = "no_finalizer_output"
    return row


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAGAS Benchmarking: BaselineModel vs Finalizer + agentic node metrics"
    )
    parser.add_argument(
        "--mode",
        choices=["logs", "fresh"],
        default="logs",
        help=(
            "logs  = read production results from existing router_e2e trace logs (default)\n"
            "fresh = run production LangGraph pipeline fresh (slow — requires Qdrant + Ollama)"
        ),
    )
    parser.add_argument(
        "--baseline-results",
        type=str,
        default=None,
        help="Path to an existing baseline_results.jsonl to skip re-running the baseline",
    )
    parser.add_argument(
        "--production-results",
        type=str,
        default=None,
        help=(
            "Path to an existing production raw results JSONL to skip re-running/extracting "
            "production. Prefer production_results_full.jsonl from a prior RAGAS run."
        ),
    )
    parser.add_argument(
        "--queries-file",
        type=str,
        default=str(_QUERIES_FILE),
        help=(
            "Test case JSON with optional generated ground_truth/case_meta fields. "
            "Use Scripts/tests/router_e2e_ground_truth_queries.json after running generate_ground_truth.py --update-tests."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Output directory (default: logs/ragas_eval/YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--no-agentic-nodes",
        action="store_true",
        help="Skip Analyst/Checker/Critic/Finalizer node evaluation.",
    )
    parser.add_argument(
        "--agentic-status-only",
        action="store_true",
        help=(
            "Quantify Analyst/Checker/Critic/Finalizer from status fields and node_audit_log only. "
            "This is faster for node scoring; model-level RAGAS metrics still use the configured judge provider."
        ),
    )
    args = parser.parse_args()

    # ---- Setup run directory ----
    run_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    today_dir = datetime.now().strftime("%Y-%m-%d")
    run_dir = Path(args.out_dir) if args.out_dir else _EVAL_LOG_ROOT / today_dir / run_ts
    audit   = AuditLogger(run_dir)

    audit.log({
        "step":             "comparison_start",
        "mode":             args.mode,
        "baseline_source":  args.baseline_results or "fresh",
        "production_source": args.production_results or ("router_e2e_logs" if args.mode == "logs" else "fresh"),
        "run_dir":          str(run_dir),
        "judge_provider":   JUDGE_PROVIDER,
        "judge_llm":        JUDGE_LLM_MODEL,
        "judge_embed":      JUDGE_EMBED_MODEL,
        "agentic_openai_judge": OPENAI_JUDGE_MODEL,
        "agentic_nodes_enabled": not args.no_agentic_nodes,
        "agentic_status_only": args.agentic_status_only,
    })
    logger.info(f"\n{'='*70}")
    logger.info(f"RAGAS Comparison  |  mode={args.mode}  |  run_dir={run_dir}")
    logger.info(f"{'='*70}\n")

    try:
        queries_file = Path(args.queries_file)
        test_cases = load_test_cases(queries_file)
        eval_batch_id = _compute_eval_batch_id(test_cases, queries_file)
        for case in test_cases:
            case["eval_batch_id"] = eval_batch_id
        snapshot_path = write_queries_snapshot(
            run_dir,
            queries_file=queries_file,
            test_cases=test_cases,
            eval_batch_id=eval_batch_id,
        )
        audit.log({"step": "test_cases_loaded", "n": len(test_cases),
                   "file": str(queries_file), "eval_batch_id": eval_batch_id, "snapshot": str(snapshot_path)})

        # ---- 1. Baseline results ----
        if args.baseline_results:
            baseline_results = load_baseline_from_file(Path(args.baseline_results))
        else:
            logger.info("Running baseline model on all test cases…")
            audit.log({"step": "baseline_run_start"})
            t0 = time.monotonic()
            baseline_results = run_baseline_fresh(test_cases, run_dir, queries_file=queries_file)
            audit.log({"step": "baseline_run_complete",
                       "n": len(baseline_results),
                       "elapsed_s": round(time.monotonic() - t0, 1)})
        baseline_results = enrich_results_with_case_contract(baseline_results, test_cases)
        baseline_results_full_path = write_results_jsonl(
            run_dir,
            filename="baseline_results_full.jsonl",
            rows=baseline_results,
        )
        baseline_answers_path = write_answers_jsonl(
            run_dir,
            filename="baseline_answers.jsonl",
            rows=baseline_results,
            include_production_fields=False,
        )
        audit.log({
            "step": "baseline_results_persisted",
            "full_file": str(baseline_results_full_path),
            "answers_file": str(baseline_answers_path),
        })

        # ---- 2. Production results ----
        if args.production_results:
            production_results = load_results_from_file(Path(args.production_results))
            audit.log({
                "step": "production_results_loaded_from_file",
                "file": str(Path(args.production_results)),
                "n": len(production_results),
            })
        elif args.mode == "logs":
            logger.info("Extracting production results from router_e2e trace logs…")
            audit.log({"step": "production_extract_start", "source": "router_e2e_logs"})
            extractor = ProductionLogExtractor()
            production_results = extractor.load_all(test_cases, eval_batch_id, audit=audit)
            audit.log({"step": "production_extract_complete",
                       "n_ok":    sum(1 for r in production_results if not r.get("error")),
                       "n_error": sum(1 for r in production_results if r.get("error"))})
        else:
            logger.info("Running production LangGraph pipeline on all test cases…")
            audit.log({"step": "production_run_start", "source": "langgraph_fresh"})
            from Scripts.agents.router import build_financial_rag_graph

            graph = build_financial_rag_graph()

            async def _run_fresh() -> List[Dict[str, Any]]:
                rows: List[Dict[str, Any]] = []
                for case in test_cases:
                    try:
                        state = await graph.ainvoke({"original_query": case["query"]})
                        rows.append(production_row_from_graph_state(case, state))
                    except Exception as exc:
                        logger.error("Production run failed for '%s': %s", case.get("name"), exc)
                        rows.append({
                            "case_id":            case.get("case_id"),
                            "case_name":          case.get("name", ""),
                            "query":              case.get("query", ""),
                            "retrieved_contexts": [],
                            "answer":             "",
                            "analyst_draft":      "",
                            "final_report":       "",
                            "critic_feedback":    [],
                            "ground_truth":       (case.get("ground_truth") or "").strip(),
                            "eval_batch_id":      case.get("eval_batch_id"),
                            "model_type":         "production",
                            "error":              str(exc),
                        })
                return rows

            production_results = _run_async_fresh(_run_fresh())
            audit.log({"step": "production_run_complete", "n": len(production_results)})
        production_results = enrich_results_with_case_contract(production_results, test_cases)
        production_results = enrich_rows_with_retrieval_audit(production_results)
        production_results_full_path = write_results_jsonl(
            run_dir,
            filename="production_results_full.jsonl",
            rows=production_results,
        )
        production_answers_path = write_answers_jsonl(
            run_dir,
            filename="production_answers.jsonl",
            rows=production_results,
            include_production_fields=True,
        )
        audit.log({
            "step": "production_results_persisted",
            "full_file": str(production_results_full_path),
            "answers_file": str(production_answers_path),
            "n_ok": sum(1 for r in production_results if not r.get("error")),
            "n_error": sum(1 for r in production_results if r.get("error")),
        })

        # ---- 3. RAGAS Evaluation ----
        evaluator = RAGASEvaluator()

        logger.info("\nEvaluating BASELINE with RAGAS…")
        audit.log({"step": "ragas_eval_baseline_start"})
        baseline_scores = evaluator.evaluate_batch(baseline_results, "baseline", audit)
        baseline_scores_path = write_scores_jsonl(
            run_dir,
            filename="ragas_scores_baseline.jsonl",
            rows=baseline_scores,
        )

        logger.info("\nEvaluating FINALIZER output with RAGAS…")
        audit.log({"step": "ragas_eval_production_start"})
        production_scores = evaluator.evaluate_batch(production_results, "finalizer", audit)
        production_scores_path = write_scores_jsonl(
            run_dir,
            filename="ragas_scores_production.jsonl",
            rows=production_scores,
        )

        try:
            logger.info(
                "\nAgentic workflow node metrics%s…",
                " (status-only)" if args.agentic_status_only else " (status + OpenAI judge)",
            )
            run_agentic_node_evaluation_optional(
                production_results,
                run_dir,
                audit,
                enabled=not args.no_agentic_nodes,
                status_only=args.agentic_status_only,
            )
        except Exception as agent_exc:
            logger.exception("Agentic workflow evaluation failed: %s", agent_exc)
            audit.log({"step": "agentic_workflow_eval_failed", "error": str(agent_exc)})

        # ---- 4. Build comparison ----
        comparison = build_comparison(baseline_scores, production_scores)

        # Save paired comparison JSON
        metrics_path = run_dir / "paired_comparison.json"
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(comparison, f, indent=2, ensure_ascii=False)
        logger.info(f"Metrics saved: {metrics_path}")

        # Save Markdown report
        md_path = run_dir / "paired_comparison.md"
        md_path.write_text(render_markdown(comparison), encoding="utf-8")
        logger.info(f"Report saved:  {md_path}")

        audit.log({
            "step":              "comparison_complete",
            "queries_snapshot_file": str(snapshot_path),
            "baseline_results_full_file": str(baseline_results_full_path),
            "baseline_answers_file": str(baseline_answers_path),
            "production_results_full_file": str(production_results_full_path),
            "production_answers_file": str(production_answers_path),
            "baseline_scores_file": str(baseline_scores_path),
            "production_scores_file": str(production_scores_path),
            "metrics_file":      str(metrics_path),
            "report_file":       str(md_path),
            "aggregate_results": comparison["aggregate"],
        })

        # ---- 5. Print summary to console ----
        _print_summary(comparison)

    except Exception as exc:
        logger.exception(f"Fatal error in RAGAS comparison: {exc}")
        audit.log({"step": "comparison_fatal_error", "error": str(exc)})
        raise
    finally:
        audit.close()
        logger.info(f"\n📂 All outputs in: {run_dir}")


def _print_summary(comparison: Dict[str, Any]) -> None:
    METRICS = comparison["metrics_evaluated"]
    print("\n" + "=" * 70)
    print("  RAGAS BENCHMARKING SUMMARY — BaselineModel vs Finalizer")
    print("=" * 70)
    print(f"  Paired cases    : {comparison['n_paired_cases']} / {comparison['n_catalog_cases']}")
    print(f"  Judge LLM       : {comparison['judge_llm']}")
    print(f"  Judge Embeddings: {comparison['judge_embed_model']}")
    print()
    print(f"  {'Metric':<42}  {'Baseline':>8}  {'Final':>8}  {'Δ':>8}  {'Winner'}")
    print(f"  {'-'*42}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*10}")
    for m in METRICS:
        agg   = comparison["aggregate"][m]
        b_str = f"{agg['baseline_avg']:.3f}"   if agg["baseline_avg"]   is not None else "  N/A  "
        p_str = f"{agg['production_avg']:.3f}" if agg["production_avg"] is not None else "  N/A  "
        d     = agg["delta_avg"]
        d_str = (f"+{d:.3f}" if d >= 0 else f"{d:.3f}") if d is not None else "   N/A "
        w     = "🏆 Finalizer" if agg["winner"] == "production" else \
                "🏆 Baseline"   if agg["winner"] == "baseline"   else "—"
        print(f"  {_short_metric(m):<42}  {b_str:>8}  {p_str:>8}  {d_str:>8}  {w}")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
