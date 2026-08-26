"""Tests for src/pipeline.py's evidence_level inference — extended this round
with platform-driven branches (preprint, clinical_trial, government_guidance,
hospital_education, peer_reviewed_study) so government/hospital pages that
never contain a keyword like "systematic review" still get a real value
instead of falling through to None.
"""
from src.pipeline import _infer_evidence_level


def test_keyword_branches_unchanged():
    """Pre-existing keyword-based inference must still work exactly as before."""
    assert _infer_evidence_level(
        "A systematic review of pediatric sleep interventions", None, "academic", "pubmed"
    ) == "systematic_review"
    assert _infer_evidence_level(
        "A randomized controlled trial of ADHD medication", None, None, None
    ) == "rct"


def test_source_type_anecdotal_and_blog_unchanged():
    assert _infer_evidence_level("x", None, "reddit", None) == "anecdotal"
    assert _infer_evidence_level("x", None, "social", None) == "anecdotal"
    assert _infer_evidence_level("x", None, "blog", None) == "blog"


def test_biorxiv_platform_maps_to_preprint():
    assert _infer_evidence_level(
        "New findings on ADHD genetics", None, "academic", "biorxiv"
    ) == "preprint"


def test_clinicaltrials_platform_maps_to_clinical_trial():
    assert _infer_evidence_level(
        "A trial of a new sleep intervention", None, "academic", "clinicaltrials"
    ) == "clinical_trial"


def test_official_health_html_crawl_maps_to_government_guidance():
    assert _infer_evidence_level(
        "ADHD | CDC", None, "official_health", "html_crawl"
    ) == "government_guidance"
    assert _infer_evidence_level(
        "Autism", None, "official_health", "playwright_crawl"
    ) == "government_guidance"
    assert _infer_evidence_level(
        "Autism condition page", None, "official_health", "nhs_api"
    ) == "government_guidance"


def test_hospital_source_type_maps_to_hospital_education():
    assert _infer_evidence_level(
        "Sleep disorders", None, "hospital", "html_crawl"
    ) == "hospital_education"


def test_academic_source_type_without_keyword_falls_back_to_peer_reviewed_study():
    assert _infer_evidence_level(
        "Prevalence of ADHD in school-age children", None, "academic", "openalex"
    ) == "peer_reviewed_study"


def test_platform_branch_does_not_override_keyword_match():
    """Keyword match takes priority over the platform-driven fallback — a
    biorxiv preprint that IS a systematic review should still be tagged
    systematic_review, not silently downgraded to "preprint"."""
    assert _infer_evidence_level(
        "A systematic review posted as a preprint", None, "academic", "biorxiv"
    ) == "systematic_review"


def test_no_signal_returns_none():
    assert _infer_evidence_level("Just a title", None, None, None) is None
