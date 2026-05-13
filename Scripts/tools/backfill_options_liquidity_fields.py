"""Backfill executable-liquidity fields for legacy options parquet snapshots.

This tool rewrites historical files under:
    Data/2_Silver_Processed/Options_Market_Data/<date>/<SYMBOL>_options_<date>.parquet

It upgrades legacy snapshots to the current liquidity contract by recomputing:
    - moneyness_ratio
    - moneyness_pct
    - spread_ratio
    - spread_pct
    - is_liquid_basic
    - is_executable_liquid
    - is_liquid (legacy alias -> is_liquid_basic)

Important:
    - `spread_pct` is stored as percentage points, so 2.5 means 2.5%, not 0.025.
    - This script intentionally recomputes the derived fields even if they already
      exist, so old ask-based spread semantics do not survive the migration.
    - The script expects pandas + pyarrow in the execution environment.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


try:
    import pandas as pd
    import numpy as np
except ImportError as exc:  # pragma: no cover - import guard for real runtime
    raise SystemExit(
        "pandas is required for this backfill tool. "
        "Run it from the project environment that includes pandas + pyarrow."
    ) from exc

try:
    import pyarrow  # noqa: F401
except ImportError as exc:  # pragma: no cover - import guard for real runtime
    raise SystemExit(
        "pyarrow is required for parquet backfill. "
        "Run it from the project environment that includes pyarrow."
    ) from exc

from Scripts.core.liquidity_policy import executable_moneyness_band_ratio


LOG = logging.getLogger("backfill_options_liquidity_fields")

OPTIONS_ROOT = PROJECT_ROOT / "Data" / "2_Silver_Processed" / "Options_Market_Data"
ORDERED_COLUMNS = [
    "snapshot_date",
    "symbol",
    "underlying_price",
    "contract_symbol",
    "option_type",
    "strike",
    "expiration",
    "dte",
    "moneyness_ratio",
    "moneyness_pct",
    "last_price",
    "bid",
    "ask",
    "spread_ratio",
    "spread_pct",
    "volume",
    "open_interest",
    "implied_volatility",
    "in_the_money",
    "is_liquid_basic",
    "is_executable_liquid",
    "is_liquid",
]
REQUIRED_INPUT_COLUMNS = {
    "symbol",
    "underlying_price",
    "strike",
    "bid",
    "ask",
    "volume",
    "open_interest",
    "dte",
}


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill executable-liquidity fields in legacy options parquet snapshots."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=OPTIONS_ROOT,
        help=f"Options parquet root (default: {OPTIONS_ROOT})",
    )
    parser.add_argument(
        "--date",
        action="append",
        default=[],
        help="Snapshot date folder(s) to target, e.g. 2026-05-10. Repeatable.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Ticker symbol(s) to target, e.g. TSLA. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview files and recomputed counts without writing parquet.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def _iter_target_files(root: Path, dates: Iterable[str], symbols: Iterable[str]) -> List[Path]:
    if not root.exists():
        raise FileNotFoundError(f"Options parquet root not found: {root}")

    date_filter = {d.strip() for d in dates if str(d).strip()}
    symbol_filter = {s.strip().upper() for s in symbols if str(s).strip()}

    files = []
    for path in sorted(root.glob("*/*.parquet")):
        snapshot_date = path.parent.name
        stem = path.stem.upper()
        if date_filter and snapshot_date not in date_filter:
            continue
        if symbol_filter and not any(stem.startswith(f"{ticker}_OPTIONS_") for ticker in symbol_filter):
            continue
        files.append(path)
    return files


def _coerce_numeric(df: pd.DataFrame, columns: Iterable[str]) -> None:
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")


def _recompute_liquidity_fields(df: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(REQUIRED_INPUT_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(f"Missing required input columns: {missing}")

    out = df.copy()
    _coerce_numeric(
        out,
        [
            "underlying_price",
            "strike",
            "bid",
            "ask",
            "volume",
            "open_interest",
            "dte",
        ],
    )

    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()

    out["moneyness_ratio"] = np.nan
    valid_underlying = out["underlying_price"] > 0
    out.loc[valid_underlying, "moneyness_ratio"] = (
        (out.loc[valid_underlying, "strike"] / out.loc[valid_underlying, "underlying_price"]) - 1.0
    ).abs()
    out["moneyness_pct"] = out["moneyness_ratio"] * 100.0

    out["spread_ratio"] = np.nan
    valid_quote = (
        out["ask"].notna()
        & out["bid"].notna()
        & (out["ask"] > 0)
        & (out["bid"] >= 0)
        & (out["ask"] >= out["bid"])
    )
    mid = (out["ask"] + out["bid"]) / 2.0
    valid_mid = valid_quote & (mid > 0)
    out.loc[valid_mid, "spread_ratio"] = (
        (out.loc[valid_mid, "ask"] - out.loc[valid_mid, "bid"]) / mid.loc[valid_mid]
    )
    out["spread_pct"] = out["spread_ratio"] * 100.0

    out["is_liquid_basic"] = (
        (out["volume"] >= 50)
        & (out["open_interest"] >= 100)
        & (out["bid"] > 0)
    ).fillna(False)

    moneyness_band = out["symbol"].map(lambda ticker: executable_moneyness_band_ratio(ticker) or 0.10)
    out["is_executable_liquid"] = (
        out["is_liquid_basic"]
        & (out["ask"] >= 0.50)
        & out["spread_ratio"].notna()
        & (out["spread_ratio"] <= 0.15)
        & out["moneyness_ratio"].notna()
        & (out["moneyness_ratio"] <= moneyness_band)
        & out["dte"].between(7, 60, inclusive="both")
    ).fillna(False)

    out["is_liquid"] = out["is_liquid_basic"]

    existing_other_columns = [c for c in out.columns if c not in ORDERED_COLUMNS]
    reordered = [c for c in ORDERED_COLUMNS if c in out.columns] + existing_other_columns
    return out[reordered]


def _write_parquet_atomically(df: pd.DataFrame, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix=target_path.stem + "_", suffix=".parquet", dir=target_path.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        df.to_parquet(temp_path, index=False, engine="pyarrow")
        temp_path.replace(target_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def main() -> int:
    args = _parse_args()
    _configure_logging(args.verbose)

    files = _iter_target_files(args.root.resolve(), args.date, args.symbol)
    if not files:
        LOG.warning("No parquet files matched the requested filters.")
        return 0

    LOG.info("Matched %d parquet file(s) under %s", len(files), args.root.resolve())

    updated = 0
    for path in files:
        try:
            df = pd.read_parquet(path, engine="pyarrow")
            patched = _recompute_liquidity_fields(df)
            liquid_basic_count = int(patched["is_liquid_basic"].sum())
            executable_count = int(patched["is_executable_liquid"].sum())
            LOG.info(
                "%s | rows=%d | liquid_basic=%d | executable=%d%s",
                path.relative_to(PROJECT_ROOT),
                len(patched),
                liquid_basic_count,
                executable_count,
                " | dry-run" if args.dry_run else "",
            )
            if not args.dry_run:
                _write_parquet_atomically(patched, path)
                updated += 1
        except Exception as exc:
            LOG.exception("Failed to patch %s: %s", path, exc)
            return 1

    if args.dry_run:
        LOG.info("Dry run complete. No files were modified.")
    else:
        LOG.info("Backfill complete. Updated %d parquet file(s).", updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
