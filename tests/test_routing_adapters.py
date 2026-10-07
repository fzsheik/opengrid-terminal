"""Every real routing adapter against httpx.MockTransport, using the providers' documented
request/response shapes: availability, quote, provision, status, stop, terminate, and the
error paths (no capacity, auth failure, timeout). Nothing touches the network.

Run:  .venv/Scripts/python tests/test_routing_adapters.py
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routing import adapters, capabilities
from routing.adapters.base import AdapterError, Availability, LaunchSpec, Offer
from routing.adapters.digitalocean import DigitalOceanAdapter
from routing.adapters.hyperstack import HyperstackAdapter
from routing.adapters.lambda_labs import LambdaAdapter
from routing.adapters.runpod import RunPodAdapter
from routing.adapters.shadeform import ShadeformAdapter
from routing.adapters.vast import VastAdapter
from routing.adapters.verda import VerdaAdapter


class Mock:
    """Routes (METHOD, path) to (status, body) or a callable(request) -> httpx.Response; records requests."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        h = self.routes.get((request.method, request.url.path))
        if h is None:
            return httpx.Response(599, json={"error": f"unmocked {request.method} {request.url.path}"})
        if callable(h):
            return h(request)
        status, body = h
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def transport(self):
        return httpx.MockTransport(self)

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


def timeout(request):
    raise httpx.ReadTimeout("timed out", request=request)


def kind_of(fn, *a):
    try:
        fn(*a)
    except AdapterError as e:
        return e.kind
    raise AssertionError("expected an AdapterError")


def offer(provider, **kw):
    base = dict(provider=provider, listing_id="L", sku="S", raw_gpu_name="R", gpu="G", gpu_count=1, region=None,
                price_per_gpu_hour=2.0, price_per_instance_hour=2.0)
    base.update(kw)
    return Offer(**base)


LAUNCH = LaunchSpec(ssh_key="opengrid", image="img")


# --------------------------------------------------------------------------

def test_lambda():
    types = {"data": {"gpu_1x_h100_sxm5": {
        "instance_type": {"name": "gpu_1x_h100_sxm5", "price_cents_per_hour": 329, "specs": {"gpus": 1}},
        "regions_with_capacity_available": [{"name": "us-east-1", "description": "Virginia, USA"}]}}}
    m = Mock({
        ("GET", "/api/v1/instance-types"): (200, types),
        ("POST", "/api/v1/instance-operations/launch"): (200, {"data": {"instance_ids": ["0920582c"]}}),
        ("GET", "/api/v1/instances/0920582c"): (200, {"data": {
            "id": "0920582c", "status": "active", "ip": "198.51.100.2", "region": {"name": "us-east-1"},
            "instance_type": {"name": "gpu_1x_h100_sxm5", "price_cents_per_hour": 329, "specs": {"gpus": 1}}}}),
        ("POST", "/api/v1/instance-operations/terminate"): (200, {"data": {"terminated_instances": [
            {"id": "0920582c", "status": "terminating"}]}}),
    })
    a = LambdaAdapter({"api_key": "k"}, transport=m.transport())
    o = offer("lambda", sku="gpu_1x_h100_sxm5", region="us-east-1")
    av = a.check_availability(o)
    assert av.available and av.live and av.region == "us-east-1" and av.list_price_per_gpu_hour == 3.29
    assert m.requests[0].headers["authorization"] == "Bearer k"
    q = a.quote(o, av)
    assert q.basis == "live_provider_api" and q.price_per_gpu_hour == 3.29
    assert a.check_availability(offer("lambda", sku="gone")).available is False
    eu = a.check_availability(offer("lambda", sku="gpu_1x_h100_sxm5", want_region_group="Europe"))
    assert eu.available is False and eu.region is None, "never launch outside the region constraint"
    assert a.missing_launch(LaunchSpec(), o) == ["ssh_key"]
    inst = a.provision(o, av, LAUNCH, "opengrid-dep_1")
    assert inst.instance_id == "0920582c" and inst.status == "provisioning"
    assert m.body() == {"region_name": "us-east-1", "instance_type_name": "gpu_1x_h100_sxm5",
                        "ssh_key_names": ["opengrid"], "name": "opengrid-dep_1"}
    st = a.status("0920582c")
    assert st.status == "running" and st.ip == "198.51.100.2" and st.price_per_gpu_hour == 3.29
    assert a.terminate("0920582c").status == "terminating" and m.body() == {"instance_ids": ["0920582c"]}
    assert kind_of(a.stop, "x") == "config", "Lambda has no stop"

    cap = Mock({("POST", "/api/v1/instance-operations/launch"): (400, {"error": {
        "code": "instance-operations/launch/insufficient-capacity",
        "message": "Not enough capacity to fulfill launch request.", "suggestion": "Choose another region"}}),
        ("GET", "/api/v1/instance-types"): (401, {"error": {"code": "global/invalid-api-key",
                                                           "message": "API key was invalid, expired, or deleted."}}),
        ("GET", "/api/v1/instances/slow"): timeout})
    b = LambdaAdapter({"api_key": "k"}, transport=cap.transport())
    assert kind_of(b.provision, o, av, LAUNCH, "n") == "capacity"
    assert kind_of(b.check_availability, o) == "auth"
    assert kind_of(b.status, "slow") == "timeout"


