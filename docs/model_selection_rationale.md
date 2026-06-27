# Model Selection Rationale

## Overview

AutoOptions uses a **role-based LLM pool** (`Scripts/core/llm_pool.py`) that assigns each agent graph node a provider + model pair, resolved dynamically from environment variables at startup. Every role supports dual-provider routing (OpenAI ↔ Ollama) with deterministic fallback.

## Agent Graph — Model Assignments

| Agent Node | Default Provider | Default Model | Ollama Fallback | Env Override | Justification |
|---|---|---|---|---|---|
| **Router** | OpenAI | `gpt-4o-mini` | `llama3:latest` | `ROUTER_MODEL` / `ROUTER_PROVIDER` | Structured JSON output (intent + time window + tickers). Latency-critical — first node in the pipeline. `gpt-4o-mini` balances cost ($0.15/1M input) with reliable JSON mode. Ollama fallback (`llama3`) for offline/air-gapped deployment. |
| **Analyst** | OpenAI | `gpt-5-mini` | `OLLAMA_ANALYST_MODEL` | `ANALYST_MODEL` / `ANALYST_PROVIDER` | Heaviest reasoning node — generates the financial draft from Silver SQL context + Gold retrieval. Requires strong instruction following for evidence-anchored narrative. `gpt-5-mini` chosen for reasoning quality at ~60% cost vs. `gpt-5`. |
| **Checker** | OpenAI | `gpt-4o-mini` | `llama3:latest` | `CHECKER_MODEL` / `CHECKER_PROVIDER` | Deterministic audit — verifies numeric claims against Silver SQL ground truth. JSON-structured output. Low reasoning ceiling needed (comparison, not generation). `gpt-4o-mini` sufficient; cost kept minimal since Checker runs on every pass. |
| **Critic** | OpenAI | `gpt-4o-mini` | `options-expert-v1:latest` | `CRITIC_MODEL` / `CRITIC_PROVIDER` | Contract compliance gate — decides `recommendation_mode` and `structure_visibility_mode`. JSON-structured output. The Ollama fallback uses a fine-tuned `options-expert-v1` model for domain-specific contract reasoning. |
| **Finalizer** | OpenAI | `gpt-5-mini` | `OLLAMA_FINALIZER_MODEL` | `FINALIZER_MODEL` / `FINALIZER_PROVIDER` | Terminal rendering node — converts the fact-checked draft into `FinalReport` Pydantic schema. Must reliably extract structured fields (trade ideas, risks, citations) from long-form text. `gpt-5-mini` for extraction quality. |

## Ingestion & Transform Helpers

| Role | Default Provider | Default Model | Justification |
|---|---|---|---|
| **News Sentiment** | OpenAI | `gpt-4o-mini` | Classifies sentiment polarity for Gold news chunks during ingestion. Low latency, high volume. |
| **SEC Parser** | OpenAI | `gpt-4o-mini` | Extracts structured fields from SEC 10-K/10-Q filings. JSON output mode. |
| **Query Extract** | OpenAI | `gpt-4o-mini` | Extracts tickers, time windows, and intent from user queries for retrieval pipeline. |
| **Query HyDE** | OpenAI | `gpt-4o-mini` | Generates hypothetical document embeddings for dense retrieval. Temperature 0.1 for diversity. |

## Routing & Cascading Design

```
User Query
    │
    ▼
  Router (gpt-4o-mini)  ──── intent + tickers + time window
    │
    ▼
  Analyst (gpt-5-mini)  ──── draft from Silver + Gold context
    │
    ▼
  Checker (gpt-4o-mini) ──── numeric audit vs. Silver SQL
    │
    ▼
  Critic (gpt-4o-mini)  ──── contract compliance gate
    │
    ▼
  Finalizer (gpt-5-mini) ── structured FinalReport
```

**Cascading policy** (from `llm_pool.py`):
1. Resolve provider from `<ROLE>_PROVIDER` env var (e.g., `ROUTER_PROVIDER=ollama`)
2. Resolve model from provider-specific env var (e.g., `OLLAMA_ROUTER_MODEL=llama3`)
3. Fall back to role-specific env var (e.g., `ROUTER_MODEL`)
4. Fall back to hardcoded default

## Cost / Latency / Quality Trade-offs

| Consideration | Decision | Rationale |
|---|---|---|
| **Router + Checker on `gpt-4o-mini`** | Low cost, low latency | These nodes produce short JSON — no reasoning depth needed. ~10× cheaper than `gpt-5-mini`. |
| **Analyst + Finalizer on `gpt-5-mini`** | Higher quality | These nodes generate/parse long-form financial text. `gpt-5-mini` chosen over `gpt-5` for 60% cost reduction with comparable instruction adherence for this domain. |
| **Critic on `gpt-4o-mini` (not `gpt-5-mini`)** | Structured gate only | Critic emits a fixed JSON contract, not free text. `gpt-4o-mini` reliably fills the schema. The Ollama fallback uses a domain fine-tune for offline use. |
| **All temperatures at 0.0** | Deterministic output | Financial compliance requires reproducible answers. Only `query_hyde` uses 0.1 for retrieval diversity. |
| **Ollama fallback for every node** | Air-gapped deployment | Enables fully local operation with `llama3` or custom fine-tunes (`options-expert-v1`). No OpenAI dependency required for production. |

## Source Reference

All model assignments are defined in [`llm_pool.py`](Scripts/core/llm_pool.py), lines 80–175 (`ROLE_TABLE` dictionary).
