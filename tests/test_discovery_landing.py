from src.discovery.landing import _passes_domain_allowlist, _surface_key_for_pair


def test_passes_domain_allowlist_exact_and_subdomain():
    assert _passes_domain_allowlist("https://cdc.gov/sleep/about", "cdc.gov")
    assert _passes_domain_allowlist("https://www.cdc.gov/sleep/about", "cdc.gov")
    assert _passes_domain_allowlist("https://sub.cdc.gov/x", "cdc.gov")


def test_passes_domain_allowlist_rejects_lookalikes_and_other_domains():
    # The prompt-level "site:cdc.gov" instruction is a soft constraint —
    # this is the hard code-level backstop (crawl.txt section 13.3 layer 2a).
    assert not _passes_domain_allowlist("https://notcdc.gov/x", "cdc.gov")
    assert not _passes_domain_allowlist("https://cdc.gov.evil.com/x", "cdc.gov")
    assert not _passes_domain_allowlist("https://nih.gov/x", "cdc.gov")


def test_surface_key_for_pair_is_stable_and_namespaced():
    assert _surface_key_for_pair("cdc.gov", "sleep") == "discovery_cdc.gov_sleep"


def test_surface_key_for_pair_topic_none_uses_the_search_queue_namespace():
    # The reactive search-queue path (websearch.txt 15.1) has no topic —
    # only a domain — so it gets its own per-domain key instead of
    # discovery_<domain>_<topic>, and mustn't collide with a real topic
    # someone could later name "None".
    assert _surface_key_for_pair("cdc.gov", None) == "discovery_search_cdc.gov"
    assert _surface_key_for_pair("cdc.gov", None) != _surface_key_for_pair("cdc.gov", "None")
