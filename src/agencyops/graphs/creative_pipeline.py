"""Creative production pipeline.

    load_guidelines -> generate -> score -> [revise ⟲] -> select -> propose -> [gate] -> dispatch

The interesting part is the revision loop. Generated copy is scored against
machine-checkable brand rules (banned phrases, character limits, required
elements). Anything that fails goes back to the model with the specific
violations attached - not a vague "try again" - and is re-scored. The loop is
bounded, so a model that cannot satisfy the rules fails loudly instead of
burning tokens forever.

This is the piece that makes the difference between "AI writes our ads" and
"AI writes ads we can actually run": compliance is enforced in code, not
hoped for in a prompt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, StateGraph

from ..config import Settings, get_settings
from ..connectors import ConnectorBundle, build_bundle
from ..connectors.base import Effect
from ..llm import LLMEngine, build_engine
from ..observability import RunTrace, timed
from .state import CreativeState

GUIDELINES_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "fixtures" / "brand_guidelines.json"
)
MAX_REVISION_ROUNDS = 2
PASS_THRESHOLD = 80


# --------------------------------------------------------------------------
# Scoring - deterministic, no LLM
# --------------------------------------------------------------------------
def score_variant(variant: dict[str, Any], guidelines: dict[str, Any]) -> dict[str, Any]:
    """Score one variant 0-100 and list concrete violations."""
    headline = variant.get("headline", "") or ""
    body = variant.get("body", "") or ""
    combined = f"{headline} {body}".lower()

    violations: list[str] = []
    score = 100

    for phrase in guidelines.get("banned_phrases", []):
        if phrase.lower() in combined:
            violations.append(f"banned phrase: {phrase!r}")
            score -= 25

    max_h = guidelines.get("max_headline_chars", 40)
    if len(headline) > max_h:
        violations.append(f"headline {len(headline)} chars, limit {max_h}")
        score -= 15

    max_b = guidelines.get("max_body_chars", 125)
    if len(body) > max_b:
        violations.append(f"body {len(body)} chars, limit {max_b}")
        score -= 15

    required = guidelines.get("required_elements", [])
    if "call to action" in required and not _has_cta(body):
        violations.append("missing call to action")
        score -= 20
    if "value proposition" in required and len(body.split()) < 6:
        violations.append("body too thin to carry a value proposition")
        score -= 10

    if not headline.strip():
        violations.append("empty headline")
        score -= 40

    # Catches copy mangled by an over-eager revision pass. Without this, a
    # naive find-and-replace fix ("cheap, fast and the best price ever" ->
    # "fast and the") scores 100 while reading like nonsense.
    for field_name, text in (("headline", headline), ("body", body)):
        if _is_incoherent(text):
            violations.append(f"{field_name} is grammatically broken after edit")
            score -= 30

    return {
        **variant,
        "score": max(score, 0),
        "violations": violations,
        "passed": max(score, 0) >= PASS_THRESHOLD and not violations,
    }


_CTA_MARKERS = (
    "shop", "buy", "get", "start", "join", "book", "try",
    "discover", "claim", "explore", "see ", "learn",
)


def _has_cta(body: str) -> bool:
    return any(m in body.lower() for m in _CTA_MARKERS)


_DANGLING = {"and", "or", "but", "the", "a", "an", "of", "for", "with", "to", "at"}


def _is_incoherent(text: str) -> bool:
    """Cheap grammatical smell test for machine-edited copy."""
    for sentence in re.split(r"[.!?]", text):
        words = [w for w in re.findall(r"[A-Za-z']+", sentence)]
        if not words:
            continue
        # A clause ending on a dangling function word means something was cut out.
        if len(words) > 1 and words[-1].lower() in _DANGLING:
            return True
        # A trailing one-letter fragment is the signature of a mid-word cut.
        if len(words) > 1 and len(words[-1]) == 1 and words[-1].lower() not in {"a", "i"}:
            return True
        # Two stacked function words with nothing between them.
        for a, b in zip(words, words[1:]):
            if a.lower() in _DANGLING and b.lower() in _DANGLING and a.lower() != b.lower():
                return True
    return False


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------
def build_creative_graph(
    bundle: ConnectorBundle | None = None,
    engine: LLMEngine | None = None,
    settings: Settings | None = None,
):
    settings = settings or get_settings()
    bundle = bundle or build_bundle(settings)
    engine = engine or build_engine(settings)

    def load_guidelines(state: CreativeState) -> dict[str, Any]:
        trace, client = state["trace"], state["client"]
        with timed(trace, "load_guidelines") as t:
            all_rules = json.loads(GUIDELINES_PATH.read_text())
            if client not in all_rules:
                t.summary = f"failed: no brand guidelines for {client!r}"
                return {"errors": [f"No brand guidelines registered for {client!r}"]}
            rules = all_rules[client]
            t.summary = (
                f"{len(rules.get('banned_phrases', []))} banned phrases, "
                f"tone: {rules.get('tone', 'n/a')}"
            )
        return {"guidelines": rules, "revision_round": 0}

    def generate(state: CreativeState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "generate") as t:
            ctx = {
                "product": state["product"],
                "audience": state["audience"],
                "key_benefit": state["key_benefit"],
                "cta": state.get("cta", "Shop now"),
                "variant_count": state.get("variant_count", 4),
                "tone": state["guidelines"].get("tone"),
            }
            resp = engine.complete("ad_copy", _generation_prompt(state), ctx)
            try:
                variants = resp.as_json()
                if not isinstance(variants, list):
                    raise ValueError("expected a JSON list of variants")
            except Exception as exc:  # noqa: BLE001
                t.summary = f"failed to parse model output: {exc}"
                return {"errors": [f"Copy generation returned unparseable output: {exc}"]}
            t.summary = f"{len(variants)} variants via {resp.engine}"
            t.detail = {"engine": resp.engine}
        return {"variants": variants}

    def score(state: CreativeState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "score") as t:
            scored = [score_variant(v, state["guidelines"]) for v in state["variants"]]
            passed = sum(1 for s in scored if s["passed"])
            t.summary = f"{passed}/{len(scored)} passed brand check"
            t.detail = {
                "scores": [s["score"] for s in scored],
                "violations": [v for s in scored for v in s["violations"]],
            }
        return {"scored": scored}

    def revise(state: CreativeState) -> dict[str, Any]:
        """Send only the failing variants back, with their specific violations."""
        trace = state["trace"]
        rnd = state.get("revision_round", 0) + 1
        with timed(trace, f"revise_round_{rnd}") as t:
            g = state["guidelines"]
            repaired: list[dict[str, Any]] = []
            for v in state["scored"]:
                if v["passed"]:
                    repaired.append({"headline": v["headline"], "body": v["body"]})
                    continue
                ctx = {
                    "headline": v["headline"],
                    "body": v["body"],
                    "banned_phrases": g.get("banned_phrases", []),
                    "max_headline_chars": g.get("max_headline_chars", 40),
                    "max_body_chars": g.get("max_body_chars", 125),
                    "cta": state.get("cta", "Shop now"),
                    "product": state["product"],
                }
                resp = engine.complete("copy_revision", _revision_prompt(v, g), ctx)
                try:
                    repaired.append(resp.as_json())
                except Exception:  # noqa: BLE001 - keep the original, it will fail scoring again
                    repaired.append({"headline": v["headline"], "body": v["body"]})
            failing = sum(1 for v in state["scored"] if not v["passed"])
            t.summary = f"revised {failing} failing variants"
        return {"variants": repaired, "revision_round": rnd}

    def select(state: CreativeState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "select") as t:
            scored = sorted(state["scored"], key=lambda s: -s["score"])
            approved = [s for s in scored if s["passed"]]
            rejected = [s for s in scored if not s["passed"]]
            t.summary = f"{len(approved)} approved, {len(rejected)} rejected after {state.get('revision_round', 0)} revision round(s)"
        return {"approved_variants": approved, "rejected_variants": rejected}

    def propose(state: CreativeState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "propose") as t:
            effects: list[Effect] = []
            for i, v in enumerate(state["approved_variants"], 1):
                effects.append(
                    Effect(
                        connector="trello",
                        action="create_card",
                        payload={
                            "list_id": state.get("trello_list_id", "creative-review"),
                            "name": f"[{state['client']}] Variant {i}: {v['headline']}",
                            "description": (
                                f"**Headline:** {v['headline']}\n"
                                f"**Body:** {v['body']}\n\n"
                                f"Brand score: {v['score']}/100\n"
                                f"Product: {state['product']} | Audience: {state['audience']}"
                            ),
                        },
                        summary=f"Queue variant {i} for design ({v['score']}/100)",
                    )
                )
            if state["rejected_variants"]:
                effects.append(
                    Effect(
                        connector="slack",
                        action="post_message",
                        payload={
                            "channel": "#creative-qa",
                            "text": _rejection_digest(state),
                        },
                        summary=f"Flag {len(state['rejected_variants'])} variants that failed brand check",
                        reversible=False,
                    )
                )
            t.summary = f"{len(effects)} effects staged, none executed"
            t.detail = {"effects": [e.render() for e in effects]}
        return {
            "effects": effects,
            "approval_required": settings.require_human_approval,
            "approved": not settings.require_human_approval,
        }

    def dispatch(state: CreativeState) -> dict[str, Any]:
        trace = state["trace"]
        with timed(trace, "dispatch") as t:
            executed = 0
            for effect in state.get("effects", []):
                if effect.status not in ("proposed", "approved"):
                    continue
                try:
                    effect.result = bundle.writer(effect.connector).execute(effect)
                    effect.status = "executed"
                    executed += 1
                except Exception as exc:  # noqa: BLE001
                    effect.status = "failed"
                    effect.result = {"error": str(exc)}
            t.summary = f"{executed}/{len(state.get('effects', []))} effects executed"
        return {}

    def halt_for_approval(state: CreativeState) -> dict[str, Any]:
        with timed(state["trace"], "await_approval") as t:
            t.summary = f"paused - {len(state.get('effects', []))} effects awaiting human approval"
        return {}

    # ------------------------------------------------------------- routing
    def after_guidelines(state: CreativeState) -> Literal["generate", "__end__"]:
        return "__end__" if state.get("errors") else "generate"

    def after_generate(state: CreativeState) -> Literal["score", "__end__"]:
        return "__end__" if state.get("errors") else "score"

    def after_score(state: CreativeState) -> Literal["revise", "select"]:
        """Loop back only if something failed and we have rounds left."""
        all_passed = all(v["passed"] for v in state["scored"])
        rounds_left = state.get("revision_round", 0) < MAX_REVISION_ROUNDS
        return "select" if all_passed or not rounds_left else "revise"

    def after_propose(state: CreativeState) -> Literal["dispatch", "await_approval"]:
        return "await_approval" if state.get("approval_required") else "dispatch"

    g = StateGraph(CreativeState)
    for name, fn in [
        ("load_guidelines", load_guidelines),
        ("generate", generate),
        ("score", score),
        ("revise", revise),
        ("select", select),
        ("propose", propose),
        ("dispatch", dispatch),
        ("await_approval", halt_for_approval),
    ]:
        g.add_node(name, fn)

    g.set_entry_point("load_guidelines")
    g.add_conditional_edges(
        "load_guidelines", after_guidelines, {"generate": "generate", "__end__": END}
    )
    g.add_conditional_edges("generate", after_generate, {"score": "score", "__end__": END})
    g.add_conditional_edges("score", after_score, {"revise": "revise", "select": "select"})
    g.add_edge("revise", "score")          # the loop
    g.add_edge("select", "propose")
    g.add_conditional_edges(
        "propose", after_propose, {"dispatch": "dispatch", "await_approval": "await_approval"}
    )
    g.add_edge("dispatch", END)
    g.add_edge("await_approval", END)
    return g.compile()


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
def _generation_prompt(state: CreativeState) -> str:
    g = state["guidelines"]
    return f"""Write {state.get('variant_count', 4)} ad copy variants.

