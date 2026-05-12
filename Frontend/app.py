from __future__ import annotations

import asyncio
from html import escape
from datetime import datetime
from typing import Any, Dict

import streamlit as st

from Frontend.audit import append_frontend_query_audit, get_effective_run_id, write_frontend_query_bundle
from Frontend.contracts import normalize_state, sanitize_text
from Frontend.pipeline import run_router_nodes_stream
from Frontend.renderers import (
    render_footer_disclaimer,
    render_macro_lagging_info_bar,
    render_final_dashboard,
    render_phase1_silver,
    render_phase2_gold,
    render_query_guide_buttons,
    render_sidebar_context,
)
from Frontend.styles import inject_theme


def _translate_recommendation_mode(mode: Any) -> str:
    mapping = {
        "actionable_options": "structure-supported",
        "directional_watchlist": "directional watchlist",
        "informational_only": "context only",
    }
    key = str(mode or "").strip()
    return mapping.get(key, key.replace("_", " ").strip() or "pending")


def _mode_phrase(final_state: Dict[str, Any]) -> str:
    metadata = (normalize_state(final_state).get("metadata") or {})
    source_types = [str(x).lower() for x in (metadata.get("source_types") or [])]
    if any(x in {"news", "sec", "gpr"} for x in source_types):
        return "news, event, and macro context"
    return "structured market signals"


def _structured_context_phrase(final_state: Dict[str, Any]) -> str:
    normalized = normalize_state(final_state)
    metadata = normalized.get("metadata") or {}
    source_types = {str(x).lower() for x in (metadata.get("source_types") or [])}
    form_type = str(metadata.get("form_type") or "").strip().upper()
    requested_sec_forms = {str(x or "").strip().upper() for x in (metadata.get("requested_sec_forms") or []) if str(x or "").strip()}
    metrics = [sanitize_text(x) for x in (metadata.get("metrics") or []) if sanitize_text(x)]

    if "sec" in source_types:
        if requested_sec_forms == {"8-K", "4"}:
            base = "SEC 8-K filing evidence and Form 4 insider flow"
        elif form_type == "8-K":
            base = "SEC 8-K filing evidence"
        elif form_type == "4":
            base = "Form 4 insider flow"
        else:
            base = "SEC filing evidence"
        if "options" in source_types:
            return f"{base} and options posture"
        if "news" in source_types:
            return f"{base} and supporting news context"
        return base

    if "news" in source_types and "gpr" in source_types:
        return "geopolitics news and GPR context"
    if "gpr" in source_types:
        return "GPR context"
    if "news" in source_types:
        return "news narrative"
    if "macro_history" in source_types:
        return "macro history and cross-asset context"
    if metrics:
        return ", ".join(metrics[:3])
    return _mode_phrase(final_state)


def _structured_goal_clause(final_state: Dict[str, Any]) -> str:
    normalized = normalize_state(final_state)
    metadata = normalized.get("metadata") or {}
    scope_contract = normalized.get("scope_contract") or {}
    query_family = sanitize_text(scope_contract.get("query_family") or "").lower()
    source_types = {sanitize_text(x).lower() for x in (metadata.get("source_types") or []) if sanitize_text(x)}
    form_type = str(metadata.get("form_type") or "").strip().upper()
    requested_sec_forms = {str(x or "").strip().upper() for x in (metadata.get("requested_sec_forms") or []) if str(x or "").strip()}
    event_keyword = sanitize_text(metadata.get("event_keyword") or "").lower().replace(" ", "_")
    if requested_sec_forms == {"8-K", "4"}:
        return "about filing-driven event risk and insider flow"
    if form_type == "8-K":
        return "about filing-driven event risk"
    if form_type == "4" or requested_sec_forms == {"4"}:
        return "about insider flow"
    if event_keyword == "event_risk":
        return "about event risk"
    if query_family.startswith("geopolitical_") or "gpr" in source_types:
        return "about geopolitics risk"
    if query_family == "cross_asset_regime" or "macro_history" in source_types:
        return "about the current regime"
    if query_family == "options_microstructure" or "options" in source_types:
        return "about options posture"
    return ""


