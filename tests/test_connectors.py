from __future__ import annotations

import pytest

from agencyops.connectors.base import AdMetrics, Effect, RetainerStatus


def test_ad_metrics_derived_fields():
    m = AdMetrics(
        campaign_id="1", campaign_name="Test", spend=1000.0,
        impressions=100_000, clicks=2_000, conversions=50, revenue=5_000.0,
    )
    assert m.ctr == pytest.approx(2.0)
    assert m.cpa == pytest.approx(20.0)
    assert m.roas == pytest.approx(5.0)


def test_ad_metrics_never_divides_by_zero():
    m = AdMetrics(
        campaign_id="1", campaign_name="Dead", spend=0.0,
        impressions=0, clicks=0, conversions=0, revenue=0.0,
    )
    assert (m.ctr, m.cpa, m.roas) == (0.0, 0.0, 0.0)


def test_retainer_flags_overrun():
    r = RetainerStatus(client="x", contracted_hours=80, used_hours=93.5)
    assert r.over_budget
    assert r.utilisation == pytest.approx(116.875)


def test_bundle_rejects_unregistered_writer(bundle):
    with pytest.raises(KeyError, match="No write connector"):
        bundle.writer("carrier-pigeon")


def test_unknown_client_raises(bundle):
    with pytest.raises(KeyError, match="Unknown client"):
        bundle.ads.fetch_metrics("no-such-client")


def test_effects_are_inert_until_dispatched(bundle):
    """Constructing an Effect must not touch the outside world."""
    slack = bundle.writer("slack")
    Effect(connector="slack", action="post_message",
           payload={"channel": "#x", "text": "hi"}, summary="test")
    assert slack.log == []


def test_writer_records_on_execute(bundle):
    effect = Effect(connector="slack", action="post_message",
                    payload={"channel": "#x", "text": "hi"}, summary="test")
    result = bundle.writer("slack").execute(effect)
    assert result["ok"] is True
    assert len(bundle.writer("slack").log) == 1
