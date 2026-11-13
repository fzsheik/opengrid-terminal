"""Wire contracts: the EXACT requests every execution adapter sends for quote / launch / status / terminate
(and list, stop, SSH key registration where it has them), pinned against each provider's documented API.

Each expected request is (method, scheme://host/path, query, headers, JSON body), with the API key shown as
<KEY>. The shapes come from the official references checked on 2026-11-13 and recorded, call by call, in
methodology/adapter-contract-audit.md. A change to any request (a renamed field, a new default, a moved
host) fails here and must be re-checked against the provider's docs before it ships.

Responses are minimal documented answers (httpx.MockTransport). Nothing touches the network.

Run:  .venv/bin/python tests/test_adapter_contracts.py
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routing.adapters.base import Availability, LaunchSpec, Offer  # noqa: E402
from routing.adapters.digitalocean import DigitalOceanAdapter  # noqa: E402
from routing.adapters.hyperstack import HyperstackAdapter  # noqa: E402
from routing.adapters.lambda_labs import LambdaAdapter  # noqa: E402
from routing.adapters.runpod import RunPodAdapter  # noqa: E402
from routing.adapters.shadeform import ShadeformAdapter  # noqa: E402
from routing.adapters.vast import VastAdapter  # noqa: E402
from routing.adapters.verda import VerdaAdapter  # noqa: E402

KEY = "contract-test-key-0123456789"
TOKEN = "verda-access-token-abcdef"
NAME = "og-dep-0a1b2c3d4e5f"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIContractTestKey ops@opengrid"
AUTH_HEADERS = ("authorization", "api_key", "x-api-key", "content-type")
JSON = {"content-type": "application/json"}


class Recorder:
    """(METHOD, path) -> (status, body); records every request as its wire contract."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.sent: list[tuple] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(wire(request))
        h = self.routes.get((request.method, request.url.path))
        if h is None:
            return httpx.Response(599, json={"error": f"unmocked {request.method} {request.url.path}"})
        status, body = h
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def take(self) -> list[tuple]:
        out, self.sent = self.sent, []
        return out


def mask(s: str) -> str:
    return s.replace(KEY, "<KEY>").replace(TOKEN, "<TOKEN>")


def wire(r: httpx.Request) -> tuple:
    # Every adapter identifies itself and asks for JSON (Verda's spec once required a user-agent).
    assert r.headers.get("accept") == "application/json" and r.headers.get("user-agent", "").startswith("opengrid"), r
    headers = {k: mask(r.headers[k]) for k in AUTH_HEADERS if k in r.headers}
    body = json.loads(mask(r.content.decode())) if r.content else None
    url = f"{r.url.scheme}://{r.url.host}{r.url.path}"
    return (r.method, url, dict(r.url.params.multi_items()), headers, body)


def check(label: str, got: list[tuple], want: list[tuple]) -> None:
    assert len(got) == len(want), f"{label}: {len(got)} requests sent, {len(want)} expected:\n" + "\n".join(map(str, got))
    for i, (g, w) in enumerate(zip(got, want)):
        for part, gv, wv in zip(("method", "url", "query", "headers", "body"), g, w):
            assert gv == wv, f"{label} request {i} {part}:\n  sent     {gv!r}\n  expected {wv!r}"


def offer(provider, **kw) -> Offer:
    base = dict(provider=provider, listing_id="L", sku="S", raw_gpu_name="R", gpu="G", gpu_count=1, region=None,
                price_per_gpu_hour=2.0, price_per_instance_hour=2.0)
    base.update(kw)
    return Offer(**base)


# --------------------------------------------------------------------------
# Lambda Cloud: https://cloud.lambda.com/api/v1/openapi.json (v1.10.0)
# --------------------------------------------------------------------------

