"""
`Scripts.core.llm_pool` - process-wide model client pool + warmup.

Current policy:
- Agent graph roles resolve provider/model dynamically from `.env`.
- Query-transform roles stay OpenAI-compatible by default.
- Ingestion helper roles can flip between OpenAI and Ollama.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleSpec:
    role: str
    default_provider: str  # "ollama" | "openai"
    model_env: str
    default_model: str
    provider_env: Optional[str] = None
    provider_legacy_env: Optional[str] = None
    provider_legacy_map: Tuple[Tuple[str, str], ...] = ()
    provider_model_envs: Tuple[Tuple[str, str], ...] = ()
    timeout_env: Optional[str] = None
    temperature: float = 0.0
    format: Optional[str] = None
    num_ctx: Optional[int] = None


def _default_expert_ollama_model() -> str:
    return os.getenv("OLLAMA_CUSTOM_MODEL_NAME", "options-expert-v1:latest")


def _default_router_ollama_model() -> str:
    return os.getenv("OLLAMA_ROUTER_MODEL", "llama3:latest")


def _default_router_openai_model() -> str:
    return os.getenv("ROUTER_OPENAI_FALLBACK_MODEL", "gpt-4o-mini")


def _default_checker_ollama_model() -> str:
    return os.getenv("OLLAMA_CHECKER_MODEL", "llama3:latest")


def _default_checker_openai_model() -> str:
    return os.getenv("CHECKER_OPENAI_FALLBACK_MODEL", "gpt-4o-mini")


def _default_critic_ollama_model() -> str:
    return os.getenv("OLLAMA_CRITIC_MODEL", _default_expert_ollama_model())


def _default_critic_openai_model() -> str:
    return os.getenv("CRITIC_OPENAI_FALLBACK_MODEL", "gpt-4o-mini")


def _default_analyst_openai_model() -> str:
    return os.getenv("ANALYST_PRIMARY_MODEL", "gpt-5-mini")


def _default_finalizer_openai_model() -> str:
    return os.getenv("FINALIZER_PRIMARY_MODEL", "gpt-5-mini")


def _default_ingestion_ollama_model() -> str:
    return os.getenv("OLLAMA_INGESTION_MODEL", _default_router_ollama_model())


def _default_ingestion_openai_model() -> str:
    return os.getenv("OPENAI_INGESTION_MODEL", "gpt-4o-mini")


ROLE_TABLE: Dict[str, RoleSpec] = {
    "router": RoleSpec(
        "router",
        "openai",
        "ROUTER_MODEL",
        _default_router_openai_model(),
        provider_env="ROUTER_PROVIDER",
        provider_model_envs=(("openai", "ROUTER_MODEL"), ("ollama", "OLLAMA_ROUTER_MODEL")),
        timeout_env="ROUTER_OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
        format="json",
    ),
    "analyst": RoleSpec(
        "analyst",
        "openai",
        "ANALYST_MODEL",
        _default_analyst_openai_model(),
        provider_env="ANALYST_PROVIDER",
        provider_legacy_env="ANALYST_LLM_BACKEND",
        provider_legacy_map=(("ollama", "ollama"), ("openai", "openai")),
        provider_model_envs=(("openai", "ANALYST_MODEL"), ("ollama", "OLLAMA_ANALYST_MODEL")),
        timeout_env="ANALYST_OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
    ),
    "checker": RoleSpec(
        "checker",
        "openai",
        "CHECKER_MODEL",
        _default_checker_openai_model(),
        provider_env="CHECKER_PROVIDER",
        provider_model_envs=(("openai", "CHECKER_MODEL"), ("ollama", "OLLAMA_CHECKER_MODEL")),
        timeout_env="CHECKER_OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
        format="json",
    ),
    "critic": RoleSpec(
        "critic",
        "openai",
        "CRITIC_MODEL",
        _default_critic_openai_model(),
        provider_env="CRITIC_PROVIDER",
        provider_model_envs=(("openai", "CRITIC_MODEL"), ("ollama", "OLLAMA_CRITIC_MODEL")),
        timeout_env="CRITIC_OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
        format="json",
    ),
    "finalizer": RoleSpec(
        "finalizer",
        "openai",
        "FINALIZER_MODEL",
        _default_finalizer_openai_model(),
        provider_env="FINALIZER_PROVIDER",
        provider_model_envs=(("openai", "FINALIZER_MODEL"), ("ollama", "OLLAMA_FINALIZER_MODEL")),
        timeout_env="FINALIZER_OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
    ),
    "news_sentiment": RoleSpec(
        "news_sentiment",
        "openai",
        "OPENAI_INGESTION_MODEL",
        _default_ingestion_openai_model(),
        provider_env="INGESTION_LLM_PROVIDER",
        provider_model_envs=(("openai", "OPENAI_INGESTION_MODEL"), ("ollama", "OLLAMA_INGESTION_MODEL")),
        timeout_env="OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
    ),
    "sec_parser": RoleSpec(
        "sec_parser",
        "openai",
        "OPENAI_INGESTION_MODEL",
        _default_ingestion_openai_model(),
        provider_env="INGESTION_LLM_PROVIDER",
        provider_model_envs=(("openai", "OPENAI_INGESTION_MODEL"), ("ollama", "OLLAMA_INGESTION_MODEL")),
        timeout_env="OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
        format="json",
    ),

    # OpenAI-compatible transform roles
    "query_extract": RoleSpec(
        "query_extract",
        "openai",
        "TRANSFORM_EXTRACTOR_MODEL",
        "gpt-4o-mini",
        timeout_env="OPENAI_TIMEOUT_SECONDS",
        temperature=0.0,
    ),
    "query_hyde": RoleSpec(
        "query_hyde",
        "openai",
        "TRANSFORM_HYDE_MODEL",
        "gpt-4o-mini",
        timeout_env="OPENAI_TIMEOUT_SECONDS",
        temperature=0.1,
    ),
}


# Prioritise agent graph roles first, then transform helpers when explicitly asked.
DEFAULT_WARMUP_ROLES: tuple[str, ...] = ("router", "analyst", "checker", "critic", "finalizer")


_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _normalized_provider(raw: str) -> Optional[str]:
    val = (raw or "").strip().lower()
    return val if val in {"openai", "ollama"} else None


def resolve_provider(spec: RoleSpec) -> str:
    provider = None
    if spec.provider_env:
        provider = _normalized_provider(os.getenv(spec.provider_env, ""))
    if provider is None and spec.provider_legacy_env:
        raw_legacy = (os.getenv(spec.provider_legacy_env, "") or "").strip().lower()
        for old_value, mapped_provider in spec.provider_legacy_map:
            if raw_legacy == old_value:
                provider = mapped_provider
                break
    return provider or spec.default_provider


def _model_env_for_provider(spec: RoleSpec, provider: str) -> str:
    for candidate_provider, env_name in spec.provider_model_envs:
        if candidate_provider == provider:
            return env_name
    return spec.model_env


def resolve_model(spec: RoleSpec) -> str:
    provider = resolve_provider(spec)
    env_name = _model_env_for_provider(spec, provider)
    model = (os.getenv(env_name, "") or "").strip()
    if model:
        return model
    fallback = (os.getenv(spec.model_env, "") or "").strip()
    if fallback:
        return fallback
    if provider == "ollama":
        if spec.role in {"analyst", "critic", "finalizer"}:
            return _default_expert_ollama_model()
        if spec.role == "checker":
            return _default_checker_ollama_model()
        if spec.role in {"news_sentiment", "sec_parser"}:
            return _default_ingestion_ollama_model()
        if spec.role == "router":
            return _default_router_ollama_model()
    return spec.default_model


def _resolved_model(spec: RoleSpec) -> str:
    return resolve_model(spec)


def role_runtime_config(role: str) -> Dict[str, Any]:
    spec = get_role_spec(role)
    provider = resolve_provider(spec)
    model_env = _model_env_for_provider(spec, provider)
    return {
        "role": spec.role,
        "provider": provider,
        "model": resolve_model(spec),
        "model_env": model_env,
        "provider_env": spec.provider_env or "",
        "timeout_env": spec.timeout_env or "",
    }


def runtime_matrix(roles: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    selected = list(roles or ROLE_TABLE.keys())
    return [role_runtime_config(role) for role in selected]


def get_role_spec(role: str) -> RoleSpec:
    key = role.lower()
    if key not in ROLE_TABLE:
        raise KeyError(f"Unknown LLM role {role!r}. Known: {sorted(ROLE_TABLE)}")
    return ROLE_TABLE[key]


def _cache_key(spec: RoleSpec) -> str:
    provider = resolve_provider(spec)
    model = resolve_model(spec)
    return f"{spec.role}:{provider}:{model}"


def get_client(role: str, **overrides: Any):
    """Provider-agnostic client getter."""
    spec = get_role_spec(role)
    if overrides:
        return _build_client(spec, **overrides)

    cache_key = _cache_key(spec)
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
    provider = resolve_provider(spec)
    if provider != "ollama":
        raise ValueError(
            f"Role {role!r} is provider={provider!r}, not ollama. "
            "Use get_client(role, ...) instead."
        )
    return get_client(role, **overrides)


def _build_client(spec: RoleSpec, **overrides: Any):
    provider = resolve_provider(spec)
    if provider == "ollama":
        return _build_ollama_client(spec, **overrides)
    if provider == "openai":
        return _build_openai_client(spec, **overrides)
    raise ValueError(f"Unsupported provider={provider!r} for role={spec.role!r}")


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
        "timeout": float(os.getenv(spec.timeout_env or "OPENAI_TIMEOUT_SECONDS", "60")),
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
    provider: str
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
            results.append(WarmupResult(role=role, provider="?", model="?", status="skipped", error="unknown role"))
            continue

        started = time.monotonic()
        provider = resolve_provider(spec)
        model = _resolved_model(spec)
        try:
            client = get_client(role)
            _ = client.invoke(prompt)
            results.append(WarmupResult(role=role, provider=provider, model=model, status="ok", latency_s=time.monotonic() - started))
        except Exception as exc:  # noqa: BLE001
            results.append(
                WarmupResult(
                    role=role,
                    provider=provider,
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
    "resolve_model",
    "resolve_provider",
    "role_runtime_config",
    "runtime_matrix",
    "warmup_async",
    "warmup_sync",
]

