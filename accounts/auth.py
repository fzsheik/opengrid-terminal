"""Who is calling. The contract every protected endpoint depends on.

    from accounts.auth import Principal, principal, require_scope

    @router.post("/v1/route")
    def route(req: RouteRequest, who: Principal = Depends(require_scope("route:execute"))):
        who.account_id   # None for the site operator (basic auth / open dev)

Two ways in:
    Authorization: Bearer opg_...   an OpenGrid API key (accounts.keys verifies it)
    the site's own login            /login cookie or basic auth (accounts.sitelogin): the operator
                                    (APP_USER/APP_PASSWORD, every scope), or the read-only viewer
                                    (VIEWER_USER/VIEWER_PASSWORD, VIEWER below); or nothing at all when
                                    APP_PASSWORD is unset (local dev: operator).

The middleware in main.py lets Bearer requests to /v1 past the site password;
this module is then the only gate, so every /v1 handler must depend on it.
"""

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request

from config import settings

ALL = "*"

# Scopes a key can hold. Data reads are separate from anything that spends money.
SCOPES = {
    "data:read": "read market data, indices, events, news",
    "route:preview": "dry-run routing decisions",
    "probes:write": "post provider probe results from a remote probe agent (probes/agent.py)",
    "route:execute": "provision compute through OpenGrid (spends money)",
    "deployments:read": "see your deployments",
    "deployments:write": "stop / terminate your deployments",
    "watchlists": "manage watchlists and alerts",
    "billing:read": "see usage and invoices",
    "keys:read": "list this account's API keys (metadata only)",
    "account:manage": "create / revoke this account's API keys and BYO provider credentials",
    "admin": "operator functions (on an API key, honoured only if the operator flagged it platform_admin)",
}


@dataclass(frozen=True)
class Principal:
    kind: str                      # "operator" | "viewer" | "api_key" | "public"
    account_id: int | None = None  # None for the operator
    key_id: int | None = None
    scopes: frozenset = field(default_factory=frozenset)
    platform_admin: bool = False   # an API key the operator explicitly allowed to act cross-tenant
    api_version: str | None = None  # the key's pinned Data API version (api/contract.py)

    def has(self, scope: str) -> bool:
        return ALL in self.scopes or scope in self.scopes


OPERATOR = Principal(kind="operator", scopes=frozenset({ALL}))
# An anonymous visitor when PUBLIC_PAGES is on: market data reads only, no account.
PUBLIC = Principal(kind="public", scopes=frozenset({"data:read"}))
# The read-only site login (VIEWER_USER): market data and Data API reads only. Deliberately data:read and nothing else:
# it has no account (account_id None), and the deployment / job / reservation reads treat "no account" as the
# operator's own rows, so any further read scope would show the operator's private data.
VIEWER = Principal(kind="viewer", scopes=frozenset({"data:read"}))


def _site_role(request: Request, header: str) -> str | None:
    """"operator" / "viewer" for the site login (basic auth or the /login cookie), else None."""
    import main
    from accounts import sitelogin  # the site password check lives with the middleware

    if not settings.app_password:
        from accounts.security import deployed

        return None if deployed() else sitelogin.OPERATOR  # open only on a developer's machine
    return main.password_ok(header) or sitelogin.role(request)  # basic auth, or the /login session cookie


def _public_path(request: Request) -> bool:
    from api import pages

    return pages.is_public(request)


def _public_read(request: Request) -> bool:
    """GET/HEAD, or a POST to one of the read-only computations in api/pages.PUBLIC_POST."""
    if request.method in ("GET", "HEAD"):
        return True
    from api import pages

    return request.method == "POST" and request.url.path in pages.PUBLIC_POST


def principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        from accounts import keys

        who = keys.verify(header[7:].strip(), request)  # raises 401/429 itself
    elif (role := _site_role(request, header)) is not None:
        who = OPERATOR if role == "operator" else VIEWER
    elif settings.public_pages and _public_read(request) and _public_path(request):
        who = PUBLIC
    else:
        raise HTTPException(401, "OpenGrid API key required", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = who
    return who


def require_any_scope(*scopes: str):
    """Any one of `scopes` suffices (e.g. keys:read or account:manage)."""
    def dep(who: Principal = Depends(principal)) -> Principal:
        if not any(who.has(s) for s in scopes):
            raise HTTPException(403, f"this key needs one of the scopes {sorted(scopes)}")
        return who

    return dep


def require_scope(scope: str):
    def dep(who: Principal = Depends(principal)) -> Principal:
        if not who.has(scope):
            raise HTTPException(403, f"this key lacks the {scope!r} scope")
        return who

    return dep
