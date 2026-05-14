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
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta, date
from pathlib import Path
from dotenv import load_dotenv

project_root = str(Path(__file__).resolve().parents[2])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load environment variables (including model cache related settings).
load_dotenv(os.path.join(project_root, ".env"))

from qdrant_client import QdrantClient, models
from fastembed import SparseTextEmbedding
from sentence_transformers import CrossEncoder

try:
    # Prefer package-relative imports when used as part of Scripts.retrieval.
    from ..vector_store.connection import get_qdrant_client, get_embedding_model
    from .schema import (
        RetrievedChunk, SourceType, FullTransformationResult, TimeWindow, ActionDirection, SentimentTarget,
        TIME_WINDOW_DAYS,
    )
    from ..core.financial_ontology import (
        EVENT_KEYWORDS_MAPPING, NEWS_TOPICS, normalize_news_topic,
    )
    from ..core.financial_narrative_contract import (
        dense_news_semantic_query,
        expanded_news_rerank_query,
        sparse_news_keyword_query,
        structured_news_relevance_score,
    )
    from .time_adapter import (
        SourceTimeKey,
        TimePredicate,
        union_epoch_range,
    )
    from ..observability.audit import record_retrieval_fallback_kpi
    from ..observability.audit import append_audit_jsonl
except ImportError as e:
    try:
        # Fallback to absolute imports when this file is executed directly.
        from Scripts.vector_store.connection import get_qdrant_client, get_embedding_model
        from Scripts.retrieval.schema import (
            RetrievedChunk, SourceType, FullTransformationResult, TimeWindow, ActionDirection, SentimentTarget,
            TIME_WINDOW_DAYS,
        )
        from Scripts.core.financial_ontology import (
            EVENT_KEYWORDS_MAPPING, NEWS_TOPICS, normalize_news_topic,
        )
        from Scripts.core.financial_narrative_contract import (
            dense_news_semantic_query,
            expanded_news_rerank_query,
            sparse_news_keyword_query,
            structured_news_relevance_score,
        )
        from Scripts.retrieval.time_adapter import (
            SourceTimeKey,
            TimePredicate,
            union_epoch_range,
        )
        from Scripts.observability.audit import record_retrieval_fallback_kpi
        from Scripts.observability.audit import append_audit_jsonl
    except ImportError:
        record_retrieval_fallback_kpi = None
        append_audit_jsonl = None
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
        self._last_sec_retrieval_contract: Dict[str, Any] = {}

        # Pre-warm ONNX Runtime thread pool so the first real query does not
        # absorb the JIT compilation cost (~3-8 s for SPLADE on CPU).
        try:
            list(self.sparse_model.query_embed("warmup"))
            self.dense_model.embed_query("warmup")
            logger.info("✅ Embedding models warmed up (ONNX thread pool ready).")
        except Exception as _warmup_err:
            logger.warning("⚠️ Warmup embedding failed (non-fatal): %s", _warmup_err)

        logger.info(f"✅ Models initialized. Reranker: {reranker_model_name}")
        self._is_initialized = True

    # Source types physically stored in the Gold (Qdrant) vector layer.
    # Every other value the LLM extractor emits ("macro", "options", etc.)
    # represents a Silver (Parquet) domain; those are stripped before any
    # Gold filter is built so the filter never guarantees zero results.
    # Used by both _build_smart_filter and retrieve_async (single definition).
    _GOLD_SOURCE_TYPES: frozenset = frozenset({"news", "sec", "gpr"})

    # Tickers whose news coverage is topic-indexed, not ticker-indexed in Qdrant.
    # For these tickers a hard `ticker` must-filter on the news collection is
    # structurally guaranteed to return zero results; the fast-path in
    # retrieve_async uses soft/drop mode instead.
    _NEWS_TOPIC_INDEXED_TICKERS: frozenset = frozenset({"SPY", "QQQ", "IWM", "GLD", "SLV"})

    # ------------------------------------------------------------------
    # Topic derivation — bridges LLM metadata to news_scraper's `topic` field.
    # ------------------------------------------------------------------
    @staticmethod
    def _derive_news_topics(metadata: Any) -> List[str]:
        """Return the news `topic` values worth filtering on for this query.

        Resolution order (most-specific first):
          1. `canonical_news_topics` emitted by transform.
          2. `event_keyword` → category via EVENT_KEYWORDS_MAPPING.
          3. The category itself, if the LLM wrote one that already matches
             a canonical news topic (covers "macro_central_banks" etc.).
          4. Empty list → no topic constraint.

        The result is normalised to the news-scraper topic form via
        `normalize_news_topic` so queries carrying the short alias
        (e.g. "precious_metals_spot") still match ingested docs written
        with the long form ("asset_precious_metals_spot").
        """
        candidates: List[str] = []
        for topic in getattr(metadata, "expanded_news_topics", []) or []:
            candidates.append(str(topic or "").strip().lower())
        for topic in getattr(metadata, "canonical_news_topics", []) or []:
            candidates.append(str(topic or "").strip().lower())

        ev = getattr(metadata, "event_keyword", "") or ""
        if ev:
            mapped = EVENT_KEYWORDS_MAPPING.get(ev.strip().lower(), "")
            if mapped:
                candidates.append(mapped)

        # Some Extractor outputs put the category verbatim into event_keyword
        # (e.g. "macro_central_banks"). Try to land that directly.
        if ev:
            candidates.append(ev.strip().lower())

        normalised = []
        for c in candidates:
            n = normalize_news_topic(c)
            if n and n in NEWS_TOPICS and n not in normalised:
                normalised.append(n)
        return normalised

    # ------------------------------------------------------------------
    # Time-predicate helpers (Gold uses the union of per-source windows)
    # ------------------------------------------------------------------
    # Mapping from Qdrant `source_type` payload value → SourceTimeKey.
    # Used to pick exactly the predicates relevant to this query so the
    # union range is never wider than needed.
    _SOURCE_TYPE_TO_KEY: Dict[str, "SourceTimeKey"] = {
        "news": SourceTimeKey.GOLD_NEWS,
        "sec":  SourceTimeKey.GOLD_SEC,
        "gpr":  SourceTimeKey.GOLD_GPR,
    }

    def _select_gold_predicates(
        self,
        source_vals: List[str],
        time_predicates: Optional[Dict["SourceTimeKey", "TimePredicate"]],
    ) -> List["TimePredicate"]:
        """Pick the predicates that actually apply to the selected Gold sources.

        If `time_predicates` is None (legacy caller / standalone test), return
        `[]` — the caller will fall back to the inline day-count logic.
        If `source_vals` is empty (all-source scan), every Gold predicate
        applies; callers typically avoid this path in production.
        """
        if not time_predicates:
            return []
        keys = [self._SOURCE_TYPE_TO_KEY[v] for v in source_vals if v in self._SOURCE_TYPE_TO_KEY]
        if not keys:
            keys = list(self._SOURCE_TYPE_TO_KEY.values())
        return [time_predicates[k] for k in keys if k in time_predicates]

    @staticmethod
    def _range_capable_time_keys(
        source_vals: List[str],
        selected_predicates: Optional[List["TimePredicate"]] = None,
    ) -> List[str]:
        keys: List[str] = []
        if selected_predicates:
            for pred in selected_predicates:
                for key, unit in zip(pred.time_keys, pred.key_units):
                    if str(unit).lower() == "epoch_s" and key not in keys:
                        keys.append(key)
        if not keys:
            for key in ("unified_timestamp", "publish_timestamp"):
                if key not in keys:
                    keys.append(key)
            if "sec" in [str(s).lower() for s in source_vals]:
                for key in ("transaction_date_epoch_s", "filed_at_epoch_s"):
                    if key not in keys:
                        keys.append(key)
        return keys

    # ------------------------------------------------------------------
    # Filter builder — now three-mode (hard / soft-ticker / no-ticker).
    # ------------------------------------------------------------------
    def _build_smart_filter(
        self,
        metadata: Any,
        ignore_time: bool = False,
        fallback_days: Optional[int] = None,
        ticker_mode: str = "hard",   # one of: "hard", "soft", "drop"
        time_predicates: Optional[Dict["SourceTimeKey", "TimePredicate"]] = None,
        sec_form_override: Optional[str] = None,
    ) -> Optional[models.Filter]:
        """Compose a Qdrant filter with configurable ticker strictness.

        ticker_mode semantics (critical — this is the fix for "Gold layer
        returns 0 even though ticker exists"):

          * "hard" — ticker goes into `must`. Every returned doc MUST have
            a matching ticker payload. Historical behaviour; use when the
            caller is confident the payload is always present (SEC).

          * "soft" — ticker goes into `should` together with a `topic`
            condition (when derivable). `min_should=1` means a doc is
            accepted if EITHER it matches a ticker OR it matches a news
            topic for this query. Restores recall for news docs whose
            `ticker` field is still NULL pre-enrichment.

          * "drop" — ticker is omitted entirely. Retains source_type +
            time window + (optional) topic. Used as a last-resort third
            fallback so "no ticker anywhere" queries still surface macro
            articles — better to return thematic context than silence.
        """
        must_conditions: List[models.FieldCondition] = []
        should_conditions: List[models.FieldCondition] = []

        def _val(obj):
            return getattr(obj, "value", obj)

        # Gold Layer is the vector store for {news, sec, gpr} ONLY. The
        # LLM extractor occasionally emits labels like "options" or "macro"
        # which are semantic domains, not physical source types — those
        # datasets live in Silver (Parquet). Keeping them in the filter
        # guarantees zero results, so we strip them HERE (single choke
        # point) and keep the rest of the pipeline untouched.
        raw_source_vals = [_val(s) for s in metadata.source_types] if metadata.source_types else []
        source_vals = [s for s in raw_source_vals if str(s).lower() in self._GOLD_SOURCE_TYPES]
        dropped_src = set(raw_source_vals) - set(source_vals)
        if dropped_src:
            logger.info(
                f"🧹 [GoldFilter] Dropped Silver-only source_types {sorted(dropped_src)}; "
                f"Gold kept={source_vals or 'ALL (no filter)'}"
            )

        primary_theme = str(getattr(metadata, "primary_theme", "") or "").strip().lower()
        primary_surface = str(getattr(metadata, "primary_surface", "") or "").strip().lower()
        if (
            primary_theme == "geopolitics"
            and primary_surface == "macro_news_surface"
            and "news" in [str(s).lower() for s in source_vals]
        ):
            source_vals = ["news"]
            logger.info(
                "📰 [GoldFilter] Geopolitics narrative mode uses news-only Gold retrieval; "
                "GPR stays in structured background."
            )

        # SEC-overconstraint guard:
        # Macro/options queries on broad ETFs (SPY/QQQ/IWM/GLD/SLV) can be
        # mislabelled as SEC by the extractor, which then forces strict
        # action/form filters and collapses Tier1 recall to zero. For ETF/index
        # tickers, SEC is not a meaningful source channel; drop it early.
        ticker_list = list(metadata.tickers) if metadata.tickers else []
        if source_vals and "sec" in source_vals:
            upper_tickers = [str(t).upper() for t in ticker_list]
            if upper_tickers and all(t in self._NEWS_TOPIC_INDEXED_TICKERS or t.startswith("^") for t in upper_tickers):
                source_vals = [s for s in source_vals if s != "sec"]
                logger.info(
                    "🧹 [GoldFilter] Dropped SEC source_type for ETF/index macro query; "
                    f"tickers={upper_tickers} | gold kept={source_vals or 'ALL'}"
                )

        # --- Ticker placement (hard / soft / drop) -----------------------
        if ticker_list and ticker_mode == "hard":
            must_conditions.append(
                models.FieldCondition(key="ticker", match=models.MatchAny(any=ticker_list))
            )
        elif ticker_list and ticker_mode == "soft":
            should_conditions.append(
                models.FieldCondition(key="ticker", match=models.MatchAny(any=ticker_list))
            )
        # ticker_mode == "drop" → omit entirely.

        # --- Topic enrichment (news only) --------------------------------
        # Emitted only when the query's event/category maps to a known
        # news topic. Placed in `should` so it widens recall; combined
        # with ticker-soft via min_should=1.
        if "news" in source_vals:
            topics = self._derive_news_topics(metadata)
            if topics:
                should_conditions.append(
                    models.FieldCondition(key="topic", match=models.MatchAny(any=topics))
                )

        # --- source_type filter (hard) -----------------------------------
        if source_vals:
            must_conditions.append(
                models.FieldCondition(key="source_type", match=models.MatchAny(any=source_vals))
            )

        # --- SEC-specific refinements (hard) -----------------------------
        if "sec" in source_vals:
            action_val = _val(metadata.action_direction)
            if str(action_val).upper() == "SELL":
                must_conditions.append(models.FieldCondition(key="action_direction", match=models.MatchAny(any=["SELL", "ACQUIRE/VEST"])))
            elif str(action_val).upper() == "BUY":
                must_conditions.append(models.FieldCondition(key="action_direction", match=models.MatchAny(any=["BUY", "ACQUIRE/VEST"])))
            elif str(action_val).upper() != "NONE":
                must_conditions.append(models.FieldCondition(key="action_direction", match=models.MatchValue(value=action_val)))

            requested_sec_forms = self._requested_sec_forms(metadata)
            form_val = _val(metadata.form_type)
            if sec_form_override:
                must_conditions.append(models.FieldCondition(key="form_type", match=models.MatchValue(value=str(sec_form_override).strip().upper())))
            elif len(requested_sec_forms) == 1:
                must_conditions.append(models.FieldCondition(key="form_type", match=models.MatchValue(value=requested_sec_forms[0])))
            elif str(form_val).upper() != "ALL":
                must_conditions.append(models.FieldCondition(key="form_type", match=models.MatchValue(value=form_val)))

        # --- News sentiment filter (hard) --------------------------------
        if "news" in source_vals:
            sentiment_val = _val(metadata.sentiment_target)
            if str(sentiment_val).upper() == "NEGATIVE":
                must_conditions.append(models.FieldCondition(key="tone_score", range=models.Range(lt=0)))
            elif str(sentiment_val).upper() == "POSITIVE":
                must_conditions.append(models.FieldCondition(key="tone_score", range=models.Range(gt=0)))

        # --- Time barrier (MUST — matches ANY of several timestamp keys) ---
        # Two-layer design, landed 2026-04-22:
        #
        # 1. **Per-source alignment** (primary path, when `time_predicates`
        #    is supplied by master_retriever):
        #      Each Gold source has its own physical cadence — news is event-
        #      level, SEC is event-level but 3-day weekend-safe, GPR is
        #      MONTHLY and must widen a "yesterday" request to the full
        #      current month. We take the UNION of epoch windows for the
        #      sources actually selected (respecting `source_vals`), which
        #      produces a single numeric Range wide enough to satisfy every
        #      picked source's cadence without widening further.
        #
        # 2. **Legacy inline fallback** (when `time_predicates` is None —
        #    standalone retriever test, e.g. the `__main__` block):
        #      Reverts to the 2026-04-22-era single-window inline logic so
        #      existing tests keep working without a MasterRetriever.
        #
        # Both paths wrap the Range in a nested `Filter(should=[...])`
        # inside the outer `must`. Qdrant evaluates the nested filter as
        # "at least one timestamp key must land in range" — this preserves
        # the hard-time contract while supporting BOTH the new
        # `unified_timestamp` key and the legacy `publish_timestamp` key
        # without forcing a full re-ingest.
        if not ignore_time:
            start_timestamp: int
            end_timestamp: int
            time_source_tag = "union_per_source"

            selected_predicates: List["TimePredicate"] = []
            if fallback_days is not None:
                # Legacy fallback-tier escalation path (Tier 2/3) — force
                # a single wide window; ignore per-source widening so the
                # retry is deterministic.
                now = datetime.now()
                start_timestamp = int((now - timedelta(days=fallback_days)).timestamp())
                end_timestamp = int(time.time())
                time_source_tag = f"legacy_fallback_{fallback_days}d"
            elif time_predicates:
                selected_predicates = self._select_gold_predicates(source_vals, time_predicates)
                if selected_predicates:
                    start_timestamp, end_timestamp = union_epoch_range(selected_predicates)
                    widened = [p.source.value for p in selected_predicates if p.widened]
                    if widened:
                        logger.info(
                            f"🕒 [GoldTime] Per-source widening applied: {widened} | "
                            f"union window: {datetime.fromtimestamp(start_timestamp).date()}..{datetime.fromtimestamp(end_timestamp).date()}"
                        )
                else:
                    # source_vals was empty AND no predicates matched — fall
                    # back to the inline legacy window.
                    now = datetime.now()
                    time_val = _val(metadata.time_window)
                    time_key = getattr(time_val, "value", time_val)
                    if not isinstance(time_key, str):
                        time_key = str(time_key) if time_key is not None else ""
                    days_delta = TIME_WINDOW_DAYS.get(time_key.lower(), 30)
                    start_timestamp = int((now - timedelta(days=days_delta)).timestamp())
                    end_timestamp = int(time.time())
                    time_source_tag = f"inline_{days_delta}d"
            else:
                # time_predicates not supplied — legacy inline window.
                now = datetime.now()
                time_val = _val(metadata.time_window)
                time_key = getattr(time_val, "value", time_val)
                if not isinstance(time_key, str):
                    time_key = str(time_key) if time_key is not None else ""
                days_delta = TIME_WINDOW_DAYS.get(time_key.lower(), 30)
                start_timestamp = int((now - timedelta(days=days_delta)).timestamp())
                end_timestamp = int(time.time())
                time_source_tag = f"inline_{days_delta}d"

            time_range = models.Range(gte=start_timestamp, lte=end_timestamp)
            time_keys = self._range_capable_time_keys(source_vals, selected_predicates)
            must_conditions.append(
                models.Filter(
                    should=[models.FieldCondition(key=key, range=time_range) for key in time_keys]
                )
            )
            logger.debug(
                f"🕒 [GoldTime] mode={time_source_tag} | "
                f"range=[{datetime.fromtimestamp(start_timestamp).date()}..{datetime.fromtimestamp(end_timestamp).date()}] "
                f"| gte_epoch_s={start_timestamp} lte_epoch_s={end_timestamp} | keys={time_keys}"
            )

        # --- Assemble final filter ---------------------------------------
        # Qdrant default semantic (stable since v1.x): when `should` is set
        # alongside `must`, a point is accepted iff ALL must conditions pass
        # AND at least one should condition matches. That's exactly the
        # "ticker OR topic" recall we want — no MinShould wrapper needed,
        # keeping the call portable across qdrant-client minor versions.
        if not must_conditions and not should_conditions:
            return None

        kwargs: Dict[str, Any] = {}
        if must_conditions:
            kwargs["must"] = must_conditions
        if should_conditions:
            kwargs["should"] = should_conditions
        return models.Filter(**kwargs)

    @staticmethod
    def _requested_sec_forms(metadata: Any) -> List[str]:
        """Return requested SEC forms as a list.

        The structured list is the canonical representation for SEC queries
        that may request multiple filing types at once. `form_type` is kept
        only as a legacy single-value summary for older callers.
        """
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

    def _reset_last_sec_retrieval_contract(self) -> None:
        self._last_sec_retrieval_contract = {
            "sec_payload_context_by_form": {},
            "sec_ranked_context_by_form": {},
            "sec_raw_hits_by_form": {},
            "sec_raw_hits_total": 0,
            "sec_rerank_kept_total": 0,
            "sec_rerank_dropped_total": 0,
            "sec_hit_stage": "not_applicable",
        }

    def get_last_sec_retrieval_contract(self) -> Dict[str, Any]:
        return dict(self._last_sec_retrieval_contract or {})

    @staticmethod
    def _sec_point_identity(point: Any) -> str:
        payload = dict(getattr(point, "payload", None) or {})
        return (
            str(payload.get("accession_no") or "").strip()
            or str(payload.get("url") or "").strip()
            or str(getattr(point, "id", "") or "").strip()
        )

    @staticmethod
    def _sec_point_epoch(point: Any) -> int:
        payload = dict(getattr(point, "payload", None) or {})
        for key in ("transaction_date_epoch_s", "filed_at_epoch_s", "unified_timestamp"):
            raw = payload.get(key)
            if isinstance(raw, (int, float)):
                return int(raw)
        return 0

    def _dedupe_and_sort_sec_points(self, points: List[Any]) -> List[Any]:
        ordered = sorted(points or [], key=self._sec_point_epoch, reverse=True)
        seen = set()
        out: List[Any] = []
        for point in ordered:
            identity = self._sec_point_identity(point)
            if identity in seen:
                continue
            seen.add(identity)
            out.append(point)
        return out

    async def _scroll_points(self, q_filter: Optional[models.Filter], limit: int) -> List[Any]:
        records, _ = await asyncio.to_thread(
            self.client.scroll,
            collection_name=self.collection_name,
            scroll_filter=q_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return list(records or [])

    async def _retrieve_sec_payload_context_by_form(
        self,
        metadata: Any,
        top_k: int,
        time_predicates: Optional[Dict["SourceTimeKey", "TimePredicate"]],
    ) -> Dict[str, List[Any]]:
        requested_forms = self._requested_sec_forms(metadata)
        if not requested_forms:
            return {}

        by_form: Dict[str, List[Any]] = {}
        for form in requested_forms:
            q_filter = self._build_smart_filter(
                metadata,
                ignore_time=False,
                ticker_mode="hard",
                time_predicates=time_predicates,
                sec_form_override=form,
            )
            raw_points = await self._scroll_points(q_filter, max(top_k * 3, 10))
            deduped = self._dedupe_and_sort_sec_points(raw_points)
            if deduped:
                by_form[form] = deduped[: max(top_k, 3)]
        return by_form

    @staticmethod
    def _flatten_sec_points_by_form(
        sec_points_by_form: Dict[str, List[Any]],
        requested_forms: List[str],
    ) -> List[Any]:
        flattened: List[Any] = []
        seen = set()
        ordered_forms = requested_forms or list(sec_points_by_form.keys())
        max_len = max((len(sec_points_by_form.get(form, []) or []) for form in ordered_forms), default=0)
        for idx in range(max_len):
            for form in ordered_forms:
                points = sec_points_by_form.get(form, []) or []
                if idx >= len(points):
                    continue
                point = points[idx]
                identity = str(getattr(point, "id", "") or "")
                if identity in seen:
                    continue
                seen.add(identity)
                flattened.append(point)
        return flattened

    @staticmethod
    def _date_in_window(raw_value: Any, start_date: date, end_date: date) -> bool:
        text = str(raw_value or "").strip()
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text[:10]).date()
        except Exception:
            return False
        return start_date <= parsed <= end_date

    def _sec_iso_window_filter(
        self,
        points: List[Any],
        metadata: Any,
        time_predicates: Optional[Dict["SourceTimeKey", "TimePredicate"]],
    ) -> List[Any]:
        """Fallback SEC time filtering using ISO filing dates from payloads."""
        sec_pred = (time_predicates or {}).get(SourceTimeKey.GOLD_SEC)
        if sec_pred is None:
            return points
        requested_forms = set(self._requested_sec_forms(metadata))
        filtered: List[Any] = []
        for point in points:
            payload = point.payload or {}
            if str(payload.get("source_type", "")).lower() != "sec":
                continue
            if requested_forms:
                payload_form = str(payload.get("form_type", "") or "").strip().upper()
                if payload_form and payload_form not in requested_forms:
                    continue
            if self._date_in_window(payload.get("filed_at"), sec_pred.start_date, sec_pred.end_date) or self._date_in_window(
                payload.get("transaction_date"), sec_pred.start_date, sec_pred.end_date
            ):
                filtered.append(point)
        return filtered

    async def retrieve_supplemental_news_async(
        self,
        original_query: str,
        transform_result: FullTransformationResult,
        top_k: int = 5,
        time_predicates: Optional[Dict["SourceTimeKey", "TimePredicate"]] = None,
        precomputed_vecs: Optional[Tuple[Any, Any]] = None,
    ) -> List[RetrievedChunk]:
        """Retrieve supplemental macro news across the full news corpus.

        This lane is intentionally decoupled from strict narrative evidence.
        It keeps the existing dense+sparse+rerank stack and drops ticker
        gating for legacy news docs, but preserves ontology topic filters and
        profile-expanded news language so "GLD narrative" searches for gold /
        bullion / safe-haven news rather than the word "narrative".

        Parameters
        ----------
        precomputed_vecs
            (dense_vec, sparse_vec) pre-computed upstream by MasterRetriever
            when both Gold and SupplementalNews lanes run for the same query.
            When provided, skips local embedding to halve CPU thread-pool load.
        """
        start_time = time.time()
        profile = getattr(transform_result.metadata, "news_semantic_profile", {}) or {}
        dense_search_query = dense_news_semantic_query(
            original_query,
            profile,
            semantic_context=getattr(transform_result.hyde, "hyde_paragraph", ""),
        )
        sparse_search_query = sparse_news_keyword_query(
            profile,
            fallback_query=getattr(transform_result.hyde, "rerank_query", original_query),
        )
        clean_search_query = expanded_news_rerank_query(
            getattr(transform_result.hyde, "rerank_query", original_query),
            profile,
        )
        qdrant_filter = None
        try:
            metadata = transform_result.metadata.model_copy(deep=True)
            metadata.source_types = [SourceType.NEWS]
            metadata.tickers = []
            supplemental_transform = transform_result.model_copy(deep=True)
            supplemental_transform.metadata = metadata

            if precomputed_vecs is not None and dense_search_query == getattr(transform_result.hyde, "hyde_paragraph", ""):
                dense_vec, sparse_vec = precomputed_vecs
            else:
                dense_query_task = asyncio.to_thread(
                    lambda: self.dense_model.embed_query(dense_search_query)
                )
                sparse_query_task = asyncio.to_thread(
                    lambda: list(self.sparse_model.query_embed(sparse_search_query))[0]
                )
                dense_vec, sparse_vec = await asyncio.gather(dense_query_task, sparse_query_task)

            qdrant_filter = self._build_smart_filter(
                metadata,
                ignore_time=False,
                ticker_mode="drop",
                time_predicates=time_predicates,
            )
            prefetch = [
                models.Prefetch(query=dense_vec, using="dense", limit=top_k * 3, filter=qdrant_filter),
                models.Prefetch(
                    query=models.SparseVector(indices=sparse_vec.indices.tolist(), values=sparse_vec.values.tolist()),
                    using="sparse",
                    limit=top_k * 3,
                    filter=qdrant_filter,
                ),
            ]

            search_results = await asyncio.to_thread(
                self.client.query_points,
                collection_name=self.collection_name,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=top_k * 2,
            )
            points = list(search_results.points or [])
            if not points:
                self._log_audit(
                    original_query,
                    clean_search_query,
                    qdrant_filter,
                    [],
                    time.time() - start_time,
                    False,
                    fallback_tier="supplemental_news_empty",
                    extra={
                        "dense_query_used": dense_search_query,
                        "sparse_query_used": sparse_search_query,
                        "sparse_query_terms": len(sparse_search_query.split()),
                    },
                )
                return []

            pairs = [[clean_search_query, p.payload.get("text", "")] for p in points]
            rerank_scores = await asyncio.to_thread(self.reranker.predict, pairs)
            for idx, p in enumerate(points):
                p.score = structured_news_relevance_score(
                    p.payload or {},
                    profile,
                    rerank_score=float(rerank_scores[idx]),
                )
            points.sort(key=lambda x: x.score, reverse=True)

            # Supplemental narrative news is a display-first lane for macro/news
            # reads. Keep rerank for ordering, but do not hard-drop low-scoring
            # news hits here; otherwise valid news can disappear before Analyst
            # ever sees it.
            valid_points = [
                p for p in points
                if str((p.payload or {}).get("source_type", "")).lower() == "news"
            ]
            formatted_results = self._format_results(valid_points[:top_k], False)
            self._log_audit(
                original_query,
                clean_search_query,
                qdrant_filter,
                formatted_results,
                time.time() - start_time,
                False,
                fallback_tier="supplemental_news",
                extra={
                    "dense_query_used": dense_search_query,
                    "sparse_query_used": sparse_search_query,
                    "sparse_query_terms": len(sparse_search_query.split()),
                },
            )
            return formatted_results
        except Exception as e:
            logger.exception(f"❌ Supplemental news retrieval failed: {e}")
            self._log_audit(
                original_query,
                clean_search_query,
                qdrant_filter,
                [],
                time.time() - start_time,
                False,
                fallback_tier="supplemental_news_error",
                error=str(e),
                extra={
                    "dense_query_used": locals().get("dense_search_query", ""),
                    "sparse_query_used": locals().get("sparse_search_query", ""),
                    "sparse_query_terms": len(str(locals().get("sparse_search_query", "") or "").split()),
                },
            )
            return []

    async def retrieve_async(
        self,
        original_query: str,
        transform_result: FullTransformationResult,
        top_k: int = 5,
        time_predicates: Optional[Dict["SourceTimeKey", "TimePredicate"]] = None,
        precomputed_vecs: Optional[Tuple[Any, Any]] = None,
    ) -> List[RetrievedChunk]:
        """Run the hybrid Gold retrieval cascade.

        Parameters
        ----------
        time_predicates
            Per-source `TimePredicate` dict compiled upstream by
            `MasterRetriever._compute_time_range`. When provided, the Gold
            filter builds a UNION epoch window across the sources named in
            `metadata.source_types` — so a mixed `["sec", "news"]` query
            gets news's tight event window AND sec's 3-day weekend-safe
            window combined into a single Range, while `["gpr"]` widens to
            the full month. When None, the legacy inline day-count window
            is used (preserves standalone test behaviour).
        precomputed_vecs
            (dense_vec, sparse_vec) pre-computed upstream by MasterRetriever
            when both Gold and SupplementalNews lanes run for the same query.
            When provided, skips local embedding to halve CPU thread-pool load.
        """
        start_time = time.time()
        clean_search_query = getattr(transform_result.hyde, "rerank_query", original_query)
        self._reset_last_sec_retrieval_contract()
        
        logger.info(f"🔍 Original Query: {original_query[:40]}...")
        logger.info(f"🎯 Denoised Rerank Query: {clean_search_query}")
        
        qdrant_filter = None
        try:
            requested_gold_sources = {
                str(getattr(s, "value", s) or "").strip().lower()
                for s in (getattr(transform_result.metadata, "source_types", None) or [])
            }
            _gold_source_vals: frozenset = requested_gold_sources & self._GOLD_SOURCE_TYPES
            _is_news_only = _gold_source_vals == {"news"}
            dense_query_text = transform_result.hyde.hyde_paragraph
            sparse_query_text = clean_search_query
            rerank_query_text = clean_search_query
            if _is_news_only:
                profile = getattr(transform_result.metadata, "news_semantic_profile", {}) or {}
                dense_query_text = dense_news_semantic_query(
                    original_query,
                    profile,
                    semantic_context=getattr(transform_result.hyde, "hyde_paragraph", ""),
                )
                sparse_query_text = sparse_news_keyword_query(
                    profile,
                    fallback_query=getattr(transform_result.hyde, "rerank_query", original_query),
                )
                rerank_query_text = expanded_news_rerank_query(
                    getattr(transform_result.hyde, "rerank_query", original_query),
                    profile,
                )
                logger.info(
                    "🧭 News retrieval queries | dense_terms=%s | sparse_terms=%s | rerank_terms=%s | sparse='%s'",
                    len(str(dense_query_text or "").split()),
                    len(str(sparse_query_text or "").split()),
                    len(str(rerank_query_text or "").split()),
                    sparse_query_text,
                )
            requested_sec_forms = self._requested_sec_forms(transform_result.metadata)
            if "sec" in requested_gold_sources and requested_sec_forms:
                sec_points_by_form = await self._retrieve_sec_payload_context_by_form(
                    transform_result.metadata,
                    top_k=top_k,
                    time_predicates=time_predicates,
                )
                if sec_points_by_form:
                    sec_payload_context_by_form = {
                        form: self._format_results(points, False)
                        for form, points in sec_points_by_form.items()
                    }
                    flattened_points = self._flatten_sec_points_by_form(sec_points_by_form, requested_sec_forms)
                    formatted_results = self._format_results(flattened_points[:top_k], False)
                    raw_hits_by_form = {
                        form: len(points or [])
                        for form, points in sec_points_by_form.items()
                    }
                    self._last_sec_retrieval_contract = {
                        "sec_payload_context_by_form": sec_payload_context_by_form,
                        "sec_ranked_context_by_form": dict(sec_payload_context_by_form),
                        "sec_raw_hits_by_form": raw_hits_by_form,
                        "sec_raw_hits_total": sum(raw_hits_by_form.values()),
                        "sec_rerank_kept_total": len(formatted_results),
                        "sec_rerank_dropped_total": max(0, sum(raw_hits_by_form.values()) - len(formatted_results)),
                        "sec_hit_stage": "payload_only",
                    }
                    self._log_audit(
                        original_query,
                        clean_search_query,
                        None,
                        formatted_results,
                        time.time() - start_time,
                        False,
                        fallback_tier="sec_payload_existence",
                        extra={
                            "sec_retrieval_mode": "payload_first",
                            "sec_payload_hits_by_form": raw_hits_by_form,
                        },
                    )
                    return formatted_results
                self._last_sec_retrieval_contract = {
                    "sec_payload_context_by_form": {},
                    "sec_ranked_context_by_form": {},
                    "sec_raw_hits_by_form": {},
                    "sec_raw_hits_total": 0,
                    "sec_rerank_kept_total": 0,
                    "sec_rerank_dropped_total": 0,
                    "sec_hit_stage": "no_payload_hits",
                }

            # Keep compatibility with LangChain embedding invocation API.
            # Precomputed vecs are accepted for all query types including news-only;
            # the caller (_fetch_gold_with_telemetry) precomputes outside wait_for.
            if precomputed_vecs is not None:
                dense_vec, sparse_vec = precomputed_vecs
            else:
                dense_query_task = asyncio.to_thread(
                    lambda: self.dense_model.embed_query(dense_query_text)
                )
                sparse_query_task = asyncio.to_thread(
                    lambda: list(self.sparse_model.query_embed(sparse_query_text))[0]
                )
                dense_vec, sparse_vec = await asyncio.gather(dense_query_task, sparse_query_task)

            async def _execute_search(q_filter):
                prefetch = [
                    models.Prefetch(query=dense_vec, using="dense", limit=top_k * 2, filter=q_filter),
                    models.Prefetch(
                        query=models.SparseVector(indices=sparse_vec.indices.tolist(), values=sparse_vec.values.tolist()),
                        using="sparse", limit=top_k * 2, filter=q_filter
                    )
                ]
                return await asyncio.to_thread(
                    self.client.query_points,
                    collection_name=self.collection_name,
                    prefetch=prefetch,
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=top_k
                )

            # ---------------------------------------------------------------
            # Retrieval cascade — two branches based on source domain.
            #
            # BRANCH A — news-only queries:
            #   News documents are indexed by `topic`, NOT by `ticker`.
            #   A hard-ticker Tier 1 is structurally guaranteed to return
            #   zero results for any commodity/ETF-backed news query
            #   (e.g. GLD, SLV, SPY) because no `ticker` payload field is
            #   present in the news collection.  Burning one Qdrant round-
            #   trip on a certain-empty filter wastes ~⅓ of the Gold timeout
            #   budget before the cascade can escalate.
            #
            #   Fix: skip hard-ticker entirely.  Start with soft-mode
            #   (topic OR ticker in should) + the exact requested time
            #   window from `time_predicates`.  If still empty, widen to
            #   180d with ticker dropped.  Two tiers instead of three;
            #   the tight window is preserved in Tier 1 so recent articles
            #   surface first.
            #
            # BRANCH B — SEC / GPR / mixed queries (original behaviour):
            #   Tier 1: strict ticker + time_predicates window.
            #   Tier 2: soft ticker + 180d.
            #   Tier 3: drop ticker + 180d.
            #   Tier 4: SEC ISO-date post-filter (SEC-only last resort).
            # ---------------------------------------------------------------
            fallback_tier = "strict"        # telemetry label
            fallback_used = False           # legacy boolean, kept for payload compat

            # Intersect the raw requested source types with the Gold-valid
            # set to get the same "source_vals" view that _build_smart_filter
            # uses internally — without sharing scope with that method.
            if _is_news_only:
                # --- Branch A: news-only fast path ---
                # Tier 1: soft-ticker + tight requested time window.
                qdrant_filter = self._build_smart_filter(
                    transform_result.metadata,
                    ignore_time=False,
                    ticker_mode="soft",
                    time_predicates=time_predicates,
                )
                search_results = await _execute_search(qdrant_filter)
                points = search_results.points
                fallback_tier = "news_soft_tight"

                # Tier 2: drop ticker entirely, widen to 180d.
                if not points:
                    logger.warning(
                        "⚠️ [News Tier1] soft-ticker+tight window returned 0. "
                        "Escalating to drop-ticker+180d."
                    )
                    qdrant_filter = self._build_smart_filter(
                        transform_result.metadata,
                        ignore_time=False,
                        fallback_days=180,
                        ticker_mode="drop",
                        time_predicates=time_predicates,
                    )
                    search_results = await _execute_search(qdrant_filter)
                    points = search_results.points
                    fallback_used = True
                    fallback_tier = "news_drop_ticker_180d"

            else:
                # --- Branch B: SEC / GPR / mixed queries (original 3-tier cascade) ---
                # Tier 1 — strict ticker, user's requested time window (aligned
                # per-source via `time_predicates` when provided).
                qdrant_filter = self._build_smart_filter(
                    transform_result.metadata,
                    ignore_time=False,
                    ticker_mode="hard",
                    time_predicates=time_predicates,
                )
                search_results = await _execute_search(qdrant_filter)
                points = search_results.points

                # Tier 2 — soft ticker (OR topic) + 180-day widened window.
                #   Targets the "ticker-not-tagged on news payloads" failure mode:
                #   old news docs without a `ticker` field still match via topic.
                #   Tier 2/3 deliberately OVERRIDE per-source predicates via
                #   `fallback_days` so the escalation stays deterministic.
                if not points:
                    logger.warning("⚠️ Tier1 (strict) returned 0. Escalating to Tier2: soft-ticker + 180d.")
                    qdrant_filter = self._build_smart_filter(
                        transform_result.metadata,
                        ignore_time=False,
                        fallback_days=180,
                        ticker_mode="soft",
                        time_predicates=time_predicates,
                    )
                    search_results = await _execute_search(qdrant_filter)
                    points = search_results.points
                    fallback_used = True
                    fallback_tier = "soft_ticker_180d"

                # Tier 3 — drop ticker entirely. Keeps source_type + topic + time.
                #   Intent: if neither ticker nor topic carried a match, surface
                #   broadly-relevant macro context rather than stay silent. The
                #   Analyst downstream can still honestly label these as weak.
                if not points:
                    logger.warning("⚠️ Tier2 (soft) returned 0. Escalating to Tier3: drop-ticker + 180d.")
                    qdrant_filter = self._build_smart_filter(
                        transform_result.metadata,
                        ignore_time=False,
                        fallback_days=180,
                        ticker_mode="drop",
                        time_predicates=time_predicates,
                    )
                    search_results = await _execute_search(qdrant_filter)
                    points = search_results.points
                    fallback_tier = "drop_ticker_180d"

                if not points and "sec" in requested_gold_sources:
                    logger.warning("⚠️ Tier3 returned 0 for SEC query. Escalating to SEC ISO-date fallback without Gold time filter.")
                    qdrant_filter = self._build_smart_filter(
                        transform_result.metadata,
                        ignore_time=True,
                        ticker_mode="hard",
                        time_predicates=time_predicates,
                    )
                    search_results = await _execute_search(qdrant_filter)
                    sec_points = self._sec_iso_window_filter(list(search_results.points or []), transform_result.metadata, time_predicates)
                    if sec_points:
                        points = sec_points
                        fallback_used = True
                        fallback_tier = "sec_iso_date_postfilter"

            if not points:
                audit_extra = None
                if _is_news_only:
                    audit_extra = {
                        "dense_query_used": dense_query_text,
                        "sparse_query_used": sparse_query_text,
                        "sparse_query_terms": len(str(sparse_query_text or "").split()),
                    }
                self._log_audit(
                    original_query, rerank_query_text, qdrant_filter, [],
                    time.time() - start_time, fallback_used, fallback_tier=fallback_tier,
                    extra=audit_extra,
                )
                return []

            # Apply precision reranking scores.
            pairs = [[rerank_query_text, p.payload.get("text", "")] for p in points]

            rerank_scores = await asyncio.to_thread(self.reranker.predict, pairs)

            for idx, p in enumerate(points):
                p.score = float(rerank_scores[idx])

            points.sort(key=lambda x: x.score, reverse=True)

            valid_points = [p for p in points if p.score > 0.01]
            top_k_results = valid_points[:top_k]

            formatted_results = self._format_results(top_k_results, fallback_used)
            audit_extra = None
            if _is_news_only:
                audit_extra = {
                    "dense_query_used": dense_query_text,
                    "sparse_query_used": sparse_query_text,
                    "sparse_query_terms": len(str(sparse_query_text or "").split()),
                }
            self._log_audit(
                original_query, rerank_query_text, qdrant_filter, formatted_results,
                time.time() - start_time, fallback_used, fallback_tier=fallback_tier,
                extra=audit_extra,
            )
            return formatted_results

        except Exception as e:
            # Use logger.exception to preserve stack trace and line numbers.
            logger.exception(f"❌ Async Retrieval Pipeline failed: {e}")
            self._log_audit(
                original_query, clean_search_query, qdrant_filter, [],
                time.time() - start_time, False, fallback_tier="error", error=str(e),
            )
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
            if record_date == "Unknown":
                for key in ("filed_at", "transaction_date", "publish_date"):
                    raw = str(payload.get(key, "") or "").strip()
                    if raw:
                        record_date = raw[:10]
                        break

            excluded_keys = {"text", "document_sparse_embedding"}
            refined_metadata = {k: v for k, v in payload.items() if k not in excluded_keys}
            
            # Add explicit tags for downstream router/agent consumers.
            refined_metadata["record_date"] = record_date 
            refined_metadata["is_fallback_180_days"] = fallback_used
            
            chunk = RetrievedChunk(
                content=payload.get("text", ""),
                source_type=payload.get("source_type", SourceType.NEWS),
                score=float(getattr(hit, "score", 1.0) or 1.0),
                metadata=refined_metadata,
                bronze_ref=bronze_anchor
            )
            retrieved_chunks.append(chunk)
            
        return retrieved_chunks

    def _log_audit(
        self,
        query: str,
        rerank_query: str,
        qdrant_filter: Optional[models.Filter],
        results: List[RetrievedChunk],
        latency: float,
        fallback_used: bool,
        fallback_tier: str = "strict",
        error: str = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        audit_payload = {
            "timestamp": datetime.now().isoformat(),
            "original_query": query,
            "rerank_query_used": rerank_query,  # Preserve exact retrieval text for observability.
            "filter_applied": qdrant_filter.model_dump() if qdrant_filter else None,
            "fallback_triggered": fallback_used,
            # New: exact tier that fired — "strict" | "soft_ticker_180d" |
            # "drop_ticker_180d" | "error". Use this field to diagnose which
            # relaxation was necessary, instead of inferring from the legacy
            # boolean which overwrites across retries.
            "fallback_tier": fallback_tier,
            "results_count": len(results),
            "top_k_scores": [round(r.score, 4) for r in results],
            "latency_sec": round(latency, 3),
            "status": "ERROR" if error else "SUCCESS",
            "error_msg": error
        }
        if extra:
            audit_payload.update(dict(extra))
        
        logger.info(f"[AUDIT_RAG_RETRIEVAL] {json.dumps(audit_payload)}")
        if callable(record_retrieval_fallback_kpi):
            try:
                record_retrieval_fallback_kpi(
                    fallback_tier=fallback_tier,
                    status=audit_payload["status"],
                    results_count=audit_payload["results_count"],
                    latency_sec=audit_payload["latency_sec"],
                )
            except Exception as kpi_e:
                logger.warning(f"Failed to write retrieval KPI summary: {kpi_e}")
        
        try:
            if callable(append_audit_jsonl):
                append_audit_jsonl(
                    module="retrieval",
                    payload=audit_payload,
                    filename="retriever_audit_trail.jsonl",
                    scoped_by_run=False,
                )
            else:
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
