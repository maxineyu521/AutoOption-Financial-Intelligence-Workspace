import os
import sys
import json
import time
import requests
import logging
import glob
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdownify import markdownify as md
from functools import wraps

# ==========================================
# 0. Dynamic Path & Industrial Logging
# ==========================================
# Repo root: .../scrapers -> data_collection -> Scripts -> project root
BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
# Make Scripts.core.universe importable whether this file is executed directly
# (`python sec_ingestion.py`) or as a module (`python -m ...sec_ingestion`).
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

today_str = datetime.now().strftime("%Y-%m-%d")

RAW_FOLDER = os.path.join(BASE_DIR, "Data", "1_Bronze_Raw", "SEC_Parsed_JSON", today_str)
# LOG_DIR still uses the SEC_Ingestion path prefix INSIDE logs/{date}/ — that is
# a log-channel name, not a config folder. The retired config/SEC_Ingestion/
# folder is no longer created here; universe + reference maps are now resolved
# entirely through Scripts.core.universe.
LOG_DIR = os.path.join(BASE_DIR, "logs", today_str, "SEC_Ingestion")

for folder in [RAW_FOLDER, LOG_DIR]:
    os.makedirs(folder, exist_ok=True)

load_dotenv(dotenv_path=os.path.join(BASE_DIR, '.env'))

progress_log_file = os.path.join(LOG_DIR, f"ingestion_progress_{today_str}.log")
logger = logging.getLogger("ProgressLogger")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

fh_prog = logging.FileHandler(progress_log_file, encoding='utf-8')
sh_prog = logging.StreamHandler()
fh_prog.setFormatter(formatter)
sh_prog.setFormatter(formatter)
logger.addHandler(fh_prog)
logger.addHandler(sh_prog)

# SEC Official User-Agent Required!
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "DataEngineer (bot@example.com)")
SEC_HEADERS = {
    'User-Agent': SEC_USER_AGENT,
    'Accept-Encoding': 'gzip, deflate'
}

