"""DigitalOcean GPU Droplets. Docs: https://docs.digitalocean.com/reference/api/digitalocean/#tag/Droplets

    GET    /v2/sizes?per_page=200          sizes[{slug, price_hourly, available, regions[], gpu_info{count}}]
    POST   /v2/droplets                    {name, region, size, image, ssh_keys[], tags[]} -> 202 droplet{id, status}
    GET    /v2/droplets/{id}               droplet{status new|active|off|archive, networks, size{price_hourly}}
    POST   /v2/droplets/{id}/actions       {type: power_off}
    DELETE /v2/droplets/{id}               204
    errors {id, message}; 422 when a size is not available in a region

A powered-off Droplet is still billed (DigitalOcean bills until destroyed): stop is
offered, but terminate is what ends spend. GPU images are named like "gpu-h100x1-base".
"""

from providers.digitalocean import BASE_URL
from routing.adapters.base import (
    PROVISIONING, RUNNING, STOPPED, TERMINATED, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer, pick_region,
)

STATUS = {"new": PROVISIONING, "active": RUNNING, "off": STOPPED, "archive": TERMINATED}


class DigitalOceanAdapter(Adapter):
    provider = "digitalocean"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("ssh_key", "image")
    CAPACITY_SIGNALS = ("not available in", "is not available", "currently unavailable", "capacity")

    def headers(self):
        return {**super().headers(), "Authorization": f"Bearer {self.credentials.get('api_key', '')}"}

    def check_availability(self, offer: Offer) -> Availability:
        sizes = (self.request("GET", "/v2/sizes", params={"per_page": 200}) or {}).get("sizes") or []
        size = next((s for s in sizes if s.get("slug") == offer.sku), None)
        if size is None:
            return Availability(available=False, live=True, note=f"size {offer.sku} is no longer listed")
        regions = size.get("regions") or []
        preferred = (offer.region or "").split(",")[0] or None
        region = pick_region(self.provider, regions, preferred, offer.want_region_group)
        count = (size.get("gpu_info") or {}).get("count") or offer.gpu_count
        price = size.get("price_hourly")
        return Availability(available=bool(size.get("available")) and region is not None, live=True, region=region,
                            list_price_per_gpu_hour=None if price is None else float(price) / count,
                            note="regions: " + (",".join(regions) or "none"), metadata={"regions": regions})

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        region = availability.region
        if not region:
            raise AdapterError("capacity", f"digitalocean: no region offers {offer.sku}")
        key = launch.ssh_key
        body = {"name": name[:63], "region": region, "size": offer.sku, "image": launch.image,
                "ssh_keys": [int(key) if str(key).isdigit() else key], "tags": ["opengrid"]}
        d = (self.request("POST", "/v2/droplets", json=body) or {}).get("droplet") or {}
        if d.get("id") is None:
            raise AdapterError("unknown_state", "digitalocean: create returned no droplet id")
        return self._instance(d, offer.gpu_count, {"launch_request": body})

    def _instance(self, d: dict, gpu_count: int | None = None, extra: dict | None = None) -> Instance:
        size = d.get("size") or {}
        n = gpu_count or (size.get("gpu_info") or {}).get("count")
        price = size.get("price_hourly")
        ip = next((n4.get("ip_address") for n4 in (d.get("networks") or {}).get("v4") or []
                   if n4.get("type") == "public"), None)
        return Instance(instance_id=str(d["id"]), status=STATUS.get(d.get("status"), UNKNOWN),
                        provider_status=d.get("status"), region=(d.get("region") or {}).get("slug"), ip=ip,
                        price_per_gpu_hour=None if price is None or not n else float(price) / n,
                        metadata={"size_slug": d.get("size_slug"), **(extra or {})})

    def status(self, instance_id: str) -> Instance:
        d = (self.request("GET", f"/v2/droplets/{instance_id}") or {}).get("droplet") or {}
        d.setdefault("id", instance_id)
        return self._instance(d)

    def stop(self, instance_id: str) -> Instance:
        self.request("POST", f"/v2/droplets/{instance_id}/actions", json={"type": "power_off"})
        return Instance(instance_id=instance_id, status=STOPPED, provider_status="off",
                        metadata={"note": "powered-off droplets are still billed"})

    def terminate(self, instance_id: str) -> Instance:
        self.request("DELETE", f"/v2/droplets/{instance_id}")
        return Instance(instance_id=instance_id, status=TERMINATED, provider_status="deleted")