def test_runpod():
    def graphql(req):
        q = json.loads(req.content)["query"]
        assert '"NVIDIA GeForce RTX 4090"' in q and "gpuCount: 1" in q and "secureCloud: true" in q
        return httpx.Response(200, json={"data": {"gpuTypes": [{"id": "NVIDIA GeForce RTX 4090",
                                                               "lowestPrice": {"uninterruptablePrice": 0.69,
                                                                               "stockStatus": "High"}}]}})

    m = Mock({
        ("POST", "/graphql"): graphql,
        ("POST", "/v1/pods"): (201, {"id": "xedezhzb9la3ye", "desiredStatus": "RUNNING", "costPerHr": 0.69,
                                     "publicIp": "", "machineId": "m1"}),
        ("GET", "/v1/pods/xedezhzb9la3ye"): (200, {"id": "xedezhzb9la3ye", "desiredStatus": "EXITED",
                                                   "costPerHr": 0.69, "gpuCount": 1}),
        ("POST", "/v1/pods/xedezhzb9la3ye/stop"): (200, {"id": "xedezhzb9la3ye", "desiredStatus": "EXITED"}),
        ("DELETE", "/v1/pods/xedezhzb9la3ye"): (204, None),
    })
    a = RunPodAdapter({"api_key": "rk"}, transport=m.transport())
    o = offer("runpod", raw_gpu_name="NVIDIA GeForce RTX 4090", provider_tier="secure")
    av = a.check_availability(o)
    assert av.available and av.list_price_per_gpu_hour == 0.69 and m.requests[0].url.host == "api.runpod.io"
    assert a.missing_launch(LaunchSpec(), o) == ["image"], "a pod needs a container image"
    inst = a.provision(o, av, LaunchSpec(image="runpod/pytorch:test", disk_gb=40, env={"A": "1"}), "opengrid-x")
    b = m.body()
    assert m.requests[-1].url.host == "rest.runpod.io" and m.requests[-1].headers["authorization"] == "Bearer rk"
    assert b["gpuTypeIds"] == ["NVIDIA GeForce RTX 4090"] and b["cloudType"] == "SECURE" and b["gpuCount"] == 1
    assert b["imageName"] == "runpod/pytorch:test" and b["interruptible"] is False and b["containerDiskInGb"] == 40
    assert inst.instance_id == "xedezhzb9la3ye" and inst.status == "running" and inst.price_per_gpu_hour == 0.69
    assert inst.metadata["launch_request"]["env"] == ["A"], "env values (possible secrets) are not kept"
    assert a.status("xedezhzb9la3ye").status == "stopped"
    assert a.stop("xedezhzb9la3ye").status == "stopped"
    assert a.terminate("xedezhzb9la3ye").status == "terminated"

    none = Mock({("POST", "/graphql"): (200, {"data": {"gpuTypes": [{"id": "x", "lowestPrice": {
        "uninterruptablePrice": None, "stockStatus": None}}]}})})
    assert RunPodAdapter({"api_key": "k"}, transport=none.transport()).check_availability(o).available is False
    errs = Mock({("POST", "/graphql"): (200, {"errors": [{"message": "bad"}]}),
                 ("POST", "/v1/pods"): (500, {"error": "create pod: There are no longer any instances available "
                                                       "with the requested specifications. Please refresh and try again."}),
                 ("GET", "/v1/pods/p"): (401, {"error": "unauthorized"})})
    e = RunPodAdapter({"api_key": "k"}, transport=errs.transport())
    assert kind_of(e.check_availability, o) == "provider_error", "GraphQL 200 with errors is a failure"
    assert kind_of(e.provision, o, av, LAUNCH, "n") == "capacity"
    assert kind_of(e.status, "p") == "auth"


