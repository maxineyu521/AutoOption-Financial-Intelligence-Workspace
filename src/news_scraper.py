# news_scraper.py
import os
from dotenv import load_dotenv
from newsapi import NewsApiClient
from typing import List, Dict, Any

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

NEWS_API_KEY = os.getenv("NEWS_API_KEY")

def scrape_news_data(keywords: List[str], limit: int = 50) -> List[Dict[str, Any]]:
    """
    Use NewsAPI to get the latest financial news.
    :param keywords: List of keywords to search for.
    :param limit: Maximum number of articles to retrieve.
    :return: List of documents.
    """
    if not NEWS_API_KEY:
        print("Warning: NEWS_API_KEY not found, skipping news data scraping.")
        return []

    print("Scraping latest financial news...")
    newsapi = NewsApiClient(api_key=NEWS_API_KEY)
    query = " OR ".join(keywords)
    
    documents = []
    try:
        top_headlines = newsapi.get_everything(
            q=query,
            language='en',
            sort_by='publishedAt',
            page_size=limit
        )

        for article in top_headlines['articles']:
            title = article.get('title', '')
            description = article.get('description', '')
            source_name = article.get('source', {}).get('name', 'N/A')
            
            if not description: # Skip if description is empty
                continue

            content = f"News source: {source_name}\nTitle: {title}\nSummary: {description}"
            
            documents.append({
                "page_content": content,
                "metadata": {
                    "source": "NewsAPI",
                    "source_name": source_name,
                    "title": title,
                    "url": article.get('url', '#'),
                    "text": content
                }
            })
            
    except Exception as e:
        print(f"Error getting news from NewsAPI: {e}")
        # NewsAPI developer accounts in local environment can only search by keywords, not by source or domain
        if "source" in str(e).lower():
            print("Hint: NewsAPI developer accounts do not support filtering by source, please check your query parameters.")

    print(f"News data scraping completed, obtained {len(documents)} articles.")
    return documents

if __name__ == '__main__':
    news_docs = scrape_news_data(['stock market', 'inflation'], 20)
    if news_docs:
        print(news_docs[0])
