"""
financial_config.py
Role: Centralized Financial Domain Knowledge & Prompt Engineering Center.
Description: This file serves as the Single Source of Truth (SSoT) for all AI agents in the RAG pipeline.
             It decouples professional terminology and expert personas from execution logic.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from Scripts.core.financial_ontology import (
    ALLOWED_CATEGORIES,
    ALLOWED_METRICS,
    ALLOWED_SOURCES,
    DATASET_PHYSICAL_SCHEMA,
)

# ==========================================
# 1. PROFESSIONAL FINANCIAL TERMINOLOGY (Taxonomy)
# ==========================================
FINANCIAL_TERMS = {
    "valuation": ["DCF", "PE ratio", "PB ratio", "EV/EBITDA", "NPV", "IRR", "WACC", "F-Score"],
    "risk_management": ["VaR (Value at Risk)", "Beta", "Volatility Skew", "Sharpe Ratio", "Sortino", "Max Drawdown", "Hedging"],
    "market_analysis": ["Technical Analysis", "Fundamental Analysis", "Sentiment Analysis", "Liquidity", "Order Flow"],
    "derivatives": ["Options", "Futures", "Swaps", "Greeks (Delta, Gamma, Theta, Vega)", "Implied Volatility (IV)", "IV Rank"],
    "fixed_income": ["Yield Curve", "Duration", "Convexity", "Credit Spread", "Default Risk", "OAS"],
    "macro_economics": ["Inflation (CPI/PCE)", "Interest Rates (Fed Funds)", "GDP", "Unemployment", "GPR (Geopolitical Risk)"]
}

# ==========================================
# 2. ANALYSIS FRAMEWORKS (The Logic Chains)
# ==========================================
ANALYSIS_FRAMEWORKS = {
    "investment_analysis": """
    ### FRAMEWORK: FUNDAMENTAL INVESTMENT ANALYSIS
    1. Quantitative: Financial statements, Profitability ratios, Debt levels.
    2. Qualitative: Moat analysis, Management quality, Industry tailwinds.
    3. Valuation: Comparative analysis vs DCF intrinsic value.
    """,
    
    "options_expert_logic": """
    ### FRAMEWORK: SYSTEMATIC OPTIONS TRADING
    1. Volatility Regime: Current IV vs Historical Volatility. Check IV Rank/Percentile.
    2. Greek Exposure: Evaluate Delta for direction, Vega for vol-sensitivity, Theta for time decay.
    3. Strategy Selection: 
       - High IV: Credit Spreads, Iron Condors (Short Vol).
       - Low IV: Long Straddles, Debit Spreads (Long Vol).
    """,

    "macro_geopolitics": """
    ### FRAMEWORK: MACRO-DRIVEN ASSET ALLOCATION
    1. GPR Impact: Track Geopolitical Risk Index for safe-haven flows (Gold/Silver).
    2. Yield Correlation: 10Y Treasury Yields vs DXY vs Precious Metals.
    3. Central Bank Pivot: Rate hike/cut expectations and terminal rate projections.
    """
}

# ==========================================
# 3. EXPERT PERSONAS (System Messages)
# ==========================================
ROLE_PROMPTS = {
    "options_strategist": """
    You are a Senior Options Strategist with 25+ years of experience in market-neutral strategies.
    Expertise: Mastery of the Greeks, volatility arbitrage, and tail-risk hedging.
    Tone: Precise, professional, data-centric, and risk-averse.
    Goal: Identify high-probability option setups with favorable risk-to-reward ratios.
    """,
    
    "macro_analyst": """
    You are a Global Macro Researcher specializing in the interaction between GPR and Commodities.
    Expertise: Cross-asset correlation, Fed policy interpretation, and precious metals.
    Tone: Analytical, forward-looking, and focused on regime shifts.
    """
}

# ==========================================
# 4. OUTPUT SPECIFICATIONS (Standardization)
# ==========================================
OUTPUT_CONSTRAINTS = {
    "markdown_report": """
    Your response must be formatted as a structured Markdown report:
    # [Asset/Strategy Name] Analysis
    ## Executive Summary
    - Confidence Level: [0-100%]
    - Primary Driver: [Macro/Technical/Earnings]
    ## Quantitative Evidence
    [List specific data points like IV Rank or Price levels]
    ## Proposed Strategy
    - Setup: [e.g., Bull Put Spread]
    - Rationale: [Max 2 sentences]
    """,
    
    "json_schema": {
        "ticker": "string",
        "direction": "Bullish/Bearish/Neutral",
        "iv_status": "High/Low/Neutral",
        "confidence": "float"
    }
}

# ==========================================
# 5. DYNAMIC CONTEXT HELPER
# ==========================================
def get_financial_context(query: str) -> str:
    """
    Dynamically retrieves relevant frameworks based on query keywords.
    Ensures the LLM is anchored with the correct domain knowledge.
    """
    query_lc = query.lower()
    context = []
    
    if any(k in query_lc for k in ["option", "greek", "iv", "volatility", "spread"]):
        context.append(ANALYSIS_FRAMEWORKS["options_expert_logic"])
    
    if any(k in query_lc for k in ["gold", "silver", "gpr", "geopolitic", "fed", "macro"]):
        context.append(ANALYSIS_FRAMEWORKS["macro_geopolitics"])
        
    if not context:
        context.append(ANALYSIS_FRAMEWORKS["investment_analysis"])
        
    return "\n\n".join(context)

def build_modelfile_system_prompt() -> str:
    """
    Specifically for 'create_options_expert.py'. 
    Combines the best of all worlds into a single System Prompt for the Modelfile.
    """
    return f"""
    {ROLE_PROMPTS['options_strategist']}
    
    [ANALYSIS STANDARDS]:
    {ANALYSIS_FRAMEWORKS['options_expert_logic']}
    {ANALYSIS_FRAMEWORKS['macro_geopolitics']}
    
    [OUTPUT REQUIREMENTS]:
    {OUTPUT_CONSTRAINTS['markdown_report']}
    """.strip()


def _compact_join(values: List[str], limit: int = 12) -> str:
    trimmed = [str(v).strip() for v in values if str(v).strip()]
    if not trimmed:
        return "(none)"
    if len(trimmed) <= limit:
        return ", ".join(trimmed)
    return ", ".join(trimmed[:limit]) + f", ... (+{len(trimmed) - limit} more)"


@lru_cache(maxsize=1)
def get_financial_ontology_context() -> str:
    """
    Compact ontology contract injected into agent system prompts.

    Goal:
    - Keep API and Ollama runs anchored to the same financial vocabulary.
    - Remind every node what the project can and cannot legitimately cite.
    """
    options_fields = sorted(DATASET_PHYSICAL_SCHEMA.get("options", []))
    macro_fields = sorted(DATASET_PHYSICAL_SCHEMA.get("macro", []))
    sec_fields = sorted(DATASET_PHYSICAL_SCHEMA.get("sec_qdrant_payload", []))
    news_fields = sorted(DATASET_PHYSICAL_SCHEMA.get("news_qdrant_payload", []))

    return (
        "=== PROJECT FINANCIAL DOMAIN CONTRACT ===\n"
        "This system operates inside a financial RAG pipeline with strict source discipline.\n"
        f"- Allowed Gold source_types: {_compact_join(sorted(ALLOWED_SOURCES), limit=8)}\n"
        f"- Allowed semantic categories: {_compact_join(sorted(ALLOWED_CATEGORIES), limit=10)}\n"
        f"- Allowed metric labels: {_compact_join(sorted(ALLOWED_METRICS), limit=12)}\n"
        "- Silver parquet is the source of truth for quantitative claims.\n"
        "- Gold Qdrant records are the source of truth for qualitative/news/SEC claims.\n"
        "- If a requested metric is not in project scope, say so plainly instead of inferring substitutes.\n"
        "- Preserve financial terminology exactly; do not rename source fields or invent synthetic fields.\n"
        f"- Options Silver fields include: {_compact_join(options_fields)}\n"
        f"- Macro Silver fields include: {_compact_join(macro_fields)}\n"
        f"- SEC Gold payload fields include: {_compact_join(sec_fields)}\n"
        f"- News Gold payload fields include: {_compact_join(news_fields)}"
    )


def get_shared_financial_system_prompt() -> str:
    """
    Shared financial context for all agent nodes, regardless of provider.
    """
    return (
        "=== INSTITUTIONAL FINANCIAL OPERATING CONTEXT ===\n"
        f"{ANALYSIS_FRAMEWORKS['options_expert_logic'].strip()}\n\n"
        f"{ANALYSIS_FRAMEWORKS['macro_geopolitics'].strip()}\n\n"
        f"{ANALYSIS_FRAMEWORKS['investment_analysis'].strip()}\n\n"
        f"{get_financial_ontology_context()}"
    )


@lru_cache(maxsize=1)
def load_modelfile_system_prompt(modelfile_path: Optional[str] = None) -> str:
    """
    Load SYSTEM instruction block from repository `modelfile`.

    Why this exists:
    - Keeps analyst runtime prompt aligned with model training/runtime card.
    - Provides one canonical source for professional system behavior.
    """
    root = Path(__file__).resolve().parents[2]
    fp = Path(modelfile_path) if modelfile_path else (root / "modelfile")
    if not fp.exists():
        return build_modelfile_system_prompt()

    raw = fp.read_text(encoding="utf-8")
    match = re.search(r'SYSTEM\s+"""(.*?)"""', raw, flags=re.DOTALL)
    if not match:
        return build_modelfile_system_prompt()
    system_prompt = match.group(1).strip()
    return system_prompt if system_prompt else build_modelfile_system_prompt()


def get_analyst_system_prompt() -> str:
    """
    Canonical analyst system prompt source.

    Policy:
    1) Prefer `modelfile` SYSTEM for strict runtime/training alignment.
    2) Fall back to synthesized institutional prompt if missing.
    """
    return (
        f"{load_modelfile_system_prompt()}\n\n"
        f"{get_shared_financial_system_prompt()}"
    ).strip()
