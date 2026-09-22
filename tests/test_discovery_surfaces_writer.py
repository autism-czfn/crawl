"""Tests for src/discovery/surfaces_writer.py — always against a tmp_path
copy of a small fixture list, never the real config/surfaces.json (this
module writes)."""
import json

from src.discovery.surfaces_writer import append_surface_entry, build_auto_surface_entry

_FIXTURE = (
    '[\n'
    '  {"key": "existing_tier1", "enabled": 1, "authority_tier": 1, "platform": "html_crawl", '
    '"poll_interval_sec": 86400, "max_items": 15, "config": {"base_url": "https://existing.gov/"}}\n'
    ']\n'
)


def _write_fixture(tmp_path):
    path = tmp_path / "surfaces.json"
    path.write_text(_FIXTURE)
    return path


def test_build_auto_surface_entry_shape():
    entry = build_auto_surface_entry(
        "newsite.gov", 1, "official_health", "parent_facing", "national health agency", "2026-08-31",
    )
    assert entry["key"] == "auto_newsite_gov"
    assert entry["enabled"] == 1
    assert entry["authority_tier"] == 1
    assert entry["platform"] == "html_crawl"
    assert entry["config"]["base_url"] == "https://newsite.gov/"
    assert entry["source_type"] == "official_health"
    assert entry["audience_type"] == "parent_facing"
    assert "AUTO-ADDED 2026-08-31" in entry["content_notes"]
    assert "national health agency" in entry["content_notes"]


def test_append_surface_entry_writes_new_entry_atomically(tmp_path):
    path = _write_fixture(tmp_path)
    entry = build_auto_surface_entry("newsite.gov", 1, "official_health", None, "reason", "2026-08-31")

    assert append_surface_entry(entry, surfaces_path=path) is True

    surfaces = json.loads(path.read_text())
    assert len(surfaces) == 2
    assert surfaces[0]["key"] == "existing_tier1"
    assert surfaces[1]["key"] == "auto_newsite_gov"
    assert surfaces[1]["config"]["base_url"] == "https://newsite.gov/"

    # One-entry-per-line style preserved, not reformatted into indent=2
    # pretty JSON — see surfaces_writer.py's docstring for why this matters.
    lines = path.read_text().splitlines()
    assert lines[0] == "["
    assert lines[-1] == "]"
    assert lines[1].rstrip(",").strip().startswith('{"key": "existing_tier1"')
    assert lines[2].strip().startswith('{"key": "auto_newsite_gov"')


def test_append_surface_entry_rejects_duplicate_key(tmp_path):
    path = _write_fixture(tmp_path)
    entry = build_auto_surface_entry("existing.gov", 2, None, None, "reason", "2026-08-31")
    entry["key"] = "existing_tier1"  # collides with the fixture's only entry

    assert append_surface_entry(entry, surfaces_path=path) is False
    assert len(json.loads(path.read_text())) == 1


def test_append_surface_entry_rejects_domain_already_covered(tmp_path):
    path = _write_fixture(tmp_path)
    # Different key, but resolves to the same domain the fixture already
    # covers as tier1 — must be rejected even though the key is unique.
    entry = build_auto_surface_entry("existing.gov", 2, None, None, "reason", "2026-08-31")

    assert append_surface_entry(entry, surfaces_path=path) is False
    assert len(json.loads(path.read_text())) == 1


def test_append_surface_entry_rejects_invalid_entry(tmp_path):
    path = _write_fixture(tmp_path)
    entry = build_auto_surface_entry("newsite.gov", 1, None, None, "reason", "2026-08-31")
    del entry["config"]["base_url"]

    assert append_surface_entry(entry, surfaces_path=path) is False
    assert len(json.loads(path.read_text())) == 1


def test_append_surface_entry_refuses_to_write_on_unparseable_existing_file(tmp_path):
    path = tmp_path / "surfaces.json"
    path.write_text("{not valid json")
    entry = build_auto_surface_entry("newsite.gov", 1, None, None, "reason", "2026-08-31")

    assert append_surface_entry(entry, surfaces_path=path) is False
    assert path.read_text() == "{not valid json"
