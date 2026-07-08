from __future__ import annotations

from bedrock_insights import notify
from bedrock_insights.notify import ThresholdAlerter


def test_alerter_fires_once_when_crossed(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_webhook", lambda url, payload, timeout=10: sent.append((url, payload)) or (True, "200"))

    a = ThresholdAlerter(2.0, "https://hooks.example/x", region="us-east-1", label="Today")
    assert a.check(1.0) is False        # below threshold
    assert a.check(2.5) is True         # crosses → fires
    assert a.check(3.0) is False        # already fired → quiet
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "https://hooks.example/x"
    assert payload["event"] == "threshold_exceeded"
    assert payload["cost"] == 2.5
    assert payload["threshold"] == 2.0
    assert "text" in payload and "$2.50" in payload["text"]


def test_alerter_no_threshold_never_fires(monkeypatch):
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: (True, "200"))
    a = ThresholdAlerter(None, "https://hooks.example/x")
    assert a.check(9999.0) is False


def test_alerter_without_webhook_still_fires_locally(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: calls.append(1) or (True, "200"))
    a = ThresholdAlerter(1.0, None)     # no webhook URL
    assert a.check(5.0) is True
    assert calls == []                  # no webhook attempted


def test_alerter_webhook_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: (False, "Connection refused"))
    a = ThresholdAlerter(1.0, "https://hooks.example/x")
    # Should fire (return True) and swallow the delivery error without raising.
    assert a.check(2.0) is True


def test_send_webhook_posts_json(monkeypatch):
    captured = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["method"] = req.get_method()
        captured["ctype"] = req.headers.get("Content-type")
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    ok, info = notify.send_webhook("https://hooks.example/y", {"text": "hi"})
    assert ok is True and info == "200"
    assert captured["method"] == "POST"
    assert captured["ctype"] == "application/json"
    assert b'"text"' in captured["data"]


def test_send_webhook_handles_exception(monkeypatch):
    def boom(req, timeout=10):
        raise OSError("network down")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    ok, info = notify.send_webhook("https://hooks.example/y", {"text": "hi"})
    assert ok is False
    assert "network down" in info


def test_send_webhook_rejects_non_http_scheme(monkeypatch):
    # A non-http(s) URL (e.g. file://, ftp://) must be refused before urlopen is
    # ever called, so it can't be used for SSRF / local-file reads.
    def must_not_call(req, timeout=10):
        raise AssertionError("urlopen must not be reached for a non-http(s) URL")
    monkeypatch.setattr(notify.urllib.request, "urlopen", must_not_call)
    for bad in ("file:///etc/passwd", "ftp://example/x", "gopher://x"):
        ok, info = notify.send_webhook(bad, {"text": "hi"})
        assert ok is False
        assert "http" in info


def test_alerter_configure_rearms(monkeypatch):
    fires = []
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: fires.append(1) or (True, "200"))
    a = ThresholdAlerter(2.0, "https://hooks.example/x")
    assert a.check(3.0) is True          # fires
    assert a.check(3.0) is False         # stays quiet
    a.configure(2.0, "https://hooks.example/x")  # no change → still armed-spent
    assert a.check(3.0) is False
    a.configure(5.0, "https://hooks.example/x")  # threshold changed → re-armed
    assert a.check(6.0) is True
    assert len(fires) == 2


def test_alerter_settings_roundtrip():
    a = ThresholdAlerter(None, None)
    assert a.settings() == {
        "threshold": None, "webhook_url": None,
        "daily_budget": None, "monthly_budget": None, "sns_topic_arn": None,
    }
    a.configure(3.5, "https://hooks.example/x", 10.0, 200.0, "arn:aws:sns:us-east-1:123:topic")
    assert a.settings() == {
        "threshold": 3.5, "webhook_url": "https://hooks.example/x",
        "daily_budget": 10.0, "monthly_budget": 200.0,
        "sns_topic_arn": "arn:aws:sns:us-east-1:123:topic",
    }


