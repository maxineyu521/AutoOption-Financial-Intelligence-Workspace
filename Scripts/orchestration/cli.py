"""
CLI entrypoint for the bot — one command, four subcommands.

    python -m Scripts ingest   [--only ...] [--force] [--dry-run]
    python -m Scripts daemon   [--poll-seconds 30]
    python -m Scripts status
    python -m Scripts query    "your question here"

``ingest`` / ``daemon`` / ``status`` touch only the orchestration layer
and have no LLM dependencies — safe to run on a CI box without Ollama.
``query`` lazily imports the agent graph so the CLI stays fast for
non-query commands.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import List, Optional

from Scripts.observability.audit import configure_root_logger, start_run
from Scripts.orchestration.pipeline import Pipeline, build_default_pipeline
from Scripts.orchestration.run_state import RunState, get_run_state

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

    ok = all(r.ok for r in results)
    _print_run_summary(results)
    return 0 if ok else 1


def _cmd_daemon(args: argparse.Namespace) -> int:
    if args.warmup:
        _run_warmup(args.warmup_roles, synchronous=False)
    pipeline = build_default_pipeline()
    pipeline.run_forever(poll_seconds=args.poll_seconds)
    return 0  # unreachable under normal operation


def _cmd_status(args: argparse.Namespace) -> int:
    rs = get_run_state()
    snap = rs.snapshot()
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

    # Warm the model pool before spinning up the agent graph so the
    # first user query does not pay the 70B cold-start penalty.
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
    print(f"{'role':<{width}}  status    latency  model")
    print("-" * (width + 40))
    for r in results:
        print(f"{r.role:<{width}}  {r.status:<8}  {r.latency_s:>6.2f}s  {r.model}")
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
                "warmup role=%s model=%s status=%s latency=%.2fs %s",
                r.role, r.model, r.status, r.latency_s,
                f"error={r.error}" if r.error else "",
            )
    else:
        llm_pool.warmup_async(roles=roles or None)
        log.info("warmup dispatched asynchronously in background")


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
        help="Warm the LLM pool in a background thread on startup.",
    )
    p_dae.add_argument(
        "--warmup-roles",
        nargs="+",
        metavar="ROLE",
        help="Override the default warmup role list (e.g. 'analyst finalizer').",
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
        help="Pre-load the heavy LLM into GPU before sending the question.",
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
        help="Pin configured LLM models into Ollama memory and exit.",
    )
    p_w.add_argument(
        "--roles",
        nargs="+",
        metavar="ROLE",
        help="Specific roles to warm up (default: query_extract + analyst).",
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
