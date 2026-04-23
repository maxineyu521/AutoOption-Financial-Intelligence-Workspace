"""
Scripts/core/financial_ontology.py
Financial Ontology Mapping Table.

Single source of truth that bridges LLM business intents and the physical
data architecture. Every mapping below has been **audited against the actual
parquet schemas on disk** (verified 2026-04-22 via DuckDB DESCRIBE):

  - Options  : Data/2_Silver_Processed/Options_Market_Data/*/*.parquet
               (18 columns, see DATASET_PHYSICAL_SCHEMA['options'])
  - Macro    : Data/2_Silver_Processed/Macro_History/*/*.parquet
               (11 columns, see DATASET_PHYSICAL_SCHEMA['macro'])
  - GPR      : Data/2_Silver_Processed/GPR_index/*.parquet
               (120 columns — raw Caldara–Iacoviello release plus enrichments)
  - SEC      : Qdrant payload (no Silver parquet); key fields:
               ticker, form_type, action_direction, tone_score, transaction_date

Rules of engagement
-------------------
1. METRIC_TO_COLUMN_MAPPING MUST only reference columns that actually exist
   in one of the physical datasets listed in DATASET_PHYSICAL_SCHEMA. This
   file is the contract; drift is a bug.
2. DERIVED metrics (Put/Call Ratio, IV Skew) map to the *source* columns
   needed to compute them — never to a pre-materialised column that does
   not exist on disk.
3. UNAVAILABLE metrics (no physical columns yet ingested, e.g. Greeks,
   13F institutional flows, yield spreads) are kept in ALLOWED_METRICS so
   the LLM can still surface user intent, but map to [] so downstream SQL
   dispatchers fail LOUD instead of hallucinating.
4. Keys in METRIC_TO_COLUMN_MAPPING MUST stay in sync with the dispatcher
   in Scripts/retrieval/sql_tools.py. When you add a new metric here,
   register a handler there.
"""

from typing import List, Dict, Set

# ---------------------------------------------------------------------------
# 0. PHYSICAL SCHEMA CONTRACT (authoritative, auto-verifiable)
# ---------------------------------------------------------------------------
# This dict is what Scripts/retrieval/sql_tools.py::_validate_parquet_contract
# should consume. Every column in METRIC_TO_COLUMN_MAPPING below must appear
# in at least one dataset here.
DATASET_PHYSICAL_SCHEMA: Dict[str, Set[str]] = {
    "options": {
        "snapshot_date", "symbol", "underlying_price", "contract_symbol",
        "option_type", "strike", "expiration", "dte", "moneyness_pct",
        "last_price", "bid", "ask", "spread_pct",
        "volume", "open_interest", "implied_volatility",
        "in_the_money", "is_liquid",
    },
    "macro": {
        "retrieval_date", "observation_date", "symbol", "name",
        "asset_class", "value", "unit", "frequency",
        "daily_change_pct", "mom_change_pct", "yoy_change_pct",
    },
    "gpr": {
        # Enriched fields written by our GPR pipeline.
        "month", "date", "gpr_percentile", "gpr_mom_pct", "gpr_yoy_pct", "gpr_3m_ma",
        # Headline + family aggregates from the Caldara–Iacoviello release.
        "gpr", "gprt", "gpra", "gprh", "gprht", "gprha",
        "share_gpr", "share_gprh", "n10", "n3h",
        # Variant constructions retained for robustness checks.
        "gpr_noew", "gpr_and", "gpr_basic",
        "gprh_noew", "gprh_and", "gprh_basic",
        # Country-level headline & historical indexes (subset — full list
        # via `pragma table_info` on the parquet; we whitelist the majors).
        "gprc_usa", "gprc_chn", "gprc_rus", "gprc_ukr", "gprc_isr",
        "gprc_sau", "gprc_ira", "gprc_kor", "gprc_jpn", "gprc_deu",
        "gprc_gbr", "gprc_fra", "gprc_ind",
        "gprhc_usa", "gprhc_chn", "gprhc_rus", "gprhc_ukr", "gprhc_isr",
        # Topic-share distribution.
        "shareh_cat_1", "shareh_cat_2", "shareh_cat_3", "shareh_cat_4",
        "shareh_cat_5", "shareh_cat_6", "shareh_cat_7", "shareh_cat_8",
        "var_name", "var_label",
    },
    # SEC insider activity lives exclusively in Qdrant (no Silver parquet).
    # Exposed here so the LLM can still emit SEC-oriented metrics and the
    # Gold retriever can match against the real payload fields.
    "sec_qdrant_payload": {
        "ticker", "form_type", "filed_at", "accession_no",
        "transaction_date", "action_direction", "tone_score", "url",
    },
    # News docs in Qdrant do NOT carry a ticker field today (see Ingestion
    # backlog); they carry topic/entities/impacted_assets. Listed for
    # completeness and to drive future ingestion enrichment.
    "news_qdrant_payload": {
        "topic", "title", "publish_date", "publish_timestamp", "source",
        "url", "entities", "impacted_assets", "volatility_implication",
        "llm_tone_score",
    },
}

