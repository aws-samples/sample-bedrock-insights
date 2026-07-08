"""HTTP-layer integration tests.

Exercise the real request handler (web.build_handler) over a live loopback
server on an ephemeral port — covering auth, routing, headers and limits
without standing up pricing or CloudWatch.
"""
from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from bedrock_insights import web

TOKEN = "s3cret-token"


class _FakeMonitor:
    def snapshot(self, period=None, flt=None, since=None):
        return {
            "totals": {"cost": 0.0, "calls": 0, "total_tokens": 0, "cache_hit_rate": 0.0},
            "models": [], "identities": [], "regions": [],
            "errors": {"total": 0, "rate": 0.0, "by_code": []},
            "now_ms": 0,
        }

    def recent(self, limit=20, region=None):
        return [{
            "t": 1, "model": "Claude", "identity": "alice", "region": "us-east-1",
            "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 3, "cost": 0.0,
            "price_known": True, "error": "",
        }][:limit]

    def window_cost(self, start_ms, end_ms):
        return 4.2


class _FakeAlerter:
    def __init__(self):
        self._s = {"threshold": None, "webhook_url": None,
                   "daily_budget": None, "monthly_budget": None, "sns_topic_arn": None}

    def settings(self):
        return dict(self._s)

    def configure(self, threshold, webhook_url, daily_budget=None, monthly_budget=None, sns_topic_arn=None):
        self._s = {"threshold": threshold, "webhook_url": webhook_url,
                   "daily_budget": daily_budget, "monthly_budget": monthly_budget,
                   "sns_topic_arn": sns_topic_arn}

    def send_test(self, url=None, topic_arn=None):
        return True, "200"


@pytest.fixture
def server():
    config = {
        "refresh_seconds": 5, "region": "us-east-1", "regions": ["us-east-1"],
        "threshold": None, "periods": [{"id": "today", "label": "Today"}],
        "default_period": "today", "bind": "127.0.0.1:0", "poll_seconds": 5,
    }
    handler = web.build_handler(_FakeMonitor(), _FakeAlerter(), config, TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def test_unauthorized_api_returns_401(server):
    resp, _ = _req(server, "GET", "/api/config")
    assert resp.status == 401


def test_unauthorized_root_returns_401_html(server):
    resp, data = _req(server, "GET", "/")
    assert resp.status == 401
    assert resp.getheader("Content-Type", "").startswith("text/html")
    assert b"token" in data.lower()


def test_query_token_authorizes_and_sets_cookie(server):
    resp, data = _req(server, "GET", f"/?token={TOKEN}")
    assert resp.status == 200
    assert "bi_token=" in (resp.getheader("Set-Cookie") or "")
    assert b"Bedrock Insights" in data


def test_bearer_token_authorizes_config(server):
    resp, data = _req(server, "GET", "/api/config",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status == 200
    assert b'"regions"' in data


def test_cookie_token_authorizes(server):
    resp, _ = _req(server, "GET", "/api/config", headers={"Cookie": f"bi_token={TOKEN}"})
    assert resp.status == 200


def test_recent_endpoint_returns_events(server):
    resp, data = _req(server, "GET", f"/api/recent?limit=20&token={TOKEN}")
    assert resp.status == 200
    assert b'"events"' in data and b"Claude" in data


def test_metrics_endpoint_text(server):
    resp, data = _req(server, "GET", "/metrics",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status == 200
    assert b"bedrock_cost_usd" in data


def test_unknown_path_404(server):
    resp, _ = _req(server, "GET", f"/nope?token={TOKEN}")
    assert resp.status == 404


def test_security_headers_present(server):
    resp, _ = _req(server, "GET", f"/api/config?token={TOKEN}")
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    assert "default-src 'none'" in (resp.getheader("Content-Security-Policy") or "")


def test_settings_post_roundtrip(server):
    import json
    body = json.dumps({"threshold": 12.5}).encode()
    resp, data = _req(server, "POST", "/api/settings",
                      headers={"Authorization": f"Bearer {TOKEN}",
                               "Content-Type": "application/json"}, body=body)
    assert resp.status == 200
    assert json.loads(data)["threshold"] == 12.5


def test_oversized_body_is_ignored(server):
    # Declared body far exceeds the cap → handler reads nothing, treats as empty.
    big = b'{"threshold":1,"pad":"' + b"x" * (70 * 1024) + b'"}'
    resp, data = _req(server, "POST", "/api/settings",
                      headers={"Authorization": f"Bearer {TOKEN}",
                               "Content-Type": "application/json"}, body=big)
    import json
    assert resp.status == 200
    assert json.loads(data)["threshold"] is None  # body ignored → cleared


def test_healthz_is_unauthenticated(server):
    resp, data = _req(server, "GET", "/healthz")  # no token
    assert resp.status == 200
    import json
    assert json.loads(data)["status"] == "ok"


def test_event_endpoint_disabled_returns_403():
    config = {
        "refresh_seconds": 5, "region": "us-east-1", "regions": ["us-east-1"],
        "threshold": None, "periods": [], "default_period": "today",
        "bind": "127.0.0.1:0", "poll_seconds": 5, "content_enabled": False,
    }
    handler = web.build_handler(_FakeMonitor(), _FakeAlerter(), config, TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        resp, _ = _req(port, "GET", "/api/event?id=e1&region=us-east-1&t=1",
                       headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_budgets_endpoint_empty_when_unset(server):
    resp, data = _req(server, "GET", f"/api/budgets?token={TOKEN}")
    assert resp.status == 200
    import json
    body = json.loads(data)
    assert body == {"daily": None, "monthly": None}


def test_budgets_endpoint_reports_progress():
    config = {
        "refresh_seconds": 5, "region": "us-east-1", "regions": ["us-east-1"],
        "threshold": None, "periods": [], "default_period": "today",
        "bind": "127.0.0.1:0", "poll_seconds": 5,
    }
    alerter = _FakeAlerter()
    alerter.configure(None, None, daily_budget=10.0, monthly_budget=100.0)
    handler = web.build_handler(_FakeMonitor(), alerter, config, TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        resp, data = _req(port, "GET", f"/api/budgets?token={TOKEN}")
        assert resp.status == 200
        import json
        body = json.loads(data)
        assert body["daily"]["budget"] == 10.0
        assert body["daily"]["cost"] == 4.2
        assert body["daily"]["fraction"] == pytest.approx(0.42)
        assert body["monthly"]["budget"] == 100.0
    finally:
        httpd.shutdown()
        httpd.server_close()
