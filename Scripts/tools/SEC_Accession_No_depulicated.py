import json
import logging
from pathlib import Path

# --- Path resolution: repo root is two levels above Scripts/tools/ ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]

BRONZE_SEC_JSONL_ROOT = (
    PROJECT_ROOT / "Data" / "1_Bronze_Raw" / "SEC_Parsed_JSON"
)
# Canonical location migrated 2026-04-22:
#   config/SEC_Processing/global_processed_registry.json  (retired)
#   -> config/runtime/sec_processed_registry.json         (current)
GLOBAL_PROCESSED_REGISTRY = (
    PROJECT_ROOT / "config" / "runtime" / "sec_processed_registry.json"
)

TARGET_DATE = "2026-04-08"

logger = logging.getLogger("DedupeBot")


def _log_dir_for_run(target_date: str) -> Path:
    """Run audit trail: logs/{YYYY-MM-DD}/ (date-aligned with other daily artifacts)."""
    return PROJECT_ROOT / "logs" / target_date


def _configure_logging(target_date: str) -> None:
    log_dir = _log_dir_for_run(target_date)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"SEC_bronze_accession_dedupe_{target_date}.log"

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    logger.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False


def _truncate_list(items, limit: int = 80):
    if len(items) <= limit:
        return list(items), 0
    return list(items[:limit]), len(items) - limit


def _load_global_processed_registry():
    if not GLOBAL_PROCESSED_REGISTRY.is_file():
        logger.warning(
            f"⚠️ Registry not found: {GLOBAL_PROCESSED_REGISTRY} — "
            "treating as empty (only within-file dedupe will apply)."
        )
        return set()
    with open(GLOBAL_PROCESSED_REGISTRY, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        logger.error("❌ global_processed_registry.json must be a JSON array of strings.")
        return set()
    return set(str(x) for x in data)


def _is_ticker_jsonl(path: Path) -> bool:
    """Bronze layout: {TICKER}.jsonl (no patched_ prefix, no date suffix)."""
    name = path.name
    if not name.endswith(".jsonl"):
        return False
    if name.startswith("patched_") or name.startswith("_"):
        return False
    if "SUMMARY" in name.upper():
        return False
    return True


def deduplicate_all_files() -> None:
    _configure_logging(TARGET_DATE)

    data_dir = BRONZE_SEC_JSONL_ROOT / TARGET_DATE
    if not data_dir.is_dir():
        logger.error(f"❌ Data directory not found: {data_dir}")
        return

    global_processed = _load_global_processed_registry()

    candidates = sorted(p for p in data_dir.iterdir() if p.is_file() and _is_ticker_jsonl(p))

    logger.info(
        "=== SEC Bronze accession deduplication run ===\n"
        f"target_date: {TARGET_DATE}\n"
        f"bronze_dir: {data_dir}\n"
        f"registry_file: {GLOBAL_PROCESSED_REGISTRY}\n"
        f"registry_accession_count: {len(global_processed)}\n"
        f"candidate_jsonl_files: {len(candidates)}"
    )

    if not candidates:
        logger.warning(
            f"⚠️ No per-ticker *.jsonl files under {data_dir} "
            "(expected names like AAPL.jsonl)."
        )
        return

    for file_path in candidates:
        filename = file_path.name
        seen_accessions = set()
        records_to_save = []

        input_lines = 0
        dropped_missing_accession = 0
        dropped_in_registry: list[str] = []
        dropped_duplicate_in_file: list[str] = []

        logger.info(f"--- Processing: {filename} (in-place rewrite) ---")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    input_lines += 1
                    record = json.loads(line)

                    acc_no = record.get("metadata", {}).get("accession_no")
                    if not acc_no:
                        dropped_missing_accession += 1
                        continue
                    if acc_no in global_processed:
                        dropped_in_registry.append(acc_no)
                        continue
                    if acc_no in seen_accessions:
                        dropped_duplicate_in_file.append(acc_no)
                        continue

                    seen_accessions.add(acc_no)
                    records_to_save.append(record)

            output_lines = len(records_to_save)
            removed_total = input_lines - output_lines

            with open(file_path, "w", encoding="utf-8") as f_out:
                for rec in records_to_save:
                    f_out.write(json.dumps(rec, ensure_ascii=False) + "\n")

            shown_reg, more_reg = _truncate_list(dropped_in_registry)
            shown_dup, more_dup = _truncate_list(dropped_duplicate_in_file)

            logger.info(
                f"Summary | file={filename}\n"
                f"  input_lines (non-empty): {input_lines}\n"
                f"  output_lines (kept):     {output_lines}\n"
                f"  removed_total:           {removed_total}\n"
                f"  dropped_missing_accession: {dropped_missing_accession}\n"
                f"  dropped_in_global_registry: {len(dropped_in_registry)}\n"
                f"  dropped_duplicate_within_file: {len(dropped_duplicate_in_file)}"
            )

            if dropped_in_registry:
                extra = f" ... ({more_reg} more)" if more_reg else ""
                logger.info(
                    "  accessions_removed_as_already_processed (sample):\n    "
                    + "\n    ".join(shown_reg)
                    + extra
                )

            if dropped_duplicate_in_file:
                extra = f" ... ({more_dup} more)" if more_dup else ""
                logger.info(
                    "  accessions_removed_as_in_file_duplicates (sample):\n    "
                    + "\n    ".join(shown_dup)
                    + extra
                )

            logger.info(f"✅ Wrote {output_lines} rows -> {file_path}")

        except Exception as e:
            logger.error(f"❌ Error while processing {filename}: {e}")


if __name__ == "__main__":
    deduplicate_all_files()
