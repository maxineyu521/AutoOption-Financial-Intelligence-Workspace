from __future__ import annotations

import asyncio
import copy
from typing import Any, AsyncIterator, Dict, Iterable

from .config import MAX_ROUTER_STEPS


APPEND_ONLY_KEYS = {"critic_feedback", "node_audit_log"}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _merge_state(state: Dict[str, Any], delta: Dict[str, Any]) -> None:
    for key, value in (delta or {}).items():
        if key in APPEND_ONLY_KEYS and isinstance(value, list):
            existing = state.get(key) or []
            state[key] = [*existing, *value]
        else:
            state[key] = value


async def run_router_nodes_stream(
    query: str,
    query_builder_contract: Dict[str, Any] | None = None,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Stream node-level updates by directly calling router.py node functions.
    This avoids waiting for a full final_state before rendering the UI.
    """
    from Scripts.agents import router

    state: Dict[str, Any] = {
        "original_query": query,
        "query_builder_contract": query_builder_contract or None,
        "critic_feedback": [],
        "node_audit_log": [],
    }

    async def _run_node(name: str, fn, payload: Dict[str, Any]) -> Dict[str, Any]:
        yield_payload = {
            "event": "node_start",
            "node": name,
            "state": copy.deepcopy(payload),
        }
        await asyncio.sleep(0)
        return yield_payload

    # Retrieval node
    yield await _run_node("retrieval_master", router.master_retrieval_node, state)
    for message in [
        "Parse query intent with ontology-aware metadata extraction.",
        "Compile source-aware time predicates for structured data and narrative evidence retrieval.",
        "Build HyDE anticipation: a hypothetical market thesis used to expand semantic recall.",
        "Query DuckDB and Parquet for deterministic options, macro, and GPR numerics.",
        "Query semantic retrieval for news, SEC filings, and broader narrative evidence.",
    ]:
        yield {
            "event": "stage_detail",
            "node": "retrieval_master",
            "message": message,
            "state": copy.deepcopy(state),
        }
        await asyncio.sleep(0)
    retrieval_delta = await router.master_retrieval_node(state)
    _merge_state(state, retrieval_delta)
    metadata = state.get("metadata")
    silver_values = ((state.get("silver_context") or {}).get("values") or {})
    hyde = state.get("hyde_anticipation") or {}
    time_range = state.get("time_range") or {}
    summary_messages = [
        (
            "Intent extracted: "
            f"tickers={_field(metadata, 'tickers', []) or []}; "
            f"metrics={_field(metadata, 'metrics', []) or []}; "
            f"sources={_field(metadata, 'source_types', []) or []}."
        ),
        (
            "Time window compiled: "
            f"{time_range.get('start_date', 'N/A')} -> {time_range.get('end_date', 'N/A')} "
            f"({time_range.get('time_window_label', 'N/A')}, {time_range.get('window_days', 'N/A')}d)."
        ),
        (
            "HyDE ready: "
            f"{str(hyde.get('rerank_query') or hyde.get('paragraph') or 'no expansion needed')[:180]}"
        ),
        (
            "Structured data retrieval complete: "
            f"{len(silver_values)} numeric fields, "
            f"{len((state.get('silver_context') or {}).get('lineage_anchors') or [])} lineage anchors."
        ),
        f"Narrative retrieval complete: {len(state.get('gold_context') or [])} semantic chunks.",
    ]
    for message in summary_messages:
        yield {
            "event": "stage_detail",
            "node": "retrieval_master",
            "message": message,
            "state": copy.deepcopy(state),
        }
        await asyncio.sleep(0)
    yield {"event": "node_end", "node": "retrieval_master", "state": copy.deepcopy(state)}

    steps = 0
    while steps < MAX_ROUTER_STEPS:
        steps += 1

        yield await _run_node("analyst", router.analyst_node, state)
        analyst_delta = await router.analyst_node(state)
        _merge_state(state, analyst_delta)
        yield {"event": "node_end", "node": "analyst", "state": copy.deepcopy(state)}

        yield await _run_node("checker", router.checker_node, state)
        checker_delta = await router.checker_node(state)
        _merge_state(state, checker_delta)
        yield {"event": "node_end", "node": "checker", "state": copy.deepcopy(state)}

        after_checker = router.route_after_checker(state)
        yield {
            "event": "route_decision",
            "node": "checker",
            "route": after_checker,
            "state": copy.deepcopy(state),
        }
        if after_checker == "analyst":
            continue
        if after_checker == "finalizer":
            yield await _run_node("finalizer", router.finalizer_node, state)
            finalizer_delta = await router.finalizer_node(state)
            _merge_state(state, finalizer_delta)
            yield {"event": "node_end", "node": "finalizer", "state": copy.deepcopy(state)}
            break

        yield await _run_node("critic", router.critic_node, state)
        critic_delta = await router.critic_node(state)
        _merge_state(state, critic_delta)
        yield {"event": "node_end", "node": "critic", "state": copy.deepcopy(state)}

        after_critic = router.route_after_critic(state)
        yield {
            "event": "route_decision",
            "node": "critic",
            "route": after_critic,
            "state": copy.deepcopy(state),
        }
        if after_critic == "analyst":
            continue

        yield await _run_node("finalizer", router.finalizer_node, state)
        finalizer_delta = await router.finalizer_node(state)
        _merge_state(state, finalizer_delta)
        yield {"event": "node_end", "node": "finalizer", "state": copy.deepcopy(state)}
        break

    yield {"event": "complete", "node": "pipeline", "state": copy.deepcopy(state)}
