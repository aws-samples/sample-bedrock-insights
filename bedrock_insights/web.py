"""
Web dashboard for bedrock-insights.

A dependency-free HTTP server (Python stdlib only) that exposes the same
CloudWatch + pricing data used by the terminal UI as a JSON API, and serves a
self-contained HTML dashboard that polls that API on an interval.

Routes
------
  GET /                  → the dashboard HTML page
  GET /api/usage         → latest cached usage snapshot as JSON
  GET /api/config        → static config (refresh interval, labels, threshold)

Scalability
-----------
A single background thread (``UsageMonitor``) polls CloudWatch incrementally —
mirroring terminal ``--live`` mode: it pins the window start at launch, then
re-queries only a short trailing overlap window every few seconds and
deduplicates events by ID. The aggregated result is cached in memory behind a
lock and re-rendered into a JSON payload once per poll.

HTTP requests only read that cached payload, so the load on CloudWatch is
constant regardless of how many browser tabs are open or how often they
refresh. ``FilterLogEvents`` is never called from the request path.
"""

from __future__ import annotations

import copy
import csv
import errno
import heapq
import hmac
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources  # nosemgrep: python37-compatibility-importlib2 — project requires Python >=3.9
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from botocore.exceptions import ClientError
from rich.console import Console

from .cloudwatch import get_time_range, iter_log_events, normalize_model_id, parse_since
from .display import period_label, since_label
from .notify import ThresholdAlerter
from .pricing import init_pricing_regions, lookup
from .storage import FactStore

