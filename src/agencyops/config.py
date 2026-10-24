"""Central configuration.

Everything is environment-driven so the same build runs in three modes:
  * offline demo   - no credentials, deterministic fixtures + stub LLM
  * staging        - real LLM, mock connectors
  * production     - real LLM, live connectors
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

NonEmptyStr = Annotated[str, Field(min_length=1)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM
    #
    # "auto" resolves to Gemini when a key is present and the offline engine
    # otherwise. It deliberately never resolves to Ollama: a workflow must not
    # start reaching a network service because a daemon happens to be running
    # on the box. Opting in is one line in .env, and the test suite pins this
    # to "offline" so the suite stays deterministic wherever it runs.
    llm_provider: Literal["auto", "ollama", "gemini", "offline"] = "auto"

    ollama_model: str = "gpt-oss:120b-cloud"
    ollama_host: str | None = None  # None -> the client's default, localhost:11434
    ollama_timeout_s: float = 120.0

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.0-flash"

    # Connectors
    connector_mode: Literal["mock", "live"] = "mock"

    # Live connectors map our client slugs (and the graphs' symbolic Trello
    # list names) onto vendor ids, as JSON in the environment. There is no
    # single-account fallback: an unmapped client fails before any request
    # rather than reading another client's account.
    meta_access_token: str | None = None
    meta_ad_accounts: dict[str, NonEmptyStr] = {}

    harvest_account_id: str | None = None
    harvest_access_token: str | None = None
    harvest_client_ids: dict[str, PositiveInt] = {}

    trello_key: str | None = None
    trello_token: str | None = None
    trello_list_ids: dict[str, NonEmptyStr] = {}

    slack_bot_token: str | None = None
    slack_default_channel: str = "#client-reporting"

    # Governance
    require_human_approval: bool = True

    @property
    def llm_available(self) -> bool:
        """Whether a remote generation engine is configured at all."""
        return self.resolved_llm_provider in ("ollama", "gemini")

    @property
    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        return "gemini" if self.gemini_api_key else "offline"

    @property
    def llm_model(self) -> str | None:
        """The model the configured provider would use, for display and traces."""
        return {
            "ollama": self.ollama_model,
            "gemini": self.gemini_model,
        }.get(self.resolved_llm_provider)


@lru_cache
def get_settings() -> Settings:
    return Settings()
