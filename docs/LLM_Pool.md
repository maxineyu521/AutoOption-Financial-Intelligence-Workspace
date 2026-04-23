# LLM Pool — Fine-Tuned 70B Warmup, Singleton Cache, Role Tiering

_Scope: `Scripts/core/llm_pool.py` + CLI `warmup` subcommand_

This document specifies the contract the rest of the system uses to
obtain Ollama LLM clients, and the operational runbook for keeping the
fine-tuned 70B model hot in GPU memory across a bot session.

---

## 1. Deployment Reality

The bot talks to Ollama for **two** distinct model roles — nothing
more. This is the single most important fact about the pool:

| Tier | Ollama tag | Base | Used by |
| --- | --- | --- | --- |
| **Expert (70B, fine-tuned)** | `options-expert-v1:latest` | `FROM Llama-3.3-70B-Instruct-Q4_K_M.gguf` | `AnalystAgent`, `CheckerAgent`, `CriticAgent`, `FinalizerAgent`, `QueryTransformer` (metadata extractor AND HyDE writer) |
| **Router (8B, vanilla)**     | `llama3:latest` | stock Meta Llama-3 | `MasterRetriever.router_llm` (intent routing), ingestion-time `news_scraper` sentiment tagger, `sec_processor` form-parser |

> The *base model* (`llama3.3:70b`) referenced in `.env` as
> `OLLAMA_BASE_MODEL` is **only** used by
> `Scripts/models/create_options_expert.py` as the `FROM` source when
> rebuilding the fine-tune — it is never served to agents at runtime.

Retrieval models (`BAAI/bge-base-en-v1.5` dense, `Splade_PP_en_v1`
sparse, `bge-reranker-v2-m3` cross-encoder) are **not** Ollama clients;
they live in `Scripts/vector_store/connection.py` and are documented
in `docs/Query_retrieval_docs/Retrieval_Architecture_and_Strategy.md`.

---

## 2. Why a Pool

Multiple call-sites across the codebase instantiate `ChatOllama`
independently (Analyst, Checker, Critic, Finalizer, two stages of the
`QueryTransformer`, `MasterRetriever.router_llm`). For an 8B model this
is cheap — the real cost lives inside Ollama, not in the Python client.
For the 70B fine-tune three things break:

| Failure mode | Consequence without the pool |
| --- | --- |
| **Cold start** | First user query waits 30–60 s while Ollama loads the Q4-quantised 70B into GPU memory. |
| **Idle eviction** | Ollama's default `keep_alive` is 5 min. A three-minute read pause between queries incurs a second cold start. |
| **Model swapping** | If the 70B fine-tune and a secondary model are both served on a single GPU, Ollama may evict the 70B to serve an 8B router call, then reload the 70B on the next Analyst call — double cold-start per revision. |

The pool solves all three by (a) centralising client construction,
(b) forcing `keep_alive` to `30m` by default, and (c) letting operators
tier models so expert and router roles never fight for the same slot.

---

## 3. Public API

```python
from Scripts.core import llm_pool

# Get a cached client for a role (case-insensitive)
llm = llm_pool.get_ollama("analyst")

# One-shot override (not cached — use sparingly)
ephemeral = llm_pool.get_ollama("analyst", temperature=0.7)

# Warm specific roles (blocking, returns per-role WarmupResult)
results = llm_pool.warmup_sync(roles=["analyst", "router"])

# Fire-and-forget background warmup — good for daemon startup
thread = llm_pool.warmup_async()
```

### 3.1 Role Table

```python
# Scripts/core/llm_pool.py
ROLE_TABLE = {
    # Expert tier — all six roles resolve to the same 70B fine-tune,
    # so Ollama holds exactly ONE 70B in GPU memory at a time.
    "analyst":        RoleSpec(... model_env="OLLAMA_CUSTOM_MODEL_NAME", temperature=0.2),
    "critic":         RoleSpec(... model_env="OLLAMA_CUSTOM_MODEL_NAME", temperature=0.3),
    "checker":        RoleSpec(... model_env="OLLAMA_CUSTOM_MODEL_NAME", temperature=0.0),
    "finalizer":      RoleSpec(... model_env="OLLAMA_CUSTOM_MODEL_NAME", temperature=0.2),
    "query_extract":  RoleSpec(... model_env="OLLAMA_CUSTOM_MODEL_NAME", temperature=0.0, format="json"),
    "query_hyde":     RoleSpec(... model_env="OLLAMA_CUSTOM_MODEL_NAME", temperature=0.2),

    # Router tier — vanilla Llama-3 8B. Short, cheap, JSON-formatted.
    "router":         RoleSpec(... model_env="OLLAMA_ROUTER_MODEL",      temperature=0.0, format="json"),

    # Ingestion tier — also Llama-3 8B, kept separate so ingestion can
    # be rolled to a different quant without touching online routing.
    "news_sentiment": RoleSpec(... model_env="OLLAMA_INGESTION_MODEL",   temperature=0.0),
    "sec_parser":     RoleSpec(... model_env="OLLAMA_INGESTION_MODEL",   temperature=0.0, format="json"),
}

DEFAULT_WARMUP_ROLES = ("analyst", "router")
```

Why `DEFAULT_WARMUP_ROLES = ("analyst", "router")`?
Warming one role per **tier** is enough — Ollama caches by underlying
model name, not by `ChatOllama` instance. Warming `analyst` pins
`options-expert-v1:latest` for Checker / Critic / Finalizer /
QueryTransformer too; warming `router` pins `llama3:latest` for
ingestion helpers.

