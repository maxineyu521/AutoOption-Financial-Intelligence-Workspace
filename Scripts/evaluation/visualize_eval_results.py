from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Scripts.evaluation.agentic_eval import _dashboard_html as _eval_dashboard_html  # noqa: E402
from Scripts.evaluation.agentic_eval import _render_report as _eval_report_md  # noqa: E402
from Scripts.evaluation.agentic_node_eval import _dashboard_html as _node_dashboard_html  # noqa: E402
from Scripts.evaluation.agentic_node_eval import _markdown_report as _node_report_md  # noqa: E402

logger = logging.getLogger("visualize_eval_results")


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


def regenerate(run_dir: Path) -> Dict[str, Path]:
    outputs: Dict[str, Path] = {}

    summary_path = run_dir / "agentic_eval_summary.json"
    cases_path = run_dir / "agentic_eval_cases.jsonl"
    if summary_path.exists() and cases_path.exists():
        payload = _read_json(summary_path)
        paired = _read_jsonl(cases_path)
        report_path = run_dir / "agentic_eval_visual_report.md"
        dashboard_path = run_dir / "agentic_eval_dashboard.html"
        report_path.write_text(
            _eval_report_md(
                summary=payload.get("summary", {}),
                paired_results=paired,
                baseline_summary=payload.get("baseline_summary", {}),
                production_summary=payload.get("production_summary", {}),
            ),
            encoding="utf-8",
        )
        dashboard_path.write_text(
            _eval_dashboard_html(
                summary=payload.get("summary", {}),
                paired_results=paired,
                baseline_summary=payload.get("baseline_summary", {}),
                production_summary=payload.get("production_summary", {}),
            ),
            encoding="utf-8",
        )
        outputs["eval_report"] = report_path
        outputs["eval_dashboard"] = dashboard_path

    metrics_path = run_dir / "agentic_node_metrics.json"
    if metrics_path.exists():
        payload = _read_json(metrics_path)
        summary = payload.get("summary", {})
        cases = payload.get("per_case", [])
        report_path = run_dir / "agentic_node_visual_report.md"
        dashboard_path = run_dir / "agentic_node_dashboard.html"
        report_path.write_text(_node_report_md(summary, cases), encoding="utf-8")
        dashboard_path.write_text(_node_dashboard_html(summary, cases), encoding="utf-8")
        outputs["node_report"] = report_path
        outputs["node_dashboard"] = dashboard_path

    if not outputs:
        raise FileNotFoundError(f"No compatible evaluation artifacts found in {run_dir}")
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate evaluation dashboards and visual reports from saved logs.")
    parser.add_argument("--run-dir", required=True, help="logs/agentic_eval/<...> directory containing evaluation artifacts.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    outputs = regenerate(Path(args.run_dir))
    print("Evaluation visuals regenerated")
    for key, path in outputs.items():
        print(f"  {key}: {path}")


if __name__ == "__main__":
    main()
