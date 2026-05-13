from __future__ import annotations

from typing import Any, Dict, List, Mapping

from Scripts.core.financial_ontology import metric_comparison_mode, query_slots_for_family

QUERY_FAMILY_ALIASES: Dict[str, str] = {
    "macro_regime": "cross_asset_regime",
    "macro_geopolitics_risk": "geopolitical_macro_read",
    "geopolitical_commodity": "geopolitical_options_read",
}

TRUTH_SLOT_TO_CONTRACT_SLOTS: Dict[str, List[str]] = {
    "iv_vs_vix_regime": ["equity_vol_signal", "macro_vol_signal"],
    "hedging_implication": ["supporting_context"],
    "gpr_regime_signal": ["latest_geopolitical_risk_anchor"],
    "options_regime_implication": ["options_vol_signal"],
}

DISCLOSURE_PHRASES: List[str] = [
    "cannot be assessed reliably",
    "cannot assess reliably",
    "was not retrieved",
    "were not retrieved",
    "is unavailable",
    "are unavailable",
    "is not available",
    "are not available",
    "is missing",
    "are missing",
]

DISCLOSURE_TARGET_ALIASES: Dict[str, List[str]] = {
    "sec": ["sec", "form 4", "form-4", "insider"],
    "sec_insider_signal": ["insider signal", "form 4", "form-4", "insider"],
    "sec_event_signal": ["8-k", "8k", "sec event", "event filing", "8-k filing", "sec filing"],
    "options": ["options", "option chain", "liquidity", "open interest", "implied volatility", "atm iv"],
    "options_liquidity_posture": ["options liquidity", "liquidity posture", "open interest", "option volume"],
    "options_vol_signal": ["options volatility", "atm iv", "implied volatility", "options"],
    "macro": ["macro", "vix", "dxy", "nasdaq", "s&p", "macro volatility"],
    "macro_history": ["macro", "vix", "dxy", "nasdaq", "s&p", "macro volatility"],
    "macro_vol_signal": ["macro volatility", "vix", "dxy"],
    "gpr": ["gpr", "geopolitical risk"],
    "geopolitical_risk_signal": ["gpr", "geopolitical risk"],
    "latest_geopolitical_risk_anchor": ["gpr", "gpr index", "geopolitical risk"],
    "geopolitical_news_signal": ["news", "headline", "article", "narrative"],
    "impact_basket_context": ["gld", "slv", "vix", "dxy", "s&p", "market impact"],
}


_TOKEN_TEXT_ALIASES: Dict[str, List[str]] = {
    "news_gold_summary": ["news", "headline", "article", "narrative"],
    "sec_gold_summary": ["form 4", "sec", "insider", "vesting", "buying", "selling"],
    "sec_event_gold_summary": ["8-k", "8k", "sec filing", "event filing", "filing", "event risk"],
    "daily_option_volume": ["daily option volume", "option volume", "contract volume"],
    "open_interest": ["open interest"],
    "executable_option_volume": ["executable option volume", "executable volume", "executable contract volume"],
    "executable_open_interest": ["executable open interest", "executable oi"],
    "market_impact_risk": ["market impact risk", "market impact"],
    "liquid_contracts": ["liquid contracts", "liquid contract"],
    "avg_spread_pct": ["executable average spread", "executable weighted spread", "average spread", "spread percentage", "avg spread", "bid ask spread", "bid-ask spread"],
    "pcr_volume": ["pcr volume", "put call ratio", "put/call ratio", "put-call ratio", "p/c ratio"],
    "pcr_open_interest": ["pcr open interest", "put call open interest", "open interest ratio"],
    "latest_atm_iv": ["atm iv", "at the money iv", "implied volatility", "atm implied", "current iv"],
    "latest_atm_iv_rank_pct": ["iv rank", "iv percentile", "iv rank percentile"],
    "latest_iv_skew": ["iv skew", "volatility skew", "put call skew", "otm put vs otm call iv skew"],
    "latest_otm_put_iv": ["otm put iv", "otm put implied volatility", "put wing iv"],
    "latest_otm_call_iv": ["otm call iv", "otm call implied volatility", "call wing iv"],
    "VIX_value": ["vix", "vix value", "volatility index"],
    "DXY_value": ["dxy", "dollar index", "us dollar index"],
    "gpr_index_level": ["gpr", "gpr index", "geopolitical risk", "gpr index level"],
    "GLD_value": ["gld", "gld price", "gld spot", "gold price"],
    "GLD_change_pct": ["gld move", "gld change", "gold move", "gold change"],
    "SLV_value": ["slv", "slv price", "silver price"],
    "SLV_change_pct": ["slv move", "slv change", "silver move", "silver change"],
    "GSPC_value": ["s&p 500", "s&p", "gspc", "spx"],
    "GSPC_change_pct": ["s&p 500 move", "s&p move", "equity move"],
    "GLD_avg_bid": ["average bid", "avg bid", "bid"],
    "GLD_avg_ask": ["average ask", "avg ask", "ask"],
    "GLD_avg_spread_pct": ["average spread", "avg spread", "spread percentage", "bid ask spread", "bid-ask spread"],
    "GLD_avg_last_price": ["average last price", "avg last price", "last price"],
}


