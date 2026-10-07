"""Hyperstack (Infrahub): VMs. Docs: https://docs.hyperstack.cloud/docs/api-reference/

    GET    /core/flavors?region=R          data[{flavors[{name, region_name, gpu, gpu_count, stock_available}]}]
    GET    /pricebook                      [{name, value}]  (per GPU-hour, keyed by GPU api name)
    POST   /core/virtual-machines          {name, environment_name, image_name, flavor_name, key_name, count,
                                            assign_floating_ip} -> {status, message, instances[{id, status, ...}]}
    GET    /core/virtual-machines/{id}     {status, message, instance{id, status, floating_ip, ...}}
    GET    /core/virtual-machines/{id}/stop
    DELETE /core/virtual-machines/{id}
    auth   header api_key: <key>

A VM needs an environment (region-bound), a key pair registered in that environment,
and an image name. The environment comes from launch defaults:
    {"hyperstack": {"environments": {"CANADA-1": "my-env"}}} or {"environment_name": "..."}.
Whether a stopped (SHUTOFF) VM still bills has not been verified; terminate to stop billing.
"""

from providers.hyperstack import BASE_URL
from routing.adapters.base import (
    CAPACITY, FAILED, INVALID, PROVISIONING, RUNNING, STOPPED, TERMINATED, TERMINATING, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer,
)

STATUS = {"CREATING": PROVISIONING, "BUILD": PROVISIONING, "STARTING": PROVISIONING, "REBOOTING": PROVISIONING,
          "ACTIVE": RUNNING, "SHUTOFF": STOPPED, "STOPPED": STOPPED, "HIBERNATED": STOPPED,
          "ERROR": FAILED, "DELETING": TERMINATING, "DELETED": TERMINATED}


class HyperstackAdapter(Adapter):
    provider = "hyperstack"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("ssh_key", "image")
    CAPACITY_SIGNALS = ("insufficient", "not enough resources", "no available", "out of capacity")

    def headers(self):
        return {**super().headers(), "api_key": self.credentials.get("api_key", "")}

    def environment(self, launch: LaunchSpec, region: str | None) -> str | None:
        envs = launch.extra.get("environments") or {}
        return envs.get(region) or launch.extra.get("environment_name")

    def missing_launch(self, launch: LaunchSpec, offer: Offer) -> list[str]:
        missing = super().missing_launch(launch, offer)
        if not self.environment(launch, offer.region):
            missing.append(f"environment for region {offer.region}")
        return missing

    def check_availability(self, offer: Offer) -> Availability:
        body = self.request("GET", "/core/flavors", params={"region": offer.region} if offer.region else None) or {}
        flavor = next((f for g in body.get("data") or [] for f in g.get("flavors") or []
                       if f.get("name") == offer.sku and (offer.region is None or f.get("region_name") == offer.region)),
                      None)
        if flavor is None:
            return Availability(available=False, live=True, note=f"flavor {offer.sku} not listed in {offer.region}")
        price = None
        book = self.request("GET", "/pricebook")
        for e in book if isinstance(book, list) else []:
            if e.get("name") == flavor.get("gpu") and e.get("value") is not None:
                price = float(e["value"])  # Hyperstack prices per GPU-hour
        return Availability(available=bool(flavor.get("stock_available")), live=True,
                            region=flavor.get("region_name"), list_price_per_gpu_hour=price,
                            note=f"stock_available={flavor.get('stock_available')}")

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        region = availability.region or offer.region
        env = self.environment(launch, region)
        if not env:
            raise AdapterError("config", f"hyperstack: no environment configured for region {region}")
        body = {"name": name[:50], "environment_name": env, "image_name": launch.image, "flavor_name": offer.sku,
                "key_name": launch.ssh_key, "count": 1, "assign_floating_ip": True}
        resp = self.request("POST", "/core/virtual-machines", json=body) or {}
        if resp.get("status") is False:  # Hyperstack can answer 200 with status false
            kind = CAPACITY if self.is_capacity_error(200, resp) else INVALID
            raise AdapterError(kind, f"hyperstack: {resp.get('message')}", 200, resp)
        vms = resp.get("instances") or []
        if not vms or vms[0].get("id") is None:
            raise AdapterError("unknown_state", f"hyperstack: create returned no VM id: {str(resp)[:300]}")
        return self._instance(vms[0], {"launch_request": body})

    def _instance(self, vm: dict, extra: dict | None = None) -> Instance:
        st = (vm.get("status") or "").upper() or None
        return Instance(instance_id=str(vm["id"]), status=STATUS.get(st, UNKNOWN), provider_status=st,
                        region=((vm.get("environment") or {}).get("region")), ip=vm.get("floating_ip") or None,
                        metadata={"environment": (vm.get("environment") or {}).get("name"), **(extra or {})})

    def status(self, instance_id: str) -> Instance:
        vm = (self.request("GET", f"/core/virtual-machines/{instance_id}") or {}).get("instance") or {}
        vm.setdefault("id", instance_id)
        return self._instance(vm)

    def stop(self, instance_id: str) -> Instance:
        self.request("GET", f"/core/virtual-machines/{instance_id}/stop")
        return Instance(instance_id=instance_id, status=STOPPED, provider_status="SHUTOFF")

    def terminate(self, instance_id: str) -> Instance:
        self.request("DELETE", f"/core/virtual-machines/{instance_id}")
        return Instance(instance_id=instance_id, status=TERMINATING, provider_status="DELETING")
