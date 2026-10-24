from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agencyops.api.main import app

client = TestClient(app)


def test_health():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["connector_mode"] == "mock"
    assert body["human_approval_required"] is True


def test_report_returns_pending_run():
    body = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    assert body["pending_approval"] is True
    assert all(e["status"] == "proposed" for e in body["effects"])
    assert body["totals"]["campaign_count"] == 3


def test_unknown_client_returns_404():
    assert client.post("/workflows/client-report", json={"client": "nope"}).status_code == 404


def test_report_document_retrievable():
    run = client.post("/workflows/client-report", json={"client": "atlas-fitness"}).json()
    md = client.get(f"/runs/{run['run_id']}/report").text
    assert md.startswith("# Atlas Fitness")


def test_approval_dispatches_effects():
    run = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    out = client.post(f"/runs/{run['run_id']}/decision", json={"decision": "approve"}).json()
    assert out["dispatched"] == len(run["effects"])
    assert all(e["status"] == "executed" for e in out["effects"])


def test_rejection_dispatches_nothing():
    run = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    out = client.post(f"/runs/{run['run_id']}/decision", json={"decision": "reject"}).json()
    assert out["dispatched"] == 0
    assert all(e["status"] == "rejected" for e in out["effects"])


def test_partial_approval():
    """An account lead can release the report but hold the action cards."""
    run = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    out = client.post(
        f"/runs/{run['run_id']}/decision",
        json={"decision": "approve", "effect_indexes": [0]},
    ).json()
    assert out["dispatched"] == 1
    assert out["effects"][0]["status"] == "executed"
    assert out["effects"][1]["status"] == "proposed"


def test_creative_endpoint():
    body = client.post("/workflows/creative", json={
        "client": "atlas-fitness", "product": "Atlas Annual Membership",
        "audience": "first-time gym joiners", "key_benefit": "a coached start",
        "cta": "Start today", "variant_count": 4,
    }).json()
    assert body["approved_variants"]
    assert body["pending_approval"] is True


def test_trace_exposed_on_run():
    run = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    detail = client.get(f"/runs/{run['run_id']}").json()
    assert [s["node"] for s in detail["trace"]["steps"]][0] == "gather"


def test_missing_run_404():
    assert client.get("/runs/deadbeef").status_code == 404


# --------------------------------------------------------------------------
# Console surface
# --------------------------------------------------------------------------
def test_client_roster_flags_creative_availability():
    roster = client.get("/clients").json()
    slugs = {c["slug"] for c in roster}
    assert {"nova-retail", "atlas-fitness"} <= slugs
    assert all(c["creative_enabled"] for c in roster if c["slug"] in slugs)


def test_run_summary_carries_start_time():
    run = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    assert run["started_at"]


def test_creative_variants_survive_a_cold_reload():
    """A reviewer opening the run later must see the copy, not just the effects."""
    run = client.post("/workflows/creative", json={
        "client": "nova-retail", "product": "Nova Winter Edit",
        "audience": "returning customers", "key_benefit": "next-day delivery",
    }).json()
    artifacts = client.get(f"/runs/{run['run_id']}").json()["artifacts"]
    assert artifacts["approved_variants"] == run["approved_variants"]


def test_console_is_served():
    body = client.get("/console/").text
    assert "AgencyOps" in body
    assert client.get("/", follow_redirects=False).status_code in (307, 308)


# --------------------------------------------------------------------------
# Decision contract
# --------------------------------------------------------------------------
def _report_run() -> dict:
    run = client.post("/workflows/client-report", json={"client": "nova-retail"}).json()
    assert len(run["effects"]) >= 2
    return run


def _decide(run_id: str, decision: str, indexes=None):
    body = {"decision": decision}
    if indexes is not None:
        body["effect_indexes"] = indexes
    return client.post(f"/runs/{run_id}/decision", json=body)


@pytest.mark.parametrize(("indexes", "code"), [([-1], 422), ([], 422), ([99], 400)])
def test_bad_effect_indexes_are_refused(indexes, code):
    run = _report_run()
    assert _decide(run["run_id"], "approve", indexes).status_code == code
    effects = client.get(f"/runs/{run['run_id']}").json()["effects"]
    assert all(e["status"] == "proposed" for e in effects)


@pytest.mark.parametrize("second", ["approve", "reject"])
def test_settled_effect_cannot_be_decided_again(second):
    run = _report_run()
    assert _decide(run["run_id"], "approve", [0]).status_code == 200
    assert _decide(run["run_id"], second, [0]).status_code == 409
    assert client.get(f"/runs/{run['run_id']}").json()["effects"][0]["status"] == "executed"


def test_mixed_selection_changes_nothing():
    run = _report_run()
    _decide(run["run_id"], "approve", [0])
    assert _decide(run["run_id"], "approve", [0, 1]).status_code == 409
    assert client.get(f"/runs/{run['run_id']}").json()["effects"][1]["status"] == "proposed"


def test_duplicate_indexes_dispatch_once():
    run = _report_run()
    out = _decide(run["run_id"], "approve", [0, 0]).json()
    assert (out["dispatched"], out["failed"], out["rejected"]) == (1, 0, 0)


