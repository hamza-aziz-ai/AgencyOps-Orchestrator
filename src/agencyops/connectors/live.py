"""Live connector implementations.

Deliberately thin. Each one wraps a documented REST endpoint with httpx,
maps the vendor payload onto our dataclasses, and stops there. All retry,
approval and orchestration concerns live above this layer.

These are wired but unexercised in the offline demo - CONNECTOR_MODE=mock is
the default. They exist so the prototype is a migration path, not a dead end.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx

from ..config import Settings
from .base import AdMetrics, Effect, RetainerStatus, TimeEntry, WriteConnector

_TIMEOUT = httpx.Timeout(20.0)


class MetaAdsConnector:
    name = "meta_ads"
    BASE = "https://graph.facebook.com/v21.0"

    def __init__(self, settings: Settings) -> None:
        self._token = settings.meta_access_token
        self._account = settings.meta_ad_account_id

    def _insights(self, since: date, until: date) -> list[AdMetrics]:
        params = {
            "access_token": self._token,
            "level": "campaign",
            "time_range": f'{{"since":"{since:%Y-%m-%d}","until":"{until:%Y-%m-%d}"}}',
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,actions,action_values",
        }
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"{self.BASE}/act_{self._account}/insights", params=params)
            resp.raise_for_status()
            rows = resp.json().get("data", [])

        out: list[AdMetrics] = []
        for row in rows:
            out.append(
                AdMetrics(
                    campaign_id=row["campaign_id"],
                    campaign_name=row["campaign_name"],
                    spend=float(row.get("spend", 0)),
                    impressions=int(row.get("impressions", 0)),
                    clicks=int(row.get("clicks", 0)),
                    conversions=_sum_action(row.get("actions"), "purchase"),
                    revenue=_sum_action(row.get("action_values"), "purchase"),
                )
            )
        return out

    def fetch_metrics(self, client: str, window_days: int = 7) -> list[AdMetrics]:
        until = date.today()
        return self._insights(until - timedelta(days=window_days), until)

    def fetch_previous_metrics(self, client: str, window_days: int = 7) -> list[AdMetrics]:
        until = date.today() - timedelta(days=window_days)
        return self._insights(until - timedelta(days=window_days), until)


def _sum_action(actions: list[dict[str, Any]] | None, action_type: str) -> float:
    if not actions:
        return 0.0
    return sum(float(a.get("value", 0)) for a in actions if a.get("action_type") == action_type)


class HarvestConnector:
    name = "harvest"
    BASE = "https://api.harvestapp.com/v2"

    def __init__(self, settings: Settings) -> None:
        self._headers = {
            "Authorization": f"Bearer {settings.harvest_access_token}",
            "Harvest-Account-Id": str(settings.harvest_account_id or ""),
            "User-Agent": "AgencyOps Orchestrator",
        }

    def fetch_entries(self, client: str, window_days: int = 7) -> list[TimeEntry]:
        since = date.today() - timedelta(days=window_days)
        params = {"from": f"{since:%Y-%m-%d}", "client": client}
        with httpx.Client(timeout=_TIMEOUT, headers=self._headers) as http:
            resp = http.get(f"{self.BASE}/time_entries", params=params)
            resp.raise_for_status()
            rows = resp.json().get("time_entries", [])

        return [
            TimeEntry(
                client=client,
                task=r.get("task", {}).get("name", "Unspecified"),
                hours=float(r.get("hours", 0)),
                person=r.get("user", {}).get("name", "Unknown"),
                entry_date=datetime.fromisoformat(r["spent_date"]).date(),
            )
            for r in rows
        ]

    def fetch_retainer(self, client: str) -> RetainerStatus:
        entries = self.fetch_entries(client, window_days=30)
        used = sum(e.hours for e in entries)
        # Contracted hours live on the project budget in Harvest.
        with httpx.Client(timeout=_TIMEOUT, headers=self._headers) as http:
            resp = http.get(f"{self.BASE}/projects", params={"client": client})
            resp.raise_for_status()
            projects = resp.json().get("projects", [])
        contracted = sum(float(p.get("budget") or 0) for p in projects)
        return RetainerStatus(client=client, contracted_hours=contracted, used_hours=used)


class SlackConnector(WriteConnector):
    name = "slack"

    def __init__(self, settings: Settings) -> None:
        self._token = settings.slack_bot_token
        self._default_channel = settings.slack_default_channel

    def execute(self, effect: Effect) -> dict[str, Any]:
        if effect.action != "post_message":
            raise ValueError(f"Slack connector cannot perform {effect.action!r}")
        body = {
            "channel": effect.payload.get("channel", self._default_channel),
            "text": effect.payload["text"],
        }
        with httpx.Client(timeout=_TIMEOUT) as http:
            resp = http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self._token}"},
                json=body,
            )
            resp.raise_for_status()
            return resp.json()


class TrelloConnector(WriteConnector):
    name = "trello"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.trello_key
        self._token = settings.trello_token

    def execute(self, effect: Effect) -> dict[str, Any]:
        if effect.action != "create_card":
            raise ValueError(f"Trello connector cannot perform {effect.action!r}")
        params = {
            "key": self._key,
            "token": self._token,
            "idList": effect.payload["list_id"],
            "name": effect.payload["name"],
            "desc": effect.payload.get("description", ""),
        }
        with httpx.Client(timeout=_TIMEOUT) as http:
            resp = http.post("https://api.trello.com/1/cards", params=params)
            resp.raise_for_status()
            return resp.json()
