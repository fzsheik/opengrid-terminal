"""Ops alerts and the suspended-account rule.

Run:  .venv/bin/python tests/test_ops_alerts.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from accounts import keys  # noqa: E402
from alerts import ops  # noqa: E402
from config import settings  # noqa: E402


def _req(method, path):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def test_suspended_accounts_can_only_see_and_stop():
    ok = keys._allowed_while_suspended
    assert ok(_req("GET", "/v1/deployments"))
    assert ok(_req("GET", "/v1/deployments/dep-abc123"))
    assert ok(_req("POST", "/v1/deployments/dep-abc123/terminate"))
    assert ok(_req("POST", "/v1/deployments/dep-abc123/stop"))
    assert not ok(_req("POST", "/v1/route")), "no new routes while suspended"
    assert not ok(_req("POST", "/v1/deployments/dep-abc123/feedback"))
    assert not ok(_req("GET", "/v1/keys"))
    assert not ok(_req("POST", "/v1/deployments"))
    assert not ok(None)


def test_ops_alert_records_and_never_raises():
    calls = []
    import quality.incidents as inc
    real = inc.record_now
    inc.record_now = lambda kind, **kw: calls.append((kind, kw))
    saved = settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret
    try:
        settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret = None, None
        assert not ops.channel_configured()
        out = ops.alert("orphan_detected", "orphan at lambda", provider="lambda", detail={"instance_id": "i-1"})
        assert out["recorded"] and not out["delivered"]
        assert calls[0][0] == "exec_orphan_detected" and calls[0][1]["provider"] == "lambda"
        # A webhook that fails to deliver must not raise into the caller.
        settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret = "https://127.0.0.1/hook", "s3cret"
        assert ops.channel_configured()
        out = ops.alert("termination_failed", "dep-1 still billing")
        assert out["recorded"] and not out["delivered"], "loopback target is refused by the SSRF guard"
    finally:
        inc.record_now = real
        settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret = saved


def test_kill_switch_alerts():
    from routing import control
    seen = []
    real = ops.alert
    ops.alert = lambda kind, title, **kw: seen.append((kind, title, kw.get("provider")))
    try:
        control._ops_alert("Live provisioning on lambda stopped by operator: test", "lambda")
    finally:
        ops.alert = real
    assert seen == [("kill_switch", "Live provisioning on lambda stopped by operator: test", "lambda")]


def test_shutdown_is_never_throttled_like_a_launch():
    from accounts.ratelimit import request_class as cls
    assert cls("POST", "/v1/deployments/dep-1/terminate") == "write"
    assert cls("POST", "/v1/deployments/dep-1/stop") == "write"
    assert cls("POST", "/v1/route") == "execute" and cls("POST", "/v1/route/rr_1/approve") == "execute"
    assert cls("POST", "/v1/admin/validation/start") == "execute"


def test_chat_message_for_discord_and_slack():
    msg = ops._chat_message("termination_failed", "major", "dep-1 still billing", "lambda",
                            {"deployment_id": "dep-1", "est_hourly_exposure_usd": 2.5, "time_in_state": "12m",
                             "suggested_action": "Terminate og-dep-1 in the Lambda console"})
    assert "dep-1 still billing" in msg and "$2.5/h" in msg and "Lambda console" in msg
    assert len(ops._chat_message("k", "major", "x" * 5000, None, None)) <= 2000, "Discord's 2000-char limit"


def test_discord_embed_payload():
    from datetime import datetime, timezone
    assert ops.is_discord("https://discord.com/api/webhooks/1/abc") and not ops.is_discord("https://hooks.slack.com/x")
    assert not ops.is_discord("https://discord.com.evil.example/api/webhooks/1/abc")
    p = ops.discord_payload("termination_failed", "major", "x" * 400, "lambda",
                            {"deployment_id": "dep-1", "est_hourly_exposure_usd": 2.5, "time_in_state": "12m",
                             "suggested_action": "y" * 3000}, datetime.now(timezone.utc))
    e = p["embeds"][0]
    assert e["color"] == 0x91C61D, "OpenGrid logo green"
    assert len(e["title"]) <= 256 and all(len(f["value"]) <= 1024 for f in e["fields"])
    assert p["username"] == "OpenGrid" and p["avatar_url"].startswith("https://")
    assert p["allowed_mentions"] == {"parse": []}, "alerts can never @-mention"
    names = {f["name"] for f in e["fields"]}
    assert {"Deployment", "Provider", "Exposure", "Time in state", "Suggested action"} <= names
    assert e["url"].endswith("/deployments/dep-1")


if __name__ == "__main__":
    for t in (test_discord_embed_payload, test_chat_message_for_discord_and_slack, test_suspended_accounts_can_only_see_and_stop, test_ops_alert_records_and_never_raises,
              test_kill_switch_alerts, test_shutdown_is_never_throttled_like_a_launch):
        t(); print(t.__name__, "ok")
