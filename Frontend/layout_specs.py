from __future__ import annotations

from typing import Any, Dict, List


METRIC_LABELS = {
    "pcr_volume": "Put/Call Ratio (Volume)",
    "pcr_open_interest": "Put/Call Ratio (Open Interest)",
    "pcr_status": "Put/Call Regime Signal",
    "vix_value": "VIX Spot",
    "vix_change_pct": "VIX Daily Change (%)",
    "gpr_index_level": "Geopolitical Risk Index",
    "gpr_percentile": "GPR Percentile",
    "gpr_trend": "GPR Trend",
}

_TICKER_SUFFIX_LABELS = {
    "executable_option_volume": "Executable Option Volume",
    "executable_open_interest": "Executable Open Interest",
    "liquid_contracts": "Executable Contract Count",
    "avg_spread_pct": "Executable Avg Spread (%)",
    "market_impact_risk": "Market Impact Risk (Executable Subset)",
}

CATEGORY_ORDER = ["Options", "Macro", "Cross-Asset", "Liquidity"]

_CANONICAL_PREFIX_ALIASES = {
    "DX_Y_NYB_": "DXY_",
}

CATEGORY_RULES = {
    "Options": ["pcr", "option", "open_interest", "implied_vol", "strike", "dte", "contract"],
    "Macro": ["vix", "gpr", "fed", "cpi", "unrate", "yield", "dxy"],
    "Cross-Asset": ["gspc", "ixic", "gld", "slv", "nasdaq", "spx"],
    "Liquidity": ["spread", "liquid", "market_impact", "volume"],
}


def canonical_metric_key(metric_key: str) -> str:
    key = (metric_key or "").strip()
    if not key:
        return key
    for alias_prefix, canonical_prefix in _CANONICAL_PREFIX_ALIASES.items():
        if key.startswith(alias_prefix):
            return canonical_prefix + key[len(alias_prefix) :]
    return key


def normalize_metric_aliases(values: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for raw_key, value in (values or {}).items():
        canonical_key = canonical_metric_key(raw_key)
        if canonical_key not in normalized or raw_key == canonical_key:
            normalized[canonical_key] = value
    return normalized


def pretty_label(metric_key: str) -> str:
    key = (metric_key or "").strip()
    if not key:
        return "Metric"
    mapped = METRIC_LABELS.get(key.lower())
    if mapped:
        return mapped
    for suffix, suffix_label in _TICKER_SUFFIX_LABELS.items():
        ticker_suffix = f"_{suffix}"
        if key.endswith(ticker_suffix):
            ticker = key[: -len(ticker_suffix)].upper()
            return f"{ticker} {suffix_label}" if ticker else suffix_label
    cleaned = key.replace("_", " ").strip()
    return " ".join([w.upper() if len(w) <= 4 else w.capitalize() for w in cleaned.split()])


def pretty_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def value_color(metric_key: str, value: Any) -> str:
    k = (metric_key or "").lower()
    text = str(value).lower()
    if "risk" in k or "status" in k:
        if "high" in text or "bearish" in text:
            return "#bf8f8a"
        if "neutral" in text or "medium" in text:
            return "#d7bf8b"
        return "#8fa78c"
    if "change_pct" in k:
        try:
            v = float(value)
            return "#8fa78c" if v >= 0 else "#bf8f8a"
        except Exception:
            return "#9f8f7b"
    return "#9f8f7b"


def categorize_metric(metric_key: str) -> str:
    k = (metric_key or "").lower()
    if any(
        token in k
        for token in (
            "executable_option_volume",
            "executable_open_interest",
            "liquid_contracts",
            "market_impact",
            "avg_spread",
        )
    ):
        return "Liquidity"
    for category in CATEGORY_ORDER:
        needles = CATEGORY_RULES.get(category, [])
        if any(n in k for n in needles):
            return category
    return "Cross-Asset"


def group_metrics_by_category(values: Dict[str, Any]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {c: [] for c in CATEGORY_ORDER}
    for key in values.keys():
        cat = categorize_metric(key)
        groups.setdefault(cat, []).append(key)
    for cat in groups:
        groups[cat] = sorted(groups[cat])
    return groups
