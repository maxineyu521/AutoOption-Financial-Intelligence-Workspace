"""
Scripts/agents/analyst.py

Analyst Agent — institutional-grade draft author for the LangGraph pipeline.

Design pillars (aligned with the architect brief):
1. **Macro-Chain reasoning**: macro_context is injected into the system prompt
   using the Macro → Meso → Micro framework so the LLM reasons about
   transmission channels (e.g. GPR spike -> safe-haven flow -> GLD call demand)
   rather than isolated facts.
2. **IV Regime awareness** (Silver upgrade from "recommendation" to "arbitrage
   discovery"): before drafting, a deterministic helper classifies the current
   implied-vol regime from silver_context. The LLM is told the regime so its
   strategy recommendation is compatible (sell premium in high-IV, buy
   protection in low-IV).
3. **Strict data lineage**: every numeric / factual claim must carry either
   `[Silver: <lineage_anchor>]` or `[Gold: <bronze_ref>]`. Downstream the
   CheckerAgent enforces this.
4. **Revision-aware**: on every re-entry the agent sees the full append-only
   feedback log from state["critic_feedback"] (both Checker + Critic senders)
   and MUST address every unresolved Fatal error.
5. **Temporal decay hint**: the LLM is explicitly told to weight newer
   gold_context entries (fresh record_date) more heavily than stale ones.
6. **Graceful degradation**: LLM failures surface as a stub draft that
   explicitly says "INSUFFICIENT DATA" rather than a hallucinated report.
"""

from __future__ import annotations

import asyncio
import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import requests
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from Scripts.agents.prompts import render_revision_block
from Scripts.core.financial_config import get_analyst_system_prompt

logger = logging.getLogger(__name__)


def _normalize_openai_base_url(raw_base_url: Optional[str], ollama_host: Optional[str]) -> str:
    """
    Normalize Ollama OpenAI-compatible base URL to end with `/v1`.

    Accepts:
      - http://localhost:11434
      - http://localhost:11434/
      - http://localhost:11434/v1
      - http://localhost:11434/v1/
    Returns:
      - http://localhost:11434/v1
    """
    candidate = (raw_base_url or "").strip()
    if not candidate:
        candidate = (ollama_host or "").strip()
    if not candidate:
        candidate = "http://localhost:11434"

    base = candidate.rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _is_ollama_runner_500(exc: Exception) -> bool:
    """Return True for known Ollama terminal runner failures."""
    msg = str(exc).lower()
    return ("status code: 500" in msg) or ("runner process has terminated" in msg)


def _collect_valid_citation_ids(silver_ctx: Dict[str, Any], gold_ctx: List[Any]) -> Dict[str, List[str]]:
    silver_ids: List[str] = []
    gold_ids: List[str] = []

    for a in (silver_ctx.get("lineage_anchors", []) if isinstance(silver_ctx, dict) else []) or []:
        s = str(a).strip()
        if s:
            silver_ids.append(s)

    for c in gold_ctx or []:
        br = getattr(c, "bronze_ref", None) or (c.get("bronze_ref") if isinstance(c, dict) else None)
        if br:
            gold_ids.append(str(br).strip())

    # Preserve order while deduping
    silver_ids = list(dict.fromkeys(silver_ids))
    gold_ids = list(dict.fromkeys(gold_ids))
    return {"silver_ids": silver_ids, "gold_ids": gold_ids}


class AnalystResult:
    """Typed envelope for the analyst node's multi-field return.

    Using a tiny structured return instead of a raw string lets the router
    plumb `iv_regime_pinned` into AgentState without awkwardly breaking
    backwards-compat with the Markdown-only contract (a plain string is
    still accessible via `.draft`).
    """

    __slots__ = ("draft", "iv_regime", "used_fallback", "model_used")

    def __init__(
        self,
        draft: str,
        iv_regime: Dict[str, Any],
        used_fallback: bool = False,
        model_used: str = "",
    ):
        self.draft = draft
        self.iv_regime = iv_regime
        self.used_fallback = used_fallback
        self.model_used = model_used


# ==========================================
# IV Regime Classifier (deterministic, no LLM)
# ==========================================
# Percentile-based thresholds (cross-asset stable) from Silver iv-rank.
_IV_RANK_HIGH_PCT_DEFAULT = float(os.getenv("IV_RANK_HIGH_PCT", "70"))
_IV_RANK_LOW_PCT_DEFAULT = float(os.getenv("IV_RANK_LOW_PCT", "30"))


