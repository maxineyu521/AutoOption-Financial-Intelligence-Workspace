"""
Connection Management Layer for Vector Store (Cloud Native Version).
Implements Factory Pattern for Embedding Models (Ollama / HuggingFace).
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

try:
    import torch
except ImportError:
    torch = None

# ==========================================
# 0. 动态日志配置 (按天隔离)
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

# 强制重载环境变量
load_dotenv(override=True)

# ==========================================
# 1. 资源初始化逻辑 (模型工厂模式)
# ==========================================
def _detect_optimal_device() -> str:
    """动态检测硬件加速器"""
    if torch is None:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return "mps"
    return "cpu"

@lru_cache(maxsize=1)
def get_embedding_model() -> Embeddings:
    """
    获取全局单例的 Embedding 模型 (工厂模式)。
    返回 LangChain 的 Embeddings 基类，实现底层解耦。
    """
    # 默认使用 ollama 提供商和 nomic-embed-text 模型
    provider = os.getenv("EMBEDDING_PROVIDER", "ollama").lower()
    model_name = os.getenv("EMBEDDING_MODEL_NAME", "nomic-embed-text")
    
    logger.info(f"Initialize Embedding Engine -> Provider: [{provider.upper()}], Model: [{model_name}]")

    try:
        if provider == "ollama":
            # 方案 A: 使用 Ollama 本地运行 (最适合 nomic-embed-text)
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            # 确保 URL 格式正确
            if not ollama_host.startswith("http"):
                ollama_host = f"http://{ollama_host}:11434"
                
            embeddings = OllamaEmbeddings(
                model=model_name,
                base_url=ollama_host
            )
            
        elif provider == "huggingface":
            # 方案 B: 使用 HuggingFace 本地加载 (适合 BGE 或没装 Ollama 的情况)
            device = _detect_optimal_device()
            logger.info(f"HuggingFace detected target device: [{device.upper()}]")
            
            embeddings = HuggingFaceEmbeddings(
                model_name=model_name,
                model_kwargs={
                    'device': device,
                    'trust_remote_code': True # 适配 Nomic 等需要自定义代码的新模型
                },
                encode_kwargs={'normalize_embeddings': True}
            )
        else:
            raise ValueError(f"Unsupported embedding provider: {provider}")

        logger.info("✅ Embedding Model loaded successfully.")
        return embeddings

    except Exception as e:
        logger.error(f"❌ Failed to load Embedding Model: {e}")
        raise RuntimeError(f"Embedding initialization failed: {e}")

@lru_cache(maxsize=1)
def get_qdrant_client(max_retries: int = 3, delay_seconds: int = 2) -> QdrantClient:
    """
    获取单例 Qdrant Cloud 客户端，带有网络容错与重试机制。
    强制使用 HTTPS REST 协议，穿透 SCC 集群防火墙。
    """
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")
    
    if not qdrant_url or not qdrant_api_key:
        error_msg = "QDRANT_URL or QDRANT_API_KEY is missing in .env file! Please configure your Cloud credentials."
        logger.error(f"❌ {error_msg}")
        raise ValueError(error_msg)
    
    masked_url = qdrant_url.replace("https://", "").split(".")[0][:8] + "..."
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connecting to Qdrant Cloud [{masked_url}] (Attempt {attempt}/{max_retries})...")
            
            client = QdrantClient(
                url=qdrant_url,
                api_key=qdrant_api_key,
                timeout=15.0, # SCC 网络可能波动，设为 15 秒
                prefer_grpc=False # 强制关闭 gRPC，只走 HTTP (443端口)，防止集群防火墙拦截
            )
            
            # 验证连接
            client.get_collections()
            
            logger.info("✅ Connected to Qdrant Cloud successfully.")
            return client
            
        except Exception as e:
            logger.warning(f"⚠️ Connection to Cloud failed on attempt {attempt}: {e}")
            if attempt < max_retries:
                logger.info(f"Retrying in {delay_seconds} seconds...")
                time.sleep(delay_seconds)
            else:
                logger.error("❌ Max retries reached. Could not connect to Qdrant Cloud.")
                raise ConnectionError(f"Failed to connect to Qdrant Cloud after {max_retries} attempts.") from e

# ==========================================
# 2. 健康检查 (Health Check)
# ==========================================
if __name__ == "__main__":
    logger.info("=== Starting System Component Health Check ===")
    try:
        # 1. 测云端数据库
        db_client = get_qdrant_client()
        collections = db_client.get_collections()
        logger.info(f"📊 Current Collections in Cloud: {[c.name for c in collections.collections]}")
        
        # 2. 测本地模型
        emb_model = get_embedding_model()
        logger.info("🎉 Health Check Passed: Cloud DB and Embedding Models are fully operational.")
    except Exception as e:
        logger.error(f"Health Check Failed: {e}")