# ---------------------------------------------------------------------------
# 1. Gold-layer source-type whitelist (Qdrant payload `source_type`)
# ---------------------------------------------------------------------------
ALLOWED_SOURCES: List[str] = ["sec", "news", "gpr"]

SOURCE_ALIAS_TO_ALLOWED_SOURCE: Dict[str, str] = {
    "sec_insider_trades": "sec",
    "sec_parsed_json":    "sec",
    "news_qdrant":        "news",
    "news_scrapes":       "news",
    "macro_narratives":   "news",
    "macro_history":      "news",
    "gpr_index":          "gpr",
}

# ---------------------------------------------------------------------------
# 2. Business-intent categories surfaced to the LLM
# ---------------------------------------------------------------------------
ALLOWED_CATEGORIES: List[str] = [
    "precious_metals_spot", "macro_inflation_employment",
    "macro_central_banks", "macro_yields_dollar", "metals_derivatives",
    "equities_spot", "equities_derivatives", "corporate_actions",
    "macro_geopolitics_risk", "macro_growth_activity", "macro_liquidity_credit",
    "commodities_energy", "commodities_agriculture",
    "fx_dollar_rates", "rates_curve_real_yields",
    "risk_sentiment_volatility", "earnings_guidance",
]

# ---------------------------------------------------------------------------
# 3. Metric whitelist exposed to the Extractor LLM
# ---------------------------------------------------------------------------
# These are the *only* labels the LLM may emit under `metrics`. New metrics
# added here MUST also appear in METRIC_TO_COLUMN_MAPPING below AND in the
# dispatcher in sql_tools.py.
ALLOWED_METRICS: List[str] = [
    # ── Options-oriented ──────────────────────────────────────────
    "Implied Volatility (IV)",
    "IV Skew",
    "Put/Call Ratio",
    "Open Interest",
    "Options Volume",
    "Options Liquidity",
    "Moneyness / OTM",
    "Time Decay / DTE",
    "Options Pricing / Spread",
    "Underlying Price",
    "Greeks (Delta/Gamma)",       # Reserved — not yet in Silver schema.

    # ── Macro / price action ──────────────────────────────────────
    "Price",
    "Price Change (%)",
    "Daily Change (%)",
    "Monthly Change (%)",
    "Yearly Change (%)",
    "Macro Trend",
    "Yield Spread",               # Reserved — requires additional ingestion.

    # ── Geopolitics (GPR) ─────────────────────────────────────────
    "GPR Index",
    "GPR Threats",
    "GPR Acts",
    "GPR Components",
    "GPR Country-level",

    # ── SEC / Insider (Qdrant-only, no Silver parquet) ────────────
    "Insider Trading",
    "Institutional Flows",        # Reserved — 13F / block-trade data not yet ingested.
]

