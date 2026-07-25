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
