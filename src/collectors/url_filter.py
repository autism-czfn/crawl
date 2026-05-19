"""Shared URL-quality filter for all web crawlers.

Every link a crawler considers following or storing must pass
``is_content_url()`` before it is enqueued or written to the DB.
This prevents nav-bar / footer / shop / account URLs from polluting
the ``crawled_items`` table.

Rules (applied in order):
  0. URL must have no fragment (``#anchor``) — fragment URLs point to
     the same page and are never independent content pages.
  1. URL must live on exactly ``seed_domain`` (netloc match).
  2. No path *segment* (split on ``/``) may appear in
     ``_BLOCKED_SEGMENTS``.  Segment matching avoids false positives
     from substring checks (e.g. the word "login" inside a legitimate
     article slug would not be a segment by itself).
  3. The path must contain at least **two** non-empty segments
     (rejects bare locale roots like ``/en/`` and domain homepages).
"""

from __future__ import annotations

from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Blocked path segments
# ---------------------------------------------------------------------------
# Any URL whose path contains one of these segments (case-insensitive, split
# on "/") will be rejected.  Add new entries here; both html_crawl and
# playwright_crawl pick them up automatically.
_BLOCKED_SEGMENTS: frozenset[str] = frozenset(
    {
        # ---- Authentication / user accounts ----
        "login",
        "logout",
        "sign-in",
        "signin",
        "sign-out",
        "signout",
        "register",
        "signup",
        "sign-up",
        "account",
        "my-account",
        "accounts",
        "profile",
        "dashboard",
        "password",
        "forgot-password",
        "reset-password",
        # ---- Shopping / e-commerce ----
        "cart",
        "checkout",
        "shop",
        "shopaap",  # AAP-specific shop subdirectory
        "store",
        "buy",
        "order",
        "orders",
        "purchase",
        # ---- Fundraising / membership ----
        "donate",
        "donation",
        "donations",
        "ways-to-give",
        "giving",
        "membership",
        "subscribe",
        "subscription",
        # ---- Employment ----
        "careers",
        "jobs",
        "employment",
        # ---- Navigation / taxonomy helpers ----
        "tag",
        "tags",
        "category",
        "categories",
        "author",
        "authors",
        "feed",
        "feeds",
        # ---- Utility / infra ----
        "search",
        "sitemap",
        "cdn-cgi",
        "wp-login",
        "wp-admin",
        # ---- Legal / policy pages ----
        "privacy",
        "terms",
        "terms-of-use",
        "terms-of-service",
        "disclaimer",
        "ad-disclaimer",
        "legal",
        "cookies",
        "cookie-policy",
    }
)


def is_content_url(
    url: str,
    seed_domain: str,
    min_path_segments: int = 2,
    child_link_domains: list[str] | None = None,
) -> bool:
    """Return ``True`` only when *url* looks like a real content page.

    Parameters
    ----------
    url:
        Absolute URL to evaluate.
    seed_domain:
        The ``netloc`` of the crawler's seed page (e.g. ``"www.aap.org"``).
        The URL must live on this exact domain, unless its domain appears in
        ``child_link_domains``.
    child_link_domains:
        Optional list of extra trusted domains (e.g. ``["pubmed.ncbi.nlm.nih.gov",
        "doi.org"]``) that are also allowed through Rule 1.  When a URL matches
        one of these domains the minimum-path-segments rule is relaxed to 1 so
        that canonical paper URLs (e.g. ``/12345678``) are not filtered out.

    Returns
    -------
    bool
        ``True`` → safe to follow/store.
        ``False`` → discard (nav/footer/utility/account link).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # Strip URL fragments (#anchor) — they point to the same page and are
    # never independent content URLs.  Checking the fragment-stripped URL
    # also prevents duplicate crawls of the same page via different anchors.
    if parsed.fragment:
        return False

    # Rule 1 — domain check
    allowed_domains = {seed_domain} | set(child_link_domains or [])
    if parsed.netloc not in allowed_domains:
        return False

    # Cross-domain links are academic paper URLs; relax min_path_segments to 1
    # so short canonical paths like /12345678 or /abs/2301.00001 pass through.
    if parsed.netloc != seed_domain:
        min_path_segments = min(min_path_segments, 1)

    # Split path into non-empty lower-cased segments
    segments = [s.lower() for s in parsed.path.split("/") if s]

    # Rule 2 — no blocked segment
    if any(seg in _BLOCKED_SEGMENTS for seg in segments):
        return False

    # Rule 3 — minimum path segments (default 2, configurable per surface)
    if len(segments) < min_path_segments:
        return False

    return True
