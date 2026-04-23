"""Agents package — LangGraph nodes (Router, Analyst, Checker, Critic, Finalizer).

Intentionally left import-empty: each agent class brings heavy LLM / LangChain
dependencies and should be imported by its leaf module path, e.g.
`from Scripts.agents.router import build_graph`. This keeps CLI / smoke tests
from paying the cost of instantiating LangChain clients just to import an
unrelated helper.
"""
