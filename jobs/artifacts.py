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
    return {"name": r.name, "size": int(r.size), "sha256": r.sha256, "content_type": r.content_type,
            "backend": r.backend, "source": r.source, "created_at": r.created_at.isoformat(),
            "simulated": r.source == "simulated"}


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
        if existing is not None:
            if existing.sha256 == got:
                return {**meta(existing), "replayed": True}
            raise ArtifactConflict("artifact_exists", f"artifact {name} already exists with different content", 409)
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
        return meta(row)

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
    have = {a["name"] for a in store().list(job_id) if a["source"] == "runner"}
    missing = [n for n in required if n not in have]
    return not missing, missing
