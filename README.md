# AutoOptions Recommendation Chatbot

## 1. Financial Logic and Problem Framing

### 1.1 The Information Gap
Macro signals that move precious-metals markets are publicly visible — Fed rates, geopolitical risk indexes, dollar dynamics, CPI prints. But for most investors, the path from 'rates are rising' to defining the appropriate volatility posture and option strategy structure remains opaque.

The raw information exists. The **translation layer** does not.

AutoOptions is built to close that gap: a multi-agent financial RAG platform that converts fragmented, heterogeneous market evidence into clear, traceable, and actionable options research.

---

### 1.2 The Cross-Asset Financial Thesis
Options mechanics are universal — strike, expiry, IV, Greeks, and liquidity apply equally to commodity ETFs and equities. But the **signal sources that drive those mechanics differ fundamentally by asset class**:

| Asset Class | Core Signal Drivers | Primary Data Sources |
|---|---|---|
| **Commodity ETFs** (`GLD`, `SLV`) | Fed policy, real rates, CPI, dollar index, GPR, risk-off flow | FRED macro series, news narratives, GPR index |
| **Single-name equities** | Insider behavior, material corporate events, earnings catalysts | SEC Form 4 (transactions), Form 8-K (disclosures), options chain |

This divergence means a single retrieval path or a single evidence type cannot cover both. **The cross-asset thesis is the reason this system requires separate signal tracks, not a design choice made in isolation.**

Signal pathway: `macro and event signals → volatility regime view → options strategy structure`

---

### 1.3 Why Existing Tools Break Here
Three structural problems that neither static dashboards nor generic RAG systems solve:

| Failure Mode | Root Cause |
|---|---|
| **Modality fragmentation** | A complete signal requires macro numbers, narrative sentiment, SEC regulatory text, and live options chains — across different formats and update cadences. No single tool ingests all four. |
| **Semantic-vs-numeric conflict** | Vector retrieval handles context well but approximates badly on precise numerics. Routing IV, Greeks, and rates through the same embedding space as news narratives degrades both. |
| **Static signal weights** | Commodity ETF analysis is macro-regime driven; single-name equity analysis is event-driven. A system that cannot dynamically shift signal emphasis based on query intent and asset type will misroute evidence. |

---

### 1.4 Financial Constraints Drive System Design
Every major architectural decision in AutoOptions is a direct consequence of the financial thesis above — not a generic engineering preference:

| Financial Constraint | System Design Response |
|---|---|
| ETF and equity paths need different signal weighting | Intent-classified retrieval routing (`sql_only` / `vector_only` / `hybrid_both`) per query |
| Options chains go stale intraday; macro series update monthly; SEC filings are event-triggered | Source-aware `TimePredicate` windows compiled per data type — not a single global lookback window |
| IV, rates, and liquidity must be exact, not approximated | Numeric data exclusively via DuckDB Silver SQL over Parquet — never embedded into vectors |
| Factual accuracy, numeric consistency, risk logic, and schema compliance are separable quality concerns | Separable multi-agent roles: `Analyst → Checker → Critic → Finalizer` — one concern per node |

---
## 2. Architecture Blueprint

![End-to-end architecture](images/AutoOption_Architecture.svg)

---

## 3. Core Outcomes — System Capabilities at a Glance

> Four engineering pillars that separate AutoOptions from a standard RAG demo.

---

#### Pillar I — Intelligent Multi-Source Data Foundation
*What it is:* a cadence-driven ingestion DAG that turns five heterogeneous source families into a clean, retrieval-ready Medallion stack.

- Integrates **5+ active source families** (FRED, options chains, SEC, GPR, news) across **4 time granularities** (`TRADING_DAILY`, `DAILY`, `WEEKLY`, `MONTHLY`).
- Parses **API JSON + scraped HTML + Form 4 XML + Form 8-K HTML**; Bronze preserves SEC EDGAR accession links for end-to-end lineage trace-back.
- Enforces idempotent upsert and replay-safe run-state anchors to prevent duplicate writes across daily runs.
- Applies **source-specific pre-vectorization strategies** — each data class is enriched differently before hitting Qdrant:
  - **Form 4 (insider transactions):** deterministic rule-based scoring — transaction-code polarity (`P`/`S`), 10b5-1 plan discount, C-suite multipliers, and post-holding awareness — to produce auditable, low-variance signal.
  - **Form 8-K (material events):** semantic item chunking by `Item X.XX` boundaries + HTML-to-Markdown normalization before LLM compression; eliminates boilerplate noise and preserves event-section granularity for precise retrieval.
  - **News and GPR narratives:** LLM-distilled summary units at ingestion time — one coherent, causally-complete narrative block per article or monthly signal; avoids the low-precision fragment problem of sentence-level chunking.
  - **Options chains and macro data:** never embedded — queried exclusively via DuckDB Silver SQL contracts for deterministic numeric truth.

