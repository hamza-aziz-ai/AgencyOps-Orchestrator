"""Engine resolution and the degradation policy.

No network anywhere: the remote engines are exercised through a local
subclass of RemoteEngine, which is the same dependency-injection approach the
rest of the suite uses for connectors.
"""
from __future__ import annotations

from typing import Any

from agencyops.config import Settings
from agencyops.llm import (
    OfflineEngine,
    RemoteEngine,
    _strip_reasoning,
    build_engine,
)


class _Boom(RemoteEngine):
    name = "boom"

    def _generate(self, task: str, prompt: str, context: dict[str, Any]) -> str:
        raise RuntimeError("provider is down")


class _Empty(RemoteEngine):
    name = "empty"

    def _generate(self, task: str, prompt: str, context: dict[str, Any]) -> str:
        return "   "


class _Fine(RemoteEngine):
    name = "fine"

    def _generate(self, task: str, prompt: str, context: dict[str, Any]) -> str:
        return "  real model output  "


REPORT_CTX = {
    "client_name": "Nova Retail",
    "totals": {"spend": 1000.0, "revenue": 4000.0, "roas": 4.0, "conversions": 20,
               "cpa": 50.0, "campaign_count": 2},
    "deltas": {"roas_pct": -12.0},
    "findings": [],
    "retainer": {"used_hours": 10.0, "contracted_hours": 20.0, "utilisation": 50.0,
                 "over_budget": False},
}


# --------------------------------------------------------------------------
# Provider resolution
# --------------------------------------------------------------------------
def test_auto_without_a_key_is_offline():
    s = Settings(llm_provider="auto", gemini_api_key=None)
    assert s.resolved_llm_provider == "offline"
    assert s.llm_available is False
    assert build_engine(s).name == "offline"


def test_auto_with_a_key_selects_gemini():
    s = Settings(llm_provider="auto", gemini_api_key="sk-test")
    assert s.resolved_llm_provider == "gemini"
    assert s.llm_available is True


def test_ollama_is_never_selected_implicitly():
    """A daemon running on the box must not silently become a dependency."""
    s = Settings(llm_provider="auto", gemini_api_key="sk-test", ollama_model="gpt-oss:120b-cloud")
    assert s.resolved_llm_provider != "ollama"


def test_ollama_requires_opting_in():
    s = Settings(llm_provider="ollama")
    assert s.resolved_llm_provider == "ollama"
    assert s.llm_available is True


def test_explicit_offline_overrides_a_present_key():
    s = Settings(llm_provider="offline", gemini_api_key="sk-test")
    assert build_engine(s).name == "offline"


def test_unbuildable_engine_falls_back_at_startup():
    """A bad host is a config error, not a reason to have no reporting."""
    s = Settings(llm_provider="ollama", ollama_host="not a url")
    assert build_engine(s).name in ("ollama", "offline")


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------
def test_a_failing_provider_degrades_to_offline_prose():
    resp = _Boom().complete("report_narrative", "prompt", REPORT_CTX)
    assert resp.engine == "offline"
    assert "Nova Retail" in resp.text
    assert "4.00" in resp.text  # grounded in the supplied figures, not invented


def test_empty_model_output_counts_as_a_failure():
    """Whitespace back from a provider is an outage, not a report."""
    resp = _Empty().complete("report_narrative", "prompt", REPORT_CTX)
    assert resp.engine == "offline"
    assert resp.text.strip()


def test_a_healthy_provider_is_reported_as_itself():
    resp = _Fine().complete("report_narrative", "prompt", REPORT_CTX)
    assert resp.engine == "fine"
    assert resp.text == "real model output"


def test_degradation_keeps_creative_output_parseable():
    """The creative pipeline parses JSON; a degraded call must still supply it."""
    ctx = {"product": "Nova Winter Edit", "audience": "returning customers",
           "key_benefit": "next-day delivery", "cta": "Shop now", "variant_count": 3}
    resp = _Boom().complete("ad_copy", "prompt", ctx)
    variants = resp.as_json()
    assert len(variants) == 3
    assert all({"headline", "body"} <= set(v) for v in variants)


# --------------------------------------------------------------------------
# Reasoning-model output
# --------------------------------------------------------------------------
def test_inline_reasoning_is_stripped_before_parsing():
    raw = '<think>The user wants JSON.\nLet me plan.</think>\n[{"headline": "Hi", "body": "There"}]'
    assert _strip_reasoning(raw).startswith("[")


def test_stripping_leaves_ordinary_output_alone():
    assert _strip_reasoning("  plain prose  ") == "plain prose"


def test_offline_engine_handles_every_registered_task():
    """A task with no offline handler would break the no-credentials guarantee."""
    from agencyops.llm import SYSTEM_PROMPTS

    engine = OfflineEngine()
    for task in SYSTEM_PROMPTS:
        assert hasattr(engine, f"_task_{task}"), f"no offline handler for {task!r}"
