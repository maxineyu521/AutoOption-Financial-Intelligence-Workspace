# market_data_scraper.py
import yfinance as yf
from typing import List, Dict, Any

def scrape_market_data(tickers: List[str]) -> List[Dict[str, Any]]:
    """
    Use yfinance to get the latest market index information.
    :param tickers: List of stock/index codes (e.g., '^GSPC', '^VIX').
    :return: List of documents.
    """
    print("Scraping latest market index data...")
    documents = []
    for ticker_code in tickers:
        try:
            ticker = yf.Ticker(ticker_code)
            hist = ticker.history(period="5d") # Get last 5 days of data
            info = ticker.info
            
            name = info.get('shortName', ticker_code)
            last_close = hist['Close'].iloc[-1]
            prev_close = hist['Close'].iloc[-2]
            change = last_close - prev_close
            change_pct = (change / prev_close) * 100

            content = (
                f"Market index {name} ({ticker_code}) latest update: "
                f"Latest closing price is {last_close:.2f}. "
                f"Compared to previous day, changed by {change:.2f} ({change_pct:.2f}%)."
            )

            documents.append({
                "page_content": content,
                "metadata": {
                    "source": "Yahoo Finance",
                    "ticker": ticker_code,
                    "name": name,
                    "text": content
                }
            })
        except Exception as e:
            print(f"Unable to get data for {ticker_code}: {e}")
            
    print(f"Market data scraping completed, obtained {len(documents)} records.")
    return documents

if __name__ == '__main__':
    market_docs = scrape_market_data(['^GSPC', '^IXIC', '^VIX'])
    print(market_docs)
