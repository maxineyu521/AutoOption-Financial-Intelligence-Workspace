"""Utilities for agent-facing Silver context views.

Retrieval may keep supplemental Silver evidence under nested provenance lanes
such as ``silver_context["compensation"]``. Agent prompts and deterministic
audits still need a single canonical evidence view. These helpers build that
view without mutating the retrieval payload, so provenance remains available
for audit/debug while values and citation contracts are visible to Analyst,
Checker, and Finalizer.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


def effective_silver_context(silver_context: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return a non-mutating Silver context with compensation evidence exposed."""
    if not isinstance(silver_context, dict):
        return {}

    effective = deepcopy(silver_context)
    compensation = effective.get("compensation")
    if not isinstance(compensation, dict):
        return effective

    values = effective.setdefault("values", {})
    if not isinstance(values, dict):
        values = {}
        effective["values"] = values
    comp_values = compensation.get("values") or {}
    if isinstance(comp_values, dict):
        values.update(comp_values)

    anchors = effective.setdefault("lineage_anchors", [])
    if not isinstance(anchors, list):
        anchors = []
        effective["lineage_anchors"] = anchors
    seen = {str(anchor) for anchor in anchors}
    for anchor in compensation.get("lineage_anchors") or []:
        anchor_s = str(anchor)
        if anchor_s not in seen:
            anchors.append(anchor)
            seen.add(anchor_s)

    citation_contract = effective.setdefault("citation_contract", {})
    if not isinstance(citation_contract, dict):
        citation_contract = {}
        effective["citation_contract"] = citation_contract
    comp_contract = compensation.get("citation_contract") or {}
    if isinstance(comp_contract, dict):
        citation_contract.update(comp_contract)

    citation_anchor_map = effective.setdefault("citation_anchor_map", {})
    if not isinstance(citation_anchor_map, dict):
        citation_anchor_map = {}
        effective["citation_anchor_map"] = citation_anchor_map
    comp_anchor_map = compensation.get("citation_anchor_map") or {}
    if isinstance(comp_anchor_map, dict):
        citation_anchor_map.update(comp_anchor_map)

    return effective


def preferred_silver_context_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer frozen Silver truth, then expose nested compensation evidence."""
    frozen = state.get("silver_context_frozen")
    if isinstance(frozen, dict) and frozen.get("values"):
        return effective_silver_context(frozen)
    silver = state.get("silver_context")
    return effective_silver_context(silver if isinstance(silver, dict) else {})