def _infer_iv_regime(
    silver_context: Dict[str, Any],
    high_pct: float = _IV_RANK_HIGH_PCT_DEFAULT,
    low_pct: float = _IV_RANK_LOW_PCT_DEFAULT,
) -> Dict[str, Any]:
    """Classify the current IV regime from silver_context.values.

    Inspects the `latest_atm_iv` + `latest_atm_iv_rank_pct` keys written by
    `Scripts/retrieval/sql_tools._handle_options_analysis`. Also surfaces
    Put/Call Ratio when present because PCR drives the regime narrative.

    Returns a stable dict the LLM prompt can consume verbatim:
        {
          "iv_regime": "HIGH" | "LOW" | "NORMAL" | "UNKNOWN",
          "atm_iv": float | None,
          "iv_rank_pct": float | None,
          "pcr_volume": float | None,
          "pcr_status": str | None,
          "thresholds": {"high_pct": ..., "low_pct": ...}
        }

    Classification rule:
      - HIGH   when iv_rank_pct >= high_pct
      - LOW    when iv_rank_pct <= low_pct
      - NORMAL otherwise
      - UNKNOWN if iv_rank_pct is absent/non-numeric
    """
    values = (silver_context or {}).get("values", {}) if isinstance(silver_context, dict) else {}
    atm_iv = values.get("latest_atm_iv")
    iv_rank_pct = values.get("latest_atm_iv_rank_pct")
    pcr_vol = values.get("pcr_volume")
    pcr_status = values.get("pcr_status")

    if not isinstance(iv_rank_pct, (int, float)):
        regime: Literal["HIGH", "LOW", "NORMAL", "UNKNOWN"] = "UNKNOWN"
    elif iv_rank_pct >= high_pct:
        regime = "HIGH"
    elif iv_rank_pct <= low_pct:
        regime = "LOW"
    else:
        regime = "NORMAL"

    return {
        "iv_regime": regime,
        "atm_iv": atm_iv,
        "iv_rank_pct": iv_rank_pct,
        "pcr_volume": pcr_vol,
        "pcr_status": pcr_status,
        "thresholds": {"high_pct": high_pct, "low_pct": low_pct},
    }


# ==========================================
# Context formatters (LLM-friendly, stable layout)
# ==========================================

def _format_silver(silver_context: Dict[str, Any]) -> str:
    """Flatten the Silver Layer payload into a deterministic table.

    The layout intentionally places every quantitative value on its own
    pipe-separated line so the Checker's regex-based number extraction is
    robust to whitespace.
    """
    if not silver_context:
        return "(no silver context)"
    values = silver_context.get("values") or {}
    lineage = silver_context.get("lineage_anchors") or []
    if not values:
        return "(silver returned no values — INSUFFICIENT DATA)"

    lines = ["| metric | value |", "|---|---|"]
    for k, v in values.items():
        lines.append(f"| {k} | {v} |")

    if lineage:
        lines.append("")
        lines.append(
            "LINEAGE ANCHORS — copy the EXACT anchor string into [Silver: <anchor>] "
            "after every numeric claim that uses the corresponding metric value:"
        )
        # Build a value→anchor hint so the LLM doesn't have to guess which
        # anchor covers which number.  We group by anchor prefix so related
        # metrics (MACRO_VIX_* covers both VIX_value and VIX_change_pct) are
        # shown together rather than as a flat list.
        anchor_covered: Dict[str, List[str]] = {}
        for lid in lineage:
            # Derive the metric-key prefix: MACRO_VIX_2026-04-23 → VIX
            parts = lid.split("_")
            if lid.startswith("MACRO_") and len(parts) >= 3:
                code = parts[1]
                prefix_keys = [k2 for k2 in values if k2.startswith(code)]
            elif lid.startswith("GPR_"):
                prefix_keys = [k2 for k2 in values if k2.startswith("gpr")]
            else:
                # For options / px / iv anchors, show all keys whose prefix
                # appears in the anchor name (best-effort heuristic).
                anchor_stem = lid.split("_")[0] if "_" in lid else lid
                prefix_keys = [k2 for k2 in values if anchor_stem.lower() in k2.lower()]
            anchor_covered[lid] = prefix_keys or ["(see table above)"]

        for lid, covered_keys in anchor_covered.items():
            val_hints = ", ".join(
                f"{k2}={values[k2]}" for k2 in covered_keys[:3] if k2 in values
            )
            if len(covered_keys) > 3:
                val_hints += f", +{len(covered_keys) - 3} more"
            lines.append(f"  [Silver: {lid}]  covers → {val_hints}")
    return "\n".join(lines)


