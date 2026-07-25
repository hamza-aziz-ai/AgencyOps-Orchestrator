"""The verify node: prose must agree with the arithmetic, or it does not ship.

Engines here are injected, not patched - a local LLMEngine that returns
whatever the test needs is the same dependency-injection the connector suite
uses, and keeps the suite free of monkeypatching.
"""
from __future__ import annotations

from typing import Any

import pytest

from agencyops.graphs.client_report import (
    MAX_NARRATION_ROUNDS,
    find_sign_violations,
    run_report,
)
from agencyops.llm import LLMResponse

DELTAS = {
    "spend_pct": 5.99,
    "revenue_pct": -2.73,
    "conversions_pct": -2.79,
    "roas_pct": -8.23,
    "cpa_pct": 9.02,
    "ctr_pct": -4.98,
}


class _Scripted:
    """Returns canned commentary, so a graph run can be steered exactly."""

    name = "scripted"

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.calls = 0

    def complete(self, task: str, prompt: str, context: dict[str, Any]) -> LLMResponse:
        self.calls += 1
        text = self._texts[min(self.calls - 1, len(self._texts) - 1)]
        return LLMResponse(text=text, engine=self.name, task=task)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def test_clean_commentary_reports_nothing():
    good = "Spend rose +5.99% while ROAS fell -8.23% and CPA rose +9.02%."
    assert find_sign_violations(good, DELTAS) == []


def test_unsigned_figures_are_not_flagged():
    """The verb carries direction here; this checker only judges characters."""
    text = "CPA rose 9.02% and ROAS fell 8.23%."
    assert find_sign_violations(text, DELTAS) == []


def test_the_observed_failure_is_caught():
    """The real one, verbatim in shape: a rising CPA described as rising but
    printed with a U+2011 non-breaking hyphen in front of the figure."""
    violations = find_sign_violations("CPA up to AED 70 (‑9.02%)", DELTAS)
    assert len(violations) == 1
    assert "cpa" in violations[0]
    assert "+9.02%" in violations[0]


@pytest.mark.parametrize(
    "dash", ["-", "‐", "‑", "‒", "–", "—", "−"]
)
def test_every_dash_variant_is_caught(dash):
    """An ASCII-only check would have missed the character that caused this."""
    assert find_sign_violations(f"CPA moved {dash}9.02% this week", DELTAS)


def test_a_fall_printed_as_a_rise_is_caught():
    assert find_sign_violations("ROAS improved +8.23% this week", DELTAS)


@pytest.mark.parametrize("rendered", ["‑9.02%", "‑9.0%", "‑9%"])
def test_every_rounding_the_model_might_use_is_caught(rendered):
    assert find_sign_violations(f"CPA moved {rendered}", DELTAS)


def test_a_magnitude_two_metrics_share_is_skipped():
    """Ambiguous attribution is not a violation. A gate that cries wolf gets
    switched off, and then it protects nothing."""
    ambiguous = {"cpa_pct": 5.0, "roas_pct": -5.0}
    assert find_sign_violations("something moved -5.00%", ambiguous) == []


def test_zero_movement_is_never_a_violation():
    assert find_sign_violations("held flat at -0.00%", {"roas_pct": 0.0}) == []


def test_missing_and_null_deltas_do_not_crash():
    assert find_sign_violations("nothing here", {}) == []
    assert find_sign_violations("nothing here", {"roas_pct": None}) == []


# --------------------------------------------------------------------------
# The node, in a real graph run
# --------------------------------------------------------------------------
CLEAN = "Spend rose +5.99%, ROAS fell -8.23%, CPA rose +9.02%. Steady as she goes."
FLIPPED = "Spend rose +5.99%, ROAS fell -8.23%, CPA moved ‑9.02%. Watch costs."


def test_clean_prose_is_kept_untouched(settings, bundle):
    s = run_report("nova-retail", bundle=bundle, engine=_Scripted(CLEAN), settings=settings)
    assert s["narrative"] == CLEAN
    assert s["sign_violations"] == []
    assert [n.node for n in s["trace"].steps].count("verify") == 1


def test_a_flipped_sign_earns_a_corrected_retry(settings, bundle):
    engine = _Scripted(FLIPPED, CLEAN)
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    nodes = [n.node for n in s["trace"].steps]
    assert "narrate_retry_1" in nodes
    assert "fallback_narrative" not in nodes
    assert s["narrative"] == CLEAN
    assert engine.calls == 2


def test_the_retry_is_told_exactly_what_was_wrong(settings, bundle):
    """A vague 'try again' is what the creative pipeline exists not to do."""
    seen: list[str] = []

    class _Recording(_Scripted):
        def complete(self, task, prompt, context):
            seen.append(prompt)
            return super().complete(task, prompt, context)

    run_report("nova-retail", bundle=bundle, engine=_Recording(FLIPPED, CLEAN),
               settings=settings)
    assert len(seen) == 2
    assert "CONTRADICTED THE COMPUTED FIGURES" in seen[1]
    assert "cpa moved +9.02%" in seen[1].lower()


def test_incurably_wrong_copy_falls_back_to_deterministic_prose(settings, bundle):
    """A model that will not reproduce a sign it was handed twice does not get
    a third go, and its copy does not reach the client."""
    engine = _Scripted(FLIPPED)  # never complies
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    nodes = [n.node for n in s["trace"].steps]
    assert "fallback_narrative" in nodes
    assert s["narrative"] != FLIPPED
    assert find_sign_violations(s["narrative"], s["deltas"]) == []


def test_the_loop_terminates(settings, bundle):
    """Invariant: every cycle needs a proof it exits."""
    engine = _Scripted(FLIPPED)
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    nodes = [n.node for n in s["trace"].steps]
    assert engine.calls == MAX_NARRATION_ROUNDS + 1
    assert nodes[-1] == "await_approval"
    assert nodes.count("verify") == MAX_NARRATION_ROUNDS + 1


def test_a_rejected_narrative_never_reaches_the_staged_slack_post(settings, bundle):
    """The end that matters: bad copy must not be staged for a client channel."""
    s = run_report("nova-retail", bundle=bundle, engine=_Scripted(FLIPPED), settings=settings)
    slack = next(e for e in s["effects"] if e.connector == "slack")
    assert FLIPPED not in slack.payload["text"]
    assert s["narrative"] in slack.payload["text"]


def test_the_trace_records_why_the_model_was_overruled(settings, bundle):
    """Months later, 'why does this week read like a template' has an answer."""
    s = run_report("nova-retail", bundle=bundle, engine=_Scripted(FLIPPED), settings=settings)
    step = next(n for n in s["trace"].steps if n.node == "fallback_narrative")
    assert step.detail["engine"] == "offline"
    assert any("cpa" in v for v in step.detail["rejected_violations"])
