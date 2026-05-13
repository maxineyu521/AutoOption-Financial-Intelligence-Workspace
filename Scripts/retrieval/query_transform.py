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
from typing import Any, List, Set
from dotenv import load_dotenv

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import AIMessage, HumanMessage
from Scripts.core import llm_pool
from Scripts.core.financial_narrative_contract import (
    build_news_semantic_profile,
    expanded_news_rerank_query,
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
    narrative_option_posture_metrics_for_tickers,
>>>>>>> Stashed changes
=======
    narrative_option_posture_metrics_for_tickers,
>>>>>>> Stashed changes
)


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
        ALLOWED_METRICS, ALLOWED_SOURCES, ALLOWED_CATEGORIES, EVENT_KEYWORDS_MAPPING,
        METRIC_TO_COLUMN_MAPPING, NEWS_TOPICS, NEWS_TOPIC_EXPANSIONS, NEWS_TOPIC_KEYWORD_HINTS, TOPIC_TO_TICKERS, is_options_native_metric,
        normalize_news_topic,
    )
    # Import local retrieval schemas.
    from .schema import (
        QueryIntent, MetadataExtraction, HyDEGeneration, FullTransformationResult, TimeWindow, BuilderQueryContract,
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
        NewsSemanticProfilePayload,
>>>>>>> Stashed changes
=======
        NewsSemanticProfilePayload,
>>>>>>> Stashed changes
        normalize_time_window_value,
    )
    # Import decoupled prompt templates.
    from ..core.prompt_templates import EXTRACTOR_SYSTEM_PROMPT, HYDE_WRITER_SYSTEM_PROMPT
