"""HTTP surface.

Four concerns only: trigger a workflow, inspect a run, release staged
effects, and serve the review console. Business logic lives in the graphs -
this layer is transport.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..config import get_settings
from ..connectors import build_bundle, run_status
from ..graphs.client_report import CLIENT_LABELS, build_report_graph
from ..graphs.creative_pipeline import build_creative_graph, clients_with_guidelines
from ..llm import build_engine
from ..observability import RunTrace
from ..runstore import STORE, StoredRun
from .schemas import CreativeRequest, DecisionRequest, ReportRequest

CONSOLE_DIR = Path(__file__).resolve().parent / "static"

log = logging.getLogger(__name__)

app = FastAPI(
    title="AgencyOps Orchestrator",
    version="0.1.0",
    description=(
        "Agentic automation for an eCommerce marketing agency. Workflows stage "
        "external writes as reviewable effects and halt for human approval "
        "before anything reaches a client."
    ),
)


def _fail(trace: RunTrace) -> None:
    """Mark a run failed and keep its trace, without masking the failure.

    Called while a workflow error is already on its way out, so a trace that
    cannot be written is logged rather than raised over the real problem.
    """
    trace.finish("failed")
    try:
        trace.persist()
    except Exception:  # noqa: BLE001 - never mask the workflow's own failure
        log.exception("could not persist trace for failed run %s", trace.run_id)


def _invoke(graph: Any, initial: dict[str, Any], trace: RunTrace) -> dict[str, Any]:
    try:
        return graph.invoke(initial)
    except Exception:
        _fail(trace)
        raise


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/console/")


@app.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "connector_mode": s.connector_mode,
        "llm_engine": build_engine(s).name,
        "llm_model": s.llm_model,
        "human_approval_required": s.require_human_approval,
    }


@app.get("/clients")
def clients() -> list[dict[str, Any]]:
    """The client roster the console offers, and what each one can run."""
    creative_ready = set(clients_with_guidelines())
    return [
        {"slug": slug, "name": name, "creative_enabled": slug in creative_ready}
        for slug, name in sorted(CLIENT_LABELS.items(), key=lambda kv: kv[1])
    ]


@app.post("/workflows/client-report")
def client_report(req: ReportRequest) -> dict[str, Any]:
    settings = get_settings()
    bundle = build_bundle(settings)
    graph = build_report_graph(bundle=bundle, engine=build_engine(settings), settings=settings)
    trace = RunTrace(workflow="client_report")

    state = _invoke(
        graph,
        {
            "client": req.client,
            "window_days": req.window_days,
            "channel": req.channel or settings.slack_default_channel,
            "trace": trace,
            "findings": [],
            "effects": [],
            "errors": [],
        },
        trace,
    )

    if state.get("errors"):
        _fail(trace)
        raise HTTPException(status_code=404, detail=state["errors"][0])

    trace.finish(run_status(state.get("effects", [])))
    run = STORE.add(
        StoredRun(
            run_id=trace.run_id,
            workflow="client_report",
            state=state,
            bundle=bundle,
            trace=trace,
            label=state.get("client_name", req.client),
            # Artifacts are what a reviewer needs to re-open the run cold, so
            # the computed figures travel with the document rather than only
            # in the response to whoever triggered it.
            artifacts={
                "report_markdown": state.get("report_markdown", ""),
                "totals": state.get("totals"),
                "deltas": state.get("deltas"),
                "findings": state.get("findings", []),
            },
        )
    )
    return {
        **run.summary(),
        "totals": state.get("totals"),
        "deltas": state.get("deltas"),
        "findings": state.get("findings"),
    }


@app.post("/workflows/creative")
def creative(req: CreativeRequest) -> dict[str, Any]:
    settings = get_settings()
    bundle = build_bundle(settings)
    graph = build_creative_graph(bundle=bundle, engine=build_engine(settings), settings=settings)
    trace = RunTrace(workflow="creative_pipeline")

    state = _invoke(
        graph,
        {
            **req.model_dump(),
            "trace": trace,
            "effects": [],
            "errors": [],
        },
        trace,
    )

    if state.get("errors"):
        _fail(trace)
        raise HTTPException(status_code=400, detail=state["errors"][0])

    trace.finish(run_status(state.get("effects", [])))
    run = STORE.add(
        StoredRun(
            run_id=trace.run_id,
            workflow="creative_pipeline",
            state=state,
            bundle=bundle,
            trace=trace,
            label=f"{req.client} / {req.product}",
            artifacts={
                "approved_variants": state.get("approved_variants", []),
                "rejected_variants": state.get("rejected_variants", []),
                "revision_rounds": state.get("revision_round", 0),
            },
        )
    )
    return {
        **run.summary(),
        "approved_variants": state.get("approved_variants", []),
        "rejected_variants": state.get("rejected_variants", []),
        "revision_rounds": state.get("revision_round", 0),
    }


@app.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    return [r.summary() for r in STORE.list()]


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        run = STORE.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No run {run_id!r}") from None
    return {**run.summary(), "trace": run.trace.to_dict(), "artifacts": run.artifacts}


@app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str) -> str:
    try:
        run = STORE.get(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No run {run_id!r}") from None
    md = run.artifacts.get("report_markdown")
    if not md:
        raise HTTPException(status_code=404, detail="This run produced no report document")
    return md


@app.post("/runs/{run_id}/decision")
def decide(run_id: str, req: DecisionRequest) -> dict[str, Any]:
    try:
        result = STORE.decide(run_id, req.decision, req.effect_indexes)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No run {run_id!r}") from None
    except IndexError:
        raise HTTPException(status_code=400, detail="effect_indexes out of range") from None
    except ValueError as exc:
        # The request is well-formed but conflicts with where the run stands.
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {**result, **STORE.get(run_id).summary()}


# --------------------------------------------------------------------------
# Review console
#
# Mounted last so it cannot shadow an API route. Plain files, no build step:
# the console has to run on the same terms as everything else - no npm, no
# CDN, no credentials.
# --------------------------------------------------------------------------
app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")
