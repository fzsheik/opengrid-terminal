"""Verda (formerly DataCrunch): VMs. Spec: https://api.verda.com/v1/openapi.json (docs https://api.verda.com/v1/docs)

    POST /v1/oauth2/token                  {grant_type: client_credentials, client_id, client_secret}
                                           -> {access_token, token_type: Bearer, expires_in}
    GET  /v1/instance-availability         [{location_code, availabilities[instance_type]}]
    GET  /v1/instance-types                [{instance_type, price_per_hour, gpu{number_of_gpus}}]   (public)
    POST /v1/ssh-keys                      {name, key} -> 201 "<uuid>"
    POST /v1/instances                     {instance_type, image, hostname, location_code, ssh_key_ids[],
                                            description, is_spot: false, tags[{key, value}] (<=10)}
                                           -> 202 "<uuid>" (a plain JSON string)
    GET  /v1/instances?status=&tag=k=v     [Instance{id, hostname, status, location, price_per_hour, created_at,
                                            tags[{id, key, value}], os_volume_id, volume_ids, gpu{number_of_gpus}}]
    GET  /v1/instances/{id}                Instance
    PUT  /v1/instances                     {action: delete|shutdown|hibernate, id, volume_ids?, delete_permanently?}
                                           -> 202 [{instanceId, action, status: success|error, error?}]
                                              207 partial, 204 already in that state, 404 not found
    errors {code, message}: 400 invalid_request, 401 unauthorized_request, 402 insufficient_funds,
           403 forbidden_action, 404 not_found, 429 rate_limit_exceeded, 500 server_error,
           503 service_unavailable = "No capacity available" (documented on POST /v1/instances)

Delete passes the OS volume and every attached volume: "If not providing a volume_ids array, only the OS
volume will be deleted and the rest detached" (spec) and detached storage keeps charging (docs.verda.com,
shutdown-hibernate-and-delete). Stop is NOT offered: "Using shutdown will keep charging your account" (spec).
The spec marks a user-agent header as required; the base adapter sends one.
"""

from providers.verda import BASE_URL
from routing.adapters.base import (
    AUTH, AdapterError, Adapter, Availability, Capabilities, InstanceState, Offer, TerminateResult, parse_time,
)

STATE = {"running": "running", "provisioning": "pending", "ordered": "pending", "new": "pending",
         "validating": "pending", "offline": "stopped", "deleting": "terminating", "discontinued": "terminated",
         "notfound": "not_found"}
ERROR_STATES = {"error", "no_capacity", "installation_failed"}
VE = "https://api.verda.com/v1/openapi.json"