# ---------------------------------------------------------------------------
# 4. Metric → physical column mapping (single source of truth)
# ---------------------------------------------------------------------------
# Conventions:
#   - Empty list `[]`  => metric is allowed but has NO physical columns yet;
#                         downstream dispatcher MUST surface this as
#                         UNAUTHORIZED / UNAVAILABLE rather than silently drop.
#   - Composite lists => the metric is DERIVED; SQL handler must aggregate
#                         these source columns (e.g. PCR from option_type+volume).
#   - Every non-empty list is audited against DATASET_PHYSICAL_SCHEMA above.
METRIC_TO_COLUMN_MAPPING: Dict[str, List[str]] = {
    # ── OPTIONS (dataset=options) ─────────────────────────────────
    "Implied Volatility (IV)": ["implied_volatility"],

    # DERIVED: IV Skew = f(IV across strikes grouped by moneyness & option_type).
    # No pre-materialised iv_skew column — handler must compute call-25Δ minus
    # put-25Δ (or the chosen convention) from these source fields.
    "IV Skew": ["implied_volatility", "moneyness_pct", "option_type", "strike"],

    # DERIVED: PCR = SUM(put.volume) / SUM(call.volume), same for open_interest.
    # Handler: Scripts/retrieval/sql_tools.py::_handle_put_call_ratio.
    "Put/Call Ratio": ["option_type", "volume", "open_interest"],

    "Open Interest":     ["open_interest"],
    "Options Volume":    ["volume"],
    "Options Liquidity": ["is_liquid", "volume", "open_interest", "spread_pct"],
    "Moneyness / OTM":   ["moneyness_pct", "in_the_money", "strike", "underlying_price"],
    "Time Decay / DTE":  ["dte", "expiration"],
    "Options Pricing / Spread": ["last_price", "bid", "ask", "spread_pct"],
    "Underlying Price":  ["underlying_price"],

    # Reserved: Greeks are NOT ingested into Silver. Listed as empty so the
    # ontology is honest and the SQL dispatcher will flag UNAUTHORIZED instead
    # of pointing at a phantom "delta_gamma_exposure" column.
    "Greeks (Delta/Gamma)": [],

    # ── MACRO (dataset=macro) ─────────────────────────────────────
    # Note: macro.value and options.underlying_price are both valid "price"
    # sources depending on which dataset the symbol belongs to. The handler
    # dispatches by symbol class (macro index vs. options underlying).
    "Price":              ["value", "underlying_price"],
    "Price Change (%)":   ["daily_change_pct", "mom_change_pct", "yoy_change_pct"],
    "Daily Change (%)":   ["daily_change_pct"],
    "Monthly Change (%)": ["mom_change_pct"],
    "Yearly Change (%)":  ["yoy_change_pct"],
    "Macro Trend":        ["value", "daily_change_pct", "mom_change_pct", "yoy_change_pct"],

    # Reserved: no yield-spread column in current macro parquet; would need
    # paired rate series (e.g. DGS10 vs DGS2) to compute at query time.
    "Yield Spread": [],

    # ── GPR (dataset=gpr) ─────────────────────────────────────────
    "GPR Index":          ["gpr", "gpr_percentile", "gpr_mom_pct", "gpr_yoy_pct", "gpr_3m_ma"],
    "GPR Threats":        ["gprt", "gprht"],
    "GPR Acts":           ["gpra", "gprha"],
    "GPR Components":     ["gpr_basic", "gpr_and", "gpr_noew",
                           "gprh_basic", "gprh_and", "gprh_noew"],
    "GPR Country-level":  ["gprc_usa", "gprc_chn", "gprc_rus", "gprc_ukr",
                           "gprc_isr", "gprc_sau", "gprc_ira", "gprc_kor",
                           "gprc_jpn", "gprc_deu", "gprc_gbr", "gprc_fra",
                           "gprc_ind"],

    # ── SEC (Qdrant payload fields, no Silver parquet) ────────────
    # Insider Trading maps to Qdrant payload filters, NOT to physical parquet
    # columns. The Gold retriever uses these as FieldCondition keys (see
    # Scripts/retrieval/qdrant_retriever.py::_build_smart_filter).
    "Insider Trading": ["action_direction", "tone_score", "form_type"],

    # Reserved: 13F / block-trade data is NOT yet in the lake.
    "Institutional Flows": [],
}

# ---------------------------------------------------------------------------
# 4b. ETF → underlying-index alias (Silver-Layer join aid)
# ---------------------------------------------------------------------------
# RATIONALE (read this before touching): our Macro_History parquet stores
# canonical *indices* (^GSPC, ^IXIC, ^RUT, ^VIX) because that is what FRED /
# academic macro pipelines expose. Users and the LLM, however, speak in ETFs
# (SPY, QQQ, IWM) because those are the tradable instruments. Without a
# translation layer, a query "SPY macro trend" would WHERE-filter the macro
# table for symbol='SPY' — which does not exist — and silently return 0 rows.
# This map is consumed by Scripts/retrieval/sql_tools.py::_handle_macro_analysis
# only. Options/SEC handlers keep the ETF symbol (their tables store ETFs).
ETF_TO_MACRO_ALIAS: Dict[str, str] = {
    "SPY":  "^GSPC",
    "IVV":  "^GSPC",
    "VOO":  "^GSPC",
    "QQQ":  "^IXIC",
    "IWM":  "^RUT",     # Present in FRED; NOT yet in our Silver macro table.
    "VIXY": "^VIX",
    # GLD / SLV are deliberately NOT aliased: our Macro_History keeps both
    # the ETF and the spot price under the ETF ticker itself (see the
    # `asset_class='Commodity ETF'` rows from macro_data_pipeline.py). Aliasing
    # them would route the query AWAY from the row that actually exists.
}

