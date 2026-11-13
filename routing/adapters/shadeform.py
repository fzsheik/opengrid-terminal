"""Shadeform: one API key launches instances on many clouds; OpenGrid uses it for Crusoe, Denvr and
Latitude.sh, whose own APIs OpenGrid has no account with. Spec: https://api.shadeform.ai/v1/openapi.json
(linked from docs.shadeform.ai/llms.txt; the old docs.shadeform.ai/openapi.yaml is 404; fetched 2026-11-13)

    GET  /instances/types?cloud=&shade_instance_type=   instance_types[{hourly_price (cents), num_gpus,
                                                         availability[{region, available, rental_type}]}]  (public)
    POST /sshkeys/add           {name, public_key} -> {id}
    GET  /sshkeys               {ssh_keys: array | null}
    POST /sshkeys/{id}/delete   409 for the default key or a key in use
    POST /instances/create      {cloud, region, shade_instance_type, shade_cloud: true, name (<=50, [A-Za-z0-9_-]),
                                 tags[str], ssh_key_id, os?, launch_configuration?, envs[{name, value}] (<=20)}
                                 -> 200 {id, status}; 400 (plain text), 404 (ssh key/template/volume not on the
                                 account), 409 {error, error_code}, 500
    GET  /instances             {instances: array | null, total}  every NON-deleted instance (deleting ones
                                 included); unpaginated unless limit/offset is sent
    GET  /instances/{id}/info   {id, cloud, name, status, tags, hourly_price (cents), cost_estimate ("decimal
                                 dollar string"), configuration{num_gpus}, ip, region, created_at, deleted_at,
                                 active_at, ssh_user, ssh_port}
    POST /instances/{id}/delete 200; "Once ... 'deleting' ... no longer be billed"; 409 while `creating`
                                 ("An instance cannot be deleted while it is `creating`", concepts) -> retried
    statuses creating | pending_provider | pending | active | error | deleting | deleted
    auth header X-API-KEY

CREDENTIAL_PROVIDER = "shadeform": the crusoe/denvr/latitude adapters authenticate ONLY with a Shadeform
credential (audit P0-4: a customer's native Crusoe/Denvr/Latitude key must never be sent to Shadeform).
No capacity code is documented on create (capacity is never inferred from text); 409 stays ambiguous.
"""

import base64
from datetime import datetime

from providers.shadeform import BASE_URL as SHADEFORM_HOST
from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, CostReport, InstanceState, Offer, TerminateResult,
    parse_time, pick_region,
)
from routing.adapters.results import PER_DEPLOYMENT, ActionResult, ProviderKey, ProviderKeyRef

STATE = {"creating": "pending", "pending_provider": "pending", "pending": "pending", "active": "running",
         "deleting": "terminating", "deleted": "terminated"}
SF = "https://api.shadeform.ai/v1/openapi.json"
MAX_ENVS = 20


class ShadeformAdapter(Adapter):
    provider = "shadeform"          # replaced by the cloud name (crusoe, denvr, latitude) at build time
    LEVEL = 3
    BASE_URL = SHADEFORM_HOST + "/v1"
    CREDENTIAL_PROVIDER = "shadeform"
    CHECK_NEEDS_CREDENTIALS = False
    REQUIRED_LAUNCH = ("ssh_key",)   # without one Shadeform's managed key is used and the user cannot log in
    NAME_MAX = 50                    # create: "Alphanumeric characters, underscores and dashes, up to 50 characters"
    SSH_KEY_REGISTRATION = PER_DEPLOYMENT
    SSH_KEY_RESOURCE = True
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
        reported_cost=("PARTIAL", "cost_estimate ('The cost incurred so far through Shadeform, as a decimal dollar "
                                  "string') on /instances/{id}/info while the instance is not deleted"),
        error_semantics=("medium", "400 plain text, 404, 409 {error, error_code}, 500; no capacity code, no 429"),
        forces_account_ssh_key=("UNKNOWN", "ssh_key_id is 'The ID of the SSH Key'; the account has a default key used "
                                           "when none is given and the docs do not say whether it is also installed "
                                           "when one is (docs.shadeform.ai instances-create, fetched 2026-10-07)"),
        billing_starts=("UNKNOWN", "active_at is exposed; billing start not documented against it"),
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

    def missing_launch(self, launch, offer):
        m = super().missing_launch(launch, offer)
        if len(launch.env or {}) > MAX_ENVS:
            m.append(f"at most {MAX_ENVS} env variables (Shadeform envs limit)")
        return m

    def _provision(self, offer, availability, launch, name):
        if not availability.region:
            raise AdapterError("capacity", f"shadeform: no available region for {offer.listing_id}", sent=False)
        key = self.launch_key(launch, name, use="id")     # never omitted: Shadeform would use its default key
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
            price_per_hour=None if cents is None else float(cents) / 100, created_at=parse_time(d.get("created_at")), running_at=parse_time(d.get("active_at")),
            time_fields={k: v for k, v in (("created_at", "created_at"), ("running_at", "active_at"),
                                           ("ended_at", "deleted_at")) if d.get(v)},
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
        try:
            r = self.request("POST", f"/instances/{instance_id}/delete", allow_text=True)
        except AdapterError as exc:
            if exc.status_code == 409:
                # "An instance cannot be deleted while it is `creating`" (concepts): retried by reconciliation.
                return TerminateResult("failed", "shadeform: delete refused (409; the instance may still be "
                                                 f"creating; will be retried): {self.scrub(exc.message)[:160]}", 409,
                                       error_kind="still_creating", retryable=True)
            raise
        return TerminateResult("accepted", "shadeform: delete accepted (billing stops at 'deleting')", 200,
                               raw_redacted=r if isinstance(r, (dict, list)) else None)

    def _list(self):
        body = self.request("GET", "/instances")
        # instances is "array | null" (spec): null is an empty account. A missing key stays a parse error.
        if not isinstance(body, dict) or "instances" not in body or not isinstance(body["instances"], (list, type(None))):
            raise AdapterError("parse", "shadeform: list returned no instances array", body=body, sent=True)
        rows = [d for d in body["instances"] or [] if isinstance(d, dict)]
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
        return CostReport(amount, start, end, basis="shadeform cost_estimate (lifetime, decimal USD string)",
                          raw_redacted={"cost_estimate": v, "status": d.get("status")})

    # -- per-deployment SSH keys (shapes per the spec header, unvalidated) --

    def _register_ssh_key(self, name, public_key):
        r = self.request("POST", "/sshkeys/add", json={"name": name, "public_key": public_key})
        if not isinstance(r, dict) or not r.get("id"):
            raise AdapterError("parse", "shadeform: ssh key registration returned no id", sent=True)
        return ProviderKeyRef(key_id=str(r["id"]), name=name, fingerprint=None)

    def _delete_ssh_key(self, key_id):
        self.request("POST", f"/sshkeys/{key_id}/delete", allow_text=True)
        return ActionResult("accepted", "shadeform: ssh key deleted", 200)

    def _list_ssh_keys(self):
        body = self.request("GET", "/sshkeys")
        # ssh_keys is "array | null" (spec): null is no keys. A missing key stays a parse error.
        if not isinstance(body, dict) or "ssh_keys" not in body or not isinstance(body["ssh_keys"], (list, type(None))):
            raise AdapterError("parse", "shadeform: sshkeys list returned no ssh_keys array", sent=True)
        return [ProviderKey(key_id=str(k.get("id")), name=k.get("name"), fingerprint=None)
                for k in body["ssh_keys"] or [] if isinstance(k, dict) and k.get("id")]
