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
    }
]