console = Console()


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to default."""
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _load_static(name: str) -> str:
    """Read a packaged static asset (HTML/CSS/JS) from bedrock_insights/static/."""
    return (resources.files("bedrock_insights") / "static" / name).read_text(encoding="utf-8")


# Background poll cadence + UI refresh default (seconds). Override via the
# BEDROCK_INSIGHTS_POLL_SECONDS env var (e.g. the CloudFormation deploy sets it).
REFRESH_SECONDS  = _env_int("BEDROCK_INSIGHTS_POLL_SECONDS", 5)
_LIVE_OVERLAP_MS = 90_000

# Max accepted request body (bytes) for POST endpoints — guards against a client
# declaring a huge Content-Length and forcing a large allocation.
_MAX_BODY_BYTES = 64 * 1024

# Security headers applied to every response. The dashboard is a self-contained
# page with inline CSS/JS, so the CSP permits 'unsafe-inline' for style/script
# but otherwise locks the page down to same-origin.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy":        "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
    ),
}

# Persistence: per-event facts survive restarts and outlive CloudWatch retention.
_DB_RETENTION_DAYS = 90
_DB_PATH = Path.home() / ".config" / "bedrock-insights" / "facts.db"

# Hybrid storage: keep recent facts in memory (fast path); aggregate older
# windows on demand from SQLite. The memory window comfortably covers all
# built-in periods (today/yesterday/week); longer custom windows hit the DB.
_MEMORY_WINDOW_DAYS = 8


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _month_start_ms() -> int:
    now = datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


def _record_ms(r: dict) -> int:
    """Best-effort event timestamp (ms) for a log record, for time bucketing."""
    ts = r.get("timestamp")
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    it = r.get("_ingestionTime")
    if isinstance(it, (int, float)) and it:
        return int(it)
    return _now_ms()


def _record_cost_tokens(r: dict, region: str | None = None) -> tuple[float, int, int, int, int, bool, str, float]:
    """Return (cost, input, output, cache_write, cache_read, price_known, display_name, saved).

    `saved` estimates how much the cache-read tokens saved versus paying the
    full input rate for them. Priced with the given region's table when
    available (multi-region).
    """
    raw_id    = r.get("modelId", "unknown")
    inp_data  = r.get("input") or {}
    inp = inp_data.get("inputTokenCount")           or 0
    cw  = inp_data.get("cacheWriteInputTokenCount") or 0
    cr  = inp_data.get("cacheReadInputTokenCount")  or 0
    out = (r.get("output") or {}).get("outputTokenCount") or 0
    p = lookup(raw_id, prefer_global=raw_id.lower().startswith("global."), region=region)
    cost = (
        inp * p.input_per_1m + out * p.output_per_1m
        + cw * p.cache_write_per_1m + cr * p.cache_read_per_1m
    ) / 1_000_000
    saved = cr * max(0.0, p.input_per_1m - p.cache_read_per_1m) / 1_000_000
    return cost, inp, out, cw, cr, (not p.needs_pricing), p.display_name, saved


# Which requestMetadata key carries the caller's team/tenant. Bedrock records
# requestMetadata verbatim in the invocation log; a proxy (e.g. LiteLLM) can
# stamp {"team": "<alias>"} per call. Override with BEDROCK_INSIGHTS_TEAM_KEY.
_TEAM_META_KEY = os.environ.get("BEDROCK_INSIGHTS_TEAM_KEY", "team")

# Opt-in: recover the team from an assumed-role session name when requestMetadata
# is absent. Some proxies name the STS session "<prefix><team>" (e.g. LiteLLM
# assumes a role with RoleSessionName="litellm-<team>"), and calls that bypass
# requestMetadata (e.g. Bedrock InvokeModel, which doesn't carry it) still expose
# that session name in identity.arn. Set BEDROCK_INSIGHTS_TEAM_SESSION_PREFIX to
# the prefix (e.g. "litellm-") to strip it and group by team. Empty = disabled.
_TEAM_SESSION_PREFIX = os.environ.get("BEDROCK_INSIGHTS_TEAM_SESSION_PREFIX", "")


def _identity_key(arn: str, record: dict | None = None) -> tuple[str, str]:
    """Return (group_key, display_label) for a Bedrock invocation.

    Prefer the per-request team tag from requestMetadata when present — this is
    the real caller when many teams share one IAM role (e.g. a proxy's IRSA
    role), which the ARN alone can't distinguish. Falls back to the IAM
    principal ARN otherwise.

      requestMetadata.team = "marketing"             → ("team/marketing", "marketing")
      arn:aws:sts::123:assumed-role/MyRole/session   → ("assumed-role/MyRole", "MyRole")
      arn:aws:iam::123:user/alice                    → ("user/alice", "alice")

    When BEDROCK_INSIGHTS_TEAM_SESSION_PREFIX is set and requestMetadata carries
    no team, an assumed-role session named "<prefix><team>" resolves to that team:

      prefix="litellm-", .../assumed-role/Role/litellm-sales → ("team/sales", "sales")
    """
    if record is not None:
        team = (record.get("requestMetadata") or {}).get(_TEAM_META_KEY)
        if team:
            return "team/" + team, team
    if not arn:
        return "unknown", "unknown"
    resource = arn.split(":")[-1]
    parts = resource.split("/")
    if parts[0] == "assumed-role" and len(parts) >= 2:
        if _TEAM_SESSION_PREFIX and len(parts) >= 3 \
                and parts[2].startswith(_TEAM_SESSION_PREFIX) \
                and len(parts[2]) > len(_TEAM_SESSION_PREFIX):
            team = parts[2][len(_TEAM_SESSION_PREFIX):]
            return "team/" + team, team
        return "assumed-role/" + parts[1], parts[1]
    if len(parts) >= 2:
        return resource, parts[-1]
    return resource, resource


def _recent_row(f: dict) -> dict:
    """Format a slim fact into a recent-invocations display row."""
    key = f.get("key") or ""
    event_id = key.split(":", 1)[1] if ":" in key else ""
    return {
        "t":                 f["t"],
        "model":             f.get("display") or f["model"],
        "model_id":          f["model"],
        "identity":          f.get("ident_label") or "unknown",
        "region":            f.get("region") or "",
        "input_tokens":      f["inp"],
        "output_tokens":     f["out"],
        "cache_read_tokens":  f["cr"],
        "cache_write_tokens": f["cw"],
        "total_tokens":      f["inp"] + f["out"],
        "cost":              round(f["cost"], 6),
        "price_known":       f["known"],
        "error":             f.get("err") or "",
        "event_id":          event_id,
    }


def _event_detail(r: dict, limit: int = 6000) -> dict:
    """Extract the (possibly truncated) request/response bodies from a log record.

    Bodies live inline in the CloudWatch record when text delivery is enabled
    and the payload is small; large payloads are offloaded to S3 (we surface the
    path but never read it). Content is returned for on-demand display only and
    is never stored by bedrock-insights.
    """
    inp = r.get("input") or {}
    out = r.get("output") or {}

    def grab(body):
        if body is None:
            return None, False
        s = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2)
        return (s[:limit], True) if len(s) > limit else (s, False)

    in_text, in_trunc = grab(inp.get("inputBodyJson"))
    out_text, out_trunc = grab(out.get("outputBodyJson"))
    return {
        "model_id":         r.get("modelId", ""),
        "region":           r.get("region", ""),
        "error":            r.get("errorCode") or "",
        "input":            in_text,
        "input_truncated":  in_trunc,
        "input_s3":         inp.get("inputBodyS3Path"),
        "output":           out_text,
        "output_truncated": out_trunc,
        "output_s3":        out.get("outputBodyS3Path"),
    }


def build_payload(usage: dict[str, dict]) -> dict:
    """Convert an aggregated usage dict into a JSON-serialisable payload.

    Mirrors the cost math in display.build_table but emits structured data
    instead of a Rich table, so the frontend can render it however it likes.
    """
    models: list[dict] = []
    total_calls = total_input = total_output = 0
    total_cache_write = total_cache_read = 0
    total_cost = 0.0
    all_prices_known = True

    for model_id, stats in sorted(usage.items()):
        inp = stats["input_tokens"]
        out = stats["output_tokens"]
        cw  = stats.get("cache_write_tokens", 0)
        cr  = stats.get("cache_read_tokens", 0)
        calls = stats["calls"]

        if "cost" in stats:                      # region-priced upstream (multi-region)
            cost = stats["cost"]
            known = stats.get("price_known", True)
            name = stats.get("display_name") or model_id
        else:                                    # single-table fallback
            p = lookup(model_id, prefer_global=stats.get("is_global", False))
            known = not p.needs_pricing
            cost = (
                inp * p.input_per_1m + out * p.output_per_1m
                + cw * p.cache_write_per_1m + cr * p.cache_read_per_1m
            ) / 1_000_000
            name = p.display_name

        if not known:
            all_prices_known = False

        total_calls       += calls
        total_input       += inp
        total_output      += out
        total_cache_write += cw
        total_cache_read  += cr
        total_cost        += cost

        models.append({
            "model_id":          model_id,
            "display_name":      name,
            "calls":             calls,
            "input_tokens":      inp,
            "output_tokens":     out,
            "cache_write_tokens": cw,
            "cache_read_tokens":  cr,
            "total_tokens":      inp + out,
            "cost":              round(cost, 6),
            "price_known":       known,
            "is_global":         stats.get("is_global", False),
            "spark":             [round(x, 6) for x in stats.get("spark", [])],
        })

    # Sort by cost desc (unknown-price models, cost 0, fall to the bottom).
    models.sort(key=lambda m: m["cost"], reverse=True)

    # Per-model cost share (fraction of total spend).
    for m in models:
        m["cost_share"] = round(m["cost"] / total_cost, 4) if total_cost > 0 else 0.0

    total_tokens = total_input + total_output
    cache_read_ratio = (
        total_cache_read / (total_input + total_cache_read)
        if (total_input + total_cache_read) > 0 else 0.0
    )

    return {
        "models": models,
        "totals": {
            "calls":              total_calls,
            "input_tokens":       total_input,
            "output_tokens":      total_output,
            "cache_write_tokens": total_cache_write,
            "cache_read_tokens":  total_cache_read,
            "total_tokens":       total_tokens,
            "cost":               round(total_cost, 6),
            "cost_known":         all_prices_known,
            "avg_cost_per_call":  round(total_cost / total_calls, 6) if total_calls else 0.0,
            "avg_tokens_per_call": round(total_tokens / total_calls, 1) if total_calls else 0.0,
            "cache_hit_rate":     round(cache_read_ratio, 4),
        },
        "has_cache": (total_cache_write + total_cache_read) > 0,
    }


def compute_projection(cost: float, start_ms: int, now_ms: int) -> dict:
    """Extrapolate a run-rate projection from spend so far.

    These are simple linear run-rate estimates (current burn rate × time), not
    forecasts — clearly labelled as estimates in the UI.
    """
    elapsed_hours = max((now_ms - start_ms) / 3.6e6, 1 / 60)
    burn_per_hour = cost / elapsed_hours
    return {
        "elapsed_hours":     round(elapsed_hours, 4),
        "burn_per_hour":     round(burn_per_hour, 6),
        "projected_daily":   round(burn_per_hour * 24, 4),
        "projected_monthly": round(burn_per_hour * 24 * 30, 2),
    }


def payload_to_csv(payload: dict) -> str:
    """Serialise a usage payload's per-model rows as CSV text."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "model_id", "display_name", "calls", "input_tokens",
        "cache_write_tokens", "cache_read_tokens", "output_tokens",
        "total_tokens", "cost", "price_known", "cost_share",
    ])
    for m in payload.get("models", []):
        writer.writerow([
            m["model_id"], m["display_name"], m["calls"], m["input_tokens"],
            m["cache_write_tokens"], m["cache_read_tokens"], m["output_tokens"],
            m["total_tokens"], f'{m["cost"]:.6f}', m["price_known"],
            f'{m.get("cost_share", 0):.4f}',
        ])
    return out.getvalue()


