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
import re
from datetime import datetime
from pathlib import Path
from typing import List
from dotenv import load_dotenv

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI


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
    def __init__(self, extractor_model: str = None, hyde_model: str = None):
        """
        🌟 架构师优化版：双 API 引擎驱动
        """
        # 从环境变量读取配置，默认使用 gpt-4o-mini
        self.extractor_model_name = extractor_model or os.getenv("TRANSFORM_EXTRACTOR_MODEL", "gpt-4o-mini")
        self.hyde_model_name = hyde_model or os.getenv("TRANSFORM_HYDE_MODEL", "gpt-4o-mini")
        
        api_key = os.getenv("OPENAI_API_KEY")
        
        # 1. 结构化抽取引擎 (必须使用 .with_structured_output)
        self.extractor_llm = ChatOpenAI(
            model=self.extractor_model_name,
            temperature=0,
            api_key=api_key
        ).with_structured_output(MetadataExtraction)

        # 2. HyDE 生成引擎（结构化输出，返回 hyde_paragraph + rerank_query）
        self.hyde_llm = ChatOpenAI(
            model=self.hyde_model_name,
            temperature=0.1, 
            api_key=api_key
        ).with_structured_output(HyDEGeneration)

        self.allowed_tickers = self._load_allowed_tickers()
        self._build_prompts()

    def _load_allowed_tickers(self) -> List[str]:
        """Build a global ticker pool (equities + ETFs + macro indices)."""
        # 1) Load SEC filer universe through the central UniverseLoader. This is
        #    now the single source of truth; there is no file-path fallback.
        sec_tickers: List[str] = []
        try:
            from Scripts.core.universe import universe as _universe
            sec_tickers = [t.upper() for t in _universe.get("sec.filers")]
        except Exception as e:
            logger.warning(
                f"UniverseLoader unavailable ({e}); allowed-ticker pool will "
                "only contain the hard-coded macro/ETF baseline."
            )

        # 2) Add macro-index symbols that are NOT in the universe manifest
        #    (these are ^-prefixed indices / DX-Y.NYB used by retrieval prompts).
        macro_index_tickers = ["^GSPC", "^IXIC", "^VIX", "DX-Y.NYB"]

        # 3) Best-effort: pull ETF tickers from the universe too, so adding a
        #    new ETF to config/universe/ automatically propagates here.
        try:
            from Scripts.core.universe import universe as _universe
            etf_tickers = [t.upper() for t in _universe.get("etf.all")]
        except Exception:
            etf_tickers = ["GLD", "SLV", "SPY", "QQQ", "IWM"]

        return list(set(sec_tickers + etf_tickers + macro_index_tickers))

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
    def _fallback_extraction(self, query: str) -> MetadataExtraction:
        """Regex-based minimal fallback to keep the pipeline alive."""
        upper_query = query.upper()
        matched_tickers = sorted({
            ticker for ticker in self.allowed_tickers
            if re.search(rf"\b{re.escape(ticker)}\b", upper_query)
        })

        return MetadataExtraction(
            logical_reasoning="Fallback extraction due to stage1 model failure.",
            tickers=matched_tickers,
            metrics=[],
            source_types=["news"],
            action_direction="NONE",
            form_type="ALL",
            sentiment_target="ANY",
            event_keyword="",
            time_window=TimeWindow.PAST_SIX_MONTHS,
        )

    async def _stage1_extract_metadata(self, query: str) -> MetadataExtraction:
        """
        使用 GPT-4o-mini 进行秒级结构化提取
        """
        try:
            chain = self.extractor_prompt | self.extractor_llm
            result = await chain.ainvoke({
                "user_query": query,
                "allowed_tickers_str": ", ".join(self.allowed_tickers),
                "allowed_metrics": ", ".join(ALLOWED_METRICS),
                "allowed_sources": ", ".join(ALLOWED_SOURCES),
                "allowed_categories": ", ".join(ALLOWED_CATEGORIES),
            })
            return result
        except Exception as e:
            logger.error(f"Stage 1 API Extraction failed: {e}. Switching to Regex Fallback.")
            return self._fallback_extraction(query)

    async def _stage2_generate_hyde(self, query: str, meta: MetadataExtraction) -> HyDEGeneration:
        """
        使用 GPT-4o-mini 生成 HyDE 辅助文本
        """
        try:
            chain = self.hyde_prompt | self.hyde_llm
            result = await chain.ainvoke({
                "user_query": query,
                "macro_background": self._get_macro_context(),
                "extracted_metadata": meta.model_dump_json(indent=2, exclude={'logical_reasoning'}),
                "reasoning_chain": meta.logical_reasoning,
            })
            return result
        except Exception as e:
            logger.error(f"Stage 2 HyDE generation failed: {e}")
            return HyDEGeneration(
                hyde_paragraph=f"Direct retrieval search for: {query}",
                rerank_query=query
            )

    async def transform_for_dual_rag(self, query: str, intent: QueryIntent) -> FullTransformationResult:
        start_time = datetime.now()
        logger.info(f"🚀 Starting Two-Stage Pipeline for query: {query[:50]}...")

        try:
            # ==========================================
            # STAGE 1: Extract Metadata 
            # ==========================================
            metadata: MetadataExtraction = await self._stage1_extract_metadata(query)
            
            # [GUARDRAIL 1] Post-clean extracted tickers using allowlist.
            valid_tickers = [t for t in metadata.tickers if t in self.allowed_tickers]
            if len(valid_tickers) != len(metadata.tickers):
                logger.warning(f"⚠️ Guardrail triggered: Dropped invalid tickers: {set(metadata.tickers) - set(valid_tickers)}")
            metadata.tickers = valid_tickers

            # [GUARDRAIL 2] Apply a default time window fallback.
            if not metadata.time_window:
                metadata.time_window = TimeWindow.PAST_SIX_MONTHS

            # [GUARDRAIL 3] Ticker-explosion cap.
            # Defends against LLM hallucination on vague phrases like
            # "recommended tickers" / "all major stocks", which on 2026-04-22
            # produced 57 tickers in a single extraction. Downstream Silver
            # SQL executes one query per metric × ticker → latency blows up
            # and the Analyst can't reason over 57 separate anchors.
            #
            # Policy:
            #   - > _TICKER_HARD_CAP (default 8)  => keep first _TICKER_KEEP (default 5),
            #     drop the rest, log LOUD so the audit trail shows degradation.
            #   - <= _TICKER_HARD_CAP              => pass through.
            _TICKER_HARD_CAP = int(os.getenv("TICKER_EXPLOSION_CAP", "8"))
            _TICKER_KEEP     = int(os.getenv("TICKER_EXPLOSION_KEEP", "5"))
            if len(metadata.tickers) > _TICKER_HARD_CAP:
                dropped = metadata.tickers[_TICKER_KEEP:]
                metadata.tickers = metadata.tickers[:_TICKER_KEEP]
                logger.warning(
                    f"⚠️ Guardrail 3 (ticker explosion): original={len(dropped) + _TICKER_KEEP} "
                    f"kept={metadata.tickers} dropped={dropped}"
                )

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
            hyde_result: HyDEGeneration = await self._stage2_generate_hyde(query, metadata)

            # [GUARDRAIL 4] HyDE empty-paragraph fallback.
            # Production audit on 2026-04-22 caught a live case where the
            # structured-output LLM returned hyde_paragraph="" for a
            # perfectly well-formed "yesterday PCR + IV Skew for AAPL" query,
            # blanking out dense retrieval entirely. Rather than let the Gold
            # engine get an empty vector, we synthesise a deterministic
            # one-liner from the metadata so the dense embedder always has a
            # coherent sentence to embed. The rerank_query is also reproduced
            # so sparse retrieval stays aligned.
            hyde_para = (hyde_result.hyde_paragraph or "").strip()
            hyde_rr = (hyde_result.rerank_query or "").strip()
            if not hyde_para:
                metric_str = ", ".join(metadata.metrics) if metadata.metrics else "market data"
                ticker_str = ", ".join(metadata.tickers) if metadata.tickers else "the broader market"
                tw_val = getattr(metadata.time_window, "value", metadata.time_window) or "recent"
                hyde_para = (
                    f"Analysis of {metric_str} for {ticker_str} over the {tw_val} window "
                    f"reflects the current implied-volatility regime and "
                    f"options-flow sentiment relevant to the query."
                )
                logger.warning(
                    f"⚠️ Guardrail 4 (HyDE empty): synthesised fallback paragraph "
                    f"({len(hyde_para)} chars) from metadata."
                )
            if not hyde_rr:
                parts = []
                if metadata.tickers:
                    parts.extend(metadata.tickers)
                if metadata.metrics:
                    parts.extend(metadata.metrics)
                if getattr(metadata, "event_keyword", None):
                    parts.append(metadata.event_keyword)
                hyde_rr = " ".join(parts) if parts else query
            hyde_result = HyDEGeneration(hyde_paragraph=hyde_para, rerank_query=hyde_rr)

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