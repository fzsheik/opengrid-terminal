"""DigitalOcean GPU Droplets. Docs: https://docs.digitalocean.com/reference/api/digitalocean/#tag/Droplets

    GET    /v2/sizes?per_page=200          sizes[{slug, price_hourly, available, regions[], gpu_info{count}}]
    POST   /v2/account/keys                {name, public_key} -> 201 ssh_key{id, fingerprint}
    POST   /v2/droplets                    {name, region, size, image, ssh_keys[], tags[], user_data?}
                                           -> 202 droplet{id, status}
    GET    /v2/droplets?tag_name=&per_page=   {droplets[], links{pages{next}}, meta{total}}
    GET    /v2/droplets/{id}               droplet{status new|active|off|archive, name, tags, created_at,
                                                   networks, size{price_hourly, gpu_info}}
    DELETE /v2/droplets/{id}               204; 404 {"id":"not_found", ...}
    errors {id, message}; 422 unprocessable_entity (the exact text for an unavailable size/region is not
           documented, so capacity is never inferred: a 422 is a plain rejection)

Name: pattern ^[a-zA-Z0-9]?[a-z0-9A-Z.\\-]*[a-z0-9A-Z]$ (no '_'), so instance names are og-<dep> in [a-z0-9-].
Every droplet is tagged 'opengrid' and its own og-<dep> name; list/find use tag_name.
Stop is NOT offered: "You are still billed for GPU Droplets that are powered off"
(https://docs.digitalocean.com/products/droplets/details/pricing/). Image must match the GPU shape
(gpu-h100x1-base for 1-GPU sizes, gpu-h100x8-base for 8-GPU, gpu-amd-base for AMD): set image per size via
launch defaults {"images": {"<size slug>": "<image>"}} or a single `image`.
"""

from routing.adapters.base import (
    AdapterError, Adapter, Availability, Capabilities, InstanceState, Offer, TerminateResult, parse_time, pick_region,
)
from providers.digitalocean import BASE_URL

STATE = {"new": "pending", "active": "running", "off": "stopped", "archive": "terminated"}
DO = "https://docs.digitalocean.com/reference/api/digitalocean/#tag/Droplets"