def test_lambda_contract():
    B, A = "https://cloud.lambda.com/api/v1", {"authorization": "Bearer <KEY>"}
    inst = {"id": "i-1", "name": NAME, "status": "active", "region": {"name": "us-east-1"},
            "instance_type": {"name": "gpu_1x_a10", "price_cents_per_hour": 75, "specs": {"gpus": 1}},
            "ssh_key_names": [NAME], "first_healthy": "2026-11-13T10:00:00Z"}
    rec = Recorder({
        ("GET", "/api/v1/instance-types"): (200, {"data": {"gpu_1x_a10": {
            "instance_type": inst["instance_type"], "regions_with_capacity_available": [{"name": "us-east-1"}]}}}),
        ("POST", "/api/v1/ssh-keys"): (200, {"data": {"id": "k1", "name": NAME, "public_key": PUB}}),
        ("POST", "/api/v1/instance-operations/launch"): (200, {"data": {"instance_ids": ["i-1"]}}),
        ("GET", "/api/v1/instances/i-1"): (200, {"data": inst}),
        ("POST", "/api/v1/instance-operations/terminate"): (200, {"data": {"terminated_instances": [inst]}}),
        ("GET", "/api/v1/instances"): (200, {"data": [inst], "page_token": None}),
    })
    a = LambdaAdapter({"api_key": KEY}, transport=httpx.MockTransport(rec))
    o = offer("lambda", sku="gpu_1x_a10", region="us-east-1")
    av = a.check_availability(o)
    check("lambda quote", rec.take(), [("GET", f"{B}/instance-types", {}, A, None)])
    assert a.provision(o, av, LaunchSpec(ssh_public_key=PUB, startup_script="#cloud-config\n"), NAME).outcome == "accepted"
    check("lambda launch", rec.take(), [
        ("POST", f"{B}/ssh-keys", {}, {**A, **JSON}, {"name": NAME, "public_key": PUB}),
        ("POST", f"{B}/instance-operations/launch", {}, {**A, **JSON},
         {"region_name": "us-east-1", "instance_type_name": "gpu_1x_a10", "ssh_key_names": [NAME], "name": NAME,
          "tags": [{"key": "opengrid", "value": NAME}], "user_data": "#cloud-config\n"})])
    assert a.status("i-1").state == "running"
    check("lambda status", rec.take(), [("GET", f"{B}/instances/i-1", {}, A, None)])
    assert a.terminate("i-1").outcome == "accepted"
    check("lambda terminate", rec.take(), [
        ("POST", f"{B}/instance-operations/terminate", {}, {**A, **JSON}, {"instance_ids": ["i-1"]})])
    a.list_instances()
    check("lambda list", rec.take(), [("GET", f"{B}/instances", {}, A, None)])   # no page_size: every instance


# --------------------------------------------------------------------------
# RunPod REST v2: https://api.runpod.io/v2/openapi.json (2.0.0; v1 retired 2026-11-15)
# --------------------------------------------------------------------------

def test_runpod_contract():
    B, A = "https://api.runpod.io/v2", {"authorization": "Bearer <KEY>", **JSON}
    pod = {"id": "pod1", "name": NAME, "status": "RUNNING", "cost": 0.98, "gpu": {"id": "NVIDIA A40", "count": 2}}
    rec = Recorder({
        ("GET", "/v2/catalog/gpus"): (200, {"gpus": [{"id": "NVIDIA A40", "price": {"secure": 0.49, "community": 0.3},
                                                      "availability": "HIGH"}]}),
        ("POST", "/v2/pods"): (201, {**pod, "status": "PROVISIONING"}),
        ("GET", "/v2/pods/pod1"): (200, pod),
        ("DELETE", "/v2/pods/pod1"): (204, None),
        ("POST", "/v2/pods/pod1/action"): (200, {**pod, "status": "EXITED"}),
        ("GET", "/v2/pods"): (200, {"pods": [pod], "pagination": {"nextCursor": None, "hasNextPage": False}}),
    })
    a = RunPodAdapter({"api_key": KEY}, transport=httpx.MockTransport(rec))
    o = offer("runpod", raw_gpu_name="NVIDIA A40", gpu_count=2)
    av = a.check_availability(o)
    check("runpod quote", rec.take(), [("GET", f"{B}/catalog/gpus", {"include": "AVAILABILITY", "product": "POD",
                                                                     "count": "2", "cloud": "SECURE"}, A, None)])
    launch = LaunchSpec(image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04", ssh_public_key=PUB,
                        disk_gb=50, env={"HF_HOME": "/workspace/hf"})
    assert a.provision(o, av, launch, NAME).outcome == "accepted"
    check("runpod launch", rec.take(), [
        ("POST", f"{B}/pods", {}, A,
         {"name": NAME, "image": launch.image, "gpu": {"id": "NVIDIA A40", "count": 2}, "cloud": "SECURE",
          "ports": ["22/tcp"], "disk": 50, "env": {"HF_HOME": "/workspace/hf", "PUBLIC_KEY": PUB}})])
    assert a.status("pod1").state == "running"
    check("runpod status", rec.take(), [("GET", f"{B}/pods/pod1", {}, A, None)])
    assert a.terminate("pod1").outcome == "accepted"
    check("runpod terminate", rec.take(), [("DELETE", f"{B}/pods/pod1", {}, A, None)])
    assert a.stop("pod1").outcome == "accepted"
    check("runpod stop", rec.take(), [("POST", f"{B}/pods/pod1/action", {}, A, {"action": "stop"})])
    a.list_instances()
    check("runpod list", rec.take(), [("GET", f"{B}/pods", {"limit": "1000"}, A, None)])


