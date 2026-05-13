FEW_SHOT_EXAMPLES = [
    {
        "scenario": "Macro & News (Geopolitics)",
        "user_query": "How did the recent Middle East tensions affect Gold prices and volatility sentiment in news?",
        "extraction": {
            "logical_reasoning": "Macro Step: Middle East tensions (Geopolitics). Meso Step: Safe-haven asset flow. Micro Step: Gold (XAU/GLD). Metrics: Price & Volatility Sentiment.",
            "tickers": ["GLD", "XAU"],
            "source_types": ["news", "gpr"],
            "event_keyword": "geopolitical_tension",
            "metrics": ["Sentiment Score", "Volatility"],
            "time_window": "past_month"
        }
    },
    {
        "scenario": "SEC Insider Trading (Form 4)",
        "user_query": "Show me all CEO stock offloading for NVDA in the last half year.",
        "extraction": {
            "logical_reasoning": "Macro Step: N/A. Meso Step: Insider activity. Micro Step: NVDA CEO. Action: SELL. Form: 4.",
            "tickers": ["NVDA"],
            "source_types": ["sec"],
            "action_direction": "SELL",
            "form_type": "4",
            "time_window": "past_six_months"
        }
    },
    {
        "scenario": "Options Chain & Volatility",
        "user_query": "Is there an IV skew shift for SPY following the recent Fed meeting?",
        "extraction": {
            "logical_reasoning": "Macro Step: Fed Meeting (Rates). Meso Step: Options market reaction. Micro Step: SPY. Metric: IV Skew.",
            "tickers": ["SPY"],
            "source_types": ["options", "news"],
            "event_keyword": "fed_meeting",
            "metrics": ["Implied Volatility (IV)", "IV Skew"],
            "time_window": "past_month"
        }
    },
    {
        "scenario": "Finalizer Expression - Options Microstructure",
        "user_query": "Past week SPY put-call ratio, ATM IV, and liquid 30-DTE puts?",
        "extraction": {
            "logical_reasoning": "Finalizer should open with strict options evidence first: PCR, ATM IV, then liquidity. Recommendation mode belongs in the second sentence, not the first."
        }
    },
    {
        "scenario": "Finalizer Expression - Insider Flow",
        "user_query": "Past month AAPL Form-4 insider flow and options risk posture?",
        "extraction": {
            "logical_reasoning": "Finalizer should open with insider direction and strength first, then connect to options posture. Do not lead with macro background."
        }
    },
    {
        "scenario": "Finalizer Expression - Cross Asset Regime",
        "user_query": "Past month QQQ ATM IV versus VIX regime for hedging?",
        "extraction": {
            "logical_reasoning": "Finalizer should open with the IV versus VIX regime interpretation first. Macro backdrop may follow later, but cannot take the first sentence."
        }
    },
    {
        "scenario": "Finalizer Expression - Geopolitical Commodity",
        "user_query": "Past month GPR index and GLD option regime for hedging?",
        "extraction": {
            "logical_reasoning": "Finalizer should open with the direct implication of GPR or geopolitical context on GLD options posture. If fallback occurred, explain the time-window extension in risks."
        }
    }
]
