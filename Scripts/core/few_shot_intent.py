# Scripts/core/few_shot_intent.py

INTENT_FEW_SHOT_EXAMPLES = """
User Query: "What is the current IV Skew for AAPL?"
Path: sql_only
Reasoning: Requesting a specific quantitative metric (IV Skew) for a specific ticker.

User Query: "How does the Fed's recent rate hike affect gold sentiment?"
Path: vector_only
Reasoning: Analytical/Qualitative question about sentiment and macro impact.

User Query: "Did the recent spike in GPR index cause the unusual put buying in SPY last Friday?"
Path: hybrid_both
Reasoning: Linking a macro event (GPR spike - Vector) with specific market data (Put buying - SQL).

User Query: "List all Form 4 insider buys for NVDA in the last 7 days."
Path: sql_only
Reasoning: Factual data retrieval from structured records.

User Query: "Explain the logic behind hedging with protective puts in a high volatility environment."
Path: vector_only
Reasoning: Educational/Conceptual explanation.
"""