from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

from .config import PROJECT_ROOT
from .contracts import (
    evidence_blend_summary,
    load_ticker_universe,
    normalize_state,
    quick_query_quality,
    sanitize_text,
    state_query_quality,
)
from .io_utils import discover_final_state_files, find_latest_file, safe_read_json, safe_read_text
from .layout_specs import (
    CATEGORY_ORDER,
    group_metrics_by_category,
    normalize_metric_aliases,
    pretty_label,
    pretty_value,
    value_color,
)
from .styles import status_color
from Scripts.core.liquidity_policy import resolve_first_available_ticker_bundle, resolve_primary_ticker


_EXECUTABLE_LIQUIDITY_NOTE = (
    "Liquidity metrics below are computed on the executable subset only: "
    "ask >= 0.50, spread <= 15%, DTE 7-60, tiered moneyness band."
)

_POSTURE_LABEL_MAP = {
    "defensive": "Defensive",
    "constructive": "Constructive",
    "neutral": "Neutral",
    "protection_heavy": "Protection Heavy",
    "neutral_flow": "Balanced Flow",
    "call_skewed": "Call-Skewed",
    "positive_put_premium": "Positive Put Premium",
    "flat": "Flat",
    "normal": "Normal",
    "high": "High",
    "low": "Low",
    "unknown": "Unknown",
}

_TIME_WINDOW_LABEL_TO_VALUE = {
    "Today": "today",
    "Yesterday": "yesterday",
    "Past week": "past_week",
    "Past month": "past_month",
    "Past 6 months": "past_six_months",
}

_SIGNAL_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "news narrative": {
        "source_types": ["news"],
    },
    "GPR context": {
        "source_types": ["gpr"],
    },
    "macro regime narrative": {
        "source_types": ["macro_history"],
    },
    "SEC 8-K event risk": {
        "source_types": ["sec"],
        "requested_sec_forms": ["8-K"],
    },
    "IV skew": {
        "source_types": ["options"],
        "metrics": ["IV Skew"],
    },
    "put-call ratio": {
        "source_types": ["options"],
        "metrics": ["Put/Call Ratio"],
    },
    "liquidity": {
        "source_types": ["options"],
        "metrics": ["Options Liquidity"],
    },
    "pricing spread": {
        "source_types": ["options"],
        "metrics": ["Options Pricing / Spread"],
    },
    "SEC Form 4 insider flow": {
        "source_types": ["sec"],
        "requested_sec_forms": ["4"],
    },
}

_LIQUIDITY_FIELDS = [
    "executable_option_volume",
    "executable_open_interest",
    "liquid_contracts",
    "avg_spread_pct",
    "market_impact_risk",
]

_ARROW_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<path d="M5 12h12" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>'
    '<path d="m13 7 5 5-5 5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
    "</svg>"
)


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


def _surface_title_html(text: str, *, variant: str = "minor") -> str:
    safe = escape(sanitize_text(text))
    return f'<div class="surface-title surface-title-{variant}">{safe}</div>'


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


def _change_class(raw_pct: str) -> str:
    text = (raw_pct or "").replace("%", "").replace("+", "").strip()
    try:
        value = float(text)
    except Exception:
        return "change-neutral"
    if value > 0:
        return "change-positive"
    if value < 0:
        return "change-negative"
    return "change-neutral"


def _extract_market_snapshot_items(md: str) -> List[Dict[str, str]]:
    if not md:
        return []
    items: List[Dict[str, str]] = []
    in_market_section = False
    pattern = re.compile(
        r"^-\s+\*\*\[(?P<asset_class>[^\]]+)\]\s+"
        r"(?P<name>.+?)\s+\((?P<symbol>[^)]+)\)\*\*:\s+"
        r"(?P<value>[-+0-9.,]+)\s+(?P<unit>[^|]+?)\s+\|\s+"
        r"Change:\s+\*\*(?P<change>[-+0-9.,]+%)\*\*\s+"
        r"\*\(Observed:\s*(?P<observed>[^)]+)\)\*"
    )
    for raw in md.splitlines():
        line = raw.strip()
        lowered = line.lower()
        if line.startswith("### ") and "market data" in lowered:
            in_market_section = True
            continue
        if in_market_section and line.startswith("### "):
            break
        if not in_market_section or not line.startswith("- "):
            continue
        match = pattern.match(line)
        if not match:
            continue
        item = {k: v.strip() for k, v in match.groupdict().items()}
        items.append(item)
    return items


def _render_macro_sidebar_snapshot(body: str) -> None:
    if not body or body == "(missing)":
        st.sidebar.warning("Macro & Market Snapshot: data missing")
        return
    items = _extract_market_snapshot_items(body)
    if not items:
        _render_sidebar_markdown_doc("Macro & Market Snapshot", body)
        return

    rows = []
    for item in items[:8]:
        asset_class = escape(sanitize_text(item["asset_class"]))
        name = escape(sanitize_text(item["name"]))
        symbol = escape(sanitize_text(item["symbol"]))
        value = escape(sanitize_text(item["value"]))
        unit = escape(sanitize_text(item["unit"]))
        change = escape(sanitize_text(item["change"]))
        observed = escape(sanitize_text(item["observed"]))
        change_class = _change_class(item["change"])
        rows.append(
            f"""
<div class="market-row">
  <div class="market-row-head">
    <span class="asset-tag">{asset_class}</span>
    <span class="market-symbol">{symbol}</span>
  </div>
  <div class="market-name">{name}</div>
  <div class="market-values">
    <span class="market-value">{value}</span>
    <span class="market-unit">{unit}</span>
    <span class="{change_class}">{change}</span>
  </div>
  <div class="market-date">Observed {observed}</div>
</div>
            """
        )

    st.sidebar.markdown(
        """
<section class="sidebar-section">
  <div class="sidebar-section-title">Macro & Market Snapshot</div>
  <div class="sidebar-section-subtitle">Daily market context from Agent_Context</div>
        """
        + "".join(rows)
        + "</section>",
        unsafe_allow_html=True,
    )


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
    return sections[-1]