# ---------------------------------------------------------------------------
# 4c. News-layer topic taxonomy & enrichment maps
# ---------------------------------------------------------------------------
# The news scraper (Scripts/data_collection/scrapers/news_scraper.py) tags
# every article with one of the keys below. The Gold retriever uses them as
# a Qdrant `topic` FieldCondition so semantic search can be grounded in the
# correct macro/asset silo even when the user never names a ticker.
NEWS_TOPICS: List[str] = [
    "macro_central_banks",
    "macro_inflation_employment",
    "macro_yields_dollar",
    "macro_geopolitics_risk",
    "asset_precious_metals_spot",
    "asset_metals_derivatives",
]

# Category ↔ topic alignment. ALLOWED_CATEGORIES (used by the Extractor LLM)
# drops the "asset_" prefix for brevity, whereas news_scraper writes it in
# full. This dict is the canonical two-way bridge; keep both directions in
# sync when a new topic is added.
NEWS_TOPIC_ALIAS: Dict[str, str] = {
    "precious_metals_spot":     "asset_precious_metals_spot",
    "metals_derivatives":       "asset_metals_derivatives",
    "asset_precious_metals_spot": "asset_precious_metals_spot",
    "asset_metals_derivatives": "asset_metals_derivatives",
    "macro_central_banks":      "macro_central_banks",
    "macro_inflation_employment": "macro_inflation_employment",
    "macro_yields_dollar":      "macro_yields_dollar",
    "macro_geopolitics_risk":   "macro_geopolitics_risk",
}

# Given a news `topic`, which tickers does an article of this topic typically
# influence? Used at ingestion time to back-fill the `ticker` payload on news
# docs so Gold ticker filters stop yielding 0 hits. Ticker universe limited to
# what ACTUALLY exists in our Silver parquets — do not add aspirational symbols.
TOPIC_TO_TICKERS: Dict[str, List[str]] = {
    "macro_central_banks":        ["FEDFUNDS", "DX-Y.NYB", "^VIX", "^GSPC", "^IXIC"],
    "macro_inflation_employment": ["CPIAUCSL", "UNRATE", "^GSPC", "^VIX"],
    "macro_yields_dollar":        ["DX-Y.NYB", "^VIX"],
    "macro_geopolitics_risk":     ["^VIX", "GLD", "^GSPC"],
    "asset_precious_metals_spot": ["GLD", "SLV"],
    "asset_metals_derivatives":   ["GLD", "SLV"],
}

# The LLM in news_scraper emits free-text asset-class labels in
# `impacted_assets` (e.g. "Gold", "USD", "Equities"). This dict normalises
# them to the same ticker universe as above. Matching is case-insensitive
# and substring-aware at ingestion time (see Scripts/Qdrant_Ingestion.py).
IMPACTED_ASSET_TO_TICKERS: Dict[str, List[str]] = {
    "gold":        ["GLD"],
    "silver":      ["SLV"],
    "precious metals": ["GLD", "SLV"],
    "usd":         ["DX-Y.NYB"],
    "us dollar":   ["DX-Y.NYB"],
    "dollar":      ["DX-Y.NYB"],
    "dxy":         ["DX-Y.NYB"],
    "equities":    ["^GSPC", "SPY"],
    "stocks":      ["^GSPC", "SPY"],
    "s&p":         ["^GSPC", "SPY"],
    "s&p 500":     ["^GSPC", "SPY"],
    "nasdaq":      ["^IXIC", "QQQ"],
    "tech":        ["^IXIC", "QQQ"],
    "vix":         ["^VIX"],
    "volatility":  ["^VIX"],
    # Explicitly unmapped (no Silver coverage yet): oil, crude, treasuries,
    # bonds, real estate, crypto. Do NOT guess — return [] so downstream
    # knows the asset class is out-of-scope.
}


def normalize_news_topic(raw: str) -> str:
    """Return the canonical news-scraper topic string for a given LLM label.
    Unknown inputs pass through unchanged so callers can decide to drop or
    warn; never raises, never silently remaps to a default (which would
    mask ontology drift).
    """
    if not raw:
        return ""
    return NEWS_TOPIC_ALIAS.get(raw.strip().lower(), raw.strip().lower())


