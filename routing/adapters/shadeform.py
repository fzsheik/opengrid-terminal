"""Shadeform: one API key launches instances on many clouds; OpenGrid uses it for Crusoe, Denvr
and Latitude.sh, whose own APIs OpenGrid has no account with. Docs: https://docs.shadeform.ai

    GET  /instances/types?cloud=&shade_instance_type=   instance_types[{hourly_price (cents), num_gpus,
                                                         availability[{region, available, rental_type}]}]  (public)
    POST /instances/create      {cloud, region, shade_instance_type, shade_cloud: true, name, ssh_key_id?, os?}
                                -> {id, cloud_assigned_id}
    GET  /instances/{id}/info   {status creating|pending_provider|pending|active|error|deleting|deleted,
                                 hourly_price (cents), configuration{num_gpus}, ip, region}
    POST /instances/{id}/delete
    auth header X-API-KEY

This is itself an aggregator route: the counterparty and the bill are Shadeform's,
and the price is Shadeform's listing of the cloud, not the cloud's own. No stop.
"""

from providers.shadeform import BASE_URL as SHADEFORM_HOST
from routing.adapters.base import (
    FAILED, PROVISIONING, RUNNING, TERMINATED, TERMINATING, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer, pick_region,
)

STATUS = {"creating": PROVISIONING, "pending_provider": PROVISIONING, "pending": PROVISIONING,
          "active": RUNNING, "error": FAILED, "deleting": TERMINATING, "deleted": TERMINATED}


class ShadeformAdapter(Adapter):
    provider = "shadeform"          # replaced by the cloud name (crusoe, denvr, latitude) at build time
    LEVEL = 3
    BASE_URL = SHADEFORM_HOST + "/v1"
    CHECK_NEEDS_CREDENTIALS = False
    CAPACITY_SIGNALS = ("no availability", "not available", "unavailable", "capacity")

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

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        if not availability.region:
            raise AdapterError("capacity", f"shadeform: no available region for {offer.listing_id}")
        body = {"cloud": self.provider, "region": availability.region, "shade_instance_type": offer.listing_id,
                "shade_cloud": True, "name": name}
        if launch.ssh_key:
            body["ssh_key_id"] = launch.ssh_key
        if launch.image:
            body["os"] = launch.image
        r = self.request("POST", "/instances/create", json=body) or {}
        if not r.get("id"):
            raise AdapterError("unknown_state", f"shadeform: create returned no id: {str(r)[:300]}")
        return Instance(instance_id=r["id"], status=PROVISIONING, provider_status="creating",
                        region=availability.region,
                        metadata={"cloud_assigned_id": r.get("cloud_assigned_id"), "launch_request": body})

    def status(self, instance_id: str) -> Instance:
        d = self.request("GET", f"/instances/{instance_id}/info") or {}
        n = (d.get("configuration") or {}).get("num_gpus")
        cents = d.get("hourly_price")
        return Instance(instance_id=instance_id, status=STATUS.get(d.get("status"), UNKNOWN),
                        provider_status=d.get("status"), region=d.get("region"), ip=d.get("ip") or None,
                        price_per_gpu_hour=None if cents is None or not n else cents / 100 / n,
                        metadata={"cloud": d.get("cloud"), "cloud_assigned_id": d.get("cloud_assigned_id"),
                                  "status_details": d.get("status_details")})

    def terminate(self, instance_id: str) -> Instance:
        self.request("POST", f"/instances/{instance_id}/delete")
        return Instance(instance_id=instance_id, status=TERMINATING, provider_status="deleting")