def _gpr_signal_class(raw: str) -> str:
    text = (raw or "").replace("%", "").replace("+", "").strip().lower()
    try:
        value = float(text)
    except Exception:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _strip_markdown_inline(text: str) -> str:
    clean = sanitize_text(text)
    clean = re.sub(r"`([^`]+)`", r"\1", clean)
    clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
    clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
    clean = re.sub(r"__([^_]+)__", r"\1", clean)
    clean = re.sub(r"_([^_]+)_", r"\1", clean)
    clean = clean.replace("---", " ")
    clean = re.sub(r"\s{2,}", " ", clean)
    return clean.strip()


def _parse_gpr_body(body: str) -> Dict[str, str]:
    clean = body or ""

    def _extract(pattern: str) -> str:
        match = re.search(pattern, clean, flags=re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else ""

    summary = _extract(r"\*\*Summary:\*\*\s*(.+?)(?=\n\s*\*\*Historical Context:\*\*|\Z)")
    history = _extract(r"\*\*Historical Context:\*\*\s*(.+?)(?=\n\s*\*\*Impact on Precious Metals:\*\*|\Z)")
    impact = _extract(r"\*\*Impact on Precious Metals:\*\*\s*(.+?)(?=\n\s*---|\Z)")
    date_value = _extract(r"\*\*Date:\*\*\s*([^\n]+)")
    score_value = _extract(r"\*\*GPR Score:\*\*\s*([^\n]+)")
    percentile = _extract(r"(\d+(?:\.\d+)?th percentile)")
    mom = _extract(r"(?:surging by|dropping by|changed by)\s*([-+]?\d+(?:\.\d+)?%)")
    yoy = _extract(r"shifted by\s*([-+]?\d+(?:\.\d+)?%)")
    moving_avg = _extract(r"(\d+(?:\.\d+)?)\s*suggests the current medium-term trend")

    return {
        "date": _strip_markdown_inline(date_value),
        "score": _strip_markdown_inline(score_value),
        "summary": _strip_markdown_inline(summary),
        "history": _strip_markdown_inline(history),
        "impact": _strip_markdown_inline(impact),
        "percentile": _strip_markdown_inline(percentile),
        "mom": _strip_markdown_inline(mom),
        "yoy": _strip_markdown_inline(yoy),
        "moving_avg": _strip_markdown_inline(moving_avg),
    }


def _style_gpr_text(text: str) -> str:
    safe = escape(_strip_markdown_inline(text))
    safe = re.sub(
        r"([-+]?\d+(?:\.\d+)?%)",
        lambda m: f'<span class="gpr-inline-chip {_gpr_signal_class(m.group(1))}">{m.group(1)}</span>',
        safe,
    )
    safe = re.sub(
        r"(\d+(?:\.\d+)?th percentile)",
        r'<span class="gpr-inline-chip neutral">\1</span>',
        safe,
    )
    safe = re.sub(
        r"\b(significant escalation|cooling off|relative stability)\b",
        r'<span class="gpr-emphasis">\1</span>',
        safe,
        flags=re.IGNORECASE,
    )
    safe = re.sub(
        r"\b(Gold|Silver|USD|Equities|Oil)\b",
        r'<span class="gpr-asset">\1</span>',
        safe,
    )
    return safe


def _render_gpr_section_blocks(body: str, render_markdown) -> None:
    gpr_fields = _parse_gpr_body(body)
    chips: List[str] = []
    if gpr_fields["date"]:
        chips.append(f'<span class="gpr-inline-chip neutral">{escape(gpr_fields["date"])}</span>')
    if gpr_fields["score"]:
        chips.append(f'<span class="gpr-inline-chip neutral">Score {escape(gpr_fields["score"])}</span>')
    if gpr_fields["mom"]:
        chips.append(
            f'<span class="gpr-inline-chip {_gpr_signal_class(gpr_fields["mom"])}">MoM {escape(gpr_fields["mom"])}</span>'
        )
    if gpr_fields["yoy"]:
        chips.append(
            f'<span class="gpr-inline-chip {_gpr_signal_class(gpr_fields["yoy"])}">YoY {escape(gpr_fields["yoy"])}</span>'
        )
    if gpr_fields["percentile"]:
        chips.append(f'<span class="gpr-inline-chip neutral">{escape(gpr_fields["percentile"])}</span>')

    if chips:
        render_markdown(f'<div class="gpr-chip-row gpr-history-chip-row">{"".join(chips)}</div>', unsafe_allow_html=True)

    sections_to_render = [
        ("Summary", gpr_fields["summary"]),
        ("Historical Context", gpr_fields["history"]),
        ("Impact on Precious Metals", gpr_fields["impact"]),
    ]
    for label, text in sections_to_render:
        if not text:
            continue
        render_markdown(
            f"""
<div class="gpr-block gpr-history-block">
  <div class="gpr-block-label">{escape(label)}</div>
  <div class="gpr-block-body">{_style_gpr_text(text)}</div>
</div>
            """,
            unsafe_allow_html=True,
        )


def _colorize_gpr_text(text: str) -> str:
    safe = escape(sanitize_text(text))
    safe = re.sub(
        r"(\b\d+(?:\.\d+)?%|\b\d+(?:\.\d+)?th percentile|\b\d+(?:\.\d+)?\b)",
        r'<span class="gpr-number">\1</span>',
        safe,
    )
    for label in ["Date", "Summary", "Historical Context", "Impact on Precious Metals"]:
        safe = safe.replace(f"{label}:", f'<span class="gpr-label">{label}:</span>')
    return safe


def _render_gpr_sidebar(gpr_md: str) -> None:
    sections = _parse_gpr_sections(gpr_md)
    current = _pick_current_gpr_section(sections)

    if not current:
        st.sidebar.info("GPR narrative is not available.")
        return

    title = current.get("title", "Current Month")
    body = current.get("body", "")
    title_html = escape(sanitize_text(title))

    st.sidebar.markdown(
        f"""
<div class="gpr-hero">
  <div class="gpr-current-title">{title_html}</div>
</div>
        """,
        unsafe_allow_html=True,
    )
    _render_gpr_section_blocks(body, st.sidebar.markdown)

    if "show_gpr_history" not in st.session_state:
        st.session_state["show_gpr_history"] = False
    if st.sidebar.button("Show last 6 months GPR history", key="gpr_history_toggle_btn", use_container_width=True):
        st.session_state["show_gpr_history"] = not st.session_state["show_gpr_history"]

    if st.session_state["show_gpr_history"]:
        st.sidebar.markdown("**Recent 6 updates**")
        historical_sections = list(reversed(sections[:-1]))[:6]
        for sec in historical_sections:
            with st.sidebar.expander(sec.get("title", "GPR update"), expanded=False):
                _render_gpr_section_blocks(sec.get("body", ""), st.markdown)


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


def _momentum_class(raw_pct: str) -> str:
    text = (raw_pct or "").replace("%", "").replace("+", "").strip()
    try:
        value = float(text)
    except Exception:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def render_macro_lagging_info_bar() -> None:
    macro_path = PROJECT_ROOT / "Data" / "Agent_Context" / "latest_macro_context.md"
    macro_md = safe_read_text(macro_path, "")
    items = _extract_lagging_macro_items(macro_md)
    if not items:
        return

    rows = []
    for item in items:
        title = escape(sanitize_text(item["title"]))
        value = escape(sanitize_text(item["value"]))
        mom = escape(sanitize_text(item["mom"]))
        yoy = escape(sanitize_text(item["yoy"]))
        observed = escape(sanitize_text(item["observed"]))
        mom_class = _momentum_class(item["mom"])
        yoy_class = _momentum_class(item["yoy"])
        rows.append(
            f"""
<div class="macro-indicator-row">
  <div class="macro-indicator-name">
    <div class="macro-indicator-title">{title}</div>
    <div class="macro-indicator-date">Observed {observed}</div>
  </div>
  <div class="macro-indicator-value">{value}</div>
  <div class="macro-indicator-momentum">
    <span class="momentum-pill {mom_class}">MoM {mom}</span>
    <span class="momentum-pill {yoy_class}">YoY {yoy}</span>
  </div>
</div>
            """
        )
    st.markdown(
        """
<div class="macro-infobar">
  <div class="macro-infobar-head">
    <div>
      <div class="macro-infobar-title">Lagging Macro Indicators</div>
      <div class="macro-infobar-subtitle">Slow-cycle macro context. MoM color shows the latest momentum shift.</div>
    </div>
  </div>
  <div class="macro-indicator-list">
        """
        + "".join(rows)
        + "</div></div>",
        unsafe_allow_html=True,
    )


def render_sidebar_context() -> None:
    st.sidebar.header("Global Context")

    macro_path = PROJECT_ROOT / "Data" / "Agent_Context" / "latest_macro_context.md"
    gpr_path = find_latest_file("Data/3_Gold_Semantic/GPR_index/**/gpr_narrative_corpus.md")

    macro_md = safe_read_text(macro_path, "(missing)")
    gpr_md = safe_read_text(gpr_path, "(missing)")

    _render_macro_sidebar_snapshot(macro_md)
    _render_gpr_sidebar(gpr_md)


@st.cache_data(show_spinner=False)
def _load_query_guide_examples() -> List[str]:
    return [
        "Today SPY put-call ratio and IV skew for options posture",
        "Past month AAPL Form 4 insider selling and liquidity for sec filing risk",
        "Past month AAPL SEC 8-K event risk for sec filing risk",
        "Past week GLD GPR context and 10Y yields for macro regime",
    ]


def _query_builder_options() -> Dict[str, List[str]]:
    universe = sorted(load_ticker_universe())
    priority = ["SPY", "QQQ", "IWM", "GLD", "SLV", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "AMZN"]
    tickers = [t for t in priority if t in universe] + [t for t in universe if t not in priority]
    return {
        "tickers": tickers or priority,
        "signals": [
            "IV skew",
            "put-call ratio",
            "liquidity",
            "pricing spread",
            "SEC Form 4 insider flow",
            "SEC 8-K event risk",
            "news narrative",
            "GPR context",
            "macro regime narrative",
        ],
        "goals": [
            "options posture",
            "sec filing risk",
            "geopolitics narrative",
            "macro regime",
        ],
    }


def _compose_builder_query(ticker: str, time_window: str, signals: List[str], goal: str) -> str:
    safe_ticker = (ticker or "").strip()
    safe_time = (time_window or "Past week").strip() or "Past week"
    signal_text = " and ".join([s for s in signals[:3] if str(s).strip()]) or "IV skew"
    asset_prefix = f" {safe_ticker}" if safe_ticker and safe_ticker != "No specific asset" else ""
    return f"{safe_time}{asset_prefix} {signal_text}?"


def _dedupe_keep_order(values: List[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for raw in values:
        item = str(raw or "").strip()
        if item and item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def _build_builder_contract(
    *,
    ticker: str,
    time_window: str,
    signals: List[str],
    goal: str,
    query: str,
) -> Dict[str, Any]:
    source_types: List[str] = []
    metrics: List[str] = []
    requested_sec_forms: List[str] = []
    selected_signals = _dedupe_keep_order([str(signal).strip() for signal in signals if str(signal).strip()])

    for signal in selected_signals:
        spec = _SIGNAL_CONTRACTS.get(str(signal).strip(), {})
        source_types.extend(spec.get("source_types", []))
        metrics.extend(spec.get("metrics", []))
        requested_sec_forms.extend(spec.get("requested_sec_forms", []))

    tickers = []
    safe_ticker = str(ticker or "").strip()
    if safe_ticker and safe_ticker != "No specific asset":
        tickers = [safe_ticker.upper()]
    time_window_value = _TIME_WINDOW_LABEL_TO_VALUE.get(str(time_window or "").strip(), "past_week")

    return {
        "query": query,
        "signals": selected_signals,
        "source_types": _dedupe_keep_order(source_types),
        "metrics": _dedupe_keep_order(metrics),
        "requested_sec_forms": _dedupe_keep_order(requested_sec_forms),
        "tickers": tickers,
        "time_window": time_window_value,
    }


def _event_asset_required(signals: List[str]) -> bool:
    return "SEC 8-K event risk" in [str(signal).strip() for signal in signals]


def _mode_card_copy(mode_name: str) -> str:
    if mode_name == "Microstructure analysis":
        return "Use this for structured signal analysis across options, SEC filing evidence, liquidity, pricing, and execution reality."
    return "Use this for event-driven analysis across filings, news flow, geopolitical context, and catalyst risk."


def render_query_builder() -> Optional[Dict[str, Any]]:
    opts = _query_builder_options()
    signal_options = opts["signals"]
    goal_options = opts["goals"]
    signal_state_key = "qb_signals"
    ticker_state_key = "qb_ticker"
    goal_state_key = "qb_goal"
    default_signals = ["IV skew", "put-call ratio"]
    selected_signals = [str(x) for x in st.session_state.get(signal_state_key, default_signals)]
    selected_goal = str(st.session_state.get(goal_state_key) or goal_options[0])
    force_asset = selected_goal == "sec filing risk" or _event_asset_required(selected_signals)

    if ticker_state_key not in st.session_state:
        st.session_state[ticker_state_key] = "AAPL" if force_asset else "No specific asset"
    elif force_asset and st.session_state.get(ticker_state_key) == "No specific asset":
        st.session_state[ticker_state_key] = "AAPL"

    ticker_col, time_col = st.columns([1, 1.4])
    with ticker_col:
        ticker_options = ["No specific asset", *opts["tickers"]]
        current_ticker = str(st.session_state.get(ticker_state_key) or ticker_options[0])
        if current_ticker not in ticker_options:
            if force_asset and "AAPL" in ticker_options:
                current_ticker = "AAPL"
            elif force_asset and len(ticker_options) > 1:
                current_ticker = ticker_options[1]
            else:
                current_ticker = ticker_options[0]
            st.session_state[ticker_state_key] = current_ticker
        default_index = ticker_options.index(current_ticker)
        ticker = st.selectbox(
            "Asset (required for SEC filing risk)" if force_asset else "Asset (optional)",
            ticker_options,
            index=default_index,
            key=ticker_state_key,
        )
    with time_col:
        time_window = st.segmented_control(
            "Time window",
            list(_TIME_WINDOW_LABEL_TO_VALUE.keys()),
            default="Past week",
            key="qb_time_window",
        )
        time_window = str(time_window or "Past week")

    signals = st.multiselect(
        "Signal chips",
        signal_options,
        default=[s for s in selected_signals if s in signal_options] or [s for s in default_signals if s in signal_options],
        max_selections=3,
        key=signal_state_key,
    )
    goal_index = goal_options.index(selected_goal) if selected_goal in goal_options else 0
    goal = st.selectbox("Goal", goal_options, index=goal_index, key=goal_state_key)
    query = _compose_builder_query(str(ticker), str(time_window), [str(x) for x in signals], str(goal))
    builder_contract = _build_builder_contract(
        ticker=str(ticker),
        time_window=str(time_window),
        signals=[str(x) for x in signals],
        goal=str(goal),
        query=query,
    )

    st.markdown(f'<div class="query-preview query-builder-data">{escape(query)}</div>', unsafe_allow_html=True)
    if st.button("Run Builder Query", key="qb_run", use_container_width=True, type="primary"):
        return {
            "query": query,
            "builder_contract": builder_contract,
        }
    return None


def render_query_guide_buttons(current_query: str = "") -> Optional[Dict[str, Any]]:
    st.markdown(
        """
<section class="guide-hero">
  <div class="guide-kicker">Query Guide</div>
  <div class="guide-title">Build an evidence-ready market question</div>
  <div class="guide-copy">Choose a workflow, signal, time window, and goal. The system cross-checks options microstructure, executive behavior, event evidence, and deterministic guardrails so weak or contradictory setups can be downgraded instead of overclaimed.</div>
</section>
        """,
        unsafe_allow_html=True,
    )
    builder_query = render_query_builder()
    if builder_query:
        return builder_query

    quality = quick_query_quality(current_query)
    st.caption(f"Query quality pre-check: {quality['score']}/100")
    if quality["suggestions"]:
        st.caption("Make it stronger: " + " ".join(quality["suggestions"][:2]))
    templates = _load_query_guide_examples()
    if not templates:
        return None

    selected: Optional[str] = None
    cols = st.columns(2)
    for idx, q in enumerate(templates):
        with cols[idx % 2]:
            if st.button(q, key=f"guide_query_btn_{idx}", use_container_width=True):
                selected = q
    if selected:
        return {"query": selected, "builder_contract": None}
    return None


def load_state_from_picker() -> Dict[str, Any]:
    files = discover_final_state_files()
    if not files:
        st.warning("No local final_state logs found.")
        return {}
    labels = [str(p.relative_to(PROJECT_ROOT)) for p in files]
    selected = st.selectbox("Pick historical final_state", labels)
    return normalize_state(safe_read_json(PROJECT_ROOT / selected))


def _render_silver_readable_parts(normalized_state: Dict[str, Any]) -> None:
    silver = _frontend_silver_values(normalized_state)
    grouped = group_metrics_by_category(silver)

    st.markdown(
        f'<div class="chat-card">{_surface_title_html("Part 4 · Signal Explorer")}</div>',
        unsafe_allow_html=True,
    )
    if not silver:
        st.info("Structured market data is not ready.")
        return

    key_candidates = [
        "pcr_volume",
        "pcr_open_interest",
        "vix_value",
        "gpr_index_level",
    ]
    primary_ticker = _resolve_display_ticker(normalized_state)
    if primary_ticker:
        key_candidates.extend([
            f"{primary_ticker}_market_impact_risk",
            f"{primary_ticker}_liquid_contracts",
        ])
    highlights = [k for k in key_candidates if k in silver][:4]
    if highlights:
        cols = st.columns(len(highlights))
        for col, key in zip(cols, highlights):
            with col:
                label = pretty_label(key)
                value = pretty_value(silver.get(key))
                color = value_color(key, silver.get(key))
                st.markdown(
                    f"""
<div class="metric-highlight">
  <div class="metric-name">{label}</div>
  <div class="metric-value" style="color:{color};">{sanitize_text(value)}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )

    category_tabs = st.tabs(CATEGORY_ORDER)
    for tab, cat in zip(category_tabs, CATEGORY_ORDER):
        with tab:
            keys = grouped.get(cat, [])
            st.caption(f"{len(keys)} mapped signals")
            if cat == "Liquidity":
                _render_liquidity_panel(normalized_state, silver)
                continue
            if not keys:
                st.info("No mapped signals in this category.")
                continue
            if cat == "Cross-Asset":
                _render_cross_asset_groups(keys, silver)
                continue
            if cat == "Macro":
                _render_macro_groups(keys, silver)
                continue
            _render_metric_rows(keys, silver)


def _cross_asset_bucket(metric_key: str) -> str:
    lowered = (metric_key or "").lower()
    if any(token in lowered for token in ["gspc", "ixic", "nasdaq", "spx"]):
        return "Equity indices"
    if any(token in lowered for token in ["gld", "slv"]):
        return "Precious metals"
    if any(token in lowered for token in ["dx_y_nyb", "dxy", "usd"]):
        return "Dollar and FX"
    return "Other cross-asset signals"


def _render_cross_asset_groups(keys: List[str], values: Dict[str, Any]) -> None:
    buckets: Dict[str, List[str]] = {}
    for key in keys:
        buckets.setdefault(_cross_asset_bucket(key), []).append(key)

    order = [
        "Equity indices",
        "Precious metals",
        "Dollar and FX",
        "Other cross-asset signals",
    ]
    for group_name in order:
        group_keys = buckets.get(group_name) or []
        if not group_keys:
            continue
        st.markdown(
            f'<div class="subsection-kicker">{escape(group_name)}</div>',
            unsafe_allow_html=True,
        )
        cols = st.columns(2)
        for idx, metric_key in enumerate(group_keys):
            label = pretty_label(metric_key)
            value = pretty_value(values.get(metric_key))
            color = value_color(metric_key, values.get(metric_key))
            with cols[idx % 2]:
                st.markdown(
                    f"""
<div class="metric-row metric-row-compact">
  <div class="metric-name">{label}</div>
  <div class="metric-value" style="color:{color};">{sanitize_text(value)}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )


def _friendly_source_label(raw_source: str) -> str:
    source = str(raw_source or "").strip().lower()
    mapping = {
        "gold.news": "News events",
        "gold.sec": "SEC filings",
        "gold.gpr": "GPR narrative",
        "silver.options": "Options data",
        "silver.macro": "Macro market data",
        "silver.gpr": "GPR index data",
    }
    if source in mapping:
        return mapping[source]
    if "." in source:
        return source.replace(".", " ").title()
    return str(raw_source or "Source")


def _frontend_silver_values(normalized_state: Dict[str, Any]) -> Dict[str, Any]:
    return normalize_metric_aliases(normalized_state.get("silver_values") or {})


def _resolve_display_ticker(normalized_state: Dict[str, Any]) -> str:
    metadata = normalized_state.get("metadata")
    scope_contract = normalized_state.get("scope_contract") or {}
    ticker = resolve_primary_ticker(metadata=metadata, scope_contract=scope_contract)
    if ticker:
        return str(ticker)
    tickers = (metadata or {}).get("tickers") or []
    return str(tickers[0]) if tickers else ""


def _pretty_posture_value(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "Unknown"
    lowered = text.lower()
    return _POSTURE_LABEL_MAP.get(lowered, " ".join(part.capitalize() for part in lowered.split("_")))


def _format_percent_like(value: Any, *, scale_0_to_1: bool = False) -> str:
    try:
        number = float(value)
    except Exception:
        return sanitize_text(value) or "N/A"
    if scale_0_to_1:
        number *= 100.0
    return f"{number:.2f}%"


def _format_decimal_like(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except Exception:
        return sanitize_text(value) or "N/A"
    return f"{number:.{digits}f}"


def _posture_inference_rows(normalized_state: Dict[str, Any]) -> List[Tuple[str, str]]:
    trace = normalized_state.get("posture_reasoning_trace") or {}
    return [
        ("Posture Label", _pretty_posture_value(normalized_state.get("posture_label"))),
        ("PCR State", _pretty_posture_value(trace.get("pcr_state"))),
        ("IV Regime", _pretty_posture_value(trace.get("iv_regime_state"))),
        ("Skew State", _pretty_posture_value(trace.get("skew_state"))),
    ]


def _volatility_detail_rows(normalized_state: Dict[str, Any]) -> List[Tuple[str, str]]:
    values = _frontend_silver_values(normalized_state)
    rows: List[Tuple[str, str]] = []
    if values.get("latest_atm_iv") is not None:
        rows.append(("ATM IV", _format_percent_like(values.get("latest_atm_iv"), scale_0_to_1=True)))
    if values.get("latest_atm_iv_rank_pct") is not None:
        rows.append(("IV Rank", _format_percent_like(values.get("latest_atm_iv_rank_pct"))))
    if values.get("latest_iv_skew") is not None:
        rows.append(("IV Skew", _format_percent_like(values.get("latest_iv_skew"), scale_0_to_1=True)))
    if values.get("latest_otm_put_iv") is not None:
        rows.append(("OTM Put IV", _format_percent_like(values.get("latest_otm_put_iv"), scale_0_to_1=True)))
    if values.get("latest_otm_call_iv") is not None:
        rows.append(("OTM Call IV", _format_percent_like(values.get("latest_otm_call_iv"), scale_0_to_1=True)))
    if values.get("data_freshness"):
        rows.append(("Observed At / Data Freshness", sanitize_text(values.get("data_freshness"))))
    return rows


def _reasoning_trace_lines(normalized_state: Dict[str, Any]) -> List[str]:
    values = _frontend_silver_values(normalized_state)
    trace = normalized_state.get("posture_reasoning_trace") or {}
    lines: List[str] = []

    pcr_value = values.get("pcr_volume")
    pcr_state = str(trace.get("pcr_state") or "").strip().lower()
    if pcr_value is not None and pcr_state == "protection_heavy":
        lines.append(f"PCR {_format_decimal_like(pcr_value, 2)} -> defensive flow")
    elif pcr_value is not None and pcr_state:
        lines.append(f"PCR {_format_decimal_like(pcr_value, 2)} -> {_pretty_posture_value(pcr_state).lower()}")

    skew_value = values.get("latest_iv_skew")
    skew_state = str(trace.get("skew_state") or "").strip().lower()
    if skew_value is not None and skew_state == "positive_put_premium":
        lines.append("positive skew -> downside protection premium")
    elif skew_value is not None and skew_state:
        lines.append(f"skew {_format_decimal_like(skew_value, 3)} -> {_pretty_posture_value(skew_state).lower()}")

    iv_rank = values.get("latest_atm_iv_rank_pct")
    iv_regime = str(trace.get("iv_regime_state") or "").strip().upper()
    if iv_rank is not None:
        regime_text = "upper-normal vol regime" if iv_regime == "NORMAL" and float(iv_rank) >= 60 else f"{_pretty_posture_value(iv_regime).lower()} vol regime"
        lines.append(f"IV rank {_format_decimal_like(iv_rank, 2)} -> {regime_text}")

    derived_label = normalized_state.get("posture_label") or trace.get("derived_label")
    if derived_label:
        lines.append(f"combined posture -> {_pretty_posture_value(derived_label).lower()}")
    return lines


def _macro_bucket(metric_key: str) -> str:
    lowered = str(metric_key or "").lower()
    if any(token in lowered for token in ["fedfunds", "yield"]):
        return "Policy and Rates"
    if any(token in lowered for token in ["cpiaucsl", "unrate"]):
        return "Inflation and Labor"
    if any(token in lowered for token in ["vix", "gpr"]):
        return "Market Regime Gauges"
    if any(token in lowered for token in ["dxy", "usd"]):
        return "Dollar and Safe-Haven Context"
    return "Other Macro Signals"


def _render_metric_rows(keys: List[str], values: Dict[str, Any], *, compact: bool = False) -> None:
    cols = st.columns(2) if compact else None
    for idx, metric_key in enumerate(keys):
        label = pretty_label(metric_key)
        value = pretty_value(values.get(metric_key))
        color = value_color(metric_key, values.get(metric_key))
        body = f"""
<div class="metric-row{' metric-row-compact' if compact else ''}">
  <div class="metric-name">{label}</div>
  <div class="metric-value" style="color:{color};">{sanitize_text(value)}</div>
</div>
        """
        if compact and cols is not None:
            with cols[idx % 2]:
                st.markdown(body, unsafe_allow_html=True)
        else:
            st.markdown(body, unsafe_allow_html=True)


def _render_macro_groups(keys: List[str], values: Dict[str, Any]) -> None:
    buckets: Dict[str, List[str]] = {}
    for key in keys:
        buckets.setdefault(_macro_bucket(key), []).append(key)
    order = [
        "Policy and Rates",
        "Inflation and Labor",
        "Market Regime Gauges",
        "Dollar and Safe-Haven Context",
        "Other Macro Signals",
    ]
    for bucket_name in order:
        bucket_keys = buckets.get(bucket_name) or []
        if not bucket_keys:
            continue
        st.markdown(f'<div class="subsection-kicker">{escape(bucket_name)}</div>', unsafe_allow_html=True)
        _render_metric_rows(bucket_keys, values)


def _render_posture_inference_block(normalized_state: Dict[str, Any]) -> None:
    rows = _posture_inference_rows(normalized_state)
    st.markdown(
        """
<div class="evidence-block evidence-block-posture">
  <div class="evidence-block-kicker">Narrative & Event Evidence</div>
  <div class="evidence-block-title">Posture inference</div>
  <div class="evidence-block-copy">A plain-language read on how the current options flow and volatility mix frame the market posture.</div>
        """,
        unsafe_allow_html=True,
    )
    for label, value in rows:
        value_class = "evidence-kv-value evidence-pill-neutral"
        lowered = value.lower()
        if any(token in lowered for token in ["defensive", "protection", "positive put premium"]):
            value_class = "evidence-kv-value evidence-pill-caution"
        elif any(token in lowered for token in ["constructive", "balanced", "normal"]):
            value_class = "evidence-kv-value evidence-pill-supportive"
        st.markdown(
            f'<div class="evidence-kv-row"><span class="evidence-kv-label">{escape(label)}</span><span class="{value_class}">{escape(value)}</span></div>',
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def _render_volatility_detail_block(normalized_state: Dict[str, Any]) -> None:
    rows = _volatility_detail_rows(normalized_state)
    if not rows:
        return
    st.markdown(
        """
<div class="evidence-block evidence-block-vol">
  <div class="evidence-block-title">Volatility detail</div>
  <div class="evidence-block-copy">The core implied-volatility markers behind the posture read, shown with the latest observed snapshot.</div>
        """,
        unsafe_allow_html=True,
    )
    for label, value in rows:
        value_class = "evidence-kv-value evidence-kv-strong"
        if "Observed At" in label:
            value_class = "evidence-kv-value evidence-kv-muted"
        st.markdown(
            f'<div class="evidence-kv-row"><span class="evidence-kv-label">{escape(label)}</span><span class="{value_class}">{escape(value)}</span></div>',
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def _render_reasoning_trace_block(normalized_state: Dict[str, Any]) -> None:
    lines = _reasoning_trace_lines(normalized_state)
    if not lines:
        return
    st.markdown(
        """
<div class="evidence-block evidence-block-trace">
  <div class="evidence-block-title">Reasoning trace</div>
  <div class="evidence-block-copy">Short deterministic links between the measured signals and the final posture label.</div>
  <div class="reasoning-trace-list">
        """,
        unsafe_allow_html=True,
    )
    for line in lines:
        left, right = [part.strip() for part in line.split("->", 1)] if "->" in line else (line.strip(), "")
        st.markdown(
            f"""
<div class="reasoning-trace-line">
  <span class="reasoning-trace-signal">{escape(left)}</span>
  <span class="reasoning-trace-arrow-icon">{_ARROW_SVG}</span>
  <span class="reasoning-trace-outcome">{escape(right)}</span>
</div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown("</div></div>", unsafe_allow_html=True)


def _render_liquidity_panel(normalized_state: Dict[str, Any], values: Dict[str, Any]) -> None:
    primary_ticker = _resolve_display_ticker(normalized_state)
    _, bundle = resolve_first_available_ticker_bundle(
        values,
        [primary_ticker] if primary_ticker else [],
        _LIQUIDITY_FIELDS,
    )
    available = {key: bundle.get(key) for key in _LIQUIDITY_FIELDS if bundle.get(key) is not None}
    if not available:
        st.markdown(
            """
<div class="liquidity-empty-state">
  <div class="liquidity-empty-badge">Coverage: unavailable</div>
  <div class="liquidity-empty-copy">Liquidity detail is unavailable for this run. The current dataset did not produce an executable-options subset for spread- and impact-based checks.</div>
</div>
            """,
            unsafe_allow_html=True,
        )
        st.caption(_EXECUTABLE_LIQUIDITY_NOTE)
        return

    ordered_keys = [key for key in _LIQUIDITY_FIELDS if key in available]
    render_values = {
        f"{primary_ticker}_{key}" if primary_ticker else key: available[key]
        for key in ordered_keys
    }
    _render_metric_rows(list(render_values.keys()), render_values)
    st.caption(_EXECUTABLE_LIQUIDITY_NOTE)


def _frontend_quick_take(text: str) -> str:
    clean = sanitize_text(text)
    clean = re.sub(r"^For\s+.+?\?\s*,?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^For\s+.+?\?\s*", "", clean, flags=re.IGNORECASE)
    return clean.strip()


def _extract_markdown_sections(md: str) -> Dict[str, str]:
    if not md:
        return {}
    sections: Dict[str, str] = {}
    current_heading: Optional[str] = None
    current_lines: List[str] = []
    for raw in str(md).splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if current_heading:
                sections[current_heading] = sanitize_text("\n".join(current_lines).strip())
            current_heading = line[3:].strip()
            current_lines = []
            continue
        if line.startswith("# "):
            continue
        if current_heading:
            current_lines.append(line)
    if current_heading:
        sections[current_heading] = sanitize_text("\n".join(current_lines).strip())
    return sections


def _render_time_consistency(normalized_state: Dict[str, Any]) -> None:
    rows = normalized_state.get("source_predicates") or []
    st.markdown(
        """
"""
        + _surface_title_html("Part 5 · Time Consistency Timeline")
        + """
<div class="timeline-section-copy">How the evidence windows line up across options, macro, filings, and narrative sources.</div>
        """,
        unsafe_allow_html=True,
    )
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
  <div class="timeline-row-head">
    <div class="timeline-label">{escape(_friendly_source_label(r.get("source", "source")))}</div>
    <div class="timeline-note timeline-note-inline">{r.get("start_date")} → {r.get("end_date")} · {note}</div>
  </div>
  <div class="timeline-row-bar">
    <div class="timeline-track">
      <div class="timeline-bar" style="left:{left:.2f}%;width:{width:.2f}%;"></div>
    </div>
  </div>
</div>
            """
        )

    st.markdown('<div class="timeline-wrap">' + "".join(bars) + "</div>", unsafe_allow_html=True)
    st.markdown(
        f'<div class="timeline-coverage">Coverage window: <strong>{min_dt.strftime("%b %d, %Y")}</strong> to <strong>{max_dt.strftime("%b %d, %Y")}</strong> ({total_days + 1} calendar days).</div>',
        unsafe_allow_html=True,
    )
    widened_sources = [r.get("source") for r in rows if r.get("widened")]
    if widened_sources:
        st.markdown(
            '<div class="timeline-callout">Extended for coverage stability: '
            + escape(", ".join([_friendly_source_label(str(x)) for x in widened_sources]))
            + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="timeline-callout timeline-callout-good">All sources align to the requested window without widening.</div>',
            unsafe_allow_html=True,
        )
    with st.expander("Timeline audit details", expanded=False):
        for r in rows:
            st.markdown(
                f"- **{_friendly_source_label(r.get('source'))}** | {r.get('start_date')} -> {r.get('end_date')} | "
                f"{r.get('window_days')}d | {r.get('granularity')} | "
                f"{'widened' if r.get('widened') else 'aligned'}"
            )


def render_phase1_silver(state: Dict[str, Any], target) -> None:
    with target:
        normalized = normalize_state(state)
        st.subheader("Structured Signals")
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
"""
            + _surface_title_html("Part 1 · Query Expansion Thesis")
            + """
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
  {_surface_title_html("Part 2 · Target Tickers and Scope")}
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
  {_surface_title_html("Part 3 · Metadata Health")}
  <b>query_quality_score:</b> {quality['score']}/100<br/>
  <b>mapped_metrics:</b> {quality['metrics_count']}<br/>
  <b>time_window:</b> {quality['time_window']}<br/>
  <b>ticker_coverage:</b> {", ".join(quality['in_universe']) if quality['in_universe'] else "None"}
</div>
            """,
            unsafe_allow_html=True,
        )
        if quality["suggestions"]:
            st.info("Make the next query stronger: " + " ".join(quality["suggestions"]))

        _render_silver_readable_parts(normalized)
        _render_time_consistency(normalized)
        st.success(evidence_blend_summary(normalized))


def render_phase2_gold(state: Dict[str, Any], target) -> None:
    with target:
        normalized = normalize_state(state)
        st.subheader("Narrative & Event Evidence")
        _render_posture_inference_block(normalized)
        _render_volatility_detail_block(normalized)
        _render_reasoning_trace_block(normalized)

        chunks = normalized.get("gold_context") or []
        if chunks:
            st.success(f"Found {len(chunks)} narrative/event evidence items.")
            for idx, chunk in enumerate(chunks[:8], start=1):
                meta = chunk.get("metadata") or {}
                source_type = sanitize_text(chunk.get("source_type") or meta.get("source_type") or "event")
                title = sanitize_text(meta.get("title") or meta.get("original_title") or meta.get("accession_no") or f"Evidence {idx}")
                topic = sanitize_text(meta.get("topic") or ", ".join(meta.get("topics") or []) or source_type)
                published = sanitize_text(meta.get("publish_date") or meta.get("filed_at") or meta.get("transaction_date") or "date unavailable")
                url = sanitize_text(meta.get("url") or meta.get("source_url") or "")
                score = chunk.get("score")
                score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "N/A"
                content = sanitize_text(chunk.get("content") or chunk.get("text") or "")
                st.markdown(
                    f"""
<div class="event-evidence-card">
  <div class="event-evidence-head">
    <span class="event-source-tag">{escape(source_type.title())}</span>
    <span class="event-score">Relevance {escape(score_text)}</span>
  </div>
  <div class="event-title">{escape(title)}</div>
  <div class="event-meta">{escape(topic)} · {escape(published)}</div>
  <div class="event-body">{escape(content[:900])}{'...' if len(content) > 900 else ''}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )
                if url:
                    st.markdown(f"[Open source]({url})")
        else:
            st.info("No additional narrative/news/event evidence returned for this run. Deterministic posture and volatility evidence are shown above.")

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
    st.header("Strategy Dashboard")

    final_strategy = normalized.get("final_strategy") or {}
    final_report = normalized.get("final_report") or {}
    markdown_sections = _extract_markdown_sections(final_strategy.get("markdown") or "")

    silver_values = _frontend_silver_values(normalized)
    pcr = silver_values.get("pcr_volume")
    vix = silver_values.get("VIX_value")
    gpr = silver_values.get("gpr_index_level")
    primary_ticker = _resolve_display_ticker(normalized)
    _, liquidity_bundle = resolve_first_available_ticker_bundle(
        silver_values,
        [primary_ticker] if primary_ticker else [],
        ["market_impact_risk"],
    )
    liquidity = liquidity_bundle.get("market_impact_risk")

    status = str(final_strategy.get("status") or "review").replace("_", " ").title()
    confidence = final_strategy.get("confidence_score") or final_report.get("confidence_score")
    confidence_text = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else str(confidence or "N/A")
    st.markdown(
        f"""
<div class="decision-strip">
  <div>
    <div class="decision-label">Final Status</div>
    <div class="decision-value">{sanitize_text(status)}</div>
  </div>
  <div>
    <div class="decision-label">Confidence</div>
    <div class="decision-value">{sanitize_text(confidence_text)}</div>
  </div>
  <div>
    <div class="decision-label">Evidence Blend</div>
    <div class="decision-note">{sanitize_text(evidence_blend_summary(normalized))}</div>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    q1, q2, q3, q4 = st.columns(4)
    pcr_level, pcr_sub = _traffic_light("pcr", pcr)
    vix_level, vix_sub = _traffic_light("vix", vix)
    gpr_level, gpr_sub = _traffic_light("gpr", gpr)
    liq_level, liq_sub = _traffic_light("liquidity", liquidity)
    _render_risk_card(q1, "PCR (Volume)", str(pcr if pcr is not None else "N/A"), pcr_level, pcr_sub)
    _render_risk_card(q2, "VIX", str(vix if vix is not None else "N/A"), vix_level, vix_sub)
    _render_risk_card(q3, "GPR", str(gpr if gpr is not None else "N/A"), gpr_level, gpr_sub)
    _render_risk_card(q4, "Liquidity Risk (Executable Subset)", str(liquidity or "Unavailable"), liq_level, liq_sub)
    st.caption(_EXECUTABLE_LIQUIDITY_NOTE)

    st.markdown(
        _surface_title_html("Market Read", variant="main"),
        unsafe_allow_html=True,
    )

    macro_summary = final_report.get("macro_summary") or "No macro summary available."
    convo = final_report.get("conversation_reply") or "No final narrative available."
    status_note = final_report.get("status_note") or ""
    asset_options_read = markdown_sections.get("Asset / Options Read") or "No asset/options read available."
    key_risks = final_report.get("key_risks_and_hedges") or []
    macro_summary = sanitize_text(macro_summary)
    convo = _frontend_quick_take(convo) or sanitize_text(convo)
    status_note = sanitize_text(status_note)

    st.markdown(
        f"""
<div class="institutional-card">
  <div class="institutional-title">Executive Summary</div>
  {convo}
</div>
        """,
        unsafe_allow_html=True,
    )
    if status_note:
        st.markdown(
            f"""
<div class="institutional-card">
  <div class="institutional-title">Status Note</div>
  {escape(status_note)}
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

    st.markdown(
        f"""
<div class="institutional-card">
  <div class="institutional-title">Asset / Options Read</div>
  {escape(sanitize_text(asset_options_read))}
</div>
        """,
        unsafe_allow_html=True,
    )

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
