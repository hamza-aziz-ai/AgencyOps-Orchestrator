"""Connector contracts.

The point of this layer: every workflow talks to an *interface*, never to a
vendor SDK. Swapping the mock Meta Ads client for the live one is a config
change, not a code change - which is what makes the graphs testable and what
lets an agency migrate off a tool without rewriting its automations.

Writes are modelled explicitly as `Effect` objects rather than executed
inline. A workflow *proposes* effects; the dispatcher executes them only
after the approval gate clears. This is the difference between an automation
you can trust with a client-facing Slack channel and one you can't.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol


# --------------------------------------------------------------------------
# Read models
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AdMetrics:
    """One campaign's performance over a window."""

    campaign_id: str
    campaign_name: str
    spend: float
    impressions: int
    clicks: int
    conversions: int
    revenue: float
    currency: str = "AED"

    @property
    def ctr(self) -> float:
        return (self.clicks / self.impressions * 100) if self.impressions else 0.0

    @property
    def cpa(self) -> float:
        return (self.spend / self.conversions) if self.conversions else 0.0

    @property
    def roas(self) -> float:
        return (self.revenue / self.spend) if self.spend else 0.0


@dataclass(frozen=True)
class TimeEntry:
    """A block of tracked agency time."""

    client: str
    task: str
    hours: float
    person: str
    entry_date: date


@dataclass(frozen=True)
class RetainerStatus:
    client: str
    contracted_hours: float
    used_hours: float

    @property
    def utilisation(self) -> float:
        return (self.used_hours / self.contracted_hours * 100) if self.contracted_hours else 0.0

    @property
    def over_budget(self) -> bool:
        return self.used_hours > self.contracted_hours


# --------------------------------------------------------------------------
# Write model
# --------------------------------------------------------------------------
@dataclass
class Effect:
    """A proposed write to an external system.

    Never executed at creation time. Collected, surfaced for approval,
    then dispatched. Carries enough context to be rendered for a human.
    """

    connector: str
    action: str
    payload: dict[str, Any]
    summary: str
    reversible: bool = True
    status: Literal["proposed", "approved", "rejected", "executed", "failed"] = "proposed"
    result: dict[str, Any] | None = None

    def render(self) -> str:
        flag = "" if self.reversible else "  [IRREVERSIBLE]"
        return f"[{self.connector}.{self.action}] {self.summary}{flag}"


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------
class AdsConnector(Protocol):
    name: str

    def fetch_metrics(self, client: str, window_days: int) -> list[AdMetrics]: ...
    def fetch_previous_metrics(self, client: str, window_days: int) -> list[AdMetrics]: ...


class TimeTrackingConnector(Protocol):
    name: str

    def fetch_entries(self, client: str, window_days: int) -> list[TimeEntry]: ...
    def fetch_retainer(self, client: str) -> RetainerStatus: ...


class WriteConnector(abc.ABC):
    """Any connector that can mutate an external system."""

    name: str

    @abc.abstractmethod
    def execute(self, effect: Effect) -> dict[str, Any]:
        """Perform the write. Only ever called by the dispatcher."""


@dataclass
class ConnectorBundle:
    """Everything the graphs are allowed to touch, injected at build time."""

    ads: AdsConnector
    time_tracking: TimeTrackingConnector
    writers: dict[str, WriteConnector] = field(default_factory=dict)

    def writer(self, name: str) -> WriteConnector:
        if name not in self.writers:
            raise KeyError(
                f"No write connector registered for {name!r}. "
                f"Available: {sorted(self.writers)}"
            )
        return self.writers[name]
