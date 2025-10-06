import yfinance as yf
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import pandas as pd
import os
import json
import time
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

class YFinanceClient:
    """Yahoo Finance client for real options chain data"""
    
    def __init__(self):
        self.session = None
        
    def get_stock_info(self, symbol: str) -> Dict[str, Any]:
        """Get basic stock information"""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            return {
                'symbol': symbol,
                'current_price': info.get('currentPrice', info.get('regularMarketPrice', 0)),
                'bid': info.get('bid', 0),
                'ask': info.get('ask', 0),
                'volume': info.get('volume', 0),
                'market_cap': info.get('marketCap', 0),
                'sector': info.get('sector', 'Unknown'),
                'industry': info.get('industry', 'Unknown')
            }
        except Exception as e:
            logger.error(f"Error getting stock info for {symbol}: {e}")
            return {}
    
    def get_options_expirations(self, symbol: str) -> List[str]:
        """Get available options expiration dates"""
        try:
            ticker = yf.Ticker(symbol)
            expirations = ticker.options
            
            if not expirations:
                logger.warning(f"No options expirations found for {symbol}")
                return []
            
            # Convert to string format and sort
            exp_dates = [str(exp) for exp in expirations]
            exp_dates.sort()
            
            logger.info(f"Found {len(exp_dates)} expiration dates for {symbol}")
            return exp_dates
            
        except Exception as e:
            logger.error(f"Error getting options expirations for {symbol}: {e}")
            return []
    
    def get_options_chain(self, symbol: str, expiration_date: str = None) -> Dict[str, Any]:
        """Get options chain for a symbol"""
        try:
            ticker = yf.Ticker(symbol)
            
            # Get available expirations if not provided
            if not expiration_date:
                expirations = ticker.options
                if not expirations:
                    logger.warning(f"No options available for {symbol}")
                    return {}
                # Use the nearest expiration
                expiration_date = str(expirations[0])
            
            # Get options chain
            options_chain = ticker.option_chain(expiration_date)
            
            if options_chain is None:
                logger.warning(f"No options chain found for {symbol} on {expiration_date}")
                return {}
            
            calls = options_chain.calls
            puts = options_chain.puts
            
            # Process calls
            calls_data = []
            if not calls.empty:
                for _, row in calls.iterrows():
                    calls_data.append({
                        'strike': float(row['strike']),
                        'bid': float(row['bid']) if pd.notna(row['bid']) else 0,
                        'ask': float(row['ask']) if pd.notna(row['ask']) else 0,
                        'last_price': float(row['lastPrice']) if pd.notna(row['lastPrice']) else 0,
                        'volume': int(row['volume']) if pd.notna(row['volume']) else 0,
                        'open_interest': int(row['openInterest']) if pd.notna(row['openInterest']) else 0,
                        'implied_volatility': float(row['impliedVolatility']) if pd.notna(row['impliedVolatility']) else 0
                    })
            
            # Process puts
            puts_data = []
            if not puts.empty:
                for _, row in puts.iterrows():
                    puts_data.append({
                        'strike': float(row['strike']),
                        'bid': float(row['bid']) if pd.notna(row['bid']) else 0,
                        'ask': float(row['ask']) if pd.notna(row['ask']) else 0,
                        'last_price': float(row['lastPrice']) if pd.notna(row['lastPrice']) else 0,
                        'volume': int(row['volume']) if pd.notna(row['volume']) else 0,
                        'open_interest': int(row['openInterest']) if pd.notna(row['openInterest']) else 0,
                        'implied_volatility': float(row['impliedVolatility']) if pd.notna(row['impliedVolatility']) else 0
                    })
            
            return {
                'symbol': symbol,
                'expiration_date': expiration_date,
                'calls': calls_data,
                'puts': puts_data,
                'total_calls': len(calls_data),
                'total_puts': len(puts_data)
            }
            
        except Exception as e:
            logger.error(f"Error getting options chain for {symbol}: {e}")
            return {}
    
    def get_liquid_strikes(self, symbol: str, expiration_date: str = None, option_type: str = 'both', min_volume: int = 10) -> List[float]:
        """Get liquid strike prices based on volume and open interest"""
        try:
            options_chain = self.get_options_chain(symbol, expiration_date)
            if not options_chain:
                return []
            
            liquid_strikes = []
            
            if option_type in ['call', 'both']:
                for call in options_chain.get('calls', []):
                    if call['volume'] >= min_volume or call['open_interest'] >= min_volume:
                        liquid_strikes.append(call['strike'])
            
            if option_type in ['put', 'both']:
                for put in options_chain.get('puts', []):
                    if put['volume'] >= min_volume or put['open_interest'] >= min_volume:
                        liquid_strikes.append(put['strike'])
            
            # Remove duplicates and sort
            liquid_strikes = sorted(list(set(liquid_strikes)))
            logger.info(f"Found {len(liquid_strikes)} liquid strikes for {symbol}")
            
            return liquid_strikes
            
        except Exception as e:
            logger.error(f"Error getting liquid strikes for {symbol}: {e}")
            return []
    
    def get_atm_strikes(self, symbol: str, expiration_date: str = None, count: int = 5) -> List[float]:
        """Get at-the-money and near-the-money strikes"""
        try:
            # Get current stock price
            stock_info = self.get_stock_info(symbol)
            current_price = stock_info.get('current_price', 0)
            
            if current_price == 0:
                logger.warning(f"Could not get current price for {symbol}")
                return []
            
            # Get options chain
            options_chain = self.get_options_chain(symbol, expiration_date)
            if not options_chain:
                return []
            
            all_strikes = []
            
            # Collect all strikes
            for call in options_chain.get('calls', []):
                all_strikes.append(call['strike'])
            for put in options_chain.get('puts', []):
                all_strikes.append(put['strike'])
            
            if not all_strikes:
                return []
            
            # Remove duplicates and sort
            all_strikes = sorted(list(set(all_strikes)))
            
            # Find strikes around current price
            atm_strikes = []
            for strike in all_strikes:
                if abs(strike - current_price) / current_price <= 0.1:  # Within 10% of current price
                    atm_strikes.append(strike)
            
            # Sort by distance from current price
            atm_strikes.sort(key=lambda x: abs(x - current_price))
            
            # Return top N strikes
            return atm_strikes[:count]
            
        except Exception as e:
            logger.error(f"Error getting ATM strikes for {symbol}: {e}")
            return []
    
    def get_volatility_data(self, symbol: str) -> Dict[str, float]:
        """Get volatility data for a symbol"""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            return {
                'implied_volatility': info.get('impliedVolatility', 0),
                'beta': info.get('beta', 0),
                '52_week_high': info.get('fiftyTwoWeekHigh', 0),
                '52_week_low': info.get('fiftyTwoWeekLow', 0),
                'average_volume': info.get('averageVolume', 0)
            }
            
        except Exception as e:
            logger.error(f"Error getting volatility data for {symbol}: {e}")
            return {}