def normalize_silver_citation_contract(
    raw_contract: Mapping[str, Any] | None,
    values: Mapping[str, Any] | None,
    lineage_anchors: List[str] | None,
) -> Dict[str, Dict[str, Any]]:
    values = dict(values or {})
    raw = dict(raw_contract or {})
    lineage = [str(anchor) for anchor in (lineage_anchors or []) if anchor is not None]
    contract: Dict[str, Dict[str, Any]] = {}
    for metric_key in values.keys():
        entry = dict(raw.get(metric_key) or {})
        contract[str(metric_key)] = {
            "metric_key": str(metric_key),
            "preferred_anchor": str(entry.get("preferred_anchor") or metric_key),
            "legacy_aliases": [str(alias) for alias in (entry.get("legacy_aliases") or []) if alias is not None],
            "audit_lineage_anchors": [str(anchor) for anchor in (entry.get("audit_lineage_anchors") or lineage) if anchor is not None],
            "observed_at": entry.get("observed_at"),
            "source_channel": entry.get("source_channel") or "primary",
            "comparison_mode": str(entry.get("comparison_mode") or metric_comparison_mode(str(metric_key))),
        }
    return contract


def build_silver_citation_registry(
    raw_contract: Mapping[str, Any] | None,
    values: Mapping[str, Any] | None,
    lineage_anchors: List[str] | None,
) -> Dict[str, Any]:
    contract = normalize_silver_citation_contract(raw_contract, values, lineage_anchors)
    preferred_anchor_to_metric: Dict[str, str] = {}
    legacy_alias_to_metric: Dict[str, str] = {}
    audit_anchor_to_metrics: Dict[str, List[str]] = {}
    for metric_key, entry in contract.items():
        preferred_anchor = str(entry.get("preferred_anchor") or metric_key)
        preferred_anchor_to_metric[preferred_anchor] = metric_key
        for alias in entry.get("legacy_aliases") or []:
            legacy_alias_to_metric[str(alias)] = metric_key
        for audit_anchor in entry.get("audit_lineage_anchors") or []:
            audit_anchor_to_metrics.setdefault(str(audit_anchor), [])
            if metric_key not in audit_anchor_to_metrics[str(audit_anchor)]:
                audit_anchor_to_metrics[str(audit_anchor)].append(metric_key)
    return {
        "contract": contract,
        "preferred_anchor_to_metric": preferred_anchor_to_metric,
        "legacy_alias_to_metric": legacy_alias_to_metric,
        "audit_anchor_to_metrics": audit_anchor_to_metrics,
    }


def parse_silver_inline_payload(payload_text: str) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for raw_piece in str(payload_text or "").split(","):
        token = " ".join(str(raw_piece or "").strip().split())
        if not token:
            continue
        lowered = token.lower()
        while lowered.startswith("silver:"):
            token = token.split(":", 1)[1].strip() if ":" in token else ""
            lowered = token.lower()
        if token:
            refs.append({"kind": "Silver", "anchor": token})
    return refs


def resolve_silver_anchor_ref(anchor_ref: Mapping[str, Any], registry: Mapping[str, Any]) -> Dict[str, Any]:
    anchor = str((anchor_ref or {}).get("anchor") or "").strip()
    preferred_anchor_to_metric = dict((registry or {}).get("preferred_anchor_to_metric") or {})
    legacy_alias_to_metric = dict((registry or {}).get("legacy_alias_to_metric") or {})
    audit_anchor_to_metrics = dict((registry or {}).get("audit_anchor_to_metrics") or {})
    contract = dict((registry or {}).get("contract") or {})

    if anchor in preferred_anchor_to_metric:
        metric_key = preferred_anchor_to_metric[anchor]
        return {
            "kind": "Silver",
            "anchor": anchor,
            "resolution_status": "canonical",
            "resolved_metric_key": metric_key,
            "preferred_anchor": anchor,
            "is_legacy_alias": False,
            "is_audit_anchor": False,
            "comparison_mode": str((contract.get(metric_key) or {}).get("comparison_mode") or metric_comparison_mode(metric_key)),
        }
    if anchor in legacy_alias_to_metric:
        metric_key = legacy_alias_to_metric[anchor]
        preferred_anchor = str((contract.get(metric_key) or {}).get("preferred_anchor") or metric_key)
        return {
            "kind": "Silver",
            "anchor": anchor,
            "resolution_status": "legacy_alias",
            "resolved_metric_key": metric_key,
            "preferred_anchor": preferred_anchor,
            "is_legacy_alias": True,
            "is_audit_anchor": False,
            "comparison_mode": str((contract.get(metric_key) or {}).get("comparison_mode") or metric_comparison_mode(metric_key)),
        }
    if anchor in audit_anchor_to_metrics:
        metric_keys = list(audit_anchor_to_metrics[anchor] or [])
        metric_key = metric_keys[0] if metric_keys else ""
        preferred_anchor = str((contract.get(metric_key) or {}).get("preferred_anchor") or metric_key)
        return {
            "kind": "Silver",
            "anchor": anchor,
            "resolution_status": "audit_lineage",
            "resolved_metric_key": metric_key,
            "preferred_anchor": preferred_anchor,
            "is_legacy_alias": False,
            "is_audit_anchor": True,
            "comparison_mode": str((contract.get(metric_key) or {}).get("comparison_mode") or metric_comparison_mode(metric_key)),
        }
    return {
        "kind": "Silver",
        "anchor": anchor,
        "resolution_status": "unresolved",
        "resolved_metric_key": "",
        "preferred_anchor": "",
        "is_legacy_alias": False,
        "is_audit_anchor": False,
        "comparison_mode": "raw_decimal",
    }


