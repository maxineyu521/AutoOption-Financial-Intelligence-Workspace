"""
Scripts/retrieval/query_transform.py

Industrial-grade query transformation engine (context-anchored two-stage transformer)
Senior Architect Version: Fully Decoupled Prompts + Robust Guardrails
"""

import os
import sys
import logging
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# --- 1. Path and environment bootstrap ---
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parents[2]
load_dotenv(PROJECT_ROOT / ".env")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- 2. Core dependency imports (robust fallback) ---
try:
    # Prefer package-relative imports when used as part of Scripts.retrieval.
    from ..core.financial_ontology import (
        ALLOWED_METRICS, ALLOWED_SOURCES, ALLOWED_CATEGORIES, METRIC_TO_COLUMN_MAPPING
    )
    # Import local retrieval schemas.
    from .schema import (
        QueryIntent, MetadataExtraction, HyDEGeneration, FullTransformationResult, TimeWindow
    )
    # Import decoupled prompt templates.
    from ..core.prompt_templates import EXTRACTOR_SYSTEM_PROMPT, HYDE_WRITER_SYSTEM_PROMPT
except ImportError as e:
    try:
        # Fallback to absolute imports when running this file directly.
        from Scripts.core.financial_ontology import (
            ALLOWED_METRICS, ALLOWED_SOURCES, ALLOWED_CATEGORIES, METRIC_TO_COLUMN_MAPPING
        )
        from Scripts.retrieval.schema import (
            QueryIntent, MetadataExtraction, HyDEGeneration, FullTransformationResult, TimeWindow
        )
        from Scripts.core.prompt_templates import EXTRACTOR_SYSTEM_PROMPT, HYDE_WRITER_SYSTEM_PROMPT
    except ImportError:
        print(f"❌ Initialization Error: Missing core dependency: {e}")
        sys.exit(1)

# Fault-tolerant few-shot loading. If unavailable, degrade to zero-shot.
try:
    from ..core.few_shot_config import FEW_SHOT_EXAMPLES
except ImportError:
    try:
        from Scripts.core.few_shot_config import FEW_SHOT_EXAMPLES
    except ImportError:
        FEW_SHOT_EXAMPLES = []

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QueryTransformer")

