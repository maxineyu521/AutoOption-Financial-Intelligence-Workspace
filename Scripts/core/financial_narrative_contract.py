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
    risk_read: str = ""
    what_would_change: str = ""

    def model_dump(self) -> Dict[str, str]:
        return asdict(self)


_TICKER_NEWS_LANGUAGE: Dict[str, Dict[str, List[str]]] = {
    "GLD": {
        "asset_terms": ["gold", "bullion", "precious metals", "safe haven"],
        "driver_terms": ["gold futures", "real yields", "treasury yields", "dollar", "dxy", "fed"],
        "impacted_asset_aliases": ["Gold", "Precious Metals", "USD"],
        "topics": ["asset_precious_metals_spot", "asset_metals_derivatives", "macro_yields_dollar", "macro_central_banks"],
    },
    "SLV": {
        "asset_terms": ["silver", "precious metals", "gold silver ratio", "industrial demand"],
        "driver_terms": ["silver futures", "real yields", "dollar", "dxy", "fed", "comex"],
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
    "GLD": ["gold", "dollar", "fed", "yields"],
    "SLV": ["silver", "gold", "dollar", "fed"],
    "SPY": ["stocks", "fed", "yields", "vix"],
    "QQQ": ["nasdaq", "fed", "yields", "dollar"],
    "^VIX": ["vix", "stocks", "risk", "fed"],
    "DX-Y.NYB": ["dollar", "fed", "yields", "rates"],
}


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
    aliases: List[str] = []
    derived_topics: List[str] = []
    impact_basket: List[str] = []

    for ticker in ticker_list:
        profile = _TICKER_NEWS_LANGUAGE.get(ticker, {})
        asset_terms.extend(profile.get("asset_terms", []))
        driver_terms.extend(profile.get("driver_terms", []))
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
        impacted_asset_aliases=_dedupe(aliases),
        impact_basket=impact_basket,
    )


def expanded_news_rerank_query(original_query: str, profile: Mapping[str, Any] | NewsSemanticProfile | None) -> str:
    """Return a compact natural-language query for news reranking.

    Kept for backwards compatibility. Sparse retrieval should use
    `sparse_news_keyword_query` instead.
    """

    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
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


def dense_news_semantic_query(
    original_query: str,
    profile: Mapping[str, Any] | NewsSemanticProfile | None,
    *,
    semantic_context: str = "",
) -> str:
    """Dense query can carry semantic context because transformer embeddings handle it well."""

    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
    terms = _dedupe(
        list(data.get("news_asset_terms") or [])[:5]
        + list(data.get("news_driver_terms") or [])[:5]
    )
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

    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
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

    data = profile.model_dump() if isinstance(profile, NewsSemanticProfile) else dict(profile or {})
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
    dxy_move = _market_value(values, "DXY_change_pct", "DX_Y_NYB_change_pct")
    vix_value = _market_value(values, "VIX_value")
    gpr_pct = _market_value(values, "gpr_percentile")

    asset_label = {
        "GLD": "gold",
        "SLV": "silver",
        "SPY": "equities",
        "QQQ": "growth equities",
        "^VIX": "volatility",
        "DX-Y.NYB": "the dollar",
    }.get(primary, primary or "the asset")

    headline = posture.get("posture_takeaway") or f"For {topic}, the cleaner read is a contextual macro/news posture rather than a live options escalation."
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
    if not reaction_bits:
        reaction_bits.append(f"{asset_label} should be framed through the retrieved macro/news channel, not through ticker text alone")
    asset_reaction = "Asset reaction: " + "; ".join(reaction_bits[:3]) + "."

    risk_read = posture.get("escalation_risk_read") or "Main risk: the setup weakens if fresh headlines, dollar/yield direction, or volatility confirmation move against the narrative."
    what_would_change = "What would change the view: fresher asset-relevant news, a clear reversal in dollar/yield pressure, or options evidence strong enough to justify moving beyond an informational read."

    return NarrativeBrief(
        headline_read=str(headline).strip(),
        news_driver=news_driver.strip(),
        macro_transmission=macro_transmission.strip(),
        asset_reaction=asset_reaction.strip(),
        risk_read=str(risk_read).strip(),
        what_would_change=what_would_change,
    )


def render_narrative_brief_block(brief: Mapping[str, Any] | NarrativeBrief | None) -> str:
    data = brief.model_dump() if isinstance(brief, NarrativeBrief) else dict(brief or {})
    if not data:
        return "(narrative brief unavailable)"
    labels = (
        ("headline_read", "Headline read"),
        ("news_driver", "News driver"),
        ("macro_transmission", "Macro transmission"),
        ("asset_reaction", "Asset reaction"),
        ("risk_read", "Risk read"),
        ("what_would_change", "What would change"),
    )
    return "\n".join(f"{label}: {str(data.get(key) or '').strip()}" for key, label in labels if str(data.get(key) or "").strip())