def test_nothing_pending_is_a_conflict():
    run = _report_run()
    _decide(run["run_id"], "reject")
    assert _decide(run["run_id"], "approve").status_code == 409


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_partial_decision_leaves_the_run_pending_and_traced(decision):
    run = _report_run()
    out = _decide(run["run_id"], decision, [1]).json()
    assert out["status"] == "blocked_on_approval" and out["pending_approval"] is True
    assert out["rejected"] == (1 if decision == "reject" else 0)
    step = client.get(f"/runs/{run['run_id']}").json()["trace"]["steps"][-1]
    assert step["node"] == "human_approval"
    assert step["detail"] == {"decision": decision, "effect_indexes": [1]}


def test_concurrent_approvals_dispatch_once(monkeypatch):
    """The second decision is proven to contend for the lock before the first
    write is released, so this cannot pass by scheduling luck."""
    import threading

    from agencyops.runstore import STORE

    run = STORE.get(_report_run()["run_id"])

    class _SpyLock:
        def __init__(self):
            self._lock, self._entries = threading.Lock(), 0
            self.contended = threading.Event()

        def __enter__(self):
            self._entries += 1
            if self._entries > 1:
                self.contended.set()
            self._lock.acquire()

        def __exit__(self, *exc):
            self._lock.release()

    spy = _SpyLock()
    monkeypatch.setattr(run, "lock", spy)
    slack = run.bundle.writer("slack")
    real, calls = slack.execute, []
    entered, release = threading.Event(), threading.Event()

    def held(effect):
        calls.append(effect)
        entered.set()
        release.wait(5)
        return real(effect)

    monkeypatch.setattr(slack, "execute", held)
    results: dict[str, object] = {}

    def decide(name):
        try:
            results[name] = STORE.decide(run.run_id, "approve", [0])
        except ValueError as exc:
            results[name] = exc

    first = threading.Thread(target=decide, args=("first",))
    second = threading.Thread(target=decide, args=("second",))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert spy.contended.wait(5), "second decision never reached the lock"
    finally:
        release.set()  # never leave a thread parked, even when an assert fails
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert results["first"]["dispatched"] == 1
    assert isinstance(results["second"], ValueError)
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Failed runs leave a trace
# --------------------------------------------------------------------------
class _Crash:
    def invoke(self, state):
        raise RuntimeError("upstream")


def test_state_error_run_persists_a_failed_trace(monkeypatch, tmp_path):
    import json

    from agencyops import observability

    monkeypatch.setattr(observability, "RUNS_DIR", tmp_path)
    assert client.post("/workflows/client-report", json={"client": "nope"}).status_code == 404
    [saved] = tmp_path.glob("client_report-*.json")
    assert json.loads(saved.read_text())["status"] == "failed"


def test_crashed_run_persists_a_failed_trace(monkeypatch, tmp_path):
    import json

    from agencyops import observability
    from agencyops.api import main

    monkeypatch.setattr(observability, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(main, "build_report_graph", lambda **kw: _Crash())
    with pytest.raises(RuntimeError, match="upstream"):
        client.post("/workflows/client-report", json={"client": "nova-retail"})
    [saved] = tmp_path.glob("client_report-*.json")
    assert json.loads(saved.read_text())["status"] == "failed"


def test_trace_write_failure_never_masks_the_run_failure(monkeypatch, caplog):
    from agencyops.api import main
    from agencyops.observability import RunTrace

    def unwritable(self, directory=None):
        raise OSError("disk full")

    monkeypatch.setattr(main, "build_report_graph", lambda **kw: _Crash())
    monkeypatch.setattr(RunTrace, "persist", unwritable)
    with pytest.raises(RuntimeError, match="upstream"):
        client.post("/workflows/client-report", json={"client": "nova-retail"})
    assert "could not persist trace" in caplog.text


def test_store_refuses_an_unknown_decision():
    from agencyops.runstore import STORE

    run = _report_run()
    with pytest.raises(ValueError):
        STORE.decide(run["run_id"], "maybe")
    assert all(e.status == "proposed" for e in STORE.get(run["run_id"]).effects)


@pytest.mark.parametrize(("path", "body", "writer"), [
    ("/workflows/client-report", {"client": "nova-retail"}, "MockSlack"),
    ("/workflows/creative", {"client": "atlas-fitness", "product": "Atlas Annual Membership",
                             "audience": "first-time gym joiners", "key_benefit": "a coached start"},
     "MockTrello"),
])
def test_gate_off_write_failure_reaches_the_api_status(monkeypatch, path, body, writer):
    from agencyops.api import main
    from agencyops.config import Settings
    from agencyops.connectors import mock

    gate_off = Settings(connector_mode="mock", llm_provider="offline", gemini_api_key=None,
                        require_human_approval=False)
    monkeypatch.setattr(main, "get_settings", lambda: gate_off)

    def down(self, effect):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(getattr(mock, writer), "execute", down)
    out = client.post(path, json=body).json()
    assert out["status"] == "completed_with_errors"
    assert any(e["status"] == "failed" for e in out["effects"])
