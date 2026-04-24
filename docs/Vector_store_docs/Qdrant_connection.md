# Qdrant Connection & Embedding Model Factory — `connection.py`

## 1. Goal

`Scripts/vector_store/connection.py` is the foundational infrastructure layer for all vector database operations. It provides a fault-tolerant, firewall-resilient connection gateway to Qdrant Cloud and a provider-agnostic factory for text embedding models. Downstream consumers (ingestion pipelines, `QdrantRetriever`) obtain verified client objects and embedding models without concerning themselves with retry logic, hardware placement, or credential management.

---

## 2. Architecture

```
Environment (.env)
       │
       ├─► get_qdrant_client()                  get_embedding_model()
       │         │                                      │
       │    Read QDRANT_HOST                    Read EMBEDDING_PROVIDER
       │    Read QDRANT_API_KEY                 Read EMBEDDING_MODEL_NAME
       │         │                              Read EMBEDDING_DEVICE
       │    QdrantClient(                               │
       │      url=QDRANT_HOST,                 ┌────────▼────────────┐
       │      api_key=QDRANT_API_KEY,          │ Provider dispatch   │
       │      prefer_grpc=False,               ├────────────────────┤
       │      timeout=15                       │ huggingface (def.)  │
       │    )                                  │ HuggingFaceEmbed.   │
       │         │                             │ + normalize=True    │
       │    Retry loop (max 3, 2 s gap)        ├────────────────────┤
       │         │                             │ ollama              │
       │    ✅ Return client                   │ OllamaEmbeddings    │
       │                                       └────────▼────────────┘
       └───────────────────────────────────── @lru_cache → Return model
```

**Design patterns in use:**
- **Singleton / `@lru_cache`** — models and clients are loaded exactly once per process; prevents memory leaks and redundant network calls.
- **Provider Factory** — `EMBEDDING_PROVIDER` env variable routes between HuggingFace and Ollama without changing downstream code.
- **gRPC disabled** — `prefer_grpc=False` forces HTTPS REST, bypassing strict institutional or cluster firewalls.

---

## 3. Code Strategy & Workflow

### 3.1 `get_qdrant_client()`

| Step | Detail |
|:---|:---|
| 1 | Read `QDRANT_HOST` and `QDRANT_API_KEY` from `.env` |
| 2 | Instantiate `QdrantClient(prefer_grpc=False, timeout=15)` — REST-only |
| 3 | On `ConnectionError` / network failure: retry up to **3 attempts** with **2-second backoff** |
| 4 | Return verified client on success; raise on final failure |

### 3.2 `get_embedding_model()`

| Branch | Env trigger | Model |
|:---|:---|:---|
| **HuggingFace** (default) | `EMBEDDING_PROVIDER=huggingface` | `HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL_NAME, device=EMBEDDING_DEVICE, encode_kwargs={"normalize_embeddings": True})` |
| **Ollama** | `EMBEDDING_PROVIDER=ollama` | `OllamaEmbeddings(base_url=OLLAMA_HOST, model=EMBEDDING_MODEL_NAME)` |

`encode_kwargs={"normalize_embeddings": True}` ensures cosine-distance compatibility with Qdrant's collection configuration.

### 3.3 `VectorStoreConnection` (used by `QdrantRetriever`)

The `QdrantRetriever` instantiates `VectorStoreConnection` which wraps `get_qdrant_client()` with additional logging and initialises both embedding models required for hybrid search:
- **Dense model:** `BAAI/bge-base-en-v1.5` (768-dim) via HuggingFace
- **Sparse model:** `Qdrant/Splade_PP_en_v1` via `fastembed`
- **Reranker:** `BAAI/bge-reranker-v2-m3` (CrossEncoder) via `sentence-transformers`

---

## 4. Output Data Schema & Configuration

### Environment Variables

| Variable | Default | Required | Purpose |
|:---|:---|:---|:---|
| `QDRANT_HOST` | — | ✅ | Full REST URL of Qdrant Cloud cluster (e.g. `https://xxx.us-east-2-0.aws.cloud.qdrant.io:6333`) |
| `QDRANT_API_KEY` | — | ✅ | Authentication token |
| `EMBEDDING_PROVIDER` | `huggingface` | | `"huggingface"` or `"ollama"` |
| `EMBEDDING_MODEL_NAME` | `BAAI/bge-base-en-v1.5` | | Dense embedding model ID |
| `EMBEDDING_DEVICE` | `cpu` | | `"cpu"`, `"cuda"`, or `"mps"` |
| `OLLAMA_HOST` | `http://localhost:11434` | | Ollama base URL (only when `EMBEDDING_PROVIDER=ollama`) |

### Return Objects

| Function | Returns |
|:---|:---|
| `get_qdrant_client()` | `qdrant_client.QdrantClient` — verified, cached |
| `get_embedding_model()` | `langchain_core.embeddings.Embeddings` — `.embed_query()` / `.embed_documents()` compatible |

### Qdrant Collection

| Property | Value |
|:---|:---|
| Collection name | `financial_rag_gold` |
| Distance metric | Cosine (dense), dot product (sparse) |
| Dense vector size | 768 (BAAI/bge-base-en-v1.5) |
| Sparse model | Qdrant/Splade_PP_en_v1 (ONNX, via fastembed) |

---

## 5. How to Test

### Connection health check

```bash
python Scripts/vector_store/connection.py
```

Expected output:
1. `✅ Connected to Qdrant Cloud successfully.`
2. List of active collections including `financial_rag_gold`
3. Embedding model initialisation log (provider, model, device)
4. Test vector dimension (768 for `BAAI/bge-base-en-v1.5`)

### Programmatic connectivity check

```python
from Scripts.vector_store.connection import get_qdrant_client, get_embedding_model

client = get_qdrant_client()
collections = [c.name for c in client.get_collections().collections]
print("Collections:", collections)

model = get_embedding_model()
vec = model.embed_query("AAPL IV skew")
print("Vector dim:", len(vec))
```

### Retry simulation

```bash
# Temporarily set wrong API key, confirm retry + failure message
QDRANT_API_KEY=invalid python -c "from Scripts.vector_store.connection import get_qdrant_client; get_qdrant_client()"
```

---

## 6. Dependencies

| Library | Purpose |
|:---|:---|
| `qdrant-client` | Qdrant Cloud REST client |
| `langchain-huggingface` | HuggingFace embedding wrapper |
| `langchain-ollama` | Ollama embedding wrapper |
| `langchain-core` | `Embeddings` base class |
| `sentence-transformers` | CrossEncoder reranker |
| `fastembed` | SPLADE sparse model (ONNX runtime) |
| `python-dotenv` | `.env` loading |

```bash
pip install qdrant-client langchain-core langchain-huggingface langchain-ollama \
    python-dotenv sentence-transformers fastembed
```
