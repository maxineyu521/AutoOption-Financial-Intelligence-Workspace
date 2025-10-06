## Compliance Note
- API usage
  - Respect rate limits/ToS for NewsAPI, FRED, SEC, yfinance. Use backoff where allowed.
- Scraping
  - Prefer YouTube Data API v3 for discovery and `youtube_transcript_api` where transcripts are permitted. Do not bypass access controls; honor language/availability limits.
- Disclosures
  - Reports are informational and not investment advice. Options involve risk, including loss of principal.
  - Maintain logs of data sources and prompts to support audit/auditability.