"""
Scripts/retrieval/qdrant_retriever.py

Industrial-grade dual-track retriever (Gold Layer - asymmetric hybrid retriever)
Senior Architect Version: RRF Fusion, Defensive Enum Extraction, Exception Tracing
"""

import logging
import sys
import os
import json
import time
import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

project_root = str(Path(__file__).resolve().parents[2])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load environment variables (including model cache related settings).
load_dotenv(os.path.join(project_root, ".env"))

from qdrant_client.http import models
from fastembed import SparseTextEmbedding
from sentence_transformers import CrossEncoder

try:
    # Prefer package-relative imports when used as part of Scripts.retrieval.
    from ..vector_store.connection import get_qdrant_client, get_embedding_model
    from .schema import (
        RetrievedChunk, SourceType, FullTransformationResult, TimeWindow, ActionDirection, SentimentTarget
    )
except ImportError as e:
    try:
        # Fallback to absolute imports when this file is executed directly.
        from Scripts.vector_store.connection import get_qdrant_client, get_embedding_model
        from Scripts.retrieval.schema import (
            RetrievedChunk, SourceType, FullTransformationResult, TimeWindow, ActionDirection, SentimentTarget
        )
    except ImportError:
        print(f"❌ Initialization Error: {e}")
        sys.exit(1)

logger = logging.getLogger("QdrantRetriever")

