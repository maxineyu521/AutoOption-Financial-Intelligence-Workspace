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
  .stButton > button[kind="primary"] * {
    color: #f4f7fb !important;
  }
  .stButton > button[kind="primary"]:hover {
    background: #1f3d62 !important;
    border-color: #1f3d62 !important;
  }
  div[data-baseweb="tag"], span[data-baseweb="tag"] {
    background: #d8d2c4 !important;
    border: 1px solid #bdb4a4 !important;
    border-radius: 8px !important;
  }
  div[data-baseweb="tag"] span, span[data-baseweb="tag"] span {
    color: var(--text-main) !important;
    font-weight: 700 !important;
  }
  div[data-baseweb="tag"] svg, span[data-baseweb="tag"] svg {
    color: var(--text-muted) !important;
    fill: var(--text-muted) !important;
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
  .app-header {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background:
      linear-gradient(135deg, rgba(39, 76, 119, 0.12), rgba(85, 122, 109, 0.08)),
      var(--bg-card-soft);
    padding: 18px 20px;
    margin: 4px 0 12px 0;
  }
  .app-header h1 {
    font-size: 2.05rem !important;
    line-height: 1.15;
    margin: 2px 0 8px 0;
    color: var(--text-main) !important;
  }
  .app-header p {
    max-width: 980px;
    margin: 0;
    color: var(--text-muted) !important;
    font-size: 1.0rem !important;
  }
  .app-kicker {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text-muted);
    font-weight: 800;
  }
  .summary-highlight {
    border: 1px solid var(--accent-border);
    border-left: 5px solid var(--timeline-bar);
    border-radius: 8px;
    background: var(--bg-card-soft);
    padding: 16px 18px;
    margin: 4px 0 14px 0;
  }
  .summary-highlight-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .summary-highlight-meta span {
    border: 1px solid var(--accent-border);
    border-radius: 999px;
    padding: 4px 9px;
    background: rgba(39, 76, 119, 0.07);
    color: var(--text-muted) !important;
    font-size: 12px;
    font-weight: 700;
  }
  .summary-highlight-body {
    color: var(--text-main);
    font-size: 1.05rem;
    line-height: 1.6;
    max-width: 1080px;
  }
  .summary-question {
    border-bottom: 1px solid var(--accent-border);
    padding-bottom: 12px;
    margin-bottom: 12px;
  }
  .summary-question-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text-muted);
    font-weight: 800;
    margin-bottom: 6px;
  }
  .summary-question-text {
    color: var(--text-main);
    font-size: 1.18rem;
    line-height: 1.45;
    font-weight: 780;
    max-width: 1100px;
  }
  .guide-hero {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background:
      linear-gradient(135deg, rgba(39,76,119,0.14), rgba(85,122,109,0.10)),
      var(--bg-card-soft);
    padding: 16px 18px;
    margin: 10px 0 12px 0;
  }
  .guide-kicker {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: var(--text-muted);
    font-weight: 800;
    margin-bottom: 5px;
  }
  .guide-title {
    font-size: 1.35rem;
    line-height: 1.2;
    color: var(--text-main);
    font-weight: 800;
    margin-bottom: 6px;
  }
  .guide-copy {
    color: var(--text-muted);
    font-size: 15px;
    line-height: 1.45;
    max-width: 980px;
  }
  .question-mode-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text-muted);
    font-weight: 850;
    margin: 10px 0 10px 0;
  }
  .mode-card {
    border: 1px solid var(--accent-border);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 8px;
    background: var(--bg-card-soft);
    min-height: 124px;
  }
  .mode-card.active {
    border-color: rgba(39,76,119,0.35);
    background:
      linear-gradient(135deg, rgba(39,76,119,0.12), rgba(85,122,109,0.08)),
      var(--bg-card-soft);
  }
  .mode-card.inactive {
    background: var(--bg-card);
  }
  .mode-card-kicker {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-muted);
    font-weight: 800;
    margin-bottom: 6px;
  }
  .mode-card-title {
    font-size: 1.05rem;
    line-height: 1.25;
    color: var(--text-main);
    font-weight: 850;
    margin-bottom: 6px;
  }
  .mode-card-copy {
    color: var(--text-muted);
    font-size: 14px;
    line-height: 1.45;
  }
  .sidebar-section {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background: var(--bg-card-soft);
    padding: 12px 12px;
    margin: 8px 0 14px 0;
  }
  .sidebar-section-title {
    color: var(--text-main) !important;
    font-size: 1.0rem;
    font-weight: 850;
    line-height: 1.2;
    letter-spacing: .01em;
    margin-bottom: 4px;
  }
  .sidebar-section-subtitle {
    color: var(--text-muted) !important;
    font-size: 12px;
    line-height: 1.35;
    margin-bottom: 10px;
  }
  .market-row {
    border-top: 1px solid var(--accent-border);
    padding: 10px 0;
  }
  .market-row:first-of-type {
    border-top: 0;
  }
  .market-row-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 5px;
  }
  .asset-tag {
    border-radius: 999px;
    background: rgba(39, 76, 119, 0.10);
    border: 1px solid rgba(39, 76, 119, 0.22);
    color: #274c77 !important;
    font-size: 10px;
    font-weight: 850;
    line-height: 1;
    padding: 4px 7px;
    text-transform: uppercase;
  }
  section[data-testid="stSidebar"] .asset-tag {
    color: #274c77 !important;
  }
  .market-symbol {
    color: var(--text-muted) !important;
    font-size: 11px;
    font-weight: 800;
  }
  section[data-testid="stSidebar"] .market-symbol {
    color: var(--text-muted) !important;
  }
  .market-name {
    color: var(--text-main) !important;
    font-size: 14px;
    font-weight: 800;
    line-height: 1.3;
    margin-bottom: 5px;
  }
  section[data-testid="stSidebar"] .market-name {
    color: var(--text-main) !important;
  }
  .market-values {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 5px;
  }
  .market-value {
    color: var(--text-main) !important;
    font-size: 20px;
    font-weight: 850;
    line-height: 1.1;
  }
  section[data-testid="stSidebar"] .market-value {
    color: var(--text-main) !important;
  }
  .market-unit {
    color: var(--text-muted) !important;
    font-size: 12px;
    font-weight: 700;
  }
  section[data-testid="stSidebar"] .market-unit,
  section[data-testid="stSidebar"] .market-date,
  section[data-testid="stSidebar"] .sidebar-section-subtitle {
    color: var(--text-muted) !important;
  }
  .change-positive, section[data-testid="stSidebar"] .change-positive {
    color: #557a6d !important;
    background: rgba(85, 122, 109, 0.13);
    border: 1px solid rgba(85, 122, 109, 0.28);
    border-radius: 999px;
    padding: 2px 7px;
    font-size: 12px;
    font-weight: 850;
  }
  .change-negative, section[data-testid="stSidebar"] .change-negative {
    color: #a6635d !important;
    background: rgba(166, 99, 93, 0.12);
    border: 1px solid rgba(166, 99, 93, 0.28);
    border-radius: 999px;
    padding: 2px 7px;
    font-size: 12px;
    font-weight: 850;
  }
  .change-neutral, section[data-testid="stSidebar"] .change-neutral {
    color: #8f7b52 !important;
    background: rgba(176, 141, 87, 0.12);
    border: 1px solid rgba(176, 141, 87, 0.28);
    border-radius: 999px;
    padding: 2px 7px;
    font-size: 12px;
    font-weight: 850;
  }
  .market-date {
    color: var(--text-muted) !important;
    font-size: 12px;
    font-style: italic;
    margin-top: 5px;
  }
  .gpr-current-title {
    color: var(--text-main) !important;
    font-size: 1.2rem;
    font-weight: 850;
    line-height: 1.3;
    margin: 2px 0 8px 0;
  }
  .gpr-hero {
    border-top: 1px solid var(--accent-border);
    padding-top: 10px;
  }
  .gpr-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 8px;
  }
  .gpr-history-chip-row {
    margin: 2px 0 8px 0;
  }
  .gpr-inline-chip {
    display: inline-block;
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 850;
    border: 1px solid var(--accent-border);
    background: var(--bg-card);
    color: var(--text-muted) !important;
  }
  .gpr-inline-chip.positive {
    border-color: rgba(85,122,109,0.45);
    background: rgba(85,122,109,0.14);
    color: #557a6d !important;
  }
  .gpr-inline-chip.negative {
    border-color: rgba(166,99,93,0.45);
    background: rgba(166,99,93,0.13);
    color: #a6635d !important;
  }
  .gpr-inline-chip.neutral {
    border-color: rgba(176,141,87,0.40);
    background: rgba(176,141,87,0.13);
    color: #8f7b52 !important;
  }
  .gpr-block {
    border-top: 1px solid var(--accent-border);
    padding: 10px 0;
  }
  .gpr-history-block {
    padding: 9px 0;
  }
  .gpr-block-label {
    color: #274c77 !important;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .05em;
    font-weight: 850;
    margin-bottom: 6px;
  }
  .gpr-block-body {
    color: var(--text-main) !important;
    font-size: 14px;
    line-height: 1.58;
  }
  .gpr-emphasis {
    color: #274c77 !important;
    font-weight: 850;
  }
  .gpr-asset {
    color: #8f7b52 !important;
    font-weight: 800;
  }
  .query-builder-shell {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background:
      linear-gradient(135deg, rgba(85,122,109,0.10), rgba(39,76,119,0.06)),
      var(--bg-card-soft);
    padding: 12px 14px;
    margin: 8px 0 10px 0;
  }
  .query-builder-shell.query-builder-narrative {
    background:
      linear-gradient(135deg, rgba(176,141,87,0.16), rgba(39,76,119,0.05)),
      var(--bg-card-soft);
    border-color: rgba(176,141,87,0.42);
  }
  .query-builder-shell.query-builder-data {
    background:
      linear-gradient(135deg, rgba(85,122,109,0.14), rgba(39,76,119,0.08)),
      var(--bg-card-soft);
    border-color: rgba(85,122,109,0.38);
  }
  .query-builder-title {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-muted);
    font-weight: 800;
  }
  .query-builder-mode {
    color: var(--text-main);
    font-size: 1.05rem;
    font-weight: 850;
    margin-top: 5px;
  }
  .query-builder-note {
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.4;
    margin-top: 4px;
  }
  .query-preview {
    border: 1px solid var(--accent-border);
    border-left: 4px solid var(--green-soft);
    border-radius: 8px;
    background: var(--bg-card);
    color: var(--text-main);
    padding: 10px 12px;
    margin: 8px 0 10px 0;
    font-size: 14px;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }
  .query-preview.query-builder-narrative {
    border-left-color: var(--yellow-soft);
    background: rgba(176,141,87,0.08);
  }
  .query-preview.query-builder-data {
    border-left-color: var(--green-soft);
    background: rgba(85,122,109,0.08);
  }
  .gpr-point {
    border-top: 1px solid var(--accent-border);
    padding: 10px 0;
    color: var(--text-main) !important;
    line-height: 1.45;
    font-size: 14px;
  }
  .gpr-label {
    color: #274c77 !important;
    font-weight: 850;
  }
  .gpr-number {
    color: #8f7b52 !important;
    background: rgba(176,141,87,0.14);
    border-radius: 6px;
    padding: 1px 5px;
    font-weight: 850;
  }
  .event-evidence-card {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background:
      linear-gradient(135deg, rgba(39,76,119,0.08), rgba(176,141,87,0.06)),
      var(--bg-card-soft);
    padding: 13px 14px;
    margin: 10px 0;
  }
  .event-evidence-head {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: center;
    margin-bottom: 6px;
  }
  .event-source-tag {
    border-radius: 999px;
    background: rgba(176,141,87,0.14);
    border: 1px solid rgba(176,141,87,0.32);
    color: #8f7b52 !important;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 850;
  }
  .event-score {
    color: var(--text-muted) !important;
    font-size: 12px;
    font-weight: 750;
  }
  .event-title {
    color: var(--text-main);
    font-size: 1.05rem;
    font-weight: 850;
    line-height: 1.3;
    margin-bottom: 4px;
  }
  .event-meta {
    color: var(--text-muted);
    font-size: 12px;
    font-weight: 700;
    margin-bottom: 8px;
  }
  .event-body {
    color: var(--text-main);
    font-size: 14px;
    line-height: 1.55;
  }
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
  .surface-title {
    color: #274c77;
    font-weight: 850;
    letter-spacing: 0;
  }
  .surface-title-main {
    font-size: 1.45rem;
    line-height: 1.2;
    margin: 14px 0 10px 0;
    color: var(--text-main);
  }
  .surface-title-minor {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-muted);
    margin-bottom: 6px;
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
    display: grid;
    gap: 14px;
    margin: 10px 0 10px 0;
    max-width: 100%;
    overflow-x: hidden;
    box-sizing: border-box;
  }
  .timeline-row {
    display: grid;
    gap: 8px;
    margin: 0;
    min-width: 0;
    max-width: 100%;
  }
  .timeline-row-head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: flex-end;
    gap: 14px;
    min-width: 0;
  }
  .timeline-row-bar {
    display: block;
    min-width: 0;
    max-width: 100%;
  }
  .timeline-label {
    font-size: 12px;
    color: #274c77;
    font-weight: 850;
    letter-spacing: .01em;
  }
  .timeline-track {
    position: relative;
    height: 12px;
    background: rgba(39,76,119,0.12);
    border-radius: 999px;
    max-width: 100%;
    overflow: hidden;
  }
  .timeline-bar {
    position: absolute;
    height: 12px;
    border-radius: 999px;
    background: linear-gradient(90deg, #8f7b52, #b08d57);
  }
  .timeline-note {
    font-size: 12px;
    color: var(--text-muted);
    text-align: right;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }
  .timeline-note-inline {
    flex: 0 0 auto;
    max-width: 240px;
  }
  .timeline-section-title {
    color: var(--text-main);
    font-size: 1.15rem;
    line-height: 1.25;
    font-weight: 850;
    margin: 14px 0 4px 0;
  }
  .timeline-section-copy {
    color: var(--text-muted);
    font-size: 14px;
    line-height: 1.45;
    margin-bottom: 8px;
  }
  .timeline-coverage {
    color: var(--text-main);
    font-size: 14px;
    margin: 8px 0 8px 0;
  }
  .timeline-callout {
    color: #8f7b52;
    background: rgba(176,141,87,0.10);
    border-left: 3px solid rgba(176,141,87,0.55);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 13px;
    line-height: 1.4;
    margin: 6px 0 8px 0;
  }
  .timeline-callout-good {
    color: #557a6d;
    background: rgba(85,122,109,0.10);
    border-left-color: rgba(85,122,109,0.55);
  }
  .institutional-card {
    border: 1px solid var(--accent-border);
    border-radius: 12px;
    background: var(--bg-card-soft);
    padding: 14px 16px;
    margin-bottom: 10px;
  }
  .report-surface-title {
    color: var(--text-main);
    font-size: 1.45rem;
    line-height: 1.2;
    font-weight: 850;
    margin: 14px 0 10px 0;
  }
  .institutional-title {
    font-size: 13px;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: var(--text-muted);
    margin-bottom: 6px;
    font-weight: 700;
  }
  .market-read-section {
    border-left: 4px solid rgba(39,76,119,0.32);
    padding: 2px 0 12px 14px;
    margin: 8px 0 14px 0;
  }
  .market-read-section.market-read-status {
    border-left-color: rgba(176,141,87,0.62);
    margin-top: 16px;
  }
  .market-read-title {
    color: var(--text-main) !important;
    font-size: 1.02rem;
    line-height: 1.3;
    font-weight: 900;
    margin-bottom: 6px;
  }
  .market-read-body {
    color: var(--text-main) !important;
    font-size: 1rem;
    line-height: 1.6;
    font-weight: 520;
    max-width: 1080px;
  }
  .structured-part-title {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-muted) !important;
    margin: 18px 0 8px 0;
    font-weight: 850;
  }
  .structured-part-title::after {
    content: none;
  }
  .structured-signal-lines {
    display: grid;
    gap: 6px;
    margin: 2px 0 14px 0;
  }
  .structured-signal-lines div {
    color: var(--text-main) !important;
    font-size: 0.98rem;
    line-height: 1.45;
  }
  .structured-signal-lines span {
    display: inline-block;
    min-width: 170px;
    color: #274c77 !important;
    font-weight: 850;
  }
  .metric-row {
    border: 1px solid var(--accent-border);
    border-radius: 10px;
    background: var(--bg-card-soft);
    padding: 10px 12px;
    margin-bottom: 8px;
  }
  .metric-row-compact {
    min-height: 84px;
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
  .subsection-kicker {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text-muted);
    font-weight: 850;
    margin: 10px 0 8px 0;
  }
  .evidence-block {
    border: 1px solid var(--accent-border);
    border-radius: 10px;
    background: var(--bg-card-soft);
    padding: 12px 14px;
    margin: 0 0 10px 0;
  }
  .evidence-block-posture {
    background:
      linear-gradient(135deg, rgba(39,76,119,0.12), rgba(85,122,109,0.08)),
      var(--bg-card-soft);
  }
  .evidence-block-vol {
    background:
      linear-gradient(135deg, rgba(85,122,109,0.10), rgba(176,141,87,0.06)),
      var(--bg-card-soft);
  }
  .evidence-block-trace {
    background:
      linear-gradient(135deg, rgba(176,141,87,0.10), rgba(39,76,119,0.05)),
      var(--bg-card-soft);
  }
  .evidence-block-kicker {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text-muted);
    font-weight: 850;
    margin-bottom: 6px;
  }
  .evidence-block-title {
    font-size: 1.02rem;
    letter-spacing: 0;
    color: var(--text-main);
    font-weight: 850;
    margin-bottom: 5px;
  }
  .evidence-block-copy {
    color: var(--text-muted);
    font-size: 13px;
    line-height: 1.45;
    margin-bottom: 10px;
  }
  .evidence-kv-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    padding: 8px 0;
    border-top: 1px solid var(--accent-border);
  }
  .evidence-kv-row:first-of-type {
    border-top: 0;
    padding-top: 0;
  }
  .evidence-kv-label {
    color: var(--text-muted);
    font-size: 13px;
    font-weight: 700;
  }
  .evidence-kv-value {
    color: var(--text-main);
    font-size: 14px;
    font-weight: 800;
    text-align: right;
  }
  .evidence-kv-strong {
    color: #274c77 !important;
  }
  .evidence-kv-muted {
    color: var(--text-muted) !important;
    font-weight: 700;
  }
  .evidence-pill-neutral,
  .evidence-pill-caution,
  .evidence-pill-supportive {
    display: inline-block;
    border-radius: 999px;
    padding: 4px 10px;
    border: 1px solid var(--accent-border);
  }
  .evidence-pill-neutral {
    color: #8f7b52 !important;
    background: rgba(176,141,87,0.10);
    border-color: rgba(176,141,87,0.30);
  }
  .evidence-pill-caution {
    color: #a6635d !important;
    background: rgba(166,99,93,0.12);
    border-color: rgba(166,99,93,0.30);
  }
  .evidence-pill-supportive {
    color: #557a6d !important;
    background: rgba(85,122,109,0.12);
    border-color: rgba(85,122,109,0.30);
  }
  .reasoning-trace-list {
    display: grid;
    gap: 8px;
  }
  .reasoning-trace-line {
    display: grid;
    grid-template-columns: minmax(180px, auto) 34px minmax(220px, 1fr);
    align-items: center;
    gap: 12px;
    color: var(--text-main);
    font-size: 14px;
    line-height: 1.45;
    border-left: 3px solid rgba(176,141,87,0.35);
    background: rgba(255,255,255,0.28);
    border-radius: 6px;
    padding: 9px 11px;
  }
  .reasoning-trace-signal {
    color: #274c77 !important;
    font-weight: 850;
    font-size: 15px;
  }
  .reasoning-trace-arrow-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    color: #8f7b52 !important;
  }
  .reasoning-trace-arrow-icon svg {
    width: 18px;
    height: 18px;
    display: block;
  }
  .reasoning-trace-outcome {
    color: var(--text-main);
    font-size: 15px;
    font-weight: 750;
  }
  .liquidity-empty-state {
    border: 1px dashed var(--accent-border);
    border-radius: 10px;
    background: rgba(39,76,119,0.04);
    padding: 14px 15px;
    margin-bottom: 8px;
  }
  .liquidity-empty-badge {
    display: inline-block;
    border-radius: 999px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 850;
    color: #8f7b52 !important;
    border: 1px solid rgba(176,141,87,0.34);
    background: rgba(176,141,87,0.12);
    margin-bottom: 8px;
  }
  .liquidity-empty-copy {
    color: var(--text-main);
    font-size: 14px;
    line-height: 1.5;
  }
  .macro-infobar {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background:
      linear-gradient(135deg, rgba(176,141,87,0.10), rgba(85,122,109,0.06)),
      var(--bg-card-soft);
    padding: 12px 12px 10px 12px;
    margin: 6px 0 12px 0;
    overflow: hidden;
  }
  .macro-infobar-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 9px;
  }
  .macro-infobar-title {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .04em;
    color: var(--text-muted);
    font-weight: 700;
  }
  .macro-infobar-subtitle {
    font-size: 13px;
    color: var(--text-muted);
    margin-top: 2px;
  }
  .macro-indicator-list {
    display: grid;
    gap: 8px;
  }
  .macro-indicator-row {
    display: grid;
    grid-template-columns: minmax(280px, 1.6fr) minmax(150px, 0.8fr) minmax(220px, 1fr);
    gap: 14px;
    align-items: center;
    padding: 12px 14px;
    border-top: 1px solid var(--accent-border);
  }
  .macro-indicator-title {
    color: var(--text-main);
    font-size: 1.05rem;
    font-weight: 800;
    line-height: 1.3;
  }
  .macro-indicator-date {
    color: var(--text-muted);
    font-size: 12px;
    margin-top: 4px;
  }
  .macro-indicator-value {
    color: var(--text-main);
    font-size: 1.55rem;
    font-weight: 850;
    line-height: 1.15;
  }
  .macro-indicator-momentum {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 7px;
  }
  .momentum-pill {
    border-radius: 999px;
    padding: 5px 9px;
    font-size: 12px;
    font-weight: 800;
    border: 1px solid var(--accent-border);
    background: var(--bg-card);
    color: var(--text-muted) !important;
  }
  .momentum-pill.positive {
    border-color: rgba(85,122,109,0.45);
    background: rgba(85,122,109,0.14);
    color: #557a6d !important;
  }
  .momentum-pill.negative {
    border-color: rgba(166,99,93,0.45);
    background: rgba(166,99,93,0.13);
    color: #a6635d !important;
  }
  .momentum-pill.neutral {
    border-color: rgba(176,141,87,0.40);
    background: rgba(176,141,87,0.13);
    color: #8f7b52 !important;
  }
  .macro-scroll-hint {
    border: 1px solid var(--accent-border);
    border-radius: 999px;
    color: var(--text-muted);
    background: var(--bg-card);
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 700;
    white-space: nowrap;
  }
  .macro-pill-track {
    display: flex;
    gap: 10px;
    overflow-x: auto;
    padding: 2px 0 7px 0;
    scroll-snap-type: x proximity;
  }
  .macro-pill-track::-webkit-scrollbar {
    height: 8px;
  }
  .macro-pill-track::-webkit-scrollbar-thumb {
    background: var(--accent-border);
    border-radius: 999px;
  }
  .macro-pill {
    border: 1px solid var(--accent-border);
    border-radius: 8px;
    background:
      linear-gradient(180deg, rgba(255,255,255,0.30), rgba(39,76,119,0.05)),
      var(--bg-card);
    box-shadow: 0 1px 0 rgba(23,43,77,0.04);
    padding: 11px 12px;
    min-width: 245px;
    max-width: 320px;
    flex: 0 0 280px;
    scroll-snap-align: start;
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
    line-height: 1.35;
    min-height: 38px;
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
  .chat-card, .risk-card, .timeline-wrap, .institutional-card,
  .metric-row, .metric-highlight, .macro-infobar, .macro-pill,
  .decision-strip {
    border-radius: 8px;
  }
  .stream-panel {
    display: grid;
    gap: 6px;
    margin-top: 6px;
  }
  .stream-step {
    border-left: 3px solid var(--timeline-bar);
    background: var(--bg-card-soft);
    border-radius: 6px;
    padding: 7px 10px;
    color: var(--text-main);
    font-size: 13px;
    line-height: 1.35;
  }
  .metric-highlight {
    border: 1px solid var(--accent-border);
    background: var(--bg-card);
    padding: 12px 14px;
    min-height: 96px;
    margin-bottom: 10px;
  }
  .decision-strip {
    display: grid;
    grid-template-columns: minmax(130px, 0.8fr) minmax(120px, 0.6fr) minmax(260px, 2.4fr);
    gap: 10px;
    border: 1px solid var(--accent-border);
    background: var(--bg-card-soft);
    padding: 12px 14px;
    margin: 8px 0 14px 0;
  }
  .decision-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0;
    color: var(--text-muted);
    font-weight: 700;
    margin-bottom: 4px;
  }
  .decision-value {
    font-size: 20px;
    font-weight: 750;
    color: var(--text-main);
  }
  .decision-note {
    font-size: 14px;
    color: var(--text-main);
    line-height: 1.45;
  }
  @media (max-width: 900px) {
    .app-header {
      padding: 14px 15px;
    }
    .app-header h1 {
      font-size: 1.65rem !important;
    }
    .summary-highlight {
      padding: 13px 14px;
    }
    .macro-indicator-row {
      grid-template-columns: 1fr;
      gap: 7px;
    }
    .macro-indicator-momentum {
      justify-content: flex-start;
    }
    .macro-pill {
      min-width: 220px;
      flex-basis: 240px;
    }
    .decision-strip {
      grid-template-columns: 1fr;
    }
    .timeline-row-head {
      flex-direction: column;
      align-items: flex-start;
    }
    .timeline-note {
      text-align: left;
    }
    .timeline-note-inline {
      max-width: none;
    }
    .evidence-kv-row {
      flex-direction: column;
      align-items: flex-start;
      gap: 6px;
    }
    .evidence-kv-value {
      text-align: left;
    }
    .reasoning-trace-line {
      grid-template-columns: 1fr;
      gap: 8px;
    }
    .reasoning-trace-arrow-icon {
      justify-content: flex-start;
      width: auto;
      height: auto;
    }
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
