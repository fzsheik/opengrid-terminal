"""Lambda Cloud: launch / inspect / terminate VMs. Spec: https://cloud.lambda.ai/api/v1/openapi.json

    GET  /instance-types                    data{type: {instance_type{price_cents_per_hour, specs{gpus}},
                                                         regions_with_capacity_available[{name}]}}
    POST /ssh-keys                          {name (<=64), public_key} -> data{id, name}
    POST /instance-operations/launch        {region_name, instance_type_name, ssh_key_names[1], name (<=64),
                                             tags[{key ^[a-z][a-z0-9-:]+$ <=55, value <=128}], user_data?}
                                            -> data{instance_ids[]}
    GET  /instances                         {data[Instance], page_token}; without page_size/page_token every
                                            instance is returned in one response
    GET  /instances/{id}                    data{id, name, status, ip, region{name}, instance_type{...}, tags}
    POST /instance-operations/terminate     {instance_ids[]} -> data{terminated_instances[]};
                                            404 global/object-does-not-exist for an unknown id
    errors {error{code, message, suggestion}}; 400 instance-operations/launch/insufficient-capacity,
           400 global/quota-exceeded, 400 global/invalid-parameters, 401 global/invalid-api-key, 429 rate-limited

Host: cloud.lambda.ai ("Production server"); cloud.lambdalabs.com is listed as "Secondary production
server (deprecated)" in the spec. No stop endpoint: Lambda offers terminate and restart only.
No billing API in the spec.
"""

from routing.adapters.base import (
    Adapter, Availability, Capabilities, InstanceState, Offer, TerminateResult, AdapterError, parse_time, pick_region,
)

BASE_URL = "https://cloud.lambda.ai/api/v1"
STATE = {"booting": "pending", "active": "running", "terminating": "terminating", "terminated": "terminated",
         "preempted": "terminated"}
ERROR_STATES = {"unhealthy"}
CAPACITY_CODE = "instance-operations/launch/insufficient-capacity"
L = "https://cloud.lambda.ai/api/v1/openapi.json"


def _code(body) -> str | None:
    err = body.get("error") if isinstance(body, dict) else None
    return err.get("code") if isinstance(err, dict) else None


