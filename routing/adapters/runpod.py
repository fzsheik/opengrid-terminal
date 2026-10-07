"""RunPod: GPU pods (containers, not VMs). Docs: https://docs.runpod.io/api-reference

    stock     POST https://api.runpod.io/graphql   gpuTypes(input:{id}) { lowestPrice(input:{gpuCount, secureCloud})
                                                   { uninterruptablePrice stockStatus } }   (what the poller reads)
    create    POST https://rest.runpod.io/v1/pods  {name, imageName, gpuTypeIds[], gpuCount, cloudType, computeType,
                                                    interruptible, containerDiskInGb?, env?}
                                                   -> Pod{id, desiredStatus, costPerHr, publicIp, ...}
    get       GET    /pods/{id}
    stop      POST   /pods/{id}/stop
    delete    DELETE /pods/{id}

Only SECURE cloud is routed: community cloud is interruptible and market.py excludes it.
`desiredStatus` is RunPod's TARGET state (RUNNING right after create, while the image
still pulls), so "running" here means RunPod accepted and scheduled the pod.
"""

import json

from routing.adapters.base import (
    PROVISIONING, RUNNING, STOPPED, TERMINATED, UNKNOWN,
    Adapter, AdapterError, Availability, Instance, LaunchSpec, Offer,
)

GRAPHQL = "https://api.runpod.io/graphql"
STATUS = {"RUNNING": RUNNING, "EXITED": STOPPED, "TERMINATED": TERMINATED, "CREATED": PROVISIONING}


class RunPodAdapter(Adapter):
    provider = "runpod"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = "https://rest.runpod.io/v1"
    REQUIRED_LAUNCH = ("image",)
    CAPACITY_SIGNALS = ("no longer any instances available", "no instances available",
                        "not enough free gpus", "could not find any pods with required specifications")

    def headers(self):
        return {**super().headers(), "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
                "content-type": "application/json"}

    def _secure(self, offer: Offer) -> bool:
        return (offer.provider_tier or "secure") != "community"

    def check_availability(self, offer: Offer) -> Availability:
        q = ("query { gpuTypes(input: {id: %s}) { id lowestPrice(input: {gpuCount: %d, secureCloud: %s}) "
             "{ uninterruptablePrice stockStatus } } }"
             % (json.dumps(offer.raw_gpu_name), offer.gpu_count, "true" if self._secure(offer) else "false"))
        body = self.request("POST", GRAPHQL, json={"query": q}) or {}
        if body.get("errors"):
            raise AdapterError("provider_error", f"runpod: graphql errors: {str(body['errors'])[:300]}", 200, body)
        types = (body.get("data") or {}).get("gpuTypes") or []
        lp = (types[0].get("lowestPrice") if types else None) or {}
        stock, total = lp.get("stockStatus"), lp.get("uninterruptablePrice")
        available = stock not in (None, "None") and total is not None
        return Availability(
            available=available, live=True, region=None,
            list_price_per_gpu_hour=None if total is None else float(total) / offer.gpu_count,
            note=f"stockStatus={stock}", metadata={"stock_status": stock},
        )

    def provision(self, offer: Offer, availability: Availability, launch: LaunchSpec, name: str) -> Instance:
        body = {"name": name[:191], "imageName": launch.image, "gpuTypeIds": [offer.raw_gpu_name],
                "gpuCount": offer.gpu_count, "cloudType": "SECURE" if self._secure(offer) else "COMMUNITY",
                "computeType": "GPU", "interruptible": False}
        if launch.disk_gb:
            body["containerDiskInGb"] = int(launch.disk_gb)
        if launch.env:
            body["env"] = dict(launch.env)
        pod = self.request("POST", "/pods", json=body) or {}
        if not pod.get("id"):
            raise AdapterError("unknown_state", f"runpod: create returned no pod id: {str(pod)[:300]}")
        return self._instance(pod, offer.gpu_count, {"launch_request": {**body, "env": sorted(body.get("env", {}))}})

    def _instance(self, pod: dict, gpu_count: int | None, extra: dict | None = None) -> Instance:
        cost = pod.get("costPerHr")
        n = gpu_count or pod.get("gpuCount")
        st = pod.get("desiredStatus")
        return Instance(
            instance_id=pod["id"], status=STATUS.get(st, UNKNOWN), provider_status=st,
            price_per_gpu_hour=None if cost is None or not n else float(cost) / n,
            ip=pod.get("publicIp") or None,
            metadata={"machine_id": pod.get("machineId"), **(extra or {})},
        )

    def status(self, instance_id: str) -> Instance:
        pod = self.request("GET", f"/pods/{instance_id}") or {}
        pod.setdefault("id", instance_id)
        return self._instance(pod, None)

    def stop(self, instance_id: str) -> Instance:
        pod = self.request("POST", f"/pods/{instance_id}/stop") or {}
        if isinstance(pod, dict) and pod.get("id"):
            return self._instance(pod, None)
        return Instance(instance_id=instance_id, status=STOPPED, provider_status="EXITED")

    def terminate(self, instance_id: str) -> Instance:
        self.request("DELETE", f"/pods/{instance_id}")
        return Instance(instance_id=instance_id, status=TERMINATED, provider_status="TERMINATED")
