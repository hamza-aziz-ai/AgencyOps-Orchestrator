"""LLM access layer.

Two engines behind one interface:

  GeminiEngine   - real generation via google-genai
  OfflineEngine  - deterministic, template-driven, zero-dependency

The offline engine is not a mock that returns "lorem ipsum". It produces
genuine, data-grounded output for every task the graphs need. That buys three
things: the demo runs on any machine with no API key, the test suite is
deterministic, and an LLM outage degrades the system to templated output
rather than taking reporting down entirely.

Every call is tagged with a `task` label. The offline engine routes on it;
the Gemini engine uses it to select the system prompt. Same contract either
way, so graph code never branches on which engine is active.
"""
from __future__ import annotations

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


# --------------------------------------------------------------------------
# Real engine
# --------------------------------------------------------------------------
class GeminiEngine:
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        from google import genai  # imported lazily - optional dependency

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_model

    def complete(self, task: str, prompt: str, context: dict[str, Any]) -> LLMResponse:
        system = SYSTEM_PROMPTS.get(task, "You are a precise, concise assistant.")
        resp = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config={"system_instruction": system, "temperature": 0.4},
        )
        return LLMResponse(text=(resp.text or "").strip(), engine=self.name, task=task)


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
def build_engine(settings: Settings | None = None) -> LLMEngine:
    settings = settings or get_settings()
    if settings.llm_available:
        try:
            return GeminiEngine(settings)
        except Exception as exc:  # pragma: no cover - depends on optional dep
            log.warning("Gemini unavailable (%s); falling back to offline engine", exc)
    return OfflineEngine()
