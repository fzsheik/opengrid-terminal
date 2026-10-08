"""Vast.ai: rent one host's ask (a docker instance). Docs: https://docs.vast.ai/api-reference

    GET    /api/v0/bundles/?q={...}        offers[{id, num_gpus, dph_total, geolocation, ...}]  (public search;
                                           dph_total includes storage for allocated_storage, default 8 GB)
    PUT    /api/v0/asks/{ask_id}/          {client_id: "me", image, disk, label, runtype, env, onstart?}
                                           -> 200 {success: true, new_contract: <instance id>}
                                           errors {success:false, error, msg}: 410 no_such_ask, 404 invalid_args
                                           "no_such_ask ...", 400 invalid_args / no_ssh_key_for_vm, 429
    GET    /api/v1/instances?limit=25&after_token=   {instances[...], next_token, total_instances}
    GET    /api/v0/instances/{id}/         {instances: {actual_status, label, dph_total, num_gpus, start_date, ...}}
                                           (an empty/null `instances` means the id is not on the account)
    PUT    /api/v0/instances/{id}/         {state: "stopped"}
    DELETE /api/v0/instances/{id}/         {success: true}; 404 "Instance not found"
    POST   /api/v0/instances/{id}/ssh/     {ssh_key}  attach a key to ONE instance (account-level keys would
                                           be added to every instance, so they are never used for customers)
    GET    /api/v0/charges?select_filters= {results[{source: "instance-<id>", amount, start, end}], next_token}
    auth   Authorization: Bearer <key>

OpenGrid's Vast listing is the MEDIAN ask across verified hosts. Routing searches live for the cheapest
verified, rentable ask of the exact GPU count, with allocated_storage = the disk the launch will request,
so the quote includes that storage. Bandwidth ($/GB, inet_up/down_cost) is NOT in the quote.
actual_status 'unknown' / 'offline': "will never reach running" (show-instance docs) -> degraded tracking.
Stopped instances keep billing for disk storage only ("Stopping an instance does not avoid storage
costs", docs.vast.ai billing) -> stopped_billing storage_only.
"""

import json
from datetime import datetime

from providers.vast import BASE_URL
from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, CostReport, InstanceState, Offer, TerminateResult, parse_time,
)
from routing.adapters.results import PER_DEPLOYMENT, ActionResult, ProviderKey, ProviderKeyRef

STATE = {"created": "pending", "loading": "pending", "scheduling": "pending", "running": "running",
         "exited": "stopped", "stopped": "stopped"}
ERROR_STATES = {"unknown", "offline"}
DISK_GB = 32
VA = "https://docs.vast.ai/api-reference/instances/create-instance"


