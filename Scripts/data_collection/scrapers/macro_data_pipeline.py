import os
import yfinance as yf
import pandas as pd
from fredapi import Fred
from datetime import datetime
from dotenv import load_dotenv
import logging

load_dotenv()

# ==========================================
# 0. Dynamic Path & Logging Setup
# ==========================================
import os
import logging
from datetime import datetime

# 1. Setup Base Directories
BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
today_str = datetime.now().strftime("%Y-%m-%d")

# 2. Construct the Daily Log Folder: logs/YYYY-MM-DD/
# This creates a clean hierarchy for automated runs
DAILY_LOG_DIR = os.path.join(BASE_DIR, "logs", today_str)
DATA_DIR = os.path.join(BASE_DIR, "Data","2_Silver_Processed", "Macro_History", today_str)
Narratives_DIR = os.path.join(BASE_DIR, "Data","3_Gold_Semantic", "Macro_Narratives", today_str)
Agent_DIR = os.path.join(BASE_DIR, "Data", "Agent_Context")

# Ensure both directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DAILY_LOG_DIR, exist_ok=True)
os.makedirs(Narratives_DIR, exist_ok=True)
os.makedirs(Agent_DIR, exist_ok=True)


# 3. Define the Log File Path
# Since it's already in a dated folder, you can use a generic name or keep the date
log_file = os.path.join(DAILY_LOG_DIR, "macro_pipeline.log")

# 4. Standard Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

logger.info(f"Logging initialized. Current log stored in: {DAILY_LOG_DIR}")