def render_preferred_silver_citation(metric_key: str, registry: Mapping[str, Any]) -> str:
    contract = dict((registry or {}).get("contract") or {})
    entry = dict(contract.get(str(metric_key)) or {})
    preferred_anchor = str(entry.get("preferred_anchor") or metric_key)
    return f"[Silver: {preferred_anchor}]"


def canonical_query_family(query_family: str) -> str:
    family = str(query_family or "").strip().lower()
    return QUERY_FAMILY_ALIASES.get(family, family)


def _normalize_text(text: str) -> str:
    chars: List[str] = []
    for ch in str(text or "").lower():
        chars.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(chars).split())


def _contains_alias(text: str, aliases: List[str]) -> bool:
    normalized = f" {_normalize_text(text)} "
    for alias in aliases:
        probe = f" {_normalize_text(alias)} "
        if probe.strip() and probe in normalized:
            return True
    return False


def _candidate_keys_for_token(token: str, silver_values: Mapping[str, Any]) -> List[str]:
    keys: List[str] = []
    if token == "sec_gold_summary":
        return keys
    if token in silver_values:
        return [token]
    suffix = f"_{token}"
    for key, value in silver_values.items():
        if value is None:
            continue
        if key == token or key.endswith(suffix):
            keys.append(str(key))
    return keys


