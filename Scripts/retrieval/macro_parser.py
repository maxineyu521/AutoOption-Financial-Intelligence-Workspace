"""
Macro-context utilities with Silver-first macro patch generation.

Design intent
-------------
The macro markdown is an LLM readability layer, not the deterministic source
of truth. The authoritative numeric contract lives in
`Data/2_Silver_Processed/Macro_History/.../macro_snapshot_*.parquet`.

This module therefore serves two roles:
1. Ticker extraction helpers used by the retrieval stack.
2. A macro patch builder that promotes Silver macro values into
   `silver_context` so Checker/Analyst can consume them directly.

Primary path:
    Silver Macro_History parquet -> values + anchors + schema/status snapshot

Fallback path:
    latest_macro_context.md regex parsing (only when parquet is unavailable)

The status snapshot is intentionally persisted into `silver_context` so the
retriever/checker pipeline can retain a stable view of:
    - which parquet schema was used,
    - which symbols were materialized,
    - which observation date anchored the patch.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "TICKER_RE",
    "TICKER_STOPWORDS",
    "MACRO_LINE_PATTERNS",
    "MACRO_GENERATED_RE",
    "parse_macro_snapshot",
    "build_macro_silver_patch",
]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MACRO_HISTORY_ROOT = _PROJECT_ROOT / "Data" / "2_Silver_Processed" / "Macro_History"
_MACRO_HISTORY_GLOB = "macro_snapshot_*.parquet"
_MACRO_NUMERIC_FIELDS: tuple[str, ...] = (
    "value",
    "daily_change_pct",
    "mom_change_pct",
    "yoy_change_pct",
)
_MACRO_REQUIRED_COLUMNS: tuple[str, ...] = (
    "retrieval_date",
    "observation_date",
    "symbol",
    "name",
    "asset_class",
    "value",
    "unit",
    "frequency",
    "daily_change_pct",
    "mom_change_pct",
    "yoy_change_pct",
)
_SYMBOL_ALIAS_MAP: Dict[str, str] = {
    "^GSPC": "GSPC",
    "^IXIC": "IXIC",
    "^VIX": "VIX",
    "DX-Y.NYB": "DXY",
    "GLD": "GLD_SPOT",
    "SLV": "SLV_SPOT",
}


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

_STRUCTURED_MACRO_LINE_CODES: Dict[str, str] = {
    "(FEDFUNDS)": "FEDFUNDS",
    "(CPIAUCSL)": "CPIAUCSL",
    "(UNRATE)": "UNRATE",
}


def _safe_symbol(symbol: str) -> str:
    if not symbol:
        return "UNKNOWN"
    if symbol in _SYMBOL_ALIAS_MAP:
        return _SYMBOL_ALIAS_MAP[symbol]
    return symbol.lstrip("^").replace("-", "_").replace(".", "_").replace("/", "_")


def _latest_macro_parquet() -> Optional[Path]:
    if not _MACRO_HISTORY_ROOT.exists():
        return None
    files = sorted(_MACRO_HISTORY_ROOT.rglob(_MACRO_HISTORY_GLOB))
    return files[-1] if files else None


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    if isinstance(v, str):
        s = v.replace(",", "").rstrip("%").strip()
        if not s:
            return None
        try:
            return round(float(s), 6)
        except ValueError:
            return None
    return None


def _extract_inline_number(text: str) -> Optional[float]:
    if text is None:
        return None
    cleaned = str(text).replace("**", "").replace(",", "").strip()
    if not cleaned:
        return None

    token_chars: List[str] = []
    started = False
    for ch in cleaned:
        if ch in "+-0123456789.":
            token_chars.append(ch)
            started = True
            continue
        if started:
            break
    if not token_chars:
        return None
    try:
        return float("".join(token_chars))
    except ValueError:
        return None


def _parse_structured_macro_lines(markdown: str) -> Dict[str, float]:
    values: Dict[str, float] = {}
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if not line or "|" not in line:
            continue

        code = next((mapped for marker, mapped in _STRUCTURED_MACRO_LINE_CODES.items() if marker in line), None)
        if not code:
            continue

        normalized = line.replace("**", "")
        segments = [seg.strip() for seg in normalized.split("|") if seg.strip()]
        if not segments:
            continue

        head = segments[0]
        if ":" not in head:
            continue
        value = _extract_inline_number(head.split(":", 1)[1])
        if value is not None:
            values[f"{code}_value"] = value

        for segment in segments[1:]:
            lower = segment.lower()
            if ":" not in segment:
                continue
            rhs = segment.split(":", 1)[1]
            parsed = _extract_inline_number(rhs)
            if parsed is None:
                continue
            if lower.startswith("mom:"):
                values[f"{code}_mom_change_pct"] = parsed
            elif lower.startswith("yoy:"):
                values[f"{code}_yoy_change_pct"] = parsed

        compat_change = (
            values.get(f"{code}_mom_change_pct")
            if f"{code}_mom_change_pct" in values
            else values.get(f"{code}_yoy_change_pct")
        )
        if compat_change is not None:
            values[f"{code}_change_pct"] = compat_change

    return values


def _build_macro_patch_from_parquet(anchor_date: date) -> Dict[str, Any]:
    """Build the authoritative macro patch from the latest Silver parquet."""
    fp = _latest_macro_parquet()
    if fp is None:
        return {"values": {}, "lineage_anchors": [], "status": {}}

    try:
        import duckdb
    except ImportError as exc:
        logger.warning("Macro parquet patch unavailable — duckdb import failed: %s", exc)
        return {"values": {}, "lineage_anchors": [], "status": {}}

    sql_path = fp.as_posix()
    con = duckdb.connect()
    try:
        schema_rows = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{sql_path}')"
        ).fetchall()
        schema_types = {str(col): str(dtype) for col, dtype, *_ in schema_rows}
        present_columns = list(schema_types.keys())
        missing_columns = [c for c in _MACRO_REQUIRED_COLUMNS if c not in schema_types]

        latest_obs_row = con.execute(
            f"SELECT MAX(observation_date) FROM read_parquet('{sql_path}')"
        ).fetchone()
        latest_obs = str(latest_obs_row[0]) if latest_obs_row and latest_obs_row[0] is not None else anchor_date.isoformat()

        rows = con.execute(
            f"""
            SELECT retrieval_date, observation_date, symbol, name, asset_class,
                   value, unit, frequency, daily_change_pct, mom_change_pct, yoy_change_pct
            FROM read_parquet('{sql_path}')
            WHERE observation_date = ?
            ORDER BY symbol
            """,
            [latest_obs],
        ).fetchall()
    finally:
        con.close()

    values: Dict[str, float] = {}
    lineage: List[str] = []
    symbol_status: Dict[str, Dict[str, Any]] = {}

    for (
        retrieval_date,
        observation_date,
        symbol,
        name,
        asset_class,
        value,
        unit,
        frequency,
        daily_change_pct,
        mom_change_pct,
        yoy_change_pct,
    ) in rows:
        code = _safe_symbol(str(symbol))
        row_map = {
            "value": value,
            "daily_change_pct": daily_change_pct,
            "mom_change_pct": mom_change_pct,
            "yoy_change_pct": yoy_change_pct,
        }

        numeric_written = False
        for field_name, raw_val in row_map.items():
            fv = _to_float(raw_val)
            if fv is None:
                continue
            values[f"{code}_{field_name}"] = fv
            numeric_written = True

        # Backward-compatible alias used by existing prompts/checkers:
        # prefer MoM for monthly macro rows, otherwise daily change, then YoY.
        compat_change = (
            _to_float(mom_change_pct)
            if _to_float(mom_change_pct) is not None
            else _to_float(daily_change_pct)
            if _to_float(daily_change_pct) is not None
            else _to_float(yoy_change_pct)
        )
        if compat_change is not None:
            values[f"{code}_change_pct"] = compat_change
            numeric_written = True

        if numeric_written:
            anchor_id = f"MACRO_{code}_{observation_date or latest_obs}"
            lineage.append(anchor_id)

        symbol_status[code] = {
            "symbol": symbol,
            "display_name": name,
            "asset_class": asset_class,
            "unit": unit,
            "frequency": frequency,
            "retrieval_date": retrieval_date,
            "observation_date": observation_date,
        }

    schema_hash = hashlib.sha1(
        "|".join(f"{k}:{v}" for k, v in sorted(schema_types.items())).encode("utf-8")
    ).hexdigest()[:12]

    status = {
        "macro_source": "silver_macro_history",
        "macro_schema": {
            "source_file": str(fp),
            "schema_columns": present_columns,
            "schema_types": schema_types,
            "missing_required_columns": missing_columns,
            "required_columns": list(_MACRO_REQUIRED_COLUMNS),
            "schema_hash": schema_hash,
            "effective_observation_date": latest_obs,
            "requested_anchor_date": anchor_date.isoformat(),
            "row_count": len(rows),
        },
        "macro_symbols": symbol_status,
    }
    return {"values": values, "lineage_anchors": sorted(set(lineage)), "status": status}


def parse_macro_snapshot(
    markdown: str,
) -> Tuple[Dict[str, float], Optional[str]]:
    """Parse ``latest_macro_context.md`` into silver-like values.

    Returns
    -------
    values
        Legacy markdown-derived values. This path exists as a fallback only.
        The authoritative path is `_build_macro_patch_from_parquet()`.
    snapshot_date
        The ISO date from the "Generated on" header or ``None``.
        Used only by the fallback path.
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

    # Lagging macro rows carry MoM / YoY semantics that the legacy regex table
    # does not fully promote into the Silver contract. Parse those lines once
    # into explicit structured fields so Checker/Analyst can cite them without
    # relying on markdown-only visibility.
    values.update(_parse_structured_macro_lines(markdown))

    return values, snapshot_date


