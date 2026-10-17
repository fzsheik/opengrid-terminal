"""Job artifacts: a provider-neutral ArtifactStore (methodology/jobs.md section "Artifacts").

    store() -> ArtifactStore        the configured backend (settings.job_artifact_store)
    ArtifactStore.put(job_id, name, data, sha256, content_type=None, source='runner') -> dict
                 .get(job_id, name) -> (meta dict, bytes) | None
                 .list(job_id) -> [meta dict]
                 .delete(job_id, name) -> bool
                 .health() -> {ok, backend, reason}

Backends
    postgres  (default) bytes in job_artifacts.bytes (bytea), on the app's Postgres volume; size-capped.
    s3        S3-compatible object storage (endpoint, bucket, region, keys from settings.job_artifact_s3_*). The
              app uploads server-side; the GPU never receives storage credentials. Enabled only when configured;
              built on a minimal client interface (put_object / get_object / delete_object) so it is unit-tested
              with a fake; the real client is boto3 when installed (not a dependency of this release).
Both backends keep the metadata row (name, size, sha256, content_type, backend, object_key) in job_artifacts.

Every put: the name matches ^[a-z0-9][a-z0-9._-]{0,63}$ (no paths, no '..'), the size is within
settings.job_artifact_max_mb, and the declared sha256 equals the sha256 of the received bytes. Same name +
same bytes again = idempotent; same name + different bytes = refused (ArtifactConflict). Reads are by job id
only; tenant checks happen in the API (a job's artifacts are visible to its account and the operator only).

Checkpoints (resumable training state, NOT results; methodology/jobs.md "Checkpoints and resume")
    name ckpt-e<NNNN>.pt (the epoch, 4 digits) -> kind 'checkpoint' in meta(); everything else is kind 'output'.
    Same sha256 verification; only source 'runner' may write one (a sandbox never fakes checkpoint bytes); one upload
    <= settings.job_checkpoint_max_mb. Latest wins: once a checkpoint is stored the job keeps only its newest
    settings.job_checkpoint_keep (by epoch) and deletes the rest; an upload older than every kept one is answered
    `superseded` (not stored). A checkpoint an unfinished job is pinned to (its spec.resume_from) is never pruned.
    Checkpoints still count toward MAX_ARTIFACTS_PER_JOB and the per-job byte quota; persisted() (the success gate)
    ignores them.
    latest_checkpoint(job_id, verify=False) -> dict | None     the relaunch interface: the newest stored checkpoint
    resolve_resume(account_id, {job_id, artifact?}, epochs=)   validate a resume_from reference -> the pinned dict
    resume_source(job_id) -> (meta, bytes)                     the pinned checkpoint, for job_id's runner download
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select

import normalize
from config import settings
from store.jobs import JobArtifact

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")  # \Z: `$` would accept a trailing newline
MAX_ARTIFACTS_PER_JOB = 32
# Total bytes per job = this x job_artifact_max_mb (32 x 64 MB = 2 GB per job otherwise). A real pytorch-mnist run
# stores ~5 MB (model.pt ~4.8 MB + three small JSON files).
MAX_ARTIFACT_BYTES_FACTOR = 4
CHECKPOINT_RE = re.compile(r"^ckpt-e([0-9]{4})\.pt\Z")
JOB_ID_RE = re.compile(r"^job-[0-9a-f]{8,32}\Z")


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ArtifactConflict(ArtifactError):
    pass


class ArtifactStoreUnavailable(ArtifactError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def max_bytes() -> int:
    return int(settings.job_artifact_max_mb) * 1024 * 1024


def checkpoint_max_bytes() -> int:
    return int(settings.job_checkpoint_max_mb) * 1024 * 1024


def checkpoint_keep() -> int:
    return max(1, min(4, int(settings.job_checkpoint_keep or 1)))


def checkpoint_epoch(name) -> int | None:
    m = CHECKPOINT_RE.match(name) if isinstance(name, str) else None
    return int(m.group(1)) if m else None


def is_checkpoint(name) -> bool:
    return checkpoint_epoch(name) is not None


def checkpoint_name(epoch: int) -> str:
    return f"ckpt-e{int(epoch):04d}.pt"


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or ".." in name:
        raise ArtifactError("artifact_name_invalid", "artifact names must match ^[a-z0-9][a-z0-9._-]{0,63}$ "
                                                     "(no paths, no '..')")
    return name


def check_sha(sha256: str | None) -> str:
    v = (sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", v):
        raise ArtifactError("artifact_sha256_required", "send the artifact's sha256 (64 hex chars) in the "
                                                        "X-Artifact-SHA256 header")
    return v


def meta(r: JobArtifact) -> dict:
    ep = checkpoint_epoch(r.name)
    out = {"name": r.name, "size": int(r.size), "sha256": r.sha256, "content_type": r.content_type,
           "backend": r.backend, "source": r.source, "created_at": r.created_at.isoformat(),
           "simulated": r.source == "simulated", "kind": "checkpoint" if ep is not None else "output"}
    if ep is not None:
        out["epoch"] = ep
    return out


def _pinned(job_id: str) -> set[str]:
    """Checkpoint names of job_id that an unfinished job will resume from: never pruned under it."""
    from store.jobs import Job
    with normalize.SessionLocal() as s:
        specs = list(s.scalars(select(Job.spec).where(Job.spec["resume_from"]["job_id"].astext == job_id,
                                                      Job.finished_at.is_(None))))
    return {str(((sp or {}).get("resume_from") or {}).get("artifact")) for sp in specs}


class ArtifactStore:
    backend = "abstract"

    # -- backend hooks --
    def _write(self, job_id: str, name: str, data: bytes) -> dict:   # -> {bytes|object_key}
        raise NotImplementedError

    def _read(self, row: JobArtifact) -> bytes:
        raise NotImplementedError

    def _remove(self, row: JobArtifact) -> None:
        raise NotImplementedError

    def health(self) -> dict:
        return {"ok": True, "backend": self.backend, "reason": None}

    # -- the contract --
    def put(self, job_id: str, name: str, data: bytes, sha256: str, *, content_type: str | None = None,
            source: str = "runner") -> dict:
        check_name(name)
        want = check_sha(sha256)
        ep = checkpoint_epoch(name)
        if ep is not None and source != "runner":
            raise ArtifactError("checkpoint_source_invalid", "checkpoints come only from the runner on the GPU")
        if ep is not None and len(data) > checkpoint_max_bytes():
            raise ArtifactError("checkpoint_too_large", f"checkpoint is {len(data)} bytes; the limit is "
                                                        f"{settings.job_checkpoint_max_mb} MB", 413)
        if len(data) > max_bytes():
            raise ArtifactError("artifact_too_large", f"artifact is {len(data)} bytes; the limit is "
                                                      f"{settings.job_artifact_max_mb} MB", 413)
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            raise ArtifactError("artifact_sha256_mismatch", "the received bytes do not match the declared sha256 "
                                                            "(corrupted or truncated upload); retry the upload")
        with normalize.SessionLocal() as s:
            existing = s.scalars(select(JobArtifact).where(JobArtifact.job_id == job_id,
                                                           JobArtifact.name == name)).first()
            count = len(list(s.scalars(select(JobArtifact.id).where(JobArtifact.job_id == job_id))))
            total = int(s.scalar(select(func.coalesce(func.sum(JobArtifact.size), 0))
                                 .where(JobArtifact.job_id == job_id)) or 0)
            ckpts = [] if ep is None else sorted(
                ((checkpoint_epoch(n), n, int(sz)) for n, sz in s.execute(
                    select(JobArtifact.name, JobArtifact.size).where(JobArtifact.job_id == job_id)) if is_checkpoint(n)),
                reverse=True)
        if existing is not None:
            if existing.sha256 == got:
                return {**meta(existing), "replayed": True}
            raise ArtifactConflict("artifact_exists", f"artifact {name} already exists with different content", 409)
        prune: list[tuple[str, int]] = []
        if ep is not None:
            keep = checkpoint_keep()
            if len(ckpts) >= keep and ep < ckpts[keep - 1][0]:
                # latest wins: a checkpoint older than every kept one (a late retry) is not stored
                return {"name": name, "size": len(data), "sha256": got, "kind": "checkpoint", "epoch": ep,
                        "source": source, "stored": False, "superseded": True}
            pinned = _pinned(job_id) if len(ckpts) >= keep else set()
            prune = [(n, sz) for _, n, sz in ckpts[keep - 1:] if n not in pinned]
            count -= len(prune)
            total -= sum(sz for _, sz in prune)
        if count >= MAX_ARTIFACTS_PER_JOB:
            raise ArtifactError("too_many_artifacts", f"at most {MAX_ARTIFACTS_PER_JOB} artifacts per job", 422)
        if total + len(data) > MAX_ARTIFACT_BYTES_FACTOR * max_bytes():
            raise ArtifactError("artifact_quota_exceeded", f"the job's artifacts would exceed "
                                                           f"{MAX_ARTIFACT_BYTES_FACTOR * settings.job_artifact_max_mb} MB "
                                                           "in total", 413)
        loc = self._write(job_id, name, data)
        row = JobArtifact(job_id=job_id, name=name, size=len(data), sha256=got,
                          content_type=(content_type or "application/octet-stream")[:128], backend=self.backend,
                          bytes=loc.get("bytes"), object_key=loc.get("object_key"), source=source,
                          created_at=_now())
        try:
            with normalize.SessionLocal.begin() as s:
                s.add(row)
        except Exception:
            # a concurrent identical upload won the unique (job_id, name): replay it if it is the same bytes
            with normalize.SessionLocal() as s:
                won = s.scalars(select(JobArtifact).where(JobArtifact.job_id == job_id,
                                                          JobArtifact.name == name)).first()
            if won is not None and won.sha256 == got:
                return {**meta(won), "replayed": True}
            raise
        out = meta(row)
        if prune:   # only after the new row exists: a job is never left without its newest checkpoint
            out["pruned"] = [n for n, _ in prune if self.delete(job_id, n)]
        return out

    def _row(self, job_id: str, name: str) -> JobArtifact | None:
        with normalize.SessionLocal() as s:
            return s.scalars(select(JobArtifact).where(JobArtifact.job_id == job_id, JobArtifact.name == name)).first()

    def get(self, job_id: str, name: str) -> tuple[dict, bytes] | None:
        check_name(name)
        r = self._row(job_id, name)
        if r is None:
            return None
        data = self._read(r) if r.backend == self.backend else backend_for(r.backend)._read(r)
        if hashlib.sha256(data).hexdigest() != r.sha256:
            raise ArtifactError("artifact_corrupt", f"stored artifact {name} no longer matches its sha256", 500)
        return meta(r), data

    def list(self, job_id: str) -> list[dict]:
        with normalize.SessionLocal() as s:
            return [meta(r) for r in s.scalars(select(JobArtifact).where(JobArtifact.job_id == job_id)
                                               .order_by(JobArtifact.name))]

    def delete(self, job_id: str, name: str) -> bool:
        r = self._row(job_id, name)
        if r is None:
            return False
        (self if r.backend == self.backend else backend_for(r.backend))._remove(r)
        with normalize.SessionLocal.begin() as s:
            row = s.get(JobArtifact, r.id)
            if row is not None:
                s.delete(row)
        return True


class PostgresStore(ArtifactStore):
    backend = "postgres"

    def _write(self, job_id, name, data):
        return {"bytes": bytes(data)}

    def _read(self, row):
        if row.bytes is None:
            with normalize.SessionLocal() as s:
                row = s.get(JobArtifact, row.id)
        return bytes(row.bytes or b"")

    def _remove(self, row):
        return None

    def health(self):
        try:
            from sqlalchemy import text
            with normalize.SessionLocal() as s:
                s.execute(text("SELECT 1 FROM job_artifacts LIMIT 1"))
            return {"ok": True, "backend": self.backend, "reason": None,
                    "max_mb": settings.job_artifact_max_mb}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": self.backend, "reason": f"job_artifacts unreadable: {type(exc).__name__}"}


class S3Store(ArtifactStore):
    """S3-compatible backend over a minimal client: put_object(Bucket, Key, Body, ContentType),
    get_object(Bucket, Key) -> {'Body': readable}, delete_object(Bucket, Key)."""
    backend = "s3"

    def __init__(self, client=None, *, bucket: str | None = None, prefix: str | None = None):
        self.bucket = bucket or settings.job_artifact_s3_bucket
        self.prefix = settings.job_artifact_s3_prefix if prefix is None else prefix
        self._client = client

    @staticmethod
    def configured() -> bool:
        return bool(settings.job_artifact_s3_bucket and settings.job_artifact_s3_access_key_id
                    and settings.job_artifact_s3_secret_access_key)

    def client(self):
        if self._client is not None:
            return self._client
        if not self.configured():
            raise ArtifactStoreUnavailable("artifact_storage_unavailable",
                                           "S3 artifact storage is selected but JOB_ARTIFACT_S3_BUCKET / keys are not "
                                           "configured", 503)
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError:
            raise ArtifactStoreUnavailable("artifact_storage_unavailable",
                                           "S3 artifact storage needs the boto3 package, which is not installed", 503)
        self._client = boto3.client("s3", endpoint_url=settings.job_artifact_s3_endpoint,
                                    region_name=settings.job_artifact_s3_region,
                                    aws_access_key_id=settings.job_artifact_s3_access_key_id,
                                    aws_secret_access_key=settings.job_artifact_s3_secret_access_key)
        return self._client

    def key(self, job_id: str, name: str) -> str:
        return f"{self.prefix}{job_id}/{check_name(name)}"

    def _write(self, job_id, name, data):
        k = self.key(job_id, name)
        try:
            self.client().put_object(Bucket=self.bucket, Key=k, Body=bytes(data), ContentType="application/octet-stream")
        except ArtifactError:
            raise
        except Exception as exc:  # noqa: BLE001 - never echo provider errors (may carry signed URLs)
            log.warning("s3 put %s failed: %s", k, type(exc).__name__)
            raise ArtifactStoreUnavailable("artifact_storage_unavailable",
                                           f"object storage refused the upload ({type(exc).__name__}); retry", 503)
        return {"object_key": k}

    def _read(self, row):
        try:
            body = self.client().get_object(Bucket=self.bucket, Key=row.object_key)["Body"]
            return body.read() if hasattr(body, "read") else bytes(body)
        except ArtifactError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ArtifactStoreUnavailable("artifact_storage_unavailable",
                                           f"object storage read failed ({type(exc).__name__})", 503)

    def _remove(self, row):
        self.client().delete_object(Bucket=self.bucket, Key=row.object_key)

    def health(self):
        if self._client is None and not self.configured():
            return {"ok": False, "backend": self.backend, "reason": "JOB_ARTIFACT_S3_* not configured"}
        try:
            self.client()
            return {"ok": True, "backend": self.backend, "reason": None, "bucket": self.bucket,
                    "max_mb": settings.job_artifact_max_mb}
        except ArtifactError as exc:
            return {"ok": False, "backend": self.backend, "reason": exc.message}


_OVERRIDE: dict[str, ArtifactStore] = {}   # tests: install a fake-backed store


def backend_for(name: str) -> ArtifactStore:
    if name in _OVERRIDE:
        return _OVERRIDE[name]
    if name == "s3":
        return S3Store()
    return PostgresStore()


def store() -> ArtifactStore:
    name = (settings.job_artifact_store or "postgres").strip().lower()
    if name not in ("postgres", "s3"):
        raise ArtifactStoreUnavailable("artifact_storage_unavailable",
                                       f"unknown JOB_ARTIFACT_STORE {name!r}; use postgres or s3", 503)
    return backend_for(name)


def health() -> dict:
    try:
        return store().health()
    except ArtifactError as exc:
        return {"ok": False, "backend": settings.job_artifact_store, "reason": exc.message}


def persisted(job_id: str, required) -> tuple[bool, list[str]]:
    """Every required artifact exists with a verified sha256 row. (ok, missing names)."""
    have = {a["name"] for a in store().list(job_id) if a["source"] == "runner" and a["kind"] != "checkpoint"}
    missing = [n for n in required if n not in have]
    return not missing, missing


# --------------------------------------------------------------------------
# Checkpoints / resume (the interface the relaunch feature builds on)
# --------------------------------------------------------------------------

def checkpoints(job_id: str) -> list[dict]:
    """The job's stored checkpoints, newest epoch first (every row was sha256-verified on upload)."""
    return sorted((a for a in store().list(job_id) if a["kind"] == "checkpoint" and a["source"] == "runner"),
                  key=lambda a: a["epoch"], reverse=True)


