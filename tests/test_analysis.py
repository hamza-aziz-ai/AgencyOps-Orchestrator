from __future__ import annotations

import pytest

from agencyops.analysis import MIN_SPEND_FOR_FINDING, aggregate, compare, find_signals
from agencyops.connectors.base import AdMetrics, RetainerStatus


def mk(cid="1", name="C", spend=10_000.0, imps=100_000, clicks=2_000, conv=100, rev=50_000.0):
    return AdMetrics(campaign_id=cid, campaign_name=name, spend=spend, impressions=imps,
                     clicks=clicks, conversions=conv, revenue=rev)


def test_aggregate_totals():
    t = aggregate([mk(), mk(cid="2")])
    assert t["campaign_count"] == 2
    assert t["spend"] == 20_000.0
    assert t["roas"] == pytest.approx(5.0)


def test_aggregate_empty_is_safe():
    t = aggregate([])
    assert t["campaign_count"] == 0 and t["roas"] == 0.0


def test_compare_computes_percentage_deltas():
    d = compare([mk(spend=11_000.0)], [mk(spend=10_000.0)])
    assert d["spend_pct"] == pytest.approx(10.0)


def test_small_spend_campaigns_are_not_flagged():
    """Percentage swings on trivial spend are noise, not findings."""
    tiny = MIN_SPEND_FOR_FINDING - 1
    cur = [mk(spend=tiny, rev=tiny * 0.5)]
    prev = [mk(spend=tiny, rev=tiny * 5)]
    assert find_signals(cur, prev) == []


def test_roas_collapse_is_high_severity():
    cur = [mk(name="Losing", spend=20_000.0, rev=20_000.0, conv=40)]
    prev = [mk(name="Losing", spend=20_000.0, rev=100_000.0, conv=200)]
    findings = find_signals(cur, prev)
    kinds = {f["type"] for f in findings}
    assert "roas_decline" in kinds
    assert any(f["severity"] == "high" for f in findings)


def test_unprofitable_campaign_is_flagged():
    cur = [mk(name="Bleeding", spend=20_000.0, rev=10_000.0)]
    prev = [mk(name="Bleeding", spend=20_000.0, rev=10_000.0)]
    assert any(f["type"] == "unprofitable" for f in find_signals(cur, prev))


def test_new_campaign_detected():
    findings = find_signals([mk(cid="99", name="Brand New")], [])
    assert findings[0]["type"] == "new_campaign"


def test_retainer_overrun_becomes_a_finding():
    r = RetainerStatus(client="x", contracted_hours=80, used_hours=95)
    findings = find_signals([mk()], [mk()], r)
    assert any(f["type"] == "retainer_overrun" for f in findings)


def test_findings_sorted_high_severity_first():
    cur = [mk(cid="1", name="Bad", spend=20_000.0, rev=10_000.0),
           mk(cid="2", name="Good", spend=20_000.0, rev=200_000.0)]
    prev = [mk(cid="1", name="Bad", spend=20_000.0, rev=10_000.0),
            mk(cid="2", name="Good", spend=20_000.0, rev=100_000.0)]
    findings = find_signals(cur, prev)
    assert findings[0]["severity"] == "high"