class LambdaAdapter(Adapter):
    provider = "lambda"
    LEVEL = 3
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("ssh_key",)
    NAME_MAX = 64
    SSH_KEY_REGISTRATION = True
    CAPABILITIES = Capabilities(
        quote=("YES", f"GET /instance-types price_cents_per_hour / specs.gpus ({L})"),
        live_availability=("YES", "regions_with_capacity_available per instance type"),
        launch=("YES", f"POST /instance-operations/launch -> data.instance_ids ({L})"),
        ssh_key_injection=("YES", "ssh_key_names (exactly one); POST /ssh-keys registers one"),
        startup_script=("YES", "user_data (cloud-init, <=1MB)"),
        status=("YES", "booting/active/unhealthy/terminating/terminated/preempted"),
        stop=("NO", "terminate / restart only"),
        terminate=("YES", "POST /instance-operations/terminate -> terminated_instances"),
        region_selection=("YES", "region_name"),
        gpu_count_selection=("PARTIAL", "fixed per instance type (gpu_1x_*, gpu_8x_*)"),
        price_known_before_launch=("YES", "catalogue cents/hour"),
        billing_unit=("per minute", "'billed in one-minute increments' (docs.lambda.ai/public-cloud/billing/)"),
        minimum_commitment=("NO", "on-demand; credit card, $10 pre-authorization"),
        interruptible=("NO", "on-demand; 'preempted' status exists and maps to terminated"),
        name_tag_at_launch=("YES", "name (<=64) + tags [{key:'opengrid', value:og-<dep>}]"),
        list_instances=("YES", "GET /instances (all instances when not paginated)"),
        idempotency_token=("NO", "none documented"),
        stopped_billing=("n/a", "no stop"),
        find_by_name=("YES", "client-side exact match on name or tag over GET /instances"),
        reported_cost=("NO", "the Lambda Cloud API has no billing/usage endpoint"),
        error_semantics=("good", "error.code: instance-operations/launch/insufficient-capacity, global/quota-exceeded"),
        risks=["Launch rate limit (one request per ~12 s)", "Capacity flickers between check and launch",
               "Billing starts at first health check, OpenGrid meters from first observed running"],
    )

    def headers(self):
        return {**super().headers(), "Authorization": f"Bearer {self.credentials.get('api_key', '')}"}

    def is_capacity_error(self, status, body):
        return status == 400 and _code(body) == CAPACITY_CODE

    def classify(self, status, body, what):
        e = super().classify(status, body, what)
        if _code(body) == "global/quota-exceeded":
            e.kind = "quota"
        return e

    def check_availability(self, offer: Offer) -> Availability:
        data = (self.request("GET", "/instance-types") or {}).get("data") or {}
        entry = data.get(offer.sku)
        if not entry:
            return Availability(available=False, live=True, note=f"instance type {offer.sku} is no longer listed")
        it = entry.get("instance_type") or {}
        gpus = (it.get("specs") or {}).get("gpus") or offer.gpu_count
        cents = it.get("price_cents_per_hour")
        regions = [r.get("name") for r in entry.get("regions_with_capacity_available") or []]
        region = pick_region(self.provider, regions, offer.region, offer.want_region_group)
        return Availability(
            available=region is not None, live=True, region=region,
            list_price_per_gpu_hour=None if cents is None else cents / 100 / gpus,
            note="regions with capacity: " + (", ".join(regions) or "none"),
            metadata={"regions_with_capacity": regions},
        )

    def _provision(self, offer, availability, launch, name):
        region = availability.region or offer.region
        if not region:
            raise AdapterError("capacity", "lambda: no region with capacity for " + offer.sku, sent=False)
        key = launch.ssh_key
        if launch.ssh_public_key and not key:
            k = self.preflight(self.request, "POST", "/ssh-keys", json={"name": name, "public_key": launch.ssh_public_key})
            key = ((k or {}).get("data") or {}).get("name") if isinstance(k, dict) else None
            if not key:
                raise AdapterError("invalid", "lambda: ssh key registration returned no name", sent=False)
        body = {"region_name": region, "instance_type_name": offer.sku, "ssh_key_names": [key], "name": name,
                "tags": [{"key": "opengrid", "value": name}]}
        if launch.startup_script:
            body["user_data"] = launch.startup_script
        r = self.request("POST", "/instance-operations/launch", json=body)
        ids = ((r or {}).get("data") or {}).get("instance_ids") if isinstance(r, dict) else None
        if not isinstance(ids, list) or len(ids) != 1 or not ids[0]:
            return self.unknown(r, "lambda: launch did not return exactly one instance id")
        return self.accepted(ids[0], {"request": body, "response": r})

    def _state(self, d: dict) -> InstanceState:
        st = d.get("status")
        it = d.get("instance_type") or {}
        cents = it.get("price_cents_per_hour")
        err = st in ERROR_STATES
        labels = []
        for t in d.get("tags") or []:
            if isinstance(t, dict) and t.get("value"):
                labels += [str(t["value"]), f"{t.get('key')}={t['value']}"]
        return InstanceState(
            state="error" if err else STATE.get(st, "unknown"), instance_id=str(d.get("id")),
            name=d.get("name"), provider_status=st, region=(d.get("region") or {}).get("name"),
            gpu=it.get("gpu_description") or it.get("name"), gpu_count=(it.get("specs") or {}).get("gpus"),
            price_per_hour=None if cents is None else cents / 100, created_at=parse_time(d.get("first_healthy")),
            ip=d.get("ip") or None, labels=labels, error_kind="provider_error_state" if err else None,
            raw_redacted={"id": d.get("id"), "name": d.get("name"), "status": st, "hostname": d.get("hostname"),
                          "instance_type": it.get("name"), "first_healthy": d.get("first_healthy")})

    def _status(self, instance_id):
        d = (self.request("GET", f"/instances/{instance_id}") or {}).get("data")
        if not isinstance(d, dict) or not d:
            raise AdapterError("parse", "lambda: status returned no instance", sent=True)
        d.setdefault("id", instance_id)
        return self._state(d)

    def _terminate(self, instance_id):
        r = self.request("POST", "/instance-operations/terminate", json={"instance_ids": [instance_id]})
        done = ((r or {}).get("data") or {}).get("terminated_instances") if isinstance(r, dict) else None
        if not isinstance(done, list):
            return TerminateResult("unknown", "lambda: terminate answered without terminated_instances", 200)
        if not any(str(i.get("id")) == str(instance_id) for i in done if isinstance(i, dict)):
            return TerminateResult("unknown", "lambda: terminate answered but did not list this instance", 200,
                                   raw_redacted=r)
        return TerminateResult("accepted", "lambda: terminate accepted", 200, raw_redacted=r)

    def _list(self):
        out, token = [], None
        for _ in range(100):
            # No page_size: Lambda then returns EVERY instance in one response (pagination is opt-in, and
            # opted-in pages are newest-first, so an instance ending between page reads could be skipped).
            body = self.request("GET", "/instances", params={"page_token": token} if token else None)
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise AdapterError("parse", "lambda: list returned no data array", body=body, sent=True)
            out += [self._state(d) for d in body["data"] if isinstance(d, dict)]
            token = body.get("page_token")
            if not token:
                return out
        raise AdapterError("parse", "lambda: more than 100 pages of instances; refusing a partial list", sent=True)