→ Reference: [Data Source Summary](./docs/Data_source_docs/Data_source_summary.md) · [SEC Data](./docs/Data_source_docs/SEC_data.md) · [Embedding and Chunking Strategy](./docs/Strategy_choices_docs/Embedding_Chunking_and_Retrieval_Fusion_Strategy.md)

---

#### Pillar II — Hybrid Retrieval and Query Intelligence
*What it is:* a three-route retrieval engine with two-stage query transformation and a multi-channel fusion stack that keeps semantic recall and numeric precision separate.

- **Dual-vector indexing** (dense + sparse) with lineage-preserving payloads and accession-based deterministic IDs prevents duplicate vectors and citation drift.
- **3-route intent classification** (`sql_only`, `vector_only`, `hybrid_both`) with typed metadata extraction + HyDE semantic expansion for each route.
- **Source-aware `TimePredicate` windows** compiled per source type to keep daily/monthly/event datasets on physically valid time bounds.
- **Asymmetric hybrid Gold retrieval** (dense + sparse + RRF fusion + cross-encoder rerank) with strict-to-relaxed fallback tiers for high recall and high precision.
- **DuckDB Silver SQL** as the deterministic numeric system of record; lineage anchors (`MACRO_*`, `LIQ_*`) are cited by the Analyst and verified by the Checker.

→ Reference: [Retrieval Architecture](./docs/Query_retrieval_docs/Retrieval_Architecture_and_Strategy.md) · [Query Intent](./docs/Query_retrieval_docs/Query_intent_docs.md) · [Qdrant Retriever](./docs/Query_retrieval_docs/Qdrant_retriever_docs.md) · [Silver SQL Tools](./docs/Query_retrieval_docs/Silver_SQL_Tools.md)

---

#### Pillar III — Multi-Agent Reasoning and Prompt Governance
*What it is:* a LangGraph state-machine that governs a four-role revision loop, backed by a centralized prompt engineering control plane.

- **LangGraph `Analyst → Checker → Critic → Finalizer` loop** with revision circuit-breakers and node telemetry reduces factual and logic hallucinations while keeping every decision auditable and status-explicit (`success` / `partial_failure`).
- **Prompt Engineering Control Plane** (`Scripts/core/`): domain-tuned role prompts, financial ontology frameworks, structured output contracts, and two-stage `extractor → HyDE` reasoning templates — all decoupled from execution logic.
- **Mixed-provider model deployment:** `options-expert` (Llama3 family, domain-tuned) as primary inference; `gpt-4o-mini` (OpenAI-compatible) as fallback and query-transform path; runtime status guards mitigate time/data drift.
- LangChain surfaces span both retrieval and orchestration: `Document`-style semantic ingestion, `ChatPromptTemplate`-driven agent prompting, and structured-output control in finalization paths.

→ Reference: [Agent Architecture](./docs/agent/Agent_Architecture.md) · [LLM Pool Operations Guide](./docs/modular_guide/LLM_Pool_Operations_Guide.md)

---

#### Pillar IV — Observability and Operational Quality
*What it is:* a dual-surface audit contract that makes every run replayable and every recommendation traceable.

- Append-only backend run logs and frontend query bundles share compatible audit semantics under `logs/`.
- Each recommendation payload carries a full evidence chain: from final strategy back to raw source lineage.
- Time anchors and run IDs enable deterministic state replay without re-ingestion.

→ Reference: [Observability](./docs/modular_guide/Observability.md) · [Time Schema Audit](./docs/Data_source_docs/Time_Schema_Audit.md)

---
## 4. Data Sources, Rationale, and Medallion Lifecycle

### 4.1 Source Registry and Business Value