def _chunk_metadata(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return dict(chunk.get("metadata") or {})
    return dict(getattr(chunk, "metadata", None) or {})


def _chunk_source_type(chunk: Any) -> str:
    raw = getattr(chunk, "source_type", None)
    if raw is None and isinstance(chunk, dict):
        raw = chunk.get("source_type", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _chunk_form_type(chunk: Any) -> str:
    md = _chunk_metadata(chunk)
    raw = md.get("form_type", "")
    return str(getattr(raw, "value", raw) or "").strip().upper()


def _token_available(token: str, silver_values: Mapping[str, Any], gold_ctx: List[Any]) -> bool:
    if token in {"sec_gold_summary", "sec_event_gold_summary", "news_gold_summary"}:
        for chunk in gold_ctx or []:
            source_type = _chunk_source_type(chunk)
            if token == "news_gold_summary" and source_type == "news":
                return True
            if token == "sec_gold_summary" and source_type == "sec" and _chunk_form_type(chunk) == "4":
                return True
            if token == "sec_event_gold_summary" and source_type == "sec" and _chunk_form_type(chunk) == "8-K":
                return True
        return False
    return bool(_candidate_keys_for_token(token, silver_values))


def _group(
    group_id: str,
    label: str,
    evidence_tokens: List[str],
    min_tokens_required: int | None = None,
    weight: float = 1.0,
) -> Dict[str, Any]:
    return {
        "group_id": group_id,
        "label": label,
        "evidence_tokens": evidence_tokens,
        "min_tokens_required": min_tokens_required or len(evidence_tokens),
        "weight": weight,
    }


def build_slot_evidence_contracts(
    query_family: str,
    query_slots: Mapping[str, str] | None,
    capability_profile: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    family = canonical_query_family(query_family) or "options_microstructure"
    slot_labels = dict(query_slots or {}) or query_slots_for_family(family)
    capability = dict(capability_profile or {})

    contracts: Dict[str, Any] = {}

    if family == "insider_flow_driven":
        wanted_slots = set(slot_labels.keys()) or {"sec_insider_signal", "options_liquidity_posture"}
        if "sec_insider_signal" in wanted_slots:
            contracts["sec_insider_signal"] = {
                "slot_name": "sec_insider_signal",
                "slot_label": slot_labels.get("sec_insider_signal", "Form-4 insider signal"),
                "satisfaction_mode": "evidence_or_disclose",
                "required_disclosures": ["sec", "sec_insider_signal"],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("sec_gold_summary", "SEC / Form-4 evidence", ["sec_gold_summary"]),
                ],
            }
        if "sec_event_signal" in wanted_slots:
            contracts["sec_event_signal"] = {
                "slot_name": "sec_event_signal",
                "slot_label": slot_labels.get("sec_event_signal", "SEC 8-K or event filing signal"),
                "satisfaction_mode": "evidence_or_disclose",
                "required_disclosures": ["sec", "sec_event_signal"],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("sec_event_gold_summary", "SEC / 8-K event filing evidence", ["sec_event_gold_summary"]),
                ],
            }
        if "options_liquidity_posture" in wanted_slots:
            contracts["options_liquidity_posture"] = {
                "slot_name": "options_liquidity_posture",
                "slot_label": slot_labels.get("options_liquidity_posture", "options liquidity posture"),
                "satisfaction_mode": "evidence_required",
                "required_disclosures": [],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("executable_volume_plus_oi", "executable option volume + executable open interest", ["executable_option_volume", "executable_open_interest"]),
                    _group("risk_plus_liquidity", "market impact risk + one executable liquidity anchor", ["market_impact_risk", "liquid_contracts", "executable_open_interest", "executable_option_volume", "avg_spread_pct"], min_tokens_required=2, weight=0.8),
                ],
            }
    elif family == "cross_asset_regime":
        contracts["equity_vol_signal"] = {
            "slot_name": "equity_vol_signal",
            "slot_label": slot_labels.get("equity_vol_signal", "equity implied-volatility signal"),
            "satisfaction_mode": "evidence_required",
            "required_disclosures": [],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("atm_iv", "ATM IV", ["latest_atm_iv"]),
            ],
        }
        contracts["macro_vol_signal"] = {
            "slot_name": "macro_vol_signal",
            "slot_label": slot_labels.get("macro_vol_signal", "macro volatility signal"),
            "satisfaction_mode": "evidence_required",
            "required_disclosures": [],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("vix_value", "VIX value", ["VIX_value"]),
            ],
        }
        contracts["supporting_context"] = {
            "slot_name": "supporting_context",
            "slot_label": slot_labels.get("supporting_context", "supporting macro or liquidity context"),
            "satisfaction_mode": "evidence_required",
            "required_disclosures": [],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("macro_or_liquidity_anchor", "one relevant macro/liquidity anchor", ["DXY_value", "market_impact_risk", "liquid_contracts", "executable_open_interest"], min_tokens_required=1),
            ],
        }
    elif family == "geopolitical_macro_read":
        contracts["latest_geopolitical_risk_anchor"] = {
            "slot_name": "latest_geopolitical_risk_anchor",
            "slot_label": slot_labels.get("latest_geopolitical_risk_anchor", "latest geopolitical risk anchor"),
            "satisfaction_mode": "evidence_required",
            "required_disclosures": [],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("gpr_index", "GPR index", ["gpr_index_level"]),
            ],
        }
        contracts["geopolitical_news_signal"] = {
            "slot_name": "geopolitical_news_signal",
            "slot_label": slot_labels.get("geopolitical_news_signal", "geopolitical news signal"),
            "satisfaction_mode": "evidence_or_disclose",
            "required_disclosures": ["news", "geopolitical_news_signal"],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("topic_news", "one relevant geopolitical news narrative", ["news_gold_summary"], min_tokens_required=1),
            ],
        }
        contracts["impact_basket_context"] = {
            "slot_name": "impact_basket_context",
            "slot_label": slot_labels.get("impact_basket_context", "primary asset or impact-basket context"),
            "satisfaction_mode": "evidence_required",
            "required_disclosures": [],
            "min_groups_required": 1,
            "evidence_groups": [
                _group(
                    "impact_basket_anchor",
                    "one relevant impact-basket anchor",
                    ["GLD_value", "GLD_change_pct", "SLV_value", "SLV_change_pct", "VIX_value", "DXY_value", "GSPC_value", "GSPC_change_pct"],
                    min_tokens_required=1,
                ),
            ],
        }
    elif family == "geopolitical_options_read":
        contracts["geopolitical_risk_signal"] = {
            "slot_name": "geopolitical_risk_signal",
            "slot_label": slot_labels.get("geopolitical_risk_signal", "geopolitical risk signal"),
            "satisfaction_mode": "evidence_required",
            "required_disclosures": [],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("gpr_index", "GPR index", ["gpr_index_level"]),
            ],
        }
        contracts["options_vol_signal"] = {
            "slot_name": "options_vol_signal",
            "slot_label": slot_labels.get("options_vol_signal", "commodity options volatility posture"),
            "satisfaction_mode": "evidence_or_disclose" if not capability.get("has_options_chain_support") else "evidence_required",
            "required_disclosures": ["options", "options_vol_signal"],
            "min_groups_required": 1,
            "evidence_groups": [
                _group("atm_iv", "ATM IV", ["latest_atm_iv"]),
                _group(
                    "options_board_state",
                    "options board state",
                    ["GLD_avg_bid", "GLD_avg_ask", "GLD_avg_spread_pct", "GLD_avg_last_price"],
                    min_tokens_required=1,
                    weight=0.8,
                ),
            ],
        }
    else:
        wanted_slots = set(slot_labels.keys()) or {"pcr_signal", "atm_iv_signal", "liquidity_signal"}
        if "pcr_signal" in wanted_slots:
            contracts["pcr_signal"] = {
                "slot_name": "pcr_signal",
                "slot_label": slot_labels.get("pcr_signal", "put/call ratio signal"),
                "satisfaction_mode": "evidence_required",
                "required_disclosures": [],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("pcr_signal", "PCR signal", ["pcr_volume", "pcr_open_interest"], min_tokens_required=1),
                ],
            }
        if "iv_skew_signal" in wanted_slots:
            contracts["iv_skew_signal"] = {
                "slot_name": "iv_skew_signal",
                "slot_label": slot_labels.get("iv_skew_signal", "IV skew signal"),
                "satisfaction_mode": "evidence_required",
                "required_disclosures": [],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("iv_skew", "IV skew", ["latest_iv_skew"]),
                ],
            }
        if "iv_or_skew_signal" in wanted_slots:
            contracts["iv_or_skew_signal"] = {
                "slot_name": "iv_or_skew_signal",
                "slot_label": slot_labels.get("iv_or_skew_signal", "IV / skew signal"),
                "satisfaction_mode": "evidence_required",
                "required_disclosures": [],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("atm_iv_or_skew", "ATM IV or skew", ["latest_iv_skew", "latest_atm_iv"], min_tokens_required=1),
                ],
            }
        if "atm_iv_signal" in wanted_slots:
            contracts["atm_iv_signal"] = {
                "slot_name": "atm_iv_signal",
                "slot_label": slot_labels.get("atm_iv_signal", "at-the-money implied volatility signal"),
                "satisfaction_mode": "evidence_required",
                "required_disclosures": [],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("atm_iv", "ATM IV", ["latest_atm_iv"]),
                ],
            }
        if "liquidity_signal" in wanted_slots:
            contracts["liquidity_signal"] = {
                "slot_name": "liquidity_signal",
                "slot_label": slot_labels.get("liquidity_signal", "options liquidity posture"),
                "satisfaction_mode": "evidence_required",
                "required_disclosures": [],
                "min_groups_required": 1,
                "evidence_groups": [
                    _group("liquidity_anchor", "one executable liquidity metric", ["liquid_contracts", "executable_open_interest", "executable_option_volume", "avg_spread_pct"], min_tokens_required=1),
                ],
            }

    return contracts