class MacroDataPipeline:
    def __init__(self, data_dir=DATA_DIR, narratives_dir=Narratives_DIR, agent_dir=Agent_DIR):
        self.data_dir = data_dir
        self.narratives_dir = narratives_dir
        self.agent_dir = agent_dir

        fred_key = os.getenv("FRED_API_KEY")
        if not fred_key:
            raise ValueError("FRED_API_KEY not found in environment variables. Please set it in your .env file.")
        self.fred = Fred(api_key=fred_key)
        
        self.market_tickers = {
            "^GSPC": {"name": "S&P 500", "class": "Equity Index", "unit": "Points", "freq": "Daily"},
            "^IXIC": {"name": "NASDAQ", "class": "Equity Index", "unit": "Points", "freq": "Daily"},
            "^VIX": {"name": "VIX Volatility", "class": "Volatility Index", "unit": "Points", "freq": "Daily"},
            "DX-Y.NYB": {"name": "US Dollar Index", "class": "Currency", "unit": "Points", "freq": "Daily"},
            "GLD": {"name": "Gold ETF", "class": "Commodity ETF", "unit": "USD", "freq": "Daily"},
            "SLV": {"name": "Silver ETF", "class": "Commodity ETF", "unit": "USD", "freq": "Daily"}
        }
        
        self.fred_series = {
            "FEDFUNDS": {"name": "Effective Federal Funds Rate", "class": "Interest Rate", "unit": "%", "freq": "Monthly"},
            "CPIAUCSL": {"name": "CPI (Inflation)", "class": "Inflation", "unit": "Index", "freq": "Monthly"},
            "UNRATE": {"name": "Unemployment Rate", "class": "Labor Market", "unit": "%", "freq": "Monthly"}
        }

    def fetch_yfinance_data(self) -> list:
        logger.info("Fetching YFinance market data...")
        tickers = list(self.market_tickers.keys())
        today_str = datetime.now().strftime("%Y-%m-%d")
        records = []
        
        try:
            # threads=False to avoid potential issues with multi-threading in some environments, especially when run in Jupyter or certain servers
            data = yf.download(tickers, period="5d", progress=False, threads=False)
            close_data = data['Close'] if isinstance(data.columns, pd.MultiIndex) else data
            
            for ticker, meta in self.market_tickers.items():
                if ticker in close_data.columns:
                    series = close_data[ticker].dropna()
                    if len(series) >= 2:
                        last_date = series.index[-1].strftime("%Y-%m-%d")
                        last_price = float(series.iloc[-1])
                        prev_price = float(series.iloc[-2])
                        change_pct = ((last_price - prev_price) / prev_price) * 100
                        
                        records.append({
                            "retrieval_date": today_str,
                            "observation_date": last_date, 
                            "symbol": ticker,
                            "name": meta["name"],
                            "asset_class": meta["class"],
                            "value": last_price,
                            "unit": meta["unit"],
                            "frequency": meta["freq"],
                            "daily_change_pct": change_pct,
                            "mom_change_pct": None, # Market data is daily, so MoM/YoY doesn't apply here, but we keep the fields for uniformity in the final DataFrame
                            "yoy_change_pct": None
                        })
        except Exception as e:
            logger.error(f"YFinance fetching failed: {e}")
            
        return records

    def fetch_fred_data(self) -> list:
        logger.info("Fetching FRED macro economic data...")
        today_str = datetime.now().strftime("%Y-%m-%d")
        records = []
        
        for series_id, meta in self.fred_series.items():
            try:
                data = self.fred.get_series(series_id)
                data = data.dropna()
                
                if len(data) >= 13:
                    latest_value = float(data.iloc[-1])
                    observation_date = data.index[-1].strftime("%Y-%m-%d")
                    
                    # Calculate MoM and YoY changes using the last 13 data points (to ensure we have the previous month and previous year values)
                    prev_month_value = float(data.iloc[-2])
                    last_year_value = float(data.iloc[-13])
                    
                    mom_change = ((latest_value - prev_month_value) / prev_month_value) * 100 if prev_month_value != 0 else 0
                    yoy_change = ((latest_value - last_year_value) / last_year_value) * 100 if last_year_value != 0 else 0
                    
                    records.append({
                        "retrieval_date": today_str,
                        "observation_date": observation_date, 
                        "symbol": series_id,
                        "name": meta["name"],
                        "asset_class": meta["class"],
                        "value": latest_value,
                        "unit": meta["unit"],
                        "frequency": meta["freq"],
                        "daily_change_pct": None,
                        "mom_change_pct": mom_change,
                        "yoy_change_pct": yoy_change
                    })
            except Exception as e:
                logger.error(f"FRED fetched {series_id} failed: {e}")
                
        return records

    def run_daily_pipeline(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        all_records = self.fetch_yfinance_data() + self.fetch_fred_data()
        
        if not all_records:
            logger.error("No data fetched, pipeline aborting.")
            return

        # 1. generate Markdown for RAG context (LLM-friendly format)
        report_lines = [f"## 📊 Macro & Market Daily Snapshot"]
        report_lines.append(f"> **Generated on:** {today_str}")
        report_lines.append("> **Note to LLM:** Macro indicators lag behind market data. Use YoY/MoM changes to gauge economic momentum.\n")
        
        report_lines.append("### 📈 Market Data (Daily)")
        for rec in all_records:
            if rec['frequency'] == 'Daily':
                report_lines.append(f"- **[{rec['asset_class']}] {rec['name']} ({rec['symbol']})**: {rec['value']:.2f} {rec['unit']} | Change: **{rec['daily_change_pct']:+.2f}%** *(Observed: {rec['observation_date']})*")
                
        report_lines.append("\n### 🏛️ Macro Economic Indicators (Lagging)")
        for rec in all_records:
            if rec['frequency'] != 'Daily':
                report_lines.append(f"- **[{rec['asset_class']}] {rec['name']} ({rec['symbol']})**: {rec['value']:.2f} {rec['unit']} | MoM: **{rec['mom_change_pct']:+.2f}%** | YoY: **{rec['yoy_change_pct']:+.2f}%** *(Observed: {rec['observation_date']})*")

        rag_context_md = "\n".join(report_lines)

        # 2. Save the Markdown context to a file (for RAG retrieval and future reference)
        md_dir = os.path.join(self.narratives_dir)
        os.makedirs(md_dir, exist_ok=True)
        
        context_file = os.path.join(md_dir, f"macro_context_{today_str}.md")
        with open(context_file, "w", encoding="utf-8") as f:
            f.write(rag_context_md)
        logger.info(f"✅ RAG Markdown Context generated: {context_file}")
        
        # Additionally, save a copy as "latest_macro_context.md" for easy retrieval by the LLM without needing to know the date-specific filename
        latest_file = os.path.join(self.agent_dir, "latest_macro_context.md")
        with open(latest_file, "w", encoding="utf-8") as f:
            f.write(rag_context_md)

        # 3. Save the structured data to a Parquet file for potential future analysis or more complex RAG queries that might benefit from structured data instead of just markdown
        df = pd.DataFrame(all_records)
        cols = ['retrieval_date', 'observation_date', 'symbol', 'name', 'asset_class', 'value', 'unit', 'frequency', 'daily_change_pct', 'mom_change_pct', 'yoy_change_pct']
        df = df[cols]
        
        parquet_file = os.path.join(self.data_dir, f"macro_snapshot_{today_str}.parquet")
        df.to_parquet(parquet_file, index=False)
        logger.info(f"✅ Metadata Parquet saved: {parquet_file}")

if __name__ == "__main__":
    pipeline = MacroDataPipeline()
    pipeline.run_daily_pipeline()