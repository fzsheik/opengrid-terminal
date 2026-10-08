"""Shared helpers for tests/test_routing_benchmark.py and tests/test_chaos.py. TESTS ONLY.

    db(name)                       scratch database + schema, normalize.SessionLocal patched, accounts 7..399
    Sim                            an in-memory provider (synthetic syn_* / m_* names) built on the base Adapter
                                   wrappers, so status / terminate / list classification is production code
    LambdaSim                      a stateful fake of the Lambda Cloud API behind httpx.MockTransport, driven
                                   through the REAL LambdaAdapter, with programmable faults per call
    market(gpu, rows)              replace the current listings of `gpu` with crafted rows
    live(...) / flags(...)         execution mode + per-provider flags
    invariants(...)                the execution-safety invariants every chaos scenario must keep

Nothing here touches the network: every provider call lands in Sim or LambdaSim.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fixtures  # noqa: E402
import scratchdb  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import Principal  # noqa: E402
from config import settings  # noqa: E402
from routing import control, scoring  # noqa: E402
from routing.adapters.base import (  # noqa: E402
    CAPACITY, NOT_FOUND, SERVER, TIMEOUT, Adapter, AdapterError, Availability,
)
from routing.adapters.results import Capabilities, InstanceState, TerminateResult  # noqa: E402
from sqlalchemy import text  # noqa: E402
from tables import ComputeListingRow, ListingObservation  # noqa: E402

SCOPES = frozenset({"data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"})
PK = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f bench@test"  # valid ed25519 framing, test-only bytes


def now() -> datetime:
    return datetime.now(timezone.utc)


def key(account_id: int) -> Principal:
    return Principal(kind="api_key", account_id=account_id, key_id=account_id * 10, scopes=SCOPES)


_acct = [100]


def fresh_account() -> int:
    """A new account per scenario: cost guards (active deployments, monthly spend) never leak between them."""
    _acct[0] += 1
    return _acct[0]


def db(name: str):
    url = scratchdb.create(name)
    S = fixtures.session(url)
    normalize.SessionLocal = S
    from store.accounts import Account
    with S.begin() as s:
        for aid in range(7, 400):
            s.add(Account(id=aid, name=f"acct{aid}", status="active", plan="free", settings={}, is_operator=False))
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:  # noqa: BLE001
        pass
    return S


def drop(S, name: str) -> None:
    S.kw["bind"].dispose()
    scratchdb.drop(name)


def q(sql: str, **params):
    with normalize.SessionLocal() as s:
        return s.execute(text(sql), params).all()


def scalar(sql: str, **params):
    with normalize.SessionLocal() as s:
        return s.execute(text(sql), params).scalar()


# --------------------------------------------------------------------------
# Market
# --------------------------------------------------------------------------

def listing(provider, price, *, gpu, avail=True, count=1, region="us-east", country="US", market_type="on_demand",
            age=timedelta(0), tier=None, interruptible=None, lid=None, at=None):
    seen = (at or now()) - age
    return ComputeListingRow(
        provider=provider, listing_id=lid or f"{provider}:{count}x", sku=f"{provider}-{count}x", raw_gpu_name="RAW " + gpu,
        canonical_gpu_name=gpu, gpu_count=count, region=region, country=country,
        price_per_gpu_hour=None if price is None else Decimal(str(price)),
        price_per_instance_hour=None if price is None else Decimal(str(round(price * count, 6))), currency="USD",
        market_type=market_type, provider_tier=tier,
        interruptible=(market_type != "on_demand") if interruptible is None else interruptible,
        available=avail, capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None,
        observed_at=seen, first_seen_at=seen - timedelta(days=5))


def market(gpus, rows) -> None:
    """Replace every current listing of `gpus` with `rows` (ComputeListingRow)."""
    gpus = [gpus] if isinstance(gpus, str) else list(gpus)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM compute_listings WHERE canonical_gpu_name = ANY(:g) OR listing_id LIKE 'bench%'"),
                  {"g": gpus})
        s.add_all(rows)
    scoring.history.cache_clear()


def history(rows: list[ComputeListingRow], obs: dict) -> None:
    """Rows + change-only observations {listing_id: [(t, price, avail), ...]}."""
    with normalize.SessionLocal.begin() as s:
        s.add_all(rows)
        s.flush()
        for lid, pts in obs.items():
            prov = next(r.provider for r in rows if r.listing_id == lid)
            for t, price, avail in pts:
                s.add(ListingObservation(provider=prov, listing_id=lid, observed_at=t,
                                         price_per_gpu_hour=Decimal(str(price)),
                                         price_per_instance_hour=Decimal(str(price)), available=avail,
                                         capacity=None, capacity_unit=None))


# --------------------------------------------------------------------------
# Execution controls
# --------------------------------------------------------------------------

def mode(m: str, env: bool = True) -> None:
    settings.routing_live_provisioning = env
    control.set_mode(m, reason="test", by="test")


def flags(provider: str, *, validated=True, live=True, supervised=True, killed=False) -> None:
    from store.routing import ProviderExecutionFlags
    with normalize.SessionLocal.begin() as s:
        row = s.get(ProviderExecutionFlags, provider)
        if row is not None:
            s.delete(row)
    if validated:
        control.mark_validated(provider, "dep-bench", {"test": "synthetic"}, "test")
    control.set_provider_flags(provider, reason="test", by="test", supervised_enabled=supervised, live_enabled=live)
    if killed:
        control.kill_provider(provider, "bench: provider incident", "test")


# --------------------------------------------------------------------------
# Sim: in-memory provider on the base Adapter wrappers
# --------------------------------------------------------------------------

class Sim(Adapter):
    """CHECK[p]: ok | unavailable | error | crash | not_found. PROVISION[p]: ok | capacity | auth | timeout |
    server. PRICE[p]: the live list price per GPU-hour. INST[iid] = {state, name, provider}."""
    LEVEL = 3
    SUPPORTS_STOP = False
    CREDENTIALS = ()
    CHECK_NEEDS_CREDENTIALS = False
    SSH_KEY_REGISTRATION = True
    CAPABILITIES = Capabilities(billing_unit=("per second", "sim"), stopped_billing=("n/a", "sim"),
                                minimum_commitment=("NO", "sim"), forces_account_ssh_key=("NO", "test fake"))
    CHECK: dict = {}
    PRICE: dict = {}
    PROVISION: dict = {}
    INST: dict = {}
    CALLS: list = []
    LOCK = threading.Lock()

    @classmethod
    def reset(cls):
        cls.CHECK.clear()
        cls.PRICE.clear()
        cls.PROVISION.clear()
        cls.INST.clear()
        cls.CALLS.clear()

    @classmethod
    def calls(cls, op, provider=None):
        return [c for c in cls.CALLS if c[0] == op and (provider is None or c[1] == provider)]

    def check_availability(self, offer):
        Sim.CALLS.append(("check", self.provider, offer.listing_id))
        m = Sim.CHECK.get(self.provider, "ok")
        if m == "unavailable":
            return Availability(available=False, live=True, note="sold out on live check")
        if m == "error":
            raise AdapterError(SERVER, f"{self.provider}: HTTP 503 from the availability API", 503, sent=True)
        if m == "crash":
            raise RuntimeError("adapter bug while parsing availability")
        if m == "not_found":
            raise AdapterError(NOT_FOUND, f"{self.provider}: instance type no longer exists", 404, sent=True)
        return Availability(available=True, live=True, region=offer.region,
                            list_price_per_gpu_hour=Sim.PRICE.get(self.provider, offer.price_per_gpu_hour))

    def _provision(self, offer, availability, launch, name):
        Sim.CALLS.append(("provision", self.provider, name))
        m = Sim.PROVISION.get(self.provider, "ok")
        if m == "capacity":
            raise AdapterError(CAPACITY, f"{self.provider}: insufficient capacity", 409, sent=True)
        if m == "auth":
            raise AdapterError("auth", f"{self.provider}: bad key", 401, sent=True)
        if m == "timeout":
            raise AdapterError(TIMEOUT, f"{self.provider}: read timeout", sent=True)
        if m == "server":
            raise AdapterError(SERVER, f"{self.provider}: HTTP 502", 502, sent=True)
        with Sim.LOCK:
            iid = f"{self.provider}-i{len(Sim.INST) + 1}"
            Sim.INST[iid] = {"state": "pending", "name": name, "provider": self.provider}
        return self.accepted(iid, {"id": iid}, status_code=200)

    def _status(self, iid):
        Sim.CALLS.append(("status", self.provider, iid))
        v = Sim.INST.get(iid)
        if v is None or v["state"] == "gone":
            raise AdapterError(NOT_FOUND, "no such instance", 404, sent=True)
        return InstanceState(v["state"], instance_id=iid, name=v["name"], price_per_hour=None)

    def _terminate(self, iid):
        Sim.CALLS.append(("terminate", self.provider, iid))
        v = Sim.INST.get(iid)
        if v is None or v["state"] == "gone":
            raise AdapterError(NOT_FOUND, "gone", 404, sent=True)
        v["state"] = "terminated"
        return TerminateResult("accepted", "deleting", 200)

    def _list(self):
        Sim.CALLS.append(("list", self.provider))
        return [InstanceState(v["state"], instance_id=k, name=v["name"]) for k, v in Sim.INST.items()
                if v["provider"] == self.provider and v["state"] not in ("gone", "terminated")]


# --------------------------------------------------------------------------
# LambdaSim: the Lambda Cloud API behind httpx.MockTransport (the real LambdaAdapter talks to it)
# --------------------------------------------------------------------------

class WorkerKilled(BaseException):
    """The process dies mid-call (OOM kill / Railway restart): nothing after this line runs."""


SKU = "gpu_1x_h100_sxm5"


class LambdaSim:
    """Instances {id: {name, status, boots_left}}. Faults are queued per operation (FIFO, one per call), or
    set persistent in `always[op]`. Operations: types, ssh, launch, status, terminate, list.

    launch faults   timeout_before | timeout_after | reset_after | http500 | http500_after | http429 |
                    garbage_after | connect_error | capacity | kill_after
    status faults   http500 | http401 | timeout | garbage
    terminate       timeout | timeout_after | http401 | http500 | noop
    list            http500 | http401 | duplicate
    ssh             http500 | http422
    """

    def __init__(self, *, boot_polls: int = 0):
        self.instances: dict[str, dict] = {}
        self.log: list[tuple] = []
        self.queue: dict[str, list[str]] = {}
        self.always: dict[str, str] = {}
        self.boot_polls = boot_polls
        self.price_cents = 329
        self.capacity = True
        self.lock = threading.Lock()
        self.reported: list[tuple] = []      # (instance id, lambda status) every status/list answer gave
        self.delay = 0.0

    def fault(self, op: str, *names: str) -> None:
        self.queue.setdefault(op, []).extend(names)

    def _take(self, op: str) -> str | None:
        with self.lock:
            qd = self.queue.get(op) or []
            if qd:
                return qd.pop(0)
        return self.always.get(op)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def creates(self, name: str | None = None) -> list[dict]:
        return [b for (m, p, b) in self.log if p == "/api/v1/instance-operations/launch"
                and (name is None or (b or {}).get("name") == name)]

    def terminates(self, iid: str | None = None) -> list[dict]:
        return [b for (m, p, b) in self.log if p == "/api/v1/instance-operations/terminate"
                and (iid is None or iid in (b or {}).get("instance_ids", []))]

    def alive(self) -> dict[str, dict]:
        return {k: v for k, v in self.instances.items() if v["status"] != "terminated"}

    def _inst(self, iid: str) -> dict:
        v = self.instances[iid]
        return {"id": iid, "name": v["name"], "status": v["status"], "ip": "198.51.100.10",
                "region": {"name": "us-east-1"},
                "instance_type": {"name": SKU, "price_cents_per_hour": self.price_cents, "specs": {"gpus": 1}},
                "tags": [{"key": "opengrid", "value": v["name"]}]}

    def _create(self, body) -> str:
        with self.lock:
            iid = "lam-" + secrets.token_hex(4)
            self.instances[iid] = {"name": body.get("name"), "status": "booting", "boots_left": self.boot_polls}
        return iid

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        self.log.append((method, path, body))
        assert "test-lambda-key" in request.headers.get("authorization", ""), "only the pinned key is ever sent"
        if path == "/api/v1/instance-types":
            f = self._take("types")
            if f == "http500":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            regions = [{"name": "us-east-1"}] if self.capacity else []
            return httpx.Response(200, json={"data": {SKU: {"instance_type": {
                "name": SKU, "price_cents_per_hour": self.price_cents, "specs": {"gpus": 1}},
                "regions_with_capacity_available": regions}}})
        if path == "/api/v1/ssh-keys":
            f = self._take("ssh")
            if f == "http500":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            if f == "http422":
                return httpx.Response(400, json={"error": {"code": "global/invalid-parameters",
                                                           "message": "invalid public key"}})
            return httpx.Response(200, json={"data": {"id": "k-" + secrets.token_hex(3), "name": body["name"]}})
        if path == "/api/v1/instance-operations/launch":
            if self.delay:
                time.sleep(self.delay)
            f = self._take("launch")
            if f == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            if f == "timeout_before":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "http429":
                return httpx.Response(429, json={"error": {"code": "global/rate-limited", "message": "slow down"}})
            if f == "http500":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            if f == "capacity":
                return httpx.Response(400, json={"error": {
                    "code": "instance-operations/launch/insufficient-capacity", "message": "no capacity"}})
            iid = self._create(body)
            if f == "timeout_after":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "reset_after":
                raise httpx.RemoteProtocolError("peer closed connection", request=request)
            if f == "http500_after":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            if f == "garbage_after":
                return httpx.Response(200, text="<html>upstream proxy error</html>")
            if f == "kill_after":
                raise WorkerKilled("worker killed after the provider accepted the launch")
            return httpx.Response(200, json={"data": {"instance_ids": [iid]}})
        if path.startswith("/api/v1/instances/") and method == "GET":
            iid = path.rsplit("/", 1)[-1]
            f = self._take("status")
            if f == "http500":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            if f == "http401":
                return httpx.Response(401, json={"error": {"code": "global/invalid-api-key", "message": "expired"}})
            if f == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "garbage":
                return httpx.Response(200, text="not json")
            v = self.instances.get(iid)
            if v is None:
                return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist"}})
            with self.lock:
                if v["status"] == "booting":
                    if v["boots_left"] <= 0:
                        v["status"] = "active"
                    else:
                        v["boots_left"] -= 1
                elif v["status"] == "terminating":
                    v["status"] = "terminated"
            self.reported.append((iid, v["status"]))
            return httpx.Response(200, json={"data": self._inst(iid)})
        if path == "/api/v1/instance-operations/terminate":
            f = self._take("terminate")
            ids = (body or {}).get("instance_ids") or []
            if f == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "http401":
                return httpx.Response(401, json={"error": {"code": "global/invalid-api-key", "message": "expired"}})
            if f == "http500":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            missing = [i for i in ids if i not in self.instances]
            if missing:
                return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist"}})
            if f != "noop":
                with self.lock:
                    for i in ids:
                        if self.instances[i]["status"] != "terminated":
                            self.instances[i]["status"] = "terminating"
            if f == "timeout_after":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"data": {"terminated_instances": [self._inst(i) for i in ids]}})
        if path == "/api/v1/instances" and method == "GET":
            f = self._take("list")
            if f == "http500":
                return httpx.Response(500, json={"error": {"code": "global/unknown"}})
            if f == "http401":
                return httpx.Response(401, json={"error": {"code": "global/invalid-api-key", "message": "expired"}})
            with self.lock:
                for v in self.instances.values():   # a terminating instance finishes between reads
                    if v["status"] == "terminating":
                        v["status"] = "terminated"
                data = [self._inst(k) for k, v in self.instances.items() if v["status"] != "terminated"]
            if f == "duplicate":
                data = data + [dict(x) for x in data]
            for x in data:
                self.reported.append((x["id"], x["status"]))
            return httpx.Response(200, json={"data": data, "page_token": None})
        return httpx.Response(599, json={"error": f"unmocked {method} {path}"})


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------

def invariants(dep_ids, *, sim: LambdaSim | None = None, sim_inst: dict | None = None) -> dict:
    """Assert the execution-safety invariants for `dep_ids` (and every instance at the fake provider).

    1. provider create calls <= 1 per deployment intent (by og-<deployment> name)
    2. no instance alive at the provider without a live/uncertain deployment or an open orphan record
    3. usage slices never overlap and never extend past confirmed termination; one usage record per slice
    4. status 'running' only with evidence that the provider reported running
    5. 'terminated' only with provider evidence (status terminated / two signals / two not_found reads)
    Returns a small summary for the report."""
    from routing import deployments
    from routing.adapters.results import instance_name

    out = {"deployments": len(dep_ids), "creates": 0, "alive": 0, "slices": 0}
    for d in dep_ids:
        name = instance_name(d)
        if sim is not None:
            n = len(sim.creates(name))
            assert n <= 1, f"{d}: {n} provider create calls for one deployment intent"
            out["creates"] += n
        if sim_inst is not None:
            n = len([c for c in Sim.calls("provision") if c[2] == name])
            assert n <= 1, f"{d}: {n} provider create calls"
            out["creates"] += n
    alive = {}
    if sim is not None:
        alive = {k: v["name"] for k, v in sim.alive().items()}
    if sim_inst is not None:
        alive.update({k: v["name"] for k, v in sim_inst.items() if v["state"] not in ("gone", "terminated")})
    out["alive"] = len(alive)
    for iid, name in alive.items():
        st = q("SELECT status FROM deployments WHERE client_name = :n", n=name)
        orphan = scalar("SELECT count(*) FROM orphan_resources WHERE instance_id = :i AND status IN ('open','terminating')",
                        i=iid)
        ok = (st and st[0][0] in deployments.LIVE_STATES) or orphan
        assert ok, f"instance {iid} ({name}) is alive at the provider but OpenGrid has {st} and no orphan record"
    for d in dep_ids:
        rows = q("SELECT period_start, period_end, usage_record_id, billable_seconds FROM usage_slices "
                 "WHERE deployment_id = :d ORDER BY period_start", d=d)
        out["slices"] += len(rows)
        for a, b in zip(rows, rows[1:]):
            assert a[1] <= b[0], f"{d}: usage slices overlap {a[0]}..{a[1]} and {b[0]}..{b[1]}"
        term = q("SELECT status, terminated_at FROM deployments WHERE deployment_id = :d", d=d)[0]
        if term[0] == "terminated" and rows:
            assert rows[-1][1] <= term[1] + timedelta(seconds=1), f"{d}: billed past confirmed termination"
        dup = scalar("SELECT count(*) FROM (SELECT period_start FROM usage_records WHERE deployment_id = :d "
                     "GROUP BY period_start HAVING count(*) > 1) x", d=d)
        assert dup == 0, f"{d}: two usage records for one period"
        recs = scalar("SELECT count(*) FROM usage_records WHERE deployment_id = :d", d=d)
        billable = len([r for r in rows if r[3] > 0])
        assert recs <= billable or not rows, f"{d}: {recs} usage records for {billable} billable slices"
        for to, ev, actor in q("SELECT to_status, evidence, actor FROM deployment_events WHERE deployment_id = :d "
                               "AND to_status IN ('running', 'terminated') AND (from_status IS DISTINCT FROM to_status)",
                               d=d):
            ev = ev or {}
            if to == "running":
                assert ev.get("state") == "running", f"{d}: running without the provider reporting running: {ev}"
            else:
                basis = str(ev.get("basis") or "")
                assert ev.get("state") in ("terminated", "not_found") or "two signals" in basis \
                    or "consecutive not_found" in basis, f"{d}: terminated without provider evidence: {ev}"
                if ev.get("state") == "not_found":
                    assert "consecutive not_found" in basis or "two signals" in basis, ev
    return out
