"""Vast.ai: rent one host's ask (a docker instance). Docs: https://docs.vast.ai/api-reference

    GET    /api/v0/bundles/?q={...}        offers[{id, num_gpus, dph_total, geolocation, ...}]  (public search)
    PUT    /api/v0/asks/{ask_id}/          {client_id: "me", image, disk, label, runtype, env}
                                           -> {success: true, new_contract: <instance id>}
    GET    /api/v0/instances/{id}/         {instances: {actual_status, dph_total, num_gpus, public_ipaddr}}
    PUT    /api/v0/instances/{id}/         {state: "stopped"}
    DELETE /api/v0/instances/{id}/         {success: true}
    auth   Authorization: Bearer <key>; 404/410 on create = the ask is gone

OpenGrid's Vast listing is the MEDIAN ask across verified hosts (an aggregate, not a
machine). Routing therefore searches live for the cheapest verified, rentable ask of the
exact GPU count, and the quote is THAT ask's price, which can differ from the median.
"""

import json

from providers.vast import BASE_URL
from routing.adapters.base import (
    CAPACITY, PROVISIONING, RUNNING, STOPPED, TERMINATED, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer,
)

STATUS = {"created": PROVISIONING, "loading": PROVISIONING, "running": RUNNING,
          "exited": STOPPED, "stopped": STOPPED, "offline": UNKNOWN}


class VastAdapter(Adapter):
    provider = "vast"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("image",)
    CHECK_NEEDS_CREDENTIALS = False
    CAPACITY_SIGNALS = ("no_such_ask", "not available", "offer unavailable", "already rented")

    def headers(self):
        h = super().headers()
        if self.credentials.get("api_key"):
            h["Authorization"] = f"Bearer {self.credentials['api_key']}"
        return h

    def classify(self, status, body, what):
        if status in (404, 410) and "/asks/" in what:
            return AdapterError(CAPACITY, f"vast: the ask is no longer available ({status})", status, body)
        return super().classify(status, body, what)

    def check_availability(self, offer: Offer) -> Availability:
        q = {"verified": {"eq": True}, "rentable": {"eq": True}, "type": "on-demand",
             "gpu_name": {"eq": offer.sku}, "num_gpus": {"eq": offer.gpu_count},
             "order": [["dph_total", "asc"]], "limit": 20}
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
                            note=f"cheapest of {len(offers)} matching asks",
                            metadata={"ask_id": best.get("id"), "machine_id": best.get("machine_id"),
                                      "reliability": best.get("reliability")})

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        ask = availability.metadata.get("ask_id")
        if not ask:
            raise AdapterError(CAPACITY, "vast: no ask id from the availability check")
        body = {"client_id": "me", "image": launch.image, "disk": int(launch.disk_gb or 32),
                "label": name, "runtype": "ssh"}
        if launch.env:
            body["env"] = dict(launch.env)
        r = self.request("PUT", f"/api/v0/asks/{ask}/", json=body) or {}
        if not r.get("success") or not r.get("new_contract"):
            kind = CAPACITY if self.is_capacity_error(200, r) else "provider_error"
            raise AdapterError(kind, f"vast: create failed: {str(r)[:300]}", 200, r)
        return Instance(instance_id=str(r["new_contract"]), status=PROVISIONING, provider_status="created",
                        region=availability.region, price_per_gpu_hour=availability.list_price_per_gpu_hour,
                        metadata={"ask_id": ask, "launch_request": {**body, "env": sorted(body.get("env", {}))}})

    def status(self, instance_id: str) -> Instance:
        d = (self.request("GET", f"/api/v0/instances/{instance_id}/") or {}).get("instances") or {}
        if isinstance(d, list):
            d = d[0] if d else {}
        st = d.get("actual_status")
        n = d.get("num_gpus")
        price = d.get("dph_total")
        return Instance(instance_id=instance_id, status=STATUS.get(st, PROVISIONING if st is None else UNKNOWN),
                        provider_status=st, region=d.get("geolocation"), ip=d.get("public_ipaddr") or None,
                        price_per_gpu_hour=None if price is None or not n else float(price) / n,
                        metadata={"machine_id": d.get("machine_id"), "cur_state": d.get("cur_state")})

    def stop(self, instance_id: str) -> Instance:
        self.request("PUT", f"/api/v0/instances/{instance_id}/", json={"state": "stopped"})
        return Instance(instance_id=instance_id, status=STOPPED, provider_status="stopped")

    def terminate(self, instance_id: str) -> Instance:
        self.request("DELETE", f"/api/v0/instances/{instance_id}/")
        return Instance(instance_id=instance_id, status=TERMINATED, provider_status="destroyed")
