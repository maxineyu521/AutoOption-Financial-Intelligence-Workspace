"""Generate the static ticker -> CIK map from SEC EDGAR.

Output: ``config/reference/ticker_to_cik.json`` — this is the canonical
location read by ``Scripts.core.universe.UniverseLoader.cik_map()`` and by
``Scripts/data_collection/scrapers/sec_ingestion.py``.

The curated filer universe (``config/universe/equity_single_name.json``) is
read here only to emit a sanity warning if it is missing.
"""

import json
import logging
import sys
from pathlib import Path

import requests

# --- Path resolution: repo root is two levels above Scripts/tools/ ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Make Scripts.core.universe importable whether this file is run directly or
# via `python -m Scripts.tools.SEC_generate_cik_map`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Canonical output location (was config/SEC_Ingestion/ — now retired).
REFERENCE_DIR = PROJECT_ROOT / "config" / "reference"
OUTPUT_CIK_MAP = REFERENCE_DIR / "ticker_to_cik.json"

REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("CIK_Generator")

# Replace with a contact email per SEC fair-access guidelines.
SEC_HEADERS = {
    "User-Agent": "DataEngineer (bot@example.com)",
    "Accept-Encoding": "gzip, deflate",
}


def generate_local_cik_map() -> None:
    logger.info("📡 Downloading latest SEC ticker -> CIK mapping...")
    url = "https://www.sec.gov/files/company_tickers.json"

    try:
        response = requests.get(url, headers=SEC_HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()

        mapping = {}
        for entry in data.values():
            ticker = entry["ticker"]
            # SEC convention: 10-digit CIK string, zero-padded on the left
            cik = str(entry["cik_str"]).zfill(10)
            mapping[ticker] = cik

        with open(OUTPUT_CIK_MAP, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=4)

        logger.info(f"✅ Extracted {len(mapping)} tickers.")
        logger.info(f"💾 Wrote CIK map to: {OUTPUT_CIK_MAP}")

        # Sanity check: the curated filer universe should exist and every
        # ticker in it should resolve to a CIK (else sec_ingestion will skip).
        try:
            from Scripts.core.universe import universe as _universe
            filers = _universe.get("sec.filers")
            missing = [t for t in filers if t.upper() not in mapping]
            if missing:
                logger.warning(
                    f"⚠️ {len(missing)} filer tickers have no CIK in the freshly "
                    f"downloaded map (sample: {missing[:8]}). sec_ingestion will "
                    "skip these until they appear in SEC EDGAR."
                )
            else:
                logger.info(
                    f"   (Filer universe OK: all {len(filers)} tickers mapped.)"
                )
        except Exception as e:
            logger.warning(
                f"Could not cross-check filer universe via UniverseLoader: {e}"
            )

    except Exception as e:
        logger.error(f"❌ Download or parse failed: {e}")


if __name__ == "__main__":
    generate_local_cik_map()
