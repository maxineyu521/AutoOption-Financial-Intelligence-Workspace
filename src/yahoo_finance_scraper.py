import os
import re
import requests
from typing import List, Dict, Any

from dotenv import load_dotenv

try:
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        TranscriptsDisabled,
        NoTranscriptFound,
        CouldNotRetrieveTranscript,
    )
except Exception:
    YouTubeTranscriptApi = None  # type: ignore
    TranscriptsDisabled = Exception  # type: ignore
    NoTranscriptFound = Exception  # type: ignore
    CouldNotRetrieveTranscript = Exception  # type: ignore


YAHOO_FINANCE_CHANNEL_ID = "UCEAZeUIeJs0IjQiqTCdVSIg"


def list_channel_videos_via_rss(channel_id: str, max_results: int = 20) -> List[Dict[str, str]]:
    try:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        resp = requests.get(feed_url, timeout=10)
        if resp.status_code != 200:
            return []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt': 'http://www.youtube.com/xml/schemas/2015',
            'media': 'http://search.yahoo.com/mrss/'
        }
        videos: List[Dict[str, str]] = []
        for entry in root.findall('atom:entry', ns)[:max_results]:
            vid_el = entry.find('yt:videoId', ns)
            title_el = entry.find('atom:title', ns)
            # media:group/media:description may hold a lightweight description
            desc_el = entry.find('media:group/media:description', ns)
            if vid_el is not None and title_el is not None:
                videos.append({
                    'video_id': vid_el.text,
                    'title': title_el.text,
                    'description': (desc_el.text if desc_el is not None else '')
                })
        return videos
    except Exception:
        return []


def clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.lower()
    cleaned = re.sub(r"\[.*?\]", "", cleaned)
    cleaned = re.sub(r"\(.*?\)", "", cleaned)
    cleaned = re.sub(r"\d+:\d+", "", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def fetch_transcript_best_effort(video_id: str) -> Dict[str, Any]:
    if YouTubeTranscriptApi is None:
        return {}

    def _fetch_text(t) -> str:
        items = t.fetch()
        return " ".join([it.get("text", "") if isinstance(it, dict) else getattr(it, "text", "") for it in items])

    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        # 1) Manual EN
        try:
            t = transcripts.find_manually_created_transcript(["en", "en-US", "en-GB"])
            text = _fetch_text(t)
            if len(text) > 50:
                return {"method": "manual_en", "text": text}
        except (NoTranscriptFound,):
            pass
        # 2) Auto EN
        try:
            t = transcripts.find_generated_transcript(["en", "en-US", "en-GB"])
            text = _fetch_text(t)
            if len(text) > 50:
                return {"method": "auto_en", "text": text}
        except (NoTranscriptFound,):
            pass
        # 3) Translate others to EN
        try:
            langs = [tr.language_code for tr in transcripts]
        except Exception:
            langs = []
        for pref in ("manual_other_to_en", "auto_other_to_en"):
            try:
                tr = transcripts.find_manually_created_transcript(langs) if pref.startswith("manual") else transcripts.find_generated_transcript(langs)
                tr_en = tr.translate('en')
                text = _fetch_text(tr_en)
                if len(text) > 50:
                    return {"method": pref, "text": text}
            except (NoTranscriptFound, CouldNotRetrieveTranscript):
                continue
        # 4) Any available
        for tr in transcripts:
            try:
                text = _fetch_text(tr)
                if len(text) > 50:
                    return {"method": "any_available", "text": text}
            except Exception:
                continue
        return {}
    except TranscriptsDisabled:
        return {}
    except Exception:
        return {}


def scrape_yahoo_finance_transcripts(videos_per_channel: int = 20, verbose: bool = True) -> List[Dict[str, Any]]:
    """Fetch Yahoo Finance videos via RSS, pull transcripts, clean and build documents.

    Returns empty list if no transcripts are available or dependency is missing.
    """
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

    videos = list_channel_videos_via_rss(YAHOO_FINANCE_CHANNEL_ID, max_results=videos_per_channel)
    if verbose:
        print(f"Yahoo Finance RSS videos fetched: {len(videos)}")
    documents: List[Dict[str, Any]] = []
    for v in videos:
        vid = v.get('video_id', '')
        title = v.get('title', '')
        description = v.get('description', '')
        data = fetch_transcript_best_effort(vid)
        if not data or not data.get('text'):
            # Fallback: store title + description so we still add lightweight context
            if verbose:
                print(f"  - No transcript for video {vid} ({title[:60]}...) - saving fallback")
            fallback_text = clean_text(description) if description else ''
            page = f"Video title: {title}\n\nDescription: {fallback_text}" if fallback_text else f"Video title: {title}"
            documents.append({
                "page_content": page,
                "metadata": {
                    "source": "YahooFinance",
                    "channel_id": YAHOO_FINANCE_CHANNEL_ID,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "text": page,
                    "raw_text": description,
                    "cleaned_text": fallback_text,
                    "transcript_method": "fallback_description"
                }
            })
            continue
        raw_text = data['text']
        cleaned = clean_text(raw_text)
        if len(cleaned) < 20:
            # Use description if transcript is too short
            if verbose:
                print(f"  - Transcript too short for {vid} ({len(cleaned)} chars) - saving description fallback")
            fallback_text = clean_text(description) if description else ''
            page = f"Video title: {title}\n\nDescription: {fallback_text}" if fallback_text else f"Video title: {title}"
            documents.append({
                "page_content": page,
                "metadata": {
                    "source": "YahooFinance",
                    "channel_id": YAHOO_FINANCE_CHANNEL_ID,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "text": page,
                    "raw_text": description,
                    "cleaned_text": fallback_text,
                    "transcript_method": "fallback_description"
                }
            })
            continue
        content = f"Video title: {title}\n\nCleaned transcript: {cleaned}"
        documents.append({
            "page_content": content,
            "metadata": {
                "source": "YahooFinance",
                "channel_id": YAHOO_FINANCE_CHANNEL_ID,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "text": content,
                "raw_text": raw_text,
                "cleaned_text": cleaned,
                "transcript_method": data.get('method', '')
            }
        })
    return documents


if __name__ == "__main__":
    docs = scrape_yahoo_finance_transcripts(videos_per_channel=20)
    print(f"Collected {len(docs)} Yahoo Finance documents")


