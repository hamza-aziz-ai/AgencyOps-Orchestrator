from __future__ import annotations

import os

# Set before anything constructs Settings. A real environment variable beats a
# .env entry in pydantic-settings, so this holds the suite offline even in a
# checkout configured for Ollama or Gemini. The API tests exercise the app
# through its own get_settings(), so pinning only the fixtures below would
# leave those free to reach the network.
os.environ["LLM_PROVIDER"] = "offline"
os.environ["CONNECTOR_MODE"] = "mock"

import pytest  # noqa: E402

from agencyops.config import Settings  # noqa: E402
from agencyops.connectors import build_bundle  # noqa: E402
from agencyops.llm import OfflineEngine  # noqa: E402


# llm_provider is pinned rather than left to resolve: Settings reads .env, and
# a developer who has switched their own checkout to Ollama must not thereby
# point the test suite at a network service.
OFFLINE = {"connector_mode": "mock", "gemini_api_key": None, "llm_provider": "offline"}


@pytest.fixture
def settings() -> Settings:
    """Offline, mock connectors, approval gate ON - the safe default."""
    return Settings(**OFFLINE, require_human_approval=True)


@pytest.fixture
def auto_settings() -> Settings:
    """Approval gate OFF - used to test the straight-through path."""
    return Settings(**OFFLINE, require_human_approval=False)


@pytest.fixture
def bundle(settings):
    return build_bundle(settings)


@pytest.fixture
def engine():
    return OfflineEngine()