class FinancialHybridRetriever:
    _instance = None  # Singleton instance for shared model/client reuse.
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(FinancialHybridRetriever, cls).__new__(cls)
            cls._instance._is_initialized = False
        return cls._instance

    def __init__(self, collection_name: str = "financial_rag_gold"):
        if getattr(self, "_is_initialized", False):
            return
            
        self.collection_name = collection_name
        self.client = get_qdrant_client()

        device = os.getenv("RETRIEVER_DEVICE", "cpu")
        threads = int(os.getenv("FASTEMBED_THREADS", 4))
        
        logger.info("Initializing Models for Retrieval Pipeline (Using Cached Weights)...")
        
        self.dense_model = get_embedding_model() 
        sparse_model_name = os.getenv("SPARSE_MODEL_NAME", "prithivida/Splade_PP_en_v1")
        self.sparse_model = SparseTextEmbedding(model_name=sparse_model_name,threads=threads)
        
        reranker_model_name = os.getenv("RERANKER_MODEL_NAME", "BAAI/bge-reranker-v2-m3")
        self.reranker = CrossEncoder(reranker_model_name, device=device)
        
        logger.info(f"✅ Models initialized. Reranker: {reranker_model_name}")
        self._is_initialized = True

    def _build_smart_filter(self, metadata: Any, ignore_time: bool = False) -> Optional[models.Filter]:
        must_conditions = []

        def _val(obj):
            return getattr(obj, "value", obj)

        if metadata.tickers:
            must_conditions.append(models.FieldCondition(key="ticker", match=models.MatchAny(any=metadata.tickers)))

        source_vals = [_val(s) for s in metadata.source_types] if metadata.source_types else []
        if source_vals:
            must_conditions.append(models.FieldCondition(key="source_type", match=models.MatchAny(any=source_vals)))

        # SEC filter expansion logic.
        if "sec" in source_vals:
            action_val = _val(metadata.action_direction)
            if str(action_val).upper() == "SELL":
                must_conditions.append(models.FieldCondition(key="action_direction", match=models.MatchAny(any=["SELL", "ACQUIRE/VEST"])))
            elif str(action_val).upper() == "BUY":
                must_conditions.append(models.FieldCondition(key="action_direction", match=models.MatchAny(any=["BUY", "ACQUIRE/VEST"])))
            elif str(action_val).upper() != "NONE":
                must_conditions.append(models.FieldCondition(key="action_direction", match=models.MatchValue(value=action_val)))
                
            form_val = _val(metadata.form_type)
            if str(form_val).upper() != "ALL":
                must_conditions.append(models.FieldCondition(key="form_type", match=models.MatchValue(value=form_val)))

        # News sentiment filter.
        if "news" in source_vals:
            sentiment_val = _val(metadata.sentiment_target)
            if str(sentiment_val).upper() == "NEGATIVE":
                must_conditions.append(models.FieldCondition(key="tone_score", range=models.Range(lt=0)))
            elif str(sentiment_val).upper() == "POSITIVE":
                must_conditions.append(models.FieldCondition(key="tone_score", range=models.Range(gt=0)))

        # Default time barrier (compatible with enum/object values).
        if not ignore_time:
            time_val = _val(metadata.time_window)
            
            # Robust mapping with explicit fallback.
            days_delta = 30  # Default one-month window in current implementation.
            if time_val == getattr(TimeWindow, "TODAY", "today"): days_delta = 1
            elif time_val == getattr(TimeWindow, "PAST_WEEK", "past_week"): days_delta = 7
            elif time_val == getattr(TimeWindow, "PAST_MONTH", "past_month"): days_delta = 30
            elif time_val == getattr(TimeWindow, "ALL", "all"): days_delta = 365
            
            now = datetime.now()
            start_timestamp = int((now - timedelta(days=days_delta)).timestamp())
            current_timestamp = int(time.time())
            
            must_conditions.append(
                models.FieldCondition(
                    key="unified_timestamp",  
                    range=models.Range(gte=start_timestamp, lte=current_timestamp)
                )
            )

        return models.Filter(must=must_conditions) if must_conditions else None

    async def retrieve_async(self, original_query: str, transform_result: FullTransformationResult, top_k: int = 5) -> List[RetrievedChunk]:
        start_time = time.time()
        clean_search_query = getattr(transform_result.hyde, "rerank_query", original_query)
        
        logger.info(f"🔍 Original Query: {original_query[:40]}...")
        logger.info(f"🎯 Denoised Rerank Query: {clean_search_query}")
        
        qdrant_filter = None
        try:
            # Keep compatibility with LangChain embedding invocation API.
            dense_query_task = asyncio.to_thread(
                lambda: self.dense_model.embed_query(transform_result.hyde.hyde_paragraph)
            )
            sparse_query_task = asyncio.to_thread(
                lambda: list(self.sparse_model.query_embed(clean_search_query))[0]
            )
            dense_vec, sparse_vec = await asyncio.gather(dense_query_task, sparse_query_task)

            async def _execute_search(q_filter):
                prefetch = [
                    models.Prefetch(query=dense_vec, using="dense", limit=top_k * 3, filter=q_filter),
                    models.Prefetch(
                        query=models.SparseVector(indices=sparse_vec.indices.tolist(), values=sparse_vec.values.tolist()),
                        using="sparse", limit=top_k * 3, filter=q_filter
                    )
                ]
                return await asyncio.to_thread(
                    self.client.query_points,
                    collection_name=self.collection_name,
                    prefetch=prefetch,
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=top_k * 2
                )

            qdrant_filter = self._build_smart_filter(transform_result.metadata, ignore_time=False)
            search_results = await _execute_search(qdrant_filter)
            fallback_used = False

            points = search_results.points
            if not points:
                logger.warning("⚠️ 0 results found with requested time window. Triggering 180-Days Fallback.")
                qdrant_filter = self._build_smart_filter(transform_result.metadata, fallback_days=180)
                search_results = await _execute_search(qdrant_filter)
                fallback_used = True
                points = search_results.points

            if not points:
                self._log_audit(original_query, clean_search_query, qdrant_filter, [], time.time() - start_time, fallback_used)
                return []
            
            # Apply precision reranking scores.
            pairs = [[clean_search_query, p.payload.get("text", "")] for p in points]
            
            rerank_scores = await asyncio.to_thread(self.reranker.predict, pairs)

            for idx, p in enumerate(points):
                p.score = float(rerank_scores[idx])

            points.sort(key=lambda x: x.score, reverse=True)

            valid_points = [p for p in points if p.score > 0.00001]
            top_k_results = valid_points[:top_k]

            formatted_results = self._format_results(top_k_results, fallback_used)
            self._log_audit(original_query, clean_search_query, qdrant_filter, formatted_results, time.time() - start_time, fallback_used)
            return formatted_results

        except Exception as e:
            # Use logger.exception to preserve stack trace and line numbers.
            logger.exception(f"❌ Async Retrieval Pipeline failed: {e}")
            self._log_audit(original_query, qdrant_filter, [], time.time() - start_time, False, error=str(e))
            return []

    def _format_results(self, points: List[Any], fallback_used: bool = False) -> List[RetrievedChunk]:
        retrieved_chunks = []
        for hit in points:
            payload = hit.payload
            bronze_anchor = payload.get("accession_no") or payload.get("url") or str(hit.id)
            record_date = "Unknown"
            if "unified_timestamp" in payload:
                try:
                    record_date = datetime.fromtimestamp(payload["unified_timestamp"]).strftime('%Y-%m-%d')
                except Exception:
                    pass

            excluded_keys = {"text", "document_sparse_embedding"}
            refined_metadata = {k: v for k, v in payload.items() if k not in excluded_keys}
            
            # Add explicit tags for downstream router/agent consumers.
            refined_metadata["record_date"] = record_date 
            refined_metadata["is_fallback_180_days"] = fallback_used
            
            chunk = RetrievedChunk(
                content=payload.get("text", ""),
                source_type=payload.get("source_type", SourceType.NEWS),
                score=hit.score, 
                metadata=refined_metadata,
                bronze_ref=bronze_anchor
            )
            retrieved_chunks.append(chunk)
            
        return retrieved_chunks

    def _log_audit(self, query: str, rerank_query: str, qdrant_filter: Optional[models.Filter], results: List[RetrievedChunk], latency: float, fallback_used: bool, error: str = None):
        audit_payload = {
            "timestamp": datetime.now().isoformat(),
            "original_query": query,
            "rerank_query_used": rerank_query,  # Preserve exact retrieval text for observability.
            "filter_applied": qdrant_filter.model_dump() if qdrant_filter else None,
            "fallback_triggered": fallback_used,
            "results_count": len(results),
            "top_k_scores": [round(r.score, 4) for r in results],
            "latency_sec": round(latency, 3),
            "status": "ERROR" if error else "SUCCESS",
            "error_msg": error
        }
        
        logger.info(f"[AUDIT_RAG_RETRIEVAL] {json.dumps(audit_payload)}")
        
        try:
            date_str = datetime.now().strftime("%Y-%m-%d")
            log_dir = Path(project_root) / "logs" / "retrieval" / date_str
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "retriever_audit_trail.jsonl"
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(audit_payload, ensure_ascii=False) + "\n")
        except Exception as log_e:
            logger.error(f"Failed to write audit log to file: {log_e}")