# Initialize global client
yfinance_client = YFinanceClient()

# YouTube API Configuration
YOUTUBE_API_KEY = "AIzaSyD1hA6jbyKxj6x3m96FM87Dci_MBttsHBY"

# Financial keywords for channel search
FINANCIAL_KEYWORDS = [
    "finance", "investing", "stock market", "trading", "economics", 
    "business", "money", "investment", "stocks", "crypto", "bitcoin",
    "financial advice", "wealth", "retirement", "portfolio", "dividend",
    "options trading", "forex", "real estate investing", "personal finance",
    "financial education", "market analysis", "financial news", "banking",
    "insurance", "tax", "budgeting", "saving", "debt", "credit"
]

def get_youtube_service():
    """Initialize YouTube API service"""
    try:
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
        return youtube
    except Exception as e:
        print(f"Error initializing YouTube API: {e}")
        return None

def search_channels_by_keyword(youtube, keyword: str, max_results: int = 50) -> List[Dict[str, Any]]:
    """Search for channels by keyword"""
    channels = []
    
    try:
        # Search for channels
        search_response = youtube.search().list(
            part='snippet',
            q=keyword,
            type='channel',
            maxResults=max_results,
            order='relevance'
        ).execute()
        
        channel_ids = []
        for item in search_response.get('items', []):
            channel_id = item['id']['channelId']
            channel_ids.append(channel_id)
        
        if not channel_ids:
            return channels
        
        # Get detailed channel information
        channels_response = youtube.channels().list(
            part='snippet,statistics',
            id=','.join(channel_ids)
        ).execute()
        
        for channel in channels_response.get('items', []):
            stats = channel.get('statistics', {})
            snippet = channel.get('snippet', {})
            
            # Parse statistics
            subscriber_count = int(stats.get('subscriberCount', 0))
            view_count = int(stats.get('viewCount', 0))
            video_count = int(stats.get('videoCount', 0))
            
            channel_info = {
                'channel_id': channel['id'],
                'title': snippet.get('title', ''),
                'description': snippet.get('description', ''),
                'subscriber_count': subscriber_count,
                'view_count': view_count,
                'video_count': video_count,
                'published_at': snippet.get('publishedAt', ''),
                'country': snippet.get('country', ''),
                'keywords': keyword,
                'relevance_score': 0  # Will be calculated later
            }
            
            channels.append(channel_info)
            
    except HttpError as e:
        print(f"HTTP Error searching for keyword '{keyword}': {e}")
    except Exception as e:
        print(f"Error searching for keyword '{keyword}': {e}")
    
    return channels