def _build_rephrased_question(final_state: Dict[str, Any]) -> str:
    normalized = normalize_state(final_state)
    metadata = normalized.get("metadata") or {}
    tickers = [str(x).upper() for x in (metadata.get("tickers") or []) if str(x).strip()]
    window = sanitize_text(metadata.get("time_window") or "recent window")
    asset_phrase = ", ".join(tickers[:2]) if tickers else "the tracked market set"
    context_phrase = _structured_context_phrase(final_state)
    goal_clause = _structured_goal_clause(final_state)
    suffix = f" {goal_clause}" if goal_clause else ""
    return f"What does {window.lower()} {context_phrase} in {asset_phrase} suggest{suffix}?"


def _clean_quick_take(reply: str) -> str:
    text = sanitize_text(reply)
    if not text:
        return ""
    if text.startswith("For "):
        query_end = text.find("?")
        if 0 <= query_end <= 220:
            remainder = text[query_end + 1 :].lstrip(" ,")
            if remainder:
                text = remainder
    return text.strip()


def _safe_download_button(
    label: str,
    *,
    data: str,
    file_name: str,
    mime: str = "text/markdown",
    key: str,
) -> None:
    """Render a download button without letting the click destroy the result view."""
    common = {
        "data": data,
        "file_name": file_name,
        "mime": mime,
        "use_container_width": True,
        "key": key,
    }
    try:
        st.download_button(label, on_click="ignore", **common)
    except Exception:
        st.download_button(label, **common)


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _assistant_brief(final_state: Dict[str, Any]) -> str:
    normalized = normalize_state(final_state)
    final_report = normalized.get("final_report") or {}
    reply = sanitize_text(final_report.get("conversation_reply") or "")
    reframed = _build_rephrased_question(final_state)
    quick_take = _clean_quick_take(reply)
    if reply:
        return f"### Reframed Question\n{reframed}\n\n### Quick Take\n{quick_take or reply}"
    return f"### Reframed Question\n{reframed}\n\n### Quick Take\nNo summary available."


def _render_product_header() -> None:
    st.markdown(
        """
<header class="app-header">
  <div>
    <div class="app-kicker">AutoOptions</div>
    <h1>Financial Intelligence Workspace</h1>
    <p>
      Built for non-experts who need a rigorous market read without forcing a trade.
      The system cross-checks options microstructure, filings, macro context, and execution reality before it allows conviction.
    </p>
  </div>
</header>
        """,
        unsafe_allow_html=True,
    )


def _render_stream_lines(lines: list[str], slot) -> None:
    rendered = []
    for item in lines[-16:]:
        rendered.append(f'<div class="stream-step">{escape(item)}</div>')
    slot.markdown('<div class="stream-panel">' + "".join(rendered) + "</div>", unsafe_allow_html=True)


def _render_summary_highlight(final_state: Dict[str, Any], assistant_msg: str) -> None:
    normalized = normalize_state(final_state)
    final_strategy = normalized.get("final_strategy") or {}
    final_report = normalized.get("final_report") or {}
    status = str(final_strategy.get("status") or "review").replace("_", " ").title()
    confidence = final_strategy.get("confidence_score") or final_report.get("confidence_score")
    confidence_text = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else str(confidence or "N/A")
    reframed = _build_rephrased_question(final_state)
    quick_take = _clean_quick_take(
        sanitize_text(final_report.get("conversation_reply") or "")
    ) or "No summary available."
    status_html = escape(sanitize_text(status))
    confidence_html = escape(sanitize_text(confidence_text))
    reframed_html = escape(sanitize_text(reframed))
    quick_take_html = escape(quick_take).replace("\n", "<br/>")

    st.markdown(
        f"""
<section class="summary-highlight">
  <div class="summary-question">
    <div class="summary-question-label">Reframed Question</div>
    <div class="summary-question-text">{reframed_html}</div>
  </div>
  <div class="summary-highlight-meta">
    <span>Quick Take</span>
    <span>Status: {status_html}</span>
    <span>Confidence: {confidence_html}</span>
  </div>
  <div class="summary-highlight-body">{quick_take_html}</div>
</section>
        """,
        unsafe_allow_html=True,
    )


