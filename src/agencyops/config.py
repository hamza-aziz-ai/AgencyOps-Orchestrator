"""Central configuration.

Everything is environment-driven so the same build runs in three modes:
  * offline demo   - no credentials, deterministic fixtures + stub LLM
  * staging        - real LLM, mock connectors
  * production     - real LLM, live connectors
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.0-flash"

    # Connectors
    connector_mode: Literal["mock", "live"] = "mock"

    meta_access_token: str | None = None
    meta_ad_account_id: str | None = None

    harvest_account_id: str | None = None
    harvest_access_token: str | None = None

    trello_key: str | None = None
    trello_token: str | None = None
    trello_board_id: str | None = None

    slack_bot_token: str | None = None
    slack_default_channel: str = "#client-reporting"

    # Governance
    require_human_approval: bool = True

    @property
    def llm_available(self) -> bool:
        return bool(self.gemini_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
