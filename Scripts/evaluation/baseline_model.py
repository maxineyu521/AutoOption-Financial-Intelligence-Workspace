"""
Scripts/Legacy_Baseline/baseline_model.py

Legacy Baseline RAG Model — single-pass, no multi-agent loop.

Design philosophy:
    This script represents the v0 architecture against which the current
    production system (LangGraph multi-agent + Medallion data) is benchmarked.
    It deliberately omits every production enhancement so the RAGAS delta
    reflects the value added by the full pipeline.

Baseline characteristics (vs production):
    ┌──────────────────────┬──────────────────────────┬──────────────────────────────┐
    │ Dimension            │ Baseline                 │ Production                   │
    ├──────────────────────┼──────────────────────────┼──────────────────────────────┤
    │ Retrieval            │ Dense cosine only        │ Hybrid (dense + sparse + RRF)│
    │ Metadata filtering   │ None                     │ TimeAdapter + type filters   │
    │ Data sources         │ Gold (Qdrant) only       │ Gold + Silver (DuckDB/Parquet)│
    │ Context enrichment   │ None                     │ Macro snapshot injection     │
    │ Agent loop           │ Single LLM call          │ Analyst→Checker→Critic→Final │
    │ Hallucination guard  │ None                     │ Deterministic + LLM audit    │
    │ Output format        │ Free text                │ Structured Pydantic FinalRpt │
    └──────────────────────┴──────────────────────────┴──────────────────────────────┘

Usage:
    # Run all test cases and write results to logs/baseline/
    python -m Scripts.Legacy_Baseline.baseline_model

    # Run a single query
    python -m Scripts.Legacy_Baseline.baseline_model --query "What is the IV on SPY?"

Output:
    logs/baseline/YYYYMMDD_HHMMSS/
        baseline_results.jsonl   — per-case structured result (for RAGAS)
        baseline_audit.jsonl     — detailed per-step audit trail
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Lazy imports — allow importing the module without all deps installed
# ---------------------------------------------------------------------------
try:
    from langchain_ollama import ChatOllama
    from langchain_core.prompts import ChatPromptTemplate
    from dotenv import load_dotenv
    from Scripts.vector_store.connection import get_qdrant_client, get_embedding_model
    from Scripts.core.financial_config import get_analyst_system_prompt
except ImportError as _e:
    raise SystemExit(
        f"Missing dependency: {_e}\n"
        "Run: pip install -r requirements.txt"
    )

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "logs" / "baseline"
DEFAULT_TRUTH_FILE = _PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_ground_truth_queries.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("baseline_model")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
QDRANT_URL    = os.getenv("QDRANT_HOST", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "financial_rag_gold")
EMBED_MODEL   = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-base-en-v1.5")
OLLAMA_MODEL  = os.getenv("BASELINE_LLM_MODEL", "llama3:latest")
TOP_K         = int(os.getenv("BASELINE_TOP_K", "5"))

# ---- Prompt template (intentionally minimal — no lineage enforcement) ----
_BASELINE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     get_analyst_system_prompt() + "\n\n"
     "[BASELINE_EVAL_MODE]\n"
     "You are running in legacy baseline evaluation mode.\n"
     "- Single pass only (no critic/checker loop).\n"
     "- Use only provided retrieval context.\n"
     "- If context is insufficient, state that explicitly.\n"
     "- Keep answer concise and factual.\n"),
    ("human",
     "[CONTEXT]\n{context}\n\n"
     "[QUESTION]\n{question}\n\n"
     "Provide a direct, structured answer:"),
])


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------

class AuditLogger:
    """Writes structured JSONL audit events to a timestamped run directory."""

    def __init__(self, run_dir: Path):
        run_dir.mkdir(parents=True, exist_ok=True)
        self._results_fh = open(run_dir / "baseline_results.jsonl", "w", encoding="utf-8")
        self._audit_fh   = open(run_dir / "baseline_audit.jsonl",  "w", encoding="utf-8")
        self._run_dir = run_dir
        logger.info(f"AuditLogger: writing to {run_dir}")

    def log_audit(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self._audit_fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._audit_fh.flush()

    def log_result(self, result: Dict[str, Any]) -> None:
        result.setdefault("ts", datetime.now(timezone.utc).isoformat())
        self._results_fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        self._results_fh.flush()

    def close(self) -> None:
        self._results_fh.close()
        self._audit_fh.close()
        logger.info(f"AuditLogger: closed — logs in {self._run_dir}")


# ---------------------------------------------------------------------------
# Baseline Model
# ---------------------------------------------------------------------------

class BaselineModel:
    """
    Single-pass retrieval + generation without any multi-agent guardrails.
    Designed to be the RAGAS evaluation counterpart to the production system.
    """

    def __init__(self):
        logger.info("BaselineModel: initialising components…")

        # 1. Embedding model (reuse production factory for parity and stability)
        t0 = time.monotonic()
        self.embeddings = get_embedding_model()
        logger.info(
            f"BaselineModel: embeddings loaded in {time.monotonic()-t0:.1f}s "
            f"(provider={type(self.embeddings).__name__})"
        )

        # 2. Qdrant client (reuse production connection layer to avoid 403 drift)
        self.qdrant = get_qdrant_client()
        logger.info(f"BaselineModel: Qdrant client ready | collection={QDRANT_COLLECTION}")

        # 3. LLM (no structured output, no chain-of-thought enforcement)
        self.llm = ChatOllama(model=OLLAMA_MODEL, temperature=0.1)
        logger.info(f"BaselineModel: LLM={OLLAMA_MODEL}")

        self._chain = _BASELINE_PROMPT | self.llm

    # ------------------------------------------------------------------ #
    #  Retrieval — dense cosine similarity only (no hybrid, no metadata)  #
    # ------------------------------------------------------------------ #

    def retrieve(self, query: str, top_k: int = TOP_K) -> List[Dict[str, Any]]:
        """Simple dense retrieval — no metadata filter, no sparse expansion."""
        query_vec = self.embeddings.embed_query(query)

        try:
            hits = self._query_qdrant_hits(query_vec, top_k)
        except Exception as exc:
            logger.warning(f"BaselineModel: Qdrant search failed ({exc}) — returning empty context")
            return []

        results = []
        for hit in hits:
            payload = hit.payload or {}
            text = (
                payload.get("page_content")
                or payload.get("content")
                or payload.get("text")
                or ""
            )
            results.append({
                "id":     str(hit.id),
                "score":  round(hit.score, 6),
                "text":   text,
                "source": payload.get("source_type", payload.get("source", "unknown")),
                "date":   payload.get("record_date", payload.get("filed_at", "")),
                "ticker": payload.get("ticker", ""),
            })

        return results

    def _query_qdrant_hits(self, query_vec: List[float], top_k: int) -> List[Any]:
        """
        Compatibility layer for Qdrant client/server combinations.
        Priority:
          1) query_points using named vector "dense" (production collection layout)
          2) query_points plain vector
          3) legacy search API with named vector tuple
          4) legacy search API plain vector
        """
        # New API path
        if hasattr(self.qdrant, "query_points"):
            try:
                qp = self.qdrant.query_points(
                    collection_name=QDRANT_COLLECTION,
                    query=query_vec,
                    using="dense",
                    limit=top_k,
                    with_payload=True,
                    with_vectors=False,
                )
                return getattr(qp, "points", qp) or []
            except Exception:
                qp = self.qdrant.query_points(
                    collection_name=QDRANT_COLLECTION,
                    query=query_vec,
                    limit=top_k,
                    with_payload=True,
                    with_vectors=False,
                )
                return getattr(qp, "points", qp) or []

        # Legacy API path
        if hasattr(self.qdrant, "search"):
            try:
                return self.qdrant.search(
                    collection_name=QDRANT_COLLECTION,
                    query_vector=("dense", query_vec),
                    limit=top_k,
                    with_payload=True,
                )
            except Exception:
                return self.qdrant.search(
                    collection_name=QDRANT_COLLECTION,
                    query_vector=query_vec,
                    limit=top_k,
                    with_payload=True,
                )

        raise RuntimeError("Unsupported qdrant_client API: neither query_points nor search is available.")

    # ------------------------------------------------------------------ #
    #  Generation — single LLM call                                        #
    # ------------------------------------------------------------------ #

    def generate(self, query: str, contexts: List[Dict[str, Any]]) -> str:
        """Single-pass LLM call with context concatenation."""
        if not contexts:
            context_str = "(No relevant context retrieved — answer based on general knowledge only.)"
        else:
            parts = []
            for i, ctx in enumerate(contexts, 1):
                meta = f"[{ctx.get('source','?')} | {ctx.get('date','?')} | score={ctx.get('score',0):.3f}]"
                parts.append(f"--- Context {i} {meta} ---\n{ctx.get('text', '')}")
            context_str = "\n\n".join(parts)

        try:
            response = self._chain.invoke({"context": context_str, "question": query})
            return getattr(response, "content", str(response)).strip()
        except Exception as exc:
            logger.error(f"BaselineModel: LLM call failed: {exc}")
            return f"ERROR: LLM call failed — {exc}"

    # ------------------------------------------------------------------ #
    #  Full pipeline run for one query                                     #
    # ------------------------------------------------------------------ #

    def run_query(
        self,
        query: str,
        case_name: str = "adhoc",
        audit: Optional[AuditLogger] = None,
    ) -> Dict[str, Any]:
        """
        Execute full baseline pipeline for one query.

        Returns a result dict ready for RAGAS evaluation:
            {
              "case_name":          str,
              "query":              str,
              "retrieved_contexts": List[str],   # plain text list for RAGAS
              "answer":             str,
              "latency_retrieval_ms": float,
              "latency_generation_ms": float,
              "latency_total_ms":     float,
              "context_count":        int,
              "avg_retrieval_score":  float,
              "model":               str,
              "embed_model":         str,
              "top_k":               int,
            }
        """
        logger.info(f"BaselineModel.run_query: case='{case_name}' | query='{query[:80]}'")
        run_start = time.monotonic()

        # -- Step 1: Retrieval --
        if audit:
            audit.log_audit({"step": "retrieval_start", "case": case_name, "query": query})
        t_retr = time.monotonic()
        contexts = self.retrieve(query)
        latency_retr_ms = (time.monotonic() - t_retr) * 1000

        if audit:
            audit.log_audit({
                "step": "retrieval_complete",
                "case": case_name,
                "n_contexts": len(contexts),
                "latency_ms": round(latency_retr_ms, 1),
                "contexts_preview": [
                    {"id": c["id"], "score": c["score"], "source": c["source"], "date": c["date"],
                     "text_snippet": c["text"][:120]}
                    for c in contexts
                ],
            })
        logger.info(
            f"  Retrieval: {len(contexts)} docs in {latency_retr_ms:.0f}ms | "
            f"top_score={contexts[0]['score']:.3f}" if contexts else
            f"  Retrieval: 0 docs in {latency_retr_ms:.0f}ms"
        )

        # -- Step 2: Generation --
        if audit:
            audit.log_audit({"step": "generation_start", "case": case_name, "model": OLLAMA_MODEL})
        t_gen = time.monotonic()
        answer = self.generate(query, contexts)
        latency_gen_ms = (time.monotonic() - t_gen) * 1000

        if audit:
            audit.log_audit({
                "step": "generation_complete",
                "case": case_name,
                "latency_ms": round(latency_gen_ms, 1),
                "answer_len": len(answer),
                "answer_preview": answer[:300],
            })
        logger.info(f"  Generation: {latency_gen_ms:.0f}ms | answer_len={len(answer)}")

        # -- Build result --
        latency_total_ms = (time.monotonic() - run_start) * 1000
        avg_score = (
            sum(c["score"] for c in contexts) / len(contexts) if contexts else 0.0
        )

        result = {
            "case_name":              case_name,
            "query":                  query,
            "retrieved_contexts":     [c["text"] for c in contexts],  # for RAGAS
            "context_metadata":       contexts,                        # for audit
            "answer":                 answer,
            "latency_retrieval_ms":   round(latency_retr_ms, 1),
            "latency_generation_ms":  round(latency_gen_ms, 1),
            "latency_total_ms":       round(latency_total_ms, 1),
            "context_count":          len(contexts),
            "avg_retrieval_score":    round(avg_score, 4),
            "model":                  OLLAMA_MODEL,
            "embed_model":            EMBED_MODEL,
            "top_k":                  TOP_K,
            "model_type":             "baseline",
        }

        if audit:
            audit.log_result(result)
            audit.log_audit({
                "step": "run_complete",
                "case": case_name,
                "latency_total_ms": round(latency_total_ms, 1),
                "verdict": "ok" if answer and not answer.startswith("ERROR") else "error",
            })

        return result

    # ------------------------------------------------------------------ #
    #  Batch runner                                                        #
    # ------------------------------------------------------------------ #

    def run_all_cases(
        self,
        queries_file: Optional[Path] = None,
        audit: Optional[AuditLogger] = None,
    ) -> List[Dict[str, Any]]:
        """
        Run all test cases from the E2E query catalogue.
        Returns list of result dicts.
        """
        if queries_file is None:
            queries_file = _PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_queries.json"

        if not queries_file.exists():
            raise FileNotFoundError(f"Test catalogue not found: {queries_file}")

        with open(queries_file, encoding="utf-8") as f:
            catalogue = json.load(f)
        cases = catalogue.get("cases", [])
        logger.info(f"BaselineModel: running {len(cases)} cases from {queries_file.name}")

        if audit:
            audit.log_audit({
                "step": "batch_start",
                "n_cases": len(cases),
                "catalogue": str(queries_file),
                "model": OLLAMA_MODEL,
                "embed_model": EMBED_MODEL,
                "top_k": TOP_K,
            })

        results = []
        for i, case in enumerate(cases, 1):
            name  = case.get("name", f"case_{i}")
            query = case.get("query", "")
            logger.info(f"\n{'='*60}")
            logger.info(f"Case {i}/{len(cases)}: {name}")
            logger.info(f"{'='*60}")

            try:
                result = self.run_query(query, case_name=name, audit=audit)
                result["case_meta"] = {
                    "expected_sources":     case.get("expected_sources", []),
                    "expected_time_window": case.get("expected_time_window", ""),
                    "notes":                case.get("notes", ""),
                }
                results.append(result)
                logger.info(
                    f"  ✅ Done | latency={result['latency_total_ms']}ms | "
                    f"contexts={result['context_count']}"
                )
            except Exception as exc:
                logger.error(f"  ❌ Case failed: {exc}")
                error_result = {
                    "case_name": name,
                    "query": query,
                    "retrieved_contexts": [],
                    "answer": f"ERROR: {exc}",
                    "latency_total_ms": 0,
                    "context_count": 0,
                    "model_type": "baseline",
                    "error": str(exc),
                }
                results.append(error_result)
                if audit:
                    audit.log_audit({"step": "case_error", "case": name, "error": str(exc)})

        if audit:
            audit.log_audit({
                "step": "batch_complete",
                "n_results": len(results),
                "n_errors": sum(1 for r in results if "error" in r),
                "avg_latency_ms": round(
                    sum(r.get("latency_total_ms", 0) for r in results) / max(len(results), 1), 1
                ),
            })

        return results


def _compute_eval_batch_id(cases: Sequence[Dict[str, Any]], queries_file: Path) -> str:
    import hashlib

    payload = {
        "queries_file": str(queries_file),
        "cases": [{"name": c.get("name"), "query": c.get("query")} for c in cases],
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _load_cases(queries_file: Path, query: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str]:
    payload = json.loads(queries_file.read_text(encoding="utf-8-sig"))
    cases = list(payload.get("cases", []))
    if query:
        cases = [case for case in cases if str(case.get("query")) == query]
    eval_batch_id = _compute_eval_batch_id(cases, queries_file)
    for case in cases:
        case["eval_batch_id"] = eval_batch_id
    return cases, eval_batch_id


def _baseline_full_row(result: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(result)
    row["answer_for_eval"] = row.get("answer", "")
    row["answer_rendered_markdown"] = row.get("answer", "")
    row["ground_truth"] = (case.get("ground_truth") or "").strip()
    row["expected_sources"] = case.get("expected_sources", [])
    row["expected_time_window"] = case.get("expected_time_window")
    row["case_meta"] = case.get("case_meta", {})
    row["eval_batch_id"] = case.get("eval_batch_id")
    row["case_id"] = case.get("case_id")
    row["trace_status"] = "complete" if row.get("answer") and not str(row.get("answer")).startswith("ERROR") else "error"
    row["retrieved_sources"] = [
        str(meta.get("source") or meta.get("source_type") or "").strip().lower()
        for meta in row.get("context_metadata") or []
        if str(meta.get("source") or meta.get("source_type") or "").strip()
    ]
    return row


def _baseline_answer_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "case_id": row.get("case_id"),
        "case_name": row.get("case_name"),
        "query": row.get("query"),
        "answer": row.get("answer"),
        "answer_for_eval": row.get("answer_for_eval"),
        "retrieved_contexts": row.get("retrieved_contexts"),
        "time_window": row.get("expected_time_window"),
        "trace_status": row.get("trace_status"),
        "requested_sources": row.get("expected_sources"),
        "retrieved_sources": row.get("retrieved_sources"),
    }


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_baseline_bundle(
    *,
    queries_file: Path = DEFAULT_TRUTH_FILE,
    run_dir: Path,
    query: Optional[str] = None,
) -> Dict[str, Path]:
    """
    Fresh-run helper for the evaluation orchestrator.

    Produces:
      - baseline_results_full.jsonl
      - baseline_answers.jsonl
      - baseline_summary.json
      - baseline_audit.jsonl
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLogger(run_dir)
    model = BaselineModel()
    try:
        if query:
            cases, eval_batch_id = _load_cases(queries_file, query=query)
            if not cases:
                raise ValueError(f"Query not found in catalogue: {query}")
            results = []
            for case in cases:
                result = model.run_query(str(case.get("query") or ""), case_name=str(case.get("name") or "adhoc"), audit=audit)
                results.append(_baseline_full_row(result, case))
        else:
            cases, eval_batch_id = _load_cases(queries_file)
            raw_results = model.run_all_cases(queries_file=queries_file, audit=audit)
            case_map = {str(case.get("query")): case for case in cases}
            results = [_baseline_full_row(result, case_map.get(str(result.get("query")), {})) for result in raw_results]

        full_path = run_dir / "baseline_results_full.jsonl"
        answers_path = run_dir / "baseline_answers.jsonl"
        summary_path = run_dir / "baseline_summary.json"
        _write_jsonl(full_path, results)
        _write_jsonl(answers_path, [_baseline_answer_row(row) for row in results])
        summary = {
            "queries_file": str(queries_file),
            "n_cases": len(results),
            "n_errors": sum(1 for row in results if row.get("error") or str(row.get("answer", "")).startswith("ERROR")),
            "eval_batch_id": eval_batch_id,
            "model": OLLAMA_MODEL,
            "embed_model": EMBED_MODEL,
            "top_k": TOP_K,
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "results_full": full_path,
            "answers": answers_path,
            "summary": summary_path,
            "audit": run_dir / "baseline_audit.jsonl",
            "legacy_results": run_dir / "baseline_results.jsonl",
        }
    finally:
        audit.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Legacy Baseline RAG model")
    parser.add_argument("--query", type=str, default=None,
                        help="Single query to run (default: run all test cases)")
    parser.add_argument("--queries-file", type=str, default=str(DEFAULT_TRUTH_FILE),
                        help="Query / truth catalogue JSON file")
    parser.add_argument("--top-k", type=int, default=TOP_K,
                        help=f"Number of contexts to retrieve (default: {TOP_K})")
    parser.add_argument("--out-dir", type=str, default=None,
                        help="Output directory for logs (default: logs/baseline/YYYYMMDD_HHMMSS)")
    args = parser.parse_args()

    # Determine run directory
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.out_dir) if args.out_dir else _LOG_DIR / run_ts
    audit = AuditLogger(run_dir)

    model = BaselineModel()

    try:
        if args.query:
            cases, _ = _load_cases(Path(args.queries_file), query=args.query)
            if cases:
                case = cases[0]
                result = model.run_query(str(case.get("query") or ""), case_name=str(case.get("name") or "adhoc"), audit=audit)
            else:
                result = model.run_query(args.query, case_name="adhoc", audit=audit)
            print("\n" + "="*60)
            print("BASELINE ANSWER:")
            print("="*60)
            print(result["answer"])
            print(f"\nContexts retrieved: {result['context_count']}")
            print(f"Latency: {result['latency_total_ms']}ms")
        else:
            results = model.run_all_cases(queries_file=Path(args.queries_file), audit=audit)
            print(f"\n✅ Completed {len(results)} cases — results in {run_dir}")
            print(f"   Results file: {run_dir / 'baseline_results.jsonl'}")
            print(f"   Audit file:   {run_dir / 'baseline_audit.jsonl'}")
    finally:
        audit.close()


if __name__ == "__main__":
    main()
