import json
import logging
from pathlib import Path

import requests

# --- Path resolution: repo root is two levels above Scripts/tools/ ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Same directory as SEC_tickers.json (see sec_ingestion.CONFIG_FOLDER)
SEC_INGESTION_CONFIG_DIR = PROJECT_ROOT / "config" / "SEC_Ingestion"
SEC_TICKERS_FILE = SEC_INGESTION_CONFIG_DIR / "SEC_tickers.json"
OUTPUT_CIK_MAP = SEC_INGESTION_CONFIG_DIR / "ticker_to_cik.json"

SEC_INGESTION_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

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
        if SEC_TICKERS_FILE.is_file():
            logger.info(f"   (Ticker universe file present: {SEC_TICKERS_FILE})")
        else:
            logger.warning(
                f"   SEC_tickers.json not found at {SEC_TICKERS_FILE} — "
                "ingestion may fall back to defaults until it exists."
            )

    except Exception as e:
        logger.error(f"❌ Download or parse failed: {e}")


if __name__ == "__main__":
    generate_local_cik_map()
