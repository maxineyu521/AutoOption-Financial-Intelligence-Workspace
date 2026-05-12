"""
Evaluation orchestrator for the contract-first financial benchmarking stack.

This script keeps the runtime chain inside ``Scripts/evaluation`` and can:

1. Reuse an existing run directory that already contains:
   - baseline_results_full.jsonl
   - production_results_full.jsonl
2. Fresh-run the evaluation stack end-to-end:
   - baseline model
   - production graph
   - contract-first evaluator
   - node-centric evaluator

All artefacts are written under:
    logs/agentic_eval/YYYY-MM-DD/<timestamp>/
unless ``--reuse-run-dir`` is provided without ``--out-dir``, in which case
evaluation outputs are written back into the reusable run directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:  # type: ignore[no-redef]
        return None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from Scripts.evaluation.agentic_eval import DEFAULT_TRUTH_FILE, run_contract_eval  # noqa: E402
from Scripts.evaluation.agentic_node_eval import run_node_eval  # noqa: E402

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "agentic_eval"

logger = logging.getLogger("run_agentic_benchmark")


def _load_baseline_runner():
    from Scripts.evaluation.baseline_model import run_baseline_bundle

    return run_baseline_bundle


def _load_production_runner():
    from Scripts.evaluation.production_model import run_production_bundle

    return run_production_bundle


class OrchestratorAudit:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, step: str, **payload: Any) -> None:
        event = {"step": step, "ts": datetime.now().isoformat(), **payload}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _default_run_dir() -> Path:
    day = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_ROOT / day / ts


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _snapshot_queries(queries_file: Path, target_dir: Path) -> Path:
    payload = json.loads(queries_file.read_text(encoding="utf-8-sig"))
    snapshot_path = target_dir / "queries_snapshot.json"
    _write_json(snapshot_path, payload)
    return snapshot_path


def _require_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")


def run_benchmark(
    *,
    queries_file: Path = DEFAULT_TRUTH_FILE,
    query: Optional[str] = None,
    out_dir: Optional[Path] = None,
    reuse_run_dir: Optional[Path] = None,
    judge_model: str = "gpt-4o",
    skip_llm_judge: bool = False,
    skip_baseline: bool = False,
    skip_production: bool = False,
    skip_contract_eval: bool = False,
    skip_node_eval: bool = False,
) -> Dict[str, Any]:
    if reuse_run_dir:
        source_run_dir = reuse_run_dir
        target_dir = out_dir or _default_run_dir()
    else:
        source_run_dir = out_dir or _default_run_dir()
        target_dir = source_run_dir

    source_run_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    audit = OrchestratorAudit(target_dir / "benchmark_orchestrator_audit.jsonl")
    audit.log(
        "benchmark_start",
        queries_file=str(queries_file),
        query=query,
        source_run_dir=str(source_run_dir),
        target_dir=str(target_dir),
        reuse_run_dir=str(reuse_run_dir) if reuse_run_dir else None,
        judge_model=None if skip_llm_judge else judge_model,
    )

    outputs: Dict[str, Any] = {
        "source_run_dir": str(source_run_dir),
        "target_dir": str(target_dir),
        "queries_file": str(queries_file),
        "query": query,
    }

    if not reuse_run_dir:
        snapshot_path = _snapshot_queries(queries_file, target_dir)
        outputs["queries_snapshot"] = str(snapshot_path)

        if not skip_baseline:
            audit.log("baseline_start", run_dir=str(source_run_dir))
            baseline_outputs = _load_baseline_runner()(
                queries_file=queries_file,
                run_dir=source_run_dir,
                query=query,
            )
            outputs["baseline"] = {k: str(v) for k, v in baseline_outputs.items()}
            audit.log("baseline_complete", outputs=outputs["baseline"])

        if not skip_production:
            audit.log("production_start", run_dir=str(source_run_dir))
            production_outputs = _load_production_runner()(
                queries_file=queries_file,
                run_dir=source_run_dir,
                query=query,
            )
            outputs["production"] = {k: str(v) for k, v in production_outputs.items()}
            audit.log("production_complete", outputs=outputs["production"])
    else:
        audit.log("reuse_mode", run_dir=str(source_run_dir))

    baseline_results = source_run_dir / "baseline_results_full.jsonl"
    production_results = source_run_dir / "production_results_full.jsonl"
    _require_file(baseline_results)
    _require_file(production_results)
    outputs["baseline_results_full"] = str(baseline_results)
    outputs["production_results_full"] = str(production_results)

    if not skip_contract_eval:
        audit.log("contract_eval_start", run_dir=str(source_run_dir), out_dir=str(target_dir))
        contract_outputs = run_contract_eval(
            truth_file=queries_file,
            baseline_results=baseline_results,
            production_results=production_results,
            run_dir=source_run_dir,
            out_dir=target_dir,
            judge_model=judge_model,
            skip_llm_judge=skip_llm_judge,
        )
        outputs["contract_eval"] = {k: str(v) for k, v in contract_outputs.items()}
        audit.log("contract_eval_complete", outputs=outputs["contract_eval"])

    if not skip_node_eval:
        audit.log("node_eval_start", run_dir=str(source_run_dir), out_dir=str(target_dir))
        node_outputs = run_node_eval(
            run_dir=source_run_dir,
            truth_file=queries_file,
            out_dir=target_dir,
        )
        outputs["node_eval"] = {k: str(v) for k, v in node_outputs.items()}
        audit.log("node_eval_complete", outputs=outputs["node_eval"])

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "mode": "reuse" if reuse_run_dir else "fresh",
        "source_run_dir": str(source_run_dir),
        "target_dir": str(target_dir),
        "queries_file": str(queries_file),
        "query": query,
        "judge_model": None if skip_llm_judge else judge_model,
        "skip_llm_judge": skip_llm_judge,
        "outputs": outputs,
    }
    manifest_path = target_dir / "benchmark_manifest.json"
    _write_json(manifest_path, manifest)
    outputs["manifest"] = str(manifest_path)
    audit.log("benchmark_complete", manifest=str(manifest_path))
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or re-evaluate the contract-first financial benchmark.")
    parser.add_argument("--queries-file", type=str, default=str(DEFAULT_TRUTH_FILE), help="Truth/query catalogue JSON.")
    parser.add_argument("--query", type=str, default=None, help="Single query to run, exact query text match.")
    parser.add_argument("--out-dir", type=str, default=None, help="Output directory. Default: logs/agentic_eval/YYYY-MM-DD/<timestamp>.")
    parser.add_argument("--reuse-run-dir", type=str, default=None, help="Reuse an existing run dir containing baseline_results_full.jsonl and production_results_full.jsonl.")
    parser.add_argument("--judge-model", type=str, default="gpt-4o", help="Judge model used by agentic_eval.")
    parser.add_argument("--skip-llm-judge", action="store_true", help="Skip LLM financial rubric scoring.")
    parser.add_argument("--skip-baseline", action="store_true", help="Skip fresh baseline run. Fresh mode only.")
    parser.add_argument("--skip-production", action="store_true", help="Skip fresh production run. Fresh mode only.")
    parser.add_argument("--skip-contract-eval", action="store_true", help="Skip contract-first evaluation.")
    parser.add_argument("--skip-node-eval", action="store_true", help="Skip node-centric evaluation.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    outputs = run_benchmark(
        queries_file=Path(args.queries_file),
        query=args.query,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        reuse_run_dir=Path(args.reuse_run_dir) if args.reuse_run_dir else None,
        judge_model=args.judge_model,
        skip_llm_judge=args.skip_llm_judge,
        skip_baseline=args.skip_baseline,
        skip_production=args.skip_production,
        skip_contract_eval=args.skip_contract_eval,
        skip_node_eval=args.skip_node_eval,
    )

    print("Agentic benchmark complete")
    print(f"  source_run_dir: {outputs['source_run_dir']}")
    print(f"  target_dir:     {outputs['target_dir']}")
    if "contract_eval" in outputs:
        print(f"  contract_eval:  {outputs['contract_eval']}")
    if "node_eval" in outputs:
        print(f"  node_eval:      {outputs['node_eval']}")
    print(f"  manifest:       {outputs['manifest']}")


if __name__ == "__main__":
    main()