def _disclosure_targets_for_slot(slot_name: str, contract: Mapping[str, Any]) -> List[str]:
    targets: List[str] = []
    for raw in list(contract.get("required_disclosures") or []) + [slot_name]:
        token = str(raw or "").strip()
        if not token:
            continue
        targets.extend(DISCLOSURE_TARGET_ALIASES.get(token, []))
        if token not in DISCLOSURE_TARGET_ALIASES:
            targets.append(token.replace("_", " "))
    label = str(contract.get("slot_label") or "").strip()
    if label:
        targets.append(label)
    out: List[str] = []
    seen = set()
    for target in targets:
        normalized = _normalize_text(target)
        if normalized and normalized not in seen:
            out.append(target)
            seen.add(normalized)
    return out


def _has_disclosure_phrase(text: str) -> bool:
    return any(_contains_alias(text, [phrase]) for phrase in DISCLOSURE_PHRASES)


def _slot_disclosure_present(answer: str, slot_name: str, contract: Mapping[str, Any]) -> bool:
    if not _has_disclosure_phrase(answer):
        return False
    targets = _disclosure_targets_for_slot(slot_name, contract)
    if not targets:
        return False
    return any(_contains_alias(answer, [target]) for target in targets)


def _group_match_summary(
    answer: str,
    group: Mapping[str, Any],
    silver_values: Mapping[str, Any],
    gold_ctx: List[Any],
) -> Dict[str, Any]:
    tokens = [str(token) for token in (group.get("evidence_tokens") or []) if str(token).strip()]
    min_tokens_required = max(int(group.get("min_tokens_required", len(tokens)) or len(tokens)), 1)
    available_tokens = [token for token in tokens if _token_available(token, silver_values, gold_ctx)]
    if len(available_tokens) < min_tokens_required:
        return {
            "active": False,
            "matched_tokens": 0,
            "available_tokens": available_tokens,
            "min_tokens_required": min_tokens_required,
        }
    matched_tokens = 0
    for token in available_tokens:
        aliases = _TOKEN_TEXT_ALIASES.get(token, [token.replace("_", " ")])
        if _contains_alias(answer, aliases):
            matched_tokens += 1
    return {
        "active": True,
        "matched_tokens": matched_tokens,
        "available_tokens": available_tokens,
        "min_tokens_required": min_tokens_required,
        "coverage": (matched_tokens / len(available_tokens)) if available_tokens else 0.0,
        "satisfied": matched_tokens >= min_tokens_required,
        "weight": float(group.get("weight", 1.0) or 1.0),
    }


