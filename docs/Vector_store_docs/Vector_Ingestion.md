# Hybrid Vector Store Ingestion Pipeline 

This documentation outlines the `ingestion.py` module, which serves as the critical bridge between your raw, processed data (SEC filings, Macro News, GPR Index) and your vector database (Qdrant).

---
## 1 — Purpose, Scope, and Design Objectives

The primary goal of this script is to ingest structured financial data into Qdrant for Retrieval-Augmented Generation (RAG). It ensures **idempotent** execution (meaning you can run it multiple times without creating duplicate records) and initializes a **Hybrid Search** environment. By combining semantic meaning with exact keyword matching, it guarantees high-fidelity data retrieval for downstream AI analysis.

---
## 2 — System Reference Architecture
The ingestion layer is designed for reliability and high-speed retrieval, built on a dual-track embedding architecture.
* **State Management:** Reads from a `pipeline_state.json` file to determine the high-water mark (the latest processed dates), ensuring only new daily data is ingested unless a `--full-refresh` is explicitly requested.

* **Hybrid Search Engine:**

    * **Dense Vectors:** Captures the contextual and semantic meaning of the text (via your external embedding model).

    * **Sparse Vectors (BM25):** Utilizes `fastembed` to capture exact keyword frequencies, crucial for financial acronyms, tickers, and specific terminology.

* **Payload Indexing:** Explicitly creates indexes on highly-queried metadata fields (like `ticker`, `topic`, `publish_timestamp`) so Qdrant can filter data efficiently before performing vector distance calculations.

---

## 3 — Data Model: Hybrid Vectors, Payload Schema, and Predicate Indexing
Data is stored in Qdrant as `PointStruct` objects, consisting of an ID, dual vectors, and a rich metadata payload.
### Vector Configuration
The system utilizes a hybrid embedding strategy managed via environment-agnostic configurations.
#### Embedding Models
* **Dense Model (Semantic Representation):** Initialized via `get_embedding_model()`. It captures the abstract meaning of financial events. 

    * *Config:* Defined in `.env` under `EMBEDDING_MODEL_NAME`.
* **Sparse Model (Lexical Precision):** Specifically utilizes `prithivida/Splade_PP_en_v1` (SPLADE architecture). It excels at identifying rare keywords like ticker symbols and specific SEC item codes.

#### Environment Management (`.env`)
To ensure portability, the following variables are leveraged:

* `QDRANT_HOST` / `QDRANT_API_KEY`: Connection parameters.

* `DENSE_VECTOR_SIZE`: (e.g., 768 or 1536) Automatically verified during class initialization via `dummy_test` embedding.

---
#### Core Ingestion Workflow
The pipeline follows a fail-safe initialization and batching logic:

```text

[ INITIALIZE ] 

  └─ Connect to Qdrant & Load Models (Dense + Sparse).

  └─ Detect Vector Dimensions dynamically.

[ SCHEMA SETUP ]

  └─ Check if "financial_rag_gold" collection exists.

  └─ Define Vector Parameters (Cosine Distance for Dense).

  └─ Initialize Predicate Indexing (Payload Schemas).

[ DATA TRANSFORMATION ]

  └─ Batch load JSONL files from Gold Layer.

  └─ Convert raw dates to Unix Timestamps (for range filtering).

  └─ Map deterministic UUIDs (Namespace-v5) for idempotency.

[ UPSERT ]

  └─ Parallel processing of Dense/Sparse vectors.

  └─ Metadata payload attachment.

```
---
#### Data Schema & Predicate Filtering
To optimize retrieval speed, the system enforces strict indexing on specific payload fields. This allows the LLM to perform **Pre-filtering** (e.g., "Find news ONLY for $AAPL between Jan and March").

##### Vector Space (Dual-Track)

* **`dense`**: High-dimensional semantic space (e.g., 768-dim).

* **`sparse`**: High-dimensional, sparse frequency space for exact matching.

##### Indexed Metadata (Filtering Predicates)

| Category | Field Name | Index Type | Purpose |

| :--- | :--- | :--- | :--- |

| **Exact Match** | `ticker`, `form_type`, `source_type` | `KEYWORD` | High-precision filtering by asset or document type. |

| **Relational** | `action_direction` | `KEYWORD` | Filtering Insider Buys vs. Sells. |

| **Multi-Select** | `topics`, `entities`, `impacted_assets` | `KEYWORD` | Supports "Contains" logic for arrays of tags. |

| **Temporal** | `unified_timestamp` | `INTEGER` | Essential for Time-Series RAG and chronological sorting. |

---
### Payload (Metadata) Schema
Every point carries a dictionary of metadata used for filtering. The script actively indexes the following keys:

| Field | Index Type | Purpose |

| :--- | :--- | :--- |

| `topic` | Keyword | Categorize data (e.g., `macro_central_banks`, `sec_form_4`). |

| `source` | Keyword | Track data origin (e.g., `SEC`, `GDELT`). |

| `publish_timestamp` | Integer | Fast time-range filtering (Unix epoch). |

| `ticker` | Keyword | Exact stock symbol matching (e.g., `AAPL`). |

| `accession_no` | Keyword | SEC document tracking and evidence linking. |

| `url` | Keyword | Direct routing back to the original bronze source. |

---
## 4 — Operational Execution Procedure

When the script is executed, it follows a strict, batch-processed pipeline:

```text

[ Start Ingestion ]

        │
        ▼
1. Initialize Clients: Connect to Qdrant & load embedding models (Dense + FastEmbed BM25).
        │
        ▼
2. Collection Setup: Check for `hybrid_macro_vectors`. If missing (or full refresh), create it.
        │
        ▼
3. Payload Indexing: Apply strict schema indices to metadata fields (Keyword, Datetime).
        │
        ▼

4. State Resolution: Read `pipeline_state.json` to identify target folders for the day.
        │
        ▼
5. Batch Processing:

   ├─ Read target `.jsonl` files (SEC, News, GPR).

   ├─ Chunk records into batches of 100.

   ├─ Generate Dense Vectors (Semantic) & Sparse Vectors (BM25 Keyword).

   └─ Upsert into Qdrant using deterministic UUIDs to prevent duplication.
        │
        ▼
[ Ingestion Complete ]

```
---
## 5 — Verification, Quality Assurance, and Observability
After running the pipeline, you can verify the health and accuracy of your data in two ways:
**Option A: Programmatic Check (Local CLI)**

If you have a validation script configured, you can run it via your terminal. It will typically query the collection stats and fetch a sample point.

```bash

python tools/check_Qdrant.py

```
**Option B: Visual Inspection (Qdrant Cloud Dashboard)**

1. Log in to your **Qdrant Cloud Console**.
2. Navigate to your specific Cluster and open the **Dashboard** / UI.
3. Select the `hybrid_macro_vectors` collection.
4. Use the UI to verify the **Point Count** has increased.
5. Inspect a random point to ensure both the `text-dense` and `text-sparse` vectors are populated alongside the formatted `payload`.
---
## 6 — Runtime Dependencies and Installation Prerequisites
Ensure your environment is properly set up. You can install all required external libraries for this specific ingestion layer using the following one-line command:

```bash

pip install qdrant-client fastembed python-dotenv tqdm

```

*(Note: This assumes your custom internal modules like `Scripts.vector_store.connection` and your dense embedding model dependencies like `langchain` or `openai` are already configured in your broader project environment).*

---

