from __future__ import annotations

import bedrock_insights.client as c
from bedrock_insights.client import MAJOR_REGIONS, default_regions, parse_regions


def test_parse_regions_none():
    assert parse_regions(None) == []
    assert parse_regions("") == []


def test_parse_regions_single():
    assert parse_regions("us-east-1") == ["us-east-1"]


def test_parse_regions_multi_dedup_and_trim():
    assert parse_regions("us-east-1, us-west-2 ,us-east-1") == ["us-east-1", "us-west-2"]


def test_parse_regions_ignores_empty_segments():
    assert parse_regions("us-east-1,,eu-west-1,") == ["us-east-1", "eu-west-1"]


def test_default_regions_includes_all_majors(monkeypatch):
    monkeypatch.setattr(c, "_default_session_region", lambda profile: None)
    assert default_regions(None) == list(MAJOR_REGIONS)


def test_default_regions_prepends_own_region(monkeypatch):
    monkeypatch.setattr(c, "_default_session_region", lambda profile: "eu-west-2")
    regs = default_regions(None)
    assert regs[0] == "eu-west-2"
    for r in MAJOR_REGIONS:
        assert r in regs


def test_default_regions_no_duplicate_when_own_is_major(monkeypatch):
    monkeypatch.setattr(c, "_default_session_region", lambda profile: "us-east-1")
    regs = default_regions(None)
    assert regs.count("us-east-1") == 1


def test_make_clients_defaults_to_majors(monkeypatch):
    monkeypatch.setattr(c, "make_client", lambda region, profile: ("client", region))
    monkeypatch.setattr(c, "_default_session_region", lambda profile: None)
    regions = [r for r, _ in c.make_clients(None, None)]
    assert regions == list(MAJOR_REGIONS)


def test_make_clients_explicit_overrides_default(monkeypatch):
    monkeypatch.setattr(c, "make_client", lambda region, profile: ("client", region))
    regions = [r for r, _ in c.make_clients("eu-west-3", None)]
    assert regions == ["eu-west-3"]
