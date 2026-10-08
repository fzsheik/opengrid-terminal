"""Lambda Cloud: launch / inspect / terminate VMs. Spec: https://cloud.lambda.ai/api/v1/openapi.json

    GET  /instance-types                    data{type: {instance_type{price_cents_per_hour, specs{gpus}},
                                                         regions_with_capacity_available[{name}]}}
    POST /ssh-keys                          {name (1-64), public_key (omit -> Lambda GENERATES a pair: never
                                            omitted here)} -> 200 data{id, name, public_key}
    GET  /ssh-keys                          data[SSHKey{id, name, public_key}]
    DELETE /ssh-keys/{id}                   200 data{} ; 404 when the id does not exist
    POST /instance-operations/launch        {region_name, instance_type_name, ssh_key_names[1], name (<=64),
                                             tags[{key ^[a-z][a-z0-9-:]+$ <=55, value <=128}], user_data?}
                                            -> data{instance_ids[]}
    GET  /instances                         {data[Instance], page_token}; without page_size/page_token every
                                            instance is returned in one response
    GET  /instances/{id}                    data{id, name, status, ip, region{name}, instance_type{...}, tags,
                                            ssh_key_names ("keys allowed to access the instance"),
                                            first_healthy ("When the instance first became healthy, or null
                                            if it never has")}  -- the ONLY lifecycle timestamp exposed: no
                                            created / terminated time in the spec
    POST /instance-operations/terminate     {instance_ids[]} -> data{terminated_instances[]};
                                            404 global/object-does-not-exist for an unknown id
    errors {error{code, message, suggestion}}; 400 instance-operations/launch/insufficient-capacity,
           400 global/quota-exceeded, 400 global/invalid-parameters, 401 global/invalid-api-key, 429 rate-limited

Host: cloud.lambda.ai ("Production server"); cloud.lambdalabs.com is listed as "Secondary production
server (deprecated)" in the spec. No stop endpoint: Lambda offers terminate and restart only.
No billing API in the spec.

Billing (docs.lambda.ai/public-cloud/billing/, fetched 2026-10-07): "Billing begins the moment you launch an
instance and the instance passes health checks, and ends the moment you terminate the instance";
"bills in one-minute increments". So billable_start = first_healthy (provider_running_at); the end is not
exposed by the API, so billable_end = the first confirmed-gone observation (flagged as an estimate).

SSH keys: the launch sends exactly the ONE key the core passed (ssh_key_names "Currently, exactly one SSH
key must be specified"); the instance reports ssh_key_names, which the tracker checks against it. A customer
public key is registered as og-<deployment> (POST /ssh-keys) and deleted after confirmed termination
(routing/adapters/resources.py).
"""

from routing.adapters.base import (
    Adapter, Availability, Capabilities, InstanceState, Offer, TerminateResult, AdapterError, parse_time, pick_region,
)
from routing.adapters.results import PER_DEPLOYMENT, ActionResult, ProviderKey, ProviderKeyRef

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
    SSH_KEY_REGISTRATION = PER_DEPLOYMENT
    SSH_KEY_RESOURCE = True
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
        forces_account_ssh_key=("NO", f"launch ssh_key_names: 'Currently, exactly one SSH key must be specified' and "
                                      f"the Instance reports ssh_key_names ('The names of the SSH keys that are allowed "
                                      f"to access the instance'); no account/default key injection is documented "
                                      f"({L}, fetched 2026-10-07). Runtime check: the tracker alerts if the instance "
                                      f"lists any key other than the one launched with"),
        billing_starts=("running", "'Billing begins the moment you launch an instance and the instance passes health "
                                   "checks' (docs.lambda.ai/public-cloud/billing/); first_healthy is that moment"),
        risks=["Launch rate limit (one request per ~12 s)", "Capacity flickers between check and launch",
               "Billing starts at first health check: OpenGrid meters from first_healthy (provider_running_at)",
               "No terminated timestamp in the API: billable_end is the first confirmed-gone observation (estimate)"],
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
        # Exactly the key the core passed (a reference) or the per-deployment key registered now. Never an
        # account default key.
        key = self.launch_key(launch, name, use="name")
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
            price_per_hour=None if cents is None else cents / 100,
            running_at=parse_time(d.get("first_healthy")),
            time_fields={"running_at": "first_healthy"} if d.get("first_healthy") else {},
            ssh_key_names=list(d["ssh_key_names"]) if isinstance(d.get("ssh_key_names"), list) else None,
            ip=d.get("ip") or None, labels=labels, error_kind="provider_error_state" if err else None,
            raw_redacted={"id": d.get("id"), "name": d.get("name"), "status": st, "hostname": d.get("hostname"),
                          "instance_type": it.get("name"), "first_healthy": d.get("first_healthy"),
                          "ssh_key_names": d.get("ssh_key_names")})

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

    # -- per-deployment SSH keys ------------------------------------------------

    def _register_ssh_key(self, name, public_key):
        if not public_key:
            # Omitting public_key makes Lambda GENERATE a key pair and return the private key: never do that.
            raise AdapterError("invalid", "lambda: no public key to register", sent=False)
        r = self.request("POST", "/ssh-keys", json={"name": name, "public_key": public_key})
        d = (r or {}).get("data") if isinstance(r, dict) else None
        if not isinstance(d, dict) or not d.get("name"):
            raise AdapterError("parse", "lambda: ssh key registration returned no key", sent=True)
        from routing.adapters.resources import fingerprint
        return ProviderKeyRef(key_id=str(d.get("id") or ""), name=str(d["name"]),
                              fingerprint=fingerprint(d.get("public_key") or public_key))

    def _delete_ssh_key(self, key_id):
        self.request("DELETE", f"/ssh-keys/{key_id}")
        return ActionResult("accepted", "lambda: ssh key deleted", 200)

    def _list_ssh_keys(self):
        from routing.adapters.resources import fingerprint
        body = self.request("GET", "/ssh-keys")
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise AdapterError("parse", "lambda: ssh-keys returned no data array", sent=True)
        return [ProviderKey(key_id=str(k.get("id")), name=k.get("name"), fingerprint=fingerprint(k.get("public_key")))
                for k in body["data"] if isinstance(k, dict) and k.get("id")]
