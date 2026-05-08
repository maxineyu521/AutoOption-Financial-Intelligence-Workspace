"""
CLI entrypoint for the bot — one command, four subcommands.

    python -m Scripts ingest   [--only ...] [--force] [--dry-run] [--with-qdrant]
    python -m Scripts daemon   [--poll-seconds 30]
    python -m Scripts status
    python -m Scripts query    "your question here"

``ingest`` / ``daemon`` / ``status`` touch only the orchestration layer
unless ``ingest --with-qdrant`` is used, in which case the vector store
ingestion layer is invoked after successful collection.
``query`` lazily imports the agent graph so the CLI stays fast for
non-query commands.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from Scripts.observability.audit import configure_root_logger, current_run_id, start_run
from Scripts.orchestration.pipeline import Pipeline, build_default_pipeline
from Scripts.orchestration.run_state import RunState, get_run_state
from Scripts.orchestration.stages import StageResult

log = logging.getLogger("orchestrator.cli")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_ingest(args: argparse.Namespace) -> int:
    pipeline = build_default_pipeline()
    results = pipeline.run_once(
        only=args.only,
        force=args.force,
        dry_run=args.dry_run,
    )

    qdrant_metrics = None
    collect_ok = all(r.ok for r in results)
    should_run_qdrant = bool(args.with_qdrant and not args.dry_run)
    if should_run_qdrant:
        if collect_ok or args.qdrant_on_partial:
            qdrant_metrics = _run_qdrant_ingestion(args)
        else:
            qdrant_metrics = {
                "status": "skipped",
                "reason": "collection_pipeline_failed",
            }

    ok = collect_ok and (
        not should_run_qdrant or (qdrant_metrics or {}).get("status") == "ok"
    )
    _print_run_summary(results)
    if qdrant_metrics is not None:
        _print_qdrant_summary(qdrant_metrics)
    report_paths = _write_ingest_report(
        results,
        qdrant_metrics=qdrant_metrics,
        ok=ok,
        report_dir=args.report_dir,
    )
    if report_paths:
        print()
        print(f"report_json: {report_paths['json']}")
        print(f"report_md:   {report_paths['markdown']}")
        print(f"dashboard:   {report_paths['html']}")
    return 0 if ok else 1


def _run_qdrant_ingestion(args: argparse.Namespace) -> dict:
    try:
        from Scripts.vector_store.ingestion import QdrantHybridIngestor
    except ImportError as exc:
        return {
            "status": "failed",
            "error": f"qdrant ingestion import failed: {exc}",
        }

    try:
        ingestor = QdrantHybridIngestor(collection_name=args.qdrant_collection)
        if args.qdrant_indexes_only:
            ingestor.init_collection_with_indexes()
            return {
                "status": "ok",
                "collection": args.qdrant_collection,
                "indexes_only": True,
                "total_upserted": 0,
            }
        source_types = set(args.qdrant_source or []) or None
        return ingestor.run_pipeline(
            full_refresh=args.qdrant_full_refresh,
            source_types=source_types,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should report, not crash unformatted.
        log.exception("qdrant ingestion failed")
        return {
            "status": "failed",
            "collection": args.qdrant_collection,
            "error": str(exc),
        }


def _cmd_daemon(args: argparse.Namespace) -> int:
    if args.warmup:
        _run_warmup(args.warmup_roles, synchronous=False)
    pipeline = build_default_pipeline()
    pipeline.run_forever(poll_seconds=args.poll_seconds)
    return 0  # unreachable under normal operation


def _cmd_status(args: argparse.Namespace) -> int:
    rs = get_run_state()
    snap = rs.snapshot()
    try:
        snap["pipeline_manifest"] = build_default_pipeline().describe()
    except Exception as exc:  # noqa: BLE001
        log.warning("status: could not build pipeline manifest: %s", exc)
    model_matrix = _load_llm_runtime_matrix()
    if model_matrix is not None:
        snap["llm_runtime"] = model_matrix
    if args.json:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
    else:
        _print_status_summary(snap)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    import asyncio

    question = " ".join(args.question).strip()
    if not question:
        print("query: empty question", file=sys.stderr)
        return 2

    # Warm the active runtime providers before spinning up the agent graph so the
    # first user query does not pay the cold-start penalty.
    if args.warmup:
        _run_warmup(args.warmup_roles, synchronous=True)

    # Lazy import — heavy LangChain / Ollama clients are only materialised
    # when the user actually asks a question. The router module exports
    # ``build_financial_rag_graph``; ``build_graph`` is kept as a
    # forward-compatible alias for parity with generic LangGraph tutorials.
    try:
        from Scripts.agents.router import build_financial_rag_graph
    except ImportError as exc:
        print(
            f"query: agent graph is not importable — {exc}\n"
            "If you only want to run the data pipeline, use `ingest` / `daemon` "
            "/ `status` which do not require the agent layer.",
            file=sys.stderr,
        )
        return 2

    graph = build_financial_rag_graph()
    log.info("query: %s", question)

    # The graph contains async nodes (master_retrieval_node, analyst_node,
    # …) so we must drive it through `ainvoke`. Using the sync `invoke`
    # will raise on the first `await`.
    result = asyncio.run(graph.ainvoke({"original_query": question}))

    # The finalizer writes its Pydantic dict to ``final_strategy`` (see
    # ``finalizer_node`` in Scripts/agents/router.py); ``final_report``
    # was a draft-era key and no longer exists.
    final_strategy = result.get("final_strategy") or result.get("draft_report")
    if final_strategy is None:
        print("<no final_strategy produced>", file=sys.stderr)
        return 1

    if isinstance(final_strategy, (dict, list)):
        print(json.dumps(final_strategy, indent=2, ensure_ascii=False))
    else:
        print(final_strategy)
    return 0


def _cmd_warmup(args: argparse.Namespace) -> int:
    try:
        from Scripts.core import llm_pool
    except ImportError as exc:
        print(f"warmup: llm_pool unavailable — {exc}", file=sys.stderr)
        return 2

    results = llm_pool.warmup_sync(roles=args.roles or None)
    width = max((len(r.role) for r in results), default=8)
    print(f"{'role':<{width}}  provider  status    latency  model")
    print("-" * (width + 52))
    for r in results:
        print(f"{r.role:<{width}}  {r.provider:<8}  {r.status:<8}  {r.latency_s:>6.2f}s  {r.model}")
        if r.error:
            print(f"  ! {r.error}")
    return 0 if all(r.status == "ok" for r in results) else 1


def _run_warmup(roles, *, synchronous: bool) -> None:
    """Shared warmup entry — imports llm_pool lazily so CLI subcommands
    that do not need an LLM never pay the import cost."""
    try:
        from Scripts.core import llm_pool
    except ImportError as exc:
        log.warning("warmup skipped — llm_pool import failed: %s", exc)
        return

    if synchronous:
        results = llm_pool.warmup_sync(roles=roles or None)
        for r in results:
            log.info(
                "warmup role=%s provider=%s model=%s status=%s latency=%.2fs %s",
                r.role, r.provider, r.model, r.status, r.latency_s,
                f"error={r.error}" if r.error else "",
            )
    else:
        llm_pool.warmup_async(roles=roles or None)
        log.info("warmup dispatched asynchronously in background")


def _load_llm_runtime_matrix() -> Optional[List[dict]]:
    try:
        from Scripts.core import llm_pool
    except ImportError:
        return None
    try:
        return llm_pool.runtime_matrix()
    except Exception as exc:  # noqa: BLE001
        log.warning("status: could not load llm runtime matrix: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Summary printers
# ---------------------------------------------------------------------------


def _print_run_summary(results) -> None:
    width = max((len(r.name) for r in results), default=8)
    header = f"{'stage':<{width}}  status    run_key           latency"
    print(header)
    print("-" * len(header))
    for r in results:
        rk = r.run_key or "-"
        print(f"{r.name:<{width}}  {r.status:<8}  {rk:<17} {r.latency_s:>6.2f}s")
        if r.error:
            print(f"  ! {r.error}")


def _print_qdrant_summary(metrics: dict) -> None:
    print()
    print("qdrant_ingestion:")
    print(f"  status:       {metrics.get('status', '-')}")
    print(f"  collection:   {metrics.get('collection', '-')}")
    print(f"  upserted:     {metrics.get('total_upserted', 0)}")
    if metrics.get("source_counts"):
        for source, count in sorted(metrics["source_counts"].items()):
            print(f"  {source:<10} {count}")
    if metrics.get("error"):
        print(f"  ! {metrics['error']}")


def _write_ingest_report(
    results: List[StageResult],
    *,
    qdrant_metrics: Optional[dict],
    ok: bool,
    report_dir: Optional[str],
) -> dict:
    base_dir = (
        Path(report_dir)
        if report_dir
        else Path("logs") / "runs" / date.today().isoformat() / current_run_id()
    )
    base_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "run_id": current_run_id(),
        "created_at": datetime.now().isoformat(),
        "status": "ok" if ok else "failed",
        "collection_stages": [
            {
                "name": r.name,
                "status": r.status,
                "run_key": r.run_key,
                "latency_s": round(r.latency_s, 3),
                "error": r.error,
                "metrics": r.metrics,
            }
            for r in results
        ],
        "qdrant_ingestion": qdrant_metrics,
    }

    json_path = base_dir / "ingest_report.json"
    md_path = base_dir / "ingest_report.md"
    html_path = base_dir / "ingest_dashboard.html"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_ingest_report_markdown(payload), encoding="utf-8")
    html_path.write_text(_render_ingest_report_html(payload), encoding="utf-8")
    return {"json": json_path, "markdown": md_path, "html": html_path}


def _render_ingest_report_markdown(payload: dict) -> str:
    qdrant = payload.get("qdrant_ingestion") or {}
    lines = [
        "# Daily Data Ingest Report",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Created at: `{payload['created_at']}`",
        f"- Status: **{payload['status'].upper()}**",
        "",
        "## Collection Stages",
        "",
        "| Stage | Status | Run key | Latency | Error |",
        "|---|---:|---:|---:|---|",
    ]
    for row in payload["collection_stages"]:
        err = (row.get("error") or "").replace("|", "\\|")
        lines.append(
            f"| `{row['name']}` | {row['status']} | {row.get('run_key') or '-'} | "
            f"{row.get('latency_s', 0):.2f}s | {err or '-'} |"
        )

    lines.extend([
        "",
        "## Qdrant Ingestion",
        "",
        f"- Status: **{qdrant.get('status', 'not_requested')}**",
        f"- Collection: `{qdrant.get('collection', '-')}`",
        f"- Total upserted: `{qdrant.get('total_upserted', 0)}`",
    ])
    if qdrant.get("target_states"):
        lines.append(f"- Target states: `{json.dumps(qdrant['target_states'], sort_keys=True)}`")
    if qdrant.get("source_counts"):
        lines.extend(["", "| Source | Upserted |", "|---|---:|"])
        for source, count in sorted(qdrant["source_counts"].items()):
            lines.append(f"| {source} | {count} |")
    if qdrant.get("error"):
        lines.extend(["", "### Error", "", f"```text\n{qdrant['error']}\n```"])
    return "\n".join(lines) + "\n"


def _render_ingest_report_html(payload: dict) -> str:
    qdrant = payload.get("qdrant_ingestion") or {}
    status = payload["status"].upper()
    status_class = "ok" if payload["status"] == "ok" else "failed"
    stage_rows = []
    for row in payload["collection_stages"]:
        stage_rows.append(
            "<tr>"
            f"<td>{html.escape(row['name'])}</td>"
            f"<td><span class='pill {html.escape(row['status'])}'>{html.escape(row['status'])}</span></td>"
            f"<td>{html.escape(str(row.get('run_key') or '-'))}</td>"
            f"<td>{float(row.get('latency_s') or 0):.2f}s</td>"
            f"<td>{html.escape(row.get('error') or '-')}</td>"
            "</tr>"
        )

    source_rows = []
    for source, count in sorted((qdrant.get("source_counts") or {}).items()):
        source_rows.append(
            f"<tr><td>{html.escape(source)}</td><td>{int(count)}</td></tr>"
        )
    if not source_rows:
        source_rows.append("<tr><td colspan='2'>No Qdrant source counts for this run.</td></tr>")

    target_states = html.escape(json.dumps(qdrant.get("target_states") or {}, sort_keys=True))
    error_block = ""
    if qdrant.get("error"):
        error_block = f"<section><h2>Error</h2><pre>{html.escape(qdrant['error'])}</pre></section>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily Data Ingest Dashboard</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #667085;
      --line: #d9e0ea;
      --ok: #087443;
      --failed: #b42318;
      --skipped: #475467;
      --accent: #2157c6;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    header {{
      margin-bottom: 24px;
    }}
    h1, h2 {{
      margin: 0;
      letter-spacing: 0;
    }}
    h1 {{
      font-size: 28px;
      font-weight: 750;
    }}
    h2 {{
      font-size: 18px;
      margin-bottom: 14px;
    }}
    .subtle {{
      color: var(--muted);
      margin-top: 6px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .metric, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    .metric {{
      padding: 16px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .value {{
      font-size: 22px;
      font-weight: 760;
      margin-top: 6px;
      overflow-wrap: anywhere;
    }}
    section {{
      padding: 18px;
      margin-top: 16px;
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 750;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 0 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 750;
      background: #eef2f6;
      color: var(--skipped);
    }}
    .pill.ok, .metric .ok {{
      background: #ecfdf3;
      color: var(--ok);
    }}
    .pill.failed, .metric .failed {{
      background: #fef3f2;
      color: var(--failed);
    }}
    .pill.skipped {{
      background: #f2f4f7;
      color: var(--skipped);
    }}
    code, pre {{
      background: #101828;
      color: #f2f4f7;
      border-radius: 6px;
    }}
    code {{
      padding: 2px 5px;
    }}
    pre {{
      padding: 12px;
      overflow-x: auto;
    }}
    @media (max-width: 820px) {{
      .grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
    }}
    @media (max-width: 560px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
      main {{
        padding: 22px 12px 36px;
      }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Daily Data Ingest Dashboard</h1>
    <div class="subtle">Run <code>{html.escape(payload['run_id'])}</code> created at {html.escape(payload['created_at'])}</div>
  </header>

  <div class="grid">
    <div class="metric"><div class="label">Run Status</div><div class="value {status_class}">{status}</div></div>
    <div class="metric"><div class="label">Collection Stages</div><div class="value">{len(payload['collection_stages'])}</div></div>
    <div class="metric"><div class="label">Qdrant Status</div><div class="value">{html.escape(qdrant.get('status', 'not_requested'))}</div></div>
    <div class="metric"><div class="label">Vectors Upserted</div><div class="value">{int(qdrant.get('total_upserted') or 0)}</div></div>
  </div>

  <section>
    <h2>Collection Stages</h2>
    <table>
      <thead><tr><th>Stage</th><th>Status</th><th>Run key</th><th>Latency</th><th>Error</th></tr></thead>
      <tbody>{''.join(stage_rows)}</tbody>
    </table>
  </section>

  <section>
    <h2>Qdrant Ingestion</h2>
    <p class="subtle">Collection: <code>{html.escape(qdrant.get('collection', '-'))}</code></p>
    <p class="subtle">Target states: <code>{target_states}</code></p>
    <table>
      <thead><tr><th>Source</th><th>Upserted</th></tr></thead>
      <tbody>{''.join(source_rows)}</tbody>
    </table>
  </section>
  {error_block}
</main>
</body>
</html>
"""


