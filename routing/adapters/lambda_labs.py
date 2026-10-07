"""Lambda Cloud: launch / inspect / terminate VMs. Docs: https://docs.lambda.ai/api/cloud

    GET  /instance-types                    data{type: {instance_type{price_cents_per_hour, specs{gpus}},
                                                         regions_with_capacity_available[{name}]}}
    POST /instance-operations/launch        {region_name, instance_type_name, ssh_key_names[], name}
                                            -> data{instance_ids[]}
    GET  /instances/{id}                    data{id, status, ip, region{name}, instance_type{...}}
    POST /instance-operations/terminate     {instance_ids[]} -> data{terminated_instances[]}
    errors                                  {error{code, message, suggestion}};
                                            capacity: code instance-operations/launch/insufficient-capacity

No stop endpoint: Lambda offers terminate and restart only.
"""

from providers.lambda_labs import BASE_URL
from routing.adapters.base import (
    CAPACITY, PROVISIONING, RUNNING, TERMINATED, TERMINATING, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer, pick_region,
)

STATUS = {"booting": PROVISIONING, "active": RUNNING, "unhealthy": UNKNOWN,
          "terminating": TERMINATING, "terminated": TERMINATED, "preempted": TERMINATED}


class LambdaAdapter(Adapter):
    provider = "lambda"
    LEVEL = 3
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("ssh_key",)
    CAPACITY_SIGNALS = ("insufficient-capacity",)

    def headers(self):
        return {**super().headers(), "Authorization": f"Bearer {self.credentials.get('api_key', '')}"}

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

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        region = availability.region or offer.region
        if not region:
            raise AdapterError(CAPACITY, "lambda: no region with capacity for " + offer.sku)
        body = {"region_name": region, "instance_type_name": offer.sku,
                "ssh_key_names": [launch.ssh_key], "name": name[:64]}
        data = (self.request("POST", "/instance-operations/launch", json=body) or {}).get("data") or {}
        ids = data.get("instance_ids") or []
        if not ids:
            raise AdapterError("unknown_state", f"lambda: launch returned no instance id: {data}")
        return Instance(instance_id=ids[0], status=PROVISIONING, provider_status="booting", region=region,
                        metadata={"launch_request": body})

    def status(self, instance_id: str) -> Instance:
        d = (self.request("GET", f"/instances/{instance_id}") or {}).get("data") or {}
        it = d.get("instance_type") or {}
        gpus = (it.get("specs") or {}).get("gpus")
        cents = it.get("price_cents_per_hour")
        return Instance(
            instance_id=instance_id, status=STATUS.get(d.get("status"), UNKNOWN), provider_status=d.get("status"),
            region=(d.get("region") or {}).get("name"), ip=d.get("ip"),
            price_per_gpu_hour=None if cents is None or not gpus else cents / 100 / gpus,
            metadata={"hostname": d.get("hostname"), "instance_type": it.get("name")},
        )

    def terminate(self, instance_id: str) -> Instance:
        body = {"instance_ids": [instance_id]}
        data = (self.request("POST", "/instance-operations/terminate", json=body) or {}).get("data") or {}
        gone = [i for i in data.get("terminated_instances") or [] if i.get("id") == instance_id]
        st = gone[0].get("status") if gone else "terminating"
        return Instance(instance_id=instance_id, status=STATUS.get(st, TERMINATING), provider_status=st)
