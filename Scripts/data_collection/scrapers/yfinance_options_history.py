import yfinance as yf
import logging
from typing import Dict, Any, List
from datetime import datetime
import pandas as pd
import numpy as np
import os
import time

# ==========================================
# 0. Dynamic Path & Logging Configuration
# ==========================================
# Resolve absolute paths based on the current script location
BASE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
DATA_DIR = os.path.join(BASE_DIR, "Data","2_Silver_Processed", "Options_Market_Data")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

today_str = datetime.now().strftime("%Y-%m-%d")
log_file = os.path.join(LOG_DIR, today_str,f"options_scraper_{today_str}.log")
os.makedirs(os.path.dirname(log_file), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class YFinanceClient:
    """
    Yahoo Finance Options & Market Data Client
    Optimized for RAG Tool Calling with derived financial metrics.
    """
    
    def __init__(self):
        self.request_delay = 1.0 # 1 second delay between requests to prevent IP ban
        
    def get_stock_info(self, symbol: str) -> Dict[str, Any]:
        """Fetch basic stock information and latest quote."""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            time.sleep(self.request_delay)
            
            return {
                'symbol': symbol,
                'current_price': info.get('currentPrice', info.get('regularMarketPrice', 0)),
                'volume': info.get('volume', 0),
                'market_cap': info.get('marketCap', 0),
            }
        except Exception as e:
            logger.error(f"Failed to fetch stock info for {symbol}: {e}")
            return {}
    
    def get_options_expirations(self, symbol: str) -> List[str]:
        """Fetch available option expiration dates."""
        try:
            ticker = yf.Ticker(symbol)
            expirations = ticker.options
            time.sleep(self.request_delay)
            
            if not expirations:
                logger.warning(f"No option expiration dates found for {symbol}")
                return []
            
            exp_dates = [str(exp) for exp in expirations]
            exp_dates.sort()
            return exp_dates
        except Exception as e:
            logger.error(f"Failed to fetch expirations for {symbol}: {e}")
            return []
    
    def get_options_chain(self, symbol: str, expiration_date: str = None) -> Dict[str, Any]:
        """Fetch the full options chain for a specific expiration date."""
        try:
            ticker = yf.Ticker(symbol)
            if not expiration_date:
                expirations = ticker.options
                if not expirations:
                    return {}
                expiration_date = str(expirations[0])
            
            options_chain = ticker.option_chain(expiration_date)
            time.sleep(self.request_delay)
            
            if options_chain is None:
                return {}
            
            def process_chain(df, opt_type):
                data = []
                if not df.empty:
                    for _, row in df.iterrows():
                        data.append({
                            'contract_symbol': str(row.get('contractSymbol', '')),
                            'strike': float(row['strike']),
                            'last_price': float(row['lastPrice']) if pd.notna(row['lastPrice']) else 0,
                            'bid': float(row['bid']) if pd.notna(row['bid']) else 0,
                            'ask': float(row['ask']) if pd.notna(row['ask']) else 0,
                            'volume': int(row['volume']) if pd.notna(row['volume']) else 0,
                            'open_interest': int(row['openInterest']) if pd.notna(row['openInterest']) else 0,
                            'implied_volatility': float(row['impliedVolatility']) if pd.notna(row['impliedVolatility']) else 0,
                            'in_the_money': bool(row.get('inTheMoney', False))
                        })
                return data
            
            return {
                'symbol': symbol,
                'expiration_date': expiration_date,
                'calls': process_chain(options_chain.calls, 'call'),
                'puts': process_chain(options_chain.puts, 'put')
            }
        except Exception as e:
            logger.error(f"Failed to fetch options chain for {symbol}: {e}")
            return {}

    def snapshot_daily_options_chain(self, symbol: str) -> bool:
        """
        Automated daily snapshot script with advanced derived metrics.
        """
        today = datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        
        # [Architecture Update 1]: Create daily subfolder
        daily_folder = os.path.join(DATA_DIR, today_str)
        os.makedirs(daily_folder, exist_ok=True)
        
        logger.info(f"Starting options snapshot for {symbol} ({today_str})...")
        
        # 1. Fetch underlying price
        stock_info = self.get_stock_info(symbol)
        current_price = stock_info.get('current_price', 0)
        if current_price == 0:
            logger.error(f"Unable to fetch current price for {symbol}. Snapshot aborted.")
            return False

        # 2. Fetch expirations
        expirations = self.get_options_expirations(symbol)
        if not expirations:
            return False

        all_options_data = []

        # 3. Loop through chains (Limit to near-term expirations)
        for exp in expirations[:10]:
            try:
                exp_date_obj = datetime.strptime(exp, "%Y-%m-%d")
                dte = (exp_date_obj - today).days
                if dte < 0: dte = 0

                chain = self.get_options_chain(symbol, exp)
                
                for call in chain.get('calls', []):
                    call.update({
                        'symbol': symbol, 'underlying_price': current_price,
                        'expiration': exp, 'dte': dte,
                        'option_type': 'call', 'snapshot_date': today_str
                    })
                    all_options_data.append(call)
                    
                for put in chain.get('puts', []):
                    put.update({
                        'symbol': symbol, 'underlying_price': current_price,
                        'expiration': exp, 'dte': dte,
                        'option_type': 'put', 'snapshot_date': today_str
                    })
                    all_options_data.append(put)
                    
            except Exception as e:
                logger.warning(f"Error processing expiration {exp} for {symbol}: {e}")
                continue
                
        if not all_options_data:
            logger.warning(f"No valid options data retrieved for {symbol}.")
            return False
            
        # 4. Convert to DataFrame for advanced vectorized calculations
        df = pd.DataFrame(all_options_data)
        
        # [Architecture Update 2]: Calculate Derived Metrics for LLM Reasoning
        # A. Moneyness Percentage: How far is the strike from the current price?
        df['moneyness_pct'] = (abs(df['strike'] - df['underlying_price']) / df['underlying_price']) * 100
        
        # B. Bid-Ask Spread Percentage: Measure of liquidity cost (safely avoid division by zero)
        df['spread_pct'] = 0.0
        mask_ask_gt_zero = df['ask'] > 0
        df.loc[mask_ask_gt_zero, 'spread_pct'] = ((df.loc[mask_ask_gt_zero, 'ask'] - df.loc[mask_ask_gt_zero, 'bid']) / df.loc[mask_ask_gt_zero, 'ask']) * 100
        
        # C. Liquidity Flag: True if it has decent volume, open interest, and valid bid
        df['is_liquid'] = (df['volume'] >= 50) & (df['open_interest'] >= 100) & (df['bid'] > 0)
        
        # Reorder columns to group related metrics together
        cols = ['snapshot_date', 'symbol', 'underlying_price', 'contract_symbol', 'option_type', 
                'strike', 'expiration', 'dte', 'moneyness_pct', 
                'last_price', 'bid', 'ask', 'spread_pct', 
                'volume', 'open_interest', 'implied_volatility', 'in_the_money', 'is_liquid']
        df = df[[c for c in cols if c in df.columns]]
        
        # Save into the daily subfolder
        file_path = os.path.join(daily_folder, f"{symbol}_options_{today_str}.parquet")
        df.to_parquet(file_path, index=False)
        
        # Log how many contracts are actually highly liquid
        liquid_count = df['is_liquid'].sum()
        logger.info(f"✅ Archived {symbol} -> Total: {len(df)} | Liquid: {liquid_count} | Path: {file_path}")
        return True

if __name__ == "__main__":
    client = YFinanceClient()
    
    # [Architecture Update 3]: Added broad market baseline symbols
    # SPY (S&P 500 Large Cap), QQQ (Nasdaq Tech), IWM (Russell 2000 Small Cap)
    # GLD (Gold), SLV (Silver)
    target_symbols = ["SPY", "QQQ", "IWM", "GLD", "SLV"]
    
    logger.info(f"Initiating daily options data pipeline for {len(target_symbols)} symbols...")
    
    for symbol in target_symbols:
        client.snapshot_daily_options_chain(symbol)
        time.sleep(2)
        
    logger.info("Pipeline execution completed successfully.")