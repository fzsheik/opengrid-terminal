"""Shadeform: one API key launches instances on many clouds; OpenGrid uses it for Crusoe, Denvr and
Latitude.sh, whose own APIs OpenGrid has no account with. Spec: https://docs.shadeform.ai/openapi.yaml

    GET  /instances/types?cloud=&shade_instance_type=   instance_types[{hourly_price (cents), num_gpus,
                                                         availability[{region, available, rental_type}]}]  (public)
    POST /sshkeys/add           {name, public_key} -> {id}
    POST /instances/create      {cloud, region, shade_instance_type, shade_cloud: true, name, tags[str],
                                 ssh_key_id, os?, launch_configuration?} -> 200 {id}
    GET  /instances             {instances[...]}  every NON-deleted instance (deleting ones included)
    GET  /instances/{id}/info   {id, cloud, name, status, tags, hourly_price (cents), cost_estimate (string),
                                 configuration{num_gpus}, ip, region, created_at, deleted_at, active_at}
    POST /instances/{id}/delete 200; "Once ... 'deleting' ... no longer be billed"
    statuses creating | pending_provider | pending | active | error | deleting | deleted
    auth header X-API-KEY

CREDENTIAL_PROVIDER = "shadeform": the crusoe/denvr/latitude adapters authenticate ONLY with a Shadeform
credential (audit P0-4: a customer's native Crusoe/Denvr/Latitude key must never be sent to Shadeform).
The spec documents only 200 responses, so every non-2xx on create other than a clear 4xx rejection is
ambiguous, and no capacity code exists (capacity is never inferred from text).
"""

import base64
from datetime import datetime

from providers.shadeform import BASE_URL as SHADEFORM_HOST
from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, CostReport, InstanceState, Offer, TerminateResult,
    parse_time, pick_region,
)

STATE = {"creating": "pending", "pending_provider": "pending", "pending": "pending", "active": "running",
         "deleting": "terminating", "deleted": "terminated"}
SF = "https://docs.shadeform.ai/openapi.yaml"


