"""Account rows, and the implicit operator account.

The site operator (basic auth, or open local dev) has no API key and
`Principal.account_id is None`. Watchlists, alerts, keys and usage still need
an owner row, so the operator acts as one account flagged `is_operator`
(created on first use, at most one by a partial unique index). Every handler
that owns data calls `account_for(who)` instead of reading who.account_id.
"""

from __future__ import annotations

import threading

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import normalize
from accounts.auth import Principal
from store.accounts import Account

STATUSES = ("active", "suspended")
_operator_id: int | None = None
_lock = threading.Lock()


def as_dict(a: Account) -> dict:
    return {"id": a.id, "name": a.name, "email": a.email, "status": a.status, "plan": a.plan,
            "settings": a.settings or {}, "is_operator": a.is_operator, "created_at": a.created_at}


def create_account(name: str, email: str | None = None, plan: str = "free", settings: dict | None = None) -> dict:
    if not name or not name.strip():
        raise ValueError("name is required")
    with normalize.SessionLocal.begin() as s:
        a = Account(name=name.strip(), email=email, plan=plan or "free", settings=settings or {},
                    status="active", is_operator=False)
        s.add(a)
        s.flush()
        return as_dict(a)


def get_account(account_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        a = s.get(Account, account_id)
        return as_dict(a) if a else None


def list_accounts() -> list[dict]:
    with normalize.SessionLocal() as s:
        return [as_dict(a) for a in s.scalars(select(Account).order_by(Account.id))]


def set_status(account_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with normalize.SessionLocal.begin() as s:
        a = s.get(Account, account_id)
        if a is None:
            raise KeyError(account_id)
        a.status = status
        return as_dict(a)


def operator_account_id() -> int:
    """The operator's account id, created on first call. Cached for the process."""
    global _operator_id
    if _operator_id is not None:
        return _operator_id
    with _lock:
        if _operator_id is not None:
            return _operator_id
        for _ in range(2):  # a concurrent creator may win the unique index; then read theirs
            with normalize.SessionLocal() as s:
                found = s.scalar(select(Account.id).where(Account.is_operator.is_(True)))
            if found is not None:
                _operator_id = found
                return found
            try:
                with normalize.SessionLocal.begin() as s:
                    a = Account(name="operator", plan="operator", status="active", is_operator=True, settings={})
                    s.add(a)
                    s.flush()
                    _operator_id = a.id
                    return a.id
            except IntegrityError:
                continue
        raise RuntimeError("could not create the operator account")


def reset_cache() -> None:
    """Tests switch databases; the cached operator id belongs to the old one."""
    global _operator_id
    _operator_id = None


def account_for(who: Principal) -> int:
    """The account a request acts as: the key's account, or the operator account."""
    if who.account_id is not None:
        return who.account_id
    if who.kind == "operator":
        return operator_account_id()
    raise HTTPException(401, "no account")