def calculate_relevance_score(channel: Dict[str, Any]) -> float:
    """Calculate relevance score based on financial content indicators"""
    score = 0.0
    title = channel.get('title', '').lower()
    description = channel.get('description', '').lower()
    
    # Financial keywords in title (higher weight)
    financial_title_keywords = [
        'finance', 'investing', 'trading', 'stocks', 'market', 'money',
        'business', 'economics', 'wealth', 'investment', 'crypto', 'bitcoin'
    ]
    
    for keyword in financial_title_keywords:
        if keyword in title:
            score += 3.0
    
    # Financial keywords in description
    financial_desc_keywords = [
        'financial', 'investment', 'trading', 'stocks', 'market analysis',
        'personal finance', 'wealth building', 'portfolio', 'dividend',
        'options', 'forex', 'real estate', 'retirement planning'
    ]
    
    for keyword in financial_desc_keywords:
        if keyword in description:
            score += 1.0
    
    # Penalty for non-financial content
    non_financial_keywords = ['gaming', 'music', 'entertainment', 'comedy', 'vlog']
    for keyword in non_financial_keywords:
        if keyword in title or keyword in description:
            score -= 2.0
    
    return max(0, score)

def fetch_financial_channels(min_channels: int = 50) -> List[Dict[str, Any]]:
    """Fetch financial channels with detailed information"""
    print(f"🔍 Fetching financial YouTube channels (target: {min_channels})...")
    
    youtube = get_youtube_service()
    if not youtube:
        print("❌ Failed to initialize YouTube API")
        return []
    
    all_channels = []
    processed_keywords = set()
    
    # Search for channels using different financial keywords
    for keyword in FINANCIAL_KEYWORDS:
        if len(all_channels) >= min_channels * 2:  # Get more than needed for filtering
            break
            
        if keyword in processed_keywords:
            continue
            
        print(f"📊 Searching for keyword: '{keyword}'...")
        channels = search_channels_by_keyword(youtube, keyword, max_results=50)
        
        for channel in channels:
            # Calculate relevance score
            channel['relevance_score'] = calculate_relevance_score(channel)
            
            # Only include channels with some financial relevance
            if channel['relevance_score'] > 0:
                all_channels.append(channel)
        
        processed_keywords.add(keyword)
        
        # Rate limiting - YouTube API has quotas
        time.sleep(0.1)
    
    # Remove duplicates based on channel_id
    unique_channels = {}
    for channel in all_channels:
        channel_id = channel['channel_id']
        if channel_id not in unique_channels:
            unique_channels[channel_id] = channel
        else:
            # Keep the one with higher relevance score
            if channel['relevance_score'] > unique_channels[channel_id]['relevance_score']:
                unique_channels[channel_id] = channel
    
    # Convert back to list
    unique_channels_list = list(unique_channels.values())
    
    # Sort by relevance score, then by subscriber count, then by view count
    unique_channels_list.sort(
        key=lambda x: (x['relevance_score'], x['subscriber_count'], x['view_count']),
        reverse=True
    )
    
    # Take top channels
    top_channels = unique_channels_list[:min_channels]
    
    print(f"✅ Found {len(unique_channels_list)} unique financial channels")
    print(f"📈 Selected top {len(top_channels)} channels by relevance and popularity")
    
    return top_channels