def test_hyperstack():
    flavors = {"status": True, "data": [{"gpu": "H100-80G-PCIe", "region_name": "CANADA-1", "flavors": [
        {"name": "n3-H100x1", "region_name": "CANADA-1", "gpu": "H100-80G-PCIe", "gpu_count": 1,
         "stock_available": True}]}]}
    m = Mock({
        ("GET", "/v1/core/flavors"): (200, flavors),
        ("GET", "/v1/pricebook"): (200, [{"name": "H100-80G-PCIe", "value": "1.90"}]),
        ("POST", "/v1/core/virtual-machines"): (200, {"status": True, "message": "Creating 1 virtual machine(s)",
                                                      "instances": [{"id": 123, "name": "opengrid-x", "status": "CREATING",
                                                                     "environment": {"name": "env-ca", "region": "CANADA-1"}}]}),
        ("GET", "/v1/core/virtual-machines/123"): (200, {"status": True, "instance": {"id": 123, "status": "ACTIVE",
                                                                                      "floating_ip": "203.0.113.5"}}),
        ("GET", "/v1/core/virtual-machines/123/stop"): (200, {"status": True, "message": "stopping"}),
        ("DELETE", "/v1/core/virtual-machines/123"): (200, {"status": True, "message": "deleting"}),
    })
    a = HyperstackAdapter({"api_key": "hk"}, transport=m.transport())
    o = offer("hyperstack", sku="n3-H100x1", region="CANADA-1")
    av = a.check_availability(o)
    assert av.available and av.region == "CANADA-1" and av.list_price_per_gpu_hour == 1.90
    assert m.requests[0].headers["api_key"] == "hk" and m.requests[0].url.params["region"] == "CANADA-1"
    assert "environment for region CANADA-1" in a.missing_launch(LAUNCH, o)
    launch = LaunchSpec.merged({"ssh_key": "k1"}, {"image": "Ubuntu Server 22.04 LTS", "environments": {"CANADA-1": "env-ca"}})
    assert a.missing_launch(launch, o) == []
    inst = a.provision(o, av, launch, "opengrid-x")
    assert m.body() == {"name": "opengrid-x", "environment_name": "env-ca", "image_name": "Ubuntu Server 22.04 LTS",
                        "flavor_name": "n3-H100x1", "key_name": "k1", "count": 1, "assign_floating_ip": True}
    assert inst.instance_id == "123" and inst.status == "provisioning"
    st = a.status("123")
    assert st.status == "running" and st.ip == "203.0.113.5"
    assert a.stop("123").status == "stopped" and a.terminate("123").status == "terminating"

    errs = Mock({("POST", "/v1/core/virtual-machines"): (200, {"status": False, "message":
                 "Insufficient resources available for flavor n3-H100x1"}),
                 ("GET", "/v1/core/flavors"): (401, {"status": False, "message": "Invalid API key"}),
                 ("GET", "/v1/core/virtual-machines/9"): timeout})
    e = HyperstackAdapter({"api_key": "x"}, transport=errs.transport())
    assert kind_of(e.provision, o, av, launch, "n") == "capacity", "200 + status false is a failure"
    assert kind_of(e.check_availability, o) == "auth"
    assert kind_of(e.status, "9") == "timeout"


