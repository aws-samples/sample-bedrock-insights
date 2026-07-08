from __future__ import annotations

import json

import pytest

from bedrock_insights import pricing
from bedrock_insights.pricing import (
    _derive_display_name,
    _normalize,
    load_overrides,
    lookup,
)


# ── _normalize ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Claude 3.5 Sonnet", "claude35sonnet"),
        ("anthropic.claude-x:0", "anthropicclaudex0"),
        ("Llama 3.2 90B", "llama3290b"),
    ],
)
def test_normalize(raw, expected):
    assert _normalize(raw) == expected


# ── _derive_display_name ─────────────────────────────────────────────────────
def test_derive_display_name_claude():
    assert _derive_display_name("anthropic.claude-sonnet-4-5-20250929-v1:0") == "Claude Sonnet 4.5"


def test_derive_display_name_strips_provider_and_version():
    # Provider prefix and trailing -v1:0 removed; words capitalised.
    assert _derive_display_name("cohere.command-r-v1:0") == "Command R"


# ── lookup ───────────────────────────────────────────────────────────────────
@pytest.fixture
def stub_pricing(monkeypatch):
    """Inject deterministic pricing caches so lookup() needs no AWS."""
    regional = {"claudetest": (3.0, 15.0, 3.75, 0.30, "Claude Test")}
    global_ = {"claudetest": (4.0, 20.0, 5.0, 0.40, "Claude Test (Global)")}
    monkeypatch.setattr(pricing, "_live_cache", regional)
    monkeypatch.setattr(pricing, "_global_cache", global_)
    monkeypatch.setattr(pricing, "_live_sorted", sorted(regional.items(), key=lambda x: -len(x[0])))
    monkeypatch.setattr(pricing, "_global_sorted", sorted(global_.items(), key=lambda x: -len(x[0])))
    monkeypatch.setattr(pricing, "_overrides", {})
    monkeypatch.setattr(pricing, "_model_names", {})


def test_lookup_regional(stub_pricing):
    p = lookup("anthropic.claude-test-v1:0")
    assert p.needs_pricing is False
    assert p.input_per_1m == 3.0
    assert p.output_per_1m == 15.0
    assert p.display_name == "Claude Test"


def test_lookup_prefers_global(stub_pricing):
    p = lookup("anthropic.claude-test", prefer_global=True)
    assert p.input_per_1m == 4.0
    assert p.display_name == "Claude Test (Global)"


def test_lookup_unknown_needs_pricing(stub_pricing):
    p = lookup("mistral.unknown-model")
    assert p.needs_pricing is True
    assert p.input_per_1m == 0.0
    # Falls back to a derived display name.
    assert "Unknown" in p.display_name or "unknown" in p.display_name.lower()


def test_lookup_uses_overrides(monkeypatch):
    monkeypatch.setattr(pricing, "_live_sorted", [])
    monkeypatch.setattr(pricing, "_global_sorted", [])
    monkeypatch.setattr(pricing, "_model_names", {})
    monkeypatch.setattr(
        pricing, "_overrides",
        {"my.custom-model": (1.0, 2.0, 0.0, 0.0, "My Custom")},
    )
    p = lookup("my.custom-model")
    assert p.needs_pricing is False
    assert p.input_per_1m == 1.0
    assert p.display_name == "My Custom"


# ── overrides file IO ────────────────────────────────────────────────────────
def test_load_overrides_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(pricing, "OVERRIDES_PATH", tmp_path / "nope.json")
    assert load_overrides() == {}


def test_load_overrides_parses_entries(monkeypatch, tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({
        "foo.model": {
            "input_per_1m": 1.5,
            "output_per_1m": 6.0,
            "display_name": "Foo",
        }
    }))
    monkeypatch.setattr(pricing, "OVERRIDES_PATH", path)
    data = load_overrides()
    assert data["foo.model"][0] == 1.5
    assert data["foo.model"][1] == 6.0
    assert data["foo.model"][4] == "Foo"


def test_load_overrides_corrupt_json(monkeypatch, tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{ not valid json ")
    monkeypatch.setattr(pricing, "OVERRIDES_PATH", path)
    assert load_overrides() == {}


# ── region-aware lookup ──────────────────────────────────────────────────────
def test_lookup_region_specific(monkeypatch):
    monkeypatch.setattr(pricing, "_region_tables", {
        "us-east-1": {"live_sorted": [("claudex", (3.0, 15.0, 0.0, 0.0, "Claude X"))], "global_sorted": []},
        "us-west-2": {"live_sorted": [("claudex", (6.0, 30.0, 0.0, 0.0, "Claude X"))], "global_sorted": []},
    })
    monkeypatch.setattr(pricing, "_live_sorted", [])
    monkeypatch.setattr(pricing, "_global_sorted", [])
    monkeypatch.setattr(pricing, "_overrides", {})
    monkeypatch.setattr(pricing, "_model_names", {})

    assert lookup("anthropic.claude-x", region="us-east-1").input_per_1m == 3.0
    assert lookup("anthropic.claude-x", region="us-west-2").input_per_1m == 6.0
    # Unknown region falls back to the (empty) default table → needs pricing.
    assert lookup("anthropic.claude-x", region="eu-west-1").needs_pricing is True


def test_lookup_region_none_uses_default(monkeypatch):
    monkeypatch.setattr(pricing, "_region_tables", {
        "us-east-1": {"live_sorted": [("claudex", (3.0, 15.0, 0.0, 0.0, "Claude X"))], "global_sorted": []},
    })
    monkeypatch.setattr(pricing, "_live_sorted", [("claudex", (9.0, 9.0, 0.0, 0.0, "Default"))])
    monkeypatch.setattr(pricing, "_global_sorted", [])
    monkeypatch.setattr(pricing, "_overrides", {})
    monkeypatch.setattr(pricing, "_model_names", {})
    # region=None → default table
    assert lookup("anthropic.claude-x").input_per_1m == 9.0


# ── debug output ─────────────────────────────────────────────────────────────
def test_debug_emits_to_stderr_only_when_enabled(capsys):
    pricing.set_debug(True)
    pricing._debug("hello-debug")
    assert "hello-debug" in capsys.readouterr().err

    pricing.set_debug(False)
    pricing._debug("should-not-show")
    assert "should-not-show" not in capsys.readouterr().err
