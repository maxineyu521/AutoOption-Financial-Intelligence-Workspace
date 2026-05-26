# Docker, Ingestion, and Evaluation Runbook

This runbook documents the current operational workflow for:

- building and restarting the Docker image,
- starting the Streamlit frontend and optional Ollama services,
- running the daily ingestion pipeline,
- and executing the current evaluation stack.

Repository root assumed throughout:

```text
C:\Users\Maxine\Documents\GitHub\Automated-Options-Recommendation-Bot
```

## 1. Requirements

Before using this runbook, make sure the local environment has:

- Docker Desktop with `docker compose`
- Python 3.11 or the project-supported Python runtime
- the Python dependencies from `requirements.txt`
- a populated `.env`
- `.env.docker` present for container-side overrides
- valid credentials for whichever provider path you use:
  - OpenAI if the runtime is OpenAI-first
  - Ollama if the runtime is Ollama-first

Ports used by the local stack:

- `8501` for Streamlit
- `11434` for Ollama

Core config files:

- `.env`
- `.env.docker`
- `docker-compose.yml`
- `Dockerfile`

## 2. Environment Files and Runtime Switching

### 2.1 `.env`

`.env` is the main runtime configuration file for host-side commands such as:

```powershell
python -m Scripts ingest --with-qdrant
python -m Scripts query "Past week SPY put-call ratio and ATM IV?"
python Scripts/evaluation/run_agentic_benchmark.py ...
```

Important variables include:

- `QDRANT_HOST`
- `QDRANT_API_KEY`
- `ROUTER_PROVIDER`
- `ANALYST_PROVIDER`
- `CHECKER_PROVIDER`
- `CRITIC_PROVIDER`
- `FINALIZER_PROVIDER`
- `INGESTION_LLM_PROVIDER`
- `OPENAI_API_KEY`
- `OLLAMA_HOST`
- `OLLAMA_BASE_URL`
- `OLLAMA_OPENAI_BASE_URL`

### 2.2 `.env.docker`

`.env.docker` is used by the Docker services for container-specific overrides.

Current default policy:

- Qdrant stays **cloud-only** and is sourced from `.env`
- `.env.docker` is used mainly for container-only paths and Ollama endpoint overrides
- the Docker app stack no longer starts a local Qdrant service by default

If you switch container-side provider settings or endpoints, recreate the affected services after the change.

### 2.3 After updating environment variables

If you change `.env` or `.env.docker`, use:

```powershell
docker compose up -d --force-recreate frontend
```

If the code or dependencies changed too, rebuild first:

```powershell
docker compose build frontend
docker compose up -d --force-recreate frontend
```

## 3. Docker Image Lifecycle

### 3.1 Build the application image

The compose file builds `autooptions/app:local` from `Dockerfile`.

Recommended build command:

```powershell
docker compose build frontend worker
```

Optional direct build:

```powershell
docker build -t autooptions/app:local .
```

Use the direct `docker build` form when you want to validate the image outside the compose stack.

### 3.2 Restart services without rebuilding

Restart running services:

```powershell
docker compose restart frontend
```

Restart the whole default stack:

```powershell
docker compose restart
```

### 3.3 Recreate services after image or env changes

If you changed application code, dependencies, or environment variables:

```powershell
docker compose build frontend worker
docker compose up -d --force-recreate frontend
```

For a full rebuild plus restart including Ollama profile services:

```powershell
docker compose --profile ollama build frontend worker
docker compose --profile ollama up -d --force-recreate ollama frontend
```

## 4. Start Frontend

### 4.1 Start the Streamlit frontend

```powershell
docker compose up -d frontend
```

Frontend URL:

- [http://localhost:8501](http://localhost:8501)

### 4.2 Check service status

```powershell
docker compose ps
```

### 4.3 Inspect logs

```powershell
docker compose logs --tail=200 frontend
```

### 4.4 Verify cloud Qdrant connectivity

```powershell
Get-Content logs/2026-05-12/connection.log | Select-Object -Last 50
```

Healthy connection lines look like:

```text
INFO - Connecting to Qdrant Cloud [https://532e...] (Attempt 1/3)...
INFO - ✅ Connected to Qdrant Cloud successfully.
```

### 4.5 Stop the default stack

Stop containers and keep volumes:

```powershell
docker compose down
```

Remove volumes too:

```powershell
docker compose down -v
```

Only use `-v` when you intentionally want to wipe Docker-managed caches or Ollama storage.

## 5. Ollama Operation

There are two supported patterns:

1. host-managed Ollama
2. Docker-managed Ollama

### 5.1 Host-managed Ollama

This is the simplest path if Ollama is already installed locally.

Start the Ollama server:

```powershell
ollama serve
```

Pull the model you want:

```powershell
ollama pull llama3:latest
```

If the graph should use Ollama, update `.env` provider fields, for example:

```text
ROUTER_PROVIDER=ollama
ANALYST_PROVIDER=ollama
CHECKER_PROVIDER=ollama
CRITIC_PROVIDER=ollama
FINALIZER_PROVIDER=ollama
INGESTION_LLM_PROVIDER=ollama
```

Then make sure the Ollama endpoint values are local:

```text
OLLAMA_HOST=http://localhost:11434
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_OPENAI_BASE_URL=http://localhost:11434/v1
```

If the Docker frontend should call the host Ollama service, keep `.env.docker` on:

```text
OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_OPENAI_BASE_URL=http://host.docker.internal:11434/v1
```

After updating provider settings, recreate the frontend container:

```powershell
docker compose up -d --force-recreate frontend
```

### 5.2 Docker-managed Ollama

The compose file includes two Ollama profile services:

- `ollama`
- `ollama-pull`

Start the Ollama server container:

```powershell
docker compose --profile ollama up -d ollama
```

Pull the configured model into the container:

```powershell
docker compose --profile ollama run --rm ollama-pull
```

If you want a different pulled model, update:

```text
OLLAMA_PULL_MODEL=<your-model-name>
```

in `.env.docker`, then run the pull command again.

If the frontend container should call Docker-managed Ollama instead of host Ollama, update `.env.docker` to:

```text
OLLAMA_HOST=http://ollama:11434
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_OPENAI_BASE_URL=http://ollama:11434/v1
```

If the graph should also run on Ollama, switch the container-side providers to:

```text
ROUTER_PROVIDER=ollama
ANALYST_PROVIDER=ollama
CHECKER_PROVIDER=ollama
CRITIC_PROVIDER=ollama
FINALIZER_PROVIDER=ollama
INGESTION_LLM_PROVIDER=ollama
```

Then recreate the frontend:

```powershell
docker compose --profile ollama up -d --force-recreate frontend
```

### 5.3 Restarting after Ollama config changes

If only the model changed:

```powershell
docker compose --profile ollama run --rm ollama-pull
docker compose --profile ollama restart ollama
```

If provider or endpoint variables changed:

```powershell
docker compose --profile ollama up -d --force-recreate ollama frontend
```

## 6. Daily Ingestion Workflow

The orchestration entrypoint is:

```powershell
python -m Scripts
```

The main operational subcommands are:

- `ingest`
- `daemon`
- `status`
- `query`
- `warmup`

The default ingest DAG defined in:

- `Scripts/orchestration/pipeline.py`

contains these stages:

- `gpr_monthly`
- `macro_trading_daily`
- `news_daily`
- `options_daily`
- `sec_ingestion_weekly`
- `sec_processor_weekly`

Runtime state is stored in:

- `config/runtime/collect_data_state.json`

and managed through:

- `Scripts/orchestration/run_state.py`

### 6.1 Recommended daily sequence

Check current pipeline state:

```powershell
python -m Scripts status
```

Run the ingest pipeline and push current Gold outputs into Qdrant Cloud:

```powershell
python -m Scripts ingest --with-qdrant
```

This does two things:

1. runs due collection stages according to cadence and `RunState`
2. invokes the vector-store ingestion layer after successful collection

### 6.2 Dry-run before execution

```powershell
python -m Scripts ingest --dry-run
```

### 6.3 Force a rerun

```powershell
python -m Scripts ingest --force --with-qdrant
```

### 6.4 Restrict to specific stages

```powershell
python -m Scripts ingest --only news_daily options_daily --with-qdrant
```

### 6.5 Restrict Qdrant ingestion to specific sources

```powershell
python -m Scripts ingest --with-qdrant --qdrant-source news sec
```

Supported source filters:

- `news`
- `sec`
- `gpr`

### 6.6 Qdrant full refresh

Use this when you want to re-embed all matching Gold files rather than only the latest watermarks:

```powershell
python -m Scripts ingest --with-qdrant --qdrant-full-refresh
```

### 6.7 Index-only reconciliation

Use this when payload indexes changed but you do not want a full upsert:

```powershell
python -m Scripts ingest --with-qdrant --qdrant-indexes-only
```

### 6.8 Run the scheduler daemon

```powershell
python -m Scripts daemon --poll-seconds 30
```

Warm models on daemon startup:

```powershell
python -m Scripts daemon --poll-seconds 30 --warmup
```

### 6.9 Warm models manually

```powershell
python -m Scripts warmup
python -m Scripts warmup --roles analyst router checker
```

### 6.10 Ingest reports

Each ingest run writes reports under:

- `logs/runs/<YYYY-MM-DD>/<run_id>/`

Typical artifacts:

- `ingest_report.json`
- `ingest_report.md`
- `ingest_dashboard.html`

## 7. Evaluation Workflow

The current evaluation path uses:

- `Scripts/evaluation/generate_ground_truth.py`
- `Scripts/evaluation/run_agentic_benchmark.py`
- `Scripts/evaluation/agentic_eval.py`
- `Scripts/evaluation/agentic_node_eval.py`

### 7.1 Step 1: generate query and truth files

General command:

```powershell
python -m Scripts.evaluation.generate_ground_truth --update-tests
```

What it updates:

- `Scripts/tests/router_e2e_ground_truth_queries.json`

### 7.2 Step 2: run a fresh full benchmark

General command:

```powershell
python Scripts/evaluation/run_agentic_benchmark.py --queries-file Scripts/tests/router_e2e_ground_truth_queries.json --judge-model <judge_model>
```

Example:

```powershell
python Scripts/evaluation/run_agentic_benchmark.py --queries-file Scripts/tests/router_e2e_ground_truth_queries.json --judge-model gpt-4o
```

This writes a fresh run under:

- `logs/agentic_eval/<YYYY-MM-DD>/<timestamp>/`

### 7.3 Step 3: reuse a previous run directory

General command:

```powershell
python Scripts/evaluation/run_agentic_benchmark.py --reuse-run-dir logs/agentic_eval/<YYYY-MM-DD>/<timestamp> --skip-llm-judge
```

Example:

```powershell
python Scripts/evaluation/run_agentic_benchmark.py --reuse-run-dir logs/agentic_eval/2026-05-09/20260509_180947 --skip-llm-judge
```

Use this when:

- `baseline_results_full.jsonl` already exists
- `production_results_full.jsonl` already exists
- you want to re-score without rerunning the models

### 7.4 Direct contract-only re-evaluation

General command:

```powershell
python Scripts/evaluation/agentic_eval.py --skip-llm-judge --baseline-results logs/agentic_eval/<YYYY-MM-DD>/<timestamp>/baseline_results_full.jsonl --production-results logs/agentic_eval/<YYYY-MM-DD>/<timestamp>/production_results_full.jsonl --out-dir logs/agentic_eval/<YYYY-MM-DD>/<reeval_folder>
```

Example:

```powershell
python Scripts/evaluation/agentic_eval.py --skip-llm-judge --baseline-results logs/agentic_eval/2026-05-09/20260509_180947/baseline_results_full.jsonl --production-results logs/agentic_eval/2026-05-09/20260509_180947/production_results_full.jsonl --out-dir logs/agentic_eval/2026-05-09/reeval_system
```

### 7.5 Direct node-only re-evaluation

General command:

```powershell
python Scripts/evaluation/agentic_node_eval.py --run-dir logs/agentic_eval/<YYYY-MM-DD>/<timestamp> --out-dir logs/agentic_eval/<YYYY-MM-DD>/<timestamp>/<node_eval_folder>
```

Example:

```powershell
python Scripts/evaluation/agentic_node_eval.py --run-dir logs/agentic_eval/2026-05-09/20260509_180947 --out-dir logs/agentic_eval/2026-05-09/20260509_180947/reeval_node
```

### 7.6 How to replace the placeholders

Use:

- `<YYYY-MM-DD>` = the dated folder already present under `logs/agentic_eval/`
- `<timestamp>` = the run folder inside that date, such as `20260509_180947`
- `<reeval_folder>` = any new folder name you want for contract re-evaluation output, such as `reeval_system`
- `<node_eval_folder>` = any new folder name you want for node re-evaluation output, such as `reeval_node`
- `<judge_model>` = the judge model you want to use, such as `gpt-4o`

### 7.7 Typical output files

Fresh benchmark run directory:

- `queries_snapshot.json`
- `benchmark_orchestrator_audit.jsonl`
- `benchmark_manifest.json`
- `baseline_results_full.jsonl`
- `baseline_answers.jsonl`
- `baseline_summary.json`
- `production_results_full.jsonl`
- `production_answers.jsonl`
- `production_summary.json`
- `production_node_events.jsonl`

Contract evaluation output:

- `agentic_eval_cases.jsonl`
- `agentic_eval_summary.json`
- `agentic_eval_report.md`
- `agentic_eval_dashboard.html`

Node evaluation output:

- `agentic_node_audit.jsonl`
- `agentic_node_events.jsonl`
- `agentic_node_events.csv`
- `agentic_node_eval_detailed.csv`
- `agentic_node_metrics.json`
- `agentic_node_report.md`
- `agentic_node_dashboard.html`

## 8. Frontend Commands

### 8.1 Run Streamlit on the host

```powershell
streamlit run Frontend/app.py
```

### 8.2 Run frontend via Docker

```powershell
docker compose up -d frontend
```

### 8.3 Query the backend directly

```powershell
python -m Scripts query "Past week SPY put-call ratio, ATM IV, and liquid 30-DTE puts?"
```

With model warmup:

```powershell
python -m Scripts query --warmup "Past week SPY put-call ratio, ATM IV, and liquid 30-DTE puts?"
```

## 9. Troubleshooting

### 9.1 `frontend` starts but cannot answer queries

Check:

- `docker compose ps`
- `docker compose logs --tail=200 frontend`
- `Get-Content logs/<YYYY-MM-DD>/connection.log | Select-Object -Last 50`

Common causes:

- `QDRANT_HOST` or `QDRANT_API_KEY` in `.env` are wrong
- the frontend container was not recreated after env changes
- provider settings in `.env.docker` do not match the intended OpenAI or Ollama path

Important note:

- the agent/node logs alone do **not** prove Qdrant connectivity
- Qdrant connection status is reported in `logs/<YYYY-MM-DD>/connection.log`

### 9.2 Qdrant connection errors on host-side Python commands

Host-side and Docker runtime now both use cloud Qdrant from `.env` by default.

Check:

- `QDRANT_HOST`
- `QDRANT_API_KEY`

### 9.3 Ollama model not found

For host Ollama:

```powershell
ollama list
ollama pull llama3:latest
```

For Docker-managed Ollama:

```powershell
docker compose --profile ollama ps
docker compose --profile ollama run --rm ollama-pull
docker compose --profile ollama logs --tail=200 ollama
```

### 9.4 Environment changes do not take effect

Recreate the affected containers:

```powershell
docker compose up -d --force-recreate frontend
```

If the runtime image changed too:

```powershell
docker compose build frontend worker
docker compose up -d --force-recreate frontend
```

### 9.5 `python -m Scripts ingest --with-qdrant` appears to skip work

That is often expected. The ingest pipeline is cadence-aware and idempotent.

Check current state:

```powershell
python -m Scripts status
```

If you intentionally want a rerun:

```powershell
python -m Scripts ingest --force --with-qdrant
```

### 9.6 Reuse evaluation fails immediately

Make sure the source run directory actually contains:

- `baseline_results_full.jsonl`
- `production_results_full.jsonl`

### 9.7 Ground-truth generation or benchmark runner fails

Check:

- `.env` is present and valid
- required model credentials are available
- the repo root is the working directory
- Qdrant is reachable if the benchmark path needs retrieval access

## 10. Command Reference

Build image:

```powershell
docker compose build frontend worker
```

Start frontend:

```powershell
docker compose up -d frontend
```

Start Docker-managed Ollama:

```powershell
docker compose --profile ollama up -d ollama
docker compose --profile ollama run --rm ollama-pull
```

Daily ingest with Qdrant upsert:

```powershell
python -m Scripts ingest --with-qdrant
```

Index-only reconciliation against Qdrant Cloud:

```powershell
python -m Scripts ingest --with-qdrant --qdrant-indexes-only
```

Status:

```powershell
python -m Scripts status
```

Generate truth:

```powershell
python -m Scripts.evaluation.generate_ground_truth --update-tests
```

Fresh benchmark:

```powershell
python Scripts/evaluation/run_agentic_benchmark.py --queries-file Scripts/tests/router_e2e_ground_truth_queries.json --judge-model gpt-4o
```

Reuse benchmark outputs:

```powershell
python Scripts/evaluation/run_agentic_benchmark.py --reuse-run-dir logs/agentic_eval/2026-05-09/20260509_180947 --skip-llm-judge
```
