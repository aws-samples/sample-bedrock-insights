from __future__ import annotations

import csv
import io

import pytest
from botocore.exceptions import ClientError

from bedrock_insights import web
from bedrock_insights.pricing import ModelPricing


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def fixed_pricing(monkeypatch):
    """Deterministic pricing: input $3, output $15, cache write $3.75, read $0.30 / 1M."""
    monkeypatch.setattr(
        web, "lookup",
        lambda model_id, prefer_global=False, region=None: ModelPricing(3.0, 15.0, 3.75, 0.30, "Claude Test", False),
    )


def _rec(eid, model="anthropic.claude-x", inp=0, out=0, cw=0, cr=0,
         ts=None, arn="", region="us-east-1", error=None):
    r = {
        "_eventId": eid,
        "modelId": model,
        "region": region,
        "input": {
            "inputTokenCount": inp,
            "cacheWriteInputTokenCount": cw,
            "cacheReadInputTokenCount": cr,
        },
        "output": {"outputTokenCount": out},
    }
    if ts:
        r["timestamp"] = ts
    if arn:
        r["identity"] = {"arn": arn}
    if error:
        r["errorCode"] = error
        r.pop("output")
    return r


# ── _identity_key ────────────────────────────────────────────────────────────
def test_identity_key_assumed_role_collapses_session():
    assert web._identity_key("arn:aws:sts::123:assumed-role/AppRole/sess-1") == \
        ("assumed-role/AppRole", "AppRole")


def test_identity_key_iam_user():
    assert web._identity_key("arn:aws:iam::123:user/alice") == ("user/alice", "alice")


def test_identity_key_empty():
    assert web._identity_key("") == ("unknown", "unknown")


def test_identity_key_prefers_request_metadata_team():
    # When requestMetadata carries a team, group by it — not the shared ARN.
    rec = {
        "requestMetadata": {"team": "marketing"},
        "identity": {"arn": "arn:aws:sts::123:assumed-role/SharedProxyRole/sess-9"},
    }
    assert web._identity_key(rec["identity"]["arn"], rec) == ("team/marketing", "marketing")


def test_identity_key_falls_back_to_arn_without_team():
    # requestMetadata present but no team key → fall back to ARN collapsing.
    rec = {
        "requestMetadata": {"environment": "prod"},
        "identity": {"arn": "arn:aws:sts::123:assumed-role/AppRole/sess-1"},
    }
    assert web._identity_key(rec["identity"]["arn"], rec) == ("assumed-role/AppRole", "AppRole")


def test_identity_key_custom_team_meta_key(monkeypatch):
    # BEDROCK_INSIGHTS_TEAM_KEY overrides which metadata key is the team.
    monkeypatch.setattr(web, "_TEAM_META_KEY", "tenant")
    rec = {"requestMetadata": {"tenant": "acme"}, "identity": {"arn": ""}}
    assert web._identity_key("", rec) == ("team/acme", "acme")


def test_identity_key_session_prefix_recovers_team(monkeypatch):
    # No requestMetadata team (e.g. an InvokeModel call), but the STS session
    # name encodes it as "litellm-<team>" → recover the team from the ARN.
    monkeypatch.setattr(web, "_TEAM_SESSION_PREFIX", "litellm-")
    arn = "arn:aws:sts::123:assumed-role/ProxyExecRole/litellm-sales"
    assert web._identity_key(arn) == ("team/sales", "sales")


def test_identity_key_session_prefix_prefers_request_metadata(monkeypatch):
    # requestMetadata team still wins over the session-name prefix when present.
    monkeypatch.setattr(web, "_TEAM_SESSION_PREFIX", "litellm-")
    rec = {
        "requestMetadata": {"team": "marketing"},
        "identity": {"arn": "arn:aws:sts::123:assumed-role/Role/litellm-sales"},
    }
    assert web._identity_key(rec["identity"]["arn"], rec) == ("team/marketing", "marketing")


def test_identity_key_session_prefix_no_match_falls_back(monkeypatch):
    # Session name without the prefix → normal assumed-role collapsing.
    monkeypatch.setattr(web, "_TEAM_SESSION_PREFIX", "litellm-")
    arn = "arn:aws:sts::123:assumed-role/AppRole/sess-1"
    assert web._identity_key(arn) == ("assumed-role/AppRole", "AppRole")