### 3.2 Environment Variables

| Variable | Default | Effect |
| --- | --- | --- |
| `OLLAMA_BASE_URL` / `OLLAMA_HOST` | `http://localhost:11434` | Passed to every `ChatOllama`. |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama pins the model after the last request. Use `-1` to never evict. |
| `OLLAMA_CUSTOM_MODEL_NAME` | `options-expert-v1:latest` | Expert-tier model (agents + QueryTransformer). |
| `OLLAMA_ROUTER_MODEL` | `llama3:latest` | Router-tier model (`MasterRetriever.router_llm`). |
| `OLLAMA_INGESTION_MODEL` | falls back to `OLLAMA_ROUTER_MODEL` | Ingestion-time sentiment / SEC parser. |

> `OLLAMA_BASE_MODEL` (`llama3.3:70b`) is **not** read by the pool — it
> is an artefact used only by `create_options_expert.py` when
> rebuilding the fine-tune from the modelfile.

---

## 4. Warmup Strategies

### 4.1 Synchronous warmup before serving a query (recommended default)

```text
$ python -m Scripts query --warmup "yesterday's AAPL IV skew?"
```

The CLI invokes `warmup_sync(DEFAULT_WARMUP_ROLES)` before building
the agent graph, so the first agent call lands on an already-hot
model. Cost: one round-trip per role (≈ 30–60 s for a 70B cold-start,
≈ 50 ms once already loaded).

### 4.2 Background warmup in daemon mode

```text
$ python -m Scripts daemon --warmup
```

The CLI spawns `warmup_async()` in a `daemon=True` thread before
entering the scheduler loop. Ticks run immediately; by the time a
user's first `query` invocation arrives the model is already pinned.

### 4.3 Standalone warmup (CI / cron prehook)

```text
$ python -m Scripts warmup --roles analyst finalizer
role       status    latency  model
----------------------------------------
analyst    ok          42.31s  options-expert-v1:latest
finalizer  ok           0.18s  options-expert-v1:latest   # reuse — already loaded
```

Because both roles map to `OLLAMA_CUSTOM_MODEL_NAME`, warming the
first pins the model for the second.

### 4.4 Failure semantics

`warmup_sync` never raises. A timeout or HTTP error becomes a
`WarmupResult(status="failed", error=...)` entry; the CLI logs it but
proceeds — a flaky warmup must never crash the process that was going
to serve queries anyway.

---

## 5. Agent Migration Pattern

Existing agents construct `ChatOllama` inline:

```python
# Scripts/agents/analyst.py  (current)
from langchain_ollama import ChatOllama

self.llm = ChatOllama(
    model=os.getenv("OLLAMA_CUSTOM_MODEL_NAME", "options-expert-v1"),
    temperature=0.2,
)
```

Target pattern after migration:

```python
from Scripts.core.llm_pool import get_ollama

self.llm = get_ollama("analyst")
```

Agents with `with_structured_output(Schema)` should cache the wrapped
object on `self` once per instance — the pool hands back the raw
client; wrapping is a per-call responsibility.

Migration is **incremental**. The pool is additive — nothing forces
agents to switch in lockstep. Migrate one agent, ship, observe
keep-alive metrics, move to the next.

---

## 6. What the Pool Does *Not* Do

- It does not load a model at Python import time. That would make
  `Scripts.core.llm_pool` unsafe to import in environments without
  Ollama running (CI, docs builds, smoke tests). Warmup is an
  explicit, opt-in operation.
- It does not pool *requests* — every call is still a fresh HTTP
  request to Ollama. What is pooled is the *client object* (session
  configuration, default kwargs) so agents stop re-materialising it.
- It does not coordinate concurrent requests to the same model. Two
  simultaneous Analyst calls still go through Ollama's own request
  queue; the pool's singleton simply makes sure both calls hit the
  same `ChatOllama` instance.

---

## 7. Operational Runbook

| Scenario | Command |
| --- | --- |
| Daemon server reboot — warm before users hit the endpoint | `python -m Scripts warmup --roles analyst router` |
| Ad-hoc backtest at 2 AM — ensure 70B pinned for an hour | `OLLAMA_KEEP_ALIVE=1h python -m Scripts query --warmup "..."` |
| CI smoke test without Ollama | Do *not* call `warmup`; agents that require an LLM will raise a clear `RuntimeError` at first use. |
| Verify which model a role resolves to | `python -c "from Scripts.core import llm_pool; print(llm_pool.get_role_spec('analyst'))"` |
| Rebuild the fine-tune after changing the modelfile | `python Scripts/models/create_options_expert.py` (uses `OLLAMA_BASE_MODEL`) |

---

## 8. Future Work

- **Per-request timeout/circuit-breaker wrapper** — catches Ollama
  hangs without killing the whole agent graph. Candidate home:
  `Scripts/core/llm_pool.safe_invoke(role, prompt, timeout_s=60)`.
- **Parallel warmup on multi-GPU** — when two models fit side-by-side
  in VRAM, `warmup_sync` could fire concurrent requests. Today it is
  serial on purpose (single-GPU machines swap models otherwise).
- **Prometheus exposition** — emit `llm_pool_warmup_latency_seconds`
  and `llm_pool_cache_hits_total` for the scheduler / query paths.
