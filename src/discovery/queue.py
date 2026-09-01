"""Builds the round-robin (domain, topic) queue for discovery_loop() —
crawl.txt section 13.2 item 1: "维护一个'域名 × 主题'轮询队列...内容直接
从config/surfaces.json里现有的tier1/2域名 + domain_tags派生出来,不用另外
手工维护一份域名清单."

Scope note: only html_crawl / playwright_crawl / sitemap surfaces go into
the queue. Academic-API platforms (pubmed, crossref, europepmc, ...) don't
map to a single browsable site: a `site:eutils.ncbi.nlm.nih.gov` WebSearch
query would be meaningless, and section 3 of crawl.txt already covers that
gap directly via the real APIs (layer 1) — WebSearch (layer 4) exists to
catch pages those APIs and the site-crawlers miss, not to duplicate the
API layer. rss surfaces are excluded too: their config carries a list of
feed URLs (sometimes spanning more than one domain), not one base_url/
start_url/sitemap_url to derive a single site: target from.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

_DEFAULT_SURFACES_PATH = Path(__file__).parent.parent.parent / "config" / "surfaces.json"

# The three platforms whose config carries exactly one base-site URL
# (base_url / start_url / sitemap_url) to derive a site: target from.
_QUEUE_PLATFORMS = {"html_crawl", "playwright_crawl", "sitemap"}


def _extract_domain(surface: dict) -> str | None:
    """Pull the bare host (no scheme, no leading www.) that this surface
    crawls, from whichever of base_url/start_url/sitemap_url its platform
    uses (see test_surfaces_config.py::test_html_crawl_and_playwright_new_
    surfaces_have_a_url_key for the platform->key mapping this mirrors).

    Falls back to an explicit config.domain key for API-platform surfaces
    (europepmc/semanticscholar/crossref/biorxiv/doaj/openalex/
    clinicaltrials/core) whose config carries API query params instead of
    any URL field — their base domain isn't derivable any other way from
    surfaces.json, only from the collector code that builds the actual
    request. That key was added to those 11 surfaces (2026-08-29)
    specifically so surface_metadata_for_domain() below can resolve them
    without a second, separately-maintained domain list — see
    websearch.txt section 15.1/E."""
    cfg = surface.get("config") or {}
    url = cfg.get("base_url") or cfg.get("start_url") or cfg.get("sitemap_url")
    if url:
        host = urlparse(url).netloc
        if host:
            return host.removeprefix("www.")
    domain = cfg.get("domain")
    if domain:
        return domain.removeprefix("www.")
    return None


def build_discovery_queue(surfaces_path: Path | str = _DEFAULT_SURFACES_PATH) -> list[tuple[str, str]]:
    """Return a sorted, deduplicated list of (domain, topic) pairs to
    round-robin through, e.g. [("cdc.gov", "eating"), ("cdc.gov", "sleep"),
    ("nhs.uk", "eating"), ...].

    Sorted (not insertion order from surfaces.json, and not set-iteration
    order) so that a given position in the queue means the same pair on
    every run — discovery_queue_state.last_index (migration
    0022_add_discovery_queue_state) is only a meaningful resume point if
    the queue itself is deterministic across restarts/redeploys.

    Tier 1/2 only, matching this repo's one authority rule everywhere else
    (crawl.txt section 4: "Everything else discarded"). A surface with no
    domain_tags (most of the pre-expansion autism surfaces — they predate
    the sleep/eating/adhd tagging work) contributes nothing; that's
    expected, not an error — see crawl.txt section 13 background, this
    layer's initial scope was sleep/eating/behavior(al)/adhd, and the
    queue picks up new tracks automatically once surfaces.json tags them,
    with no code change needed here.
    """
    with open(surfaces_path) as f:
        surfaces = json.load(f)

    pairs: set[tuple[str, str]] = set()
    for surface in surfaces:
        if surface.get("authority_tier") not in (1, 2):
            continue
        if surface.get("platform") not in _QUEUE_PLATFORMS:
            continue
        domain = _extract_domain(surface)
        if not domain:
            continue
        for topic in surface.get("domain_tags") or []:
            pairs.add((domain, topic))

    return sorted(pairs)


def surface_metadata_for_pair(
    domain: str,
    topic: str,
    surfaces_path: Path | str = _DEFAULT_SURFACES_PATH,
) -> tuple[int | None, str | None, str | None]:
    """Returns (authority_tier, source_type, audience_type) copied from
    whichever tier1/2 surface(s) contributed this (domain, topic) pair to
    the queue. Used by src/discovery/landing.py to seed the synthetic
    "discovery_<domain>_<topic>" Surface row that discovery-found items
    get attributed to (save_items() requires a real Surface row to read
    those fields from — src/pipeline.py:281 — and a websearch-found URL
    has no surface of its own otherwise).

    When more than one surface shares a (domain, topic) pair (e.g. two
    aap.org surfaces both tagged "eating"), the best (lowest/most
    trusted) authority_tier wins; surface key breaks ties, for a
    deterministic result regardless of surfaces.json's file order.

    Returns (None, None, None) if no matching surface is found — callers
    should treat that as "attribute conservatively" (no tier/source_type
    claimed), not as an error; the (domain, topic) pair still came from
    build_discovery_queue() in the first place, so this should only
    happen if surfaces.json changed between the two calls.
    """
    with open(surfaces_path) as f:
        surfaces = json.load(f)

    matches = []
    for surface in surfaces:
        if surface.get("authority_tier") not in (1, 2):
            continue
        if surface.get("platform") not in _QUEUE_PLATFORMS:
            continue
        if topic not in (surface.get("domain_tags") or []):
            continue
        if _extract_domain(surface) != domain:
            continue
        matches.append(surface)

    if not matches:
        return None, None, None

    matches.sort(key=lambda s: (s.get("authority_tier", 99), s["key"]))
    best = matches[0]
    return best.get("authority_tier"), best.get("source_type"), best.get("audience_type")


def surface_metadata_for_domain(
    domain: str,
    surfaces_path: Path | str = _DEFAULT_SURFACES_PATH,
) -> tuple[int | None, str | None, str | None]:
    """Domain-only version of surface_metadata_for_pair(), for the
    search-queue reactive path (websearch.txt section 15.1): search finds
    a URL via a live user query with no (domain, topic) pair attached,
    only a trigger_query — there's nothing to match
    surface_metadata_for_pair()'s topic filter against.

    Deliberately NOT restricted to _QUEUE_PLATFORMS like
    build_discovery_queue()/surface_metadata_for_pair() are — that
    restriction exists because a site:<domain> WebSearch query only makes
    sense against a single browsable site (see this module's top-level
    docstring), which doesn't apply here: search has already found a
    specific URL by whatever means, this is only asking "does crawl
    recognize this domain as tier1/2 authoritative". Academic-API surfaces
    (europepmc/semanticscholar/crossref/biorxiv/doaj/openalex/
    clinicaltrials/core) are legitimate tier1/2 domains for this check —
    see websearch.txt section E's domain allowlist, which lists them
    alongside the html_crawl/playwright_crawl/sitemap domains.

    Same tie-break rule as surface_metadata_for_pair() when more than one
    surface matches: most trusted (lowest tier) wins, surface key breaks
    ties, for a deterministic result regardless of file order.

    Returns (None, None, None) if domain doesn't match any tier1/2 surface
    at all — callers (src/discovery/search_queue_loop.py) treat that as
    "out of scope", not an error.
    """
    with open(surfaces_path) as f:
        surfaces = json.load(f)

    matches = [
        surface
        for surface in surfaces
        if surface.get("authority_tier") in (1, 2)
        and _extract_domain(surface) == domain
    ]
    if not matches:
        return None, None, None

    matches.sort(key=lambda s: (s.get("authority_tier", 99), s["key"]))
    best = matches[0]
    return best.get("authority_tier"), best.get("source_type"), best.get("audience_type")
