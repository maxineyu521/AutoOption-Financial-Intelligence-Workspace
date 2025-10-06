# financial_config.py
"""
Financial Domain Specialization Configuration
Contains financial terminology, analysis frameworks, professional prompts, etc.
"""

# Financial professional terminology and concepts
FINANCIAL_TERMS = {
    "valuation": ["DCF", "PE ratio", "PB ratio", "EV/EBITDA", "NPV", "IRR", "WACC"],
    "risk_management": ["VaR", "beta", "volatility", "correlation", "diversification", "hedging"],
    "market_analysis": ["technical analysis", "fundamental analysis", "market sentiment", "liquidity"],
    "derivatives": ["options", "futures", "swaps", "forwards", "greeks", "implied volatility"],
    "fixed_income": ["yield curve", "duration", "convexity", "credit spread", "default risk"],
    "macro_economics": ["inflation", "interest rates", "GDP", "unemployment", "fiscal policy", "monetary policy"]
}

# Financial analysis frameworks
ANALYSIS_FRAMEWORKS = {
    "investment_analysis": """
    Investment Analysis Framework:
    1. Fundamental Analysis
       - Financial Statement Analysis
       - Industry Analysis 
       - Competitive Analysis
       - Management Assessment
    
    2. Technical Analysis
       - Price Trends
       - Support/Resistance Levels
       - Technical Indicators
       - Volume Analysis
    
    3. Risk Assessment
       - Market Risk
       - Credit Risk
       - Liquidity Risk
       - Operational Risk
    """,
    
    "options_analysis": """
    Options Analysis Framework:
    1. Basic Analysis
       - Intrinsic Value
       - Time Value
       - Implied Volatility
    
    2. Greeks
       - Delta: Price sensitivity
       - Gamma: Delta change rate
       - Theta: Time decay
       - Vega: Volatility sensitivity
       - Rho: Interest rate sensitivity
    
    3. Strategy Evaluation
       - Risk/Reward Ratio
       - Win Rate
       - Maximum Drawdown
    """
}

# Professional prompt templates
FINANCIAL_PROMPTS = {
    "analyst_role": """
    You are a senior financial analyst with the following professional background:
    - CFA (Chartered Financial Analyst) certification
    - 15 years of investment banking and asset management experience
    - Expertise in stocks, bonds, derivatives, foreign exchange and other financial instruments
    - Familiar with major global financial markets and regulatory environments
    
    Please analyze financial issues with a professional, objective, and rigorous attitude, providing insights based on data and logic.
    """,
    
    "risk_manager_role": """
    You are an experienced risk management expert with:
    - FRM (Financial Risk Manager) certification
    - 10 years of institutional risk management experience
    - Expertise in VaR models, stress testing, scenario analysis
    - Familiar with Basel Accords and regulatory requirements
    
    Please evaluate investment opportunities from a risk management perspective, identify potential risks and provide mitigation measures.
    """,
    
    "quantitative_analyst_role": """
    You are a quantitative analyst with expertise in:
    - Financial mathematics and statistics background
    - Proficiency in Python, R, MATLAB and other programming tools
    - Familiar with machine learning applications in finance
    - Experience in high-frequency trading and algorithmic trading
    
    Please provide quantitative insights based on mathematical models and statistical analysis.
    """
}

# Financial calculation tool prompts
CALCULATION_TOOLS = {
    "present_value": "Present value calculation: PV = FV / (1 + r)^n",
    "future_value": "Future value calculation: FV = PV * (1 + r)^n", 
    "annuity": "Annuity present value: PVA = PMT * [1 - (1 + r)^-n] / r",
    "bond_yield": "Bond yield calculation: YTM = (C + (FV - PV)/n) / ((FV + PV)/2)",
    "black_scholes": "Black-Scholes options pricing model",
    "var_calculation": "VaR calculation: VaR = μ - σ * Z(α)"
}

# Regulatory and compliance knowledge
REGULATORY_KNOWLEDGE = {
    "sec_regulations": "SEC regulatory requirements, disclosure requirements, insider trading rules",
    "basel_iii": "Basel III capital adequacy requirements",
    "dodd_frank": "Dodd-Frank Act financial reform",
    "mifid_ii": "EU Markets in Financial Instruments Directive II",
    "gaap_ifsrs": "US GAAP and IFRS accounting standard differences"
}

def get_financial_context(query: str) -> str:
    """Return relevant financial professional context based on query content"""
    context_parts = []
    
    # Add basic analysis frameworks
    if any(word in query.lower() for word in ["investment", "stock", "analysis"]):
        context_parts.append(ANALYSIS_FRAMEWORKS["investment_analysis"])
    
    if any(word in query.lower() for word in ["option", "derivative", "options"]):
        context_parts.append(ANALYSIS_FRAMEWORKS["options_analysis"])
    
    # Add relevant terminology
    relevant_terms = []
    for category, terms in FINANCIAL_TERMS.items():
        if any(term in query.lower() for term in terms):
            relevant_terms.extend(terms)
    
    if relevant_terms:
        context_parts.append(f"Relevant financial terminology: {', '.join(relevant_terms)}")
    
    return "\n\n".join(context_parts)

def get_role_based_prompt(role: str = "analyst") -> str:
    """Get role-based professional prompt"""
    return FINANCIAL_PROMPTS.get(role, FINANCIAL_PROMPTS["analyst_role"])
