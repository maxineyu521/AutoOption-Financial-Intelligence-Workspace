"""
Scripts/core/financial_reasoning_contract.py

Compact reasoning contract for downstream strategy-review agents.

Purpose:
1. Keep option-structure policy, abstention policy, and data-capability policy
   in one auditable place.
2. Convert project-scope ontology into a small, prompt-safe contract for the
   Critic without dumping the entire ontology into every LLM call.
3. Provide deterministic helpers that translate current state into a
   data-capability profile.
"""

from __future__ import annotations

from typing import Any, Dict, List

from Scripts.core.financial_ontology import METRIC_TO_COLUMN_MAPPING


STRATEGY_ARCHETYPES: Dict[str, str] = {
    "HIGH": (
        "Rich premium regime. Prefer defined-risk premium harvesting or capped "
        "directional expressions. Naked long premium requires a near-term, specific catalyst."
    ),
    "LOW": (
        "Cheap optionality regime. Prefer long optionality or debit structures. "
        "Systematic short premium is usually under-compensated."
    ),
    "NORMAL": (
        "Direction-first regime. Structure choice should stay proportional to the "
        "strength and horizon of the evidence."
    ),
    "UNKNOWN": (
        "Regime confidence is weak. Prefer caveated, informational framing over "
        "high-conviction structure claims."
    ),
}


OPTION_STRUCTURE_POLICY: List[str] = [
    "Direction can be correct while structure is still poor.",
    "High IV + naked long premium + no near-term catalyst is a direct structure mismatch.",
    "High IV + long premium around a specific near-term catalyst can be acceptable, but defined-risk spreads are preferred.",
    "Low IV favors long optionality / debit structures over short premium harvesting.",
    "Long-dated DTE should be explicitly justified when the thesis is event-driven or short-window.",
]


INVESTOR_SUITABILITY_RULES: List[str] = [
    "Assume a risk-averse ordinary investor by default.",
    "Prefer simpler, defined-risk structures over aggressive or highly path-dependent trades.",
    "If strike-level precision is not well supported by Silver options data, stay at directional_watchlist or informational_only.",
    "Do not present speculative complexity as the default recommendation for a conservative audience.",
]


ABSTENTION_RULES: List[str] = [
    "If the evidence does not support a concrete options structure, downgrade to informational-only output.",
    "Conflicting signals, thin options data, stale event evidence, or scope-missing key metrics are reasons to prefer abstention.",
    "Absence of a specific trade idea is not a failure when scope only supports topic-level directional framing.",
]


DATA_CAPABILITY_RULES: List[str] = [
    "Unavailable metrics from ontology must trigger caveats, not hallucinated substitutes.",
    "Concrete options structures require actual options microstructure support from project data, not macro narrative alone.",
    "Gold news / SEC context can justify direction and catalysts, but should not invent strike precision when Silver options support is thin.",
    "SEC evidence is subtype-sensitive: Form-4 insider transaction evidence and 8-K event filing evidence are not interchangeable.",
    "Concrete strike / expiration guidance requires Silver-layer options support, not only macro or Gold evidence.",
]

