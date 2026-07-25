from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ReportRequest(BaseModel):
    client: str = Field(..., examples=["nova-retail"])
    window_days: int = Field(7, ge=1, le=90)
    channel: str | None = Field(None, examples=["#nova-retail-reporting"])


class CreativeRequest(BaseModel):
    client: str = Field(..., examples=["atlas-fitness"])
    product: str = Field(..., examples=["Atlas Annual Membership"])
    audience: str = Field(..., examples=["first-time gym joiners in Dubai"])
    key_benefit: str = Field(..., examples=["a coached start, not a cold treadmill"])
    cta: str = "Start today"
    variant_count: int = Field(4, ge=1, le=10)


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    effect_indexes: list[int] | None = Field(
        None, description="Omit to apply the decision to every staged effect."
    )


class RunSummary(BaseModel):
    run_id: str
    workflow: str
    label: str
    status: str
    pending_approval: bool
    effects: list[dict[str, Any]]
    errors: list[str]
