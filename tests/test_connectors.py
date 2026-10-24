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


# --------------------------------------------------------------------------
# Run status and the shared dispatch path
# --------------------------------------------------------------------------
from agencyops.connectors.base import ConnectorBundle, WriteConnector, run_status  # noqa: E402


def _fx(*statuses: str) -> list[Effect]:
    return [Effect(connector="slack", action="post_message", payload={}, summary="s", status=s)
            for s in statuses]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), "completed"),
        (("rejected", "rejected"), "rejected"),
        (("executed", "rejected"), "completed"),
        (("failed", "rejected"), "completed_with_errors"),
        (("proposed", "failed"), "blocked_on_approval"),
        (("executed", "proposed"), "blocked_on_approval"),
    ],
)
def test_run_status_follows_the_effects(statuses, expected):
    assert run_status(_fx(*statuses)) == expected


class _Writer(WriteConnector):
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    def execute(self, effect):
        self.calls += 1
        if self.fail:
            raise RuntimeError("vendor down")
        return {"ok": True}


def test_dispatch_continues_after_a_failure_and_skips_settled_effects():
    bad, good = _Writer("bad", fail=True), _Writer("good")
    bundle = ConnectorBundle(ads=None, time_tracking=None, writers={"bad": bad, "good": good})
    effects = [
        Effect(connector="bad", action="a", payload={}, summary="s"),
        Effect(connector="good", action="a", payload={}, summary="s"),
        Effect(connector="good", action="a", payload={}, summary="s", status="executed"),
        Effect(connector="good", action="a", payload={}, summary="s", status="failed"),
    ]
    assert bundle.dispatch(effects) == (1, 1)
    assert [e.status for e in effects] == ["failed", "executed", "executed", "failed"]
    assert effects[0].result == {"error": "vendor down"}
    assert good.calls == 1  # neither settled effect was re-sent


# --------------------------------------------------------------------------
# Live adapters, against httpx.MockTransport - adapter behaviour only, not a
# claim about the vendors' current APIs.
# --------------------------------------------------------------------------
import httpx  # noqa: E402

from agencyops.config import Settings  # noqa: E402
from agencyops.connectors import live  # noqa: E402

LIVE = {"connector_mode": "live", "llm_provider": "offline", "gemini_api_key": None}


def _mock_http(monkeypatch, reply):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=reply(request))

    real = httpx.Client
    monkeypatch.setattr(live.httpx, "Client",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    return calls


def test_meta_reads_each_clients_own_account(monkeypatch):
    calls = _mock_http(monkeypatch, lambda r: {"data": []})
    meta = live.MetaAdsConnector(
        Settings(**LIVE, meta_ad_accounts={"nova-retail": "111", "atlas-fitness": "222"}))
    for client in ("nova-retail", "atlas-fitness"):
        meta.fetch_metrics(client)
        meta.fetch_previous_metrics(client)
    assert [r.url.path for r in calls] == (
        ["/v21.0/act_111/insights"] * 2 + ["/v21.0/act_222/insights"] * 2)


def test_destinations_are_fixed_when_the_connector_is_built(monkeypatch):
    """A run keeps the destinations it was reviewed against, even if settings change later."""
    calls = _mock_http(monkeypatch, lambda r: {"data": [], "time_entries": [], "projects": [], "id": "c"})
    s = Settings(**LIVE, meta_ad_accounts={"nova-retail": "111"},
                 harvest_client_ids={"nova-retail": 1}, trello_list_ids={"creative-review": "L1"})
    meta, harvest, trello = live.MetaAdsConnector(s), live.HarvestConnector(s), live.TrelloConnector(s)
    s.meta_ad_accounts["nova-retail"] = "999"
    s.harvest_client_ids["nova-retail"] = 9
    s.trello_list_ids["creative-review"] = "L9"
    meta.fetch_metrics("nova-retail")
    harvest.fetch_entries("nova-retail")
    trello.execute(Effect(connector="trello", action="create_card",
                          payload={"list_id": "creative-review", "name": "n"}, summary="s"))
    assert calls[0].url.path == "/v21.0/act_111/insights"
    assert calls[1].url.params["client_id"] == "1"
    assert calls[2].url.params["idList"] == "L1"


def test_unmapped_clients_and_lists_fail_before_any_request(monkeypatch):
    calls = _mock_http(monkeypatch, lambda r: {})
    s = Settings(**LIVE, meta_ad_accounts={"nova-retail": "111"},
                 harvest_client_ids={"nova-retail": 1}, trello_list_ids={"creative-review": "L1"})
    with pytest.raises(KeyError):
        live.MetaAdsConnector(s).fetch_metrics("atlas-fitness")
    with pytest.raises(KeyError):
        live.HarvestConnector(s).fetch_retainer("atlas-fitness")
    with pytest.raises(KeyError):
        live.TrelloConnector(s).execute(Effect(
            connector="trello", action="create_card",
            payload={"list_id": "media-buying-actions", "name": "n"}, summary="s"))
    assert calls == []


def test_harvest_filters_by_numeric_client_id(monkeypatch):
    calls = _mock_http(monkeypatch, lambda r: {"time_entries": [], "projects": []})
    harvest = live.HarvestConnector(Settings(**LIVE, harvest_client_ids={"nova-retail": 1234567}))
    harvest.fetch_retainer("nova-retail")
    assert [r.url.path for r in calls] == ["/v2/time_entries", "/v2/projects"]
    for r in calls:
        assert r.url.params["client_id"] == "1234567"
        assert "client" not in r.url.params


def test_trello_sends_the_configured_list_id(monkeypatch):
    calls = _mock_http(monkeypatch, lambda r: {"id": "card1"})
    trello = live.TrelloConnector(Settings(**LIVE, trello_list_ids={"creative-review": "abc123"}))
    trello.execute(Effect(connector="trello", action="create_card",
                          payload={"list_id": "creative-review", "name": "n"}, summary="s"))
    assert calls[0].url.params["idList"] == "abc123"


def test_slack_post_with_ok_true_is_executed(monkeypatch):
    _mock_http(monkeypatch, lambda r: {"ok": True, "ts": "1"})
    effect = Effect(connector="slack", action="post_message",
                    payload={"channel": "#x", "text": "hi"}, summary="s")
    bundle = ConnectorBundle(ads=None, time_tracking=None,
                             writers={"slack": live.SlackConnector(Settings(**LIVE))})
    assert bundle.dispatch([effect]) == (1, 0)
    assert effect.status == "executed" and effect.result["ok"] is True


@pytest.mark.parametrize("reply", [{"ok": False, "error": "channel_not_found"}, {}])
def test_slack_post_counts_only_on_explicit_ok(monkeypatch, reply):
    _mock_http(monkeypatch, lambda r: reply)
    slack = live.SlackConnector(Settings(**LIVE))
    effect = Effect(connector="slack", action="post_message",
                    payload={"channel": "#x", "text": "hi"}, summary="s")
    bundle = ConnectorBundle(ads=None, time_tracking=None, writers={"slack": slack})
    assert bundle.dispatch([effect]) == (0, 1)
    assert effect.status == "failed"
    assert reply.get("error", "no ok flag") in effect.result["error"]
