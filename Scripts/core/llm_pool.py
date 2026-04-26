"""
`Scripts.core.llm_pool` - process-wide model client pool + warmup.

Mixed-provider layout:
- Ollama roles: router/checker/ingestion helpers.
- OpenAI-compatible roles: query_extract/query_hyde (retriever transform).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleSpec:
    role: str
    provider: str  # "ollama" | "openai"
    model_env: str
    default_model: str
    temperature: float = 0.0
    format: Optional[str] = None
    num_ctx: Optional[int] = None


def _default_expert_model() -> str:
    return os.getenv("OLLAMA_CUSTOM_MODEL_NAME", "options-expert-v1:latest")


def _default_router_model() -> str:
    return os.getenv("OLLAMA_ROUTER_MODEL", "llama3:latest")


def _default_checker_model() -> str:
    return os.getenv("OLLAMA_CHECKER_MODEL", "llama3:latest")


def _default_ingestion_model() -> str:
    return os.getenv("OLLAMA_INGESTION_MODEL", _default_router_model())


ROLE_TABLE: Dict[str, RoleSpec] = {
    # Ollama runtime roles
    "router": RoleSpec("router", "ollama", "OLLAMA_ROUTER_MODEL", _default_router_model(), temperature=0.0, format="json"),
    "checker": RoleSpec("checker", "ollama", "OLLAMA_CHECKER_MODEL", _default_checker_model(), temperature=0.0, format="json"),
    "news_sentiment": RoleSpec("news_sentiment", "ollama", "OLLAMA_INGESTION_MODEL", _default_ingestion_model(), temperature=0.0),
    "sec_parser": RoleSpec("sec_parser", "ollama", "OLLAMA_INGESTION_MODEL", _default_ingestion_model(), temperature=0.0, format="json"),

    # OpenAI-compatible transform roles
    "query_extract": RoleSpec("query_extract", "openai", "TRANSFORM_EXTRACTOR_MODEL", "gpt-4o-mini", temperature=0.0),
    "query_hyde": RoleSpec("query_hyde", "openai", "TRANSFORM_HYDE_MODEL", "gpt-4o-mini", temperature=0.1),

    # Legacy aliases (kept for compatibility; not default warmup)
    "analyst": RoleSpec("analyst", "ollama", "OLLAMA_ANALYST_MODEL", _default_expert_model(), temperature=0.0),
    "critic": RoleSpec("critic", "ollama", "OLLAMA_CRITIC_MODEL", _default_expert_model(), temperature=0.0),
    "finalizer": RoleSpec("finalizer", "ollama", "OLLAMA_FINALIZER_MODEL", _default_expert_model(), temperature=0.0),
}


# Prioritise expert warmup first, then lightweight router/checker.
DEFAULT_WARMUP_ROLES: tuple[str, ...] = ("analyst", "router", "checker")


_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _resolved_model(spec: RoleSpec) -> str:
    return os.getenv(spec.model_env, spec.default_model)


def get_role_spec(role: str) -> RoleSpec:
    key = role.lower()
    if key not in ROLE_TABLE:
        raise KeyError(f"Unknown LLM role {role!r}. Known: {sorted(ROLE_TABLE)}")
    return ROLE_TABLE[key]


def get_client(role: str, **overrides: Any):
    """Provider-agnostic client getter."""
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


def get_ollama(role: str, **overrides: Any):
    """Backward-compatible Ollama-only accessor."""
    spec = get_role_spec(role)
    if spec.provider != "ollama":
        raise ValueError(
            f"Role {role!r} is provider={spec.provider!r}, not ollama. "
            "Use get_client(role, ...) instead."
        )
    return get_client(role, **overrides)


def _build_client(spec: RoleSpec, **overrides: Any):
    if spec.provider == "ollama":
        return _build_ollama_client(spec, **overrides)
    if spec.provider == "openai":
        return _build_openai_client(spec, **overrides)
    raise ValueError(f"Unsupported provider={spec.provider!r} for role={spec.role!r}")


def _build_ollama_client(spec: RoleSpec, **overrides: Any):
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "langchain_ollama is not installed; install with "
            "`pip install -U langchain-ollama`."
        ) from exc

    model = _resolved_model(spec)
    kwargs: Dict[str, Any] = {
        "model": model,
        "temperature": spec.temperature,
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
    }
    if spec.format:
        kwargs["format"] = spec.format
    if spec.num_ctx:
        kwargs["num_ctx"] = spec.num_ctx
    kwargs.update(overrides)

    log.info(
        "llm_pool: build role=%s provider=ollama model=%s keep_alive=%s",
        spec.role,
        model,
        kwargs["keep_alive"],
    )
    return ChatOllama(**kwargs)


def _build_openai_client(spec: RoleSpec, **overrides: Any):
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "langchain_openai is not installed; install with "
            "`pip install -U langchain-openai`."
        ) from exc

    model = _resolved_model(spec)
    kwargs: Dict[str, Any] = {
        "model": model,
        "temperature": spec.temperature,
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "timeout": float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60")),
    }
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(overrides)

    log.info(
        "llm_pool: build role=%s provider=openai model=%s base_url=%s",
        spec.role,
        model,
        kwargs.get("base_url", "(default)"),
    )
    return ChatOpenAI(**kwargs)


WARMUP_PROMPT = "Reply with 'ok'."


@dataclass
class WarmupResult:
    role: str
    model: str
    status: str  # "ok" | "failed" | "skipped"
    latency_s: float = 0.0
    error: Optional[str] = None


def warmup_sync(
    roles: Optional[Iterable[str]] = None,
    *,
    prompt: str = WARMUP_PROMPT,
    timeout_s: float = 120.0,
) -> List[WarmupResult]:
    """Warm selected roles sequentially. Failures are returned, never raised."""
    import time

    _ = timeout_s  # API compatibility placeholder
    selected = list(roles or DEFAULT_WARMUP_ROLES)
    results: List[WarmupResult] = []
    for role in selected:
        try:
            spec = get_role_spec(role)
        except KeyError:
            results.append(WarmupResult(role=role, model="?", status="skipped", error="unknown role"))
            continue

        started = time.monotonic()
        model = _resolved_model(spec)
        try:
            client = get_client(role)
            _ = client.invoke(prompt)
            results.append(WarmupResult(role=role, model=model, status="ok", latency_s=time.monotonic() - started))
        except Exception as exc:  # noqa: BLE001
            results.append(
                WarmupResult(
                    role=role,
                    model=model,
                    status="failed",
                    latency_s=time.monotonic() - started,
                    error=str(exc),
                )
            )
    return results


def warmup_async(
    roles: Optional[Iterable[str]] = None,
    *,
    prompt: str = WARMUP_PROMPT,
) -> "threading.Thread":
    t = threading.Thread(
        target=warmup_sync,
        kwargs={"roles": roles, "prompt": prompt},
        name="llm_pool.warmup",
        daemon=True,
    )
    t.start()
    return t


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "DEFAULT_WARMUP_ROLES",
    "ROLE_TABLE",
    "RoleSpec",
    "WarmupResult",
    "clear_cache",
    "get_client",
    "get_ollama",
    "get_role_spec",
    "warmup_async",
    "warmup_sync",
]

