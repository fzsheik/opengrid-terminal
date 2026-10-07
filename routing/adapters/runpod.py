"""RunPod: GPU pods (containers, not VMs). Spec: https://rest.runpod.io/v1/openapi.json

    stock     POST https://api.runpod.io/graphql   gpuTypes(input:{id}) { lowestPrice(input:{gpuCount, secureCloud})
                                                   { uninterruptablePrice stockStatus } }   (pod TOTAL price)
    create    POST /pods  {name (<=191), imageName, gpuTypeIds[], gpuCount, cloudType, computeType, interruptible,
                           containerDiskInGb?, volumeInGb, env{}, dockerStartCmd?} -> 201 Pod; 400 "Invalid input."
    list      GET  /pods?name=<name>               [Pod]   (bare array)
    get       GET  /pods/{id}                      Pod{id, name, desiredStatus RUNNING|EXITED|TERMINATED, costPerHr,
                                                       gpu{count}, lastStartedAt, publicIp}; 404 when missing
    stop      POST /pods/{id}/stop
    delete    DELETE /pods/{id}                    204 "Pod successfully deleted."
    billing   GET  /billing/pods?podId=&startTime=&endTime=&bucketSize=hour&grouping=podId
                                                   [{amount (USD), podId, time, timeBilledMs, diskSpaceBilledGb}]

Only SECURE cloud is routed: community cloud is interruptible and market.py excludes it.
`desiredStatus` is RunPod's TARGET state (RUNNING right after create, while the image still pulls):
"running" here means RunPod accepted and scheduled the pod, not that the container is serving.
A customer public key is injected with the per-pod SSH_PUBLIC_KEY env var
(https://docs.runpod.io/pods/configuration/use-ssh). volumeInGb is sent as 0 unless asked for: a volume
persists and bills after stop. Stopped pods: GPU and container disk are not charged; volume disk is
($0.20/GB/month) (https://docs.runpod.io/pods/pricing) -> stopped_billing storage_only.
"""

import json
from datetime import datetime

from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, CostReport, InstanceState, Offer, TerminateResult, parse_time,
)

GRAPHQL = "https://api.runpod.io/graphql"
STATE = {"RUNNING": "running", "EXITED": "stopped", "TERMINATED": "terminated", "CREATED": "pending"}
RP = "https://rest.runpod.io/v1/openapi.json"


