"""
`Scripts.retrieval` — public API of the retrieval / transformation layer.

This facade is deliberately **lazy** (PEP 562). Until recently the module
eagerly imported ``query_transform`` / ``qdrant_retriever`` /
``master_retriever`` at package import time, which transitively dragged
``langchain_ollama`` / ``langchain_core`` / ``dotenv`` / heavy ontology
regex tables into every caller — including the CLI's ``status`` and
``ingest --dry-run`` paths which do *not* need the LLM. That made cold
starts slow and broke smoke tests that stub only the pieces they need.

Rules of the road
-----------------
* **Pure-data types** from :mod:`Scripts.retrieval.schema` load eagerly
  because they have no runtime dependencies — importing them is free
  and they are referenced almost everywhere.
* **LLM-backed classes** (``QueryTransformer``, ``FinancialHybridRetriever``,
  ``MasterRetriever``) load lazily on first attribute access and are
  cached into module globals, so subsequent accesses are free.

Example
-------
>>> from Scripts.retrieval import TimeWindow           # eager — fast
>>> from Scripts.retrieval import MasterRetriever     # triggers lazy
...                                                    # load; cached.
>>> # Equivalent direct-leaf import for agents that want to skip the
>>> # facade entirely (no LLM dep resolution until instance creation):
>>> from Scripts.retrieval.master_retriever import MasterRetriever
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

# ---------------------------------------------------------------------------
# Eager re-exports: dependency-free data types
# ---------------------------------------------------------------------------
from .schema import (
    ActionDirection,
    AgentState,
    FormTypeFilter,
    FullTransformationResult,
    HyDEGeneration,
    MetadataExtraction,
    QueryIntent,
    RetrievedChunk,
    SearchMode,
    SECMetadata,
    SentimentTarget,
    SourceType,
    SQLResult,
    TIME_WINDOW_DAYS,
    TimeWindow,
    time_window_to_days,
)

# ---------------------------------------------------------------------------
# Lazy attribute table — resolved on first access via PEP 562 __getattr__
# ---------------------------------------------------------------------------

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    # attr_name            -> (submodule,           export_name)
    "QueryTransformer":    ("query_transform",     "QueryTransformer"),
    "FinancialHybridRetriever": (
        "qdrant_retriever",
        "FinancialHybridRetriever",
    ),
    "MasterRetriever":     ("master_retriever",    "MasterRetriever"),
}


def __getattr__(name: str) -> Any:  # PEP 562 — runs only on unknown attrs
    if name in _LAZY_ATTRS:
        submodule, export = _LAZY_ATTRS[name]
        module = import_module(f"{__name__}.{submodule}")
        obj = getattr(module, export)
        globals()[name] = obj  # cache so future lookups are free
        return obj
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:  # Tooling-friendly completion
    return sorted(set(globals().keys()) | set(__all__))


__all__ = [
    # Eager data types
    "ActionDirection",
    "AgentState",
    "FormTypeFilter",
    "FullTransformationResult",
    "HyDEGeneration",
    "MetadataExtraction",
    "QueryIntent",
    "RetrievedChunk",
    "SearchMode",
    "SECMetadata",
    "SentimentTarget",
    "SourceType",
    "SQLResult",
    "TIME_WINDOW_DAYS",
    "TimeWindow",
    "time_window_to_days",
    # Lazy LLM-backed classes
    "QueryTransformer",
    "FinancialHybridRetriever",
    "MasterRetriever",
]

if TYPE_CHECKING:  # Static analysers only — no runtime cost.
    from .master_retriever import MasterRetriever  # noqa: F401
    from .qdrant_retriever import FinancialHybridRetriever  # noqa: F401
    from .query_transform import QueryTransformer  # noqa: F401
