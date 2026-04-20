# Context-Anchored Query Transformation Engine

This documentation details the `query_transform.py` module, which serves as the intelligent vanguard of the retrieval pipeline. It intercepts user queries and transforms them into highly structured, database-ready parameters.

## 1. Strategic Objective (Goal)
The primary goal of this module is to operate as an **industrial-grade, two-stage query transformation engine**. It eliminates the ambiguity of raw human questions by parsing them into deterministic filters (Metadata Extraction) while simultaneously enhancing semantic retrieval through synthetic document generation (HyDE). This drastically reduces the "needle in a haystack" problem during vector searches and ensures downstream multi-agent workflows receive perfectly clean data.

---

## 2. System Architecture
The module is built upon a **Fully Decoupled Prompt & Guardrail Architecture**, ensuring high stability and preventing Large Language Model (LLM) hallucinations:

* **Ontology-Driven Constraints:** Tightly coupled with `financial_ontology.py` to enforce strict adherence to allowed vocabularies (`ALLOWED_METRICS`, `ALLOWED_SOURCES`).
* **Schema Enforcement:** Utilizes Pydantic schemas (defined in `schema.py`) to guarantee that the LLM's output is consistently parsed into strongly-typed objects.
* **Asynchronous AI Pipeline:** Built natively with `asyncio` and `ChatOllama`, allowing for non-blocking execution of LLM prompts, significantly reducing latency during the two-stage transformation.

---

## 3. Code Strategy & Execution Workflow
The code executes a sequential, two-stage AI strategy designed to maximize the fidelity of the final Retrieval-Augmented Generation (RAG) context.

1.  **Stage 1: Analytical Metadata Extraction**
    The engine passes the query to the LLM equipped with the `EXTRACTOR_SYSTEM_PROMPT`. The LLM identifies target tickers, maps financial metrics to database-friendly column names, and deduces time windows.
2.  **Stage 2: HyDE (Hypothetical Document Embeddings) Generation**
    Using the reasoning from Stage 1 and the `HYDE_WRITER_SYSTEM_PROMPT`, the LLM drafts a fake, textbook-perfect "financial report paragraph" that directly answers the user's question. This paragraph is embedded to find the closest real documents in the Gold Semantic Layer.

```text
[ Raw User Query ]
        │
        ▼
[ Stage 1: Metadata Extractor ]
  ├─► Validate via Financial Ontology
  └─► Extract: Tickers, Metrics, Action Direction, Time Window
        │
        ▼
[ Stage 2: HyDE Generator ]
  └─► Generate: Hypothetical Financial Paragraph
        │
        ▼
[ Output: FullTransformationResult ]
  └─► Handed off to Qdrant Retriever & SQL Tools
```

---

## 4. Output Data Schema & Routing
The engine produces an in-memory `FullTransformationResult` object. It does not save to a physical file path; instead, it dynamically routes this structured payload directly into the retrieval pipeline.

### Schema: `FullTransformationResult`

| Component | Field Name | Data Type | Purpose |
| :--- | :--- | :--- | :--- |
| **Metadata** | `logical_reasoning` | String | The LLM's step-by-step logic detailing why specific variables were extracted. |
| **Metadata** | `tickers` | List[String] | Explicit stock or ETF symbols identified (e.g., `["SPY", "NVDA"]`). |
| **Metadata** | `metrics` | List[String] | Canonical financial metrics requested (e.g., `["iv_skew", "put_call_ratio"]`). |
| **Metadata** | `action_direction` | String | Identifies categorical actions (e.g., `BUY`, `SELL`, `NEUTRAL`). |
| **Metadata** | `time_window` | Object | Standardized date range used for temporal pre-filtering in Qdrant. |
| **HyDE** | `hyde_paragraph` | String | The synthetic financial text used for Dense Vector similarity matching. |

---

## 5. Verification & Testing Protocol
The script includes a built-in asynchronous sandbox at the bottom of the file (`if __name__ == "__main__":`), allowing developers to independently verify the LLM's extraction logic and prompt effectiveness without triggering the entire RAG pipeline.

**How to Test:**
Run the module directly from your terminal:
```bash
python Scripts/retrieval/query_transform.py
```

**Expected Successful Output:**
The console will print a distinct separation of stages:
1.  **[STAGE 1]:** Displays the `🧠 Reasoning`, `📊 Tickers`, `📈 Metrics`, and `🕒 Time Window` extracted from the mock query.
2.  **[STAGE 2]:** Prints the generated fake financial text designed for vector matching.
```json
{"timestamp": "2026-04-19T21:16:00.977910", 
"latency_seconds": 107.96, 
"original_query": "What recent insider buying activity has there been for TSLA and how did the market react?", 
"primary_route": "hybrid_both", 
"transformation_result": {
  "metadata": {
    "logical_reasoning": "Macro Step: Insider activity. Meso Step: Market reaction. Micro Step: TSLA insiders. Action: BUY.", 
    "tickers": ["TSLA"], 
    "metrics": ["Insider Trading", "Price Change (%)"], 
    "source_types": ["sec", "news"], 
    "action_direction": "BUY", 
    "form_type": "4", 
    "sentiment_target": "ANY", 
    "event_keyword": "insider_buying", 
    "time_window": "past_month"}, 
    "hyde": {
      "hyde_paragraph": "TSLA insiders have recently filed Form 4s indicating buying activity, which may signal confidence in the company's prospects, potentially leading to higher stock prices.", 
      "rerank_query": "TSLA Form 4 insider buying executives past month"}, "mapped_physical_columns": ["mom_change_pct", "insider_net_flow", "daily_change_pct"]
    }
}

```

---

## 6. Environment Dependencies
Ensure your `.env` file is properly configured at the `PROJECT_ROOT` level, and that local Ollama services are active if running local models. 

**Required Installations:**
Execute the following one-line command to install the required dependencies for this module:

```bash
pip install langchain-core langchain-ollama pydantic python-dotenv asyncio
```