class RunPodAdapter(Adapter):
    provider = "runpod"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = "https://rest.runpod.io/v1"
    REQUIRED_LAUNCH = ("image",)
    NAME_MAX = 191
    SSH_KEY_REGISTRATION = True        # per-pod SSH_PUBLIC_KEY env
    CAPABILITIES = Capabilities(
        quote=("YES", "GraphQL lowestPrice(gpuCount).uninterruptablePrice = pod total (verified live 2026-10)"),
        live_availability=("PARTIAL", "stockStatus High/Medium/Low/null; no datacenter"),
        launch=("YES", f"POST /pods -> 201 Pod ({RP})"),
        ssh_key_injection=("YES", "account keys, or per-pod SSH_PUBLIC_KEY env (docs.runpod.io/pods/configuration/use-ssh)"),
        startup_script=("PARTIAL", "dockerStartCmd (string command split by the adapter is not attempted; refused)"),
        status=("PARTIAL", "desiredStatus is the TARGET state, not proof the container runs"),
        stop=("YES", "POST /pods/{id}/stop"),
        terminate=("YES", "DELETE /pods/{id} 204; confirmed by status/list"),
        region_selection=("NO (not sent)", "dataCenterIds/countryCodes exist; RunPod routes are not region-pinned"),
        gpu_count_selection=("YES", "gpuCount"),
        price_known_before_launch=("YES", "lowestPrice; costPerHr in the create response"),
        billing_unit=("per second", "'billed by the second' (docs.runpod.io/pods/pricing)"),
        minimum_commitment=("NO", "needs at least one hour's worth of credits to deploy"),
        interruptible=("NO (interruptible false, SECURE)", "community/spot exist, not routed"),
        name_tag_at_launch=("PARTIAL", "name only (<=191, not unique); no tags"),
        list_instances=("YES", "GET /pods?name= (bare array)"),
        idempotency_token=("NO", "none in spec"),
        stopped_billing=("storage_only", "stopped: GPU + container disk not charged, volume disk $0.20/GB/mo "
                                         "(docs.runpod.io/pods/pricing)"),
        find_by_name=("YES", "GET /pods?name=og-<dep>, exact match (names are not unique: >1 = ambiguous)"),
        reported_cost=("YES", "GET /billing/pods?podId&startTime&endTime&grouping=podId -> amount (USD)"),
        error_semantics=("weak", "create documents only 201/400"),
        risks=["Container pods, not VMs: image must run sshd", "GraphQL stock endpoint may be retired (UNCONFIRMED)",
               "desiredStatus is a target state"],
    )

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
            raise AdapterError("server", f"runpod: graphql errors: {self.scrub(str(body['errors']))[:300]}", 200)
        types = (body.get("data") or {}).get("gpuTypes") or []
        lp = (types[0].get("lowestPrice") if types else None) or {}
        stock, total = lp.get("stockStatus"), lp.get("uninterruptablePrice")
        available = stock not in (None, "None") and total is not None
        return Availability(
            available=available, live=True, region=None,
            list_price_per_gpu_hour=None if total is None else float(total) / offer.gpu_count,
            note=f"stockStatus={stock}", metadata={"stock_status": stock},
        )

    def missing_launch(self, launch, offer):
        m = super().missing_launch(launch, offer)
        if launch.startup_script:
            m.append("startup_script is not supported by this adapter (bake it into the image)")
        return m

    def _provision(self, offer, availability, launch, name):
        env = dict(launch.env or {})
        if launch.ssh_public_key:
            env["SSH_PUBLIC_KEY"] = launch.ssh_public_key
        body = {"name": name, "imageName": launch.image, "gpuTypeIds": [offer.raw_gpu_name],
                "gpuCount": offer.gpu_count, "cloudType": "SECURE" if self._secure(offer) else "COMMUNITY",
                "computeType": "GPU", "interruptible": False, "volumeInGb": int(launch.extra.get("volume_gb") or 0)}
        if launch.disk_gb:
            body["containerDiskInGb"] = int(launch.disk_gb)
        if env:
            body["env"] = env
        pod = self.request("POST", "/pods", json=body)
        if not isinstance(pod, dict) or not pod.get("id"):
            return self.unknown(pod, "runpod: create returned no pod id")
        return self.accepted(pod["id"], {"request": body, "response": pod})

    def _state(self, pod: dict) -> InstanceState:
        st = pod.get("desiredStatus")
        cost = pod.get("costPerHr")
        return InstanceState(
            state=STATE.get(st, "unknown"), instance_id=str(pod.get("id")), name=pod.get("name"), provider_status=st,
            gpu=(pod.get("gpu") or {}).get("displayName"),
            gpu_count=(pod.get("gpu") or {}).get("count") or pod.get("gpuCount"),
            price_per_hour=None if cost is None else float(cost), created_at=parse_time(pod.get("lastStartedAt")),
            ip=pod.get("publicIp") or None,
            raw_redacted={k: pod.get(k) for k in ("id", "name", "desiredStatus", "costPerHr", "lastStartedAt",
                                                  "lastStatusChange", "machineId")})

    def _status(self, instance_id):
        pod = self.request("GET", f"/pods/{instance_id}")
        if not isinstance(pod, dict) or not pod:
            raise AdapterError("parse", "runpod: get returned no pod", sent=True)
        pod.setdefault("id", instance_id)
        return self._state(pod)

    def _stop(self, instance_id):
        r = self.request("POST", f"/pods/{instance_id}/stop", allow_text=True)
        return TerminateResult("accepted", "runpod: stop accepted (GPU billing stops; volume storage still bills)",
                               raw_redacted=r if isinstance(r, dict) else None)

    def _terminate(self, instance_id):
        self.request("DELETE", f"/pods/{instance_id}", allow_text=True)
        return TerminateResult("accepted", "runpod: delete accepted (confirm with status/list)")

    def _pods(self, params):
        rows = self.request("GET", "/pods", params=params)
        if not isinstance(rows, list):
            raise AdapterError("parse", "runpod: list returned no array", body=rows, sent=True)
        return [self._state(p) for p in rows if isinstance(p, dict)]

    def _list(self):
        return self._pods(None)

    def _find(self, name):
        return self._pods({"name": name})

    def reported_cost(self, instance_id: str, start: datetime | None, end: datetime | None) -> CostReport:
        if start is None or end is None:
            return CostReport(None, reason="runpod billing needs a start and end time")
        try:
            rows = self.request("GET", "/billing/pods", params={
                "podId": instance_id, "startTime": start.isoformat(), "endTime": end.isoformat(),
                "bucketSize": "hour", "grouping": "podId"})
            if not isinstance(rows, list):
                raise ValueError("billing returned no array")
            mine = [r for r in rows if isinstance(r, dict) and str(r.get("podId") or instance_id) == str(instance_id)]
            total = sum(float(r.get("amount") or 0) for r in mine)
        except (AdapterError, ValueError, TypeError) as exc:
            return CostReport(None, start, end, reason="runpod billing unavailable: "
                                                       f"{self.scrub(getattr(exc, 'message', str(exc)))[:160]}")
        if not mine:
            return CostReport(None, start, end, reason="runpod billing has no rows for this pod yet")
        return CostReport(round(total, 6), start, end, basis="runpod /billing/pods amount (hour buckets)",
                          raw_redacted=mine[:50])