def build_macro_silver_patch(
    macro_md: Optional[str],
    anchor_date: date,
) -> Dict[str, Any]:
    """Produce the Silver macro patch.

    Primary source:
        latest Silver Macro_History parquet

    Fallback source:
        `latest_macro_context.md`

    Returned shape:
        {
          "values": {...},
          "lineage_anchors": [...],
          "status": {...}
        }
    """
    parquet_patch = _build_macro_patch_from_parquet(anchor_date)
    if parquet_patch.get("values"):
        return parquet_patch

    values, snapshot_date = parse_macro_snapshot(macro_md or "")
    if not values:
        return {"values": {}, "lineage_anchors": [], "status": {}}

    effective_date = snapshot_date or anchor_date.isoformat()
    distinct_codes: set = set()
    for k in values.keys():
        if k.endswith("_value"):
            distinct_codes.add(k[: -len("_value")])
        elif k.endswith("_change_pct"):
            distinct_codes.add(k[: -len("_change_pct")])
    lineage = [f"MACRO_{code}_{effective_date}" for code in sorted(distinct_codes)]
    return {
        "values": values,
        "lineage_anchors": lineage,
        "status": {
            "macro_source": "macro_markdown_fallback",
            "macro_schema": {
                "source_file": str(_PROJECT_ROOT / "Data" / "Agent_Context" / "latest_macro_context.md"),
                "schema_columns": ["markdown_fallback"],
                "schema_types": {"markdown_fallback": "regex_extracted"},
                "missing_required_columns": list(_MACRO_REQUIRED_COLUMNS),
                "required_columns": list(_MACRO_REQUIRED_COLUMNS),
                "schema_hash": "fallback",
                "effective_observation_date": effective_date,
                "requested_anchor_date": anchor_date.isoformat(),
                "row_count": len(distinct_codes),
            },
            "macro_symbols": {},
        },
    }
