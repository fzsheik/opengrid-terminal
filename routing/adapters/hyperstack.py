"""Hyperstack (Infrahub): VMs. Spec: https://docs.hyperstack.cloud/openapi/hyperstack.json

    GET    /core/flavors?region=R          data[{flavors[{name, region_name, gpu, gpu_count, stock_available}]}]
    GET    /pricebook                      [{name, value}]  (value is a STRING, per GPU-hour, keyed by GPU name)
    POST   /core/keypairs                  {environment_name, name (<=50), public_key} -> keypair{id, name}
    POST   /core/virtual-machines          {name (<=50), environment_name, image_name, flavor_name, key_name, count,
                                            assign_floating_ip, labels[str], security_rules[...]}
                                           -> 200 {status: true, instances[{id, status, ...}]}
    GET    /core/virtual-machines?page=&pageSize=&search=   {instances[...], count, page, page_size}
    GET    /core/virtual-machines/{id}     {status, instance{id, name, status, labels, created_at, floating_ip,
                                            flavor{gpu, gpu_count}, environment{name, region}}}
    DELETE /core/virtual-machines/{id}     200 {status: true}; 400 "... is currently being created and cannot be
                                           deleted yet ..." (bad_request); 404 not_found
    GET    /billing/billing/history/virtual-machine/{id}?start_date=&end_date=
                                           billing_history_vm_details{billing_history[{metrics{incurred_bill}}]}
    errors {status: false, message, error_reason: bad_request|not_found|forbidden|not_allowed|unauthorized|
            server_error|compatibility_blocked}; NO capacity error is documented.
    auth   header api_key: <key>

A VM needs an environment (region-bound) and an image name. The environment comes from launch defaults:
    {"hyperstack": {"environments": {"CANADA-1": "my-env"}}} or {"environment_name": "..."}.
New VMs have NO inbound firewall rules (https://docs.hyperstack.cloud/docs/network/firewalls), so the
launch adds an SSH (tcp/22) ingress rule from launch default `ssh_ingress_cidr` (default 0.0.0.0/0).

Stop is NOT offered: SHUTOFF keeps billing for every resource (states-and-billing). Hibernate stops GPU
billing but restore can fail for capacity; not implemented. Terminate during CREATING is refused (400)
and reported as a retryable failure: the reconciler re-issues it.
"""

from datetime import datetime

from providers.hyperstack import BASE_URL
from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, CostReport, InstanceState, LaunchSpec, Offer,
    TerminateResult, parse_time,
)

STATE = {"CREATING": "pending", "BUILD": "pending", "STARTING": "pending", "REBOOTING": "pending",
         "RESTORING": "pending", "ACTIVE": "running", "SHUTOFF": "stopped", "STOPPED": "stopped",
         "HIBERNATING": "stopping", "HIBERNATED": "stopped", "DELETING": "terminating", "DELETED": "terminated"}
ERROR_STATES = {"ERROR"}
PAGE = 100

HS = "https://docs.hyperstack.cloud/openapi/hyperstack.json"