class VerdaAdapter(Adapter):
    provider = "verda"
    LEVEL = 3
    SUPPORTS_STOP = False
    BASE_URL = BASE_URL
    CREDENTIALS = ("client_id", "client_secret")
    REQUIRED_LAUNCH = ("ssh_key", "image")
    NAME_MAX = 60
    SSH_KEY_REGISTRATION = True
    CAPABILITIES = Capabilities(
        quote=("YES", f"GET /v1/instance-types price_per_hour (whole instance) ({VE})"),
        live_availability=("YES", "GET /v1/instance-availability (authenticated)"),
        launch=("YES", f"POST /v1/instances -> 202 id string ({VE})"),
        ssh_key_injection=("YES", "ssh_key_ids (UUIDs); POST /v1/ssh-keys registers one"),
        startup_script=("NO (not plumbed)", "startup_script_id needs a pre-registered /v1/scripts entry"),
        status=("YES", "13-value status enum; error/no_capacity/installation_failed tracked as degraded"),
        stop=("NO (disabled)", "spec: 'Using shutdown will keep charging your account'"),
        terminate=("YES", "PUT /v1/instances action delete with os_volume_id + volume_ids, delete_permanently"),
        region_selection=("YES", "location_code"),
        gpu_count_selection=("PARTIAL", "fixed per instance type"),
        price_known_before_launch=("YES", "FIXED_PRICE contracts only"),
        billing_unit=("10 min prepaid, unused refunded", "docs.verda.com/welcome-to-verda/pricing-and-billing/"),
        minimum_commitment=("NO", "prepaid balance; at zero balance instances are discontinued, volumes deleted"),
        interruptible=("NO (is_spot false)", "is_spot / contract SPOT exists"),
        name_tag_at_launch=("YES", "hostname + tags[{key:'opengrid', value:og-<dep>}] (<=10 tags)"),
        list_instances=("YES", "GET /v1/instances?tag=opengrid=<name> (plain array)"),
        idempotency_token=("NO", "none documented"),
        stopped_billing=("full", "spec: shutdown keeps charging; delete stops it"),
        find_by_name=("YES", "tag filter opengrid=<og-name>, exact match on hostname or tag value"),
        reported_cost=("NO", "no per-instance cost endpoint (/v1/balance and /v1/journal carry no per-instance cost)"),
        error_semantics=("good", "{code,message}; 503 service_unavailable documented as no capacity"),
        risks=["OAuth client credentials (token refreshed once on 401)",
               "Detached volumes keep billing: delete passes every volume id",
               "At zero balance instances are discontinued and volumes deleted"],
    )

    _token: str | None = None

    def is_capacity_error(self, status, body):
        # Documented: 503 {"code": "service_unavailable", "message": "No capacity available"} on create.
        # An edge/CDN 503 without that JSON stays ambiguous.
        return status == 503 and isinstance(body, dict) and body.get("code") == "service_unavailable"

    def classify(self, status, body, what):
        e = super().classify(status, body, what)
        if e.kind == "capacity" and not what.startswith("POST /v1/instances"):
            e.kind = "server"     # a 503 on any other call is an outage, not a capacity answer
        return e

    def token(self) -> str:
        if self._token is None:
            body = {"grant_type": "client_credentials", "client_id": self.credentials.get("client_id"),
                    "client_secret": self.credentials.get("client_secret")}
            r = super().request("POST", "/v1/oauth2/token", json=body) or {}
            if not isinstance(r, dict) or not r.get("access_token"):
                raise AdapterError(AUTH, "verda: token endpoint returned no access_token", sent=False)
            self._token = r["access_token"]
        return self._token

    def _secrets(self):
        return super()._secrets() + ((self._token,) if self._token else ())

    def request(self, method, path, **kw):
        if path.startswith("/v1/oauth2"):
            return super().request(method, path, **kw)
        headers = kw.pop("headers", {})
        try:
            return super().request(method, path, headers={**headers, "Authorization": f"Bearer {self.token()}"}, **kw)
        except AdapterError as exc:
            if exc.status_code != 401 or self._token is None or method != "GET":
                raise                     # never re-send a mutating call (create / delete)
            self._token = None            # expired token: refresh once, reads only
            return super().request(method, path, headers={**headers, "Authorization": f"Bearer {self.token()}"}, **kw)

    def check_availability(self, offer: Offer) -> Availability:
        rows = self.request("GET", "/v1/instance-availability") or []
        locations = [r.get("location_code") for r in rows if offer.sku in (r.get("availabilities") or [])]
        region = None
        if locations:
            region = locations[0]
            if offer.want_region_group:
                try:
                    from regions import region_group
                    inside = [l for l in locations if region_group("verda", l, None) == offer.want_region_group]
                except ImportError:
                    inside = []
                region = inside[0] if inside else None
        price = None
        types = self.request("GET", "/v1/instance-types") or []
        for t in types if isinstance(types, list) else []:
            if t.get("instance_type") == offer.sku and t.get("price_per_hour") is not None:
                n = (t.get("gpu") or {}).get("number_of_gpus") or offer.gpu_count
                price = float(t["price_per_hour"]) / n
        return Availability(available=region is not None, live=True, region=region, list_price_per_gpu_hour=price,
                            note="locations with stock: " + (", ".join(locations) or "none"),
                            metadata={"locations": locations})

    def missing_launch(self, launch, offer):
        m = super().missing_launch(launch, offer)
        if launch.startup_script:
            m.append("startup_script is not supported by this adapter (remove it)")
        return m

    def _provision(self, offer, availability, launch, name):
        if not availability.region:
            raise AdapterError("capacity", f"verda: no location has {offer.sku}", sent=False)
        self.preflight(self.token)
        key = launch.ssh_key
        if launch.ssh_public_key and not key:
            k = self.preflight(self.request, "POST", "/v1/ssh-keys", json={"name": name, "key": launch.ssh_public_key},
                               allow_text=True)
            key = k.strip().strip('"') if isinstance(k, str) else (k or {}).get("id") if isinstance(k, dict) else None
            if not key:
                raise AdapterError("invalid", "verda: ssh key registration returned no id", sent=False)
        body = {"instance_type": offer.sku, "image": launch.image, "hostname": name,
                "location_code": availability.region, "ssh_key_ids": [key],
                "description": f"OpenGrid {name}", "is_spot": False,
                "tags": [{"key": "opengrid", "value": name}]}
        r = self.request("POST", "/v1/instances", json=body, allow_text=True)
        iid = r.strip().strip('"') if isinstance(r, str) else (r.get("id") if isinstance(r, dict) else None)
        if not iid or " " in str(iid) or len(str(iid)) > 128:
            return self.unknown(r, "verda: deploy returned no usable instance id")
        return self.accepted(iid, {"request": body, "response": r})

    def _state(self, d: dict) -> InstanceState:
        st = d.get("status")
        tags = d.get("tags") or []
        labels = []
        for t in tags if isinstance(tags, list) else []:
            if isinstance(t, dict):
                labels += [str(t.get("value"))] if t.get("value") else []
                labels.append(f"{t.get('key')}={t.get('value')}" if t.get("value") else str(t.get("key")))
        err = st in ERROR_STATES
        price = d.get("price_per_hour")
        return InstanceState(
            state="error" if err else STATE.get(st, "unknown"), instance_id=str(d.get("id")),
            name=d.get("hostname"), provider_status=st, region=d.get("location"),
            gpu=d.get("instance_type"), gpu_count=(d.get("gpu") or {}).get("number_of_gpus"),
            price_per_hour=None if price is None else float(price), created_at=parse_time(d.get("created_at")),
            ip=d.get("ip") or None, labels=labels, error_kind="provider_error_state" if err else None,
            raw_redacted={k: d.get(k) for k in ("id", "hostname", "status", "location", "instance_type",
                                                "price_per_hour", "created_at", "os_volume_id", "volume_ids")})

    def _status(self, instance_id):
        d = self.request("GET", f"/v1/instances/{instance_id}")
        if not isinstance(d, dict) or not d:
            raise AdapterError("parse", "verda: status returned no instance", body=d, sent=True)
        d.setdefault("id", instance_id)
        return self._state(d)

    def _terminate(self, instance_id):
        volumes, note = [], ""
        try:
            d = self.request("GET", f"/v1/instances/{instance_id}") or {}
            volumes = [v for v in [d.get("os_volume_id"), *(d.get("volume_ids") or [])] if v]
            volumes = list(dict.fromkeys(volumes))
        except AdapterError as exc:
            if exc.kind == "not_found":
                return TerminateResult("already_gone", "verda: instance not found", 404, error_kind="not_found")
            # GPU billing matters more than storage: delete anyway, and say volumes may remain.
            note = " (volume ids unreadable: attached data volumes may remain and keep billing; check)"
        body = {"action": "delete", "id": instance_id, "delete_permanently": True}
        if volumes:
            body["volume_ids"] = volumes
        r = self.request("PUT", "/v1/instances", json=body, allow_text=True)
        items = r if isinstance(r, list) else []
        mine = [i for i in items if isinstance(i, dict) and str(i.get("instanceId")) == str(instance_id)] or items
        errs = [i for i in mine if isinstance(i, dict) and i.get("status") == "error"]
        if errs:
            e = errs[0]
            if str(e.get("statusCode")) == "404":
                return TerminateResult("already_gone", f"verda: {e.get('error')}", 404, error_kind="not_found")
            return TerminateResult("failed", f"verda: delete refused: {e.get('error')}", error_kind="provider_refused",
                                   raw_redacted=items)
        return TerminateResult("accepted", "verda: delete accepted" + note, raw_redacted={"request": body, "response": r})

    def _list_q(self, params):
        rows = self.request("GET", "/v1/instances", params=params)
        if not isinstance(rows, list):
            raise AdapterError("parse", "verda: list returned no array", body=rows, sent=True)
        return [self._state(d) for d in rows if isinstance(d, dict)]

    def _list(self):
        return self._list_q(None)

    def _find(self, name):
        return self._list_q({"tag": f"opengrid={name}"})
