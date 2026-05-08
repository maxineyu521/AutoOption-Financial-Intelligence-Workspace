import os
import requests
import json
import time
from datetime import datetime, timezone
from newspaper import Article
from duckduckgo_search import DDGS
import warnings
import uuid
import re
import hashlib

from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

warnings.filterwarnings('ignore')

# ==========================================
# 0. Dynamic Path & Directory Setup
# ==========================================
# Ingestion-time sentiment / cleaning model. Canonical tag is `llama3:latest`
# (vanilla Meta Llama-3 8B). Respect `.env` overrides so ops can swap the
# model without code changes — see docs/LLM_Pool.md §1 for the two-tier
# model contract.
_INGESTION_MODEL = os.getenv(
    "OLLAMA_INGESTION_MODEL",
    os.getenv("OLLAMA_ROUTER_MODEL", "llama3:latest"),
)
_OPENAI_INGESTION_MODEL = os.getenv("OPENAI_INGESTION_MODEL", "gpt-4o-mini")
_INGESTION_LLM_PROVIDER = os.getenv("INGESTION_LLM_PROVIDER", "openai").lower()

if _INGESTION_LLM_PROVIDER == "ollama":
    print(f"Initializing local {_INGESTION_MODEL} as the data cleaning engine...")
    llm = ChatOllama(
        model=_INGESTION_MODEL,
        temperature=0,
        base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
    )
else:
    print(f"Initializing {_OPENAI_INGESTION_MODEL} as the data cleaning engine...")
    _openai_kwargs = {
        "model": _OPENAI_INGESTION_MODEL,
        "temperature": 0,
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "timeout": float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60")),
    }
    _openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if _openai_base_url:
        _openai_kwargs["base_url"] = _openai_base_url
    llm = ChatOpenAI(**_openai_kwargs)

# 1. Get current system date (Format: YYYY-MM-DD)
current_date = datetime.now().strftime("%Y-%m-%d")

# 2. Repo root: .../scrapers -> data_collection -> Scripts -> project root
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))

# 3. Inject date hierarchy for data: Project_Root/Data/news/YYYY-MM-DD/
DATA_FOLDER = os.path.join(PROJECT_ROOT, "Data", "1_Bronze_Raw", "News_Scrapes", current_date)

# 4. Define specific layers within the daily folder
RAW_FOLDER = os.path.join(DATA_FOLDER, "Raw")
FULL_TEXT_FOLDER = os.path.join(DATA_FOLDER, "Full_text")
QDRANT_INPUT_FOLDER = os.path.join(PROJECT_ROOT, "Data", "3_Gold_Semantic", "News_Qdrant", current_date)

# 5. Maintain logs in a date-partitioned folder (logs/YYYY-MM-DD/)
LOG_FOLDER = os.path.join(PROJECT_ROOT, "logs", current_date)