Product: {state['product']}
Audience: {state['audience']}
Key benefit: {state['key_benefit']}
Call to action: {state.get('cta', 'Shop now')}

BRAND RULES (hard constraints)
Tone: {g.get('tone')}
Headline: max {g.get('max_headline_chars', 40)} characters
Body: max {g.get('max_body_chars', 125)} characters
Never use: {', '.join(g.get('banned_phrases', [])) or 'n/a'}
Must include: {', '.join(g.get('required_elements', [])) or 'n/a'}
Compliance: {g.get('compliance_notes', '')}

Return a JSON list of objects with keys "headline" and "body"."""


def _revision_prompt(variant: dict[str, Any], guidelines: dict[str, Any]) -> str:
    return f"""This ad copy failed the brand check. Fix only the listed violations.

Headline: {variant['headline']}
Body: {variant['body']}

VIOLATIONS
{chr(10).join('- ' + v for v in variant['violations'])}

RULES
Tone: {guidelines.get('tone')}
Headline max {guidelines.get('max_headline_chars', 40)} chars, body max {guidelines.get('max_body_chars', 125)} chars.
Never use: {', '.join(guidelines.get('banned_phrases', [])) or 'n/a'}

Return JSON with keys "headline" and "body"."""


def _rejection_digest(state: CreativeState) -> str:
    lines = [
        f"*Creative QA — {state['client']} / {state['product']}*",
        f"{len(state['rejected_variants'])} variant(s) could not be brought within brand guidelines "
        f"after {MAX_REVISION_ROUNDS} revision rounds:",
        "",
    ]
    for v in state["rejected_variants"]:
        lines.append(f"• _{v['headline']}_ — score {v['score']}/100")
        for viol in v["violations"]:
            lines.append(f"    ↳ {viol}")
    lines += ["", "These need a human copywriter rather than another model pass."]
    return "\n".join(lines)


def run_creative(
    client: str,
    product: str,
    audience: str,
    key_benefit: str,
    cta: str = "Shop now",
    variant_count: int = 4,
    **build_kwargs: Any,
) -> CreativeState:
    graph = build_creative_graph(**build_kwargs)
    trace = RunTrace(workflow="creative_pipeline")
    initial: CreativeState = {
        "client": client,
        "product": product,
        "audience": audience,
        "key_benefit": key_benefit,
        "cta": cta,
        "variant_count": variant_count,
        "trace": trace,
        "effects": [],
        "errors": [],
    }
    result = graph.invoke(initial)
    trace.finish(
        "blocked_on_approval"
        if result.get("approval_required") and not result.get("approved")
        else "completed"
    )
    return result