class QueryTransformer:
    def __init__(self):
        self.model_name = os.getenv("OLLAMA_CUSTOM_MODEL_NAME", "options-expert-v1:latest")
        
        # Load ticker allowlist for prompt injection and post-LLM guardrails.
        self.allowed_tickers = self._load_allowed_tickers()
        
        # --- LLM initialization ---
        # Stage 1: strict extractor model.
        self.extractor_llm = ChatOllama(
            model=self.model_name, temperature=0, format="json"
        ).with_structured_output(MetadataExtraction)
        
        # Stage 2: semantic writer model.
        self.hyde_llm = ChatOllama(
            model=self.model_name, temperature=0.2, format="json"
        ).with_structured_output(HyDEGeneration)

        self._build_prompts()

    def _load_allowed_tickers(self) -> List[str]:
        """Build a global ticker pool (equities + ETFs + macro indices)."""
        # 1) Load SEC ticker universe.
        sec_tickers = []
        ticker_path = PROJECT_ROOT / "config" / "SEC_Ingestion" / "SEC_tickers.json"
        try:
            if ticker_path.exists():
                with open(ticker_path, 'r') as f:
                    sec_tickers = [t.upper() for t in json.load(f)]
        except Exception as e:
            logger.warning(f"Could not load SEC_tickers.json: {e}")
            
        # 2) Add ETF and macro index symbols used by retrieval prompts.
        macro_and_etf_tickers = [
            "GLD", "SLV", "SPY", "QQQ", "IWM",  # Options and commodity ETFs.
            "^GSPC", "^IXIC", "^VIX", "DX-Y.NYB"  # Macro indices.
        ]
        
        return list(set(sec_tickers + macro_and_etf_tickers))

    def _build_prompts(self):
        """Build decoupled prompts aligned with external template variables."""
        
        # --- STAGE 1 PROMPT ---
        extractor_messages = [("system", EXTRACTOR_SYSTEM_PROMPT)]
        
        # Dynamically assemble few-shot blocks to avoid template brace collisions.
        if FEW_SHOT_EXAMPLES:
            for ex in FEW_SHOT_EXAMPLES:
                extractor_messages.append(HumanMessage(content=f"Query: {ex['user_query']}"))
                extractor_messages.append(AIMessage(content=json.dumps(ex['extraction'])))
                
        extractor_messages.append(("human", "Query: {user_query}"))
        self.extractor_prompt = ChatPromptTemplate.from_messages(extractor_messages)

        # --- STAGE 2 PROMPT ---
        # Macro/background/metadata/reasoning placeholders are injected in the SYSTEM template.
        self.hyde_prompt = ChatPromptTemplate.from_messages([
            ("system", HYDE_WRITER_SYSTEM_PROMPT),
            ("human", "User Query: {user_query}")
        ])

    def _get_macro_context(self) -> str:
        """Load macro context used by HyDE generation."""
        context_path = PROJECT_ROOT / "Data" / "Agent_Context" / "latest_macro_context.md"
        if context_path.exists():
            return context_path.read_text(encoding='utf-8')
        return "Market conditions are currently stable."

    def _save_audit_trail(self, query: str, intent: QueryIntent, result_data: dict, latency: float):
        """Persist query transformation audit logs."""
        try:
            date_str = datetime.now().strftime("%Y-%m-%d")
            log_dir = PROJECT_ROOT / "logs" / "query_transform" / date_str
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "query_audit_trail.jsonl"
            
            audit_payload = {
                "timestamp": datetime.now().isoformat(),
                "latency_seconds": round(latency, 2),
                "original_query": query,
                "primary_route": intent.primary_route if intent else "unknown",
                "transformation_result": result_data
            }
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(audit_payload, ensure_ascii=False) + "\n")
            logger.info(f"💡 Audit Log saved to {log_file}")
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")

    async def transform_for_dual_rag(self, query: str, intent: QueryIntent) -> FullTransformationResult:
        start_time = datetime.now()
        logger.info(f"🚀 Starting Two-Stage Pipeline for query: {query[:50]}...")

        try:
            # ==========================================
            # STAGE 1: Extract Metadata 
            # ==========================================
            metadata: MetadataExtraction = await self.extractor_llm.ainvoke(
                self.extractor_prompt.format_prompt(
                    user_query=query, 
                    allowed_tickers_str=", ".join(self.allowed_tickers),
                    allowed_metrics=", ".join(ALLOWED_METRICS),
                    allowed_sources=", ".join(ALLOWED_SOURCES),
                    allowed_categories=", ".join(ALLOWED_CATEGORIES)
                )
            )
            
            # [GUARDRAIL 1] Post-clean extracted tickers using allowlist.
            valid_tickers = [t for t in metadata.tickers if t in self.allowed_tickers]
            if len(valid_tickers) != len(metadata.tickers):
                logger.warning(f"⚠️ Guardrail triggered: Dropped invalid tickers: {set(metadata.tickers) - set(valid_tickers)}")
            metadata.tickers = valid_tickers

            # [GUARDRAIL 2] Apply a default time window fallback.
            if not metadata.time_window:
                metadata.time_window = TimeWindow.PAST_SIX_MONTHS

            # Map semantic metrics to physical database columns.
            mapped_cols = set()
            for m in metadata.metrics:
                if m in METRIC_TO_COLUMN_MAPPING:
            # Mapping values are lists, so set.update is required here.
                    mapped_cols.update(METRIC_TO_COLUMN_MAPPING[m]) 
            mapped_physical_columns = list(mapped_cols)

            # ==========================================
            # STAGE 2: Generate HyDE
            # ==========================================
            hyde_result: HyDEGeneration = await self.hyde_llm.ainvoke(
                self.hyde_prompt.format_prompt(
                    user_query=query,
                    macro_background=self._get_macro_context(),
                    extracted_metadata=metadata.model_dump_json(indent=2, exclude={'logical_reasoning'}),
                    reasoning_chain=metadata.logical_reasoning
                )
            )

            # Aggregate stage outputs into one typed payload.
            final_result = FullTransformationResult(
                metadata=metadata,
                hyde=hyde_result,
                mapped_physical_columns=mapped_physical_columns
            )

            latency = (datetime.now() - start_time).total_seconds()
            self._save_audit_trail(query, intent, final_result.model_dump(), latency)
            logger.info(f"✅ Pipeline completed perfectly in {latency:.2f}s")
            return final_result

        except Exception as e:
            logger.error(f"❌ Two-Stage Transformation Failure: {e}")
            latency = (datetime.now() - start_time).total_seconds()
            self._save_audit_trail(query, intent, {"error": str(e), "stage": "transformation_failed"}, latency)
            raise e

# ==========================================
# Standalone test entrypoint
# ==========================================
if __name__ == "__main__":
    async def test():
        transformer = QueryTransformer()
        mock_intent = QueryIntent(primary_route="hybrid_both")
        
        #query = "How did NVDA's recent insider sales from the past week impact its options IV skew, given the current macro climate?"
        #query = "How did gold and silver price changes from recent weeks impact their options IV skew, given the current geopolitical climate?"
        query = "what is the current put/call ratio for SPY and how does it compare to historical levels? Also, how has the institutional flow been for SPY options in the past month?"
        result = await transformer.transform_for_dual_rag(query, mock_intent)
        
        print("\n" + "="*60)
        print("[STAGE 1:  (Metadata & Reasoning)]")
        print(f"🧠 Reasoning: {result.metadata.logical_reasoning}")
        print(f"📊 Tickers: {result.metadata.tickers}")
        print(f"📈 Metrics: {result.metadata.metrics}")
        print(f"🏛️ Action: {result.metadata.action_direction}")
        print(f"🕒 Time Window: {result.metadata.time_window}")
        print("-" * 60)
        print("[STAGE 2: HyDE  (Gold Layer)]")
        print(result.hyde.hyde_paragraph)
        print("="*60)
        
    asyncio.run(test())