def latest_checkpoint(job_id: str, *, verify: bool = False) -> dict | None:
    """The newest resumable checkpoint of job_id -> {job_id, artifact, name, epoch, size, sha256, created_at} or None.
    verify=True re-reads the bytes and re-checks the sha256 (ArtifactError artifact_corrupt if they changed).
    A new job continues from it with spec.resume_from = {"job_id": job_id, "artifact": <artifact>}."""
    cs = checkpoints(job_id)
    if not cs:
        return None
    c = cs[0]
    if verify:
        store().get(job_id, c["name"])
    return {"job_id": job_id, "artifact": c["name"], "name": c["name"], "epoch": c["epoch"], "size": c["size"],
            "sha256": c["sha256"], "created_at": c["created_at"]}


def _job(job_id: str):
    if not JOB_ID_RE.match(job_id or ""):
        return None
    from store.jobs import Job
    with normalize.SessionLocal() as s:
        return s.get(Job, job_id)


def resolve_resume(account_id: int | None, ref: dict, *, epochs: int) -> dict:
    """Validate a resume_from reference for a new job of account_id -> the pinned {job_id, artifact, epoch, sha256,
    size}. The source must be the SAME account's job (otherwise resume_job_not_found: no cross-account oracle); the
    checkpoint must be stored from the runner, re-verify against its sha256, and leave epochs to train."""
    src = str((ref or {}).get("job_id") or "")
    job = _job(src)
    if job is None or job.account_id != account_id:
        raise ArtifactError("resume_job_not_found", f"resume_from.job_id {src[:40]!r}: no such job in this account", 404)
    name = (ref or {}).get("artifact")
    if name is None:
        latest = latest_checkpoint(src)
        if latest is None:
            raise ArtifactError("resume_checkpoint_not_found", f"job {src} has no stored checkpoint to resume from"
                                                               + (" (sandbox jobs store none)" if job.sandbox else ""))
        name = latest["artifact"]
    if not is_checkpoint(name):
        raise ArtifactError("resume_artifact_not_checkpoint", "resume_from.artifact must be a checkpoint "
                                                              "(ckpt-eNNNN.pt), not a result artifact")
    row = next((c for c in checkpoints(src) if c["name"] == name), None)
    if row is None:
        raise ArtifactError("resume_checkpoint_not_found", f"checkpoint {name} of job {src} is not stored (never "
                                                           "uploaded, or superseded by a newer one)")
    store().get(src, name)   # re-checks the stored bytes against the sha256 (artifact_corrupt otherwise)
    if row["epoch"] >= int(epochs):
        raise ArtifactError("resume_nothing_to_do", f"checkpoint {name} is after epoch {row['epoch']}; config.epochs "
                                                    f"{int(epochs)} leaves nothing to train (raise epochs)")
    return {"job_id": src, "artifact": name, "epoch": row["epoch"], "sha256": row["sha256"], "size": row["size"]}


def resume_source(job_id: str) -> tuple[dict, bytes]:
    """The checkpoint job_id was created to resume from (spec.resume_from, pinned at submission), re-checked now:
    same account, still stored, runner-sourced, same sha256. Raises ArtifactError."""
    job = _job(job_id)
    ref = ((job.spec or {}).get("resume_from") if job is not None else None) or None
    if not ref:
        raise ArtifactError("resume_not_requested", "this job was not created to resume from a checkpoint", 404)
    src = _job(str(ref.get("job_id") or ""))
    if src is None or src.account_id != job.account_id or not is_checkpoint(ref.get("artifact")):
        raise ArtifactError("resume_checkpoint_unavailable", "the checkpoint to resume from is not available", 404)
    got = store().get(src.job_id, ref["artifact"])
    if got is None or got[0]["source"] != "runner":
        raise ArtifactError("resume_checkpoint_unavailable", "the checkpoint to resume from is no longer stored", 404)
    if got[0]["sha256"] != ref.get("sha256"):
        raise ArtifactError("resume_checkpoint_changed", "the stored checkpoint differs from the one pinned at "
                                                         "submission", 422)
    return got
