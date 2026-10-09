"""Discord / Slack channel feeds: deployments, market events and news, one webhook each.

    ops           OPS_ALERT_WEBHOOK_URL       urgent money-at-risk alerts (alerts/ops.py sends these)
    deployments   DEPLOYMENTS_WEBHOOK_URL     lifecycle: awaiting approval, approved, running, terminated, failed
    market        MARKET_WEBHOOK_URL          notable / major market events (price moves, lows, cheapest, sell-outs)
    news          NEWS_WEBHOOK_URL            stories with relevance >= NEWS_POST_MIN_RELEVANCE

A job (`channel_feeds`, every minute) posts new rows in time order. Each item is posted at most once,
recorded in ops_alert_state (kind "post:<channel>", subject = the item key), so a restart never
re-posts. On a channel's first run only a watermark is set: history is never flooded into a fresh
channel. Delivery uses alerts/notifier.deliver (pinned, SSRF-safe, signed with
OPS_ALERT_WEBHOOK_SECRET); a failure is logged and the item retried next run, never raised.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from config import settings
from jobs import job

log = logging.getLogger(__name__)

GREEN = 0x91C61D            # the OpenGrid logo green
MAX_PER_RUN = 10            # Discord allows ~30 messages/min per webhook; stay well under

CHANNELS = {
    "deployments": "deployments_webhook_url",
    "market": "market_webhook_url",
    "news": "news_webhook_url",
}

# Deployment transitions worth a message (others are internal steps).
_DEP_STATES = {
    "pending_approval": ("🟡", "Route awaiting approval"),
    "approved": ("✅", "Route approved"),
    "provisioning": ("🚀", "Launching"),
    "running": ("🟢", "Running"),
    "terminating": ("🛑", "Terminating"),
    "terminated": ("⚫", "Terminated"),
    "rejected": ("✖️", "Route rejected"),
    "quote_expired": ("⏱️", "Quote expired"),
    "provision_failed": ("❌", "Launch failed (nothing created)"),
    "provider_rejected": ("❌", "Provider rejected the launch"),
    "provider_timeout": ("⚠️", "Provider timed out: outcome unknown"),
    "launch_unknown": ("⚠️", "Launch outcome unknown"),
    "degraded": ("⚠️", "Provider reports a problem"),
    "termination_failed": ("🔴", "Termination failed"),
    "credentials_unavailable": ("🔴", "Credentials unavailable"),
    "orphan_suspected": ("🔴", "Possible orphan"),
}
_SEV_MARK = {"major": "🔴", "notable": "🟠", "info": "🔵"}


def url_for(channel: str) -> str | None:
    return getattr(settings, CHANNELS[channel], None)


def configured() -> dict[str, bool]:
    return {c: bool(url_for(c) and settings.ops_alert_webhook_secret) for c in CHANNELS}


# --------------------------------------------------------------------------
# Message builders (pure): one Discord embed per item; Slack gets the same text
# --------------------------------------------------------------------------

def _base() -> dict:
    return {"username": "OpenGrid", "avatar_url": settings.ops_alert_logo_url, "allowed_mentions": {"parse": []}}


def _link(path: str) -> str | None:
    return settings.public_base_url.rstrip("/") + path if settings.public_base_url else None


def _embed(title: str, description: str | None, fields: list, *, url: str | None, at: datetime | None,
           footer: str) -> dict:
    e = {"title": title[:256], "color": GREEN,
         "fields": [{"name": n, "value": str(v)[:1024], "inline": inline} for n, v, inline in fields
                    if v not in (None, "")][:25],
         "footer": {"text": footer, "icon_url": settings.ops_alert_logo_url},
         "author": {"name": "OpenGrid", "icon_url": settings.ops_alert_logo_url}}
    if description:
        e["description"] = description[:4096]
    if url:
        e["url"] = url
    if at:
        e["timestamp"] = at.isoformat()
    return e


def deployment_message(row: dict) -> dict:
    mark, label = _DEP_STATES.get(row["to_status"], ("•", row["to_status"]))
    gpu = f"{row.get('gpu_count') or 1}× {row.get('gpu') or 'GPU'}"
    price = row.get("quote_price_per_gpu_hour")
    fields = [("Deployment", f"`{row['deployment_id']}`", True), ("Provider", row.get("provider"), True),
              ("GPU", gpu, True),
              ("Quote", None if price is None else f"${float(price):.2f}/GPU-h", True),
              ("Purpose", row.get("purpose"), True), ("By", row.get("actor"), True),
              ("Auto-terminates", row.get("terminate_deadline_at") and
               f"<t:{int(row['terminate_deadline_at'].timestamp())}:f>", True)]
    if row.get("reason"):
        fields.append(("Reason", row["reason"], False))
    return {**_base(), "embeds": [_embed(f"{mark} {label} · {row.get('provider') or ''} {gpu}".strip(), None, fields,
                                         url=_link(f"/deployments/{row['deployment_id']}"), at=row.get("at"),
                                         footer="OpenGrid deployments")]}


def market_message(row: dict) -> dict:
    pct = row.get("pct")
    before, after = row.get("value_before"), row.get("value_after")
    move = None
    if before is not None and after is not None:
        move = f"${float(before):.2f} → ${float(after):.2f}" + (f" ({pct * 100:+.1f}%)" if pct is not None else "")
    fields = [("GPU", row.get("gpu"), True), ("Provider", row.get("provider") or "market", True),
              ("Move", move, True), ("Region", row.get("region_group"), True),
              ("Type", f"`{row['type']}`", True), ("Severity", row.get("severity"), True)]
    from api.common import gpu_slug
    url = _link(f"/gpu/{gpu_slug(row['gpu'])}") if row.get("gpu") else _link("/events")
    return {**_base(), "embeds": [_embed(f"{_SEV_MARK.get(row.get('severity'), '•')} {row['title']}", None, fields,
                                         url=url, at=row.get("occurred_at"), footer="OpenGrid market events")]}


def news_message(row: dict) -> dict:
    topics = ", ".join((row.get("topics") or [])[:5]) or None
    summary = (row.get("summary") or "").strip()
    if len(summary) > 300:
        summary = summary[:297].rsplit(" ", 1)[0] + "…"
    fields = [("Source", row.get("source_name") or row.get("source_id"), True),
              ("Relevance", f"{int(row.get('relevance') or 0)}/100", True), ("Topics", topics, True)]
    return {**_base(), "embeds": [_embed(f"📰 {row.get('title') or 'News'}", summary or None, fields,
                                         url=row.get("url"), at=row.get("published_at"),
                                         footer="OpenGrid news · related, not necessarily causal")]}


def plain_text(payload: dict) -> str:
    """Slack fallback: the embed title plus its fields, one line."""
    e = (payload.get("embeds") or [{}])[0]
    facts = " · ".join(f"{f['name']}: {f['value']}" for f in e.get("fields", []))
    return f"{e.get('title', '')}\n{facts}\n{e.get('url', '')}".strip()[:1900]


# --------------------------------------------------------------------------
# Delivery + once-only bookkeeping
# --------------------------------------------------------------------------

def send(channel: str, payload: dict) -> bool:
    url = url_for(channel)
    if not url or not settings.ops_alert_webhook_secret:
        return False
    from alerts import notifier
    from alerts.ops import is_discord

    body = dict(payload) if is_discord(url) else {"text": plain_text(payload)}
    body.setdefault("source", "opengrid")
    body.setdefault("channel", channel)
    via, _ = notifier.deliver([{"type": "webhook", "url": url}], body, settings.ops_alert_webhook_secret)
    return "webhook" in via


def _claim(session, channel: str, key: str) -> bool:
    """True the first time (channel, key) is seen; the row is the 'already posted' marker."""
    now = datetime.now(timezone.utc)
    r = session.execute(text("""
        INSERT INTO ops_alert_state (kind, subject, status, first_at, last_seen_at, sent_count, updated_at)
        VALUES (:k, :s, 'resolved', :now, :now, 0, :now)
        ON CONFLICT ON CONSTRAINT uq_ops_alert_kind_subject DO NOTHING RETURNING id"""),
        {"k": f"post:{channel}", "s": key[:160], "now": now}).first()
    return r is not None


def _unclaim(session, channel: str, key: str) -> None:
    session.execute(text("DELETE FROM ops_alert_state WHERE kind = :k AND subject = :s"),
                    {"k": f"post:{channel}", "s": key[:160]})


def _watermark(session, channel: str) -> datetime | None:
    r = session.execute(text("SELECT payload FROM ops_alert_state WHERE kind = 'feed_watermark' AND subject = :c"),
                        {"c": channel}).scalar()
    return datetime.fromisoformat(r["at"]) if r and r.get("at") else None


def _set_watermark(session, channel: str, at: datetime) -> None:
    now = datetime.now(timezone.utc)
    session.execute(text("""
        INSERT INTO ops_alert_state (kind, subject, status, first_at, last_seen_at, sent_count, payload, updated_at)
        VALUES ('feed_watermark', :c, 'open', :now, :now, 0, CAST(:p AS jsonb), :now)
        ON CONFLICT ON CONSTRAINT uq_ops_alert_kind_subject
        DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at"""),
        {"c": channel, "now": now, "p": '{"at": "%s"}' % at.isoformat()})


_QUERIES = {
    "deployments": ("""
        SELECT e.id AS key, e.at AS ts, e.deployment_id, e.to_status, e.actor, e.reason, e.at,
               d.provider, d.gpu, d.gpu_count, d.purpose, d.terminate_deadline_at,
               q.quote_price_per_gpu_hour
        FROM deployment_events e JOIN deployments d USING (deployment_id)
        LEFT JOIN quotes q ON q.id = d.quote_id
        WHERE e.at > :since AND e.to_status = ANY(:states) ORDER BY e.at, e.id LIMIT :n""", deployment_message),
    "market": ("""
        SELECT id AS key, detected_at AS ts, type, severity, title, gpu, provider, region_group, pct,
               value_before, value_after, occurred_at
        FROM market_events
        WHERE detected_at > :since AND severity IN ('notable', 'major') AND type <> 'coverage_started'
        ORDER BY detected_at, id LIMIT :n""", market_message),
    "news": ("""
        SELECT COALESCE(i.story_id, i.id) AS key, i.first_seen_at AS ts, i.title, i.summary, i.url, i.relevance,
               i.published_at, i.topics, i.source_id, s.name AS source_name
        FROM news_items i LEFT JOIN news_sources s ON s.id = i.source_id
        WHERE i.first_seen_at > :since AND i.relevance >= :minrel
          AND NOT (i.published_at_inferred AND i.published_at <> i.first_seen_at)
          AND i.published_at > now() - interval '3 days'
        ORDER BY i.first_seen_at, i.id LIMIT :n""", news_message),
}


def run_channel(channel: str, *, now: datetime | None = None) -> dict:
    """Post new items for one channel. Returns counts."""
    import normalize

    out = {"channel": channel, "posted": 0, "failed": 0, "skipped": 0}
    if not configured()[channel]:
        out["skipped"] = "not configured"
        return out
    now = now or datetime.now(timezone.utc)
    sql, build = _QUERIES[channel]
    with normalize.SessionLocal.begin() as s:
        since = _watermark(s, channel)
        if since is None:   # first run: start from now, never flood history
            _set_watermark(s, channel, now)
            out["skipped"] = "watermark initialised"
            return out
        rows = [dict(r._mapping) for r in s.execute(text(sql), {
            "since": since - timedelta(minutes=5), "n": MAX_PER_RUN * 3,
            "states": list(_DEP_STATES), "minrel": settings.news_post_min_relevance})]
    newest = since
    for row in rows:
        key = str(row["key"])
        with normalize.SessionLocal.begin() as s:
            if not _claim(s, channel, key):
                continue      # already posted (overlap window or another worker)
        try:
            ok = send(channel, build(row))
        except Exception:  # noqa: BLE001 - a feed must never break the job runner
            log.exception("channel %s: building/sending item %s failed", channel, key)
            ok = False
        if ok:
            out["posted"] += 1
            newest = max(newest, row["ts"])
        else:
            out["failed"] += 1
            with normalize.SessionLocal.begin() as s:
                _unclaim(s, channel, key)   # retried next run
            break                            # keep order: don't post later items past a failure
        if out["posted"] >= MAX_PER_RUN:
            break
    if newest > since:
        with normalize.SessionLocal.begin() as s:
            _set_watermark(s, channel, newest)
    return out


@job("channel_feeds", every_seconds=60, initial_delay_seconds=90)
def _feeds_job():
    return [run_channel(c) for c in CHANNELS]


# --------------------------------------------------------------------------
# Test messages: what each channel will be about
# --------------------------------------------------------------------------

def test_messages() -> dict[str, dict]:
    now = datetime.now(timezone.utc)
    from alerts import ops

    return {
        "ops": ops.discord_payload(
            "past_deadline", "test", "TEST · #opengrid-ops: urgent, money-at-risk alerts", "lambda",
            {"deployment_id": "dep-example", "est_hourly_exposure_usd": 0.75, "time_in_state": "7m",
             "suggested_action": "This channel gets orphans, failed terminations, past-deadline deployments, unknown "
                                 "states, provider outages, overspend and kill switches. Act on every message."}, now),
        "deployments": deployment_message({
            "deployment_id": "dep-example", "to_status": "running", "provider": "lambda", "gpu_count": 1,
            "gpu": "NVIDIA A10 24GB PCIe", "quote_price_per_gpu_hour": 0.75, "purpose": "validation", "actor": "system",
            "terminate_deadline_at": now + timedelta(minutes=30), "at": now,
            "reason": "TEST · #opengrid-deployments: every route awaiting approval, approval, launch, running, "
                      "termination and failure, with a link to the deployment."}),
        "market": market_message({
            "type": "price_move", "severity": "notable", "provider": "lium", "gpu": "NVIDIA H100 80GB SXM5",
            "title": "TEST · #opengrid-market: notable and major market events (example: Lium cut H100 by 12.0%)",
            "value_before": 1.48, "value_after": 1.30, "pct": -0.12, "region_group": "US", "occurred_at": now}),
        "news": news_message({
            "title": "TEST · #opengrid-news: high-relevance GPU compute news",
            "summary": f"Stories scoring {settings.news_post_min_relevance}+ relevance (GPUs, providers, capacity, "
                       "pricing, export controls), one post per story, linked to the source.",
            "url": _link("/news"), "relevance": 72, "published_at": now, "topics": ["capacity", "pricing_report"],
            "source_name": "OpenGrid"}),
    }


def send_tests() -> dict[str, bool]:
    """Send one example to every configured channel (ops included). Returns {channel: delivered}."""
    from alerts import notifier
    from alerts.ops import is_discord

    out = {}
    for channel, payload in test_messages().items():
        url = settings.ops_alert_webhook_url if channel == "ops" else url_for(channel)
        if not url or not settings.ops_alert_webhook_secret:
            out[channel] = False
            continue
        body = payload if is_discord(url) else {"text": plain_text(payload)}
        via, _ = notifier.deliver([{"type": "webhook", "url": url}], body, settings.ops_alert_webhook_secret)
        out[channel] = "webhook" in via
    return out
