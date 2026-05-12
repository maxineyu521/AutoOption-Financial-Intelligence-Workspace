"""
Node-centric workflow evaluation for the financial RAG stack.

This complements ``agentic_eval.py``:

- ``agentic_eval.py`` scores final system outputs against contract-first rules.
- ``agentic_node_eval.py`` scores the internal workflow itself:
  revision burden, degraded paths, analyst-to-final evidence retention,
  checker / critic feedback adoption, and per-node contribution analytics.

Outputs are written under:
    logs/agentic_eval/YYYY-MM-DD/<timestamp>/

Artefacts:
    - agentic_node_audit.jsonl
    - agentic_node_events.jsonl
    - agentic_node_eval_detailed.csv
    - agentic_node_events.csv
    - agentic_node_metrics.json
    - agentic_node_report.md
    - agentic_node_dashboard.html
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Scripts.evaluation.agentic_eval import (  # noqa: E402
    CONCRETE_STRUCTURE_PATTERNS,
    DEFAULT_TRUTH_FILE,
    RAGAS_LOG_ROOT,
    _answer_body,
    _contains_any,
    _coverage_disclosure_pass,
    _evidence_retention,
    _extract_metric_map_from_contexts,
    _extract_numbers,
    _financial_truthfulness,
    _intent_coverage,
    _latest_ragas_run,
    _missing_query_slots,
    _missing_sources,
    _near_match,
    _read_json,
    _read_jsonl,
    _resolved_mode,
    _structured_truth,
    _truth_map,
)

logger = logging.getLogger("agentic_node_eval")

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "logs" / "agentic_eval"

CSS = """
:root {
  --bg: #f7f8fb;
  --panel: #ffffff;
  --ink: #1f2937;
  --muted: #667085;
  --line: #d9e0ea;
  --blue: #2563eb;
  --green: #16a34a;
  --amber: #d97706;
  --red: #dc2626;
  --slate: #334155;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: Inter, Segoe UI, Arial, sans-serif;
  line-height: 1.5;
}
.page {
  width: min(1240px, calc(100vw - 48px));
  margin: 32px auto 56px;
}
.hero, .panel, .metric-card, table {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 10px 24px rgba(15, 23, 42, 0.06);
}
.hero { padding: 28px; }
h1 { margin: 0 0 8px; font-size: 30px; letter-spacing: 0; }
h2 { margin: 30px 0 12px; font-size: 20px; letter-spacing: 0; }
h3 { margin: 18px 0 8px; font-size: 16px; letter-spacing: 0; }
.muted { color: var(--muted); }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
  margin-top: 18px;
}
.metric-card { padding: 16px; }
.metric-card .label {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}
.metric-card .value {
  margin-top: 6px;
  font-size: 26px;
  font-weight: 700;
}
.panel {
  padding: 18px;
  margin-top: 14px;
}
table {
  width: 100%;
  border-collapse: collapse;
  overflow: hidden;
}
th, td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th {
  background: #eef3fb;
  color: var(--slate);
  font-size: 12px;
  text-transform: uppercase;
}
tr:last-child td { border-bottom: none; }
.good { color: var(--green); font-weight: 600; }
.warn { color: var(--amber); font-weight: 600; }
.bad { color: var(--red); font-weight: 600; }
.pill {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 3px 9px;
  font-size: 12px;
  border: 1px solid var(--line);
  background: #f8fafc;
  gap: 6px;
}
.bar-track {
  width: 100%;
  height: 10px;
  background: #e8eef8;
  border-radius: 999px;
  overflow: hidden;
}
.bar-fill {
  height: 10px;
  background: var(--blue);
  border-radius: 999px;
}
pre {
  white-space: pre-wrap;
  background: #0f172a;
  color: #e2e8f0;
  padding: 14px;
  border-radius: 8px;
  overflow: auto;
}
.chart-wrap {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
}
.chart-card {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
}
.legend { font-size: 12px; color: var(--muted); margin-top: 8px; }
.note {
  margin-top: 12px;
  color: var(--ink);
  background: #f8fafc;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px 16px;
}
.note p, .note ul { margin: 0 0 8px; }
.note p:last-child, .note ul:last-child { margin-bottom: 0; }
.tight { margin: 8px 0 0 18px; padding: 0; }
.metric-section { margin-bottom: 16px; }
.metric-section:last-child { margin-bottom: 0; }
"""

NODE_METRIC_DESCRIPTIONS: Dict[str, str] = {
    "n_cases": "Number of paired production cases included in this node-evaluation run.",
    "mean_revision_count": "Average number of workflow passes needed before the answer reached its final state.",
    "degraded_case_rate": "Share of cases that exited through a degraded fallback path instead of a normal complete render.",
    "checker_circuit_break_count": "Count of cases where Checker findings effectively stopped the normal path and forced a fallback-style outcome.",
    "completed_after_critic_count": "Count of cases that completed normally after passing through Critic review.",
    "analyst_logic_integrity_rate": "How often the first Analyst draft avoided direction-changing financial logic errors.",
    "analyst_truth_integrity_rate": "How often the first Analyst draft stayed faithful to supported facts and honest missing-data disclosure.",
    "analyst_intent_coverage_avg": "How well the Analyst draft answered the user’s requested slots before later safety correction.",
    "analyst_slot_answer_rate": "How often the Analyst either answered each requested slot or honestly disclosed that it could not.",
    "analyst_semantic_evidence_carry_avg": "How often the Analyst carried enough semantic evidence to support each answered slot.",
    "analyst_raw_metric_retention_avg": "Legacy raw retention diagnostic: how much raw retrieval wording or numeric detail was repeated in the draft.",
    "analyst_structure_hint_presence_rate": "How often the Analyst still preserved one usable structure idea without turning it into final compliance prose.",
    "checker_fix_adoption_avg": "Whether Checker findings actually translated into cleaner final behavior, not just whether Checker ran.",
    "critic_feedback_adoption_avg": "Whether Critic constraints were reflected in downstream mode, disclosure, and structure behavior.",
    "critic_actionability_downgrade_success_rate": "How often Critic successfully pulled risky outputs back from live recommendation mode.",
    "critic_risk_disclosure_success_rate": "How often high-risk signals identified by Critic made it into the final answer.",
    "critic_non_actionable_information_retention_delta_avg": "Average semantic-information change from Analyst to Finalizer in non-actionable cases after Critic downgrade constraints. Near 0 means little compression; more negative values mean the answer was flattened.",
    "finalizer_logic_integrity_rate": "How often the final answer avoided direction-changing financial logic mistakes after all node intervention.",
    "finalizer_truth_integrity_rate": "How often the final answer stayed grounded in supported facts and honest missing-data handling.",
    "finalizer_slot_answer_rate": "How often the final answer still answered each requested slot or honestly disclosed that it could not.",
    "finalizer_semantic_evidence_retention_avg": "How much semantic evidence survived into the final answer after downstream safety and rendering.",
    "finalizer_disclosure_honesty_rate": "How consistently the final answer preserved honest disclosure for slots that could not be answered from retrieval.",
    "finalizer_information_value_avg": "How complete and user-useful the final answer remains after safety and rendering constraints.",
    "finalizer_risk_disclosure_pass_rate": "How often the Finalizer explicitly preserved required risk language.",
    "finalizer_illustrative_structure_rate": "How often the Finalizer preserved a legal illustrative structure when the mode allowed it.",
    "downgraded_answer_usefulness_floor_avg": "Minimum usefulness floor for downgraded answers: 0.0 means required downgrade disclosure failed or no answer survived, 0.5 means information survived, 1.0 means information survived and the answer stayed query-first.",
    "critical_failure_case_rate": "Share of cases containing direction-changing financial logic failures.",
    "material_failure_case_rate": "Share of cases containing fact-support or honest-disclosure failures.",
    "compliance_failure_case_rate": "Share of cases containing non-actionable boundary or illustrative-contract failures.",
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "None", "nan"):
            return None
        x = float(value)
        if x != x:
            return None
        return x
    except (TypeError, ValueError):
        return None


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    return round(mean(clean), 4) if clean else None


def _fmt(value: Any, digits: int = 3) -> str:
    x = _safe_float(value)
    if x is None:
        return "N/A"
    return f"{x:.{digits}f}"


def _score_class(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return "muted"
    if x >= 0.75:
        return "good"
    if x >= 0.45:
        return "warn"
    return "bad"


def _critic_delta_label(delta: Optional[float]) -> str:
    x = _safe_float(delta)
    if x is None:
        return "Compression: unknown"
    if abs(x) <= 0.01:
        return "Compression: flat preservation"
    if x > 0:
        return "Compression: clarification gain"
    if x >= -0.10:
        return "Compression: mild"
    if x >= -0.25:
        return "Compression: moderate"
    return "Compression: severe"


def _html_table(headers: List[str], rows: List[List[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body: List[str] = []
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _md_table(headers: List[str], rows: List[List[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(lines)


def _bar(value: Any) -> str:
    x = _safe_float(value)
    width = max(0, min(100, int(round((x or 0.0) * 100))))
    return (
        f"<div class='bar-track'><div class='bar-fill' style='width:{width}%'></div></div>"
        f"<div class='{_score_class(x)}'>{_fmt(x)}</div>"
    )


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _flatten(prefix: str, value: Any, out: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _flatten(key, v, out)
    elif isinstance(value, list):
        out[prefix] = json.dumps(value, ensure_ascii=False)
    else:
        out[prefix] = value


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write("")
        return
    flat_rows: List[Dict[str, Any]] = []
    keys: set[str] = set()
    for row in rows:
        flat: Dict[str, Any] = {}
        _flatten("", row, flat)
        flat_rows.append(flat)
        keys.update(flat.keys())
    ordered = sorted(keys)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ordered, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat_rows)


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
        for node, vals in buckets.items()
    }


def _feedback_item(obj: Any) -> Optional[Dict[str, Any]]:
    if isinstance(obj, dict):
        return {
            "sender": str(obj.get("sender") or ""),
            "error_type": str(obj.get("error_type") or ""),
            "comment": str(obj.get("comment") or ""),
            "revision_index": obj.get("revision_index"),
        }
    text = str(obj or "").strip()
    if not text:
        return None

    def _extract_single_quoted_field(blob: str, field: str) -> str:
        prefix = f"{field}='"
        start = blob.find(prefix)
        if start < 0:
            return ""
        start += len(prefix)
        end = blob.find("'", start)
        if end < 0:
            return ""
        return blob[start:end]

    def _extract_int_field(blob: str, field: str) -> Optional[int]:
        prefix = f"{field}="
        start = blob.find(prefix)
        if start < 0:
            return None
        start += len(prefix)
        digits: List[str] = []
        for ch in blob[start:]:
            if ch.isdigit():
                digits.append(ch)
                continue
            break
        if not digits:
            return None
        return int("".join(digits))

    return {
        "sender": _extract_single_quoted_field(text, "sender"),
        "error_type": _extract_single_quoted_field(text, "error_type"),
        "comment": _extract_single_quoted_field(text, "comment") or text,
        "revision_index": _extract_int_field(text, "revision_index"),
    }


def _feedback_by_sender(row: Dict[str, Any], sender: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in row.get("critic_feedback") or []:
        parsed = _feedback_item(item)
        if parsed and parsed.get("sender", "").lower() == sender.lower():
            out.append(parsed)
    return out


def _feedback_categories(items: Sequence[Any]) -> List[str]:
    cats: List[str] = []
    for item in items:
        text = str(item or "")
        start = text.find("[")
        end = text.find("]", start + 1) if start >= 0 else -1
        if start >= 0 and end > start + 1:
            cats.append(text[start + 1:end])
    return cats


def _first_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    for idx, ch in enumerate(stripped):
        if ch not in ".!?":
            continue
        next_idx = idx + 1
        if next_idx >= len(stripped) or stripped[next_idx].isspace():
            return stripped[: next_idx].strip()
    return stripped


def _revision_constraints_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    card = row.get("finalizer_input_card") or {}
    constraints = card.get("revision_constraints") if isinstance(card, dict) else None
    if isinstance(constraints, dict):
        return constraints
    constraints = row.get("revision_constraints") or {}
    return constraints if isinstance(constraints, dict) else {}


def _final_report_struct_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    report = row.get("final_report_struct")
    if isinstance(report, dict):
        return report
    preview = row.get("final_strategy_preview") or {}
    report = preview.get("final_report") if isinstance(preview, dict) else None
    return report if isinstance(report, dict) else {}


def _report_text_sections(row: Dict[str, Any]) -> List[str]:
    report = _final_report_struct_from_row(row)
    sections: List[str] = []
    for value in (
        row.get("answer_for_eval"),
        row.get("answer"),
        row.get("answer_rendered_markdown"),
        row.get("final_report"),
        report.get("conversation_reply") if isinstance(report, dict) else None,
        report.get("macro_summary") if isinstance(report, dict) else None,
    ):
        text = str(value or "").strip()
        if text:
            sections.append(text)
    for item in report.get("key_risks_and_hedges") or []:
        text = str(item or "").strip()
        if text:
            sections.append(text)
    return sections


def _text_blob(row: Dict[str, Any]) -> str:
    return "\n".join(section.lower() for section in _report_text_sections(row))


def _trade_ideas_from_row(row: Dict[str, Any]) -> List[Any]:
    report = _final_report_struct_from_row(row)
    trade_ideas = report.get("trade_ideas") if isinstance(report, dict) else None
    return list(trade_ideas or []) if isinstance(trade_ideas, list) else []


def _finalizer_input_card_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    card = row.get("finalizer_input_card")
    return card if isinstance(card, dict) else {}


def _retrieval_outcome_summary_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    card = _finalizer_input_card_from_row(row)
    summary = card.get("retrieval_outcome_summary") if isinstance(card, dict) else None
    if isinstance(summary, dict):
        return summary
    summary = row.get("retrieval_outcome_summary")
    return summary if isinstance(summary, dict) else {}


def _scope_contract_summary_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    card = _finalizer_input_card_from_row(row)
    summary = card.get("scope_contract_summary") if isinstance(card, dict) else None
    if isinstance(summary, dict):
        return summary
    summary = row.get("scope_contract_summary")
    return summary if isinstance(summary, dict) else {}


def _contains_any_phrase(blob: str, phrases: Sequence[str]) -> bool:
    lowered = blob.lower()
    return any(str(phrase).lower() in lowered for phrase in phrases)


def _why_not_now_present(row: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    report = _final_report_struct_from_row(row)
    sections: List[str] = []
    for value in (
        row.get("answer_for_eval"),
        row.get("answer"),
        report.get("conversation_reply") if isinstance(report, dict) else None,
    ):
        text = str(value or "").strip().lower()
        if text:
            sections.append(text)
    for item in report.get("key_risks_and_hedges") or []:
        text = str(item or "").strip().lower()
        if text:
            sections.append(text)
    why_not_now_text = str(constraints.get("why_not_now_text") or "").strip().lower()
    if why_not_now_text and any(why_not_now_text in section for section in sections):
        return True
    fallback_markers = [
        "why not now",
        "data insufficient",
        "does not support a live strike-level",
        "until execution conditions improve",
        "until stronger silver options support is available",
        "until stronger options support is available",
    ]
    return any(_contains_any_phrase(section, fallback_markers) for section in sections)


def _evaluate_downgraded_answer_state(
    *,
    row: Dict[str, Any],
    final_mode: str,
    constraints: Dict[str, Any],
    risk_disclosure_pass: Optional[bool],
) -> Dict[str, Any]:
    report = _final_report_struct_from_row(row)
    retrieval_outcome = _retrieval_outcome_summary_from_row(row)
    scope_contract = _scope_contract_summary_from_row(row)
    answer_present = bool(str(row.get("answer_for_eval") or row.get("answer") or "").strip())
    why_not_now_required = bool(constraints.get("must_explain_why_not_now"))
    risk_required = bool(constraints.get("must_disclose_risk"))
    return {
        "is_non_actionable_mode": final_mode in {"informational_only", "directional_watchlist"},
        "risk_required": risk_required,
        "risk_retained": None if not risk_required else bool(risk_disclosure_pass),
        "why_not_now_required": why_not_now_required,
        "why_not_now_present": None if not why_not_now_required else _why_not_now_present(row, constraints),
        "trade_ideas_present": bool(_trade_ideas_from_row(row)),
        "answer_present": answer_present,
        "retrieval_outcome_summary": retrieval_outcome,
        "scope_contract_summary": scope_contract,
        "final_report_struct": report,
    }


def _risk_disclosure_present(row: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    sections = [section.lower() for section in _report_text_sections(row)]
    main_risk_text = str(constraints.get("main_risk_text") or "").strip().lower()
    if main_risk_text and any(main_risk_text in section for section in sections):
        return True
    market_impact_risk = str(constraints.get("market_impact_risk") or "").strip().lower()
    risk_needles = [
        "market impact risk is high",
        "market impact risk high",
        "execution conditions improve",
        "execution risk",
    ]
    if market_impact_risk == "high":
        risk_needles.extend(
            [
                "high market impact risk",
                "non-actionable until execution conditions improve",
            ]
        )
    return any(_contains_any_phrase(section, risk_needles) for section in sections)


def _illustrative_structure_present(row: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    sections = [section.lower() for section in _report_text_sections(row)]
    hint = str(constraints.get("illustrative_structure_hint") or "").strip().lower()
    if hint and any(hint in section for section in sections):
        return True
    needles = [
        "illustrative only",
        "not a live recommendation",
        "not a current recommendation",
        "not a current or live recommendation",
        "template to watch",
        "if conditions improved",
        "cleaner template",
    ]
    return any(_contains_any_phrase(section, needles) for section in sections)


def _contains_contract_level_precision(blob: str) -> bool:
    tokens = blob.replace("/", " ").replace("-", " ").replace("(", " ").replace(")", " ").split()
    for token in tokens:
        lowered = token.lower().strip(".,:;")
        if lowered.endswith(("c", "p")) and lowered[:-1].replace(".", "", 1).isdigit():
            return True
    precision_needles = [
        "strike ",
        "strikes ",
        "expiration ",
        "expiry ",
        "30-dte",
        "45-dte",
        "60-dte",
        "buy the ",
        "sell the ",
        "open a ",
        "enter a ",
        "initiate a ",
        "establish a ",
    ]
    return any(needle in blob for needle in precision_needles)


def _contains_live_trade_wording(blob: str) -> bool:
    live_needles = [
        "buy the ",
        "sell the ",
        "open a ",
        "open the ",
        "enter a ",
        "enter the ",
        "initiate a ",
        "establish a ",
        "put on a ",
        "take the ",
    ]
    return any(needle in blob for needle in live_needles)


def _evaluate_illustrative_wording(
    *,
    final_mode: str,
    constraints: Dict[str, Any],
    row: Dict[str, Any],
) -> Dict[str, Any]:
    illustrative_applicable = constraints.get("structure_visibility_mode") == "illustrative_structure"
    illustrative_present = _illustrative_structure_present(row, constraints) if illustrative_applicable else None
    if not illustrative_applicable:
        return {
            "illustrative_applicable": False,
            "illustrative_structure_present": None,
            "illustrative_boundary_clear": None,
            "illustrative_compliance_pass": True,
            "illustrative_failure_reason": None,
        }

    if not illustrative_present:
        return {
            "illustrative_applicable": True,
            "illustrative_structure_present": False,
            "illustrative_boundary_clear": bool(constraints.get("forbid_actionable_recommendation")),
            "illustrative_compliance_pass": True,
            "illustrative_failure_reason": None,
        }

    blob = _text_blob(row)
    allow_illustrative = bool(constraints.get("allow_illustrative_structure"))
    trade_ideas_present = bool(_trade_ideas_from_row(row))
    active_trade_wording = trade_ideas_present or (
        _contains_live_trade_wording(blob) and _contains_contract_level_precision(blob)
    )
    boundary_clear = bool(constraints.get("forbid_actionable_recommendation")) and not trade_ideas_present

    if not allow_illustrative:
        return {
            "illustrative_applicable": True,
            "illustrative_structure_present": True,
            "illustrative_boundary_clear": boundary_clear,
            "illustrative_compliance_pass": False,
            "illustrative_failure_reason": "Illustrative wording appeared even though the finalizer contract did not allow it.",
        }

    if final_mode == "directional_watchlist":
        if active_trade_wording:
            failure_reason = "Illustrative wording in directional_watchlist mode crossed into active trade wording."
            compliance_pass = False
        elif not boundary_clear:
            failure_reason = "Directional_watchlist illustrative wording was not anchored to a non-live watchlist boundary."
            compliance_pass = False
        else:
            failure_reason = None
            compliance_pass = True
    elif final_mode == "informational_only":
        if active_trade_wording:
            failure_reason = "Illustrative wording in informational_only mode crossed into active recommendation wording."
            compliance_pass = False
        elif bool(constraints.get("market_read_only")):
            failure_reason = "Informational_only answer carried market_read_only posture that belongs in directional_watchlist."
            compliance_pass = False
        elif not boundary_clear:
            failure_reason = "Informational_only illustrative wording lacked a clearly degraded non-live boundary."
            compliance_pass = False
        else:
            failure_reason = None
            compliance_pass = True
    else:
        failure_reason = None
        compliance_pass = True

    return {
        "illustrative_applicable": True,
        "illustrative_structure_present": True,
        "illustrative_boundary_clear": boundary_clear,
        "illustrative_compliance_pass": compliance_pass,
        "illustrative_failure_reason": failure_reason,
    }


def _evaluate_non_actionable_compliance(
    *,
    final_mode: str,
    constraints: Dict[str, Any],
    row: Dict[str, Any],
    query_first_answer_pass: bool,
    risk_disclosure_pass: Optional[bool],
) -> Dict[str, Any]:
    if final_mode not in {"informational_only", "directional_watchlist"}:
        return {
            "boundary_pass": True,
            "mode_specific_compliance_failures": [],
            "illustrative": _evaluate_illustrative_wording(final_mode=final_mode, constraints=constraints, row=row),
        }

    blob = _text_blob(row)
    trade_ideas_present = bool(_trade_ideas_from_row(row))
    risk_required = bool(constraints.get("must_disclose_risk"))
    risk_ok = True if not risk_required else bool(risk_disclosure_pass)
    must_explain_why_not_now = bool(constraints.get("must_explain_why_not_now"))
    illustrative = _evaluate_illustrative_wording(final_mode=final_mode, constraints=constraints, row=row)
    failures: List[str] = []

    if not risk_ok:
        failures.append(f"{final_mode} answer omitted required risk language.")

    if final_mode == "informational_only":
        if must_explain_why_not_now:
            if not _why_not_now_present(row, constraints):
                failures.append("Informational_only degraded mode required a why-not-now explanation, but it was missing.")
        if trade_ideas_present:
            failures.append("Informational_only answer included trade_ideas despite degraded mode.")
        if _contains_live_trade_wording(blob) and _contains_contract_level_precision(blob):
            failures.append("Informational_only answer crossed into active trade wording.")
    else:
        if trade_ideas_present:
            failures.append("Directional_watchlist answer included trade_ideas instead of staying monitoring-only.")
        if _contains_live_trade_wording(blob) and _contains_contract_level_precision(blob):
            failures.append("Directional_watchlist answer included contract-level action rather than monitoring-level framing.")

    if not illustrative.get("illustrative_compliance_pass", True) and illustrative.get("illustrative_failure_reason"):
        failures.append(str(illustrative["illustrative_failure_reason"]))

    boundary_pass = not bool(failures)
    return {
        "boundary_pass": boundary_pass,
        "mode_specific_compliance_failures": failures,
        "illustrative": illustrative,
    }


def _analyst_structure_hint_present(draft: str) -> bool:
    lowered = (draft or "").lower()
    if "structure hint:" in lowered:
        return True
    return _contains_any(CONCRETE_STRUCTURE_PATTERNS, lowered)


def _query_first_pass(row: Dict[str, Any], structured_truth: Dict[str, Any], missing_slots: Sequence[str]) -> bool:
    sentence = _first_sentence(_answer_body(row))
    if not sentence:
        return False
    eval_out = _intent_coverage(structured_truth, {"answer_for_eval": sentence, "answer": sentence}, missing_slots)
    answered = sum(1 for status in eval_out.get("slot_status", {}).values() if status in {"answered", "disclosed_unanswerable"})
    total = len(eval_out.get("slot_status", {}))
    return answered >= 1 if total else True


def _retrieval_to_analyst_capture(analyst_draft: str, contexts: Sequence[str]) -> Dict[str, Any]:
    metric_map = _extract_metric_map_from_contexts(contexts)
    filtered = dict(metric_map)
    family = ""
    if "__family__" in metric_map:
        family = str(metric_map["__family__"])
    analyst_numbers = _extract_numbers(analyst_draft)
    numeric_values: List[float] = []
    key_hits = 0
    keys_considered = 0
    family_filters = {
        "options_microstructure": ("pcr", "iv", "skew", "liquid", "open_interest", "volume", "spread", "impact", "contract", "dte"),
        "single_name_options": ("iv", "skew", "liquid", "open_interest", "volume", "spread", "impact", "contract", "dte"),
        "cross_asset_regime": ("iv", "vix", "gspc", "ixic", "dxy", "change_pct", "macro"),
        "insider_flow_driven": ("liquid", "open_interest", "volume", "spread", "impact", "contract", "iv"),
        "macro_geopolitics_risk": ("gpr", "gld", "spot", "iv", "liquid", "open_interest", "volume", "spread", "impact"),
        "geopolitical_commodity": ("gpr", "gld", "spot", "iv", "liquid", "open_interest", "volume", "spread", "impact"),
    }
    if family and family in family_filters:
        keywords = tuple(str(keyword).lower() for keyword in family_filters[family])
        filtered = {
            key: value
            for key, value in metric_map.items()
            if any(keyword in str(key).lower() for keyword in keywords)
        }
        if not filtered:
            filtered = dict(metric_map)
    for key, raw_value in filtered.items():
        if key == "__family__":
            continue
        keys_considered += 1
        if key.lower().replace("_", " ") in analyst_draft.lower():
            key_hits += 1
        try:
            numeric_values.append(float(str(raw_value).replace("%", "").replace(",", "").split()[0]))
        except (TypeError, ValueError):
            continue
    if numeric_values:
        numeric_capture = sum(1 for value in numeric_values if _near_match(value, analyst_numbers)) / len(numeric_values)
    else:
        numeric_capture = 1.0
    anchor_capture = (key_hits / keys_considered) if keys_considered else 1.0
    score = round((numeric_capture + anchor_capture) / 2.0, 4)
    return {
        "legacy_numeric_capture_rate": round(numeric_capture, 4),
        "legacy_anchor_capture_rate": round(anchor_capture, 4),
        "legacy_raw_metric_retention_score": score,
        "retrieval_numeric_capture_rate": round(numeric_capture, 4),
        "retrieval_anchor_capture_rate": round(anchor_capture, 4),
        "evidence_capture_score": score,
    }


def _as_eval_row(row: Dict[str, Any], answer: str) -> Dict[str, Any]:
    cloned = dict(row)
    cloned["answer"] = answer
    cloned["answer_for_eval"] = answer
    return cloned


def _analyst_metrics(case: Dict[str, Any], row: Dict[str, Any], structured_truth: Dict[str, Any]) -> Dict[str, Any]:
    draft = str(row.get("analyst_draft") or "")
    draft_row = _as_eval_row(row, draft)
    missing_slots = _missing_query_slots(case, row, structured_truth)
    intent = _intent_coverage(structured_truth, draft_row, missing_slots)
    truthfulness = _financial_truthfulness(case, draft_row, structured_truth)
    capture_contexts = list(row.get("retrieved_contexts") or [])
    capture_contexts.append(f"Silver metric: __family__ = {structured_truth.get('query_family')}")
    capture = _retrieval_to_analyst_capture(draft, capture_contexts)
    concrete_structure = _contains_any(CONCRETE_STRUCTURE_PATTERNS, draft)
    truth_checks = truthfulness.get("checks", {})
    logic_integrity_rate = round(mean([
        1.0 if truth_checks.get("regime_logic_pass", True) else 0.0,
        1.0 if truth_checks.get("sec_taxonomy_pass", True) else 0.0,
    ]), 4)
    truth_integrity_rate = round(mean([
        1.0 if truth_checks.get("unsupported_fact_pass", True) else 0.0,
        1.0 if truth_checks.get("missing_info_honesty_pass", True) else 0.0,
        1.0 if truth_checks.get("financial_common_sense_pass", True) else 0.0,
    ]), 4)
    return {
        "runs": int((row.get("node_counts") or {}).get("analyst", 0)),
        "draft_len": len(draft),
        "intent_coverage_score": intent.get("score", 0.0),
        "slot_status": intent.get("slot_status", {}),
        "slot_answer_rate": intent.get("slot_answer_rate", intent.get("score", 0.0)),
        "slot_evidence_strength": intent.get("slot_evidence_strength", {}),
        "slot_disclosure_honesty": intent.get("slot_disclosure_honesty", {}),
        "semantic_evidence_carry_score": intent.get("semantic_evidence_carry_score", 1.0),
        "contract_source": intent.get("contract_source", "structured_truth_only"),
        "truthfulness_pass": bool(truthfulness.get("pass")),
        "truthfulness_checks": truth_checks,
        "truthfulness_issues": truthfulness.get("failures", []),
        "coverage_disclosure_pass": _coverage_disclosure_pass(case, draft_row, structured_truth),
        "concrete_structure_present": concrete_structure,
        "logic_integrity_rate": logic_integrity_rate,
        "truth_integrity_rate": truth_integrity_rate,
        "structure_hint_present": _analyst_structure_hint_present(draft),
        **capture,
    }


def _checker_metrics(
    case: Dict[str, Any],
    row: Dict[str, Any],
    structured_truth: Dict[str, Any],
    analyst: Dict[str, Any],
    final_truth: Dict[str, Any],
    final_coverage_disclosure_pass: bool,
) -> Dict[str, Any]:
    events = [ev for ev in (row.get("node_audit_log") or []) if isinstance(ev, dict) and ev.get("node") == "checker"]
    verdicts = [str(ev.get("verdict")) for ev in events if ev.get("verdict")]
    findings_total = sum(int(ev.get("findings_n") or 0) for ev in events)
    verdict_counts = dict(Counter(verdicts))
    adoption_signals: List[bool] = []
    analyst_checks = analyst.get("truthfulness_checks", {})
    final_checks = final_truth.get("checks", {})
    if not analyst_checks.get("unsupported_fact_pass", True):
        adoption_signals.append(bool(final_checks.get("unsupported_fact_pass", False)))
    if not analyst_checks.get("missing_info_honesty_pass", True):
        adoption_signals.append(bool(final_checks.get("missing_info_honesty_pass", False)))
    if not analyst_checks.get("structure_support_pass", True):
        adoption_signals.append(bool(final_checks.get("structure_support_pass", False)))
    if findings_total > 0 and not adoption_signals:
        adoption_signals.append(bool(final_truth.get("pass")) and final_coverage_disclosure_pass)
    adoption_score = round(sum(1.0 for x in adoption_signals if x) / len(adoption_signals), 4) if adoption_signals else 1.0
    return {
        "runs": len(events),
        "verdict_counts": verdict_counts,
        "findings_total": findings_total,
        "findings_per_run": round(findings_total / len(events), 4) if events else 0.0,
        "minor_verdict_count": verdict_counts.get("minor", 0),
        "fatal_verdict_count": verdict_counts.get("fatal", 0),
        "fix_adoption_score": adoption_score,
        "contributed_to_fix": findings_total > 0 and adoption_score >= 0.5,
    }


def _critic_metrics(
    case: Dict[str, Any],
    row: Dict[str, Any],
    structured_truth: Dict[str, Any],
    analyst: Dict[str, Any],
    final_truth: Dict[str, Any],
    final_mode: str,
    final_coverage_disclosure_pass: bool,
) -> Dict[str, Any]:
    events = [ev for ev in (row.get("node_audit_log") or []) if isinstance(ev, dict) and ev.get("node") == "critic"]
    verdicts = [str(ev.get("verdict")) for ev in events if ev.get("verdict")]
    verdict_counts = dict(Counter(verdicts))
    findings_total = sum(int(ev.get("findings_n") or 0) for ev in events)
    fatal_feedback = _feedback_by_sender(row, "Critic")
    minor_suggestions = row.get("critic_minor_suggestions") or []
    categories = _feedback_categories([f.get("comment") for f in fatal_feedback] + list(minor_suggestions))
    actionable_modes = {"actionable_options"}
    concrete_removed = bool(analyst.get("concrete_structure_present")) and final_mode not in actionable_modes
    constraints = _revision_constraints_from_row(row)
    risk_disclosure_success = (
        _risk_disclosure_present(row, constraints)
        if constraints.get("must_disclose_risk")
        else None
    )
    final_row = _as_eval_row(row, str(row.get("answer_for_eval") or row.get("answer") or ""))
    missing_slots = _missing_query_slots(case, row, structured_truth)
    final_intent = _intent_coverage(structured_truth, final_row, missing_slots)
    downgrade_state = _evaluate_downgraded_answer_state(
        row=row,
        final_mode=final_mode,
        constraints=constraints,
        risk_disclosure_pass=risk_disclosure_success,
    )
    query_first = _query_first_pass(row, structured_truth, missing_slots)
    compliance_eval = _evaluate_non_actionable_compliance(
        final_mode=final_mode,
        constraints=constraints,
        row=row,
        query_first_answer_pass=query_first,
        risk_disclosure_pass=risk_disclosure_success,
    )
    non_actionable_information_retention_delta: Optional[float]
    if final_mode in {"informational_only", "directional_watchlist"} and constraints.get("forbid_actionable_recommendation"):
        non_actionable_information_retention_delta = round(
            float(final_intent.get("semantic_evidence_carry_score", 0.0))
            - float(analyst.get("semantic_evidence_carry_score", 0.0)),
            4,
        )
    else:
        non_actionable_information_retention_delta = None
    actionability_downgrade_success = (
        final_mode in {"informational_only", "directional_watchlist"}
        if constraints.get("forbid_actionable_recommendation")
        else True
    )
    applicable: List[bool] = []
    if verdict_counts.get("fatal", 0) or "iv_regime_fit" in categories or "evidence_sufficiency_and_abstention" in categories:
        applicable.append(concrete_removed or final_mode in {"informational_only", "directional_watchlist"})
    if "insider_signal_weakness" in categories:
        applicable.append(final_coverage_disclosure_pass)
    if "macro_contradiction" in categories:
        applicable.append(bool(final_truth.get("checks", {}).get("regime_logic_pass", False)) or final_mode in {"informational_only", "directional_watchlist"})
    if not applicable and (verdict_counts.get("pass", 0) or minor_suggestions):
        applicable.append(bool(final_truth.get("pass")))
    if risk_disclosure_success is not None:
        applicable.append(bool(risk_disclosure_success))
    applicable.append(bool(actionability_downgrade_success))
    adoption_score = round(sum(1.0 for x in applicable if x) / len(applicable), 4) if applicable else 1.0
    spurious_fatal_proxy = bool(verdict_counts.get("fatal", 0) and not concrete_removed and final_mode in actionable_modes)
    retention_delta_score = (
        max(0.0, min(1.0, 1.0 + float(non_actionable_information_retention_delta)))
        if non_actionable_information_retention_delta is not None
        else 0.5
    )
    return {
        "runs": len(events),
        "verdict_counts": verdict_counts,
        "findings_total": findings_total,
        "fatal_feedback_n": len(fatal_feedback),
        "minor_suggestions_n": len(minor_suggestions),
        "suggestion_categories": categories,
        "feedback_adoption_score": adoption_score,
        "concrete_structure_demoted": concrete_removed,
        "mode_downgrade_adopted": final_mode in {"informational_only", "directional_watchlist"},
        "actionability_downgrade_success": bool(actionability_downgrade_success),
        "risk_disclosure_success": risk_disclosure_success,
        "non_actionable_information_retention_delta": non_actionable_information_retention_delta,
        "non_actionable_information_retention_delta_score": retention_delta_score,
        "spurious_fatal_proxy": spurious_fatal_proxy,
    }


def _finalizer_metrics(
    case: Dict[str, Any],
    row: Dict[str, Any],
    structured_truth: Dict[str, Any],
    final_truth: Dict[str, Any],
    final_coverage_disclosure_pass: bool,
) -> Dict[str, Any]:
    final_row = _as_eval_row(row, str(row.get("answer_for_eval") or row.get("answer") or ""))
    missing_slots = _missing_query_slots(case, row, structured_truth)
    intent = _intent_coverage(structured_truth, final_row, missing_slots)
    evidence = _evidence_retention(row)
    final_mode = _resolved_mode(row)
    constraints = _revision_constraints_from_row(row)
    disclosure_retention_pass = final_coverage_disclosure_pass
    query_first_answer_pass = _query_first_pass(final_row, structured_truth, missing_slots)
    rendering_drift_flag = not final_truth.get("pass", False)
    risk_required = bool(constraints.get("must_disclose_risk"))
    risk_disclosure_pass = _risk_disclosure_present(row, constraints) if risk_required else None
    downgrade_state = _evaluate_downgraded_answer_state(
        row=row,
        final_mode=final_mode,
        constraints=constraints,
        risk_disclosure_pass=risk_disclosure_pass,
    )
    compliance_eval = _evaluate_non_actionable_compliance(
        final_mode=final_mode,
        constraints=constraints,
        row=row,
        query_first_answer_pass=query_first_answer_pass,
        risk_disclosure_pass=risk_disclosure_pass,
    )
    illustrative = compliance_eval.get("illustrative") or {}
    illustrative_applicable = illustrative.get("illustrative_applicable")
    illustrative_structure_present = illustrative.get("illustrative_structure_present")
    illustrative_boundary_clear = illustrative.get("illustrative_boundary_clear")
    illustrative_compliance_pass = illustrative.get("illustrative_compliance_pass", True)
    illustrative_failure_reason = illustrative.get("illustrative_failure_reason")
    boundary_pass = bool(compliance_eval.get("boundary_pass", True))
    if final_mode in {"informational_only", "directional_watchlist"}:
        if (
            (downgrade_state["risk_required"] and downgrade_state["risk_retained"] is not True)
            or (downgrade_state["why_not_now_required"] and downgrade_state["why_not_now_present"] is not True)
            or (downgrade_state["answer_present"] is not True)
        ):
            downgraded_answer_usefulness_floor = 0.0
        elif query_first_answer_pass:
            downgraded_answer_usefulness_floor = 1.0
        else:
            downgraded_answer_usefulness_floor = 0.5
    else:
        downgraded_answer_usefulness_floor = None
    truth_checks = final_truth.get("checks", {})
    logic_integrity_rate = round(mean([
        1.0 if truth_checks.get("regime_logic_pass", True) else 0.0,
        1.0 if truth_checks.get("sec_taxonomy_pass", True) else 0.0,
    ]), 4)
    truth_integrity_rate = round(mean([
        1.0 if truth_checks.get("unsupported_fact_pass", True) else 0.0,
        1.0 if truth_checks.get("missing_info_honesty_pass", True) else 0.0,
        1.0 if truth_checks.get("financial_common_sense_pass", True) else 0.0,
    ]), 4)
    info_components: List[float] = [1.0 if query_first_answer_pass else 0.0, float(intent.get("score", 0.0))]
    info_components.append(1.0 if not risk_required else (1.0 if risk_disclosure_pass else 0.0))
    info_components.append(1.0 if boundary_pass else 0.0)
    if illustrative_applicable:
        info_components.append(1.0 if illustrative_compliance_pass else 0.0)
    information_value_score = round(mean(info_components), 4)
    legacy_compliance_failures = list(final_truth.get("compliance_failures", []) or [])
    adjusted_compliance_failures: List[str] = []
    for item in legacy_compliance_failures:
        lowered = str(item).lower()
        if any(
            needle in lowered
            for needle in (
                "illustrative",
                "non-actionable",
                "watchlist",
                "live recommendation",
                "informational",
            )
        ):
            continue
        adjusted_compliance_failures.append(str(item))
    adjusted_compliance_failures.extend(compliance_eval.get("mode_specific_compliance_failures", []))
    critical_failures = list(final_truth.get("critical_failures", []) or [])
    material_failures = list(final_truth.get("material_failures", []) or [])
    if critical_failures:
        highest_failure_severity = "critical"
    elif material_failures:
        highest_failure_severity = "material"
    elif adjusted_compliance_failures:
        highest_failure_severity = "compliance"
    else:
        highest_failure_severity = None
    final_truthfulness_pass = not bool(critical_failures or material_failures or adjusted_compliance_failures)
    rendering_drift_flag = not final_truthfulness_pass
    return {
        "runs": int((row.get("node_counts") or {}).get("finalizer", 0)),
        "status": row.get("finalizer_status"),
        "trigger_node": row.get("finalizer_trigger_node"),
        "confidence": row.get("finalizer_confidence"),
        "degraded_reason": row.get("finalizer_degraded_reason"),
        "mode": final_mode,
        "query_first_answer_pass": query_first_answer_pass,
        "disclosure_retention_pass": disclosure_retention_pass,
        "risk_disclosure_required": risk_required,
        "risk_disclosure_pass": risk_disclosure_pass,
        "illustrative_applicable": illustrative_applicable,
        "illustrative_structure_present": illustrative_structure_present,
        "illustrative_boundary_clear": illustrative_boundary_clear,
        "illustrative_compliance_pass": illustrative_compliance_pass,
        "illustrative_failure_reason": illustrative_failure_reason,
        "boundary_pass": boundary_pass,
        "downgraded_answer_usefulness_floor": downgraded_answer_usefulness_floor,
        "logic_integrity_rate": logic_integrity_rate,
        "truth_integrity_rate": truth_integrity_rate,
        "information_value_score": information_value_score,
        "rendering_drift_flag": rendering_drift_flag,
        "final_truthfulness_pass": final_truthfulness_pass,
        "final_truthfulness_checks": final_truth.get("checks", {}),
        "highest_failure_severity": highest_failure_severity,
        "critical_failures": critical_failures,
        "material_failures": material_failures,
        "compliance_failures": adjusted_compliance_failures,
        "intent_coverage_score": intent.get("score", 0.0),
        "slot_status": intent.get("slot_status", {}),
        "slot_answer_rate": intent.get("slot_answer_rate", intent.get("score", 0.0)),
        "slot_evidence_strength": intent.get("slot_evidence_strength", {}),
        "slot_disclosure_honesty": intent.get("slot_disclosure_honesty", {}),
        "semantic_evidence_retention_score": intent.get("semantic_evidence_carry_score", 1.0),
        "disclosure_honesty_rate": intent.get("slot_disclosure_honesty_rate", 1.0),
        "contract_source": intent.get("contract_source", "structured_truth_only"),
        **evidence,
    }


def _revision_path(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for ev in row.get("node_audit_log") or []:
        if not isinstance(ev, dict):
            continue
        node = str(ev.get("node") or "")
        revision_n = ev.get("revision_n")
        verdict = ev.get("verdict")
        if node:
            if verdict:
                parts.append(f"{node}@r{revision_n}:{verdict}")
            else:
                parts.append(f"{node}@r{revision_n}")
    return " -> ".join(parts)


def _path_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    node_log = row.get("node_audit_log") or []
    checker_events = [ev for ev in node_log if isinstance(ev, dict) and ev.get("node") == "checker"]
    critic_events = [ev for ev in node_log if isinstance(ev, dict) and ev.get("node") == "critic"]
    finalizer_status = row.get("finalizer_status")
    trigger = row.get("finalizer_trigger_node")
    revision_count = int(row.get("revision_count") or 0)
    return {
        "revision_count": revision_count,
        "degraded": finalizer_status == "degraded",
        "degraded_reason": row.get("finalizer_degraded_reason"),
        "finalizer_status": finalizer_status,
        "finalizer_trigger_node": trigger,
        "checker_minor_count": sum(1 for ev in checker_events if str(ev.get("verdict")) == "minor"),
        "checker_pass_count": sum(1 for ev in checker_events if str(ev.get("verdict")) == "pass"),
        "critic_fatal_count": sum(1 for ev in critic_events if str(ev.get("verdict")) == "fatal"),
        "critic_pass_count": sum(1 for ev in critic_events if str(ev.get("verdict")) == "pass"),
        "circuit_break_after_checker": bool(trigger == "checker" and finalizer_status == "degraded"),
        "completed_after_critic": bool(trigger == "critic" and finalizer_status == "complete"),
        "revision_path": _revision_path(row),
    }


def _node_contribution(
    analyst: Dict[str, Any],
    checker: Dict[str, Any],
    critic: Dict[str, Any],
    finalizer: Dict[str, Any],
) -> Dict[str, Any]:
    analyst_logic = float(analyst.get("logic_integrity_rate", 0.0))
    final_logic = float(finalizer.get("logic_integrity_rate", 0.0))
    analyst_truth = float(analyst.get("truth_integrity_rate", 0.0))
    final_truth = float(finalizer.get("truth_integrity_rate", 0.0))
    analyst_information = round(mean([
        float(analyst.get("semantic_evidence_carry_score", 0.0)),
        float(analyst.get("slot_answer_rate", 0.0)),
    ]), 4)
    final_semantic = float(finalizer.get("semantic_evidence_retention_score", 0.0))
    final_informational_completeness = float(finalizer.get("information_value_score", 0.0))
    final_information = round((0.7 * final_semantic) + (0.3 * final_informational_completeness), 4)
    analyst_to_final_logic_gain = round(final_logic - analyst_logic, 4)
    analyst_to_final_truth_gain = round(final_truth - analyst_truth, 4)
    analyst_to_final_information_gain = round(final_information - analyst_information, 4)
    analyst_to_final_intent_gain = round(float(finalizer.get("intent_coverage_score", 0.0)) - float(analyst.get("intent_coverage_score", 0.0)), 4)
    disclosure_added_gain = 1.0 if (not analyst.get("coverage_disclosure_pass") and finalizer.get("disclosure_retention_pass")) else 0.0
    structure_safing_gain = 1.0 if (analyst.get("concrete_structure_present") and finalizer.get("mode") != "actionable_options") else 0.0
    analyst_score = round(mean([
        analyst_logic,
        analyst_truth,
        float(analyst.get("semantic_evidence_carry_score", 0.0)),
        float(analyst.get("slot_answer_rate", 0.0)),
    ]), 4)
    checker_score = round(mean([
        float(checker.get("fix_adoption_score", 1.0)),
        1.0 if not checker.get("fatal_verdict_count", 0) else 0.0,
        1.0 if checker.get("contributed_to_fix") or checker.get("findings_total", 0) == 0 else 0.5,
    ]), 4)
    critic_score = round(mean([
        float(critic.get("feedback_adoption_score", 1.0)),
        0.0 if critic.get("spurious_fatal_proxy") else 1.0,
        1.0 if critic.get("actionability_downgrade_success", True) else 0.0,
        0.5 if critic.get("risk_disclosure_success") is None else (1.0 if critic.get("risk_disclosure_success") else 0.0),
        float(critic.get("non_actionable_information_retention_delta_score", 0.5)),
    ]), 4)
    finalizer_score = round(
        (0.25 * final_logic)
        + (0.25 * final_truth)
        + (0.35 * final_semantic)
        + (0.15 * final_informational_completeness),
        4,
    )
    return {
        "analyst_to_final_logic_gain": analyst_to_final_logic_gain,
        "analyst_to_final_truth_gain": analyst_to_final_truth_gain,
        "analyst_to_final_information_gain": analyst_to_final_information_gain,
        "analyst_to_final_intent_gain": analyst_to_final_intent_gain,
        "disclosure_added_gain": disclosure_added_gain,
        "structure_safing_gain": structure_safing_gain,
        "analyst_contribution_score": analyst_score,
        "checker_contribution_score": checker_score,
        "critic_contribution_score": critic_score,
        "finalizer_contribution_score": finalizer_score,
    }


def _event_rows(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, ev in enumerate(row.get("node_audit_log") or []):
        if not isinstance(ev, dict):
            continue
        out.append(
            {
                "case_id": row.get("case_id"),
                "case_name": row.get("case_name"),
                "query": row.get("query"),
                "event_index": index,
                "node": ev.get("node"),
                "revision_n": ev.get("revision_n"),
                "verdict": ev.get("verdict"),
                "findings_n": ev.get("findings_n"),
                "latency_ms": ev.get("latency_ms"),
                "t_start": ev.get("t_start"),
                "key_state_in": ev.get("key_state_in"),
                "key_state_out": ev.get("key_state_out"),
                "finalizer_status": row.get("finalizer_status"),
                "revision_count": row.get("revision_count"),
            }
        )
    return out


def _evaluate_case(case: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    structured_truth = _structured_truth(case)
    final_truth = _financial_truthfulness(case, row, structured_truth)
    final_coverage_disclosure_pass = _coverage_disclosure_pass(case, row, structured_truth)
    analyst = _analyst_metrics(case, row, structured_truth)
    finalizer = _finalizer_metrics(case, row, structured_truth, final_truth, final_coverage_disclosure_pass)
    checker = _checker_metrics(case, row, structured_truth, analyst, final_truth, final_coverage_disclosure_pass)
    critic = _critic_metrics(case, row, structured_truth, analyst, final_truth, str(finalizer.get("mode") or ""), final_coverage_disclosure_pass)
    path = _path_metrics(row)
    contribution = _node_contribution(analyst, checker, critic, finalizer)
    return {
        "case_id": row.get("case_id"),
        "case_name": row.get("case_name"),
        "query": row.get("query"),
        "trace_status": row.get("trace_status"),
        "retrieved_sources": row.get("retrieved_sources") or [],
        "missing_sources": _missing_sources(case, row, structured_truth),
        "missing_query_slots": _missing_query_slots(case, row, structured_truth),
        "node_counts": _node_counts(row.get("node_audit_log")),
        "node_latency": _node_latency_summary(row.get("node_audit_log")),
        "path_analysis": path,
        "analyst": analyst,
        "checker": checker,
        "critic": critic,
        "finalizer": finalizer,
        "node_contribution": contribution,
        "node_audit_log": row.get("node_audit_log") or [],
    }


def _summarize(case_rows: Sequence[Dict[str, Any]], event_rows: Sequence[Dict[str, Any]], run_dir: Path) -> Dict[str, Any]:
    analyst_scores = [row["node_contribution"]["analyst_contribution_score"] for row in case_rows]
    checker_scores = [row["node_contribution"]["checker_contribution_score"] for row in case_rows]
    critic_scores = [row["node_contribution"]["critic_contribution_score"] for row in case_rows]
    finalizer_scores = [row["node_contribution"]["finalizer_contribution_score"] for row in case_rows]
    node_run_counts: Counter[str] = Counter()
    checker_verdict_counts: Counter[str] = Counter()
    critic_verdict_counts: Counter[str] = Counter()
    finalizer_status_counts: Counter[str] = Counter()
    trigger_counts: Counter[str] = Counter()
    revision_counts: List[int] = []
    degraded_count = 0
    circuit_break_count = 0
    completed_after_critic_count = 0
    for row in case_rows:
        node_run_counts.update(row.get("node_counts") or {})
        checker_verdict_counts.update(row["checker"].get("verdict_counts") or {})
        critic_verdict_counts.update(row["critic"].get("verdict_counts") or {})
        status = str(row["path_analysis"].get("finalizer_status") or "")
        trigger = str(row["path_analysis"].get("finalizer_trigger_node") or "")
        if status:
            finalizer_status_counts.update([status])
        if trigger:
            trigger_counts.update([trigger])
        revision_counts.append(int(row["path_analysis"].get("revision_count") or 0))
        if row["path_analysis"].get("degraded"):
            degraded_count += 1
        if row["path_analysis"].get("circuit_break_after_checker"):
            circuit_break_count += 1
        if row["path_analysis"].get("completed_after_critic"):
            completed_after_critic_count += 1
    return {
        "generated_at": datetime.now().isoformat(),
        "run_dir": str(run_dir),
        "n_cases": len(case_rows),
        "node_run_counts": dict(node_run_counts),
        "checker_verdict_counts": dict(checker_verdict_counts),
        "critic_verdict_counts": dict(critic_verdict_counts),
        "finalizer_status_counts": dict(finalizer_status_counts),
        "finalizer_trigger_counts": dict(trigger_counts),
        "mean_revision_count": round(mean(revision_counts), 4) if revision_counts else 0.0,
        "max_revision_count": max(revision_counts) if revision_counts else 0,
        "degraded_case_count": degraded_count,
        "degraded_case_rate": round(degraded_count / len(case_rows), 4) if case_rows else 0.0,
        "checker_circuit_break_count": circuit_break_count,
        "completed_after_critic_count": completed_after_critic_count,
        "analyst_slot_answer_rate": _mean(row["analyst"].get("slot_answer_rate") for row in case_rows),
        "analyst_semantic_evidence_carry_avg": _mean(row["analyst"].get("semantic_evidence_carry_score") for row in case_rows),
        "analyst_raw_metric_retention_avg": _mean(row["analyst"].get("legacy_raw_metric_retention_score") for row in case_rows),
        "analyst_intent_coverage_avg": _mean(row["analyst"].get("intent_coverage_score") for row in case_rows),
        "analyst_logic_integrity_rate": _mean(row["analyst"].get("logic_integrity_rate") for row in case_rows),
        "analyst_truth_integrity_rate": _mean(row["analyst"].get("truth_integrity_rate") for row in case_rows),
        "analyst_structure_hint_presence_rate": _mean(1.0 if row["analyst"].get("structure_hint_present") else 0.0 for row in case_rows),
        "checker_fix_adoption_avg": _mean(row["checker"].get("fix_adoption_score") for row in case_rows),
        "critic_feedback_adoption_avg": _mean(row["critic"].get("feedback_adoption_score") for row in case_rows),
        "critic_actionability_downgrade_success_rate": _mean(1.0 if row["critic"].get("actionability_downgrade_success") else 0.0 for row in case_rows),
        "critic_risk_disclosure_success_rate": _mean(
            1.0 if row["critic"].get("risk_disclosure_success") else 0.0
            for row in case_rows
            if row["critic"].get("risk_disclosure_success") is not None
        ),
        "critic_non_actionable_information_retention_delta_avg": _mean(
            row["critic"].get("non_actionable_information_retention_delta")
            for row in case_rows
            if row["critic"].get("non_actionable_information_retention_delta") is not None
        ),
        "critic_spurious_fatal_proxy_rate": _mean(1.0 if row["critic"].get("spurious_fatal_proxy") else 0.0 for row in case_rows),
        "finalizer_slot_answer_rate": _mean(row["finalizer"].get("slot_answer_rate") for row in case_rows),
        "finalizer_semantic_evidence_retention_avg": _mean(row["finalizer"].get("semantic_evidence_retention_score") for row in case_rows),
        "finalizer_disclosure_honesty_rate": _mean(row["finalizer"].get("disclosure_honesty_rate") for row in case_rows),
        "finalizer_evidence_retention_avg": _mean(row["finalizer"].get("evidence_retention_score") for row in case_rows),
        "finalizer_disclosure_retention_rate": _mean(1.0 if row["finalizer"].get("disclosure_retention_pass") else 0.0 for row in case_rows),
        "finalizer_logic_integrity_rate": _mean(row["finalizer"].get("logic_integrity_rate") for row in case_rows),
        "finalizer_truth_integrity_rate": _mean(row["finalizer"].get("truth_integrity_rate") for row in case_rows),
        "finalizer_information_value_avg": _mean(row["finalizer"].get("information_value_score") for row in case_rows),
        "finalizer_risk_disclosure_pass_rate": _mean(
            1.0 if row["finalizer"].get("risk_disclosure_pass") else 0.0
            for row in case_rows
            if row["finalizer"].get("risk_disclosure_pass") is not None
        ),
        "finalizer_illustrative_structure_rate": _mean(
            1.0 if row["finalizer"].get("illustrative_structure_present") else 0.0
            for row in case_rows
            if row["finalizer"].get("illustrative_structure_present") is not None
        ),
        "downgraded_answer_usefulness_floor_avg": _mean(
            row["finalizer"].get("downgraded_answer_usefulness_floor")
            for row in case_rows
            if row["finalizer"].get("downgraded_answer_usefulness_floor") is not None
        ),
        "finalizer_query_first_pass_rate": _mean(1.0 if row["finalizer"].get("query_first_answer_pass") else 0.0 for row in case_rows),
        "finalizer_truthfulness_pass_rate": _mean(1.0 if row["finalizer"].get("final_truthfulness_pass") else 0.0 for row in case_rows),
        "critical_failure_case_rate": _mean(1.0 if row["finalizer"].get("highest_failure_severity") == "critical" or row["finalizer"].get("critical_failures") else 0.0 for row in case_rows),
        "material_failure_case_rate": _mean(1.0 if row["finalizer"].get("material_failures") else 0.0 for row in case_rows),
        "compliance_failure_case_rate": _mean(1.0 if row["finalizer"].get("compliance_failures") else 0.0 for row in case_rows),
        "analyst_contribution_avg": _mean(analyst_scores),
        "checker_contribution_avg": _mean(checker_scores),
        "critic_contribution_avg": _mean(critic_scores),
        "finalizer_contribution_avg": _mean(finalizer_scores),
        "analyst_to_final_logic_gain_avg": _mean(row["node_contribution"].get("analyst_to_final_logic_gain") for row in case_rows),
        "analyst_to_final_truth_gain_avg": _mean(row["node_contribution"].get("analyst_to_final_truth_gain") for row in case_rows),
        "analyst_to_final_information_gain_avg": _mean(row["node_contribution"].get("analyst_to_final_information_gain") for row in case_rows),
        "analyst_to_final_intent_gain_avg": _mean(row["node_contribution"].get("analyst_to_final_intent_gain") for row in case_rows),
        "structure_safing_gain_rate": _mean(row["node_contribution"].get("structure_safing_gain") for row in case_rows),
        "event_rows": len(event_rows),
    }


def _node_high_summary(summary: Dict[str, Any]) -> str:
    degraded = summary.get("degraded_case_rate", 0.0) or 0.0
    truth = summary.get("finalizer_truthfulness_pass_rate", 0.0) or 0.0
    info = summary.get("downgraded_answer_usefulness_floor_avg", 0.0) or 0.0
    if truth >= 0.8 and info >= 0.6:
        return "The workflow is mostly keeping answers safe while still preserving useful non-actionable information under downgrade."
    if degraded >= 0.4:
        return "A large share of cases are still reaching degraded paths, so safety is being preserved partly by workflow friction rather than smooth convergence."
    return "The workflow is directionally sound, but the handoff between nodes still needs tightening to turn safety gains into consistently strong final answers."


def _metric_guide_sections(summary: Dict[str, Any]) -> List[Tuple[str, List[List[str]]]]:
    section_keys: List[Tuple[str, List[str]]] = [
        (
            "Workflow Overview",
            [
                "n_cases",
                "mean_revision_count",
                "degraded_case_rate",
                "checker_circuit_break_count",
                "completed_after_critic_count",
            ],
        ),
        (
            "Analyst",
            [
                "analyst_logic_integrity_rate",
                "analyst_truth_integrity_rate",
                "analyst_slot_answer_rate",
                "analyst_semantic_evidence_carry_avg",
                "analyst_intent_coverage_avg",
                "analyst_raw_metric_retention_avg",
            ],
        ),
        ("Checker", ["checker_fix_adoption_avg"]),
        (
            "Critic",
            [
                "critic_actionability_downgrade_success_rate",
                "critic_risk_disclosure_success_rate",
                "critic_non_actionable_information_retention_delta_avg",
            ],
        ),
        (
            "Finalizer",
            [
                "finalizer_logic_integrity_rate",
                "finalizer_truth_integrity_rate",
                "finalizer_slot_answer_rate",
                "finalizer_semantic_evidence_retention_avg",
                "finalizer_disclosure_honesty_rate",
                "finalizer_information_value_avg",
                "finalizer_risk_disclosure_pass_rate",
                "finalizer_illustrative_structure_rate",
                "downgraded_answer_usefulness_floor_avg",
            ],
        ),
        (
            "Outcome Quality",
            [
                "critical_failure_case_rate",
                "material_failure_case_rate",
                "compliance_failure_case_rate",
            ],
        ),
    ]
    sections: List[Tuple[str, List[List[str]]]] = []
    for title, keys in section_keys:
        rows: List[List[str]] = []
        for key in keys:
            value = summary.get(key) if key in summary else summary.get(key)
            rows.append([key, _fmt(value) if key != "n_cases" else html.escape(str(summary.get("n_cases", "N/A"))), NODE_METRIC_DESCRIPTIONS.get(key, "")])
        sections.append((title, rows))
    return sections


def _svg_radar(title: str, labels: Sequence[str], values: Sequence[Optional[float]], color: str = "#2563eb") -> str:
    cx, cy, r = 160, 160, 110
    points_axes: List[str] = []
    points_poly: List[str] = []
    label_html: List[str] = []
    n = max(1, len(labels))
    for idx, (label, value) in enumerate(zip(labels, values)):
        angle = (-math.pi / 2) + (2 * math.pi * idx / n)
        ax = cx + r * math.cos(angle)
        ay = cy + r * math.sin(angle)
        points_axes.append(f"<line x1='{cx}' y1='{cy}' x2='{ax:.1f}' y2='{ay:.1f}' stroke='#d9e0ea' />")
        score = max(0.0, min(1.0, float(value or 0.0)))
        px = cx + (r * score) * math.cos(angle)
        py = cy + (r * score) * math.sin(angle)
        points_poly.append(f"{px:.1f},{py:.1f}")
        lx = cx + (r + 18) * math.cos(angle)
        ly = cy + (r + 18) * math.sin(angle)
        label_html.append(f"<text x='{lx:.1f}' y='{ly:.1f}' font-size='11' text-anchor='middle' fill='#475467'>{html.escape(label)}</text>")
    rings = "".join(f"<circle cx='{cx}' cy='{cy}' r='{r * frac:.1f}' fill='none' stroke='#eef3fb' />" for frac in (0.25, 0.5, 0.75, 1.0))
    axes = "".join(points_axes)
    labels_svg = "".join(label_html)
    poly = " ".join(points_poly)
    return f"""
    <div class='chart-card'>
      <h3>{html.escape(title)}</h3>
      <svg viewBox='0 0 320 320' width='100%' height='320' role='img' aria-label='{html.escape(title)} radar chart'>
        {rings}
        {axes}
        <polygon points='{poly}' fill='{color}22' stroke='{color}' stroke-width='2'></polygon>
        {labels_svg}
      </svg>
    </div>"""


def _svg_line_chart(title: str, labels: Sequence[str], values: Sequence[Optional[float]], color: str = "#2563eb") -> str:
    width, height = 420, 220
    left, top, bottom = 36, 18, 34
    plot_w = width - left - 16
    plot_h = height - top - bottom
    clean = [max(0.0, min(1.0, float(v or 0.0))) for v in values]
    if len(clean) == 1:
        xs = [left + plot_w / 2]
    else:
        xs = [left + (plot_w * idx / max(1, len(clean) - 1)) for idx in range(len(clean))]
    ys = [top + plot_h * (1 - val) for val in clean]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    dots = "".join(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{color}' />" for x, y in zip(xs, ys))
    label_svg = "".join(
        f"<text x='{x:.1f}' y='{height - 10}' font-size='11' text-anchor='middle' fill='#667085'>{html.escape(label[:18])}</text>"
        for x, label in zip(xs, labels)
    )
    return f"""
    <div class='chart-card'>
      <h3>{html.escape(title)}</h3>
      <svg viewBox='0 0 {width} {height}' width='100%' height='{height}' role='img' aria-label='{html.escape(title)} line chart'>
        <line x1='{left}' y1='{top + plot_h}' x2='{width - 12}' y2='{top + plot_h}' stroke='#d9e0ea' />
        <line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' stroke='#d9e0ea' />
        <polyline fill='none' stroke='{color}' stroke-width='2.5' points='{poly}' />
        {dots}
        {label_svg}
      </svg>
    </div>"""


def _markdown_report(summary: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> str:
    critic_delta = summary.get("critic_non_actionable_information_retention_delta_avg")
    critic_delta_label = _critic_delta_label(critic_delta)
    summary_rows = [
        ["Analyst contribution avg", _fmt(summary.get("analyst_contribution_avg"))],
        ["Checker contribution avg", _fmt(summary.get("checker_contribution_avg"))],
        ["Critic contribution avg", _fmt(summary.get("critic_contribution_avg"))],
        ["Finalizer contribution avg", _fmt(summary.get("finalizer_contribution_avg"))],
        ["Mean revision count", _fmt(summary.get("mean_revision_count"))],
        ["Degraded case rate", _fmt(summary.get("degraded_case_rate"))],
        ["Checker fix adoption avg", _fmt(summary.get("checker_fix_adoption_avg"))],
        ["Critic feedback adoption avg", _fmt(summary.get("critic_feedback_adoption_avg"))],
        ["Critic downgrade success rate", _fmt(summary.get("critic_actionability_downgrade_success_rate"))],
        ["Critic risk disclosure success rate", _fmt(summary.get("critic_risk_disclosure_success_rate"))],
        ["Critic non-actionable information retention delta avg", _fmt(summary.get("critic_non_actionable_information_retention_delta_avg"))],
        ["Analyst slot answer rate", _fmt(summary.get("analyst_slot_answer_rate"))],
        ["Analyst semantic evidence carry avg", _fmt(summary.get("analyst_semantic_evidence_carry_avg"))],
        ["Analyst raw metric retention avg", _fmt(summary.get("analyst_raw_metric_retention_avg"))],
        ["Critical failure case rate", _fmt(summary.get("critical_failure_case_rate"))],
        ["Material failure case rate", _fmt(summary.get("material_failure_case_rate"))],
        ["Compliance failure case rate", _fmt(summary.get("compliance_failure_case_rate"))],
        ["Finalizer logic integrity rate", _fmt(summary.get("finalizer_logic_integrity_rate"))],
        ["Finalizer truth integrity rate", _fmt(summary.get("finalizer_truth_integrity_rate"))],
        ["Finalizer slot answer rate", _fmt(summary.get("finalizer_slot_answer_rate"))],
        ["Finalizer semantic evidence retention avg", _fmt(summary.get("finalizer_semantic_evidence_retention_avg"))],
        ["Finalizer disclosure honesty rate", _fmt(summary.get("finalizer_disclosure_honesty_rate"))],
        ["Finalizer informational completeness avg", _fmt(summary.get("finalizer_information_value_avg"))],
        ["Finalizer evidence retention avg", _fmt(summary.get("finalizer_evidence_retention_avg"))],
        ["Finalizer risk disclosure pass rate", _fmt(summary.get("finalizer_risk_disclosure_pass_rate"))],
        ["Finalizer illustrative structure rate", _fmt(summary.get("finalizer_illustrative_structure_rate"))],
        ["Downgraded answer usefulness floor avg", _fmt(summary.get("downgraded_answer_usefulness_floor_avg"))],
    ]
    contribution_rows = [
        [
            "Analyst",
            _fmt(summary.get("analyst_contribution_avg")),
            "Analyst Logic",
            _fmt(summary.get("analyst_logic_integrity_rate")),
            "First evidence-backed draft quality: does the Analyst avoid direction-changing logic mistakes while answering the requested slots?",
        ],
        [
            "Checker",
            _fmt(summary.get("checker_contribution_avg")),
            "Checker Fix Adoption",
            _fmt(summary.get("checker_fix_adoption_avg")),
            "QA correction adoption: do Checker findings actually land downstream and clean up the final behavior?",
        ],
        [
            "Critic",
            _fmt(summary.get("critic_contribution_avg")),
            "Non-actionable info delta (0 is best)",
            f"{_fmt(critic_delta)} ({critic_delta_label})",
            "Governance and compression control: this centered delta shows whether non-actionable answers preserved Analyst evidence or flattened it.",
        ],
        [
            "Finalizer",
            _fmt(summary.get("finalizer_contribution_avg")),
            "Finalizer Truth Integrity",
            _fmt(summary.get("finalizer_truth_integrity_rate")),
            "Final answer truthfulness and rendering integrity after all safety constraints and revisions.",
        ],
    ]
    case_rows = []
    for row in cases:
        case_rows.append(
            [
                row.get("case_name"),
                row["path_analysis"].get("revision_count"),
                f"{row['path_analysis'].get('finalizer_status')} | {row['finalizer'].get('mode')}",
                _fmt(row["analyst"].get("semantic_evidence_carry_score")),
                _fmt(row["checker"].get("fix_adoption_score")),
                _fmt(row["critic"].get("feedback_adoption_score")),
                _fmt(row["critic"].get("risk_disclosure_success")),
                _fmt(row["node_contribution"].get("analyst_to_final_logic_gain")),
                _fmt(row["node_contribution"].get("analyst_to_final_truth_gain")),
                _fmt(row["node_contribution"].get("analyst_to_final_information_gain")),
            ]
        )
    lines = [
        "# Agentic Node Evaluation",
        "",
        "## High Summary",
        _node_high_summary(summary),
    ]
    lines.extend([
        "",
        "## Summary",
        "",
        f"- Cases: {summary.get('n_cases')}",
        f"- Mean revision count: {_fmt(summary.get('mean_revision_count'))}",
        f"- Degraded case rate: {_fmt(summary.get('degraded_case_rate'))}",
        f"- Analyst -> Final truth gain avg: {_fmt(summary.get('analyst_to_final_truth_gain_avg'))}",
        f"- Analyst -> Final logic gain avg: {_fmt(summary.get('analyst_to_final_logic_gain_avg'))}",
        f"- Analyst -> Final information gain avg: {_fmt(summary.get('analyst_to_final_information_gain_avg'))}",
        f"- Analyst -> Final intent gain avg: {_fmt(summary.get('analyst_to_final_intent_gain_avg'))}",
        "",
        "## Metric Guide",
        "",
    ])
    for section_title, rows in _metric_guide_sections(summary):
        lines.extend([
            f"### {section_title}",
            "",
            _md_table(["Metric", "Value", "What it means"], rows),
            "",
        ])
    lines.extend([
        "",
        _md_table(["Metric", "Value"], summary_rows),
        "",
        "## Revision / Degraded Path",
        "",
        f"- Finalizer status counts: `{summary.get('finalizer_status_counts', {})}`",
        f"- Finalizer trigger counts: `{summary.get('finalizer_trigger_counts', {})}`",
        f"- Checker circuit-break count: `{summary.get('checker_circuit_break_count')}`",
        f"- Completed-after-critic count: `{summary.get('completed_after_critic_count')}`",
        "",
        "## Node Contribution Analytics",
        "",
        _md_table(["Node", "Contribution", "Key Metric", "Value", "What it means"], contribution_rows),
        "",
        "- `Contribution` = normalized composite node score for overall run performance. It is roughly on a `0-1` scale and higher is better.",
        "- `Value` = the raw headline metric shown for interpretability. These values do not all share the same scale and should not be compared across nodes as if all were `0-1` positive metrics.",
        f"- Critic uses a centered metric where `0` is best. Current run: `{_fmt(critic_delta)}` (`{critic_delta_label}`). Near `0` means little compression, more negative means stronger flattening, and more positive means downstream clarification.",
        "",
        "## Per-case Node Matrix",
        "",
        _md_table(
            ["Case", "Revisions", "Finalizer", "Analyst semantic evidence", "Checker adoption", "Critic adoption", "Critic risk", "Logic gain", "Truth gain", "Info gain"],
            case_rows,
        ),
        "",
        "- `Analyst` column = `semantic_evidence_carry_score`: `0.0` means almost no required semantic evidence survived in the draft, `0.5` means partial carry, and `1.0` means strong carry across the requested slots.",
        "- `Checker` column = `fix_adoption_score`: `0.0` means checker findings did not translate into cleaner final behavior, `0.5` means partial or mixed adoption, and `1.0` means clean adoption or no fix needed.",
        "- `Critic` column = `feedback_adoption_score`: `0.0` means critic governance feedback did not land downstream, `0.5` means partial or mixed influence, and `1.0` means downgrade / risk / governance feedback was adopted cleanly.",
        "- These three per-case node columns are all normalized `0-1` node-influence scores. They are not the same thing as the `Value` column in Node Contribution Analytics.",
        "",
        "## Downgrade Diagnostics",
        "",
        _md_table(
            ["Case", "Mode", "Risk required", "Risk retained", "Illustrative applicable", "Illustrative present"],
            [
                [
                    row.get("case_name"),
                    row["finalizer"].get("mode"),
                    row["finalizer"].get("risk_disclosure_required"),
                    row["finalizer"].get("risk_disclosure_pass"),
                    row["finalizer"].get("illustrative_applicable"),
                    row["finalizer"].get("illustrative_structure_present"),
                ]
                for row in cases
            ],
        ),
        "",
        "- `informational_only`: read-only answer. The workflow concluded that the evidence or governance contract does not support a live recommendation.",
        "- `directional_watchlist`: directional or regime read is supportable, but not a live trade recommendation. One clearly non-live illustrative structure hint is allowed when the contract permits it.",
        "- `actionable_options`: live options recommendation path. The system has enough options-layer support to justify an options-specific actionable answer, potentially including structure-level or contract-level guidance.",
        "",
        "- `Risk required`: the governance contract said the final answer must explicitly carry risk language.",
        "- `Risk retained`: that required risk language survived into the final answer.",
        "- `Illustrative applicable`: the finalizer contract allowed an illustrative non-live structure in this case.",
        "- `Illustrative present`: a non-live illustrative structure actually appeared in the final answer when it was allowed.",
        "- `informational_only` stays stricter: if `must_explain_why_not_now=True`, the answer must explain why-not-now, and any illustrative wording must remain clearly degraded and non-live.",
        "- `directional_watchlist` may legally include one illustrative structure hint, but only if it stays monitoring-oriented and does not cross into active trade wording.",
        "",
      ])
    lines.extend([
        "## Node Contribution Analytics Notes",
        "",
        "- `Contribution` = normalized composite node score for overall run performance. It is roughly on a `0-1` scale and higher is better.",
        "- `Value` = the raw headline metric shown for interpretability. These values do not all share the same scale and should not be compared across nodes as if they were all `0-1` positive metrics.",
        f"- Critic uses a centered metric: `Non-actionable info delta (0 is best)`. Current run: `{_fmt(critic_delta)}` ({critic_delta_label}). Near `0` means little compression, more negative means stronger flattening, and more positive means downstream clarification.",
        "",
    ])
    for row in cases:
        lines += [
            f"### {row.get('case_name')}",
            "",
            f"- Revision path: `{row['path_analysis'].get('revision_path')}`",
            f"- Missing sources: `{row.get('missing_sources')}`",
            f"- Missing slots: `{row.get('missing_query_slots')}`",
            f"- Analyst truthfulness issues: `{row['analyst'].get('truthfulness_issues', [])}`",
            f"- Checker verdict counts: `{row['checker'].get('verdict_counts', {})}`",
            f"- Critic categories: `{row['critic'].get('suggestion_categories', [])}`",
            f"- Finalizer status: `{row['finalizer'].get('status')}` | mode: `{row['finalizer'].get('mode')}`",
            f"- Highest failure severity: `{row['finalizer'].get('highest_failure_severity')}`",
            f"- Critical failures: `{row['finalizer'].get('critical_failures', [])}`",
            f"- Material failures: `{row['finalizer'].get('material_failures', [])}`",
            f"- Compliance failures: `{row['finalizer'].get('compliance_failures', [])}`",
            "",
        ]
    return "\n".join(lines)


def _dashboard_html(summary: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> str:
    critic_delta = summary.get("critic_non_actionable_information_retention_delta_avg")
    critic_delta_label = _critic_delta_label(critic_delta)
    cards = [
        ("Cases", summary.get("n_cases"), "Number of paired production cases in this node run."),
        ("Analyst Logic", _fmt(summary.get("analyst_logic_integrity_rate")), NODE_METRIC_DESCRIPTIONS["analyst_logic_integrity_rate"]),
        ("Checker Fix Adoption", _fmt(summary.get("checker_fix_adoption_avg")), NODE_METRIC_DESCRIPTIONS["checker_fix_adoption_avg"]),
        ("Critic Risk Disclosure", _fmt(summary.get("critic_risk_disclosure_success_rate")), NODE_METRIC_DESCRIPTIONS["critic_risk_disclosure_success_rate"]),
        ("Finalizer Truth Integrity", _fmt(summary.get("finalizer_truth_integrity_rate")), NODE_METRIC_DESCRIPTIONS["finalizer_truth_integrity_rate"]),
        ("Final Risk", _fmt(summary.get("finalizer_risk_disclosure_pass_rate")), NODE_METRIC_DESCRIPTIONS["finalizer_risk_disclosure_pass_rate"]),
        ("Truth Pass", _fmt(summary.get("finalizer_truthfulness_pass_rate")), "Final hard-pass rate after all node intervention."),
        ("Logic Delta", _fmt(summary.get("analyst_to_final_logic_gain_avg")), "Average change in logic integrity from the first Analyst draft to the final answer."),
        ("Truth Delta", _fmt(summary.get("analyst_to_final_truth_gain_avg")), "Average change in truth integrity from the first Analyst draft to the final answer."),
        ("Information Delta", _fmt(summary.get("analyst_to_final_information_gain_avg")), "Average change in information completeness from the Analyst draft to the final answer."),
    ]
    card_html = "".join(
        f"<div class='metric-card'><div class='label'>{html.escape(str(label))}</div><div class='value'>{html.escape(str(value))}</div><div class='desc'>{html.escape(str(desc))}</div></div>"
        for label, value, desc in cards
    )
    node_table = _html_table(
        ["Node", "Contribution", "Key Metric", "Value", "What it means"],
        [
            [
                "Analyst",
                _bar(summary.get("analyst_contribution_avg")),
                "Analyst Logic",
                _fmt(summary.get("analyst_logic_integrity_rate")),
                "First evidence-backed draft quality: does the Analyst avoid direction-changing logic mistakes while answering the requested slots?",
            ],
            [
                "Checker",
                _bar(summary.get("checker_contribution_avg")),
                "Checker Fix Adoption",
                _fmt(summary.get("checker_fix_adoption_avg")),
                "QA correction adoption: do Checker findings actually land downstream and clean up the final behavior?",
            ],
            [
                "Critic",
                _bar(summary.get("critic_contribution_avg")),
                "Non-actionable info delta (0 is best)",
                f"{html.escape(_fmt(critic_delta))}<br><span class='muted'>{html.escape(critic_delta_label)}</span>",
                "Governance and compression control: this centered delta shows whether non-actionable answers preserved Analyst evidence or flattened it.",
            ],
            [
                "Finalizer",
                _bar(summary.get("finalizer_contribution_avg")),
                "Finalizer Truth Integrity",
                _fmt(summary.get("finalizer_truth_integrity_rate")),
                "Final answer truthfulness and rendering integrity after all safety constraints and revisions.",
            ],
        ],
    )
    path_table = _html_table(
        ["Metric", "Value"],
        [
            ["Node run counts", html.escape(json.dumps(summary.get("node_run_counts", {}), ensure_ascii=False))],
            ["Checker verdict counts", html.escape(json.dumps(summary.get("checker_verdict_counts", {}), ensure_ascii=False))],
            ["Critic verdict counts", html.escape(json.dumps(summary.get("critic_verdict_counts", {}), ensure_ascii=False))],
            ["Finalizer status counts", html.escape(json.dumps(summary.get("finalizer_status_counts", {}), ensure_ascii=False))],
            ["Finalizer trigger counts", html.escape(json.dumps(summary.get("finalizer_trigger_counts", {}), ensure_ascii=False))],
            ["Checker circuit-break count", summary.get("checker_circuit_break_count")],
            ["Completed-after-critic count", summary.get("completed_after_critic_count")],
        ],
    )
    case_rows: List[List[Any]] = []
    for row in cases:
        case_rows.append(
            [
                html.escape(str(row.get("case_name"))),
                html.escape(str(row["path_analysis"].get("revision_count"))),
                html.escape(f"{row['path_analysis'].get('finalizer_status')} | {row['finalizer'].get('mode')}"),
                _bar(row["analyst"].get("semantic_evidence_carry_score")),
                _bar(row["checker"].get("fix_adoption_score")),
                _bar(row["critic"].get("feedback_adoption_score")),
                html.escape(str(row["path_analysis"].get("revision_path"))),
            ]
        )
    per_case_table = _html_table(
        ["Case", "Revisions", "Finalizer", "Analyst", "Checker", "Critic", "Revision Path"],
        case_rows,
    )
    downgrade_table = _html_table(
        ["Case", "Structured mode", "Risk required", "Risk retained", "Illustrative applicable", "Illustrative present"],
        [
            [
                html.escape(str(row.get("case_name"))),
                html.escape(str(row["finalizer"].get("mode"))),
                html.escape(str(row["finalizer"].get("risk_disclosure_required"))),
                html.escape(str(row["finalizer"].get("risk_disclosure_pass"))),
                html.escape(str(row["finalizer"].get("illustrative_applicable"))),
                html.escape(str(row["finalizer"].get("illustrative_structure_present"))),
            ]
            for row in cases
        ],
    )
    metric_guide = "".join(
        f"<div class='metric-section'><h3>{html.escape(section_title)}</h3>{_html_table(['Metric', 'Value', 'What it means'], rows)}</div>"
        for section_title, rows in _metric_guide_sections(summary)
    )
    radar_html = "".join(
        [
            _svg_radar(
                "Analyst Common Dimensions",
                ["Logic", "Truthfulness", "Slot Coverage", "Semantic Evidence"],
                [
                    summary.get("analyst_logic_integrity_rate"),
                    summary.get("analyst_truth_integrity_rate"),
                    summary.get("analyst_slot_answer_rate"),
                    summary.get("analyst_semantic_evidence_carry_avg"),
                ],
                color="#2563eb",
            ),
            _svg_radar(
                "Finalizer Common Dimensions",
                ["Logic", "Truthfulness", "Slot Coverage", "Semantic Evidence"],
                [
                    summary.get("finalizer_logic_integrity_rate"),
                    summary.get("finalizer_truth_integrity_rate"),
                    summary.get("finalizer_slot_answer_rate"),
                    summary.get("finalizer_semantic_evidence_retention_avg"),
                ],
                color="#16a34a",
            ),
        ]
    )
    per_case_note = """
    <div class="note" style="margin-top:12px">
      <strong>Finalizer</strong> combines workflow status and answer mode.
      <ul class="tight">
        <li><strong>complete | informational_only</strong>: the workflow finished and the final answer was intentionally kept read-only and non-actionable.</li>
        <li><strong>complete | directional_watchlist</strong>: the workflow finished with a directional or regime read, but still without a concrete live trade recommendation.</li>
        <li><strong>complete | actionable_options</strong>: the workflow finished with an options-specific actionable path, meaning the evidence and contract supported an options recommendation at structure or contract granularity.</li>
        <li><strong>degraded | ...</strong>: the workflow exited through a degraded fallback path rather than a normal complete render.</li>
      </ul>
      <p><strong>Per-case node columns</strong> are normalized node-influence scores, not raw metric values.</p>
      <ul class="tight">
        <li><strong>Analyst</strong> = <code>semantic_evidence_carry_score</code>: <code>0.0</code> means almost no required semantic evidence survived in the draft, <code>0.5</code> means partial carry, and <code>1.0</code> means strong carry across the requested slots.</li>
        <li><strong>Checker</strong> = <code>fix_adoption_score</code>: <code>0.0</code> means checker findings did not translate into cleaner final behavior, <code>0.5</code> means partial or mixed adoption, and <code>1.0</code> means clean adoption or no fix needed.</li>
        <li><strong>Critic</strong> = <code>feedback_adoption_score</code>: <code>0.0</code> means critic governance feedback did not land downstream, <code>0.5</code> means partial or mixed influence, and <code>1.0</code> means downgrade / risk / governance feedback was adopted cleanly.</li>
      </ul>
      <p>These bars are all <code>0-1</code> normalized node-influence scores. They are not the same thing as the <strong>Value</strong> column in Node Contribution Analytics.</p>
    </div>
    """
    contribution_note = f"""
    <div class="note" style="margin-top:12px">
      <p><strong>Contribution</strong> = normalized composite node score for overall run performance. It is roughly on a <code>0-1</code> scale and higher is better.</p>
      <p><strong>Value</strong> = the raw headline metric shown for interpretability. These values do not all share the same scale and should not be compared across nodes as if all were <code>0-1</code> positive metrics.</p>
      <p><strong>Critic special case</strong>: <code>Non-actionable info delta (0 is best)</code> is a centered metric. Current run: <code>{html.escape(_fmt(critic_delta))}</code> ({html.escape(critic_delta_label)}). Near <code>0</code> means little compression, more negative means stronger flattening, and more positive means downstream clarification.</p>
    </div>
    """
    downgrade_note = """
    <div class="note" style="margin-top:12px">
      <p><strong>Structured mode</strong> is the final actionability mode chosen by the workflow:</p>
      <ul class="tight">
        <li><strong>informational_only</strong>: use this when the answer should stay read-only because the system lacks enough evidence or the governance layer forbids a live recommendation. When <code>must_explain_why_not_now=True</code>, the answer must explicitly explain why-not-now.</li>
        <li><strong>directional_watchlist</strong>: use this when a directional or regime view is supportable, but the evidence is still not strong enough for a concrete options trade. One clearly non-live illustrative structure hint is allowed when the contract permits it.</li>
        <li><strong>actionable_options</strong>: use this when the evidence and contract support a live options recommendation path with enough options-layer backing for structure-level or contract-level guidance.</li>
      </ul>
      <ul class="tight">
        <li><strong>Risk required</strong>: whether the governance contract said the final answer must explicitly carry risk language.</li>
        <li><strong>Risk retained</strong>: whether that required risk language actually survived into the final answer.</li>
        <li><strong>Illustrative applicable</strong>: whether the finalizer contract allowed an illustrative structure in this case.</li>
        <li><strong>Illustrative present</strong>: whether a legal non-live illustrative structure actually appeared in the final answer when it was allowed.</li>
        <li><strong>Compliance interpretation</strong>: <code>informational_only</code> remains stricter; <code>directional_watchlist</code> may include a monitor-only structure hint, but it fails if that hint crosses into active trade wording.</li>
      </ul>
    </div>
    """
    return f"""<!doctype html>
  <html lang="en">
  <head>
    <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agentic Node Dashboard</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <h1>Agentic Node Dashboard</h1>
      <div class="muted">{html.escape(_node_high_summary(summary))}</div>
      <div class="grid">{card_html}</div>
    </section>
    <section>
      <h2>Revision / Degraded Path Analysis</h2>
      <div class="panel">{path_table}</div>
    </section>
    <section>
      <h2>Metric Guide</h2>
      <div class="panel">{metric_guide}</div>
    </section>
    <section>
      <h2>Node Contribution Analytics</h2>
      <div class="panel">{node_table}</div>
      {contribution_note}
    </section>
      <section>
        <h2>Common-Dimension Radar</h2>
        <div class="chart-wrap">{radar_html}</div>
      </section>
      <section>
        <h2>Per-case Node Matrix</h2>
        <div class="panel">{per_case_table}</div>
        {per_case_note}
      </section>
      <section>
        <h2>Downgrade Diagnostics</h2>
        <div class="panel">{downgrade_table}</div>
        {downgrade_note}
      </section>
      <section>
        <h2>Summary JSON</h2>
        <pre>{html.escape(json.dumps(summary, indent=2, ensure_ascii=False))}</pre>
    </section>
  </main>
