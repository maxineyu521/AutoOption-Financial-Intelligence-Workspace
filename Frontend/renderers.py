from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

from .config import PROJECT_ROOT
from .contracts import evidence_blend_summary, normalize_state, quick_query_quality, sanitize_text, state_query_quality
from .io_utils import discover_final_state_files, find_latest_file, safe_read_json, safe_read_text
from .layout_specs import CATEGORY_ORDER, group_metrics_by_category, pretty_label, pretty_value, value_color
from .styles import status_color


def _markdown_overview(md: str, max_headers: int = 6, max_bullets: int = 8) -> Dict[str, List[str]]:
    headers: List[str] = []
    bullets: List[str] = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            headers.append(re.sub(r"^#+\s*", "", line))
        elif line.startswith(("- ", "* ")):
            bullets.append(line[2:].strip())
    return {
        "headers": headers[:max_headers],
        "bullets": bullets[:max_bullets],
    }


def _render_sidebar_markdown_doc(title: str, body: str, max_points: int = 6) -> None:
    if not body or body == "(missing)":
        st.sidebar.warning(f"{title}: data missing")
        return

    overview = _markdown_overview(body, max_headers=4, max_bullets=max_points)
    points = overview["bullets"] if overview["bullets"] else overview["headers"]
    st.sidebar.markdown(
        f"""
<div class="chat-card">
  <div class="chat-card-title">{title}</div>
</div>
        """,
        unsafe_allow_html=True,
    )
    for p in points[:max_points]:
        st.sidebar.markdown(f"- {p}")


def _parse_gpr_sections(md: str) -> List[Dict[str, str]]:
    if not md:
        return []
    lines = md.splitlines()
    sections: List[Dict[str, str]] = []
    current_title: Optional[str] = None
    current_body: List[str] = []

    for raw in lines:
        line = raw.strip()
        is_header = line.startswith("#") and "geopolitical risk" in line.lower()
        if is_header:
            if current_title:
                sections.append({"title": current_title, "body": "\n".join(current_body).strip()})
            current_title = re.sub(r"^#+\s*", "", line)
            current_body = []
            continue
        if current_title:
            current_body.append(raw)

    if current_title:
        sections.append({"title": current_title, "body": "\n".join(current_body).strip()})
    return sections


