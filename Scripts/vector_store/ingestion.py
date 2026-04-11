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
from tqdm import tqdm
from dotenv import load_dotenv

# 引入 Qdrant 原生对象
from qdrant_client.http import models
from qdrant_client.models import PointStruct, VectorParams, SparseVectorParams, Distance

# 引入 FastEmbed 用于生成稀疏向量
from fastembed import SparseTextEmbedding

import sys
from pathlib import Path


project_root = str(Path(__file__).resolve().parents[2])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from Scripts.vector_store.connection import get_qdrant_client, get_embedding_model
except ImportError:
    from .connection import get_qdrant_client, get_embedding_model

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
        
        # 动态检测维度
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

    def init_collection_with_indexes(self):
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
            
            # ==========================================
            # 严格按照架构要求的 Payload 索引策略
            # ==========================================
            index_schemas = [
                # 1. 强匹配/精确过滤 (Keyword)
                ("ticker", models.PayloadSchemaType.KEYWORD),
                ("form_type", models.PayloadSchemaType.KEYWORD),
                ("action_direction", models.PayloadSchemaType.KEYWORD),
                ("source_type", models.PayloadSchemaType.KEYWORD),
                
                # 2. 数组包含过滤 (Qdrant 的 KEYWORD 天然支持 Array of Strings 包含查询)
                ("topics", models.PayloadSchemaType.KEYWORD),
                ("impacted_assets", models.PayloadSchemaType.KEYWORD),
                ("entities", models.PayloadSchemaType.KEYWORD),
                
                # 3. 时间范围过滤 (Integer)
                ("unified_timestamp", models.PayloadSchemaType.INTEGER),
            ]
            
            for field, schema in index_schemas:
                self.client.create_payload_index(self.collection_name, field_name=field, field_schema=schema)
                logger.info(f"✅ Payload Index created for: {field}")
        else:
            logger.info(f"✅ Collection '{self.collection_name}' already exists. Skipping initialization.")

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
                    
                # 2. 统一时间戳
                raw_date = metadata.get("publish_timestamp") # News 优先拿这个
                if not raw_date:
                    raw_date = metadata.get("publish_date") or metadata.get("filed_at") or metadata.get("transaction_date")
                
                # 如果已经是整数时间戳(News/GPR)，直接用；否则转为时间戳
                if isinstance(raw_date, int):
                    unified_ts = raw_date
                else:
                    unified_ts = self._to_unix_timestamp(str(raw_date))
                
                # 3. 防御性 Metadata 构建 (防止漏斗丢失)
                payload = {
                    "text": text,
                    "source_type": source_type,
                    "has_bronze_evidence": has_bronze_evidence,
                    "unified_timestamp": unified_ts,
                    
                    # 提供默认值防止空值无法被过滤
                    "ticker": metadata.get("ticker", "NONE"),
                    "form_type": metadata.get("form_type", "NONE"),
                    "action_direction": metadata.get("action_direction", "NONE"),
                    "topics": metadata.get("topics") or [metadata.get("topic", "NONE")], # 兼容 SEC 和 News 的字段名差异
                    "impacted_assets": metadata.get("impacted_assets", []),
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
                
                # 5. 构建与 Upsert
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
        读取上游调度器留下的状态快照 (Watermark Tracking)。
        将调度器的 job keys 映射到我们的 source_types 上。
        """
        state_file = self.project_root / "config" / "runtime" / "collect_data_state.json"
        states = {}
        
        # 默认回退机制：如果找不到 state 文件，默认跑当天的
        today = datetime.now().strftime("%Y-%m-%d")
        current_month = datetime.now().strftime("%Y-%m")
        
        if state_file.exists():
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    last_run_keys = data.get("last_run_keys", {})
                    
                    # 建立 Job Key 到 Ingestion Source Type 的映射
                    states = {
                        "news": last_run_keys.get("news_daily", today),
                        "gpr": last_run_keys.get("gpr_monthly", current_month),
                        # 假设你以后会有 sec_daily，如果没有这里会默认用今天
                        "sec": last_run_keys.get("sec_daily", today) 
                    }
                logger.info(f"📄 Successfully loaded upstream states: {states}")
                return states
            except Exception as e:
                logger.error(f"⚠️ Failed to parse state file: {e}. Falling back to current date.")
                
        # Fallback
        return {"news": today, "gpr": current_month, "sec": today}

    def run_pipeline(self, full_refresh: bool = False):
        """
        执行管线。
        :param full_refresh: 如果为 True，则无视状态文件，强行全量重跑。
        """
        logger.info("="*50)
        logger.info(f"🚀 Starting Hybrid Search Data Ingestion Pipeline (Full Refresh: {full_refresh})")
        logger.info("="*50)
        
        if not self.gold_layer_dir.exists():
            logger.error(f"❌ Data directory not found: {self.gold_layer_dir}")
            return
            
        self.init_collection_with_indexes()
        
        # 获取上游抓取的日期水位线
        target_states = self._get_run_states()
        
        total_upserted = 0
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
            found_files = list(self.gold_layer_dir.rglob(filename))
            
            # 获取这个数据源应该跑哪一天的日期标识
            target_date_str = target_states.get(source_type, "")
            
            for f_path in found_files:
                # 核心逻辑：如果开启全量刷新，或者文件路径中包含了目标日期，则处理
                if full_refresh or (target_date_str and target_date_str in str(f_path)):
                    count = self.process_and_upsert_file(f_path, source_type)
                    total_upserted += count
                else:
                    # 不是目标日期的历史文件，直接跳过 (静默，不污染日志)
                    pass

        logger.info("="*50)
        logger.info(f"🎉 Pipeline Complete! Documents Upserted this run: {total_upserted}")
        logger.info("="*50)

if __name__ == "__main__":
    ingestor = QdrantHybridIngestor()
    ingestor.run_pipeline()