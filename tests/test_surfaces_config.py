"""Config-level tests for config/surfaces.json — every surface, but with
extra assertions for the sleep/eating/adhd expansion (domain_tags/topic_tags/
evidence taxonomy additions).
"""
import json
from pathlib import Path

import pytest

from src.scheduler import _COLLECTOR_MAP

SURFACES_PATH = Path(__file__).parent.parent / "config" / "surfaces.json"

_VALID_SOURCE_TYPES = {
    "official_health", "academic", "hospital", "nonprofit", "community",
    "news", "social",
}
_VALID_AUDIENCE_TYPES = {"parent_facing", "clinician_facing", "mixed", "research"}
_VALID_DOMAIN_TAGS = {"sleep", "eating", "behavioral", "adhd", "autism"}
_NEW_DOMAINS = {"sleep", "eating", "adhd"}


@pytest.fixture(scope="module")
def surfaces():
    with open(SURFACES_PATH) as f:
        return json.load(f)


# Exact keys added by the sleep/eating/adhd expansion. Deliberately an
# explicit list rather than a "_sleep"/"_eating"/"_adhd" suffix match — a
# suffix match also catches pre-existing unrelated surfaces (e.g.
# yt_how_to_adhd, a tier-3 YouTube-channel surface that predates this work
# and is correctly excluded from the tier-1/2 expansion's requirements).
_NEW_SURFACE_KEYS = {
    "pubmed_sleep", "pubmed_eating", "pubmed_adhd",
    "europepmc_sleep", "europepmc_eating", "europepmc_adhd",
    "semanticscholar_sleep", "semanticscholar_eating", "semanticscholar_adhd",
    "crossref_sleep", "crossref_eating", "crossref_adhd",
    "doaj_sleep", "doaj_eating", "doaj_adhd",
    "openalex_sleep", "openalex_eating", "openalex_adhd",
    "core_sleep", "core_eating", "core_adhd",
    "clinicaltrials_sleep", "clinicaltrials_eating", "clinicaltrials_adhd",
    "biorxiv_sleep", "biorxiv_eating", "biorxiv_adhd",
    "nhs_sleep", "nhs_eating", "nhs_adhd",
    "cdc_sleep", "cdc_nutrition", "cdc_adhd",
    "nhlbi_sleep", "medlineplus_sleep", "nichd_sleep",
    "nutritiongov_eating", "dietaryguidelines_eating", "niddk_eating",
    "fda_nutrition", "medlineplus_eating", "medlineplus_adhd",
    "nice_eating", "nice_adhd", "nimh_adhd",
    "aasm_sleep", "sleepeducation_sleep", "mayo_sleep",
    "clevelandclinic_sleep", "hopkins_sleep", "chop_sleep", "bostonchildrens_sleep",
    "eatright_eating", "healthychildren_eating", "aga_eating",
    "clevelandclinic_eating", "hopkins_eating", "chop_eating", "bostonchildrens_eating",
    "healthychildren_adhd", "chop_adhd", "bostonchildrens_adhd", "germany_neuro_adhd",
}


@pytest.fixture(scope="module")
def new_surfaces(surfaces):
    """Surfaces added by the sleep/eating/adhd expansion (see _NEW_SURFACE_KEYS)."""
    return [s for s in surfaces if s["key"] in _NEW_SURFACE_KEYS]


def test_new_surface_keys_all_present(surfaces):
    found = {s["key"] for s in surfaces} & _NEW_SURFACE_KEYS
    missing = _NEW_SURFACE_KEYS - found
    assert not missing, f"expected new surfaces missing from surfaces.json: {missing}"


def test_surfaces_json_is_valid_json(surfaces):
    assert isinstance(surfaces, list)
    assert len(surfaces) > 0


def test_no_duplicate_keys(surfaces):
    keys = [s["key"] for s in surfaces]
    assert len(keys) == len(set(keys)), "duplicate surface keys found"


