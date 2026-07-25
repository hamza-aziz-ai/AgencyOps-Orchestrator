"""Weekly client reporting workflow.

    gather -> analyse -> narrate -> assemble -> propose -> [approval gate] -> dispatch

Why a graph rather than a linear script: the approval gate is a real
interrupt. The graph halts with the report drafted and the outbound writes
staged but unsent, persists that state, and resumes only when a human
approves. A Zapier chain cannot pause mid-run and survive a restart.

Node responsibilities are strictly separated:
  gather   - I/O only, no logic
  analyse  - deterministic maths, no LLM
  narrate  - LLM only, no maths
  propose  - builds Effects, executes nothing
  dispatch - the only node permitted to cause side effects
"""
from __future__ import annotations

import time
from typing import Any, Literal

from langgraph.graph import END, StateGraph

from ..analysis import aggregate, compare, find_signals
from ..config import Settings, get_settings
from ..connectors import ConnectorBundle, build_bundle
from ..connectors.base import Effect
from ..llm import LLMEngine, build_engine
from ..observability import RunTrace, timed
from .state import ReportState

CLIENT_LABELS = {"nova-retail": "Nova Retail", "atlas-fitness": "Atlas Fitness"}


def build_report_graph(
    bundle: ConnectorBundle | None = None,
    engine: LLMEngine | None = None,
    settings: Settings | None = None,
):
    settings = settings or get_settings()
    bundle = bundle or build_bundle(settings)
    engine = engine or build_engine(settings)

    # ---------------------------------------------------------------- nodes
    def gather(state: ReportState) -> dict[str, Any]:
        trace, client = state["trace"], state["client"]
        window = state.get("window_days", 7)
        with timed(trace, "gather") as t:
            try:
                current = bundle.ads.fetch_metrics(client, window)
                previous = bundle.ads.fetch_previous_metrics(client, window)
                entries = bundle.time_tracking.fetch_entries(client, window)
                retainer = bundle.time_tracking.fetch_retainer(client)
            except KeyError as exc:
                t.summary = f"failed: {exc}"
                return {"errors": [str(exc)]}
            t.summary = f"{len(current)} campaigns, {len(entries)} time entries"
            t.detail = {"window_days": window}
        return {
            "current": current,
            "previous": previous,
            "time_entries": entries,
            "retainer": retainer,
            "client_name": CLIENT_LABELS.get(client, client.replace("-", " ").title()),
        }

    def analyse(state: ReportState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "analyse") as t:
            totals = aggregate(state["current"])
            deltas = compare(state["current"], state["previous"])
            findings = find_signals(state["current"], state["previous"], state.get("retainer"))
            highs = sum(1 for f in findings if f["severity"] == "high")
            t.summary = f"{len(findings)} findings ({highs} high severity)"
            t.detail = {"roas_delta_pct": deltas["roas_pct"]}
        return {"totals": totals, "deltas": deltas, "findings": findings}

    def narrate(state: ReportState) -> dict[str, Any]:
        trace, retainer = state["trace"], state.get("retainer")
        with timed(trace, "narrate") as t:
            context = {
                "client_name": state["client_name"],
                "totals": state["totals"],
                "deltas": state["deltas"],
                "findings": state["findings"],
                "currency": state["totals"].get("currency", "AED"),
                "retainer": {
                    "used_hours": retainer.used_hours if retainer else 0,
                    "contracted_hours": retainer.contracted_hours if retainer else 0,
                    "utilisation": retainer.utilisation if retainer else 0,
                    "over_budget": retainer.over_budget if retainer else False,
                },
            }
            prompt = _narrative_prompt(context)
            resp = engine.complete("report_narrative", prompt, context)
            t.summary = f"narrative via {resp.engine} ({len(resp.text)} chars)"
            t.detail = {"engine": resp.engine}
        return {"narrative": resp.text}

    def assemble(state: ReportState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "assemble") as t:
            md = _render_markdown(state)
            t.summary = f"report assembled ({len(md.splitlines())} lines)"
        return {"report_markdown": md}

    def propose(state: ReportState) -> dict[str, Any]:
        """Stage the outbound writes. Nothing leaves the building here."""
        trace = state["trace"]
        channel = state.get("channel", settings.slack_default_channel)
        with timed(trace, "propose") as t:
            effects = [
                Effect(
                    connector="slack",
                    action="post_message",
                    payload={"channel": channel, "text": state["report_markdown"]},
                    summary=f"Post weekly report for {state['client_name']} to {channel}",
                    reversible=False,
                )
            ]
            high = [f for f in state["findings"] if f["severity"] == "high"]
            for f in high:
                effects.append(
                    Effect(
                        connector="trello",
                        action="create_card",
                        payload={
                            "list_id": "media-buying-actions",
                            "name": f"[{state['client_name']}] {f['headline'][:80]}",
                            "description": f["recommendation"],
                        },
                        summary=f"Create action card: {f['type']} on {f['campaign'] or 'account'}",
                    )
                )
            t.summary = f"{len(effects)} effects staged, none executed"
            t.detail = {"effects": [e.render() for e in effects]}
        return {
            "effects": effects,
            "approval_required": settings.require_human_approval,
            "approved": not settings.require_human_approval,
        }

    def dispatch(state: ReportState) -> dict[str, Any]:
        """The only node with permission to mutate the outside world."""
        trace = state["trace"]
        with timed(trace, "dispatch") as t:
            executed = 0
            for effect in state.get("effects", []):
                if effect.status not in ("proposed", "approved"):
                    continue
                try:
                    effect.result = bundle.writer(effect.connector).execute(effect)
                    effect.status = "executed"
                    executed += 1
                except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                    effect.status = "failed"
                    effect.result = {"error": str(exc)}
            t.summary = f"{executed}/{len(state.get('effects', []))} effects executed"
        return {}

    def halt_for_approval(state: ReportState) -> dict[str, Any]:
        with timed(state["trace"], "await_approval") as t:
            t.summary = (
                f"paused - {len(state.get('effects', []))} effects awaiting human approval"
            )
        return {}

    # ------------------------------------------------------------- routing
    def after_gather(state: ReportState) -> Literal["analyse", "__end__"]:
        return "__end__" if state.get("errors") else "analyse"

    def after_propose(state: ReportState) -> Literal["dispatch", "await_approval"]:
        return "await_approval" if state.get("approval_required") else "dispatch"

    # --------------------------------------------------------------- graph
    g = StateGraph(ReportState)
    g.add_node("gather", gather)
    g.add_node("analyse", analyse)
    g.add_node("narrate", narrate)
    g.add_node("assemble", assemble)
    g.add_node("propose", propose)
    g.add_node("dispatch", dispatch)
    g.add_node("await_approval", halt_for_approval)

    g.set_entry_point("gather")
    g.add_conditional_edges("gather", after_gather, {"analyse": "analyse", "__end__": END})
    g.add_edge("analyse", "narrate")
    g.add_edge("narrate", "assemble")
    g.add_edge("assemble", "propose")
    g.add_conditional_edges(
        "propose", after_propose, {"dispatch": "dispatch", "await_approval": "await_approval"}
    )
    g.add_edge("dispatch", END)
    g.add_edge("await_approval", END)
    return g.compile()


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------
def _narrative_prompt(ctx: dict[str, Any]) -> str:
    findings = "\n".join(
        f"- [{f['severity']}] {f['headline']} | action: {f['recommendation']}"
        for f in ctx["findings"]
    ) or "- none above materiality threshold"
    t, d, r = ctx["totals"], ctx["deltas"], ctx["retainer"]
    return f"""Client: {ctx['client_name']}
Currency: {ctx['currency']}

THIS WEEK
spend={t['spend']:,.0f} revenue={t['revenue']:,.0f} conversions={t['conversions']} \
roas={t['roas']:.2f} cpa={t['cpa']:,.2f} ctr={t['ctr']:.2f}%

WEEK-ON-WEEK CHANGE (%)
spend={d['spend_pct']} revenue={d['revenue_pct']} conversions={d['conversions_pct']} \
roas={d['roas_pct']} cpa={d['cpa_pct']}

MATERIAL FINDINGS
{findings}

RETAINER
{r['used_hours']:.1f} of {r['contracted_hours']:.1f} hours used ({r['utilisation']:.0f}%), \
over_budget={r['over_budget']}

Write the client commentary."""