def _print_status_summary(snap: dict) -> None:
    print(f"state_path: {snap['state_path']}")
    print(f"updated_at: {snap.get('updated_at') or '(never)'}")
    print()
    print("last_run_keys:")
    for k, v in sorted(snap["last_run_keys"].items()):
        print(f"  {k:<28} {v}")
    print()
    print("anchors (agent-side time anchors):")
    for ds, d in snap["anchors"].items():
        print(f"  {ds:<10} {d}")
    anchor_keys = snap.get("dataset_anchor_keys") or {}
    if anchor_keys:
        print()
        print("dataset_anchor_keys:")
        for ds, key in anchor_keys.items():
            print(f"  {ds:<10} {key}")
    llm_runtime = snap.get("llm_runtime") or []
    if llm_runtime:
        print()
        print("llm_runtime:")
        print("  role            provider  model")
        print("  -----------------------------------------------")
        for row in llm_runtime:
            print(
                f"  {str(row.get('role', '')):<14} "
                f"{str(row.get('provider', '')):<8}  "
                f"{str(row.get('model', ''))}"
            )
    pipeline_manifest = snap.get("pipeline_manifest") or []
    if pipeline_manifest:
        print()
        print("pipeline_manifest:")
        print("  stage                      cadence        depends_on")
        print("  --------------------------------------------------------------")
        for row in pipeline_manifest:
            depends = ",".join(row.get("depends_on") or []) or "-"
            print(
                f"  {str(row.get('name', '')):<26} "
                f"{str(row.get('cadence', '')):<13} "
                f"{depends}"
            )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="options-bot",
        description=(
            "Data ingestion + RAG inference orchestrator for the "
            "Automated Options Recommendation Bot."
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Root logger level (default: INFO).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ingest
    p_ing = sub.add_parser(
        "ingest",
        help="Run data pipeline stages that are due (idempotent).",
    )
    p_ing.add_argument(
        "--only",
        nargs="+",
        metavar="STAGE",
        help="Run only these stages (upstream deps are auto-included).",
    )
    p_ing.add_argument(
        "--force",
        action="store_true",
        help="Ignore is_up_to_date — re-run even if today's key already matches.",
    )
    p_ing.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without executing anything.",
    )
    p_ing.add_argument(
        "--with-qdrant",
        action="store_true",
        help="After successful collection, upsert today's Gold JSONL files into Qdrant.",
    )
    p_ing.add_argument(
        "--qdrant-full-refresh",
        action="store_true",
        help="With --with-qdrant, re-embed all matching Gold files instead of only the latest watermarks.",
    )
    p_ing.add_argument(
        "--qdrant-indexes-only",
        action="store_true",
        help="With --with-qdrant, reconcile collection payload indexes without upserting vectors.",
    )
    p_ing.add_argument(
        "--qdrant-collection",
        default="financial_rag_gold",
        help="Qdrant collection name for --with-qdrant (default: financial_rag_gold).",
    )
    p_ing.add_argument(
        "--qdrant-source",
        nargs="+",
        choices=["news", "sec", "gpr"],
        help="Limit Qdrant ingestion to one or more source types.",
    )
    p_ing.add_argument(
        "--qdrant-on-partial",
        action="store_true",
        help="Run Qdrant ingestion even if one collection stage failed.",
    )
    p_ing.add_argument(
        "--report-dir",
        default=None,
        help="Directory for ingest_report.json and ingest_report.md.",
    )
    p_ing.set_defaults(handler=_cmd_ingest)

    # daemon
    p_dae = sub.add_parser(
        "daemon",
        help="Long-running scheduler loop (same as legacy collect_data.py).",
    )
    p_dae.add_argument("--poll-seconds", type=int, default=30)
    p_dae.add_argument(
        "--warmup",
        action="store_true",
        help="Warm the active provider/model pool in a background thread on startup.",
    )
    p_dae.add_argument(
        "--warmup-roles",
        nargs="+",
        metavar="ROLE",
        help="Override the default warmup role list (for example: 'router analyst finalizer').",
    )
    p_dae.set_defaults(handler=_cmd_daemon)

    # status
    p_stat = sub.add_parser(
        "status",
        help="Show last_run_keys and agent time anchors from runtime state.",
    )
    p_stat.add_argument("--json", action="store_true", help="Emit JSON.")
    p_stat.set_defaults(handler=_cmd_status)

    # query
    p_q = sub.add_parser(
        "query",
        help="Ask the multi-agent graph a question (interactive one-shot).",
    )
    p_q.add_argument("question", nargs="+", help="Natural-language question.")
    p_q.add_argument(
        "--warmup",
        action="store_true",
        help="Pre-load the active runtime models before sending the question.",
    )
    p_q.add_argument(
        "--warmup-roles",
        nargs="+",
        metavar="ROLE",
        help="Override the default warmup role list.",
    )
    p_q.set_defaults(handler=_cmd_query)

    # warmup (standalone — useful for CI / cron prehook)
    p_w = sub.add_parser(
        "warmup",
        help="Warm the configured provider/model clients and exit.",
    )
    p_w.add_argument(
        "--roles",
        nargs="+",
        metavar="ROLE",
        help="Specific roles to warm up (default: router + checker).",
    )
    p_w.set_defaults(handler=_cmd_warmup)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(argv)

    configure_root_logger(level=args.log_level)
    start_run(tag=args.cmd)

    log.info("cli: cmd=%s args=%s", args.cmd, vars(args))
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
