import os
import requests
import json
import time
from datetime import datetime
from newspaper import Article
from duckduckgo_search import DDGS
import warnings
from langchain_ollama import ChatOllama
import uuid

warnings.filterwarnings('ignore')

# ==========================================
# 0. 初始化配置
# ==========================================
print("正在初始化本地 Llama 3 模型作为数据清洗引擎...")
llm = ChatOllama(model="llama3", temperature=0)

DATA_FOLDER = "./Data/Qdrant_Input"
os.makedirs(DATA_FOLDER, exist_ok=True)

TOPICS = {
    "geopolitical": '(gold OR silver OR GLD OR SLV) AND (conflict OR war OR sanctions OR "Middle East")',
    "usd_index": '(gold OR silver OR GLD OR SLV) AND ("dollar index" OR DXY)',
    "macro_fed": '(gold OR silver OR GLD OR SLV) AND ("Federal Reserve" OR FOMC)',
    "macro_inflation": '(gold OR silver OR GLD OR SLV) AND (inflation OR CPI)',
    "macro_rates": '(gold OR silver OR GLD OR SLV) AND "interest rate"'
}

# ==========================================
# 1. 爬虫与原始数据保留
# ==========================================
def fetch_gdelt_metadata(query, max_retries=2):
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {"query": query, "mode": "artlist", "format": "json", "timespan": "24h", "maxrecords": 5, "sort": "DateDesc"}
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
# 2. LLM 智能提取与评分 (升级版)
# ==========================================
def llm_extract_and_score(full_text):
    """提取高质量摘要并赋予严格的 Tone Score"""
    truncated_text = full_text[:4000] 
    
    prompt = f"""
    You are an expert macro-financial analyst evaluating news for a Gold/Silver Options trading desk.
    Read the article below. 
    
    CRITICAL INSTRUCTION: If the text is in a foreign language (Spanish, Turkish, Arabic, etc.), TRANSLATE it to English internally before generating the summary.
    If the text is just a cookie consent notice, a 404 error, or completely lacks any news content, reply ONLY with: INVALID_CONTENT.

    Otherwise, provide your analysis in the EXACT format below:
    
    TONE_SCORE: [Assign an integer from -5 to +5 based on the impact on Gold prices]
    SUMMARY: [Write a dense, 3-4 sentence financial summary. You MUST include specific numbers, percentages, entities, and forward-looking market trends mentioned in the text. Maximize information retention.]

    TONE SCORING RUBRIC (Impact on GOLD):
    +3 to +5 (Bullish): Federal Reserve cutting rates, inflation rising, US Dollar weakening, geopolitical conflicts escalating, major central bank gold buying.
    -3 to -5 (Bearish): Federal Reserve hiking or holding rates high, inflation cooling, US Dollar strengthening, geopolitical tensions resolving.
    0 (Neutral): General market commentary without directional drivers, or unrelated content.

    Article:
    {truncated_text}
    """
    try:
        return llm.invoke(prompt).content.strip()
    except Exception as e:
        print(f"LLM Error: {e}")
        return "INVALID_CONTENT"

def parse_llm_response(response_text):
    if "INVALID_CONTENT" in response_text or "SUMMARY:" not in response_text:
        return None, None
    try:
        parts = response_text.split("SUMMARY:")
        score_str = parts[0].replace("TONE_SCORE:", "").strip()
        tone_score = int(score_str)
        summary_text = parts[1].strip()
        return tone_score, summary_text
    except Exception:
        return 0, response_text.replace("SUMMARY:", "").strip() # 退化处理：保留文本，分数为0

# ==========================================
# 3. 主控流程
# ==========================================
def process_and_save_to_jsonl(topic_name, query):
    print(f"\n{'='*40}\nProcessing topic: {topic_name}\n{'='*40}")
    articles = fetch_gdelt_metadata(query)
    
    if not articles:
        return

    # 1. 恢复：保存原始数据 (Raw Data)
    raw_output_file = os.path.join(DATA_FOLDER, f"raw_gdelt_{topic_name}.jsonl")
    with open(raw_output_file, 'a', encoding='utf-8') as f_raw:
        for item in articles:
            f_raw.write(json.dumps(item, ensure_ascii=False) + '\n')

    chunk_output_file = os.path.join(DATA_FOLDER, f"qdrant_{topic_name}_processed.jsonl")
    stats = {"total": len(articles), "scraped": 0, "llm_passed": 0, "llm_discarded": 0}
    
    with open(chunk_output_file, 'a', encoding='utf-8') as f_out:
        for item in articles:
            target_url = item.get('url')
            article_title = item.get('title', '')
            publish_date = item.get('urldatetime', '')
            
            # 抓取全文
            full_text, status = scrape_full_text(target_url, title=article_title)
            if status == "failed":
                continue
            stats["scraped"] += 1
            
            # LLM 处理
            llm_response = llm_extract_and_score(full_text)
            tone_score, dense_summary = parse_llm_response(llm_response)
            
            # 新的拦截逻辑：只拦截真正的垃圾页面
            if not dense_summary:
                print(f"  [丢弃] 页面无效/无内容: {article_title[:40]}...")
                stats["llm_discarded"] += 1
                continue
                
            stats["llm_passed"] += 1
            print(f"  [保留] Tone: {tone_score:2d} | {article_title[:40]}...")
            
            contextualized_text = (
                f"Document Title: {article_title}\n"
                f"Topic Category: {topic_name}\n"
                f"Publish Date: {publish_date}\n"
                f"LLM Gold Tone Score: {tone_score}\n"
                f"---\n"
                f"{dense_summary}"
            )
            
            doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, target_url))
            record = {
                "id": doc_id,
                "text": contextualized_text, 
                "metadata": {
                    "topic": topic_name,
                    "title": article_title,
                    "publish_date": publish_date,
                    "source": item.get('domain', 'Unknown Source'),
                    "url": target_url,
                    "llm_tone_score": tone_score
                }
            }
            f_out.write(json.dumps(record, ensure_ascii=False) + '\n')
            
    # 日志输出
    log_msg = (f"Topic [{topic_name}] -> Fetched: {stats['total']} | Scraped: {stats['scraped']} | "
               f"Saved: {stats['llm_passed']} | Discarded: {stats['llm_discarded']}\n")
    print("-" * 40 + "\n" + log_msg)
    with open(os.path.join(DATA_FOLDER, "scraper_execution.log"), 'a') as f_log:
        f_log.write(f"{datetime.now()}: {log_msg}")

if __name__ == "__main__":
    for topic_name, query in TOPICS.items():
        process_and_save_to_jsonl(topic_name, query)
        time.sleep(2) 
    print("\n[+] 所有数据清洗提纯完毕，可用于 Qdrant 检索！")