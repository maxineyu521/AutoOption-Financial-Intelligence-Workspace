# Hybrid Vector Store Ingestion Pipeline — `ingestion.py`

## 1. Goal

`Scripts/vector_store/ingestion.py` (`QdrantHybridIngestor`) is the critical bridge between Gold-layer JSONL artefacts (SEC filings, news, GPR narratives) and the `financial_rag_gold` Qdrant collection. It guarantees **idempotent** upserts via deterministic UUID generation, initialises a **hybrid search** environment (dense + sparse vectors + payload indexes), and advances a high-water-mark state file so only incremental data is processed on subsequent runs.

---

## 2. Architecture

```
Gold-layer JSONL files (SEC / News / GPR)
           │
           ▼
 QdrantHybridIngestor.__init__()
           │  • get_qdrant_client()   — REST connection, 3-retry
           │  • get_embedding_model() — dense model (BAAI/bge-base-en-v1.5)
           │  • fastembed SPLADE      — sparse model (Qdrant/Splade_PP_en_v1)
           │  • Detect dense vector dimension via dummy embed
           │  • Ensure collection exists with correct vector config
           │  • Create payload indexes (ticker, source_type, form_type, …)
           │
           ▼
 ingest_folder(source_type, folder_path)
           │
           ├─ Read pipeline_state.json → high-water-mark (last processed date)
           ├─ Enumerate date folders newer than HWM
           │
           ▼  for each JSONL line:
           ├─ Parse {text, metadata}
           ├─ Deterministic UUID from hash(source_type + accession_no / url / id)
           │      → idempotent: re-running same data = same UUID → upsert, not duplicate
           │
           ├─ Dense embedding: get_embedding_model().embed_documents([text])
           ├─ Sparse embedding: SPLADE.query_embed(text)  → sparse vector dict
           │
           ▼  PointStruct(id=uuid, vector={dense, sparse}, payload={...metadata...})
           │
           ├─ Batch upsert to Qdrant (batch_size=100)
           │
           └─ Update pipeline_state.json high-water-mark
```

---

## 3. Code Strategy & Workflow

### 3.1 Initialisation Sequence

| Step | Detail |
|:---|:---|
| **Connection** | `get_qdrant_client()` — HTTPS REST, cached singleton |
| **Dense model** | `get_embedding_model()` — default `BAAI/bge-base-en-v1.5` (768-dim, cosine) |
| **Sparse model** | `fastembed.SparseTextEmbedding("Qdrant/Splade_PP_en_v1")` — ONNX SPLADE for lexical precision |
| **Dimension detection** | `dummy_test` embed → auto-detect `DENSE_VECTOR_SIZE` (fallback to env var) |
| **Collection setup** | Create `financial_rag_gold` if absent with named vectors `dense` (Cosine) + `sparse` (Dot) |
| **Payload indexes** | Explicitly created for: `ticker`, `source_type`, `form_type`, `topic`, `publish_timestamp`, `unified_timestamp`, `action_direction`, `tone_score` |

### 3.2 Idempotency via Deterministic UUID

Each point ID is computed as `uuid5(NAMESPACE_URL, source_type + primary_key)` where `primary_key` is, in priority order: `accession_no` (SEC), `url` (news), or `id` (GPR). This means:

- Re-running ingestion with the same JSONL → same UUIDs → Qdrant upserts overwrite in place, no duplicates.
- Changing `text` or `metadata` for the same source document → same UUID → controlled update.

### 3.3 Hybrid Vector Strategy

| Vector type | Model | Captures | Qdrant vector name |
|:---|:---|:---|:---|
| **Dense** | `BAAI/bge-base-en-v1.5` (768-dim) | Semantic meaning, context, paraphrase | `dense` |
| **Sparse** | `Qdrant/Splade_PP_en_v1` (SPLADE) | Exact keywords: tickers, SEC codes, acronyms | `sparse` |

At query time, `QdrantRetriever` uses **Reciprocal Rank Fusion (RRF)** to merge dense and sparse score lists, followed by `BAAI/bge-reranker-v2-m3` CrossEncoder reranking of the top-K candidates.

### 3.4 High-Water-Mark State

`config/runtime/pipeline_state.json` stores the last successfully ingested date per source type. On each run, `ingest_folder` only processes date folders newer than the stored HWM. `--full-refresh` flag bypasses HWM and re-ingests all available data.

---

## 4. Output Data Schema & Paths

### Source JSONL Paths (inputs)