def semantic_slot_evidence_eval(
    slot_contracts: Mapping[str, Any] | None,
    answer: str,
    retrieval_outcome: Mapping[str, Any] | None,
    silver_values: Mapping[str, Any] | None,
    gold_ctx: List[Any] | None,
) -> Dict[str, Any]:
    contracts = dict(slot_contracts or {})
    values = dict(silver_values or {})
    missing_slots = {str(slot).strip() for slot in (dict(retrieval_outcome or {}).get("missing_query_slots") or []) if str(slot).strip()}

    slot_status: Dict[str, str] = {}
    slot_evidence_strength: Dict[str, Optional[str]] = {}
    slot_disclosure_honesty: Dict[str, Optional[bool]] = {}
    slot_strength_scores: Dict[str, float] = {}
    contract_conflicts: Dict[str, bool] = {}

    for slot_name, contract in contracts.items():
        slot_name_s = str(slot_name)
        groups = list(contract.get("evidence_groups") or [])
        mode = str(contract.get("satisfaction_mode") or "evidence_required")
        disclosure_present = _slot_disclosure_present(answer, slot_name_s, contract)

        summaries = [_group_match_summary(answer, group, values, gold_ctx or []) for group in groups]
        active = [summary for summary in summaries if summary.get("active")]
        satisfied = [summary for summary in active if summary.get("satisfied")]
        evidence_available = bool(active)
        contract_conflicts[slot_name_s] = bool(slot_name_s in missing_slots and evidence_available)

        if slot_name_s in missing_slots and not evidence_available:
            if disclosure_present:
                slot_status[slot_name_s] = "disclosed_unanswerable"
                slot_disclosure_honesty[slot_name_s] = True
                slot_strength_scores[slot_name_s] = 0.75
            else:
                slot_status[slot_name_s] = "missed"
                slot_disclosure_honesty[slot_name_s] = False
                slot_strength_scores[slot_name_s] = 0.0
            slot_evidence_strength[slot_name_s] = None
            continue

        if active:
            avg_coverage = sum(float(summary.get("coverage", 0.0)) for summary in active) / len(active)
        else:
            avg_coverage = 0.0

        if not active:
            if mode == "evidence_or_disclose" and disclosure_present:
                slot_status[slot_name_s] = "disclosed_unanswerable"
                slot_disclosure_honesty[slot_name_s] = True
                slot_strength_scores[slot_name_s] = 0.75
            elif mode == "evidence_or_disclose":
                slot_status[slot_name_s] = "missed"
                slot_disclosure_honesty[slot_name_s] = False
                slot_strength_scores[slot_name_s] = 0.0
            else:
                slot_status[slot_name_s] = "unsupported_by_retrieval"
                slot_disclosure_honesty[slot_name_s] = None
                slot_strength_scores[slot_name_s] = 0.0
            slot_evidence_strength[slot_name_s] = None
            continue

        if len(satisfied) >= max(int(contract.get("min_groups_required", 1) or 1), 1):
            slot_status[slot_name_s] = "answered"
            slot_disclosure_honesty[slot_name_s] = None
            if avg_coverage >= 0.95:
                slot_evidence_strength[slot_name_s] = "strong"
                slot_strength_scores[slot_name_s] = 1.0
            elif avg_coverage >= 0.6:
                slot_evidence_strength[slot_name_s] = "acceptable"
                slot_strength_scores[slot_name_s] = 0.75
            else:
                slot_evidence_strength[slot_name_s] = "weak"
                slot_strength_scores[slot_name_s] = 0.5
            continue

        if mode == "evidence_or_disclose" and disclosure_present:
            slot_status[slot_name_s] = "disclosed_unanswerable"
            slot_disclosure_honesty[slot_name_s] = True
            slot_evidence_strength[slot_name_s] = None
            slot_strength_scores[slot_name_s] = 0.75
        else:
            slot_status[slot_name_s] = "missed"
            slot_disclosure_honesty[slot_name_s] = False if disclosure_present else None
            slot_evidence_strength[slot_name_s] = "weak" if avg_coverage > 0 else None
            slot_strength_scores[slot_name_s] = 0.0

    applicable = [status for status in slot_status.values() if status != "not_applicable"]
    answered_like = [status for status in slot_status.values() if status in {"answered", "disclosed_unanswerable"}]
    disclosure_values = [value for value in slot_disclosure_honesty.values() if value is not None]
    semantic_scores = [slot_strength_scores.get(slot, 0.0) for slot in slot_status]

    return {
        "slot_status": slot_status,
        "slot_evidence_strength": slot_evidence_strength,
        "slot_disclosure_honesty": slot_disclosure_honesty,
        "slot_answer_rate": round(len(answered_like) / len(applicable), 4) if applicable else 1.0,
        "slot_disclosure_honesty_rate": round(sum(1.0 for value in disclosure_values if value) / len(disclosure_values), 4) if disclosure_values else 1.0,
        "semantic_evidence_carry_score": round(sum(semantic_scores) / len(semantic_scores), 4) if semantic_scores else 1.0,
        "contract_conflicts": contract_conflicts,
    }


