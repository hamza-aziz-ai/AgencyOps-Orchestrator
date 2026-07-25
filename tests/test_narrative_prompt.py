"""The narrate prompt must not leave direction of travel to inference.

Motivated by a real failure against gpt-oss: given a bare `cpa=16.09`, the
model wrote "CPA up to AED 70 (-16%)" - correct about the news being bad,
wrong about the sign, and contradicting the report table two lines above.
"""
from __future__ import annotations

from agencyops.graphs.client_report import _movement_table, _narrative_prompt

DELTAS = {
    "spend_pct": 5.99,
    "revenue_pct": -2.73,
    "conversions_pct": -2.79,
    "roas_pct": -8.23,
    "cpa_pct": 9.02,
    "ctr_pct": -4.98,
}

CTX = {
    "client_name": "Nova Retail",
    "currency": "AED",
    "totals": {"spend": 70800.0, "revenue": 413300.0, "conversions": 1151,
               "roas": 5.8376, "cpa": 61.51, "ctr": 1.1575},
    "deltas": DELTAS,
    "findings": [],
    "retainer": {"used_hours": 93.5, "contracted_hours": 80.0,
                 "utilisation": 116.9, "over_budget": True},
}


def _row(table: str, metric: str) -> str:
    return next(line for line in table.splitlines() if line.strip().startswith(metric))


# --------------------------------------------------------------------------
# Signs are explicit
# --------------------------------------------------------------------------
def test_every_movement_carries_an_explicit_sign():
    """A bare `16.09` is the input that produced the bug."""
    table = _movement_table(DELTAS)
    for metric in ("spend", "revenue", "conversions", "roas", "cpa", "ctr"):
        row = _row(table, metric)
        assert "+" in row or "-" in row, row


def test_a_rise_is_positive_and_named_as_a_rise():
    row = _row(_movement_table(DELTAS), "cpa")
    assert "+9.02%" in row
    assert "rose" in row
    assert "fell" not in row


def test_a_fall_is_negative_and_named_as_a_fall():
    row = _row(_movement_table(DELTAS), "roas")
    assert "-8.23%" in row
    assert "fell" in row
    assert "rose" not in row


# --------------------------------------------------------------------------
# Direction and judgement are separated
# --------------------------------------------------------------------------
def test_a_rising_cost_is_marked_worse_while_staying_positive():
    """The exact case that was inverted: bad news, positive number."""
    row = _row(_movement_table(DELTAS), "cpa")
    assert "+9.02%" in row
    assert "worse" in row


def test_a_falling_cost_is_marked_better_while_staying_negative():
    row = _row(_movement_table({**DELTAS, "cpa_pct": -9.02}), "cpa")
    assert "-9.02%" in row
    assert "fell" in row
    assert "better" in row


def test_a_rising_revenue_is_marked_better():
    row = _row(_movement_table({**DELTAS, "revenue_pct": 4.0}), "revenue")
    assert "+4.00%" in row
    assert "better" in row


def test_spend_is_never_judged_good_or_bad():
    """Spend is the input, not the outcome. Calling a rise 'worse' would push
    the model toward apologising for deploying budget that worked."""
    table = _movement_table({**DELTAS, "spend_pct": 40.0})
    row = _row(table, "spend")
    assert "+40.00%" in row
    assert "rose" in row
    assert "better" not in row and "worse" not in row


def test_no_movement_is_flat_and_unjudged():
    row = _row(_movement_table({**DELTAS, "roas_pct": 0.0}), "roas")
    assert "held flat" in row
    assert "better" not in row and "worse" not in row


# --------------------------------------------------------------------------
# The prompt itself
# --------------------------------------------------------------------------
def test_prompt_instructs_against_sign_flipping():
    prompt = _narrative_prompt(CTX)
    lowered = prompt.lower()
    assert "copy the sign and the verb exactly" in lowered
    assert "minus" in lowered


def test_ctr_reaches_the_model():
    """compare() computes it and the report table prints it, so omitting it
    from the prompt invites the model to fill the gap itself."""
    assert "ctr" in _narrative_prompt(CTX)
    assert "-4.98%" in _narrative_prompt(CTX)


def test_prompt_still_carries_levels_and_retainer():
    prompt = _narrative_prompt(CTX)
    assert "70,800" in prompt
    assert "93.5 of 80.0 hours" in prompt
    assert "over_budget=True" in prompt


def test_missing_delta_key_does_not_break_the_prompt():
    """A new metric in _METRIC_SENSE before compare() emits it must not crash
    the narrate node mid-run."""
    row = _row(_movement_table({"spend_pct": 1.0}), "roas")
    assert "held flat" in row