# --------------------------------------------------------------------------
# Hyperstack: https://docs.hyperstack.cloud/openapi/hyperstack.json
# --------------------------------------------------------------------------

def test_hyperstack_contract():
    B, A = "https://infrahub-api.nexgencloud.com/v1", {"api_key": "<KEY>"}
    vm = {"id": 123, "name": NAME, "status": "ACTIVE", "labels": ["opengrid", NAME],
          "flavor": {"gpu": "A100-80G-PCIe", "gpu_count": 1}, "environment": {"name": "env-ca", "region": "CANADA-1"}}
    rec = Recorder({
        ("GET", "/v1/core/flavors"): (200, {"status": True, "data": [{"gpu": "A100-80G-PCIe", "region_name": "CANADA-1",
            "flavors": [{"name": "n3-A100x1", "region_name": "CANADA-1", "gpu": "A100-80G-PCIe", "gpu_count": 1,
                         "stock_available": True}]}]}),
        ("GET", "/v1/pricebook"): (200, [{"name": "A100-80G-PCIe", "value": "1.35"}]),
        ("POST", "/v1/core/keypairs"): (200, {"status": True, "keypair": {"id": 5, "name": NAME, "fingerprint": "ab"}}),
        ("POST", "/v1/core/virtual-machines"): (200, {"status": True, "instances": [{**vm, "status": "CREATING"}]}),
        ("GET", "/v1/core/virtual-machines/123"): (200, {"status": True, "instance": vm}),
        ("DELETE", "/v1/core/virtual-machines/123"): (200, {"status": True, "message": "Instance is being deleted."}),
        ("GET", "/v1/core/virtual-machines"): (200, {"status": True, "instances": [vm], "count": 1, "page": 1,
                                                     "page_size": 100}),
    })
    a = HyperstackAdapter({"api_key": KEY}, transport=httpx.MockTransport(rec))
    o = offer("hyperstack", sku="n3-A100x1", region="CANADA-1")
    av = a.check_availability(o)
    assert av.available and av.list_price_per_gpu_hour == 1.35
    check("hyperstack quote", rec.take(), [("GET", f"{B}/core/flavors", {"region": "CANADA-1"}, A, None),
                                           ("GET", f"{B}/pricebook", {}, A, None)])
    launch = LaunchSpec.merged({"ssh_public_key": PUB}, {"image": "Ubuntu Server 22.04 LTS R550 CUDA 12.4 with Docker",
                                                         "environments": {"CANADA-1": "env-ca"}})
    assert a.provision(o, av, launch, NAME).outcome == "accepted"
    check("hyperstack launch", rec.take(), [
        ("POST", f"{B}/core/keypairs", {}, {**A, **JSON}, {"environment_name": "env-ca", "name": NAME, "public_key": PUB}),
        ("POST", f"{B}/core/virtual-machines", {}, {**A, **JSON},
         {"name": NAME, "environment_name": "env-ca", "image_name": launch.image, "flavor_name": "n3-A100x1",
          "key_name": NAME, "count": 1, "assign_floating_ip": True, "labels": ["opengrid", NAME],
          "security_rules": [{"direction": "ingress", "ethertype": "IPv4", "protocol": "tcp",
                              "remote_ip_prefix": "0.0.0.0/0", "port_range_min": 22, "port_range_max": 22}]})])
    assert a.status("123").state == "running"
    check("hyperstack status", rec.take(), [("GET", f"{B}/core/virtual-machines/123", {}, A, None)])
    assert a.terminate("123").outcome == "accepted"
    check("hyperstack terminate", rec.take(), [("DELETE", f"{B}/core/virtual-machines/123", {}, A, None)])
    a.list_instances()
    check("hyperstack list", rec.take(), [("GET", f"{B}/core/virtual-machines", {"page": "1", "pageSize": "100"}, A, None)])


# --------------------------------------------------------------------------
# DigitalOcean: github.com/digitalocean/openapi (DigitalOcean-public.v2.yaml)
# --------------------------------------------------------------------------

