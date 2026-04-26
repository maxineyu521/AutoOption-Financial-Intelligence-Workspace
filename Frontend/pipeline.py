from __future__ import annotations

import asyncio
import copy
from typing import Any, AsyncIterator, Dict, Iterable

from .config import MAX_ROUTER_STEPS


APPEND_ONLY_KEYS = {"critic_feedback", "node_audit_log"}


def _merge_state(state: Dict[str, Any], delta: Dict[str, Any]) -> None:
    for key, value in (delta or {}).items():
        if key in APPEND_ONLY_KEYS and isinstance(value, list):
            existing = state.get(key) or []
            state[key] = [*existing, *value]
        else:
            state[key] = value


async def run_router_nodes_stream(query: str) -> AsyncIterator[Dict[str, Any]]:
    """
    Stream node-level updates by directly calling router.py node functions.
    This avoids waiting for a full final_state before rendering the UI.
    """
    from Scripts.agents import router

    state: Dict[str, Any] = {
        "original_query": query,
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
    retrieval_delta = await router.master_retrieval_node(state)
    _merge_state(state, retrieval_delta)
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
