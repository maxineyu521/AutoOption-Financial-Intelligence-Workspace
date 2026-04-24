"""
``Scripts.core.yfinance_bootstrap`` — cluster-safe yfinance initialisation.

Why this module exists
----------------------
``yfinance`` persists a per-ticker timezone lookup into a SQLite file
(``tkr-tz.db``). SQLite requires POSIX advisory locks (``fcntl``) that
most shared cluster filesystems (NFSv3, GPFS, BeeGFS, Lustre) either
drop or mis-implement, producing ``sqlite3.OperationalError('locking
protocol')`` — the exact failure captured on 2026-04-22 in both
``logs/2026-04-22/options_scraper_2026-04-22.log`` and
``logs/2026-04-22/macro_pipeline.log``.

The fix is environment-independent: at import time, redirect the
yfinance cache to a path that *does* support file locks. This module
encapsulates the resolution logic so callers don't have to remember
``export TMPDIR=…`` on every new SGE / SCC node.

Resolution order
----------------
1. ``$YFINANCE_CACHE_DIR``             — explicit operator override
2. ``/scratch/$USER/yfinance-cache``    — BU SCC local scratch (preferred)
3. ``$TMPDIR/yfinance-cache``           — SGE-allocated scratch
4. ``tempfile.gettempdir()/yfinance-cache`` — last-resort portable
   fallback (usually ``/tmp`` on Linux, ``%TEMP%`` on Windows)

Design notes
------------
* **Idempotent.** Calling :func:`configure_yfinance_cache` more than
  once is a no-op after the first successful call — scrapers that
  import each other transitively won't double-configure.
* **Silent on success, single log on activation.** The scrapers still
  own their own logging; this module emits exactly one INFO line so
  ops can verify *which* path actually won.
* **No side-effects at import time.** The helper is callable but
  nothing runs until a scraper explicitly invokes it. This keeps
  ``Scripts.core`` import-safe on environments without ``yfinance``
  (docs builds, CI smoke tests).

Usage
-----
Add these two lines at the TOP of any yfinance-dependent scraper, before
any ``import yfinance`` or ``yf.*`` call that would touch the tz cache::

    from Scripts.core.yfinance_bootstrap import configure_yfinance_cache
    configure_yfinance_cache()
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

__all__ = ["configure_yfinance_cache", "resolved_cache_dir"]

_STATE: dict = {"configured": False, "path": None}


def _repo_root() -> Path:
    """``Scripts/core/yfinance_bootstrap.py`` → repository root."""
    return Path(__file__).resolve().parents[2]


def _candidate_paths() -> List[Path]:
    """Build the ordered list of candidate cache directories.

    Empty / ``None`` entries are skipped so the first truthy candidate
    wins. This keeps the resolution table declarative — adding a fifth
    fallback (e.g. a Docker volume) is a single list entry.
    """
    user = os.getenv("USER") or os.getenv("USERNAME") or "app"
    candidates: List[Path] = [
        os.getenv("YFINANCE_CACHE_DIR"),           
        os.getenv("XDG_CACHE_HOME"),               
        f"/tmp/{user}_yfinance_cache",             
        f"/scratch/{user}/yfinance-cache",         
        (os.getenv("TMPDIR") or "").rstrip("/") + "/yfinance-cache" if os.getenv("TMPDIR") else None,
        os.path.join(tempfile.gettempdir(), f"{user}_yfinance_cache")          
    ]
    return [Path(c) for c in candidates if c]


def _first_writable(paths: List[Path]) -> Path:
    """Return the first path we can ``mkdir -p`` successfully.

    We only check write-ability (``os.access(..., os.W_OK)``) *after*
    the directory exists; some clusters require the directory to be
    created before permission bits are even queryable. Hard failure
    (``PermissionError`` / ``OSError`` on every candidate) is a
    deployment error — we surface it explicitly instead of silently
    falling through to a read-only path.
    """
    last_error: Optional[Exception] = None
    for candidate in paths:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if os.access(candidate, os.W_OK):
                return candidate
        except OSError as exc:  # permission, read-only FS, path too long, …
            last_error = exc
            continue
    raise RuntimeError(
        f"yfinance_bootstrap: no writable cache candidate succeeded. "
        f"Tried: {[str(p) for p in paths]}. Last error: {last_error!r}"
    )


def configure_yfinance_cache(force: bool = False) -> Path:
    """Point yfinance's SQLite tz cache at a lock-safe directory.

    Parameters
    ----------
    force
        Re-resolve even if a previous call already succeeded. Useful
        in tests and long-running daemons that want to re-check after
        a filesystem mount change.

    Returns
    -------
    pathlib.Path
        The directory yfinance will now write its cache into.

    Raises
    ------
    RuntimeError
        If *every* candidate path is unwritable. This is a deployment
        error that should fail-fast at scraper startup rather than
        producing silent "locking protocol" errors hours later.
    """
    if _STATE["configured"] and not force:
        return _STATE["path"]

    chosen = _first_writable(_candidate_paths())

    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover — hit only on docs builds
        raise RuntimeError(
            "yfinance_bootstrap.configure_yfinance_cache called but "
            "`yfinance` is not installed. Install it or guard the call "
            "with `if TYPE_CHECKING:`."
        ) from exc

    yf.set_tz_cache_location(str(chosen))

    _STATE["configured"] = True
    _STATE["path"] = chosen
    log.info(
        "yfinance_bootstrap: tz cache → %s (candidates tried in order: %s)",
        chosen,
        [str(p) for p in _candidate_paths()],
    )
    return chosen


def resolved_cache_dir() -> Optional[Path]:
    """Return the cache directory picked by the most recent successful
    :func:`configure_yfinance_cache` call, or ``None`` if the helper
    has never run. Cheap enough to call from health-check endpoints."""
    return _STATE["path"]
