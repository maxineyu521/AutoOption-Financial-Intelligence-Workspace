# youtube_scraper.py
import os
import re
import string
from dotenv import load_dotenv
from typing import List, Dict, Any
from collections import Counter
import logging
import requests

# Delayed import to avoid environment issues
try:
    from googleapiclient.discovery import build
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        TranscriptsDisabled,
        NoTranscriptFound,
        NoTranscriptAvailable,
        CouldNotRetrieveTranscript,
        TooManyRequests,
    )
    YOUTUBE_APIS_AVAILABLE = True
except ImportError as e:
    print(f"Warning: YouTube APIs not available: {e}")
    YOUTUBE_APIS_AVAILABLE = False
    build = None
    YouTubeTranscriptApi = None
    TranscriptsDisabled = Exception
    NoTranscriptFound = Exception
    NoTranscriptAvailable = Exception
    CouldNotRetrieveTranscript = Exception
    TooManyRequests = Exception

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

# Financial keywords tracking
FINANCIAL_KEYWORDS = [
    # Macroeconomics
    "inflation", "deflation", "recession", "recovery", "growth", "gdp", "unemployment",
    "interest rates", "fed", "federal reserve", "monetary policy", "fiscal policy",
    
    # Market indicators
    "vix", "volatility", "spy", "qqq", "dow", "nasdaq", "s&p 500", "market cap",
    "pe ratio", "earnings", "revenue", "profit", "loss", "dividend",
    
    # Investment strategies
    "options", "calls", "puts", "strike", "expiration", "premium", "delta", "gamma",
    "stocks", "bonds", "etf", "mutual fund", "portfolio", "diversification",
    
    # Market sentiment
    "bullish", "bearish", "neutral", "optimistic", "pessimistic", "fear", "greed",
    "market sentiment", "investor confidence", "risk appetite",
    
    # Industry terms
    "earnings beat", "earnings miss", "guidance", "upgrade", "downgrade",
    "buy", "sell", "hold", "strong buy", "strong sell", "outperform", "underperform"
]

# Data cleaning patterns
CLEANING_PATTERNS = [
    (r'\[.*?\]', ''),  # remove content in square brackets
    (r'\(.*?\)', ''),  # remove content in parentheses
    (r'\d+:\d+', ''),  # remove timestamps
    (r'[^\w\s]', ' '),  # remove punctuation
    (r'\s+', ' '),  # collapse multiple spaces
]

def get_channel_videos_via_rss(channel_id: str, max_results: int = 10) -> List[Dict[str, str]]:
    """Fallback: fetch recent videos via YouTube RSS feed (no API key needed)."""
    try:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        resp = requests.get(feed_url, timeout=10)
        if resp.status_code != 200:
            return []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt': 'http://www.youtube.com/xml/schemas/2015'
        }
        items: List[Dict[str, str]] = []
        for entry in root.findall('atom:entry', ns)[:max_results]:
            vid_el = entry.find('yt:videoId', ns)
            title_el = entry.find('atom:title', ns)
            if vid_el is not None and title_el is not None:
                items.append({'video_id': vid_el.text, 'title': title_el.text})
        return items
    except Exception:
        return []


def get_channel_videos(channel_id: str, max_results: int = 10) -> List[Dict[str, str]]:
    if not YOUTUBE_APIS_AVAILABLE or not YOUTUBE_API_KEY:
        # Fallback: try RSS feed to list videos without Data API
        videos = get_channel_videos_via_rss(channel_id, max_results=max_results)
        if videos:
            print(f"Info: Using RSS feed for channel {channel_id} ({len(videos)} videos)")
            return videos
        # If RSS also fails, keep previous warnings
        if not YOUTUBE_APIS_AVAILABLE:
            print("Warning: YouTube APIs not available, skipping YouTube data scraping.")
        if not YOUTUBE_API_KEY:
            print("Warning: YOUTUBE_API_KEY not set, skipping YouTube data scraping.")
        return []
    
    try:
        # Disable discovery cache to avoid warnings
        youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY, cache_discovery=False)
    except Exception as e:
        print(f"Error initializing YouTube API: {e}")
        return []
    
    try:
        # Check that the channel exists
        request = youtube.channels().list(part="contentDetails", id=channel_id)
        response = request.execute()
        
        # Ensure response contains items
        if 'items' not in response or not response['items']:
            print(f"Warning: Channel {channel_id} not found or has no content")
            return []
        
        playlist_id = response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        
        # Fetch videos from the uploads playlist
        request = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=max_results)
        response = request.execute()
        
        # Ensure response contains items
        if 'items' not in response:
            print(f"Warning: No videos found in playlist for channel {channel_id}")
            return []
        
        return [
            {
                'video_id': item['contentDetails']['videoId'],
                'title': item['snippet'].get('title', ''),
                'description': item['snippet'].get('description', '')
            }
            for item in response.get('items', [])
        ]
        
    except Exception as e:
        print(f"Error getting videos for channel {channel_id}: {e}")
        # Provide more details for API quota errors
        if "quotaExceeded" in str(e):
            print("YouTube API quota exceeded. Please try again later or check your API usage.")
        elif "forbidden" in str(e).lower():
            print(f"Access forbidden for channel {channel_id}. Channel may be private or restricted.")
        return []