def test_digitalocean():
    sizes = {"sizes": [{"slug": "gpu-h100x1-80gb", "price_hourly": 3.39, "available": True,
                        "regions": ["nyc2", "tor1"], "gpu_info": {"count": 1, "model": "nvidia_h100"}}],
             "links": {}, "meta": {"total": 1}}
    m = Mock({
        ("GET", "/v2/sizes"): (200, sizes),
        ("POST", "/v2/droplets"): (202, {"droplet": {"id": 3164444, "name": "opengrid-x", "status": "new",
                                                     "region": {"slug": "tor1"},
                                                     "size": {"price_hourly": 3.39, "gpu_info": {"count": 1}}}}),
        ("GET", "/v2/droplets/3164444"): (200, {"droplet": {"id": 3164444, "status": "active", "region": {"slug": "tor1"},
                                                            "size": {"price_hourly": 3.39, "gpu_info": {"count": 1}},
                                                            "networks": {"v4": [{"ip_address": "10.1.1.1", "type": "private"},
                                                                                {"ip_address": "192.0.2.9", "type": "public"}]}}}),
        ("POST", "/v2/droplets/3164444/actions"): (201, {"action": {"id": 1, "type": "power_off"}}),
        ("DELETE", "/v2/droplets/3164444"): (204, None),
    })
    a = DigitalOceanAdapter({"api_key": "dk"}, transport=m.transport())
    o = offer("digitalocean", sku="gpu-h100x1-80gb", region="nyc2,tor1", want_region_group="Canada")
    av = a.check_availability(o)
    assert av.available and av.region == "tor1" and av.list_price_per_gpu_hour == 3.39
    inst = a.provision(o, av, LaunchSpec(ssh_key="512189", image="gpu-h100x1-base"), "opengrid-x")
    assert m.body() == {"name": "opengrid-x", "region": "tor1", "size": "gpu-h100x1-80gb", "image": "gpu-h100x1-base",
                        "ssh_keys": [512189], "tags": ["opengrid"]}
    assert inst.instance_id == "3164444" and inst.status == "provisioning"
    st = a.status("3164444")
    assert st.status == "running" and st.ip == "192.0.2.9" and st.price_per_gpu_hour == 3.39
    assert a.stop("3164444").status == "stopped" and m.body() == {"type": "power_off"}
    assert a.terminate("3164444").status == "terminated"

    errs = Mock({("POST", "/v2/droplets"): (422, {"id": "unprocessable_entity",
                                                 "message": "Size is not available in this region."}),
                 ("GET", "/v2/sizes"): (401, {"id": "Unauthorized", "message": "Unable to authenticate you"}),
                 ("GET", "/v2/droplets/1"): timeout})
    e = DigitalOceanAdapter({"api_key": "x"}, transport=errs.transport())
    assert kind_of(e.provision, o, av, LAUNCH, "n") == "capacity"
    assert kind_of(e.check_availability, o) == "auth"
    assert kind_of(e.status, "1") == "timeout"


def test_shadeform():
    types = {"instance_types": [{"cloud": "crusoe", "shade_instance_type": "H100_sxm5x8", "num_gpus": 8,
                                 "hourly_price": 2780, "availability": [
                                     {"region": "us-east1-a", "available": True, "display_name": "US, Virginia"},
                                     {"region": "eu-iceland1-a", "available": False}]}]}
    m = Mock({
        ("GET", "/v1/instances/types"): (200, types),
        ("POST", "/v1/instances/create"): (200, {"id": "d290f1ee-6c54-4b01-90e6-d701748f0851",
                                                 "cloud_assigned_id": "13b057d7"}),
        ("GET", "/v1/instances/d290f1ee-6c54-4b01-90e6-d701748f0851/info"): (200, {
            "id": "d290f1ee-6c54-4b01-90e6-d701748f0851", "cloud": "crusoe", "region": "us-east1-a",
            "status": "active", "hourly_price": 2780, "ip": "203.0.113.20",
            "configuration": {"num_gpus": 8, "gpu_type": "H100"}}),
        ("POST", "/v1/instances/d290f1ee-6c54-4b01-90e6-d701748f0851/delete"): (200, None),
    })
    public = ShadeformAdapter(None, transport=m.transport(), provider="crusoe")
    o = offer("crusoe", listing_id="H100_sxm5x8", gpu_count=8)
    av = public.check_availability(o)
    assert av.available and av.region == "us-east1-a" and abs(av.list_price_per_gpu_hour - 3.475) < 1e-9
    assert "x-api-key" not in m.requests[0].headers, "the catalogue is public; no key needed or sent"
    assert m.requests[0].url.params["cloud"] == "crusoe"
    assert public.missing_credentials() == ["api_key"], "launching does need the Shadeform key"
    a = ShadeformAdapter({"api_key": "sk"}, transport=m.transport(), provider="crusoe")
    inst = a.provision(o, av, LaunchSpec(), "opengrid-x")
    assert m.requests[-1].headers["x-api-key"] == "sk"
    assert m.body() == {"cloud": "crusoe", "region": "us-east1-a", "shade_instance_type": "H100_sxm5x8",
                        "shade_cloud": True, "name": "opengrid-x"}
    assert inst.status == "provisioning" and inst.instance_id.startswith("d290")
    st = a.status(inst.instance_id)
    assert st.status == "running" and abs(st.price_per_gpu_hour - 3.475) < 1e-9 and st.ip == "203.0.113.20"
    assert a.terminate(inst.instance_id).status == "terminating"
    assert kind_of(a.stop, "x") == "config", "Shadeform has no stop"

    errs = Mock({("POST", "/v1/instances/create"): (400, {"error": "no availability for H100_sxm5x8 in us-east1-a"}),
                 ("GET", "/v1/instances/x/info"): (401, {"error": "invalid api key"}),
                 ("GET", "/v1/instances/types"): timeout})
    e = ShadeformAdapter({"api_key": "x"}, transport=errs.transport(), provider="crusoe")
    assert kind_of(e.provision, o, av, LaunchSpec(), "n") == "capacity"
    assert kind_of(e.status, "x") == "auth"
    assert kind_of(e.check_availability, o) == "timeout"


