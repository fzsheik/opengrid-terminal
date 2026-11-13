"""RunPod: GPU pods (containers, not VMs). REST v2 spec: https://api.runpod.io/v2/openapi.json (2.0.0, fetched 2026-11-13)

REST v1 (rest.runpod.io/v1) "is deprecated and will be retired on November 15, 2026" and the GraphQL API
"will be retired in early 2027" (docs.runpod.io/api-reference-v2/migrate-from-v1): this adapter uses v2 only.

    stock     GET  /v2/catalog/gpus?include=AVAILABILITY&product=POD&count=N&cloud=SECURE
                   {gpus[{id, price{secure, community} (USD/h for a SINGLE GPU), availability NONE|LOW|MEDIUM|HIGH,
                          dataCenters[{id, availability}]}]}
    create    POST /v2/pods  {name (required), image, gpu{id, count}, cloud SECURE|COMMUNITY, ports["22/tcp"], env{},
                              disk?, mounts{persistent{size >= 10, path}}?}  (unevaluatedProperties: false)
                   -> 201 Pod; 202 QueuedPod only when `queue` is sent (never sent here)
                   errors RFC 9457 {title, status, detail, errors[]}: 400 rule broken OR "could not be placed"
                   (capacity "carries no machine-readable code"), 402 insufficient balance, 403, 413, 422, 429
    list      GET  /v2/pods?limit=1000&cursor=   {pods[Pod], pagination{nextCursor, hasNextPage}}  newest-first;
                   no name filter ("Filter in your own code after reading every page")
    get       GET  /v2/pods/{id}   Pod{id, name, status PROVISIONING|STARTING|RUNNING|EXITED|ERROR|TERMINATED,
                                       cost (USD/h), gpu{id, count}, dataCenterId, createdAt, startedAt,
                                       ssh{proxy, direct{host, port}}}; 404 when missing
    stop      POST /v2/pods/{id}/action {action: stop} -> 200 Pod; 409 when not valid for the status
    delete    DELETE /v2/pods/{id}  204; 404; 409 cluster member
    billing   GET  /v2/billing/pods?podId=&startTime=&endTime=&bucketSize=hour
                   {records[{podId, totalAmount, gpuAmount, diskAmount, startTime, endTime}], metadata{totals}}
    auth      Authorization: Bearer <key>; RateLimit / RateLimit-Policy headers, 429 + Retry-After

Only SECURE cloud is routed: community cloud is excluded by market.py. `status` is the ACTUAL state in v2
(RUNNING = "container is healthy"). The customer public key is the per-pod PUBLIC_KEY env var: `startSsh`
(not sent) would inject the ACCOUNT's keys "unless the request already sets one"; direct SSH needs
"22/tcp" in ports (v2 has no default ports). A volume (`volume_gb`, >= 10) is mounts.persistent at
/workspace (v1's volumeMountPath default); it persists and bills after stop. Stopped pods: "a stopped pod
isn't billed for compute, only for its volume disk" (Pod.cost) -> stopped_billing storage_only.
"""

from datetime import datetime, timezone

from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, CostReport, InstanceState, Offer, TerminateResult, parse_time,
)
from routing.adapters.results import PER_DEPLOYMENT

STATE = {"PROVISIONING": "pending", "STARTING": "pending", "RUNNING": "running", "EXITED": "stopped",
         "TERMINATED": "terminated"}
ERROR_STATES = {"ERROR"}
POD_STATUSES = set(STATE) | ERROR_STATES
SSH_PORTS = ["22/tcp"]
VOLUME_MIN_GB = 10
VOLUME_PATH = "/workspace"
RP = "https://api.runpod.io/v2/openapi.json"