def test_every_platform_has_a_registered_collector(surfaces):
    unknown = {s["platform"] for s in surfaces if s["platform"] not in _COLLECTOR_MAP}
    assert not unknown, f"platforms with no collector: {unknown}"


def test_new_surfaces_exist(new_surfaces):
    """Sanity check that the fixture actually found the expansion — guards
    against this test file silently testing zero surfaces if the key
    convention ever changes."""
    assert len(new_surfaces) >= 60, f"expected 60+ new surfaces, found {len(new_surfaces)}"


def test_new_surfaces_have_valid_authority_tier(new_surfaces):
    for s in new_surfaces:
        assert s.get("authority_tier") in (1, 2), (
            f"{s['key']}: authority_tier must be 1 or 2 (tier-1/2-only expansion), "
            f"got {s.get('authority_tier')!r}"
        )


def test_new_surfaces_have_valid_source_type(new_surfaces):
    for s in new_surfaces:
        assert s.get("source_type") in _VALID_SOURCE_TYPES, (
            f"{s['key']}: source_type {s.get('source_type')!r} not in {_VALID_SOURCE_TYPES}"
        )


def test_new_surfaces_have_valid_audience_type(new_surfaces):
    for s in new_surfaces:
        assert s.get("audience_type") in _VALID_AUDIENCE_TYPES, (
            f"{s['key']}: audience_type {s.get('audience_type')!r} not in {_VALID_AUDIENCE_TYPES}"
        )


def test_new_surfaces_have_domain_tags(new_surfaces):
    for s in new_surfaces:
        tags = s.get("domain_tags")
        assert tags, f"{s['key']}: domain_tags missing/empty"
        assert set(tags) <= _VALID_DOMAIN_TAGS, f"{s['key']}: unknown domain_tags {tags}"
        assert set(tags) & _NEW_DOMAINS, f"{s['key']}: domain_tags {tags} has no new-expansion domain"


def test_new_surfaces_have_topic_tags(new_surfaces):
    for s in new_surfaces:
        assert s.get("topic_tags"), f"{s['key']}: topic_tags missing/empty"


def test_disabled_new_surfaces_document_why(new_surfaces):
    """Every enabled:0 surface added this round must explain the limitation
    in content_notes — no silent omission (per the finalized spec's rule)."""
    for s in new_surfaces:
        if s.get("enabled") == 0:
            notes = s.get("content_notes", "")
            assert "DISABLED" in notes, f"{s['key']}: enabled:0 but no DISABLED reason in content_notes"


def test_html_crawl_and_playwright_new_surfaces_have_a_url_key(new_surfaces):
    for s in new_surfaces:
        if s["platform"] == "html_crawl":
            assert "base_url" in s["config"], f"{s['key']}: html_crawl missing base_url"
        elif s["platform"] == "playwright_crawl":
            assert "start_url" in s["config"], f"{s['key']}: playwright_crawl missing start_url"
        elif s["platform"] == "sitemap":
            assert "sitemap_url" in s["config"] and "filter_path" in s["config"], (
                f"{s['key']}: sitemap missing sitemap_url/filter_path"
            )
        elif s["platform"] == "nhs_api":
            assert "slugs" in s["config"], f"{s['key']}: nhs_api surface should set explicit slugs"


def test_academic_api_surfaces_have_query(new_surfaces):
    academic_platforms = {
        "pubmed", "europepmc", "semanticscholar", "crossref", "doaj",
        "openalex", "core", "clinicaltrials", "biorxiv",
    }
    for s in new_surfaces:
        if s["platform"] in academic_platforms:
            assert s["config"].get("query"), f"{s['key']}: missing query"


def test_backfilled_existing_autism_surfaces_have_source_type(surfaces):
    """Bonus low-cost backfill from the finalized spec (section 2b) — spot
    check the surfaces it explicitly named."""
    expected = {
        "mayo_autism": "hospital",
        "pubmed_autism": "academic",
        "clinicaltrials_autism": "academic",
    }
    by_key = {s["key"]: s for s in surfaces}
    for key, expected_type in expected.items():
        assert by_key[key].get("source_type") == expected_type