def _render_audit_diagnostics(final_state: Dict[str, Any], run_id: str, bundle_paths: Dict[str, str] | None = None) -> None:
    bundle_paths = bundle_paths or {}
    translated_mode = _translate_recommendation_mode(
        final_state.get("recommendation_mode") or final_state.get("actionability_mode")
    )
    st.markdown("#### Routing and Revision Snapshot")
    col_rev, col_check, col_crit, col_mode = st.columns(4)
    col_rev.metric("Revision Count", str(final_state.get("revision_count", 0)))
    col_check.metric("Checker Verdict", str(final_state.get("checker_verdict", "N/A")))
    col_crit.metric("Critic Verdict", str(final_state.get("critic_verdict", "N/A")))
    col_mode.metric("Final Answer Boundary", translated_mode.title())
    st.caption(f"Run ID: `{run_id}`")
    st.caption("Builder signals and canonical goal control query framing. Final answer boundary is set later by deterministic financial guardrails.")
    st.markdown(
        """
Canonical builder goals map to backend families: options posture, SEC filing risk, geopolitics narrative, and macro regime.
Finalizer boundaries map as: `actionable_options` -> structure-supported, `directional_watchlist` -> directional watchlist, `informational_only` -> context only.
        """
    )

    st.markdown("#### Node Audit Log")
    audit_rows = []
    for row in final_state.get("node_audit_log") or []:
        latency_raw = row.get("latency_ms")
        try:
            latency_ms = float(latency_raw) if latency_raw is not None else 0.0
        except Exception:
            latency_ms = 0.0
        audit_rows.append(
            {
                "node": str(row.get("node", "")),
                "latency_ms": latency_ms,
                "verdict": str(row.get("verdict", "")),
                "findings_n": str(row.get("findings_n", "")),
                "t_start": str(row.get("t_start", "")),
            }
        )
    if not audit_rows:
        st.info("No audit log rows found.")
        return

    known_order = ["retrieval_master", "analyst", "checker", "critic", "finalizer"]
    discovered_nodes = sorted({r["node"] for r in audit_rows if r.get("node")})
    default_nodes = [n for n in known_order if n in discovered_nodes] or discovered_nodes
    selected_nodes = st.multiselect(
        "Filter by node",
        options=discovered_nodes,
        default=default_nodes,
        key="audit_node_filter",
    )
    sort_mode = st.selectbox(
        "Sort by",
        options=["Latency (desc)", "Latency (asc)", "Start time (asc)", "Start time (desc)"],
        index=0,
        key="audit_sort_mode",
    )
    filtered = [r for r in audit_rows if (not selected_nodes or r["node"] in selected_nodes)]
    if sort_mode == "Latency (desc)":
        filtered = sorted(filtered, key=lambda r: r["latency_ms"], reverse=True)
    elif sort_mode == "Latency (asc)":
        filtered = sorted(filtered, key=lambda r: r["latency_ms"])
    elif sort_mode == "Start time (desc)":
        filtered = sorted(filtered, key=lambda r: r["t_start"], reverse=True)
    else:
        filtered = sorted(filtered, key=lambda r: r["t_start"])

    display_rows = [
        {
            "node": r["node"],
            "latency_ms": f"{r['latency_ms']:.1f}",
            "verdict": r["verdict"],
            "findings_n": r["findings_n"],
            "t_start": r["t_start"],
        }
        for r in filtered
    ]
    st.dataframe(display_rows, width="stretch", hide_index=True)
    with st.expander("Raw audit fields", expanded=False):
        st.json(
            {
                "run_id": run_id,
                "time_range": final_state.get("time_range"),
                "source_predicates": (final_state.get("time_range") or {}).get("source_predicates"),
                "node_audit_log": final_state.get("node_audit_log"),
                "trace_path": bundle_paths.get("trace"),
                "final_state_path": bundle_paths.get("final_state"),
                "summary_path": bundle_paths.get("summary"),
            },
            expanded=False,
        )


def _render_result_workspace(
    final_state: Dict[str, Any],
    *,
    run_id: str,
    bundle_paths: Dict[str, str] | None = None,
    assistant_msg: str | None = None,
) -> None:
    final_state = normalize_state(final_state)
    bundle_paths = bundle_paths or {}
    assistant_msg = assistant_msg or _assistant_brief(final_state)

    tab_summary, tab_report, tab_context = st.tabs(["Quick Take", "Deep Dive", "Evidence"])

    with tab_summary:
        _render_summary_highlight(final_state, assistant_msg)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Rerun This Analysis", use_container_width=True, key="btn_rerun_pipeline"):
                st.session_state["pending_prompt"] = st.session_state.get("latest_query", "")
                st.session_state["pending_builder_contract"] = st.session_state.get("latest_builder_contract")
                st.rerun()
        with c2:
            _safe_download_button(
                "Download Quick Take",
                data=assistant_msg,
                file_name="quick_take.md",
                key="download_quick_take",
            )

    with tab_report:
        render_final_dashboard(final_state)
        final_strategy = final_state.get("final_strategy") or {}
        report_md = sanitize_text(final_strategy.get("markdown") or "")
        _safe_download_button(
            "Download Markdown Report",
            data=report_md or "No report markdown available.",
            file_name="Market_Read_Report.md",
            key="download_markdown_report",
        )

    with tab_context:
        context_mode = st.radio(
            "Evidence view",
            ["Structured Signals", "News & Events"],
            horizontal=True,
            key="evidence_context_mode",
        )
        if context_mode == "Structured Signals":
            render_phase1_silver(final_state, st.container())
        else:
            render_phase2_gold(final_state, st.container())

    with st.expander("Engineering audit & routing", expanded=False):
        st.caption("Diagnostic routing details separated from the investor-facing answer.")
        _render_audit_diagnostics(final_state, run_id, bundle_paths)


