"""Verda (formerly DataCrunch): VMs. Docs: https://api.verda.com/v1/docs

    POST /v1/oauth2/token                  {grant_type: client_credentials, client_id, client_secret}
                                           -> {access_token, token_type: Bearer, expires_in}
    GET  /v1/instance-availability         [{location_code, availabilities[instance_type]}]
    GET  /v1/instance-types                [{instance_type, price_per_hour, gpu{number_of_gpus}}]   (public)
    POST /v1/instances                     {instance_type, image, hostname, location_code, ssh_key_ids[],
                                            description, is_spot: false} -> 202, the instance id; 503 = no capacity
    GET  /v1/instances/{id}                {id, status, ip, price_per_hour, location_code, ...}
    PUT  /v1/instances                     {action: shutdown|delete, id}

Whether a shut-down instance still bills has not been verified; delete ends it.
"""

from providers.verda import BASE_URL
from routing.adapters.base import (
    AUTH, CAPACITY, FAILED, PROVISIONING, RUNNING, STOPPED, TERMINATED, TERMINATING, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer,
)

STATUS = {"running": RUNNING, "provisioning": PROVISIONING, "ordered": PROVISIONING, "new": PROVISIONING,
          "validating": PROVISIONING, "offline": STOPPED, "deleting": TERMINATING, "discontinued": TERMINATED,
          "notfound": TERMINATED, "error": FAILED, "no_capacity": FAILED, "installation_failed": FAILED,
          "unknown": UNKNOWN}


class VerdaAdapter(Adapter):
    provider = "verda"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = BASE_URL
    CREDENTIALS = ("client_id", "client_secret")
    REQUIRED_LAUNCH = ("ssh_key", "image")
    CAPACITY_SIGNALS = ("no capacity", "not available")

    _token: str | None = None

    def classify(self, status, body, what):
        if status == 503 and "/v1/instances" in what:
            return AdapterError(CAPACITY, f"verda: no capacity (503) for {what}", status, body)
        return super().classify(status, body, what)

    def token(self) -> str:
        if self._token is None:
            body = {"grant_type": "client_credentials", "client_id": self.credentials.get("client_id"),
                    "client_secret": self.credentials.get("client_secret")}
            r = super().request("POST", "/v1/oauth2/token", json=body) or {}
            if not r.get("access_token"):
                raise AdapterError(AUTH, "verda: token endpoint returned no access_token")
            self._token = r["access_token"]
        return self._token

    def request(self, method, path, **kw):
        if path.startswith("/v1/oauth2"):
            return super().request(method, path, **kw)
        headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {self.token()}"}
        return super().request(method, path, headers=headers, **kw)

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

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        if not availability.region:
            raise AdapterError(CAPACITY, f"verda: no location has {offer.sku}")
        body = {"instance_type": offer.sku, "image": launch.image, "hostname": name[:60],
                "location_code": availability.region, "ssh_key_ids": [launch.ssh_key],
                "description": "provisioned by OpenGrid", "is_spot": False}
        r = self.request("POST", "/v1/instances", json=body)
        iid = r if isinstance(r, str) else (r or {}).get("id") if isinstance(r, dict) else None
        if not iid:
            raise AdapterError("unknown_state", f"verda: deploy returned no instance id: {str(r)[:200]}")
        return Instance(instance_id=iid.strip('"'), status=PROVISIONING, provider_status="ordered",
                        region=availability.region, metadata={"launch_request": body})

    def status(self, instance_id: str) -> Instance:
        d = self.request("GET", f"/v1/instances/{instance_id}") or {}
        n = (d.get("gpu") or {}).get("number_of_gpus")
        price = d.get("price_per_hour")
        return Instance(instance_id=instance_id, status=STATUS.get(d.get("status"), UNKNOWN),
                        provider_status=d.get("status"), region=d.get("location"), ip=d.get("ip") or None,
                        price_per_gpu_hour=None if price is None or not n else float(price) / n,
                        metadata={"instance_type": d.get("instance_type")})

    def stop(self, instance_id: str) -> Instance:
        self.request("PUT", "/v1/instances", json={"action": "shutdown", "id": instance_id})
        return Instance(instance_id=instance_id, status=STOPPED, provider_status="offline")

    def terminate(self, instance_id: str) -> Instance:
        self.request("PUT", "/v1/instances", json={"action": "delete", "id": instance_id})
        return Instance(instance_id=instance_id, status=TERMINATING, provider_status="deleting")