def _render_markdown(state: ReportState) -> str:
    t, d = state["totals"], state["deltas"]
    cur = t.get("currency", "AED")
    retainer = state.get("retainer")

    def arrow(v: float) -> str:
        return "▲" if v > 0 else ("▼" if v < 0 else "—")

    lines = [
        f"# {state['client_name']} — Weekly Performance Report",
        f"_Window: last {state.get('window_days', 7)} days · generated by AgencyOps Orchestrator_",
        "",
        "## Headline numbers",
        "",
        "| Metric | This week | WoW |",
        "|---|---:|---:|",
        f"| Spend | {cur} {t['spend']:,.0f} | {arrow(d['spend_pct'])} {d['spend_pct']:+.1f}% |",
        f"| Revenue | {cur} {t['revenue']:,.0f} | {arrow(d['revenue_pct'])} {d['revenue_pct']:+.1f}% |",
        f"| ROAS | {t['roas']:.2f} | {arrow(d['roas_pct'])} {d['roas_pct']:+.1f}% |",
        f"| Conversions | {t['conversions']:,} | {arrow(d['conversions_pct'])} {d['conversions_pct']:+.1f}% |",
        f"| CPA | {cur} {t['cpa']:,.2f} | {arrow(d['cpa_pct'])} {d['cpa_pct']:+.1f}% |",
        f"| CTR | {t['ctr']:.2f}% | {arrow(d['ctr_pct'])} {d['ctr_pct']:+.1f}% |",
        "",
        "## Commentary",
        "",
        state.get("narrative", "_not generated_"),
        "",
    ]

    findings = state.get("findings", [])
    if findings:
        lines += ["## What we're acting on", ""]
        for f in findings:
            badge = {"high": "🔴", "medium": "🟠", "info": "🟢"}[f["severity"]]
            lines += [f"**{badge} {f['headline']}**", f"> {f['recommendation']}", ""]

    if retainer:
        lines += [
            "## Resourcing",
            "",
            f"- Contracted: **{retainer.contracted_hours:.1f}h**",
            f"- Used: **{retainer.used_hours:.1f}h** ({retainer.utilisation:.0f}%)",
        ]
        by_task: dict[str, float] = {}
        for e in state.get("time_entries", []):
            by_task[e.task] = by_task.get(e.task, 0) + e.hours
        for task, hours in sorted(by_task.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {task}: {hours:.1f}h")
        lines.append("")

    return "\n".join(lines)


def run_report(
    client: str,
    window_days: int = 7,
    channel: str | None = None,
    **build_kwargs: Any,
) -> ReportState:
    """Convenience entry point used by the CLI, API and tests."""
    graph = build_report_graph(**build_kwargs)
    trace = RunTrace(workflow="client_report")
    settings = build_kwargs.get("settings") or get_settings()
    initial: ReportState = {
        "client": client,
        "window_days": window_days,
        "channel": channel or settings.slack_default_channel,
        "trace": trace,
        "findings": [],
        "effects": [],
        "errors": [],
    }
    result = graph.invoke(initial)
    trace.finish("blocked_on_approval" if result.get("approval_required") and not result.get("approved") else "completed")
    return result
