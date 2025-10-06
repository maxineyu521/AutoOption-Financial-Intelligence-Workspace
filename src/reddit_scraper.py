# reddit_scraper.py
import os
import praw
from dotenv import load_dotenv
from typing import List, Dict, Any

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

def get_reddit_instance() -> praw.Reddit:
    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    user_agent = os.getenv("REDDIT_USER_AGENT")

    if not all([client_id, client_secret, user_agent]):
        raise ValueError("Please set Reddit API credentials in .env file")

    return praw.Reddit(client_id=client_id, client_secret=client_secret, user_agent=user_agent)

def scrape_reddit_data(subreddits: List[str], keywords: List[str], limit_per_subreddit: int = 50) -> List[Dict[str, Any]]:
    print(f"Scraping data from Reddit subreddits {subreddits}...")
    reddit = get_reddit_instance()
    documents = []
    query = " OR ".join(keywords)

    for sub_name in subreddits:
        try:
            subreddit = reddit.subreddit(sub_name)
            submissions = subreddit.search(query, sort='hot', limit=limit_per_subreddit)
            
            count = 0
            for submission in submissions:
                if submission.score < 5 or submission.is_self is False or not submission.selftext:
                    continue

                post_content = f"Post title: {submission.title}\nPost content: {submission.selftext}"
                documents.append({
                    "page_content": post_content,
                    "metadata": {
                        "source": "Reddit", "subreddit": sub_name, "title": submission.title,
                        "url": submission.url, "score": submission.score, "text": post_content
                    }
                })
                count += 1
            print(f"Found and processed {count} posts in r/{sub_name}.")
        except Exception as e:
            print(f"Unable to access or process subreddit r/{sub_name}: {e}")

    print(f"Reddit data scraping completed, obtained {len(documents)} documents.")
    return documents
