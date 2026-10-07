"""The design-partner onboarding state machine, computed from what the system actually holds.

    account_created -> key_created -> credentials_connected (optional) -> first_preview
        -> first_supervised_deployment -> feedback_given

Nothing is stored: each step is true when its evidence exists (an active API key, a BYO
credential, a preview route request, a deployment the provider reported running, a feedback
row). So the state cannot drift from reality, and an operator never edits it by hand.
"""

from __future__ import annotations

from sqlalchemy import text

import normalize
import observability as obs

STEPS = (
    ("account_created", "Account created", False, None),
    ("key_created", "API key created", False, "POST /v1/keys"),
    ("credentials_connected", "Provider credentials connected (optional: OpenGrid-managed keys work too)", True,
     "POST /v1/credentials"),
    ("first_preview", "First route preview", False, "POST /v1/route/preview"),
    ("first_supervised_deployment", "First supervised deployment ran", False,
     "POST /v1/route (the operator approves the quote)"),
    ("feedback_given", "Feedback on a deployment", False, "POST /v1/deployments/{deployment_id}/feedback"),
)


def _one(s, sql: str, **p):
    return s.execute(text(sql), p).first()


def status(account_id: int) -> dict:
    done: dict[str, tuple[bool, str | None]] = {}
    with normalize.SessionLocal() as s:
        acct = _one(s, "SELECT id, created_at FROM accounts WHERE id = :a", a=account_id)
        done["account_created"] = (acct is not None, acct and f"account {acct.id} created {acct.created_at.isoformat()}")
        k = _one(s, "SELECT count(*) AS n, min(created_at) AS first_at FROM api_keys WHERE account_id = :a "
                    "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())", a=account_id)
        done["key_created"] = (bool(k and k.n), k and k.n and f"{k.n} active key(s), first {k.first_at.isoformat()}")
        c = _one(s, "SELECT count(*) AS n FROM provider_credentials WHERE account_id = :a AND revoked_at IS NULL",
                 a=account_id)
        done["credentials_connected"] = (bool(c and c.n), c and c.n and f"{c.n} BYO credential(s)")
        p = _one(s, "SELECT id, created_at FROM route_requests WHERE account_id = :a AND preview "
                    "ORDER BY created_at LIMIT 1", a=account_id)
        done["first_preview"] = (p is not None, p and f"{p.id} at {p.created_at.isoformat()}")
        purpose = "AND coalesce(d.purpose, 'customer') = 'customer'" if "purpose" in obs.columns(s, "deployments") else ""
        r = _one(s, "SELECT d.deployment_id, min(e.at) AS first_at FROM deployments d JOIN deployment_events e "
                    "ON e.deployment_id = d.deployment_id AND e.to_status = 'running' "
                    f"WHERE d.account_id = :a {purpose} GROUP BY d.deployment_id ORDER BY 2 LIMIT 1", a=account_id)
        done["first_supervised_deployment"] = (r is not None, r and f"{r.deployment_id} running at {r.first_at.isoformat()}")
        f = _one(s, "SELECT deployment_id, created_at FROM deployment_feedback WHERE account_id = :a "
                    "ORDER BY created_at LIMIT 1", a=account_id) if obs.has_table(s, "deployment_feedback") else None
        done["feedback_given"] = (f is not None, f and f"on {f.deployment_id}")
    steps, nxt = [], None
    for key, label, optional, how in STEPS:
        ok, evidence = done[key]
        steps.append({"step": key, "label": label, "done": bool(ok), "optional": optional,
                      "evidence": evidence or None, "how": how})
        if not ok and not optional and nxt is None:
            nxt = {"step": key, "label": label, "how": how}
    required = [x for x in steps if not x["optional"]]
    return {"account_id": account_id, "steps": steps, "next_step": nxt,
            "complete": all(x["done"] for x in required),
            "progress": f"{sum(x['done'] for x in required)}/{len(required)} required steps",
            "links": {"guide": "/methodology/first-live-route", "preview": "/v1/route/preview",
                      "deployments": "/v1/deployments", "profile": "/v1/partners/me"}}
