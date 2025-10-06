# fred_scraper.py
import os
from dotenv import load_dotenv
from fredapi import Fred
from typing import List, Dict, Any

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

FRED_API_KEY = os.getenv("FRED_API_KEY")

def scrape_fred_data(series_ids: List[str]) -> List[Dict[str, Any]]:
    """
    Get macroeconomic data from FRED.
    :param series_ids: List of FRED data series IDs (e.g., 'UNRATE').
    :return: List of documents.
    """
    if not FRED_API_KEY:
        print("Warning: FRED_API_KEY not found, skipping macroeconomic data scraping.")
        return []

    print("Scraping macroeconomic data from FRED...")
    fred = Fred(api_key=FRED_API_KEY)
    documents = []

    for series_id in series_ids:
        try:
            # Get latest data point
            data = fred.get_series_latest_release(series_id)
            info = fred.get_series_info(series_id)
            
            latest_value = data.iloc[-1]
            date = data.index[-1].strftime('%Y-%m-%d')
            title = info.get('title', series_id)
            units = info.get('units_short', '')

            content = (
                f"Macroeconomic indicator '{title}' ({series_id}): "
                f"Latest data shows that on {date}, the value was {latest_value} {units}."
            )
            
            documents.append({
                "page_content": content,
                "metadata": {
                    "source": "FRED",
                    "series_id": series_id,
                    "title": title,
                    "text": content
                }
            })
        except Exception as e:
            print(f"Unable to get FRED data series {series_id}: {e}")
            
    print(f"FRED data scraping completed, obtained {len(documents)} records.")
    return documents

if __name__ == '__main__':
    fred_docs = scrape_fred_data(['UNRATE', 'FEDFUNDS'])
    print(fred_docs)