except ImportError as e:
    try:
        # Fallback to absolute imports when running this file directly.
        from Scripts.core.financial_ontology import (
            ALLOWED_METRICS, ALLOWED_SOURCES, ALLOWED_CATEGORIES, EVENT_KEYWORDS_MAPPING,
            METRIC_TO_COLUMN_MAPPING, NEWS_TOPICS, NEWS_TOPIC_EXPANSIONS, NEWS_TOPIC_KEYWORD_HINTS, TOPIC_TO_TICKERS, is_options_native_metric,
            normalize_news_topic,
        )
        from Scripts.retrieval.schema import (
            QueryIntent, MetadataExtraction, HyDEGeneration, FullTransformationResult, TimeWindow, BuilderQueryContract,
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
            NewsSemanticProfilePayload,
>>>>>>> Stashed changes
=======
            NewsSemanticProfilePayload,
>>>>>>> Stashed changes
            normalize_time_window_value,
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

        # 1. 结构化抽取引擎
        self.extractor_llm = llm_pool.get_client(
            "query_extract",
            model=self.extractor_model_name,
        ).with_structured_output(MetadataExtraction)

        # 2. HyDE 生成引擎
        self.hyde_llm = llm_pool.get_client(
            "query_hyde",
            model=self.hyde_model_name,
        ).with_structured_output(HyDEGeneration)

        logger.info(
            "QueryTransformer init | extractor=%s | hyde=%s | openai_timeout=%s | base_url=%s",
            self.extractor_model_name,
            self.hyde_model_name,
            os.getenv("OPENAI_TIMEOUT_SECONDS", "60"),
            os.getenv("OPENAI_BASE_URL", "").strip() or "(default)",
        )

        self.allowed_tickers = self._load_allowed_tickers()
        self.covered_option_tickers = self._load_covered_option_tickers()
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

    def _load_covered_option_tickers(self) -> List[str]:
        """Authoritative covered universe for options-scope gating."""
        try:
            from Scripts.core.universe import universe as _universe
            return [t.upper() for t in _universe.get("options.scrape")]
        except Exception as e:
            logger.warning(
                f"Covered options universe unavailable ({e}); falling back to ETF + single-name allowlist only."
            )
            return [t for t in self.allowed_tickers if not str(t).startswith("^") and str(t) != "DX-Y.NYB"]

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

    @staticmethod
    def _dedupe_keep_order(values: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for raw in values or []:
            item = str(raw or "").strip()
            if item and item not in seen:
                out.append(item)
                seen.add(item)
        return out

    def _normalize_canonical_news_topics(self, metadata: MetadataExtraction) -> List[str]:
        topics: List[str] = []
        for raw_topic in getattr(metadata, "canonical_news_topics", []) or []:
            topic = normalize_news_topic(str(raw_topic or ""))
            if topic in NEWS_TOPICS:
                topics.append(topic)

        if topics:
            return self._dedupe_keep_order(topics)

        event_keyword = str(getattr(metadata, "event_keyword", "") or "").strip().lower()
        if event_keyword:
            mapped = EVENT_KEYWORDS_MAPPING.get(event_keyword, "")
            topic = normalize_news_topic(mapped or event_keyword)
            if topic in NEWS_TOPICS:
                topics.append(topic)

        source_types = {str(s).lower() for s in (getattr(metadata, "source_types", []) or [])}
        metrics = {str(m).strip().lower() for m in (getattr(metadata, "metrics", []) or [])}
        signals = [str(s).strip().lower() for s in (getattr(metadata, "signals", []) or []) if str(s).strip()]
        tickers = [str(t).upper().strip() for t in (getattr(metadata, "tickers", None) or []) if str(t).strip()]
        logical_reasoning = str(getattr(metadata, "logical_reasoning", "") or "").strip().lower()
        hint_parts = [
            logical_reasoning,
            " ".join(signals),
            " ".join(metrics),
            " ".join(tickers),
        ]
        hint_text = " ".join(part for part in hint_parts if part).strip().lower()

        if not topics and any(ticker in {"GLD", "SLV"} for ticker in tickers):
            topics.append("asset_precious_metals_spot")

        def _hint_matches(topic_name: str) -> bool:
            return any(token in hint_text for token in NEWS_TOPIC_KEYWORD_HINTS.get(topic_name, []))

        if _hint_matches("macro_yields_dollar"):
            topics.append("macro_yields_dollar")
        if _hint_matches("macro_central_banks"):
            topics.append("macro_central_banks")
        if _hint_matches("asset_metals_derivatives"):
            topics.append("asset_metals_derivatives")

        if not topics and ("gpr" in source_types or "gpr index" in metrics):
            topics.append("macro_geopolitics_risk")

        return self._dedupe_keep_order(topics)

    def _expanded_news_topics(self, metadata: MetadataExtraction) -> List[str]:
        canonical_topics = self._normalize_canonical_news_topics(metadata)
        if not canonical_topics:
            return []
        expanded_tail: List[str] = []
        canonical_set: Set[str] = set(canonical_topics)
        for topic in canonical_topics:
            for candidate in NEWS_TOPIC_EXPANSIONS.get(topic, [topic]):
                normalized = normalize_news_topic(candidate)
                if normalized in NEWS_TOPICS and normalized not in canonical_set and normalized not in expanded_tail:
                    expanded_tail.append(normalized)
        return [*canonical_topics, *expanded_tail]

    def _apply_news_semantic_profile(self, metadata: MetadataExtraction) -> MetadataExtraction:
        profile = build_news_semantic_profile(
            tickers=[str(t).upper().strip() for t in (getattr(metadata, "tickers", []) or []) if str(t).strip()],
            canonical_topics=list(getattr(metadata, "canonical_news_topics", []) or []),
            expanded_topics=list(getattr(metadata, "expanded_news_topics", []) or []),
        )
        payload = profile.model_dump()
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        metadata.news_semantic_profile = payload
        metadata.news_search_terms = list(payload.get("news_search_terms") or [])
        metadata.news_asset_terms = list(payload.get("news_asset_terms") or [])
        metadata.news_driver_terms = list(payload.get("news_driver_terms") or [])
=======
        metadata.news_semantic_profile = NewsSemanticProfilePayload.model_validate(payload)
        metadata.news_search_terms = list(payload.get("news_search_terms") or [])
        metadata.news_asset_terms = list(payload.get("news_asset_terms") or [])
        metadata.news_driver_terms = list(payload.get("news_driver_terms") or [])
        metadata.dense_context_terms = list(payload.get("dense_context_terms") or [])
>>>>>>> Stashed changes
=======
        metadata.news_semantic_profile = NewsSemanticProfilePayload.model_validate(payload)
        metadata.news_search_terms = list(payload.get("news_search_terms") or [])
        metadata.news_asset_terms = list(payload.get("news_asset_terms") or [])
        metadata.news_driver_terms = list(payload.get("news_driver_terms") or [])
        metadata.dense_context_terms = list(payload.get("dense_context_terms") or [])
>>>>>>> Stashed changes
        if not getattr(metadata, "canonical_news_topics", None):
            metadata.canonical_news_topics = list(payload.get("canonical_topics") or [])
            metadata.primary_news_topic = metadata.canonical_news_topics[0] if metadata.canonical_news_topics else ""
        if not getattr(metadata, "expanded_news_topics", None):
            metadata.expanded_news_topics = list(payload.get("expanded_topics") or [])
        return metadata

<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
=======
>>>>>>> Stashed changes
    def _apply_narrative_option_posture_metrics(self, metadata: MetadataExtraction) -> MetadataExtraction:
        """Attach GLD/SLV options posture metrics without changing family routing."""

        source_types = {str(s).strip().lower() for s in (getattr(metadata, "source_types", []) or []) if str(s).strip()}
        primary_surface = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
        primary_theme = str(getattr(metadata, "primary_theme", "") or "").strip().lower()
        if "options" in source_types or primary_surface == "options_surface" or primary_theme == "options":
            return metadata
        if not ({"news", "macro_history"} & source_types):
            return metadata

        topics = set(getattr(metadata, "canonical_news_topics", []) or []) | set(getattr(metadata, "expanded_news_topics", []) or [])
        tickers = [str(t).upper().strip() for t in (getattr(metadata, "tickers", []) or []) if str(t).strip()]
        is_metals_narrative = bool({"asset_precious_metals_spot", "asset_metals_derivatives", "macro_yields_dollar"} & topics)
        if not is_metals_narrative and not any(ticker in {"GLD", "SLV"} for ticker in tickers):
            return metadata

        enriched_metrics = [
            *list(getattr(metadata, "metrics", []) or []),
            *narrative_option_posture_metrics_for_tickers(tickers),
        ]
        metadata.metrics = self._dedupe_keep_order([str(metric).strip() for metric in enriched_metrics if str(metric).strip()])
        return metadata

<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
    @staticmethod
    def _has_explicit_gpr_intent(metadata: MetadataExtraction, query: str = "") -> bool:
        metrics = {str(m).strip().lower() for m in (getattr(metadata, "metrics", []) or [])}
        signals = {str(s).strip().lower() for s in (getattr(metadata, "signals", []) or []) if str(s).strip()}
        query_l = str(query or "").strip().lower()
        return (
            "gpr index" in metrics
            or "gpr context" in signals
            or "gpr context" in query_l
            or "gpr index" in query_l
            or "geopolitics news" in query_l
            or "geopolitical" in query_l
        )

    def _normalize_structural_contract(
        self,
        metadata: MetadataExtraction,
        query: str,
    ) -> MetadataExtraction:
        source_types = self._dedupe_keep_order(
            [str(s).strip().lower() for s in (getattr(metadata, "source_types", []) or []) if str(s).strip()]
        )
        metrics = {str(m).strip() for m in (getattr(metadata, "metrics", []) or []) if str(m).strip()}
        metrics_l = {metric.lower() for metric in metrics}
        signals = {
            str(signal).strip().lower()
            for signal in (getattr(metadata, "signals", []) or [])
            if str(signal).strip()
        }
        canonical_topics = set(getattr(metadata, "canonical_news_topics", []) or [])
        query_l = str(query or "").strip().lower()
        explicit_gpr_intent = self._has_explicit_gpr_intent(metadata, query)

<<<<<<< Updated upstream
<<<<<<< Updated upstream
        has_options_native_metric = any(is_options_native_metric(metric) for metric in metrics)
        if "options" in source_types or has_options_native_metric:
            normalized_sources: List[str] = ["options"]
            if "news" in source_types:
                normalized_sources.append("news")
            if "macro_history" in source_types:
                normalized_sources.append("macro_history")
            metadata.source_types = normalized_sources
            metadata.primary_surface = "options_surface"
            metadata.primary_theme = "options"
            metadata.analysis_surfaces = self._dedupe_keep_order(
                [surface for surface in (getattr(metadata, "analysis_surfaces", []) or []) if str(surface).strip().lower() != "geopolitical_context"]
            )
            return metadata

=======
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        cross_asset_macro_news = (
            "news" in source_types
            and not explicit_gpr_intent
            and (
                "macro regime narrative" in signals
                or "macro regime" in query_l
                or "macro_yields_dollar" in canonical_topics
                or "macro_central_banks" in canonical_topics
                or "asset_precious_metals_spot" in canonical_topics
            )
        )
        if cross_asset_macro_news:
            metadata.source_types = ["macro_history", "news"]
            metadata.primary_theme = "cross_asset"
            if str(getattr(metadata, "primary_surface", "") or "").strip().lower() != "options_surface":
                metadata.primary_surface = "macro_news_surface"
            metadata.analysis_surfaces = self._dedupe_keep_order(
                [surface for surface in (getattr(metadata, "analysis_surfaces", []) or []) if str(surface).strip().lower() != "geopolitical_context"]
            )
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
=======
>>>>>>> Stashed changes
            return metadata

        has_options_native_metric = any(is_options_native_metric(metric) for metric in metrics)
        if "options" in source_types or has_options_native_metric:
            normalized_sources: List[str] = ["options"]
            if "news" in source_types:
                normalized_sources.append("news")
            if "macro_history" in source_types:
                normalized_sources.append("macro_history")
            metadata.source_types = normalized_sources
            metadata.primary_surface = "options_surface"
            metadata.primary_theme = "options"
            metadata.analysis_surfaces = self._dedupe_keep_order(
                [surface for surface in (getattr(metadata, "analysis_surfaces", []) or []) if str(surface).strip().lower() != "geopolitical_context"]
            )
            return metadata
<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        return metadata

    @staticmethod
    def _coerce_builder_contract(raw_contract: dict | None) -> BuilderQueryContract | None:
        if not isinstance(raw_contract, dict) or not raw_contract:
            return None
        payload = dict(raw_contract)
        if "time_window" in payload:
            payload["time_window"] = normalize_time_window_value(payload.get("time_window"), default=TimeWindow.PAST_WEEK.value)
        try:
            return BuilderQueryContract.model_validate(payload)
        except Exception as e:
            logger.warning(f"⚠️ Builder contract ignored: {e}")
            return None

    @staticmethod
    def _normalize_requested_sec_forms(values: List[Any] | None) -> List[str]:
        out: List[str] = []
        for raw in values or []:
            form = str(getattr(raw, "value", raw) or "").strip().upper()
            if form in {"8-K", "4"} and form not in out:
                out.append(form)
        return out

    def _apply_builder_contract(
        self,
        metadata: MetadataExtraction,
        builder_contract: BuilderQueryContract | None,
    ) -> MetadataExtraction:
        if builder_contract is None:
            return metadata

        if builder_contract.tickers:
            metadata.tickers = [str(t).upper().strip() for t in builder_contract.tickers if str(t).strip()]
        if getattr(builder_contract, "signals", None):
            merged_signals = [
                *getattr(metadata, "signals", []),
                *[str(signal).strip() for signal in (builder_contract.signals or []) if str(signal).strip()],
            ]
            metadata.signals = self._dedupe_keep_order(merged_signals)
        if builder_contract.metrics:
            merged_metrics = [*metadata.metrics, *[str(m).strip() for m in builder_contract.metrics if str(m).strip()]]
            metadata.metrics = self._dedupe_keep_order(merged_metrics)
        if builder_contract.source_types:
            metadata.source_types = self._dedupe_keep_order(
                [str(s).strip().lower() for s in builder_contract.source_types if str(s).strip()]
            )
        if builder_contract.requested_sec_forms:
            metadata.requested_sec_forms = self._normalize_requested_sec_forms(
                [*getattr(metadata, "requested_sec_forms", []), *builder_contract.requested_sec_forms]
            )
        if builder_contract.time_window:
            metadata.time_window = builder_contract.time_window
        if metadata.requested_sec_forms:
            if len(metadata.requested_sec_forms) == 1:
                metadata.form_type = metadata.requested_sec_forms[0]
            else:
                metadata.form_type = "ALL"

        return metadata

    def _resolve_primary_theme(self, metadata: MetadataExtraction) -> str:
        source_types = {str(s).lower() for s in (getattr(metadata, "source_types", []) or [])}
        metrics = {str(m).strip().lower() for m in (getattr(metadata, "metrics", []) or [])}
        canonical_topics = set(self._normalize_canonical_news_topics(metadata))
        signals = {str(s).strip().lower() for s in (getattr(metadata, "signals", []) or []) if str(s).strip()}
        explicit_gpr_intent = (
            "gpr index" in metrics
            or "gpr context" in signals
        )
        explicit_macro_news_narrative = (
            "news" in source_types
            and "macro_history" in source_types
            and "gpr" not in source_types
            and "macro regime narrative" in signals
            and "news narrative" in signals
        )
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        if ("options" in source_types or any(is_options_native_metric(metric) for metric in metrics)) and not explicit_gpr_intent:
            return "options"
        candidate = str(getattr(metadata, "primary_theme", "") or "").strip().lower()
        if explicit_macro_news_narrative:
            return "cross_asset"
=======
        candidate = str(getattr(metadata, "primary_theme", "") or "").strip().lower()
        if explicit_macro_news_narrative:
            return "cross_asset"
=======
        candidate = str(getattr(metadata, "primary_theme", "") or "").strip().lower()
        if explicit_macro_news_narrative:
            return "cross_asset"
>>>>>>> Stashed changes
        if candidate == "cross_asset" and "news" in source_types and "macro_history" in source_types:
            return "cross_asset"
        if ("options" in source_types or any(is_options_native_metric(metric) for metric in metrics)) and not explicit_gpr_intent:
            return "options"
<<<<<<< Updated upstream
>>>>>>> Stashed changes
=======
>>>>>>> Stashed changes
        if candidate in {"insider", "geopolitics", "cross_asset", "options"}:
            return candidate
        if "sec" in source_types or {"sec form 4 insider flow", "sec 8-k event risk"} & signals:
            return "insider"
        if explicit_gpr_intent or "macro_geopolitics_risk" in canonical_topics:
            return "geopolitics"
        if "macro_history" in source_types or "macro regime narrative" in signals:
            return "cross_asset"
        return "options"

    def _resolve_primary_surface(self, metadata: MetadataExtraction) -> str:
        source_types = {str(s).lower() for s in (getattr(metadata, "source_types", []) or [])}
        metrics = [str(m) for m in (getattr(metadata, "metrics", []) or [])]
<<<<<<< Updated upstream
<<<<<<< Updated upstream
        if "options" in source_types or any(is_options_native_metric(metric) for metric in metrics):
            return "options_surface"
        candidate = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
=======
        candidate = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
        if candidate == "macro_news_surface" and "news" in source_types and "macro_history" in source_types:
            return "macro_news_surface"
        if "options" in source_types or any(is_options_native_metric(metric) for metric in metrics):
            return "options_surface"
>>>>>>> Stashed changes
=======
        candidate = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
        if candidate == "macro_news_surface" and "news" in source_types and "macro_history" in source_types:
            return "macro_news_surface"
        if "options" in source_types or any(is_options_native_metric(metric) for metric in metrics):
            return "options_surface"
>>>>>>> Stashed changes
        if candidate in {"options_surface", "macro_news_surface"}:
            return candidate
        return "macro_news_surface"

    def _resolve_asset_scope(
        self,
        metadata: MetadataExtraction,
        *,
        comparison_targets: List[str],
    ) -> str:
        candidate = str(getattr(metadata, "asset_scope", "") or "").strip().lower()
        if candidate in {"single_name", "benchmark", "basket"}:
            return candidate
        tickers = [str(t).upper().strip() for t in (getattr(metadata, "tickers", []) or []) if str(t).strip()]
        unique_tickers = list(dict.fromkeys(tickers))
        if comparison_targets:
            return "benchmark"
        if len(unique_tickers) == 1:
            return "single_name"
        if len(unique_tickers) > 1:
            return "basket"
        return "unspecified"

    def _resolve_read_profile(
        self,
        metadata: MetadataExtraction,
        *,
        primary_theme: str,
        primary_surface: str,
    ) -> str:
        metrics = {str(m).strip().lower() for m in (getattr(metadata, "metrics", []) or [])}
        posture_metrics = {
            "put/call ratio",
            "options liquidity",
            "open interest",
            "options volume",
            "iv skew",
            "implied volatility",
            "implied volatility (iv)",
        }
        posture_metric_hits = {metric for metric in metrics if metric in posture_metrics}

        def _structural_options_profile() -> str:
            if primary_theme == "insider":
                return "event_risk"
            if primary_surface != "options_surface":
                return "board_state"
            if len(posture_metric_hits) >= 2:
                return "posture_read"
            if any(metric in posture_metric_hits for metric in {"put/call ratio", "options liquidity"}):
                return "posture_read"
            return "board_state"

        candidate = str(getattr(metadata, "read_profile", "") or "").strip().lower()
        if candidate in {"posture_read", "event_risk"}:
            return candidate
        if candidate == "structure_request":
            return _structural_options_profile()
        return _structural_options_profile()

    def _normalize_comparison_targets(
        self,
        metadata: MetadataExtraction,
        *,
        primary_ticker: str,
    ) -> List[str]:
        explicit = [str(t).upper().strip() for t in (getattr(metadata, "comparison_targets", []) or []) if str(t).strip()]
        derived = [str(t).upper().strip() for t in (getattr(metadata, "tickers", []) or [])[1:] if str(t).strip()]
        candidates = explicit + derived
        out: List[str] = []
        for ticker in candidates:
            if ticker and ticker != primary_ticker and ticker in self.allowed_tickers and ticker not in out:
                out.append(ticker)
        return out

    def _resolve_analysis_surfaces(
        self,
        metadata: MetadataExtraction,
        *,
        primary_theme: str,
        primary_surface: str,
        comparison_targets: List[str],
    ) -> List[str]:
        surfaces: List[str] = []
        requested = [str(s).strip().lower() for s in (getattr(metadata, "analysis_surfaces", []) or []) if str(s).strip()]
        source_types = {str(s).lower() for s in (getattr(metadata, "source_types", []) or [])}
        metrics = {str(m).strip().lower() for m in (getattr(metadata, "metrics", []) or [])}
        signals = {str(s).strip().lower() for s in (getattr(metadata, "signals", []) or []) if str(s).strip()}
        explicit_gpr_intent = (
            "gpr" in source_types
            or "gpr index" in metrics
            or "gpr context" in signals
        )
        valid_requested = [
            surface for surface in requested
            if surface in {"insider_signal", "options_surface", "macro_context", "benchmark_context"}
            or (surface == "geopolitical_context" and (primary_theme == "geopolitics" or explicit_gpr_intent))
        ]
        surfaces.extend(valid_requested)
        if primary_theme == "insider":
            surfaces.append("insider_signal")
        if primary_surface == "options_surface":
            surfaces.append("options_surface")
        if primary_theme == "geopolitics":
            surfaces.append("geopolitical_context")
        if primary_theme == "cross_asset" or "macro_history" in {str(s).lower() for s in (getattr(metadata, "source_types", []) or [])}:
            surfaces.append("macro_context")
        if comparison_targets:
            surfaces.append("benchmark_context")
        return self._dedupe_keep_order(surfaces)

    def _build_supporting_contracts(
        self,
        metadata: MetadataExtraction,
        *,
        primary_theme: str,
        primary_surface: str,
    ) -> List[dict]:
        topics = self._normalize_canonical_news_topics(metadata)
        if primary_theme != "geopolitics" or primary_surface != "macro_news_surface" or not topics:
            return []
        supporting: List[dict] = []
        for topic in topics:
            compensation_targets = [ticker for ticker in TOPIC_TO_TICKERS.get(topic, []) if ticker in self.allowed_tickers]
            if compensation_targets:
                supporting.append(
                    {
                        "contract_type": "silver_enrichment",
                        "topic": topic,
                        "source_type": "macro_history",
                        "compensation_targets": compensation_targets,
                    }
                )
        return supporting

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
        """Deterministic minimal fallback to keep the pipeline alive."""
        tokens = {
            token.strip(" \t\r\n,;:!?()[]{}'\"").upper()
            for token in str(query or "").split()
        }
        matched_tickers = sorted({
            ticker for ticker in self.allowed_tickers
            if ticker.upper() in tokens
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
            primary_theme="cross_asset",
            primary_surface="macro_news_surface",
            canonical_news_topics=[],
            analysis_surfaces=["macro_context"],
            comparison_targets=[],
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
                "allowed_news_topics": ", ".join(NEWS_TOPICS),
            })
            return result
        except Exception as e:
            logger.error(f"Stage 1 API Extraction failed: {e}. Switching to deterministic fallback.")
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

    async def transform_for_dual_rag(
        self,
        query: str,
        intent: QueryIntent,
        query_builder_contract: dict | None = None,
    ) -> FullTransformationResult:
        start_time = datetime.now()
        logger.info(f"🚀 Starting Two-Stage Pipeline for query: {query[:50]}...")

        try:
            # ==========================================
            # STAGE 1: Extract Metadata 
            # ==========================================
            metadata: MetadataExtraction = await self._stage1_extract_metadata(query)
            metadata = self._apply_builder_contract(
                metadata,
                self._coerce_builder_contract(query_builder_contract),
            )
            if not metadata.requested_sec_forms:
                inferred_form = str(getattr(metadata, "form_type", "") or "").strip().upper()
                if inferred_form in {"8-K", "4"}:
                    metadata.requested_sec_forms = [inferred_form]
            
            # [GUARDRAIL 1] Post-clean extracted tickers using allowlist.
            normalized_tickers = [str(t).upper().strip() for t in (metadata.tickers or []) if str(t).strip()]
            valid_tickers = [t for t in normalized_tickers if t in self.allowed_tickers]
            invalid_tickers = [t for t in normalized_tickers if t not in self.allowed_tickers]
            if invalid_tickers:
                logger.warning(f"⚠️ Guardrail triggered: Dropped invalid tickers: {set(invalid_tickers)}")
            in_scope_tickers = [t for t in valid_tickers if t in self.covered_option_tickers]
            out_of_scope_tickers = invalid_tickers + [t for t in valid_tickers if t not in self.covered_option_tickers]
            metadata.tickers = in_scope_tickers

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

            primary_ticker = metadata.tickers[0] if metadata.tickers else ""
            metadata.canonical_news_topics = self._normalize_canonical_news_topics(metadata)
            metadata.primary_news_topic = metadata.canonical_news_topics[0] if metadata.canonical_news_topics else ""
            metadata.expanded_news_topics = self._expanded_news_topics(metadata)
            metadata = self._apply_news_semantic_profile(metadata)
<<<<<<< Updated upstream
<<<<<<< Updated upstream
=======
            metadata = self._apply_narrative_option_posture_metrics(metadata)
>>>>>>> Stashed changes
=======
            metadata = self._apply_narrative_option_posture_metrics(metadata)
>>>>>>> Stashed changes
            metadata = self._normalize_structural_contract(metadata, query)
            metadata.primary_theme = self._resolve_primary_theme(metadata)
            metadata.primary_surface = self._resolve_primary_surface(metadata)
            metadata.comparison_targets = self._normalize_comparison_targets(
                metadata,
                primary_ticker=primary_ticker,
            )
            metadata.asset_scope = self._resolve_asset_scope(
                metadata,
                comparison_targets=metadata.comparison_targets,
            )
            metadata.read_profile = self._resolve_read_profile(
                metadata,
                primary_theme=metadata.primary_theme,
                primary_surface=metadata.primary_surface,
            )
            metadata.analysis_surfaces = self._resolve_analysis_surfaces(
                metadata,
                primary_theme=metadata.primary_theme,
                primary_surface=metadata.primary_surface,
                comparison_targets=metadata.comparison_targets,
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
                if getattr(metadata, "news_search_terms", None):
                    parts.extend(list(metadata.news_search_terms)[:8])
                hyde_rr = " ".join(parts) if parts else query
            # [GUARDRAIL 5] News rerank-query override — conditional on LLM quality.
            # expanded_news_rerank_query produces a keyword-bag (asset + driver terms)
            # optimised for SPLADE sparse search. However, it yields ~12 tokens even
            # after Fix 2b, which is still heavier than the ~5-token LLM output.
            # When Stage 1 succeeded and produced a substantive hyde_rr (>= 3 words),
            # trust it and skip the override: the LLM already wrote news-specific
            # language ("GLD macro regime narrative news narrative") that SPLADE can
            # use efficiently. Only fall back to the keyword expansion when:
<<<<<<< Updated upstream
<<<<<<< Updated upstream
            #   a) Stage 1 failed (regex fallback — logical_reasoning carries the marker), OR
=======
            #   a) Stage 1 failed (deterministic fallback — logical_reasoning carries the marker), OR
>>>>>>> Stashed changes
=======
            #   a) Stage 1 failed (deterministic fallback — logical_reasoning carries the marker), OR
>>>>>>> Stashed changes
            #   b) The LLM returned a trivially short or empty rerank query (< 3 words).
            _is_stage1_fallback = "Fallback extraction" in (getattr(metadata, "logical_reasoning", "") or "")
            _hyde_rr_is_trivial = len(str(hyde_rr or "").split()) < 3
            if "news" in {str(s).lower() for s in (getattr(metadata, "source_types", []) or [])}:
                if _is_stage1_fallback or _hyde_rr_is_trivial:
                    profile_query = expanded_news_rerank_query(query, getattr(metadata, "news_semantic_profile", {}) or {})
                    if profile_query:
                        hyde_rr = profile_query
                        hyde_para = (
                            f"Recent financial news on {profile_query} frames the macro narrative, "
                            "asset reaction, and risk transmission relevant to the requested market read."
                        )
            requested_sec_forms = self._normalize_requested_sec_forms(getattr(metadata, "requested_sec_forms", []) or [])
            form_type = str(getattr(metadata, "form_type", "") or "").strip().upper()
            if set(requested_sec_forms) == {"8-K", "4"}:
                ticker_str = ", ".join(metadata.tickers) if metadata.tickers else "the selected issuer"
                tw_val = getattr(metadata.time_window, "value", metadata.time_window) or "recent"
                hyde_para = (
                    f"Analysis of {ticker_str} SEC filing evidence over the {tw_val} window focuses on "
                    f"both 8-K issuer event filings and Form 4 insider flow, preserving filing subtype distinctions "
                    f"for the query."
                )
                hyde_rr = f"{ticker_str} SEC filings 8-K Form 4".strip()
            elif requested_sec_forms == ["4"] or form_type == "4":
                ticker_str = ", ".join(metadata.tickers) if metadata.tickers else "the selected issuer"
                tw_val = getattr(metadata.time_window, "value", metadata.time_window) or "recent"
                hyde_para = (
                    f"Analysis of {ticker_str} SEC Form 4 filing evidence over the {tw_val} window focuses on "
                    f"insider transaction disclosures while preserving transaction subtype details for the query."
                )
                hyde_rr = f"{ticker_str} SEC Form 4 filings".strip()
            elif form_type == "8-K":
                ticker_str = ", ".join(metadata.tickers) if metadata.tickers else "the selected issuer"
                tw_val = getattr(metadata.time_window, "value", metadata.time_window) or "recent"
                hyde_para = (
                    f"Analysis of {ticker_str} SEC 8-K filing evidence over the {tw_val} window focuses on "
                    f"issuer event filings, filing topics, and filing-driven implications relevant to the query."
                )
                hyde_rr = f"{ticker_str} SEC 8-K filings".strip()
            hyde_result = HyDEGeneration(hyde_paragraph=hyde_para, rerank_query=hyde_rr)

            # Aggregate stage outputs into one typed payload.
            final_result = FullTransformationResult(
                metadata=metadata,
                hyde=hyde_result,
                mapped_physical_columns=mapped_physical_columns,
                in_scope_tickers=in_scope_tickers,
                out_of_scope_tickers=out_of_scope_tickers,
                supporting_contracts=self._build_supporting_contracts(
                    metadata,
                    primary_theme=metadata.primary_theme,
                    primary_surface=metadata.primary_surface,
                ),
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
