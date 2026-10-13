"""Runner callback tokens: derived, never stored (methodology/jobs.md section "Runner authentication").

    token  = HMAC-SHA256(pepper, "job:" + job_id + ":" + nonce)   hex, 64 chars
    header = Authorization: Bearer ogj_<job_id>.<token>

The pepper is the API-key pepper (accounts.keys.pepper(): settings.api_key_pepper, required when deployed). Only
the nonce, expiry and revocation live in job_tokens; the token itself exists only inside the rendered bootstrap
script handed to the provider (whose stored copy is redacted) and on the GPU. Rotating the pepper invalidates
every runner token, like every API key.

verify() checks, in this order, and fails closed: header shape -> job exists -> a token row of THIS job whose
derived value matches (constant time) -> not revoked -> not expired -> job not finished. A token for job A can
never authenticate a call for job B (the job id is inside the HMAC and in the URL, and both must agree).
Accepting a call extends nothing (no sliding expiry).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update

import normalize
from config import settings
from store.jobs import Job, JobToken

PREFIX = "ogj_"
JOB_ID_RE = re.compile(r"^job-[0-9a-f]{8,32}\Z")   # \Z, not $: no trailing newline
_HEADER_RE = re.compile(r"^ogj_(job-[0-9a-f]{8,32})\.([0-9a-f]{64})\Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pepper() -> bytes:
    from accounts import keys
    return keys.pepper()


def derive(job_id: str, nonce: str) -> str:
    return hmac.new(_pepper(), f"job:{job_id}:{nonce}".encode(), hashlib.sha256).hexdigest()


def header_value(job_id: str, token: str) -> str:
    return f"Bearer {PREFIX}{job_id}.{token}"


def issue(job_id: str, *, expires_at: datetime) -> tuple[int, str]:
    """A new token row for `job_id` -> (token_id, token). The caller embeds the token in the bootstrap and
    forgets it; nothing here stores or logs it."""
    nonce = secrets.token_hex(24)
    with normalize.SessionLocal.begin() as s:
        row = JobToken(job_id=job_id, nonce=nonce, created_at=_now(), expires_at=expires_at)
        s.add(row)
        s.flush()
        tid = row.id
    return tid, derive(job_id, nonce)


def revoke_all(s, job_id: str, reason: str) -> int:
    r = s.execute(update(JobToken).where(JobToken.job_id == job_id, JobToken.revoked_at.is_(None))
                  .values(revoked_at=_now(), revoke_reason=reason[:200]))
    return r.rowcount or 0


@dataclass(frozen=True)
class RunnerAuth:
    job_id: str
    token_id: int
    account_id: int | None


def _deny(code: str, message: str, status: int = 401):
    raise HTTPException(status, {"code": code, "message": message},
                        headers={"WWW-Authenticate": "Bearer"} if status == 401 else None)


def verify(authorization: str | None, job_id: str, *, allow_finished: bool = False) -> RunnerAuth:
    """Authenticate a runner callback for `job_id` (the URL's). Raises HTTPException 401/403/404/409."""
    h = (authorization or "").strip()
    if not h.lower().startswith("bearer "):
        _deny("runner_token_required", "runner callbacks authenticate only with the job token: "
                                       "Authorization: Bearer ogj_<job_id>.<token>")
    m = _HEADER_RE.match(h[7:].strip())
    if not m:
        # An OpenGrid API key (opg_...) or anything else is never accepted here.
        _deny("runner_token_invalid", "not a job runner token (expected ogj_<job_id>.<token>; API keys are not "
                                      "accepted on runner routes)")
    tok_job, tok = m.group(1), m.group(2)
    if not hmac.compare_digest(tok_job, job_id or ""):
        _deny("runner_token_wrong_job", "this token belongs to a different job", 403)
    with normalize.SessionLocal() as s:
        job = s.get(Job, job_id)
        rows = list(s.scalars(select(JobToken).where(JobToken.job_id == job_id).order_by(JobToken.id)))
    if job is None:
        _deny("runner_token_invalid", "invalid runner token")   # same answer as a bad token: no job oracle
    match = None
    for r in rows:   # constant-time compare against every token row of this job
        if hmac.compare_digest(derive(job_id, r.nonce), tok):
            match = r
    if match is None:
        _deny("runner_token_invalid", "invalid runner token")
    if match.revoked_at is not None:
        _deny("runner_token_revoked", "this job's runner token was revoked (the job is finished or was re-launched)")
    if match.expires_at <= _now():
        _deny("runner_token_expired", "this job's runner token expired (deadline + grace)")
    from jobs import states
    if not allow_finished and states.is_finished(job):
        _deny("job_finished", f"job {job_id} is finished ({job.status}); no further runner callbacks", 409)
    return RunnerAuth(job_id=job_id, token_id=match.id, account_id=job.account_id)


def touch(token_id: int, last_seq: int | None) -> None:
    with normalize.SessionLocal.begin() as s:
        row = s.get(JobToken, token_id, with_for_update=True)
        if row is None:
            return
        row.last_used_at = _now()
        if last_seq is not None and (row.last_seq is None or last_seq > row.last_seq):
            row.last_seq = last_seq


def expiry_for(deadline: datetime) -> datetime:
    return deadline + timedelta(minutes=int(settings.job_token_grace_minutes))
