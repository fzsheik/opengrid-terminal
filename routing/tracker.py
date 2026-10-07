"""Background job: poll every live deployment's status, record uptime and interruptions,
and retry billing for terminated deployments whose usage has not been recorded yet.

Registered on import; api/routing.py imports this module so the job loads with the app.
"""

import logging

from jobs import job
from routing import deployments, transactions

log = logging.getLogger(__name__)


def track() -> dict:
    ids = deployments.live_ids()
    checked = failed = 0
    for dep_id in ids:
        try:
            r = deployments.refresh(dep_id)
            checked += 1
            failed += 0 if r.get("refreshed") else 1
        except Exception:
            failed += 1
            log.exception("tracking %s failed", dep_id)
    billed = 0
    for dep_id in transactions.unbilled():
        billed += transactions.bill(dep_id) is not None
    return {"live": len(ids), "checked": checked, "not_refreshed": failed, "billed": billed}


@job("routing_tracker", every_seconds=60, initial_delay_seconds=40)
def _track_job():
    return track()