class DigitalOceanAdapter(Adapter):
    provider = "digitalocean"
    LEVEL = 3
    SUPPORTS_STOP = False
    BASE_URL = BASE_URL
    REQUIRED_LAUNCH = ("ssh_key", "image")
    NAME_MAX = 63
    SSH_KEY_REGISTRATION = True
    CAPABILITIES = Capabilities(
        quote=("YES", "GET /v2/sizes price_hourly (whole droplet) / gpu_info.count"),
        live_availability=("PARTIAL", "size.available + regions is catalogue availability; capacity only known at create"),
        launch=("YES", f"POST /v2/droplets -> 202 droplet ({DO})"),
        ssh_key_injection=("YES", "ssh_keys ids/fingerprints; POST /v2/account/keys registers one"),
        startup_script=("YES", "user_data (<=64 KiB)"),
        status=("YES", "new/active/off/archive"),
        stop=("NO (disabled)", "'You are still billed for GPU Droplets that are powered off' (droplets pricing)"),
        terminate=("YES", "DELETE /v2/droplets/{id} 204; confirmed by status/list"),
        region_selection=("YES", "region"),
        gpu_count_selection=("PARTIAL", "fixed per size slug (x1 / x8)"),
        price_known_before_launch=("YES", "price_hourly"),
        billing_unit=("per second", "'billed per second with a minimum charge of 60 seconds or $0.01'"),
        minimum_commitment=("NO", "may charge the card partway through the cycle for new customers"),
        interruptible=("NO (on-demand sizes)", "spot exists for some AMD/B300 sizes, not routed"),
        name_tag_at_launch=("YES", "name (hostname chars) + tags ['opengrid', og-<dep>]"),
        list_instances=("YES", "GET /v2/droplets (paginated, links.pages.next)"),
        idempotency_token=("NO", "none documented"),
        stopped_billing=("full", "powered-off GPU droplets are billed (droplets/details/pricing)"),
        find_by_name=("YES", "GET /v2/droplets?tag_name=og-<dep>"),
        reported_cost=("NO", "billing is per monthly invoice only; no per-droplet cost endpoint used"),
        error_semantics=("good", "{id, message}; 422 for invalid size/region (text undocumented)"),
        risks=["Image must match the GPU shape", "Droplet limit and 10 concurrent creates",
               "Catalogue availability is not live stock"],
    )

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
                            note="catalogue availability (capacity confirmed only at create); regions: "
                                 + (",".join(regions) or "none"),
                            metadata={"regions": regions})

    def image_for(self, launch, offer) -> str | None:
        return (launch.extra.get("images") or {}).get(offer.sku) or launch.image

    def missing_launch(self, launch, offer):
        m = [f for f in super().missing_launch(launch, offer) if f != "image"]
        if not self.image_for(launch, offer):
            m.append("image")
        return m

    def _provision(self, offer, availability, launch, name):
        region = availability.region
        if not region:
            raise AdapterError("capacity", f"digitalocean: no region offers {offer.sku}", sent=False)
        key = launch.ssh_key
        if launch.ssh_public_key and not key:
            k = self.preflight(self.request, "POST", "/v2/account/keys", json={"name": name, "public_key": launch.ssh_public_key})
            key = ((k or {}).get("ssh_key") or {}).get("id") if isinstance(k, dict) else None
            if key is None:
                raise AdapterError("invalid", "digitalocean: ssh key registration returned no id", sent=False)
        body = {"name": name, "region": region, "size": offer.sku, "image": self.image_for(launch, offer),
                "ssh_keys": [int(key) if str(key).isdigit() else key], "tags": ["opengrid", name]}
        if launch.startup_script:
            body["user_data"] = launch.startup_script
        r = self.request("POST", "/v2/droplets", json=body)
        d = (r or {}).get("droplet") if isinstance(r, dict) else None
        if not isinstance(d, dict) or d.get("id") is None:
            return self.unknown(r, "digitalocean: create returned no droplet id")
        return self.accepted(d["id"], {"request": body, "response": r})

    def _state(self, d: dict) -> InstanceState:
        size = d.get("size") or {}
        gi = size.get("gpu_info") or d.get("gpu_info") or {}
        price = size.get("price_hourly")
        ip = next((n4.get("ip_address") for n4 in (d.get("networks") or {}).get("v4") or []
                   if n4.get("type") == "public"), None)
        st = d.get("status")
        return InstanceState(
            state=STATE.get(st, "unknown"), instance_id=str(d.get("id")), name=d.get("name"), provider_status=st,
            region=(d.get("region") or {}).get("slug"), gpu=gi.get("model"), gpu_count=gi.get("count"),
            price_per_hour=None if price is None else float(price), created_at=parse_time(d.get("created_at")),
            ip=ip, labels=[str(t) for t in d.get("tags") or []],
            raw_redacted={k: d.get(k) for k in ("id", "name", "status", "created_at", "tags", "size_slug")})

    def _status(self, instance_id):
        d = (self.request("GET", f"/v2/droplets/{instance_id}") or {}).get("droplet")
        if not isinstance(d, dict) or not d:
            raise AdapterError("parse", "digitalocean: get returned no droplet", sent=True)
        d.setdefault("id", instance_id)
        return self._state(d)

    def _terminate(self, instance_id):
        self.request("DELETE", f"/v2/droplets/{instance_id}", allow_text=True)
        return TerminateResult("accepted", "digitalocean: destroy accepted (confirm with status/list)", 204)

    def _droplets(self, params: dict) -> list[InstanceState]:
        out, page = [], 1
        for _ in range(100):
            body = self.request("GET", "/v2/droplets", params={**params, "per_page": 200, "page": page})
            if not isinstance(body, dict) or not isinstance(body.get("droplets"), list):
                raise AdapterError("parse", "digitalocean: list returned no droplets array", body=body, sent=True)
            out += [self._state(d) for d in body["droplets"] if isinstance(d, dict)]
            if not (((body.get("links") or {}).get("pages") or {}).get("next")):
                total = (body.get("meta") or {}).get("total")
                if isinstance(total, int) and total > len(out):
                    raise AdapterError("parse", f"digitalocean: list incomplete ({len(out)} of {total})", sent=True)
                return out
            page += 1
        raise AdapterError("parse", "digitalocean: more than 100 pages of droplets; refusing a partial list", sent=True)

    def _list(self):
        return self._droplets({})

    def _find(self, name):
        return self._droplets({"tag_name": name})
