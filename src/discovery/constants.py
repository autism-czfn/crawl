"""Canonical enum values for a surfaces.json tier1/2 entry.

Shared by tests/test_surfaces_config.py (validates every hand-added
surface against these) and src/discovery/classifier.py (constrains the
auto-classifier's `claude -p` output — see DOMAIN_CLASSIFY_SCHEMA in
schemas.py — to the same enums, so an auto-added surface can never carry
a source_type/audience_type/domain_tags value the rest of the system
doesn't already recognize). Previously these three sets lived only in
the test file; moved here because src/ code shouldn't import from tests/.
"""

VALID_SOURCE_TYPES = {
    "official_health", "academic", "hospital", "nonprofit", "community",
    "news", "social",
}
VALID_AUDIENCE_TYPES = {"parent_facing", "clinician_facing", "mixed", "research"}
VALID_DOMAIN_TAGS = {"sleep", "eating", "behavioral", "adhd", "autism"}
