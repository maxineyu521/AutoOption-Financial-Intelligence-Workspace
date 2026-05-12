"""
Scripts/Legacy_Baseline/generate_ground_truth.py

Generate in-scope English ground-truth test cases for the financial RAG stack.

Design goals:
  1. Keep every query inside the project scope documented in
     docs/modular_guide/User_Query_Guide.md.
  2. Reuse Scripts/core/financial_ontology.py as the contract for allowed
     sources, metrics, and ticker semantics.
  3. Respect the time model documented in
     docs/Data_source_docs/Time_Schema_Audit.md: explicit windows are preferred;
     missing time phrases default to six months, but current local data starts
     in April 2026, so truth answers must say when the effective evidence window
     is clamped by available partitions.
  4. Evaluate node reasoning, not only raw fact recall. Each ground truth answer
     contains an "Expected reasoning path" section for Analyst, Checker, Critic,
     and Finalizer.
  5. Score strict scope only. Soft context (for example weakly aligned news
     background) can appear in the truth answer as optional narrative support,
     but only strict in-scope evidence should be required for evaluation.

Output defaults:
  logs/ground_truth/<timestamp>/ground_truth_cases.json
  logs/ground_truth/<timestamp>/queries.jsonl
  Scripts/tests/router_e2e_ground_truth_queries.json  (with --update-tests)

Usage:
  python -m Scripts.Legacy_Baseline.generate_ground_truth
  python -m Scripts.Legacy_Baseline.generate_ground_truth --update-tests
  python -m Scripts.Legacy_Baseline.generate_ground_truth --out-dir logs/ground_truth/manual

Optional LLM polish:
  Set --llm-polish to ask GPT to improve wording only. Numeric facts and
  reasoning constraints are produced deterministically first and remain the
  source of truth.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:  # type: ignore[no-redef]
        return None

from Scripts.core.financial_ontology import (
    ALLOWED_METRICS,
    ALLOWED_SOURCES,
    ETF_TO_MACRO_ALIAS,
    INSIDER_FLOW_QUERY_SLOTS,
    METRIC_TO_COLUMN_MAPPING,
    NEWS_TOPIC_ALIAS,
    SEC_ACTION_TAXONOMY,
    TOPIC_TO_TICKERS,
)
from Scripts.core.financial_reasoning_contract import (
    ABSTENTION_RULES,
    DATA_CAPABILITY_RULES,
    INVESTOR_SUITABILITY_RULES,
    OPTION_STRUCTURE_POLICY,
    STRATEGY_ARCHETYPES,
)
from Scripts.core.few_shot_config import FEW_SHOT_EXAMPLES

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment]


load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "Data"
SILVER_DIR = DATA_DIR / "2_Silver_Processed"
GOLD_DIR = DATA_DIR / "3_Gold_Semantic"
RUNTIME_STATE = PROJECT_ROOT / "config" / "runtime" / "collect_data_state.json"
TEST_OUTPUT = PROJECT_ROOT / "Scripts" / "tests" / "router_e2e_ground_truth_queries.json"
QUERY_ONLY_OUTPUT = PROJECT_ROOT / "Scripts" / "tests" / "ground_truth_queries.jsonl"
LOG_DIR = PROJECT_ROOT / "logs" / "ground_truth"

OPTIONS_DIR = SILVER_DIR / "Options_Market_Data"
MACRO_DIR = SILVER_DIR / "Macro_History"
GPR_FILE = SILVER_DIR / "GPR_index" / "gpr_monthly_enriched.parquet"
SEC_DIR = GOLD_DIR / "SEC_Insider_Trades"
NEWS_DIR = GOLD_DIR / "News_Qdrant"

DEFAULT_WINDOW_DAYS = 180
DATA_START_FLOOR = date(2026, 4, 1)
DEFAULT_TARGET_DTE = 30

SUPPORTED_OPTION_TICKERS = {"SPY", "QQQ", "IWM", "GLD", "SLV"}
SUPPORTED_MACRO_SYMBOLS = {
    "^VIX", "^GSPC", "^IXIC", "^RUT", "DX-Y.NYB", "FEDFUNDS",
    "CPIAUCSL", "UNRATE", "GLD", "SLV",
}

logger = logging.getLogger("generate_ground_truth")


INTENT_SLOTS_BY_FAMILY: Dict[str, Dict[str, str]] = {
    "options_microstructure": {
        "pcr_signal": "Put/call ratio signal",
        "atm_iv_signal": "ATM implied volatility signal",
        "liquidity_signal": "Options liquidity / 30-DTE liquidity signal",
    },
    "single_name_options": {
        "iv_or_skew_signal": "Single-name IV / skew signal",
        "liquidity_signal": "Options liquidity / 30-DTE liquidity signal",
    },
    "cross_asset_regime": {
        "iv_vs_vix_regime": "QQQ IV versus VIX regime interpretation",
    },
    "geopolitical_commodity": {
        "gpr_regime_signal": "GPR regime / geopolitical risk signal",
        "options_regime_implication": "GLD options regime implication for hedging",
    },
    "insider_flow_driven": dict(INSIDER_FLOW_QUERY_SLOTS),
}


def _expected_mode_ceiling(spec: QuerySpec) -> str:
    # These benchmark cases are designed to reward evidence discipline rather
    # than forced strike-level action. Directional/watchlist framing is the
    # highest expected ceiling across the current representative set.
    return "directional_watchlist"


def _required_disclosures(spec: QuerySpec) -> List[str]:
    disclosures = [
        "If a strict source or query slot is missing at runtime, explicitly say which part cannot be assessed reliably.",
        "If a requested metric is unavailable from project data, state that limitation instead of substituting a proxy.",
    ]
    if spec.intent_family == "insider_flow_driven":
        disclosures.append(
            "SEC/Form-4 answers must distinguish SELL, BUY, and ACQUIRE/VEST explicitly."
        )
    return disclosures


def _forbidden_claims(spec: QuerySpec) -> List[str]:
    base = [
        "Do not invent unsupported numbers, strike levels, expirations, or catalysts.",
        "Do not replace missing strict evidence with soft context or narrative inference.",
        "Do not present intraday or real-time facts outside local project data.",
    ]
    family_specific = {
        "options_microstructure": [
            "Do not recommend a concrete options structure if only topic-level options evidence is available.",
        ],
        "single_name_options": [
            "Do not claim IV skew values or structure precision unless the options evidence directly supports it.",
        ],
        "cross_asset_regime": [
            "Do not convert IV/VIX regime framing into a strike-level hedge recommendation without options support.",
        ],
        "geopolitical_commodity": [
            "Do not turn GPR or macro narrative alone into strike-level GLD options guidance.",
        ],
        "insider_flow_driven": [
            "Do not collapse ACQUIRE/VEST into BUY or SELL.",
            "Do not describe insider selling if SEC evidence is absent or does not support selling.",
        ],
    }
    return base + family_specific.get(spec.intent_family, [])


def _financial_logic_expectations(spec: QuerySpec) -> List[str]:
    expectations = [
        "Do not say more than the strict evidence supports.",
        "Prefer abstention or downgrade over an unsupported concrete structure.",
        "Risk-averse, defined-risk framing is preferred over aggressive speculative structure.",
    ]
    family_specific = {
        "cross_asset_regime": [
            "Regime interpretation should connect IV and VIX without overclaiming strike-level action.",
        ],
        "insider_flow_driven": [
            "Insider flow is a signal-quality input, not a standalone trade recommendation.",
        ],
    }
    return expectations + family_specific.get(spec.intent_family, [])


def _observed_sec_actions(evidence: EvidenceBundle) -> List[str]:
    observed: List[str] = []
    for fact in evidence.gold_facts:
        for action in re.findall(r"\baction ([A-Z/]+)\b", fact):
            if action not in observed:
                observed.append(action)
    return observed


def _structured_truth(spec: QuerySpec, evidence: EvidenceBundle) -> Dict[str, Any]:
    slots = INTENT_SLOTS_BY_FAMILY.get(spec.intent_family, {})
    observed_actions = _observed_sec_actions(evidence)
    structured = {
        "query_family": spec.intent_family,
        "intent_slots": slots,
        "expected_sources": spec.expected_sources,
        "expected_mode_ceiling": _expected_mode_ceiling(spec),
        "required_disclosures": _required_disclosures(spec),
        "forbidden_claims": _forbidden_claims(spec),
        "financial_logic_expectations": _financial_logic_expectations(spec),
        "coverage_allows_disclosure_pass": True,
    }
    if spec.intent_family == "insider_flow_driven":
        structured["sec_action_taxonomy_expectation"] = {
            "definitions": dict(SEC_ACTION_TAXONOMY),
            "observed_actions": observed_actions,
        }
    return structured


@dataclass(frozen=True)
class QuerySpec:
    name: str
    query: str
    intent_family: str
    route_focus: str
    time_window: Optional[str]
    tickers: List[str]
    metrics: List[str]
    source_types: List[str]
    node_focus: List[str]
    expected_sources: List[str]
    notes: str
    event_keyword: Optional[str] = None
    strategy_intent: Optional[str] = None
    allow_default_time: bool = False
    soft_context_sources: List[str] = field(default_factory=list)


@dataclass
class EvidenceBundle:
    silver_facts: List[str] = field(default_factory=list)
    gold_facts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    citations: List[str] = field(default_factory=list)


def _iso(d: date) -> str:
    return d.isoformat()


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _latest_partition(root: Path) -> Optional[date]:
    dates: List[date] = []
    if not root.exists():
        return None
    for p in root.iterdir():
        if p.is_dir():
            d = _parse_date(p.name)
            if d is not None:
                dates.append(d)
    return max(dates) if dates else None


def _earliest_partition(root: Path) -> Optional[date]:
    dates: List[date] = []
    if not root.exists():
        return None
    for p in root.iterdir():
        if p.is_dir():
            d = _parse_date(p.name)
            if d is not None:
                dates.append(d)
    return min(dates) if dates else None


def _runtime_anchor() -> date:
    """Choose the latest usable local evidence anchor.

    Runtime state can lag behind backfilled data. For ground-truth generation,
    prefer actual local partitions and use runtime state only as an additional
    signal.
    """
    candidates: List[date] = []
    for root in (OPTIONS_DIR, MACRO_DIR, NEWS_DIR, SEC_DIR):
        d = _latest_partition(root)
        if d:
            candidates.append(d)

    if RUNTIME_STATE.exists():
        try:
            raw = json.loads(RUNTIME_STATE.read_text(encoding="utf-8"))
            for v in (raw.get("last_run_keys") or {}).values():
                d = _parse_date(v)
                if d:
                    candidates.append(d)
        except Exception as exc:
            logger.warning("Could not read runtime state %s: %s", RUNTIME_STATE, exc)

    return max(candidates) if candidates else date.today()


def _available_start() -> date:
    candidates = [
        d for d in (
            _earliest_partition(OPTIONS_DIR),
            _earliest_partition(MACRO_DIR),
            _earliest_partition(NEWS_DIR),
            _earliest_partition(SEC_DIR),
            DATA_START_FLOOR,
        )
        if d is not None
    ]
    return min(candidates) if candidates else DATA_START_FLOOR


def _window_days(label: Optional[str]) -> int:
    if label is None:
        return DEFAULT_WINDOW_DAYS
    norm = label.lower()
    return {
        "today": 1,
        "yesterday": 2,
        "past_week": 7,
        "past_month": 30,
        "past_six_months": DEFAULT_WINDOW_DAYS,
    }.get(norm, DEFAULT_WINDOW_DAYS)


def _effective_window(spec: QuerySpec, anchor: date, data_start: date) -> Dict[str, Any]:
    requested_label = spec.time_window or "past_six_months"
    requested_days = _window_days(spec.time_window)
    raw_start = anchor - timedelta(days=requested_days - 1)
    effective_start = max(raw_start, data_start)
    return {
        "requested_label": requested_label,
        "requested_days": requested_days,
        "anchor_date": _iso(anchor),
        "requested_start_date": _iso(raw_start),
        "effective_start_date": _iso(effective_start),
        "effective_end_date": _iso(anchor),
        "default_time_applied": spec.time_window is None,
        "clamped_by_available_data": effective_start > raw_start,
    }


def _partition_on_or_before(root: Path, anchor: date) -> Optional[Path]:
    candidates: List[Tuple[date, Path]] = []
    if not root.exists():
        return None
    for p in root.iterdir():
        if not p.is_dir():
            continue
        d = _parse_date(p.name)
        if d and d <= anchor:
            candidates.append((d, p))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0])[-1][1]


def _read_parquet(path: Path) -> Optional["pd.DataFrame"]:
    if pd is None:
        logger.warning("Could not read parquet %s: pandas is not installed.", path)
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        logger.warning("Could not read parquet %s: %s", path, exc)
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _fmt_float(value: Any, digits: int = 4) -> str:
    x = _safe_float(value)
    if x is None:
        return "not available"
    return f"{x:.{digits}f}"


def _fmt_pct(value: Any, digits: int = 2) -> str:
    x = _safe_float(value)
    if x is None:
        return "not available"
    return f"{x:.{digits}f}%"


def _option_file(ticker: str, anchor: date) -> Optional[Path]:
    candidates: List[Tuple[date, Path]] = []
    if not OPTIONS_DIR.exists():
        return None
    for part in OPTIONS_DIR.iterdir():
        if not part.is_dir():
            continue
        d = _parse_date(part.name)
        if d is None or d > anchor:
            continue
        fp = part / f"{ticker}_options_{part.name}.parquet"
        if fp.exists():
            candidates.append((d, fp))
    return sorted(candidates, key=lambda x: x[0])[-1][1] if candidates else None


def _macro_file(anchor: date) -> Optional[Path]:
    part = _partition_on_or_before(MACRO_DIR, anchor)
    if not part:
        return None
    fp = part / f"macro_snapshot_{part.name}.parquet"
    return fp if fp.exists() else None


def _nearest_dte_frame(df: "pd.DataFrame", target_dte: int = DEFAULT_TARGET_DTE) -> "pd.DataFrame":
    if df.empty or "dte" not in df:
        return df
    dte = pd.to_numeric(df["dte"], errors="coerce")
    if dte.dropna().empty:
        return df
    nearest = (dte - target_dte).abs().idxmin()
    nearest_dte = int(dte.loc[nearest])
    return df[dte == nearest_dte]


def _atm_row(df: "pd.DataFrame") -> Optional[Dict[str, Any]]:
    if df.empty:
        return None
    local = df.copy()
    if "moneyness_pct" in local:
        local["_atm_abs"] = pd.to_numeric(local["moneyness_pct"], errors="coerce").abs()
    elif {"strike", "underlying_price"}.issubset(local.columns):
        strike = pd.to_numeric(local["strike"], errors="coerce")
        px = pd.to_numeric(local["underlying_price"], errors="coerce")
        local["_atm_abs"] = ((strike - px) / px).abs()
    else:
        return None
    local = local.dropna(subset=["_atm_abs"])
    if local.empty:
        return None
    return local.sort_values("_atm_abs").iloc[0].to_dict()


def _option_summary(
    ticker: str,
    anchor: date,
    requested_metrics: Optional[Sequence[str]] = None,
) -> Tuple[List[str], List[str], List[str]]:
    fp = _option_file(ticker, anchor)
    facts: List[str] = []
    warnings: List[str] = []
    citations: List[str] = []
    metrics = {str(m) for m in (requested_metrics or [])}
    if fp is None:
        warnings.append(f"No local options Silver parquet was found for {ticker} on or before {anchor}.")
        return facts, warnings, citations

    df = _read_parquet(fp)
    if df is None or df.empty:
        warnings.append(f"Options parquet for {ticker} is empty or unreadable: {fp}.")
        return facts, warnings, citations

    snapshot = str(df.get("snapshot_date", pd.Series([fp.parent.name])).iloc[0])[:10]
    local_30 = _nearest_dte_frame(df)
    atm = _atm_row(local_30) or _atm_row(df)
    if atm and ({"Implied Volatility (IV)", "Time Decay / DTE", "Options Liquidity", "IV Skew"} & metrics or not metrics):
        facts.append(
            f"{ticker} options Silver snapshot {snapshot}: nearest-ATM contract "
            f"{atm.get('contract_symbol', 'unknown')} has implied volatility "
            f"{_fmt_float(atm.get('implied_volatility'))}, strike {_fmt_float(atm.get('strike'), 2)}, "
            f"DTE {_fmt_float(atm.get('dte'), 0)}, and moneyness {_fmt_float(atm.get('moneyness_pct'), 2)}%."
        )
        citations.append(f"Silver: {ticker}_options_{snapshot}")

    if {"Put/Call Ratio"} & metrics and {"option_type", "volume", "open_interest"}.issubset(df.columns):
        option_type = df["option_type"].astype(str).str.lower()
        call_vol = pd.to_numeric(df.loc[option_type == "call", "volume"], errors="coerce").fillna(0).sum()
        put_vol = pd.to_numeric(df.loc[option_type == "put", "volume"], errors="coerce").fillna(0).sum()
        call_oi = pd.to_numeric(df.loc[option_type == "call", "open_interest"], errors="coerce").fillna(0).sum()
        put_oi = pd.to_numeric(df.loc[option_type == "put", "open_interest"], errors="coerce").fillna(0).sum()
        vol_pcr = (put_vol / call_vol) if call_vol else None
        oi_pcr = (put_oi / call_oi) if call_oi else None
        facts.append(
            f"{ticker} Put/Call Ratio from Silver options: volume PCR {_fmt_float(vol_pcr, 4)} "
            f"(put volume {int(put_vol)}, call volume {int(call_vol)}); open-interest PCR "
            f"{_fmt_float(oi_pcr, 4)} (put OI {int(put_oi)}, call OI {int(call_oi)})."
        )

    if {"option_type", "implied_volatility", "moneyness_pct"}.issubset(df.columns) and ({"IV Skew", "Implied Volatility (IV)"} & metrics or not metrics):
        iv = pd.to_numeric(df["implied_volatility"], errors="coerce")
        mny = pd.to_numeric(df["moneyness_pct"], errors="coerce")
        calls = df[(df["option_type"].astype(str).str.lower() == "call") & (mny >= 0)]
        puts = df[(df["option_type"].astype(str).str.lower() == "put") & (mny <= 0)]
        call_iv = pd.to_numeric(calls["implied_volatility"], errors="coerce").dropna()
        put_iv = pd.to_numeric(puts["implied_volatility"], errors="coerce").dropna()
        if not call_iv.empty and not put_iv.empty:
            skew = float(put_iv.mean() - call_iv.mean())
            facts.append(
                f"{ticker} IV skew proxy: average OTM-put IV minus average OTM-call IV is "
                f"{_fmt_float(skew, 4)} using available Silver contracts."
            )
        elif not iv.dropna().empty:
            facts.append(f"{ticker} option IV range is {_fmt_float(iv.min())} to {_fmt_float(iv.max())}.")

    if "is_liquid" in df.columns and ({"Options Liquidity", "Options Volume", "Open Interest"} & metrics or not metrics):
        liquid = df[df["is_liquid"].astype(bool)]
        liquid_n = int(len(liquid))
        facts.append(f"{ticker} liquidity screen: {liquid_n} contracts are marked is_liquid=True.")
        if liquid_n:
            top = liquid.sort_values(["open_interest", "volume"], ascending=False).head(3)
            legs = []
            for _, row in top.iterrows():
                legs.append(
                    f"{row.get('contract_symbol')} strike {_fmt_float(row.get('strike'), 2)}, "
                    f"{str(row.get('option_type')).lower()}, DTE {_fmt_float(row.get('dte'), 0)}, "
                    f"OI {int(_safe_float(row.get('open_interest')) or 0)}, "
                    f"volume {int(_safe_float(row.get('volume')) or 0)}"
                )
            facts.append("Most liquid contracts by OI/volume: " + "; ".join(legs) + ".")

    return facts, warnings, citations


def _macro_summary(anchor: date, symbols: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
    fp = _macro_file(anchor)
    facts: List[str] = []
    warnings: List[str] = []
    citations: List[str] = []
    if fp is None:
        warnings.append(f"No local macro Silver parquet was found on or before {anchor}.")
        return facts, warnings, citations

    df = _read_parquet(fp)
    if df is None or df.empty:
        warnings.append(f"Macro parquet is empty or unreadable: {fp}.")
        return facts, warnings, citations

    available = set(str(x) for x in df.get("symbol", pd.Series(dtype=str)).dropna().unique())
    for sym in symbols:
        lookup = ETF_TO_MACRO_ALIAS.get(sym, sym)
        if lookup not in available:
            warnings.append(f"Macro symbol {lookup} for query symbol {sym} is not present in {fp.parent.name}.")
            continue
        row = df[df["symbol"].astype(str) == lookup].sort_values("observation_date").tail(1).iloc[0]
        facts.append(
            f"Macro Silver {lookup} ({row.get('name', lookup)}) as of {str(row.get('observation_date'))[:10]}: "
            f"value {_fmt_float(row.get('value'), 4)} {row.get('unit', '')}, "
            f"daily change {_fmt_pct(row.get('daily_change_pct'))}, "
            f"MoM {_fmt_pct(row.get('mom_change_pct'))}, YoY {_fmt_pct(row.get('yoy_change_pct'))}."
        )
        citations.append(f"Silver: macro_snapshot_{fp.parent.name}:{lookup}")
    return facts, warnings, citations


def _gpr_summary() -> Tuple[List[str], List[str], List[str]]:
    facts: List[str] = []
    warnings: List[str] = []
    citations: List[str] = []
    if not GPR_FILE.exists():
        warnings.append("No local GPR Silver parquet was found.")
        return facts, warnings, citations
    df = _read_parquet(GPR_FILE)
    if df is None or df.empty:
        warnings.append(f"GPR parquet is empty or unreadable: {GPR_FILE}.")
        return facts, warnings, citations
    row = df.sort_values("date").tail(1).iloc[0]
    facts.append(
        f"GPR Silver latest monthly observation {str(row.get('date'))[:10]}: GPR index "
        f"{_fmt_float(row.get('gpr'), 2)}, percentile {_fmt_pct(row.get('gpr_percentile'))}, "
        f"MoM {_fmt_pct(row.get('gpr_mom_pct'))}, YoY {_fmt_pct(row.get('gpr_yoy_pct'))}."
    )
    citations.append("Silver: gpr_monthly_enriched:latest")
    return facts, warnings, citations


def _iter_jsonl(paths: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.warning("Could not read JSONL %s: %s", path, exc)


def _gold_sec_summary(tickers: Sequence[str], window: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    facts: List[str] = []
    warnings: List[str] = []
    citations: List[str] = []
    start = _parse_date(window["effective_start_date"])
    end = _parse_date(window["effective_end_date"])
    if start is None or end is None:
        return facts, ["Invalid SEC window."], citations

    paths = sorted(SEC_DIR.glob("*/qdrant_ready.jsonl"))
    docs: List[Dict[str, Any]] = []
    for item in _iter_jsonl(paths):
        meta = item.get("metadata") or {}
        ticker = str(meta.get("ticker") or "").upper()
        filed = _parse_date(meta.get("transaction_date") or meta.get("filed_at"))
        if ticker in tickers and filed and start <= filed <= end:
            docs.append(item)

    if not docs:
        warnings.append(
            f"No SEC Gold Form-4 evidence matched {list(tickers)} between {_iso(start)} and {_iso(end)}."
        )
        return facts, warnings, citations

    by_action: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs:
        action = str((doc.get("metadata") or {}).get("action_direction") or "UNKNOWN").upper()
        by_action.setdefault(action, []).append(doc)

    total = len(docs)
    action_bits = ", ".join(f"{k}: {len(v)}" for k, v in sorted(by_action.items()))
    facts.append(f"SEC Gold Form-4 evidence for {', '.join(tickers)}: {total} matched filings/events ({action_bits}).")

    for doc in docs[:4]:
        meta = doc.get("metadata") or {}
        accession = meta.get("accession_no", "unknown")
        facts.append(
            f"SEC detail: {doc.get('text', '')} Accession {accession}, filed {meta.get('filed_at')}, "
            f"transaction date {meta.get('transaction_date')}, action {meta.get('action_direction')}."
        )
        citations.append(f"Gold SEC: {accession}")
    return facts, warnings, citations


def _gold_news_summary(spec: QuerySpec, window: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    facts: List[str] = []
    warnings: List[str] = []
    citations: List[str] = []
    start = _parse_date(window["effective_start_date"])
    end = _parse_date(window["effective_end_date"])
    if start is None or end is None:
        return facts, ["Invalid News window."], citations

    topic_candidates = [
        NEWS_TOPIC_ALIAS.get(spec.event_keyword or "", spec.event_keyword or ""),
        "macro_geopolitics_risk" if "GPR Index" in spec.metrics else "",
    ]
    topics = {t for t in topic_candidates if t}

    docs: List[Dict[str, Any]] = []
    for path in sorted(NEWS_DIR.glob("*/*.jsonl")):
        part_date = _parse_date(path.parent.name)
        if part_date is None or not (start <= part_date <= end):
            continue
        if topics and not any(topic in path.name for topic in topics):
            continue
        for item in _iter_jsonl([path]):
            meta = item.get("metadata") or {}
            impacted = [str(x).upper() for x in meta.get("impacted_assets", []) or []]
            text_blob = f"{item.get('text', '')} {meta.get('title', '')}".upper()
            if spec.tickers and not any(t.upper() in impacted or t.upper() in text_blob for t in spec.tickers):
                if not any(t in ("GLD", "SLV", "SPY", "QQQ", "IWM") for t in spec.tickers):
                    continue
            docs.append(item)

    if not docs:
        warnings.append(
            f"No News Gold evidence matched the query topic/tickers between {_iso(start)} and {_iso(end)}."
        )
        return facts, warnings, citations

    by_topic: Dict[str, int] = {}
    by_vol: Dict[str, int] = {}
    tones: List[float] = []
    for doc in docs:
        meta = doc.get("metadata") or {}
        topic = str(meta.get("topic") or "unknown")
        by_topic[topic] = by_topic.get(topic, 0) + 1
        vol = str(meta.get("volatility_implication") or "unknown")
        by_vol[vol] = by_vol.get(vol, 0) + 1
        tone = _safe_float(meta.get("llm_tone_score"))
        if tone is not None:
            tones.append(tone)

    topic_bits = ", ".join(f"{k}: {v}" for k, v in sorted(by_topic.items()))
    vol_bits = ", ".join(f"{k}: {v}" for k, v in sorted(by_vol.items()))
    tone_avg = sum(tones) / len(tones) if tones else None
    facts.append(
        f"News Gold evidence: {len(docs)} matched articles ({topic_bits}); volatility implication counts: "
        f"{vol_bits}; average LLM tone score {_fmt_float(tone_avg, 2)}."
    )

    for doc in docs[:3]:
        meta = doc.get("metadata") or {}
        facts.append(
            f"News detail: '{meta.get('title', 'untitled')}' from {meta.get('source', 'unknown')} "
            f"on {str(meta.get('publish_date', 'unknown'))[:10]} says volatility implication "
            f"{meta.get('volatility_implication', 'unknown')} for impacted assets "
            f"{meta.get('impacted_assets', [])}."
        )
        citations.append(f"Gold News: {meta.get('title', 'untitled')}")
    return facts, warnings, citations


def _validate_spec(spec: QuerySpec) -> List[str]:
    errors: List[str] = []
    for src in spec.source_types:
        if src not in ALLOWED_SOURCES and src not in {"options", "macro_history"}:
            errors.append(f"Unsupported source type: {src}")
    for metric in spec.metrics:
        if metric not in ALLOWED_METRICS:
            errors.append(f"Metric is not in financial_ontology.ALLOWED_METRICS: {metric}")
        elif metric in METRIC_TO_COLUMN_MAPPING and not METRIC_TO_COLUMN_MAPPING[metric]:
            errors.append(f"Metric is allowed but currently unavailable in physical data: {metric}")
    if not spec.allow_default_time and not spec.time_window:
        errors.append("Query has no explicit time window and allow_default_time=False.")
    out_of_scope = [
        t for t in spec.tickers
        if (
            t not in SUPPORTED_OPTION_TICKERS
            and t not in SUPPORTED_MACRO_SYMBOLS
            and t not in ETF_TO_MACRO_ALIAS
            and t not in _known_single_names()
        )
    ]
    if out_of_scope:
        errors.append(f"Ticker(s) outside local covered universe: {out_of_scope}")
    return errors


def _known_single_names() -> set:
    if not OPTIONS_DIR.exists():
        return set()
    out = set()
    for fp in OPTIONS_DIR.glob("*/*_options_*.parquet"):
        out.add(fp.name.split("_options_")[0].upper())
    return out


def _query_specs() -> List[QuerySpec]:
    """Curated query suite based on User_Query_Guide + few-shot patterns.

    These are deliberately short, production-like queries that vary ticker,
    time window, source signal, and user intent phrasing. The goal is to mimic
    a broader mix of realistic user asks while staying inside the documented
    product scope.
    """
    return [
        QuerySpec(
            name="Microstructure - SPY hedge posture",
            query="Today SPY put-call ratio and ATM IV for 30-DTE puts?",
            intent_family="options_microstructure",
            route_focus="sql_only",
            time_window="today",
            tickers=["SPY"],
            metrics=["Put/Call Ratio", "Implied Volatility (IV)", "Options Liquidity", "Time Decay / DTE"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_numeric_grounding"],
            expected_sources=["options"],
            strategy_intent="put hedge",
            notes="Guide-aligned SPY hedge query using a same-day index options window.",
        ),
        QuerySpec(
            name="Microstructure - QQQ downside hedge",
            query="Past week QQQ put-call ratio and liquid 30-DTE puts?",
            intent_family="options_microstructure",
            route_focus="sql_only",
            time_window="past_week",
            tickers=["QQQ"],
            metrics=["Put/Call Ratio", "Options Liquidity", "Time Decay / DTE"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_numeric_grounding"],
            expected_sources=["options"],
            strategy_intent="put hedge",
            notes="Uses simpler user wording and focuses on downside hedge conditions instead of a repeated template.",
        ),
        QuerySpec(
            name="Microstructure - IWM downside screen",
            query="Past month IWM ATM IV and 30-DTE put liquidity screen?",
            intent_family="options_microstructure",
            route_focus="sql_only",
            time_window="past_month",
            tickers=["IWM"],
            metrics=["Implied Volatility (IV)", "Options Liquidity", "Time Decay / DTE"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_numeric_grounding"],
            expected_sources=["options"],
            strategy_intent="small-cap hedge",
            notes="Adds a small-cap ETF variant with more natural screening language.",
        ),
        QuerySpec(
            name="Microstructure - GLD volatility screen",
            query="Today GLD IV skew and liquid 30-DTE hedge strikes?",
            intent_family="options_microstructure",
            route_focus="sql_only",
            time_window="today",
            tickers=["GLD"],
            metrics=["Implied Volatility (IV)", "IV Skew", "Options Liquidity", "Time Decay / DTE"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_numeric_grounding"],
            expected_sources=["options"],
            strategy_intent="commodity hedge",
            notes="Directly mirrors the guide's GLD example and tests a concise metals options phrasing.",
        ),
        QuerySpec(
            name="Single-name options - AAPL IV skew posture",
            query="Past month AAPL IV skew and liquid 30-DTE puts?",
            intent_family="single_name_options",
            route_focus="sql_only",
            time_window="past_month",
            tickers=["AAPL"],
            metrics=["Implied Volatility (IV)", "IV Skew", "Options Liquidity", "Time Decay / DTE"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_citation_grounding"],
            expected_sources=["options"],
            strategy_intent="liquidity-aware structure",
            notes="Keeps AAPL as the anchor single-name options case but with more user-like wording.",
        ),
        QuerySpec(
            name="Single-name options - NVDA premium posture",
            query="Past week NVDA ATM IV and call-side liquidity posture?",
            intent_family="single_name_options",
            route_focus="sql_only",
            time_window="past_week",
            tickers=["NVDA"],
            metrics=["Implied Volatility (IV)", "Options Liquidity"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_citation_grounding"],
            expected_sources=["options"],
            strategy_intent="call-side posture",
            notes="Adds a second covered single-name with a different side-of-book emphasis.",
        ),
        QuerySpec(
            name="Single-name options - AMD skew setup",
            query="Today AMD IV skew and options liquidity posture?",
            intent_family="single_name_options",
            route_focus="sql_only",
            time_window="today",
            tickers=["AMD"],
            metrics=["Implied Volatility (IV)", "IV Skew", "Options Liquidity"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_citation_grounding"],
            expected_sources=["options"],
            strategy_intent="skew-aware posture",
            notes="Tests whether the single-name options path still works with a terse same-day AMD phrasing.",
        ),
        QuerySpec(
            name="Single-name options - TSLA downside liquidity",
            query="Past month TSLA put-side liquidity and ATM IV posture?",
            intent_family="single_name_options",
            route_focus="sql_only",
            time_window="past_month",
            tickers=["TSLA"],
            metrics=["Implied Volatility (IV)", "Options Liquidity"],
            source_types=["options"],
            node_focus=["analyst_numeric_synthesis", "checker_citation_grounding"],
            expected_sources=["options"],
            strategy_intent="downside liquidity posture",
            notes="Brings in a higher-beta single name without defaulting every single-name test to AAPL.",
        ),
        QuerySpec(
            name="Cross-asset regime - QQQ IV versus VIX",
            query="Today QQQ ATM IV versus VIX divergence hedge signal?",
            intent_family="cross_asset_regime",
            route_focus="hybrid_both",
            time_window="today",
            tickers=["QQQ", "^VIX"],
            metrics=["Implied Volatility (IV)", "Macro Trend", "Price Change (%)"],
            source_types=["options", "news"],
            node_focus=["analyst_cross_asset_reasoning", "critic_regime_fit"],
            expected_sources=["options", "macro_history"],
            soft_context_sources=["news"],
            notes="Guide-derived QQQ regime case focused on IV versus VIX rather than an extra hedge-implication slot.",
        ),
        QuerySpec(
            name="Cross-asset regime - SPY Fed and yields impact",
            query="Past week FOMC and 10Y yields impact on SPY puts?",
            intent_family="cross_asset_regime",
            route_focus="hybrid_both",
            time_window="past_week",
            tickers=["SPY", "^TNX"],
            metrics=["Implied Volatility (IV)", "Macro Trend", "Price Change (%)"],
            source_types=["options", "news"],
            node_focus=["analyst_cross_asset_reasoning", "critic_regime_fit"],
            expected_sources=["options", "macro_history"],
            soft_context_sources=["news"],
            event_keyword="FOMC",
            notes="Adds an event-driven macro prompt with one primary intent: what Fed plus yields imply for SPY downside positioning.",
        ),
        QuerySpec(
            name="Cross-asset regime - SPY CPI and VIX",
            query="Past month CPI and VIX regime for SPY hedge demand?",
            intent_family="cross_asset_regime",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["SPY", "^VIX"],
            metrics=["Implied Volatility (IV)", "Macro Trend", "Price Change (%)"],
            source_types=["options", "news"],
            node_focus=["analyst_cross_asset_reasoning", "critic_regime_fit"],
            expected_sources=["options", "macro_history"],
            soft_context_sources=["news"],
            event_keyword="CPI",
            notes="Macro regime variant that uses CPI as the news anchor while staying focused on SPY hedge demand.",
        ),
        QuerySpec(
            name="Cross-asset regime - IWM small-cap stress",
            query="Past month IWM IV versus VIX stress regime read?",
            intent_family="cross_asset_regime",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["IWM", "^VIX"],
            metrics=["Implied Volatility (IV)", "Macro Trend", "Price Change (%)"],
            source_types=["options", "news"],
            node_focus=["analyst_cross_asset_reasoning", "critic_regime_fit"],
            expected_sources=["options", "macro_history"],
            soft_context_sources=["news"],
            notes="Retains a small-cap cross-asset case, but with more natural stress-regime phrasing.",
        ),
        QuerySpec(
            name="Geopolitical commodity - GLD risk backdrop",
            query="Past week GLD IV skew and geopolitical risk context?",
            intent_family="geopolitical_commodity",
            route_focus="hybrid_both",
            time_window="past_week",
            tickers=["GLD"],
            metrics=["Implied Volatility (IV)", "IV Skew", "GPR Index", "Macro Trend"],
            source_types=["options", "news"],
            node_focus=["analyst_cross_asset_reasoning", "critic_regime_fit"],
            expected_sources=["options", "gpr"],
            soft_context_sources=["news"],
            strategy_intent="hedge framing",
            notes="A guide-style GLD query that explicitly binds options context to geopolitical risk.",
        ),
        QuerySpec(
            name="Insider flow - AAPL selling signal",
            query="Past month AAPL Form-4 selling signal and put positioning?",
            intent_family="insider_flow_driven",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["AAPL"],
            metrics=["Insider Trading", "Implied Volatility (IV)", "Options Liquidity"],
            source_types=["sec", "options"],
            node_focus=["checker_lineage_grounding", "critic_insider_signal_fit"],
            expected_sources=["sec", "options"],
            event_keyword="insider_selling",
            strategy_intent="put positioning",
            notes="Directly follows the guide's canonical AAPL Form-4 selling query shape.",
        ),
        QuerySpec(
            name="Insider flow - NVDA insider signal",
            query="Past month NVDA Form-4 insider signal and ATM IV posture?",
            intent_family="insider_flow_driven",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["NVDA"],
            metrics=["Insider Trading", "Implied Volatility (IV)", "Options Liquidity"],
            source_types=["sec", "options"],
            node_focus=["checker_lineage_grounding", "critic_insider_signal_fit"],
            expected_sources=["sec", "options"],
            strategy_intent="signal-quality posture",
            notes="Extends insider-flow evaluation beyond AAPL while keeping the same SEC plus options reasoning contract.",
        ),
        QuerySpec(
            name="Insider flow - AMD buying versus vesting",
            query="Past month AMD insider buying versus vesting signal?",
            intent_family="insider_flow_driven",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["AMD"],
            metrics=["Insider Trading"],
            source_types=["sec"],
            node_focus=["checker_lineage_grounding", "critic_insider_signal_fit"],
            expected_sources=["sec"],
            strategy_intent="signal-quality taxonomy",
            notes="Pure SEC taxonomy case that checks whether the system distinguishes buying from acquire/vest events.",
        ),
        QuerySpec(
            name="Insider flow - TSLA default window posture",
            query="TSLA Form-4 insider activity and options liquidity posture?",
            intent_family="insider_flow_driven",
            route_focus="hybrid_both",
            time_window=None,
            tickers=["TSLA"],
            metrics=["Insider Trading", "Options Liquidity"],
            source_types=["sec", "options"],
            node_focus=["checker_lineage_grounding", "critic_insider_signal_fit"],
            expected_sources=["sec", "options"],
            strategy_intent="liquidity posture",
            allow_default_time=True,
            notes="Default-window insider-flow case to keep coverage on unspecified time phrasing.",
        ),
        QuerySpec(
            name="Geopolitical commodity - GLD hedge posture",
            query="Past month GLD geopolitical risk and options hedge posture?",
            intent_family="geopolitical_commodity",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["GLD"],
            metrics=["GPR Index", "Implied Volatility (IV)", "Options Liquidity"],
            source_types=["gpr", "options", "news"],
            node_focus=["analyst_macro_to_options_reasoning", "critic_macro_consistency"],
            expected_sources=["gpr", "options"],
            soft_context_sources=["news"],
            strategy_intent="hedge posture",
            notes="Primary GLD geopolitical hedge case with explicit risk-plus-options coupling.",
        ),
        QuerySpec(
            name="Geopolitical commodity - SLV safe-haven posture",
            query="Past month SLV geopolitical risk and options hedge posture?",
            intent_family="geopolitical_commodity",
            route_focus="hybrid_both",
            time_window="past_month",
            tickers=["SLV"],
            metrics=["GPR Index", "Implied Volatility (IV)", "Options Liquidity"],
            source_types=["gpr", "options", "news"],
            node_focus=["analyst_macro_to_options_reasoning", "critic_macro_consistency"],
            expected_sources=["gpr", "options"],
            soft_context_sources=["news"],
            strategy_intent="hedge posture",
            notes="Adds metals diversity without changing the family-level reasoning contract.",
        ),
        QuerySpec(
            name="Geopolitical commodity - GLD volatility and risk",
            query="Past week GLD options volatility and geopolitical risk posture?",
            intent_family="geopolitical_commodity",
            route_focus="hybrid_both",
            time_window="past_week",
            tickers=["GLD"],
            metrics=["GPR Index", "Options Liquidity", "Implied Volatility (IV)"],
            source_types=["gpr", "options", "news"],
            node_focus=["analyst_macro_to_options_reasoning", "critic_macro_consistency"],
            expected_sources=["gpr", "options"],
            soft_context_sources=["news"],
            strategy_intent="volatility-aware hedge posture",
            notes="Short-window GLD variant that emphasizes options volatility without collapsing into a repetitive template.",
        ),
    ]


def _contextualize_few_shots() -> str:
    snippets = []
    for ex in FEW_SHOT_EXAMPLES:
        extraction = ex.get("extraction", {})
        metrics = ", ".join(extraction.get("metrics", []) or [])
        snippets.append(
            f"{ex.get('scenario')}: query='{ex.get('user_query')}', metrics=[{metrics}], "
            f"time_window={extraction.get('time_window')}"
        )
    return " | ".join(snippets)


def _strict_scope_sentence(spec: QuerySpec) -> str:
    strict_sources = ", ".join(spec.expected_sources) if spec.expected_sources else "none"
    base = (
        "Scope: Strict scoring for this case is limited to in-project evidence that is mandatory for the query. "
        f"Required sources for scoring are [{strict_sources}]. "
        "The answer must stay inside local project data, and it must not use real-time or intraday facts. "
        "Short-answer evaluation targets the direct answer layer first; full report narrative is secondary."
    )
    if spec.soft_context_sources:
        soft_sources = ", ".join(spec.soft_context_sources)
        base += (
            f" Soft context from [{soft_sources}] may appear as narrative background, "
            "but it is optional support and should not be required for scoring."
        )
    base += (
        " Time remains a retrieval and fallback contract: when the query does not ask for an exact date boundary, "
        "short-answer correctness should focus on whether the answer preserves strict evidence rather than whether it "
        "repeats an exact anchor date."
    )
    return base


def _structure_support_note(spec: QuerySpec) -> str:
    strict_sources = {src.lower() for src in spec.expected_sources}
    if strict_sources == {"options"}:
        return (
            "Because strict scoring is options-only here, the answer may stay at directional_watchlist or "
            "informational_only if it preserves the options evidence and avoids unsupported strike precision."
        )
    if "sec" in strict_sources or "gpr" in strict_sources:
        return (
            "Because this case mixes directional context with options posture, a valid answer may downgrade to "
            "directional_watchlist or informational_only when concrete structure support is thin."
        )
    if "macro_history" in strict_sources:
        return (
            "Macro and options evidence may justify regime-level hedging logic without requiring strike-specific action."
        )
    return (
        "A valid answer may stay caveated if the available strict evidence supports direction more clearly than precise structure."
    )


def _contract_lines(spec: QuerySpec) -> List[str]:
    regime_lines = []
    if spec.intent_family in {"cross_asset_regime", "macro_regime", "geopolitical_commodity"}:
        for regime in ("HIGH", "LOW", "NORMAL", "UNKNOWN"):
            regime_lines.append(f"{regime}: {STRATEGY_ARCHETYPES[regime]}")
    policy_lines = [
        OPTION_STRUCTURE_POLICY[0],
        OPTION_STRUCTURE_POLICY[1],
        OPTION_STRUCTURE_POLICY[3],
        INVESTOR_SUITABILITY_RULES[0],
        INVESTOR_SUITABILITY_RULES[1],
        ABSTENTION_RULES[0],
        DATA_CAPABILITY_RULES[0],
        DATA_CAPABILITY_RULES[3],
    ]
    if spec.soft_context_sources:
        policy_lines.append("Soft Gold/news context can support framing, but it cannot replace strict mandatory evidence.")
    policy_lines.append(_structure_support_note(spec))
    return regime_lines + policy_lines


def _reasoning_path(spec: QuerySpec, window: Dict[str, Any], evidence: EvidenceBundle) -> List[str]:
    lines = [
        "Analyst: identify the query family, bind the ticker/event/time window, and build the answer from strict in-scope evidence first; soft context can support framing but must not replace mandatory evidence.",
        "Checker: verify strict numeric claims, anchors, and time-window discipline only; unsupported metrics must be called unavailable rather than inferred.",
        "Critic: apply the financial reasoning contract to structure fit, abstention, data capability, and investor suitability without re-litigating raw numbers.",
        "Finalizer: keep conversation_reply query-first and short, with recommendation_mode in the second sentence; macro backdrop belongs in the report sections, not the opening answer.",
    ]
    if window["default_time_applied"]:
        lines.append("Router/Time: because the query omitted a time phrase, the expected default is past_six_months.")
    if window["clamped_by_available_data"]:
        lines.append(
            "Time audit: the requested window extends before available local data; the answer must disclose the effective evidence window."
        )
    if any("No " in w for w in evidence.warnings):
        lines.append("Evidence gap: missing local evidence should lower confidence and must not be filled from prior knowledge.")
    if "critic_regime_fit" in spec.node_focus:
        lines.append("Critic focus: do not recommend long premium in a high-IV regime without acknowledging premium cost and catalyst justification.")
    if "critic_insider_signal_fit" in spec.node_focus:
        lines.append("Critic focus: insider selling/buying is a signal quality check, not a standalone trade recommendation.")
    lines.extend(_contract_lines(spec))
    return lines


def _truth_answer_boundary(spec: QuerySpec) -> str:
    lines = [
        "Truth-answer boundary: the short answer is judged as a direct conclusion layer, not as a full macro report.",
        "A valid short answer should preserve strict evidence, place recommendation_mode as a boundary rather than a headline, and keep macro narrative in the report layer.",
        "Time wording is not a primary short-answer correctness target unless the user explicitly asks for a concrete date boundary; time discipline is mainly enforced through retrieval and fallback contracts.",
        "Unavailable metrics must trigger a caveat, not a substitute or extrapolated proxy.",
        "Unsupported tickers, Greeks, yield spreads, intraday prices, and external real-time facts are always out of bounds.",
    ]
    if "options" not in {src.lower() for src in spec.expected_sources}:
        lines.append("Because options are not a strict scoring source here, strike-level precision is not required and should not be rewarded.")
    else:
        lines.append(_structure_support_note(spec))
    lines.append("For a risk-averse ordinary investor, defined-risk and simpler expressions are preferable to speculative complexity when both are logically valid.")
    return " ".join(lines)


def _build_ground_truth(spec: QuerySpec, anchor: date, data_start: date) -> Dict[str, Any]:
    validation_errors = _validate_spec(spec)
    window = _effective_window(spec, anchor, data_start)
    evidence = EvidenceBundle()

    for ticker in spec.tickers:
        if ticker in _known_single_names() or ticker in SUPPORTED_OPTION_TICKERS:
            facts, warnings, citations = _option_summary(ticker, anchor, spec.metrics)
            evidence.silver_facts.extend(facts)
            evidence.warnings.extend(warnings)
            evidence.citations.extend(citations)

    macro_symbols = [
        t for t in spec.tickers
        if t.startswith("^") or t in ETF_TO_MACRO_ALIAS or t in SUPPORTED_MACRO_SYMBOLS
    ]
    needs_macro = (
        "macro_history" in spec.expected_sources
        or "Macro Trend" in spec.metrics
        or "Price Change (%)" in spec.metrics
    )
    if needs_macro:
        facts, warnings, citations = _macro_summary(anchor, macro_symbols or ["^VIX"])
        evidence.silver_facts.extend(facts)
        evidence.warnings.extend(warnings)
        evidence.citations.extend(citations)

    if "GPR Index" in spec.metrics or "gpr" in spec.source_types:
        facts, warnings, citations = _gpr_summary()
        evidence.silver_facts.extend(facts)
        evidence.warnings.extend(warnings)
        evidence.citations.extend(citations)

    if "sec" in spec.source_types:
        facts, warnings, citations = _gold_sec_summary(spec.tickers, window)
        evidence.gold_facts.extend(facts)
        evidence.warnings.extend(warnings)
        evidence.citations.extend(citations)

    if "news" in spec.source_types:
        facts, warnings, citations = _gold_news_summary(spec, window)
        evidence.gold_facts.extend(facts)
        evidence.warnings.extend(warnings)
        evidence.citations.extend(citations)

    evidence.warnings.extend(validation_errors)
    reasoning = _reasoning_path(spec, window, evidence)

    answer_lines = [
        _strict_scope_sentence(spec),
    ]
    if spec.time_window:
        answer_lines.append(
            f"Requested evidence horizon: {window['requested_label']}. "
            "Short-answer scoring should respect this horizon conceptually, but exact anchor-date phrasing is not required."
        )
    if window["clamped_by_available_data"]:
        answer_lines.append(
            "Retrieval/fallback time limitation: the requested horizon extends beyond available local data, so facts before the effective evidence window must not be inferred."
        )
    if evidence.silver_facts:
        answer_lines.append("Silver facts: " + " ".join(evidence.silver_facts))
    if evidence.gold_facts:
        gold_label = (
            "Soft context facts"
            if spec.soft_context_sources and not any(src in spec.expected_sources for src in ("news",))
            else "Gold facts"
        )
        answer_lines.append(f"{gold_label}: " + " ".join(evidence.gold_facts))
    if evidence.warnings:
        answer_lines.append("Data limitations: " + " ".join(sorted(set(evidence.warnings))))
    answer_lines.append("Expected reasoning path: " + " ".join(reasoning))
    answer_lines.append(_truth_answer_boundary(spec))

    return {
        "name": spec.name,
        "query": spec.query,
        "ground_truth": "\n".join(answer_lines),
        "expected_sources": spec.expected_sources,
        "expected_time_window": spec.time_window or "past_six_months",
        "structured_truth": _structured_truth(spec, evidence),
        "case_meta": {
            "intent_family": spec.intent_family,
            "route_focus": spec.route_focus,
            "scoring_scope": "strict_only",
            "strict_expected_sources": spec.expected_sources,
            "soft_context_sources": spec.soft_context_sources,
            "tickers": spec.tickers,
            "metrics": spec.metrics,
            "source_types": spec.source_types,
            "node_focus": spec.node_focus,
            "event_keyword": spec.event_keyword,
            "strategy_intent": spec.strategy_intent,
            "ontology_metrics_validated": [m for m in spec.metrics if m in ALLOWED_METRICS],
            "few_shot_reference": _contextualize_few_shots(),
            "time_window": window,
            "evaluation_targets": {
                "short_answer": "conversation_reply",
                "full_report": "markdown",
            },
            "runtime_contract_alignment": {
                "scope_source": "Scripts/core/financial_ontology.py",
                "reasoning_source": "Scripts/core/financial_reasoning_contract.py",
                "strict_scoring_only": True,
            },
            "citations": sorted(set(evidence.citations)),
            "validation_errors": validation_errors,
        },
        "notes": spec.notes,
    }


def _polish_with_llm(case: Dict[str, Any]) -> Dict[str, Any]:
    """Optional wording polish, never fact generation."""
    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        logger.warning("LLM polish skipped; missing dependency: %s", exc)
        return case

    prompt = ChatPromptTemplate.from_template(
        "Rewrite the following ground-truth answer in concise professional English. "
        "Do not add, remove, or change any fact, number, ticker, date, limitation, "
        "or reasoning constraint.\n\n{truth}"
    )
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    response = (prompt | llm).invoke({"truth": case["ground_truth"]})
    polished = dict(case)
    polished["ground_truth"] = getattr(response, "content", str(response)).strip()
    polished.setdefault("case_meta", {})["llm_polished"] = True
    return polished


def generate_cases(llm_polish: bool = False) -> Dict[str, Any]:
    anchor = _runtime_anchor()
    data_start = _available_start()
    cases = []
    for spec in _query_specs():
        case = _build_ground_truth(spec, anchor, data_start)
        if llm_polish:
            case = _polish_with_llm(case)
        cases.append(case)
    return {
        "schema_version": "2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "language": "en",
        "description": (
            "Ground-truth catalogue generated inside project scope. Queries follow "
            "docs/modular_guide/User_Query_Guide.md and terminology is validated "
            "against Scripts/core/financial_ontology.py. Scoring is strict-scope only: "
            "mandatory evidence is scored, while soft background context remains optional. "
            "Reasoning boundaries inherit Scripts/core/financial_reasoning_contract.py."
        ),
        "contract_sources": [
            "Scripts/core/financial_ontology.py",
            "Scripts/core/financial_reasoning_contract.py",
        ],
        "data_contract": {
            "anchor_date": _iso(anchor),
            "available_data_start": _iso(data_start),
            "default_time_window_when_unspecified": "past_six_months",
            "time_reference_docs": [
                "docs/Data_source_docs/Time_Schema_Audit.md",
                "docs/Data_source_docs/Data_source_summary.md",
            ],
            "forbidden_ground_truth_content": [
                "out-of-universe symbols",
                "intraday or real-time quotes",
                "Greeks as factual values",
                "yield spreads as factual values",
                "fundamental valuation",
                "crypto, FX majors, single-name bonds",
            ],
            "scoring_scope": "strict_only",
        },
        "cases": cases,
    }


def _write_outputs(payload: Dict[str, Any], out_dir: Path, update_tests: bool) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / "ground_truth_cases.json"
    queries_path = out_dir / "queries.jsonl"
    cases_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(queries_path, "w", encoding="utf-8") as fh:
        for case in payload["cases"]:
            fh.write(json.dumps({
                "name": case["name"],
                "query": case["query"],
                "expected_time_window": case["expected_time_window"],
                "expected_sources": case["expected_sources"],
            }, ensure_ascii=False) + "\n")

    written = {"cases": cases_path, "queries": queries_path}
    if update_tests:
        TEST_OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        with open(QUERY_ONLY_OUTPUT, "w", encoding="utf-8") as fh:
            for case in payload["cases"]:
                fh.write(json.dumps({"name": case["name"], "query": case["query"]}, ensure_ascii=False) + "\n")
        written["test_cases"] = TEST_OUTPUT
        written["test_queries"] = QUERY_ONLY_OUTPUT
    return written


def generate_oracle_truth(query: str, silver_ctx: str, gold_ctx: str) -> str:
    """Backward-compatible helper for ad-hoc oracle generation.

    Prefer ``generate_cases`` for benchmark data. This helper keeps the old
    call surface but switches the instruction language to English and makes the
    ontology boundary explicit.
    """
    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("generate_oracle_truth requires langchain-openai.") from exc

    allowed_metrics = ", ".join(ALLOWED_METRICS)
    prompt = ChatPromptTemplate.from_template(
        "You are a strict financial RAG evaluator. Generate an English ground-truth "
        "answer for the user query using only the supplied Silver and Gold context.\n\n"
        "Ontology boundary: allowed metrics are {allowed_metrics}. If a requested "
        "metric is absent from context or physically unavailable, state that limitation. "
        "Do not use external facts, intraday data, or investment advice beyond conditional "
        "reasoning from evidence.\n\n"
        "User query: {query}\n\n"
        "Silver structured context:\n{silver}\n\n"
        "Gold semantic context:\n{gold}\n\n"
        "Ground truth answer:"
    )
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    response = (prompt | llm).invoke({
        "allowed_metrics": allowed_metrics,
        "query": query,
        "silver": silver_ctx,
        "gold": gold_ctx,
    })
    return getattr(response, "content", str(response)).strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate project-scoped English ground-truth cases for RAGAS evaluation."
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Output directory. Default: logs/ground_truth/YYYYMMDD_HHMMSS",
    )
    parser.add_argument(
        "--update-tests",
        action="store_true",
        help="Also write Scripts/tests/router_e2e_ground_truth_queries.json and ground_truth_queries.jsonl.",
    )
    parser.add_argument(
        "--llm-polish",
        action="store_true",
        help="Optionally polish wording with GPT without changing deterministic facts.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else LOG_DIR / run_ts
    payload = generate_cases(llm_polish=args.llm_polish)
    written = _write_outputs(payload, out_dir, update_tests=args.update_tests)

    print("Generated ground-truth catalogue")
    print(f"  cases:   {written['cases']}")
    print(f"  queries: {written['queries']}")
    if "test_cases" in written:
        print(f"  test cases:   {written['test_cases']}")
        print(f"  test queries: {written['test_queries']}")
    print(f"  n_cases: {len(payload['cases'])}")
    print(f"  anchor:  {payload['data_contract']['anchor_date']}")
    print(f"  start:   {payload['data_contract']['available_data_start']}")


if __name__ == "__main__":
    main()
