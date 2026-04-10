import os
import json
import glob
import logging
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from langchain_ollama import ChatOllama
from dotenv import load_dotenv

# ==========================================
# 0. Configuration
# ==========================================
parser = argparse.ArgumentParser()
parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
args = parser.parse_args()
TARGET_DATE = args.date

BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
RAW_FOLDER = os.path.join(BASE_DIR, "Data", "1_Bronze_Raw", "SEC_Parsed_JSON", TARGET_DATE)
QDRANT_INPUT_FOLDER = os.path.join(BASE_DIR, "Data", "3_Gold_Semantic",  "SEC_Insider_Trades", TARGET_DATE)
LOG_DIR = os.path.join(BASE_DIR, "logs", TARGET_DATE, "SEC_Ingestion")
GLOBAL_REGISTRY_PATH = os.path.join(BASE_DIR, "config", "SEC_Processing", "global_processed_registry.json")

for folder in [QDRANT_INPUT_FOLDER, LOG_DIR]:
    os.makedirs(folder, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 1. Global Registry (Real-time sync)
# ==========================================
def load_global_registry() -> set:
    if os.path.exists(GLOBAL_REGISTRY_PATH):
        with open(GLOBAL_REGISTRY_PATH, 'r') as f:
            return set(json.load(f))
    return set()

def save_single_to_registry(accession_no: str):
    """append a single accession number to the global registry in a thread-safe way"""
    registry = load_global_registry()
    registry.add(accession_no)
    with open(GLOBAL_REGISTRY_PATH, 'w') as f:
        json.dump(list(registry), f)

# ==========================================
# 2. Enhanced Expert Rules for Form 4
# ==========================================
def process_form4_rules(ticker: str, parsed_data: dict) -> dict:
    """business rules: zero dollar transactions with share changes are likely vesting events, planned 10b5-1 sales are less negative, and C-suite insider buys are more positive"""
    owner = parsed_data.get("reporting_owner", "Unknown")
    role = parsed_data.get("role", "Officer").lower()
    transactions = parsed_data.get("transactions", [])
    
    if not transactions: return None

    net_value = 0
    total_shares_traded = 0
    is_10b5_1 = False
    
    for t in transactions:
        val = t.get("total_value", 0)
        shares = t.get("shares", 0)
        total_shares_traded += shares
        if t.get("is_10b5_1_planned"): is_10b5_1 = True
            
        if t.get("code") == "P": net_value += val
        elif t.get("code") == "S": net_value -= val

    # score logic: +3 for buys, -3 for sells, +2 if C-suite buyer, -2 if C-suite seller, -1 for 10b5-1 planned sells, 0 for vesting/zero-dollar
    tone_score = 0
    action_direction = "NONE"
    
    if net_value > 0:
        tone_score = 3
        if "chief" in role or "ceo" in role: tone_score += 2
        action_direction = "BUY"
        action_word = "bought"
    elif net_value < 0:
        if is_10b5_1:
            tone_score = -1
        else:
            tone_score = -3
            if "chief" in role or "ceo" in role: tone_score -= 2
        action_direction = "SELL"
        action_word = "sold"
    else:
        # if net value is zero but shares changed, it's likely a vesting event which is generally neutral in tone
        tone_score = 0
        action_direction = "ACQUIRE/VEST"
        action_word = "acquired (via RSU vesting)"

    plan_text = " under a pre-planned 10b5-1 trading plan" if is_10b5_1 and net_value < 0 else ""
    summary = f"{owner} ({role.title()}) of {ticker} {action_word} {total_shares_traded:,.0f} shares worth ${abs(net_value):,.2f}{plan_text}."

    return {
        "summary": summary,
        "transaction_date": transactions[0].get("date", "Unknown"),
        "tone_score": tone_score,
        "action_direction": action_direction
    }

# ==========================================
# 3. LLM Setup for 8-K
# ==========================================
try:
    llm = ChatOllama(model="llama3", temperature=0, base_url="http://localhost:11434", format="json")
except Exception:
    llm = None

def process_8k_llm(ticker: str, text: str) -> dict:
    if not llm: return None
    prompt = f"""
    Analyze SEC 8-K for {ticker}. Return STRICT JSON:
    {{"summary": "2 sentence summary.", "transaction_date": "YYYY-MM-DD", "tone_score": <Integer -5 to +5>, "topics": ["<Tag>"]}}
    Text: {text[:4000]}
    """
    try:
        response = llm.invoke(prompt)
        return json.loads(response.content)
    except:
        return None

# ==========================================
# 4. Concurrent Processor
# ==========================================
def process_single_record(raw_data: dict, global_processed: set):
    """for mutiple filings in the same day, we want to process them concurrently to speed up the LLM calls for 8-K analysis. The global registry is passed in to avoid duplicates."""
    meta = raw_data.get("metadata", {})
    accession_no = meta.get("accession_no")
    
    if accession_no in global_processed:
        return None  # Skip

    ticker = meta.get("ticker")
    form_type = meta.get("form_type")
    parsed_data = raw_data.get("parsed_data", {})
    
    result = None
    if form_type == "4" and parsed_data:
        result = process_form4_rules(ticker, parsed_data)
        topics = ["Insider Trading", "Form 4"]
    elif form_type == "8-K":
        text = parsed_data.get("full_markdown", "") or str(parsed_data.get("item_chunks", ""))
        result = process_8k_llm(ticker, text)
        topics = result.get("topics", ["8-K Event"]) if result else []
    
    if not result: return None

    final_record = {
        "text": result.get("summary", ""),
        "metadata": {
            **meta,
            "transaction_date": result.get("transaction_date", "Unknown"),
            "tone_score": result.get("tone_score", 0),
            "action_direction": result.get("action_direction", "NONE"),
            "topics": topics,
            "processed_at": datetime.now().isoformat()
        }
    }
    return (accession_no, final_record)

def run_pipeline():
    raw_files = glob.glob(os.path.join(RAW_FOLDER, "*.jsonl"))
    if not raw_files: return

    global_processed = load_global_registry()
    output_file = os.path.join(QDRANT_INPUT_FOLDER, "qdrant_ready.jsonl")
    
    all_lines = []
    for file_path in raw_files:
        with open(file_path, 'r', encoding='utf-8') as f:
            all_lines.extend([json.loads(line) for line in f])

    logger.info(f"Loaded {len(all_lines)} total records. Processing...")
    
    success_count = 0
    # max_workers can be tuned based on the expected number of filings and the latency of the LLM calls. For a small number of filings, 4-8 workers should be sufficient without overwhelming the system or the LLM. For larger batches, consider increasing but monitor resource usage.
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_single_record, line, global_processed): line for line in all_lines}
        
        for future in as_completed(futures):
            try:
                res = future.result()
                if res:
                    accession_no, final_record = res
                    # append to output file immediately to avoid data loss on crashes, and to allow partial results to be available even if the pipeline is interrupted
                    with open(output_file, 'a', encoding='utf-8') as f_out:
                        f_out.write(json.dumps(final_record, ensure_ascii=False) + '\n')
                    
                    # update global registry immediately to prevent duplicates in the same batch
                    save_single_to_registry(accession_no)
                    success_count += 1
                    logger.info(f"✅ Processed: {final_record['metadata']['ticker']} | Form {final_record['metadata']['form_type']} | Tone: {final_record['metadata']['tone_score']}")
            except Exception as e:
                logger.error(f"Thread Error: {e}")

    logger.info(f"🎉 Processing complete! {success_count} new records added to Qdrant ready file.")

if __name__ == "__main__":
    run_pipeline()