class HyperstackAdapter(Adapter):
    provider = "hyperstack"
    LEVEL = 3
    SUPPORTS_STOP = False
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("ssh_key", "image")
    NAME_MAX = 50
    SSH_KEY_REGISTRATION = True
    ERROR_STATE_BILLED = False     # "ERROR incurs no charges" (docs/billing/states-and-billing)
    CAPABILITIES = Capabilities(
        quote=("YES", "GET /pricebook value per GPU-hour x flavor gpu_count (docs.hyperstack.cloud/docs/billing/pricebook)"),
        live_availability=("YES", f"GET /core/flavors stock_available ({HS})"),
        launch=("YES", f"POST /core/virtual-machines ({HS})"),
        ssh_key_injection=("YES", "key_name of a keypair in the environment; POST /core/keypairs registers one"),
        startup_script=("NO (not plumbed)", "user_data exists; encoding not verified, so a startup script is refused"),
        status=("YES", "GET /core/virtual-machines/{id}; CREATING/ACTIVE/SHUTOFF/HIBERNATED/DELETING/ERROR"),
        stop=("NO (disabled)", "SHUTOFF: 'billing continues for all resources' (docs/billing/states-and-billing)"),
        terminate=("YES", "DELETE /core/virtual-machines/{id}; 400 while CREATING -> retried by reconciliation"),
        region_selection=("YES", "region-bound environment_name"),
        gpu_count_selection=("PARTIAL", "fixed per flavor"),
        price_known_before_launch=("YES", "pricebook"),
        billing_unit=("per minute", "'per-minute structure' (docs/billing/states-and-billing)"),
        minimum_commitment=("NO", "prepaid balance must cover running resources + 1h of the new VM"),
        interruptible=("NO (on-demand flavors)", "spot flavors exist and are excluded by market rules"),
        name_tag_at_launch=("YES", "name (maxLength 50) + labels[str] = ['opengrid', og-<dep>]"),
        list_instances=("YES", "GET /core/virtual-machines?page&pageSize&search (search = name substring)"),
        idempotency_token=("NO", "none documented"),
        stopped_billing=("full", "SHUTOFF: billing continues for all resources; HIBERNATED bills disk + IP only"),
        find_by_name=("YES", "search=<og-name> then exact match on name or label"),
        reported_cost=("YES", "GET /billing/billing/history/virtual-machine/{id} incurred_bill (spec: returns 500 "
                              "intermittently)"),
        error_semantics=("medium", "real HTTP codes + error_reason; capacity error undocumented -> never inferred"),
        risks=["New VMs have no inbound rules: SSH rule added at launch",
               "Terminate refused while CREATING (retried)", "Environment + keypair needed per region",
               "ERROR state incurs no charges per docs but the VM must still be deleted"],
    )

    def headers(self):
        return {**super().headers(), "api_key": self.credentials.get("api_key", "")}

    def environment(self, launch: LaunchSpec, region: str | None) -> str | None:
        envs = launch.extra.get("environments") or {}
        return envs.get(region) or launch.extra.get("environment_name")

    def missing_launch(self, launch: LaunchSpec, offer: Offer) -> list[str]:
        missing = super().missing_launch(launch, offer)
        if not self.environment(launch, offer.region):
            missing.append(f"environment for region {offer.region}")
        if launch.startup_script:
            missing.append("startup_script is not supported by this adapter (remove it)")
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

    def _provision(self, offer, availability, launch, name):
        region = availability.region or offer.region
        env = self.environment(launch, region)
        if not env:
            raise AdapterError("config", f"hyperstack: no environment configured for region {region}")
        key_name = launch.ssh_key
        if launch.ssh_public_key and not launch.ssh_key:
            kp = self.preflight(self.request, "POST", "/core/keypairs", json={"environment_name": env, "name": name,
                                                               "public_key": launch.ssh_public_key}) or {}
            key_name = ((kp.get("keypair") or {}).get("name")) if isinstance(kp, dict) else None
            if not key_name:
                # Key registration creates no compute, so a failure here is a clean rejection.
                raise AdapterError("invalid", "hyperstack: keypair registration returned no name", sent=False)
        cidr = launch.extra.get("ssh_ingress_cidr") or "0.0.0.0/0"
        body = {"name": name, "environment_name": env, "image_name": launch.image, "flavor_name": offer.sku,
                "key_name": key_name, "count": 1, "assign_floating_ip": True, "labels": ["opengrid", name],
                "security_rules": [{"direction": "ingress", "ethertype": "IPv4", "protocol": "tcp",
                                    "remote_ip_prefix": cidr, "port_range_min": 22, "port_range_max": 22}]}
        resp = self.request("POST", "/core/virtual-machines", json=body)
        if not isinstance(resp, dict) or resp.get("status") is not True:
            # 200 without status:true is undocumented: the VM may have been scheduled.
            return self.unknown(resp, "hyperstack: create answered 200 without status:true")
        vms = resp.get("instances") or []
        if len(vms) != 1 or vms[0].get("id") is None:
            return self.unknown(resp, "hyperstack: create returned no single VM id")
        return self.accepted(vms[0]["id"], {"request": body, "response": resp})

    def _state(self, vm: dict) -> InstanceState:
        st = (vm.get("status") or "").upper() or None
        flavor = vm.get("flavor") or {}
        env = vm.get("environment") or {}
        err = st in ERROR_STATES
        return InstanceState(
            state="error" if err else STATE.get(st, "unknown"), instance_id=str(vm.get("id")),
            name=vm.get("name"), provider_status=st, region=env.get("region"), gpu=flavor.get("gpu") or None,
            gpu_count=flavor.get("gpu_count"), created_at=parse_time(vm.get("created_at")),
            ip=vm.get("floating_ip") or None, labels=[str(x) for x in vm.get("labels") or []],
            error_kind="provider_error_state" if err else None,
            raw_redacted={k: vm.get(k) for k in ("id", "name", "status", "vm_state", "power_state", "labels",
                                                 "created_at", "floating_ip")})

    def _status(self, instance_id):
        body = self.request("GET", f"/core/virtual-machines/{instance_id}") or {}
        vm = body.get("instance") if isinstance(body, dict) else None
        if not vm:
            raise AdapterError("parse", "hyperstack: status returned no instance", body=body, sent=True)
        vm.setdefault("id", instance_id)
        return self._state(vm)

    def _terminate(self, instance_id):
        try:
            r = self.request("DELETE", f"/core/virtual-machines/{instance_id}")
        except AdapterError as exc:
            msg = str((exc.body or {}).get("message") if isinstance(exc.body, dict) else exc.body or "").lower()
            if exc.status_code == 400 and "being created" in msg:
                return TerminateResult("failed", "hyperstack: VM is still CREATING; delete is refused until it "
                                                 "finishes (will be retried)", 400, error_kind="still_creating",
                                       retryable=True)
            raise
        if isinstance(r, dict) and r.get("status") is False:
            return TerminateResult("unknown", f"hyperstack: delete answered 200 with status false: {r.get('message')}",
                                   200, raw_redacted=r)
        return TerminateResult("accepted", "hyperstack: VM is being deleted", 200, raw_redacted=r)

    def _pages(self, params: dict) -> list[dict]:
        out, seen = [], set()
        for page in range(1, 51):
            body = self.request("GET", "/core/virtual-machines", params={**params, "page": page, "pageSize": PAGE}) or {}
            if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
                raise AdapterError("parse", "hyperstack: list returned no instances array", body=body, sent=True)
            vms = body["instances"]
            new = [v for v in vms if v.get("id") not in seen]
            if vms and not new:      # the server ignores paging and repeats the full list
                return out
            seen.update(v.get("id") for v in new)
            out.extend(new)
            count = body.get("count")
            if len(vms) < PAGE or (isinstance(count, int) and len(out) >= count) or body.get("page") is None:
                return out
        raise AdapterError("parse", "hyperstack: more than 50 pages of VMs; refusing a partial list", sent=True)

    def _list(self):
        return [self._state(v) for v in self._pages({})]

    def _find(self, name):
        return [self._state(v) for v in self._pages({"search": name})]

    def reported_cost(self, instance_id: str, start: datetime | None, end: datetime | None) -> CostReport:
        if start is None or end is None:
            return CostReport(None, reason="hyperstack billing history needs a start and end date")
        fmt = "%Y-%m-%dT%H:%M:%S"
        try:
            body = self.request("GET", f"/billing/billing/history/virtual-machine/{instance_id}",
                                params={"start_date": start.strftime(fmt), "end_date": end.strftime(fmt)}) or {}
            rows = ((body.get("billing_history_vm_details") or {}).get("billing_history")) or []
            total = sum(float((r.get("metrics") or {}).get("incurred_bill") or 0) for r in rows)
        except (AdapterError, ValueError, TypeError, AttributeError) as exc:
            return CostReport(None, start, end, reason=f"hyperstack billing history unavailable: "
                                                       f"{self.scrub(getattr(exc, 'message', str(exc)))[:200]}")
        if not rows:
            return CostReport(None, start, end, reason="hyperstack billing history has no rows for this VM yet")
        return CostReport(round(total, 6), start, end, basis="hyperstack billing history incurred_bill",
                          raw_redacted=self.redact(rows[:20]))