class ShadeformAdapter(Adapter):
    provider = "shadeform"          # replaced by the cloud name (crusoe, denvr, latitude) at build time
    LEVEL = 3
    BASE_URL = SHADEFORM_HOST + "/v1"
    CREDENTIAL_PROVIDER = "shadeform"
    CHECK_NEEDS_CREDENTIALS = False
    REQUIRED_LAUNCH = ("ssh_key",)   # without one Shadeform's managed key is used and the user cannot log in
    SSH_KEY_REGISTRATION = True
    CAPABILITIES = Capabilities(
        quote=("YES", f"hourly_price in cents per instance ({SF})"),
        live_availability=("YES", "availability[{region, available, rental_type}] (public catalogue)"),
        launch=("YES", f"POST /instances/create -> {{id}} ({SF})"),
        ssh_key_injection=("YES", "ssh_key_id (POST /sshkeys/add registers one); required here"),
        startup_script=("YES", "launch_configuration{type: script, script_configuration{base64_script}}"),
        status=("YES", "GET /instances/{id}/info status enum"),
        stop=("NO", "no stop endpoint"),
        terminate=("YES", "POST /instances/{id}/delete; billing stops at 'deleting'"),
        region_selection=("YES", "region"),
        gpu_count_selection=("PARTIAL", "fixed per shade_instance_type"),
        price_known_before_launch=("YES", "catalogue; 'same rate as going direct' (getting-started/faq) UNVERIFIED"),
        billing_unit=("per second", "per-second metering (guides/spotinstances)"),
        minimum_commitment=("NO", "prepaid wallet must be topped up (getting-started/quickstart)"),
        interruptible=("NO (rental_type on_demand)", "spot rental_type exists, not used"),
        name_tag_at_launch=("YES", "name + tags ['opengrid', og-<dep>] ('searchable')"),
        list_instances=("YES", "GET /instances (non-deleted only; no filter); filtered to this cloud"),
        idempotency_token=("NO", "none documented"),
        stopped_billing=("n/a", "no stop"),
        find_by_name=("YES", "client-side exact match on name or tag over GET /instances"),
        reported_cost=("PARTIAL", "cost_estimate (string, 'cost incurred ... via Shadeform'; unit assumed USD, "
                                  "UNVERIFIED) on /instances/{id}/info while the instance is not deleted"),
        error_semantics=("poor", "spec documents only 200 responses; 5xx/402/429 semantics unknown"),
        risks=["Aggregator: Shadeform is the counterparty and bills the wallet",
               "Only non-deleted instances are listed: absence after delete is the confirmation signal",
               "Bare-metal types (Latitude) can take long to become active"],
    )

    def headers(self):
        h = super().headers()
        if self.credentials.get("api_key"):
            h["X-API-KEY"] = self.credentials["api_key"]
        return h

    def check_availability(self, offer: Offer) -> Availability:
        body = self.request("GET", "/instances/types",
                            params={"cloud": self.provider, "shade_instance_type": offer.listing_id}) or {}
        t = next((x for x in body.get("instance_types") or [] if x.get("shade_instance_type") == offer.listing_id), None)
        if t is None:
            return Availability(available=False, live=True, note=f"{offer.listing_id} no longer listed by Shadeform")
        up = [a.get("region") for a in t.get("availability") or []
              if a.get("available") and a.get("rental_type", "on_demand") == "on_demand"]
        region = pick_region(self.provider, up, None, offer.want_region_group)
        n = t.get("num_gpus") or offer.gpu_count
        cents = t.get("hourly_price")
        return Availability(available=region is not None, live=True, region=region,
                            list_price_per_gpu_hour=None if cents is None else cents / 100 / n,
                            note="available regions: " + (", ".join(up) or "none"), metadata={"regions": up})

    def _provision(self, offer, availability, launch, name):
        if not availability.region:
            raise AdapterError("capacity", f"shadeform: no available region for {offer.listing_id}", sent=False)
        key = launch.ssh_key
        if launch.ssh_public_key and not key:
            k = self.preflight(self.request, "POST", "/sshkeys/add", json={"name": name, "public_key": launch.ssh_public_key})
            key = (k or {}).get("id") if isinstance(k, dict) else None
            if not key:
                raise AdapterError("invalid", "shadeform: ssh key registration returned no id", sent=False)
        body = {"cloud": self.provider, "region": availability.region, "shade_instance_type": offer.listing_id,
                "shade_cloud": True, "name": name, "tags": ["opengrid", name], "ssh_key_id": key}
        if launch.image:
            body["os"] = launch.image
        if launch.startup_script:
            body["launch_configuration"] = {"type": "script", "script_configuration": {
                "base64_script": base64.b64encode(launch.startup_script.encode()).decode()}}
        if launch.env:
            body["envs"] = [{"name": k, "value": v} for k, v in launch.env.items()]
        r = self.request("POST", "/instances/create", json=body)
        if not isinstance(r, dict) or not r.get("id"):
            return self.unknown(r, "shadeform: create returned no id")
        return self.accepted(r["id"], {"request": body, "response": r})

    def _state(self, d: dict) -> InstanceState:
        st = d.get("status")
        n = (d.get("configuration") or {}).get("num_gpus")
        cents = d.get("hourly_price")
        err = st == "error"
        ended = parse_time(d.get("deleted_at")) if st in ("deleting", "deleted") else None
        return InstanceState(
            state="error" if err else STATE.get(st, "unknown"), instance_id=str(d.get("id")),
            name=d.get("name"), provider_status=st, region=d.get("region"),
            gpu=(d.get("configuration") or {}).get("gpu_type"), gpu_count=n,
            price_per_hour=None if cents is None else float(cents) / 100, created_at=parse_time(d.get("created_at")),
            ip=d.get("ip") or None, labels=[str(t) for t in d.get("tags") or []],
            error_kind="provider_error_state" if err else None, ended_at=ended,
            raw_redacted={k: d.get(k) for k in ("id", "cloud", "name", "status", "status_details", "region",
                                                "cost_estimate", "created_at", "active_at", "deleted_at", "tags")})

    def _status(self, instance_id):
        d = self.request("GET", f"/instances/{instance_id}/info")
        if not isinstance(d, dict) or not d:
            raise AdapterError("parse", "shadeform: info returned no instance", body=d, sent=True)
        d.setdefault("id", instance_id)
        return self._state(d)

    def _terminate(self, instance_id):
        r = self.request("POST", f"/instances/{instance_id}/delete", allow_text=True)
        return TerminateResult("accepted", "shadeform: delete accepted (billing stops at 'deleting')", 200,
                               raw_redacted=r if isinstance(r, (dict, list)) else None)

    def _list(self):
        body = self.request("GET", "/instances")
        if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
            raise AdapterError("parse", "shadeform: list returned no instances array", body=body, sent=True)
        rows = body["instances"]
        # One Shadeform account serves several clouds; each OpenGrid provider sees its own cloud.
        if self.provider != "shadeform":
            rows = [d for d in rows if d.get("cloud") == self.provider]
        return [self._state(d) for d in rows]

    def reported_cost(self, instance_id: str, start: datetime | None, end: datetime | None) -> CostReport:
        try:
            d = self.request("GET", f"/instances/{instance_id}/info") or {}
            v = d.get("cost_estimate")
            amount = None if v in (None, "") else float(v)
        except (AdapterError, ValueError, TypeError, AttributeError) as exc:
            return CostReport(None, start, end, reason="shadeform cost_estimate unreadable (deleted instances are not "
                                                       f"served): {self.scrub(getattr(exc, 'message', str(exc)))[:160]}")
        if amount is None:
            return CostReport(None, start, end, reason="shadeform returned no cost_estimate")
        return CostReport(amount, start, end, basis="shadeform cost_estimate (lifetime; unit assumed USD, UNVERIFIED)",
                          raw_redacted={"cost_estimate": v, "status": d.get("status")})
