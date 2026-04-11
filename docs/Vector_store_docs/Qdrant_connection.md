# Connection Management & Model Factory Layer

This documentation details the `connection.py` module. This script acts as the foundational infrastructure layer for your vector operations, abstracting away the complexities of cloud database authentication, network resilience, and hardware-accelerated AI model initialization.

## 1. Strategic Objective

The primary goal of this module is to provide a robust, fault-tolerant connection gateway to **Qdrant Cloud** while serving as a dynamic **Factory** for text embedding models. It ensures that downstream applications (like ingestion pipelines or RAG agents) can reliably access the database and AI models without worrying about network drops, firewall restrictions, or underlying hardware configurations (e.g., Apple Silicon vs. Nvidia GPUs).

---

## 2. System Architecture

The module is designed around three core institutional-grade engineering patterns:

- **Singleton / Caching Pattern (`@lru_cache`):** Prevents memory leaks and redundant network calls by ensuring that large embedding models and database clients are loaded into memory exactly once per session.
- **Provider Factory Engine:** Dynamically routes model initialization based on environment variables, allowing seamless swapping between local inferences (Ollama) and in-memory transformer models (HuggingFace) without altering downstream code.
- **Firewall-Resilient Connectivity:** Explicitly disables gRPC (`prefer_grpc=False`) and forces standard RESTful HTTP/HTTPS connections. This prevents the Qdrant client from being blocked by strict institutional or cluster firewalls.

---

## 3. Execution & Routing Workflow

The module isolates the initialization of the database client and the embedding model into two distinct, safe workflows. 

```text
[ INITIALIZATION REQUEST ]
          │
          ├─► Database Route (get_qdrant_client)
          │      └─ Read Credentials from .env
          │      └─ Attempt Qdrant Cloud Connection (HTTPS only)
          │      └─ Network Check: Success? 
          │           ├── YES: Return Client Object
          │           └── NO: Trigger Retry Protocol (Max 3 attempts, 15s delay)
          │
          └─► Embedding Route (get_embedding_model)
                 └─ Read EMBEDDING_PROVIDER from .env
                 ├─► If "ollama":
                 │      └─ Bind to localhost LLM server.
                 └─► If "huggingface" (Default):
                        └─ Detect Hardware Acceleration (CUDA vs. Apple MPS vs. CPU).
                        └─ Load Model weights into VRAM/RAM.
                 └─ Cache Instance & Return LangChain Embeddings Object
```

---

## 4. Configuration Schema & Output Artifacts

Because this is a connection manager rather than a data processor, its "schema" revolves around required environmental configurations and the instantiated objects it returns.

### Environment Schema (`.env` requirements)


| Variable               | Default Value         | Purpose                                                                     |
| ---------------------- | --------------------- | --------------------------------------------------------------------------- |
| `QDRANT_HOST`          | *Required*            | The REST URL for your Qdrant Cloud cluster.                                 |
| `QDRANT_API_KEY`       | *Required*            | Authentication token for cloud access.                                      |
| `EMBEDDING_PROVIDER`   | `huggingface`         | Toggle between `huggingface` or `ollama`.                                   |
| `EMBEDDING_MODEL_NAME` | (Depends on provider) | Specifies the exact model (e.g., `sentence-transformers/all-MiniLM-L6-v2`). |


### Output Artifacts

- `**get_qdrant_client()*`* returns: A verified `qdrant_client.QdrantClient` object.
- `**get_embedding_model()**` returns: A `langchain_core.embeddings.Embeddings` compliant object, ready for `.embed_query()` or `.embed_documents()`.

---

## 5. Diagnostic & Verification

The script includes a built-in health check block. You can independently execute this file to verify both your cloud network connection and local hardware acceleration setup.

Run the module directly from your terminal:

```bash
python connection.py
```

**Expected Successful Output:**

1. A log confirming connection to Qdrant Cloud.
2. A list of active collections currently hosted in your cloud database.
3. A hardware detection log (e.g., `Using device: mps` or `cuda`).
4. A test vector dimension readout (e.g., `Embedding Model Test Success. Vector Dimension: 384`), confirming the AI model is correctly loaded into memory.

---

## 6. Environment Dependencies

Ensure your environment has the necessary libraries to support Qdrant Cloud, LangChain routing, and optional local tensor processing. Install them via this single command:

```bash
pip install qdrant-client langchain-core langchain-huggingface langchain-ollama python-dotenv torch sentence-transformers
```

*(Note: `torch` and `sentence-transformers` are required if you are using the default HuggingFace provider strategy).*
```