# Connection Management & Model Factory Layer

This documentation details the `connection.py` module. This script acts as the foundational infrastructure layer for your vector operations, abstracting away the complexities of cloud database authentication, network resilience, and AI model initialization for embeddings.

## 1. Strategic Objective

The primary goal of this module is to provide a robust, fault-tolerant connection gateway to **Qdrant Cloud** while serving as a dynamic **Factory** for text embedding models. It ensures that downstream applications (like ingestion pipelines or RAG agents) can reliably access the database and embedding models without worrying about network drops, firewall restrictions, or misconfigured hardware: device placement for HuggingFace is **explicit** via `EMBEDDING_DEVICE` (default `cpu`), rather than implied by the runtime.

---

## 2. System Architecture

The module is designed around three core institutional-grade engineering patterns:

- **Singleton / Caching Pattern (`@lru_cache`):** Prevents memory leaks and redundant network calls by ensuring that large embedding models and database clients are loaded into memory exactly once per session.
- **Provider Factory Engine:** Dynamically routes model initialization based on environment variables, allowing seamless swapping between local inferences (Ollama) and HuggingFace-backed embeddings (default **`BAAI/bge-large-en-v1.5`**) without altering downstream code.
- **Firewall-Resilient Connectivity:** Explicitly disables gRPC (`prefer_grpc=False`) and forces standard RESTful HTTP/HTTPS connections. This prevents the Qdrant client from being blocked by strict institutional or cluster firewalls.

---

## 3. Execution & Routing Workflow

The module isolates the initialization of the database client and the embedding model into two distinct, safe workflows.

```text
[ INITIALIZATION REQUEST ]
          │
          ├─► Database Route (get_qdrant_client)
          │      └─ Read Credentials from .env
          │      └─ Attempt Qdrant Cloud Connection (HTTPS only, client timeout 15s)
          │      └─ Network Check: Success?
          │           ├── YES: Return Client Object
          │           └── NO: Trigger Retry Protocol (max 3 attempts, 2s between attempts)
          │
          └─► Embedding Route (get_embedding_model)
                 └─ Read EMBEDDING_PROVIDER, EMBEDDING_MODEL_NAME, EMBEDDING_DEVICE from .env
                 ├─► If "ollama":
                 │      └─ Use OLLAMA_HOST (default http://localhost:11434) and model_name.
                 └─► If "huggingface" (Default):
                        └─ Load HuggingFace model with device from EMBEDDING_DEVICE (default cpu).
                        └─ encode_kwargs: normalize_embeddings=True (cosine-friendly for Qdrant).
                 └─ Cache Instance & Return LangChain Embeddings Object
```

---

## 4. Configuration Schema & Output Artifacts

Because this is a connection manager rather than a data processor, its "schema" revolves around required environmental configurations and the instantiated objects it returns.

### Environment Schema (`.env` requirements)


| Variable               | Default Value              | Purpose                                                                 |
| ---------------------- | -------------------------- | ----------------------------------------------------------------------- |
| `QDRANT_HOST`          | *Required*                 | The REST URL for your Qdrant Cloud cluster.                             |
| `QDRANT_API_KEY`       | *Required*                 | Authentication token for cloud access.                                  |
| `EMBEDDING_PROVIDER`   | `huggingface`              | Toggle between `huggingface` or `ollama`.                              |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-large-en-v1.5` | HuggingFace model id or Ollama embedding model name, per provider.      |
| `EMBEDDING_DEVICE`     | `cpu`                      | Device string passed to HuggingFace (e.g. `cpu`, `cuda`, `mps`).        |
| `OLLAMA_HOST`          | `http://localhost:11434`   | Base URL for Ollama when `EMBEDDING_PROVIDER=ollama`.                   |


### Output Artifacts

- **`get_qdrant_client()`** returns: A verified `qdrant_client.QdrantClient` object.
- **`get_embedding_model()`** returns: A `langchain_core.embeddings.Embeddings` compliant object, ready for `.embed_query()` or `.embed_documents()`.

---

## 5. Diagnostic & Verification

The script includes a built-in health check block. You can independently execute this file to verify both your cloud network connection and the embedding stack.

Run the module directly from your terminal (from the repository root):

```bash
python Scripts/vector_store/connection.py
```

**Expected Successful Output:**

1. A log confirming connection to Qdrant Cloud.
2. A list of active collections currently hosted in your cloud database.
3. An initialization log line from `get_embedding_model()` listing provider, model name, and device (from your `.env` or defaults).
4. A test vector dimension readout (e.g. for the default **`BAAI/bge-large-en-v1.5`**, dimension **1024**), confirming the embedding model runs end-to-end.

---

## 6. Environment Dependencies

Ensure your environment has the necessary libraries to support Qdrant Cloud, LangChain routing, and optional local tensor processing. Install them via this single command:

```bash
pip install qdrant-client langchain-core langchain-huggingface langchain-ollama python-dotenv torch sentence-transformers
```

*(Note: `torch` and `sentence-transformers` are required if you are using the default HuggingFace provider strategy.)*
