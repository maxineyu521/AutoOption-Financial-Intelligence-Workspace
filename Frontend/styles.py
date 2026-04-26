from __future__ import annotations

import streamlit as st


def inject_theme() -> None:
    st.markdown(
        """
<style>
  :root {
    --bg-main: #eef2f7;
    --bg-card: #f6f8fb;
    --bg-card-soft: #f9fbfd;
    --text-main: #172b4d;
    --text-muted: #5f6f86;
    --accent-border: #c7d1df;
    --timeline-track: #dbe3ee;
    --timeline-bar: #8f7b52;
    --pill-text: #24364f;
    --green-soft: #557a6d;
    --yellow-soft: #b08d57;
    --red-soft: #a6635d;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-main: #10161f;
      --bg-card: #172233;
      --bg-card-soft: #1a273b;
      --text-main: #d7e1ef;
      --text-muted: #9fb1c8;
      --accent-border: #2b3d57;
      --timeline-track: #263447;
      --timeline-bar: #c4a871;
      --pill-text: #f3f7ff;
      --green-soft: #73a193;
      --yellow-soft: #c4a871;
      --red-soft: #cb877f;
    }
  }
  .stApp {
    background: var(--bg-main);
    color: var(--text-main);
    font-size: 16px;
  }
  .stMarkdown, .stText, .stMetric, label, p, span, div {
    color: var(--text-main);
  }
  .stStatus, .stChatMessage, .stRadio, .stButton, .stTextInput {
    color: var(--text-main) !important;
  }
  section[data-testid="stSidebar"] {
    background: var(--bg-card) !important;
    color: var(--text-main) !important;
    border-right: 1px solid var(--accent-border);
  }
  section[data-testid="stSidebar"] * {
    color: var(--text-main) !important;
  }
  .stButton > button, .stDownloadButton > button {
    background: var(--bg-card-soft) !important;
    color: var(--text-main) !important;
    border: 1px solid var(--accent-border) !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
  }
  .stButton > button:hover, .stDownloadButton > button:hover {
    border-color: var(--timeline-bar) !important;
    background: rgba(176, 141, 87, 0.12) !important;
  }
  .stButton > button[kind="primary"] {
    background: #274c77 !important;
    color: #f4f7fb !important;
    border: 1px solid #274c77 !important;
  }
  .stButton > button[kind="primary"]:hover {
    background: #1f3d62 !important;
    border-color: #1f3d62 !important;
  }
  .stTextInput input, .stChatInput input, textarea {
    color: var(--text-main) !important;
    background: var(--bg-card-soft) !important;
    border: 1px solid var(--accent-border) !important;
  }
  .stTabs [data-baseweb="tab-list"] {
    gap: 8px;
  }
  .stTabs [data-baseweb="tab"] {
    background: var(--bg-card-soft);
    border: 1px solid var(--accent-border);
    border-radius: 10px 10px 0 0;
    color: var(--text-main);
    padding: 8px 12px;
    font-weight: 600;
  }
  .stTabs [aria-selected="true"] {
    border-color: var(--timeline-bar) !important;
    color: var(--text-main) !important;
    background: rgba(39, 76, 119, 0.08) !important;
  }
  .stCaption {
    font-size: 14px !important;
    color: var(--text-muted);
  }
  h1 { font-size: 2.0rem !important; }
  h2 { font-size: 1.6rem !important; }
  h3 { font-size: 1.35rem !important; }
  p, li { font-size: 1.0rem !important; line-height: 1.55; }
  .chat-card {
    border: 1px solid var(--accent-border);
    border-radius: 12px;
    padding: 12px 14px;
    background: var(--bg-card-soft);
    margin-bottom: 8px;
  }
  .chat-card-title {
    font-size: 12px;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-bottom: 6px;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .risk-card {
    border: 1px solid var(--accent-border);
    border-radius: 10px;
    padding: 12px 14px;
    background: var(--bg-card);
    min-height: 110px;
  }
  .risk-title { font-size: 12px; color: var(--text-muted); margin-bottom: 8px; }
  .risk-value { font-size: 22px; font-weight: 700; color: var(--text-main); margin-bottom: 6px; }
  .risk-pill {
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    border-radius: 999px;
    padding: 3px 9px;
    color: var(--pill-text);
  }
  .timeline-wrap {
    border: 1px solid var(--accent-border);
    border-radius: 12px;
    background: var(--bg-card-soft);
    padding: 10px 12px;
    margin-bottom: 8px;
  }
  .timeline-row {
    display: grid;
    grid-template-columns: 150px 1fr 110px;
    gap: 8px;
    align-items: center;
    margin: 8px 0;
  }
  .timeline-label {
    font-size: 12px;
    color: var(--text-muted);
    font-weight: 700;
  }
  .timeline-track {
    position: relative;
    height: 14px;
    background: var(--timeline-track);
    border-radius: 999px;
  }
  .timeline-bar {
    position: absolute;
    height: 14px;
    border-radius: 999px;
    background: var(--timeline-bar);
  }
  .timeline-note {
    font-size: 12px;
    color: var(--text-muted);
    text-align: right;
  }
  .institutional-card {
    border: 1px solid var(--accent-border);
    border-radius: 12px;
    background: var(--bg-card-soft);
    padding: 14px 16px;
    margin-bottom: 10px;
  }
  .institutional-title {
    font-size: 13px;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-bottom: 6px;
    font-weight: 700;
  }
  .metric-row {
    border: 1px solid var(--accent-border);
    border-radius: 10px;
    background: var(--bg-card-soft);
    padding: 10px 12px;
    margin-bottom: 8px;
  }
  .metric-name {
    font-size: 13px;
    color: var(--text-muted);
    margin-bottom: 4px;
    font-weight: 700;
  }
  .metric-value {
    font-size: 18px;
    font-weight: 700;
    color: var(--text-main);
  }
  .macro-infobar {
    border: 1px solid var(--accent-border);
    border-radius: 12px;
    background: linear-gradient(90deg, rgba(176,141,87,0.10), rgba(23,43,77,0.06));
    padding: 10px 12px;
    margin: 6px 0 12px 0;
  }
  .macro-infobar-title {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .04em;
    color: var(--text-muted);
    margin-bottom: 8px;
    font-weight: 700;
  }
  .macro-pill {
    border: 1px solid var(--accent-border);
    border-radius: 10px;
    background: var(--bg-card-soft);
    padding: 8px 10px;
    margin-bottom: 6px;
  }
  .macro-pill-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-main);
    margin-bottom: 2px;
  }
  .macro-pill-body {
    font-size: 14px;
    color: var(--text-main);
  }
  .macro-pill-meta {
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 3px;
  }
  .disclaimer-note {
    margin-top: 14px;
    padding-top: 8px;
    border-top: 1px dashed var(--accent-border);
    font-size: 12px;
    color: var(--text-muted);
    line-height: 1.4;
  }
</style>
        """,
        unsafe_allow_html=True,
    )


def status_color(level: str) -> str:
    normalized = (level or "").lower()
    if normalized in {"red", "high", "risk_on_red"}:
        return "#a6635d"
    if normalized in {"yellow", "medium", "warning"}:
        return "#b08d57"
    return "#557a6d"
