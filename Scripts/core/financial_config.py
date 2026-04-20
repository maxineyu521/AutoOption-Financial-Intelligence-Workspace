"""
financial_config.py
Role: Centralized Financial Domain Knowledge & Prompt Engineering Center.
Description: This file serves as the Single Source of Truth (SSoT) for all AI agents in the RAG pipeline.
             It decouples professional terminology and expert personas from execution logic.
"""

from typing import Dict, List, Optional

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