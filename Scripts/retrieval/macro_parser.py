"""
Macro-context parsing utilities.

Background
----------
The Analyst prompt mandates a "Macro Regime Snapshot" citing VIX / GSPC /
DXY / rates, but the Checker only recognises Silver ``lineage_anchors``
and Gold ``bronze_ref``. Every macro number the Analyst cites was
therefore flagged *Fatal*, burning all three revisions and forcing
every run into degraded mode with confidence 0.3.

The fix is architectural: promote the macro snapshot to a first-class
Silver citation source by parsing ``Data/Agent_Context/latest_macro_context.md``
into ``(values, lineage_anchors)`` and merging them into
``silver_context`` before the Analyst draws its draft.

Anchor naming uses a stable ``MACRO_<CODE>_<anchor_date>`` convention
that the Finalizer already routes to the "Macro Data" UI category
(see ``finalizer.py:_collect_evidence_pool``).

Why this lives in its own module
--------------------------------
* **Separation of concerns.** ``master_retriever.py`` is the *orchestrator*;
  regex tables for a specific markdown format belong next to the data,
  not next to the orchestration logic.
* **Testability.** ``Scripts/tests`` can target this module directly
  without spinning up the Qdrant / SQL / LLM stack that
  ``master_retriever.py`` drags in.
* **Schema evolution.** When the macro snapshot markdown changes shape,
  the diff is localised to ~200 lines here — ``MasterRetriever`` is
  untouched.

The ticker-extraction helpers (``TICKER_RE`` / ``TICKER_STOPWORDS``)
share the same "markdown → retrieval metadata" concern and are
colocated for symmetry; the HyDE novel-ticker extractor in
``master_retriever.py`` imports them from here.

Public surface
--------------
* ``parse_macro_snapshot(markdown) -> (values, snapshot_date)``
* ``build_macro_silver_patch(macro_md, anchor_date) -> {values, lineage_anchors}``
* ``TICKER_RE``, ``TICKER_STOPWORDS`` (for HyDE novel-ticker detection)
* ``MACRO_LINE_PATTERNS``, ``MACRO_GENERATED_RE`` (for downstream tests)
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "TICKER_RE",
    "TICKER_STOPWORDS",
    "MACRO_LINE_PATTERNS",
    "MACRO_GENERATED_RE",
    "parse_macro_snapshot",
    "build_macro_silver_patch",
]


# ---------------------------------------------------------------------------
# Ticker extraction (used by the HyDE "novel ticker" back-injection path)
# ---------------------------------------------------------------------------

# Regex for ticker-shaped tokens. We deliberately accept 1–5 upper-case
# letters with optional leading "$" (Bloomberg style). Multi-word /
# punctuation tickers (e.g. ^GSPC, BRK.B) are NOT captured here — they
# require a richer tokenizer and almost never appear verbatim in HyDE.
TICKER_RE: re.Pattern = re.compile(r"(?<![A-Z])\$?([A-Z]{1,5})(?![A-Z])")

# Stop-words that collide with ticker-shape tokens and must NEVER trigger
# SQL. This is a belt-and-suspenders filter BEFORE the ontology whitelist:
# even if someone accidentally added "IT" to the allowed_tickers pool, we
# still do not want to SQL-query "IT" because of one HyDE sentence.
TICKER_STOPWORDS: set = {
    # English glue words that regex-match as upper-case tokens
    "A", "I", "IS", "IT", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF",
    "IN", "ON", "OR", "SO", "TO", "US", "WE", "THE", "AND", "BUT", "FOR",
    "ARE", "HAS", "HAD", "HOW", "NOT", "OUT", "ONE", "TWO", "ITS", "NEW",
    "WILL", "WITH", "WERE", "THIS", "THAT", "FROM", "INTO", "OVER",
    "SUCH", "THAN", "ALSO", "BEEN", "WHEN", "WHILE", "WHERE",
    # Finance-specific non-ticker acronyms frequently in HyDE prose
    "IV", "PCR", "DTE", "VIX", "GDP", "CPI", "PPI", "NFP", "FOMC",
    "ETF", "IPO", "SEC", "USD", "EUR", "JPY", "OTM", "ITM", "ATM",
    "MOM", "YOY", "QOQ", "EPS", "GPR", "CEO", "CFO", "SP", "BID", "ASK",
}


# ---------------------------------------------------------------------------
# Macro markdown parser
# ---------------------------------------------------------------------------

# Lines we know how to parse. Each tuple is (label_regex, metric_key, has_pct).
# `label_regex` matches the bullet line; group(1) is the value, group(2)
# (when present) is the % change. Patterns are permissive on whitespace,
# parenthesised ticker codes, and optional trailing "Points / USD / % / Index".
MACRO_LINE_PATTERNS: List[Tuple[re.Pattern, str, bool]] = [
    (re.compile(
        r"S&P\s*500.*?:\s*\*?\*?([\d.,]+)\s*Points.*?Change:\s*\*?\*?([+-]?[\d.]+)%",
        re.IGNORECASE,
    ), "GSPC", True),
    (re.compile(
        r"NASDAQ.*?:\s*\*?\*?([\d.,]+)\s*Points.*?Change:\s*\*?\*?([+-]?[\d.]+)%",
        re.IGNORECASE,
    ), "IXIC", True),
    (re.compile(
        r"VIX.*?:\s*\*?\*?([\d.,]+)\s*Points.*?Change:\s*\*?\*?([+-]?[\d.]+)%",
        re.IGNORECASE,
    ), "VIX", True),
    (re.compile(
        r"US\s*Dollar\s*Index.*?:\s*\*?\*?([\d.,]+)\s*Points(?:.*?Change:\s*\*?\*?([+-]?[\d.]+)%)?",
        re.IGNORECASE,
    ), "DXY", True),
    (re.compile(
        r"Gold\s*ETF\s*\(GLD\).*?:\s*\*?\*?([\d.,]+)\s*USD(?:.*?Change:\s*\*?\*?([+-]?[\d.]+)%)?",
        re.IGNORECASE,
    ), "GLD_SPOT", True),
    (re.compile(
        r"Silver\s*ETF\s*\(SLV\).*?:\s*\*?\*?([\d.,]+)\s*USD(?:.*?Change:\s*\*?\*?([+-]?[\d.]+)%)?",
        re.IGNORECASE,
    ), "SLV_SPOT", True),
    (re.compile(
        r"Effective\s*Federal\s*Funds\s*Rate.*?:\s*\*?\*?([\d.]+)\s*%",
        re.IGNORECASE,
    ), "FEDFUNDS", False),
    (re.compile(
        r"CPI.*?:\s*\*?\*?([\d.,]+)\s*Index.*?YoY:\s*\*?\*?([+-]?[\d.]+)%",
        re.IGNORECASE,
    ), "CPIAUCSL", True),
    (re.compile(
        r"Unemployment\s*Rate.*?:\s*\*?\*?([\d.]+)\s*%",
        re.IGNORECASE,
    ), "UNRATE", False),
]

# Extract the "Generated on: YYYY-MM-DD" header. Falls back to the
# caller-provided anchor when absent.
MACRO_GENERATED_RE: re.Pattern = re.compile(
    r"Generated on:\s*\*{0,2}\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def parse_macro_snapshot(
    markdown: str,
) -> Tuple[Dict[str, float], Optional[str]]:
    """Parse ``latest_macro_context.md`` into silver values.

    Returns
    -------
    values
        ``{metric_key: float}`` mapping. Keys follow the
        ``{CODE}_value`` / ``{CODE}_change_pct`` convention so the
        Analyst's numeric audit can match a draft that says "VIX at
        18.25" against ``VIX_value=18.25``. Keys missing from the
        markdown are omitted — this lets the Checker still reject
        fabricated metrics.
    snapshot_date
        The ISO date from the "Generated on" header or ``None``.
        Callers use it as the macro anchor when present (more precise
        than the SQL-derived ``options_daily`` anchor).
    """
    values: Dict[str, float] = {}
    if not markdown:
        return values, None

    gen_match = MACRO_GENERATED_RE.search(markdown)
    snapshot_date = gen_match.group(1) if gen_match else None

    for pattern, key, has_pct in MACRO_LINE_PATTERNS:
        m = pattern.search(markdown)
        if not m:
            continue
        try:
            val = float(m.group(1).replace(",", ""))
        except (ValueError, IndexError):
            continue
        values[f"{key}_value"] = val
        if has_pct:
            try:
                pct_raw = m.group(2)
                if pct_raw is not None:
                    values[f"{key}_change_pct"] = float(pct_raw)
            except (ValueError, IndexError):
                pass

    return values, snapshot_date


def build_macro_silver_patch(
    macro_md: Optional[str],
    anchor_date: date,
) -> Dict[str, Any]:
    """Produce the Silver patch dict ``{values, lineage_anchors}`` for macro.

    Anchors follow the ``MACRO_<CODE>_<date>`` convention — the
    Finalizer already maps a ``MACRO_*`` prefix to the "Macro Data"
    evidence category. Returns an empty dict when the markdown is
    absent or unparseable so :meth:`MasterRetriever.retrieve` can merge
    unconditionally.
    """
    values, snapshot_date = parse_macro_snapshot(macro_md or "")
    if not values:
        return {"values": {}, "lineage_anchors": []}

    effective_date = snapshot_date or anchor_date.isoformat()
    # Strip the metric-suffix so `VIX_value` / `VIX_change_pct` both map
    # to a single `VIX` code. One anchor per code keeps the lineage list
    # short and easy for the LLM to cite.
    distinct_codes: set = set()
    for k in values.keys():
        if k.endswith("_value"):
            distinct_codes.add(k[: -len("_value")])
        elif k.endswith("_change_pct"):
            distinct_codes.add(k[: -len("_change_pct")])
    lineage = [f"MACRO_{code}_{effective_date}" for code in sorted(distinct_codes)]

    return {"values": values, "lineage_anchors": lineage}