def evaluate_truth_slot_semantics(
    intent_slots: Mapping[str, str] | None,
    slot_contracts: Mapping[str, Any] | None,
    answer: str,
    retrieval_outcome: Mapping[str, Any] | None,
    silver_values: Mapping[str, Any] | None,
    gold_ctx: List[Any] | None,
) -> Dict[str, Any]:
    contract_eval = semantic_slot_evidence_eval(slot_contracts, answer, retrieval_outcome, silver_values, gold_ctx)
    contract_status = dict(contract_eval.get("slot_status") or {})
    contract_strength = dict(contract_eval.get("slot_evidence_strength") or {})
    contract_honesty = dict(contract_eval.get("slot_disclosure_honesty") or {})

    slot_status: Dict[str, str] = {}
    slot_strength: Dict[str, Optional[str]] = {}
    slot_honesty: Dict[str, Optional[bool]] = {}
    slot_scores: List[float] = []
    truth_contract_conflicts: Dict[str, bool] = {}

    for truth_slot in dict(intent_slots or {}):
        mapped_slots = TRUTH_SLOT_TO_CONTRACT_SLOTS.get(str(truth_slot), [str(truth_slot)])
        statuses = [contract_status.get(slot, "not_applicable") for slot in mapped_slots]
        strengths = [contract_strength.get(slot) for slot in mapped_slots if contract_strength.get(slot)]
        honesty_values = [contract_honesty.get(slot) for slot in mapped_slots if contract_honesty.get(slot) is not None]
        truth_contract_conflicts[str(truth_slot)] = any(
            bool((contract_eval.get("contract_conflicts") or {}).get(slot, False))
            for slot in mapped_slots
        )

        if statuses and all(status == "answered" for status in statuses):
            slot_status[str(truth_slot)] = "answered"
            if strengths and all(strength == "strong" for strength in strengths):
                slot_strength[str(truth_slot)] = "strong"
                slot_scores.append(1.0)
            elif strengths and all(strength in {"strong", "acceptable"} for strength in strengths):
                slot_strength[str(truth_slot)] = "acceptable"
                slot_scores.append(0.75)
            else:
                slot_strength[str(truth_slot)] = "weak"
                slot_scores.append(0.5)
            slot_honesty[str(truth_slot)] = None
        elif statuses and all(status in {"answered", "disclosed_unanswerable"} for status in statuses) and "disclosed_unanswerable" in statuses:
            slot_status[str(truth_slot)] = "disclosed_unanswerable"
            slot_strength[str(truth_slot)] = None
            slot_honesty[str(truth_slot)] = all(bool(value) for value in honesty_values) if honesty_values else True
            slot_scores.append(0.75 if slot_honesty[str(truth_slot)] else 0.0)
        elif statuses and all(status in {"unsupported_by_retrieval", "not_applicable"} for status in statuses):
            slot_status[str(truth_slot)] = "unsupported_by_retrieval"
            slot_strength[str(truth_slot)] = None
            slot_honesty[str(truth_slot)] = None
        else:
            slot_status[str(truth_slot)] = "missed"
            slot_strength[str(truth_slot)] = None
            slot_honesty[str(truth_slot)] = False if honesty_values else None
            slot_scores.append(0.0)

    applicable = [status for status in slot_status.values() if status not in {"unsupported_by_retrieval", "not_applicable"}]
    answered_like = [status for status in slot_status.values() if status in {"answered", "disclosed_unanswerable"}]
    honesty_values = [value for value in slot_honesty.values() if value is not None]

    return {
        "slot_status": slot_status,
        "slot_evidence_strength": slot_strength,
        "slot_disclosure_honesty": slot_honesty,
        "slot_answer_rate": round(len(answered_like) / len(applicable), 4) if applicable else 1.0,
        "slot_disclosure_honesty_rate": round(sum(1.0 for value in honesty_values if value) / len(honesty_values), 4) if honesty_values else 1.0,
        "semantic_evidence_carry_score": round(sum(slot_scores) / len(slot_scores), 4) if slot_scores else 1.0,
        "contract_source": "runtime_slot_contracts" if slot_contracts else "structured_truth_only",
        "contract_slot_status": contract_status,
        "contract_conflicts": truth_contract_conflicts,
    }


def render_slot_evidence_contract_block(slot_contracts: Mapping[str, Any] | None) -> str:
    contracts = dict(slot_contracts or {})
    if not contracts:
        return "No slot-level evidence contract was available in this run; carry the strongest supported evidence."

    lines = ["Slot-level evidence contract:"]
    for slot_name, contract in contracts.items():
        label = str(contract.get("slot_label") or slot_name)
        groups = [
            str(group.get("label") or group.get("group_id") or "evidence group")
            for group in (contract.get("evidence_groups") or [])
        ]
        mode = str(contract.get("satisfaction_mode") or "evidence_required")
        if groups:
            lines.append(f"- {label} [{mode}]: " + "; ".join(groups))
        else:
            lines.append(f"- {label} [{mode}]")
    return "\n".join(lines)