def test_digitalocean_contract():
    B, A = "https://api.digitalocean.com/v2", {"authorization": "Bearer <KEY>"}
    d = {"id": 3164444, "name": NAME, "status": "active", "tags": ["opengrid", NAME], "region": {"slug": "tor1"},
         "size": {"price_hourly": 3.39, "gpu_info": {"count": 1}}}
    rec = Recorder({
        ("GET", "/v2/sizes"): (200, {"sizes": [{"slug": "gpu-h100x1-80gb", "price_hourly": 3.39, "available": True,
                                                "regions": ["tor1"], "gpu_info": {"count": 1}}], "links": {}, "meta": {"total": 1}}),
        ("POST", "/v2/account/keys"): (201, {"ssh_key": {"id": 512190, "fingerprint": "3b:16", "public_key": PUB, "name": NAME}}),
        ("POST", "/v2/droplets"): (202, {"droplet": {**d, "status": "new"}, "links": {"actions": []}}),
        ("GET", "/v2/droplets/3164444"): (200, {"droplet": d}),
        ("DELETE", "/v2/droplets/3164444"): (204, None),
        ("GET", "/v2/droplets"): (200, {"droplets": [d], "links": {}, "meta": {"total": 1}}),
    })
    a = DigitalOceanAdapter({"api_key": KEY}, transport=httpx.MockTransport(rec))
    o = offer("digitalocean", sku="gpu-h100x1-80gb")
    av = a.check_availability(o)
    check("digitalocean quote", rec.take(), [("GET", f"{B}/sizes", {"per_page": "200"}, A, None)])
    launch = LaunchSpec.merged({"ssh_public_key": PUB, "startup_script": "#!/bin/bash\necho ok\n"},
                               {"images": {"gpu-h100x1-80gb": "gpu-h100x1-base"}})
    assert a.provision(o, av, launch, NAME).outcome == "accepted"
    check("digitalocean launch", rec.take(), [
        ("POST", f"{B}/account/keys", {}, {**A, **JSON}, {"name": NAME, "public_key": PUB}),
        ("POST", f"{B}/droplets", {}, {**A, **JSON},
         {"name": NAME, "region": "tor1", "size": "gpu-h100x1-80gb", "image": "gpu-h100x1-base", "ssh_keys": [512190],
          "tags": ["opengrid", NAME], "user_data": "#!/bin/bash\necho ok\n"})])
    assert a.status("3164444").state == "running"
    check("digitalocean status", rec.take(), [("GET", f"{B}/droplets/3164444", {}, A, None)])
    assert a.terminate("3164444").outcome == "accepted"
    check("digitalocean terminate", rec.take(), [("DELETE", f"{B}/droplets/3164444", {}, A, None)])
    a.list_instances()
    check("digitalocean list", rec.take(), [
        ("GET", f"{B}/droplets", {"type": "gpus", "per_page": "200", "page": "1"}, A, None)])
    a.find_instance(NAME)       # tag_name cannot be combined with type: tag alone (then the type=gpus list if empty)
    check("digitalocean find", rec.take(), [
        ("GET", f"{B}/droplets", {"tag_name": NAME, "per_page": "200", "page": "1"}, A, None)])


# --------------------------------------------------------------------------
# Shadeform (Crusoe / Denvr / Latitude): https://api.shadeform.ai/v1/openapi.json
# --------------------------------------------------------------------------

