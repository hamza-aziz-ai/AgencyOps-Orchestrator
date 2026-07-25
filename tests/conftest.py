from __future__ import annotations

import pytest

from agencyops.config import Settings
from agencyops.connectors import build_bundle
from agencyops.llm import OfflineEngine


@pytest.fixture
def settings() -> Settings:
    """Offline, mock connectors, approval gate ON - the safe default."""
    return Settings(connector_mode="mock", gemini_api_key=None, require_human_approval=True)


@pytest.fixture
def auto_settings() -> Settings:
    """Approval gate OFF - used to test the straight-through path."""
    return Settings(connector_mode="mock", gemini_api_key=None, require_human_approval=False)


@pytest.fixture
def bundle(settings):
    return build_bundle(settings)


@pytest.fixture
def engine():
    return OfflineEngine()