def evaluate_retrieval_slot_support(
    slot_contracts: Mapping[str, Any] | None,
    retrieval_outcome: Mapping[str, Any] | None,
    silver_values: Mapping[str, Any] | None,
    gold_ctx: List[Any] | None,
) -> Dict[str, Any]:
    contracts = dict(slot_contracts or {})
    values = dict(silver_values or {})
    outcome = dict(retrieval_outcome or {})
    missing_slots = {
        str(slot).strip()
        for slot in (outcome.get("missing_query_slots") or [])
        if str(slot).strip()
    }
    news_coverage_status = str(outcome.get("news_coverage_status") or "").strip().lower()
    background_only_read = bool(outcome.get("background_only_read"))

    slot_status: Dict[str, str] = {}
    slot_strength_scores: Dict[str, float] = {}

    for slot_name, contract in contracts.items():
        slot_name_s = str(slot_name)
        if (
            slot_name_s == "geopolitical_news_signal"
            and background_only_read
            and news_coverage_status == "no_fresh_news_retrieved"
        ):
            slot_status[slot_name_s] = "coverage_disclosed"
            slot_strength_scores[slot_name_s] = 0.75
            continue
        groups = list(contract.get("evidence_groups") or [])
        min_groups_required = max(int(contract.get("min_groups_required", 1) or 1), 1)
        active_groups = 0
        satisfied_groups = 0

        for group in groups:
            group_tokens = [str(token) for token in (group.get("evidence_tokens") or []) if str(token).strip()]
            min_tokens_required = max(int(group.get("min_tokens_required", len(group_tokens)) or len(group_tokens)), 1)
            available_tokens = [token for token in group_tokens if _token_available(token, values, gold_ctx or [])]
            if len(available_tokens) < min_tokens_required:
                continue
            active_groups += 1
            satisfied_groups += 1

        if slot_name_s in missing_slots and satisfied_groups < min_groups_required:
            slot_status[slot_name_s] = "missing_required_retrieval"
            slot_strength_scores[slot_name_s] = 0.0
        elif active_groups == 0:
            slot_status[slot_name_s] = "unsupported_by_retrieval"
            slot_strength_scores[slot_name_s] = 0.0
        elif satisfied_groups >= min_groups_required:
            slot_status[slot_name_s] = "retrieved"
            slot_strength_scores[slot_name_s] = 1.0
        else:
            slot_status[slot_name_s] = "partial"
            slot_strength_scores[slot_name_s] = 0.5

    applicable = [status for status in slot_status.values() if status != "not_applicable"]
    retrieved = [status for status in slot_status.values() if status in {"retrieved", "coverage_disclosed"}]
    hard_gate_pass = bool(slot_status) and all(status in {"retrieved", "coverage_disclosed"} for status in slot_status.values())

    return {
        "slot_status": slot_status,
        "slot_support_rate": round(len(retrieved) / len(applicable), 4) if applicable else 1.0,
        "hard_gate_pass": hard_gate_pass if slot_status else bool(silver_values),
        "retrieval_support_score": round(sum(slot_strength_scores.values()) / len(slot_strength_scores), 4) if slot_strength_scores else (1.0 if silver_values else 0.0),
    }


def evaluate_slot_coverage(
    slot_contracts: Mapping[str, Any] | None,
    draft: str,
    retrieval_outcome: Mapping[str, Any] | None,
    silver_values: Mapping[str, Any] | None,
    gold_ctx: List[Any] | None,
) -> Dict[str, Any]:
    contracts = dict(slot_contracts or {})
    values = dict(silver_values or {})
    outcome = dict(retrieval_outcome or {})
    missing_slots = {str(slot).strip() for slot in (outcome.get("missing_query_slots") or []) if str(slot).strip()}
    news_coverage_status = str(outcome.get("news_coverage_status") or "").strip().lower()
    background_only_read = bool(outcome.get("background_only_read"))

    evidence_section = draft or ""
    total_weight = 0.0
    achieved_weight = 0.0
    present_labels: List[str] = []
    missing_labels: List[str] = []
    slot_status: Dict[str, str] = {}

    for slot_name, contract in contracts.items():
        slot_name_s = str(slot_name)
        if slot_name_s in missing_slots:
            slot_status[slot_name_s] = "not_reliably_answerable"
            continue
        if (
            slot_name_s == "geopolitical_news_signal"
            and background_only_read
            and news_coverage_status == "no_fresh_news_retrieved"
        ):
            if _slot_disclosure_present(evidence_section, slot_name_s, contract):
                slot_status[slot_name_s] = "coverage_disclosed"
            else:
                slot_status[slot_name_s] = "not_reliably_answerable"
            continue

        groups = list(contract.get("evidence_groups") or [])
        min_groups_required = max(int(contract.get("min_groups_required", 1) or 1), 1)
        satisfied_groups = 0
        active_groups = 0

        for group in groups:
            group_tokens = [str(token) for token in (group.get("evidence_tokens") or []) if str(token).strip()]
            min_tokens_required = max(int(group.get("min_tokens_required", len(group_tokens)) or len(group_tokens)), 1)
            available_tokens = [token for token in group_tokens if _token_available(token, values, gold_ctx or [])]
            if len(available_tokens) < min_tokens_required:
                continue

            active_groups += 1
            total_weight += float(group.get("weight", 1.0) or 1.0)
            matched_tokens = 0
            for token in available_tokens:
                aliases = _TOKEN_TEXT_ALIASES.get(token, [token.replace("_", " ")])
                if _contains_alias(evidence_section, aliases):
                    matched_tokens += 1
            label = str(group.get("label") or group.get("group_id") or "evidence group")
            if matched_tokens >= min_tokens_required:
                satisfied_groups += 1
                achieved_weight += float(group.get("weight", 1.0) or 1.0)
                present_labels.append(label)
            else:
                missing_labels.append(label)

        if active_groups == 0:
            slot_status[slot_name_s] = "unsupported_by_retrieval"
        elif satisfied_groups >= min_groups_required:
            slot_status[slot_name_s] = "answered"
        else:
            slot_status[slot_name_s] = "incomplete"

    coverage_score = achieved_weight / total_weight if total_weight > 0 else 1.0
    hard_gate_pass = all(status in {"answered", "not_reliably_answerable"} for status in slot_status.values())

    return {
        "slot_status": slot_status,
        "query_family_slots": list(contracts.keys()),
        "required_evidence_keys_present": sorted(set(present_labels)),
        "required_evidence_keys_missing": sorted(set(missing_labels)),
        "required_evidence_count_met": hard_gate_pass,
        "coverage_score": round(coverage_score, 4),
        "hard_gate_pass": hard_gate_pass,
    }