| Source Domain | Acquisition Layer | Silver/Gold Contract | Core Business Value |
| --- | --- | --- | --- |
| FRED macro series | `fredapi` pipeline | Parquet + narrative context | Real-rate/CPI regime guidance for precious-metal pricing |
| Options market data | `yfinance` scrapers | Silver Parquet (structured) | IV/OI/moneyness/liquidity truth set |
| SEC filings | SEC EDGAR ingestion + processing | Bronze parsed Form 4 XML + Form 8-K HTML; Gold enriched semantic JSONL | Insider flow + material event intelligence with traceable accession-level lineage |
| Geopolitical risk | `GPR_index.py` | Silver metrics + Gold narrative/JSONL | Risk-off lead indicator for metals and volatility |
| Global news (GDELT + full text) | `news_scraper.py` | Bronze raw/full text + Gold enriched JSONL | Event/tone-driven volatility context |


### 4.2 Medallion Processing Contract
1. **Bronze:** raw source-native payloads (JSONL/HTML/XML/CSV).
2. **Silver:** deterministic normalized Parquet tables (numeric system of record).
3. **Gold:** retrieval-ready semantic artifacts (JSONL/Markdown) for vector search.
4. **Agent Context:** prompt convenience snapshots, never replacing Silver truth.

---

## 5. Runtime Workflow and Operating Commands

### Runtime Workflow

![Runtime workflow](images/AutoOption_Workflow.svg)

---



## 6. Qdrant Pipeline and Hybrid Search Strategy

![Ingestion phase workflow](images/Ingestion_Phase_Workflow.svg)

### 6.1 Ingestion and Indexing Principles
- Inject structured metadata before vectorization (`ticker`, `source_type`, `form_type`, timestamps, lineage IDs) to enable deterministic filtering.
- Use schema-enforced upsert and payload indexing for high-selectivity retrieval at query time.
- Run asymmetric hybrid retrieval channels: dense semantic vectors (HyDE paragraph) + sparse lexical vectors (rerank query) with server-side fusion.
- Apply cross-encoder reranking and score gating so only high-signal chunks enter downstream reasoning.
- Preserve idempotency and auditability via accession/url-based lineage and retrieval audit trails.

![Retrieval workflow](images/Retrieval_workflow.svg)

### 6.2 Retrieval Execution Logic
1. Classify route intent (`sql_only`, `vector_only`, `hybrid_both`) and transform query into typed metadata + HyDE payload.
2. Compile per-source `TimePredicate` windows and build smart metadata filters (ticker/source/topic/SEC-specific controls).
3. Execute route-specific retrieval plan with independent Gold/Silver timeout budgets.
4. Run hybrid Gold retrieval (`dense + sparse + RRF + rerank`) under strict-to-relaxed fallback tiers when needed.
5. Run Silver deterministic retrieval with metric allowlist, ticker caps, and lineage anchors.
6. Optionally apply HyDE entity compensation to query additional Silver entities without contaminating primary truth.
7. Assemble one AgentState-ready payload with `time_range`, `hyde_anticipation`, `status`, and latency telemetry.

---

## 7. Multi-Agent Orchestration and Prompting Control

![Multi-agent and frontend workflow](images/Muti-agent_Frontend_workflow.svg)

### 7.1 Agent Responsibilities
- **Analyst:** drafts strategy from merged context with citation discipline.
- **Checker:** verifies factual integrity, numeric consistency, and anchor correctness.
- **Critic:** challenges strategic logic, regime fit, and risk posture.
- **Finalizer:** emits schema-constrained final report for downstream consumers.

### 7.2 Prompt and Governance Layer
- Global context injection from latest macro snapshot.
- Role-specific prompts with hard constraints and structured output contracts.
- Deterministic state fields for revisions, verdict routing, and fallback signaling.

### 7.3 Workflow Governance
- Circuit-breaker on revision loops.
- Append-only audit logs for node-level replay.
- Separation of blocking findings vs non-blocking refinements.

---

## 8. Dependency Surface and Linked Specifications

### 8.1 Prerequisites (Reproducible Baseline)
- Python `3.10+` with `pip`.
- A running Ollama endpoint (default `http://localhost:11434`) with required models pulled.
- A reachable Qdrant instance with API access.
- FRED credential and SEC-compliant user agent.
- Optional OpenAI-compatible credential for fallback and transform roles.

