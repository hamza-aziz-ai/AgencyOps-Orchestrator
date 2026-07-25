"""Connector registry - resolves the bundle for the configured mode."""
from __future__ import annotations

from ..config import Settings, get_settings
from .base import (
    AdMetrics,
    AdsConnector,
    ConnectorBundle,
    Effect,
    RetainerStatus,
    TimeEntry,
    TimeTrackingConnector,
    WriteConnector,
)
from .mock import MockEmail, MockHarvest, MockMetaAds, MockSlack, MockTrello

__all__ = [
    "AdMetrics",
    "AdsConnector",
    "ConnectorBundle",
    "Effect",
    "RetainerStatus",
    "TimeEntry",
    "TimeTrackingConnector",
    "WriteConnector",
    "build_bundle",
]


def build_bundle(settings: Settings | None = None) -> ConnectorBundle:
    settings = settings or get_settings()

    if settings.connector_mode == "live":
        from .live import HarvestConnector, MetaAdsConnector, SlackConnector, TrelloConnector

        return ConnectorBundle(
            ads=MetaAdsConnector(settings),
            time_tracking=HarvestConnector(settings),
            writers={
                "slack": SlackConnector(settings),
                "trello": TrelloConnector(settings),
            },
        )

    return ConnectorBundle(
        ads=MockMetaAds(),
        time_tracking=MockHarvest(),
        writers={"slack": MockSlack(), "trello": MockTrello(), "email": MockEmail()},
    )
