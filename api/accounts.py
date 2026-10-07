"""accounts, API keys, usage, BYO credentials, billing, watchlists and alerts.

Self-service (an API key acts on its own account; the operator acts on the operator account):
    GET    /v1/me                      who am I: account, key, scopes, rate-limit budget
    GET    /v1/keys                    this account's keys (metadata only)
    POST   /v1/keys                    create a key; the secret is returned ONCE      [account:manage]
    DELETE /v1/keys/{id}               revoke                                          [account:manage]
    GET    /v1/usage                   recent API usage summary
    GET    /v1/credentials             BYO provider credentials, masked                [account:manage]
    POST   /v1/credentials             store one (encrypted)                           [account:manage]
    DELETE /v1/credentials/{id}        revoke                                          [account:manage]
Billing (models only, no payments)                                                     [billing:read]
    GET /v1/billing/usage, /v1/billing/invoices, /v1/billing/policy
Watchlists and alerts                                                                  [watchlists]
    /v1/watchlists[/{id}[/items[/{item_id}]]], /v1/alerts[/{id}], /v1/alerts/firings, POST /v1/alerts/{id}/test
Operator                                                                               [admin]
    GET/POST /v1/admin/accounts, POST /v1/admin/accounts/{id}/keys, POST /v1/admin/accounts/{id}/status,
    POST /v1/admin/keys/{id}/revoke, GET /v1/admin/usage, GET/POST /v1/admin/fee-policies,
    POST /v1/admin/credits, POST /v1/admin/invoices/draft?period=YYYY-MM
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

import alerts.evaluator  # noqa: F401  registers the `alerts` job
from accounts import credentials, keys, ratelimit
from accounts import usage as api_usage  # registers the usage flush / retention jobs
from accounts.accounts import account_for, create_account, get_account, list_accounts, set_status
from accounts.auth import ALL, SCOPES, Principal, principal, require_scope
from alerts import rules, watchlists
from api.common import TRANSACTION, envelope, resolve_gpu
from billing import invoices, policy

log = logging.getLogger(__name__)
router = APIRouter()


def install(app) -> None:
    """Called by main.py: usage logging + rate-limit headers, and the deploy config checks."""
    keys.check_config()
    try:
        credentials.check_config()
    except RuntimeError as e:  # BYO storage fails closed at use; do not take the whole site down for it
        log.warning("%s: BYO credential storage disabled", e)
    app.add_middleware(api_usage.UsageMiddleware)


def _404(what: str):
    return HTTPException(404, f"{what} not found")


def _400(e: Exception):
    return HTTPException(400, str(e).strip("'\""))


# ---------------------------------------------------------------- self-service

@router.get("/v1/me", tags=["accounts"], summary="The calling account, key, scopes and rate-limit budget")
def me(who: Principal = Depends(principal)):
    acct = get_account(account_for(who))
    key = keys.get_key(who.key_id) if who.key_id else None
    return envelope({
        "principal": who.kind, "account": acct, "key": key,
        "scopes": sorted(who.scopes) if ALL not in who.scopes else ["*"],
        "rate_limit": ratelimit.status(who.key_id, key["rate_limit_per_minute"]) if key else None,
        "rate_limit_defaults": rate_limit_defaults(),
        "scopes_catalog": scopes_catalog(),
    })


def scopes_catalog() -> list[dict]:
    return [{"scope": k, "description": v, "spends_money": k == "route:execute"} for k, v in SCOPES.items()]


def rate_limit_defaults() -> dict:
    """The default per-minute budgets per request class (accounts/ratelimit.py), labelled as defaults."""
    return {
        "label": "defaults that apply to API keys without a per-key override; the operator (site login) is not "
                 "rate limited",
        "classes": {
            "read": {"limit_per_minute": ratelimit.limit_for("read", None), "applies_to": "GET / HEAD / OPTIONS"},
            "write": {"limit_per_minute": ratelimit.limit_for("write", None), "applies_to": "other methods"},
            "execute": {"limit_per_minute": ratelimit.limit_for("execute", None),
                        "applies_to": "non-GET under /v1/route and /v1/deployments"},
        },
        "override_rule": "a key's rate_limit_per_minute replaces the read budget and caps write / execute "
                         "(it can lower, never raise, them)",
    }


@router.get("/v1/scopes", tags=["accounts"], summary="Every scope an API key can hold, with what it allows")
def scopes(who: Principal = Depends(principal)):
    return envelope(scopes_catalog(), count=len(SCOPES),
                    note="operator (site login) holds every scope; keys hold only the scopes granted at creation")


class KeyIn(BaseModel):
    name: str = "default"
    scopes: list[str] = Field(default_factory=lambda: ["data:read"])
    expires_in_days: float | None = Field(None, gt=0, le=3650)
    rate_limit_per_minute: int | None = Field(None, ge=1, le=100_000)


def _make_key(account_id: int, body: KeyIn, grantor: Principal) -> dict:
    if not grantor.has(ALL):
        extra = set(body.scopes) - set(grantor.scopes)
        if extra:
            raise HTTPException(403, f"a key cannot grant scopes it does not hold: {sorted(extra)}")
    expires = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    try:
        return keys.create_key(account_id, body.name, body.scopes, expires, body.rate_limit_per_minute)
    except ValueError as e:
        raise _400(e)
    except KeyError:
        raise _404("account")


@router.get("/v1/keys", tags=["accounts"], summary="This account's API keys (no secrets)")
def list_my_keys(who: Principal = Depends(principal)):
    return envelope(keys.list_keys(account_for(who)))


@router.post("/v1/keys", tags=["accounts"], status_code=201, summary="Create a key; the secret is shown once")
def create_my_key(body: KeyIn, who: Principal = Depends(require_scope("account:manage"))):
    return envelope(_make_key(account_for(who), body, who),
                    note="store `secret` now: OpenGrid keeps only a hash and cannot show it again")


@router.delete("/v1/keys/{key_id}", tags=["accounts"], summary="Revoke one of this account's keys")
def revoke_my_key(key_id: int, who: Principal = Depends(require_scope("account:manage"))):
    try:
        return envelope(keys.revoke_key(key_id, account_for(who)))
    except KeyError:
        raise _404("key")


@router.get("/v1/usage", tags=["accounts"], summary="Recent API usage for this account")
def my_usage(hours: float = Query(24, gt=0, le=24 * 90), who: Principal = Depends(principal)):
    return envelope(api_usage.summary(account_for(who), hours), methodology="billing")


class CredentialIn(BaseModel):
    provider: str
    secret: str = Field(min_length=1, max_length=4096)
    label: str | None = None


@router.get("/v1/credentials", tags=["accounts"], summary="BYO provider credentials (masked) and managed defaults")
def list_credentials(who: Principal = Depends(require_scope("account:manage"))):
    return envelope({"byo": credentials.list_for(account_for(who)),
                     "opengrid_managed_providers": credentials.managed_providers()},
                    note="secrets are never returned; OpenGrid-managed credentials are the default")


@router.post("/v1/credentials", tags=["accounts"], status_code=201, summary="Store a BYO provider credential")
def add_credential(body: CredentialIn, who: Principal = Depends(require_scope("account:manage"))):
    try:
        return envelope(credentials.add(account_for(who), body.provider, body.secret, body.label))
    except ValueError as e:
        raise _400(e)


@router.delete("/v1/credentials/{credential_id}", tags=["accounts"], summary="Revoke a BYO provider credential")
def revoke_credential(credential_id: int, who: Principal = Depends(require_scope("account:manage"))):
    try:
        return envelope(credentials.revoke(account_for(who), credential_id))
    except KeyError:
        raise _404("credential")


# ---------------------------------------------------------------- billing

def _period(t: datetime) -> str:
    return f"{t.year:04d}-{t.month:02d}"


@router.get("/v1/billing/usage", tags=["billing"], summary="Metered compute and its charge lines")
def billing_usage(period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
                  who: Principal = Depends(require_scope("billing:read"))):
    t0 = t1 = None
    if period:
        t0, t1 = invoices.period_bounds(period)
    return envelope(invoices.usage_for(account_for(who), t0, t1), kind=TRANSACTION, methodology="billing", period=period)


@router.get("/v1/billing/invoices", tags=["billing"], summary="Draft invoices (no payments are processed)")
def billing_invoices(who: Principal = Depends(require_scope("billing:read"))):
    a = account_for(who)
    return envelope({"invoices": invoices.invoices_for(a), "credits": invoices.credits_for(a)},
                    kind=TRANSACTION, methodology="billing")


@router.get("/v1/billing/policy", tags=["billing"], summary="The fee policy in force for this account")
def billing_policy(who: Principal = Depends(require_scope("billing:read"))):
    p = policy.active(account_for(who))
    return envelope(policy.as_dict(p) if p else None, methodology="billing",
                    note=None if p else "no fee policy is configured: only provider cost passes through")


# ---------------------------------------------------------------- watchlists

class WatchlistIn(BaseModel):
    name: str


class ItemIn(BaseModel):
    kind: str
    gpu: str | None = None
    provider: str | None = None
    region_group: str | None = None
    index_id: str | None = None


@router.get("/v1/watchlists", tags=["watchlists"], summary="This account's watchlists")
def list_watchlists(current: bool = False, who: Principal = Depends(require_scope("watchlists"))):
    return envelope(watchlists.all_for(account_for(who), with_current=current), methodology="alerts")


@router.post("/v1/watchlists", tags=["watchlists"], status_code=201)
def create_watchlist(body: WatchlistIn, who: Principal = Depends(require_scope("watchlists"))):
    try:
        return envelope(watchlists.create(account_for(who), body.name))
    except ValueError as e:
        raise _400(e)


@router.get("/v1/watchlists/{watchlist_id}", tags=["watchlists"], summary="One watchlist with current observed prices")
def get_watchlist(watchlist_id: int, who: Principal = Depends(require_scope("watchlists"))):
    try:
        return envelope(watchlists.get(account_for(who), watchlist_id), methodology="alerts")
    except KeyError:
        raise _404("watchlist")


@router.patch("/v1/watchlists/{watchlist_id}", tags=["watchlists"])
def rename_watchlist(watchlist_id: int, body: WatchlistIn, who: Principal = Depends(require_scope("watchlists"))):
    try:
        return envelope(watchlists.rename(account_for(who), watchlist_id, body.name))
    except KeyError:
        raise _404("watchlist")


@router.delete("/v1/watchlists/{watchlist_id}", tags=["watchlists"], status_code=204)
def delete_watchlist(watchlist_id: int, who: Principal = Depends(require_scope("watchlists"))):
    try:
        watchlists.remove(account_for(who), watchlist_id)
    except KeyError:
        raise _404("watchlist")
    return Response(status_code=204)


@router.post("/v1/watchlists/{watchlist_id}/items", tags=["watchlists"], status_code=201)
def add_watchlist_item(watchlist_id: int, body: ItemIn, who: Principal = Depends(require_scope("watchlists"))):
    gpu = resolve_gpu(body.gpu) if body.gpu else None
    try:
        return envelope(watchlists.add_item(account_for(who), watchlist_id, body.kind, gpu=gpu, provider=body.provider,
                                            region_group=body.region_group, index_id=body.index_id))
    except ValueError as e:
        raise _400(e)
    except KeyError:
        raise _404("watchlist")


@router.delete("/v1/watchlists/{watchlist_id}/items/{item_id}", tags=["watchlists"], status_code=204)
def delete_watchlist_item(watchlist_id: int, item_id: int, who: Principal = Depends(require_scope("watchlists"))):
    try:
        watchlists.remove_item(account_for(who), watchlist_id, item_id)
    except KeyError:
        raise _404("watchlist item")
    return Response(status_code=204)


# ---------------------------------------------------------------- alerts

class RuleIn(BaseModel):
    params: dict
    name: str | None = None
    channels: list[dict] | None = None
    cooldown_seconds: int | None = None
    status: str = "active"


class RulePatch(BaseModel):
    params: dict | None = None
    name: str | None = None
    channels: list[dict] | None = None
    cooldown_seconds: int | None = None
    status: str | None = None


def _params(p: dict | None) -> dict | None:
    if p and p.get("gpu"):
        p = {**p, "gpu": resolve_gpu(p["gpu"])}
    return p


@router.get("/v1/alerts/firings", tags=["alerts"], summary="In-app feed of fired alerts, newest first")
def alert_firings(rule_id: int | None = None, hours: float = Query(24 * 7, gt=0, le=24 * 365),
                  limit: int = Query(100, ge=1, le=1000), who: Principal = Depends(require_scope("watchlists"))):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return envelope(rules.firings(account_for(who), rule_id, since, limit), methodology="alerts")


@router.get("/v1/alerts", tags=["alerts"], summary="This account's alert rules")
def list_rules(who: Principal = Depends(require_scope("watchlists"))):
    return envelope(rules.all_for(account_for(who)), methodology="alerts")


@router.post("/v1/alerts", tags=["alerts"], status_code=201, summary="Create an alert rule")
def create_rule(body: RuleIn, who: Principal = Depends(require_scope("watchlists"))):
    try:
        out = rules.create(account_for(who), _params(body.params), body.name, body.channels, body.cooldown_seconds, body.status)
    except ValueError as e:
        raise _400(e)
    return envelope(out, methodology="alerts",
                    note="webhook_secret (if present) is shown once; use it to verify X-OpenGrid-Signature")


@router.get("/v1/alerts/{rule_id}", tags=["alerts"])
def get_rule(rule_id: int, who: Principal = Depends(require_scope("watchlists"))):
    try:
        return envelope(rules.get_rule(account_for(who), rule_id), methodology="alerts")
    except KeyError:
        raise _404("alert rule")


@router.patch("/v1/alerts/{rule_id}", tags=["alerts"])
def update_rule(rule_id: int, body: RulePatch, who: Principal = Depends(require_scope("watchlists"))):
    try:
        return envelope(rules.update(account_for(who), rule_id, name=body.name, params=_params(body.params),
                                     channels=body.channels, cooldown_seconds=body.cooldown_seconds, status=body.status))
    except ValueError as e:
        raise _400(e)
    except KeyError:
        raise _404("alert rule")


@router.delete("/v1/alerts/{rule_id}", tags=["alerts"], status_code=204)
def delete_rule(rule_id: int, who: Principal = Depends(require_scope("watchlists"))):
    try:
        rules.remove(account_for(who), rule_id)
    except KeyError:
        raise _404("alert rule")
    return Response(status_code=204)


@router.post("/v1/alerts/{rule_id}/test", tags=["alerts"], summary="Evaluate a rule now (dry: no state change, no delivery)")
def test_rule(rule_id: int, who: Principal = Depends(require_scope("watchlists"))):
    try:
        return envelope(rules.test(account_for(who), rule_id), methodology="alerts")
    except KeyError:
        raise _404("alert rule")


# ---------------------------------------------------------------- operator

admin = require_scope("admin")


class AccountIn(BaseModel):
    name: str
    email: str | None = None
    plan: str = "free"
    settings: dict | None = None


class StatusIn(BaseModel):
    status: str


@router.get("/v1/admin/accounts", tags=["admin"])
def admin_accounts(who: Principal = Depends(admin)):
    return envelope(list_accounts())


@router.post("/v1/admin/accounts", tags=["admin"], status_code=201)
def admin_create_account(body: AccountIn, who: Principal = Depends(admin)):
    try:
        return envelope(create_account(body.name, body.email, body.plan, body.settings))
    except ValueError as e:
        raise _400(e)


@router.post("/v1/admin/accounts/{account_id}/status", tags=["admin"], summary="Suspend or reactivate an account")
def admin_account_status(account_id: int, body: StatusIn, who: Principal = Depends(admin)):
    try:
        return envelope(set_status(account_id, body.status))
    except ValueError as e:
        raise _400(e)
    except KeyError:
        raise _404("account")


@router.post("/v1/admin/accounts/{account_id}/keys", tags=["admin"], status_code=201)
def admin_create_key(account_id: int, body: KeyIn, who: Principal = Depends(admin)):
    return envelope(_make_key(account_id, body, who), note="store `secret` now; it is shown once")


@router.post("/v1/admin/keys/{key_id}/revoke", tags=["admin"])
def admin_revoke_key(key_id: int, who: Principal = Depends(admin)):
    try:
        return envelope(keys.revoke_key(key_id))
    except KeyError:
        raise _404("key")


@router.get("/v1/admin/usage", tags=["admin"], summary="API usage across accounts (or one)")
def admin_usage(account_id: int | None = None, key_id: int | None = None, hours: float = Query(24, gt=0, le=24 * 90),
                who: Principal = Depends(admin)):
    return envelope(api_usage.summary(account_id, hours, key_id=key_id), methodology="billing",
                    buffered=api_usage.pending(), dropped=api_usage.dropped)


class PolicyIn(BaseModel):
    name: str
    components: list[dict]
    account_id: int | None = None
    effective_from: datetime | None = None
    note: str | None = None


@router.get("/v1/admin/fee-policies", tags=["admin"], summary="Every fee policy version")
def admin_policies(account_id: int | None = None, who: Principal = Depends(admin)):
    return envelope(policy.history(account_id), methodology="billing")


@router.post("/v1/admin/fee-policies", tags=["admin"], status_code=201, summary="New fee policy version (global or per account)")
def admin_create_policy(body: PolicyIn, who: Principal = Depends(admin)):
    try:
        return envelope(policy.create(body.name, body.components, body.account_id, body.effective_from, body.note),
                        methodology="billing")
    except ValueError as e:
        raise _400(e)


class CreditIn(BaseModel):
    account_id: int
    amount_usd: Decimal = Field(gt=0)
    reason: str
    expires_at: datetime | None = None


@router.post("/v1/admin/credits", tags=["admin"], status_code=201)
def admin_credit(body: CreditIn, who: Principal = Depends(admin)):
    try:
        return envelope(invoices.add_credit(body.account_id, body.amount_usd, body.reason, body.expires_at))
    except ValueError as e:
        raise _400(e)
    except KeyError:
        raise _404("account")


@router.post("/v1/admin/invoices/draft", tags=["admin"], summary="Build / rebuild DRAFT invoices for a month")
def admin_draft(period: str = Query(..., pattern=r"^\d{4}-\d{2}$"), account_id: int | None = None,
                who: Principal = Depends(admin)):
    try:
        return envelope(invoices.draft(period, account_id), kind=TRANSACTION, methodology="billing",
                        note="drafts only: OpenGrid processes no payments")
    except ValueError as e:
        raise _400(e)
