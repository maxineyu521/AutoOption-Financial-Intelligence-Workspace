from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "agentic_eval" / "model_comparison"


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    scoring: str
    section: str
    description: str


COMPARISON_METRICS: List[MetricSpec] = [
    MetricSpec(
        "analyst_logic_integrity_rate",
        "Analyst Logic Integrity",
        "higher",
        "Analyst",
        "How often the Analyst draft avoided direction-changing financial logic mistakes.",
    ),
    MetricSpec(
        "analyst_truth_integrity_rate",
        "Analyst Truth Integrity",
        "higher",
        "Analyst",
        "How often the Analyst draft stayed faithful to supported facts and honest missing-data disclosure.",
    ),
    MetricSpec(
        "analyst_slot_answer_rate",
        "Analyst Slot Answer Rate",
        "higher",
        "Analyst",
        "How often the Analyst answered each requested slot or honestly disclosed that it could not.",
    ),
    MetricSpec(
        "analyst_semantic_evidence_carry_avg",
        "Analyst Semantic Evidence Carry",
        "higher",
        "Analyst",
        "How often the Analyst carried enough semantic evidence to support the answered slots.",
    ),
    MetricSpec(
        "checker_fix_adoption_avg",
        "Checker Fix Adoption",
        "higher",
        "Checker",
        "How often Checker findings translated into cleaner downstream behavior.",
    ),
    MetricSpec(
        "critic_feedback_adoption_avg",
        "Critic Feedback Adoption",
        "higher",
        "Critic",
        "How often Critic guidance was reflected in later node behavior.",
    ),
    MetricSpec(
        "critic_non_actionable_information_retention_delta_avg",
        "Critic Non-actionable Info Delta",
        "center_zero",
        "Critic",
        "Centered delta for non-actionable cases. Zero is best; more negative values mean stronger downstream information compression.",
    ),
    MetricSpec(
        "critic_actionability_downgrade_success_rate",
        "Critic Downgrade Success",
        "higher",
        "Critic",
        "How often Critic successfully pulled risky outputs back from live recommendation mode.",
    ),
    MetricSpec(
        "critic_risk_disclosure_success_rate",
        "Critic Risk Disclosure",
        "higher",
        "Critic",
        "How often high-risk signals identified by Critic made it into the final answer.",
    ),
    MetricSpec(
        "finalizer_truthfulness_pass_rate",
        "Final Truth Pass Rate",
        "higher",
        "Finalizer",
        "How often the final answer passed the hard truthfulness gate.",
    ),
    MetricSpec(
        "finalizer_truth_integrity_rate",
        "Final Truth Integrity",
        "higher",
        "Finalizer",
        "How often the final answer stayed grounded in supported facts and honest missing-data handling.",
    ),
    MetricSpec(
        "finalizer_slot_answer_rate",
        "Final Slot Answer Rate",
        "higher",
        "Finalizer",
        "How often the final answer still answered each requested slot or honestly disclosed that it could not.",
    ),
    MetricSpec(
        "finalizer_semantic_evidence_retention_avg",
        "Final Semantic Evidence Retention",
        "higher",
        "Finalizer",
        "How much semantic evidence survived into the final answer after downstream safety and rendering.",
    ),
    MetricSpec(
        "finalizer_disclosure_honesty_rate",
        "Final Disclosure Honesty",
        "higher",
        "Finalizer",
        "How consistently the final answer preserved honest disclosure for slots that could not be answered from retrieval.",
    ),
    MetricSpec(
        "finalizer_information_value_avg",
        "Final Informational Completeness",
        "higher",
        "Finalizer",
        "How complete and user-useful the final answer remains after all safety and rendering constraints.",
    ),
    MetricSpec(
        "finalizer_risk_disclosure_pass_rate",
        "Final Risk Disclosure",
        "higher",
        "Finalizer",
        "How often the final answer retained required risk language.",
    ),
    MetricSpec(
        "finalizer_illustrative_structure_rate",
        "Final Illustrative Structure Rate",
        "higher",
        "Finalizer",
        "How often a legal illustrative structure survived when the mode allowed it.",
    ),
    MetricSpec(
        "downgraded_answer_usefulness_floor_avg",
        "Downgraded Answer Usefulness Floor",
        "higher",
        "Finalizer",
        "Minimum usefulness floor for downgraded answers: 0.0 means required downgrade disclosure failed or no answer survived, 0.5 means information survived, 1.0 means information survived and stayed query-first.",
    ),
    MetricSpec(
        "critical_failure_case_rate",
        "Critical Failure Rate",
        "lower",
        "Risk",
        "Share of cases with direction-changing financial logic failures.",
    ),
    MetricSpec(
        "material_failure_case_rate",
        "Material Failure Rate",
        "lower",
        "Risk",
        "Share of cases with fact-support or honest-disclosure failures.",
    ),
    MetricSpec(
        "compliance_failure_case_rate",
        "Compliance Failure Rate",
        "lower",
        "Risk",
        "Share of cases with non-actionable boundary or illustrative-contract failures.",
    ),
]

