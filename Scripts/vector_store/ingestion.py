"""
Institutional-Grade Vector Store Ingestion Layer.
Implements:
1. Dual-Track Idempotent Ingestion (UUIDv5 for SEC, Native IDs for News/GPR).
2. Hybrid Search Initialization (Dense + Sparse/BM25 vectors).
3. Payload Indexing (Strict categorization: Keyword, Array, Datetime).
4. Bronze Evidence Linking (Tracking SEC accession_no).
"""

import os
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from tqdm import tqdm
from dotenv import load_dotenv

# import Qdrant native objects
from qdrant_client.http import models
from qdrant_client.models import PointStruct, VectorParams, SparseVectorParams, Distance

# import FastEmbed for sparse vector generation
from fastembed import SparseTextEmbedding

import sys
from pathlib import Path


project_root = str(Path(__file__).resolve().parents[2])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from Scripts.vector_store.connection import get_qdrant_client, get_embedding_model

load_dotenv(override=True)

SYSTEM_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')

def setup_ingestion_logger():
    project_root = Path(__file__).resolve().parents[2]
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_dir = project_root / "logs" / today_str
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / "ingestion.log"
    logger = logging.getLogger("HybridIngestion")
    
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        
    return logger

logger = setup_ingestion_logger()

class QdrantHybridIngestor:
    def __init__(self, collection_name: str = "financial_rag_gold"):
        self.collection_name = collection_name
        self.project_root = Path(__file__).resolve().parents[2]
        self.gold_layer_dir = self.project_root / "Data" / "3_Gold_Semantic"
        self.batch_size = 100 
        
        logger.info("Initializing Connections & AI Models...")
        self.client = get_qdrant_client()
        self.dense_model = get_embedding_model()
        self.sparse_model = SparseTextEmbedding(model_name="prithivida/Splade_PP_en_v1")
        
        # dynamically detect dimension
        dummy_vector = self.dense_model.embed_query("dummy_test")
        self.dense_dimension = len(dummy_vector)
        logger.info(f"Dense Vector Dimension detected as: {self.dense_dimension}")

    def _get_uuid_v5(self, accession_no: str) -> str:
        return str(uuid.uuid5(SYSTEM_NAMESPACE, accession_no))

    def _to_unix_timestamp(self, date_str: str) -> int:
        if not date_str: return 0
        try:
            return int(datetime.strptime(date_str[:10], "%Y-%m-%d").timestamp())
        except Exception:
            return 0

    # ======================================================================
    # Canonical payload-index schema (single source of truth).
    #
    # Every key the retriever filters on MUST appear here. Qdrant rejects a
    # Range filter with `400 Bad Request: Index required but not found for …`
    # if the payload key is not indexed as `integer`/`float`, so missing a
    # key here is NOT a silent perf regression — it's a hard pipeline break.
    #
    # Production audit 2026-04-22 caught exactly this: the Gold retriever
    # filters on `unified_timestamp` OR `publish_timestamp` (nested should),
    # but only `unified_timestamp` was indexed → every query 400'd whenever
    # source_type picked up a payload without `unified_timestamp`.
    # ======================================================================
    _REQUIRED_INDEXES: list = [
        # -- Keyword filters (exact match / array contains) --
        ("ticker",            models.PayloadSchemaType.KEYWORD),
        ("form_type",         models.PayloadSchemaType.KEYWORD),
        ("action_direction",  models.PayloadSchemaType.KEYWORD),
        ("source_type",       models.PayloadSchemaType.KEYWORD),
        ("topic",             models.PayloadSchemaType.KEYWORD),
        ("topics",            models.PayloadSchemaType.KEYWORD),
        ("impacted_assets",   models.PayloadSchemaType.KEYWORD),
        ("entities",          models.PayloadSchemaType.KEYWORD),
        # -- Numeric range filters (Gold time barrier) --
        # BOTH keys must be indexed because the retriever uses nested
        # `should=[unified_timestamp, publish_timestamp]` to heal legacy
        # payloads that only carry one of the two.
        ("unified_timestamp", models.PayloadSchemaType.INTEGER),
        ("publish_timestamp", models.PayloadSchemaType.INTEGER),
        # SEC-specific numeric companion timestamps. These mirror the
        # ISO-date payload fields (`filed_at`, `transaction_date`) so the
        # Qdrant retriever can bind a Range filter without depending on
        # DATETIME payload support or legacy `unified_timestamp` semantics.
        ("filed_at_epoch_s", models.PayloadSchemaType.INTEGER),
        ("transaction_date_epoch_s", models.PayloadSchemaType.INTEGER),
        # -- Optional numeric auxiliary filters (News tone ranges) --
        ("tone_score",        models.PayloadSchemaType.INTEGER),
        ("llm_tone_score",    models.PayloadSchemaType.INTEGER),
    ]

    def _ensure_payload_indexes(self) -> None:
        """Idempotently create every index in `_REQUIRED_INDEXES`.

        Safe to call on both fresh and existing collections:
          - Fresh collection → creates all indexes.
          - Existing collection missing one index → creates only the missing
            ones (the "already exists" branch raises a benign 400 that we
            swallow and log at INFO).
          - Existing collection already fully indexed → every call is a
            no-op; no drop, no re-vectorise, no downtime.

        This is the auto-heal path that saved the 2026-04-22 outage: the
        collection had been created back when only `unified_timestamp` was
        in the schema, and the retriever's `publish_timestamp` Range filter
        was 400-ing until this method was promoted out of the "if not
        exists" branch.
        """
        for field, schema in self._REQUIRED_INDEXES:
            try:
                self.client.create_payload_index(
                    self.collection_name, field_name=field, field_schema=schema,
                )
                logger.info(f"✅ Payload Index ensured: {field} ({schema})")
            except Exception as e:
                # Qdrant returns 400/409 for "index already exists" — treat
                # as success but keep the message so operators can confirm
                # the index is there. Any *other* error surfaces loudly.
                msg = str(e).lower()
                if "already exists" in msg or "conflict" in msg or "409" in msg:
                    logger.info(f"ℹ️ Payload Index already present: {field}")
                else:
                    logger.error(f"❌ Failed to ensure index for {field}: {e}")
                    raise

    def init_collection_with_indexes(self):
        """Create the collection if missing, then always reconcile indexes.

        Index reconciliation runs on EVERY call (not just at collection-
        create time) so schema additions in `_REQUIRED_INDEXES` propagate to
        existing production collections on the next ingest run — no manual
        drop-and-recreate required.
        """
        if not self.client.collection_exists(self.collection_name):
            logger.info(f"🚀 Creating new Hybrid Collection: {self.collection_name}")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": VectorParams(size=self.dense_dimension, distance=Distance.COSINE)
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams()
                }
            )
        else:
            logger.info(f"✅ Collection '{self.collection_name}' already exists. Reconciling payload indexes…")

        # Always ensure indexes — auto-heals collections that predate a
        # schema addition (e.g. `publish_timestamp` in 2026-04-22).
        self._ensure_payload_indexes()

    def process_and_upsert_file(self, file_path: Path, source_type: str) -> int:
        points = []
        success_count = 0
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"❌ Could not read {file_path.name}: {e}")
            return 0
            
        logger.info(f"📦 Processing {len(lines)} records from {file_path.name} (Type: {source_type.upper()})...")
        
        for line in tqdm(lines, desc=f"Embedding {source_type}"):
            if not line.strip(): continue
            try:
                data = json.loads(line)
                text = data.get("text", "")
                metadata = data.get("metadata", {})
                
                # 1. ID 处理
                acc_no = metadata.get("accession_no")
                if source_type == "sec" and acc_no:
                    point_id = self._get_uuid_v5(acc_no)
                    has_bronze_evidence = True
                else:
                    point_id = data.get("id", str(uuid.uuid4()))
                    has_bronze_evidence = False
                    
                # 2. unified timestamp
                # SEC is anchored to transaction_date first, then filed_at.
                # This keeps the primary event timestamp aligned with the
                # transaction the user usually means when asking about Form-4
                # insider selling/buying/vesting.
                if source_type == "sec":
                    raw_date = metadata.get("transaction_date") or metadata.get("filed_at")
                else:
                    raw_date = metadata.get("publish_timestamp") # News takes precedence over this
                    if not raw_date:
                        raw_date = metadata.get("publish_date") or metadata.get("filed_at") or metadata.get("transaction_date")
                
                # if the date is already an integer timestamp (News/GPR), use it; otherwise convert to timestamp
                if isinstance(raw_date, int):
                    unified_ts = raw_date
                else:
                    unified_ts = self._to_unix_timestamp(str(raw_date))
                filed_at_epoch_s = self._to_unix_timestamp(str(metadata.get("filed_at", "") or ""))
                transaction_date_epoch_s = self._to_unix_timestamp(str(metadata.get("transaction_date", "") or ""))
                
                # 3. defensive metadata construction to prevent data loss
                payload = {
                    "text": text,
                    "source_type": source_type,
                    "has_bronze_evidence": has_bronze_evidence,
                    "unified_timestamp": unified_ts,
                    "filed_at_epoch_s": filed_at_epoch_s,
                    "transaction_date_epoch_s": transaction_date_epoch_s,
                    
                    # provide default values to prevent empty values from being filtered out
                    "ticker": metadata.get("ticker", "NONE"),
                    "form_type": metadata.get("form_type", "NONE"),
                    "action_direction": metadata.get("action_direction", "NONE"),
                    "topics": metadata.get("topics") or [metadata.get("topic", "NONE")], 
                    "entities": metadata.get("entities", []),
                    
                    **metadata
                }
                
                # 4. Hybrid Embedding
                dense_vec = self.dense_model.embed_query(text)
                sparse_gen = list(self.sparse_model.embed([text]))[0]
                sparse_vec = models.SparseVector(
                    indices=sparse_gen.indices.tolist(), 
                    values=sparse_gen.values.tolist()
                )
                
                # 5. Upsert
                points.append(PointStruct(
                    id=point_id,
                    vector={
                        "dense": dense_vec,
                        "sparse": sparse_vec
                    },
                    payload=payload
                ))
                
                if len(points) >= self.batch_size:
                    self.client.upsert(collection_name=self.collection_name, points=points)
                    success_count += len(points)
                    points = []
                    
            except Exception as e:
                logger.error(f"⚠️ Error processing record in {file_path.name}: {e}")
                
        if points:
            try:
                self.client.upsert(collection_name=self.collection_name, points=points)
                success_count += len(points)
            except Exception as e:
                logger.error(f"❌ Error during final batch upsert in {file_path.name}: {e}")
                
        logger.info(f"✅ Completed {file_path.name}: Upserted {success_count} documents.")
        return success_count

    def _get_run_states(self) -> dict:
        """
        read the state file to get the last run keys
        """
        state_file = self.project_root / "config" / "runtime" / "collect_data_state.json"
        states = {}
        
        # default fallback mechanism: if the state file is not found, default to the current date
        today = datetime.now().strftime("%Y-%m-%d")
        current_month = datetime.now().strftime("%Y-%m")
        
        if state_file.exists():
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    last_run_keys = data.get("last_run_keys", {})
                    
                    # map the job key to the ingestion source type
                    states = {
                        "news": last_run_keys.get("news_daily", today),
                        "gpr": last_run_keys.get("gpr_monthly", current_month),
                        "sec": last_run_keys.get("sec_daily", today) 
                    }
                logger.info(f"📄 Successfully loaded upstream states: {states}")
                return states
            except Exception as e:
                logger.error(f"⚠️ Failed to parse state file: {e}. Falling back to current date.")
                
        # Fallback
        return {"news": today, "gpr": current_month, "sec": today}

    def run_pipeline(
        self,
        full_refresh: bool = True,
        source_types: Optional[set[str]] = None,
        **kwargs,
    ) -> dict:
        """
        :param full_refresh: if True, full refresh the pipeline
        """
        if source_types is None and "source_types" in kwargs:
            source_types = kwargs.pop("source_types")
        else:
            kwargs.pop("source_types", None)

        if kwargs:
            logger.warning(
                "Ignoring unsupported run_pipeline kwargs: %s",
                sorted(kwargs.keys()),
            )

        logger.info("="*50)
        logger.info(f"🚀 Starting Hybrid Search Data Ingestion Pipeline (Full Refresh: {full_refresh})")
        if source_types:
            logger.info(f"Source filter enabled: {sorted(source_types)}")
        logger.info("="*50)
        
        if not self.gold_layer_dir.exists():
            logger.error(f"❌ Data directory not found: {self.gold_layer_dir}")
            return {
                "status": "failed",
                "collection": self.collection_name,
                "full_refresh": full_refresh,
                "source_types": sorted(source_types) if source_types else None,
                "total_upserted": 0,
                "error": f"Data directory not found: {self.gold_layer_dir}",
            }
            
        self.init_collection_with_indexes()
        
        # get the upstream watermark
        target_states = self._get_run_states()
        
        total_upserted = 0
        files_processed = []
        files_skipped = []
        source_counts = {}
        file_mappings = [
            ("sec", "qdrant_ready.jsonl"),
            ("news", "qdrant_asset_precious_metals_spot_processed.jsonl"),
            ("news", "qdrant_macro_inflation_employment_processed.jsonl"), 
            ("news", "qdrant_macro_central_banks_processed.jsonl"),
            ("news", "qdrant_macro_yields_dollar_processed.jsonl"),
            ("news", "qdrant_asset_metals_derivatives_processed.jsonl"),
            ("gpr", "qdrant_gpr_input.jsonl")
        ]
        
        for source_type, filename in file_mappings:
            if source_types and source_type not in source_types:
                continue
            found_files = list(self.gold_layer_dir.rglob(filename))
            
            # get the date identifier for this data source
            target_date_str = target_states.get(source_type, "")
            
            for f_path in found_files:
                # core logic: if full refresh is enabled, or the file path contains the target date, then process
                if full_refresh or (target_date_str and target_date_str in str(f_path)):
                    count = self.process_and_upsert_file(f_path, source_type)
                    total_upserted += count
                    source_counts[source_type] = source_counts.get(source_type, 0) + count
                    files_processed.append({
                        "source_type": source_type,
                        "path": str(f_path.relative_to(self.project_root)),
                        "upserted": count,
                    })
                else:
                    # not the target date history file, skip
                    files_skipped.append({
                        "source_type": source_type,
                        "path": str(f_path.relative_to(self.project_root)),
                        "target_state": target_date_str,
                    })

        logger.info("="*50)
        logger.info(f"🎉 Pipeline Complete! Documents Upserted this run: {total_upserted}")
        logger.info("="*50)
        return {
            "status": "ok",
            "collection": self.collection_name,
            "full_refresh": full_refresh,
            "source_types": sorted(source_types) if source_types else None,
            "target_states": target_states,
            "total_upserted": total_upserted,
            "source_counts": source_counts,
            "files_processed": files_processed,
            "files_skipped": files_skipped,
        }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Qdrant Hybrid Ingestor (init + upsert pipeline)."
    )
    parser.add_argument(
        "--indexes-only",
        action="store_true",
        help=(
            "Only reconcile payload indexes on the existing collection — "
            "no embedding, no upsert. Use this to auto-heal a collection "
            "that predates a new index (e.g. publish_timestamp added "
            "2026-04-22) without re-vectorising data."
        ),
    )
    parser.add_argument(
        "--no-full-refresh",
        action="store_true",
        help="Only process files matching the upstream watermark (default is full refresh).",
    )
    args = parser.parse_args()

    ingestor = QdrantHybridIngestor()
    if args.indexes_only:
        # Fast path for the 400 "Index required but not found" auto-heal.
        # Completes in ~1s regardless of collection size.
        ingestor.init_collection_with_indexes()
        logger.info("🔧 Index-only reconciliation complete.")
    else:
        ingestor.run_pipeline(full_refresh=not args.no_full_refresh)
