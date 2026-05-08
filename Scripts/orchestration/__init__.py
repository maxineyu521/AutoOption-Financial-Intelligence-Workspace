"""Orchestration layer — pipeline DAG, runtime state, CLI entrypoint.

Intentionally re-exports only the *light* surface so this module is safe
to import from anywhere (agents, tests, Jupyter) without dragging in the
agent graph / LangChain. Heavy things (CLI entry, LLM-backed query) live
in ``Scripts.orchestration.cli`` and must be imported explicitly.
"""
from Scripts.orchestration.run_state import (
    Cadence,
    DATASET_ANCHOR_KEYS,
    RunState,
    default_state_path,
    get_run_state,
    reset_run_state_singleton,
    run_key,
    run_key_to_date,
)
from Scripts.orchestration.stages import (
    CallableStage,
    ScriptStage,
    Stage,
    StageResult,
)
from Scripts.orchestration.pipeline import (
    Pipeline,
    build_default_pipeline,
    default_stage_manifest,
)

__all__ = [
    "Cadence",
    "DATASET_ANCHOR_KEYS",
    "RunState",
    "default_state_path",
    "get_run_state",
    "reset_run_state_singleton",
    "run_key",
    "run_key_to_date",
    "Stage",
    "StageResult",
    "ScriptStage",
    "CallableStage",
    "Pipeline",
    "build_default_pipeline",
    "default_stage_manifest",
]
