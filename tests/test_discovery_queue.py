"""Tests for src/discovery/queue.py against the real config/surfaces.json
— same pattern as test_surfaces_config.py (no mock config; the real file
is small and stable enough to assert against directly)."""
from src.discovery.queue import (
    build_discovery_queue,
    surface_metadata_for_domain,
    surface_metadata_for_pair,
    _extract_domain,
)
from src.discovery.schemas import DISCOVERY_RESULTS_SCHEMA


def test_build_discovery_queue_returns_deduplicated_sorted_pairs():
    queue = build_discovery_queue()
    assert len(queue) > 20, f"expected 20+ (domain, topic) pairs, got {len(queue)}"
    assert queue == sorted(set(queue)), "queue must be deduplicated and sorted"


def test_build_discovery_queue_pairs_are_tier1_2_domains_only():
    queue = build_discovery_queue()
    domains = {domain for domain, _ in queue}
    # Spot-check a few known tier1/2 html_crawl/playwright_crawl/sitemap
    # domains from surfaces.json are present...
    assert "cdc.gov" in domains
    assert "nhs.uk" in domains
    # ...and that no scheme/www./path leaked through.
    for domain in domains:
        assert not domain.startswith("http"), domain
        assert not domain.startswith("www."), domain
        assert "/" not in domain, domain


def test_build_discovery_queue_is_stable_across_calls():
    """discovery_queue_state.last_index only means anything if the same
    index always maps to the same pair (migration 0022's whole point)."""
    assert build_discovery_queue() == build_discovery_queue()


def test_extract_domain_prefers_base_url_start_url_sitemap_url_in_order():
    assert _extract_domain({"config": {"base_url": "https://www.cdc.gov/sleep/"}}) == "cdc.gov"
    assert _extract_domain({"config": {"start_url": "https://www.nice.org.uk/x"}}) == "nice.org.uk"
    assert _extract_domain({"config": {"sitemap_url": "https://www.nhs.uk/sitemap.xml"}}) == "nhs.uk"
    assert _extract_domain({"config": {}}) is None
    assert _extract_domain({}) is None


def test_discovery_results_schema_is_well_formed():
    assert DISCOVERY_RESULTS_SCHEMA["type"] == "object"
    props = DISCOVERY_RESULTS_SCHEMA["properties"]["results"]["items"]["properties"]
    assert set(props.keys()) == {"url", "title"}


def test_surface_metadata_for_pair_returns_real_values_for_every_queue_pair():
    """Every pair build_discovery_queue() emits must resolve back to real
    metadata — landing.py's synthetic Surface row would otherwise silently
    get authority_tier=None (i.e. an untiered/untrusted row) for a pair
    that supposedly came from a tier1/2 surface in the first place."""
    for domain, topic in build_discovery_queue():
        tier, source_type, audience_type = surface_metadata_for_pair(domain, topic)
        assert tier in (1, 2), f"({domain}, {topic}): expected tier 1/2, got {tier!r}"
        assert source_type, f"({domain}, {topic}): missing source_type"


def test_surface_metadata_for_pair_unknown_pair_returns_none():
    assert surface_metadata_for_pair("nonexistent.example", "sleep") == (None, None, None)


def test_extract_domain_falls_back_to_explicit_config_domain_key():
    # Academic-API surfaces (europepmc/semanticscholar/crossref/biorxiv/
    # doaj/openalex/clinicaltrials/core) have no base_url/start_url/
    # sitemap_url — their config carries API query params instead — so
    # they rely entirely on this fallback (websearch.txt 15.1/E).
    assert _extract_domain({"config": {"query": "x", "domain": "europepmc.org"}}) == "europepmc.org"
    assert _extract_domain({"config": {"query": "x", "domain": "www.core.ac.uk"}}) == "core.ac.uk"
    # base_url/start_url/sitemap_url still win over config.domain when both present.
    assert _extract_domain({"config": {"base_url": "https://cdc.gov/x", "domain": "wrong.example"}}) == "cdc.gov"


def test_surface_metadata_for_domain_resolves_known_tier12_domains():
    # cdc.gov: an html_crawl-family domain (already covered by
    # surface_metadata_for_pair's own _QUEUE_PLATFORMS-restricted lookup).
    tier, source_type, audience_type = surface_metadata_for_domain("cdc.gov")
    assert tier in (1, 2)
    assert source_type

    # europepmc.org: an API-platform domain that surface_metadata_for_pair
    # can NEVER resolve (excluded by _QUEUE_PLATFORMS) but that websearch.txt
    # section E explicitly lists as a valid tier1/2 domain for the reactive
    # search-queue path — this is exactly the gap surface_metadata_for_domain
    # exists to close.
    tier, source_type, audience_type = surface_metadata_for_domain("europepmc.org")
    assert tier == 2
    assert source_type == "academic"


def test_surface_metadata_for_domain_unknown_domain_returns_none():
    # The germany_neuro domain mismatch from websearch.txt section 十四C:
    # crawl actually crawls neurologen-und-psychiater-im-netz.org, not
    # dgkjp.de (which is what search's sources.json has registered) — this
    # must resolve to "unrecognized", not silently succeed.
    assert surface_metadata_for_domain("dgkjp.de") == (None, None, None)
    assert surface_metadata_for_domain("nonexistent.example") == (None, None, None)