TREND_KEYS = [
    "finalizer_truthfulness_pass_rate",
    "analyst_semantic_evidence_carry_avg",
    "finalizer_semantic_evidence_retention_avg",
    "downgraded_answer_usefulness_floor_avg",
    "compliance_failure_case_rate",
]

HTML_CSS = """
body { font-family: Inter, Segoe UI, Arial, sans-serif; background:#f8fafc; color:#0f172a; margin:0; }
.page { max-width: 1560px; margin: 0 auto; padding: 28px; }
.hero, .panel { background:#ffffff; border:1px solid #e2e8f0; border-radius:16px; padding:20px 22px; margin-bottom:18px; box-shadow:0 8px 24px rgba(15,23,42,0.04); }
h1 { margin:0 0 6px 0; font-size:30px; }
h2 { margin:0 0 12px 0; font-size:20px; }
h3 { margin:0 0 10px 0; font-size:16px; }
.muted { color:#475569; line-height:1.5; }
.grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin-top:14px; }
.metric-card { border:1px solid #e2e8f0; border-radius:12px; padding:14px; background:#fcfcfd; }
.metric-card .label { color:#475569; font-size:12px; text-transform:uppercase; letter-spacing:0.04em; }
.metric-card .value { font-size:28px; font-weight:700; margin:6px 0; }
.metric-card .desc { color:#475569; font-size:13px; line-height:1.45; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { border-bottom:1px solid #e2e8f0; padding:10px 8px; text-align:left; vertical-align:top; }
th { color:#334155; background:#f8fafc; position:sticky; top:0; }
.good { color:#15803d; font-weight:600; }
.bad { color:#b91c1c; font-weight:600; }
.warn { color:#b45309; font-weight:600; }
.pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:12px; margin:4px 8px 0 0; }
.small { font-size:12px; color:#64748b; }
.section-grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(420px,1fr)); gap:16px; }
.note { background:#f8fafc; border-left:4px solid #2563eb; padding:12px 14px; border-radius:8px; color:#334155; }
ul.tight { margin:8px 0 0 18px; }
"""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "None", "nan"):
            return None
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def _fmt_num(value: Any, digits: int = 3) -> str:
    x = _safe_float(value)
    if x is None:
        return "N/A"
    return f"{x:.{digits}f}"


def _is_improvement(before: float, after: float, scoring: str) -> bool:
    if scoring == "higher":
        return after > before
    if scoring == "lower":
        return after < before
    if scoring == "center_zero":
        return abs(after) < abs(before)
    raise ValueError(f"Unknown scoring mode: {scoring}")


def _best_value_sort_key(value: float, scoring: str) -> float:
    if scoring == "higher":
        return value
    if scoring == "lower":
        return -value
    if scoring == "center_zero":
        return -abs(value)
    raise ValueError(f"Unknown scoring mode: {scoring}")


def _fmt_delta(value: Optional[float], scoring: str, *, before: Optional[float] = None, after: Optional[float] = None) -> Tuple[str, str]:
    if value is None:
        return "N/A", "warn"
    sign = "+" if value > 0 else ""
    label = f"{sign}{value:.3f}"
    if value == 0:
        return label, "warn"
    if before is not None and after is not None:
        improved = _is_improvement(before, after, scoring)
    else:
        improved = value > 0 if scoring == "higher" else value < 0
    return label, "good" if improved else "bad"


