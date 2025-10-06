# src/sec_scraper.py
import os
from rich.console import Console
from datetime import datetime, timedelta

from sec_api import QueryApi


console = Console()

def get_sec_filings():
    api_key = os.getenv("SEC_API_KEY")
    if not api_key:
        console.print("[bold orange]⚠ SEC_API_KEY not set, skipping SEC insider trading filings scraping.[/bold orange]")
        return []

    try:
        # Initialize generic QueryApi
        queryApi = QueryApi(api_key=api_key)
        
        # Get date range for past 30 days
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

        # Build query
        # We specify formType as "4" here
        query = {
            "query": { "query_string": {
                "query": f"formType:\"4\" AND filedAt:[{start_date_str} TO {end_date_str}]"
            }},
            "from": "0",
            "size": "50", # Get latest 50 filings
            "sort": [{ "filedAt": { "order": "desc" } }]
        }
        
        # Execute query
        response = queryApi.get_filings(query)
        filings = response.get('filings', [])

        if not filings:
            console.print("[yellow]No new SEC Form 4 filings found in the past 30 days.[/yellow]")
            return []

        documents = []
        for filing in filings:
            # Extract key information
            company_name = filing.get('companyName', 'N/A')
            form_type = filing.get('formType', 'N/A')
            filed_at = filing.get('filedAt', 'N/A')
            link = filing.get('linkToFilingDetails', '#')
            
            # Create a concise text summary as page_content
            # RAG systems prefer processing text over metadata
            content = (
                f"Company {company_name} filed a {form_type} document. "
                f"Filing date was {filed_at}. "
                f"This document reports insider trading activities of company personnel."
            )
            
            documents.append({
                "page_content": content,
                "metadata": {
                    "source": "SEC",
                    "type": "Form 4 Filing",
                    "company": company_name,
                    "filed_at": filed_at,
                    "link": link
                }
            })
            
        return documents

    except Exception as e:
        console.print(f"[bold red]❌ Error getting SEC filings: {e}[/bold red]")
        return []