# ==========================================
# 1. Resilient Network & Local CIK Mapping
# ==========================================
def retry_on_exception(retries=3, delay=2):
    """A decorator to retry a function on exceptions, with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if i < retries - 1:
                        wait_time = delay * (2 ** i)
                        logger.warning(f"Network issue/429 triggered: {e}. Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Failed after {retries} retries.")
                        raise e
        return wrapper
    return decorator

def load_local_cik_map() -> dict:
    """Thin shim retained for backward compat with external callers.

    Delegates to ``Scripts.core.universe.UniverseLoader.cik_map()``, which reads
    ``config/reference/ticker_to_cik.json``.
    """
    try:
        from Scripts.core.universe import universe as _universe
        mapping = _universe.cik_map()
        logger.info(f"✅ Successfully loaded {len(mapping)} CIK mappings via UniverseLoader")
        return mapping
    except Exception as e:
        logger.error(f"❌ UniverseLoader.cik_map() failed: {e}. Run SEC_generate_cik_map.py first.")
        return {}

def load_tickers(file_name: str) -> list:
    """Thin shim retained for backward compat.

    Maps the legacy file name ``SEC_tickers.json`` to
    ``universe.get('sec.filers')``; anything else returns an empty list with a
    warning.
    """
    try:
        from Scripts.core.universe import universe as _universe
    except Exception as e:
        logger.error(f"UniverseLoader unavailable: {e}")
        return []
    if file_name.lower() in ("sec_tickers.json", "equity_single_name.json"):
        tickers = _universe.get("sec.filers")
        logger.info(f"Loaded {len(tickers)} tickers via UniverseLoader (sec.filers)")
        return tickers
    logger.warning(
        f"load_tickers({file_name}): no universe-role mapping for this legacy "
        "filename; returning empty list. Migrate the caller to universe.get(role)."
    )
    try:
        with open(file_name, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {file_name}: {e}")
        return []

# ==========================================
# 2. Parsers (Unchanged Routing Logic)
# ==========================================
@retry_on_exception()
def fetch_and_parse_8k_html(url: str) -> dict:
    time.sleep(0.15)
    response = requests.get(url, headers=SEC_HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    
    for tag in soup(['script', 'style', 'head', 'title', 'meta']):
        tag.decompose()
        
    html_content = str(soup)
    md_text = md(html_content, strip=['a', 'img'], heading_style="ATX")
    md_text = re.sub(r'\n{3,}', '\n\n', md_text)

    item_pattern = re.compile(
        r'(Item\s+\d\.\d{2}.*?)(?=\nItem\s+\d\.\d{2}|\nSignatures?|\nS I G N A T U R E S|$)', 
        re.IGNORECASE | re.DOTALL
    )
    
    chunks = {}
    for match in item_pattern.finditer(md_text):
        chunk_text = match.group(1).strip()
        item_num_match = re.search(r'Item\s+(\d\.\d{2})', chunk_text, re.IGNORECASE)
        if item_num_match:
            chunks[item_num_match.group(1)] = chunk_text
            
    if not chunks:
        return {"is_chunked": False, "full_markdown": md_text}

    return {"is_chunked": True, "item_chunks": chunks}

@retry_on_exception()
def fetch_and_parse_form4_xml(url: str) -> dict:
    raw_xml_url = re.sub(r'/xsl[^/]+/', '/', url)
    time.sleep(0.15)
    response = requests.get(raw_xml_url, headers=SEC_HEADERS, timeout=15)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    
    def get_text(element, path, default=""):
        node = element.find(path)
        return node.text if node is not None else default

    reporting_owner = get_text(root, ".//reportingOwnerId/rptOwnerName", "Unknown")
    title = get_text(root, ".//reportingOwnerRelationship/officerTitle", "")
    if not title:
        is_director = get_text(root, ".//reportingOwnerRelationship/isDirector") == "true"
        title = "Director" if is_director else "Other"

    is_10b5_1 = False
    for footnote in root.findall(".//footnotes/footnote"):
        if footnote.text and "10b5-1" in footnote.text:
            is_10b5_1 = True
            break

    transactions = []
    for trans in root.findall(".//nonDerivativeTransaction"):
        date = get_text(trans, ".//transactionDate/value")
        code = get_text(trans, ".//transactionCoding/transactionCode")
        shares = float(get_text(trans, ".//transactionAmounts/transactionShares/value", "0"))
        price = float(get_text(trans, ".//transactionAmounts/transactionPricePerShare/value", "0"))
        acq_disp = get_text(trans, ".//transactionAmounts/transactionAcquiredDisposedCode/value")
        post_shares = float(get_text(trans, ".//postTransactionAmounts/sharesOwnedFollowingTransaction/value", "0"))
        
        if code == "P": direction = "STRONG_BUY (Open Market)"
        elif code == "S": direction = "STRONG_SELL (Open Market)"
        elif code == "M": direction = "VESTING (Options/RSU)"
        elif code == "F": direction = "TAX_WITHHOLDING"
        else: direction = f"OTHER ({acq_disp})"

        transactions.append({
            "date": date, "code": code, "direction": direction,
            "shares": shares, "price": price, "total_value": shares * price,
            "post_transaction_shares": post_shares, "is_10b5_1_planned": is_10b5_1
        })
        
    return {"reporting_owner": reporting_owner, "role": title, "transactions": transactions}

# ==========================================
# 3. Pipeline Core (Direct EDGAR Search)
# ==========================================
@retry_on_exception()
def get_sec_filings_by_cik(cik: str, start_date_str: str, end_date_str: str) -> list:
    """use CIK to directly query SEC EDGAR for recent 4 and 8-K filings, with date filtering"""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    time.sleep(0.15) 
    response = requests.get(url, headers=SEC_HEADERS, timeout=15)
    response.raise_for_status()
    
    data = response.json()
    recent = data.get("filings", {}).get("recent", {})
    if not recent: return []

    filings = []
    # EDGAR provides filings in reverse chronological order, so we can stop once we go past the date range
    for idx, form in enumerate(recent.get("form", [])):
        filed_at = recent["filingDate"][idx]
        
        # filter by date range and form type
        if form in ["4", "8-K"] and start_date_str <= filed_at <= end_date_str:
            acc_no_with_dashes = recent["accessionNumber"][idx]
            acc_no_clean = acc_no_with_dashes.replace('-', '')
            primary_doc = recent["primaryDocument"][idx]
            
            # SEC apends the CIK in the URL path, but it should be without leading zeros
            cik_stripped = str(int(cik))
            url_doc = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_no_clean}/{primary_doc}"
            
            filings.append({
                "formType": form,
                "filedAt": filed_at,
                "accessionNo": acc_no_with_dashes,
                "linkToFilingDetails": url_doc
            })
            
    return filings

def process_sec_data(ticker: str, cik: str):
    logger.info(f"Processing: {ticker} (CIK: {cik})")
    
    start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    end_date = datetime.now().strftime('%Y-%m-%d')

    try:
        filings = get_sec_filings_by_cik(cik, start_date, end_date)
        if not filings:
            logger.info(f"[{ticker}] No 4 or 8-K filings found in the past 7 days.")
            return

        output_file = os.path.join(RAW_FOLDER, f"{ticker}.jsonl")
        
        with open(output_file, 'a', encoding='utf-8') as f_out:
            for filing in filings:
                form_type = filing.get('formType', '')
                url = filing.get('linkToFilingDetails', '')
                
                record = {
                    "metadata": {
                        "ticker": ticker,
                        "form_type": form_type,
                        "filed_at": filing.get('filedAt', ''),
                        "accession_no": filing.get('accessionNo', ''),
                        "url": url,
                        "ingested_at": datetime.now().isoformat()
                    }
                }

                if form_type == "4":
                    parsed_xml_data = fetch_and_parse_form4_xml(url)
                    if not parsed_xml_data: continue
                    record["parsed_data"] = parsed_xml_data 
                    record["raw_text"] = "XML_PARSED_SUCCESSFULLY" 
                elif form_type == "8-K":
                    parsed_8k_data = fetch_and_parse_8k_html(url)
                    if not parsed_8k_data: continue
                    
                    record["parsed_data"] = parsed_8k_data
                    record["raw_text"] = "8K_PARSED_INTO_MARKDOWN_CHUNKS"
                
                f_out.write(json.dumps(record, ensure_ascii=False) + '\n')
                
    except Exception as e:
        logger.error(f"Pipeline Error for {ticker}: {e}")

# ==========================================
# 4. Statistics Engine
# ==========================================
def generate_daily_summary():
    logger.info("Generating Summary Report...")
    summary = {"date": today_str, "total_filings": 0, "ticker_breakdown": {}}
    
    for file_path in glob.glob(os.path.join(RAW_FOLDER, "*.jsonl")):
        ticker = os.path.splitext(os.path.basename(file_path))[0]
        counts = {"4": 0, "8-K": 0}
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                item = json.loads(line)
                ftype = item["metadata"]["form_type"]
                if ftype in counts: counts[ftype] += 1
                summary["total_filings"] += 1
        
        summary["ticker_breakdown"][ticker] = counts

    summary_path = os.path.join(RAW_FOLDER, "_SUMMARY.json")
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=4)
    logger.info(f"Summary generated at {summary_path}")

# ==========================================
# Execution
# ==========================================
if __name__ == "__main__":
    if SEC_USER_AGENT == "DataEngineer (bot@example.com)":
        logger.warning("⚠️ WARNING: You are using the default SEC_USER_AGENT. Please set a custom User-Agent in your .env file to avoid being blocked by SEC.")

    # Universe + reference maps are resolved through Scripts.core.universe —
    # the single source of truth (config/universe/ + config/reference/).
    from Scripts.core.universe import universe as _universe
    nasdaq_targets = _universe.get("sec.filers")
    logger.info(f"✅ Loaded {len(nasdaq_targets)} SEC filers via UniverseLoader (role=sec.filers)")

    if nasdaq_targets:
        ticker_to_cik = _universe.cik_map()
        
        if not ticker_to_cik:
            logger.error("❌ CIK mapping is empty. Cannot proceed with SEC data ingestion.")
            exit(1)
            
        for sym in nasdaq_targets:
            cik = ticker_to_cik.get(sym)
            if not cik:
                logger.warning(f"Could not find CIK for ticker {sym}. Skipping.")
                continue
            process_sec_data(sym, cik)
            
    generate_daily_summary()