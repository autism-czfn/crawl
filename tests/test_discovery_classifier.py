import json

from src.discovery.classifier import _build_prompt, _parse_classification


def test_build_prompt_includes_domain_url_and_context():
    prompt = _build_prompt(
        "newsite.gov", "https://newsite.gov/adhd", "ADHD Guidance",
        "National guidance on ADHD diagnosis", "how is adhd diagnosed",
    )
    assert "newsite.gov" in prompt
    assert "https://newsite.gov/adhd" in prompt
    assert "ADHD Guidance" in prompt
    assert "National guidance on ADHD diagnosis" in prompt
    assert "how is adhd diagnosed" in prompt


def test_build_prompt_handles_missing_context():
    prompt = _build_prompt("newsite.gov", "https://newsite.gov/x", None, None, None)
    assert "(none)" in prompt
    assert "(unknown)" in prompt


def test_parse_classification_accepts_valid_high_confidence_tier1():
    payload = {
        "structured_output": {
            "tier": 1, "source_type": "official_health", "audience_type": "parent_facing",
            "confidence": "high", "reason": "national health agency",
        },
    }
    out = _parse_classification(json.dumps(payload).encode(), "newsite.gov")
    assert out == {
        "tier": 1, "source_type": "official_health", "audience_type": "parent_facing",
        "confidence": "high", "reason": "national health agency",
    }


def test_parse_classification_falls_back_to_result_string():
    payload = {"result": json.dumps({"tier": 2, "confidence": "medium", "reason": "secondary source"})}
    out = _parse_classification(json.dumps(payload).encode(), "newsite.gov")
    assert out["tier"] == 2
    assert out["confidence"] == "medium"
    assert out["source_type"] is None
    assert out["audience_type"] is None


def test_parse_classification_accepts_reject_with_null_tier():
    payload = {"structured_output": {"tier": None, "confidence": "high", "reason": "personal blog"}}
    out = _parse_classification(json.dumps(payload).encode(), "newsite.gov")
    assert out == {
        "tier": None, "source_type": None, "audience_type": None,
        "confidence": "high", "reason": "personal blog",
    }


def test_parse_classification_handles_malformed_json():
    assert _parse_classification(b"not json", "newsite.gov") is None


def test_parse_classification_handles_missing_structured_data():
    assert _parse_classification(json.dumps({"type": "result"}).encode(), "newsite.gov") is None


def test_parse_classification_rejects_invalid_tier():
    payload = {"structured_output": {"tier": 3, "confidence": "high", "reason": "x"}}
    assert _parse_classification(json.dumps(payload).encode(), "newsite.gov") is None


def test_parse_classification_rejects_invalid_confidence():
    payload = {"structured_output": {"tier": 1, "confidence": "certain", "reason": "x"}}
    assert _parse_classification(json.dumps(payload).encode(), "newsite.gov") is None


def test_parse_classification_rejects_missing_reason():
    payload = {"structured_output": {"tier": 1, "confidence": "high", "reason": ""}}
    assert _parse_classification(json.dumps(payload).encode(), "newsite.gov") is None