AUDIT_BOUNDARY_RULES: List[str] = [
    "Checker owns factual consistency, source existence, citation validity, and slot/disclosure compliance.",
    "Critic owns qualitative wording-strength, interpretive overreach, and reasoning quality.",
    "A supported value with a weak or missing citation is a citation/scope issue, not a source hallucination.",
]


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_data_capability_profile(
    metadata: Any,
    silver_ctx: Dict[str, Any],
    gold_ctx: List[Any],
    time_range: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Translate current state into a compact data-capability profile."""
    metrics = list(_obj_get(metadata, "metrics", []) or [])
    requested_sources = list(_obj_get(metadata, "source_types", []) or [])
    event_keyword = _obj_get(metadata, "event_keyword", "") or ""
    time_window = _obj_get(metadata, "time_window", "") or (
        (time_range or {}).get("time_window_label", "")
    )

    # A metric is unavailable when:
    #   (a) it is not registered in the ontology at all — unknown metrics must
    #       not silently pass as "available", and
    #   (b) it is registered but maps to an empty column list, which means the
    #       physical column has not yet been ingested (e.g. Greeks, Yield Spread).
    unavailable_metrics = [
        metric for metric in metrics
        if metric not in METRIC_TO_COLUMN_MAPPING
        or not METRIC_TO_COLUMN_MAPPING.get(metric)
    ]
    available_metrics = [
        metric for metric in metrics
        if metric not in unavailable_metrics
    ]

    values = (silver_ctx or {}).get("values", {}) if isinstance(silver_ctx, dict) else {}
    has_iv_signal = any(
        key in values for key in ("latest_atm_iv", "latest_atm_iv_rank_pct", "latest_iv_skew", "implied_volatility")
    )
    has_liquidity_signal = any(
        key in values for key in (
            "pcr_volume",
            "executable_open_interest",
            "executable_option_volume",
            "avg_spread_pct",
            "market_impact_risk",
            "liquid_contracts",
            "open_interest",
            "volume",
            "spread_pct",
        )
    )
    has_strike_support = any(
        key in values for key in ("strike", "moneyness_pct", "underlying_price")
    )
    has_dte_support = any(
        key in values for key in ("dte", "expiration")
    )
    has_price_signal = any(
        key in values for key in ("underlying_price", "value", "daily_change_pct", "mom_change_pct")
    )
    has_gold_evidence = bool(gold_ctx)
    has_options_source = "options" in {str(src).lower() for src in requested_sources}
    has_options_chain_support = bool(
        has_options_source and (has_iv_signal or has_liquidity_signal or has_strike_support or has_dte_support)
    )

    can_support_concrete_option_structure = bool(
        has_options_chain_support
        and has_iv_signal
        and (has_liquidity_signal or has_price_signal)
        and has_strike_support
    )
    if metrics and set(metrics).issubset(set(unavailable_metrics)):
        can_support_concrete_option_structure = False

    return {
        "requested_metrics": metrics,
        "requested_sources": requested_sources,
        "requested_time_window": str(time_window),
        "event_keyword": str(event_keyword),
        "available_metrics": available_metrics,
        "unavailable_metrics": unavailable_metrics,
        "has_options_source": has_options_source,
        "has_options_chain_support": has_options_chain_support,
        "has_iv_signal": has_iv_signal,
        "has_liquidity_signal": has_liquidity_signal,
        "has_strike_support": has_strike_support,
        "has_dte_support": has_dte_support,
        "has_price_signal": has_price_signal,
        "has_gold_evidence": has_gold_evidence,
        "can_support_concrete_option_structure": can_support_concrete_option_structure,
    }


def render_reasoning_contract() -> str:
    sections = [
        "=== OPTION STRATEGY ARCHETYPES ===",
    ]
    for regime, description in STRATEGY_ARCHETYPES.items():
        sections.append(f"- {regime}: {description}")

    sections.append("\n=== OPTION STRUCTURE POLICY ===")
    sections.extend(f"- {rule}" for rule in OPTION_STRUCTURE_POLICY)

    sections.append("\n=== INVESTOR SUITABILITY RULES ===")
    sections.extend(f"- {rule}" for rule in INVESTOR_SUITABILITY_RULES)

    sections.append("\n=== ABSTENTION RULES ===")
    sections.extend(f"- {rule}" for rule in ABSTENTION_RULES)

    sections.append("\n=== DATA CAPABILITY RULES ===")
    sections.extend(f"- {rule}" for rule in DATA_CAPABILITY_RULES)

    sections.append("\n=== AUDIT BOUNDARY RULES ===")
    sections.extend(f"- {rule}" for rule in AUDIT_BOUNDARY_RULES)
    return "\n".join(sections)


def render_data_capability_profile(profile: Dict[str, Any]) -> str:
    if not profile:
        return "(data capability profile unavailable)"

    lines = [
        "=== DATA CAPABILITY PROFILE ===",
        f"requested_metrics={profile.get('requested_metrics')}",
        f"requested_sources={profile.get('requested_sources')}",
        f"requested_time_window={profile.get('requested_time_window')}",
        f"event_keyword={profile.get('event_keyword') or '(none)'}",
        f"available_metrics={profile.get('available_metrics')}",
        f"unavailable_metrics={profile.get('unavailable_metrics')}",
        f"has_options_source={profile.get('has_options_source')}",
        f"has_options_chain_support={profile.get('has_options_chain_support')}",
        f"has_iv_signal={profile.get('has_iv_signal')}",
        f"has_liquidity_signal={profile.get('has_liquidity_signal')}",
        f"has_strike_support={profile.get('has_strike_support')}",
        f"has_dte_support={profile.get('has_dte_support')}",
        f"has_price_signal={profile.get('has_price_signal')}",
        f"has_gold_evidence={profile.get('has_gold_evidence')}",
        f"can_support_concrete_option_structure={profile.get('can_support_concrete_option_structure')}",
    ]
    return "\n".join(lines)
