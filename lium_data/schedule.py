"""Background jobs (registered by api/lium.py on import, like fx.register()).

    lium_public    every 15 min: the keyless feeds + Lium's published throughput. Needs POLLER_ENABLED and
                   LIUM_FEEDS_ENABLED (default on).
    lium_grafana   hourly: rental starts and hourly spend / fleet from Lium's public Grafana dashboards. Needs
                   POLLER_ENABLED, LIUM_FEEDS_ENABLED and LIUM_GRAFANA_ENABLED (default on).
    lium_partner   hourly: the data-partner feeds. Registered only when LIUM_PARTNER_API_KEY is set (and the poller
                   is on); while Lium answers 403 it re-checks every 6 h (partner.FORBIDDEN_RECHECK_HOURS).
"""

_registered = False


def register() -> list[str]:
    global _registered
    from config import settings
    from jobs import job
    from lium_data import grafana, partner, public

    if _registered or not settings.poller_enabled or not settings.lium_feeds_enabled:
        return []
    _registered = True
    names = ["lium_public"]
    job("lium_public", every_seconds=public.POLL_SECONDS, initial_delay_seconds=90)(public.poll)
    if settings.lium_grafana_enabled:
        job("lium_grafana", every_seconds=grafana.POLL_SECONDS, initial_delay_seconds=150)(grafana.poll)
        names.append("lium_grafana")
    if partner.api_key():
        job("lium_partner", every_seconds=partner.POLL_SECONDS, initial_delay_seconds=120)(partner.poll)
        names.append("lium_partner")
    return names
