"""Observability primitives — structured logging + unified audit paths.

Kept dependency-free so that every script / agent can import it at boot
without paying the cost of heavy libraries.
"""
from Scripts.observability.audit import (
    audit_path,
    configure_root_logger,
    current_run_id,
    get_audit_logger,
    start_run,
)

__all__ = [
    "audit_path",
    "configure_root_logger",
    "current_run_id",
    "get_audit_logger",
    "start_run",
]
