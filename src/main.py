# main.py
import os
import argparse
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

try:
    # When running as a package: python -m src.main
    from .agent_system import AgentWorkflow
    from .data_models import FinalReport
    from . import market_data_scraper
    from . import fred_scraper
    from . import news_scraper
    from . import yahoo_finance_scraper
    from . import reddit_scraper
    from . import youtube_scraper
    from . import sec_scraper
except ImportError:
    # When running as a script: python src/main.py
    from agent_system import AgentWorkflow
    from data_models import FinalReport
    import market_data_scraper
    import fred_scraper
    import news_scraper
    import yahoo_finance_scraper
    import reddit_scraper
    import youtube_scraper
    import sec_scraper

console = Console()
load_dotenv()

def display_report(report: FinalReport):
    """Display final report using Rich library with beautiful formatting"""
    console.print(Panel(
        f"[bold]Summary:[/bold]\n{report.summary}\n\n"
        f"[bold]Key Findings:[/bold]\n" + "".join([f"- {item}\n" for item in report.key_findings]) + "\n"
        f"[bold]Counter-Arguments & Risks:[/bold]\n" + "".join([f"- {item}\n" for item in report.counter_arguments]) + "\n"
        f"[bold]Confidence Score:[/bold] {report.confidence_score * 100:.1f}%\n"
        f"[bold]Uncertainty Notes:[/bold]\n{report.uncertainty_notes}",
        title="[bold blue]Comprehensive Financial Analysis Report[/bold blue]",
        expand=True
    ))

def run_data_pipeline(agent_workflow: AgentWorkflow):
    console.print(Panel("[bold green]🚀 Starting data collection pipeline...[/bold green]"))

    # Default parameter configuration (can be adjusted or read from environment variables/config files as needed)
    market_tickers = ["^GSPC", "^VIX"]
    fred_series_ids = ["UNRATE", "CPIAUCSL", "FEDFUNDS"]
    news_keywords = ["stocks", "inflation", "interest rates", "earnings"]
    reddit_subreddits = ["wallstreetbets", "investing"]
    reddit_keywords = ["AAPL", "NVDA", "SPY", "rate", "inflation"]

    # YouTube scraper will automatically load channel IDs from Youtube_channel_ID file
    # No need to manually read channel IDs here
    youtube_channel_ids = None  # Let youtube_scraper handle channel ID loading
    try:
        youtube_videos_per_channel = int(os.getenv("YOUTUBE_VIDEOS_PER_CHANNEL", "5"))
    except ValueError:
        youtube_videos_per_channel = 5

    data_sources = {
        "Market Indices (S&P 500, VIX)": lambda: market_data_scraper.scrape_market_data(market_tickers),
        "FRED Macroeconomic Data": lambda: fred_scraper.scrape_fred_data(fred_series_ids),
        "Global Financial News": lambda: news_scraper.scrape_news_data(news_keywords, limit=50),
        "Reddit (r/wallstreetbets, r/investing)": lambda: reddit_scraper.scrape_reddit_data(reddit_subreddits, reddit_keywords, limit_per_subreddit=50),
        "YouTube Financial Channels": lambda: youtube_scraper.scrape_youtube_data(youtube_channel_ids, videos_per_channel=youtube_videos_per_channel, min_transcripts=0),
        "Yahoo Finance Videos": lambda: yahoo_finance_scraper.scrape_yahoo_finance_transcripts(videos_per_channel=20),
        "SEC Insider Trading Filings": sec_scraper.get_sec_filings,
    }

    all_documents = []
    for name, func in data_sources.items():
        try:
            console.print(f"\n[cyan]>> Processing: {name}...[/cyan]")
            documents = func()
            if documents:
                console.print(f"[green]✔ Successfully retrieved {len(documents)} records.[/green]")
                all_documents.extend(documents)
            else:
                console.print(f"[orange]⚠ No data retrieved from {name} or skipped.[/orange]")
        except Exception as e:
            console.print(f"[bold red]❌ Unexpected error processing {name}: {e}[/bold red]")

    if all_documents:
        agent_workflow.embed_and_store(all_documents)
    else:
        console.print("[bold orange]⚠ Warning: No data collected from any source, knowledge base is empty.[/bold orange]")
    
    console.print(Panel("[bold green]✅ All data sources processed.[/bold green]"))

def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="Financial Analysis Agent System")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Start test mode, skip data collection, use small amount of mock data for quick system validation."
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Run in non-interactive mode, only collect data and exit."
    )
    args = parser.parse_args()

    collection_name = "financial_signals"
    
    # Read hostnames from environment variables
    qdrant_host = os.getenv("QDRANT_HOST")
    ollama_host = os.getenv("OLLAMA_HOST")
    
    if not qdrant_host or not ollama_host:
        console.print("[bold red]Error: QDRANT_HOST and OLLAMA_HOST environment variables must be set.[/bold red]")
        console.print("Please check your docker-compose.yml file.")
        return

    agent_workflow = AgentWorkflow(
        collection_name=collection_name,
        qdrant_host=qdrant_host,
        ollama_host=ollama_host
    )

    if args.test:
        console.print(Panel("[bold yellow]🚀 Test mode activated (Test Mode)[/bold yellow]"))
        dummy_documents = [
            {"page_content": "Apple (AAPL) announces stock buyback program.", "metadata": {"source": "Mock Data-Apple"}},
            {"page_content": "Federal Reserve Chairman hints at potential rate cuts.", "metadata": {"source": "Mock Data-Fed"}}
        ]
        agent_workflow.embed_and_store(dummy_documents)
    else:
        run_data_pipeline(agent_workflow)

    # Skip interactive mode if --no-interactive flag is set
    if args.no_interactive:
        console.print(Panel("[bold green]✅ Data collection completed. Exiting in non-interactive mode.[/bold green]"))
        return

    console.print("\n[bold cyan]💡 You can now start asking questions.[/bold cyan]")
    while True:
        try:
            user_query = console.input("[bold]Please enter your question (type 'quit' to exit): [/bold]")
            if user_query.lower() == 'quit':
                break
            if not user_query:
                continue
            final_report = agent_workflow.run(user_query)
            display_report(final_report)
        except KeyboardInterrupt:
            console.print("\n[bold orange]Received keyboard interrupt. Exiting...[/bold orange]")
            break
        except EOFError:
            console.print("\n[bold orange]Input stream closed. Exiting...[/bold orange]")
            break
        except Exception as e:
            console.print(f"[bold red]Unknown error occurred: {e}[/bold red]")
            # Add a small delay to prevent rapid error loops
            import time
            time.sleep(1)

    console.print("\n[bold orange]Program exited.[/bold orange]")

if __name__ == "__main__":
    main()

