"""
``Scripts.core.llm_pool`` — process-wide LLM client pool + warmup.

Why this module exists
----------------------
Multiple call-sites across the codebase (``QueryTransformer``,
``MasterRetriever.router_llm``, ``AnalystAgent``, ``CheckerAgent``,
``CriticAgent``, ``FinalizerAgent``, ``sec_processor``, ``news_scraper``,
…) instantiate ``ChatOllama`` independently. For small models this is
cheap, but the production deployment uses a fine-tuned **70B** model
(``options-expert-v1:latest`` built ``FROM Llama-3.3-70B-Instruct-Q4_K_M``)
and that introduces two real problems:

1. **First-use latency spike.** Ollama loads the model into GPU on the
   first request (30–60 s for a Q4-quantised 70B). Without a process-
   wide warmup, the *first* user query pays this penalty in full.
2. **Silent eviction.** Ollama's default ``keep_alive`` is 5 minutes.
   If an analyst query finishes and the user pauses to read the
   report, the 70B model is evicted — the next query pays the cold-
   start tax again. Setting ``keep_alive="30m"`` (or ``-1`` for
   forever) turns this into a one-time cost per daemon life.

Deployment reality (as of 2026-04-22)
-------------------------------------
Only **two** Ollama models are used at runtime:

* ``options-expert-v1:latest`` — the fine-tuned 70B. Used by **every
  agent** (Analyst / Checker / Critic / Finalizer) AND by
  ``QueryTransformer`` (both metadata-extraction and HyDE stages).
  Env override: ``OLLAMA_CUSTOM_MODEL_NAME``.
* ``llama3:latest`` — vanilla Llama-3 8B. Used **only** by the cheap
  intent router (``MasterRetriever.router_llm``) and by ingestion-time
  utilities (``news_scraper`` sentiment, ``sec_processor`` form-parser).
  Env override: ``OLLAMA_ROUTER_MODEL``.

Retrieval-time models (dense embeddings, SPLADE sparse, cross-encoder
reranker) are **not** Ollama clients — they are HuggingFace /
``fastembed`` artefacts managed by ``Scripts/vector_store/connection.py``
and are therefore *out of scope* for this pool.

Design
------
* A **role-keyed singleton cache**: ``get_ollama("analyst")`` returns the
  same underlying ``ChatOllama`` every time, so agents stop re-parsing
  the same HTTP URL and headers per call.
* **Explicit warmup**: ``warmup_sync()`` / ``warmup_async()`` fire a
  trivial prompt through each configured role, forcing Ollama to pin
  the model in GPU memory before the user's first real query arrives.
  CLI ``query`` calls this automatically; other code paths can opt in.
* **Two tiers, not four**: the expert tier (`options-expert-v1`) and
  the router tier (`llama3`). The table is env-driven so swapping in a
  different quant or a larger 405B is a config change.

Environment variables
---------------------
* ``OLLAMA_BASE_URL`` / ``OLLAMA_HOST``  (default ``http://localhost:11434``)
* ``OLLAMA_KEEP_ALIVE``                  (default ``30m``)
* ``OLLAMA_CUSTOM_MODEL_NAME``           (default ``options-expert-v1:latest``)
* ``OLLAMA_ROUTER_MODEL``                (default ``llama3:latest``)
* ``OLLAMA_INGESTION_MODEL``             (default = router; used by
  ``news_scraper`` / ``sec_processor`` when they migrate to the pool)

This module does not hard-depend on ``langchain_ollama`` at import
time — the client is created lazily inside :func:`get_ollama` so
``Scripts.core`` stays import-safe on environments without the
package (CI, docs builds).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleSpec:
    """How a role wants its ``ChatOllama`` instance built."""

    role: str
    model_env: str
    default_model: str
    temperature: float = 0.0
    format: Optional[str] = None     # e.g. "json" for structured extractors
    num_ctx: Optional[int] = None    # Ollama per-request context window


def _default_expert_model() -> str:
    """Fine-tuned 70B used by all agents + QueryTransformer."""
    return os.getenv("OLLAMA_CUSTOM_MODEL_NAME", "options-expert-v1:latest")


def _default_router_model() -> str:
    """Cheap vanilla Llama-3 used by intent routing + ingestion helpers."""
    return os.getenv("OLLAMA_ROUTER_MODEL", "llama3:latest")


def _default_ingestion_model() -> str:
    """Ingestion-time sentiment / form-parsing. Defaults to router tier."""
    return os.getenv("OLLAMA_INGESTION_MODEL", _default_router_model())


# Roles the agents ask for. Extend this table when adding new agents
# instead of inlining env lookups at call-sites.
#
# Two-tier reality:
#   * "expert"     → options-expert-v1:latest  (fine-tuned Llama-3.3-70B)
#   * "router"     → llama3:latest              (vanilla Llama-3 8B)
#
ROLE_TABLE: Dict[str, RoleSpec] = {
    # --- Expert tier (70B) --------------------------------------------------
    # All reasoning-heavy agents share the same underlying model so Ollama
    # only needs to hold one 70B in GPU memory at a time.
    "analyst":        RoleSpec("analyst",        "OLLAMA_CUSTOM_MODEL_NAME", _default_expert_model(), temperature=0.2),
    "critic":         RoleSpec("critic",         "OLLAMA_CUSTOM_MODEL_NAME", _default_expert_model(), temperature=0.3),
    "checker":        RoleSpec("checker",        "OLLAMA_CUSTOM_MODEL_NAME", _default_expert_model(), temperature=0.0),
    "finalizer":      RoleSpec("finalizer",      "OLLAMA_CUSTOM_MODEL_NAME", _default_expert_model(), temperature=0.2),
    "query_extract":  RoleSpec("query_extract",  "OLLAMA_CUSTOM_MODEL_NAME", _default_expert_model(), temperature=0.0, format="json"),
    "query_hyde":     RoleSpec("query_hyde",     "OLLAMA_CUSTOM_MODEL_NAME", _default_expert_model(), temperature=0.2),

    # --- Router tier (Llama-3 8B) -------------------------------------------
    # Cheap short-output roles where the fine-tune would be overkill.
    "router":         RoleSpec("router",         "OLLAMA_ROUTER_MODEL",      _default_router_model(),    temperature=0.0, format="json"),

    # --- Ingestion tier (Llama-3 8B) ----------------------------------------
    # Kept as a distinct role so ingestion can be swapped independently of
    # the online router if the pipeline moves to a different quant.
    "news_sentiment": RoleSpec("news_sentiment", "OLLAMA_INGESTION_MODEL",   _default_ingestion_model(), temperature=0.0),
    "sec_parser":     RoleSpec("sec_parser",     "OLLAMA_INGESTION_MODEL",   _default_ingestion_model(), temperature=0.0, format="json"),
}

# Roles that are typically warmed up before serving queries. Callers can
# still pass an explicit list to `warmup_sync` to override.
#
# The expert tier is shared by 6 roles — warming up ONE of them is
# sufficient because Ollama caches by model name, not by
# ``ChatOllama`` instance. Warm "analyst" as the canonical 70B handle
# and "router" as the canonical cheap handle.
DEFAULT_WARMUP_ROLES: tuple[str, ...] = (
    "analyst",   # pins options-expert-v1:latest → covers all agent roles
    "router",    # pins llama3:latest            → covers intent routing
)


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------


_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _resolved_model(spec: RoleSpec) -> str:
    """Final model string — env override wins over dataclass default."""
    return os.getenv(spec.model_env, spec.default_model)


def get_role_spec(role: str) -> RoleSpec:
    """Return the ``RoleSpec`` for ``role`` (case-insensitive). Unknown
    roles raise ``KeyError`` — this is intentional so typos surface
    early rather than silently creating a second cache entry."""
    key = role.lower()
    if key not in ROLE_TABLE:
        raise KeyError(
            f"Unknown LLM role {role!r}. Known: {sorted(ROLE_TABLE)}"
        )
    return ROLE_TABLE[key]


def get_ollama(role: str, **overrides: Any):
    """Return a cached ``ChatOllama`` instance for ``role``.

    Parameters
    ----------
    role
        Role key from :data:`ROLE_TABLE`.
    **overrides
        Per-call overrides (``temperature``, ``num_ctx``, …). When any
        override is present we **do not cache** — this call site gets a
        fresh client so the default singleton stays untouched.
    """
    spec = get_role_spec(role)

    if overrides:
        return _build_client(spec, **overrides)

    cache_key = spec.role
    client = _CACHE.get(cache_key)
    if client is not None:
        return client

    with _CACHE_LOCK:
        client = _CACHE.get(cache_key)
        if client is None:
            client = _build_client(spec)
            _CACHE[cache_key] = client
        return client


def _build_client(spec: RoleSpec, **overrides: Any):
    """Lazy import of ``langchain_ollama`` — keeps ``Scripts.core`` safe
    to import on dependency-lean environments."""
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "langchain_ollama is not installed; install it before using "
            "the LLM pool (`pip install -U langchain-ollama`)."
        ) from exc

    model = _resolved_model(spec)
    kwargs: Dict[str, Any] = {
        "model":       model,
        "temperature": spec.temperature,
        "base_url":    os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "keep_alive":  os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
    }
    if spec.format:
        kwargs["format"] = spec.format
    if spec.num_ctx:
        kwargs["num_ctx"] = spec.num_ctx
    kwargs.update(overrides)

    log.info(
        "llm_pool: build role=%s model=%s keep_alive=%s",
        spec.role,
        model,
        kwargs["keep_alive"],
    )
    return ChatOllama(**kwargs)


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------


WARMUP_PROMPT = "Reply with 'ok'."


@dataclass
class WarmupResult:
    role: str
    model: str
    status: str           # "ok" | "failed" | "skipped"
    latency_s: float = 0.0
    error: Optional[str] = None


def warmup_sync(
    roles: Optional[Iterable[str]] = None,
    *,
    prompt: str = WARMUP_PROMPT,
    timeout_s: float = 120.0,
) -> List[WarmupResult]:
    """Pin each configured role's model into Ollama's GPU memory.

    Fires a tiny prompt at each role sequentially (models that would
    evict each other on a single-GPU host). Returns a per-role report
    callers can log / render in the CLI.

    This function never raises — failures become ``WarmupResult(status=
    "failed")`` entries so a flaky 70B does not tank the entire
    session.
    """
    import time

    selected = list(roles or DEFAULT_WARMUP_ROLES)
    results: List[WarmupResult] = []
    for role in selected:
        try:
            spec = get_role_spec(role)
        except KeyError:
            results.append(WarmupResult(role=role, model="?", status="skipped",
                                        error="unknown role"))
            continue

        started = time.monotonic()
        model = _resolved_model(spec)
        try:
            client = get_ollama(role)
            _ = client.invoke(prompt)
            results.append(WarmupResult(
                role=role, model=model, status="ok",
                latency_s=time.monotonic() - started,
            ))
        except Exception as exc:  # noqa: BLE001 — want full trap
            results.append(WarmupResult(
                role=role, model=model, status="failed",
                latency_s=time.monotonic() - started,
                error=str(exc),
            ))
    return results


def warmup_async(
    roles: Optional[Iterable[str]] = None,
    *,
    prompt: str = WARMUP_PROMPT,
) -> "threading.Thread":
    """Fire-and-forget warmup. Returns the ``Thread`` in case the caller
    wants to ``join()`` before serving the first query.

    Use this in daemon mode: warmup happens in the background while the
    scheduler loop starts ticking, so the first user query lands on an
    already-loaded model.
    """
    t = threading.Thread(
        target=warmup_sync,
        kwargs={"roles": roles, "prompt": prompt},
        name="llm_pool.warmup",
        daemon=True,
    )
    t.start()
    return t


def clear_cache() -> None:
    """Drop every cached client. Used by tests; rarely needed in prod."""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "DEFAULT_WARMUP_ROLES",
    "ROLE_TABLE",
    "RoleSpec",
    "WarmupResult",
    "clear_cache",
    "get_ollama",
    "get_role_spec",
    "warmup_async",
    "warmup_sync",
]