def test_alerter_send_test_uses_given_url(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_webhook", lambda url, payload, timeout=10: sent.append((url, payload)) or (True, "200"))
    a = ThresholdAlerter(None, None)
    ok, info = a.send_test("https://hooks.example/test")
    assert ok is True
    assert sent[0][0] == "https://hooks.example/test"
    assert sent[0][1]["event"] == "test"


def test_alerter_send_test_no_url():
    a = ThresholdAlerter(None, None)
    ok, info = a.send_test(None)
    assert ok is False


# ── budgets (warning / critical, per-period dedup) ───────────────────────────
def test_budget_warning_then_critical(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_webhook",
                        lambda u, p, timeout=10: sent.append(p) or (True, "200"))
    a = ThresholdAlerter(None, "https://hooks.example/x", daily_budget=10.0)
    assert a.check_budgets(8.5, 0, "2026-06-30", "2026-06") == [("daily", "warning")]
    assert a.check_budgets(8.6, 0, "2026-06-30", "2026-06") == []          # same level, quiet
    assert a.check_budgets(10.0, 0, "2026-06-30", "2026-06") == [("daily", "critical")]
    assert a.check_budgets(12.0, 0, "2026-06-30", "2026-06") == []          # already critical
    assert [p["event"] for p in sent] == ["budget_warning", "budget_critical"]


def test_budget_rearms_next_period(monkeypatch):
    monkeypatch.setattr(notify, "send_webhook", lambda *a, **k: (True, "200"))
    a = ThresholdAlerter(None, None, monthly_budget=100.0)
    assert a.check_budgets(0, 100, "d", "2026-06") == [("monthly", "critical")]
    assert a.check_budgets(0, 100, "d", "2026-06") == []                    # same month quiet
    assert a.check_budgets(0, 100, "d", "2026-07") == [("monthly", "critical")]  # new month re-arms


def test_no_budget_never_fires():
    a = ThresholdAlerter(None, None)
    assert a.check_budgets(999, 999, "d", "m") == []


# ── anomaly alerts (deduped per bucket) ──────────────────────────────────────
def test_notify_anomaly_dedup(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_webhook",
                        lambda u, p, timeout=10: sent.append(p) or (True, "200"))
    a = ThresholdAlerter(None, "https://hooks.example/x")
    assert a.notify_anomaly({"bucket_t": 123, "cost": 5.0, "baseline": 1.0}) is True
    assert a.notify_anomaly({"bucket_t": 123, "cost": 6.0, "baseline": 1.0}) is False  # same bucket
    assert a.notify_anomaly({"bucket_t": 456, "cost": 7.0, "baseline": 1.0}) is True
    assert len(sent) == 2 and sent[0]["event"] == "cost_anomaly"


# ── Amazon SNS delivery ───────────────────────────────────────────────────────
def test_send_sns_publishes_message(monkeypatch):
    captured = {}

    class _FakeSns:
        def publish(self, **kwargs):
            captured.update(kwargs)
            return {"MessageId": "msg-123"}

    monkeypatch.setattr(notify.boto3, "client", lambda name: _FakeSns())
    ok, info = notify.send_sns("arn:aws:sns:us-east-1:123:topic", {"text": "hi", "event": "test"})
    assert ok is True
    assert info == "msg-123"
    assert captured["TopicArn"] == "arn:aws:sns:us-east-1:123:topic"
    assert captured["Message"] == "hi"
    assert captured["MessageAttributes"]["event"]["StringValue"] == "test"


def test_send_sns_handles_client_error(monkeypatch):
    from botocore.exceptions import ClientError

    class _FakeSns:
        def publish(self, **kwargs):
            raise ClientError({"Error": {"Code": "NotFound", "Message": "no topic"}}, "Publish")

    monkeypatch.setattr(notify.boto3, "client", lambda name: _FakeSns())
    ok, info = notify.send_sns("arn:aws:sns:us-east-1:123:missing", {"text": "hi"})
    assert ok is False
    assert "no topic" in info or "NotFound" in info


def test_send_sns_falls_back_to_json_when_no_text(monkeypatch):
    captured = {}

    class _FakeSns:
        def publish(self, **kwargs):
            captured.update(kwargs)
            return {"MessageId": "m1"}

    monkeypatch.setattr(notify.boto3, "client", lambda name: _FakeSns())
    notify.send_sns("arn:aws:sns:us-east-1:123:topic", {"event": "budget_critical", "cost": 5.0})
    assert '"event"' in captured["Message"]


def test_alerter_delivers_to_both_webhook_and_sns(monkeypatch):
    webhook_calls = []
    sns_calls = []
    monkeypatch.setattr(notify, "send_webhook",
                        lambda url, payload, timeout=10: webhook_calls.append(url) or (True, "200"))
    monkeypatch.setattr(notify, "send_sns",
                        lambda arn, payload: sns_calls.append(arn) or (True, "m1"))
    a = ThresholdAlerter(1.0, "https://hooks.example/x")
    a.configure(1.0, "https://hooks.example/x", sns_topic_arn="arn:aws:sns:us-east-1:123:topic")
    assert a.check(2.0) is True
    assert webhook_calls == ["https://hooks.example/x"]
    assert sns_calls == ["arn:aws:sns:us-east-1:123:topic"]


def test_alerter_sns_only_no_webhook(monkeypatch):
    sns_calls = []
    monkeypatch.setattr(notify, "send_sns",
                        lambda arn, payload: sns_calls.append(payload) or (True, "m1"))
    a = ThresholdAlerter(1.0, None, sns_topic_arn="arn:aws:sns:us-east-1:123:topic")
    assert a.check(5.0) is True
    assert len(sns_calls) == 1
    assert sns_calls[0]["event"] == "threshold_exceeded"


def test_send_test_with_explicit_topic_arn(monkeypatch):
    sns_calls = []
    monkeypatch.setattr(notify, "send_sns",
                        lambda arn, payload: sns_calls.append((arn, payload)) or (True, "m1"))
    a = ThresholdAlerter(None, None)
    ok, info = a.send_test(topic_arn="arn:aws:sns:us-east-1:123:topic")
    assert ok is True
    assert sns_calls[0][0] == "arn:aws:sns:us-east-1:123:topic"
    assert sns_calls[0][1]["event"] == "test"


def test_send_test_no_channel_configured():
    a = ThresholdAlerter(None, None)
    ok, info = a.send_test()
    assert ok is False
    assert "no webhook" in info.lower() or "topic" in info.lower()


def test_budget_and_anomaly_deliver_to_sns(monkeypatch):
    sns_calls = []
    monkeypatch.setattr(notify, "send_sns",
                        lambda arn, payload: sns_calls.append(payload["event"]) or (True, "m1"))
    a = ThresholdAlerter(None, None, daily_budget=10.0, sns_topic_arn="arn:aws:sns:us-east-1:123:topic")
    a.check_budgets(9.0, 0, "d", "m")
    a.notify_anomaly({"bucket_t": 1, "cost": 5.0, "baseline": 1.0})
    assert "budget_warning" in sns_calls
    assert "cost_anomaly" in sns_calls
