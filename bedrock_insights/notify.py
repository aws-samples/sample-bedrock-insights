"""
Outbound threshold alerts.

Dependency-free (stdlib urllib) POST of a JSON payload to a webhook URL, and/or
a publish to an Amazon SNS topic, when spend crosses a threshold. The webhook
payload includes a Slack-compatible ``text`` field plus structured fields any
generic webhook consumer can use; the SNS message uses the same text plus a
structured JSON attribute so subscribers (email, SMS, Lambda, SQS, other
webhooks via SNS) can act on it either way.

Only aggregate spend figures are sent — never prompt/response content.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console

_console = Console()


def send_webhook(url: str, payload: dict, timeout: int = 10) -> tuple[bool, str]:
    """POST ``payload`` as JSON to ``url``. Returns (ok, info_or_error).

    The URL is user-supplied (configured from the dashboard), so we restrict it
    to http(s) before opening it — this rejects ``file://``, ``ftp://`` and other
    schemes urllib would otherwise honour (SSRF / local-file read).
    """
    if not url.lower().startswith(("http://", "https://")):
        return False, "webhook URL must start with http:// or https://"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # nosemgrep: dynamic-urllib-use-detected — scheme validated to http(s) above
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return (200 <= int(code) < 300), str(code)
    except Exception as exc:  # noqa: BLE001 — network errors must never crash the caller
        return False, str(exc)


def send_sns(topic_arn: str, payload: dict) -> tuple[bool, str]:
    """Publish ``payload`` to an SNS topic. Returns (ok, message_id_or_error).

    The SNS message body is the payload's ``text`` (readable in email/SMS
    subscribers); the full structured payload also rides along as a message
    attribute (``bedrock_insights_payload``) for Lambda/SQS subscribers that
    want to parse it programmatically.
    """
    try:
        sns = boto3.client("sns")
        resp = sns.publish(
            TopicArn=topic_arn,
            Subject="Bedrock Insights alert"[:100],
            Message=payload.get("text", json.dumps(payload)),
            MessageAttributes={
                "bedrock_insights_payload": {
                    "DataType": "String",
                    "StringValue": json.dumps(payload)[:256000],
                },
                "event": {
                    "DataType": "String",
                    "StringValue": str(payload.get("event", "alert")),
                },
            },
        )
        return True, resp.get("MessageId", "")
    except (ClientError, BotoCoreError) as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — never let a delivery failure crash the caller
        return False, str(exc)


class ThresholdAlerter:
    """Fires a one-shot alert (terminal + optional webhook) when cost ≥ threshold.

    De-duplicates: once fired it stays quiet for the rest of the run, so a live
    or web monitor that keeps polling won't spam the channel.
    """

    #: fraction of a budget at which a "warning" (vs "critical") alert fires.
    WARN_FRAC = 0.8

    def __init__(
        self,
        threshold: float | None,
        webhook_url: str | None = None,
        *,
        daily_budget: float | None = None,
        monthly_budget: float | None = None,
        sns_topic_arn: str | None = None,
        region: str = "",
        label: str = "",
        console: Console | None = None,
    ) -> None:
        self.threshold = threshold
        self.webhook_url = webhook_url
        self.daily_budget = daily_budget
        self.monthly_budget = monthly_budget
        self.sns_topic_arn = sns_topic_arn
        self.region = region
        self.label = label
        self._console = console or _console
        self._fired = False               # window-threshold one-shot
        self._budget_fired: dict[str, str] = {}  # "scope:period" -> "warning"|"critical"
        self._anomaly_key: str | None = None     # dedup key of the last anomaly alerted
        self._lock = threading.Lock()

    @property
    def fired(self) -> bool:
        return self._fired

    def configure(
        self,
        threshold: float | None,
        webhook_url: str | None,
        daily_budget: float | None = None,
        monthly_budget: float | None = None,
        sns_topic_arn: str | None = None,
    ) -> None:
        """Update thresholds/budgets/webhook/SNS at runtime (from the dashboard). Re-arms on change."""
        with self._lock:
            if (threshold != self.threshold or webhook_url != self.webhook_url
                    or sns_topic_arn != self.sns_topic_arn):
                self._fired = False  # re-arm so the new threshold can fire again
            if (daily_budget != self.daily_budget or monthly_budget != self.monthly_budget
                    or webhook_url != self.webhook_url or sns_topic_arn != self.sns_topic_arn):
                self._budget_fired.clear()  # re-arm budget levels
                self._anomaly_key = None
            self.threshold = threshold
            self.webhook_url = webhook_url
            self.daily_budget = daily_budget
            self.monthly_budget = monthly_budget
            self.sns_topic_arn = sns_topic_arn

    def settings(self) -> dict:
        with self._lock:
            return {
                "threshold": self.threshold,
                "webhook_url": self.webhook_url,
                "daily_budget": self.daily_budget,
                "monthly_budget": self.monthly_budget,
                "sns_topic_arn": self.sns_topic_arn,
            }

    def _deliver(self, payload: dict) -> None:
        """Fan out one alert payload to whichever channels are configured."""
        with self._lock:
            webhook, topic = self.webhook_url, self.sns_topic_arn
        if webhook:
            ok, info = send_webhook(webhook, payload)
            if ok:
                self._console.print(f"[dim]Alert delivered to webhook (HTTP {info}).[/dim]")
            else:
                self._console.print(f"[yellow]Webhook delivery failed:[/yellow] {info}")
        if topic:
            ok, info = send_sns(topic, payload)
            if ok:
                self._console.print(f"[dim]Alert published to SNS (message {info}).[/dim]")
            else:
                self._console.print(f"[yellow]SNS publish failed:[/yellow] {info}")

    def _build_payload(self, cost: float, threshold: float) -> dict:
        text = (
            f":rotating_light: *Bedrock Insights alert* — spend "
            f"${cost:.4f} crossed threshold ${threshold:.2f}\n"
            f"Window: {self.label or 'n/a'} · Region: {self.region or 'n/a'}"
        )
        return {
            "text":      text,
            "event":     "threshold_exceeded",
            "source":    "bedrock-insights",
            "cost":      round(cost, 6),
            "threshold": threshold,
            "region":    self.region,
            "window":    self.label,
        }

    def send_test(self, url: str | None = None, topic_arn: str | None = None) -> tuple[bool, str]:
        """Send a one-off test message to verify a webhook and/or an SNS topic.

        Tests whichever of (url, topic_arn) is given; falls back to the
        configured webhook/topic when neither argument is passed. Returns the
        result of the first channel attempted (webhook first) for backward
        compatibility with callers expecting a single (ok, info) pair.
        """
        target_url = url if url is not None else self.webhook_url
        target_topic = topic_arn if topic_arn is not None else self.sns_topic_arn
        if not target_url and not target_topic:
            return False, "no webhook URL or SNS topic configured"
        payload = {
            "text":   ":white_check_mark: *Bedrock Insights* test alert — your notification channel is configured correctly.",
            "event":  "test",
            "source": "bedrock-insights",
            "region": self.region,
            "window": self.label,
        }
        results: list[tuple[bool, str]] = []
        if target_url:
            results.append(send_webhook(target_url, payload))
        if target_topic:
            results.append(send_sns(target_topic, payload))
        # Aggregate: ok only if every attempted channel succeeded; report all info.
        ok = all(r[0] for r in results)
        info = "; ".join(r[1] for r in results)
        return ok, info

    def check_budgets(self, daily_cost: float, monthly_cost: float,
                      day_key: str, month_key: str) -> list[tuple[str, str]]:
        """Fire daily/monthly budget alerts at warning (≥80%) and critical (≥100%).

        De-duplicates per (scope, period): warning fires at most once per period,
        critical fires at most once per period (and still fires even if warning
        already did). Returns the list of (scope, level) alerts fired this call.
        """
        fired: list[tuple[str, str]] = []
        for scope, cost, budget, pkey in (
            ("daily", daily_cost, self.daily_budget, day_key),
            ("monthly", monthly_cost, self.monthly_budget, month_key),
        ):
            if not budget or budget <= 0:
                continue
            frac = cost / budget
            level = "critical" if frac >= 1.0 else ("warning" if frac >= self.WARN_FRAC else None)
            if level is None:
                continue
            key = f"{scope}:{pkey}"
            with self._lock:
                prev = self._budget_fired.get(key)
                if level == "critical" and prev == "critical":
                    continue
                if level == "warning" and prev in ("warning", "critical"):
                    continue
                self._budget_fired[key] = level
            icon = "🔴" if level == "critical" else "🟠"
            self._console.print(
                f"\n[bold]{icon} {scope.upper()} BUDGET {level.upper()}:[/bold] "
                f"${cost:.4f} of ${budget:.2f} ({frac * 100:.0f}%)\n"
            )
            payload = {
                "text": (
                    f"{icon} *Bedrock Insights — {scope} budget {level}*\n"
                    f"Spend ${cost:.4f} is {frac * 100:.0f}% of the ${budget:.2f} {scope} budget."
                ),
                "event": f"budget_{level}",
                "source": "bedrock-insights",
                "scope": scope, "cost": round(cost, 6),
                "budget": budget, "fraction": round(frac, 4),
            }
            self._deliver(payload)
            fired.append((scope, level))
        return fired

    def notify_anomaly(self, info: dict) -> bool:
        """Fire a one-off alert for a detected cost spike (deduped per bucket)."""
        key = str(info.get("bucket_t"))
        with self._lock:
            if self._anomaly_key == key:
                return False
            self._anomaly_key = key
        cost = info.get("cost", 0.0)
        baseline = info.get("baseline", 0.0)
        self._console.print(
            f"\n[bold yellow]📈 COST ANOMALY:[/bold yellow] a bucket cost "
            f"${cost:.4f} vs ~${baseline:.4f} baseline\n"
        )
        payload = {
            "text": (
                "📈 *Bedrock Insights — cost anomaly*\n"
                f"A time bucket cost ${cost:.4f}, well above the ~${baseline:.4f} baseline."
            ),
            "event": "cost_anomaly",
            "source": "bedrock-insights",
            "cost": round(cost, 6), "baseline": round(baseline, 6),
        }
        self._deliver(payload)
        return True

    def check(self, cost: float) -> bool:
        """Alert if cost crosses the threshold for the first time. Returns True if fired."""
        with self._lock:
            if self.threshold is None or self._fired or cost < self.threshold:
                return False
            self._fired = True
            threshold = self.threshold

        self._console.print(
            f"\n[bold red]⚠  THRESHOLD EXCEEDED:[/bold red]  "
            f"${cost:.4f} ≥ ${threshold:.2f}\n"
        )
        self._deliver(self._build_payload(cost, threshold))
        return True