def _best_version(metric: MetricSpec, runs: Sequence[Dict[str, Any]]) -> str:
    scored: List[Tuple[float, str]] = []
    for run in runs:
        value = _safe_float(run["metrics"].get(metric.key))
        if value is not None:
            scored.append((value, run["label"]))
    if not scored:
        return "N/A"
    return max(scored, key=lambda item: _best_value_sort_key(item[0], metric.scoring))[1]


def _severity_rank(severity: Optional[str]) -> int:
    order = {None: 0, "": 0, "compliance": 1, "material": 2, "critical": 3}
    return order.get(severity, 0)


def _line_svg(labels: Sequence[str], values: Sequence[Optional[float]], *, title: str, color: str = "#2563eb") -> str:
    width = 560
    height = 220
    left = 42
    top = 20
    chart_w = width - left - 20
    chart_h = height - top - 42
    clean_vals = [float(v) for v in values if v is not None]
    if not clean_vals:
        return f"<div class='panel'><h3>{html.escape(title)}</h3><div class='small'>No data</div></div>"
    min_v = min(0.0, min(clean_vals))
    max_v = max(1.0, max(clean_vals))
    span = max(max_v - min_v, 1e-6)
    points = []
    labels_svg = []
    for idx, label in enumerate(labels):
        x = left + (chart_w * idx / max(1, len(labels) - 1))
        value = values[idx]
        if value is not None:
            y = top + chart_h - ((float(value) - min_v) / span) * chart_h
            points.append((x, y, value))
        labels_svg.append(
            f"<text x='{x:.1f}' y='{height - 14}' font-size='11' text-anchor='middle' fill='#64748b'>{html.escape(label)}</text>"
        )
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
    dots = "".join(
        f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{color}' />"
        f"<text x='{x:.1f}' y='{y - 10:.1f}' font-size='10' text-anchor='middle' fill='#0f172a'>{value:.2f}</text>"
        for x, y, value in points
    )
    grid = "".join(
        f"<line x1='{left}' y1='{top + chart_h * frac:.1f}' x2='{left + chart_w}' y2='{top + chart_h * frac:.1f}' stroke='#e2e8f0' stroke-width='1' />"
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    return (
        f"<div class='panel'><h3>{html.escape(title)}</h3>"
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' role='img' aria-label='{html.escape(title)}'>"
        f"{grid}<polyline fill='none' stroke='{color}' stroke-width='3' points='{polyline}' />{dots}{''.join(labels_svg)}</svg></div>"
    )


def _delta_summaries(runs: Sequence[Dict[str, Any]], metrics: Sequence[MetricSpec]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for idx in range(1, len(runs)):
        prev = runs[idx - 1]
        curr = runs[idx]
        improvements: List[str] = []
        regressions: List[str] = []
        for metric in metrics:
            before = _safe_float(prev["metrics"].get(metric.key))
            after = _safe_float(curr["metrics"].get(metric.key))
            if before is None or after is None:
                continue
            delta = after - before
            if delta == 0:
                continue
            improved = _is_improvement(before, after, metric.scoring)
            text = f"{metric.label} ({delta:+.3f})"
            if improved:
                improvements.append(text)
            else:
                regressions.append(text)
        rows.append(
            {
                "from": prev["label"],
                "to": curr["label"],
                "improvements": improvements[:5],
                "regressions": regressions[:5],
            }
        )
    return rows


def _coalesce_slot_status(case_payload: Dict[str, Any]) -> Dict[str, Any]:
    finalizer = case_payload.get("finalizer") or {}
    analyst = case_payload.get("analyst") or {}
    return (
        finalizer.get("slot_status")
        or analyst.get("slot_status")
        or {}
    )


def _load_run(label: str, run_dir: Path) -> Dict[str, Any]:
    node_payload = _read_json(run_dir / "agentic_node_metrics.json")
    metrics = dict(node_payload.get("summary") or {})
    case_map: Dict[str, Dict[str, Any]] = {}
    for case_payload in node_payload.get("per_case") or []:
        case_name = str(case_payload.get("case_name") or case_payload.get("query") or "")
        finalizer = case_payload.get("finalizer") or {}
        analyst = case_payload.get("analyst") or {}
        critic = case_payload.get("critic") or {}
        checker = case_payload.get("checker") or {}
        case_map[case_name] = {
            "financial_truthfulness_pass": finalizer.get("final_truthfulness_pass"),
            "highest_failure_severity": finalizer.get("highest_failure_severity"),
            "critical_failures": finalizer.get("critical_failures", []),
            "material_failures": finalizer.get("material_failures", []),
            "compliance_failures": finalizer.get("compliance_failures", []),
            "mode": finalizer.get("mode"),
            "analyst_semantic_evidence": analyst.get("semantic_evidence_carry_score"),
            "final_semantic_evidence": finalizer.get("semantic_evidence_retention_score"),
            "analyst_slot_answer_rate": analyst.get("slot_answer_rate"),
            "finalizer_slot_answer_rate": finalizer.get("slot_answer_rate"),
            "critic_adoption": critic.get("feedback_adoption_score"),
            "checker_adoption": checker.get("fix_adoption_score"),
            "risk_disclosure_pass": finalizer.get("risk_disclosure_pass"),
            "slot_status": _coalesce_slot_status(case_payload),
            "informational_value_under_downgrade": finalizer.get("informational_value_under_downgrade"),
        }
    return {
        "label": label,
        "run_dir": run_dir,
        "metrics": metrics,
        "cases": case_map,
    }


def _metric_table(metrics: Sequence[MetricSpec], runs: Sequence[Dict[str, Any]]) -> str:
    headers = ["Metric", "Best"] + [run["label"] for run in runs]
    rows = []
    for metric in metrics:
        cells = [metric.label, _best_version(metric, runs)]
        prev_val: Optional[float] = None
        for run in runs:
            val = _safe_float(run["metrics"].get(metric.key))
            if val is None:
                cells.append("N/A")
                continue
            text = _fmt_num(val)
            if prev_val is not None:
                delta, css = _fmt_delta(val - prev_val, metric.scoring, before=prev_val, after=val)
                text = f"{text}<br><span class='{css} small'>{delta} vs prev</span>"
            prev_val = val
            cells.append(text)
        rows.append(cells)
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _markdown_metric_table(metrics: Sequence[MetricSpec], runs: Sequence[Dict[str, Any]]) -> str:
    headers = ["Metric", "Best"] + [run["label"] for run in runs]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for metric in metrics:
        row = [metric.label, _best_version(metric, runs)]
        prev_val: Optional[float] = None
        for run in runs:
            val = _safe_float(run["metrics"].get(metric.key))
            if val is None:
                row.append("N/A")
                continue
            text = _fmt_num(val)
            if prev_val is not None:
                delta, _ = _fmt_delta(val - prev_val, metric.scoring, before=prev_val, after=val)
                text = f"{text} ({delta})"
            prev_val = val
            row.append(text)
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _case_matrix_markdown(runs: Sequence[Dict[str, Any]]) -> str:
    case_names = sorted({name for run in runs for name in run["cases"].keys()})
    headers = ["Case"] + [run["label"] for run in runs]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for case_name in case_names:
        row = [case_name]
        for run in runs:
            case = run["cases"].get(case_name, {})
            passed = case.get("financial_truthfulness_pass")
            sev = case.get("highest_failure_severity") or "-"
            analyst_sem = _fmt_num(case.get("analyst_semantic_evidence"))
            final_sem = _fmt_num(case.get("final_semantic_evidence"))
            row.append(
                f"{'Pass' if passed else 'Fail'} / {sev} / A {analyst_sem} / F {final_sem}"
            )
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _case_matrix_html(runs: Sequence[Dict[str, Any]]) -> str:
    case_names = sorted({name for run in runs for name in run["cases"].keys()})
    headers = ["Case"] + [run["label"] for run in runs]
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body_rows = []
    for case_name in case_names:
        cells = [f"<td>{html.escape(case_name)}</td>"]
        for run in runs:
            case = run["cases"].get(case_name, {})
            passed = bool(case.get("financial_truthfulness_pass"))
            severity = case.get("highest_failure_severity") or "-"
            css = "good" if passed else ("bad" if _severity_rank(severity) >= 2 else "warn")
            flags = []
            if case.get("critical_failures"):
                flags.append("critical")
            if case.get("material_failures"):
                flags.append("material")
            if case.get("compliance_failures"):
                flags.append("compliance")
            slot_status = case.get("slot_status") or {}
            slot_short = ", ".join(f"{k}:{v}" for k, v in slot_status.items()) if slot_status else "n/a"
            cells.append(
                f"<td><div class='{css}'>{'Pass' if passed else 'Fail'}</div>"
                f"<div class='small'>Severity: {html.escape(str(severity))}</div>"
                f"<div class='small'>A sem: {_fmt_num(case.get('analyst_semantic_evidence'))} | F sem: {_fmt_num(case.get('final_semantic_evidence'))}</div>"
                f"<div class='small'>Flags: {html.escape(', '.join(flags) if flags else 'none')}</div>"
                f"<div class='small'>Slots: {html.escape(slot_short)}</div></td>"
            )
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _current_version_assessment(current: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    m = current["metrics"]
    strengths: List[str] = []
    limits: List[str] = []
    truth_pass = _safe_float(m.get("finalizer_truthfulness_pass_rate")) or 0.0
    if truth_pass >= 0.6:
        strengths.append(f"Final-answer truth pass is now {truth_pass:.2f}, which is in a strong range for this benchmark.")
    if (_safe_float(m.get("critical_failure_case_rate")) or 1.0) == 0.0:
        strengths.append("No critical financial logic failures remain in the final answers.")
    if (_safe_float(m.get("analyst_slot_answer_rate")) or 0.0) >= 0.9:
        strengths.append("The Analyst now reliably answers the requested slots or explicitly discloses when a slot is not answerable.")
    if (_safe_float(m.get("finalizer_disclosure_honesty_rate")) or 0.0) >= 1.0:
        strengths.append("Final disclosure honesty is perfect in this sample: missing-data constraints are preserved all the way to the answer.")

    if (_safe_float(m.get("analyst_semantic_evidence_carry_avg")) or 1.0) < 0.7:
        limits.append("Analyst semantic evidence carry is still not strong enough; the first draft remains too light on supporting information.")
    if (_safe_float(m.get("compliance_failure_case_rate")) or 0.0) >= 0.4:
        limits.append("Compliance remains the most frequent residual failure mode, especially around illustrative structure boundaries.")
    critic_delta = _safe_float(m.get("critic_non_actionable_information_retention_delta_avg"))
    if critic_delta is not None and critic_delta <= -0.05:
        limits.append("Critic and Finalizer still compress non-actionable answers meaningfully; the safety layer is working, but it still trims too much information.")
    if (_safe_float(m.get("downgraded_answer_usefulness_floor_avg")) or 0.0) <= 0.6:
        limits.append("Downgraded answers still sit near the minimum usefulness floor rather than preserving a stronger user-facing read.")
    return strengths[:4], limits[:4]


def _executive_readout(runs: Sequence[Dict[str, Any]]) -> List[str]:
    latest = runs[-1]["metrics"]
    first = runs[0]["metrics"]
    best_truth_run = _best_version(next(metric for metric in COMPARISON_METRICS if metric.key == "finalizer_truthfulness_pass_rate"), runs)
    best_info_run = _best_version(next(metric for metric in COMPARISON_METRICS if metric.key == "finalizer_information_value_avg"), runs)
    best_compliance_run = _best_version(next(metric for metric in COMPARISON_METRICS if metric.key == "compliance_failure_case_rate"), runs)
    first_truth = _safe_float(first.get("finalizer_truthfulness_pass_rate")) or 0.0
    latest_truth = _safe_float(latest.get("finalizer_truthfulness_pass_rate")) or 0.0
    first_comp = _safe_float(first.get("compliance_failure_case_rate")) or 0.0
    latest_comp = _safe_float(latest.get("compliance_failure_case_rate")) or 0.0
    critic_delta = _safe_float(latest.get("critic_non_actionable_information_retention_delta_avg"))
    return [
        f"Across these {len(runs)} iterations, final truth pass improved from {first_truth:.2f} in the first run to {latest_truth:.2f} in the latest run.",
        f"The strongest truth-pass run is `{best_truth_run}`, while the strongest final informational completeness run is `{best_info_run}`.",
        f"Compliance failure rate improved from {first_comp:.2f} to {latest_comp:.2f}; the cleanest compliance profile is `{best_compliance_run}`.",
        f"The latest run still shows non-actionable information compression through Critic's centered delta (`{_fmt_num(critic_delta)}`), so safety is strong but not free.",
        f"The current best read is that the workflow is now strong at evidence grounding and hard safety, but still only average on downgraded-answer usefulness floor (`{_fmt_num(latest.get('downgraded_answer_usefulness_floor_avg'))}`).",
    ]


def _render_markdown(runs: Sequence[Dict[str, Any]]) -> str:
    latest = runs[-1]
    deltas = _delta_summaries(runs, COMPARISON_METRICS)
    strengths, limits = _current_version_assessment(latest)
    lines: List[str] = []
    lines.append("# External Model Comparison Dashboard")
    lines.append("")
    lines.append("## Summary")
    lines.append(
        f"This comparison reviews {len(runs)} node-evaluation runs of the same financial RAG workflow. The goal is to show which changes improved slot coverage, semantic evidence carry, safety boundaries, and final-answer reliability."
    )
    lines.append("")
    lines.append("## Run Lineup")
    for run in runs:
        lines.append(f"- **{run['label']}**: `{run['run_dir']}`")
    lines.append("")
    lines.append("## Iteration Deltas")
    for row in deltas:
        lines.append(f"### {row['from']} -> {row['to']}")
        if row["improvements"]:
            lines.append(f"- Improved: {', '.join(row['improvements'])}")
        if row["regressions"]:
            lines.append(f"- Regressed: {', '.join(row['regressions'])}")
        if not row["improvements"] and not row["regressions"]:
            lines.append("- No measurable change in the tracked summary metrics.")
        lines.append("")
    lines.append("## Node Metrics")
    lines.append(_markdown_metric_table(COMPARISON_METRICS, runs))
    lines.append("")
    lines.append("## Per-case Comparison")
    lines.append(_case_matrix_markdown(runs))
    lines.append("")
    lines.append(f"## Current Version Assessment ({latest['label']})")
    lines.append("")
    lines.append("### Strengths")
    for item in strengths:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### Limitations")
    for item in limits:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Executive Readout")
    for item in _executive_readout(runs):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _render_html(runs: Sequence[Dict[str, Any]]) -> str:
    latest = runs[-1]
    strengths, limits = _current_version_assessment(latest)
    deltas = _delta_summaries(runs, COMPARISON_METRICS)
    labels = [run["label"] for run in runs]
    trend_specs = {metric.key: metric for metric in COMPARISON_METRICS}
    charts = "".join(
        _line_svg(
            labels,
            [_safe_float(run["metrics"].get(key)) for run in runs],
            title=trend_specs[key].label,
            color="#dc2626" if trend_specs[key].scoring == "lower" else "#2563eb",
        )
        for key in TREND_KEYS
    )

    cards = [
        (
            "Latest Final Truth Pass",
            _fmt_num(latest["metrics"].get("finalizer_truthfulness_pass_rate")),
            "Final-answer hard truth pass rate for the current version.",
        ),
        (
            "Latest Analyst Semantic Evidence",
            _fmt_num(latest["metrics"].get("analyst_semantic_evidence_carry_avg")),
            "Primary Analyst evidence-carry score under the new slot-based evaluator.",
        ),
        (
            "Latest Final Semantic Evidence",
            _fmt_num(latest["metrics"].get("finalizer_semantic_evidence_retention_avg")),
            "How much semantic evidence survived into the final answer.",
        ),
        (
            "Latest Compliance Failure",
            _fmt_num(latest["metrics"].get("compliance_failure_case_rate")),
            "Residual non-actionable / illustrative contract failure rate.",
        ),
    ]
    card_html = "".join(
        f"<div class='metric-card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='desc'>{html.escape(desc)}</div></div>"
        for label, value, desc in cards
    )

    delta_blocks = []
    for row in deltas:
        items = []
        if row["improvements"]:
            items.append(
                "<div class='good'>Improved</div><ul class='tight'>"
                + "".join(f"<li>{html.escape(x)}</li>" for x in row["improvements"])
                + "</ul>"
            )
        if row["regressions"]:
            items.append(
                "<div class='bad'>Regressed</div><ul class='tight'>"
                + "".join(f"<li>{html.escape(x)}</li>" for x in row["regressions"])
                + "</ul>"
            )
        if not items:
            items.append("<div class='small'>No measurable change in tracked summary metrics.</div>")
        delta_blocks.append(f"<div class='panel'><h3>{html.escape(row['from'])} → {html.escape(row['to'])}</h3>{''.join(items)}</div>")

    strengths_html = "".join(f"<li>{html.escape(item)}</li>" for item in strengths)
    limits_html = "".join(f"<li>{html.escape(item)}</li>" for item in limits)
    readout_html = "".join(f"<li>{html.escape(item)}</li>" for item in _executive_readout(runs))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>External Model Comparison Dashboard</title>
  <style>{HTML_CSS}</style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <h1>External Model Comparison Dashboard</h1>
      <div class="muted">This dashboard compares {len(runs)} node-evaluation runs of the financial RAG workflow using the latest slot-based semantic evidence metrics. It is designed for engineering review and iteration tracking, with special emphasis on slot coverage, safety, and post-downgrade information retention.</div>
      <div class="grid">{card_html}</div>
    </section>
    <section class="panel">
      <h2>Run Lineup</h2>
      <div>{''.join(f"<span class='pill'>{html.escape(run['label'])}</span>" for run in runs)}</div>
      <div class="small" style="margin-top:10px">{'<br>'.join(html.escape(str(run['run_dir'])) for run in runs)}</div>
    </section>
    <section>
      <h2>Iteration Delta Readout</h2>
      <div class="section-grid">{''.join(delta_blocks)}</div>
    </section>
    <section>
      <h2>Trend Visuals</h2>
      <div class="section-grid">{charts}</div>
    </section>
    <section class="panel">
      <h2>Node Comparison</h2>
      {_metric_table(COMPARISON_METRICS, runs)}
    </section>
    <section class="panel">
      <h2>Per-case Comparison</h2>
      {_case_matrix_html(runs)}
    </section>
    <section class="section-grid">
      <div class="panel">
        <h2>Current Version Strengths</h2>
        <ul class="tight">{strengths_html}</ul>
      </div>
      <div class="panel">
        <h2>Current Version Limitations</h2>
        <ul class="tight">{limits_html}</ul>
      </div>
    </section>
    <section class="panel">
      <h2>Audience-ready Summary</h2>
      <ul class="tight">{readout_html}</ul>
      <div class="note" style="margin-top:14px">
        Read the Critic headline metric as a centered delta, not a success rate: values near 0 mean little compression, more negative values mean stronger flattening, and positive values mean downstream clarification.
      </div>
    </section>
  </main>
</body>
</html>"""


def build_comparison(runs: Sequence[Tuple[str, Path]], out_dir: Path) -> Dict[str, Path]:
    loaded = [_load_run(label, path) for label, path in runs]
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "model_comparison_report.md"
    html_path = out_dir / "model_comparison_dashboard.html"
    payload_path = out_dir / "model_comparison_payload.json"

    md_path.write_text(_render_markdown(loaded), encoding="utf-8")
    html_path.write_text(_render_html(loaded), encoding="utf-8")
    payload_path.write_text(
        json.dumps(
            [
                {
                    "label": run["label"],
                    "run_dir": str(run["run_dir"]),
                    "metrics": run["metrics"],
                    "cases": run["cases"],
                }
                for run in loaded
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {"report": md_path, "dashboard": html_path, "payload": payload_path}


def _parse_run_arg(raw: str) -> Tuple[str, Path]:
    if "::" not in raw:
        raise ValueError(f"Invalid --run value: {raw}. Expected LABEL::PATH")
    label, path = raw.split("::", 1)
    return label.strip(), Path(path.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a cross-run model comparison dashboard from saved node-evaluation logs."
    )
    parser.add_argument("--run", action="append", required=True, help="Run in the form LABEL::logs/agentic_eval/.../reeval_node")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory. Defaults to logs/agentic_eval/model_comparison/<timestamp>.",
    )
    args = parser.parse_args()

    runs = [_parse_run_arg(item) for item in args.run]
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs = build_comparison(runs, out_dir)
    print("Model comparison generated")
    print(f"  report:    {outputs['report']}")
    print(f"  dashboard: {outputs['dashboard']}")
    print(f"  payload:   {outputs['payload']}")


if __name__ == "__main__":
    main()
