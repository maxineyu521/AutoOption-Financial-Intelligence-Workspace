"""
Bronze SEC JSONL patch utility (Form 4 XML + 8-K HTML).

Purpose
-------
Ingestion writes per-ticker JSONL under ``Data/1_Bronze_Raw/SEC_Parsed_JSON/{date}/``.
Some rows may still lack structured ``parsed_data`` (e.g. Form 4 needs EDGAR XML;
8-K benefits from HTML cleanup and item chunking). This script re-opens those
source files, fetches the filing URL content, runs the same parsers used in
``sec_ingestion`` (XML for Form 4, HTML→markdown chunks for 8-K), and writes
parallel outputs ``patched_{TICKER}.jsonl`` in the same date folder. It does
not change the original files.

Logging
-------
Patch runs log to ``logs/{target_date}/SEC_Ingestion/`` (same date partition and
SEC subtree as ``sec_ingestion`` / ``sec_processor``), in a dedicated file
``patch_form4_8k_{target_date}.log`` so ingestion progress logs stay separate.
"""

import glob
import json
import logging
import os
import sys
from pathlib import Path

# --- Path resolution: repo root is two levels above Scripts/tools/ ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Bronze SEC JSONL layout (matches sec_ingestion.RAW_FOLDER)
DATA_REL_PARTS = ("Data", "1_Bronze_Raw", "SEC_Parsed_JSON")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Scripts.data_collection.scrapers.sec_ingestion import (
    fetch_and_parse_form4_xml,
    fetch_and_parse_8k_html,
)

logger = logging.getLogger("PatchBot")


def _patch_log_dir(target_date: str) -> Path:
    """Daily SEC logs: logs/YYYY-MM-DD/SEC_Ingestion/ (shared with ingestion pipeline)."""
    return PROJECT_ROOT / "logs" / target_date / "SEC_Ingestion"


def _configure_logging(target_date: str) -> None:
    log_dir = _patch_log_dir(target_date)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"patch_form4_8k_{target_date}.log"

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


def _is_source_bronze_jsonl(path: Path) -> bool:
    """Per-ticker bronze files: {TICKER}.jsonl — skip patched outputs and summaries."""
    name = path.name
    if not name.endswith(".jsonl"):
        return False
    if name.startswith("patched_") or name.startswith("_"):
        return False
    if "SUMMARY" in name.upper():
        return False
    return True


def run_patch(target_date: str) -> None:
    _configure_logging(target_date)

    target_folder = PROJECT_ROOT.joinpath(*DATA_REL_PARTS, target_date)

    if not target_folder.is_dir():
        logger.error(f"❌ Path does not exist: {target_folder}")
        logger.info(f"💡 Verify PROJECT_ROOT is correct: {PROJECT_ROOT}")
        return

    search_pattern = str(target_folder / "*.jsonl")
    all_files = glob.glob(search_pattern)
    target_files = [
        f
        for f in all_files
        if _is_source_bronze_jsonl(Path(f))
    ]

    if not target_files:
        logger.warning(f"⚠️ No source *.jsonl files to process under {target_folder}.")
        return

    logger.info(
        f"🚀 Starting patch run | target_date={target_date} | files={len(target_files)} | "
        f"log_dir={_patch_log_dir(target_date)}"
    )

    for file_path in target_files:
        filename = os.path.basename(file_path)
        patched_file_path = target_folder / f"patched_{filename}"

        if patched_file_path.is_file():
            logger.info(f"⏭️ Skipping; output already exists: {patched_file_path}")
            continue

        logger.info(f"📝 Processing: {filename}")

        rows_processed = 0
        form4_patched = 0
        form8k_patched = 0

        try:
            with open(file_path, "r", encoding="utf-8") as f_in, open(
                patched_file_path, "w", encoding="utf-8"
            ) as f_out:

                for line_num, line in enumerate(f_in, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        record = json.loads(line)
                        metadata = record.get("metadata", {})
                        form_type = str(metadata.get("form_type", ""))
                        url = metadata.get("url", "")

                        # --- Form 4: fetch EDGAR XML and parse into structured fields ---
                        if form_type == "4" and url:
                            if not record.get("parsed_data"):
                                logger.info(
                                    f"   -> Fetch Form 4: {metadata.get('ticker')} (line {line_num})"
                                )
                                parsed_xml = fetch_and_parse_form4_xml(url)

                                if parsed_xml:
                                    record["parsed_data"] = parsed_xml
                                    record["raw_text"] = "XML_PARSED_SUCCESSFULLY"
                                    form4_patched += 1
                                else:
                                    logger.error(f"   ❌ Form 4 parse failed: {url}")
                                    record["parsed_data"] = {}

                        # --- 8-K: fetch HTML, strip noise, split into item markdown chunks ---
                        elif form_type == "8-K" and url:
                            if not record.get("parsed_data") or record.get(
                                "raw_text"
                            ) != "8K_PARSED_INTO_MARKDOWN_CHUNKS":
                                logger.info(
                                    f"   -> Fetch 8-K: {metadata.get('ticker')} (line {line_num})"
                                )
                                parsed_8k = fetch_and_parse_8k_html(url)

                                if parsed_8k:
                                    record["parsed_data"] = parsed_8k
                                    record["raw_text"] = "8K_PARSED_INTO_MARKDOWN_CHUNKS"
                                    form8k_patched += 1
                                else:
                                    logger.error(f"   ❌ 8-K parse failed: {url}")
                                    record["parsed_data"] = {}

                        if "parsed_data" not in record:
                            record["parsed_data"] = {}

                        f_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                        rows_processed += 1

                    except Exception as e:
                        logger.error(f"   ❌ Parse error on line {line_num}: {e}")

            logger.info(
                f"✅ Finished {filename}: rows={rows_processed} | Form 4 patches={form4_patched} | 8-K patches={form8k_patched}"
            )

        except Exception as e:
            logger.error(f"❌ File processing aborted for {filename}: {e}")
            if patched_file_path.is_file():
                patched_file_path.unlink()


if __name__ == "__main__":
    DATE_TO_FIX = "2026-04-04"
    run_patch(DATE_TO_FIX)