def test_vast():
    def bundles(req):
        q = json.loads(req.url.params["q"])
        assert q["gpu_name"] == {"eq": "RTX 4090"} and q["num_gpus"] == {"eq": 1} and q["verified"] == {"eq": True}
        return httpx.Response(200, json={"offers": [
            {"id": 111, "num_gpus": 1, "dph_total": 0.35, "geolocation": "Texas, US", "machine_id": 9, "reliability": 0.99},
            {"id": 112, "num_gpus": 1, "dph_total": 0.40, "geolocation": "Ontario, CA", "machine_id": 10}]})

    m = Mock({
        ("GET", "/api/v0/bundles/"): bundles,
        ("PUT", "/api/v0/asks/111/"): (200, {"success": True, "new_contract": 7777}),
        ("GET", "/api/v0/instances/7777/"): (200, {"instances": {"actual_status": "running", "num_gpus": 1,
                                                               "dph_total": 0.35, "public_ipaddr": "198.51.100.7"}}),
        ("PUT", "/api/v0/instances/7777/"): (200, {"success": True}),
        ("DELETE", "/api/v0/instances/7777/"): (200, {"success": True}),
    })
    a = VastAdapter({"api_key": "vk"}, transport=m.transport())
    o = offer("vast", sku="RTX 4090", price_per_gpu_hour=0.42)
    av = a.check_availability(o)
    assert av.available and av.metadata["ask_id"] == 111 and av.list_price_per_gpu_hour == 0.35
    q = a.quote(o, av)
    assert q.price_per_gpu_hour == 0.35 and q.basis == "live_provider_api", "the ask's price, not the median"
    ca = a.check_availability(offer("vast", sku="RTX 4090", want_region_group="Canada"))
    assert ca.metadata["ask_id"] == 112
    inst = a.provision(o, av, LaunchSpec(image="pytorch/pytorch:latest"), "opengrid-x")
    assert m.requests[-1].headers["authorization"] == "Bearer vk"
    assert m.body() == {"client_id": "me", "image": "pytorch/pytorch:latest", "disk": 32, "label": "opengrid-x",
                        "runtype": "ssh"}
    assert inst.instance_id == "7777" and inst.status == "provisioning"
    st = a.status("7777")
    assert st.status == "running" and st.ip == "198.51.100.7"
    assert a.stop("7777").status == "stopped" and m.body() == {"state": "stopped"}
    assert a.terminate("7777").status == "terminated"

    errs = Mock({("PUT", "/api/v0/asks/111/"): (404, {"success": False, "error": "no_such_ask",
                                                     "msg": "Instance type no longer available"}),
                 ("GET", "/api/v0/instances/1/"): (401, {"success": False, "error": "auth_error"}),
                 ("GET", "/api/v0/bundles/"): (200, {"offers": []})})
    e = VastAdapter({"api_key": "x"}, transport=errs.transport())
    assert kind_of(e.provision, o, av, LaunchSpec(image="i"), "n") == "capacity"
    assert kind_of(e.status, "1") == "auth"
    assert e.check_availability(o).available is False