class RunPodAdapter(Adapter):
    provider = "runpod"
    LEVEL = 3
    SUPPORTS_STOP = True
    BASE_URL = "https://api.runpod.io"
    REQUIRED_LAUNCH = ("image",)
    NAME_MAX = 191
    SSH_KEY_REGISTRATION = PER_DEPLOYMENT   # per-pod PUBLIC_KEY env: no provider key object to delete
    SSH_KEY_RESOURCE = False
    CAPABILITIES = Capabilities(
        quote=("YES", f"GET /v2/catalog/gpus price.secure (per GPU-hour) x count ({RP})"),
        live_availability=("PARTIAL", "availability NONE/LOW/MEDIUM/HIGH for count + cloud; per-data-center list"),
        launch=("YES", f"POST /v2/pods -> 201 Pod ({RP})"),
        ssh_key_injection=("YES", "per-pod env PUBLIC_KEY + ports ['22/tcp'] (v2 CreatePodRequest.startSsh)"),
        startup_script=("PARTIAL", "args/cmd/entrypoint exist; not plumbed (refused: bake it into the image)"),
        status=("YES", "PROVISIONING/STARTING/RUNNING/EXITED/ERROR/TERMINATED (actual state in v2)"),
        stop=("YES", "POST /v2/pods/{id}/action {action: stop}"),
        terminate=("YES", "DELETE /v2/pods/{id} 204; confirmed by status/list"),
        region_selection=("NO (not sent)", "dataCenterIds exists; RunPod routes are not region-pinned"),
        gpu_count_selection=("YES", "gpu.count"),
        price_known_before_launch=("YES", "catalog price; Pod.cost in the create response"),
        billing_unit=("per second", "'billed by the second' (docs.runpod.io/pods/pricing)"),
        minimum_commitment=("NO", "402 insufficient balance; needs at least one hour's worth of credits"),
        interruptible=("NO (SECURE on-demand)", "community exists, not routed; v2 has no spot pods"),
        name_tag_at_launch=("PARTIAL", "name only (not unique); create accepts no tags"),
        list_instances=("YES", "GET /v2/pods (cursor pages of up to 1000)"),
        idempotency_token=("NO", "none on pod create (v2 spec)"),
        stopped_billing=("storage_only", "Pod.cost: 'a stopped pod isn't billed for compute, only for its volume disk'"),
        find_by_name=("YES", "client-side exact match over GET /v2/pods (names are not unique: >1 = ambiguous)"),
        reported_cost=("YES", "GET /v2/billing/pods?podId&startTime&endTime -> records[].totalAmount (USD)"),
        error_semantics=("medium", "RFC 9457; 400 mixes rule violations and capacity (no capacity code)"),
        forces_account_ssh_key=("UNKNOWN", "v2: startSsh 'injects a PUBLIC_KEY environment variable carrying your "
                                           "account's registered SSH public keys, unless the request already sets "
                                           "one'; OpenGrid sets PUBLIC_KEY and never sends startSsh, but the v1 "
                                           "docs said account keys are injected automatically. Not validated live "
                                           f"({RP}, fetched 2026-11-13)"),
        billing_starts=("UNKNOWN", "per-second billing; start event not tied to an API field"),
        risks=["Container pods, not VMs: image must run sshd from PUBLIC_KEY (RunPod official images do)",
               "REST v1 retired 2026-11-15: v1 pod ids are not documented to work on v2",
               "400 on create may be capacity or a rule violation (no code)"],
    )

    def headers(self):
        return {**super().headers(), "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
                "content-type": "application/json"}

    def _cloud(self, offer: Offer) -> str:
        return "COMMUNITY" if (offer.provider_tier or "secure") == "community" else "SECURE"

    def check_availability(self, offer: Offer) -> Availability:
        cloud = self._cloud(offer)
        body = self.request("GET", "/v2/catalog/gpus", params={
            "include": "AVAILABILITY", "product": "POD", "count": offer.gpu_count, "cloud": cloud}) or {}
        gpu = next((g for g in body.get("gpus") or [] if isinstance(g, dict) and g.get("id") == offer.raw_gpu_name),
                   None)
        if gpu is None:
            return Availability(available=False, live=True, note=f"gpu type {offer.raw_gpu_name} not in the catalog")
        stock = gpu.get("availability")
        price = (gpu.get("price") or {}).get(cloud.lower())
        return Availability(
            available=stock in ("LOW", "MEDIUM", "HIGH") and price is not None, live=True, region=None,
            list_price_per_gpu_hour=None if price is None else float(price),
            note=f"availability={stock}",
            metadata={"availability": stock,
                      "data_centers": [d.get("id") for d in gpu.get("dataCenters") or []
                                       if isinstance(d, dict) and d.get("availability") not in (None, "NONE")]})

    def _volume_gb(self, launch) -> int:
        return int(launch.extra.get("volume_gb") or 0)

    def missing_launch(self, launch, offer):
        m = super().missing_launch(launch, offer)
        if launch.startup_script:
            m.append("startup_script is not supported by this adapter (bake it into the image)")
        if 0 < self._volume_gb(launch) < VOLUME_MIN_GB:
            m.append(f"volume_gb must be at least {VOLUME_MIN_GB} (RunPod v2 mounts.persistent minimum)")
        return m

    def _provision(self, offer, availability, launch, name):
        env = dict(launch.env or {})
        if launch.ssh_public_key:
            env["PUBLIC_KEY"] = launch.ssh_public_key
        body = {"name": name, "image": launch.image, "gpu": {"id": offer.raw_gpu_name, "count": offer.gpu_count},
                "cloud": self._cloud(offer), "ports": list(SSH_PORTS)}
        if launch.disk_gb:
            body["disk"] = int(launch.disk_gb)
        if self._volume_gb(launch):
            body["mounts"] = {"persistent": {"size": self._volume_gb(launch), "path": VOLUME_PATH}}
        if env:
            body["env"] = env
        pod = self.request("POST", "/v2/pods", json=body)
        if not isinstance(pod, dict) or not pod.get("id"):
            return self.unknown(pod, "runpod: create returned no pod id")
        if pod.get("status") not in POD_STATUSES:
            # A QueuedPod (202, status QUEUED) carries a queue id, not a pod id: never adopt it as the instance.
            return self.unknown(pod, f"runpod: create answered without a pod status ({pod.get('status')!r})")
        return self.accepted(pod["id"], {"request": body, "response": pod})

    def _state(self, pod: dict) -> InstanceState:
        st = pod.get("status")
        cost = pod.get("cost")
        gpu = pod.get("gpu") or {}
        direct = (pod.get("ssh") or {}).get("direct") or {}
        err = st in ERROR_STATES
        return InstanceState(
            state="error" if err else STATE.get(st, "unknown"), instance_id=str(pod.get("id")), name=pod.get("name"),
            provider_status=st, region=pod.get("dataCenterId"), gpu=gpu.get("id"), gpu_count=gpu.get("count"),
            price_per_hour=None if cost is None else float(cost), created_at=parse_time(pod.get("createdAt")),
            time_fields={"created_at": "createdAt"} if pod.get("createdAt") else {},
            ip=direct.get("host") or None, error_kind="provider_error_state" if err else None,
            raw_redacted={k: pod.get(k) for k in ("id", "name", "status", "cost", "createdAt", "startedAt",
                                                  "dataCenterId", "cloud")})

    def _status(self, instance_id):
        pod = self.request("GET", f"/v2/pods/{instance_id}")
        if not isinstance(pod, dict) or not pod:
            raise AdapterError("parse", "runpod: get returned no pod", sent=True)
        pod.setdefault("id", instance_id)
        return self._state(pod)

    def _stop(self, instance_id):
        r = self.request("POST", f"/v2/pods/{instance_id}/action", json={"action": "stop"})
        return TerminateResult("accepted", "runpod: stop accepted (compute billing stops; volume storage still bills)",
                               raw_redacted={k: r.get(k) for k in ("id", "status")} if isinstance(r, dict) else None)

    def _terminate(self, instance_id):
        self.request("DELETE", f"/v2/pods/{instance_id}", allow_text=True)
        return TerminateResult("accepted", "runpod: delete accepted (confirm with status/list)", 204)

    def _list(self):
        out, cursor = [], None
        for _ in range(100):
            body = self.request("GET", "/v2/pods", params={"limit": 1000, **({"cursor": cursor} if cursor else {})})
            if not isinstance(body, dict) or not isinstance(body.get("pods"), list):
                raise AdapterError("parse", "runpod: list returned no pods array", body=body, sent=True)
            out += [self._state(p) for p in body["pods"] if isinstance(p, dict)]
            page = body.get("pagination")
            if not isinstance(page, dict):
                raise AdapterError("parse", "runpod: list returned no pagination block", sent=True)
            cursor = page.get("nextCursor")
            if not page.get("hasNextPage"):
                return out
            if not cursor:
                raise AdapterError("parse", "runpod: hasNextPage without a nextCursor; refusing a partial list", sent=True)
        raise AdapterError("parse", "runpod: more than 100 pages of pods; refusing a partial list", sent=True)

    def reported_cost(self, instance_id: str, start: datetime | None, end: datetime | None) -> CostReport:
        if start is None or end is None:
            return CostReport(None, reason="runpod billing needs a start and end time")

        def rfc3339(t: datetime) -> str:
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()

        try:
            body = self.request("GET", "/v2/billing/pods", params={
                "podId": instance_id, "startTime": rfc3339(start), "endTime": rfc3339(end), "bucketSize": "hour"})
            rows = body.get("records") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise ValueError("billing returned no records array")
            mine = [r for r in rows if isinstance(r, dict) and str(r.get("podId") or instance_id) == str(instance_id)]
            total = sum(float(r.get("totalAmount") or 0) for r in mine)
        except (AdapterError, ValueError, TypeError) as exc:
            return CostReport(None, start, end, reason="runpod billing unavailable: "
                                                       f"{self.scrub(getattr(exc, 'message', str(exc)))[:160]}")
        if not mine:
            return CostReport(None, start, end, reason="runpod billing has no records for this pod yet")
        return CostReport(round(total, 6), start, end, basis="runpod /v2/billing/pods totalAmount (hour buckets)",
                          raw_redacted=mine[:50])
