"""LLM access layer.

Three engines behind one interface:

  OllamaEngine   - real generation via Ollama (gpt-oss by default)
  GeminiEngine   - real generation via google-genai
  OfflineEngine  - deterministic, template-driven, zero-dependency

The offline engine is not a mock that returns "lorem ipsum". It produces
genuine, data-grounded output for every task the graphs need. That buys three
things: the demo runs on any machine with no API key, the test suite is
deterministic, and an LLM outage degrades the system to templated output
rather than taking reporting down entirely.

That third property is enforced here rather than hoped for: both remote
engines degrade to the offline engine on any failure, per call. A model
provider going down must not take weekly client reporting with it, and a node
that raises on an expected failure would strand a run with no report and no
trace of why.

Every call is tagged with a `task` label. The offline engine routes on it;
the remote engines use it to select the system prompt. Same contract either
way, so graph code never branches on which engine is active.
"""
from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings, get_settings

log = logging.getLogger(__name__)


SYSTEM_PROMPTS: dict[str, str] = {
    "report_narrative": (
        "You are a senior performance marketing strategist at an eCommerce agency, "
        "writing the commentary section of a weekly client report. Write for a "
        "commercially literate client who is not a media buyer. Lead with what "
        "changed and why it matters commercially. Be specific with numbers. "
        "Never invent a figure that is not in the supplied data. If performance "
        "declined, say so plainly and state the corrective action. "
        "Three short paragraphs, no headings, no bullet points."
    ),
    "ad_copy": (
        "You are a direct-response copywriter. Produce ad copy variants as strict "
        "JSON: a list of objects with keys 'headline' and 'body'. No prose, no "
        "markdown fences, no commentary outside the JSON."
    ),
    "copy_revision": (
        "You are a direct-response copywriter revising ad copy to satisfy brand "
        "and compliance rules. Return strict JSON with keys 'headline' and "
        "'body'. Preserve the persuasive intent; fix only what violates the rules."
    ),
}


@dataclass
class LLMResponse:
    text: str
    engine: str
    task: str

    def as_json(self) -> Any:
        """Parse JSON out of a completion, tolerating fenced code blocks."""
        cleaned = re.sub(r"^```(?:json)?|```$", "", self.text.strip(), flags=re.MULTILINE)
        return json.loads(cleaned.strip())


class LLMEngine(Protocol):
    name: str

    def complete(self, task: str, prompt: str, context: dict[str, Any]) -> LLMResponse: ...


def system_prompt(task: str) -> str:
    return SYSTEM_PROMPTS.get(task, "You are a precise, concise assistant.")


# --------------------------------------------------------------------------
# Remote engines
# --------------------------------------------------------------------------
class RemoteEngine(abc.ABC):
    """Base for engines that depend on a service outside this process.

    Subclasses implement `_generate`. Everything else here is the degradation
    policy, kept in one place so a new provider cannot accidentally ship
    without it.

    The returned `LLMResponse` carries the engine that actually produced the
    text, not the one that was configured. The trace records that field, so
    "why does this week's commentary read like a template" is answerable
    after the fact instead of being a mystery.
    """

    name = "remote"

    @abc.abstractmethod
    def _generate(self, task: str, prompt: str, context: dict[str, Any]) -> str:
        """Return raw model text, or raise."""

    def complete(self, task: str, prompt: str, context: dict[str, Any]) -> LLMResponse:
        try:
            text = self._generate(task, prompt, context).strip()
            if not text:
                raise ValueError("model returned empty output")
        except Exception as exc:  # noqa: BLE001 - degradation is the point
            log.warning(
                "%s failed on task %r (%s); degrading to the offline engine",
                self.name,
                task,
                exc,
            )
            return _OFFLINE.complete(task, prompt, context)
        return LLMResponse(text=text, engine=self.name, task=task)