def _format_gold(gold_context: List[Any]) -> str:
    """Serialise RetrievedChunk objects (or plain dicts, as a fallback) into
    a numbered, citation-ready list.

    Each line exposes `bronze_ref` as the citation key so the LLM can emit
    `[Gold: <bronze_ref>]` verbatim.
    """
    if not gold_context:
        return "(no gold context)"

    lines = []
    for i, chunk in enumerate(gold_context, 1):
        # RetrievedChunk is a Pydantic model; fall back to dict for robustness.
        content = getattr(chunk, "content", None) or (chunk.get("content", "") if isinstance(chunk, dict) else "")
        bronze = getattr(chunk, "bronze_ref", None) or (chunk.get("bronze_ref", "UNKNOWN") if isinstance(chunk, dict) else "UNKNOWN")
        src = getattr(chunk, "source_type", None) or (chunk.get("source_type", "") if isinstance(chunk, dict) else "")
        meta = getattr(chunk, "metadata", None) or (chunk.get("metadata", {}) if isinstance(chunk, dict) else {})
        record_date = meta.get("record_date", "unknown") if isinstance(meta, dict) else "unknown"
        ticker = meta.get("ticker", "") if isinstance(meta, dict) else ""

        # Truncate long content to keep the prompt lean — 600 chars preserves
        # roughly one paragraph of SEC or news text, the reranker already
        # surfaced the most relevant portion.
        snippet = (content or "").replace("\n", " ").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"

        lines.append(
            f"[{i}] source={src} | ticker={ticker} | record_date={record_date} | "
            f"bronze_ref={bronze}\n    {snippet}"
        )
    return "\n".join(lines)


def _load_macro_context(state_value: Optional[str]) -> str:
    """Prefer the macro context already in state, else fall back to the
    on-disk daily snapshot. A hard-coded fallback keeps the pipeline alive
    even when the data pipeline has not yet produced a macro snapshot."""
    if state_value and state_value.strip():
        return state_value

    try:
        project_root = Path(__file__).resolve().parents[2]
        fp = project_root / "Data" / "Agent_Context" / "latest_macro_context.md"
        if fp.exists():
            return fp.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"AnalystAgent: macro context fallback failed: {e}")

    return "Market conditions are currently stable. (fallback — no snapshot available)"


# ==========================================
# AnalystAgent class (router-compatible)
# ==========================================

