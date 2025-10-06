Of course. I've restructured and refined the `README.md` to create a more logical flow, integrating the project deliverables and clarifying the overall architecture and usage. The new structure guides the user from the high-level objective down to the technical details and operational commands.

Here is the revised and completed `README.md` file:

-----

# Options Bot 🤖

An automated financial research pipeline that synthesizes data from multiple sources (YouTube, News, Reddit, FRED, SEC filings, and market data) to generate daily paper-trading options ideas. The system uses a Qdrant vector database for knowledge management and a locally-hosted LLM (via Ollama) within a multi-agent framework to produce structured analysis reports.

## 🎯 Project Objective

This project serves as an educational exercise to:

1.  Master the engineering of multi-agent LLM systems.
2.  Develop a practical understanding of how to integrate heterogeneous information sources for decision-making under uncertainty.

The goal is to build a daily research bot that synthesizes global market and social information streams to generate 5–10 paper-trading options recommendations, complete with detailed rationales and source citations.

-----

## ✨ Key Features

  * **Multi-Source Data Pipeline**: Aggregates data from YouTube, Reddit, News APIs, FRED, SEC filings, and general market sources.
  * **Vectorized Knowledge Base**: Utilizes a Qdrant vector database for efficient, scalable semantic retrieval of all ingested information.
  * **Local LLM Orchestration**: Runs on local, open-source models (e.g., `mistral:7b`, `options-expert`) via Ollama for privacy and cost-effectiveness.
  * **Multi-Agent Decision Framework**: Employs an Analyst-Checker-Critic agent loop to synthesize signals, validate facts, and challenge assumptions.
  * **Automated Daily Reporting**: Generates structured JSON and Markdown reports with actionable options ideas, rationales, and confidence levels.

-----

## 🏗️ System Architecture

The system operates in a sequential pipeline:

1.  **Data Collection**: Various scraper modules fetch data from their respective sources.
2.  **Data Processing & Storage**: The collected text is cleaned, processed, and stored as vector embeddings in the Qdrant database with associated metadata.
3.  **LLM-Powered Analysis**: A multi-agent system (`agent_system.py`) queries the vector database to retrieve relevant context and orchestrates a series of LLM calls to analyze the data, generate insights, and formulate trade ideas.
4.  **Report Generation**: The final output is structured into a `FinalReport` Pydantic model and saved as both JSON and Markdown files.

For a detailed visual overview of the data flow and component interactions, please see `ARCHITECTURE.md`.

-----

## 🚀 Getting Started

### Prerequisites

  * Docker and Docker Compose
  * A **YouTube Data API v3 key** is required.
  * API keys for **FRED**, **NewsAPI**, and the **SEC** are optional but recommended for full functionality.

### Environment Variables

Configure the following environment variables in the `docker-compose.prod.yml` file or a separate `.env` file:

  * `QDRANT_HOST`: The hostname for the Qdrant container (e.g., `qdrant_vdb_prod`).
  * `OLLAMA_HOST`: The URL for the Ollama container (e.g., `http://ollama_llm_prod:11434`).
  * `OLLAMA_MODEL`: The default model to use (e.g., `options-expert` or `mistral:7b-instruct-q4_0`).
  * `YOUTUBE_API_KEY`: Your YouTube Data API key.
  * **Optional**: `FRED_API_KEY`, `NEWS_API_KEY`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`.

### Installation & Running the System

From the root of the repository, build and launch the production services in detached mode:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

You can check the status of the running containers:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

-----

## ⚙️ Usage

Once the containers are running, you can interact with the application using `docker exec`.

### Run the Main Pipeline

To execute the full data collection and analysis pipeline in a non-interactive mode:

```bash
docker exec -it financial_agent_app_prod python src/main.py --no-interactive
```

### Start an Interactive Q\&A Session

To start a session where you can ask questions based on the ingested data:

```bash
docker exec -it financial_agent_app_prod python src/main.py
```

### Generate the Daily Production Report

To run the script that specifically generates the daily options ideas report:

```bash
docker exec -it financial_agent_app_prod python src/tools/generate_production_report.py
```

This command will output `options_ideas.json` and `options_ideas.md` to a new directory at `reports/YYYY-MM-DD/`.

### Create the Fine-tuned `options-expert` Model

To create and validate the custom `options-expert` model within Ollama:

```bash
docker exec -it financial_agent_app_prod python src/tools/create_options_expert.py
```

-----

## 📝 Project Deliverables & Documentation

This repository fulfills the following project requirements:

  * **Full Codebase & Containerization**: The complete, containerized application is available in this private GitHub repository.
  * **Model Selection Memo**: The rationale for choosing the default LLM, including comparisons and trade-offs, is documented in `PRODUCTION_OPS_NOTES.md`.
  * **Example Run**: An example of the system's inputs, outputs, and logs is also included in `PRODUCTION_OPS_NOTES.md`.
  * **Additional Signal Sources**: A note documenting other high-signal data sources that could be integrated is available in `PRODUCTION_OPS_NOTES.md`.
  * **Compliance Note**: A section covering API usage terms, scraping restrictions, and disclosure rules is detailed in `PRODUCTION_OPS_NOTES.md`.

-----

## 🔧 Troubleshooting

  * **Missing Transcripts**: If transcript fetching fails frequently, some channels may lack captions or API rate limits may have been hit. Consider widening the scan size and adding more channels to `tools/Youtube_channel_ID`.
  * **`youtube_transcript_api` issues**: Ensure the library is correctly installed in the container by checking the `requirements.txt` and Docker build logs.
  * **Qdrant/Ollama Health**: Check container logs for errors (`docker logs -f <container_name>`). Ensure ports 6333 (Qdrant) and 11434 (Ollama) are accessible between containers and that Docker volumes are correctly mounted.

### Common Docker Commands

```bash
# Stop and remove all production containers
docker compose -f docker-compose.prod.yml down

# Force a rebuild of the application image without using cache
docker compose -f docker-compose.prod.yml build --no-cache

# Restart the main application container
docker restart financial_agent_app_prod

# Tail the logs of the main application container
docker logs -f financial_agent_app_prod
```