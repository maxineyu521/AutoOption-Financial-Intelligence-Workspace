"""Deterministic market-posture contract for read-only posture outputs.

Purpose:
1. Centralize posture labels and their derivation from validated financial metrics.
2. Separate current priced state (`base_regime_read`) from next-step deterioration
   risk (`escalation_risk_read`).
3. Expose an auditable reasoning trace instead of relying on hidden model reasoning.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from Scripts.core.liquidity_policy import (
    classify_market_impact_risk,
    resolve_first_available_ticker_bundle,
    resolve_primary_ticker,
)


PostureLabel = Literal["constructive", "neutral", "neutral_to_defensive", "defensive", "stressed"]

POSTURE_LABELS: tuple[PostureLabel, ...] = (
    "constructive",
    "neutral",
    "neutral_to_defensive",
    "defensive",
    "stressed",
)


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _canonical_query_family(scope_contract: Dict[str, Any]) -> str:
    return str((scope_contract or {}).get("query_family") or "").strip().lower()


def _read_profile(scope_contract: Dict[str, Any]) -> str:
    return str((scope_contract or {}).get("read_profile") or "").strip().lower()


def requires_posture_takeaway(scope_contract: Dict[str, Any], recommendation_mode: str | None) -> bool:
    scope = scope_contract if isinstance(scope_contract, dict) else {}
    if _read_profile(scope) != "posture_read":
        return False
    if _canonical_query_family(scope) not in {"options_microstructure", "cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
        return False
    if str(recommendation_mode or "").strip().lower() == "actionable_options":
        return False
    return True


def _resolve_market_impact(
    values: Dict[str, Any],
    *,
    state: Optional[Dict[str, Any]] = None,
    metadata: Any = None,
    scope_contract: Optional[Dict[str, Any]] = None,
) -> str:
    explicit = str(values.get("market_impact_risk") or "").strip()
    if explicit:
        return explicit

    primary_ticker = resolve_primary_ticker(
        state=state,
        metadata=metadata,
        scope_contract=scope_contract or {},
    )
    options_ticker, bundle = resolve_first_available_ticker_bundle(
        values,
        [primary_ticker] if primary_ticker else [],
        ["avg_spread_pct", "liquid_contracts", "market_impact_risk"],
    )
    bundle_explicit = str(bundle.get("market_impact_risk") or "").strip()
    if bundle_explicit:
        return bundle_explicit
    return classify_market_impact_risk(
        options_ticker or primary_ticker,
        bundle.get("avg_spread_pct"),
        bundle.get("liquid_contracts"),
    )


def _derive_pcr_state(values: Dict[str, Any]) -> str:
    pcr_volume = _coerce_float(values.get("pcr_volume"))
    if pcr_volume is None:
        pcr_volume = _coerce_float(values.get("pcr_open_interest"))
    if pcr_volume is None:
        return "unknown"
    if pcr_volume >= 1.15:
        return "protection_heavy"
    if pcr_volume < 0.85:
        return "call_skewed"
    return "neutral_flow"


def _derive_iv_regime_state(iv_regime_pinned: Dict[str, Any] | None) -> str:
    regime = str((iv_regime_pinned or {}).get("iv_regime") or "UNKNOWN").strip().upper()
    if regime in {"LOW", "NORMAL", "HIGH"}:
        return regime
    return "UNKNOWN"


def _derive_iv_richness_state(values: Dict[str, Any], iv_regime_pinned: Dict[str, Any] | None) -> str:
    iv_rank = _coerce_float(values.get("latest_atm_iv_rank_pct"))
    if iv_rank is not None:
        if iv_rank < 30:
            return "cheap"
        if iv_rank < 55:
            return "mid_range"
        if iv_rank < 75:
            return "firm"
        return "rich"

    iv_regime = _derive_iv_regime_state(iv_regime_pinned)
    if iv_regime == "LOW":
        return "cheap"
    if iv_regime == "HIGH":
        return "rich"
    if iv_regime == "NORMAL":
        return "mid_range"
    return "unknown"


def _derive_skew_state(values: Dict[str, Any]) -> tuple[str, str]:
    skew = _coerce_float(values.get("latest_iv_skew"))
    if skew is None:
        return "unknown", "unknown"
    if skew >= 0.10:
        return "positive_put_premium", "strong"
    if skew >= 0.03:
        return "positive_put_premium", "modest"
    if skew <= -0.10:
        return "negative_call_premium", "strong"
    if skew <= -0.03:
        return "negative_call_premium", "modest"
    return "flat", "flat"


def _derive_liquidity_state(
    values: Dict[str, Any],
    *,
    state: Optional[Dict[str, Any]] = None,
    metadata: Any = None,
    scope_contract: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    market_impact = _resolve_market_impact(
        values,
        state=state,
        metadata=metadata,
        scope_contract=scope_contract,
    )
    normalized = str(market_impact or "Unknown").strip()
    if normalized == "High":
        return "fragile", normalized
    if normalized in {"Low", "Medium"}:
        return "healthy", normalized
    return "unknown", normalized or "Unknown"


def _derive_posture_label(
    *,
    pcr_state: str,
    iv_regime_state: str,
    skew_state: str,
    skew_intensity: str,
    liquidity_state: str,
) -> PostureLabel:
    if iv_regime_state == "HIGH" and (
        skew_state == "positive_put_premium" and skew_intensity == "strong"
        or pcr_state == "protection_heavy"
    ):
        return "stressed"

    if skew_state == "positive_put_premium" and (
        pcr_state == "protection_heavy" or iv_regime_state == "HIGH"
    ):
        return "defensive"

    if (
        iv_regime_state == "NORMAL"
        and pcr_state in {"neutral_flow", "protection_heavy"}
        and skew_state == "positive_put_premium"
    ):
        return "neutral_to_defensive"

    if iv_regime_state == "NORMAL" and pcr_state == "neutral_flow" and skew_state in {"flat", "unknown"}:
        return "neutral"

    if iv_regime_state == "LOW" and pcr_state == "call_skewed" and skew_state in {"flat", "negative_call_premium", "unknown"}:
        return "constructive"

    if pcr_state == "call_skewed" and skew_state == "negative_call_premium":
        return "constructive"

    if liquidity_state == "fragile" and iv_regime_state == "HIGH":
        return "defensive"

    return "neutral"


def _derive_transition_risk(
    *,
    pcr_state: str,
    iv_regime_state: str,
    iv_richness_state: str,
    skew_state: str,
    liquidity_state: str,
    label: PostureLabel,
) -> str:
    if liquidity_state == "fragile":
        return "execution_fragility"
    if iv_regime_state == "HIGH" or iv_richness_state == "rich":
        return "premium_compression"
    if (
        label in {"defensive", "stressed", "neutral_to_defensive"}
        and pcr_state == "protection_heavy"
        and skew_state == "positive_put_premium"
    ):
        return "defensive_flow_acceleration"
    if iv_richness_state in {"cheap", "mid_range"}:
        return "vol_spike_repricing"
    return "regime_reversal"


def _render_posture_takeaway(label: PostureLabel, query_family: str) -> str:
    subject = "The current market posture"
    if query_family == "options_microstructure":
        subject = "The current hedge posture"
    return f"{subject} is {label.replace('_', '-')}."


def _render_posture_rationale(
    *,
    label: PostureLabel,
    pcr_state: str,
    iv_regime_state: str,
    skew_state: str,
    liquidity_state: str,
) -> str:
    if label == "stressed":
        return "The options posture reflects stressed protection demand and elevated premium for downside insurance."
    if label == "defensive":
        return "Protection demand is clearly elevated, suggesting a defensive posture with more active downside hedging."
    if label == "neutral_to_defensive":
        if liquidity_state == "fragile":
            return "Baseline downside protection is present, but fragile execution conditions argue for a more defensive posture."
        return "Market participants appear to be carrying baseline downside protection, but not chasing premium defensively."
    if label == "constructive":
        return "Protection demand looks light relative to the current premium regime, which leans more constructive than defensive."
    if iv_regime_state == "LOW":
        return "Premium remains relatively inexpensive, and the current flow does not point to an aggressive defensive bid."
    if pcr_state == "neutral_flow" and skew_state in {"flat", "unknown"}:
        return "Flow and volatility remain broadly balanced, pointing to a neutral posture rather than an active hedge chase."
    return "The current flow and volatility mix point to a neutral posture rather than a stressed hedging regime."


def _render_base_regime_read(
    *,
    label: PostureLabel,
    pcr_state: str,
    iv_regime_state: str,
    iv_richness_state: str,
    skew_state: str,
) -> str:
    if iv_regime_state == "HIGH" or iv_richness_state == "rich":
        return "Downside protection is already expensive, with volatility premium trading in a rich and more stressed regime."
    if label == "defensive" and pcr_state == "protection_heavy" and skew_state == "positive_put_premium":
        if iv_richness_state == "firm":
            return "Downside protection is already elevated, with put premium firm rather than cheap, but the posture is not yet dislocated into a full stress regime."
        return "Downside protection is already active and leaning defensive, with put premium elevated from a non-cheap base."
    if label == "neutral_to_defensive" and skew_state == "positive_put_premium":
        return "Baseline downside protection is already present, and put premium is beginning to firm even though the market is not yet in a stressed hedge regime."
    if iv_regime_state == "LOW" or iv_richness_state == "cheap":
        return "Downside protection is still relatively inexpensive, and the current regime does not yet reflect a stressed demand for hedges."
    if iv_regime_state == "NORMAL" and iv_richness_state == "firm":
        return "Volatility is already trading from a firm base rather than a cheap one, so current hedge pricing is no longer especially benign."
    if iv_regime_state == "NORMAL":
        return "Volatility is in a middle-of-the-range regime, with pricing neither distressed nor unusually cheap."
    return "The current options regime is balanced enough to support a posture read, but without a full stress signal."


def _render_escalation_risk(archetype: str, values: Dict[str, Any], iv_regime_pinned: Dict[str, Any] | None) -> str:
    iv_rank = _coerce_float(values.get("latest_atm_iv_rank_pct"))
    if archetype == "execution_fragility":
        return "The primary risk is execution fragility: hedge implementation could become materially more expensive under stressed liquidity."
    if archetype == "premium_compression":
        return "The main risk is overpaying for already-rich downside protection and then seeing volatility premium compress."
    if archetype == "defensive_flow_acceleration":
        return "The main risk is a further acceleration in downside hedging demand that pushes put premium materially higher and can move the posture from defensive into stressed."
    if archetype == "vol_spike_repricing":
        if iv_rank is not None:
            return f"With IV rank at {iv_rank:.2f}%, the primary risk is a volatility spike that sharply reprices downside hedges higher."
        return "The primary risk is a volatility spike that sharply reprices downside hedges higher."
    iv_regime = _derive_iv_regime_state(iv_regime_pinned)
    if iv_regime == "LOW":
        return "The main risk is a reversal toward heavier downside hedging that makes currently cheaper protection materially more expensive."
    return "The main risk is a reversal in options flow and skew that removes the current posture signal and weakens the read."


def derive_posture_contract(
    values: Dict[str, Any],
    iv_regime_pinned: Dict[str, Any] | None,
    scope_contract: Dict[str, Any],
    recommendation_mode: str | None,
    *,
    state: Optional[Dict[str, Any]] = None,
    metadata: Any = None,
) -> Dict[str, Any]:
    required = requires_posture_takeaway(scope_contract, recommendation_mode)
    query_family = _canonical_query_family(scope_contract)
    pcr_state = _derive_pcr_state(values)
    iv_regime_state = _derive_iv_regime_state(iv_regime_pinned)
    iv_richness_state = _derive_iv_richness_state(values, iv_regime_pinned)
    skew_state, skew_intensity = _derive_skew_state(values)
    liquidity_state, market_impact_risk = _derive_liquidity_state(
        values,
        state=state,
        metadata=metadata,
        scope_contract=scope_contract,
    )

    if required:
        label = _derive_posture_label(
            pcr_state=pcr_state,
            iv_regime_state=iv_regime_state,
            skew_state=skew_state,
            skew_intensity=skew_intensity,
            liquidity_state=liquidity_state,
        )
        transition_risk = _derive_transition_risk(
            pcr_state=pcr_state,
            iv_regime_state=iv_regime_state,
            iv_richness_state=iv_richness_state,
            skew_state=skew_state,
            liquidity_state=liquidity_state,
            label=label,
        )
        takeaway = _render_posture_takeaway(label, query_family)
        rationale = _render_posture_rationale(
            label=label,
            pcr_state=pcr_state,
            iv_regime_state=iv_regime_state,
            skew_state=skew_state,
            liquidity_state=liquidity_state,
        )
        base_regime_read = _render_base_regime_read(
            label=label,
            pcr_state=pcr_state,
            iv_regime_state=iv_regime_state,
            iv_richness_state=iv_richness_state,
            skew_state=skew_state,
        )
        escalation_risk_read = _render_escalation_risk(transition_risk, values, iv_regime_pinned)
    else:
        label = ""
        takeaway = ""
        rationale = ""
        transition_risk = ""
        base_regime_read = ""
        escalation_risk_read = ""

    return {
        "requires_posture_takeaway": required,
        "posture_label": label,
        "posture_takeaway": takeaway,
        "posture_rationale": rationale,
        "base_regime_read": base_regime_read,
        "escalation_risk_archetype": transition_risk,
        "escalation_risk_read": escalation_risk_read,
        "posture_reasoning_trace": {
            "query_family": query_family,
            "market_analysis_only": bool((scope_contract or {}).get("market_analysis_only")),
            "read_profile": _read_profile(scope_contract),
            "recommendation_mode": str(recommendation_mode or ""),
            "pcr_state": pcr_state,
            "iv_regime_state": iv_regime_state,
            "iv_richness_state": iv_richness_state,
            "skew_state": skew_state,
            "skew_intensity": skew_intensity,
            "liquidity_state": liquidity_state,
            "market_impact_risk": market_impact_risk,
            "posture_transition_risk": transition_risk,
            "derived_label": label,
        },
    }


def derive_posture_market_risk(
    values: Dict[str, Any],
    iv_regime_pinned: Dict[str, Any] | None,
    posture_contract: Dict[str, Any],
    scope_contract: Dict[str, Any],
    *,
    state: Optional[Dict[str, Any]] = None,
    metadata: Any = None,
) -> str:
    if not bool((posture_contract or {}).get("requires_posture_takeaway")):
        return ""
    explicit = str((posture_contract or {}).get("escalation_risk_read") or "").strip()
    if explicit:
        return explicit
    trace = dict((posture_contract or {}).get("posture_reasoning_trace") or {})
    archetype = str(
        (posture_contract or {}).get("escalation_risk_archetype")
        or trace.get("posture_transition_risk")
        or ""
    ).strip()
    if not archetype:
        return ""
    return _render_escalation_risk(archetype, values, iv_regime_pinned)


def render_posture_contract_block(posture_contract: Dict[str, Any]) -> str:
    if not posture_contract:
        return "(posture contract unavailable)"
    trace = dict(posture_contract.get("posture_reasoning_trace") or {})
    lines = [
        "=== POSTURE CONTRACT ===",
        f"requires_posture_takeaway={posture_contract.get('requires_posture_takeaway')}",
        f"posture_label={posture_contract.get('posture_label') or '(none)'}",
        f"posture_takeaway={posture_contract.get('posture_takeaway') or '(none)'}",
        f"posture_rationale={posture_contract.get('posture_rationale') or '(none)'}",
        f"base_regime_read={posture_contract.get('base_regime_read') or '(none)'}",
        f"escalation_risk_archetype={posture_contract.get('escalation_risk_archetype') or '(none)'}",
        f"escalation_risk_read={posture_contract.get('escalation_risk_read') or '(none)'}",
    ]
    for key in ("pcr_state", "iv_regime_state", "iv_richness_state", "skew_state", "skew_intensity", "liquidity_state", "market_impact_risk", "posture_transition_risk", "derived_label"):
        lines.append(f"{key}={trace.get(key)}")
    return "\n".join(lines)