def filter_videos_with_captions(api_key: str, videos: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Use YouTube Data API v3 to keep only videos that declare captions available.
    This improves transcript hit rate but is not perfect.
    """
    if build is None or not videos or not api_key:
        return videos
    try:
        youtube = build('youtube', 'v3', developerKey=api_key, cache_discovery=False)
        filtered: List[Dict[str, str]] = []
        for i in range(0, len(videos), 50):
            batch = videos[i:i+50]
            ids = ",".join(v['video_id'] for v in batch)
            resp = youtube.videos().list(part="contentDetails,snippet", id=ids).execute()
            details = {it['id']: it for it in resp.get('items', [])}
            for v in batch:
                it = details.get(v['video_id'])
                if not it:
                    continue
                caption_flag = (it.get('contentDetails', {}).get('caption') == 'true')
                is_live = it.get('snippet', {}).get('liveBroadcastContent') in ('live', 'upcoming')
                if caption_flag and not is_live:
                    filtered.append(v)
        return filtered or videos
    except Exception:
        return videos


def video_has_transcript(video_id: str) -> bool:
    """Quick check if any transcript is available via list_transcripts."""
    if YouTubeTranscriptApi is None:
        return False
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        for _ in transcripts:
            return True
        return False
    except Exception:
        return False

def clean_transcript_text(text: str) -> str:
    """
    Clean transcript text: remove timestamps, punctuation issues, and common ASR artifacts
    """
    if not text:
        return ""
    
    # Apply cleaning patterns
    cleaned_text = text.lower()
    for pattern, replacement in CLEANING_PATTERNS:
        cleaned_text = re.sub(pattern, replacement, cleaned_text)
    
    # Remove common auto-generated subtitle artifacts
    common_errors = [
        r'\b(um|uh|ah|er|mm|hmm)\b',  # filler words
        r'\b(you know|like|so|well|right)\b',  # colloquialisms
        r'\b(transcriber|speaker|narrator)\b',  # transcriber tag
        r'\b\[.*?\]\b',  # square bracket content
        r'\b\(.*?\)\b',  # parenthesis content
    ]
    
    for error_pattern in common_errors:
        cleaned_text = re.sub(error_pattern, '', cleaned_text)
    
    # Final cleanup
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()
    
    return cleaned_text

def extract_financial_keywords(text: str) -> Dict[str, int]:
    """
    keywords
    """
    if not text:
        return {}
    
    text_lower = text.lower()
    keyword_counts = {}
    
    for keyword in FINANCIAL_KEYWORDS:
        # Count keywords Frequency
        count = len(re.findall(r'\b' + re.escape(keyword.lower()) + r'\b', text_lower))
        if count > 0:
            keyword_counts[keyword] = count
    
    return keyword_counts

def analyze_transcript_sentiment(text: str) -> Dict[str, Any]:
    """
    Analyze market sentiment of the transcript
    """
    if not text:
        return {"sentiment": "neutral", "confidence": 0.0}
    
    text_lower = text.lower()
    
    # Bullish keywords
    bullish_keywords = ["bullish", "optimistic", "growth", "recovery", "strong", "beat", "upgrade", "buy"]
    # Bearish keywords
    bearish_keywords = ["bearish", "pessimistic", "decline", "weak", "miss", "downgrade", "sell", "crash"]
    
    bullish_count = sum(1 for keyword in bullish_keywords if keyword in text_lower)
    bearish_count = sum(1 for keyword in bearish_keywords if keyword in text_lower)
    
    total_sentiment_words = bullish_count + bearish_count
    
    if total_sentiment_words == 0:
        return {"sentiment": "neutral", "confidence": 0.0}
    
    bullish_ratio = bullish_count / total_sentiment_words
    
    if bullish_ratio > 0.6:
        sentiment = "bullish"
    elif bullish_ratio < 0.4:
        sentiment = "bearish"
    else:
        sentiment = "neutral"
    
    confidence = abs(bullish_ratio - 0.5) * 2  # confidence in [0,1]
    
    return {
        "sentiment": sentiment,
        "confidence": confidence,
        "bullish_score": bullish_count,
        "bearish_score": bearish_count
    }

def get_transcript_with_fallback(video_id: str) -> Dict[str, Any]:
    """
    Get transcript prioritizing list_transcripts with language selection, translation, and backoff
    """
    if not YOUTUBE_APIS_AVAILABLE:
        print(f"      ❌ YouTube APIs not available for video {video_id}")
        return None
    
    def _fetch_text_from_transcript(transcript_obj) -> str:
        try:
            items = transcript_obj.fetch()
            return " ".join([it["text"] if isinstance(it, dict) else getattr(it, "text", "") for it in items])
        except TooManyRequests:

            import time
            for delay in (1, 2, 4):
                time.sleep(delay)
                try:
                    items = transcript_obj.fetch()
                    return " ".join([it["text"] if isinstance(it, dict) else getattr(it, "text", "") for it in items])
                except TooManyRequests:
                    continue
            raise

    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)


        try:
            t = transcripts.find_manually_created_transcript(["en", "en-US", "en-GB"])
            raw_text = _fetch_text_from_transcript(t)
            if raw_text and len(raw_text) > 50:
                return process_transcript_data(raw_text, video_id, "manual_en")
        except (NoTranscriptFound, NoTranscriptAvailable):
            pass


        try:
            t = transcripts.find_generated_transcript(["en", "en-US", "en-GB"])
            raw_text = _fetch_text_from_transcript(t)
            if raw_text and len(raw_text) > 50:
                return process_transcript_data(raw_text, video_id, "auto_en")
        except (NoTranscriptFound, NoTranscriptAvailable):
            pass


        try:
            available_languages = [tr.language_code for tr in transcripts]
        except Exception:
            available_languages = []

        for pref in ("manual_other_to_en", "auto_other_to_en"):
            try:
                if pref.startswith("manual"):
                    tr = transcripts.find_manually_created_transcript(available_languages)
                else:
                    tr = transcripts.find_generated_transcript(available_languages)
                tr_en = tr.translate("en")
                raw_text = _fetch_text_from_transcript(tr_en)
                if raw_text and len(raw_text) > 50:
                    return process_transcript_data(raw_text, video_id, pref)
            except (NoTranscriptFound, NoTranscriptAvailable, CouldNotRetrieveTranscript) as e:
                
                print(f"      ⚠️  {pref} failed for {video_id}: {e}")
                continue


        try:
            for tr in transcripts:
                try:
                    raw_text = _fetch_text_from_transcript(tr)
                    if raw_text and len(raw_text) > 50:
                        return process_transcript_data(raw_text, video_id, "any_available")
                except Exception:
                    continue
        except Exception:
            pass

        print(f"      ❌ No usable transcripts for video {video_id}")
        return None
            
    except TranscriptsDisabled:
        print(f"      ❌ Transcripts disabled for video {video_id}")
        return None
    except TooManyRequests:
        print(f"      ⚠️  Rate limited when listing transcripts for {video_id}; retrying with backoff")
        import time
        for delay in (1, 2, 4):
            time.sleep(delay)
            try:
                return get_transcript_with_fallback(video_id)
            except TooManyRequests:
                continue
        print(f"      ❌ Rate limit persists for {video_id}")
        return None
    except (NoTranscriptFound, NoTranscriptAvailable) as e:
        print(f"      ❌ No transcript available for video {video_id}: {e}")
        return None
    except CouldNotRetrieveTranscript as e:
        print(f"      ⚠️  Could not retrieve transcript for {video_id}: {e}; trying alternatives...")
        return try_alternative_transcript_methods(video_id)
    except Exception as e:
        error_msg = str(e)
        if "blocked" in error_msg.lower() or "ip" in error_msg.lower():
            print(f"      ⚠️  IP blockage suspected for {video_id}, trying alternative methods...")
            return try_alternative_transcript_methods(video_id)
        elif "not found" in error_msg.lower():
            print(f"      ❌ Video not found: {video_id}")
            return None
        elif "quota" in error_msg.lower():
            print(f"      ❌ API quota exceeded for video {video_id}")
            return None
        else:
            print(f"      ❌ Error getting transcript for video {video_id}: {error_msg}")
            return None


def try_alternative_transcript_methods(video_id: str) -> Dict[str, Any]:
    """
    Try alternative transcript methods (language enumeration, translation, backoff)
    """
    languages_to_try = ['en', 'en-US', 'en-GB', 'hi', 'es', 'de', 'fr', 'ja', 'ko', 'zh-Hans', 'zh-Hant']


    for lang in languages_to_try:
        try:
            lst = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang])
            raw_text = " ".join([it["text"] if isinstance(it, dict) else getattr(it, "text", "") for it in lst])
            if raw_text and len(raw_text) > 50:
                print(f"      ✅ Got transcript using language={lang} for video {video_id}")
                method = f"direct_{lang}"
                if lang.startswith('en'):
                    return process_transcript_data(raw_text, video_id, method)

                try:
                    transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
                    tr = transcripts.find_manually_created_transcript([lang]) if lang else None
                    if not tr:
                        tr = transcripts.find_generated_transcript([lang])
                    tr_en = tr.translate('en')
                    text_en = " ".join([it["text"] if isinstance(it, dict) else getattr(it, "text", "") for it in tr_en.fetch()])
                    if text_en and len(text_en) > 50:
                        return process_transcript_data(text_en, video_id, f"translate_{lang}_to_en")
                except Exception:
                    return process_transcript_data(raw_text, video_id, method)
        except Exception as e:
            print(f"      ⚠️  language={lang} failed for {video_id}: {e}")

    
    try:
        import time
        for delay in (1, 2, 4):
            time.sleep(delay)
            try:
                transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
                for tr in transcripts:
                    try:
                        items = tr.fetch()
                        raw_text = " ".join([it["text"] if isinstance(it, dict) else getattr(it, "text", "") for it in items])
                        if raw_text and len(raw_text) > 50:
                            return process_transcript_data(raw_text, video_id, f"retry_any_available_{delay}s")
                    except Exception:
                        continue
            except TooManyRequests:
                continue
    except Exception as e:
        print(f"      ⚠️  Backoff retry failed for {video_id}: {e}")
    
    print(f"      ❌ All alternative methods failed for video {video_id}")
    return None


def process_transcript_data(raw_text: str, video_id: str, method: str) -> Dict[str, Any]:
    """
    Process transcript data
    """
    # Cleaning
    cleaned_text = clean_transcript_text(raw_text)
    
    if not cleaned_text or len(cleaned_text) < 50:
        print(f"      ❌ Transcript too short for video {video_id} ({len(cleaned_text) if cleaned_text else 0} chars)")
        return None
    
    keyword_counts = extract_financial_keywords(cleaned_text)
    
    sentiment_analysis = analyze_transcript_sentiment(cleaned_text)
    
    return {
        "raw_text": raw_text,
        "cleaned_text": cleaned_text,
        "keyword_counts": keyword_counts,
        "sentiment_analysis": sentiment_analysis,
        "word_count": len(cleaned_text.split()),
        "financial_relevance": len(keyword_counts) / len(FINANCIAL_KEYWORDS) if FINANCIAL_KEYWORDS else 0,
        "transcript_method": method
    }


def get_transcript(video_id: str) -> Dict[str, Any]:
    """
    Get video transcript, clean and analyze it, with detailed error messages
    """
    return get_transcript_with_fallback(video_id)

def analyze_channel_topics(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyze channel topics and keyword frequency
    """
    if not documents:
        return {}
    
    # 合并所有关键词计数
    all_keywords = Counter()
    sentiment_scores = {"bullish": 0, "bearish": 0, "neutral": 0}
    total_financial_relevance = 0
    
    for doc in documents:
        metadata = doc.get("metadata", {})
        if "keyword_counts" in metadata:
            for keyword, count in metadata["keyword_counts"].items():
                all_keywords[keyword] += count
        
        if "sentiment_analysis" in metadata:
            sentiment = metadata["sentiment_analysis"].get("sentiment", "neutral")
            sentiment_scores[sentiment] += 1
        
        if "financial_relevance" in metadata:
            total_financial_relevance += metadata["financial_relevance"]
    
    # calculate financial_relevance
    avg_financial_relevance = total_financial_relevance / len(documents) if documents else 0
    
    return {
        "top_keywords": dict(all_keywords.most_common(20)),
        "sentiment_distribution": sentiment_scores,
        "average_financial_relevance": avg_financial_relevance,
        "total_videos_analyzed": len(documents)
    }

def load_channel_ids_from_file(file_path: str = None) -> List[str]:
    """
    Load channel IDs from the Youtube_channel_ID file
    """
    if file_path is None:
        # Default file path
        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(current_dir, 'tools', 'Youtube_channel_ID')
    
    channel_ids = []
    
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # Strip comment part
                    if '<!--' in line:
                        line = line.split('<!--')[0].strip()
                    # Only accept valid YouTube channel ID format
                    if line and len(line) == 24 and line.startswith('UC'):
                        channel_ids.append(line)
            
            print(f"✅ Loaded {len(channel_ids)} channel IDs from {file_path}")
        except Exception as e:
            print(f"❌ Error reading channel IDs from {file_path}: {e}")
    else:
        print(f"❌ Channel ID file not found: {file_path}")
    
    return channel_ids


def scrape_youtube_data(channel_ids: List[str] = None, videos_per_channel: int = 10, min_transcripts: int = 50) -> List[Dict[str, Any]]:
    """
    Extract, clean, and analyze transcripts from YouTube channels.
    If channel_ids is None, load from Youtube_channel_ID file.
    No restrictions; provide detailed error messages.
    """
    # Load from file if no channel_ids provided
    if channel_ids is None:
        channel_ids = load_channel_ids_from_file()
    
    if not channel_ids:
        print("❌ No channel IDs available for scraping")
        return []
    
    print(f"Scraping data from {len(channel_ids)} YouTube channels...")
    print(f"Target: At least {min_transcripts} valid transcripts")
    print("Note: No restrictions applied - will process all available channels")
    
    documents = []
    successful_channels = 0
    failed_channels = 0
    channels_with_no_videos = 0
    channels_with_no_transcripts = 0
    all_keywords = Counter()
    
    for i, channel_id in enumerate(channel_ids):
        print(f"Processing channel {i+1}/{len(channel_ids)}: {channel_id}")
        
        try:
            # Fetch channel videos
            videos = get_channel_videos(channel_id, max_results=videos_per_channel)
            
            if not videos:
                failed_channels += 1
                channels_with_no_videos += 1
                print(f"  ❌ No videos found for channel {channel_id}")
                continue
                
            print(f"  📹 Found {len(videos)} videos")
            channel_documents = 0
            channel_keywords = Counter()
            videos_without_transcripts = 0
            
            # Prefer videos that actually have captions
            videos = filter_videos_with_captions(YOUTUBE_API_KEY, videos)

            for j, video in enumerate(videos):
                print(f"    Processing video {j+1}/{len(videos)}: {video['title'][:50]}...")
                
                # Skip early when no transcript is exposed
                if not video_has_transcript(video['video_id']):
                    # Fallback: store title + description for context
                    desc = video.get('description', '')
                    fallback_text = clean_transcript_text(desc) if desc else ''
                    content = f"Video title: {video['title']}\n\nDescription: {fallback_text}" if fallback_text else f"Video title: {video['title']}"
                    documents.append({
                        "page_content": content,
                        "metadata": {
                            "source": "YouTube",
                            "channel_id": channel_id,
                            "title": video['title'],
                            "url": f"https://www.youtube.com/watch?v={video['video_id']}",
                            "text": content,
                            "raw_text": desc,
                            "cleaned_text": fallback_text,
                            "transcript_method": "fallback_description"
                        }
                    })
                    videos_without_transcripts += 1
                    continue
                transcript_data = get_transcript(video['video_id'])
                
                if transcript_data is None:
                    # Fallback: title + description
                    desc = video.get('description', '')
                    fallback_text = clean_transcript_text(desc) if desc else ''
                    content = f"Video title: {video['title']}\n\nDescription: {fallback_text}" if fallback_text else f"Video title: {video['title']}"
                    documents.append({
                        "page_content": content,
                        "metadata": {
                            "source": "YouTube",
                            "channel_id": channel_id,
                            "title": video['title'],
                            "url": f"https://www.youtube.com/watch?v={video['video_id']}",
                            "text": content,
                            "raw_text": desc,
                            "cleaned_text": fallback_text,
                            "transcript_method": "fallback_description"
                        }
                    })
                    videos_without_transcripts += 1
                    print(f"      ❌ No transcript available for video {video['video_id']} (saved fallback)")
                    continue
                
                # Accept all transcripts (no financial relevance threshold)
                if transcript_data.get("financial_relevance", 0) >= 0:
                    
                    # Build enriched content
                    content = f"Video title: {video['title']}\n\nCleaned transcript: {transcript_data['cleaned_text']}"
                    
                    # Add keyword info
                    if transcript_data.get("keyword_counts"):
                        content += f"\n\nKey financial terms: {', '.join(transcript_data['keyword_counts'].keys())}"
                    
                    # Add sentiment info
                    sentiment = transcript_data.get("sentiment_analysis", {})
                    if sentiment:
                        content += f"\n\nMarket sentiment: {sentiment.get('sentiment', 'neutral')} (confidence: {sentiment.get('confidence', 0):.2f})"
                    
                    documents.append({
                        "page_content": content,
                        "metadata": {
                            "source": "YouTube", 
                            "channel_id": channel_id, 
                            "title": video['title'],
                            "url": f"https://www.youtube.com/watch?v={video['video_id']}",
                            "text": content,
                            "raw_text": transcript_data.get("raw_text", ""),
                            "cleaned_text": transcript_data.get("cleaned_text", ""),
                            "keyword_counts": transcript_data.get("keyword_counts", {}),
                            "sentiment_analysis": transcript_data.get("sentiment_analysis", {}),
                            "word_count": transcript_data.get("word_count", 0),
                            "financial_relevance": transcript_data.get("financial_relevance", 0)
                        }
                    })
                    
                    # Accumulate keyword counts
                    if transcript_data.get("keyword_counts"):
                        for keyword, count in transcript_data["keyword_counts"].items():
                            channel_keywords[keyword] += count
                            all_keywords[keyword] += count
                    
                    channel_documents += 1
                    print(f"      ✅ Transcript collected (relevance: {transcript_data.get('financial_relevance', 0):.3f})")
            
            # Per-channel summary
            if channel_documents > 0:
                successful_channels += 1
                print(f"  ✅ Successfully processed {channel_documents} transcripts from channel {channel_id}")
                print(f"     - Videos without transcripts: {videos_without_transcripts}")
                print(f"     - Top keywords: {', '.join([k for k, v in channel_keywords.most_common(5)])}")
            else:
                failed_channels += 1
                if videos_without_transcripts > 0:
                    channels_with_no_transcripts += 1
                    print(f"  ❌ Channel {channel_id} has {len(videos)} videos but no transcripts available")
                else:
                    print(f"  ❌ No valid transcripts found for channel {channel_id}")
                
        except Exception as e:
            failed_channels += 1
            print(f"  ❌ Error processing channel {channel_id}: {e}")
            continue
    
    # Global topic analysis
    if documents:
        topic_analysis = analyze_channel_topics(documents)
        print(f"\n📊 YouTube Data Analysis:")
        print(f"  - Top financial keywords: {', '.join([k for k, v in all_keywords.most_common(10)])}")
        print(f"  - Sentiment distribution: {topic_analysis.get('sentiment_distribution', {})}")
        print(f"  - Average financial relevance: {topic_analysis.get('average_financial_relevance', 0):.2f}")
    
    print(f"\n📈 YouTube Data Scraping Summary:")
    print(f"  - Total channels processed: {len(channel_ids)}")
    print(f"  - Successful channels: {successful_channels}")
    print(f"  - Failed channels: {failed_channels}")
    print(f"    - Channels with no videos: {channels_with_no_videos}")
    print(f"    - Channels with no transcripts: {channels_with_no_transcripts}")
    print(f"  - Total documents obtained: {len(documents)}")
    print(f"  - Total unique keywords found: {len(all_keywords)}")
    
    if len(documents) < min_transcripts:
        print(f"⚠️  Warning: Only collected {len(documents)} transcripts, target was {min_transcripts}")
        print(f"   This is likely due to:")
        print(f"   - YouTube blocking transcript requests from cloud IPs")
        print(f"   - Many videos not having transcripts enabled")
        print(f"   - API quota limitations")
    
    return documents
