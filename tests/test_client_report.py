from __future__ import annotations

import pytest

from agencyops.graphs.client_report import run_report


def test_report_runs_end_to_end(settings, bundle, engine):
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    assert s["report_markdown"].startswith("# Nova Retail")
    assert s["narrative"]
    assert s["totals"]["campaign_count"] == 3


def test_report_halts_before_sending_anything(settings, bundle, engine):
    """The whole point: nothing reaches Slack without a human."""
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    assert s["approval_required"] is True
    assert s["approved"] is False
    assert all(e.status == "proposed" for e in s["effects"])
    assert bundle.writer("slack").log == []
    assert bundle.writer("trello").log == []


def test_report_dispatches_when_gate_disabled(auto_settings, engine):
    from agencyops.connectors import build_bundle

    b = build_bundle(auto_settings)
    s = run_report("nova-retail", bundle=b, engine=engine, settings=auto_settings)
    assert all(e.status == "executed" for e in s["effects"])
    assert len(b.writer("slack").log) == 1


def test_high_severity_findings_create_action_cards(settings, bundle, engine):
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    highs = [f for f in s["findings"] if f["severity"] == "high"]
    cards = [e for e in s["effects"] if e.connector == "trello"]
    assert len(cards) == len(highs) > 0


def test_slack_post_marked_irreversible(settings, bundle, engine):
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    slack = next(e for e in s["effects"] if e.connector == "slack")
    assert slack.reversible is False


def test_unknown_client_short_circuits_without_side_effects(settings, bundle, engine):
    s = run_report("ghost-client", bundle=bundle, engine=engine, settings=settings)
    assert s["errors"]
    assert not s.get("effects")
    assert bundle.writer("slack").log == []


def test_trace_records_every_node(settings, bundle, engine):
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    nodes = [step.node for step in s["trace"].steps]
    assert nodes == ["gather", "analyse", "narrate", "assemble", "propose", "await_approval"]


def test_healthy_client_reports_fewer_problems(settings, bundle, engine):
    nova = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    atlas = run_report("atlas-fitness", bundle=bundle, engine=engine, settings=settings)
    nova_high = sum(1 for f in nova["findings"] if f["severity"] == "high")
    atlas_high = sum(1 for f in atlas["findings"] if f["severity"] == "high")
    assert atlas_high < nova_high


def test_narrative_contains_no_invented_figures(settings, bundle, engine):
    """The LLM narrates numbers computed in code; it must not introduce new ones."""
    s = run_report("nova-retail", bundle=bundle, engine=engine, settings=settings)
    t = s["totals"]
    narrative = s["narrative"]
    assert f"{t['spend']:,.0f}" in narrative
    assert f"{t['roas']:.2f}" in narrative