def test_shadeform_contract():
    B, A = "https://api.shadeform.ai/v1", {"x-api-key": "<KEY>"}
    i = {"id": "sf-1", "cloud": "crusoe", "name": NAME, "status": "active", "region": "culpeper-usa-1",
         "tags": ["opengrid", NAME], "hourly_price": 210, "configuration": {"num_gpus": 1}}
    rec = Recorder({
        ("GET", "/v1/instances/types"): (200, {"instance_types": [{"cloud": "crusoe", "shade_instance_type": "A100_80G",
            "hourly_price": 210, "num_gpus": 1,
            "availability": [{"region": "culpeper-usa-1", "available": True, "rental_type": "on_demand"}]}]}),
        ("POST", "/v1/sshkeys/add"): (200, {"id": "key-uuid"}),
        ("POST", "/v1/instances/create"): (200, {"id": "sf-1", "status": "creating"}),
        ("GET", "/v1/instances/sf-1/info"): (200, i),
        ("POST", "/v1/instances/sf-1/delete"): (200, None),
        ("GET", "/v1/instances"): (200, {"instances": [i], "total": 1}),
    })
    a = ShadeformAdapter({"api_key": KEY}, transport=httpx.MockTransport(rec), provider="crusoe")
    o = offer("crusoe", listing_id="A100_80G")
    av = a.check_availability(o)
    check("shadeform quote", rec.take(), [
        ("GET", f"{B}/instances/types", {"cloud": "crusoe", "shade_instance_type": "A100_80G"}, A, None)])
    launch = LaunchSpec(ssh_public_key=PUB, image="ubuntu22.04_cuda12.8_shade_os", startup_script="echo hi",
                        env={"HF_HOME": "/data/hf"})
    assert a.provision(o, av, launch, NAME).outcome == "accepted"
    check("shadeform launch", rec.take(), [
        ("POST", f"{B}/sshkeys/add", {}, {**A, **JSON}, {"name": NAME, "public_key": PUB}),
        ("POST", f"{B}/instances/create", {}, {**A, **JSON},
         {"cloud": "crusoe", "region": "culpeper-usa-1", "shade_instance_type": "A100_80G", "shade_cloud": True,
          "name": NAME, "tags": ["opengrid", NAME], "ssh_key_id": "key-uuid", "os": "ubuntu22.04_cuda12.8_shade_os",
          "launch_configuration": {"type": "script", "script_configuration": {"base64_script": "ZWNobyBoaQ=="}},
          "envs": [{"name": "HF_HOME", "value": "/data/hf"}]})])
    assert a.status("sf-1").state == "running"
    check("shadeform status", rec.take(), [("GET", f"{B}/instances/sf-1/info", {}, A, None)])
    assert a.terminate("sf-1").outcome == "accepted"
    check("shadeform terminate", rec.take(), [("POST", f"{B}/instances/sf-1/delete", {}, A, None)])
    a.list_instances()
    check("shadeform list", rec.take(), [("GET", f"{B}/instances", {}, A, None)])


# --------------------------------------------------------------------------
# Vast.ai: https://docs.vast.ai/api-reference/openapi.yaml
# --------------------------------------------------------------------------

def test_vast_contract():
    B, A = "https://console.vast.ai/api", {"authorization": "Bearer <KEY>"}
    inst = {"id": 7777, "label": NAME, "actual_status": "running", "num_gpus": 1, "dph_total": 0.35}
    rec = Recorder({
        ("POST", "/api/v0/bundles/"): (200, {"offers": [{"id": 111, "num_gpus": 1, "dph_total": 0.35,
                                                         "geolocation": "Texas, US", "machine_id": 9}]}),
        ("PUT", "/api/v0/asks/111/"): (200, {"success": True, "new_contract": 7777}),
        ("POST", "/api/v0/instances/7777/ssh/"): (200, {"success": True, "msg": "ok"}),
        ("GET", "/api/v0/instances/7777/"): (200, {"instances": inst}),
        ("DELETE", "/api/v0/instances/7777/"): (200, {"success": True, "msg": "Instance destroyed successfully"}),
        ("PUT", "/api/v0/instances/7777/"): (200, {"success": True}),
        ("GET", "/api/v1/instances"): (200, {"instances": [inst], "next_token": None, "total_instances": 1}),
    })
    a = VastAdapter({"api_key": KEY}, transport=httpx.MockTransport(rec))
    o = offer("vast", sku="RTX 4090")
    av = a.check_availability(o)
    check("vast quote", rec.take(), [
        ("POST", f"{B}/v0/bundles/", {}, {**A, **JSON},
         {"verified": {"eq": True}, "rentable": {"eq": True}, "type": "on-demand", "gpu_name": {"eq": "RTX 4090"},
          "num_gpus": {"eq": 1}, "allocated_storage": 32, "order": [["dph_total", "asc"]], "limit": 20})])
    launch = LaunchSpec(image="pytorch/pytorch:latest", ssh_public_key=PUB, startup_script="echo hi")
    assert a.provision(o, av, launch, NAME).outcome == "accepted"
    check("vast launch", rec.take(), [
        ("PUT", f"{B}/v0/asks/111/", {}, {**A, **JSON},
         {"client_id": "me", "image": "pytorch/pytorch:latest", "disk": 32, "label": NAME, "runtype": "ssh",
          "onstart": "echo hi"}),
        ("POST", f"{B}/v0/instances/7777/ssh/", {}, {**A, **JSON}, {"ssh_key": PUB})])
    assert a.status("7777").state == "running"
    check("vast status", rec.take(), [("GET", f"{B}/v0/instances/7777/", {}, A, None)])
    assert a.terminate("7777").outcome == "accepted"
    check("vast terminate", rec.take(), [("DELETE", f"{B}/v0/instances/7777/", {}, A, None)])
    assert a.stop("7777").outcome == "accepted"
    check("vast stop", rec.take(), [("PUT", f"{B}/v0/instances/7777/", {}, {**A, **JSON}, {"state": "stopped"})])
    a.list_instances()
    check("vast list", rec.take(), [("GET", f"{B}/v1/instances", {"limit": "25"}, A, None)])


