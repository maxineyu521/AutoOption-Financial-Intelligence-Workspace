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
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama

from Scripts.core.few_shot_intent import INTENT_FEW_SHOT_EXAMPLES
from Scripts.core.intent_router_prompt_templates import ROUTER_SYSTEM_PROMPT
from Scripts.retrieval.qdrant_retriever import FinancialHybridRetriever
from Scripts.retrieval.query_transform import QueryTransformer
from Scripts.retrieval.schema import (
    FullTransformationResult,
    MetadataExtraction,
    QueryIntent,
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

# Public re-export surface — keeps `__all__` explicit for static analysers.
__all__ = ["MasterRetriever", "TIME_WINDOW_DAYS", "time_window_to_days"]

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
        self.router_llm = ChatOllama(
            model=os.getenv("OLLAMA_ROUTER_MODEL", "llama3:latest"),
            temperature=float(os.getenv("ROUTER_TEMPERATURE", 0.0)),
            num_predict=20,
        )

        # --- 3. Runtime knobs (env-tunable without code change) ---
        self.gold_timeout = float(os.getenv("GOLD_TIMEOUT", 5.0))
        self.silver_timeout = float(os.getenv("SILVER_TIMEOUT", 8.0))

        logger.info(
            f"🏛️ MasterRetriever ready | Gold_TO: {self.gold_timeout}s | "
            f"Silver_TO: {self.silver_timeout}s | HE_novel_cap: {_HE_NOVEL_TICKERS_CAP}"
        )

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
            logger.error(f"❌ [Router Error] {e}. Falling back to hybrid.")
            return QueryIntent(primary_route="hybrid_both")

    # ======================================================================
    # B. Engine wrappers (per-engine timeouts + exception isolation)
    # ======================================================================

    async def _fetch_gold_with_telemetry(
        self,
        query: str,
        transform_result: FullTransformationResult,
        top_k: int = 5,
    ) -> List[Any]:
        """Gold wrapper with independent timeout + exception isolation.

        Forwards the live `time_predicates` dict (compiled once upstream in
        `_compute_time_range`) so Qdrant can build source-specific time
        filters rather than rederiving the window on every call.
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
                ),
                timeout=self.gold_timeout,
            )
            logger.debug(f"📊 [Telemetry] Gold engine finished in {time.time() - t0:.3f}s (top_k={top_k})")
            return res
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ [Gold Timeout] Exceeded {self.gold_timeout}s. Returning empty context.")
            return []
        except Exception as e:
            logger.error(f"❌ [Gold Failure] {repr(e)}")
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
            return res or {"values": {}, "lineage_anchors": []}
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ [Silver Timeout] Exceeded {self.silver_timeout}s.")
            return {"values": {}, "lineage_anchors": [], "error": "Timeout"}
        except Exception as e:
            logger.error(f"❌ [Silver Failure] {repr(e)}")
            return {"values": {}, "lineage_anchors": [], "error": str(type(e).__name__)}

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

            "数据统计区间: 2025-10-20 ~ 2026-04-18 (window_days=180, default)"

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

    async def retrieve(self, user_query: str) -> Dict[str, Any]:
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
        transform_res = await self.transformer.transform_for_dual_rag(user_query, intent)
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

        # ==================================================================
        # 4. HyDE Entity Back-Injection (regex + whitelist)
        # ==================================================================
        hyde_info = self._extract_hyde_entities(
            hyde_paragraph=transform_res.hyde.hyde_paragraph if transform_res.hyde else "",
            seed_tickers=metadata.tickers,
        )
        novel_tickers: List[str] = hyde_info["novel_tickers"]

        # ==================================================================
        # 5. Build per-route task plan (all three may be None)
        # ==================================================================
        gold_task = None
        silver_task = None
        compensation_task = None

        if route == "hybrid_both":
            gold_task = self._fetch_gold_with_telemetry(user_query, transform_res, top_k=5)
            if metadata.tickers:
                silver_task = self._fetch_silver_with_telemetry(metadata)
            # Optional HE compensation — only fires when the HE introduced
            # NEW tickers the user didn't mention.
            if novel_tickers:
                compensation_task = self._run_silver_compensation(metadata, novel_tickers)

        elif route == "vector_only":
            gold_task = self._fetch_gold_with_telemetry(user_query, transform_res, top_k=5)
            # Primary Silver is OFF by design for vector_only; compensation is
            # the ONLY path into Parquet — realises "Entity Back-Injection".
            if novel_tickers:
                compensation_task = self._run_silver_compensation(metadata, novel_tickers)
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
            if novel_tickers:
                compensation_task = self._run_silver_compensation(metadata, novel_tickers)

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
        for name, t in (("gold", gold_task), ("silver", silver_task), ("compensation", compensation_task)):
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
            "silver_context": {
                "values": {},
                "lineage_anchors": [],
                "source_channel": "primary",
            },
            "hyde_anticipation": {
                "paragraph": hyde_info["paragraph"],
                "rerank_query": (transform_res.hyde.rerank_query if transform_res.hyde else ""),
                "raw_candidates": hyde_info["raw_candidates"],
                "whitelisted_tickers": hyde_info["whitelisted"],
                "novel_tickers": novel_tickers,
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
            elif slot == "silver":
                primary_silver = res or {"values": {}, "lineage_anchors": []}
                if primary_silver.get("error"):
                    partial_failure = True
                final_context["silver_context"] = {
                    "values": primary_silver.get("values", {}),
                    "lineage_anchors": primary_silver.get("lineage_anchors", []),
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
                    "source_channel": "silver_layer_via_hyde_expansion",
                    "trigger_entities": comp.get("trigger_entities", []),
                    "per_ticker_errors": comp.get("per_ticker_errors", {}),
                }

        if partial_failure:
            final_context["status"] = "partial_failure"

        # ==================================================================
        # 7.5  Macro → Silver anchor injection (Root Cause A fix, 2026-04-22)
        # ==================================================================
        # Parse the daily macro snapshot into silver values + MACRO_* anchors
        # so the Analyst's "Macro Regime Snapshot" section has citable
        # anchors. Without this, the Analyst cites VIX=18.25 from the prose
        # macro block, the Checker has no matching anchor/value, and every
        # revision fails — the 4/4 degraded-mode failure mode in
        # docs/test/2026-04-22/router_e2e_deep_analysis.md (Root Cause A).
        try:
            macro_md = self._read_macro_snapshot()
            anchor_for_macro = date.fromisoformat(time_range["anchor_date"]) \
                if time_range and time_range.get("anchor_date") else date.today()
            macro_patch = _build_macro_silver_patch(macro_md, anchor_for_macro)
            if macro_patch["values"]:
                sv = final_context["silver_context"]
                sv.setdefault("values", {}).update(macro_patch["values"])
                existing_anchors = sv.setdefault("lineage_anchors", []) or []
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
            "silver_context": {
                "values": {},
                "lineage_anchors": [],
                "source_channel": "primary",
                "error": reason,
            },
            "hyde_anticipation": None,
            "status": "partial_failure",
            "latency_stats": {"total_e2e": f"{time.time() - start_ts:.3f}s"},
        }
