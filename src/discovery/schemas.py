"""--json-schema payloads for the `claude -p` calls in the discovery layer
(crawl.txt section 13). Kept as plain dicts (not pydantic models) because
their only job is to be json.dumps()'d straight into the CLI's
--json-schema flag and to json.loads() the reply — there's no ORM/DB
object on the other end, `claude -p` never writes into crawled_items
directly (see section 13.4: it only ever supplies candidate URLs, the
existing pipeline.py does the real fetch/store).
"""
from src.discovery.constants import VALID_AUDIENCE_TYPES, VALID_SOURCE_TYPES

# Used by query_generator.py's single (domain, topic) WebSearch call —
# see crawl.txt section 3 "RETEST 2026-08-26": narrowed to one domain/one
# topic per call, metadata only (url + title), never full body text or a
# written summary (that's what keeps `claude -p` out of the "decides
# what's trustworthy" role — section 2/4 draw that line at the code level,
# not the prompt level).
#
# Deliberately no "snippet"/"publish_date" fields even though section 3's
# prose mentions collecting them: the 2026-08-26 retest that actually
# proved this shape works only asked for "up to 5 real result URLs+titles"
# and enforcing extra fields in the schema forces the model to invent
# a value when it doesn't have one on hand, which title-only avoids.
# authority_filter.py + crawled_items.url dedup (section 13.3 layer 2)
# happen in code afterwards regardless of what's returned here.
#
# maxItems raised 5 -> 10 (2026-08-28): checked the first two real
# discovery_loop() runs before touching this — neither actually hit the
# old cap of 5 (0 and 3 results respectively), so 5 was never the
# bottleneck for what's been observed so far. Raised anyway as pure
# headroom for less-picked-over domain/topic pairs later in the 48-pair
# queue: measured real cost per call so far (~$0.03-0.06, both live
# WebSearch tests) sits well under the $0.20 --max-budget-usd cap, so
# there's real margin to let a call return more before it'd ever hit
# that ceiling.
DISCOVERY_RESULTS_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["url", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

# Used by classifier.py's single-domain "should this be tier1/2" call —
# websearch.txt section 十九 made automatic. tier/source_type/audience_type
# are nullable (a reject has no tier to report); source_type/audience_type
# are constrained to the same enums src/discovery/constants.py and
# test_surfaces_config.py already validate every hand-added surface
# against, so an auto-added entry can never carry a value the rest of the
# system doesn't recognize. confidence and reason are always required —
# the model must say how sure it is and why, even on a reject, so an
# out_of_scope row stays legible in the setup.sh option-10 report instead
# of going back to being silent.
DOMAIN_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"type": ["integer", "null"], "enum": [1, 2, None]},
        "source_type": {"type": ["string", "null"], "enum": sorted(VALID_SOURCE_TYPES) + [None]},
        "audience_type": {"type": ["string", "null"], "enum": sorted(VALID_AUDIENCE_TYPES) + [None]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["tier", "confidence", "reason"],
    "additionalProperties": False,
}
