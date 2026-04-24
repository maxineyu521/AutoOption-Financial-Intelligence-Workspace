"""
test_master_retriever.py

Financial RAG architecture - end-to-end test for the MasterRetriever facade.
Scope: multi-scenario concurrent retrieval tests, failure isolation checks,
and daily-rotating audit log output.
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

# Dynamic bootstrap: walk up three levels to resolve project root.
project_root = Path(__file__).parent.parent.parent.resolve()
sys.path.append(str(project_root))

# Import retrieval components via package exports.
from Scripts.retrieval import QueryTransformer, MasterRetriever, QueryIntent

# ==========================================
# 1. Production-style test logging setup
# ==========================================
def setup_test_logger():
    """Configure daily partitioned test logs at logs/test/YYYY-MM-DD/."""
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = project_root / "logs" / "test" / today
    log_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%H%M%S")
    log_file = log_dir / f"master_retriever_test_{timestamp}.log"
    
    logger = logging.getLogger("TestAudit")
    logger.setLevel(logging.DEBUG)
    
    # Avoid duplicate handler registration.
    if not logger.handlers:
        # 1) File sink.
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        
        # 2) Console sink.
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        
        formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        
        logger.addHandler(fh)
        logger.addHandler(ch)
        
    return logger, log_file

# ==========================================
# 2. Concurrent retrieval E2E orchestration
# ==========================================
async def run_tests():
    logger, log_path = setup_test_logger()
    logger.info("==================================================")
    logger.info("🚀 Starting MasterRetriever E2E test")
    logger.info(f"📁 Audit log file: {log_path}")
    logger.info("==================================================")

    try:
        # API surface check: verify package exports resolve correctly.
        _ = QueryTransformer
        _ = QueryIntent

        logger.info("⏳ Initializing MasterRetriever...")
        master_retriever = MasterRetriever()
        master_retriever.gold_timeout = 6.0
        master_retriever.silver_timeout = 10.0
        logger.info("✅ System components initialized.\n")
    except Exception as e:
        logger.error(f"❌ Startup failed, please check environment: {e}", exc_info=True)
        return

    # Three representative financial queries.
    test_queries = [
        # Query 1: Quant-heavy options case (primarily stresses Silver).
        "What is the current IV Skew and Put/Call Ratio for AAPL?",
        
        # Query 2: Qualitative macro/geopolitical case (stresses Gold and cross-modal retrieval).
        "How did the recent spike in Geopolitical Risk (GPR) index affect SPY's macro trend?",
        
        # Query 3: Hybrid complex case (tests SEC filtering and liquidity assessment).
        "Did AAPL executives' Form 4 insider selling last week negatively impact the options market liquidity?"
    ]

    for idx, query in enumerate(test_queries, 1):
        logger.info(f"▶️ [TEST {idx}/3] Query: '{query}'")
        
        try:
            # Step 1: Trigger black-box retrieval through MasterRetriever.
            # Intent routing and transformation are handled internally by retrieve().
            logger.info("  [1] Triggering MasterRetriever (Gold & Silver concurrency)...")
            results = await master_retriever.retrieve(user_query=query)
            
            # Step 2: Result assertion and reporting.
            gold_count = len(results["gold_context"])
            silver_keys = list(results["silver_context"].get("values", {}).keys())
            
            logger.info("  [2] Retrieval result summary:")
            logger.info(f"      - 🥇 Gold (Qdrant): Retrieved {gold_count} text chunks.")
            logger.info(f"      - 🥈 Silver (SQL): Computed {len(silver_keys)} metrics: {silver_keys}")
            
            # Print lineage anchors if present.
            anchors = results["silver_context"].get("lineage_anchors", [])
            if anchors:
                logger.info(f"      - 🔗 Data lineage: captured {len(anchors)} underlying anchors.")

        except Exception as e:
            logger.error(f"❌ Query execution failed: {str(e)}", exc_info=True)
            
        logger.info("-" * 60 + "\n")

    logger.info("🎉 All tests completed.")

if __name__ == "__main__":
    # Windows-specific asyncio policy optimization.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(run_tests())