def test_identity_key_session_prefix_disabled_by_default():
    # Without the env var configured, session names are never parsed as teams.
    arn = "arn:aws:sts::123:assumed-role/ProxyExecRole/litellm-sales"
    assert web._identity_key(arn) == ("assumed-role/ProxyExecRole", "ProxyExecRole")


# ── build_payload ────────────────────────────────────────────────────────────
def test_build_payload_cost_and_totals(fixed_pricing):
    usage = {
        "anthropic.claude-x": {
            "calls": 2, "input_tokens": 1_000_000, "output_tokens": 1_000_000,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "is_global": False,
        }
    }
    payload = web.build_payload(usage)
    # 1M input @ $3 + 1M output @ $15 = $18
    assert payload["totals"]["cost"] == pytest.approx(18.0)
    assert payload["totals"]["calls"] == 2
    assert payload["totals"]["avg_cost_per_call"] == pytest.approx(9.0)
    assert payload["models"][0]["cost_share"] == pytest.approx(1.0)
    assert payload["has_cache"] is False


def test_build_payload_cache_hit_rate(fixed_pricing):
    usage = {
        "m": {"calls": 1, "input_tokens": 750, "output_tokens": 0,
              "cache_write_tokens": 0, "cache_read_tokens": 250, "is_global": False}
    }
    payload = web.build_payload(usage)
    assert payload["has_cache"] is True
    # cache_read / (input + cache_read) = 250 / 1000
    assert payload["totals"]["cache_hit_rate"] == pytest.approx(0.25)


def test_build_payload_shares_sum_to_one(fixed_pricing):
    usage = {
        "a": {"calls": 1, "input_tokens": 1000, "output_tokens": 0,
              "cache_write_tokens": 0, "cache_read_tokens": 0, "is_global": False},
        "b": {"calls": 1, "input_tokens": 3000, "output_tokens": 0,
              "cache_write_tokens": 0, "cache_read_tokens": 0, "is_global": False},
    }
    payload = web.build_payload(usage)
    assert sum(m["cost_share"] for m in payload["models"]) == pytest.approx(1.0)


# ── UsageMonitor ─────────────────────────────────────────────────────────────
def _monitor(monkeypatch, records):
    monkeypatch.setattr(web, "iter_log_events", lambda client, s, e: iter(list(records)))
    return web.UsageMonitor(clients=[("us-east-1", None)], period="today", since=None)


def test_monitor_empty_before_prime(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [])
    assert m.snapshot()["totals"]["calls"] == 0


def test_monitor_prime_aggregates(fixed_pricing, monkeypatch):
    recs = [_rec("a", inp=100, out=50), _rec("b", inp=200, out=80)]
    m = _monitor(monkeypatch, recs)
    m.prime()
    snap = m.snapshot()
    assert snap["totals"]["calls"] == 2
    assert snap["totals"]["input_tokens"] == 300


def test_monitor_dedup(fixed_pricing, monkeypatch):
    recs = [_rec("a", inp=100), _rec("b", inp=200)]
    m = _monitor(monkeypatch, recs)
    m.prime()
    m._ingest(m._start_ms)  # same events again
    assert m.snapshot()["totals"]["calls"] == 2


def test_monitor_new_event_increments(fixed_pricing, monkeypatch):
    recs = [_rec("a", inp=100)]
    m = _monitor(monkeypatch, recs)
    m.prime()
    recs.append(_rec("b", inp=10))
    m._ingest(m._start_ms)
    assert m.snapshot()["totals"]["calls"] == 2


