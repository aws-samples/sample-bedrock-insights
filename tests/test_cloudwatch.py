from __future__ import annotations

import pytest

from bedrock_insights.cloudwatch import (
    get_time_range,
    normalize_model_id,
    parse_since,
)


# ── parse_since ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value, seconds",
    [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("1.5h", 5400)],
)
def test_parse_since_durations(value, seconds):
    start_ms, end_ms = parse_since(value)
    assert end_ms >= start_ms
    # Window span should match the requested duration (within 2s of clock drift).
    assert abs((end_ms - start_ms) - seconds * 1000) <= 2000


@pytest.mark.parametrize("value", ["", "10", "h", "10x", "abc", "-5m", "10 mins"])
def test_parse_since_invalid(value):
    with pytest.raises(ValueError):
        parse_since(value)


def test_parse_since_tolerates_whitespace_and_case():
    start_ms, end_ms = parse_since("  2H ")
    assert abs((end_ms - start_ms) - 7200 * 1000) <= 2000


# ── get_time_range ───────────────────────────────────────────────────────────
def test_get_time_range_today():
    start_ms, end_ms = get_time_range("today")
    assert start_ms < end_ms
    span_h = (end_ms - start_ms) / 3.6e6
    assert 0 <= span_h <= 24


def test_get_time_range_week_is_seven_days():
    start_ms, end_ms = get_time_range("week")
    span_days = (end_ms - start_ms) / 8.64e7
    assert abs(span_days - 7) < 0.01


def test_get_time_range_yesterday_full_day():
    start_ms, end_ms = get_time_range("yesterday")
    span_h = (end_ms - start_ms) / 3.6e6
    assert abs(span_h - 24) < 0.01


def test_get_time_range_unknown_raises():
    with pytest.raises(ValueError):
        get_time_range("decade")


# ── normalize_model_id ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("us.anthropic.claude-3-5-sonnet:0", "anthropic.claude-3-5-sonnet"),
        ("eu.anthropic.claude-sonnet-4", "anthropic.claude-sonnet-4"),
        ("global.anthropic.claude-x:1", "anthropic.claude-x"),
        ("anthropic.claude-3:0", "anthropic.claude-3"),
        (
            "arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic.claude-x:0",
            "anthropic.claude-x",
        ),
    ],
)
def test_normalize_model_id(raw, expected):
    assert normalize_model_id(raw) == expected

