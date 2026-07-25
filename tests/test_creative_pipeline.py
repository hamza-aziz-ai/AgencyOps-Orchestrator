from __future__ import annotations

import pytest

from agencyops.graphs.creative_pipeline import (
    MAX_REVISION_ROUNDS,
    run_creative,
    score_variant,
)

RULES = {
    "banned_phrases": ["cheap", "guaranteed"],
    "required_elements": ["call to action"],
    "max_headline_chars": 40,
    "max_body_chars": 125,
}


def test_clean_variant_scores_full_marks():
    v = score_variant({"headline": "Hydration made simple", "body": "Built for busy days. Shop the set."}, RULES)
    assert v["score"] == 100 and v["passed"] and v["violations"] == []


def test_banned_phrase_fails():
    v = score_variant({"headline": "Cheap hydration", "body": "Get yours today."}, RULES)
    assert not v["passed"]
    assert any("banned phrase" in x for x in v["violations"])


def test_overlong_headline_fails():
    v = score_variant({"headline": "x" * 60, "body": "Shop the set now."}, RULES)
    assert any("headline" in x and "limit" in x for x in v["violations"])


def test_missing_cta_fails():
    v = score_variant({"headline": "Nice product", "body": "It is a product that exists."}, RULES)
    assert any("call to action" in x for x in v["violations"])


def test_incoherent_copy_is_caught():
    """Guards against a revision pass mangling copy into nonsense."""
    v = score_variant({"headline": "Hydration", "body": "fast and the for busy people. Shop now."}, RULES)
    assert not v["passed"]
    assert any("grammatically broken" in x for x in v["violations"])


def test_score_never_goes_negative():
    v = score_variant({"headline": "", "body": "cheap guaranteed cheap guaranteed"}, RULES)
    assert v["score"] == 0


# ---------------------------------------------------------------- graph ----
def test_pipeline_produces_compliant_variants(settings, bundle, engine):
    s = run_creative("nova-retail", "Nova Hydrate", "busy professionals",
                     "all-day hydration", "Shop the set", variant_count=4,
                     bundle=bundle, engine=engine, settings=settings)
    assert s["approved_variants"]
    for v in s["approved_variants"]:
        assert v["passed"] and v["violations"] == []


def test_revision_loop_is_bounded(settings, bundle, engine):
    """A model that cannot comply must fail loudly, not loop forever."""
    s = run_creative("nova-retail", "Nova Hydrate", "busy professionals",
                     "all-day hydration", "Shop the set", variant_count=6,
                     bundle=bundle, engine=engine, settings=settings)
    assert s["revision_round"] <= MAX_REVISION_ROUNDS
    revisions = [st for st in s["trace"].steps if st.node.startswith("revise_round")]
    assert len(revisions) <= MAX_REVISION_ROUNDS


def test_unsalvageable_copy_escalates_to_a_human(settings, bundle, engine):
    s = run_creative("nova-retail", "Nova Hydrate", "busy professionals",
                     "all-day hydration", "Shop the set", variant_count=6,
                     bundle=bundle, engine=engine, settings=settings)
    if s["rejected_variants"]:
        qa = [e for e in s["effects"] if e.connector == "slack"]
        assert len(qa) == 1
        assert "human copywriter" in qa[0].payload["text"]


def test_pipeline_stages_nothing_without_approval(settings, bundle, engine):
    s = run_creative("nova-retail", "Nova Hydrate", "busy professionals",
                     "all-day hydration", variant_count=3,
                     bundle=bundle, engine=engine, settings=settings)
    assert all(e.status == "proposed" for e in s["effects"])
    assert bundle.writer("trello").log == []


def test_unknown_brand_is_rejected(settings, bundle, engine):
    s = run_creative("mystery-brand", "Thing", "people", "benefit",
                     bundle=bundle, engine=engine, settings=settings)
    assert s["errors"]
    assert not s.get("effects")


def test_midword_fragment_is_caught():
    """A headline cut mid-word passes a character count but reads as broken."""
    v = score_variant(
        {"headline": "Not a cold t", "body": "Built for beginners. Start today."}, RULES
    )
    assert any("grammatically broken" in x for x in v["violations"])


def test_offline_revision_truncates_on_word_boundary(engine):
    resp = engine.complete(
        "copy_revision",
        "",
        {
            "headline": "A coached start, not a cold treadmill, without the guesswork",
            "body": "Short body. Start today.",
            "banned_phrases": [],
            "max_headline_chars": 40,
            "max_body_chars": 125,
            "cta": "Start today",
            "product": "Atlas",
        },
    )
    headline = resp.as_json()["headline"]
    assert len(headline) <= 40
    assert not headline.endswith((" ", ",")), "should trim trailing punctuation"
    # every token must be a whole word from the original
    original = "A coached start, not a cold treadmill, without the guesswork"
    assert all(tok in original for tok in headline.split())