def _pick_current_gpr_section(sections: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not sections:
        return None
    now = datetime.utcnow()
    month_name = now.strftime("%B").lower()
    year = str(now.year)

    for sec in reversed(sections):
        title = sec.get("title", "").lower()
        if month_name in title and year in title:
            return sec
    for sec in reversed(sections):
        if month_name in sec.get("title", "").lower():
            return sec
    return sections[-1]


def _render_gpr_sidebar(gpr_md: str) -> None:
    sections = _parse_gpr_sections(gpr_md)
    current = _pick_current_gpr_section(sections)

    st.sidebar.markdown(
        """
<div class="chat-card">
  <div class="chat-card-title">Geopolitical Risk (GPR) Index</div>
</div>
        """,
        unsafe_allow_html=True,
    )

    if not current:
        st.sidebar.info("GPR narrative is not available.")
        return

    title = current.get("title", "Current Month")
    body = current.get("body", "")
    st.sidebar.markdown(f"**{title}**")
    gpr_overview = _markdown_overview(body, max_headers=0, max_bullets=8)
    if gpr_overview["bullets"]:
        for point in gpr_overview["bullets"][:6]:
            st.sidebar.markdown(f"- {point}")
    elif body:
        st.sidebar.markdown(body[:900] + ("..." if len(body) > 900 else ""))

    if "show_gpr_history" not in st.session_state:
        st.session_state["show_gpr_history"] = False
    if st.sidebar.button("Show last 6 months GPR history", key="gpr_history_toggle_btn", use_container_width=True):
        st.session_state["show_gpr_history"] = not st.session_state["show_gpr_history"]

    if st.session_state["show_gpr_history"]:
        st.sidebar.markdown("**Recent 6 updates**")
        for sec in sections[-6:]:
            with st.sidebar.expander(sec.get("title", "GPR update"), expanded=False):
                ov = _markdown_overview(sec.get("body", ""), max_headers=0, max_bullets=8)
                if ov["bullets"]:
                    for point in ov["bullets"][:6]:
                        st.markdown(f"- {point}")
                else:
                    st.markdown(sec.get("body", "(empty)"))


def _extract_lagging_macro_items(md: str) -> List[Dict[str, str]]:
    if not md:
        return []
    lines = md.splitlines()
    start = -1
    for i, raw in enumerate(lines):
        if "macro economic indicators (lagging)" in raw.lower():
            start = i + 1
            break
    if start < 0:
        return []

    bucket: List[str] = []
    for raw in lines[start:]:
        line = raw.strip()
        if line.startswith("### "):
            break
        if line.startswith("- "):
            bucket.append(line)

    items: List[Dict[str, str]] = []
    for line in bucket:
        title_match = re.search(r"\*\*([^*]+)\*\*", line)
        title = title_match.group(1) if title_match else "Macro Indicator"
        remainder = line
        if title_match:
            remainder = line[title_match.end():].lstrip(": ").strip()
        obs_match = re.search(r"\*\(Observed:\s*([^)]+)\)\*", line)
        observed = obs_match.group(1).strip() if obs_match else "N/A"
        value = re.sub(r"\|\s*MoM:.*$", "", remainder).strip()
        mom_match = re.search(r"MoM:\s*\*\*([^*]+)\*\*", line)
        yoy_match = re.search(r"YoY:\s*\*\*([^*]+)\*\*", line)
        mom = mom_match.group(1).strip() if mom_match else "N/A"
        yoy = yoy_match.group(1).strip() if yoy_match else "N/A"
        items.append(
            {
                "title": title,
                "value": value,
                "mom": mom,
                "yoy": yoy,
                "observed": observed,
            }
        )
    return items[:3]


def render_macro_lagging_info_bar() -> None:
    macro_path = PROJECT_ROOT / "Data" / "Agent_Context" / "latest_macro_context.md"
    macro_md = safe_read_text(macro_path, "")
    items = _extract_lagging_macro_items(macro_md)
    if not items:
        return

    cards = []
    for item in items:
        cards.append(
            f"""
<div class="macro-pill">
  <div class="macro-pill-title">{sanitize_text(item['title'])}</div>
  <div class="macro-pill-body">{sanitize_text(item['value'])}</div>
  <div class="macro-pill-meta">MoM: {sanitize_text(item['mom'])} | YoY: {sanitize_text(item['yoy'])}</div>
  <div class="macro-pill-meta">Observed: {sanitize_text(item['observed'])}</div>
</div>
            """
        )
    st.markdown(
        '<div class="macro-infobar"><div class="macro-infobar-title">Lagging Macro Indicators</div>'
        + "".join(cards)
        + "</div>",
        unsafe_allow_html=True,
    )


def render_sidebar_context() -> None:
    st.sidebar.header("Global Context")

    macro_path = PROJECT_ROOT / "Data" / "Agent_Context" / "latest_macro_context.md"
    gpr_path = find_latest_file("Data/3_Gold_Semantic/GPR_index/**/gpr_narrative_corpus.md")

    macro_md = safe_read_text(macro_path, "(missing)")
    gpr_md = safe_read_text(gpr_path, "(missing)")

    _render_sidebar_markdown_doc("Macro & Market Snapshot", macro_md)
    _render_gpr_sidebar(gpr_md)


@st.cache_data(show_spinner=False)
def _load_query_guide_examples() -> List[str]:
    guide_path = PROJECT_ROOT / "docs" / "User_Query_Guide.md"
    text = safe_read_text(guide_path, "")
    fallback = [
        "Past month AAPL Form-4 selling signal and put positioning?",
        "Today SPY put-call ratio and ATM IV for 30-DTE puts",
        "Past week FOMC and 10Y yields impact on QQQ options",
        "Today GLD IV skew and liquid 30-DTE hedge strikes",
    ]
    if not text:
        return fallback

    inline = re.findall(r"-\s*`([^`]+)`", text)
    plain = re.findall(r"-\s+([A-Za-z][^\n]+)", text)
    plain_queries = [
        q.strip()
        for q in plain
        if (
            "today" in q.lower()
            or "past " in q.lower()
            or "form-4" in q.lower()
            or "put-call ratio" in q.lower()
        )
    ]
    candidates = [q.strip() for q in [*inline, *plain_queries] if 20 <= len(q.strip()) <= 120]

    deduped: List[str] = []
    seen = set()
    for q in candidates:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(q)
    return deduped[:6] if deduped else fallback


def render_query_guide_buttons(current_query: str = "") -> Optional[str]:
    st.markdown("#### Query Guide")
    st.markdown("Pick a template to start quickly:")
    quality = quick_query_quality(current_query)
    st.caption(f"Query quality pre-check: {quality['score']}/100")
    if quality["suggestions"]:
        st.caption("Suggestions: " + " ".join(quality["suggestions"][:2]))
    templates = _load_query_guide_examples()
    if not templates:
        return None

    selected: Optional[str] = None
    cols = st.columns(2)
    for idx, q in enumerate(templates):
        with cols[idx % 2]:
            if st.button(q, key=f"guide_query_btn_{idx}", use_container_width=True):
                selected = q
    return selected


def load_state_from_picker() -> Dict[str, Any]:
    files = discover_final_state_files()
    if not files:
        st.warning("No local final_state logs found.")
        return {}
    labels = [str(p.relative_to(PROJECT_ROOT)) for p in files]
    selected = st.selectbox("Pick historical final_state", labels)
    return normalize_state(safe_read_json(PROJECT_ROOT / selected))


def _render_silver_readable_parts(normalized_state: Dict[str, Any]) -> None:
    silver = normalized_state.get("silver_values") or {}
    grouped = group_metrics_by_category(silver)

    st.markdown('<div class="chat-card"><div class="chat-card-title">Part 4 · Silver Detailed Signals</div></div>', unsafe_allow_html=True)
    if not silver:
        st.info("Silver data not ready.")
        return

    c1, c2 = st.columns(2)
    left_cats = CATEGORY_ORDER[:2]
    right_cats = CATEGORY_ORDER[2:]

    with c1:
        for cat in left_cats:
            st.markdown(f"**{cat}**")
            keys = grouped.get(cat, [])
            if not keys:
                st.caption("No mapped signals.")
                continue
            for k in keys[:12]:
                label = pretty_label(k)
                value = pretty_value(silver.get(k))
                color = value_color(k, silver.get(k))
                st.markdown(
                    f"""
<div class="metric-row">
  <div class="metric-name">{label}</div>
  <div class="metric-value" style="color:{color};">{sanitize_text(value)}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )
    with c2:
        for cat in right_cats:
            st.markdown(f"**{cat}**")
            keys = grouped.get(cat, [])
            if not keys:
                st.caption("No mapped signals.")
                continue
            for k in keys[:12]:
                label = pretty_label(k)
                value = pretty_value(silver.get(k))
                color = value_color(k, silver.get(k))
                st.markdown(
                    f"""
<div class="metric-row">
  <div class="metric-name">{label}</div>
  <div class="metric-value" style="color:{color};">{sanitize_text(value)}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )


def _render_time_consistency(normalized_state: Dict[str, Any]) -> None:
    rows = normalized_state.get("source_predicates") or []
    st.markdown('<div class="chat-card"><div class="chat-card-title">Part 5 · Time Consistency Timeline</div></div>', unsafe_allow_html=True)
    if not rows:
        st.info("No source_predicates available.")
        return
    valid = [r for r in rows if r.get("start_date") and r.get("end_date")]
    if not valid:
        for r in rows:
            st.markdown(
                f"- **{r.get('source')}** | start={r.get('start_date')} | end={r.get('end_date')} | "
                f"window={r.get('window_days')}d | {r.get('granularity')}"
            )
        return

    def _dt(s: str) -> datetime:
        return datetime.strptime(str(s), "%Y-%m-%d")

    min_dt = min(_dt(r["start_date"]) for r in valid)
    max_dt = max(_dt(r["end_date"]) for r in valid)
    total_days = max((max_dt - min_dt).days, 1)

    bars: List[str] = []
    for r in rows:
        if not r.get("start_date") or not r.get("end_date"):
            continue
        start = _dt(r["start_date"])
        end = _dt(r["end_date"])
        left = ((start - min_dt).days / total_days) * 100
        width = max((((end - start).days + 1) / total_days) * 100, 1.5)
        note = "widened" if r.get("widened") else "aligned"
        bars.append(
            f"""
<div class="timeline-row">
  <div class="timeline-label">{r.get("source", "source")}</div>
  <div class="timeline-track">
    <div class="timeline-bar" style="left:{left:.2f}%;width:{width:.2f}%;"></div>
  </div>
  <div class="timeline-note">{r.get("start_date")} → {r.get("end_date")} ({note})</div>
</div>
            """
        )

    st.markdown('<div class="timeline-wrap">' + "".join(bars) + "</div>", unsafe_allow_html=True)
    st.markdown(
        f"Coverage window: **{min_dt.strftime('%b %d, %Y')}** to **{max_dt.strftime('%b %d, %Y')}** "
        f"({total_days + 1} calendar days).",
    )
    widened_sources = [r.get("source") for r in rows if r.get("widened")]
    if widened_sources:
        st.info("Widened for stability: " + ", ".join([str(x) for x in widened_sources]))
    else:
        st.success("All sources align to the requested window without widening.")
    with st.expander("Timeline audit details", expanded=False):
        for r in rows:
            st.markdown(
                f"- **{r.get('source')}** | {r.get('start_date')} -> {r.get('end_date')} | "
                f"{r.get('window_days')}d | {r.get('granularity')} | "
                f"{'widened' if r.get('widened') else 'aligned'}"
            )


def render_phase1_silver(state: Dict[str, Any], target) -> None:
    with target:
        normalized = normalize_state(state)
        st.subheader("Phase 1 - Context Understanding")
        metadata = normalized.get("metadata") or {}
        hyde = normalized.get("hyde_anticipation") or {}
        quality = state_query_quality(normalized)
        metadata_with_lineage = {
            **metadata,
            "latest_update_date": normalized.get("latest_update_date", "Unknown"),
        }

        st.markdown(
            """
<div class="chat-card">
  <div class="chat-card-title">Part 1 · Market Thesis (HyDE)</div>
</div>
            """,
            unsafe_allow_html=True,
        )
        st.write(sanitize_text(hyde.get("paragraph", "HyDE anticipation is not available yet.")))
        if hyde.get("rerank_query"):
            st.markdown(f"**Focus Query:** {hyde.get('rerank_query')}")
        if hyde.get("whitelisted_tickers"):
            st.markdown(f"**Expanded Universe:** {', '.join(hyde.get('whitelisted_tickers', []))}")

        st.markdown(
            f"""
<div class="chat-card">
  <div class="chat-card-title">Part 2 · Target Tickers and Scope</div>
  <b>Target Tickers:</b> {", ".join(metadata_with_lineage.get("tickers", [])) or "N/A"}<br/>
  <b>Requested Window:</b> {metadata_with_lineage.get("time_window", "N/A")}<br/>
  <b>Data Sources:</b> {", ".join(metadata_with_lineage.get("source_types", [])) or "N/A"}<br/>
  <b>Signal Type:</b> {metadata_with_lineage.get("event_keyword", "N/A")}<br/>
  <b>Direction Bias:</b> {metadata_with_lineage.get("action_direction", "N/A")}<br/>
  <b>Latest Update Date:</b> {metadata_with_lineage.get("latest_update_date", "Unknown")}
</div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""
<div class="chat-card">
  <div class="chat-card-title">Part 3 · Metadata Health</div>
  <b>query_quality_score:</b> {quality['score']}/100<br/>
  <b>mapped_metrics:</b> {quality['metrics_count']}<br/>
  <b>time_window:</b> {quality['time_window']}<br/>
  <b>ticker_coverage:</b> {", ".join(quality['in_universe']) if quality['in_universe'] else "None"}
</div>
            """,
            unsafe_allow_html=True,
        )
        if quality["suggestions"]:
            st.info("Suggestions: " + " ".join(quality["suggestions"]))

        _render_silver_readable_parts(normalized)
        _render_time_consistency(normalized)
        st.success(evidence_blend_summary(normalized))


def render_phase2_gold(state: Dict[str, Any], target) -> None:
    with target:
        normalized = normalize_state(state)
        st.subheader("Phase 2 - Gold / Filings")
        sales = normalized.get("sec_sales") or []
        if sales:
            st.success(f"Detected {len(sales)} Form-4 SELL records.")
            for idx, sale in enumerate(sales, start=1):
                st.markdown(
                    f"""
<div class="chat-card">
  <div class="chat-card-title">SEC Signal {idx}</div>
  <b>Executive:</b> {sanitize_text(sale.get('executive'))}<br/>
  <b>Filing Date:</b> {sanitize_text(sale.get('filed_at'))}<br/>
  <b>Transaction Date:</b> {sanitize_text(sale.get('transaction_date'))}<br/>
  <b>Narrative:</b> {sanitize_text(sale.get('content'))}
</div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.info("Gold filings not ready or no SELL records.")
        st.caption(evidence_blend_summary(normalized))
        links = normalized.get("silver_links") or []
        if links:
            st.markdown("**Related Source Links**")
            for link in links[:8]:
                st.markdown(f"- [Open source]({link})")
        if sales:
            st.markdown("**Filing Links**")
            for sale in sales:
                url = sanitize_text(sale.get("url"))
                accession = sanitize_text(sale.get("accession_no"))
                filed_at = sanitize_text(sale.get("filed_at"))
                if url:
                    st.markdown(f"- [{accession or 'SEC filing'} ({filed_at})]({url})")


def render_phase12_first_ready(state: Dict[str, Any], phase1_slot, phase2_slot) -> None:
    normalized = normalize_state(state)

    def _prepare_phase1(payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload

    def _prepare_phase2(payload: Dict[str, Any]) -> Dict[str, Any]:
        return payload

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_prepare_phase1, normalized): "phase1",
            pool.submit(_prepare_phase2, normalized): "phase2",
        }
        for fut in as_completed(futures):
            bucket = futures[fut]
            partial = fut.result()
            if bucket == "phase1":
                render_phase1_silver(partial, phase1_slot)
            else:
                render_phase2_gold(partial, phase2_slot)


def _traffic_light(metric: str, value: Any) -> Tuple[str, str]:
    m = metric.lower()
    if m == "pcr":
        if value is None:
            return "yellow", "No data"
        v = float(value)
        if v > 1.0:
            return "red", "Put-heavy"
        if v >= 0.7:
            return "yellow", "Balanced"
        return "green", "Call-heavy"
    if m == "vix":
        if value is None:
            return "yellow", "No data"
        v = float(value)
        if v >= 25:
            return "red", "Stress"
        if v >= 18:
            return "yellow", "Elevated"
        return "green", "Calm"
    if m == "gpr":
        if value is None:
            return "yellow", "No data"
        v = float(value)
        if v >= 250:
            return "red", "Geopolitical stress"
        if v >= 150:
            return "yellow", "Watch"
        return "green", "Stable"
    if m == "liquidity":
        txt = str(value or "Unknown").lower()
        if "high" in txt:
            return "red", "High impact risk"
        if "medium" in txt:
            return "yellow", "Medium impact risk"
        if "low" in txt:
            return "green", "Low impact risk"
        return "yellow", "Unknown"
    return "yellow", "Unknown"


def _render_risk_card(target, title: str, value: str, level: str, subtitle: str) -> None:
    color = status_color(level)
    target.markdown(
        f"""
<div class="risk-card">
  <div class="risk-title">{title}</div>
  <div class="risk-value">{value}</div>
  <span class="risk-pill" style="background:{color};">{subtitle}</span>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_final_dashboard(state: Dict[str, Any]) -> None:
    normalized = normalize_state(state)
    st.markdown("---")
    st.header("Final Dashboard")

    final_strategy = normalized.get("final_strategy") or {}
    final_report = normalized.get("final_report") or {}

    silver_values = normalized.get("silver_values") or {}
    pcr = silver_values.get("pcr_volume")
    vix = silver_values.get("VIX_value")
    gpr = silver_values.get("gpr_index_level")
    liquidity = silver_values.get("AAPL_market_impact_risk")

    q1, q2, q3, q4 = st.columns(4)
    pcr_level, pcr_sub = _traffic_light("pcr", pcr)
    vix_level, vix_sub = _traffic_light("vix", vix)
    gpr_level, gpr_sub = _traffic_light("gpr", gpr)
    liq_level, liq_sub = _traffic_light("liquidity", liquidity)
    _render_risk_card(q1, "PCR (Volume)", str(pcr if pcr is not None else "N/A"), pcr_level, pcr_sub)
    _render_risk_card(q2, "VIX", str(vix if vix is not None else "N/A"), vix_level, vix_sub)
    _render_risk_card(q3, "GPR", str(gpr if gpr is not None else "N/A"), gpr_level, gpr_sub)
    _render_risk_card(q4, "Liquidity Risk", str(liquidity or "N/A"), liq_level, liq_sub)

    st.markdown(
        """
<div class="institutional-card">
  <div class="institutional-title">Institutional Options Strategy Report</div>
</div>
        """,
        unsafe_allow_html=True,
    )

    macro_summary = final_report.get("macro_summary") or "No macro summary available."
    convo = final_report.get("conversation_reply") or "No final narrative available."
    trade_ideas = final_report.get("trade_ideas") or []
    key_risks = final_report.get("key_risks_and_hedges") or []
    macro_summary = sanitize_text(macro_summary)
    convo = sanitize_text(convo)

    st.markdown(
        f"""
<div class="institutional-card">
  <div class="institutional-title">Executive Summary</div>
  {convo}
</div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
<div class="institutional-card">
  <div class="institutional-title">Macro Regime Narrative</div>
  {macro_summary}
</div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="institutional-card"><div class="institutional-title">Trade Ideas</div></div>', unsafe_allow_html=True)
    if trade_ideas:
        for idx, idea in enumerate(trade_ideas, start=1):
            if isinstance(idea, dict):
                title = idea.get("title") or idea.get("name") or f"Idea {idx}"
                thesis = idea.get("thesis") or idea.get("rationale") or str(idea)
                st.markdown(f"**{idx}. {title}**")
                st.markdown(f"- {sanitize_text(thesis)}")
            else:
                st.markdown(f"- {sanitize_text(idea)}")
    else:
        st.info("No actionable trade idea generated for this run.")

    st.markdown('<div class="institutional-card"><div class="institutional-title">Risk Controls and Hedges</div></div>', unsafe_allow_html=True)
    if key_risks:
        for rk in key_risks:
            st.markdown(f"- {sanitize_text(rk)}")
    else:
        st.info("No explicit risk/hedge block produced.")

def render_footer_disclaimer() -> None:
    st.markdown(
        """
<div class="disclaimer-note">
  Disclaimer: This material is provided for informational purposes only and may contain AI-generated errors.
  It is not investment advice, a solicitation, or a recommendation to buy or sell any security.
</div>
        """,
        unsafe_allow_html=True,
    )
