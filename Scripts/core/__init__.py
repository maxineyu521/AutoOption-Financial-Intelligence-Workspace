"""
`Scripts.core` — pure primitives layer.

Contract (enforced by convention, not code):
    * zero network I/O
    * zero LLM clients
    * zero scraper dependencies
    * safe to import from any orchestrator / agent / scraper / test

Because this package is imported by *everything* in the project, eager
imports of heavy sub-modules (large regex tables, pandas, etc.) would
force every caller to pay that cost. Instead we expose a PEP 562 lazy
facade: `from Scripts.core import trading_calendar` loads the module
the first time it is referenced, and caches the result in this module's
globals so subsequent accesses are free.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = [
    # Sub-modules (lazy)
    "trading_calendar",
    "financial_ontology",
    "financial_config",
    "universe",
    "prompt_templates",
    "intent_router_prompt_templates",
    "few_shot_config",
    "few_shot_intent",
    "llm_pool",
    "yfinance_bootstrap",
    # Re-exported primitives — the most common agent-side helpers
    "is_business_day",
    "previous_business_day",
    "next_business_day",
    "n_business_days_back",
    "clamp_to_business_day",
    "business_days_between",
]

_LAZY_SUBMODULES = frozenset(
    {
        "trading_calendar",
        "financial_ontology",
        "financial_config",
        "universe",
        "prompt_templates",
        "intent_router_prompt_templates",
        "few_shot_config",
        "few_shot_intent",
        "llm_pool",
        "yfinance_bootstrap",
    }
)

_LAZY_SYMBOLS: dict[str, tuple[str, str]] = {
    "is_business_day": ("trading_calendar", "is_business_day"),
    "previous_business_day": ("trading_calendar", "previous_business_day"),
    "next_business_day": ("trading_calendar", "next_business_day"),
    "n_business_days_back": ("trading_calendar", "n_business_days_back"),
    "clamp_to_business_day": ("trading_calendar", "clamp_to_business_day"),
    "business_days_between": ("trading_calendar", "business_days_between"),
}


def __getattr__(name: str) -> Any:  # PEP 562
    if name in _LAZY_SUBMODULES:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    if name in _LAZY_SYMBOLS:
        mod_name, attr = _LAZY_SYMBOLS[name]
        obj = getattr(import_module(f"{__name__}.{mod_name}"), attr)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:  # Help IDEs / REPL completion
    return sorted(list(globals().keys()) + list(__all__))


if TYPE_CHECKING:  # Static analysers only — never executed at runtime
    from . import (  # noqa: F401
        financial_config,
        financial_ontology,
        few_shot_config,
        few_shot_intent,
        intent_router_prompt_templates,
        prompt_templates,
        trading_calendar,
        universe,
    )
    from .trading_calendar import (  # noqa: F401
        business_days_between,
        clamp_to_business_day,
        is_business_day,
        n_business_days_back,
        next_business_day,
        previous_business_day,
    )
