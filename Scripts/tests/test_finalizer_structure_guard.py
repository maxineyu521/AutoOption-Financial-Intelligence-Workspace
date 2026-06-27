"""
Drift-guard regression tests for the Finalizer structure visibility enforcement.

These tests verify the Phase 2 Workstream C fix:
- no_structure mode cannot render trade ideas
- illustrative_structure mode labels examples non-live
- recommended_structure mode passes through unchanged
- true_risk_text is sourced for the Risks section when provided

No LLM required — all tests use deterministic Pydantic models.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Scripts.agents.finalizer import (
    FinalReport,
    FinalizerRenderGuard,
    SourceCitation,
    TradeIdea,
    _enforce_structure_visibility_mode,
    _strip_concrete_structure_text,
)


# ==========================================
# Fixtures
# ==========================================

def _sample_trade_idea(**overrides: Any) -> TradeIdea:
    defaults = {
        "ticker": "SPY",
        "asset_class": "Index ETF",
        "market_outlook": "Bearish",
        "option_strategy": "Long Put",
        "strike_details": "Buy 540P",
        "expiration_date": "30-45 DTE",
        "rationale": "Elevated VIX with weak macro backdrop supports protective put positioning.",
        "catalysts": ["CPI release next week"],
        "risk_profile": "Medium",
        "supporting_evidence": [],
    }
    defaults.update(overrides)
    return TradeIdea(**defaults)


def _sample_report(**overrides: Any) -> FinalReport:
    defaults = {
        "macro_summary": "VIX elevated, DXY stable, GPR moderate.",
        "trade_ideas": [_sample_trade_idea()],
        "key_risks_and_hedges": ["VIX could spike further on FOMC."],
        "confidence_score": 0.72,
        "conversation_reply": (
            "For SPY, the current setup favors a Long Put around 540P "
            "with 30-45 DTE. Main risk: VIX could spike further."
        ),
    }
    defaults.update(overrides)
    return FinalReport(**defaults)


def _state_with_constraints(
    structure_visibility_mode: str = "no_structure",
    recommendation_mode: str = "informational_only",
    allow_illustrative_structure: bool = False,
    illustrative_structure_hint: str = "",
    illustrative_structure_text: str = "",
    true_risk_text: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    constraints = {
        "structure_visibility_mode": structure_visibility_mode,
        "actionability_mode": recommendation_mode,
        "forbid_actionable_recommendation": recommendation_mode != "actionable_options",
        "allow_illustrative_structure": allow_illustrative_structure,
        "illustrative_structure_hint": illustrative_structure_hint,
        "illustrative_structure_text": illustrative_structure_text,
        "true_risk_text": true_risk_text,
        **extra,
    }
    return {
        "finalizer_input_card": {
            "revision_constraints": constraints,
            "recommendation_mode": recommendation_mode,
        },
        "revision_constraints": constraints,
        "original_query": "Test query",
    }


# ==========================================
# Tests: no_structure enforcement
# ==========================================

class TestNoStructureEnforcement:
    """Verify that no_structure mode strips all trade structure from the output."""

    def test_strips_trade_ideas(self):
        report = _sample_report()
        assert len(report.trade_ideas) == 1, "Precondition: report has trade ideas"

        state = _state_with_constraints(structure_visibility_mode="no_structure")
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="informational_only",
            captured_trade_ideas=list(report.trade_ideas),
        )

        assert report.trade_ideas == [], "no_structure must strip all trade ideas"

    def test_clears_illustrative_text(self):
        report = _sample_report()
        report._render_illustrative_structure_text = "Illustrative only: Long Put on SPY"

        state = _state_with_constraints(structure_visibility_mode="no_structure")
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="informational_only",
            captured_trade_ideas=[],
        )

        assert report._render_illustrative_structure_text == "", \
            "no_structure must clear illustrative structure text"

    def test_strips_concrete_structure_from_conversation_reply(self):
        report = _sample_report(
            conversation_reply="For SPY, consider a Long Put around strike 540P with 30-DTE expiry."
        )

        state = _state_with_constraints(structure_visibility_mode="no_structure")
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="informational_only",
            captured_trade_ideas=[],
        )

        lowered = report.conversation_reply.lower()
        assert "long put" not in lowered, "no_structure must strip 'Long Put' from reply"
        assert "540p" not in lowered, "no_structure must strip strike references from reply"

    def test_no_structure_with_directional_watchlist(self):
        """Even in directional_watchlist mode, no_structure must strip everything."""
        report = _sample_report()
        state = _state_with_constraints(
            structure_visibility_mode="no_structure",
            recommendation_mode="directional_watchlist",
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="directional_watchlist",
            captured_trade_ideas=list(report.trade_ideas),
        )

        assert report.trade_ideas == []
        assert report._render_illustrative_structure_text == ""


# ==========================================
# Tests: illustrative_structure enforcement
# ==========================================

class TestIllustrativeStructureEnforcement:
    """Verify that illustrative_structure mode preserves framing but labels non-live."""

    def test_clears_trade_ideas_but_preserves_illustrative_text(self):
        report = _sample_report()
        captured = list(report.trade_ideas)

        state = _state_with_constraints(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="directional_watchlist",
            allow_illustrative_structure=True,
            illustrative_structure_hint="Long Put on SPY around 540P",
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="directional_watchlist",
            captured_trade_ideas=captured,
        )

        assert report.trade_ideas == [], \
            "illustrative_structure must clear trade_ideas (structure lives in text only)"
        assert "illustrative only" in report._render_illustrative_structure_text.lower(), \
            "illustrative_structure must set illustrative framing text"

    def test_labels_non_live(self):
        report = _sample_report()
        captured = list(report.trade_ideas)

        state = _state_with_constraints(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="directional_watchlist",
            allow_illustrative_structure=True,
            illustrative_structure_hint="Long Put on SPY",
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="directional_watchlist",
            captured_trade_ideas=captured,
        )

        text = report._render_illustrative_structure_text.lower()
        assert "not a live recommendation" in text, \
            "illustrative_structure must contain 'not a live recommendation'"

    def test_builds_hint_from_captured_trade_ideas(self):
        """When upstream hint is empty, the guard builds from captured trade ideas."""
        report = _sample_report()
        captured = list(report.trade_ideas)

        state = _state_with_constraints(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="informational_only",
            allow_illustrative_structure=False,  # even when allow is False
            illustrative_structure_hint="",  # no hint from critic
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="informational_only",
            captured_trade_ideas=captured,
        )

        text = report._render_illustrative_structure_text
        assert "illustrative only" in text.lower(), \
            "Must build illustrative text even without upstream hint"
        assert "SPY" in text, "Must include ticker from captured trade ideas"
        assert "Long Put" in text, "Must include strategy from captured trade ideas"

    def test_uses_prebuilt_illustrative_text(self):
        """When critic provides illustrative_structure_text, use it directly."""
        report = _sample_report()
        prebuilt = "Illustrative only: Bull Call Spread on QQQ is not a live recommendation."

        state = _state_with_constraints(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="directional_watchlist",
            illustrative_structure_text=prebuilt,
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="directional_watchlist",
            captured_trade_ideas=list(report.trade_ideas),
        )

        assert report._render_illustrative_structure_text == prebuilt

    def test_true_risk_text_sources_risks_section(self):
        """When true_risk_text is provided, it becomes the Risks section source."""
        report = _sample_report(
            key_risks_and_hedges=["Some old risk text."]
        )

        state = _state_with_constraints(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="directional_watchlist",
            true_risk_text="Market impact risk is High.",
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="directional_watchlist",
            captured_trade_ideas=list(report.trade_ideas),
        )

        assert report.key_risks_and_hedges == ["Market impact risk is High."], \
            "true_risk_text must source the Risks section"


# ==========================================
# Tests: recommended_structure pass-through
# ==========================================

class TestRecommendedStructurePassThrough:
    """Verify that recommended_structure mode passes through unchanged."""

    def test_preserves_trade_ideas(self):
        report = _sample_report()
        original_ideas = list(report.trade_ideas)

        state = _state_with_constraints(
            structure_visibility_mode="recommended_structure",
            recommendation_mode="actionable_options",
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="actionable_options",
            captured_trade_ideas=original_ideas,
        )

        assert len(report.trade_ideas) == len(original_ideas), \
            "recommended_structure must preserve trade ideas"

    def test_preserves_conversation_reply(self):
        original_reply = "For SPY, the current setup favors a Long Put around 540P."
        report = _sample_report(conversation_reply=original_reply)

        state = _state_with_constraints(
            structure_visibility_mode="recommended_structure",
            recommendation_mode="actionable_options",
        )
        _enforce_structure_visibility_mode(
            report,
            state=state,
            recommendation_mode="actionable_options",
            captured_trade_ideas=list(report.trade_ideas),
        )

        assert report.conversation_reply == original_reply


# ==========================================
# Tests: _strip_concrete_structure_text
# ==========================================

class TestStripConcreteStructureText:
    """Verify the text-stripping helper correctly removes structure patterns."""

    @pytest.mark.parametrize("text,expected_absent", [
        ("Consider a Long Put for hedging.", "long put"),
        ("Short Call spread at strike 540.", "short call"),
        ("Buy a Bull Call Spread on SPY.", "bull call spread"),
        ("Iron Condor is suitable here.", "iron condor"),
        ("Straddle the earnings event.", "straddle"),
        ("Buy the 540P for protection.", "540P"),
    ])
    def test_strips_patterns(self, text: str, expected_absent: str):
        result = _strip_concrete_structure_text(text)
        assert expected_absent.lower() not in result.lower(), \
            f"Expected '{expected_absent}' to be stripped from: {result}"

    def test_preserves_non_structure_text(self):
        text = "VIX is elevated. Monitor macro conditions."
        result = _strip_concrete_structure_text(text)
        assert "VIX" in result
        assert "macro" in result


# ==========================================
# Tests: FinalizerRenderGuard schema
# ==========================================

class TestFinalizerRenderGuard:
    """Verify the typed render guard schema validates correctly."""

    def test_defaults_to_no_structure(self):
        guard = FinalizerRenderGuard()
        assert guard.structure_visibility_mode == "no_structure"
        assert guard.forbid_actionable_recommendation is True

    def test_resolved_illustrative_text_no_structure_returns_empty(self):
        guard = FinalizerRenderGuard(structure_visibility_mode="no_structure")
        result = guard.resolved_illustrative_text(captured_trade_ideas=[])
        assert result == ""

    def test_resolved_illustrative_text_with_hint(self):
        guard = FinalizerRenderGuard(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="directional_watchlist",
            illustrative_structure_hint="Long Put on SPY",
        )
        result = guard.resolved_illustrative_text(captured_trade_ideas=[])
        assert "illustrative only" in result.lower()
        assert "Long Put on SPY" in result
        assert "not a live recommendation" in result.lower()

    def test_resolved_illustrative_text_from_trade_ideas(self):
        guard = FinalizerRenderGuard(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="informational_only",
            illustrative_structure_hint="",
        )
        ideas = [_sample_trade_idea()]
        result = guard.resolved_illustrative_text(captured_trade_ideas=ideas)
        assert "illustrative only" in result.lower()
        assert "SPY" in result
        assert "not a current or live recommendation" in result.lower()

    def test_resolved_illustrative_text_fallback_when_no_hint_or_ideas(self):
        guard = FinalizerRenderGuard(
            structure_visibility_mode="illustrative_structure",
            recommendation_mode="informational_only",
        )
        result = guard.resolved_illustrative_text(captured_trade_ideas=[])
        assert "illustrative only" in result.lower()
        assert "the structure from the analyst draft" in result.lower()

    def test_invalid_mode_rejected(self):
        with pytest.raises(Exception):
            FinalizerRenderGuard(structure_visibility_mode="invalid_mode")