# ==========================================
# Local sandbox test (end-to-end retrieval chain)
# ==========================================
if __name__ == "__main__":
    import asyncio
    # Import query transformation and intent schema for standalone E2E test.
    try:
        from .query_transform import QueryTransformer
        from .schema import QueryIntent
    except ImportError:
        from Scripts.retrieval.query_transform import QueryTransformer
        from Scripts.retrieval.schema import QueryIntent

    async def test_end_to_end():
        # 1) Initialize both engines.
        print("⚙️ Initializing RAG Pipeline Engines...")
        transformer = QueryTransformer()
        retriever = FinancialHybridRetriever()
        
        # 2) Example user query.
        #query = "How did AAPL executives' Form 4 offloading past week impact IV skew?"
        query = "What recent insider buying activity has there been for TSLA and how did the market react?"
        mock_intent = QueryIntent(primary_route="hybrid_both")
        
        print(f"\n🗣️ User Query: {query}")
        print("-" * 60)
        
        try:
            # 3) Stage 1: run transformer (LLM) for metadata + HyDE.
            print("🧠 Stage 1: LLM Transforming Query...")
            transform_result = await transformer.transform_for_dual_rag(query, mock_intent)
            
            # Print extraction outputs for debug visibility.
            print(f"   -> 🎯 Extracted Tickers: {transform_result.metadata.tickers}")
            print(f"   -> 🎯 Extracted Time: {transform_result.metadata.time_window}")
            print(f"   -> 🎯 Generated HyDE: {transform_result.hyde.hyde_paragraph[:80]}...")
            print("-" * 60)
            
            # 4) Stage 2: pass transformed payload into retriever.
            print("🔍 Stage 2: Qdrant Hybrid Retrieving...")
            results = await retriever.retrieve_async(original_query=query, transform_result=transform_result)
            
            # 5) Print retrieval results.
            print(f"\n✅ Final Retrieved: {len(results)} chunks.")
            for i, res in enumerate(results):
                print(f"[{i+1}] Score: {res.score:.4f} | Bronze Ref: {res.bronze_ref}")
                print(f"    Fallback Used: {res.metadata.get('fallback_used', False)}")
                print(f"    Source: {res.source_type} | Ticker: {res.metadata.get('ticker')}")
                print(f"    Snippet: {res.content[:150]}...\n")
                
        except Exception as e:
            print(f"\n❌ Pipeline Test Failed: {e}")

    asyncio.run(test_end_to_end())