def main() -> None:
    # Step 1: Setup and layout
    st.set_page_config(page_title="AutoOptions Financial Intelligence Workspace", layout="wide")
    inject_theme()
    _render_product_header()
    render_macro_lagging_info_bar()
    render_sidebar_context()

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = [
            {
                "role": "assistant",
                "content": (
                    "I lower the barrier for non-experts by turning options, filings, and macro evidence into a disciplined market read. "
                    "Deterministic guardrails cross-check microstructure, executive behavior, and event evidence, so if the case is weak I will refuse to force a trade."
                ),
            }
        ]
    if "latest_state" not in st.session_state:
        st.session_state["latest_state"] = {}
    if "latest_bundle_paths" not in st.session_state:
        st.session_state["latest_bundle_paths"] = {}
    if "latest_run_id" not in st.session_state:
        st.session_state["latest_run_id"] = ""
    if "latest_assistant_msg" not in st.session_state:
        st.session_state["latest_assistant_msg"] = ""
    if "latest_builder_contract" not in st.session_state:
        st.session_state["latest_builder_contract"] = None

    guide_payload = render_query_guide_buttons(st.session_state.get("query", ""))

    for msg in st.session_state["chat_history"]:
        avatar = ":material/insights:" if msg["role"] == "assistant" else ":material/account_circle:"
        with st.chat_message(msg["role"], avatar=avatar):
            st.markdown(msg["content"])

    typed_prompt = st.chat_input("Type your query...", key="chat_input_main")
    pending_prompt = st.session_state.pop("pending_prompt", "")
    pending_builder_contract = st.session_state.pop("pending_builder_contract", None)
    selected_payload = guide_payload if isinstance(guide_payload, dict) else None
    prompt = typed_prompt or (selected_payload or {}).get("query") or pending_prompt
    query_builder_contract = None if typed_prompt else (
        (selected_payload or {}).get("builder_contract")
        if selected_payload is not None
        else pending_builder_contract
    )
    if not prompt:
        if st.session_state.get("latest_state"):
            _render_result_workspace(
                st.session_state["latest_state"],
                run_id=st.session_state.get("latest_run_id") or get_effective_run_id(),
                bundle_paths=st.session_state.get("latest_bundle_paths") or {},
                assistant_msg=st.session_state.get("latest_assistant_msg") or None,
            )
            render_footer_disclaimer()
        return

    st.session_state["chat_history"].append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar=":material/account_circle:"):
        st.markdown(prompt)

    st.session_state["query"] = prompt
    with st.chat_message("assistant", avatar=":material/insights:"):
        run_id = get_effective_run_id()
        final_state: Dict[str, Any] = {}
        trace_events: list[Dict[str, Any]] = []

        # Step 2: streaming status indicator
        with st.status("Launching the analysis workflow...", expanded=True) as phase3_status:
            stream_slot = st.empty()
            stream_lines: list[str] = []

            def _push_stream_line(node_name: str) -> None:
                ts = datetime.utcnow().strftime("%H:%M:%S")
                pretty = node_name.replace("_", " ").title()
                stream_lines.append(f"{ts} | {pretty}")
                _render_stream_lines(stream_lines, stream_slot)

            def _push_stream_detail(message: str) -> None:
                ts = datetime.utcnow().strftime("%H:%M:%S")
                stream_lines.append(f"{ts} | {message}")
                _render_stream_lines(stream_lines, stream_slot)

            def _node_end_summary(node_name: str, state: Dict[str, Any]) -> str:
                if node_name == "analyst":
                    iv_regime = (state.get("iv_regime_pinned") or {}).get("iv_regime", "pending")
                    return f"Analyst draft complete: {len(str(state.get('draft_report') or ''))} chars, IV regime={iv_regime}."
                if node_name == "checker":
                    return f"Checker audit complete: verdict={state.get('checker_verdict') or 'pending'}, feedback={len(state.get('critic_feedback') or [])}."
                if node_name == "critic":
                    translated_mode = _translate_recommendation_mode(
                        state.get("recommendation_mode") or state.get("actionability_mode")
                    )
                    return (
                        "Critic review complete: "
                        f"verdict={state.get('critic_verdict') or 'pending'}, "
                        f"answer boundary={translated_mode}."
                    )
                if node_name == "finalizer":
                    final_strategy = state.get("final_strategy") or {}
                    final_report = final_strategy.get("final_report") or {}
                    return f"Finalizer complete: report fields={len(final_report)}, status={final_strategy.get('status') or 'ready'}."
                return f"{node_name.replace('_', ' ').title()} complete."

            phase3_status.update(label="Stage 1: Gathering market, macro, and event evidence...", state="running")

            async def _consume_stream() -> Dict[str, Any]:
                latest: Dict[str, Any] = {}
                async for event in run_router_nodes_stream(prompt, query_builder_contract=query_builder_contract):
                    trace_events.append(event)
                    latest = normalize_state(event.get("state") or latest)
                    event_type = event.get("event")
                    node_name = str(event.get("node", "")).strip()
                    if event_type == "stage_detail":
                        _push_stream_detail(str(event.get("message") or "Retrieval progress updated."))
                        continue
                    if event_type == "node_end" and node_name:
                        _push_stream_detail(_node_end_summary(node_name, event.get("state") or {}))
                        continue
                    if event_type == "route_decision":
                        _push_stream_detail(f"Routing decision after {node_name}: next node = {event.get('route')}.")
                        continue
                    if event_type == "node_start" and node_name:
                        _push_stream_line(node_name)
                        if node_name in {"analyst", "checker", "critic"}:
                            phase3_status.update(label="Stage 2: Reasoning across the evidence set...", state="running")
                        if node_name == "finalizer":
                            phase3_status.update(label="Stage 3: Finalizing the report and answer...", state="running")
                return latest

            final_state = _run_async(_consume_stream())

            phase3_status.update(
                label="Analysis complete",
                state="complete" if final_state else "error",
                expanded=True,
            )

        if not final_state:
            bundle_paths = write_frontend_query_bundle(
                query=prompt,
                final_state={},
                trace_events=trace_events,
            )
            st.error("No final state available.")
            append_frontend_query_audit(
                {
                    "run_id": run_id,
                    "query": prompt,
                    "status": "error",
                    "error": "No final state available",
                    "trace_path": bundle_paths["trace"],
                    "final_state_path": bundle_paths["final_state"],
                    "summary_path": bundle_paths["summary"],
                }
            )
            return

        final_state = normalize_state(final_state)
        bundle_paths = write_frontend_query_bundle(
            query=prompt,
            final_state=final_state,
            trace_events=trace_events,
        )

        assistant_msg = _assistant_brief(final_state)
        _render_result_workspace(
            final_state,
            run_id=run_id,
            bundle_paths=bundle_paths,
            assistant_msg=assistant_msg,
        )
        render_footer_disclaimer()
        st.session_state["latest_state"] = final_state
        st.session_state["latest_query"] = prompt
        st.session_state["latest_builder_contract"] = query_builder_contract
        st.session_state["latest_run_id"] = run_id
        st.session_state["latest_bundle_paths"] = bundle_paths
        st.session_state["latest_assistant_msg"] = assistant_msg
        st.session_state["chat_history"].append({"role": "assistant", "content": assistant_msg})
        append_frontend_query_audit(
            {
                "run_id": run_id,
                "query": prompt,
                "status": "success",
                "final_status": (final_state.get("final_strategy") or {}).get("status"),
                "confidence_score": (final_state.get("final_report") or {}).get("confidence_score")
                or (final_state.get("final_strategy") or {}).get("confidence_score"),
                "node_count": len(final_state.get("node_audit_log") or []),
                "event_date_utc": datetime.utcnow().date().isoformat(),
                "trace_path": bundle_paths["trace"],
                "final_state_path": bundle_paths["final_state"],
                "summary_path": bundle_paths["summary"],
            }
        )


if __name__ == "__main__":
    main()
