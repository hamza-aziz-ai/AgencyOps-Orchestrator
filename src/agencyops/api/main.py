"""HTTP surface.

Three concerns only: trigger a workflow, inspect a run, release staged
effects. Business logic lives in the graphs - this layer is transport.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from ..config import get_settings
from ..connectors import build_bundle
from ..graphs.client_report import build_report_graph
from ..graphs.creative_pipeline import build_creative_graph
from ..llm import build_engine
from ..observability import RunTrace
from ..runstore import STORE, StoredRun
from .schemas import CreativeRequest, DecisionRequest, ReportRequest

app = FastAPI(
    title="AgencyOps Orchestrator",
    version="0.1.0",
    description=(
        "Agentic automation for an eCommerce marketing agency. Workflows stage "
        "external writes as reviewable effects and halt for human approval "
        "before anything reaches a client."
    ),
)


@app.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "connector_mode": s.connector_mode,
        "llm_engine": build_engine(s).name,
        "human_approval_required": s.require_human_approval,
    }


@app.post("/workflows/client-report")
def client_report(req: ReportRequest) -> dict[str, Any]:
    settings = get_settings()
    bundle = build_bundle(settings)
    graph = build_report_graph(bundle=bundle, engine=build_engine(settings), settings=settings)
    trace = RunTrace(workflow="client_report")

    state = graph.invoke(
        {
            "client": req.client,
            "window_days": req.window_days,
            "channel": req.channel or settings.slack_default_channel,
            "trace": trace,
            "findings": [],
            "effects": [],
            "errors": [],
        }
    )

    if state.get("errors"):
        trace.finish("failed")
        raise HTTPException(status_code=404, detail=state["errors"][0])

    trace.finish(
        "blocked_on_approval"
        if state.get("approval_required") and not state.get("approved")
        else "completed"
    )
    run = STORE.add(
        StoredRun(
            run_id=trace.run_id,
            workflow="client_report",
            state=state,
            bundle=bundle,
            trace=trace,
            label=state.get("client_name", req.client),
            artifacts={"report_markdown": state.get("report_markdown", "")},
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

    state = graph.invoke(
        {
            **req.model_dump(),
            "trace": trace,
            "effects": [],
            "errors": [],
        }
    )

    if state.get("errors"):
        trace.finish("failed")
        raise HTTPException(status_code=400, detail=state["errors"][0])

    trace.finish(
        "blocked_on_approval"
        if state.get("approval_required") and not state.get("approved")
        else "completed"
    )
    run = STORE.add(
        StoredRun(
            run_id=trace.run_id,
            workflow="creative_pipeline",
            state=state,
            bundle=bundle,
            trace=trace,
            label=f"{req.client} / {req.product}",
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
    return {**result, **STORE.get(run_id).summary()}
