"""
Scripts/retrieval/master_retriever.py

Financial RAG orchestration layer (Master Retriever).

Design patterns: Facade + Wrapper.

Core capabilities (this version):
1.  Prompt decoupling — all prompts live under Scripts/core/.
2.  Per-engine timeouts — Gold (vector) and Silver (SQL) get independent
    asyncio.wait_for budgets so one slow engine never stalls the other.
3.  Graceful degradation — engine failures surface as structured error dicts
    so the Analyst can honestly say "INSUFFICIENT DATA" instead of hallucinating.
4.  End-to-end telemetry — per-node latency is persisted on the return payload.
5.  **Semantic-SQL Parallelism** (new): under `sql_only` we also fire HyDE so
    the Analyst has LLM prior knowledge to hedge when Parquet returns empty.
6.  **HyDE Entity Back-Injection** (new): under `vector_only` we regex-extract
    tickers from the HE paragraph, intersect with the ontology whitelist, and
    trigger compensation Silver queries for the *novel* tickers. This turns
    "no data" into "Silver penetration via semantic expansion".
7.  **HE Guardrail** (new): HE-extracted tickers MUST pass the ontology
    whitelist before they can touch Parquet. No whitelist, no SQL.
8.  **Source-channel tagging** (new): every slice of silver_context is tagged
    with `source_channel ∈ {primary, hyde_expansion}` so the Checker agent
    can relax numeric audit for HE-triggered data (avoiding false fatal).
9.  **Time-range audit** (new): the final `start_date` / `end_date` used by
    Silver is exposed on the return payload so the Analyst can print the
    compliance footer ("data interval: YYYY-MM-DD ~ YYYY-MM-DD").
"""

import asyncio
import copy
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from Scripts.core.few_shot_intent import INTENT_FEW_SHOT_EXAMPLES
from Scripts.core.intent_router_prompt_templates import ROUTER_SYSTEM_PROMPT
from Scripts.retrieval.qdrant_retriever import FinancialHybridRetriever
from Scripts.retrieval.query_transform import QueryTransformer
from Scripts.retrieval.schema import (
    FullTransformationResult,
    MetadataExtraction,
    QueryIntent,
    RetrievalOutcome,
    ScopeContract,
    SourceCoverageContract,
    TimeContract,
    TimeWindow,
    # Re-exported global time-window policy. The physical constants live in
    # schema.py to keep Gold/Silver free of a circular import against this
    # facade; `from Scripts.retrieval.master_retriever import TIME_WINDOW_DAYS`
    # still works for downstream callers.
    TIME_WINDOW_DAYS,
    time_window_to_days,
)
from Scripts.retrieval.sql_tools import SilverSQLTool
from Scripts.retrieval.time_adapter import (
    SourceTimeKey,
    TimePredicate,
    compile_all as compile_all_time_predicates,
)
from Scripts.core.financial_ontology import (
    ALLOWED_METRICS,
    ALLOWED_SOURCES,
    METRIC_TO_COLUMN_MAPPING,
    NEWS_TOPICS,
    SEC_ACTION_TAXONOMY,
    TOPIC_IMPACT_BASKETS,
    TOPIC_TO_TICKERS,
    is_options_native_metric,
    missing_slots_for_query_family,
    normalize_news_topic,
    query_slots_for_family,
)
from Scripts.core.financial_reasoning_contract import build_data_capability_profile
from Scripts.core.evidence_contracts import build_slot_evidence_contracts, canonical_query_family, evaluate_retrieval_slot_support
from Scripts.core.liquidity_policy import resolve_primary_ticker
from Scripts.core.sec_analysis import compose_sec_analysis_bundle
from Scripts.observability.audit import append_audit_jsonl

# Public re-export surface — keeps `__all__` explicit for static analysers.
__all__ = ["MasterRetriever", "TIME_WINDOW_DAYS", "time_window_to_days"]

_BRONZE_SEC_ROOT = Path(__file__).resolve().parents[2] / "Data" / "1_Bronze_Raw" / "SEC_Parsed_JSON"

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (tuned to real-world Ollama throughput, not defaults)
# ---------------------------------------------------------------------------

# Top_k for the Gold probe that runs when the router chose sql_only. Purpose:
# if SQL succeeds the LLM will prefer precise numerics; if SQL fails there is
# always at least one news anchor. This does NOT change business routing.
_SQL_ONLY_FALLBACK_TOPK = 2

# Max number of novel tickers the HE back-injection can promote to a real
# Silver compensation query. A hard cap protects the silver_timeout budget:
# three tickers × ~8s each is already on the order of one-third of a browser's
# patience threshold.
_HE_NOVEL_TICKERS_CAP = int(os.getenv("HE_NOVEL_TICKERS_CAP", "3"))
_MACRO_ONLY_HINT_TERMS = (
    "fomc", "federal reserve", "fed decision", "rate decision",
    "10-year treasury", "10y treasury", "treasury yield", "yields",
    "dot plot", "policy rate",
)
_MACRO_ONLY_SAFE_TICKERS = {"SPY", "QQQ", "IWM", "GLD", "SLV"}
_EXPLICIT_GOLD_CONTEXT_PHRASES = (
    "headline",
    "headlines",
    "news impact",
    "headline impact",
    "event driver",
    "event-driven",
    "event driven",
    "catalyst",
    "catalysts",
    "what changed",
    "why today",
    "news",
)
# ---------------------------------------------------------------------------
# Ticker / macro parsing — delegated to `Scripts.retrieval.macro_parser`.
#
# Rationale: the regex tables + macro markdown parser are orthogonal to
# the retrieval orchestration logic in this file. Moving them to a
# dedicated module keeps `master_retriever.py` focused on *wiring*
# (router + Qdrant + SQL) while enabling isolated unit tests.
#
# The underscored aliases below are preserved for backwards compatibility
# with any downstream code or test that imported the private names from
# this module; new callers should import directly from
# `Scripts.retrieval.macro_parser`.
# ---------------------------------------------------------------------------
from Scripts.retrieval.macro_parser import (  # noqa: E402 — kept near usage
    MACRO_GENERATED_RE as _MACRO_GENERATED_RE,
    MACRO_LINE_PATTERNS as _MACRO_LINE_PATTERNS,
    TICKER_RE as _TICKER_RE,
    TICKER_STOPWORDS as _TICKER_STOPWORDS,
    build_macro_silver_patch as _build_macro_silver_patch,
    parse_macro_snapshot as _parse_macro_snapshot,
)


