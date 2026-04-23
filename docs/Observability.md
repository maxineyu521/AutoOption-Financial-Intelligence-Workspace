# Observability Layer — Unified Logging and Audit Paths

_Scope: `Scripts/observability/` + the layout under `logs/`_

This document specifies the directory layout for every log produced by
the bot, the helpers that route them, and the migration recipe for
legacy loggers.

---

## 1. Problem Statement

Before this layer was introduced the project wrote logs into a zoo of
top-level folders:

```
logs/
├── retrieval/{date}/
├── query_transform/{date}/
├── router_e2e/{date}/
├── Parquet_Query/{date}/
├── scheduler/
├── test/
└── ...
```

Investigating a single production incident meant stitching together
four or five timestamp-matched files by hand. Each module hand-built
its date-partitioned path, which drifted whenever a timezone or daily
boundary shifted.

The remedy: one well-known tree per *orchestrator invocation*, one
helper function that every producer uses.

---

## 2. Target Layout

```
logs/
├── runs/                                       ← canonical, run-scoped
│   └── {YYYY-MM-DD}/
│       └── {run_id}/                           ← one folder per CLI invocation
│           ├── orchestrator.log                # root logger
│           ├── retrieval/
│           │   ├── retrieval.jsonl
│           │   └── retriever_audit_trail.jsonl
│           ├── query_transform/
│           │   └── query_audit_trail.jsonl
│           ├── router_e2e/
│           │   └── ...
│           └── sql_range/
│               └── audit.jsonl
│
├── scheduler/                                  ← legacy daemon, back-compat
│   └── collect_data.log
│
├── retrieval/{YYYY-MM-DD}/…                    ← legacy flat, still readable
├── query_transform/{YYYY-MM-DD}/…
└── router_e2e/{YYYY-MM-DD}/…
```

- `run_id` format: `{YYYYMMDD}_{HHMMSS}_{tag}_{uuid6}`.
  Example: `20260422_153042_query_ab12cd`.
- The legacy flat layout still works — `audit_path(..., scoped_by_run=False)`
  returns a path under `logs/{module}/{date}/` so offline tools that
  consumed the old layout continue to function during the migration.

---

## 3. Public API

```python
from Scripts.observability import (
    configure_root_logger,   # once at CLI / daemon boot
    start_run,               # once per invocation
    current_run_id,          # read-only helper
    audit_path,              # compute a log file path
    get_audit_logger,        # returns a logger pointed at audit_path
)
```

### 3.1 `configure_root_logger(level="INFO", also_to_file=True)`

- Installs a stdout handler and (optionally) a file handler at
  `logs/runs/{today}/{run_id}/orchestrator.log`.
- Idempotent: replaces previously attached handlers so pytest /
  Jupyter do not double-print.
- Called by `Scripts.orchestration.cli.main()` on every invocation.

### 3.2 `start_run(tag="session")` + `current_run_id()`

- `start_run` mints a new `run_id` and stores it in a `ContextVar`.
- `current_run_id` returns the active id, auto-starting an "adhoc"
  run if the caller forgot to initialise one (useful in notebooks).

### 3.3 `audit_path(module, anchor=None, filename=None, scoped_by_run=True)`

| Argument | Purpose |
| --- | --- |
| `module` | Logical module name — `"retrieval" / "query_transform" / "router_e2e" / "sql_range"`. Becomes both a folder name *and* the default filename stem. |
| `anchor` | The **data** date this log pertains to (default `date.today()`). Use the agent's `time_range.anchor_date` for determinism — this keeps backfill audits in the correct folder. |
| `filename` | Override the default `<module>.jsonl` leaf. Useful for existing consumers that already expect names like `retriever_audit_trail.jsonl`. |
| `scoped_by_run` | `True` ⇒ `logs/runs/{date}/{run_id}/{module}/<filename>`; `False` ⇒ `logs/{module}/{date}/<filename>`. |

Side-effect: the parent folder is created.

### 3.4 `get_audit_logger(name, module, anchor=None, level="INFO")`

Returns a `logging.Logger` with a single file handler attached at
`audit_path(module, anchor, filename=f"{name}.log")`. Safe to call
repeatedly — duplicate handlers are de-duplicated.

```python
from Scripts.observability import get_audit_logger

log = get_audit_logger("QdrantRetriever", module="retrieval")
log.info("search filter=%s", filt)
```

---

## 4. Migration Recipe for Legacy Loggers

Most existing modules currently hand-build log paths:

```python
log_path = Path("logs/retrieval") / date.today().isoformat() / "retriever_audit_trail.jsonl"
log_path.parent.mkdir(parents=True, exist_ok=True)
```

Replace with a single call:

```python
from Scripts.observability import audit_path

log_path = audit_path(
    module="retrieval",
    anchor=state["time_range"]["anchor_date"],    # deterministic anchor
    filename="retriever_audit_trail.jsonl",
)
```

Migration can happen one module at a time. Both layouts coexist until
you flip `scoped_by_run` defaults for that consumer.

---

## 5. Scheduler / Daemon Logs

`Scripts/data_collection/collect_data.py::configure_logging` still
writes to `logs/scheduler/collect_data.log`. When you switch to the
new CLI daemon (`python -m Scripts daemon`), logs flow through
`configure_root_logger` into `logs/runs/{today}/{run_id}/orchestrator.log`
instead. Both paths are supported indefinitely.

---

## 6. Retention Guidance

- `logs/runs/` grows one folder per invocation. On a machine that runs
  the daemon continuously, retention is measured in daemon restarts —
  for each daemon lifetime there is one `orchestrator.log`.
- Keep at least the last 7 calendar days of `logs/runs/{date}` for
  post-mortem analysis.
- Archive older `runs/` to cold storage via a nightly cron — no module
  depends on logs older than the current day.
