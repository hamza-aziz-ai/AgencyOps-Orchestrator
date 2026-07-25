"""Weekly client reporting workflow.

    gather -> analyse -> narrate ⇄ verify -> assemble -> propose -> [gate] -> dispatch

Why a graph rather than a linear script: the approval gate is a real
interrupt. The graph halts with the report drafted and the outbound writes
staged but unsent, persists that state, and resumes only when a human
approves. A Zapier chain cannot pause mid-run and survive a restart.

Node responsibilities are strictly separated:
  gather   - I/O only, no logic
  analyse  - deterministic maths, no LLM
  narrate  - LLM only, no maths
  verify   - deterministic maths, no LLM, and no power to rewrite
  propose  - builds Effects, executes nothing
  dispatch - the only node permitted to cause side effects

`verify` exists because prompting alone cannot make a guarantee. The model is
handed every figure pre-computed and told to reproduce the signs exactly; it
still occasionally prints one inverted, because a rising cost is bad news and
a minus sign is how prose usually signals bad news. So the claim is enforced
rather than requested: the commentary is checked against the arithmetic, a
disagreement earns one corrected retry, and copy that still contradicts the
figures is replaced by the offline engine's templated prose. Same shape as the
creative pipeline's brand check - machine-checkable rule, specific violations
fed back, bounded rounds, deterministic floor.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from langgraph.graph import END, StateGraph

from ..analysis import aggregate, compare, find_signals
from ..config import Settings, get_settings
from ..connectors import ConnectorBundle, build_bundle
from ..connectors.base import Effect
from ..llm import LLMEngine, OfflineEngine, build_engine
from ..observability import RunTrace, timed
from .state import ReportState

CLIENT_LABELS = {"nova-retail": "Nova Retail", "atlas-fitness": "Atlas Fitness"}

# One corrective pass. If the model cannot reproduce a sign it was handed
# twice, another attempt is not the answer - the offline engine is.
MAX_NARRATION_ROUNDS = 1

# Every dash-like character a model might print in front of a percentage.
# The observed failure used U+2011, a non-breaking hyphen, which an
# ASCII-only check would have missed entirely.
_DASHES = "-‐‑‒–—−"


def _magnitudes(pct: float) -> set[str]:
    """The ways a model might render this figure: 9.02%, 9.0%, 9%."""
    return {f"{abs(pct):.2f}", f"{abs(pct):.1f}", f"{abs(pct):.0f}"}


def find_sign_violations(narrative: str, deltas: dict[str, Any]) -> list[str]:
    """Report every percentage the prose printed with the wrong sign character.

    Deterministic, text-only, and incapable of repair: it compares what the
    commentary printed against what `compare()` computed, and says so. Keeping
    it unable to rewrite anything is what makes it usable as a gate - a checker
    that also fixes has no independent opinion left to trust.
    """
    metrics = {
        key[:-4]: float(value)
        for key, value in deltas.items()
        if key.endswith("_pct") and value is not None
    }

    # A magnitude two metrics share in opposite directions cannot be pinned to
    # either from the text alone. Skipping it is the honest outcome; guessing
    # would put false failures in front of a human until they stopped reading.
    seen: dict[str, set[bool]] = {}
    for pct in metrics.values():
        if pct:
            for mag in _magnitudes(pct):
                seen.setdefault(mag, set()).add(pct > 0)

    violations: list[str] = []
    for metric, pct in sorted(metrics.items()):
        if not pct:
            continue
        for mag in sorted(_magnitudes(pct)):
            if len(seen.get(mag, ())) != 1:
                continue
            pattern = rf"([{_DASHES}+])\s?{re.escape(mag)}\s?%"
            match = re.search(pattern, narrative)
            if match and (match.group(1) == "+") != (pct > 0):
                violations.append(
                    f"{metric} moved {pct:+.2f}% but the commentary prints "
                    f"'{match.group(0).strip()}'"
                )
                break
    return violations


# --------------------------------------------------------------------------
# Verb checking
#
# The sign check above catches a character. This catches the other half of the
# same error - "CPA fell 9.02%" when it rose - which needs a verb attributed to
# a metric across arbitrary sentence structure. That attribution is where a
# checker starts crying wolf, so every rule below is built to abstain rather
# than guess. An unattributable verb produces no verdict, and no verdict is a
# pass.
# --------------------------------------------------------------------------

# Purely directional words only. "improved", "worsened", "better", "stronger"
# are deliberately absent: a falling CPA is an improvement, so judgement words
# do not map onto a direction. Conflating the two is the bug this whole gate
# exists to catch - a checker that repeats the model's mistake is worse than
# no checker.
_ROSE = (
    "rose", "rise", "risen", "rises", "rising", "increased", "increase",
    "increases", "increasing", "climbed", "climbs", "climbing", "grew",
    "grown", "growing", "jumped", "jumps", "jumping", "up", "higher",
)
_FELL = (
    "fell", "fall", "fallen", "falls", "falling", "decreased", "decrease",
    "decreases", "decreasing", "declined", "decline", "declines", "declining",
    "dropped", "drop", "drops", "dropping", "slipped", "slips", "slipping",
    "dipped", "dip", "dips", "dipping", "down", "lower",
)

# Longest alias wins, so "return on ad spend" is one mention of ROAS rather
# than also a mention of spend - which would bind ROAS's verb to spend and
# invent a violation out of correct prose.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "roas": ("return on ad spend", "roas"),
    "cpa": ("cost per acquisition", "cost per conversion", "cost per sale", "cpa"),
    "ctr": ("click-through rate", "click through rate", "clickthrough rate",
            "click-through", "ctr"),
    "conversions": ("conversions", "conversion"),
    "revenue": ("revenue", "sales"),
    "spend": ("ad spend", "spending", "spend", "budget"),
}

# A verb this far past the metric name is answering a different question.
_WINDOW = 45

# Where one clause stops speaking for the metric and the next starts. Truncating
# here is what stops "the dip in conversions and the rise in CPA" reading as a
# claim that conversions rose.
_CLAUSE_BREAK = re.compile(r"\.|;|\band\b|\bbut\b|\bwhile\b|\bwhilst\b|\bwhereas\b")

_DIRECTION = re.compile(rf"\b({'|'.join(_ROSE + _FELL)})\b")

# "the rise in CPA", "a decline in ROAS" - a verb before the metric only counts
# when a preposition ties it to this metric. Bare "rose ... CPA" does not bind:
# in "spend rose 5.99% but revenue fell", the nearest earlier verb belongs to
# the previous metric.
_BOUND_BEFORE = re.compile(
    rf"\b({'|'.join(_ROSE + _FELL)})\b[^.;]{{0,24}}?\b(?:in|for|on)\s*$"
)


def _metric_spans(lowered: str) -> list[tuple[int, int, str]]:
    """Non-overlapping metric mentions, longest alias winning."""
    found = [
        (m.start(), m.end(), metric)
        for metric, aliases in _METRIC_ALIASES.items()
        for alias in aliases
        for m in re.finditer(rf"\b{re.escape(alias)}\b", lowered)
    ]
    found.sort(key=lambda s: (s[0] - s[1], s[0]))  # longest match first
    accepted: list[tuple[int, int, str]] = []
    for start, end, metric in found:
        if not any(start < e and s < end for s, e, _ in accepted):
            accepted.append((start, end, metric))
    return sorted(accepted)


def _claimed_direction(text: str) -> bool | None:
    match = _DIRECTION.search(_CLAUSE_BREAK.split(text)[0])
    return match.group(1) in _ROSE if match else None


def _cites(region: str, magnitude: str) -> bool:
    """Does this clause quote the figure as a percentage?

    The lookbehind stops the 0-decimal form of 5.99 ("6") matching the "6%"
    inside "16%", which would anchor a spend check to a campaign's CPA.
    """
    return re.search(rf"(?<![\d.]){re.escape(magnitude)}\s?%", region) is not None


def find_verb_violations(narrative: str, deltas: dict[str, Any]) -> list[str]:
    """Report every metric the prose says moved the wrong way.

    Attribution rules, both deliberately narrow:
      * a verb after the metric binds, up to the next metric or clause break
      * a verb before it binds only through a preposition ("the rise in CPA")

    Anything else abstains.
    """
    metrics = {
        key[:-4]: float(value)
        for key, value in deltas.items()
        if key.endswith("_pct") and value is not None
    }
    lowered = narrative.lower()
    spans = _metric_spans(lowered)

    violations: list[str] = []
    reported: set[str] = set()
    for i, (start, end, metric) in enumerate(spans):
        pct = metrics.get(metric)
        if not pct or metric in reported:
            continue

        # Never read past the next metric mention: that clause is about it.
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(lowered)
        previous_end = spans[i - 1][1] if i else 0
        region = lowered[max(previous_end, start - _WINDOW):min(stop, end + _WINDOW)]

        # The clause must cite this metric's *account-level* movement before we
        # judge its verb. A good report also discusses individual campaigns -
        # "the Ramadan set saw ROAS fall 14%" - and a campaign is free to move
        # opposite to the account it belongs to. Without this anchor the check
        # reads those sentences as contradictions and is wrong about correct
        # prose, which is the one failure a gate cannot afford.
        if not any(_cites(region, mag) for mag in _magnitudes(pct)):
            continue

        claimed = _claimed_direction(lowered[end:min(stop, end + _WINDOW)])
        if claimed is None:
            match = _BOUND_BEFORE.search(lowered[max(previous_end, start - _WINDOW):start])
            claimed = match.group(1) in _ROSE if match else None

        if claimed is None or claimed == (pct > 0):
            continue

        violations.append(
            f"{metric} moved {pct:+.2f}% but the commentary describes it as "
            f"{'rising' if claimed else 'falling'}"
        )
        reported.add(metric)
    return violations


def find_narrative_violations(narrative: str, deltas: dict[str, Any]) -> list[str]:
    """Every way the prose can contradict the arithmetic: sign, then direction."""
    return (
        find_sign_violations(narrative, deltas)
        + find_verb_violations(narrative, deltas)
    )


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
        trace = state["trace"]
        rnd = state.get("narration_round", 0) + 1
        with timed(trace, "narrate" if rnd == 1 else f"narrate_retry_{rnd - 1}") as t:
            context = _narrative_context(state)
            prompt = _narrative_prompt(context)
            corrections = state.get("sign_violations") or []
            if corrections:
                prompt = f"{prompt}\n\n{_correction_block(corrections)}"
            resp = engine.complete("report_narrative", prompt, context)
            t.summary = f"narrative via {resp.engine} ({len(resp.text)} chars)"
            t.detail = {"engine": resp.engine, "corrections_fed_back": len(corrections)}
        # Clear the violations the retry was given; verify decides afresh.
        return {"narrative": resp.text, "narration_round": rnd, "sign_violations": []}

    def verify(state: ReportState) -> dict[str, Any]:
        """Check the prose against the arithmetic. Reports, never repairs."""
        trace = state["trace"]
        with timed(trace, "verify") as t:
            violations = find_narrative_violations(state["narrative"], state["deltas"])
            t.summary = (
                "commentary agrees with the computed figures"
                if not violations
                else f"{len(violations)} statement(s) contradict the computed figures"
            )
            t.detail = {"violations": violations}
        return {"sign_violations": violations}

    def fallback_narrative(state: ReportState) -> dict[str, Any]:
        """Last resort: templated prose that cannot disagree with the maths.

        Reached only when the model contradicted figures it was handed, twice.
        Losing the richer commentary is the cheaper failure - a client who
        catches the prose and the table disagreeing stops trusting both.
        """
        trace = state["trace"]
        with timed(trace, "fallback_narrative") as t:
            context = _narrative_context(state)
            resp = OfflineEngine().complete("report_narrative", "", context)
            t.summary = (
                f"model output rejected after {state.get('narration_round', 0)} "
                f"attempt(s); using deterministic commentary"
            )
            t.detail = {
                "engine": resp.engine,
                "rejected_violations": state.get("sign_violations", []),
            }
        return {"narrative": resp.text, "sign_violations": []}

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

    def after_verify(state: ReportState) -> Literal["narrate", "fallback_narrative", "assemble"]:
        """Clean prose ships; wrong signs get one corrected retry, then the
        deterministic engine. Bounded by MAX_NARRATION_ROUNDS."""
        if not state.get("sign_violations"):
            return "assemble"
        if state.get("narration_round", 0) > MAX_NARRATION_ROUNDS:
            return "fallback_narrative"
        return "narrate"

    def after_propose(state: ReportState) -> Literal["dispatch", "await_approval"]:
        return "await_approval" if state.get("approval_required") else "dispatch"

    # --------------------------------------------------------------- graph
    g = StateGraph(ReportState)
    g.add_node("gather", gather)
    g.add_node("analyse", analyse)
    g.add_node("narrate", narrate)
    g.add_node("verify", verify)
    g.add_node("fallback_narrative", fallback_narrative)
    g.add_node("assemble", assemble)
    g.add_node("propose", propose)
    g.add_node("dispatch", dispatch)
    g.add_node("await_approval", halt_for_approval)

    g.set_entry_point("gather")
    g.add_conditional_edges("gather", after_gather, {"analyse": "analyse", "__end__": END})
    g.add_edge("analyse", "narrate")
    g.add_edge("narrate", "verify")
    g.add_conditional_edges(
        "verify",
        after_verify,
        {
            "narrate": "narrate",              # the loop, bounded above
            "fallback_narrative": "fallback_narrative",
            "assemble": "assemble",
        },
    )
    g.add_edge("fallback_narrative", "assemble")
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
# Which way is "good" for each metric. Spend is deliberately unjudged - more
# spend is neither win nor loss on its own, it is the input.
#
# This table exists because of an observed failure: handed a bare `cpa=16.09`,
# the model wrote "CPA up to AED 70 (-16%)". It had correctly worked out that a
# rising CPA is bad news and reached for a minus sign to say so, inverting a
# figure the report table states correctly two lines above. The fix is to stop
# making it infer: give it the sign, the verb and the judgement separately, so
# "is this bad?" and "which way did it move?" are never the same question.
_METRIC_SENSE: dict[str, str | None] = {
    "spend": None,
    "revenue": "higher_is_better",
    "conversions": "higher_is_better",
    "roas": "higher_is_better",
    "cpa": "lower_is_better",
    "ctr": "higher_is_better",
}


def _movement_table(deltas: dict[str, Any]) -> str:
    """Render week-on-week movement with the sign, verb and judgement spelled out."""
    rows = []
    for metric, sense in _METRIC_SENSE.items():
        pct = float(deltas.get(f"{metric}_pct", 0.0) or 0.0)
        if pct > 0:
            verb = "rose"
        elif pct < 0:
            verb = "fell"
        else:
            verb = "held flat"

        if sense is None or pct == 0:
            note = ""
        elif (pct > 0) == (sense == "higher_is_better"):
            note = "  better than last week"
        else:
            note = "  worse than last week"
        rows.append(f"  {metric:<12}{pct:+.2f}%  {verb}{note}")
    return "\n".join(rows)


def _narrative_context(state: ReportState) -> dict[str, Any]:
    """Everything the prose is allowed to be about. Shared by every node that
    generates commentary, so the retry and the fallback cannot drift from the
    figures the first attempt was given."""
    retainer = state.get("retainer")
    return {
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


def _correction_block(violations: list[str]) -> str:
    listed = "\n".join(f"- {v}" for v in violations)
    return (
        "YOUR PREVIOUS DRAFT CONTRADICTED THE COMPUTED FIGURES\n"
        f"{listed}\n"
        "Rewrite the commentary. Keep the analysis and the recommendations; "
        "correct the signs so they match the movement table above. A figure "
        "that rose keeps its + sign even when rising is the bad outcome - say "
        "in words that it is worse."
    )


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

WEEK-ON-WEEK CHANGE
Each row below is computed, not estimated. Copy the sign and the verb exactly.
A metric marked "rose" is described as rising and keeps its + sign, even when
rising is the bad outcome. Never attach a minus sign to a number to signal that
it is bad news - say the number rose, and say plainly that this is worse.
{_movement_table(d)}

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