def _merge_citation_contract(
    base: Optional[Dict[str, Any]],
    extra: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(base or {})
    for metric_key, entry in (extra or {}).items():
        merged[str(metric_key)] = dict(entry or {})
    return merged


def _deprecated_anchor_map_from_contract(
    citation_contract: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for metric_key, entry in (citation_contract or {}).items():
        preferred = (entry or {}).get("preferred_anchor")
        if preferred:
            out[str(metric_key)] = str(preferred)
    return out


def _extend_silver_context_contract(
    sql_tool: SilverSQLTool,
    silver_ctx: Dict[str, Any],
    *,
    values: Dict[str, Any],
    lineage_anchors: List[str],
    observed_at: Optional[str],
    source_channel: str,
    explicit_contract: Optional[Dict[str, Any]] = None,
) -> None:
    """Extend Silver context with canonical citation metadata for new values."""
    contract = explicit_contract or sql_tool._build_citation_contract(
        values,
        lineage_anchors=lineage_anchors,
        observed_at=observed_at,
        source_channel=source_channel,
    )
    silver_ctx["citation_contract"] = _merge_citation_contract(
        silver_ctx.get("citation_contract"),
        contract,
    )
    silver_ctx["citation_anchor_map"] = {
        **dict(silver_ctx.get("citation_anchor_map") or {}),
        **_deprecated_anchor_map_from_contract(contract),
    }


class MasterRetriever:
    """Facade over QueryTransformer + Qdrant + Silver SQL with per-route
    retrieval plans and full HyDE semantic-hedge support.
    """

    def __init__(self):
        # --- 1. Engine initialisation (heavy) ---
        self.qdrant = FinancialHybridRetriever()
        self.sql_tool = SilverSQLTool()
        self.transformer = QueryTransformer()

        # --- 2. Router LLM configuration (cheap, short responses) ---
        router_provider = os.getenv("ROUTER_PROVIDER", "").strip().lower()
        if router_provider not in {"openai", "ollama"}:
            router_provider = "openai"
        self.router_provider = router_provider

        default_openai_model = os.getenv("ROUTER_OPENAI_FALLBACK_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
        default_ollama_model = os.getenv("OLLAMA_ROUTER_MODEL", "llama3:latest").strip() or "llama3:latest"
        self.router_model_name = os.getenv("ROUTER_MODEL", "").strip() or (
            default_openai_model if self.router_provider == "openai" else default_ollama_model
        )
        self.router_llm = self._build_router_llm(self.router_provider, self.router_model_name)

        self.router_fallback_enabled = os.getenv(
            "ROUTER_ENABLE_MODEL_FALLBACK",
            os.getenv("ROUTER_OPENAI_FALLBACK_ENABLED", "1"),
        ) == "1"
        self.router_fallback_provider = "ollama" if self.router_provider == "openai" else "openai"
        self.router_fallback_model = (
            default_ollama_model
            if self.router_fallback_provider == "ollama"
            else default_openai_model
        )
        self.router_fallback_llm = None
        if self.router_fallback_enabled:
            self.router_fallback_llm = self._build_router_llm(
                self.router_fallback_provider,
                self.router_fallback_model,
            )

        # --- 3. Runtime knobs (env-tunable without code change) ---
        self.gold_timeout = float(os.getenv("GOLD_TIMEOUT", 15.0))
        self.silver_timeout = float(os.getenv("SILVER_TIMEOUT", 10.0))

        logger.info(
            f"🏛️ MasterRetriever ready | Gold_TO: {self.gold_timeout}s | "
            f"Silver_TO: {self.silver_timeout}s | HE_novel_cap: {_HE_NOVEL_TICKERS_CAP}"
        )

    def _build_router_llm(self, provider: str, model_name: str):
        temperature = float(os.getenv("ROUTER_TEMPERATURE", 0.0))
        if provider == "ollama":
            return ChatOllama(
                model=model_name,
                temperature=temperature,
                num_predict=20,
            )

        openai_kwargs: Dict[str, Any] = {
            "model": model_name,
            "temperature": temperature,
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "timeout": float(os.getenv("ROUTER_OPENAI_TIMEOUT_SECONDS", "40")),
        }
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        if openai_base_url:
            openai_kwargs["base_url"] = openai_base_url
        return ChatOpenAI(**openai_kwargs)

    # ======================================================================
    # A. Intent classification (unchanged — proven in production)
    # ======================================================================

    async def _classify_intent(self, query: str) -> QueryIntent:
        """Lightweight intent routing after prompt decoupling."""
        prompt = ChatPromptTemplate.from_template(ROUTER_SYSTEM_PROMPT)
        chain = prompt | self.router_llm

        start_t = time.time()
        try:
            response = await chain.ainvoke({
                "examples": INTENT_FEW_SHOT_EXAMPLES,
                "query": query,
            })
            intent_str = response.content.strip().lower()

            route = "hybrid_both"
            for p in ["sql_only", "vector_only", "hybrid_both"]:
                if p in intent_str:
                    route = p
                    break

            logger.info(f"⚡ [Telemetry] Router choice: {route} | Cost: {time.time() - start_t:.3f}s")
            return QueryIntent(primary_route=route)
        except Exception as e:
            if self.router_fallback_enabled and self.router_fallback_llm is not None:
                try:
                    logger.warning(
                        f"⚠️ [Router] {self.router_provider} failed ({type(e).__name__}); "
                        f"retrying with {self.router_fallback_provider} fallback model={self.router_fallback_model}"
                    )
                    fb_response = await (prompt | self.router_fallback_llm).ainvoke({
                        "examples": INTENT_FEW_SHOT_EXAMPLES,
                        "query": query,
                    })
                    intent_str = getattr(fb_response, "content", str(fb_response)).strip().lower()
                    route = "hybrid_both"
                    for p in ["sql_only", "vector_only", "hybrid_both"]:
                        if p in intent_str:
                            route = p
                            break
                    logger.info(
                        f"⚡ [Telemetry] Router fallback choice: {route} | Cost: {time.time() - start_t:.3f}s"
                    )
                    return QueryIntent(primary_route=route)
                except Exception as fallback_err:
                    logger.error(
                        f"❌ [Router Fallback Error] {fallback_err}. "
                        "Falling back to hybrid."
                    )
            logger.error(f"❌ [Router Error] {e}. Falling back to hybrid.")
            return QueryIntent(primary_route="hybrid_both")

    def _apply_macro_only_source_hint(self, query: str, metadata) -> None:
        """Drop SEC source hint for macro-rate queries on broad ETFs/indexes.

        This prevents accidental SEC over-filtering in Gold retrieval for queries
        like "FOMC + 10Y yields + SPY puts", where SEC is usually irrelevant.
        """
        if metadata is None:
            return
        q = (query or "").lower()
        if not any(t in q for t in _MACRO_ONLY_HINT_TERMS):
            return
        tickers = [str(t).upper() for t in (getattr(metadata, "tickers", None) or [])]
        if tickers and not all(t in _MACRO_ONLY_SAFE_TICKERS or t.startswith("^") for t in tickers):
            return

        raw_sources = list(getattr(metadata, "source_types", None) or [])
        norm_sources = [getattr(s, "value", s) for s in raw_sources]
        if "sec" in [str(s).lower() for s in norm_sources]:
            metadata.source_types = [s for s in norm_sources if str(s).lower() != "sec"]
            logger.info(
                "🧭 [MacroOnlyHint] Dropped SEC source_type for macro-rate query | "
                f"tickers={tickers or ['(none)']} | sources={metadata.source_types}"
            )

    @staticmethod
    def _metadata_source_values(metadata: MetadataExtraction | None) -> List[str]:
        raw = list(getattr(metadata, "source_types", None) or [])
        values = [str(getattr(s, "value", s)).lower() for s in raw]
        supported = set(ALLOWED_SOURCES) | {"options", "macro_history"}
        ordered: List[str] = []
        for item in values:
            if item in supported and item not in ordered:
                ordered.append(item)
        return ordered

    @classmethod
    def _strict_source_hits(
        cls,
        *,
        metadata: MetadataExtraction | None,
        strict_sources: List[str],
        silver_context: Dict[str, Any],
        gold_context: List[Any],
        supplemental_news_context: Optional[List[Any]] = None,
        sec_forms_retrieved: Optional[List[str]] = None,
    ) -> List[str]:
        values = dict((silver_context or {}).get("values") or {})
        gold_sources = {
            cls._chunk_source_type(chunk)
            for chunk in gold_context or []
        }
        supplemental_news_sources = {
            cls._chunk_source_type(chunk)
            for chunk in supplemental_news_context or []
        }
        hits: List[str] = []
        if "options" in strict_sources and values:
            option_keys = (
                "latest_atm_iv", "latest_iv_skew", "pcr_volume", "pcr_open_interest",
                "SPY_executable_option_volume", "AAPL_executable_option_volume", "QQQ_executable_option_volume",
                "GLD_executable_option_volume", "SLV_executable_option_volume",
            )
            if any(key in values for key in option_keys):
                hits.append("options")
        if "macro_history" in strict_sources and values:
            macro_keys = ("VIX_value", "DXY_value", "GSPC_value", "IXIC_value", "FEDFUNDS_value", "CPIAUCSL_value")
            if any(key in values for key in macro_keys):
                hits.append("macro_history")
        if "gpr" in strict_sources and (("gpr" in gold_sources) or ("gpr_index_level" in values)):
            hits.append("gpr")
        resolved_sec_forms = list(sec_forms_retrieved or cls._sec_forms_retrieved(metadata, gold_context))
        if "sec" in strict_sources and (resolved_sec_forms or "sec" in gold_sources):
            hits.append("sec")
        if "news" in strict_sources and ("news" in gold_sources or "news" in supplemental_news_sources):
            hits.append("news")
        return hits

    @staticmethod
    def _soft_context_sources(query_family: str) -> List[str]:
        family = canonical_query_family(query_family)
        if family in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}:
            return ["news"]
        return []

    @staticmethod
    def _append_unique(target: List[str], value: str) -> None:
        item = str(value or "").strip().lower()
        if item and item not in target:
            target.append(item)

    @staticmethod
    def _options_native_query_slots(query_slots: Dict[str, str]) -> bool:
        slot_keys = {str(slot).strip() for slot in (query_slots or {}).keys() if str(slot).strip()}
        if not slot_keys:
            return False
        native_slots = {
            "pcr_signal",
            "iv_skew_signal",
            "atm_iv_signal",
            "liquidity_signal",
            "iv_or_skew_signal",
            "options_liquidity_posture",
            "options_vol_signal",
            "equity_vol_signal",
        }
        return slot_keys.issubset(native_slots)

    @staticmethod
    def _canonical_news_topics(metadata: MetadataExtraction | None) -> List[str]:
        topics: List[str] = []
        for raw in list(getattr(metadata, "canonical_news_topics", None) or []):
            topic = normalize_news_topic(str(raw or ""))
            if topic and topic in NEWS_TOPICS and topic not in topics:
                topics.append(topic)
        return topics

    @staticmethod
    def _primary_news_topic(metadata: MetadataExtraction | None) -> str:
        topic = normalize_news_topic(str(getattr(metadata, "primary_news_topic", "") or ""))
        if topic in NEWS_TOPICS:
            return topic
        topics = MasterRetriever._canonical_news_topics(metadata)
        return topics[0] if topics else ""

    @staticmethod
    def _expanded_news_topics(metadata: MetadataExtraction | None) -> List[str]:
        topics: List[str] = []
        for raw in list(getattr(metadata, "expanded_news_topics", None) or []):
            topic = normalize_news_topic(str(raw or ""))
            if topic and topic in NEWS_TOPICS and topic not in topics:
                topics.append(topic)
        return topics

    @classmethod
    def _resolved_primary_theme(cls, metadata: MetadataExtraction | None) -> str:
        metrics = {metric.lower() for metric in cls._metric_values(metadata)}
        source_types = set(cls._metadata_source_values(metadata))
        topics = set(cls._canonical_news_topics(metadata))
        signals = {
            str(signal or "").strip().lower()
            for signal in (getattr(metadata, "signals", None) or [])
            if str(signal or "").strip()
        }
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
        if ("options" in source_types or any(is_options_native_metric(metric) for metric in metrics)) and not explicit_gpr_intent:
            return "options"
        candidate = str(getattr(metadata, "primary_theme", "") or "").strip().lower()
        if explicit_macro_news_narrative:
            return "cross_asset"
        if candidate in {"insider", "geopolitics", "cross_asset", "options"}:
            return candidate
        if "sec" in source_types:
            return "insider"
        if explicit_gpr_intent or "macro_geopolitics_risk" in topics:
            return "geopolitics"
        if "macro_history" in source_types:
            return "cross_asset"
        return "options"

    @classmethod
    def _resolved_primary_surface(cls, metadata: MetadataExtraction | None) -> str:
        source_types = set(cls._metadata_source_values(metadata))
        metrics = cls._metric_values(metadata)
        if "options" in source_types or any(is_options_native_metric(metric) for metric in metrics):
            return "options_surface"
        candidate = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
        if candidate in {"options_surface", "macro_news_surface"}:
            return candidate
        return "macro_news_surface"

    @classmethod
    def _resolved_query_family(cls, metadata: MetadataExtraction | None) -> str:
        theme = cls._resolved_primary_theme(metadata)
        surface = cls._resolved_primary_surface(metadata)
        if theme == "insider":
            return "insider_flow_driven"
        if theme == "geopolitics":
            return "geopolitical_options_read" if surface == "options_surface" else "geopolitical_macro_read"
        if theme == "cross_asset":
            return "cross_asset_regime"
        return "options_microstructure"

    @staticmethod
    def _resolved_asset_scope(metadata: MetadataExtraction | None) -> str:
        candidate = str(getattr(metadata, "asset_scope", "") or "").strip().lower()
        if candidate in {"single_name", "benchmark", "basket"}:
            return candidate
        comparison_targets = [str(t).strip() for t in (getattr(metadata, "comparison_targets", None) or []) if str(t).strip()]
        tickers = [str(t).strip() for t in (getattr(metadata, "tickers", None) or []) if str(t).strip()]
        if comparison_targets:
            return "benchmark"
        if len(tickers) == 1:
            return "single_name"
        if len(tickers) > 1:
            return "basket"
        return "unspecified"

    @staticmethod
    def _resolved_read_profile(metadata: MetadataExtraction | None) -> str:
        metrics = {metric.lower() for metric in MasterRetriever._metric_values(metadata)}
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
            if MasterRetriever._resolved_primary_theme(metadata) == "insider":
                return "event_risk"
            if MasterRetriever._resolved_primary_surface(metadata) != "options_surface":
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

    @classmethod
    def _analysis_surfaces(cls, metadata: MetadataExtraction | None) -> List[str]:
        requested = [
            str(surface or "").strip().lower()
            for surface in (getattr(metadata, "analysis_surfaces", None) or [])
            if str(surface or "").strip()
        ]
        valid = {"insider_signal", "options_surface", "macro_context", "geopolitical_context", "benchmark_context"}
        source_types = set(cls._metadata_source_values(metadata))
        metrics = {metric.lower() for metric in cls._metric_values(metadata)}
        signals = {
            str(signal or "").strip().lower()
            for signal in (getattr(metadata, "signals", None) or [])
            if str(signal or "").strip()
        }
        explicit_gpr_intent = (
            "gpr" in source_types
            or "gpr index" in metrics
            or "gpr context" in signals
        )
        surfaces: List[str] = [
            surface for surface in requested
            if surface in valid and (surface != "geopolitical_context" or explicit_gpr_intent or cls._resolved_primary_theme(metadata) == "geopolitics")
        ]
        theme = cls._resolved_primary_theme(metadata)
        surface = cls._resolved_primary_surface(metadata)
        comparison_targets = [
            str(ticker).upper().strip()
            for ticker in (getattr(metadata, "comparison_targets", None) or [])
            if str(ticker).strip()
        ]
        if theme == "insider" and "insider_signal" not in surfaces:
            surfaces.append("insider_signal")
        if surface == "options_surface" and "options_surface" not in surfaces:
            surfaces.append("options_surface")
        if theme == "cross_asset" and "macro_context" not in surfaces:
            surfaces.append("macro_context")
        if theme == "geopolitics" and "geopolitical_context" not in surfaces:
            surfaces.append("geopolitical_context")
        if comparison_targets and "benchmark_context" not in surfaces:
            surfaces.append("benchmark_context")
        return surfaces

    @classmethod
    def _comparison_targets(cls, metadata: MetadataExtraction | None) -> List[str]:
        primary_ticker = str(((getattr(metadata, "tickers", None) or []) or [""])[0]).upper().strip() if (getattr(metadata, "tickers", None) or []) else ""
        out: List[str] = []
        for raw in list(getattr(metadata, "comparison_targets", None) or []):
            ticker = str(raw or "").upper().strip()
            if ticker and ticker != primary_ticker and ticker not in out:
                out.append(ticker)
        return out

    @staticmethod
    def _requested_sec_forms(metadata: MetadataExtraction | None) -> List[str]:
        forms: List[str] = []
        for raw in list(getattr(metadata, "requested_sec_forms", None) or []):
            form = str(getattr(raw, "value", raw) or "").strip().upper()
            if form in {"8-K", "4"} and form not in forms:
                forms.append(form)
        if forms:
            return forms
        form_type = str(getattr(metadata, "form_type", "") or "").strip().upper()
        if form_type in {"8-K", "4"}:
            return [form_type]
        return []

    @staticmethod
    def _chunk_source_type(chunk: Any) -> str:
        raw = getattr(chunk, "source_type", None)
        if raw is None and isinstance(chunk, dict):
            raw = chunk.get("source_type", "")
        return str(getattr(raw, "value", raw) or "").strip().lower()

    @staticmethod
    def _chunk_metadata(chunk: Any) -> Dict[str, Any]:
        if isinstance(chunk, dict):
            md = chunk.get("metadata")
            return dict(md or {})
        return dict(getattr(chunk, "metadata", None) or {})

    @classmethod
    def _chunk_form_type(cls, chunk: Any) -> str:
        md = cls._chunk_metadata(chunk)
        raw = md.get("form_type", "")
        return str(getattr(raw, "value", raw) or "").strip().upper()

    @classmethod
    def _sec_forms_retrieved(
        cls,
        metadata: MetadataExtraction | None,
        gold_context: List[Any],
    ) -> List[str]:
        requested = cls._requested_sec_forms(metadata)
        seen: List[str] = []
        for chunk in gold_context or []:
            if cls._chunk_source_type(chunk) != "sec":
                continue
            form = cls._chunk_form_type(chunk)
            if not form:
                continue
            if requested and form not in requested:
                continue
            if form not in seen:
                seen.append(form)
        return seen

    @staticmethod
    def _sec_payload_context_by_form(sec_retrieval_contract: Optional[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        contract = sec_retrieval_contract if isinstance(sec_retrieval_contract, dict) else {}
        raw = contract.get("sec_payload_context_by_form") or {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for form, chunks in dict(raw).items():
            form_key = str(form or "").strip().upper()
            if form_key in {"4", "8-K"}:
                out[form_key] = list(chunks or [])
        return out

    @classmethod
    def _sec_payload_chunks(
        cls,
        metadata: MetadataExtraction | None,
        sec_retrieval_contract: Optional[Dict[str, Any]],
        gold_context: List[Any],
    ) -> List[Any]:
        payload_map = cls._sec_payload_context_by_form(sec_retrieval_contract)
        if not payload_map:
            return [chunk for chunk in (gold_context or []) if cls._chunk_source_type(chunk) == "sec"]
        ordered_forms = cls._requested_sec_forms(metadata) or list(payload_map.keys())
        chunks: List[Any] = []
        for form in ordered_forms:
            chunks.extend(list(payload_map.get(form, []) or []))
        return chunks

    @classmethod
    def _sec_forms_retrieved_from_contract(
        cls,
        metadata: MetadataExtraction | None,
        sec_retrieval_contract: Optional[Dict[str, Any]],
        gold_context: List[Any],
    ) -> List[str]:
        payload_map = cls._sec_payload_context_by_form(sec_retrieval_contract)
        if payload_map:
            requested = cls._requested_sec_forms(metadata)
            forms = [form for form in requested if payload_map.get(form)]
            return forms or [form for form, chunks in payload_map.items() if chunks]
        return cls._sec_forms_retrieved(metadata, gold_context)

    @classmethod
    def _sec_slot_hits(
        cls,
        metadata: MetadataExtraction | None,
        gold_context: List[Any],
    ) -> List[str]:
        forms = set(cls._sec_forms_retrieved(metadata, gold_context))
        hits: List[str] = []
        if "8-K" in forms:
            hits.append("sec_event_signal")
        if "4" in forms:
            hits.append("sec_insider_signal")
        return hits

    @classmethod
    def _sec_slot_missing(
        cls,
        metadata: MetadataExtraction | None,
        gold_context: List[Any],
    ) -> List[str]:
        requested_slots = cls._active_sec_slots(metadata)
        slot_hits = set(cls._sec_slot_hits(metadata, gold_context))
        return [slot for slot in requested_slots if slot not in slot_hits]

    @staticmethod
    @lru_cache(maxsize=256)
    def _load_bronze_sec_rows(ticker: str) -> List[Dict[str, Any]]:
        safe_ticker = str(ticker or "").upper().strip()
        if not safe_ticker or not _BRONZE_SEC_ROOT.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for path in sorted(_BRONZE_SEC_ROOT.glob(f"*/{safe_ticker}.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line_s = line.strip()
                        if not line_s:
                            continue
                        try:
                            rows.append(json.loads(line_s))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
        return rows

    @classmethod
    def _bronze_sec_record(cls, ticker: str, accession_no: str) -> Dict[str, Any]:
        safe_accession = str(accession_no or "").strip()
        if not safe_accession:
            return {}
        for row in cls._load_bronze_sec_rows(ticker):
            metadata = dict(row.get("metadata") or {})
            if str(metadata.get("accession_no") or "").strip() == safe_accession:
                return row
        return {}

    def _sec_index_presence_mismatch(
        self,
        metadata: MetadataExtraction | None,
        time_range: Dict[str, Any],
        gold_context: List[Any],
    ) -> bool:
        if gold_context or "sec" not in self._metadata_source_values(metadata):
            return False
        ticker_set = {
            str(ticker or "").upper().strip()
            for ticker in (getattr(metadata, "tickers", None) or [])
            if str(ticker or "").strip()
        }
        requested_forms = set(self._requested_sec_forms(metadata))
        if not ticker_set or not requested_forms:
            return False
        start_date = str((time_range or {}).get("start_date") or "").strip()
        end_date = str((time_range or {}).get("end_date") or "").strip()
        if not start_date or not end_date:
            return False
        sec_root = Path(__file__).resolve().parents[2] / "Data" / "3_Gold_Semantic" / "SEC_Insider_Trades"
        if not sec_root.exists():
            return False
        candidate_files = sorted(sec_root.glob("*/qdrant_ready.jsonl"), reverse=True)
        for candidate in candidate_files:
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        row = json.loads(raw_line)
                        row_md = dict(row.get("metadata") or {})
                        ticker = str(row_md.get("ticker", "") or "").upper().strip()
                        form = str(row_md.get("form_type", "") or "").strip().upper()
                        filed_at = str(row_md.get("filed_at", "") or "").strip()[:10]
                        transaction_date = str(row_md.get("transaction_date", "") or "").strip()[:10]
                        row_date = filed_at or transaction_date
                        if ticker not in ticker_set or form not in requested_forms or not row_date:
                            continue
                        if start_date <= row_date <= end_date:
                            return True
            except Exception:
                continue
        return False

    @staticmethod
    def _retrieved_news_count(gold_context: List[Any]) -> int:
        count = 0
        for chunk in gold_context or []:
            source_type = getattr(getattr(chunk, "source_type", None), "value", getattr(chunk, "source_type", None))
            if str(source_type or "").strip().lower() == "news":
                count += 1
        return count

    @staticmethod
    def _emit_retrieval_timeout_audit(
        *,
        query: str,
        transform_result: FullTransformationResult,
        fallback_tier: str,
        latency: float,
        stage: str,
    ) -> None:
        payload = {
            "timestamp": datetime.now().isoformat(),
            "original_query": query,
            "rerank_query_used": str(getattr(transform_result.hyde, "rerank_query", "") or query),
            "filter_applied": None,
            "fallback_triggered": False,
            "fallback_tier": fallback_tier,
            "results_count": 0,
            "top_k_scores": [],
            "latency_sec": round(float(latency), 3),
            "status": "TIMEOUT",
            "error_msg": f"{stage} timeout before retriever audit emission",
            "retrieval_stage": stage,
        }
        try:
            append_audit_jsonl(
                module="retrieval",
                payload=payload,
                filename="retriever_audit_trail.jsonl",
                scoped_by_run=False,
            )
        except Exception as e:
            logger.warning(f"Failed to write retrieval timeout audit ({stage}): {e}")

    @staticmethod
    def _has_impact_basket_context(truth_values: Dict[str, Any]) -> bool:
        basket_keys = (
            "GLD_value",
            "GLD_change_pct",
            "SLV_value",
            "SLV_change_pct",
            "VIX_value",
            "DXY_value",
            "GSPC_value",
            "GSPC_change_pct",
        )
        return any(truth_values.get(key) is not None for key in basket_keys)

    @classmethod
    def _active_sec_slots(cls, metadata: MetadataExtraction | None) -> List[str]:
        requested_forms = cls._requested_sec_forms(metadata)
        if requested_forms:
            slots: List[str] = []
            if "8-K" in requested_forms:
                slots.append("sec_event_signal")
            if "4" in requested_forms:
                slots.append("sec_insider_signal")
            return slots
        return [cls._active_sec_slot(metadata)]

    @staticmethod
    def _active_sec_slot(metadata: MetadataExtraction | None) -> str:
        form_type = str(getattr(metadata, "form_type", "") or "").strip().upper()
        event_keyword = str(getattr(metadata, "event_keyword", "") or "").strip().lower()
        action_direction = str(getattr(metadata, "action_direction", "") or "").strip().upper()
        if form_type == "8-K":
            return "sec_event_signal"
        if form_type == "4":
            return "sec_insider_signal"
        if event_keyword in {"event_risk", "event_driven", "event"}:
            return "sec_event_signal"
        if action_direction and action_direction != "NONE":
            return "sec_insider_signal"
        return "sec_insider_signal"

    def _compensation_targets(self, metadata: MetadataExtraction | None, query_family: str) -> List[str]:
        family = canonical_query_family(query_family)
        covered_option_tickers = set(getattr(self.transformer, "covered_option_tickers", []) or [])
        allowed_tickers = set(getattr(self.transformer, "allowed_tickers", []) or [])
        primary_tickers = [
            str(ticker).upper().strip()
            for ticker in (getattr(metadata, "tickers", None) or [])
            if str(ticker).strip()
        ]
        targets: List[str] = []
        for topic in self._canonical_news_topics(metadata):
            basket = list(TOPIC_IMPACT_BASKETS.get(topic, [])) or list(TOPIC_TO_TICKERS.get(topic, []))
            ordered_candidates: List[str] = []
            for ticker in primary_tickers:
                if ticker in basket and ticker not in ordered_candidates:
                    ordered_candidates.append(ticker)
            for ticker in basket:
                ticker_s = str(ticker).upper().strip()
                if ticker_s and ticker_s not in ordered_candidates:
                    ordered_candidates.append(ticker_s)
            for ticker_s in ordered_candidates:
                if not ticker_s or ticker_s in targets:
                    continue
                if family == "geopolitical_options_read":
                    if ticker_s in covered_option_tickers:
                        targets.append(ticker_s)
                elif ticker_s in allowed_tickers:
                    targets.append(ticker_s)
        return targets

    @staticmethod
    def _requires_explicit_gold_context(user_query: str) -> bool:
        query_l = (user_query or "").lower()
        return any(phrase in query_l for phrase in _EXPLICIT_GOLD_CONTEXT_PHRASES)

    @classmethod
    def _normalize_source_requirements(
        cls,
        *,
        route: str,
        query_family: str,
        metadata: MetadataExtraction | None,
        user_query: str,
        query_slots: Dict[str, str],
        market_analysis_only: bool,
    ) -> tuple[List[str], List[str], str]:
        family = canonical_query_family(query_family)
        metadata_sources = cls._metadata_source_values(metadata)
        metadata_source_set = set(metadata_sources)
        metrics_l = {metric.lower() for metric in cls._metric_values(metadata)}
        canonical_topics = set(cls._canonical_news_topics(metadata))
        strict_sources: List[str] = []
        soft_sources: List[str] = list(cls._soft_context_sources(family))
        options_native_slots = cls._options_native_query_slots(query_slots)
        requires_explicit_gold_context = cls._requires_explicit_gold_context(user_query)
        silver_primary_route = route == "sql_only"
        primary_surface = cls._resolved_primary_surface(metadata)

        if family in {"options_microstructure", "geopolitical_options_read"}:
            cls._append_unique(strict_sources, "options")
        elif family == "insider_flow_driven" and primary_surface == "options_surface":
            cls._append_unique(strict_sources, "options")

        if family in {"geopolitical_macro_read", "geopolitical_options_read"}:
            if (
                "gpr" in metadata_source_set
                or "gpr index" in metrics_l
                or "macro_geopolitics_risk" in canonical_topics
            ):
                cls._append_unique(strict_sources, "gpr")
            if "news" in metadata_source_set:
                if family == "geopolitical_macro_read":
                    cls._append_unique(soft_sources, "news")
                else:
                    cls._append_unique(strict_sources, "news")
            if family == "geopolitical_macro_read" and (
                "macro_history" in metadata_source_set
                or bool(getattr(metadata, "tickers", None))
                or "macro_geopolitics_risk" in canonical_topics
            ):
                cls._append_unique(strict_sources, "macro_history")

        if family == "cross_asset_regime" and "macro_history" in metadata_source_set:
            cls._append_unique(strict_sources, "macro_history")
        if family == "cross_asset_regime" and "news" in metadata_source_set:
            cls._append_unique(soft_sources, "news")

        if family == "options_microstructure":
            if options_native_slots:
                cls._append_unique(strict_sources, "options")
            elif "options" in metadata_sources:
                cls._append_unique(strict_sources, "options")

            if options_native_slots and market_analysis_only and not requires_explicit_gold_context:
                if "news" in metadata_sources:
                    cls._append_unique(soft_sources, "news")
                coverage_basis = "silver_only" if silver_primary_route else "silver_primary_with_soft_gold"
                return strict_sources or ["options"], soft_sources, coverage_basis

            for source in metadata_sources:
                if source == "options":
                    cls._append_unique(strict_sources, source)
                    continue
                if source == "news":
                    if requires_explicit_gold_context or route == "vector_only":
                        cls._append_unique(strict_sources, source)
                    else:
                        cls._append_unique(soft_sources, source)
                    continue
                if route == "vector_only" and requires_explicit_gold_context:
                    cls._append_unique(strict_sources, source)
                else:
                    cls._append_unique(soft_sources, source)

            if any(src in strict_sources for src in ("news", "gpr", "sec")):
                coverage_basis = "hybrid_required"
            else:
                coverage_basis = "silver_only" if silver_primary_route else "silver_primary_with_soft_gold"
            return strict_sources or ["options"], soft_sources, coverage_basis

        for source in metadata_sources:
            if source in strict_sources:
                continue
            cls._append_unique(strict_sources, source)
        if any(src in strict_sources for src in ("news", "gpr", "sec")) or route == "vector_only":
            coverage_basis = "hybrid_required"
        elif route == "sql_only":
            coverage_basis = "silver_only"
        else:
            coverage_basis = "silver_primary_with_soft_gold"
        return strict_sources, soft_sources, coverage_basis

    @staticmethod
    def _query_slots(query_family: str) -> Dict[str, str]:
        return query_slots_for_family(query_family)

    @staticmethod
    def _metric_values(metadata: MetadataExtraction | None) -> List[str]:
        return [str(m or "") for m in (getattr(metadata, "metrics", None) or []) if str(m or "").strip()]

    @classmethod
    def _dynamic_query_slots(
        cls,
        query_family: str,
        metadata: MetadataExtraction,
        user_query: str,
    ) -> Dict[str, str]:
        family = canonical_query_family(query_family)
        base_slots = cls._query_slots(family)
        if family == "insider_flow_driven":
            dynamic_slots: Dict[str, str] = {}
            for sec_slot in cls._active_sec_slots(metadata):
                if sec_slot in base_slots:
                    dynamic_slots[sec_slot] = base_slots[sec_slot]
            if cls._resolved_primary_surface(metadata) == "options_surface" and "options_liquidity_posture" in base_slots:
                dynamic_slots["options_liquidity_posture"] = base_slots["options_liquidity_posture"]
            return dynamic_slots or {"sec_insider_signal": base_slots.get("sec_insider_signal", "Form-4 insider selling / buying / vesting signal")}
        if family != "options_microstructure":
            return base_slots

        metrics_l = {metric.lower() for metric in cls._metric_values(metadata)}
        read_profile = cls._resolved_read_profile(metadata)

        wants_pcr = "put/call ratio" in metrics_l
        wants_skew = "iv skew" in metrics_l
        wants_iv = any(metric in metrics_l for metric in {"implied volatility", "implied volatility (iv)"})
        wants_liquidity = any(metric in metrics_l for metric in {"options liquidity", "open interest", "options volume", "options pricing / spread"})

        dynamic_slots: Dict[str, str] = {}
        if wants_pcr:
            dynamic_slots["pcr_signal"] = base_slots.get("pcr_signal", "put/call ratio signal")
        if wants_skew:
            dynamic_slots["iv_skew_signal"] = "IV skew signal"
        elif wants_iv:
            dynamic_slots["atm_iv_signal"] = base_slots.get("atm_iv_signal", "at-the-money implied volatility signal")
        elif read_profile == "board_state":
            dynamic_slots["iv_or_skew_signal"] = base_slots.get("iv_or_skew_signal", "IV / skew signal")
        if wants_liquidity or read_profile == "posture_read":
            dynamic_slots["liquidity_signal"] = base_slots.get("liquidity_signal", "options liquidity posture")

        return dynamic_slots or base_slots

    @classmethod
    def _detect_market_analysis_only(
        cls,
        *,
        metadata: MetadataExtraction | None,
        query_family: str,
    ) -> bool:
        read_profile = cls._resolved_read_profile(metadata)
        if read_profile == "posture_read":
            return True
        return canonical_query_family(query_family) in {"cross_asset_regime", "geopolitical_macro_read", "geopolitical_options_read"}

    @staticmethod
    def _infer_query_family(metadata: MetadataExtraction, user_query: str) -> str:
        return MasterRetriever._resolved_query_family(metadata)

    def _build_runtime_contracts(
        self,
        *,
        user_query: str,
        route: str,
        metadata: MetadataExtraction,
        silver_context: Dict[str, Any],
        silver_context_frozen: Optional[Dict[str, Any]],
        gold_context: List[Any],
        supplemental_news_context: List[Any],
        sec_retrieval_contract: Optional[Dict[str, Any]],
        time_range: Dict[str, Any],
        is_fallback: bool,
        in_scope_tickers: Optional[List[str]] = None,
        out_of_scope_tickers: Optional[List[str]] = None,
        refusal_reason: str = "",
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        query_family = canonical_query_family(self._infer_query_family(metadata, user_query))
        primary_theme = self._resolved_primary_theme(metadata)
        primary_surface = self._resolved_primary_surface(metadata)
        asset_scope = self._resolved_asset_scope(metadata)
        read_profile = self._resolved_read_profile(metadata)
        canonical_news_topics = self._canonical_news_topics(metadata)
        primary_news_topic = self._primary_news_topic(metadata)
        expanded_news_topics = self._expanded_news_topics(metadata)
        analysis_surfaces = self._analysis_surfaces(metadata)
        comparison_targets = self._comparison_targets(metadata)
        compensation_targets = self._compensation_targets(metadata, query_family)
        query_slots = self._dynamic_query_slots(query_family, metadata, user_query)
        market_analysis_only = self._detect_market_analysis_only(
            metadata=metadata,
            query_family=query_family,
        )
        strict_sources, soft_context_sources, coverage_basis = self._normalize_source_requirements(
            route=route,
            query_family=query_family,
            metadata=metadata,
            user_query=user_query,
            query_slots=query_slots,
            market_analysis_only=market_analysis_only,
        )
        requested_window = str(getattr(getattr(metadata, "time_window", None), "value", getattr(metadata, "time_window", None)) or "past_six_months")
        requested_days = time_window_to_days(requested_window, default=180)
        effective_window = str((time_range or {}).get("time_window_label") or requested_window)
        effective_days = int((time_range or {}).get("window_days") or requested_days)
        time_defaulted = bool((time_range or {}).get("is_default_window_applied"))
        time_extended = bool(is_fallback or effective_days > requested_days)

        truth_ctx = silver_context_frozen if isinstance(silver_context_frozen, dict) and silver_context_frozen else silver_context
        truth_values = dict((truth_ctx or {}).get("values") or {})
        sec_payload_context_by_form = self._sec_payload_context_by_form(sec_retrieval_contract)
        sec_forms_requested = self._requested_sec_forms(metadata)
        if not sec_payload_context_by_form:
            sec_payload_context_by_form = {}
            for chunk in gold_context or []:
                if self._chunk_source_type(chunk) != "sec":
                    continue
                form = self._chunk_form_type(chunk)
                if form not in {"4", "8-K"}:
                    continue
                if sec_forms_requested and form not in sec_forms_requested:
                    continue
                sec_payload_context_by_form.setdefault(form, []).append(
                    chunk if isinstance(chunk, dict) else chunk.model_dump()
                )
        sec_analysis_bundle = compose_sec_analysis_bundle(
            sec_forms_requested=sec_forms_requested,
            sec_payload_context_by_form=sec_payload_context_by_form,
            bronze_lookup=self._bronze_sec_record,
        )
        sec_existence = sec_analysis_bundle.existence
        sec_forms_retrieved = list(sec_existence.sec_forms_retrieved or [])
        sec_slot_hits = list(sec_existence.sec_slot_hits or [])
        sec_slot_missing = list(sec_existence.sec_slot_missing or [])
        sec_analysis_features = [
            *[feature.model_dump() for feature in list(sec_analysis_bundle.form4_features or [])],
            *[feature.model_dump() for feature in list(sec_analysis_bundle.form8k_features or [])],
        ]
        form4_analysis_result = sec_analysis_bundle.form4_analysis_result.model_dump()
        form8k_analysis_result = sec_analysis_bundle.form8k_analysis_result.model_dump()
        sec_payload_chunks = self._sec_payload_chunks(metadata, sec_retrieval_contract, gold_context)
        retrieved_news_count = self._retrieved_news_count(gold_context)
        supplemental_news_count = self._retrieved_news_count(supplemental_news_context)
        news_coverage_status = "not_applicable"
        background_only_read = False
        supplemental_news_status = "not_applicable"
        # cross_asset_regime (e.g. "GLD news narrative") requests news as a strict
        # source alongside macro_history. When Gold retrieval times out or returns
        # nothing, the finalizer must know news is missing so it can surface the
        # correct disclosure ("No news retrieved in window") instead of treating
        # news as not-applicable to this query family.
        # background_only_read stays geopolitical_macro_read-only: that flag
        # signals the GPR-structured-background fallback path, which depends on
        # gpr_index_level presence and is not applicable to cross_asset.
        if query_family in {"geopolitical_macro_read", "cross_asset_regime"}:
            news_coverage_status = "fresh_news_found" if retrieved_news_count > 0 else "no_fresh_news_retrieved"
            background_only_read = (
                query_family == "geopolitical_macro_read"
                and retrieved_news_count == 0
                and truth_values.get("gpr_index_level") is not None
                and self._has_impact_basket_context(truth_values)
            )
        strict_sources_hit = self._strict_source_hits(
            metadata=metadata,
            strict_sources=strict_sources,
            silver_context=truth_ctx or {},
            gold_context=gold_context,
            supplemental_news_context=supplemental_news_context,
            sec_forms_retrieved=sec_forms_retrieved,
        )
        gold_sources_hit = {
            self._chunk_source_type(chunk)
            for chunk in gold_context or []
        }
        soft_sources_hit = [src for src in soft_context_sources if src in gold_sources_hit]
        missing_strict_sources = [src for src in strict_sources if src not in strict_sources_hit]
        missing_query_slots = missing_slots_for_query_family(query_family, missing_strict_sources, query_slots)
        if query_family == "geopolitical_macro_read" and background_only_read:
            missing_strict_sources = [src for src in missing_strict_sources if src != "news"]
            missing_query_slots = [slot for slot in missing_query_slots if slot != "geopolitical_news_signal"]
        if query_family == "insider_flow_driven":
            missing_query_slots = list(dict.fromkeys([
                *[slot for slot in missing_query_slots if slot not in {"sec_insider_signal", "sec_event_signal"}],
                *sec_slot_missing,
            ]))
        sec_index_presence_mismatch = self._sec_index_presence_mismatch(
            metadata=metadata,
            time_range=time_range,
            gold_context=sec_payload_chunks or gold_context,
        )

        data_capability_profile = build_data_capability_profile(metadata, truth_ctx or {}, gold_context or [], time_range or {})
        if data_capability_profile.get("can_support_concrete_option_structure"):
            output_mode_ceiling = "actionable_options"
            specificity_ceiling = "structure_allowed"
        elif data_capability_profile.get("has_options_chain_support") or data_capability_profile.get("has_gold_evidence") or data_capability_profile.get("has_price_signal"):
            output_mode_ceiling = "directional_watchlist"
            specificity_ceiling = "watchlist_only"
        else:
            output_mode_ceiling = "informational_only"
            specificity_ceiling = "no_structure"

        unavailable_metrics = [
            metric for metric in (getattr(metadata, "metrics", None) or [])
            if metric in METRIC_TO_COLUMN_MAPPING and not METRIC_TO_COLUMN_MAPPING.get(metric)
        ]
        supported_tickers = []
        allowed_tickers = set(getattr(self.transformer, "allowed_tickers", []) or [])
        for ticker in (getattr(metadata, "tickers", None) or []):
            ticker_u = str(ticker).upper().strip()
            if not ticker_u:
                continue
            if not allowed_tickers or ticker_u in allowed_tickers or ticker_u.startswith("^"):
                if ticker_u not in supported_tickers:
                    supported_tickers.append(ticker_u)
        if in_scope_tickers is None:
            in_scope_tickers = list(supported_tickers)
        if out_of_scope_tickers is None:
            out_of_scope_tickers = []
        scope_status = "out_of_scope" if out_of_scope_tickers else "in_scope"
        data_backed_families = {
            "options_microstructure",
            "cross_asset_regime",
            "geopolitical_macro_read",
            "geopolitical_options_read",
        }
        is_data_backed_read = route == "sql_only" and query_family in data_backed_families
        slot_evidence_contracts = build_slot_evidence_contracts(
            query_family=query_family,
            query_slots=query_slots,
            capability_profile=data_capability_profile,
        )
        retrieval_slot_support = evaluate_retrieval_slot_support(
            slot_contracts=slot_evidence_contracts,
            retrieval_outcome={
                "missing_query_slots": missing_query_slots,
                "news_coverage_status": news_coverage_status,
                "background_only_read": background_only_read,
                "retrieved_news_count": retrieved_news_count,
                "supplemental_news_status": supplemental_news_status,
                "supplemental_news_count": supplemental_news_count,
            },
            silver_values=truth_values,
            gold_ctx=gold_context or [],
        )
        hard_data_sufficient_for_answer = bool(
            truth_values
            and (
                (
                    is_data_backed_read
                    and retrieval_slot_support.get("hard_gate_pass", False)
                )
                or (
                    market_analysis_only
                    and retrieval_slot_support.get("hard_gate_pass", False)
                )
            )
        )
        if not (is_data_backed_read or market_analysis_only):
            hard_data_sufficient_for_answer = bool(
                truth_values
                and not missing_strict_sources
                and not missing_query_slots
                and retrieval_slot_support.get("hard_gate_pass", False)
            )
        primary_ticker = resolve_primary_ticker(
            metadata=metadata,
            scope_contract={"primary_ticker": (in_scope_tickers[0] if in_scope_tickers else "")},
        ) or ""

        disclosures: List[str] = []
        if unavailable_metrics:
            disclosures.append(
                "Unavailable metrics require a caveat, not a substitute: "
                + ", ".join(str(m) for m in unavailable_metrics)
            )
        if missing_strict_sources:
            disclosures.append(
                "Missing strict sources lower output confidence: "
                + ", ".join(missing_strict_sources)
            )
        if missing_query_slots:
            disclosures.append(
                "Missing query slots cannot be answered reliably: "
                + ", ".join(missing_query_slots)
            )
        if query_family == "geopolitical_macro_read" and background_only_read:
            disclosures.append(
                "No fresh geopolitical news was retrieved in the requested window; treat this answer as a background-only read anchored to GPR and cross-asset impact context."
            )
        if sec_index_presence_mismatch:
            disclosures.append(
                "Prepared SEC source data appears to contain matching filings for this ticker/window, but the live Gold retrieval did not return them; treat this as a possible index or ingestion mismatch."
            )
        if query_family == "insider_flow_driven" and "sec_insider_signal" in query_slots:
            disclosures.append(
                "SEC action taxonomy is explicit: SELL means insider disposition, BUY means open-market purchase, and ACQUIRE/VEST means vesting-related acquisition rather than open-market buying or selling."
            )
        if query_family == "insider_flow_driven" and "sec_event_signal" in query_slots:
            disclosures.append(
                "SEC filing subtype is explicit: 8-K event filings are distinct from Form-4 insider transaction disclosures."
            )
        if time_defaulted:
            disclosures.append("The requested query omitted a time phrase, so the effective window used the project default.")
        if time_extended:
            disclosures.append("The effective evidence window was widened or fallback-adjusted and should be disclosed in the answer.")

        time_contract = TimeContract(
            requested_window=requested_window,
            effective_window=effective_window,
            window_days=effective_days,
            is_default_window_applied=time_defaulted,
            is_extended_window=time_extended,
        )
        source_coverage = SourceCoverageContract(
            strict_sources_expected=strict_sources,
            strict_sources_hit=strict_sources_hit,
            soft_sources_expected=soft_context_sources,
            soft_sources_hit=soft_sources_hit,
            missing_strict_sources=missing_strict_sources,
            missing_query_slots=missing_query_slots,
            news_coverage_status=news_coverage_status,
            background_only_read=background_only_read,
            retrieved_news_count=retrieved_news_count,
            supplemental_news_status=supplemental_news_status,
            supplemental_news_count=supplemental_news_count,
        )
        scope_contract = ScopeContract(
            query_family=query_family,
            strict_sources=strict_sources,
            soft_context_sources=soft_context_sources,
            allowed_metrics=[m for m in (getattr(metadata, "metrics", None) or []) if m in ALLOWED_METRICS],
            unavailable_metrics=unavailable_metrics,
            supported_tickers=supported_tickers,
            requested_time_window=requested_window,
            effective_time_window=effective_window,
            output_mode_ceiling=output_mode_ceiling,
            specificity_ceiling=specificity_ceiling,
            required_disclosures=disclosures,
            query_slots=query_slots,
            slot_evidence_contracts=slot_evidence_contracts,
            sec_action_taxonomy=dict(SEC_ACTION_TAXONOMY) if query_family == "insider_flow_driven" else {},
            sec_analysis_contract={
                "form4_analysis_contract": {
                    "analysis_fields": [
                        "action_direction",
                        "shares",
                        "price",
                        "total_value",
                        "remaining_shares",
                        "is_cluster_trade",
                        "is_10b5_1_planned",
                    ],
                    "answer_slots": [
                        "trader_identity_role",
                        "transaction_type",
                        "scale_materiality",
                        "planned_vs_discretionary",
                        "clustering_coordination",
                        "residual_holdings_context",
                        "directional_insider_flow_interpretation",
                    ],
                },
                "form8k_analysis_contract": {
                    "analysis_fields": [
                        "tone_score",
                        "topics",
                        "entities",
                        "filed_at",
                        "content",
                    ],
                    "answer_slots": [
                        "event_category",
                        "tone_skew",
                        "event_risk_interpretation",
                        "repeat_vs_isolated_event_pressure",
                    ],
                },
                "missing_policy": "degradable_disclosure",
            } if query_family == "insider_flow_driven" else {},
            primary_ticker=primary_ticker,
            analysis_mode="data_backed_read" if is_data_backed_read else "default_read",
            coverage_basis=coverage_basis,
            requires_catalyst_confirmation=not (is_data_backed_read or market_analysis_only),
            gold_context_optional=bool(is_data_backed_read or market_analysis_only),
            hard_data_sufficient_for_answer=hard_data_sufficient_for_answer,
            market_analysis_only=market_analysis_only,
            scope_status=scope_status,
            in_scope_tickers=list(in_scope_tickers),
            out_of_scope_tickers=list(out_of_scope_tickers),
            refusal_reason=refusal_reason,
            primary_theme=primary_theme,
            primary_surface=primary_surface,
            canonical_news_topics=canonical_news_topics,
            primary_news_topic=primary_news_topic,
            expanded_news_topics=expanded_news_topics,
            analysis_surfaces=analysis_surfaces,
            comparison_targets=comparison_targets,
            compensation_targets=compensation_targets,
            requested_sec_forms=self._requested_sec_forms(metadata),
            asset_scope=asset_scope,
            read_profile=read_profile,
            news_coverage_status=news_coverage_status,
            background_only_read=background_only_read,
            retrieved_news_count=retrieved_news_count,
            supplemental_news_status=supplemental_news_status,
            supplemental_news_count=supplemental_news_count,
        )
        retrieval_outcome = RetrievalOutcome(
            strict_sources_hit=strict_sources_hit,
            soft_sources_hit=soft_sources_hit,
            missing_strict_sources=missing_strict_sources,
            missing_query_slots=missing_query_slots,
            sec_forms_requested=sec_forms_requested,
            sec_forms_retrieved=sec_forms_retrieved,
            sec_slot_hits=sec_slot_hits,
            sec_slot_missing=sec_slot_missing,
            sec_payload_context_by_form=sec_payload_context_by_form,
            sec_analysis_bundle=sec_analysis_bundle,
            sec_analysis_features=sec_analysis_features,
            form4_analysis_result=form4_analysis_result,
            form8k_analysis_result=form8k_analysis_result,
            sec_index_presence_mismatch=sec_index_presence_mismatch,
            news_coverage_status=news_coverage_status,
            background_only_read=background_only_read,
            retrieved_news_count=retrieved_news_count,
            supplemental_news_status=supplemental_news_status,
            supplemental_news_count=supplemental_news_count,
            has_gold_evidence=bool(sec_payload_chunks or gold_context),
            has_silver_evidence=bool(truth_values),
            is_fallback=bool(is_fallback),
            time_window_extended=time_extended,
            time_window_defaulted=time_defaulted,
            time_contract=time_contract,
            source_coverage=source_coverage,
            scope_status=scope_status,
            in_scope_tickers=list(in_scope_tickers),
            out_of_scope_tickers=list(out_of_scope_tickers),
            refusal_reason=refusal_reason,
        )
        return scope_contract.model_dump(), retrieval_outcome.model_dump()

    # ======================================================================
    # B. Engine wrappers (per-engine timeouts + exception isolation)
    # ======================================================================

    async def _fetch_gold_with_telemetry(
        self,
        query: str,
        transform_result: FullTransformationResult,
        top_k: int = 5,
        precomputed_vecs: Optional[Any] = None,
    ) -> List[Any]:
        """Gold wrapper with independent timeout + exception isolation.

        Forwards the live `time_predicates` dict (compiled once upstream in
        `_compute_time_range`) so Qdrant can build source-specific time
        filters rather than rederiving the window on every call.

        Parameters
        ----------
        precomputed_vecs
            (dense_vec, sparse_vec) pre-computed upstream when both Gold and
            SupplementalNews run for the same query.  Forwarded to
            `retrieve_async` to skip redundant embedding work.
        """
        t0 = time.time()
        predicates = getattr(self, "_current_predicate_set", None)
        try:
            res = await asyncio.wait_for(
                self.qdrant.retrieve_async(
                    original_query=query,
                    transform_result=transform_result,
                    top_k=top_k,
                    time_predicates=predicates,
                    precomputed_vecs=precomputed_vecs,
                ),
                timeout=self.gold_timeout,
            )
            logger.debug(f"📊 [Telemetry] Gold engine finished in {time.time() - t0:.3f}s (top_k={top_k})")
            return res
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ [Gold Timeout] Exceeded {self.gold_timeout}s. Returning empty context.")
            self._emit_retrieval_timeout_audit(
                query=query,
                transform_result=transform_result,
                fallback_tier="gold_timeout",
                latency=time.time() - t0,
                stage="gold",
            )
            return []
        except Exception as e:
            logger.error(f"❌ [Gold Failure] {repr(e)}")
            return []

    async def _fetch_supplemental_news_with_telemetry(
        self,
        query: str,
        transform_result: FullTransformationResult,
        top_k: int = 5,
        precomputed_vecs: Optional[Any] = None,
    ) -> List[Any]:
        """Supplemental macro-news lane for narrative families.

        Parameters
        ----------
        precomputed_vecs
            (dense_vec, sparse_vec) pre-computed upstream when both Gold and
            SupplementalNews run for the same query.  Forwarded to
            `retrieve_supplemental_news_async` to skip redundant embedding.
        """
        t0 = time.time()
        predicates = getattr(self, "_current_predicate_set", None)
        try:
            res = await asyncio.wait_for(
                self.qdrant.retrieve_supplemental_news_async(
                    original_query=query,
                    transform_result=transform_result,
                    top_k=top_k,
                    time_predicates=predicates,
                    precomputed_vecs=precomputed_vecs,
                ),
                timeout=self.supplemental_news_timeout,
            )
            logger.debug(
                f"📊 [Telemetry] Supplemental news engine finished in {time.time() - t0:.3f}s (top_k={top_k})"
            )
            return res
        except asyncio.TimeoutError:
            logger.warning(
                f"⚠️ [Supplemental News Timeout] Exceeded {self.supplemental_news_timeout}s. Returning empty supplemental context."
            )
            self._emit_retrieval_timeout_audit(
                query=query,
                transform_result=transform_result,
                fallback_tier="supplemental_news_timeout",
                latency=time.time() - t0,
                stage="supplemental_news",
            )
            return []
        except Exception as e:
            logger.error(f"❌ [Supplemental News Failure] {repr(e)}")
            return []

    async def _fetch_silver_with_telemetry(self, metadata: MetadataExtraction) -> Dict[str, Any]:
        """Silver wrapper with structured degradation on failure.

        Threads the live `time_predicates` dict through to `SilverSQLTool`
        so handler-side time alignment (DAILY weekend-safe, MONTHLY month-
        widened) lands on the same compiled predicate the Gold retriever
        consumes — one window, one audit trail.
        """
        t0 = time.time()
        predicates = getattr(self, "_current_predicate_set", None)
        try:
            res = await asyncio.wait_for(
                self.sql_tool.query_parquet_by_metadata(
                    metadata, time_predicates=predicates,
                ),
                timeout=self.silver_timeout,
            )
            logger.debug(f"📊 [Telemetry] Silver engine finished in {time.time() - t0:.3f}s")
            return res or {"values": {}, "lineage_anchors": [], "citation_contract": {}, "citation_anchor_map": {}}
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ [Silver Timeout] Exceeded {self.silver_timeout}s.")
            return {"values": {}, "lineage_anchors": [], "citation_contract": {}, "citation_anchor_map": {}, "error": "Timeout"}
        except Exception as e:
            logger.error(f"❌ [Silver Failure] {repr(e)}")
            return {"values": {}, "lineage_anchors": [], "citation_contract": {}, "citation_anchor_map": {}, "error": str(type(e).__name__)}

    # ======================================================================
    # C. NEW helpers — Time-range audit / HE back-injection / compensation
    # ======================================================================

    def _compute_time_range(
        self,
        metadata: MetadataExtraction,
        default_applied: bool,
    ) -> Dict[str, Any]:
        """Derive the authoritative (anchor, start, end) triple for this query.

        Uses SilverSQLTool's Dynamic Time Anchor so the date aligns with the
        last successfully ingested Silver partition (survives weekends, holidays,
        backfills). Exposed on the return payload so the Analyst can emit a
        compliance footer verbatim, e.g.

            "data range: 2025-10-20 ~ 2026-04-18 (window_days=180, default)"

        Also compiles a **per-source `TimePredicate` set** (news / SEC / GPR /
        options / macro / silver_gpr) via `time_adapter.compile_all`. These
        predicates are attached to the payload under `source_predicates` so
        the Gold retriever and every Silver handler can apply a time window
        aligned to its own physical schema (MONTHLY widened, DAILY weekend-
        safe, EVENT respects the raw semantic window) without duplicating
        the widening logic across layers. See docs/Data_source_docs/
        Time_Schema_Audit.md for the full per-source contract.
        """
        tw_value = getattr(metadata.time_window, "value", metadata.time_window)
        window_days = time_window_to_days(metadata.time_window, default=180)

        try:
            # "options" is the most common Silver dataset; the anchor logic
            # falls back to date.today() if the runtime state is missing.
            anchor: date = self.sql_tool._get_anchor_date("options")
        except Exception as e:
            logger.warning(f"time_range: anchor lookup failed, defaulting to today(): {e}")
            anchor = date.today()

        start = anchor - timedelta(days=window_days)

        # Compile per-source predicates once; they ride along the payload so
        # audit logs capture exactly which window hit each source.
        predicate_set = compile_all_time_predicates(
            metadata.time_window, anchor,
            label=str(tw_value) if tw_value else None,
        )
        serialised_predicates = {
            key.value: pred.to_dict() for key, pred in predicate_set.items()
        }

        payload = {
            "time_window_label": str(tw_value) if tw_value else "past_six_months",
            "window_days": window_days,
            "anchor_date": anchor.isoformat(),
            "start_date": start.isoformat(),
            "end_date": anchor.isoformat(),
            "is_default_window_applied": bool(default_applied),
            # Per-source compiled predicates (serialised dict form). Live,
            # runtime-usable objects are kept on `self._predicate_set_cache`
            # for the retrievers via `_current_predicate_set`.
            "source_predicates": serialised_predicates,
        }
        logger.info(
            f"🕒 [TimeRange] {payload['start_date']} → {payload['end_date']} "
            f"(label={payload['time_window_label']}, days={window_days}, "
            f"default_applied={default_applied}) | sources_compiled="
            f"{list(serialised_predicates.keys())}"
        )
        # Stash the live dict so `retrieve()` can pass it to Gold/Silver
        # without re-running the compiler. Cleared at the end of each call.
        self._current_predicate_set: Dict[SourceTimeKey, TimePredicate] = predicate_set
        return payload

    def _extract_hyde_entities(
        self,
        hyde_paragraph: str,
        seed_tickers: List[str],
    ) -> Dict[str, Any]:
        """Back-inject tickers mentioned in HyDE, filtered by ontology whitelist.

        Pipeline:
            1. Regex `[A-Z]{1,5}` candidates
            2. Subtract `_TICKER_STOPWORDS` (IV, CPI, THE, …)
            3. Intersect with `self.transformer.allowed_tickers` (HE Guardrail)
            4. Subtract the seed tickers already on the metadata
            5. Cap at `_HE_NOVEL_TICKERS_CAP`

        Returns a structured payload so the Analyst can see:
            - What HE thought of
            - What survived the guardrail
            - What is *novel* (i.e. should trigger compensation SQL)
        """
        paragraph = hyde_paragraph or ""
        raw_candidates = set(m.group(1) for m in _TICKER_RE.finditer(paragraph))
        post_stopword = raw_candidates - _TICKER_STOPWORDS

        allowed = set(getattr(self.transformer, "allowed_tickers", []) or [])
        whitelisted = sorted(post_stopword & allowed)

        seed_set = {t.upper() for t in (seed_tickers or [])}
        novel = [t for t in whitelisted if t not in seed_set][:_HE_NOVEL_TICKERS_CAP]

        if novel:
            logger.info(
                f"🧠 [HyDE-BackInjection] Novel tickers (whitelisted): {novel} "
                f"| raw={sorted(raw_candidates)} | allowed_hit={whitelisted}"
            )

        return {
            "paragraph": paragraph,
            "raw_candidates": sorted(raw_candidates),
            "whitelisted": whitelisted,
            "novel_tickers": novel,
        }

    async def _run_silver_compensation(
        self,
        base_metadata: MetadataExtraction,
        novel_tickers: List[str],
    ) -> Dict[str, Any]:
        """Fire one Silver query per HE-derived novel ticker and aggregate.

        Rationale:
            - SilverSQLTool consumes `metadata.tickers[0]` (see sql_tools.py).
              So we dispatch N independent calls in parallel rather than one
              multi-ticker call.
            - Each sub-query reuses the SAME timeout budget as the primary
              Silver task (`_fetch_silver_with_telemetry`) — no new knob,
              no new surprise.
            - Deep-copies the base metadata so audit logs never conflate
              "primary intent" with "HE-derived probe".

        Returns a dict with `source_channel = hyde_expansion` so the Checker
        agent can relax numeric audit for this slice (these values were NOT
        requested by the user).
        """
        if not novel_tickers:
            return {
                "values": {},
                "lineage_anchors": [],
                "citation_contract": {},
                "citation_anchor_map": {},
                "source_channel": "hyde_expansion",
                "trigger_entities": [],
            }

        tasks = []
        for tk in novel_tickers:
            m_copy = base_metadata.model_copy(deep=True)
            m_copy.tickers = [tk.upper()]
            tasks.append(self._fetch_silver_with_telemetry(m_copy))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        aggregated: Dict[str, Any] = {
            "values": {},
            "lineage_anchors": [],
            "citation_contract": {},
            "citation_anchor_map": {},
            "source_channel": "hyde_expansion",
            "trigger_entities": list(novel_tickers),
            "per_ticker_errors": {},
        }
        for tk, r in zip(novel_tickers, results):
            if isinstance(r, Exception):
                aggregated["per_ticker_errors"][tk] = str(type(r).__name__)
                continue
            if not isinstance(r, dict):
                continue
            if r.get("error"):
                aggregated["per_ticker_errors"][tk] = r["error"]
            aggregated["values"].update(r.get("values", {}))
            aggregated["lineage_anchors"].extend(r.get("lineage_anchors", []))
            aggregated["citation_contract"] = _merge_citation_contract(
                aggregated.get("citation_contract"),
                r.get("citation_contract"),
            )
            aggregated["citation_anchor_map"].update(
                r.get("citation_anchor_map")
                or _deprecated_anchor_map_from_contract(r.get("citation_contract"))
            )

        logger.info(
            f"🔁 [HyDE-Compensation] tickers={novel_tickers} | "
            f"values={len(aggregated['values'])} | "
            f"anchors={len(aggregated['lineage_anchors'])} | "
            f"errors={aggregated['per_ticker_errors'] or '{}'}"
        )
        return aggregated

    # ======================================================================
    # D. Main entry — Orchestrator with per-route plan
    # ======================================================================

    async def retrieve(self, user_query: str, query_builder_contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Main pipeline: intent → transform → per-route concurrent fetch → assemble.

        Per-route plan (in plain English):

            hybrid_both : full Gold(top_k=5) + full Silver(metadata)
                         + optional Silver compensation on novel HE tickers

            vector_only : full Gold(top_k=5) as primary
                         + Silver compensation ONLY via HE back-injection
                         (the whole point of "Entity Back-Injection")

            sql_only    : full Silver(metadata) as primary
                         + tiny Gold probe(top_k=2) as news-anchor safety
                         + HyDE paragraph surfaced as `hyde_anticipation` for
                           the Analyst's LLM prior-knowledge hedge
        """
        overall_start = time.time()

        # ==================================================================
        # 1. Intent classification
        # ==================================================================
        intent = await self._classify_intent(user_query)
        route = intent.primary_route

        # ==================================================================
        # 2. Two-stage transform (metadata + HyDE)
        # ==================================================================
        transform_start = time.time()
        transform_res = await self.transformer.transform_for_dual_rag(
            user_query,
            intent,
            query_builder_contract=query_builder_contract,
        )
        logger.info(f"🧠 [Telemetry] Transform cost: {time.time() - transform_start:.3f}s")

        metadata = transform_res.metadata if transform_res else None
        if metadata is None:
            # Transformer failure is unrecoverable here — return a structured
            # empty payload so the Analyst can surface INSUFFICIENT DATA.
            return self._empty_payload(
                intent=route,
                reason="transform_failed",
                start_ts=overall_start,
            )

        # Router hint guardrail for macro-rate queries.
        self._apply_macro_only_source_hint(user_query, metadata)

        # ==================================================================
        # 3. Time-window guardrail + canonical TimeRange
        # ==================================================================
        # schema.handle_empty_window already forces PAST_SIX_MONTHS on
        # invalid/missing input (so this branch almost never fires). We
        # double-check here so a future schema refactor can't silently regress.
        default_applied = False
        if not metadata.time_window:
            metadata.time_window = TimeWindow.PAST_SIX_MONTHS
            default_applied = True
            logger.info("🕒 [TimePolicy] Applied default window: past_six_months (180d)")

        time_range = self._compute_time_range(metadata, default_applied=default_applied)

        if transform_res.out_of_scope_tickers:
            refusal_reason = (
                "Ticker is outside the local covered universe for this pipeline: "
                + ", ".join(transform_res.out_of_scope_tickers)
            )
            return self._out_of_scope_payload(
                intent=route,
                user_query=user_query,
                metadata=metadata,
                time_range=time_range,
                in_scope_tickers=list(transform_res.in_scope_tickers or []),
                out_of_scope_tickers=list(transform_res.out_of_scope_tickers or []),
                reason=refusal_reason,
                start_ts=overall_start,
            )

        # ==================================================================
        # 4. HyDE Entity Back-Injection (regex + whitelist)
        # ==================================================================
        hyde_info = self._extract_hyde_entities(
            hyde_paragraph=transform_res.hyde.hyde_paragraph if transform_res.hyde else "",
            seed_tickers=metadata.tickers,
        )
        novel_tickers: List[str] = hyde_info["novel_tickers"]
        structured_compensation_targets = [
            str(ticker).upper().strip()
            for ticker in self._compensation_targets(metadata, self._resolved_query_family(metadata))
            if str(ticker).strip()
        ]
        compensation_targets: List[str] = []
        for ticker in structured_compensation_targets + novel_tickers:
            if ticker and ticker not in compensation_targets:
                compensation_targets.append(ticker)

        # ==================================================================
        # 5. Build per-route task plan (all three may be None)
        # ==================================================================
        gold_task = None
        silver_task = None
        compensation_task = None
        supplemental_news_task = None

        if route == "hybrid_both":
            gold_task = self._fetch_gold_with_telemetry(user_query, transform_res, top_k=5)
            if metadata.tickers:
                silver_task = self._fetch_silver_with_telemetry(metadata)
            # Optional HE compensation — only fires when the HE introduced
            # NEW tickers the user didn't mention.
            if compensation_targets:
                compensation_task = self._run_silver_compensation(metadata, compensation_targets)

        elif route == "vector_only":
            gold_task = self._fetch_gold_with_telemetry(user_query, transform_res, top_k=5)
            # Primary Silver is OFF by design for vector_only; compensation is
            # the ONLY path into Parquet — realises "Entity Back-Injection".
            if compensation_targets:
                compensation_task = self._run_silver_compensation(metadata, compensation_targets)
            elif metadata.tickers:
                # If HE produced nothing novel but the user explicitly named
                # tickers, still run Silver as a courtesy (labelled primary).
                silver_task = self._fetch_silver_with_telemetry(metadata)

        elif route == "sql_only":
            # Primary = Silver; Gold is a tiny fallback probe.
            if metadata.tickers:
                silver_task = self._fetch_silver_with_telemetry(metadata)
            gold_task = self._fetch_gold_with_telemetry(
                user_query, transform_res, top_k=_SQL_ONLY_FALLBACK_TOPK
            )
            # HE compensation is still available when HE produces novel tickers —
            # realises "Semantic-SQL Parallelism". The hyde_anticipation payload
            # below also exposes the full HyDE paragraph to the Analyst so the
            # LLM prior knowledge can fill the gap when Parquet is empty.
            if compensation_targets:
                compensation_task = self._run_silver_compensation(metadata, compensation_targets)

        else:
            # Unknown route — degrade to hybrid.
            logger.warning(f"[Route] Unknown primary_route={route} — falling back to hybrid_both.")
            gold_task = self._fetch_gold_with_telemetry(user_query, transform_res, top_k=5)
            if metadata.tickers:
                silver_task = self._fetch_silver_with_telemetry(metadata)

        # ==================================================================
        # 6. Execute plan concurrently with per-task isolation
        # ==================================================================
        awaitables = []
        slots: List[str] = []
        for name, t in (("gold", gold_task), ("silver", silver_task), ("compensation", compensation_task), ("supplemental_news", supplemental_news_task)):
            if t is not None:
                awaitables.append(t)
                slots.append(name)

        results = await asyncio.gather(*awaitables, return_exceptions=True) if awaitables else []

        # ==================================================================
        # 7. Assemble the final context
        # ==================================================================
        final_context: Dict[str, Any] = {
            "intent": route,
            "metadata": metadata,
            "time_range": time_range,
            "gold_context": [],
            "supplemental_news_context": [],
            "sec_retrieval_contract": {},
            "silver_context": {
                "values": {},
                "lineage_anchors": [],
                "citation_contract": {},
                "citation_anchor_map": {},
                "source_channel": "primary",
            },
            "hyde_anticipation": {
                "paragraph": hyde_info["paragraph"],
                "rerank_query": (transform_res.hyde.rerank_query if transform_res.hyde else ""),
                "raw_candidates": hyde_info["raw_candidates"],
                "whitelisted_tickers": hyde_info["whitelisted"],
                "novel_tickers": novel_tickers,
                "compensation_targets": compensation_targets,
                # Only sql_only lifts the HE paragraph as a semantic hedge;
                # the other routes still expose it for auditability but the
                # Analyst will weight it lower.
                "source_channel": "semantic_hedge" if route == "sql_only" else "reference",
            },
            "status": "success",
            "latency_stats": {},
        }

        partial_failure = False
        for slot, res in zip(slots, results):
            if isinstance(res, Exception):
                logger.critical(f"🔥 Slot {slot} raised: {repr(res)}")
                partial_failure = True
                continue

            if slot == "gold":
                final_context["gold_context"] = res or []
                final_context["sec_retrieval_contract"] = self.qdrant.get_last_sec_retrieval_contract()
            elif slot == "supplemental_news":
                final_context["supplemental_news_context"] = res or []
            elif slot == "silver":
                primary_silver = res or {"values": {}, "lineage_anchors": [], "citation_contract": {}, "citation_anchor_map": {}}
                if primary_silver.get("error"):
                    partial_failure = True
                final_context["silver_context"] = {
                    "values": primary_silver.get("values", {}),
                    "lineage_anchors": primary_silver.get("lineage_anchors", []),
                    "citation_contract": primary_silver.get("citation_contract", {}),
                    "citation_anchor_map": primary_silver.get("citation_anchor_map", {}),
                    "source_channel": "primary",
                    **(
                        {"error": primary_silver["error"]}
                        if primary_silver.get("error") else {}
                    ),
                }
            elif slot == "compensation":
                comp = res or {}
                # Physical separation: HE-triggered data lives under
                # `silver_context.compensation`, NOT merged into primary
                # values. Downstream the Checker agent reads this key to
                # relax numeric drift rules for HE data.
                final_context["silver_context"]["compensation"] = {
                    "values": comp.get("values", {}),
                    "lineage_anchors": comp.get("lineage_anchors", []),
                    "citation_contract": comp.get("citation_contract", {}),
                    "citation_anchor_map": comp.get("citation_anchor_map", {}),
                    "source_channel": "silver_layer_via_hyde_expansion",
                    "trigger_entities": comp.get("trigger_entities", []),
                    "per_ticker_errors": comp.get("per_ticker_errors", {}),
                }

        if partial_failure:
            final_context["status"] = "partial_failure"

        # ==================================================================
        # 7.5  Macro → Silver anchor injection (Root Cause A fix, 2026-04-22)
        # ==================================================================
        # Promote macro data into Silver context. The primary source is now
        # Silver Macro_History parquet (authoritative numeric truth); markdown
        # is only a fallback if parquet is unavailable. This gives the Analyst
        # citable MACRO_* anchors while preserving schema/status metadata for
        # downstream deterministic checking and drift audits.
        try:
            macro_md = self._read_macro_snapshot()
            anchor_for_macro = date.fromisoformat(time_range["anchor_date"]) \
                if time_range and time_range.get("anchor_date") else date.today()
            macro_patch = _build_macro_silver_patch(macro_md, anchor_for_macro)
            if macro_patch["values"]:
                sv = final_context["silver_context"]
                sv.setdefault("values", {}).update(macro_patch["values"])
                existing_anchors = sv.setdefault("lineage_anchors", [])
                _extend_silver_context_contract(
                    self.sql_tool,
                    sv,
                    values=macro_patch["values"],
                    lineage_anchors=list(macro_patch.get("lineage_anchors", [])),
                    observed_at=str(anchor_for_macro),
                    source_channel=str(sv.get("source_channel", "primary") or "primary"),
                    explicit_contract=macro_patch.get("citation_contract"),
                )
                patch_status = macro_patch.get("status") or {}
                if patch_status:
                    existing_status = sv.setdefault("status", {})
                    for k, v in patch_status.items():
                        if isinstance(v, dict) and isinstance(existing_status.get(k), dict):
                            existing_status[k].update(v)
                        else:
                            existing_status[k] = v
                # De-duplicate while preserving ordering of the existing anchors.
                seen = set(existing_anchors)
                for a in macro_patch["lineage_anchors"]:
                    if a not in seen:
                        existing_anchors.append(a)
                        seen.add(a)
                logger.info(
                    f"🌐 [MacroSilverPatch] injected {len(macro_patch['values'])} values + "
                    f"{len(macro_patch['lineage_anchors'])} MACRO_* anchors."
                )
        except Exception as e:
            # Non-fatal: the pipeline still produces a full context without
            # macro anchors; the Analyst simply loses macro citability.
            logger.warning(f"MacroSilverPatch failed ({type(e).__name__}): {e}")

        # ==================================================================
        # 7.6  GPR always-on patch (Root Cause gap-2 fix, 2026-04-23)
        # ==================================================================
        # Inject the latest GPR row unconditionally — previously this only
        # ran when the query routed to _handle_geopolitical_analysis (i.e.
        # user explicitly asked about geopolitics).  The Analyst prompt's
        # MACRO_CHAIN_DIRECTIVE mandates a "GPR index" mention in the Macro
        # Regime Snapshot section regardless of query type, so the anchor
        # must always be available for citation.
        try:
            import copy as _copy
            gpr_result = self.sql_tool._handle_geopolitical_analysis(
                ticker="GPR", meta=metadata
            )
            if gpr_result and gpr_result.get("values"):
                sv = final_context["silver_context"]
                sv.setdefault("values", {}).update(gpr_result["values"])
                _extend_silver_context_contract(
                    self.sql_tool,
                    sv,
                    values=gpr_result["values"],
                    lineage_anchors=list(gpr_result.get("lineage_anchors", [])),
                    observed_at=gpr_result.get("observed_at"),
                    source_channel=str(sv.get("source_channel", "primary") or "primary"),
                    explicit_contract=gpr_result.get("citation_contract"),
                )
                existing = set(sv.setdefault("lineage_anchors", []))
                for a in gpr_result.get("lineage_anchors", []):
                    if a not in existing:
                        sv["lineage_anchors"].append(a)
                        existing.add(a)
                logger.info(
                    f"🌍 [GPRPatch] injected {len(gpr_result['values'])} values "
                    f"+ {len(gpr_result.get('lineage_anchors', []))} GPR anchors."
                )
        except Exception as e:
            logger.warning(f"GPRPatch failed ({type(e).__name__}): {e}")

        # ==================================================================
        # 7.7  Macro Parquet full-scan patch (Root Cause gap-1 fix, 2026-04-23)
        # ==================================================================
        # Read ALL symbols from Macro_History for the latest observation date
        # and inject them into silver_context.  This supersedes the markdown
        # regex parser for any symbol that parquet already covers (parquet wins
        # on precision) and adds long-tail symbols the regex never handled.
        try:
            macro_glob = self.sql_tool.macro_glob
            macro_query = f"""
                SELECT symbol, value, daily_change_pct, mom_change_pct, observation_date
                FROM read_parquet('{macro_glob}')
                WHERE observation_date = (
                    SELECT MAX(observation_date) FROM read_parquet('{macro_glob}')
                )
            """
            rows = self.sql_tool.conn.execute(macro_query).fetchall()
            if rows:
                sv = final_context["silver_context"]
                pq_values: dict = {}
                pq_anchors: list = []
                existing_anchors = set(sv.setdefault("lineage_anchors", []))
                for sym, val, daily_chg, mom_chg, obs_date in rows:
                    # Sanitise symbol for use as a dict key / anchor name.
                    safe = sym.lstrip("^").replace("-", "_").replace(".", "_")
                    if val is not None:
                        pq_values[f"{safe}_value"] = round(float(val), 4)
                    chg = mom_chg if mom_chg is not None else daily_chg
                    if chg is not None:
                        pq_values[f"{safe}_change_pct"] = round(float(chg), 4)
                    anchor_id = f"MACRO_{safe}_{obs_date}"
                    if anchor_id not in existing_anchors:
                        pq_anchors.append(anchor_id)
                        existing_anchors.add(anchor_id)
                sv["values"].update(pq_values)
                sv["lineage_anchors"].extend(pq_anchors)
                _extend_silver_context_contract(
                    self.sql_tool,
                    sv,
                    values=pq_values,
                    lineage_anchors=pq_anchors,
                    observed_at=str(rows[0][4]),
                    source_channel=str(sv.get("source_channel", "primary") or "primary"),
                )
                logger.info(
                    f"📊 [MacroParquetPatch] injected {len(pq_values)} values "
                    f"+ {len(pq_anchors)} anchors from {len(rows)} Macro symbols."
                )
        except Exception as e:
            logger.warning(f"MacroParquetPatch failed ({type(e).__name__}): {e}")

        # ==================================================================
        # 7.8  Write immutable silver_context_frozen (P1 fix, 2026-04-23)
        # ==================================================================
        # After ALL patch steps are complete, snapshot the Silver context so
        # CheckerAgent always has a stable, rescue-independent ground truth.
        # Rescue passes may still update `silver_context` (used by the NEXT
        # analyst revision for additive data), but the Checker's deterministic
        # numeric audit uses `silver_context_frozen` exclusively — preventing
        # atm_iv oscillation and cross-revision anchor drift.
        try:
            import copy as _copy
            final_context["silver_context_frozen"] = _copy.deepcopy(
                final_context["silver_context"]
            )
        except Exception as e:
            logger.warning(f"silver_context_frozen deepcopy failed ({type(e).__name__}): {e}")
            final_context["silver_context_frozen"] = None

        try:
            scope_contract, retrieval_outcome = self._build_runtime_contracts(
                user_query=user_query,
                route=route,
                metadata=metadata,
                silver_context=final_context["silver_context"],
                silver_context_frozen=final_context.get("silver_context_frozen"),
                gold_context=final_context["gold_context"],
                supplemental_news_context=final_context.get("supplemental_news_context", []),
                sec_retrieval_contract=final_context.get("sec_retrieval_contract"),
                time_range=time_range,
                is_fallback=(final_context["status"] != "success"),
                in_scope_tickers=list(transform_res.in_scope_tickers or []),
                out_of_scope_tickers=list(transform_res.out_of_scope_tickers or []),
            )
            final_context["scope_contract"] = scope_contract
            final_context["retrieval_outcome"] = retrieval_outcome
        except Exception as e:
            logger.warning(f"runtime contract compilation failed ({type(e).__name__}): {e}")
            final_context["scope_contract"] = None
            final_context["retrieval_outcome"] = None

        final_context["latency_stats"]["total_e2e"] = f"{time.time() - overall_start:.3f}s"
        logger.info(
            f"✅ [Master Retriever] route={route} | E2E: {final_context['latency_stats']['total_e2e']} "
            f"| status={final_context['status']} | HE_compensation="
            f"{'YES' if novel_tickers else 'NO'}"
        )
        return final_context

    # ======================================================================
    # D.5 Utility — macro snapshot loader (lookup, cached per process)
    # ======================================================================

    _MACRO_SNAPSHOT_CACHE: Optional[str] = None
    _MACRO_SNAPSHOT_MTIME: Optional[float] = None

    def _read_macro_snapshot(self) -> Optional[str]:
        """Read `Data/Agent_Context/latest_macro_context.md` with a per-process
        mtime cache. Router also reads this file for the Analyst prompt, but
        we re-read here so master_retriever's Silver patch stays self-
        contained (retriever owns the silver_context contract end-to-end).

        Returns None if the file is absent — `_build_macro_silver_patch`
        then no-ops cleanly. Any IO error is swallowed: a missing or
        malformed macro snapshot must NEVER take down retrieval.
        """
        try:
            project_root = Path(__file__).resolve().parents[2]
            fp = project_root / "Data" / "Agent_Context" / "latest_macro_context.md"
            if not fp.exists():
                return None
            mtime = fp.stat().st_mtime
            if (
                self._MACRO_SNAPSHOT_CACHE is not None
                and self._MACRO_SNAPSHOT_MTIME == mtime
            ):
                return self._MACRO_SNAPSHOT_CACHE
            content = fp.read_text(encoding="utf-8")
            # Cache on the instance (class attribute holder) so concurrent
            # calls within the same process reuse the parsed result.
            MasterRetriever._MACRO_SNAPSHOT_CACHE = content
            MasterRetriever._MACRO_SNAPSHOT_MTIME = mtime
            return content
        except Exception as e:
            logger.debug(f"_read_macro_snapshot: non-fatal {type(e).__name__}: {e}")
            return None

    # ======================================================================
    # E. Utility — structured empty payload on hard failure
    # ======================================================================

    def _empty_payload(self, intent: str, reason: str, start_ts: float) -> Dict[str, Any]:
        """Return a fully-typed, Analyst-safe payload when Transform blows up."""
        logger.error(f"❌ [Master Retriever] Hard failure: {reason}")
        return {
            "intent": intent,
            "metadata": None,
            "time_range": None,
            "gold_context": [],
            "supplemental_news_context": [],
            "sec_retrieval_contract": {},
            "silver_context": {
                "values": {},
                "lineage_anchors": [],
                "citation_contract": {},
                "citation_anchor_map": {},
                "source_channel": "primary",
                "error": reason,
            },
            "hyde_anticipation": None,
            "scope_contract": None,
            "retrieval_outcome": None,
            "status": "partial_failure",
            "latency_stats": {"total_e2e": f"{time.time() - start_ts:.3f}s"},
        }

    def _out_of_scope_payload(
        self,
        *,
        intent: str,
        user_query: str,
        metadata: MetadataExtraction,
        time_range: Dict[str, Any],
        in_scope_tickers: List[str],
        out_of_scope_tickers: List[str],
        reason: str,
        start_ts: float,
    ) -> Dict[str, Any]:
        """Return a structured refusal payload for unsupported ticker scope."""
        scope_contract, retrieval_outcome = self._build_runtime_contracts(
            user_query=user_query,
            route=intent,
            metadata=metadata,
            silver_context={"values": {}, "lineage_anchors": [], "citation_contract": {}, "citation_anchor_map": {}},
            silver_context_frozen=None,
            gold_context=[],
            supplemental_news_context=[],
            sec_retrieval_contract={},
            time_range=time_range,
            is_fallback=True,
            in_scope_tickers=in_scope_tickers,
            out_of_scope_tickers=out_of_scope_tickers,
            refusal_reason=reason,
        )
        return {
            "intent": intent,
            "metadata": metadata,
            "time_range": time_range,
            "gold_context": [],
            "supplemental_news_context": [],
            "sec_retrieval_contract": {},
            "silver_context": {
                "values": {},
                "lineage_anchors": [],
                "citation_contract": {},
                "citation_anchor_map": {},
                "source_channel": "primary",
                "error": reason,
            },
            "hyde_anticipation": None,
            "scope_contract": scope_contract,
            "retrieval_outcome": retrieval_outcome,
            "status": "partial_failure",
            "latency_stats": {"total_e2e": f"{time.time() - start_ts:.3f}s"},
        }