def test_monitor_trend_buckets_sum_to_total(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    # Two events ~2h apart → distinct hourly buckets in the week view (clock-robust).
    recs = [
        _rec("a", inp=1000, out=200, ts=(now - timedelta(hours=3)).isoformat()),
        _rec("b", inp=2000, out=400, ts=(now - timedelta(hours=1)).isoformat()),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    snap = m.snapshot("week")
    assert snap["trend"]["bucket_seconds"] == 3600  # week → hourly buckets
    assert len(snap["trend"]["points"]) == 2
    bsum = round(sum(p["cost"] for p in snap["trend"]["points"]), 6)
    assert bsum == pytest.approx(snap["totals"]["cost"])


def test_monitor_identity_breakdown(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", inp=1000, arn="arn:aws:sts::1:assumed-role/AppRole/s1"),
        _rec("2", inp=500, arn="arn:aws:sts::1:assumed-role/AppRole/s2"),
        _rec("3", inp=2000, arn="arn:aws:iam::1:user/bob"),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    ids = {i["label"]: i for i in m.snapshot()["identities"]}
    assert set(ids) == {"AppRole", "bob"}
    assert ids["AppRole"]["calls"] == 2  # two sessions merged
    assert ids["bob"]["calls"] == 1


def test_monitor_region_breakdown(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", inp=100, region="us-east-1"),
        _rec("2", inp=100, region="us-east-1"),
        _rec("3", inp=100, region="eu-west-1"),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    regs = {r["region"]: r for r in m.snapshot()["regions"]}
    assert regs["us-east-1"]["calls"] == 2
    assert regs["eu-west-1"]["calls"] == 1


def test_monitor_error_breakdown(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", inp=100),
        _rec("2", error="ThrottlingException"),
        _rec("3", error="AccessDeniedException"),
        _rec("4", error="ThrottlingException"),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    err = m.snapshot()["errors"]
    assert err["total"] == 3
    assert err["rate"] == pytest.approx(0.75)
    by_code = {c["code"]: c["count"] for c in err["by_code"]}
    assert by_code == {"ThrottlingException": 2, "AccessDeniedException": 1}


def test_monitor_client_error_sets_warning_and_preserves_data(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("a", inp=100)])
    m.prime()
    assert m.snapshot()["totals"]["calls"] == 1

    def boom(client, s, e):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            "FilterLogEvents",
        )

    monkeypatch.setattr(web, "iter_log_events", boom)
    m._ingest(m._start_ms)
    snap = m.snapshot()
    assert snap["warning"]["code"] == "AccessDeniedException"
    assert snap["totals"]["calls"] == 1  # cached data preserved


def test_monitor_thread_lifecycle(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [])
    m.start()
    assert m._thread is not None and m._thread.is_alive()
    m.stop()
    m._thread.join(timeout=2)
    assert not m._thread.is_alive()


# ── render_prometheus ────────────────────────────────────────────────────────
def test_prometheus_contains_core_metrics(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", inp=1000, out=200, region="us-east-1",
             arn="arn:aws:iam::1:user/alice"),
        _rec("2", error="ThrottlingException", region="us-east-1",
             arn="arn:aws:iam::1:user/alice"),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    text = web.render_prometheus(m.snapshot())

    # Family declarations present.
    for name in [
        "bedrock_cost_usd", "bedrock_calls", "bedrock_tokens",
        "bedrock_error_rate", "bedrock_model_cost_usd", "bedrock_model_tokens",
        "bedrock_identity_calls", "bedrock_region_calls", "bedrock_errors",
    ]:
        assert f"# TYPE {name} gauge" in text, name

    # A labelled sample line for the error code.
    assert 'bedrock_errors{code="ThrottlingException"} 1' in text
    # Identity label rendered.
    assert 'identity="alice"' in text
    # Region label rendered.
    assert 'region="us-east-1"' in text
    # Token type label rendered.
    assert 'type="input"' in text


def test_prometheus_escapes_label_values():
    assert web._prom_escape('a"b\\c') == 'a\\"b\\\\c'


def test_prometheus_well_formed_lines(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("1", inp=10, out=5)])
    m.prime()
    text = web.render_prometheus(m.snapshot())
    # Every non-comment, non-empty line must have exactly "name{...} value" or "name value".
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert line.count(" ") >= 1
        value = line.rsplit(" ", 1)[1]
        float(value)  # value must parse as a number


# ── compute_projection ───────────────────────────────────────────────────────
def test_compute_projection_run_rate():
    # $6 spent over 2 hours → $3/hr → $72/day → $2160/month
    now = 1_000_000_000_000
    start = now - 2 * 3_600_000
    proj = web.compute_projection(6.0, start, now)
    assert proj["burn_per_hour"] == pytest.approx(3.0)
    assert proj["projected_daily"] == pytest.approx(72.0)
    assert proj["projected_monthly"] == pytest.approx(2160.0)


def test_compute_projection_zero_cost():
    now = 1_000_000_000_000
    proj = web.compute_projection(0.0, now - 3_600_000, now)
    assert proj["projected_daily"] == 0.0


def test_monitor_includes_projection(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("1", inp=1000, out=200)])
    m.prime()
    assert "projection" in m.snapshot()
    assert "projected_daily" in m.snapshot()["projection"]


# ── period switching & filtering ─────────────────────────────────────────────
def test_monitor_filter_by_model(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", model="anthropic.claude-x", inp=1000),
        _rec("2", model="anthropic.claude-x", inp=500),
        _rec("3", model="meta.llama-y", inp=2000),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    full = m.snapshot()
    assert full["totals"]["calls"] == 3

    filtered = m.snapshot(None, {"model": "anthropic.claude-x"})
    assert filtered["totals"]["calls"] == 2
    assert filtered["totals"]["input_tokens"] == 1500
    assert {x["model_id"] for x in filtered["models"]} == {"anthropic.claude-x"}
    assert filtered["filter"] == {"model": "anthropic.claude-x"}


def test_monitor_filter_by_identity(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", inp=100, arn="arn:aws:iam::1:user/alice"),
        _rec("2", inp=100, arn="arn:aws:iam::1:user/bob"),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    filtered = m.snapshot(None, {"identity": "user/alice"})
    assert filtered["totals"]["calls"] == 1
    assert [i["label"] for i in filtered["identities"]] == ["alice"]


def test_monitor_filter_by_region(fixed_pricing, monkeypatch):
    recs = [
        _rec("1", inp=100, region="us-east-1"),
        _rec("2", inp=100, region="eu-west-1"),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    filtered = m.snapshot(None, {"region": "eu-west-1"})
    assert filtered["totals"]["calls"] == 1
    assert [r["region"] for r in filtered["regions"]] == ["eu-west-1"]


def test_monitor_period_switch_today_vs_week(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recs = [
        _rec("now", inp=100),  # no ts → t = now, always within today and week
        _rec("old", inp=100, ts=(now - timedelta(days=2)).isoformat()),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    # Today excludes the 2-day-old event; week includes both.
    assert m.snapshot("today")["totals"]["calls"] == 1
    assert m.snapshot("week")["totals"]["calls"] == 2
    assert m.snapshot("today")["window"]["period"] == "today"
    assert m.snapshot("week")["trend"]["bucket_seconds"] == 3600


def test_monitor_periods_list_default(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [])
    ids = [p["id"] for p in m.periods()]
    assert ids == ["today", "yesterday", "week"]
    assert m._default_period == "today"


def test_monitor_custom_since_window(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recs = [
        _rec("recent", inp=100, ts=(now - timedelta(minutes=30)).isoformat()),
        _rec("old", inp=100, ts=(now - timedelta(hours=5)).isoformat()),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    # A 1h custom window includes only the recent event.
    snap = m.snapshot(period="week", since="1h")
    assert snap["window"]["period"] == "custom"
    assert snap["totals"]["calls"] == 1
    # Invalid since falls back to the period (week → both events).
    assert m.snapshot(period="week", since="bogus")["totals"]["calls"] == 2


def test_monitor_periods_list_with_since(fixed_pricing, monkeypatch):
    monkeypatch.setattr(web, "iter_log_events", lambda c, s, e: iter([]))
    m = web.UsageMonitor(clients=[("us-east-1", None)], period="today", since="2h")
    ids = [p["id"] for p in m.periods()]
    assert ids[0] == "since"
    assert m._default_period == "since"


# ── aggregation cache ────────────────────────────────────────────────────────
def test_snapshot_cache_reuses_until_new_events(fixed_pricing, monkeypatch):
    recs = [_rec("1", inp=100), _rec("2", inp=200)]
    m = _monitor(monkeypatch, recs)
    m.prime()

    calls = {"n": 0}
    real = web._aggregate_facts
    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(web, "_aggregate_facts", counting)

    a = m.snapshot()           # miss → compute
    b = m.snapshot()           # hit
    c = m.snapshot()           # hit
    assert calls["n"] == 1, "identical requests should reuse the cached aggregation"
    assert a["totals"]["calls"] == b["totals"]["calls"] == c["totals"]["calls"] == 2

    # Different filter is a distinct cache key → recompute.
    m.snapshot(None, {"model": "anthropic.claude-x"})
    assert calls["n"] == 2

    # New events bump the version → cache invalidated.
    recs.append(_rec("3", inp=50))
    m._ingest(m._start_ms)
    m.snapshot()
    assert calls["n"] == 3


def test_snapshot_cache_refreshes_volatile_fields(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("1", inp=100)])
    m.prime()
    a = m.snapshot()
    b = m.snapshot()
    # now_ms is re-stamped per call even on a cache hit.
    assert b["now_ms"] >= a["now_ms"]
    # Mutating the returned payload must not corrupt the cached base.
    a["totals"]["calls"] = 999
    assert m.snapshot()["totals"]["calls"] == 1


def test_filter_key_canonical():
    assert web._filter_key(None) == ""
    assert web._filter_key({"model": "x"}) == "model=x"
    # Order-independent.
    assert web._filter_key({"region": "r", "model": "x"}) == \
        web._filter_key({"model": "x", "region": "r"})


# ── multi-region ─────────────────────────────────────────────────────────────
def test_monitor_multi_region_prices_per_region(monkeypatch):
    # Region-specific pricing: us-east-1 input $3/1M, us-west-2 input $6/1M.
    def price(model_id, prefer_global=False, region=None):
        rate = 6.0 if region == "us-west-2" else 3.0
        return ModelPricing(rate, 0.0, 0.0, 0.0, "Claude Test", False)
    monkeypatch.setattr(web, "lookup", price)

    recs = {
        "E": [{"_eventId": "e1", "modelId": "anthropic.claude-x", "region": "us-east-1",
               "input": {"inputTokenCount": 1_000_000}, "output": {"outputTokenCount": 0}}],
        "W": [{"_eventId": "w1", "modelId": "anthropic.claude-x", "region": "us-west-2",
               "input": {"inputTokenCount": 1_000_000}, "output": {"outputTokenCount": 0}}],
    }
    monkeypatch.setattr(web, "iter_log_events", lambda client, s, e: iter(recs[client]))

    m = web.UsageMonitor(clients=[("us-east-1", "E"), ("us-west-2", "W")], period="today", since=None)
    m.prime()
    snap = m.snapshot()

    assert snap["totals"]["calls"] == 2
    # 1M @ $3 (east) + 1M @ $6 (west) = $9 — region-correct pricing
    assert snap["totals"]["cost"] == pytest.approx(9.0)
    regions = {r["region"]: r for r in snap["regions"]}
    assert set(regions) == {"us-east-1", "us-west-2"}
    assert regions["us-east-1"]["cost"] == pytest.approx(3.0)
    assert regions["us-west-2"]["cost"] == pytest.approx(6.0)


def test_monitor_multi_region_filter_by_region(fixed_pricing, monkeypatch):
    recs = {
        "E": [{"_eventId": "e1", "modelId": "m", "region": "us-east-1",
               "input": {"inputTokenCount": 100}, "output": {"outputTokenCount": 0}}],
        "W": [{"_eventId": "w1", "modelId": "m", "region": "us-west-2",
               "input": {"inputTokenCount": 200}, "output": {"outputTokenCount": 0}}],
    }
    monkeypatch.setattr(web, "iter_log_events", lambda client, s, e: iter(recs[client]))
    m = web.UsageMonitor(clients=[("us-east-1", "E"), ("us-west-2", "W")], period="today", since=None)
    m.prime()
    assert m.snapshot()["totals"]["calls"] == 2
    only_west = m.snapshot(None, {"region": "us-west-2"})
    assert only_west["totals"]["calls"] == 1
    assert only_west["totals"]["input_tokens"] == 200


# ── CSV export ───────────────────────────────────────────────────────────────
def test_payload_to_csv_header_and_rows(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [
        _rec("1", model="anthropic.claude-x", inp=1000, out=200),
        _rec("2", model="anthropic.claude-x", inp=500, out=100),
    ])
    m.prime()
    rows = list(csv.reader(io.StringIO(web.payload_to_csv(m.snapshot()))))
    assert rows[0] == [
        "model_id", "display_name", "calls", "input_tokens",
        "cache_write_tokens", "cache_read_tokens", "output_tokens",
        "total_tokens", "cost", "price_known", "cost_share",
    ]
    assert len(rows) == 2  # header + one model
    assert rows[1][0] == "anthropic.claude-x"
    assert rows[1][2] == "2"        # calls
    assert rows[1][3] == "1500"     # input tokens


def test_payload_to_csv_empty():
    rows = list(csv.reader(io.StringIO(web.payload_to_csv({"models": []}))))
    assert len(rows) == 1  # header only


# ── token auth ───────────────────────────────────────────────────────────────
def test_token_matches_disabled_when_no_expected():
    assert web._token_matches(None, None) is True
    assert web._token_matches("anything", None) is True


def test_token_matches_requires_exact_token():
    assert web._token_matches("secret", "secret") is True
    assert web._token_matches("wrong", "secret") is False
    assert web._token_matches(None, "secret") is False
    assert web._token_matches("", "secret") is False


# ── persistence integration ──────────────────────────────────────────────────
def test_monitor_persists_and_reloads(fixed_pricing, monkeypatch, tmp_path):
    from bedrock_insights.storage import FactStore

    recs = [
        _rec("e1", model="anthropic.claude-x", inp=100),
        _rec("e2", model="anthropic.claude-x", inp=200),
    ]
    monkeypatch.setattr(web, "iter_log_events", lambda c, s, e: iter(list(recs)))

    store = FactStore(tmp_path / "facts.db")
    m = web.UsageMonitor(clients=[("us-east-1", None)], period="today", since=None, store=store)
    m.prime()
    assert m.snapshot()["totals"]["calls"] == 2
    assert store.count() == 2          # facts written to disk
    store.close()

    # A fresh monitor on the same DB loads history and de-dups re-ingested events.
    store2 = FactStore(tmp_path / "facts.db")
    m2 = web.UsageMonitor(clients=[("us-east-1", None)], period="today", since=None, store=store2)
    assert m2.snapshot()["totals"]["calls"] == 2   # loaded from disk before any poll
    m2.prime()                                      # re-ingests the same events
    assert m2.snapshot()["totals"]["calls"] == 2   # dedup via seeded seen_ids
    assert store2.count() == 2
    store2.close()


def test_monitor_without_store_unaffected(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("1", inp=100)])
    m.prime()
    assert m.snapshot()["totals"]["calls"] == 1   # store=None path still works


# ── hybrid memory / SQLite ────────────────────────────────────────────────────
def test_monitor_hybrid_old_window_hits_sqlite(fixed_pricing, tmp_path):
    import time

    from bedrock_insights.storage import FactStore

    now = int(time.time() * 1000)

    def f(t, inp):
        return {
            "t": t, "model": "anthropic.claude-x", "is_global": False,
            "ident_key": "u", "ident_label": "u", "region": "us-east-1", "err": "",
            "inp": inp, "out": 0, "cw": 0, "cr": 0, "cost": 0.0, "known": True, "display": "M",
        }

    store = FactStore(tmp_path / "facts.db")
    store.add_many([
        ("r:old", f(now - 20 * 86_400_000, 100)),   # 20 days old → DB only
        ("r:recent", f(now - 60_000, 200)),          # 1 min ago → memory
    ])

    m = web.UsageMonitor(clients=[("us-east-1", None)], period="today", since=None,
                         store=store, memory_window_days=8)

    # Only the recent fact is loaded into memory.
    assert len(m._facts) == 1

    # A recent window is served from memory (recent fact only).
    assert m.snapshot(None, None, "1h")["totals"]["calls"] == 1

    # A 30-day window reaches older than the memory floor → aggregated from SQLite.
    snap = m.snapshot(None, None, "30d")
    assert snap["totals"]["calls"] == 2
    assert snap["totals"]["input_tokens"] == 300
    store.close()


# ── poll-interval env override ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "value,expected",
    [
        ("60", 60),
        ("5", 5),
        ("", 7),        # unset/empty → default
        ("abc", 7),     # non-numeric → default
        ("0", 7),       # non-positive → default
        ("-10", 7),     # negative → default
    ],
)
def test_env_int_poll_seconds(monkeypatch, value, expected):
    monkeypatch.setenv("BEDROCK_INSIGHTS_POLL_SECONDS", value)
    assert web._env_int("BEDROCK_INSIGHTS_POLL_SECONDS", 7) == expected


def test_env_int_missing_uses_default(monkeypatch):
    monkeypatch.delenv("BEDROCK_INSIGHTS_POLL_SECONDS", raising=False)
    assert web._env_int("BEDROCK_INSIGHTS_POLL_SECONDS", 5) == 5


# ── recent invocations ───────────────────────────────────────────────────────
def test_recent_newest_first_and_limit(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    recs = [
        _rec("e1", inp=100, ts=iso(now - timedelta(minutes=5))),
        _rec("e2", inp=200, ts=iso(now - timedelta(minutes=1))),
        _rec("e3", inp=300, ts=iso(now - timedelta(minutes=3))),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    rows = m.recent(limit=2)
    assert len(rows) == 2
    # newest first: e2 (1m ago) then e3 (3m ago)
    assert rows[0]["input_tokens"] == 200
    assert rows[1]["input_tokens"] == 300
    assert {"t", "model", "identity", "region", "input_tokens",
            "output_tokens", "cost", "price_known", "error"}.issubset(rows[0])


def test_recent_filter_by_region(fixed_pricing, monkeypatch):
    recs = {
        "E": [{"_eventId": "e1", "modelId": "m", "region": "us-east-1",
               "input": {"inputTokenCount": 100}, "output": {"outputTokenCount": 0}}],
        "W": [{"_eventId": "e2", "modelId": "m", "region": "us-west-2",
               "input": {"inputTokenCount": 200}, "output": {"outputTokenCount": 0}}],
    }
    monkeypatch.setattr(web, "iter_log_events", lambda client, s, e: iter(recs[client]))
    m = web.UsageMonitor(clients=[("us-east-1", "E"), ("us-west-2", "W")], period="today", since=None)
    m.prime()
    east = m.recent(limit=20, region="us-east-1")
    assert len(east) == 1 and east[0]["region"] == "us-east-1"


def test_recent_row_marks_error_and_unknown_price():
    fact = {"t": 1, "model": "m", "display": "Model M", "ident_label": "alice",
            "region": "us-east-1", "err": "ThrottlingException",
            "inp": 10, "out": 0, "cw": 0, "cr": 5, "cost": 0.0, "known": False}
    row = web._recent_row(fact)
    assert row["error"] == "ThrottlingException"
    assert row["price_known"] is False
    assert row["model"] == "Model M"
    assert row["total_tokens"] == 10


# ── per-model sparkline ──────────────────────────────────────────────────────
def test_model_sparkline_present_and_sums_to_cost(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    recs = [
        _rec("e1", inp=1000, ts=iso(now - timedelta(hours=2))),
        _rec("e2", inp=1000, ts=iso(now - timedelta(hours=1))),
        _rec("e3", inp=1000, ts=iso(now - timedelta(minutes=5))),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    model = m.snapshot(since="3h")["models"][0]
    assert len(model["spark"]) == 20
    assert abs(sum(model["spark"]) - model["cost"]) < 1e-6


# ── cache savings estimate ───────────────────────────────────────────────────
def test_cache_savings_in_totals(fixed_pricing, monkeypatch):
    # 1M cache-read tokens priced at $0.30/1M vs $3.00/1M input → saves $2.70.
    recs = [_rec("e1", inp=100, cr=1_000_000)]
    m = _monitor(monkeypatch, recs)
    m.prime()
    totals = m.snapshot(since="3h")["totals"]
    assert abs(totals["cache_savings"] - 2.70) < 1e-6


def test_cache_savings_zero_without_cache(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("e1", inp=100, out=50)])
    m.prime()
    assert m.snapshot(since="3h")["totals"]["cache_savings"] == 0.0


# ── absolute drill-in window ─────────────────────────────────────────────────
def test_absolute_window_snapshot(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    recs = [
        _rec("e1", inp=100, ts=iso(now - timedelta(minutes=10))),
        _rec("e2", inp=100, ts=iso(now - timedelta(minutes=2))),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    end = int(now.timestamp() * 1000)
    start = int((now - timedelta(minutes=5)).timestamp() * 1000)
    snap = m.snapshot(start=start, end=end)
    assert snap["totals"]["calls"] == 1  # only e2 falls in the last 5 minutes
    assert snap["window"]["period"] == "custom"


# ── on-demand event detail (prompt / response bodies) ────────────────────────
def test_fetch_event_returns_bodies(fixed_pricing, monkeypatch):
    rec = {
        "_eventId": "e1", "modelId": "anthropic.claude-x", "region": "us-east-1",
        "input": {"inputTokenCount": 10,
                  "inputBodyJson": {"messages": [{"role": "user", "content": "hi there"}]}},
        "output": {"outputTokenCount": 5, "outputBodyJson": {"content": "hello back"}},
    }
    m = _monitor(monkeypatch, [rec])
    m.prime()
    d = m.fetch_event("us-east-1", "e1", web._now_ms())
    assert "hi there" in d["input"] and "hello back" in d["output"]
    assert d["error"] == "" and d["model_id"] == "anthropic.claude-x"


def test_fetch_event_not_found(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [])
    m.prime()
    d = m.fetch_event("us-east-1", "missing", web._now_ms())
    assert "error" in d and d.get("input") is None


def test_fetch_event_missing_id(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [])
    assert "error" in m.fetch_event("us-east-1", "", 0)


def test_recent_row_includes_event_id():
    fact = {"t": 1, "model": "m", "display": "M", "ident_label": "u", "region": "us-east-1",
            "err": "", "inp": 1, "out": 1, "cw": 0, "cr": 0, "cost": 0.0, "known": True,
            "key": "us-east-1:evt-123"}
    assert web._recent_row(fact)["event_id"] == "evt-123"


# ── operation dimension ──────────────────────────────────────────────────────
def _rec_op(eid, op, inp=100):
    r = _rec(eid, inp=inp)
    r["operation"] = op
    return r


def test_operation_breakdown(fixed_pricing, monkeypatch):
    recs = [_rec_op("1", "Converse"), _rec_op("2", "InvokeModel"), _rec_op("3", "Converse")]
    m = _monitor(monkeypatch, recs)
    m.prime()
    ops = {o["operation"]: o for o in m.snapshot(since="3h")["operations"]}
    assert ops["Converse"]["calls"] == 2
    assert ops["InvokeModel"]["calls"] == 1


def test_filter_by_operation(fixed_pricing, monkeypatch):
    recs = [_rec_op("1", "Converse"), _rec_op("2", "InvokeModel"), _rec_op("3", "Converse")]
    m = _monitor(monkeypatch, recs)
    m.prime()
    assert m.snapshot(None, {"operation": "Converse"}, "3h")["totals"]["calls"] == 2


# ── period-over-period comparison ────────────────────────────────────────────
def test_period_comparison_delta(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    recs = [
        _rec("cur", inp=1000, ts=iso(now - timedelta(minutes=30))),   # current 3h window
        _rec("prev", inp=1000, ts=iso(now - timedelta(hours=4))),     # previous 3h window
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    cmp = m.snapshot(since="3h")["comparison"]
    assert cmp["prev_calls"] == 1
    assert cmp["delta_pct"] == 0.0  # equal cost → 0% change


def test_period_comparison_no_prior_data(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [_rec("only", inp=100)])
    m.prime()
    cmp = m.snapshot(since="1h")["comparison"]
    assert cmp["prev_cost"] == 0.0 and cmp["delta_pct"] is None


# ── anomaly detection ────────────────────────────────────────────────────────
def test_detect_anomaly_flags_spike():
    pts = [{"t": i, "cost": 1.0} for i in range(8)] + [{"t": 8, "cost": 50.0}]
    a = web._detect_anomaly(pts)
    assert a is not None and a["bucket_t"] == 8 and a["cost"] == 50.0


def test_detect_anomaly_none_when_flat():
    pts = [{"t": i, "cost": 1.0} for i in range(9)]
    assert web._detect_anomaly(pts) is None


def test_detect_anomaly_needs_history():
    assert web._detect_anomaly([{"t": 0, "cost": 9.0}]) is None


# ── window_cost (bypasses the aggregation cache; used for budget checks) ─────
def test_window_cost_sums_without_caching(fixed_pricing, monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()  # noqa: E731
    recs = [
        _rec("a", inp=1000, ts=iso(now - timedelta(minutes=10))),
        _rec("b", inp=1000, ts=iso(now - timedelta(minutes=1))),
    ]
    m = _monitor(monkeypatch, recs)
    m.prime()
    start = int((now - timedelta(hours=1)).timestamp() * 1000)
    end = int(now.timestamp() * 1000)
    cost = m.window_cost(start, end)
    assert cost > 0
    # Calling repeatedly with a slightly different `end` (simulating a moving
    # "now") must not error and must not require the value to be cached.
    cost2 = m.window_cost(start, end + 1000)
    assert cost2 >= cost


def test_window_cost_zero_when_no_facts(fixed_pricing, monkeypatch):
    m = _monitor(monkeypatch, [])
    m.prime()
    assert m.window_cost(0, web._now_ms()) == 0.0
