# Technical Documentation: Asymmetric Hybrid Retrieval Engine (Gold Layer)

## 1. Strategic Objective (Goal)
The primary objective of the `qdrant_retriever.py` module is to serve as the **Asymmetric Hybrid Retrieval Engine** for the system's Gold Semantic Layer. It bridges the gap between the LLM's query transformation (HyDE & Metadata) and the Qdrant vector database. By enforcing deterministic metadata pre-filtering and utilizing Reciprocal Rank Fusion (RRF), it guarantees that downstream multi-agent workflows receive context that is both semantically relevant and highly precise regarding specific financial entities and timeframes.

---

## 2. System Architecture
The module is engineered with a focus on high-throughput, low-latency, and fault-tolerant execution:

* **Singleton Resource Management (`_instance`):** Guarantees that heavy assets (database connections, Dense/Sparse embedding models) are loaded into memory exactly once per application lifecycle, preventing VRAM leaks.
* **Dual-Track Embedding Topology:** * *Dense Track:* Utilizes standard embeddings (e.g., HuggingFace/Nomic) against the LLM-generated HyDE paragraph to capture broad semantic intent (e.g., "flight to safety", "IV crush").
    * *Sparse Track:* Utilizes `fastembed` (SPLADE) against the raw user query to enforce exact-match lexical scoring for rare tickers or financial acronyms.
* **Defensive Filtering & RRF:** Employs Qdrant's native `models.Filter` for hard bounds (date ranges, specific tickers) and `models.Fusion.RRF` to mathematically balance Dense and Sparse retrieval scores.

---

## 3. Code Strategy & Execution Workflow
The execution strategy operates asynchronously, mapping the structured transformation payload into a complex database query, executing it, and standardizing the output.

```text
[ Input: FullTransformationResult & Raw Query ]
        │
        ▼
[ Step 1: Deterministic Filter Construction ]
  ├─► Evaluate TimeWindow (Convert to Unix Timestamp constraints)
  └─► Evaluate Tickers (Generate 'Should' matching filters)
        │
        ▼
[ Step 2: Asymmetric Vectorization ]
  ├─► Embed HyDE Paragraph -> Dense Vector (Semantic)
  └─► Embed Raw Query -> Sparse Vector (Lexical/BM25)
        │
        ▼
[ Step 3: Qdrant Prefetch & Fusion Execution ]
  └─► Execute Qdrant `FusionQuery(fusion=RRF)`
  └─► Apply Step 1 Filters to limit search space BEFORE vector math.
        │
        ▼
[ Output: List[RetrievedChunk] ]
  └─► Mapped & cleansed for the Agent Orchestrator
```

---

## 4. Output Data Schema & Routing
The module does not write to a physical file path. It operates entirely in memory, returning a strictly typed Python list of `RetrievedChunk` objects. This payload is routed directly to the LangGraph agents (Analyst, Checker, Critic).

### Schema: `RetrievedChunk` (Mapped Output)

| Field Name | Data Type | Source/Index | Purpose |
| :--- | :--- | :--- | :--- |
| `chunk_id` | String | Qdrant `id` | Unique UUID of the point for traceability. |
| `text` | String | Qdrant Payload | The actual financial summary/news text. |
| `score` | Float | RRF Algorithm | Combined relevance score used for MMR reranking. |
| `source_type` | Enum / String | Qdrant Payload | Categorizes origin (e.g., `SEC_Form_4`, `GDELT_News`). |
| `timestamp` | Integer | Qdrant Payload | Unix epoch of publication, critical for temporal weighting. |
| `bronze_ref` | String | Qdrant Payload | Accession number or URL linking back to the raw Bronze data. |
| `metadata` | Dictionary | Qdrant Payload | Unpacked entity tags (tickers, impacted assets). |

---

## 5. Verification & Testing Protocol
The script includes an asynchronous sandboxed testing environment (`if __name__ == "__main__":`). It utilizes a mock implementation of the `QueryTransformer` to simulate a full query-to-retrieval lifecycle without requiring the entire system to run.

**How to Test:**
Execute the script directly from your terminal:
```bash
python Scripts/retrieval/qdrant_retriever.py
```

**Expected Successful Output:**
1. **Stage 1 (Mock):** Console prints the simulated LLM extraction (Tickers, Time, HyDE generation).
2. **Stage 2 (Retrieval):** The console prints `🔍 Stage 2: Qdrant Hybrid Retrieving...`.
3. **Final Result:** Outputs `✅ Final Retrieved: [N] chunks` followed by an enumerated list of results displaying their RRF Scores, Bronze References, Source Types, and primary Tickers.

```json
{"timestamp": "2026-04-19T21:16:01.849708", 
"original_query": "What recent insider buying activity has there been for TSLA and how did the market react?", 
"rerank_query_used": "TSLA Form 4 insider buying executives past month", "filter_applied": {
  "should": null, 
  "min_should": null, 
  "must": [
    {"key": "ticker", "match": {"any": ["TSLA"]}, "range": null, "geo_bounding_box": null, "geo_radius": null, "geo_polygon": null, "values_count": null, "is_empty": null, "is_null": null}, 
    {"key": "source_type", "match": {"any": ["sec", "news"]}, "range": null, "geo_bounding_box": null, "geo_radius": null, "geo_polygon": null, "values_count": null, "is_empty": null, "is_null": null}, 
    {"key": "action_direction", "match": {"any": ["BUY", "ACQUIRE/VEST"]}, "range": null, "geo_bounding_box": null, "geo_radius": null, "geo_polygon": null, "values_count": null, "is_empty": null, "is_null": null}, 
    {"key": "form_type", "match": {"value": "4"}, "range": null, "geo_bounding_box": null, "geo_radius": null, "geo_polygon": null, "values_count": null, "is_empty": null, "is_null": null}, 
    {"key": "unified_timestamp", "match": null, "range": {"lt": null, "gt": null, "gte": 1774055761.0, "lte": 1776647761.0}, "geo_bounding_box": null, "geo_radius": null, "geo_polygon": null, "values_count": null, "is_empty": null, "is_null": null}], "must_not": null}, 
    "fallback_triggered": false, "results_count": 1, "top_k_scores": [0.0145], "latency_sec": 0.857, "status": "SUCCESS", "error_msg": null}
```

---

## 6. Environment Dependencies
Ensure your `.env` file contains your Qdrant credentials and HuggingFace cache paths. 

**One-Line Installation Command:**
```bash
pip install qdrant-client fastembed sentence-transformers python-dotenv asyncio
```
*(Note: This module relies on the system's core schemas and connection factory located in `Scripts.vector_store.connection` and `Scripts.retrieval.schema`).*