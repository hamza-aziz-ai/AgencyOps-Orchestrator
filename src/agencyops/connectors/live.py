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
        self._accounts = dict(settings.meta_ad_accounts)

    def _account(self, client: str) -> str:
        if client not in self._accounts:
            raise KeyError(f"Unknown client {client!r} in Meta Ads")
        return self._accounts[client]

    def _insights(self, account: str, since: date, until: date) -> list[AdMetrics]:
        params = {
            "access_token": self._token,
            "level": "campaign",
            "time_range": f'{{"since":"{since:%Y-%m-%d}","until":"{until:%Y-%m-%d}"}}',
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,actions,action_values",
        }
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(f"{self.BASE}/act_{account}/insights", params=params)
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
        account = self._account(client)
        until = date.today()
        return self._insights(account, until - timedelta(days=window_days), until)

    def fetch_previous_metrics(self, client: str, window_days: int = 7) -> list[AdMetrics]:
        account = self._account(client)
        until = date.today() - timedelta(days=window_days)
        return self._insights(account, until - timedelta(days=window_days), until)


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
        self._client_ids = dict(settings.harvest_client_ids)

    def _client_id(self, client: str) -> int:
        # Harvest filters by its numeric client_id, not by our slug.
        if client not in self._client_ids:
            raise KeyError(f"Unknown client {client!r} in Harvest")
        return self._client_ids[client]

    def fetch_entries(self, client: str, window_days: int = 7) -> list[TimeEntry]:
        params = {
            "from": f"{date.today() - timedelta(days=window_days):%Y-%m-%d}",
            "client_id": self._client_id(client),
        }
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
        client_id = self._client_id(client)
        entries = self.fetch_entries(client, window_days=30)
        used = sum(e.hours for e in entries)
        # Contracted hours live on the project budget in Harvest.
        with httpx.Client(timeout=_TIMEOUT, headers=self._headers) as http:
            resp = http.get(f"{self.BASE}/projects", params={"client_id": client_id})
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
            result = resp.json()
        # Slack reports most failures as HTTP 200 with ok:false. Anything but an
        # explicit ok:true is a failed post, never an executed one.
        if result.get("ok") is not True:
            raise RuntimeError(f"Slack chat.postMessage failed: {result.get('error', 'no ok flag')}")
        return result


class TrelloConnector(WriteConnector):
    name = "trello"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.trello_key
        self._token = settings.trello_token
        self._lists = dict(settings.trello_list_ids)

    def execute(self, effect: Effect) -> dict[str, Any]:
        if effect.action != "create_card":
            raise ValueError(f"Trello connector cannot perform {effect.action!r}")
        list_name = effect.payload["list_id"]
        if list_name not in self._lists:
            raise KeyError(f"No Trello list id configured for {list_name!r}")
        params = {
            "key": self._key,
            "token": self._token,
            "idList": self._lists[list_name],
            "name": effect.payload["name"],
            "desc": effect.payload.get("description", ""),
        }
        with httpx.Client(timeout=_TIMEOUT) as http:
            resp = http.post("https://api.trello.com/1/cards", params=params)
            resp.raise_for_status()
            return resp.json()