# --------------------------------------------------------------------------
# Verda: https://api.verda.com/v1/openapi.json
# --------------------------------------------------------------------------

def test_verda_contract():
    B, A = "https://api.verda.com/v1", {"authorization": "Bearer <TOKEN>"}
    iid = "4fc4b5b8-0d6e-4b0b-9f2b-1d0c2c8e6f11"
    inst = {"id": iid, "hostname": NAME, "status": "running", "location": "FIN-01", "price_per_hour": 2.49,
            "os_volume_id": "vol-os", "volume_ids": ["vol-data"], "tags": [{"id": "t1", "key": "opengrid", "value": NAME}],
            "gpu": {"number_of_gpus": 1}, "instance_type": "1H100.80S.30V"}
    rec = Recorder({
        ("POST", "/v1/oauth2/token"): (200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600,
                                             "refresh_token": "r", "scope": "cloud-api-v1"}),
        ("GET", "/v1/instance-availability"): (200, [{"location_code": "FIN-01", "availabilities": ["1H100.80S.30V"]}]),
        ("GET", "/v1/instance-types"): (200, [{"instance_type": "1H100.80S.30V", "price_per_hour": "2.19",
                                               "gpu": {"number_of_gpus": 1},
                                               "location_prices": [{"location_code": "FIN-01", "price_per_hour": "2.49",
                                                                    "spot_price": "0.9"}]}]),
        ("POST", "/v1/ssh-keys"): (201, "key-uuid"),
        ("POST", "/v1/instances"): (202, iid),
        ("GET", f"/v1/instances/{iid}"): (200, inst),
        ("PUT", "/v1/instances"): (202, [{"instanceId": iid, "action": "delete", "status": "success", "statusCode": 202}]),
        ("GET", "/v1/instances"): (200, [inst]),
    })
    a = VerdaAdapter({"client_id": "cid", "client_secret": KEY}, transport=httpx.MockTransport(rec))
    o = offer("verda", sku="1H100.80S.30V")
    av = a.check_availability(o)
    assert av.list_price_per_gpu_hour == 2.49, "the location's price, not the deprecated default"
    check("verda quote", rec.take(), [
        ("POST", f"{B}/oauth2/token", {}, JSON, {"grant_type": "client_credentials", "client_id": "cid",
                                                 "client_secret": "<KEY>"}),
        ("GET", f"{B}/instance-availability", {}, A, None),
        ("GET", f"{B}/instance-types", {}, A, None)])
    assert a.provision(o, av, LaunchSpec(ssh_public_key=PUB, image="ubuntu-24.04-cuda-12.8-open-docker"), NAME).outcome == "accepted"
    check("verda launch", rec.take(), [
        ("POST", f"{B}/ssh-keys", {}, {**A, **JSON}, {"name": NAME, "key": PUB}),
        ("POST", f"{B}/instances", {}, {**A, **JSON},
         {"instance_type": "1H100.80S.30V", "image": "ubuntu-24.04-cuda-12.8-open-docker", "hostname": NAME,
          "location_code": "FIN-01", "ssh_key_ids": ["key-uuid"], "description": f"OpenGrid {NAME}", "is_spot": False,
          "tags": [{"key": "opengrid", "value": NAME}]})])
    assert a.status(iid).state == "running"
    check("verda status", rec.take(), [("GET", f"{B}/instances/{iid}", {}, A, None)])
    assert a.terminate(iid).outcome == "accepted"
    check("verda terminate", rec.take(), [
        ("GET", f"{B}/instances/{iid}", {}, A, None),
        ("PUT", f"{B}/instances", {}, {**A, **JSON}, {"action": "delete", "id": iid, "delete_permanently": True,
                                                       "volume_ids": ["vol-os", "vol-data"]})])
    a.list_instances()
    check("verda list", rec.take(), [("GET", f"{B}/instances", {}, A, None)])
    a.find_instance(NAME)
    check("verda find", rec.take(), [("GET", f"{B}/instances", {"tag": f"opengrid={NAME}"}, A, None)])


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
