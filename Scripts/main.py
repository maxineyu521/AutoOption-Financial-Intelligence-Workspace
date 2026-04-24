"""Thin CLI shell — delegates to ``Scripts.orchestration.cli``.

Kept for backward compatibility with existing shortcuts like
``python Scripts/main.py ingest --dry-run``. New invocations should
prefer ``python -m Scripts ingest --dry-run`` which is exactly
equivalent but stays robust under packaging / PyInstaller.
"""
from __future__ import annotations

from Scripts.orchestration.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
