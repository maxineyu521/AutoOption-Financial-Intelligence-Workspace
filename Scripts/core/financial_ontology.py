"""
config/financial_ontology.py
Financial Ontology Mapping Table.
Acts as the bridge between LLM business intents and physical database architectures.
"""

from typing import List, Dict

# Defines the valid sources for the 'source_types' field in Qdrant
ALLOWED_SOURCES: List[str] = ["sec", "news", "gpr"]

SOURCE_ALIAS_TO_ALLOWED_SOURCE: Dict[str, str] = {
    "sec_insider_trades": "sec",
    "sec_parsed_json": "sec",
    "news_qdrant": "news",
    "news_scrapes": "news",
    "macro_narratives": "news",
    "macro_history": "news",
    "gpr_index": "gpr"
}

ALLOWED_CATEGORIES: List[str] = [
    "precious_metals_spot", "macro_inflation_employment", 
    "macro_central_banks", "macro_yields_dollar", "metals_derivatives",
    "equities_spot", "equities_derivatives", "corporate_actions",
    "macro_geopolitics_risk", "macro_growth_activity", "macro_liquidity_credit",
    "commodities_energy", "commodities_agriculture",
    "fx_dollar_rates", "rates_curve_real_yields",
    "risk_sentiment_volatility", "earnings_guidance"
]

ALLOWED_METRICS: List[str] = [
    # Options-oriented intent metrics.
    "Implied Volatility (IV)", 
    "Put/Call Ratio",
    "Greeks (Delta/Gamma)",
    "Insider Trading", "Institutional Flows",
    "Open Interest", 
    "IV Skew",
    "Options Liquidity",       # Maps to fields such as is_liquid, volume, and open_interest.
    "Moneyness / OTM",         # Maps to fields such as moneyness_pct and in_the_money.
    "Time Decay / DTE",        # Maps to dte-related option horizon fields.
    "Options Pricing / Spread",# Maps to quote fields like bid, ask, and spread_pct.
    
    # Macro and SEC-oriented intent metrics.
    "Price",                   # Canonical latest price signal for macro or equity assets.
    "Price Change (%)",        # Maps to daily_change_pct and mom_change_pct fields.
    "Insider Trading", 
    "Yield Spread"
]
# Maps the LLM extracted metrics to actual database physical columns

METRIC_TO_COLUMN_MAPPING: Dict[str, List[str]] = {
    "Implied Volatility (IV)": ["implied_volatility"],
    "IV Skew": ["iv_skew"],  # Keeps skew retrieval explicitly mapped to the physical column.
    
    # Map higher-level LLM concepts to concrete storage fields defined in the data schema.
    "Options Liquidity": ["is_liquid", "volume", "open_interest"],
    "Moneyness / OTM": ["moneyness_pct", "in_the_money", "strike"],
    "Time Decay / DTE": ["dte", "expiration"],
    "Options Pricing / Spread": ["last_price", "bid", "ask", "spread_pct"],
    
    # Ensure generic "Price" requests can resolve to available physical price columns.
    "Price": ["value", "underlying_price"], 
    "Price Change (%)": ["daily_change_pct", "mom_change_pct"],
    "Insider Trading": ["insider_net_flow"],
    "Institutional Flows": ["institutional_block_volume"],
    "Put/Call Ratio": ["put_call_ratio"],
    "Greeks (Delta/Gamma)": ["delta_gamma_exposure"],
    "Yield Spread": ["yield_spread"],    
    "Open Interest": ["open_interest"],
}

# Used to map event-driven triggers to standard categories
EVENT_KEYWORDS_MAPPING: Dict[str, str] = {
    "earnings_beat": "earnings_guidance",
    "earnings_miss": "earnings_guidance",
    "guidance_raise": "earnings_guidance",
    "guidance_cut": "earnings_guidance",

    "insider_buying": "corporate_actions",
    "insider_selling": "corporate_actions",
    "share_buyback": "corporate_actions",
    "dividend_hike": "corporate_actions",
    "dividend_cut": "corporate_actions",
    "merger_acquisition": "corporate_actions",

    "rate_hike": "macro_central_banks",
    "rate_cut": "macro_central_banks",
    "fomc_minutes": "macro_central_banks",
    "powell_speech": "macro_central_banks",
    "qt_tightening": "macro_liquidity_credit",
    "liquidity_injection": "macro_liquidity_credit",

    "ppi_release": "macro_inflation_employment",
    "nfp_release": "macro_inflation_employment",
    "jobless_claims": "macro_inflation_employment",
    "wage_growth": "macro_inflation_employment",

    "gdp_release": "macro_growth_activity",
    "pmi_release": "macro_growth_activity",
    "retail_sales": "macro_growth_activity",

    "dxy_breakout": "fx_dollar_rates",
    "usd_strength": "fx_dollar_rates",
    "usd_weakness": "fx_dollar_rates",
    "yield_curve_steepen": "rates_curve_real_yields",
    "yield_curve_invert": "rates_curve_real_yields",
    "real_yield_rise": "rates_curve_real_yields",

    "gold_spike": "precious_metals_spot",
    "silver_rally": "precious_metals_spot",
    "metals_term_structure": "metals_derivatives",
    "oil_supply_shock": "commodities_energy",
    "crop_supply_shock": "commodities_agriculture",

    "vol_spike": "risk_sentiment_volatility",
    "risk_off": "risk_sentiment_volatility",
    "risk_on": "risk_sentiment_volatility"
}