</body>
</html>"""


def _load_cases(run_dir: Path, truth_file: Path) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    production_path = run_dir / "production_results_full.jsonl"
    if not production_path.exists():
        raise FileNotFoundError(f"production_results_full.jsonl not found in {run_dir}")
    production_rows = _read_jsonl(production_path)
    truth_payload = _read_json(truth_file)
    truth_map = _truth_map(truth_payload)
    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for row in production_rows:
        query = str(row.get("query") or "")
        case = truth_map.get(query)
        if not case:
            logger.warning("Skipping unpaired node-eval row for query: %s", query)
            continue
        pairs.append((case, row))
    return pairs


def run_node_eval(
    *,
    run_dir: Path,
    truth_file: Path = DEFAULT_TRUTH_FILE,
    out_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    pairs = _load_cases(run_dir, truth_file)
    target_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y-%m-%d") / datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir.mkdir(parents=True, exist_ok=True)
    case_rows: List[Dict[str, Any]] = []
    event_rows: List[Dict[str, Any]] = []
    audit_rows: List[Dict[str, Any]] = []
    for case, row in pairs:
        evaluated = _evaluate_case(case, row)
        case_rows.append(evaluated)
        event_rows.extend(_event_rows(row))
        audit_rows.append(
            {
                "step": "agentic_node_case",
                "ts": datetime.now().isoformat(),
                "case_id": evaluated.get("case_id"),
                "case_name": evaluated.get("case_name"),
                "query": evaluated.get("query"),
                "path_analysis": evaluated.get("path_analysis"),
                "analyst": evaluated.get("analyst"),
                "checker": evaluated.get("checker"),
                "critic": evaluated.get("critic"),
                "finalizer": evaluated.get("finalizer"),
                "node_contribution": evaluated.get("node_contribution"),
            }
        )

    summary = _summarize(case_rows, event_rows, run_dir)
    metrics_payload = {"summary": summary, "per_case": case_rows}

    audit_path = target_dir / "agentic_node_audit.jsonl"
    events_path = target_dir / "agentic_node_events.jsonl"
    detailed_csv_path = target_dir / "agentic_node_eval_detailed.csv"
    events_csv_path = target_dir / "agentic_node_events.csv"
    metrics_path = target_dir / "agentic_node_metrics.json"
    report_path = target_dir / "agentic_node_report.md"
    dashboard_path = target_dir / "agentic_node_dashboard.html"

    _write_jsonl(audit_path, audit_rows)
    _write_jsonl(events_path, event_rows)
    _write_csv(detailed_csv_path, case_rows)
    _write_csv(events_csv_path, event_rows)
    metrics_path.write_text(json.dumps(metrics_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(_markdown_report(summary, case_rows), encoding="utf-8")
    dashboard_path.write_text(_dashboard_html(summary, case_rows), encoding="utf-8")
    return {
        "audit": audit_path,
        "events": events_path,
        "detailed_csv": detailed_csv_path,
        "events_csv": events_csv_path,
        "metrics": metrics_path,
        "report": report_path,
        "dashboard": dashboard_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Node-centric workflow evaluation for the financial RAG stack.")
    parser.add_argument("--run-dir", type=str, default=None, help="RAGAS run directory to evaluate. Defaults to latest.")
    parser.add_argument("--truth-file", type=str, default=str(DEFAULT_TRUTH_FILE), help="Structured truth JSON file.")
    parser.add_argument("--out-dir", type=str, default=None, help="Explicit output directory. Defaults to logs/agentic_eval/YYYY-MM-DD/<timestamp>.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    run_dir = Path(args.run_dir) if args.run_dir else _latest_ragas_run()
    truth_file = Path(args.truth_file)
    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        day = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = DEFAULT_OUTPUT_ROOT / day / ts
    outputs = run_node_eval(run_dir=run_dir, truth_file=truth_file, out_dir=out_dir)

    print("Agentic node evaluation generated")
    print(f"  metrics:   {outputs['metrics']}")
    print(f"  report:    {outputs['report']}")
    print(f"  dashboard: {outputs['dashboard']}")


if __name__ == "__main__":
    main()