# 6. Safely create all directories (exist_ok=True prevents errors on rerun)
for folder in [RAW_FOLDER, FULL_TEXT_FOLDER, QDRANT_INPUT_FOLDER, LOG_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# Use a unified log file for all runs on the current date
LOG_FILE = os.path.join(LOG_FOLDER, f"scraper_{current_date}.log")

def log_print(message):
    print(message)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")

log_print("System Startup: Dynamic directories configured. Logs are ready.")

# ==========================================
# 1. Enhanced RAG Topics Definition
# ==========================================
MACRO_TOPICS = {
    # Strict quotes applied to eliminate ambiguous abbreviations
    "macro_central_banks": '("Federal Reserve" OR FOMC OR "Jerome Powell" OR "European Central Bank" OR "Bank of Japan" OR "interest rate" OR "monetary policy")',
    
    # Labor market and wage keywords strictly grouped
    "macro_inflation_employment": '(inflation OR CPI OR PCE OR payrolls OR "labor market" OR "wage growth")',
    
    # Fixed the fatal space=AND bug in GDELT syntax
    "macro_yields_dollar": '("yields" OR DXY OR "dollar index" OR "US dollar" OR "bond market" OR "Treasuries")',
    
    # Geopolitical and country-specific hotspots
    "macro_geopolitics_risk": '(geopolitics OR sanctions OR "Middle East" OR Ukraine OR Taiwan)'
}

ASSET_TOPICS = {
    # Precious metals spot with strict AND logic
    "asset_precious_metals_spot": '(gold OR silver) AND (price OR market OR trading OR bullion OR "safe haven")',
    
    # Derivatives terminology
    "asset_metals_derivatives": '(gold OR silver) (COMEX OR options OR futures OR ETF OR VIX)'
}

ALL_TOPICS = {**MACRO_TOPICS, **ASSET_TOPICS}

# ==========================================
# 1.5 Utility: Timestamp Parser
# ==========================================
def parse_to_timestamp(date_str):
    """Converts ISO 8601 or GDELT datetime strings to an integer Unix epoch."""
    if not date_str:
        return int(time.time())
    try:
        # Handle ISO format (e.g., 2026-04-08T21:47:44Z)
        if 'T' in date_str:
            clean_str = date_str.replace('Z', '+00:00')
            return int(datetime.fromisoformat(clean_str).timestamp())
        # Handle raw GDELT format (e.g., 20260408214744)
        else:
            dt = datetime.strptime(date_str[:14], "%Y%m%d%H%M%S")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        # Fallback to current time if parsing fails
        return int(time.time())

# ==========================================
# 2. Scraper & Text Extraction Logic
# ==========================================
def fetch_gdelt_metadata(query, max_retries=2):
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {"query": query, "mode": "artlist", "format": "json", "timespan": "48h", "maxrecords": 10, "sort": "DateDesc"}
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 429:
                time.sleep(10)
                continue
            response.raise_for_status()
            return response.json().get('articles', [])
        except Exception:
            time.sleep(5)
    return []

def _download_and_parse(url):
    try:
        article = Article(url)
        article.download()
        article.parse()
        return article.text
    except Exception:
        return None

def scrape_full_text(url, title=None):
    text = _download_and_parse(url)
    if text and len(text) > 150:
        return text, "direct_success"
    if title:
        try:
            with DDGS() as ddgs:
                search_query = f'"{title}" site:finance.yahoo.com'
                results = list(ddgs.text(search_query, max_results=1))
                if results and 'href' in results[0]:
                    yahoo_url = results[0]['href']
                    fallback_text = _download_and_parse(yahoo_url)
                    if fallback_text and len(fallback_text) > 150:
                        return fallback_text, "fallback_success"
        except Exception:
            pass
    return None, "failed"

# ==========================================
# 3. LLM Extraction, Translation & Scoring
# ==========================================
def llm_extract_and_score(full_text, original_title):
    truncated_text = full_text[:4000] 
    prompt = f"""
    You are an expert quantitative macro-financial analyst evaluating news for a Gold/Silver Options trading desk.
    
    CRITICAL INSTRUCTIONS:
    1. If the text is in a foreign language, TRANSLATE the concepts to English.
    2. If the text is a cookie consent, 404 error, or completely unrelated to finance/macro/geopolitics, reply ONLY with: INVALID_CONTENT.
    3. All your outputs MUST be in English.

    Format your output EXACTLY like this:
    ENGLISH_TITLE: [Translate the original title to English if foreign. If English, just copy it.]
    TONE_SCORE: [Assign an integer from -5 to +5 based on the expected impact on Gold/Silver prices]
    ENTITIES: [Provide a comma-separated list of 3-5 key entities explicitly mentioned]
    IMPACTED_ASSETS: [Comma-separated list of broadly affected asset classes, e.g., Equities, Gold, Oil, USD]
    VOLATILITY_IMPLICATION: [State ONLY 'Increase', 'Decrease', or 'Neutral' based on how this event impacts market fear/VIX]
    SUMMARY: [Write a dense, 2-3 sentence financial summary. EXPLAIN the macro transmission mechanism concisely.]

    TONE SCORING RUBRIC (Impact on GOLD/SILVER):
    +3 to +5 (Bullish): Fed cutting rates, inflation rising, USD weakening, geopolitical conflicts escalating.
    -3 to -5 (Bearish): Fed hiking rates, inflation cooling, USD strengthening, conflicts resolving.
    0 (Neutral): Market commentary without clear directional macro drivers.

    Original Title: {original_title}
    Article Text:
    {truncated_text}
    """
    try:
        return llm.invoke(prompt).content.strip()
    except Exception as e:
        log_print(f"LLM Error: {e}")
        return "INVALID_CONTENT"

def parse_llm_response(response_text, original_title):
    if "INVALID_CONTENT" in response_text or "SUMMARY:" not in response_text:
        return None, 0, [], [], "Neutral", None
        
    try:
        eng_title = original_title
        title_match = re.search(r"ENGLISH_TITLE:\s*(.*?)\n", response_text, re.IGNORECASE)
        if title_match: eng_title = title_match.group(1).strip()
            
        score_match = re.search(r"TONE_SCORE:\s*([-+]?\d+)", response_text, re.IGNORECASE)
        tone_score = int(score_match.group(1)) if score_match else 0
        
        entities = []
        entity_match = re.search(r"ENTITIES:\s*(.*?)\n", response_text, re.IGNORECASE)
        if entity_match:
            raw_entities = entity_match.group(1).split(',')
            entities = [e.strip() for e in raw_entities if e.strip()]

        impacted_assets = []
        asset_match = re.search(r"IMPACTED_ASSETS:\s*(.*?)\n", response_text, re.IGNORECASE)
        if asset_match and "none" not in asset_match.group(1).lower():
            raw_assets = asset_match.group(1).split(',')
            impacted_assets = [a.strip() for a in raw_assets if a.strip()]

        vol_match = re.search(r"VOLATILITY_IMPLICATION:\s*(.*?)\n", response_text, re.IGNORECASE)
        vol_implication = vol_match.group(1).strip() if vol_match else "Neutral"
        
        summary_start = response_text.find("SUMMARY:")
        dense_summary = response_text[summary_start:].replace("SUMMARY:", "").strip()
            
        return eng_title, tone_score, entities, impacted_assets, vol_implication, dense_summary
    except Exception as e:
        return original_title, 0, [], [], "Neutral", response_text.replace("SUMMARY:", "").strip()

# ==========================================
# 4. Main Pipeline Controller
# ==========================================
def process_and_save_to_jsonl(topic_name, query):
    log_print(f"\n{'='*40}\nProcessing topic: {topic_name}\n{'='*40}")
    articles = fetch_gdelt_metadata(query)
    
    if not articles:
        return

    # Implementing a robust deduplication mechanism using URL slugs and cleaned titles
    seen_signatures = set()
    unique_articles = []
    
    for item in articles:
        url = item.get('url', '')
        title = item.get('title', '').lower()
        
        # 1. Extract a URL slug (last part of the URL path) to use as a unique identifier
        slug_match = re.search(r'/([^/]+-[-a-zA-Z0-9]+)/?$', url)
        url_slug = slug_match.group(1) if slug_match else ""
        
        # 2. Clean the title (remove suffixes, non-alphanumeric characters)
        clean_title = re.sub(r'[^a-z0-9]', '', title.split('|')[0].strip())
        
        # 3. Generate a unique signature for the article
        article_signature = url_slug if url_slug else clean_title
        
        if article_signature not in seen_signatures:
            seen_signatures.add(article_signature)
            unique_articles.append(item)
        else:
            log_print(f"  [DEDUPLICATED] Skipping duplicate: {title[:40]}...")
            
    # After deduplication, we proceed with the unique set of articles
    articles = unique_articles
    
    if not articles:
        log_print(f"[{topic_name}] No data fetched for this query.")
        return

    raw_output_file = os.path.join(RAW_FOLDER, f"raw_gdelt_{topic_name}.jsonl")
    with open(raw_output_file, 'a', encoding='utf-8') as f_raw:
        for item in articles:
            f_raw.write(json.dumps(item, ensure_ascii=False) + '\n')

    full_text_output_file = os.path.join(FULL_TEXT_FOLDER, f"full_text_{topic_name}.jsonl")
    qdrant_output_file = os.path.join(QDRANT_INPUT_FOLDER, f"qdrant_{topic_name}_processed.jsonl")
    
    stats = {"total": len(articles), "scraped": 0, "llm_passed": 0, "llm_discarded": 0}
    current_time_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    with open(full_text_output_file, 'a', encoding='utf-8') as f_full, \
         open(qdrant_output_file, 'a', encoding='utf-8') as f_qdrant:
        
        for item in articles:
            target_url = item.get('url')
            article_title = item.get('title', '')
            
            # Standardize date for Vector DB filtering
            publish_date = item.get('urldatetime', '')
            if not publish_date:
                publish_date = current_time_iso
            
            # Convert string date to Integer Unix Epoch for fast DB range filters
            publish_timestamp = parse_to_timestamp(publish_date)
                
            full_text, status = scrape_full_text(target_url, title=article_title)
            if status == "failed":
                continue
            stats["scraped"] += 1
            
            full_text_record = {
                "url": target_url,
                "title": article_title,
                "publish_date": publish_date,
                "full_text": full_text
            }
            f_full.write(json.dumps(full_text_record, ensure_ascii=False) + '\n')
            
            llm_response = llm_extract_and_score(full_text, article_title)
            
            # Unpack the newly expanded metadata fields
            english_title, tone_score, entities_list, impacted_assets, vol_implication, dense_summary = parse_llm_response(llm_response, article_title)
            
            if not dense_summary:
                log_print(f"  [DISCARDED] Invalid content: {article_title[:40]}...")
                stats["llm_discarded"] += 1
                continue
                
            stats["llm_passed"] += 1
            log_print(f"  [KEPT] Tone: {tone_score:2d} | Volatility: {vol_implication[:8]} | {english_title[:40]}...")
            
            # Clean vector text (Removed "Entities:" prefix to prevent embedding pollution)
            clean_vector_text = f"{english_title}. {dense_summary}"
            
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, target_url))
            qdrant_record = {
                "id": doc_id,
                "text": clean_vector_text, 
                "metadata": {
                    "topic": topic_name,
                    "title": english_title,
                    "original_title": article_title,
                    "publish_date": publish_date,
                    "publish_timestamp": publish_timestamp,
                    "source": item.get('domain', 'Unknown Source'),
                    "url": target_url,
                    "entities": entities_list,
                    "impacted_assets": impacted_assets,
                    "volatility_implication": vol_implication,
                    "llm_tone_score": tone_score
                }
            }
            f_qdrant.write(json.dumps(qdrant_record, ensure_ascii=False) + '\n')
            
    log_print(f"Topic [{topic_name}] -> Fetched: {stats['total']} | Scraped: {stats['scraped']} | Saved: {stats['llm_passed']} | Discarded: {stats['llm_discarded']}")
    log_print("-" * 40)

if __name__ == "__main__":
    for topic_name, query in ALL_TOPICS.items():
        process_and_save_to_jsonl(topic_name, query)
        time.sleep(5) 
    log_print("\n[+] All data cleaning and purification complete. Files archived successfully!")