### 8.2 Environment Setup
1. Create and activate a virtual environment.
2. Install dependencies from `requirements.txt`.
3. Copy `.env.sample` to `.env` and populate secrets/endpoints.

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.sample .env
```

Minimum `.env` keys to validate before first run:
- `FRED_API_KEY`
- `SEC_USER_AGENT`
- `QDRANT_HOST`
- `QDRANT_API_KEY`
- `OLLAMA_BASE_URL`
- `OLLAMA_CUSTOM_MODEL_NAME`
- `OLLAMA_ROUTER_MODEL`
- `OPENAI_API_KEY` (if fallback is enabled)

### 8.3 Reproducible First Run

### Core backend checks
```bash
python -m Scripts warmup --roles analyst router checker
python -m Scripts ingest
python Scripts/vector_store/ingestion.py --no-full-refresh
python -m Scripts query "Past week FOMC and 10Y yields impact on SPY puts?"
python Scripts/tests/test_router_e2e.py
```

### Frontend checks
```bash
streamlit run Frontend/app.py
```

### 8.4 Validation and Test Protocol

Validation checklist:
- Query renders progressive sections before final report completion.
- Backend run-scoped logs appear under `logs/runs/<today>/<run_id>/`.
- Frontend bundle files appear under `logs/frontend_query/<today>/`.
- `query_audit_trail.jsonl` includes resolvable paths to trace/final_state/summary artifacts.

## 9. Module Topology and Documentation Index

| Module Domain | High-Level Responsibility | Primary Code Surface | Key Operational Outputs | Detailed Documentation |
| --- | --- | --- | --- | --- |
| Agent graph runtime | Executes deterministic multi-agent control loop (`retrieval_master -> analyst -> checker -> critic -> finalizer`) with revision circuit-breakers and node telemetry | `Scripts/agents/` | `final_strategy`, `node_audit_log`, revision-aware state transitions | [`docs/agent/Agent_Architecture.md`](./docs/agent/Agent_Architecture.md) |
| Retrieval and query transform | Performs intent routing, metadata extraction, HyDE anticipation, Gold/Silver hybrid retrieval, and time-window predicate compilation | `Scripts/retrieval/` | retrieval payloads (`metadata`, `gold_context`, `silver_context`, `time_range`, `hyde_anticipation`) | [`docs/Query_retrieval_docs/Retrieval_Architecture_and_Strategy.md`](./docs/Query_retrieval_docs/Retrieval_Architecture_and_Strategy.md) |
| Data ingestion and processing | Runs cadence-driven ingestion DAG from external sources to Bronze/Silver/Gold datasets | `Scripts/data_collection/`, `Scripts/orchestration/` | medallion-layer datasets under `Data/1_Bronze_Raw`, `Data/2_Silver_Processed`, `Data/3_Gold_Semantic` | [`docs/Data_source_docs/Data_source_summary.md`](./docs/Data_source_docs/Data_source_summary.md) |
| Vector store layer | Ingests Gold semantic artifacts and manages retrieval index connectivity | `Scripts/vector_store/` | Qdrant-ingested semantic collections and ingestion audit metadata | [`docs/Vector_store_docs/Vector_Ingestion.md`](./docs/Vector_store_docs/Vector_Ingestion.md) |
| Orchestration CLI plane | Provides production entrypoints for `ingest`, `daemon`, `status`, `query`, and `warmup` with run-state persistence | `Scripts/orchestration/cli.py`, `Scripts/main.py`, `Scripts/__main__.py` | run-scoped state updates, stage execution summaries, operational command surface | [`docs/modular_guide/Orchestration.md`](./docs/modular_guide/Orchestration.md) |
| Frontend execution plane | Streams node-by-node execution progress and renders final institutional strategy dashboard | `Frontend/` | frontend query bundles (`*_trace.jsonl`, `*_summary.json`, `*_final_state.json`) | [`docs/modular_guide/Frontend Runtime Guide.md`](./docs/modular_guide/Frontend_Runtime_Guide.md) |
| Observability and audit | Standardizes backend + frontend telemetry contracts for replayability and compliance traceability | `Scripts/observability/`, `Frontend/audit.py` | `logs/runs/<YYYY-MM-DD>/<run_id>/...` and `logs/frontend_query/<YYYY-MM-DD>/...` artifacts | [`docs/modular_guide/Observability.md`](./docs/modular_guide/Observability.md) |
| LLM control plane | Centralizes role-level model selection, warmup policy, and fallback behavior | `Scripts/core/llm_pool.py`, `Scripts/core/financial_config.py` | warmup health status and role-aligned model runtime behavior | [`docs/modular_guide/LLM Pool Operations Guide.md`](./docs/modular_guide/LLM_Pool_Operations_Guide.md) |
| Testing and guardrails | Validates end-to-end routing correctness, retrieval contract integrity, and safety rules | `Scripts/tests/` | regression evidence for routing, retrieval, and guardrail behavior | [`docs/modular_guide/Documentation Control Plane.md`](./docs/modular_guide/Documentation_Control_Plane.md) |
