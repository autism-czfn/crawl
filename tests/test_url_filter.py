"""Tests for src/collectors/url_filter.py — the shared content-URL gate used
by html_crawl and playwright_crawl.

Covers both the pre-existing behavior (must not regress) and the fix made
while adding the sleep/eating/adhd hospital sources: exact-string segment
matching was letting extension-suffixed nav segments like "login.aspx"
through, because "login.aspx" != "login".
"""
from src.collectors.url_filter import is_content_url

DOMAIN = "www.childrenshospital.org"


def test_blocks_plain_blocked_segment():
    assert is_content_url(f"https://{DOMAIN}/donate", DOMAIN) is False


def test_allows_ordinary_content_url():
    assert is_content_url(f"https://{DOMAIN}/conditions/adhd", DOMAIN) is True


def test_rejects_bare_homepage():
    assert is_content_url(f"https://{DOMAIN}/", DOMAIN) is False


def test_rejects_off_domain_url():
    assert is_content_url("https://other-site.org/conditions/adhd", DOMAIN) is False


def test_rejects_fragment_url():
    assert is_content_url(f"https://{DOMAIN}/conditions/adhd#overview", DOMAIN) is False


def test_extension_suffixed_blocked_segment_is_caught():
    """Regression test for the bug found while verifying healthychildren.org:
    a segment like "login.aspx" must be blocked the same way "login" is,
    not slip through on an exact-string mismatch."""
    url = "https://www.healthychildren.org/english/pages/login.aspx"
    assert is_content_url(url, "www.healthychildren.org") is False


def test_extension_suffixed_non_blocked_segment_still_allowed():
    """The extension-stripping fix must not become a blanket ban on all
    .aspx/.html pages — only ones whose base name is itself blocked."""
    url = "https://www.healthychildren.org/english/health-issues/adhd.aspx"
    assert is_content_url(url, "www.healthychildren.org") is True


def test_new_hospital_nav_segments_are_blocked():
    """Segments added this round after observing them pollute link discovery
    on childrenshospital.org / chop.edu."""
    for segment in (
        "find-a-doctor", "find-a-provider", "request-appointment",
        "make-an-appointment", "schedule-appointment", "pay-your-bill-online",
        "pay-bill", "billing-insurance", "doctors-departments", "sponsors",
        "events",
    ):
        url = f"https://{DOMAIN}/{segment}/some-page"
        assert is_content_url(url, DOMAIN) is False, f"{segment} should be blocked"


def test_min_path_segments_still_enforced():
    assert is_content_url(f"https://{DOMAIN}/en/", DOMAIN, min_path_segments=2) is False
    assert is_content_url(f"https://{DOMAIN}/en/adhd", DOMAIN, min_path_segments=2) is True


def test_child_link_domains_relaxes_segment_requirement():
    assert is_content_url(
        "https://pubmed.ncbi.nlm.nih.gov/12345678",
        DOMAIN,
        child_link_domains=["pubmed.ncbi.nlm.nih.gov"],
    ) is True


def test_multiword_blocked_phrase_caught_inside_compound_segment():
    """Regression test for a bug found live-testing hopkins_sleep/hopkins_eating:
    hopkinsmedicine.org links to a single path segment
    "johns-hopkins-medicine-request-appointment" that CONTAINS but does not
    equal the blocked phrase "request-appointment" — the original
    whole-segment-only match let it through."""
    url = "https://www.hopkinsmedicine.org/patient-care/johns-hopkins-medicine-request-appointment"
    assert is_content_url(url, "www.hopkinsmedicine.org") is False


def test_mychart_patient_portal_segment_blocked():
    url = "https://www.hopkinsmedicine.org/patient-care/mychart"
    assert is_content_url(url, "www.hopkinsmedicine.org") is False


def test_single_word_blocked_term_still_requires_whole_segment_match():
    """The subsequence-matching fix is scoped to multi-word blocked phrases
    only — a single blocked word like "login" appearing as one token among
    several in an unrelated compound segment must NOT be blocked (this is
    the exact false-positive the module's original docstring warns about)."""
    url = f"https://{DOMAIN}/blog/the-login-page-redesign-explained"
    assert is_content_url(url, DOMAIN) is True


def test_multiword_phrase_tokens_out_of_order_not_blocked():
    """Subsequence matching requires the blocked phrase's tokens in order
    and contiguous — "appointment-request" (reversed) must not trip the
    "request-appointment" blocklist entry."""
    url = f"https://{DOMAIN}/services/appointment-request-info"
    assert is_content_url(url, DOMAIN) is True