def tickers_from_impacted_assets(labels: List[str]) -> List[str]:
    """Map news_scraper's free-text `impacted_assets` list to concrete
    tickers present in our Silver parquets. Case-insensitive, substring
    tolerant, de-duplicated. Returns [] when no label is recognised.
    """
    out: List[str] = []
    for label in (labels or []):
        k = (label or "").strip().lower()
        if not k:
            continue
        if k in IMPACTED_ASSET_TO_TICKERS:
            out.extend(IMPACTED_ASSET_TO_TICKERS[k])
            continue
        # Substring fallback: "US Dollar Index" -> matches "us dollar".
        for needle, tickers in IMPACTED_ASSET_TO_TICKERS.items():
            if needle in k:
                out.extend(tickers)
                break
    # Preserve insertion order while deduplicating.
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# ---------------------------------------------------------------------------
# 5. Event-keyword → category mapping (LLM narrative hinting)
# ---------------------------------------------------------------------------
EVENT_KEYWORDS_MAPPING: Dict[str, str] = {
    "earnings_beat":       "earnings_guidance",
    "earnings_miss":       "earnings_guidance",
    "guidance_raise":      "earnings_guidance",
    "guidance_cut":        "earnings_guidance",

    "insider_buying":      "corporate_actions",
    "insider_selling":     "corporate_actions",
    "share_buyback":       "corporate_actions",
    "dividend_hike":       "corporate_actions",
    "dividend_cut":        "corporate_actions",
    "merger_acquisition":  "corporate_actions",

    "rate_hike":           "macro_central_banks",
    "rate_cut":            "macro_central_banks",
    "fomc_minutes":        "macro_central_banks",
    "powell_speech":       "macro_central_banks",
    "qt_tightening":       "macro_liquidity_credit",
    "liquidity_injection": "macro_liquidity_credit",

    "ppi_release":         "macro_inflation_employment",
    "nfp_release":         "macro_inflation_employment",
    "jobless_claims":      "macro_inflation_employment",
    "wage_growth":         "macro_inflation_employment",

    "gdp_release":         "macro_growth_activity",
    "pmi_release":         "macro_growth_activity",
    "retail_sales":        "macro_growth_activity",

    "dxy_breakout":        "fx_dollar_rates",
    "usd_strength":        "fx_dollar_rates",
    "usd_weakness":        "fx_dollar_rates",
    "yield_curve_steepen": "rates_curve_real_yields",
    "yield_curve_invert":  "rates_curve_real_yields",
    "real_yield_rise":     "rates_curve_real_yields",

    "gold_spike":          "precious_metals_spot",
    "silver_rally":        "precious_metals_spot",
    "metals_term_structure": "metals_derivatives",
    "oil_supply_shock":    "commodities_energy",
    "crop_supply_shock":   "commodities_agriculture",

    "vol_spike":           "risk_sentiment_volatility",
    "risk_off":            "risk_sentiment_volatility",
    "risk_on":             "risk_sentiment_volatility",

    # Liquidity-centric triggers (matches new "Options Liquidity" metric).
    "options_liquidity":   "equities_derivatives",
    "liquidity_dry_up":    "risk_sentiment_volatility",
}


# ---------------------------------------------------------------------------
# 6. Self-test guard (runs on import in dev only — cheap, no IO)
# ---------------------------------------------------------------------------
def _assert_mapping_integrity() -> None:
    """Fail-loud assertion that every mapped column exists in at least one
    physical dataset. Catches copy-paste regressions the moment they land.
    Runs on import when PYTHON_ENV == 'dev' so prod boot is unaffected.
    """
    all_physical: Set[str] = set()
    for cols in DATASET_PHYSICAL_SCHEMA.values():
        all_physical |= cols

    broken: Dict[str, List[str]] = {}
    for metric, cols in METRIC_TO_COLUMN_MAPPING.items():
        missing = [c for c in cols if c not in all_physical]
        if missing:
            broken[metric] = missing
    if broken:
        raise AssertionError(
            "financial_ontology: METRIC_TO_COLUMN_MAPPING references columns "
            f"not present in any DATASET_PHYSICAL_SCHEMA entry: {broken}"
        )


# Opt-in to avoid surprising imports in prod. Flip on in CI / dev shells.
import os as _os
if _os.getenv("PYTHON_ENV", "").lower() == "dev":
    _assert_mapping_integrity()
