import json

from src.discovery.query_generator import _build_prompt, _parse_output


def test_build_prompt_includes_site_scope_and_known_urls():
    prompt = _build_prompt("cdc.gov", "sleep", ["https://cdc.gov/sleep/a", "https://cdc.gov/sleep/b"])
    assert "site:cdc.gov sleep" in prompt
    assert "https://cdc.gov/sleep/a" in prompt
    assert "https://cdc.gov/sleep/b" in prompt


def test_build_prompt_handles_no_known_urls():
    prompt = _build_prompt("cdc.gov", "sleep", [])
    assert "site:cdc.gov sleep" in prompt
    assert "(none yet)" in prompt


def test_parse_output_prefers_structured_output_field():
    payload = {
        "structured_output": {"results": [{"url": "https://cdc.gov/x", "title": "X"}]},
        "result": "should not be used",
    }
    out = _parse_output(json.dumps(payload).encode(), "cdc.gov", "sleep")
    assert out == [{"url": "https://cdc.gov/x", "title": "X"}]


def test_parse_output_falls_back_to_result_string():
    payload = {"result": json.dumps({"results": [{"url": "https://cdc.gov/y", "title": "Y"}]})}
    out = _parse_output(json.dumps(payload).encode(), "cdc.gov", "sleep")
    assert out == [{"url": "https://cdc.gov/y", "title": "Y"}]


def test_parse_output_drops_incomplete_candidates():
    payload = {"structured_output": {"results": [
        {"url": "https://cdc.gov/x"},          # missing title
        {"title": "no url"},                    # missing url
        {"url": "https://cdc.gov/z", "title": "Z"},
    ]}}
    out = _parse_output(json.dumps(payload).encode(), "cdc.gov", "sleep")
    assert out == [{"url": "https://cdc.gov/z", "title": "Z"}]


def test_parse_output_handles_malformed_json():
    assert _parse_output(b"not json", "cdc.gov", "sleep") == []


def test_parse_output_handles_missing_structured_data():
    assert _parse_output(json.dumps({"type": "result"}).encode(), "cdc.gov", "sleep") == []