def _prom_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_prometheus(payload: dict) -> str:
    """Render a usage payload as Prometheus text exposition format (v0.0.4).

    Metrics are gauges reflecting the current window (they reset when the
    process restarts). Suitable for scraping into Prometheus/Grafana.
    """
    lines: list[str] = []

    def metric(name: str, mtype: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            if labels:
                ls = ",".join(f'{k}="{_prom_escape(str(v))}"' for k, v in labels.items())
                lines.append(f"{name}{{{ls}}} {value}")
            else:
                lines.append(f"{name} {value}")

    t       = payload["totals"]
    models  = payload.get("models", [])
    idents  = payload.get("identities", [])
    regions = payload.get("regions", [])
    errors  = payload.get("errors", {"rate": 0.0, "by_code": []})

    metric("bedrock_cost_usd", "gauge", "Estimated total cost (USD) for the window",
           [({}, t["cost"])])
    metric("bedrock_calls", "gauge", "Total model invocations for the window",
           [({}, t["calls"])])
    metric("bedrock_tokens", "gauge", "Total tokens (input + output) for the window",
           [({}, t["total_tokens"])])
    metric("bedrock_cache_hit_rate", "gauge", "Cache read tokens / (input + cache read)",
           [({}, t["cache_hit_rate"])])
    metric("bedrock_error_rate", "gauge", "Failed calls / total calls",
           [({}, errors["rate"])])

    metric("bedrock_model_cost_usd", "gauge", "Estimated cost (USD) per model",
           [({"model": m["model_id"]}, m["cost"]) for m in models])
    metric("bedrock_model_calls", "gauge", "Invocations per model",
           [({"model": m["model_id"]}, m["calls"]) for m in models])
    metric(
        "bedrock_model_tokens", "gauge", "Tokens per model by type",
        [({"model": m["model_id"], "type": "input"},  m["input_tokens"])  for m in models]
        + [({"model": m["model_id"], "type": "output"}, m["output_tokens"]) for m in models]
        + [({"model": m["model_id"], "type": "cache_read"},  m["cache_read_tokens"])  for m in models]
        + [({"model": m["model_id"], "type": "cache_write"}, m["cache_write_tokens"]) for m in models],
    )

    metric("bedrock_identity_cost_usd", "gauge", "Estimated cost (USD) per IAM identity",
           [({"identity": i["label"]}, i["cost"]) for i in idents])
    metric("bedrock_identity_calls", "gauge", "Invocations per IAM identity",
           [({"identity": i["label"]}, i["calls"]) for i in idents])

    metric("bedrock_region_cost_usd", "gauge", "Estimated cost (USD) per region",
           [({"region": r["region"]}, r["cost"]) for r in regions])
    metric("bedrock_region_calls", "gauge", "Invocations per region",
           [({"region": r["region"]}, r["calls"]) for r in regions])

    metric("bedrock_errors", "gauge", "Failed invocations by error code",
           [({"code": c["code"]}, c["count"]) for c in errors.get("by_code", [])])

    return "\n".join(lines) + "\n"


def _resolve_window(period: str, since: str | None) -> tuple[int, int, str]:
    """Return (start_ms, end_ms, label) for a period or a --since duration."""
    if since:
        start_ms, end_ms = parse_since(since)
        return start_ms, end_ms, since_label(since)
    start_ms, end_ms = get_time_range(period)
    return start_ms, end_ms, period_label(period)


def _abs_window_label(start_ms: int, end_ms: int) -> str:
    """Short label for an absolute drill-in window, e.g. '06-30 12:05–12:10'."""
    s = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    e = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    return f"{s:%m-%d %H:%M}–{e:%H:%M} UTC"


def _bucket_seconds_for(span_ms: int) -> int:
    """Pick trend granularity from a window span: ≤3h→1min, ≤36h→5min, else 1h."""
    if span_ms <= 3 * 3_600_000:
        return 60
    if span_ms <= 36 * 3_600_000:
        return 300
    return 3_600


_SPARK_N = 20  # fixed-width per-model sparkline buckets (bounded payload size)


def _aggregate_facts(
    facts: list[dict], start_ms: int, end_ms: int, bucket_seconds: int, flt: dict | None,
) -> dict:
    """Aggregate slim per-event facts into a full dashboard payload.

    Applies a time window and an optional dimension filter (model / identity /
    region). Runs on demand per request so the dashboard can switch periods and
    drill into a single model/identity/region without re-querying CloudWatch.
    """
    bucket_ms = bucket_seconds * 1000
    f_model     = flt.get("model")     if flt else None
    f_identity  = flt.get("identity")  if flt else None
    f_region    = flt.get("region")    if flt else None
    f_operation = flt.get("operation") if flt else None

    usage:   dict[str, dict] = {}
    buckets: dict[int, dict] = {}
    by_identity: dict[str, dict] = {}
    by_region:   dict[str, dict] = {}
    by_operation: dict[str, dict] = {}
    errors_by_code: dict[str, int] = {}
    error_total = 0
    saved_total = 0.0

    for f in facts:
        if f["t"] < start_ms or f["t"] > end_ms:
            continue
        if f_model is not None and f["model"] != f_model:
            continue
        if f_identity is not None and f["ident_key"] != f_identity:
            continue
        if f_region is not None and f["region"] != f_region:
            continue
        if f_operation is not None and (f.get("op") or "") != f_operation:
            continue

        u = usage.setdefault(f["model"], {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "is_global": False,
            "cost": 0.0, "price_known": True, "display_name": f["model"],
            "spark": [0.0] * _SPARK_N,
        })
        u["calls"]              += 1
        u["input_tokens"]       += f["inp"]
        u["output_tokens"]      += f["out"]
        u["cache_write_tokens"] += f["cw"]
        u["cache_read_tokens"]  += f["cr"]
        u["is_global"]           = u["is_global"] or f["is_global"]
        u["cost"]               += f["cost"]
        u["price_known"]         = u["price_known"] and f["known"]
        u["display_name"]        = f["display"]

        span = end_ms - start_ms
        si = 0 if span <= 0 else min(_SPARK_N - 1, int((f["t"] - start_ms) / span * _SPARK_N))
        u["spark"][si] += f["cost"]
        saved_total += f.get("saved", 0.0)

        bt = (f["t"] // bucket_ms) * bucket_ms
        b = buckets.setdefault(bt, {"cost": 0.0, "input": 0, "output": 0, "calls": 0})
        b["cost"]   += f["cost"]
        b["input"]  += f["inp"]
        b["output"] += f["out"]
        b["calls"]  += 1

        idn = by_identity.setdefault(f["ident_key"], {
            "label": f["ident_label"], "calls": 0, "input": 0, "output": 0,
            "cost": 0.0, "errors": 0,
        })
        idn["calls"]  += 1
        idn["input"]  += f["inp"]
        idn["output"] += f["out"]
        idn["cost"]   += f["cost"]
        idn["errors"] += 1 if f["err"] else 0

        reg = by_region.setdefault(f["region"], {
            "calls": 0, "input": 0, "output": 0, "cost": 0.0, "errors": 0,
        })
        reg["calls"]  += 1
        reg["input"]  += f["inp"]
        reg["output"] += f["out"]
        reg["cost"]   += f["cost"]
        reg["errors"] += 1 if f["err"] else 0

        op = by_operation.setdefault(f.get("op") or "unknown", {
            "calls": 0, "input": 0, "output": 0, "cost": 0.0, "errors": 0,
        })
        op["calls"]  += 1
        op["input"]  += f["inp"]
        op["output"] += f["out"]
        op["cost"]   += f["cost"]
        op["errors"] += 1 if f["err"] else 0

        if f["err"]:
            error_total += 1
            errors_by_code[f["err"]] = errors_by_code.get(f["err"], 0) + 1

    payload = build_payload(usage)
    payload["totals"]["cache_savings"] = round(saved_total, 6)
    total_cost  = payload["totals"]["cost"]
    total_calls = payload["totals"]["calls"]

    def share(c):
        return round(c / total_cost, 4) if total_cost > 0 else 0.0

    payload["trend"] = {
        "bucket_seconds": bucket_seconds,
        "points": [
            {
                "t": b, "cost": round(d["cost"], 6),
                "input_tokens": d["input"], "output_tokens": d["output"],
                "total_tokens": d["input"] + d["output"], "calls": d["calls"],
            }
            for b, d in sorted(buckets.items())
        ],
    }
    payload["identities"] = sorted(
        (
            {
                "key": key, "label": d["label"], "calls": d["calls"],
                "input_tokens": d["input"], "output_tokens": d["output"],
                "total_tokens": d["input"] + d["output"],
                "cost": round(d["cost"], 6), "errors": d["errors"],
                "cost_share": share(d["cost"]),
            }
            for key, d in by_identity.items()
        ),
        key=lambda x: x["cost"], reverse=True,
    )[:12]
    payload["regions"] = sorted(
        (
            {
                "region": name, "calls": d["calls"],
                "total_tokens": d["input"] + d["output"],
                "cost": round(d["cost"], 6), "errors": d["errors"],
                "cost_share": share(d["cost"]),
            }
            for name, d in by_region.items()
        ),
        key=lambda x: x["cost"], reverse=True,
    )
    payload["operations"] = sorted(
        (
            {
                "operation": name, "calls": d["calls"],
                "total_tokens": d["input"] + d["output"],
                "cost": round(d["cost"], 6), "errors": d["errors"],
                "cost_share": share(d["cost"]),
            }
            for name, d in by_operation.items()
        ),
        key=lambda x: x["cost"], reverse=True,
    )
    payload["errors"] = {
        "total": error_total,
        "rate":  round(error_total / total_calls, 4) if total_calls else 0.0,
        "by_code": sorted(
            ({"code": c, "count": n} for c, n in errors_by_code.items()),
            key=lambda x: x["count"], reverse=True,
        ),
    }
    payload["projection"] = compute_projection(total_cost, start_ms, end_ms)
    payload["anomaly"] = _detect_anomaly(payload["trend"]["points"])
    return payload


def _detect_anomaly(points: list[dict]) -> dict | None:
    """Flag the latest trend bucket if its cost is a clear spike vs. the prior ones.

    Heuristic: cost > mean + 3·stddev of the earlier buckets, and > 1.5× the mean.
    Returns {bucket_t, cost, baseline} or None. Needs a few buckets of history.
    """
    costs = [p["cost"] for p in points]
    if len(costs) < 6:
        return None
    base, last = costs[:-1], costs[-1]
    n = len(base)
    mean = sum(base) / n
    std = (sum((c - mean) ** 2 for c in base) / n) ** 0.5
    if last > 0 and last > mean + 3 * std and last > mean * 1.5:
        return {"bucket_t": points[-1]["t"], "cost": round(last, 6), "baseline": round(mean, 6)}
    return None


def _sum_window(facts: list[dict], start_ms: int, end_ms: int, flt: dict | None) -> tuple[float, int]:
    """Sum (cost, calls) over a window with the same dimension filter as the main view."""
    f_model     = flt.get("model")     if flt else None
    f_identity  = flt.get("identity")  if flt else None
    f_region    = flt.get("region")    if flt else None
    f_operation = flt.get("operation") if flt else None
    cost, calls = 0.0, 0
    for f in facts:
        if f["t"] < start_ms or f["t"] > end_ms:
            continue
        if f_model is not None and f["model"] != f_model:
            continue
        if f_identity is not None and f["ident_key"] != f_identity:
            continue
        if f_region is not None and f["region"] != f_region:
            continue
        if f_operation is not None and (f.get("op") or "") != f_operation:
            continue
        cost += f["cost"]
        calls += 1
    return cost, calls


def _previous_window(period: str, start_ms: int, end_ms: int) -> tuple[int, int, str]:
    """Return (prev_start, prev_end, label) for the comparable prior window."""
    day = 86_400_000
    if period in ("today", "yesterday"):
        delta, label = day, "vs yesterday" if period == "today" else "vs prior day"
    elif period == "week":
        delta, label = 7 * day, "vs prior week"
    else:  # custom since / absolute drill-in → shift by the window's own span
        delta, label = (end_ms - start_ms), "vs prior period"
    return start_ms - delta, end_ms - delta, label


def _filter_key(flt: dict | None) -> str:
    """Canonical, hashable string for a filter dict (cache key component)."""
    if not flt:
        return ""
    return ";".join(f"{k}={flt[k]}" for k in sorted(flt))


class UsageMonitor:
    """Background poller retaining slim per-event facts for on-demand aggregation.

    Like terminal --live mode, it polls CloudWatch incrementally (trailing
    overlap window + event-ID dedup) and never re-queries from the request path.
    Instead of caching one pre-aggregated payload, it keeps a compact fact per
    event so HTTP requests can aggregate any sub-window or filter on demand —
    powering UI period switching and drill-down filters without extra AWS calls.

    To serve period switches, it polls from the *earliest* start it may be asked
    about (the wider of the launch window and the past 7 days).
    """

    _SWITCHABLE = ("today", "yesterday", "week")

    def __init__(self, clients, period: str, since: str | None,
                 poll_callback=None, store=None, memory_window_days: int = _MEMORY_WINDOW_DAYS) -> None:
        # clients: list of (region, logs_client) tuples (one per region).
        self._clients = clients
        self._since = since
        self._default_period = "since" if since else period
        self._poll_callback = poll_callback
        self._store = store
        self._memory_window_ms = memory_window_days * 86_400_000

        starts = [get_time_range("week")[0]]
        starts.append(parse_since(since)[0] if since else get_time_range(period)[0])
        self._start_ms = min(starts)
        self._poll_from_ms = self._start_ms

        self._facts: list[dict] = []
        self._seen_ids: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_updated: datetime | None = None
        self._last_error: tuple[str, str] | None = None

        # Memory holds facts at or after this timestamp; older windows are served
        # from SQLite. Trimmed forward on each poll to keep memory bounded.
        self._memory_floor_ms = _now_ms() - self._memory_window_ms

        # Load recent persisted history into memory so trends are available
        # immediately and survive restarts. Seeding seen_ids prevents
        # CloudWatch's overlap window from re-counting events we already have.
        if store is not None:
            facts, keys = store.load(self._memory_floor_ms)
            self._facts.extend(facts)
            self._seen_ids.update(keys)

        # On-demand aggregation cache. Keyed by (period, filter); the whole cache
        # is invalidated whenever the fact count changes (i.e. a new poll added
        # events), so entries live at most one poll interval. This makes repeated
        # identical requests (many tabs, the Prometheus scraper, per-tab polling)
        # O(1) instead of O(events) when the data hasn't changed.
        self._cache: dict[tuple, dict] = {}
        self._cache_version = -1
        self._cache_lock = threading.Lock()
        # Monotonic counter bumped whenever the fact list changes; used as the
        # cache version so trimming + appends can't collide on an equal length.
        self._facts_version = 0

    # ── window resolution ────────────────────────────────────────────────────
    def _window_for(self, period: str | None, since: str | None = None) -> tuple[str, int, int, str, int]:
        """Return (period, start_ms, end_ms, label, bucket_seconds) for a request.

        A valid `since` duration (e.g. "2h") overrides the named period, giving a
        custom rolling window — bounded by the facts actually retained in memory.
        """
        if since:
            try:
                s, e = parse_since(since)
                return "custom", s, e, since_label(since), _bucket_seconds_for(e - s)
            except ValueError:
                pass  # invalid duration → fall back to the period
        period = period or self._default_period
        if period == "since" and self._since:
            s, e = parse_since(self._since)
            return "since", s, e, since_label(self._since), _bucket_seconds_for(e - s)
        if period in self._SWITCHABLE:
            s, e = get_time_range(period)
            bucket = 3_600 if period == "week" else 300
            return period, s, e, period_label(period), bucket
        # Unknown period → fall back to the launch default.
        if self._since:
            s, e = parse_since(self._since)
            return "since", s, e, since_label(self._since), _bucket_seconds_for(e - s)
        s, e = get_time_range(self._default_period)
        bucket = 3_600 if self._default_period == "week" else 300
        return self._default_period, s, e, period_label(self._default_period), bucket

    def periods(self) -> list[dict]:
        opts = [
            {"id": "today",     "label": "Today"},
            {"id": "yesterday", "label": "Yesterday"},
            {"id": "week",      "label": "Past 7 Days"},
        ]
        if self._since:
            opts.insert(0, {"id": "since", "label": since_label(self._since)})
        return opts

    # ── ingest ────────────────────────────────────────────────────────────────
    def _ingest(self, from_ms: int) -> None:
        """Pull new events from every region (concurrently) since from_ms, dedup, append facts."""
        to_ms = _now_ms()

        def fetch(region_client):
            region, client = region_client
            return region, list(iter_log_events(client, from_ms, to_ms))

        results: list[tuple[str, list]] = []
        error: tuple[str, str] | None = None

        if len(self._clients) == 1:
            region, client = self._clients[0]
            try:
                results.append((region, list(iter_log_events(client, from_ms, to_ms))))
            except ClientError as exc:
                error = (exc.response["Error"]["Code"], exc.response["Error"]["Message"])
        else:
            # Fetch all regions in parallel; a failure in one region becomes a
            # warning but doesn't drop the others.
            with ThreadPoolExecutor(max_workers=min(len(self._clients), 8)) as pool:
                futures = [pool.submit(fetch, rc) for rc in self._clients]
                for fut in futures:
                    try:
                        results.append(fut.result())
                    except ClientError as exc:
                        error = (exc.response["Error"]["Code"], exc.response["Error"]["Message"])

        # Dedup + fact-building stays single-threaded (shared seen_ids set).
        new_facts = []
        new_rows = []   # (key, fact) for persistence
        for client_region, records in results:
            for r in records:
                eid = r.get("_eventId", "")
                key = f"{client_region}:{eid}" if eid else ""
                if key and key in self._seen_ids:
                    continue
                if key:
                    self._seen_ids.add(key)
                raw_id = r.get("modelId", "unknown")
                rec_region = r.get("region") or client_region
                cost, inp, out, cw, cr, known, display, saved = _record_cost_tokens(r, rec_region)
                ident_key, ident_label = _identity_key((r.get("identity") or {}).get("arn", ""), r)
                fact = {
                    "key":          key,
                    "t":            _record_ms(r),
                    "model":        normalize_model_id(raw_id),
                    "is_global":    raw_id.lower().startswith("global."),
                    "ident_key":    ident_key,
                    "ident_label":  ident_label,
                    "region":       rec_region,
                    "err":          r.get("errorCode") or "",
                    "op":           r.get("operation") or "",
                    "inp": inp, "out": out, "cw": cw, "cr": cr, "cost": cost,
                    "known": known, "display": display, "saved": saved,
                }
                new_facts.append(fact)
                if key:
                    new_rows.append((key, fact))

        floor = _now_ms() - self._memory_window_ms
        with self._lock:
            self._facts.extend(new_facts)
            # Trim memory to the window; older facts stay in SQLite (served via SQL).
            trimmed = False
            if self._facts and self._facts[0]["t"] < floor:
                self._facts = [f for f in self._facts if f["t"] >= floor]
                self._seen_ids = {f["key"] for f in self._facts if f.get("key")}
                trimmed = True
            self._memory_floor_ms = floor
            self._last_updated = datetime.now(timezone.utc)
            self._last_error = error
            if new_facts or trimmed:
                self._facts_version += 1

        # Persist outside the monitor lock (the store has its own lock).
        if self._store is not None and new_rows:
            self._store.add_many(new_rows)

    def _loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            self._ingest(max(self._start_ms, self._poll_from_ms))
            self._poll_from_ms = _now_ms() - _LIVE_OVERLAP_MS
            if self._poll_callback is not None:
                try:
                    self._poll_callback()
                except Exception:  # noqa: BLE001 — alert errors must not kill polling
                    pass

    # ── public API ───────────────────────────────────────────────────────────
    def prime(self) -> None:
        """Blocking initial load of the full tracked window before serving."""
        self._ingest(self._start_ms)
        self._poll_from_ms = _now_ms() - _LIVE_OVERLAP_MS

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="usage-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def snapshot(self, period: str | None = None, flt: dict | None = None,
                 since: str | None = None,
                 start: int | None = None, end: int | None = None) -> dict:
        """Aggregate retained facts for the requested window and filter.

        An explicit absolute (start, end) window — used when drilling into a
        chart bucket — overrides period/since. Results are cached per
        (period, since, start, end, filter) and reused until the next poll.
        """
        if start is not None and end is not None:
            start_ms, end_ms = int(start), int(end)
            period, label = "custom", _abs_window_label(start_ms, end_ms)
            bucket_seconds = _bucket_seconds_for(max(1, end_ms - start_ms))
        else:
            period, start_ms, end_ms, label, bucket_seconds = self._window_for(period, since)
        with self._lock:
            version = self._facts_version
            view = self._facts[:]   # shallow snapshot of the fact list
            err = self._last_error
            updated = self._last_updated

        cache_key = (period, since or "", start or "", end or "", _filter_key(flt))
        with self._cache_lock:
            if version != self._cache_version:  # facts changed → whole cache stale
                self._cache.clear()
                self._cache_version = version
            base = self._cache.get(cache_key)

        if base is None:
            if self._store is not None and start_ms < self._memory_floor_ms:
                # Window reaches older than memory holds → aggregate from SQLite.
                src, _ = self._store.load(start_ms)
            else:
                src = view
            base = _aggregate_facts(src, start_ms, end_ms, bucket_seconds, flt)
            base["window"] = {
                "label": label, "start_ms": start_ms, "end_ms": end_ms, "period": period,
            }
            # Period-over-period comparison: same-length window immediately before.
            prev_start, prev_end, cmp_label = _previous_window(period, start_ms, end_ms)
            if self._store is not None and prev_start < self._memory_floor_ms:
                prev_src, _ = self._store.load(prev_start)
            else:
                prev_src = view
            prev_cost, prev_calls = _sum_window(prev_src, prev_start, prev_end, flt)
            cur_cost = base["totals"]["cost"]
            delta_pct = ((cur_cost - prev_cost) / prev_cost * 100) if prev_cost > 0 else None
            base["comparison"] = {
                "label":      cmp_label,
                "prev_cost":  round(prev_cost, 6),
                "prev_calls": prev_calls,
                "delta_pct":  round(delta_pct, 1) if delta_pct is not None else None,
            }
            with self._cache_lock:
                if version == self._cache_version:  # still fresh — store it
                    self._cache[cache_key] = base

        # Deep-copy the cached base so the cached aggregate stays isolated — a
        # consumer that mutates the returned payload can't corrupt it. The
        # payload is small (bounded models/buckets/breakdowns), so this is far
        # cheaper than re-running O(events) aggregation.
        payload = copy.deepcopy(base)
        payload["generated_at"] = (updated or datetime.now(timezone.utc)).isoformat()
        payload["now_ms"] = _now_ms()
        if flt:
            payload["filter"] = flt
        if err is not None:
            payload["warning"] = {"code": err[0], "message": err[1]}
        return payload

    def window_cost(self, start_ms: int, end_ms: int) -> float:
        """Sum cost over an absolute window without touching the aggregation cache.

        Used for budget checks, which run every poll with a constantly moving
        `end` (now) — routing that through snapshot()'s cache would produce a
        fresh, never-reused cache key on every call and force a full
        re-aggregation each time. This does a direct O(events) sum instead.
        """
        with self._lock:
            view = self._facts[:]
        if self._store is not None and start_ms < self._memory_floor_ms:
            src, _ = self._store.load(start_ms)
        else:
            src = view
        cost, _ = _sum_window(src, start_ms, end_ms, None)
        return cost

    def recent(self, limit: int = 20, region: str | None = None) -> list[dict]:
        """Return the most recent retained events (newest first) as display rows."""
        with self._lock:
            facts = self._facts if not region else [f for f in self._facts if f["region"] == region]
            # nlargest is O(n) + O(limit log limit) and returns newest-first.
            rows = heapq.nlargest(limit, facts, key=lambda f: f["t"])
        return [_recent_row(f) for f in rows]

    def fetch_event(self, region: str | None, event_id: str, t_ms: int,
                    pad_ms: int = 30_000, max_scan: int = 5_000) -> dict:
        """Re-read a single invocation's full record from CloudWatch on demand.

        Used to show the request/response bodies for one event. Content is
        fetched live and never persisted — it stays only in CloudWatch. The scan
        is bounded (narrow time window + a page/scan cap) to keep the cost of
        FilterLogEvents predictable under high log volume.
        """
        if not event_id:
            return {"error": "missing event id"}
        by_region = dict(self._clients)
        targets = [(region, by_region[region])] if region in by_region else self._clients
        for _reg, client in targets:
            try:
                scanned = 0
                for r in iter_log_events(client, t_ms - pad_ms, t_ms + pad_ms):
                    if r.get("_eventId") == event_id:
                        return _event_detail(r)
                    scanned += 1
                    if scanned >= max_scan:
                        break
            except ClientError as exc:
                return {"error": exc.response["Error"]["Code"]}
        return {"error": "not found (aged out, or too many events in the window)"}


def _parse_window(qs: dict) -> tuple[int | None, int | None]:
    """Parse optional absolute ?start=&end= (epoch ms) query params."""
    try:
        start = int(qs.get("start", [None])[0])
        end = int(qs.get("end", [None])[0])
        return start, end
    except (TypeError, ValueError):
        return None, None


def _token_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time token check. expected=None means auth is disabled (always ok)."""
    if expected is None:
        return True
    return presented is not None and hmac.compare_digest(presented, expected)


def build_handler(monitor, alerter, config, token):
    """Build the dashboard's HTTP request handler bound to the given dependencies.

    Extracted from run_web so the routing/auth layer can be exercised by HTTP
    integration tests without standing up pricing or CloudWatch.
    """

    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr request logging; we print our own startup line.
        def log_message(self, *args) -> None:  # noqa: D401
            pass

        def _security_headers(self) -> None:
            for k, v in _SECURITY_HEADERS.items():
                self.send_header(k, v)

        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200, cookie: str | None = None) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"bi_token={cookie}; HttpOnly; SameSite=Strict; Path=/",
                )
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_attachment(self, body: bytes, content_type: str, filename: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        # ── auth ────────────────────────────────────────────────────────────
        def _query_token(self):
            return parse_qs(urlparse(self.path).query).get("token", [None])[0]

        def _presented_token(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "bi_token":
                    return v
            return self._query_token()

        def _authorized(self) -> bool:
            return _token_matches(self._presented_token(), token)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            # Unauthenticated liveness probe (no sensitive data) for load
            # balancers / uptime monitors.
            if path == "/healthz":
                self._send_json({"status": "ok"})
                return

            if not self._authorized():
                if path == "/":
                    self._send_html(_AUTH_REQUIRED_HTML, status=401)
                else:
                    self._send_json({"error": "Unauthorized", "message": "Missing or invalid token"}, status=401)
                return

            if path == "/":
                # If the token arrived via ?token=, drop a cookie so later
                # requests (and reloads) authenticate without it in the URL.
                cookie = self._query_token() if token is not None else None
                self._send_html(DASHBOARD_HTML, cookie=cookie)
            elif path == "/api/config":
                self._send_json(config)
            elif path == "/api/settings":
                self._send_json(alerter.settings())
            elif path == "/api/budgets":
                settings = alerter.settings()
                daily_budget = settings.get("daily_budget")
                monthly_budget = settings.get("monthly_budget")
                now = _now_ms()
                body = {"daily": None, "monthly": None}
                if daily_budget:
                    daily_cost = monitor.window_cost(get_time_range("today")[0], now)
                    body["daily"] = {
                        "budget": daily_budget, "cost": round(daily_cost, 6),
                        "fraction": round(daily_cost / daily_budget, 4) if daily_budget else 0.0,
                    }
                if monthly_budget:
                    monthly_cost = monitor.window_cost(_month_start_ms(), now)
                    body["monthly"] = {
                        "budget": monthly_budget, "cost": round(monthly_cost, 6),
                        "fraction": round(monthly_cost / monthly_budget, 4) if monthly_budget else 0.0,
                    }
                self._send_json(body)
            elif path == "/api/usage":
                qs = parse_qs(parsed.query)
                period = qs.get("period", [None])[0]
                since  = qs.get("since", [None])[0]
                start, end = _parse_window(qs)
                flt = {}
                for dim in ("model", "identity", "region", "operation"):
                    val = qs.get(dim, [None])[0]
                    if val:
                        flt[dim] = val
                self._send_json(monitor.snapshot(period, flt or None, since, start, end))
            elif path == "/api/export":
                qs = parse_qs(parsed.query)
                fmt = (qs.get("format", ["json"])[0]).lower()
                period = qs.get("period", [None])[0]
                since  = qs.get("since", [None])[0]
                start, end = _parse_window(qs)
                flt = {}
                for dim in ("model", "identity", "region", "operation"):
                    val = qs.get(dim, [None])[0]
                    if val:
                        flt[dim] = val
                payload = monitor.snapshot(period, flt or None, since, start, end)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                if fmt == "csv":
                    self._send_attachment(
                        payload_to_csv(payload).encode("utf-8"),
                        "text/csv; charset=utf-8",
                        f"bedrock-usage-{stamp}.csv",
                    )
                else:
                    self._send_attachment(
                        json.dumps(payload, indent=2).encode("utf-8"),
                        "application/json; charset=utf-8",
                        f"bedrock-usage-{stamp}.json",
                    )
            elif path == "/api/recent":
                qs = parse_qs(parsed.query)
                try:
                    limit = int(qs.get("limit", ["20"])[0])
                except ValueError:
                    limit = 20
                limit = max(1, min(limit, 200))
                region = qs.get("region", [None])[0]
                self._send_json({"events": monitor.recent(limit, region)})
            elif path == "/api/event":
                if not config.get("content_enabled", True):
                    self._send_json(
                        {"error": "prompt/response viewing is disabled on this server"},
                        status=403,
                    )
                    return
                qs = parse_qs(parsed.query)
                event_id = qs.get("id", [None])[0]
                region = qs.get("region", [None])[0]
                try:
                    t = int(qs.get("t", ["0"])[0])
                except (TypeError, ValueError):
                    t = 0
                self._send_json(monitor.fetch_event(region, event_id or "", t))
            elif path == "/metrics":
                self._send_text(
                    render_prometheus(monitor.snapshot()),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
            else:
                self._send_json({"error": "NotFound", "message": path}, status=404)

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            if length <= 0 or length > _MAX_BODY_BYTES:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw or b"{}")
            except (json.JSONDecodeError, ValueError):
                return {}

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._authorized():
                self._send_json({"error": "Unauthorized", "message": "Missing or invalid token"}, status=401)
                return
            body = self._read_json()

            if path == "/api/settings":
                def _num(field):
                    """Parse an optional non-negative number field; returns (value, error)."""
                    raw = body.get(field)
                    if raw in ("", None):
                        return None, None
                    try:
                        v = float(raw)
                    except (TypeError, ValueError):
                        return None, f"{field} must be a number"
                    if v < 0:
                        return None, f"{field} must be ≥ 0"
                    return v, None

                threshold, err = _num("threshold")
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                daily_budget, err = _num("daily_budget")
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                monthly_budget, err = _num("monthly_budget")
                if err:
                    self._send_json({"error": err}, status=400)
                    return
                webhook_url = body.get("webhook_url") or None
                sns_topic_arn = body.get("sns_topic_arn") or None
                alerter.configure(threshold, webhook_url, daily_budget, monthly_budget, sns_topic_arn)
                config["threshold"] = threshold  # keep /api/config in sync
                self._send_json(alerter.settings())

            elif path == "/api/test-webhook":
                ok, info = alerter.send_test(
                    body.get("webhook_url") or None,
                    body.get("sns_topic_arn") or None,
                )
                self._send_json({"ok": ok, "message": info})

            else:
                self._send_json({"error": "NotFound", "message": path}, status=404)

    return Handler


def run_web(
    clients,
    bedrock_client,
    host: str,
    port: int,
    token: str | None = None,
    persist: bool = True,
    show_content: bool = True,
) -> None:
    """Start the dashboard HTTP server and block until interrupted.

    The initial view is today's usage with alerts disabled; the time window,
    threshold, webhook and refresh interval are all configured from the web UI.
    When `token` is set, every route requires it (cookie, Bearer header, or
    ?token= query) so the dashboard can be shared safely.
    When `persist` is True, per-event facts are stored in SQLite so history
    survives restarts and outlives CloudWatch log retention.
    """
    regions = [r for r, _ in clients]
    region_label = ", ".join(regions) or "unknown"
    period   = "today"   # initial window; changed at runtime from the dashboard
    threshold = None     # configured from the dashboard's Settings panel
    webhook   = None
    since     = None

    console.print("[dim]Loading pricing data…[/dim]")
    init_pricing_regions(regions, bedrock_client)

    store = None
    if persist:
        try:
            store = FactStore(_DB_PATH)
            store.prune(_now_ms() - _DB_RETENTION_DAYS * 86_400_000)
        except Exception as exc:  # noqa: BLE001 — fall back to in-memory on DB failure
            console.print(f"[yellow]Persistence disabled ({exc}); running in-memory only.[/yellow]")
            store = None

    _, _, label = _resolve_window(period, since)
    alerter = ThresholdAlerter(threshold, webhook, region=region_label, label=label, console=console)
    monitor = UsageMonitor(clients, period, since, store=store, memory_window_days=_MEMORY_WINDOW_DAYS)
    # Wire alerting: window threshold, daily/monthly budgets (warning/critical),
    # and cost-spike anomalies. Skip the work entirely when nothing is configured
    # so the continuous poll loop stays cheap in the common case.
    def _run_alert_checks(snap: dict) -> None:
        alerter.check(snap["totals"]["cost"])
        if alerter.daily_budget or alerter.monthly_budget:
            now = _now_ms()
            # Direct window sums (bypass the aggregation cache — see window_cost).
            daily = monitor.window_cost(get_time_range("today")[0], now)
            monthly = monitor.window_cost(_month_start_ms(), now)
            alerter.check_budgets(daily, monthly, _day_key(), _month_key())
        if snap.get("anomaly") and alerter.webhook_url:
            alerter.notify_anomaly(snap["anomaly"])

    def _poll_alert() -> None:
        if not (alerter.threshold or alerter.daily_budget
                or alerter.monthly_budget or alerter.webhook_url):
            return
        snap = monitor.snapshot()
        # Budget/threshold/anomaly checks can each POST a webhook; run them off
        # the polling thread so a slow/unresponsive endpoint (up to the 10s
        # send_webhook timeout, ×4 possible alerts) never stalls CloudWatch polling.
        threading.Thread(target=_run_alert_checks, args=(snap,), daemon=True).start()

    monitor._poll_callback = _poll_alert
    if store is not None:
        console.print(
            f"[dim]Persistence: {len(monitor._facts)} recent event(s) in memory, "
            f"{store.count()} total on disk at {_DB_PATH} "
            f"(memory window {_MEMORY_WINDOW_DAYS}d, kept {_DB_RETENTION_DAYS}d).[/dim]"
        )

    config = {
        "refresh_seconds": REFRESH_SECONDS,
        "region":          region_label,
        "regions":         regions,
        "threshold":       threshold,
        "periods":         monitor.periods(),
        "default_period":  monitor._default_period,
        "bind":            f"{host}:{port}",
        "poll_seconds":    REFRESH_SECONDS,
        "content_enabled": show_content,
    }

    Handler = build_handler(monitor, alerter, config, token)

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            console.print(
                f"[red]Port {port} is already in use.[/red] "
                "Another bedrock-insights dashboard may still be running.\n"
                f"[dim]Try a different port with [bold]--port[/bold], "
                f"or find the process with [bold]lsof -i :{port}[/bold].[/dim]"
            )
        elif exc.errno in (errno.EACCES, errno.EPERM):
            console.print(
                f"[red]Permission denied binding to {host}:{port}.[/red] "
                "[dim]Ports below 1024 usually require elevated privileges — "
                "pick a higher port with [bold]--port[/bold].[/dim]"
            )
        else:
            console.print(f"[red]Could not start dashboard on {host}:{port}:[/red] {exc}")
        return

    # Prime the cache with the full window once, then let the background
    # poller keep it fresh with cheap incremental queries.
    console.print("[dim]Loading initial data from CloudWatch…[/dim]")
    monitor.prime()
    if alerter.threshold is not None:
        alerter.check(monitor.snapshot()["totals"]["cost"])  # initial load may already exceed
    monitor.start()

    url = f"http://{host}:{port}"
    dash_url = f"{url}/?token={token}" if token else url
    console.print(f"\n[bold green]Bedrock Insights dashboard[/bold green] → [bold]{dash_url}[/bold]")
    console.print(f"[dim]Metrics (Prometheus) → {url}/metrics[/dim]")
    console.print(f"[dim]Window: {label}  •  Region: {region_label}  •  Polling CloudWatch every {REFRESH_SECONDS}s[/dim]")
    if token:
        console.print(
            "[dim]Token auth is enabled — open the URL above (it includes the token). "
            "Scrapers can pass [bold]Authorization: Bearer <token>[/bold].[/dim]"
        )
        if host not in ("127.0.0.1", "localhost", "::1"):
            console.print(
                "[yellow]⚠  Exposed beyond localhost. Traffic is plain HTTP — put it behind a "
                "TLS-terminating reverse proxy for real remote use.[/yellow]"
            )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]⚠  Listening on {host} with no authentication — anyone who can reach this "
            "address can view your AWS usage/cost data. Pass [bold]--token[/bold] to require a token.[/yellow]"
        )
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        if store is not None:
            store.close()
        httpd.shutdown()
        console.print("\n[dim]Dashboard stopped.[/dim]")


_AUTH_REQUIRED_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Bedrock Insights — auth required</title>
<style>body{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:420px;text-align:center;line-height:1.6}code{background:#161b22;border:1px solid #2a3340;
border-radius:6px;padding:2px 6px;color:#58a6ff}</style></head>
<body><div class="box"><h2>🔒 Access token required</h2>
<p>This dashboard is protected. Open it with your token appended to the URL:</p>
<p><code>?token=YOUR_TOKEN</code></p></div></body></html>"""


def _build_dashboard_html() -> str:
    """Assemble the self-contained dashboard page from its HTML/CSS/JS parts."""
    return (
        _load_static("dashboard.html")
        .replace("/*__APP_CSS__*/", _load_static("app.css"))
        .replace("//__APP_JS__", _load_static("app.js"))
    )


DASHBOARD_HTML = _build_dashboard_html()
