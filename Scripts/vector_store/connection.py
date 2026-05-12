"""
Connection Management Layer for Vector Store (Cloud Native Version).
Implements Factory Pattern for Embedding Models (HuggingFace CPU-Bound / Ollama).
Manages secure RESTful connections to Qdrant Cloud.
"""

import os
import time
import logging
from datetime import datetime
from pathlib import Path
from functools import lru_cache
from dotenv import load_dotenv

from qdrant_client import QdrantClient
from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_ollama import OllamaEmbeddings

# ==========================================
# 0. Dynamic logging (daily log isolation)
# ==========================================
def setup_logger():
    project_root = Path(__file__).resolve().parents[2]
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_dir = project_root / "logs" / today_str
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / "connection.log"
    
    logger = logging.getLogger("VectorStoreConnection")
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

logger = setup_logger()

# Resolve project root and load `.env` only as a fallback. In Docker and
# other managed runtimes the process environment is already authoritative, so
# we must not overwrite injected values like QDRANT_HOST.
project_root = Path(__file__).resolve().parents[2]
load_dotenv(project_root / ".env", override=False)

# ==========================================
# 1. Factory: embedding model (device controlled via env)
# ==========================================
@lru_cache(maxsize=1)
def get_embedding_model() -> Embeddings:
    """
    Factory with explicit device control via environment variables.
    Returns a cached LangChain Embeddings implementation (one per process).
    """
    provider = os.getenv("EMBEDDING_PROVIDER", "huggingface").lower()
    model_name = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-large-en-v1.5")
    
    # Guardrail: device comes from .env; default cpu
    device = os.getenv("EMBEDDING_DEVICE", "cpu").lower()
    
    logger.info(f"Initialize Embedding Engine -> Provider: [{provider.upper()}], Model: [{model_name}], Device: [{device.upper()}]")

    try:
        if provider == "ollama":
            # Warning: Ollama embeddings can contend with a local LLM in production
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            logger.warning("Using Ollama for embeddings. This may cause OOM if a large LLM is concurrently active.")
            return OllamaEmbeddings(
                model=model_name,
                base_url=ollama_host
            )
            
        elif provider == "huggingface":
            # Recommended: HuggingFace local; device from EMBEDDING_DEVICE (default cpu)
            model_kwargs = {'device': device}
            encode_kwargs = {'normalize_embeddings': True}  # Normalize for cosine similarity in Qdrant
            
            emb_model = HuggingFaceEmbeddings(
                model_name=model_name,
                model_kwargs=model_kwargs,
                encode_kwargs=encode_kwargs
            )
            logger.info("✅ HuggingFace Embedding Model loaded successfully.")
            return emb_model
            
        else:
            raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {provider}")
            
    except Exception as e:
        logger.error(f"❌ Failed to load Embedding Model: {e}")
        raise

# ==========================================
# 2. Qdrant Cloud connection
# ==========================================
@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """
    Return cached Qdrant Cloud client singleton.
    """
    qdrant_url = os.getenv("QDRANT_HOST")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    if not qdrant_url or not qdrant_api_key:
        logger.error("QDRANT_HOST or QDRANT_API_KEY is missing in .env")
        raise ValueError("Database credentials missing.")

    max_retries = 3
    delay_seconds = 2

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Qdrant Cloud [{qdrant_url[:12]}...] (Attempt {attempt}/{max_retries})...")
            
            client = QdrantClient(
                url=qdrant_url,
                api_key=qdrant_api_key,
                timeout=15.0, 
                prefer_grpc=False  # HTTP/HTTPS only; avoids many firewall/grpc blocks
            )
            
            client.get_collections()
            logger.info("✅ Connected to Qdrant Cloud successfully.")
            return client
            
        except Exception as e:
            logger.warning(f"⚠️ Connection to Cloud failed on attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(delay_seconds)
            else:
                logger.error("❌ Max retries reached. Could not connect to Qdrant Cloud.")
                raise ConnectionError(f"Failed to connect to Qdrant Cloud.") from e

# ==========================================
# 3. Health check
# ==========================================
if __name__ == "__main__":
    logger.info("=== Starting System Component Health Check ===")
    try:
        db_client = get_qdrant_client()
        collections = db_client.get_collections()
        logger.info(f"📊 Current Collections in Cloud: {[c.name for c in collections.collections]}")
        
        emb_model = get_embedding_model()
        
        # Smoke-test embedding pipeline
        test_vec = emb_model.embed_query("Apple Inc. financial results.")
        logger.info(f"✅ Embedding test passed. Vector dimension: {len(test_vec)}")
        logger.info("🎉 Health Check Passed: Cloud DB and CPU Embedding Model are fully operational.")
    except Exception as e:
        logger.error(f"❌ Health Check Failed: {e}", exc_info=True)
