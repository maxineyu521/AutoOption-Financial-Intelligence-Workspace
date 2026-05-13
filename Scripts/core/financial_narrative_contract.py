"""
Scripts/core/financial_narrative_contract.py

Deterministic public contract for macro/news narrative families.

This module deliberately contains auditable mapping and summarisation helpers,
not hidden chain-of-thought.  It translates ontology facts plus retrieved
evidence into compact narrative fields that downstream agents can render
without falling back to metric-line dumps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping

from Scripts.core.financial_ontology import (
    NEWS_TOPIC_EXPANSIONS,
    NEWS_TOPIC_KEYWORD_HINTS,
    TOPIC_IMPACT_BASKETS,
    TOPIC_TO_TICKERS,
    normalize_news_topic,
    tickers_from_impacted_assets,
)


@dataclass(frozen=True)
class NewsSemanticProfile:
    """Public, serialisable news-search profile for an asset/topic read."""

    tickers: List[str] = field(default_factory=list)
    canonical_topics: List[str] = field(default_factory=list)
    expanded_topics: List[str] = field(default_factory=list)
    news_search_terms: List[str] = field(default_factory=list)
    news_asset_terms: List[str] = field(default_factory=list)
    news_driver_terms: List[str] = field(default_factory=list)
<<<<<<< Updated upstream
=======
    dense_context_terms: List[str] = field(default_factory=list)
>>>>>>> Stashed changes
    impacted_asset_aliases: List[str] = field(default_factory=list)
    impact_basket: List[str] = field(default_factory=list)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NarrativeBrief:
    """Frontend-safe narrative fields consumed by Analyst/Finalizer."""

    headline_read: str = ""
    news_driver: str = ""
    macro_transmission: str = ""
    asset_reaction: str = ""
<<<<<<< Updated upstream
    risk_read: str = ""
=======
    game_theory_read: str = ""
    volatility_setup: str = ""
    risk_read: str = ""
    risk_trigger: str = ""
>>>>>>> Stashed changes
    what_would_change: str = ""

    def model_dump(self) -> Dict[str, str]:
        return asdict(self)


_TICKER_NEWS_LANGUAGE: Dict[str, Dict[str, List[str]]] = {
    "GLD": {
        "asset_terms": ["gold", "bullion", "precious metals", "safe haven"],
        "driver_terms": ["gold futures", "real yields", "treasury yields", "dollar", "dxy", "fed"],
<<<<<<< Updated upstream
=======
        "dense_context_terms": ["10-year Treasury yields", "10Y yields", "real yields", "FOMC", "Fed policy path"],
>>>>>>> Stashed changes
        "impacted_asset_aliases": ["Gold", "Precious Metals", "USD"],
        "topics": ["asset_precious_metals_spot", "asset_metals_derivatives", "macro_yields_dollar", "macro_central_banks"],
    },
    "SLV": {
        "asset_terms": ["silver", "precious metals", "gold silver ratio", "industrial demand"],
        "driver_terms": ["silver futures", "real yields", "dollar", "dxy", "fed", "comex"],
<<<<<<< Updated upstream
=======
        "dense_context_terms": ["10-year Treasury yields", "10Y yields", "real yields", "FOMC", "Fed policy path"],
>>>>>>> Stashed changes
        "impacted_asset_aliases": ["Silver", "Precious Metals", "USD"],
        "topics": ["asset_precious_metals_spot", "asset_metals_derivatives", "macro_yields_dollar", "macro_central_banks"],
    },
    "SPY": {
        "asset_terms": ["stocks", "equities", "s&p 500", "risk appetite"],
        "driver_terms": ["vix", "earnings", "fed", "treasury yields", "dollar"],
        "impacted_asset_aliases": ["Equities", "Stocks", "S&P 500"],
        "topics": ["macro_central_banks", "macro_yields_dollar", "macro_geopolitics_risk"],
    },
    "QQQ": {
        "asset_terms": ["nasdaq", "technology stocks", "growth equities"],
        "driver_terms": ["rates", "treasury yields", "fed", "dollar", "vix"],
        "impacted_asset_aliases": ["Nasdaq", "Tech", "Equities"],
        "topics": ["macro_central_banks", "macro_yields_dollar", "macro_geopolitics_risk"],
    },
    "^VIX": {
        "asset_terms": ["volatility", "vix", "risk sentiment"],
        "driver_terms": ["geopolitical risk", "equities", "hedging demand"],
        "impacted_asset_aliases": ["VIX", "Volatility", "Equities"],
        "topics": ["macro_geopolitics_risk", "macro_yields_dollar"],
    },
    "DX-Y.NYB": {
        "asset_terms": ["dollar", "dxy", "us dollar", "dollar index"],
        "driver_terms": ["fed", "treasury yields", "real yields", "rates"],
        "impacted_asset_aliases": ["USD", "US Dollar", "DXY"],
        "topics": ["macro_yields_dollar", "macro_central_banks"],
    },
}


def _dedupe(values: List[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in values or []:
        item = str(raw or "").strip()
        key = item.lower()
        if item and key not in seen:
            out.append(item)
            seen.add(key)
    return out


<<<<<<< Updated upstream
=======
def _profile_data(profile: Mapping[str, Any] | NewsSemanticProfile | Any | None) -> Dict[str, Any]:
    if profile is None:
        return {}
    if isinstance(profile, NewsSemanticProfile):
        return profile.model_dump()
    if isinstance(profile, Mapping):
        return dict(profile)
    model_dump = getattr(profile, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


>>>>>>> Stashed changes
_SPARSE_TERM_NORMALIZATION: Dict[str, str] = {
    "bullion": "gold",
    "precious metals": "gold",
    "safe haven": "gold",
    "gold futures": "gold",
    "silver futures": "silver",
    "gold silver ratio": "silver",
    "industrial demand": "silver",
    "treasury yields": "yields",
    "real yields": "yields",
    "treasury yield": "yields",
    "us dollar": "dollar",
    "dollar index": "dollar",
    "dxy": "dollar",
    "fomc": "fed",
    "powell": "fed",
    "central bank": "fed",
    "central banks": "fed",
    "geopolitical risk": "risk",
    "risk sentiment": "risk",
    "risk appetite": "stocks",
    "s&p 500": "stocks",
    "equities": "stocks",
    "technology stocks": "nasdaq",
    "growth equities": "nasdaq",
}

_HIGH_FREQUENCY_NEWS_TERMS: List[str] = [
    "gold",
    "silver",
    "dollar",
    "fed",
    "yields",
    "stocks",
    "nasdaq",
    "vix",
    "oil",
    "risk",
    "rates",
    "inflation",
]

_TICKER_SPARSE_TERMS: Dict[str, List[str]] = {
<<<<<<< Updated upstream
    "GLD": ["gold", "dollar", "fed", "yields"],
    "SLV": ["silver", "gold", "dollar", "fed"],
    "SPY": ["stocks", "fed", "yields", "vix"],
    "QQQ": ["nasdaq", "fed", "yields", "dollar"],
    "^VIX": ["vix", "stocks", "risk", "fed"],
    "DX-Y.NYB": ["dollar", "fed", "yields", "rates"],
}

=======
    "GLD": ["gold", "dollar", "yields"],
    "SLV": ["silver", "gold", "dollar"],
    "SPY": ["fed", "yields", "vix"],
    "QQQ": ["nasdaq", "fed", "dollar"],
    "^VIX": ["vix", "risk", "fed"],
    "DX-Y.NYB": ["dollar", "fed", "yields"],
}

_TICKER_DENSE_TERMS: Dict[str, List[str]] = {
    "GLD": ["gold", "real yields", "dollar", "FOMC", "10-year Treasury yields"],
    "SLV": ["silver", "gold", "real yields", "dollar", "FOMC"],
    "SPY": ["stocks", "fed", "treasury yields", "vix"],
    "QQQ": ["nasdaq", "fed", "treasury yields", "dollar"],
    "^VIX": ["vix", "stocks", "risk", "fed"],
    "DX-Y.NYB": ["dollar", "fed", "treasury yields", "rates"],
}

_NARRATIVE_OPTION_POSTURE_METRICS: Dict[str, List[str]] = {
    "GLD": ["Implied Volatility (IV)", "IV Skew", "Options Liquidity"],
    "SLV": ["Implied Volatility (IV)", "IV Skew", "Options Liquidity"],
}

_CENTRAL_BANK_ENTITIES = {"fed", "fomc", "powell", "central bank", "central banks", "federal reserve"}


def narrative_option_posture_metrics_for_tickers(tickers: List[str] | None) -> List[str]:
    """Return option-posture metrics needed by macro/news narrative reads.

    This is intentionally a narrative contract, not a source-routing rule:
    callers can enrich Silver metrics without converting the whole query into
    an options-family request.
    """

    metrics: List[str] = []
    for ticker in [str(t).upper().strip() for t in (tickers or []) if str(t).strip()]:
        metrics.extend(_NARRATIVE_OPTION_POSTURE_METRICS.get(ticker, []))
    return _dedupe(metrics)

>>>>>>> Stashed changes

def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _chunk_source_type(chunk: Any) -> str:
    raw = _obj_get(chunk, "source_type", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _chunk_metadata(chunk: Any) -> Dict[str, Any]:
    meta = _obj_get(chunk, "metadata", {}) or {}
    return dict(meta) if isinstance(meta, Mapping) else {}


def _chunk_content(chunk: Any) -> str:
    return " ".join(str(_obj_get(chunk, "content", "") or "").split())


def build_news_semantic_profile(
    *,
    tickers: List[str] | None = None,
    canonical_topics: List[str] | None = None,
    expanded_topics: List[str] | None = None,
) -> NewsSemanticProfile:
    """Build deterministic search language for news retrieval."""

    ticker_list = _dedupe([str(t).upper().strip() for t in (tickers or []) if str(t).strip()])
    topics = _dedupe([
        normalize_news_topic(str(topic or ""))
        for topic in (canonical_topics or [])
        if str(topic or "").strip()
    ])

    asset_terms: List[str] = []
    driver_terms: List[str] = []
<<<<<<< Updated upstream
=======
    dense_context_terms: List[str] = []
>>>>>>> Stashed changes
    aliases: List[str] = []
    derived_topics: List[str] = []
    impact_basket: List[str] = []

    for ticker in ticker_list:
        profile = _TICKER_NEWS_LANGUAGE.get(ticker, {})
        asset_terms.extend(profile.get("asset_terms", []))
        driver_terms.extend(profile.get("driver_terms", []))
<<<<<<< Updated upstream
=======
        dense_context_terms.extend(profile.get("dense_context_terms", []))
>>>>>>> Stashed changes
        aliases.extend(profile.get("impacted_asset_aliases", []))
        derived_topics.extend(profile.get("topics", []))

    topics = _dedupe([*topics, *derived_topics])
    expanded = _dedupe([
        normalize_news_topic(str(topic or ""))
        for topic in (expanded_topics or [])
        if str(topic or "").strip()
    ])
    for topic in topics:
        expanded.extend(NEWS_TOPIC_EXPANSIONS.get(topic, [topic]))
        asset_terms.extend(NEWS_TOPIC_KEYWORD_HINTS.get(topic, []))
        impact_basket.extend(TOPIC_IMPACT_BASKETS.get(topic, []) or TOPIC_TO_TICKERS.get(topic, []))

    expanded = _dedupe([normalize_news_topic(topic) for topic in expanded])
    impact_basket = _dedupe([*impact_basket, *ticker_list])
    news_search_terms = _dedupe([*asset_terms, *driver_terms, *topics])

    return NewsSemanticProfile(
        tickers=ticker_list,
        canonical_topics=topics,
        expanded_topics=expanded,
        news_search_terms=news_search_terms,
        news_asset_terms=_dedupe(asset_terms),
        news_driver_terms=_dedupe(driver_terms),
<<<<<<< Updated upstream
=======
        dense_context_terms=_dedupe(dense_context_terms),
>>>>>>> Stashed changes
        impacted_asset_aliases=_dedupe(aliases),
        impact_basket=impact_basket,
    )


def expanded_news_rerank_query(original_query: str, profile: Mapping[str, Any] | NewsSemanticProfile | None) -> str:
    """Return a compact natural-language query for news reranking.

    Kept for backwards compatibility. Sparse retrieval should use
    `sparse_news_keyword_query` instead.
    """

<<<<<<< Updated upstream
    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
=======
    data = _profile_data(profile)
>>>>>>> Stashed changes
    # Ontology identifiers (e.g. "asset_precious_metals_spot", "macro_central_banks")
    # are internal routing keys, not words that appear in news text. Including them
    # in the SPLADE query inflates token count with zero recall benefit — topic-level
    # filtering is already handled by the Qdrant `should=[topic IN [...]]` clause in
    # _build_smart_filter. Only natural-language asset and driver terms are kept.
    terms = _dedupe(
        list(data.get("news_asset_terms") or [])[:6]
        + list(data.get("news_driver_terms") or [])[:6]
    )
    if not terms:
        return " ".join(str(original_query or "").split())
    return " ".join(terms)


<<<<<<< Updated upstream
=======
def dense_news_terms(profile: Mapping[str, Any] | NewsSemanticProfile | None, *, max_terms: int = 5) -> List[str]:
    """Return compact dense-only semantic terms.

    Dense embeddings can use phrase-level concepts, but long keyword bags still
    dilute intent.  Prefer ticker-specific coverage terms; fall back to a tiny
    mix of asset, driver, and dense-context fields.
    """

    data = _profile_data(profile)
    ticker_terms: List[str] = []
    for ticker in data.get("tickers") or []:
        ticker_terms.extend(_TICKER_DENSE_TERMS.get(str(ticker).upper(), []))
    if ticker_terms:
        return _dedupe(ticker_terms)[: max(max_terms, 1)]

    terms = _dedupe(
        list(data.get("news_asset_terms") or [])[:2]
        + list(data.get("news_driver_terms") or [])[:2]
        + list(data.get("dense_context_terms") or [])[:2]
    )
    return terms[: max(max_terms, 1)]


>>>>>>> Stashed changes
def dense_news_semantic_query(
    original_query: str,
    profile: Mapping[str, Any] | NewsSemanticProfile | None,
    *,
    semantic_context: str = "",
) -> str:
    """Dense query can carry semantic context because transformer embeddings handle it well."""

<<<<<<< Updated upstream
    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
    terms = _dedupe(
        list(data.get("news_asset_terms") or [])[:5]
        + list(data.get("news_driver_terms") or [])[:5]
    )
=======
    terms = dense_news_terms(profile, max_terms=5)
>>>>>>> Stashed changes
    base = " ".join(str(semantic_context or original_query or "").split())
    if terms:
        return f"{base} {' '.join(terms)}".strip()
    return base


def sparse_news_keyword_query(
    profile: Mapping[str, Any] | NewsSemanticProfile | None,
    *,
    fallback_query: str = "",
    max_terms: int = 4,
) -> str:
    """Return only high-frequency news vocabulary for SPLADE sparse retrieval.

    This intentionally excludes ontology topic IDs and lower-frequency phrases.
    Topic precision belongs to the Qdrant payload filter; sparse text should be
    short and made of common news words to avoid slow, noisy expansions.
    """

<<<<<<< Updated upstream
    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
=======
    data = _profile_data(profile)
>>>>>>> Stashed changes
    ticker_terms: List[str] = []
    for ticker in data.get("tickers") or []:
        ticker_terms.extend(_TICKER_SPARSE_TERMS.get(str(ticker).upper(), []))
    if ticker_terms:
        return " ".join(_dedupe(ticker_terms)[: max(max_terms, 1)])

    candidates: List[str] = []
    for raw in list(data.get("news_asset_terms") or []) + list(data.get("news_driver_terms") or []):
        term = str(raw or "").strip().lower()
        if not term:
            continue
        normalized = _SPARSE_TERM_NORMALIZATION.get(term, term)
        if normalized in _HIGH_FREQUENCY_NEWS_TERMS:
            candidates.append(normalized)

    ordered = [term for term in _HIGH_FREQUENCY_NEWS_TERMS if term in set(candidates)]
    if ordered:
        return " ".join(ordered[: max(max_terms, 1)])

    # Last resort: strip to a tiny token budget from the existing query. This
    # keeps standalone fallback behaviour alive without reintroducing long
    # narrative phrases into sparse retrieval.
    fallback_terms = [
        token.lower()
        for token in str(fallback_query or "").split()
        if token.isalpha() and len(token) > 2
    ]
    return " ".join(_dedupe(fallback_terms)[: max(max_terms, 1)])


def structured_news_relevance_score(
    payload: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | NewsSemanticProfile | None,
    *,
    rerank_score: float = 0.0,
) -> float:
    """Metadata-aware score for supplemental news ordering; no regex needed."""

<<<<<<< Updated upstream
    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
=======
    data = _profile_data(profile)
>>>>>>> Stashed changes
    payload = dict(payload or {})
    score = float(rerank_score or 0.0)

    if str(payload.get("source_type", "")).lower() == "news":
        score += 0.20

    topic = normalize_news_topic(str(payload.get("topic") or ""))
    if topic and topic in set(data.get("expanded_topics") or data.get("canonical_topics") or []):
        score += 0.35

    payload_assets = [str(item) for item in (payload.get("impacted_assets") or []) if str(item).strip()]
    mapped_tickers = set(tickers_from_impacted_assets(payload_assets))
    basket = {str(t).upper() for t in (data.get("impact_basket") or data.get("tickers") or [])}
    if mapped_tickers and basket and mapped_tickers & basket:
        score += 0.30

    aliases = {str(a).strip().lower() for a in (data.get("impacted_asset_aliases") or []) if str(a).strip()}
    if aliases and any(str(asset).strip().lower() in aliases for asset in payload_assets):
        score += 0.15

    raw_date = str(payload.get("publish_date") or payload.get("record_date") or "").strip()[:10]
    if raw_date:
        try:
            age = max((datetime.now().date() - datetime.fromisoformat(raw_date).date()).days, 0)
            if age <= 7:
                score += 0.10
            elif age <= 30:
                score += 0.05
        except ValueError:
            pass

    return round(score, 6)


def _best_news_line(chunks: List[Any]) -> str:
    for chunk in chunks or []:
        if _chunk_source_type(chunk) != "news":
            continue
        meta = _chunk_metadata(chunk)
        title = str(meta.get("title") or meta.get("original_title") or "").strip()
        source = str(meta.get("source") or "").strip()
        date = str(meta.get("publish_date") or meta.get("record_date") or "").strip()[:10]
        volatility = str(meta.get("volatility_implication") or "").strip()
        impacted = [str(x).strip() for x in (meta.get("impacted_assets") or []) if str(x).strip()]
        bits = []
        if title:
            bits.append(f"'{title}'")
        elif _chunk_content(chunk):
            bits.append(_chunk_content(chunk)[:120].rstrip())
        if source or date:
            bits.append("from " + " / ".join(x for x in (source, date) if x))
        if impacted:
            bits.append("flags " + ", ".join(impacted[:3]))
        if volatility:
            bits.append(f"with {volatility.lower()} volatility implication")
        return " ".join(bits).strip()
    return ""


def _market_value(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if values.get(key) is not None:
            return values.get(key)
    return None


<<<<<<< Updated upstream
=======
def _primary_ticker_bundle(values: Mapping[str, Any], primary: str) -> Dict[str, Any]:
    primary = str(primary or "").upper().strip()
    if not primary:
        return {}
    prefix = f"{primary}_"
    return {
        key[len(prefix):]: value
        for key, value in dict(values or {}).items()
        if str(key).startswith(prefix)
    }


def _has_central_bank_signal(chunks: List[Any]) -> bool:
    for chunk in chunks or []:
        if _chunk_source_type(chunk) != "news":
            continue
        meta = _chunk_metadata(chunk)
        topic = normalize_news_topic(str(meta.get("topic") or ""))
        if topic == "macro_central_banks":
            return True
        entities = {str(item).strip().lower() for item in (meta.get("entities") or []) if str(item).strip()}
        if entities & _CENTRAL_BANK_ENTITIES:
            return True
        title = str(meta.get("title") or meta.get("original_title") or "").strip().lower()
        if any(entity in title for entity in _CENTRAL_BANK_ENTITIES):
            return True
    return False


def _fmt_num(value: Any, digits: int = 2) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


>>>>>>> Stashed changes
def build_narrative_brief(
    *,
    query_family: str,
    original_query: str,
    silver_values: Mapping[str, Any] | None,
    gold_context: List[Any] | None,
    supplemental_news_context: List[Any] | None,
    metadata: Any = None,
    posture_contract: Mapping[str, Any] | None = None,
    retrieval_outcome: Mapping[str, Any] | None = None,
) -> NarrativeBrief:
    """Compose public narrative fields from retrieved evidence."""

    values = dict(silver_values or {})
    posture = dict(posture_contract or {})
    outcome = dict(retrieval_outcome or {})
    family = str(query_family or "").strip().lower()
    topic = " ".join(str(original_query or "the requested market read").split())
    tickers = [str(t).upper() for t in (_obj_get(metadata, "tickers", []) or []) if str(t).strip()]
    primary = tickers[0] if tickers else str(_obj_get(metadata, "primary_ticker", "") or "").upper()
    news_line = _best_news_line(list(supplemental_news_context or []) + list(gold_context or []))

    gld_move = _market_value(values, "GLD_change_pct", "GLD_SPOT_change_pct")
    slv_move = _market_value(values, "SLV_change_pct", "SLV_SPOT_change_pct")
<<<<<<< Updated upstream
    dxy_move = _market_value(values, "DXY_change_pct", "DX_Y_NYB_change_pct")
    vix_value = _market_value(values, "VIX_value")
    gpr_pct = _market_value(values, "gpr_percentile")
=======
    gld_value = _market_value(values, "GLD_value", "GLD_SPOT_value")
    slv_value = _market_value(values, "SLV_value", "SLV_SPOT_value")
    dxy_value = _market_value(values, "DXY_value", "DX_Y_NYB_value")
    dxy_move = _market_value(values, "DXY_change_pct", "DX_Y_NYB_change_pct")
    ten_year_value = _market_value(values, "TNX_value", "TNX_close", "US10Y_value", "US10Y_yield")
    vix_value = _market_value(values, "VIX_value")
    gpr_pct = _market_value(values, "gpr_percentile")
    atm_iv = _market_value(values, "latest_atm_iv")
    iv_rank = _market_value(values, "latest_atm_iv_rank_pct")
    iv_skew = _market_value(values, "latest_iv_skew")
    ticker_bundle = _primary_ticker_bundle(values, primary)
    avg_spread_pct = ticker_bundle.get("avg_spread_pct")
    liquid_contracts = ticker_bundle.get("liquid_contracts")
    market_impact_risk = ticker_bundle.get("market_impact_risk")
>>>>>>> Stashed changes

    asset_label = {
        "GLD": "gold",
        "SLV": "silver",
        "SPY": "equities",
        "QQQ": "growth equities",
        "^VIX": "volatility",
        "DX-Y.NYB": "the dollar",
    }.get(primary, primary or "the asset")

<<<<<<< Updated upstream
    headline = posture.get("posture_takeaway") or f"For {topic}, the cleaner read is a contextual macro/news posture rather than a live options escalation."
=======
    if primary in {"GLD", "SLV"}:
        level_text = ""
        if primary == "GLD" and gld_value is not None:
            level_text = f" with GLD near {_fmt_num(gld_value)}"
        elif primary == "SLV" and slv_value is not None:
            level_text = f" with SLV near {_fmt_num(slv_value)}"
        headline = (
            f"For {topic}, the core read is a precious-metals macro narrative{level_text}: "
            "dollar/yield pressure and central-bank path uncertainty decide whether the bid becomes trend or fades at resistance."
        )
    else:
        headline = posture.get("posture_takeaway") or (
            f"For {topic}, the cleaner read is a macro/news transmission narrative anchored to the retrieved cross-asset evidence."
        )
>>>>>>> Stashed changes
    if news_line:
        news_driver = f"The news driver to weigh is {news_line}."
    elif outcome.get("background_only_read") and outcome.get("news_coverage_status") == "no_fresh_news_retrieved":
        news_driver = "No fresh in-window news headline was retrieved, so the read should lean on structured macro and cross-asset evidence."
    else:
        news_driver = "News coverage is supplemental here; the read should not be forced from a generic narrative headline."

    transmission_bits: List[str] = []
    if gpr_pct is not None and family.startswith("geopolitical"):
        transmission_bits.append(f"geopolitical risk is elevated by percentile context ({gpr_pct})")
    if dxy_move is not None:
        transmission_bits.append(f"dollar direction is part of the transmission channel ({dxy_move}% move)")
    if vix_value is not None:
        transmission_bits.append(f"volatility remains a market-risk gauge (VIX {vix_value})")
<<<<<<< Updated upstream
=======
    if primary in {"GLD", "SLV"}:
        transmission_bits.append("yield direction is explicitly part of the metals news channel")
>>>>>>> Stashed changes
    if not transmission_bits:
        transmission_bits.append("the macro channel runs through rates, dollar direction, risk appetite, and safe-haven demand")
    macro_transmission = "Macro transmission: " + "; ".join(transmission_bits[:3]) + "."

    reaction_bits: List[str] = []
    if primary == "GLD" and gld_move is not None:
        reaction_bits.append(f"gold/GLD is the primary asset read, with GLD move at {gld_move}%")
    elif primary == "SLV" and slv_move is not None:
        reaction_bits.append(f"silver/SLV is the primary asset read, with SLV move at {slv_move}%")
    elif gld_move is not None:
        reaction_bits.append(f"gold/GLD gives the precious-metals anchor ({gld_move}% move)")
    if slv_move is not None and primary != "SLV":
        reaction_bits.append(f"SLV adds the silver confirmation leg ({slv_move}% move)")
<<<<<<< Updated upstream
=======
    if iv_rank is not None:
        reaction_bits.append(f"IV rank is {_fmt_num(iv_rank)}%, so optionality is part of the setup rather than a footnote")
    if iv_skew is not None:
        reaction_bits.append(f"IV skew is {_fmt_num(iv_skew, 4)}, framing put-versus-call premium asymmetry")
>>>>>>> Stashed changes
    if not reaction_bits:
        reaction_bits.append(f"{asset_label} should be framed through the retrieved macro/news channel, not through ticker text alone")
    asset_reaction = "Asset reaction: " + "; ".join(reaction_bits[:3]) + "."

<<<<<<< Updated upstream
    risk_read = posture.get("escalation_risk_read") or "Main risk: the setup weakens if fresh headlines, dollar/yield direction, or volatility confirmation move against the narrative."
    what_would_change = "What would change the view: fresher asset-relevant news, a clear reversal in dollar/yield pressure, or options evidence strong enough to justify moving beyond an informational read."
=======
    news_chunks = list(supplemental_news_context or []) + list(gold_context or [])
    if _has_central_bank_signal(news_chunks):
        game_theory_read = (
            "Game-theory read: Fed-path disagreement is the volatility source; every inflation, jobs, or policy headline can reprice "
            "both the dollar/yield leg and the safe-haven leg."
        )
    else:
        game_theory_read = (
            "Game-theory read: the trade is a two-player tug-of-war between safe-haven demand and dollar/yield resistance, "
            "so confirmation should come from both news tone and cross-asset reaction."
        )

    volatility_setup = ""
    if primary in {"GLD", "SLV"} and isinstance(iv_rank, (int, float)):
        liquidity_clause = ""
        if avg_spread_pct is not None:
            liquidity_clause = f" Execution quality matters because the executable spread is {_fmt_num(avg_spread_pct, 3)}%."
        elif liquid_contracts is not None:
            liquidity_clause = f" Liquidity has {int(liquid_contracts)} executable contracts."
        elif market_impact_risk:
            liquidity_clause = f" Liquidity risk reads {market_impact_risk}."
        if iv_rank <= 30:
            volatility_setup = (
                f"Volatility setup: IV rank is low at {_fmt_num(iv_rank)}%; if {asset_label} is basing rather than breaking down, "
                f"the asymmetric expression is long volatility plus long-{asset_label} convexity, such as a straddle/strangle watch."
                f"{liquidity_clause}"
            )
        else:
            volatility_setup = (
                f"Volatility setup: IV rank at {_fmt_num(iv_rank)}% is not a clean cheap-vol trigger, so the options read should wait "
                f"for either lower premium or a stronger breakout/breakdown confirmation.{liquidity_clause}"
            )

    if primary in {"GLD", "SLV"}:
        yield_clause = (
            f" and 10-year yields keep pushing higher from {_fmt_num(ten_year_value)}"
            if ten_year_value is not None
            else " and 10-year yields keep pushing higher"
        )
        risk_trigger = (
            "Risk Trigger: if USD DXY reclaims 100"
            f"{yield_clause}, tighter financial conditions would pressure gold's non-yielding carry, "
            "making the $4,800 gold resistance area a local-top risk."
        )
    elif dxy_value is not None:
        risk_trigger = f"Risk Trigger: if DXY reverses materially from {_fmt_num(dxy_value)}, reassess the macro transmission channel."
    else:
        risk_trigger = "Risk Trigger: a clean reversal in dollar/yield pressure would weaken the current macro transmission channel."

    risk_read = risk_trigger if primary in {"GLD", "SLV"} else (posture.get("escalation_risk_read") or risk_trigger)
    what_would_change = (
        "What would change the view: fresher asset-relevant news, a clear reversal in dollar/yield pressure, "
        "or options evidence that changes the long-vol versus wait-for-confirmation balance."
    )
>>>>>>> Stashed changes

    return NarrativeBrief(
        headline_read=str(headline).strip(),
        news_driver=news_driver.strip(),
        macro_transmission=macro_transmission.strip(),
        asset_reaction=asset_reaction.strip(),
<<<<<<< Updated upstream
        risk_read=str(risk_read).strip(),
=======
        game_theory_read=game_theory_read.strip(),
        volatility_setup=volatility_setup.strip(),
        risk_read=str(risk_read).strip(),
        risk_trigger=risk_trigger.strip(),
>>>>>>> Stashed changes
        what_would_change=what_would_change,
    )


<<<<<<< Updated upstream
=======
def news_contract_from_chunks(
    chunks: List[Any],
    tickers: List[str],
    original_query: str = "",
) -> NarrativeBrief:
    """Return a deterministic NarrativeBrief from gold news evidence chunks.

    Caller owns LLM elaboration; this contract owns factual field values.
    No regex, no LLM call — pure field extraction and ontology look-up.

    Selection logic
    ---------------
    * ``news_driver``: title/content from the highest-scored news chunk
      (``score`` attribute or dict key; falls back to first news chunk).
    * ``macro_transmission``: joined ``dense_context_terms`` from the first
      recognised ticker's entry in ``_TICKER_NEWS_LANGUAGE``; falls back to
      a generic precious-metals / cross-asset phrase.
    * All other fields: empty strings (caller may enrich via ``build_narrative_brief``).
    """
    ticker_list = [str(t).upper().strip() for t in (tickers or []) if str(t).strip()]
    primary = ticker_list[0] if ticker_list else ""

    news_chunks = [c for c in (chunks or []) if _chunk_source_type(c) == "news"]

    best_chunk: Any = None
    if news_chunks:
        def _score(c: Any) -> float:
            raw = _obj_get(c, "score", None)
            return float(raw) if isinstance(raw, (int, float)) else 0.0

        best_chunk = max(news_chunks, key=_score)

    news_driver = ""
    if best_chunk is not None:
        meta = _chunk_metadata(best_chunk)
        title = str(meta.get("title") or meta.get("original_title") or "").strip()
        source = str(meta.get("source") or "").strip()
        date_str = str(meta.get("publish_date") or meta.get("record_date") or "").strip()[:10]
        content_snip = _chunk_content(best_chunk)[:160].rstrip() if not title else ""
        headline_text = title or content_snip
        bits = []
        if headline_text:
            bits.append(f"'{headline_text}'")
        if source or date_str:
            bits.append("from " + " / ".join(x for x in (source, date_str) if x))
        news_driver = ("The news driver to weigh is " + " ".join(bits).strip() + ".") if bits else ""

    ticker_lang = _TICKER_NEWS_LANGUAGE.get(primary, {})
    dense_terms = ticker_lang.get("dense_context_terms") or []
    if dense_terms:
        macro_transmission = (
            "Macro transmission: the key macro channels for "
            f"{primary or 'this asset'} include "
            + ", ".join(str(t) for t in dense_terms[:4])
            + "."
        )
    else:
        macro_transmission = (
            "Macro transmission: the macro channel runs through rates, "
            "dollar direction, risk appetite, and safe-haven demand."
        )

    return NarrativeBrief(
        news_driver=news_driver,
        macro_transmission=macro_transmission,
    )


>>>>>>> Stashed changes
def render_narrative_brief_block(brief: Mapping[str, Any] | NarrativeBrief | None) -> str:
    data = brief.model_dump() if isinstance(brief, NarrativeBrief) else dict(brief or {})
    if not data:
        return "(narrative brief unavailable)"
    labels = (
        ("headline_read", "Headline read"),
        ("news_driver", "News driver"),
        ("macro_transmission", "Macro transmission"),
        ("asset_reaction", "Asset reaction"),
<<<<<<< Updated upstream
        ("risk_read", "Risk read"),
=======
        ("game_theory_read", "Game-theory read"),
        ("volatility_setup", "Volatility setup"),
        ("risk_read", "Risk read"),
        ("risk_trigger", "Risk trigger"),
>>>>>>> Stashed changes
        ("what_would_change", "What would change"),
    )
    return "\n".join(f"{label}: {str(data.get(key) or '').strip()}" for key, label in labels if str(data.get(key) or "").strip())
