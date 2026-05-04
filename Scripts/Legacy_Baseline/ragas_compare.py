"""
Scripts/Legacy_Baseline/ragas_compare.py

RAGAS Benchmarking Harness — Baseline vs Production Model Evaluation.

Measures the value-add of the full multi-agent Medallion pipeline against
the legacy single-pass baseline, using the RAGAS evaluation framework.

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
        comparison_metrics.json    — final metric table (per-case + aggregate delta)
        comparison_report.md       — human-readable Markdown summary
        ragas_audit.jsonl          — detailed per-case evaluation audit trail

Dependencies:
    pip install ragas langchain-ollama langchain-huggingface qdrant-client
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Lazy dependency check
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    from langchain_ollama import ChatOllama, OllamaEmbeddings
    from ragas import EvaluationDataset, evaluate, SingleTurnSample
    from ragas.metrics import (
        Faithfulness,
        AnswerRelevancy,
        LLMContextPrecisionWithoutReference,
    )
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
_EVAL_LOG_DIR = _PROJECT_ROOT / "logs" / "ragas_eval"
_BASELINE_LOG_DIR = _PROJECT_ROOT / "logs" / "baseline"
_E2E_LOG_DIR = _PROJECT_ROOT / "logs" / "router_e2e"
_QUERIES_FILE = _PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_queries.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("ragas_compare")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JUDGE_LLM_MODEL  = os.getenv("RAGAS_JUDGE_MODEL",   "llama3:latest")
JUDGE_EMBED_MODEL = os.getenv("RAGAS_EMBED_MODEL",  "nomic-embed-text")
OLLAMA_BASE_URL  = os.getenv("OLLAMA_BASE_URL",      "http://localhost:11434")

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
        - question         : from _meta.query
        - answer           : from the last analyst draft (or final_strategy if present)
        - retrieved_contexts : gold_context texts + silver value summary

    Log path pattern:
        logs/router_e2e/YYYYMMDD/<run_ts>_<idx>_<case_slug>_trace.jsonl
    """

    def find_latest_logs(self) -> Dict[str, Path]:
        """
        Find the most recent trace file for each test case name.
        Returns dict: {normalized_case_name: Path}
        """
        pattern = "**/*_trace.jsonl"
        all_logs = sorted(_E2E_LOG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        logger.info(f"ProductionLogExtractor: found {len(all_logs)} trace files under {_E2E_LOG_DIR}")

        case_map: Dict[str, Path] = {}
        for p in all_logs:
            # Extract case name from filename: <ts>_<idx>_<slug>_trace.jsonl
            parts = p.stem.replace("_trace", "").split("_")
            # Skip ts (first part, yyyymmdd_hhmmss) and idx (next integer)
            # Everything after idx is the slug
            slug_parts = []
            skip_count = 0
            for part in parts:
                if skip_count < 2:
                    skip_count += 1
                    continue
                slug_parts.append(part)
            slug = "_".join(slug_parts)
            if slug and slug not in case_map:
                case_map[slug] = p

        logger.info(f"ProductionLogExtractor: latest logs by case slug: {list(case_map.keys())}")
        return case_map

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

        # Collect contexts from the first retrieval step
        gold_texts: List[str] = []
        silver_summary: str   = ""

        # Collect the last analyst draft as the answer
        answer: str = ""

        for line in lines[1:]:
            node  = line.get("node", "")
            delta = line.get("delta_preview", {})

            if node == "retrieval_master":
                gold_ctx = delta.get("gold_context", [])
                for g in gold_ctx:
                    text = g.get("content") or g.get("text") or ""
                    if text:
                        gold_texts.append(text)
                # Summarise silver values as a compact context string
                silver = delta.get("silver_context", {})
                vals   = silver.get("values", {})
                if vals:
                    kv_pairs = [f"{k}={v}" for k, v in list(vals.items())[:20]]
                    silver_summary = "Silver layer values: " + " | ".join(kv_pairs)

            if node == "analyst":
                draft = delta.get("draft_report", "")
                if draft:
                    answer = draft  # overwrite → keeps the LATEST analyst revision

            # Use final_strategy markdown if finalizer ran
            if node == "finalizer":
                fs = delta.get("final_strategy", {})
                md = fs.get("markdown") or fs.get("final_report", {}).get("macro_summary", "")
                if md:
                    answer = md

        # Assemble retrieved_contexts list for RAGAS
        retrieved_contexts: List[str] = []
        for t in gold_texts:
            if t.strip():
                retrieved_contexts.append(t)
        if silver_summary:
            retrieved_contexts.append(silver_summary)

        if not answer:
            logger.warning(f"ProductionLogExtractor: no answer found in {path.name}")
            return None

        return {
            "case_name":          case_name,
            "query":              query,
            "retrieved_contexts": retrieved_contexts,
            "answer":             answer,
            "model_type":         "production",
            "log_file":           str(path),
        }

    def load_all(self, test_cases: List[Dict]) -> List[Dict[str, Any]]:
        """Load production results for all test cases, matching by name slug."""
        case_logs  = self.find_latest_logs()
        results: List[Dict[str, Any]] = []

        for case in test_cases:
            name = case.get("name", "")
            # Build slug the same way the test runner does
            slug = name.lower().replace(" ", "_").replace("-", "_").replace("(", "").replace(")", "")[:40]

            # Try exact slug match first, then fuzzy prefix match
            matched_path: Optional[Path] = None
            for log_slug, log_path in case_logs.items():
                if slug in log_slug or log_slug in slug:
                    matched_path = log_path
                    break
                # Also try matching on individual words
                slug_words = set(slug.split("_"))
                log_words  = set(log_slug.split("_"))
                if len(slug_words & log_words) >= 2:
                    matched_path = log_path
                    break

            if matched_path is None:
                logger.warning(
                    f"ProductionLogExtractor: no trace log matched case '{name}' (slug='{slug}'). "
                    "Skipping — run the E2E harness first or use --mode fresh."
                )
                results.append({
                    "case_name":          name,
                    "query":              case.get("query", ""),
                    "retrieved_contexts": [],
                    "answer":             "MISSING — no trace log found for this case",
                    "model_type":         "production",
                    "error":              "no_trace_log",
                })
                continue

            parsed = self._parse_trace(matched_path)
            if parsed is None:
                results.append({
                    "case_name":   name,
                    "query":       case.get("query", ""),
                    "retrieved_contexts": [],
                    "answer":      "PARSE_ERROR",
                    "model_type":  "production",
                    "error":       "parse_failed",
                })
            else:
                logger.info(f"  ✅ Loaded production result for '{name}' from {matched_path.name}")
                results.append(parsed)

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
        logger.info(f"RAGASEvaluator: judge_llm={JUDGE_LLM_MODEL} | judge_embed={JUDGE_EMBED_MODEL}")

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

        self._judge_llm  = LangchainLLMWrapper(_llm)
        self._judge_emb  = LangchainEmbeddingsWrapper(_emb)

        # Build metric instances with shared judge
        self._metrics = [
            Faithfulness(llm=self._judge_llm),
            AnswerRelevancy(llm=self._judge_llm, embeddings=self._judge_emb),
            LLMContextPrecisionWithoutReference(llm=self._judge_llm),
        ]
        self._metric_names = ["faithfulness", "answer_relevancy", "llm_context_precision_without_reference"]

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
            "judge_llm": JUDGE_LLM_MODEL,
            "judge_embed": JUDGE_EMBED_MODEL,
            "metrics": self._metric_names,
        })

        scored: List[Dict[str, Any]] = []

        for i, res in enumerate(results):
            case_name = res.get("case_name", f"case_{i+1}")
            query     = res.get("query", "")
            answer    = res.get("answer", "")
            contexts  = res.get("retrieved_contexts", [])

            logger.info(f"  [{i+1}/{len(results)}] RAGAS evaluating: '{case_name}' | model={model_label}")

            # Skip cases with missing data
            if res.get("error") or not answer or answer.startswith("MISSING") or answer.startswith("ERROR"):
                row = {
                    "case_name": case_name,
                    "query":     query,
                    "model":     model_label,
                    "faithfulness":                              None,
                    "answer_relevancy":                         None,
                    "llm_context_precision_without_reference":  None,
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
                sample  = SingleTurnSample(
                    user_input=query,
                    response=answer,
                    retrieved_contexts=contexts,
                )
                dataset = EvaluationDataset(samples=[sample])
                result  = evaluate(dataset=dataset, metrics=self._metrics)
                scores_df = result.to_pandas()
                row_scores = scores_df.iloc[0].to_dict() if not scores_df.empty else {}

                latency_ms = (time.monotonic() - t0) * 1000

                row = {
                    "case_name": case_name,
                    "query":     query,
                    "model":     model_label,
                    "faithfulness":
                        _safe_float(row_scores.get("faithfulness")),
                    "answer_relevancy":
                        _safe_float(row_scores.get("answer_relevancy")),
                    "llm_context_precision_without_reference":
                        _safe_float(row_scores.get("llm_context_precision_without_reference")),
                    "latency_ragas_ms": round(latency_ms, 1),
                    "context_count":    len(contexts),
                    "answer_len":       len(answer),
                    "error": None,
                }
                scored.append(row)

                audit.log({
                    "step":       "ragas_case_complete",
                    "case":       case_name,
                    "model":      model_label,
                    "scores":     {k: row[k] for k in self._metric_names},
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
                    f"latency={latency_ms:.0f}ms"
                )

            except Exception as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                logger.error(f"    ❌ RAGAS failed for '{case_name}': {exc}")
                row = {
                    "case_name": case_name,
                    "query":     query,
                    "model":     model_label,
                    "faithfulness":                              None,
                    "answer_relevancy":                         None,
                    "llm_context_precision_without_reference":  None,
                    "latency_ragas_ms": round(latency_ms, 1),
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


def build_comparison(
    baseline_scores: List[Dict[str, Any]],
    production_scores: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Merge baseline and production scores by case_name.
    Returns a comparison dict ready for JSON export + Markdown rendering.
    """
    METRICS = [
        "faithfulness",
        "answer_relevancy",
        "llm_context_precision_without_reference",
    ]

    # Index by case_name
    base_by_case = {r["case_name"]: r for r in baseline_scores}
    prod_by_case = {r["case_name"]: r for r in production_scores}

    all_cases = sorted(set(list(base_by_case.keys()) + list(prod_by_case.keys())))

    per_case: List[Dict] = []
    for case_name in all_cases:
        b = base_by_case.get(case_name, {})
        p = prod_by_case.get(case_name, {})
        row: Dict[str, Any] = {"case_name": case_name}
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
        per_case.append(row)

    # Aggregate averages
    aggregate: Dict[str, Any] = {}
    for m in METRICS:
        base_avg = _avg([base_by_case.get(c, {}).get(m) for c in all_cases])
        prod_avg = _avg([prod_by_case.get(c, {}).get(m) for c in all_cases])
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

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "n_cases":          len(all_cases),
        "judge_llm":        JUDGE_LLM_MODEL,
        "judge_embed_model": JUDGE_EMBED_MODEL,
        "metrics_evaluated": METRICS,
        "per_case":         per_case,
        "aggregate":        aggregate,
    }


def render_markdown(comparison: Dict[str, Any]) -> str:
    """Render the comparison dict to a Markdown report."""
    METRICS = comparison["metrics_evaluated"]
    now_str = comparison["generated_at"][:19].replace("T", " ")

    lines = [
        "# RAGAS Benchmarking Report — Baseline vs Production",
        f"> Generated: {now_str} UTC  |  Judge LLM: `{comparison['judge_llm']}`  "
        f"|  Cases: {comparison['n_cases']}",
        "",
        "---",
        "",
        "## 1. Aggregate Results",
        "",
        "| Metric | Baseline | Production | Δ Delta | Winner |",
        "|--------|----------|------------|---------|--------|",
    ]
    for m in METRICS:
        agg = comparison["aggregate"][m]
        b   = f"{agg['baseline_avg']:.3f}"   if agg["baseline_avg"]   is not None else "N/A"
        p   = f"{agg['production_avg']:.3f}" if agg["production_avg"] is not None else "N/A"
        d   = (f"+{agg['delta_avg']:.3f}" if agg["delta_avg"] >= 0 else f"{agg['delta_avg']:.3f}") \
              if agg["delta_avg"] is not None else "N/A"
        w   = "🏆 Production" if agg["winner"] == "production" else \
              "🏆 Baseline"   if agg["winner"] == "baseline"   else "—"
        short = _short_metric(m)
        lines.append(f"| **{short}** | {b} | {p} | {d} | {w} |")

    lines += [
        "",
        "---",
        "",
        "## 2. Per-Case Breakdown",
        "",
    ]

    for row in comparison["per_case"]:
        lines.append(f"### Case: {row['case_name']}")
        lines.append("")
        lines.append("| Metric | Baseline | Production | Δ | Winner |")
        lines.append("|--------|----------|------------|---|--------|")
        for m in METRICS:
            cell = row.get(m, {})
            b    = f"{cell['baseline']:.3f}"   if cell.get("baseline")   is not None else "N/A"
            p    = f"{cell['production']:.3f}" if cell.get("production") is not None else "N/A"
            d_v  = cell.get("delta")
            d    = (f"+{d_v:.3f}" if d_v >= 0 else f"{d_v:.3f}") if d_v is not None else "N/A"
            w    = "🏆 Prod" if cell.get("winner") == "production" else \
                   "🏆 Base" if cell.get("winner") == "baseline"   else "—"
            short = _short_metric(m)
            lines.append(f"| {short} | {b} | {p} | {d} | {w} |")
        lines.append("")

    lines += [
        "---",
        "",
        "## 3. Metric Definitions",
        "",
        "| Metric | Definition | Requirement |",
        "|--------|------------|-------------|",
        "| **Faithfulness** | Fraction of claims in the answer that are verifiably grounded in the "
        "retrieved context. Measures hallucination risk. | Reference-free |",
        "| **Answer Relevancy** | Semantic similarity between the generated answer and the question. "
        "High scores mean the answer stays on-topic. | Reference-free |",
        "| **Context Precision** | Fraction of retrieved chunks that are judged relevant to the question. "
        "Measures retrieval signal quality. | Reference-free |",
        "",
        "---",
        "",
        "## 4. Interpretation Guide",
        "",
        f"- ✅ Score ≥ {_PASS_THRESHOLD}  → Acceptable",
        f"- ⚠️ Score {_WARN_THRESHOLD}–{_PASS_THRESHOLD} → Needs attention",
        f"- ❌ Score < {_WARN_THRESHOLD} → Failing",
        "",
        "A positive Δ (production – baseline) indicates the production system outperforms",
        "the legacy single-pass baseline on that metric.",
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
    }.get(m, m)


# ===========================================================================
# Section 5 — Orchestrator
# ===========================================================================

def load_test_cases() -> List[Dict]:
    with open(_QUERIES_FILE, encoding="utf-8") as f:
        return json.load(f).get("cases", [])


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


def run_baseline_fresh(test_cases: List[Dict], run_dir: Path) -> List[Dict[str, Any]]:
    """Run the baseline model on all test cases."""
    from Scripts.Legacy_Baseline.baseline_model import BaselineModel, AuditLogger as BL_AuditLogger

    bl_run_dir = _BASELINE_LOG_DIR / run_dir.name
    bl_audit   = BL_AuditLogger(bl_run_dir)
    model      = BaselineModel()
    try:
        results = model.run_all_cases(audit=bl_audit)
    finally:
        bl_audit.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAGAS Benchmarking: Baseline vs Production RAG pipeline"
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
        "--out-dir",
        type=str,
        default=None,
        help="Output directory (default: logs/ragas_eval/YYYYMMDD_HHMMSS)",
    )
    args = parser.parse_args()

    # ---- Setup run directory ----
    run_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.out_dir) if args.out_dir else _EVAL_LOG_DIR / run_ts
    audit   = AuditLogger(run_dir)

    audit.log({
        "step":             "comparison_start",
        "mode":             args.mode,
        "baseline_source":  args.baseline_results or "fresh",
        "run_dir":          str(run_dir),
        "judge_llm":        JUDGE_LLM_MODEL,
        "judge_embed":      JUDGE_EMBED_MODEL,
    })
    logger.info(f"\n{'='*70}")
    logger.info(f"RAGAS Comparison  |  mode={args.mode}  |  run_dir={run_dir}")
    logger.info(f"{'='*70}\n")

    try:
        test_cases = load_test_cases()
        audit.log({"step": "test_cases_loaded", "n": len(test_cases),
                   "file": str(_QUERIES_FILE)})

        # ---- 1. Baseline results ----
        if args.baseline_results:
            baseline_results = load_baseline_from_file(Path(args.baseline_results))
        else:
            logger.info("Running baseline model on all test cases…")
            audit.log({"step": "baseline_run_start"})
            t0 = time.monotonic()
            baseline_results = run_baseline_fresh(test_cases, run_dir)
            audit.log({"step": "baseline_run_complete",
                       "n": len(baseline_results),
                       "elapsed_s": round(time.monotonic() - t0, 1)})

        # ---- 2. Production results ----
        if args.mode == "logs":
            logger.info("Extracting production results from router_e2e trace logs…")
            audit.log({"step": "production_extract_start", "source": "router_e2e_logs"})
            extractor = ProductionLogExtractor()
            production_results = extractor.load_all(test_cases)
            audit.log({"step": "production_extract_complete",
                       "n_ok":    sum(1 for r in production_results if not r.get("error")),
                       "n_error": sum(1 for r in production_results if r.get("error"))})
        else:
            # Fresh production run via LangGraph
            logger.info("Running production LangGraph pipeline on all test cases…")
            audit.log({"step": "production_run_start", "source": "langgraph_fresh"})
            from Scripts.main import run_pipeline_for_query  # type: ignore
            production_results = []
            for case in test_cases:
                try:
                    state = run_pipeline_for_query(case["query"])
                    gold_texts = [g.get("content", g.get("text", "")) for g in state.get("gold_context", [])]
                    answer     = state.get("draft_report", "") or ""
                    fs         = state.get("final_strategy", {})
                    if fs:
                        answer = fs.get("markdown") or answer
                    production_results.append({
                        "case_name":          case["name"],
                        "query":              case["query"],
                        "retrieved_contexts": gold_texts,
                        "answer":             answer,
                        "model_type":         "production",
                    })
                except Exception as exc:
                    logger.error(f"Production run failed for '{case['name']}': {exc}")
                    production_results.append({
                        "case_name":          case["name"],
                        "query":              case["query"],
                        "retrieved_contexts": [],
                        "answer":             "",
                        "model_type":         "production",
                        "error":              str(exc),
                    })
            audit.log({"step": "production_run_complete", "n": len(production_results)})

        # ---- 3. RAGAS Evaluation ----
        evaluator = RAGASEvaluator()

        logger.info("\nEvaluating BASELINE with RAGAS…")
        audit.log({"step": "ragas_eval_baseline_start"})
        baseline_scores = evaluator.evaluate_batch(baseline_results, "baseline", audit)

        logger.info("\nEvaluating PRODUCTION with RAGAS…")
        audit.log({"step": "ragas_eval_production_start"})
        production_scores = evaluator.evaluate_batch(production_results, "production", audit)

        # ---- 4. Build comparison ----
        comparison = build_comparison(baseline_scores, production_scores)

        # Save metrics JSON
        metrics_path = run_dir / "comparison_metrics.json"
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(comparison, f, indent=2, ensure_ascii=False)
        logger.info(f"Metrics saved: {metrics_path}")

        # Save Markdown report
        md_path = run_dir / "comparison_report.md"
        md_path.write_text(render_markdown(comparison), encoding="utf-8")
        logger.info(f"Report saved:  {md_path}")

        audit.log({
            "step":              "comparison_complete",
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
    print("  RAGAS BENCHMARKING SUMMARY — Baseline vs Production")
    print("=" * 70)
    print(f"  Cases evaluated : {comparison['n_cases']}")
    print(f"  Judge LLM       : {comparison['judge_llm']}")
    print(f"  Judge Embeddings: {comparison['judge_embed_model']}")
    print()
    print(f"  {'Metric':<42}  {'Baseline':>8}  {'Prod':>8}  {'Δ':>8}  {'Winner'}")
    print(f"  {'-'*42}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*10}")
    for m in METRICS:
        agg   = comparison["aggregate"][m]
        b_str = f"{agg['baseline_avg']:.3f}"   if agg["baseline_avg"]   is not None else "  N/A  "
        p_str = f"{agg['production_avg']:.3f}" if agg["production_avg"] is not None else "  N/A  "
        d     = agg["delta_avg"]
        d_str = (f"+{d:.3f}" if d >= 0 else f"{d:.3f}") if d is not None else "   N/A "
        w     = "🏆 Production" if agg["winner"] == "production" else \
                "🏆 Baseline"   if agg["winner"] == "baseline"   else "—"
        print(f"  {_short_metric(m):<42}  {b_str:>8}  {p_str:>8}  {d_str:>8}  {w}")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
