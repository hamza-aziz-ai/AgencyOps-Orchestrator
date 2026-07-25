"""Fixture-backed connectors.

These are not throwaway stubs. They implement the same protocols as the live
clients, return the same dataclasses, and enforce the same failure modes
(unknown client -> KeyError). That means the entire test suite and the demo
exercise the real graph logic, and swapping to live credentials changes
nothing above this layer.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .base import (
    AdMetrics,
    Effect,
    RetainerStatus,
    TimeEntry,
    WriteConnector,
)

FIXTURES = Path(__file__).resolve().parents[3] / "data" / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


class MockMetaAds:
    name = "meta_ads"

    def __init__(self) -> None:
        self._data = _load("meta_ads.json")

    def _read(self, client: str, bucket: str) -> list[AdMetrics]:
        if client not in self._data:
            raise KeyError(f"Unknown client {client!r} in Meta Ads")
        return [AdMetrics(**row) for row in self._data[client][bucket]]

    def fetch_metrics(self, client: str, window_days: int = 7) -> list[AdMetrics]:
        return self._read(client, "current")

    def fetch_previous_metrics(self, client: str, window_days: int = 7) -> list[AdMetrics]:
        return self._read(client, "previous")


class MockHarvest:
    name = "harvest"

    def __init__(self) -> None:
        self._data = _load("harvest.json")

    def _client(self, client: str) -> dict[str, Any]:
        if client not in self._data:
            raise KeyError(f"Unknown client {client!r} in Harvest")
        return self._data[client]

    def fetch_entries(self, client: str, window_days: int = 7) -> list[TimeEntry]:
        rows = self._client(client)["entries"]
        start = date.today() - timedelta(days=window_days)
        return [
            TimeEntry(
                client=client,
                task=r["task"],
                hours=r["hours"],
                person=r["person"],
                entry_date=start + timedelta(days=i % max(window_days, 1)),
            )
            for i, r in enumerate(rows)
        ]

    def fetch_retainer(self, client: str) -> RetainerStatus:
        r = self._client(client)["retainer"]
        return RetainerStatus(
            client=client,
            contracted_hours=r["contracted_hours"],
            used_hours=r["used_hours"],
        )


class _RecordingWriter(WriteConnector):
    """Captures writes instead of performing them, and keeps a log.

    The log is what the tests assert against and what the demo prints, so a
    reviewer can see exactly what would have hit the client's Slack channel.
    """

    def __init__(self) -> None:
        self.log: list[Effect] = []

    def execute(self, effect: Effect) -> dict[str, Any]:
        self.log.append(effect)
        return {"ok": True, "mode": "mock", "connector": self.name, "action": effect.action}


class MockSlack(_RecordingWriter):
    name = "slack"

    def execute(self, effect: Effect) -> dict[str, Any]:
        out = super().execute(effect)
        out["channel"] = effect.payload.get("channel", "#general")
        out["ts"] = f"mock-ts-{len(self.log)}"
        return out


class MockTrello(_RecordingWriter):
    name = "trello"

    def execute(self, effect: Effect) -> dict[str, Any]:
        out = super().execute(effect)
        out["card_id"] = f"mock-card-{len(self.log)}"
        out["card_url"] = f"https://trello.com/c/mock-card-{len(self.log)}"
        return out


class MockEmail(_RecordingWriter):
    name = "email"

    def execute(self, effect: Effect) -> dict[str, Any]:
        out = super().execute(effect)
        out["message_id"] = f"mock-msg-{len(self.log)}"
        return out