class VastAdapter(Adapter):
    provider = "vast"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("image",)
    CHECK_NEEDS_CREDENTIALS = False
    NAME_MAX = 63
    SSH_KEY_REGISTRATION = PER_DEPLOYMENT   # attached per instance after create (no key object to delete)
    SSH_KEY_RESOURCE = False
    CAPABILITIES = Capabilities(
        quote=("YES", "dph_total of the chosen ask with allocated_storage = launch disk (search-offers docs)"),
        live_availability=("YES", "public bundles search, verified + rentable"),
        launch=("YES", f"PUT /api/v0/asks/{{id}}/ -> new_contract ({VA})"),
        ssh_key_injection=("PARTIAL", "POST /api/v0/instances/{id}/ssh/ after create; runtype ssh"),
        startup_script=("YES", "onstart (<=4048 chars)"),
        status=("YES", "actual_status; unknown/offline never reach running"),
        stop=("YES", "PUT /api/v0/instances/{id}/ {state: stopped}"),
        terminate=("YES", "DELETE /api/v0/instances/{id}/ {success}; confirmed by status/list"),
        region_selection=("PARTIAL", "by choosing an ask (geolocation)"),
        gpu_count_selection=("YES", "num_gpus per ask"),
        price_known_before_launch=("PARTIAL", "per ask; bandwidth billed extra per GB"),
        billing_unit=("per second", "'every second your instance is in the active/connected state' (billing docs)"),
        minimum_commitment=("NO", "prepaid credits"),
        interruptible=("NO (no bid sent)", "interruptible = bid price, not used"),
        name_tag_at_launch=("YES", "label = og-<dep>"),
        list_instances=("YES", "GET /api/v1/instances (limit 25, next_token)"),
        idempotency_token=("NO", "none"),
        stopped_billing=("storage_only", "'Stopping an instance does not avoid storage costs' (docs.vast.ai billing)"),
        find_by_name=("YES", "client-side exact match on label over the full list"),
        reported_cost=("YES", "GET /api/v0/charges source instance-<id> amount (day-granular filter)"),
        error_semantics=("good", "410 no_such_ask, 400 invalid_args, 429"),
        forces_account_ssh_key=("YES", "'Adding a key to your account keys only applies to new instances' - account "
                                       "keys are installed on every new instance (docs.vast.ai/instances/sshscp, "
                                       "fetched 2026-10-07)"),
        billing_starts=("UNKNOWN", "per-second billing; start event not tied to an API field"),
        risks=["Single unvetted hosts with variable reliability", "Bandwidth billed per GB, not in the quote",
               "Hosts can go offline: instance never reaches running"],
    )

    def headers(self):
        h = super().headers()
        if self.credentials.get("api_key"):
            h["Authorization"] = f"Bearer {self.credentials['api_key']}"
        return h

    def is_capacity_error(self, status, body):
        err = body.get("error") if isinstance(body, dict) else None
        msg = str(body.get("msg") or "") if isinstance(body, dict) else ""
        return (status == 410 or err == "no_such_ask" or (status == 404 and msg.startswith("no_such_ask")))

    def check_availability(self, offer: Offer) -> Availability:
        q = {"verified": {"eq": True}, "rentable": {"eq": True}, "type": "on-demand",
             "gpu_name": {"eq": offer.sku}, "num_gpus": {"eq": offer.gpu_count},
             "allocated_storage": DISK_GB, "order": [["dph_total", "asc"]], "limit": 20}
        offers = (self.request("GET", "/api/v0/bundles/", params={"q": json.dumps(q)}) or {}).get("offers") or []
        offers = [o for o in offers if o.get("dph_total") and o.get("num_gpus") == offer.gpu_count]
        if offer.want_region_group:
            try:
                from regions import region_group
                offers = [o for o in offers if region_group("vast", o.get("geolocation"), None) == offer.want_region_group]
            except ImportError:
                offers = []
        if not offers:
            return Availability(available=False, live=True, note="no verified rentable ask of this shape")
        best = offers[0]
        return Availability(available=True, live=True, region=best.get("geolocation"),
                            list_price_per_gpu_hour=float(best["dph_total"]) / best["num_gpus"],
                            note=f"cheapest of {len(offers)} matching asks (incl. {DISK_GB} GB storage; "
                                 f"bandwidth excluded)",
                            metadata={"ask_id": best.get("id"), "machine_id": best.get("machine_id"),
                                      "reliability": best.get("reliability"), "disk_gb": DISK_GB})

    def missing_launch(self, launch, offer):
        m = super().missing_launch(launch, offer)
        if launch.startup_script and len(launch.startup_script) > 4048:
            m.append("startup_script longer than 4048 characters (Vast onstart limit)")
        return m

    def _provision(self, offer, availability, launch, name):
        ask = availability.metadata.get("ask_id")
        if not ask:
            raise AdapterError("capacity", "vast: no ask id from the availability check", sent=False)
        body = {"client_id": "me", "image": launch.image, "disk": int(launch.disk_gb or DISK_GB),
                "label": name, "runtype": "ssh"}
        if launch.env:
            body["env"] = dict(launch.env)
        if launch.startup_script:
            body["onstart"] = launch.startup_script
        r = self.request("PUT", f"/api/v0/asks/{ask}/", json=body)
        if not isinstance(r, dict) or r.get("success") is not True or not r.get("new_contract"):
            return self.unknown(r, "vast: create answered 2xx without success/new_contract")
        iid = str(r["new_contract"])
        note = ""
        if launch.ssh_public_key:
            try:
                self.request("POST", f"/api/v0/instances/{iid}/ssh/", json={"ssh_key": launch.ssh_public_key})
            except AdapterError as exc:
                note = f"instance created but attaching the SSH key failed: {self.scrub(exc.message)[:160]}"
        return self.accepted(iid, {"request": body, "response": r, "ask_id": ask}, message=note)

    def _state(self, d: dict, instance_id=None) -> InstanceState:
        st = d.get("actual_status")
        price = d.get("dph_total")
        err = st in ERROR_STATES
        state = "error" if err else STATE.get(st, "pending" if st is None and d.get("intended_status") == "running"
                                                     else "unknown")
        return InstanceState(
            state=state, instance_id=str(d.get("id") or instance_id), name=d.get("label"), provider_status=st,
            region=d.get("geolocation"), gpu=d.get("gpu_name"), gpu_count=d.get("num_gpus"),
            price_per_hour=None if price is None else float(price), created_at=parse_time(d.get("start_date")),
            time_fields={"created_at": "start_date"} if d.get("start_date") else {},
            ip=d.get("public_ipaddr") or None, labels=[d["label"]] if d.get("label") else [],
            error_kind="provider_error_state" if err else None,
            raw_redacted={k: d.get(k) for k in ("id", "label", "actual_status", "intended_status", "cur_state",
                                                "dph_total", "start_date", "machine_id")})

    def _status(self, instance_id):
        body = self.request("GET", f"/api/v0/instances/{instance_id}/")
        d = body.get("instances") if isinstance(body, dict) else None
        if isinstance(d, list):
            d = d[0] if d else None
        if not d:
            # Vast answers 200 with no instance for an id that is not (or no longer) on the account.
            raise AdapterError("not_found", f"vast: instance {instance_id} not returned", 200, sent=True)
        return self._state(d, instance_id)

    def _stop(self, instance_id):
        r = self.request("PUT", f"/api/v0/instances/{instance_id}/", json={"state": "stopped"})
        if isinstance(r, dict) and r.get("success") is False:
            return TerminateResult("failed", f"vast: stop refused: {r.get('msg') or r.get('error')}",
                                   error_kind="provider_refused")
        return TerminateResult("accepted", "vast: stop accepted (GPU billing stops; storage still bills)")

    def _terminate(self, instance_id):
        r = self.request("DELETE", f"/api/v0/instances/{instance_id}/")
        if isinstance(r, dict) and r.get("success") is False:
            return TerminateResult("failed", f"vast: destroy refused: {r.get('msg') or r.get('error')}",
                                   error_kind="provider_refused", raw_redacted=r)
        if not isinstance(r, dict) or r.get("success") is not True:
            return TerminateResult("unknown", "vast: destroy answered without success:true", raw_redacted=r)
        return TerminateResult("accepted", "vast: destroy accepted (confirm with status/list)", raw_redacted=r)

    def _list(self):
        out, token = [], None
        for _ in range(400):
            params = {"limit": 25, **({"after_token": token} if token else {})}
            body = self.request("GET", "/api/v1/instances", params=params)
            if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
                raise AdapterError("parse", "vast: list returned no instances array", body=body, sent=True)
            out += [self._state(d) for d in body["instances"] if isinstance(d, dict)]
            token = body.get("next_token")
            if not token or not body["instances"]:
                total = body.get("total_instances")
                if isinstance(total, int) and total > len(out):
                    raise AdapterError("parse", f"vast: list incomplete ({len(out)} of {total})", sent=True)
                return out
        raise AdapterError("parse", "vast: too many pages of instances; refusing a partial list", sent=True)

    def reported_cost(self, instance_id: str, start: datetime | None, end: datetime | None) -> CostReport:
        if start is None or end is None:
            return CostReport(None, reason="vast charges need a start and end time")
        f = {"day": {"gte": int(start.timestamp()) - 86400, "lte": int(end.timestamp()) + 86400},
             "type": {"in": ["instance"]}}
        total, rows, token = 0.0, [], None
        try:
            for _ in range(20):
                params = {"select_filters": json.dumps(f), "limit": 500, **({"after_token": token} if token else {})}
                body = self.request("GET", "/api/v0/charges", params=params) or {}
                for r in body.get("results") or []:
                    if str(r.get("source")) == f"instance-{instance_id}":
                        rows.append(r)
                        total += float(r.get("amount") or 0)
                token = body.get("next_token")
                if not token:
                    break
            else:
                raise ValueError("too many pages of charges")
        except (AdapterError, ValueError, TypeError, AttributeError) as exc:
            return CostReport(None, start, end, reason="vast charges unavailable: "
                                                       f"{self.scrub(getattr(exc, 'message', str(exc)))[:160]}")
        if not rows:
            return CostReport(None, start, end, reason="vast charges list nothing for this instance yet")
        return CostReport(round(total, 6), start, end, basis="vast /api/v0/charges (day-granular; gpu+disk+bandwidth)",
                          raw_redacted=rows[:50])