class OllamaEngine(RemoteEngine):
    """Generation via Ollama - local daemon or its hosted `-cloud` models."""

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        from ollama import Client  # imported lazily - optional dependency

        kwargs: dict[str, Any] = {"timeout": settings.ollama_timeout_s}
        if settings.ollama_host:
            kwargs["host"] = settings.ollama_host
        self._client = Client(**kwargs)
        self._model = settings.ollama_model

    def _generate(self, task: str, prompt: str, context: dict[str, Any]) -> str:
        resp = self._client.chat(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt(task)},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.4},
        )
        return _strip_reasoning(resp.message.content or "")


class GeminiEngine(RemoteEngine):
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        from google import genai  # imported lazily - optional dependency

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    def _generate(self, task: str, prompt: str, context: dict[str, Any]) -> str:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config={"system_instruction": system_prompt(task), "temperature": 0.4},
        )
        return resp.text or ""


_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Drop inline chain-of-thought from a reasoning model's output.

    gpt-oss returns its reasoning in a separate `thinking` field, so this is
    normally a no-op. It is here because the failure it prevents is silent and
    expensive: one model or client version that inlines <think> blocks instead
    turns `ad_copy` into unparseable JSON, and the creative pipeline reports a
    generation failure rather than a formatting one.
    """
    return _THINK_BLOCK.sub("", text).strip()


# --------------------------------------------------------------------------
# Offline engine
# --------------------------------------------------------------------------
class OfflineEngine:
    """Deterministic generation grounded in the supplied context."""

    name = "offline"

    def complete(self, task: str, prompt: str, context: dict[str, Any]) -> LLMResponse:
        handler = getattr(self, f"_task_{task}", None)
        if handler is None:
            text = f"[offline engine: no handler for task {task!r}]"
        else:
            text = handler(context)
        return LLMResponse(text=text, engine=self.name, task=task)

    # -- report commentary -------------------------------------------------
    def _task_report_narrative(self, ctx: dict[str, Any]) -> str:
        client = ctx.get("client_name", "the account")
        cur = ctx.get("totals", {})
        delta = ctx.get("deltas", {})
        findings = ctx.get("findings", [])
        retainer = ctx.get("retainer", {})
        currency = ctx.get("currency", "AED")

        roas_delta = delta.get("roas_pct", 0.0)
        direction = "improved" if roas_delta >= 0 else "softened"

        p1 = (
            f"{client} spent {currency} {cur.get('spend', 0):,.0f} across "
            f"{cur.get('campaign_count', 0)} campaigns this week, returning "
            f"{currency} {cur.get('revenue', 0):,.0f} at a blended ROAS of "
            f"{cur.get('roas', 0):.2f}. Blended performance {direction} "
            f"{abs(roas_delta):.1f}% against the prior week, with "
            f"{cur.get('conversions', 0):,} conversions at an average CPA of "
            f"{currency} {cur.get('cpa', 0):,.2f}."
        )

        if findings:
            bits = "; ".join(f["headline"] for f in findings[:3])
            p2 = (
                f"Three things drove the movement: {bits}. These are the levers "
                f"we are acting on first, because they account for the majority "
                f"of the week-on-week variance rather than normal fluctuation."
            )
        else:
            p2 = (
                "No campaign moved outside its normal variance band this week, "
                "so we are holding budget allocation steady and continuing to "
                "build creative volume against the top-performing audiences."
            )

        util = retainer.get("utilisation", 0.0)
        if retainer.get("over_budget"):
            p3 = (
                f"On resourcing, we used {retainer.get('used_hours', 0):.1f} of "
                f"{retainer.get('contracted_hours', 0):.1f} contracted hours "
                f"({util:.0f}%). The overage sits mainly in ad-hoc requests and "
                f"creative production. We will raise this on the next call so we "
                f"can either rebalance the scope or formally extend the retainer."
            )
        else:
            p3 = (
                f"Resourcing is on track at {retainer.get('used_hours', 0):.1f} of "
                f"{retainer.get('contracted_hours', 0):.1f} contracted hours "
                f"({util:.0f}%), leaving headroom for the additional creative "
                f"testing outlined above."
            )

        return "\n\n".join([p1, p2, p3])

    # -- creative generation ----------------------------------------------
    def _task_ad_copy(self, ctx: dict[str, Any]) -> str:
        product = ctx.get("product", "the product")
        audience = ctx.get("audience", "shoppers")
        benefit = ctx.get("key_benefit", "better results")
        cta = ctx.get("cta", "Shop now")
        n = int(ctx.get("variant_count", 3))

        templates = [
            ("{benefit}, without the guesswork", "Built for {audience}. {product} makes it simple. {cta}."),
            ("Why {audience} switch to {product}", "{benefit} from day one. See what the difference feels like. {cta}."),
            ("{product}: {benefit}", "Thousands of {audience} already made the move. {cta}."),
            ("Made for {audience}", "{product} delivers {benefit}. No noise, no filler. {cta}."),
            ("The {product} difference", "{benefit}, designed around how {audience} actually live. {cta}."),
            ("Guaranteed {benefit} — act now", "Cheap, fast and the best price ever for {audience}. {cta}."),
        ]

        variants = []
        for i in range(n):
            head_t, body_t = templates[i % len(templates)]
            variants.append(
                {
                    "headline": head_t.format(
                        benefit=benefit.capitalize(), audience=audience, product=product
                    ),
                    "body": body_t.format(
                        benefit=benefit.capitalize(),
                        audience=audience,
                        product=product,
                        cta=cta,
                    ),
                }
            )
        return json.dumps(variants, indent=2)

    def _task_copy_revision(self, ctx: dict[str, Any]) -> str:
        headline = ctx.get("headline", "")
        body = ctx.get("body", "")
        banned = ctx.get("banned_phrases", [])
        max_h = int(ctx.get("max_headline_chars", 40))
        max_b = int(ctx.get("max_body_chars", 125))
        cta = ctx.get("cta", "Shop now")

        for phrase in banned:
            headline = re.sub(re.escape(phrase), "", headline, flags=re.IGNORECASE)
            body = re.sub(re.escape(phrase), "", body, flags=re.IGNORECASE)

        headline = re.sub(r"\s{2,}", " ", headline).strip(" ,.-—")
        body = re.sub(r"\s{2,}", " ", body).strip(" ,.-—")

        if not headline:
            headline = ctx.get("product", "Discover more")
        if cta.lower() not in body.lower():
            body = f"{body.rstrip('. ')}. {cta}." if body else f"{cta}."

        return json.dumps(
            {"headline": _truncate(headline, max_h), "body": _truncate(body, max_b)}, indent=2
        )


# The instance remote engines degrade to. Stateless, so one is enough.
_OFFLINE = OfflineEngine()


def _truncate(text: str, limit: int) -> str:
    """Cut to a length limit on a word boundary.

    Mid-word truncation ("...not a cold t") reads as broken copy and would
    sail past a pure character-count check, so the trimming has to be
    word-aware at the point of edit rather than patched up downstream.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-—")


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
ENGINES: dict[str, type[RemoteEngine]] = {
    "ollama": OllamaEngine,
    "gemini": GeminiEngine,
}


def build_engine(settings: Settings | None = None) -> LLMEngine:
    """Resolve the configured engine, or the offline one if it cannot be built.

    Construction failing here means a missing dependency or bad config, which
    is worth a warning at startup. A *call* failing later is handled inside
    RemoteEngine, because by then a run is already in flight.
    """
    settings = settings or get_settings()
    provider = settings.resolved_llm_provider
    engine_cls = ENGINES.get(provider)
    if engine_cls is None:
        return _OFFLINE
    try:
        return engine_cls(settings)
    except Exception as exc:  # pragma: no cover - depends on optional deps
        log.warning("%s unavailable (%s); falling back to offline engine", provider, exc)
    return _OFFLINE