def save_channels_to_markdown(channels: List[Dict[str, Any]], filename: str = "Youtube_channel_ID.md"):
    """Save channels to markdown file"""
    try:
        # Get the directory of the current file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(current_dir, 'tools', filename)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("# Financial YouTube Channels\n\n")
            f.write("Auto-generated list of financial YouTube channels sorted by relevance score, subscriber count, and view count.\n\n")
            f.write(f"**Generated on:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Total Channels:** {len(channels)}\n\n")
            
            # Statistics
            total_subscribers = sum(ch['subscriber_count'] for ch in channels)
            total_views = sum(ch['view_count'] for ch in channels)
            avg_relevance = sum(ch['relevance_score'] for ch in channels) / len(channels)
            
            f.write("## Statistics\n\n")
            f.write(f"- **Total Subscribers:** {total_subscribers:,}\n")
            f.write(f"- **Total Views:** {total_views:,}\n")
            f.write(f"- **Average Relevance Score:** {avg_relevance:.2f}\n\n")
            
            f.write("## Channel List\n\n")
            f.write("| # | Channel ID | Title | Subscribers | Views | Relevance |\n")
            f.write("|---|------------|-------|-------------|-------|----------|\n")
            
            for i, channel in enumerate(channels, 1):
                channel_id = channel['channel_id']
                title = channel['title']
                subscribers = channel['subscriber_count']
                views = channel['view_count']
                relevance = channel['relevance_score']
                
                f.write(f"| {i} | `{channel_id}` | {title} | {subscribers:,} | {views:,} | {relevance:.1f} |\n")
            
            f.write("\n## Raw Channel IDs\n\n")
            f.write("Copy these IDs for use in YouTube scraper:\n\n")
            
            for channel in channels:
                channel_id = channel['channel_id']
                title = channel['title']
                f.write(f"`{channel_id}`  <!-- {title} -->\n")
        
        print(f"💾 Saved {len(channels)} channels to {file_path}")
        return file_path
        
    except Exception as e:
        print(f"❌ Error saving channels to markdown: {e}")
        return None

def update_youtube_channel_ids():
    """Main function to fetch and save YouTube channel IDs"""
    print("🚀 YouTube Financial Channels Fetcher")
    print("=" * 50)
    
    # Fetch financial channels
    channels = fetch_financial_channels(min_channels=50)
    
    if not channels:
        print("❌ No channels found")
        return None
    
    # Print summary
    print(f"\n🏆 Top 10 Financial Channels:")
    print("=" * 80)
    
    for i, channel in enumerate(channels[:10], 1):
        title = channel['title']
        channel_id = channel['channel_id']
        subscribers = channel['subscriber_count']
        views = channel['view_count']
        relevance = channel['relevance_score']
        
        print(f"{i:2d}. {title}")
        print(f"    ID: {channel_id}")
        print(f"    Subscribers: {subscribers:,}")
        print(f"    Total Views: {views:,}")
        print(f"    Relevance Score: {relevance:.1f}")
        print()
    
    # Save to markdown file
    file_path = save_channels_to_markdown(channels, "Youtube_channel_ID.md")
    
    if file_path:
        print(f"✅ Successfully created YouTube channel ID file: {file_path}")
        return file_path
    else:
        print("❌ Failed to create YouTube channel ID file")
        return None
