"""Tests for the NHS collector's config-driven slug generalization
(src/collectors/nhs.py). Only exercises the pure _build_urls() helper — no
network calls.
"""
from src.collectors.nhs import _build_urls, _DEFAULT_SLUGS


def test_default_slugs_match_original_hardcoded_behavior():
    """Regression test: nhs_autism sets no "slugs" in its surfaces.json
    config, so it must produce the exact same URL list the collector built
    before the generalization (all under /conditions/)."""
    old_slugs = [
        "autism", "autism/what-is-autism", "autism/signs-of-autism",
        "autism/getting-diagnosed", "autism/help-and-support",
        "autism/autism-and-everyday-life",
        "autism/autism-and-everyday-life/communicating",
        "autism/autism-and-everyday-life/community-care-and-support",
        "developmental-delay", "social-care-and-support-guide",
        "attention-deficit-hyperactivity-disorder-adhd",
        "sensory-processing-disorder", "learning-disabilities",
        "stammering", "selective-mutism", "dyspraxia",
    ]
    expected = [f"https://www.nhs.uk/conditions/{slug}/" for slug in old_slugs]
    assert _build_urls({}) == expected


def test_custom_slugs_used_when_provided():
    config = {"slugs": ["conditions/insomnia", "conditions/sleepwalking"]}
    assert _build_urls(config) == [
        "https://www.nhs.uk/conditions/insomnia/",
        "https://www.nhs.uk/conditions/sleepwalking/",
    ]


def test_custom_slugs_support_non_conditions_namespaces():
    """This is the bug the original generalization draft had: real nhs.uk
    sleep/eating content lives under /live-well/ and /mental-health/, not
    just /conditions/ — the URL join must not hardcode a "/conditions/"
    prefix."""
    config = {"slugs": [
        "live-well/sleep-and-tiredness/healthy-sleep-tips-for-children",
        "mental-health/feelings-symptoms-behaviours/behaviours/eating-disorders/overview",
    ]}
    urls = _build_urls(config)
    assert urls == [
        "https://www.nhs.uk/live-well/sleep-and-tiredness/healthy-sleep-tips-for-children/",
        "https://www.nhs.uk/mental-health/feelings-symptoms-behaviours/behaviours/eating-disorders/overview/",
    ]


def test_slugs_with_surrounding_slashes_are_normalized():
    config = {"slugs": ["/conditions/insomnia/"]}
    assert _build_urls(config) == ["https://www.nhs.uk/conditions/insomnia/"]


def test_default_slugs_constant_is_used_as_fallback():
    assert _build_urls({}) == [f"https://www.nhs.uk/{s}/" for s in _DEFAULT_SLUGS]
