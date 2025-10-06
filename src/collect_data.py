#!/usr/bin/env python3
"""
Data collection script - non-interactive mode
Used for data collection in Docker environment, avoiding interactive input issues
"""

import os
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

try:
    # When called via: python -m src.collect_data
    from .agent_system import AgentWorkflow
    from . import market_data_scraper
    from . import fred_scraper
    from . import news_scraper
    from . import reddit_scraper
    from . import youtube_scraper
    from . import sec_scraper
    from . import yahoo_finance_scraper
except ImportError:
    # When called via: python src/collect_data.py
    from agent_system import AgentWorkflow
    import market_data_scraper
    import fred_scraper
    import news_scraper
    import reddit_scraper
    import youtube_scraper
    import sec_scraper
    import yahoo_finance_scraper


console = Console()
load_dotenv()


def collect_all_data():
    """Collect data from all data sources"""
    console.print("[bold green]🚀 Starting data collection...[/bold green]")

    # Parameters
    market_tickers = ["^GSPC", "^VIX", "AAPL", "NVDA", "TSLA"]
    fred_series_ids = ["UNRATE", "CPIAUCSL", "FEDFUNDS", "GDP"]
    news_keywords = ["stocks", "options", "earnings", "inflation", "interest rates"]
    reddit_subreddits = ["wallstreetbets", "investing", "options"]
    reddit_keywords = ["AAPL", "NVDA", "SPY", "QQQ", "options", "calls", "puts"]

    # YouTube
    youtube_channel_ids_env = os.getenv("YOUTUBE_CHANNEL_IDS", "").strip()
    youtube_channel_ids = [cid.strip() for cid in youtube_channel_ids_env.split(",") if cid.strip()]
    youtube_videos_per_channel = int(os.getenv("YOUTUBE_VIDEOS_PER_CHANNEL", "3"))

    # Data sources
    data_sources = {
        "Market Indices": lambda: market_data_scraper.scrape_market_data(market_tickers),
        "FRED Macro Data": lambda: fred_scraper.scrape_fred_data(fred_series_ids),
        "Financial News": lambda: news_scraper.scrape_news_data(news_keywords, limit=20),
        "Reddit Discussions": lambda: reddit_scraper.scrape_reddit_data(reddit_subreddits, reddit_keywords, limit_per_subreddit=10),
        "YouTube Channels": lambda: youtube_scraper.scrape_youtube_data(youtube_channel_ids, videos_per_channel=youtube_videos_per_channel),
        "Yahoo Finance Videos": lambda: yahoo_finance_scraper.scrape_yahoo_finance_transcripts(videos_per_channel=20, verbose=True),
        "SEC Filings": sec_scraper.get_sec_filings,
    }

    all_documents = []
    total_collected = 0

    for name, func in data_sources.items():
        try:
            console.print(f"\n[cyan]📊 Processing: {name}...[/cyan]")
            documents = func()
            if documents:
                console.print(f"[green]✅ Retrieved {len(documents)} records[/green]")
                all_documents.extend(documents)
                total_collected += len(documents)
            else:
                console.print(f"[yellow]⚠️  No data retrieved from {name} or skipped[/yellow]")
        except Exception as e:
            console.print(f"[red]❌ Error processing {name}: {e}[/red]")

    console.print(f"\n[bold green]📈 Collection complete. Total {total_collected} records[/bold green]")

    # Store to Qdrant
    if all_documents:
        console.print("[cyan]💾 Storing data to Qdrant...[/cyan]")
        try:
            qdrant_host = os.getenv("QDRANT_HOST", "qdrant_vdb")
            ollama_host = os.getenv("OLLAMA_HOST", "ollama_llm")

            agent_workflow = AgentWorkflow(
                collection_name="financial_signals",
                qdrant_host=qdrant_host,
                ollama_host=ollama_host
            )

            agent_workflow.embed_and_store(all_documents)
            console.print("[green]✅ Stored to Qdrant successfully[/green]")

            # Verify
            response, _ = agent_workflow.qdrant_client.scroll(
                collection_name="financial_signals",
                limit=5,
                with_payload=True
            )
            console.print(f"[blue]📊 Verification: {len(response)} sample records present[/blue]")

        except Exception as e:
            console.print(f"[red]❌ Error storing data: {e}[/red]")
            return False
    else:
        console.print("[yellow]⚠️  No data collected from any source[/yellow]")
        return False

    return True


def main():
    """Main entry point"""
    console.print(Panel("[bold blue]🔍 Financial Data Collection[/bold blue]"))

    success = collect_all_data()

    if success:
        console.print(Panel("[bold green]✅ Data collection finished![/bold green]"))
        return 0
    else:
        console.print(Panel("[bold red]❌ Data collection failed![/bold red]"))
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())


