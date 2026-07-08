from __future__ import annotations

from unittest.mock import MagicMock

from bedrock_insights import setup_cmd
from bedrock_insights.client import MAJOR_REGIONS


def _fake_session_factory(configured_regions=(), raise_in=()):
    """Build a fake boto3.Session(...) constructor.

    configured_regions: regions where get_model_invocation_logging_configuration
                        reports logging already enabled (so setup short-circuits).
    raise_in:           regions where constructing the session should raise, to
                        exercise auto_setup's per-region error isolation.
    """
    def factory(profile_name=None, region_name=None):
        if region_name in raise_in:
            raise RuntimeError(f"boom in {region_name}")
        session = MagicMock()
        session.region_name = region_name

        bedrock = MagicMock()
        if region_name in configured_regions:
            bedrock.get_model_invocation_logging_configuration.return_value = {
                "loggingConfig": {"cloudWatchConfig": {"logGroupName": "x", "roleArn": "y"}}
            }
        else:
            bedrock.get_model_invocation_logging_configuration.side_effect = RuntimeError("not configured")

        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}

        def client(name):
            return {"bedrock": bedrock, "logs": MagicMock(), "iam": MagicMock(), "sts": sts}[name]
        session.client.side_effect = client
        return session
    return factory


# ── run_setup default region scope ───────────────────────────────────────────
def test_run_setup_defaults_to_major_regions(monkeypatch):
    seen = []
    monkeypatch.setattr(
        setup_cmd, "_run_setup_region",
        lambda region, profile, retention=None, out=None: seen.append(region),
    )
    setup_cmd.run_setup(None, None)
    assert seen == list(MAJOR_REGIONS)


def test_run_setup_respects_explicit_region(monkeypatch):
    seen = []
    monkeypatch.setattr(
        setup_cmd, "_run_setup_region",
        lambda region, profile, retention=None, out=None: seen.append(region),
    )
    setup_cmd.run_setup("us-east-1,eu-west-1", None)
    assert seen == ["us-east-1", "eu-west-1"]


# ── auto_setup: idempotent + isolates per-region failures ───────────────────
def test_auto_setup_skips_already_configured(monkeypatch, capsys):
    monkeypatch.setattr(setup_cmd.boto3, "Session",
                        _fake_session_factory(configured_regions={"us-east-1"}))
    # Should not raise, and should short-circuit for the already-configured region.
    setup_cmd.auto_setup(["us-east-1"], None)
    assert "already enabled" in capsys.readouterr().out


def test_auto_setup_continues_after_region_failure(monkeypatch):
    order = []

    def fake_run_setup_region(region, profile, retention=None, out=None):
        order.append(region)
        if region == "us-east-1":
            raise RuntimeError("boom")

    monkeypatch.setattr(setup_cmd, "_run_setup_region", fake_run_setup_region)
    # Must not raise even though one region's setup blows up (order isn't
    # guaranteed since regions now run concurrently — just check both ran).
    setup_cmd.auto_setup(["us-east-1", "us-west-2"], None)
    assert sorted(order) == ["us-east-1", "us-west-2"]


def test_auto_setup_empty_region_list_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(setup_cmd, "_run_setup_region", lambda *a, **k: called.append(1))
    setup_cmd.auto_setup([], None)
    assert called == []


def test_auto_setup_runs_regions_concurrently(monkeypatch):
    """Total time should be close to the slowest region, not the sum of all."""
    import time as time_mod

    def slow_setup(region, profile, retention=None, out=None):
        time_mod.sleep(0.2)

    monkeypatch.setattr(setup_cmd, "_run_setup_region", slow_setup)
    start = time_mod.time()
    setup_cmd.auto_setup(["a", "b", "c", "d"], None)
    elapsed = time_mod.time() - start
    # Sequential would take ~0.8s; concurrent should be well under that.
    assert elapsed < 0.6
