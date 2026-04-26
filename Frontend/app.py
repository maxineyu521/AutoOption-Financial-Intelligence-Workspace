from __future__ import annotations

import asyncio
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
    if reply:
        return f"### Quick Take\n{reply}"
    return "### Quick Take\nNo summary available."


def main() -> None:
    # Step 1: Setup and layout
    st.set_page_config(page_title="Options Bot Frontend", layout="wide")
    inject_theme()
    st.title("Automated Options Recommendation Bot")
    render_macro_lagging_info_bar()
    render_sidebar_context()

    st.subheader("Main Canvas - Chatbot Analysis")
    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = [
            {"role": "assistant", "content": "Our objective is to democratize institutional-grade options trading by bridging the gap between quantitative market data and qualitative semantic insights (e.g., GLD/SLV vs. correlated equities). Ask an options/macro question to start analysis."}
        ]
    if "latest_state" not in st.session_state:
        st.session_state["latest_state"] = {}

    for msg in st.session_state["chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    guide_query = render_query_guide_buttons(st.session_state.get("query", ""))
    prompt = st.chat_input("Type your query...", key="chat_input_main")
    prompt = prompt or guide_query
    if not prompt:
        if st.session_state.get("latest_state"):
            render_final_dashboard(st.session_state["latest_state"])
        return

    st.session_state["chat_history"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    st.session_state["query"] = prompt
    with st.chat_message("assistant"):
        run_id = get_effective_run_id()
        final_state: Dict[str, Any] = {}
        trace_events: list[Dict[str, Any]] = []

        # Step 2: streaming status indicator
        with st.status("Launching financial analysis pipeline...", expanded=True) as phase3_status:
            stream_slot = st.empty()
            stream_lines: list[str] = []

            def _push_stream_line(node_name: str) -> None:
                ts = datetime.utcnow().strftime("%H:%M:%S")
                pretty = node_name.replace("_", " ").title()
                stream_lines.append(f"{ts} | {pretty}")
                stream_slot.markdown("\n".join([f"- `{x}`" for x in stream_lines[-10:]]))

            phase3_status.update(label="Stage 1: Retrieving options chain and SQL context...", state="running")

            async def _consume_stream() -> Dict[str, Any]:
                latest: Dict[str, Any] = {}
                async for event in run_router_nodes_stream(prompt):
                    trace_events.append(event)
                    latest = normalize_state(event.get("state") or latest)
                    event_type = event.get("event")
                    node_name = str(event.get("node", "")).strip()
                    if event_type == "node_start" and node_name:
                        _push_stream_line(node_name)
                        if node_name in {"analyst", "checker", "critic"}:
                            phase3_status.update(label="Stage 2: Analyst reasoning and Gold integration...", state="running")
                        if node_name == "finalizer":
                            phase3_status.update(label="Stage 3: Finalizing structured report...", state="running")
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

        # Step 3: layered tabs layout
        tab_summary, tab_report, tab_audit, tab_context = st.tabs(
            ["Quick Take", "Deep Dive", "Audit & Routing", "Context"]
        )

        with tab_summary:
            assistant_msg = _assistant_brief(final_state)
            st.markdown(assistant_msg)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Force Rerun Analysis", use_container_width=True, key="btn_rerun_pipeline"):
                    st.rerun()
            with c2:
                st.download_button(
                    "Download Quick Take",
                    data=assistant_msg,
                    file_name="quick_take.md",
                    mime="text/markdown",
                    use_container_width=True,
                )

        with tab_report:
            render_final_dashboard(final_state)
            final_strategy = final_state.get("final_strategy") or {}
            report_md = sanitize_text(final_strategy.get("markdown") or "")
            st.download_button(
                "Download Markdown Report",
                data=report_md or "No report markdown available.",
                file_name="Institutional_Options_Strategy_Report.md",
                mime="text/markdown",
                use_container_width=True,
            )

        with tab_audit:
            st.markdown("#### Routing and Revision Snapshot")
            col_rev, col_check, col_crit = st.columns(3)
            col_rev.metric("Revision Count", str(final_state.get("revision_count", 0)))
            col_check.metric("Checker Verdict", str(final_state.get("checker_verdict", "N/A")))
            col_crit.metric("Critic Verdict", str(final_state.get("critic_verdict", "N/A")))
            st.caption(f"Run ID: `{run_id}`")

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
            if audit_rows:
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

                display_rows = []
                for r in filtered:
                    display_rows.append(
                        {
                            "node": r["node"],
                            "latency_ms": f"{r['latency_ms']:.1f}",
                            "verdict": r["verdict"],
                            "findings_n": r["findings_n"],
                            "t_start": r["t_start"],
                        }
                    )
                st.dataframe(display_rows, width="stretch", hide_index=True)
            else:
                st.info("No audit log rows found.")

        with tab_context:
            with st.expander("Macro Preamble", expanded=False):
                st.markdown(sanitize_text(final_state.get("macro_context", "No macro context available.")))

            col_left, col_right = st.columns(2)
            with col_left:
                render_phase1_silver(final_state, st.container())
            with col_right:
                render_phase2_gold(final_state, st.container())

            with st.expander("Developer mode (raw audit fields)", expanded=False):
                st.markdown("Engineering and compliance diagnostics.")
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

        render_footer_disclaimer()
        st.session_state["latest_state"] = final_state
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