| Source | Gold path |
|:---|:---|
| SEC filings | `Data/3_Gold_Semantic/SEC_Insider_Trades/{YYYY-MM-DD}/qdrant_ready.jsonl` |
| News / GDELT | `Data/3_Gold_Semantic/News_Qdrant/{YYYY-MM-DD}/qdrant_{topic}_processed.jsonl` |
| GPR narratives | `Data/3_Gold_Semantic/GPR_index/{YYYY-MM-DD}/qdrant_gpr_input.jsonl` |

### Qdrant Point Payload Schema

All source types share a unified payload schema. Fields that do not apply to a source type are omitted or set to `null`.

| Payload key | Type | Applies to | Description |
|:---|:---|:---|:---|
| `text` | `string` | All | Human-readable content; used for embedding |
| `source_type` | `string` | All | `"sec"`, `"news"`, `"gpr"` |
| `ticker` | `string` | SEC, News | Stock symbol |
| `form_type` | `string` | SEC | `"4"` or `"8-K"` |
| `filed_at` | `string` | SEC | Filing date `YYYY-MM-DD` |
| `accession_no` | `string` | SEC | Dedup / UUID seed |
| `transaction_date` | `string` | SEC | Actual event date |
| `tone_score` | `int` | SEC, News | −5 … +5 |
| `action_direction` | `string` | SEC | `"BUY"`, `"SELL"`, `"ACQUIRE/VEST"`, `"NONE"` |
| `topics` | `array[string]` | SEC, News | Thematic tags |
| `unified_timestamp` | `float` | All | Unix seconds (epoch); used by `TimeAdapter` Range filter |
| `publish_timestamp` | `float` | News, GPR | Unix seconds from article/GPR date |
| `topic` | `string` | News | GDELT topic slug (e.g. `macro_central_banks`) |
| `source_domain` | `string` | News | Article origin domain |
| `bronze_ref` | `string` | All | Reference to Bronze artefact; used as `[Gold: <bronze_ref>]` citation in agent drafts |
| `record_date` | `string` | All | ISO date for temporal decay weighting in Analyst prompt |

### Payload Index Configuration

| Index field | Type | Purpose |
|:---|:---|:---|
| `ticker` | keyword | Per-ticker filter in retrieval |
| `source_type` | keyword | Route Gold queries to correct data domain |
| `form_type` | keyword | Isolate Form 4 vs 8-K SEC queries |
| `topic` | keyword | News topic filter |
| `publish_timestamp` | float | Qdrant `Range` time filter (legacy news) |
| `unified_timestamp` | float | Qdrant `Range` time filter (all sources, post-2026-04-22) |
| `action_direction` | keyword | Insider buy/sell signal filter |
| `tone_score` | integer | Sentiment filtering |

### State File

`config/runtime/pipeline_state.json`:
```json
{
  "sec":  "2026-04-19",
  "news": "2026-04-19",
  "gpr":  "2026-04-10"
}
```

---

## 5. How to Test

### Full ingestion run

```bash
python Scripts/Qdrant_Ingestion.py
# Expected: batch upsert logs per source, HWM updated in pipeline_state.json
```

### Force re-ingestion of all data

```bash
python Scripts/Qdrant_Ingestion.py --full-refresh
```

### Verify collection health

```python
from Scripts.vector_store.connection import get_qdrant_client
client = get_qdrant_client()
info = client.get_collection("financial_rag_gold")
print("Points:", info.points_count)
print("Vectors:", info.vectors_count)
```

### Retrieval smoke test (post-ingestion)

```bash
python -m Scripts.tests.test_master_retriever
# Expected: Tier1 query returns > 0 chunks for an AAPL insider query
```

### Payload index verification

```python
from Scripts.vector_store.connection import get_qdrant_client
client = get_qdrant_client()
info = client.get_collection("financial_rag_gold")
for field, config in info.payload_schema.items():
    print(field, config)
# Expected: ticker, source_type, form_type, topic, unified_timestamp, … all indexed
```

---

## 6. Dependencies

| Library | Purpose |
|:---|:---|
| `qdrant-client` | Qdrant Cloud upsert API |
| `fastembed` | SPLADE sparse embedding (ONNX runtime) |
| `langchain-huggingface` | Dense embedding model |
| `sentence-transformers` | CrossEncoder reranker (used by QdrantRetriever at query time) |
| `pydantic` | Data validation |
| `python-dotenv` | `.env` loading |

```bash
pip install qdrant-client fastembed langchain-huggingface sentence-transformers pydantic python-dotenv
```

**Required env variables:** `QDRANT_HOST`, `QDRANT_API_KEY` (see `docs/Vector_store_docs/Qdrant_connection.md`).

**Runtime prerequisite:** Gold JSONL files must exist in `Data/3_Gold_Semantic/` before ingestion. Run the data collection pipeline (`Scripts/data_collection/collect_data.py`) and SEC processor first.
