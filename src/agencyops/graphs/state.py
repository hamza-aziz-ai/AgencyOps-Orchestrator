"""Shared graph state definitions."""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from ..connectors.base import AdMetrics, Effect, RetainerStatus, TimeEntry
from ..observability import RunTrace


def _append(left: list[Any], right: list[Any]) -> list[Any]:
    return (left or []) + (right or [])


class ReportState(TypedDict, total=False):
    # inputs
    client: str
    client_name: str
    window_days: int
    channel: str

    # gathered
    current: list[AdMetrics]
    previous: list[AdMetrics]
    time_entries: list[TimeEntry]
    retainer: RetainerStatus

    # derived
    totals: dict[str, Any]
    deltas: dict[str, Any]
    findings: Annotated[list[dict[str, Any]], _append]
    narrative: str
    # Replaced wholesale each round, not accumulated: the current verdict on
    # the current draft is the only one that means anything.
    sign_violations: list[str]
    narration_round: int
    report_markdown: str

    # governance
    effects: Annotated[list[Effect], _append]
    approval_required: bool
    approved: bool

    # meta
    trace: RunTrace
    errors: Annotated[list[str], _append]


class CreativeState(TypedDict, total=False):
    # inputs
    client: str
    product: str
    audience: str
    key_benefit: str
    cta: str
    variant_count: int
    trello_list_id: str

    # derived
    guidelines: dict[str, Any]
    variants: list[dict[str, Any]]
    scored: list[dict[str, Any]]
    revision_round: int
    approved_variants: list[dict[str, Any]]
    rejected_variants: list[dict[str, Any]]

    # governance
    effects: Annotated[list[Effect], _append]
    approval_required: bool
    approved: bool

    # meta
    trace: RunTrace
    errors: Annotated[list[str], _append]
