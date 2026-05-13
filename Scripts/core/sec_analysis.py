from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from Scripts.core.sec_contract import (
    SECAnalysisBundle,
    SECCoverageContract,
    SECCoverageMode,
    SECExistenceResult,
    Form4AnalysisContract,
    Form4Feature,
    Form8KAnalysisContract,
    Form8KFeature,
    SECMissingDisclosure,
)


_VALID_SEC_FORMS = ("4", "8-K")
_SLOT_BY_FORM = {
    "4": "sec_insider_signal",
    "8-K": "sec_event_signal",
}


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _chunk_metadata(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return dict(chunk.get("metadata") or {})
    return dict(getattr(chunk, "metadata", None) or {})


def _chunk_as_dict(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return dict(chunk)
    model_dump = getattr(chunk, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {
        "content": _chunk_content(chunk),
        "metadata": _chunk_metadata(chunk),
    }


def _chunk_content(chunk: Any) -> str:
    return str(_obj_get(chunk, "content", "") or "").strip()


def _chunk_form_type(chunk: Any) -> str:
    metadata = _chunk_metadata(chunk)
    return str(metadata.get("form_type") or "").strip().upper()


def _normalize_forms(values: List[Any] | None) -> List[str]:
    out: List[str] = []
    for raw in values or []:
        form = str(getattr(raw, "value", raw) or "").strip().upper()
        if form in _VALID_SEC_FORMS and form not in out:
            out.append(form)
    return out


def _normalize_payload_context_by_form(
    sec_payload_context_by_form: Mapping[str, Any] | None,
    *,
    sec_forms_requested: List[str] | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    raw = dict(sec_payload_context_by_form or {})
    normalized: Dict[str, List[Dict[str, Any]]] = {}
    for form, chunks in raw.items():
        form_key = str(form or "").strip().upper()
        if form_key not in _VALID_SEC_FORMS:
            continue
        normalized[form_key] = [_chunk_as_dict(chunk) for chunk in list(chunks or [])]
    if sec_forms_requested:
        ordered: Dict[str, List[Dict[str, Any]]] = {}
        for form in _normalize_forms(sec_forms_requested):
            ordered[form] = list(normalized.get(form, []) or [])
        return ordered
    return normalized


def build_sec_existence_result(
    *,
    sec_forms_requested: List[str] | None,
    sec_payload_context_by_form: Mapping[str, Any] | None,
) -> SECExistenceResult:
    requested = _normalize_forms(sec_forms_requested)
    normalized_payload = _normalize_payload_context_by_form(
        sec_payload_context_by_form,
        sec_forms_requested=requested,
    )
    if requested:
        retrieved = [form for form in requested if normalized_payload.get(form)]
    else:
        retrieved = [form for form, chunks in normalized_payload.items() if chunks]
    sec_slot_hits = [_SLOT_BY_FORM[form] for form in retrieved if form in _SLOT_BY_FORM]
    sec_slot_missing = [_SLOT_BY_FORM[form] for form in requested if form not in retrieved and form in _SLOT_BY_FORM]
    return SECExistenceResult(
        sec_forms_requested=requested,
        sec_forms_retrieved=retrieved,
        sec_payload_context_by_form=normalized_payload,
        sec_slot_hits=sec_slot_hits,
        sec_slot_missing=sec_slot_missing,
    )


def derive_sec_coverage_contract(existence: SECExistenceResult) -> SECCoverageContract:
    requested = list(existence.sec_forms_requested or [])
    retrieved = list(existence.sec_forms_retrieved or [])
    missing_forms = [form for form in requested if form not in retrieved]
    coverage_mode: SECCoverageMode = "none"
    if requested and len(retrieved) == len(requested):
        coverage_mode = "full"
    elif retrieved:
        coverage_mode = "partial"
    return SECCoverageContract(
        sec_forms_requested=requested,
        sec_forms_retrieved=retrieved,
        missing_forms=missing_forms,
        coverage_mode=coverage_mode,
        sec_slot_hits=list(existence.sec_slot_hits or []),
        sec_slot_missing=list(existence.sec_slot_missing or []),
    )


def derive_sec_missing_disclosure(coverage: SECCoverageContract) -> SECMissingDisclosure:
    return SECMissingDisclosure(
        missing_forms=list(coverage.missing_forms or []),
        missing_slots=list(coverage.sec_slot_missing or []),
        coverage_mode=coverage.coverage_mode,
        has_missing=bool(coverage.missing_forms or coverage.sec_slot_missing),
    )


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _owner_from_content(content: str) -> str:
    clean = str(content or "").strip()
    if not clean:
        return ""
    if " (" in clean:
        return clean.split(" (", 1)[0].strip()
    if " of " in clean:
        return clean.split(" of ", 1)[0].strip()
    return clean[:80].strip()


def derive_form4_features(
    payload_chunks: List[Any],
    *,
    bronze_lookup: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
) -> List[Form4Feature]:
    features: List[Form4Feature] = []
    for chunk in payload_chunks or []:
        if _chunk_form_type(chunk) != "4":
            continue
        metadata = _chunk_metadata(chunk)
        ticker = str(metadata.get("ticker") or "").upper().strip()
        accession_no = str(metadata.get("accession_no") or "").strip()
        bronze_record = dict((bronze_lookup or (lambda *_: {}))(ticker, accession_no) or {})
        parsed = dict(bronze_record.get("parsed_data") or {})
        transactions = list(parsed.get("transactions") or [])

        total_shares = 0.0
        total_value = 0.0
        weighted_value = 0.0
        remaining_shares: Optional[float] = None
        is_10b5_1_planned = False
        transaction_date = str(metadata.get("transaction_date") or "").strip()

        for tx in transactions:
            shares = _safe_float(tx.get("shares")) or 0.0
            price = _safe_float(tx.get("price"))
            tx_value = _safe_float(tx.get("total_value")) or 0.0
            post_shares = _safe_float(tx.get("post_transaction_shares"))
            tx_date = str(tx.get("date") or "").strip()
            total_shares += abs(shares)
            total_value += abs(tx_value)
            if price is not None and shares:
                weighted_value += abs(shares) * price
            if post_shares is not None:
                remaining_shares = post_shares
            if bool(tx.get("is_10b5_1_planned")):
                is_10b5_1_planned = True
            if not transaction_date and tx_date:
                transaction_date = tx_date

        avg_price = (weighted_value / total_shares) if total_shares > 0 and weighted_value > 0 else None
        features.append(
            Form4Feature(
                ticker=ticker,
                accession_no=accession_no,
                owner=str(parsed.get("reporting_owner") or _owner_from_content(_chunk_content(chunk))).strip(),
                role=str(parsed.get("role") or "").strip(),
                action_direction=str(metadata.get("action_direction") or "").upper().strip() or "NONE",
                transaction_date=transaction_date or str(metadata.get("filed_at") or "").strip(),
                filed_at=str(metadata.get("filed_at") or "").strip(),
                shares=total_shares if total_shares > 0 else None,
                price=avg_price,
                total_value=total_value if total_value > 0 else None,
                remaining_shares=remaining_shares,
                is_10b5_1_planned=is_10b5_1_planned,
                content=_chunk_content(chunk),
                url=str(metadata.get("url") or "").strip(),
            )
        )

    cluster_counts: Dict[tuple[str, str], int] = {}
    for feature in features:
        cluster_key = (feature.ticker, feature.transaction_date)
        cluster_counts[cluster_key] = cluster_counts.get(cluster_key, 0) + 1

    normalized: List[Form4Feature] = []
    for feature in features:
        cluster_key = (feature.ticker, feature.transaction_date)
        cluster_count = cluster_counts.get(cluster_key, 1)
        normalized.append(
            feature.model_copy(
                update={
                    "is_cluster_trade": cluster_count > 1,
                    "cluster_count": cluster_count,
                }
            )
        )
    return normalized


def derive_form8k_features(payload_chunks: List[Any]) -> List[Form8KFeature]:
    features: List[Form8KFeature] = []
    for chunk in payload_chunks or []:
        if _chunk_form_type(chunk) != "8-K":
            continue
        metadata = _chunk_metadata(chunk)
        features.append(
            Form8KFeature(
                ticker=str(metadata.get("ticker") or "").upper().strip(),
                accession_no=str(metadata.get("accession_no") or "").strip(),
                filed_at=str(metadata.get("filed_at") or "").strip(),
                tone_score=_safe_float(metadata.get("tone_score")),
                topics=[str(topic).strip() for topic in list(metadata.get("topics") or []) if str(topic).strip()],
                entities=[str(entity).strip() for entity in list(metadata.get("entities") or []) if str(entity).strip()],
                content=_chunk_content(chunk),
                url=str(metadata.get("url") or "").strip(),
            )
        )
    return features


def derive_form4_analysis_result(form4_features: List[Form4Feature]) -> Form4AnalysisContract:
    if not form4_features:
        return Form4AnalysisContract()
    sell_count = sum(1 for feature in form4_features if str(feature.action_direction or "").upper() == "SELL")
    buy_count = sum(1 for feature in form4_features if str(feature.action_direction or "").upper() == "BUY")
    vest_count = sum(1 for feature in form4_features if str(feature.action_direction or "").upper() == "ACQUIRE/VEST")
    planned_count = sum(1 for feature in form4_features if feature.is_10b5_1_planned)
    cluster_count = sum(1 for feature in form4_features if feature.is_cluster_trade)
    total_value = sum(float(feature.total_value or 0.0) for feature in form4_features if isinstance(feature.total_value, (int, float)))
    directional_read = "mixed"
    if sell_count > 0 and sell_count >= buy_count + vest_count:
        directional_read = "selling_pressure"
    elif buy_count > 0 and buy_count >= sell_count:
        directional_read = "buying_support"
    elif vest_count > 0 and sell_count == 0 and buy_count == 0:
        directional_read = "compensation_vesting"
    return Form4AnalysisContract(
        filing_count=len(form4_features),
        sell_count=sell_count,
        buy_count=buy_count,
        vest_count=vest_count,
        planned_count=planned_count,
        cluster_count=cluster_count,
        total_value=total_value if total_value > 0 else None,
        directional_read=directional_read,
    )


def derive_form8k_analysis_result(form8k_features: List[Form8KFeature]) -> Form8KAnalysisContract:
    if not form8k_features:
        return Form8KAnalysisContract()
    negative = sum(1 for feature in form8k_features if isinstance(feature.tone_score, (int, float)) and float(feature.tone_score) < 0)
    positive = sum(1 for feature in form8k_features if isinstance(feature.tone_score, (int, float)) and float(feature.tone_score) > 0)
    neutral = len(form8k_features) - negative - positive
    dominant_tone = "neutral"
    if negative > max(positive, neutral):
        dominant_tone = "negative"
    elif positive > max(negative, neutral):
        dominant_tone = "positive"
    categories: List[str] = []
    for feature in form8k_features:
        for topic in list(feature.topics or []):
            if topic and topic not in categories:
                categories.append(topic)
    latest_feature = sorted(form8k_features, key=lambda feature: feature.filed_at or "", reverse=True)[0]
    event_pressure = "contained"
    if dominant_tone == "negative":
        event_pressure = "elevated"
    elif dominant_tone == "positive":
        event_pressure = "constructive"
    elif len(form8k_features) > 1 and categories:
        event_pressure = "mixed"
    return Form8KAnalysisContract(
        filing_count=len(form8k_features),
        negative_count=negative,
        positive_count=positive,
        neutral_count=neutral,
        dominant_tone=dominant_tone,
        categories=categories,
        event_pressure=event_pressure,
        repeat_pattern="repeated" if len(form8k_features) > 1 else "isolated",
        latest_filing_date=latest_feature.filed_at,
        latest_filing_content=latest_feature.content,
    )


def compose_sec_analysis_bundle(
    *,
    sec_forms_requested: List[str] | None,
    sec_payload_context_by_form: Mapping[str, Any] | None,
    bronze_lookup: Optional[Callable[[str, str], Mapping[str, Any]]] = None,
) -> SECAnalysisBundle:
    existence = build_sec_existence_result(
        sec_forms_requested=sec_forms_requested,
        sec_payload_context_by_form=sec_payload_context_by_form,
    )
    coverage = derive_sec_coverage_contract(existence)
    form4_features = derive_form4_features(
        list(existence.sec_payload_context_by_form.get("4", []) or []),
        bronze_lookup=bronze_lookup,
    )
    form8k_features = derive_form8k_features(
        list(existence.sec_payload_context_by_form.get("8-K", []) or []),
    )
    form4_result = derive_form4_analysis_result(form4_features)
    form8k_result = derive_form8k_analysis_result(form8k_features)
    missing_disclosure = derive_sec_missing_disclosure(coverage)
    return SECAnalysisBundle(
        coverage=coverage,
        existence=existence,
        form4_features=form4_features,
        form8k_features=form8k_features,
        form4_analysis_result=form4_result,
        form8k_analysis_result=form8k_result,
        missing_disclosure=missing_disclosure,
    )
