from __future__ import annotations

from typing import Any, Dict, List


METRIC_LABELS = {
    "pcr_volume": "Put/Call Ratio (Volume)",
    "pcr_open_interest": "Put/Call Ratio (Open Interest)",
    "pcr_status": "Put/Call Regime Signal",
    "aapl_daily_option_volume": "AAPL Daily Options Volume",
    "aapl_open_interest": "AAPL Aggregate Open Interest",
    "aapl_avg_spread_pct": "AAPL Average Spread (%)",
    "aapl_liquid_contracts": "AAPL Liquid Contract Count",
    "aapl_market_impact_risk": "AAPL Market Impact Risk",
    "vix_value": "VIX Spot",
    "vix_change_pct": "VIX Daily Change (%)",
    "gpr_index_level": "Geopolitical Risk Index",
    "gpr_percentile": "GPR Percentile",
    "gpr_trend": "GPR Trend",
}

CATEGORY_ORDER = ["Options", "Macro", "Cross-Asset", "Liquidity"]

CATEGORY_RULES = {
    "Options": ["pcr", "option", "open_interest", "implied_vol", "strike", "dte", "contract"],
    "Macro": ["vix", "gpr", "fed", "cpi", "unrate", "yield", "dxy"],
    "Cross-Asset": ["gspc", "ixic", "gld", "slv", "dx_y_nyb", "nasdaq", "spx"],
    "Liquidity": ["spread", "liquid", "market_impact", "volume"],
}


def pretty_label(metric_key: str) -> str:
    key = (metric_key or "").strip()
    if not key:
        return "Metric"
    mapped = METRIC_LABELS.get(key.lower())
    if mapped:
        return mapped
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
