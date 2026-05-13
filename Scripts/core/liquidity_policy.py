"""Shared liquidity-tier and ticker-metric resolution helpers.

Purpose:
1. Remove ticker hard-coding from agents.
2. Derive liquidity tiers from the tracked universe plus a small Tier-1 override set.
3. Keep market-impact classification deterministic and provider-agnostic.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from Scripts.core.universe import universe


LiquidityTier = Literal["tier1", "tier2", "tier3", "unknown"]
MarketImpactRisk = Literal["Low", "Medium", "High", "Unknown"]

_TIER1_SINGLE_NAME_OVERRIDES = {"AAPL", "MSFT", "NVDA", "TSLA"}

_TIER_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "tier1": {
        "low_spread_pct": 3.0,
        "low_liquid_contracts": 100,
        "medium_spread_pct": 6.0,
        "medium_liquid_contracts": 50,
    },
    "tier2": {
        "low_spread_pct": 6.0,
        "low_liquid_contracts": 50,
        "medium_spread_pct": 10.0,
        "medium_liquid_contracts": 20,
    },
    "tier3": {
        "low_spread_pct": 10.0,
        "low_liquid_contracts": 20,
        "medium_spread_pct": 15.0,
        "medium_liquid_contracts": 10,
    },
}

_TICKER_METRIC_SUFFIXES = {
    "daily_option_volume",
    "open_interest",
    "executable_option_volume",
    "executable_open_interest",
    "liquid_contracts",
    "market_impact_risk",
    "avg_spread_pct",
    "underlying_price",
}

_EXECUTABLE_MONEYNESS_BAND_RATIO: Dict[str, float] = {
    "tier1": 0.15,
    "tier2": 0.10,
    "tier3": 0.10,
}


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def resolve_primary_ticker(
    state: Optional[Dict[str, Any]] = None,
    metadata: Any = None,
    scope_contract: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    scope = scope_contract or (state or {}).get("scope_contract") or {}
    primary = str(scope.get("primary_ticker") or "").upper().strip()
    if primary:
        return primary

    in_scope = list(scope.get("in_scope_tickers") or [])
    if in_scope:
        primary = str(in_scope[0]).upper().strip()
        if primary:
            return primary

    if metadata is None and state:
        metadata = state.get("metadata")
    for ticker in (_obj_get(metadata, "tickers", []) or []):
        ticker_u = str(ticker).upper().strip()
        if ticker_u:
            return ticker_u
    return None


def liquidity_tier_of(ticker: Optional[str]) -> LiquidityTier:
    ticker_u = str(ticker or "").upper().strip()
    if not ticker_u:
        return "unknown"

    broad_market = set(universe.get("etf.broad_market"))
    commodity = set(universe.get("etf.commodity"))
    single_names = set(universe.get("equity.single_name"))

    if ticker_u in broad_market or ticker_u in _TIER1_SINGLE_NAME_OVERRIDES:
        return "tier1"
    if ticker_u in commodity:
        return "tier3"
    if ticker_u in single_names:
        return "tier2"
    return "unknown"


def thresholds_for_ticker(ticker: Optional[str]) -> Optional[Dict[str, float]]:
    tier = liquidity_tier_of(ticker)
    if tier == "unknown":
        return None
    return dict(_TIER_THRESHOLDS[tier])


def executable_moneyness_band_ratio(ticker: Optional[str]) -> Optional[float]:
    tier = liquidity_tier_of(ticker)
    return _EXECUTABLE_MONEYNESS_BAND_RATIO.get(tier)


def classify_market_impact_risk(
    ticker: Optional[str],
    avg_spread_pct: Any,
    liquid_contracts: Any,
) -> MarketImpactRisk:
    thresholds = thresholds_for_ticker(ticker)
    if thresholds is None:
        return "Unknown"
    if avg_spread_pct is None or liquid_contracts is None:
        return "Unknown"

    try:
        spread = float(avg_spread_pct)
        contracts = int(liquid_contracts)
    except (TypeError, ValueError):
        return "Unknown"

    if spread <= thresholds["low_spread_pct"] and contracts >= thresholds["low_liquid_contracts"]:
        return "Low"
    if spread <= thresholds["medium_spread_pct"] and contracts >= thresholds["medium_liquid_contracts"]:
        return "Medium"
    return "High"


def available_ticker_prefixes(silver_values: Dict[str, Any]) -> List[str]:
    prefixes: List[str] = []
    for raw_key in silver_values or {}:
        key = str(raw_key)
        parts = key.split("_", 1)
        if len(parts) != 2:
            continue
        ticker, suffix = parts
        if suffix in _TICKER_METRIC_SUFFIXES and ticker.isupper() and ticker not in prefixes:
            prefixes.append(ticker)
    return prefixes


def resolve_ticker_metric_bundle(
    silver_values: Dict[str, Any],
    ticker: Optional[str],
    suffixes: Iterable[str],
) -> Dict[str, Any]:
    ticker_u = str(ticker or "").upper().strip()
    bundle: Dict[str, Any] = {}
    for suffix in suffixes:
        bundle[str(suffix)] = silver_values.get(f"{ticker_u}_{suffix}") if ticker_u else None
    return bundle


def resolve_first_available_ticker_bundle(
    silver_values: Dict[str, Any],
    candidate_tickers: Iterable[str],
    suffixes: Iterable[str],
) -> Tuple[Optional[str], Dict[str, Any]]:
    checked: List[str] = []
    for ticker in candidate_tickers:
        ticker_u = str(ticker or "").upper().strip()
        if not ticker_u or ticker_u in checked:
            continue
        checked.append(ticker_u)
        bundle = resolve_ticker_metric_bundle(silver_values, ticker_u, suffixes)
        if any(value is not None for value in bundle.values()):
            return ticker_u, bundle

    for ticker in available_ticker_prefixes(silver_values):
        if ticker in checked:
            continue
        bundle = resolve_ticker_metric_bundle(silver_values, ticker, suffixes)
        if any(value is not None for value in bundle.values()):
            return ticker, bundle

    return None, {str(suffix): None for suffix in suffixes}