class AnalystAgent:
    """LangGraph-compatible analyst node implementation.

    Public contract:
        agent = AnalystAgent()
        result = await agent.generate_report(state)   # AnalystResult
        draft_markdown = result.draft
        iv_regime_dict = result.iv_regime

    The router in Scripts/agents/router.py wraps this and writes
    `result.draft` into ``state["draft_report"]`` and (on the first pass
    only) ``result.iv_regime`` into ``state["iv_regime_pinned"]``.
    """

    def __init__(self):
        # Primary engine: gpt-4o-mini via OpenAI API.
        # Backup engine: options-expert-v1:latest via local Ollama (OpenAI-compatible).
        self.temperature = float(os.getenv("ANALYST_TEMPERATURE", "0.0"))
        self.max_tokens = int(os.getenv("ANALYST_MAX_TOKENS", "180"))
        self.timeout_s = float(os.getenv("ANALYST_TIMEOUT_SECONDS", "120"))
        self.max_retries = int(os.getenv("ANALYST_MAX_RETRIES", "2"))
        self.retry_backoff_s = float(os.getenv("ANALYST_RETRY_BACKOFF_SECONDS", "1.5"))
        self.healthcheck_enabled = os.getenv("ANALYST_HEALTHCHECK_ENABLED", "1") == "1"
        self.healthcheck_timeout_s = float(os.getenv("ANALYST_HEALTHCHECK_TIMEOUT_SECONDS", "4"))
        self.enable_model_fallback = os.getenv("ANALYST_ENABLE_MODEL_FALLBACK", "1") == "1"
        self.fast_fail_on_500 = os.getenv("ANALYST_FAST_FAIL_ON_OLLAMA_500", "1") == "1"
        self.fallback_switch_sla_s = float(os.getenv("ANALYST_FALLBACK_SWITCH_SLA_SECONDS", "1.0"))

        # RAG context window controls (prompt-size guardrails).
        self.max_macro_chars = int(os.getenv("ANALYST_MAX_MACRO_CHARS", "1600"))
        self.max_silver_lines = int(os.getenv("ANALYST_MAX_SILVER_LINES", "70"))
        self.max_gold_chars = int(os.getenv("ANALYST_MAX_GOLD_CHARS", "3200"))
        self.max_payload_chars = int(os.getenv("ANALYST_MAX_PAYLOAD_CHARS", "9000"))

        # Primary: OpenAI gpt-4o-mini
        self.model_name = os.getenv("ANALYST_PRIMARY_MODEL", "gpt-4o-mini")
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        self.openai_fallback_timeout_s = float(os.getenv("ANALYST_OPENAI_FALLBACK_TIMEOUT_SECONDS", "45"))

        # Backup: Ollama options-expert-v1:latest
        self.ollama_backup_model = os.getenv("OLLAMA_ANALYST_MODEL", "options-expert-v1:latest")
        self.base_url = _normalize_openai_base_url(
            os.getenv("OLLAMA_OPENAI_BASE_URL"),
            os.getenv("OLLAMA_HOST"),
        )
        self.api_key = os.getenv("OLLAMA_OPENAI_API_KEY", "ollama")

        self.llm = self._build_openai_fallback_llm(self.model_name)
        self.fallback_llm = None
        self.primary_healthy = True
        self.fallback_healthy = True

        self.ollama_fallback_enabled = (
            self.enable_model_fallback
            and os.getenv("ANALYST_OLLAMA_FALLBACK_ENABLED", "1") == "1"
        )
        if self.ollama_fallback_enabled:
            self.fallback_llm = self._build_ollama_llm(self.ollama_backup_model)

        if self.healthcheck_enabled and self.fallback_llm is not None:
            ollama_ok = self._healthcheck_model(self.ollama_backup_model)
            if not ollama_ok:
                self.fallback_healthy = False
                logger.info("AnalystAgent: Ollama backup unavailable at startup; OpenAI primary only.")

        self.system_prompt = get_analyst_system_prompt()
        self.system_prompt = (
            f"{self.system_prompt}\n\n"
            "=== CITATION ENFORCEMENT (RUNTIME HARD RULE) ===\n"
            "Use ONLY IDs explicitly listed in the payload under VALID_SILVER_IDS / VALID_GOLD_IDS.\n"
            "Never invent, truncate, or normalize IDs. Never use placeholders like GOLD_CONTEXT,\n"
            "SILVER_CONTEXT, MACRO_CONTEXT. If no valid ID exists, write INSUFFICIENT DATA."
        )
        self.prompt_chain = ChatPromptTemplate.from_messages([
            ("system", self.system_prompt),
            ("human", "Here is the context and query:\n\n{payload}"),
        ])
        logger.info(
            "AnalystAgent: primary=%s (openai) | backup=%s (ollama) | retries=%s | fast_fail_500=%s",
            self.model_name,
            self.ollama_backup_model if self.fallback_llm is not None else "disabled",
            self.max_retries,
            self.fast_fail_on_500,
        )

    def _build_ollama_llm(self, model_name: str) -> ChatOpenAI:
        """Build a ChatOpenAI client pointed at Ollama's OpenAI-compatible API."""
        return ChatOpenAI(
            model=model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout_s,
        )

    def _build_openai_fallback_llm(self, model_name: str) -> ChatOpenAI:
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": self.openai_api_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.openai_fallback_timeout_s,
        }
        if self.openai_base_url:
            kwargs["base_url"] = self.openai_base_url
        return ChatOpenAI(**kwargs)

    def _models_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"

    def _healthcheck_model(self, model_name: str) -> bool:
        """Best-effort Ollama model healthcheck via OpenAI-compatible /models."""
        try:
            resp = requests.get(
                self._models_endpoint(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.healthcheck_timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
            model_ids = {str(item.get("id", "")) for item in payload.get("data", []) if isinstance(item, dict)}
            ready = model_name in model_ids
            if not ready:
                logger.warning(
                    "AnalystAgent: healthcheck ok but model missing | model=%s | available=%s",
                    model_name,
                    sorted([m for m in model_ids if m])[:10],
                )
            return ready
        except Exception as exc:
            logger.warning("AnalystAgent: healthcheck failed for model=%s | err=%s", model_name, exc)
            return False

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        s = (text or "").strip()
        if len(s) <= max_chars:
            return s
        return s[:max_chars] + "\n...(truncated)..."

    def _build_payload_text(
        self,
        user_query: str,
        macro_ctx: str,
        iv_regime_block: str,
        silver_ctx: Dict[str, Any],
        gold_ctx: List[Any],
        revision_block: str,
    ) -> str:
        ids = _collect_valid_citation_ids(silver_ctx, gold_ctx)
        valid_silver_ids = ", ".join(ids["silver_ids"][:80]) if ids["silver_ids"] else "(none)"
        valid_gold_ids = ", ".join(ids["gold_ids"][:80]) if ids["gold_ids"] else "(none)"
        macro = self._clip_text(macro_ctx, self.max_macro_chars)
        silver_full = _format_silver(silver_ctx)
        silver_lines = silver_full.splitlines()
        if len(silver_lines) > self.max_silver_lines:
            silver = "\n".join(silver_lines[: self.max_silver_lines]) + "\n...(silver truncated)..."
        else:
            silver = silver_full

        gold = self._clip_text(_format_gold(gold_ctx), self.max_gold_chars)
        payload = (
            "=== USER QUERY ===\n"
            f"{user_query}\n\n"
            "=== MACRO ENVIRONMENT ===\n"
            f"{macro}\n\n"
            "=== IV REGIME (deterministic) ===\n"
            f"{iv_regime_block}\n\n"
            "=== SILVER CONTEXT ===\n"
            f"{silver}\n\n"
            "=== GOLD CONTEXT ===\n"
            f"{gold}\n\n"
            "=== REVISION FEEDBACK ===\n"
            f"{revision_block or '(none)'}\n\n"
            "=== VALID CITE IDS (STRICT) ===\n"
            f"VALID_SILVER_IDS: {valid_silver_ids}\n"
            f"VALID_GOLD_IDS: {valid_gold_ids}\n\n"
            "=== OUTPUT POLICY ===\n"
            "Use only provided evidence. If missing data, explicitly say INSUFFICIENT DATA. "
            "Never output placeholder citation IDs."
        )
        return self._clip_text(payload, self.max_payload_chars)

    async def _invoke_with_retry(self, llm: ChatOpenAI, payload: Dict[str, Any], model_name: str):
        last_err: Optional[Exception] = None
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await (self.prompt_chain | llm).ainvoke(payload)
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "AnalystAgent: invoke failed | model=%s | attempt=%s/%s | err=%s",
                    model_name,
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                if self.fast_fail_on_500 and _is_ollama_runner_500(exc):
                    logger.warning(
                        "AnalystAgent: detected Ollama 500 runner error; trigger fallback immediately "
                        "(target_switch_sla=%.2fs).",
                        self.fallback_switch_sla_s,
                    )
                    raise exc
                if attempt < attempts:
                    await asyncio.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))
        if last_err is not None:
            raise last_err
        raise RuntimeError("AnalystAgent retry loop failed without explicit exception.")

    async def generate_report(self, state: Dict[str, Any]) -> AnalystResult:
        """Run one analyst pass and return the draft Markdown + pinned IV regime.

        Returns
        -------
        AnalystResult
            ``.draft`` is the Markdown (existing contract), ``.iv_regime``
            is the dict the router writes into ``state["iv_regime_pinned"]``
            so subsequent revisions reuse the same regime instead of
            recomputing it from a Silver table that may have mutated under
            the Checker's rescue refresh.

        Note: `revision_count` increment is owned by the router's analyst_node,
        not by this method — keeps the SoC clean and lets the route functions
        rely on a single source of truth for "how many drafts have been tried".
        """
        user_query = state.get("original_query", "")
        macro_ctx = _load_macro_context(state.get("macro_context"))
        silver_ctx = state.get("silver_context", {}) or {}
        gold_ctx = state.get("gold_context", []) or []
        feedback_log = state.get("critic_feedback", []) or []
        revision_n = state.get("revision_count", 0)

        # Deterministic pre-compute: IV regime.
        # IV Regime Pinning (2026-04-22, docs/test/2026-04-22/
        # router_e2e_deep_analysis.md §6.5):
        #   The regime MUST be stable across revisions otherwise the
        #   strategy recommendation flips direction mid-pipeline (Test 4
        #   flipped NORMAL → LOW → NORMAL over three revisions because the
        #   Checker's rescue refreshed `latest_atm_iv` each time). We pin
        #   the regime on the very first pass and reuse it verbatim — the
        #   router writes it back onto AgentState.iv_regime_pinned.
        pinned = state.get("iv_regime_pinned")
        if isinstance(pinned, dict) and pinned.get("iv_regime"):
            iv_regime = pinned
            logger.info(
                f"AnalystAgent: reusing pinned IV regime={iv_regime['iv_regime']} "
                f"| atm_iv={iv_regime.get('atm_iv')} (revision={revision_n})"
            )
        else:
            iv_regime = _infer_iv_regime(silver_ctx)
            logger.info(
                f"AnalystAgent: pinning IV regime={iv_regime['iv_regime']} "
                f"| atm_iv={iv_regime['atm_iv']} (first pass; will be reused on revisions)"
            )

        iv_regime_block = (
            f"iv_regime={iv_regime['iv_regime']} | atm_iv={iv_regime['atm_iv']} "
            f"| iv_rank_pct={iv_regime.get('iv_rank_pct')} "
            f"| pcr_volume={iv_regime['pcr_volume']} | pcr_status={iv_regime['pcr_status']} "
            f"| thresholds={iv_regime['thresholds']}"
        )

        # Revision-aware prompting — empty on the first pass.
        revision_block = render_revision_block(feedback_log)

        try:
            logger.info(
                f"AnalystAgent: invoking LLM | revision={revision_n} | "
                f"iv_regime={iv_regime['iv_regime']} | gold_n={len(gold_ctx)} | "
                f"silver_values_n={len(silver_ctx.get('values', {}))}"
            )
            payload = {
                "payload": self._build_payload_text(
                    user_query=user_query,
                    macro_ctx=macro_ctx,
                    iv_regime_block=iv_regime_block,
                    silver_ctx=silver_ctx,
                    gold_ctx=gold_ctx,
                    revision_block=revision_block,
                )
            }
            response = await self._invoke_with_retry(self.llm, payload, self.model_name)

            draft = getattr(response, "content", str(response)).strip()
            if not draft:
                raise RuntimeError("LLM returned empty draft")
            return AnalystResult(
                draft=draft,
                iv_regime=iv_regime,
                used_fallback=False,
                model_used=self.model_name,
            )

        except Exception as e:
            # Primary (OpenAI) failed -> switch to Ollama backup.
            if self.enable_model_fallback and self.fallback_llm is not None and self.fallback_healthy:
                try:
                    fallback_started = asyncio.get_running_loop().time()
                    logger.warning(
                        "AnalystAgent: primary OpenAI path failed; switching to Ollama backup model=%s",
                        self.ollama_backup_model,
                    )
                    payload = {
                        "payload": self._build_payload_text(
                            user_query=user_query,
                            macro_ctx=macro_ctx,
                            iv_regime_block=iv_regime_block,
                            silver_ctx=silver_ctx,
                            gold_ctx=gold_ctx,
                            revision_block=revision_block,
                        )
                    }
                    response = await self._invoke_with_retry(self.fallback_llm, payload, self.ollama_backup_model)
                    draft = getattr(response, "content", str(response)).strip()
                    if draft:
                        switch_elapsed = asyncio.get_running_loop().time() - fallback_started
                        if switch_elapsed > self.fallback_switch_sla_s:
                            logger.warning(
                                "AnalystAgent: backup switch exceeded SLA %.2fs (actual=%.2fs)",
                                self.fallback_switch_sla_s,
                                switch_elapsed,
                            )
                        return AnalystResult(
                            draft=draft,
                            iv_regime=iv_regime,
                            used_fallback=True,
                            model_used=self.ollama_backup_model,
                        )
                except Exception as fallback_err:
                    logger.exception("AnalystAgent: Ollama backup model failed: %s", fallback_err)

            logger.exception(f"AnalystAgent: LLM call failed: {e}")
            fallback_draft = (
                "## Analyst Draft — DEGRADED MODE\n"
                "INSUFFICIENT DATA — the Analyst LLM call failed. No recommendation "
                "will be produced until the model is available again.\n\n"
                f"Diagnostic: {type(e).__name__}"
            )
            return AnalystResult(
                draft=fallback_draft,
                iv_regime=iv_regime,
                used_fallback=False,
                model_used="degraded",
            )