def test_verda():
    tokens = []

    def token(req):
        b = json.loads(req.content)
        assert b == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "cs"}
        tokens.append(1)
        return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "expires_in": 3600,
                                         "refresh_token": "r", "scope": "cloud-api-v1"})

    m = Mock({
        ("POST", "/v1/oauth2/token"): token,
        ("GET", "/v1/instance-availability"): (200, [{"location_code": "FIN-01", "availabilities": ["1H100.80S.30V"]},
                                                     {"location_code": "ICE-01", "availabilities": []}]),
        ("GET", "/v1/instance-types"): (200, [{"instance_type": "1H100.80S.30V", "price_per_hour": "2.19",
                                               "gpu": {"number_of_gpus": 1}}]),
        ("POST", "/v1/instances"): (202, "4fc4b5b8-0d6e-4b0b-9f2b-1d0c2c8e6f11"),
        ("GET", "/v1/instances/4fc4b5b8-0d6e-4b0b-9f2b-1d0c2c8e6f11"): (200, {
            "id": "4fc4b5b8-0d6e-4b0b-9f2b-1d0c2c8e6f11", "status": "running", "ip": "192.0.2.44",
            "price_per_hour": 2.19, "gpu": {"number_of_gpus": 1}, "location": "FIN-01"}),
        ("PUT", "/v1/instances"): (202, None),
    })
    a = VerdaAdapter({"client_id": "cid", "client_secret": "cs"}, transport=m.transport())
    assert VerdaAdapter({}).missing_credentials() == ["client_id", "client_secret"]
    o = offer("verda", sku="1H100.80S.30V")
    av = a.check_availability(o)
    assert av.available and av.region == "FIN-01" and av.list_price_per_gpu_hour == 2.19
    assert all(r.headers["authorization"] == "Bearer tok" for r in m.requests if "oauth2" not in r.url.path)
    inst = a.provision(o, av, LaunchSpec(ssh_key="key-uuid", image="ubuntu-24.04-cuda-12.8-open-docker"), "opengrid-x")
    b = m.body()
    assert b["instance_type"] == "1H100.80S.30V" and b["location_code"] == "FIN-01" and b["ssh_key_ids"] == ["key-uuid"]
    assert b["is_spot"] is False and inst.instance_id == "4fc4b5b8-0d6e-4b0b-9f2b-1d0c2c8e6f11"
    st = a.status(inst.instance_id)
    assert st.status == "running" and st.price_per_gpu_hour == 2.19 and st.ip == "192.0.2.44"
    assert a.stop(inst.instance_id).status == "stopped" and m.body() == {"action": "shutdown", "id": inst.instance_id}
    assert a.terminate(inst.instance_id).status == "terminating" and m.body()["action"] == "delete"
    assert len(tokens) == 1, "the token is fetched once per adapter"

    cap = Mock({("POST", "/v1/oauth2/token"): token, ("POST", "/v1/instances"): (503, {"code": "service_unavailable",
                                                                                      "message": "Not enough capacity"}),
                ("GET", "/v1/instances/slow"): timeout})
    e = VerdaAdapter({"client_id": "cid", "client_secret": "cs"}, transport=cap.transport())
    assert kind_of(e.provision, o, av, LaunchSpec(ssh_key="k", image="i"), "n") == "capacity"
    assert kind_of(e.status, "slow") == "timeout"
    bad = Mock({("POST", "/v1/oauth2/token"): (401, {"code": "unauthorized_request", "message": "Invalid client"})})
    assert kind_of(VerdaAdapter({"client_id": "x", "client_secret": "y"}, transport=bad.transport()).check_availability,
                   o) == "auth"


def test_registry_is_honest():
    for c in capabilities.all_capabilities():
        assert c["verified_live"] is False, c["provider"]
        if c["level_supported_by_provider_api"] is not None:
            assert c["level_implemented"] <= c["level_supported_by_provider_api"], c["provider"]
        assert c["level_implemented"] == adapters.level(c["provider"]), "implemented level comes from the code"
    for p in ("aws", "nebius", "massedcompute", "voltagepark", "hyperbolic", "lium", "salad"):
        assert capabilities.capability(p)["level_implemented"] == 0, p
    for p in ("lambda", "runpod", "hyperstack", "digitalocean", "crusoe", "denvr", "latitude", "vast", "verda"):
        assert capabilities.capability(p)["level_implemented"] == 3, p
    assert capabilities.capability("crusoe")["via"] == "shadeform"
    assert capabilities.capability("syn_nobody")["level_implemented"] == 0
    # stop is claimed only where the adapter implements it
    assert not capabilities.capability("lambda")["supports_stop"] and capabilities.capability("runpod")["supports_stop"]


def test_launch_spec_merge():
    ls = LaunchSpec.merged({"image": "mine", "env": {}}, {"image": "default", "ssh_key": "k", "environments": {"A": "e"}})
    assert ls.image == "mine" and ls.ssh_key == "k" and ls.extra == {"environments": {"A": "e"}}


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
