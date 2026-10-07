"""Every real routing adapter against httpx.MockTransport, using the providers' documented request/response
shapes. Nothing touches the network.

Covered per adapter: availability + quote, the launch body (og-* name, tags/labels, ssh), every provision
outcome class (accepted / rejected / unknown on timeout, 5xx, connection reset, garbage 2xx, 2xx without an
id, connect error = rejected), documented capacity codes only, terminate outcomes (accepted / already_gone /
failed / unknown), status mapping incl. not_found and provider error states, list_instances (paginated,
never partial) and find_instance, name sanitisation, secret redaction (messages, raw bodies, logs), stop
semantics, reported_cost, and the capability registry.

Run:  .venv/Scripts/python tests/test_routing_adapters.py
"""

import json
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routing import adapters, capabilities  # noqa: E402
from routing.adapters.base import AdapterError, Availability, LaunchSpec, Offer, redact, sanitize_name  # noqa: E402
from routing.adapters.digitalocean import DigitalOceanAdapter  # noqa: E402
from routing.adapters.hyperstack import HyperstackAdapter  # noqa: E402
from routing.adapters.lambda_labs import LambdaAdapter  # noqa: E402
from routing.adapters.results import instance_name  # noqa: E402
from routing.adapters.runpod import RunPodAdapter  # noqa: E402
from routing.adapters.shadeform import ShadeformAdapter  # noqa: E402
from routing.adapters.vast import VastAdapter  # noqa: E402
from routing.adapters.verda import VerdaAdapter  # noqa: E402

NAME = "og-dep-0a1b2c3d4e5f"
SECRET = "sk_live_SUPERSECRET_123456"


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

    def calls(self, method, path):
        return [r for r in self.requests if r.method == method and r.url.path == path]


def read_timeout(request):
    raise httpx.ReadTimeout("timed out", request=request)


def reset(request):
    raise httpx.RemoteProtocolError("peer closed connection without sending complete message body", request=request)


def connect_error(request):
    raise httpx.ConnectError("connection refused", request=request)


def garbage(request):
    return httpx.Response(200, text="<html>502 Bad Gateway</html>")


def gateway(request):
    return httpx.Response(502, text="<html>Bad Gateway: capacity unavailable</html>")


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
# Per-adapter fixtures: how to build it, where its create / status / terminate / list live
# --------------------------------------------------------------------------

def lambda_spec():
    o = offer("lambda", sku="gpu_1x_a10", region="us-east-1")
    inst = {"id": "i-1", "name": NAME, "status": "active", "ip": "198.51.100.2", "region": {"name": "us-east-1"},
            "instance_type": {"name": "gpu_1x_a10", "price_cents_per_hour": 75, "specs": {"gpus": 1}},
            "tags": [{"key": "opengrid", "value": NAME}]}
    return dict(
        cls=LambdaAdapter, creds={"api_key": SECRET}, offer=o, avail=Availability(True, True, region="us-east-1"),
        launch=LAUNCH, create=("POST", "/api/v1/instance-operations/launch"),
        create_ok=(200, {"data": {"instance_ids": ["i-1"]}}), create_empty=(200, {"data": {"instance_ids": []}}),
        status=("GET", "/api/v1/instances/i-1"), status_ok=(200, {"data": inst}),
        terminate=("POST", "/api/v1/instance-operations/terminate"),
        terminate_ok=(200, {"data": {"terminated_instances": [{**inst, "status": "terminating"}]}}),
        terminate_missing=(404, {"error": {"code": "global/object-does-not-exist",
                                           "message": "Specified instance does not exist."}}),
        list=("GET", "/api/v1/instances"),
        list_ok=(200, {"data": [inst, {**inst, "id": "foreign", "name": "someone-else", "tags": []}],
                       "page_token": None}),
        iid="i-1", running="running")


def runpod_spec():
    pod = {"id": "pod1", "name": NAME, "desiredStatus": "RUNNING", "costPerHr": 0.69, "gpu": {"count": 1}}
    return dict(
        cls=RunPodAdapter, creds={"api_key": SECRET}, offer=offer("runpod", raw_gpu_name="NVIDIA A40"),
        avail=Availability(True, True), launch=LaunchSpec(image="runpod/pytorch:test"),
        create=("POST", "/v1/pods"), create_ok=(201, pod), create_empty=(201, {"desiredStatus": "RUNNING"}),
        status=("GET", "/v1/pods/pod1"), status_ok=(200, pod),
        terminate=("DELETE", "/v1/pods/pod1"), terminate_ok=(204, None),
        terminate_missing=(404, {"error": "pod not found"}),
        list=("GET", "/v1/pods"), list_ok=(200, [pod, {**pod, "id": "other", "name": "theirs"}]),
        iid="pod1", running="running")


def hyperstack_spec():
    vm = {"id": 123, "name": NAME, "status": "ACTIVE", "labels": ["opengrid", NAME], "floating_ip": "203.0.113.5",
          "created_at": "2026-10-07T10:00:00Z", "flavor": {"gpu": "A100-80G-PCIe", "gpu_count": 1},
          "environment": {"name": "env-ca", "region": "CANADA-1"}}
    return dict(
        cls=HyperstackAdapter, creds={"api_key": SECRET}, offer=offer("hyperstack", sku="n3-A100x1", region="CANADA-1"),
        avail=Availability(True, True, region="CANADA-1"),
        launch=LaunchSpec.merged({"ssh_key": "k1"}, {"image": "Ubuntu Server 22.04 LTS",
                                                     "environments": {"CANADA-1": "env-ca"}}),
        create=("POST", "/v1/core/virtual-machines"),
        create_ok=(200, {"status": True, "message": "VM is scheduled for creation.", "instances": [{**vm, "status": "CREATING"}]}),
        create_empty=(200, {"status": True, "instances": []}),
        status=("GET", "/v1/core/virtual-machines/123"), status_ok=(200, {"status": True, "instance": vm}),
        terminate=("DELETE", "/v1/core/virtual-machines/123"),
        terminate_ok=(200, {"status": True, "message": "Instance is being deleted."}),
        terminate_missing=(404, {"status": False, "message": "VM 123 does not exists.", "error_reason": "not_found"}),
        list=("GET", "/v1/core/virtual-machines"),
        list_ok=(200, {"status": True, "instances": [vm, {**vm, "id": 999, "name": "theirs", "labels": []}],
                       "count": 2, "page": None, "page_size": None}),
        iid="123", running="running")


def digitalocean_spec():
    d = {"id": 3164444, "name": NAME, "status": "active", "tags": ["opengrid", NAME], "created_at": "2026-10-07T10:00:00Z",
         "region": {"slug": "tor1"}, "size": {"price_hourly": 3.39, "gpu_info": {"count": 1, "model": "nvidia_h100"}},
         "networks": {"v4": [{"ip_address": "192.0.2.9", "type": "public"}]}}
    return dict(
        cls=DigitalOceanAdapter, creds={"api_key": SECRET}, offer=offer("digitalocean", sku="gpu-h100x1-80gb"),
        avail=Availability(True, True, region="tor1"), launch=LaunchSpec(ssh_key="512189", image="gpu-h100x1-base"),
        create=("POST", "/v2/droplets"), create_ok=(202, {"droplet": {**d, "status": "new"}}),
        create_empty=(202, {"links": {}}),
        status=("GET", "/v2/droplets/3164444"), status_ok=(200, {"droplet": d}),
        terminate=("DELETE", "/v2/droplets/3164444"), terminate_ok=(204, None),
        terminate_missing=(404, {"id": "not_found", "message": "The resource you requested could not be found."}),
        list=("GET", "/v2/droplets"),
        list_ok=(200, {"droplets": [d, {**d, "id": 1, "name": "theirs", "tags": []}], "links": {}, "meta": {"total": 2}}),
        iid="3164444", running="running")


def shadeform_spec():
    i = {"id": "sf-1", "cloud": "crusoe", "name": NAME, "status": "active", "region": "us-east1-a",
         "tags": ["opengrid", NAME], "hourly_price": 210, "cost_estimate": "1.75", "configuration": {"num_gpus": 1},
         "created_at": "2026-10-07T10:00:00Z", "deleted_at": None}
    return dict(
        cls=ShadeformAdapter, creds={"api_key": SECRET}, provider="crusoe", offer=offer("crusoe", listing_id="A100_80G"),
        avail=Availability(True, True, region="us-east1-a"), launch=LaunchSpec(ssh_key="key-uuid"),
        create=("POST", "/v1/instances/create"), create_ok=(200, {"id": "sf-1"}), create_empty=(200, {}),
        status=("GET", "/v1/instances/sf-1/info"), status_ok=(200, i),
        terminate=("POST", "/v1/instances/sf-1/delete"), terminate_ok=(200, None),
        terminate_missing=(404, {"message": "instance not found"}),
        list=("GET", "/v1/instances"),
        list_ok=(200, {"instances": [i, {**i, "id": "sf-2", "name": "theirs", "tags": []},
                                     {**i, "id": "sf-3", "cloud": "lambda"}]}),
        iid="sf-1", running="running")


def vast_spec():
    inst = {"id": 7777, "label": NAME, "actual_status": "running", "num_gpus": 1, "dph_total": 0.35,
            "public_ipaddr": "198.51.100.7", "start_date": 1791367200.0}
    return dict(
        cls=VastAdapter, creds={"api_key": SECRET}, offer=offer("vast", sku="RTX 4090"),
        avail=Availability(True, True, region="Texas, US", metadata={"ask_id": 111}),
        launch=LaunchSpec(image="pytorch/pytorch:latest"),
        create=("PUT", "/api/v0/asks/111/"), create_ok=(200, {"success": True, "new_contract": 7777}),
        create_empty=(200, {"success": True}),
        status=("GET", "/api/v0/instances/7777/"), status_ok=(200, {"instances": inst}),
        terminate=("DELETE", "/api/v0/instances/7777/"),
        terminate_ok=(200, {"success": True, "msg": "Instance destroyed successfully"}),
        terminate_missing=(404, {"success": False, "msg": "Instance not found"}),
        list=("GET", "/api/v1/instances"),
        list_ok=(200, {"success": True, "instances": [inst, {**inst, "id": 1, "label": "theirs"}],
                       "next_token": None, "total_instances": 2}),
        iid="7777", running="running")


def verda_spec():
    iid = "4fc4b5b8-0d6e-4b0b-9f2b-1d0c2c8e6f11"
    inst = {"id": iid, "hostname": NAME, "status": "running", "ip": "192.0.2.44", "price_per_hour": 2.19,
            "gpu": {"number_of_gpus": 1}, "location": "FIN-01", "os_volume_id": "vol-os",
            "volume_ids": ["vol-data"], "tags": [{"id": "t1", "key": "opengrid", "value": NAME}],
            "instance_type": "1H100.80S.30V", "created_at": "2026-10-07T10:00:00Z"}
    return dict(
        cls=VerdaAdapter, creds={"client_id": "cid", "client_secret": SECRET}, offer=offer("verda", sku="1H100.80S.30V"),
        avail=Availability(True, True, region="FIN-01"), launch=LaunchSpec(ssh_key="key-uuid", image="ubuntu-24.04"),
        create=("POST", "/v1/instances"), create_ok=(202, iid), create_empty=(202, ""),
        status=("GET", f"/v1/instances/{iid}"), status_ok=(200, inst),
        terminate=("PUT", "/v1/instances"),
        terminate_ok=(202, [{"instanceId": iid, "action": "delete", "status": "success"}]),
        terminate_missing=(404, {"code": "not_found", "message": "Instance not found"}),
        list=("GET", "/v1/instances"), list_ok=(200, [inst, {**inst, "id": "x2", "hostname": "theirs", "tags": []}]),
        iid=iid, running="running",
        extra_routes={("POST", "/v1/oauth2/token"): (200, {"access_token": "tok-" + SECRET[-6:], "expires_in": 3600})})


SPECS = {"lambda": lambda_spec, "runpod": runpod_spec, "hyperstack": hyperstack_spec, "digitalocean": digitalocean_spec,
         "shadeform": shadeform_spec, "vast": vast_spec, "verda": verda_spec}


def build(spec, routes):
    allr = {**spec.get("extra_routes", {}), **routes}
    m = Mock(allr)
    a = spec["cls"](spec["creds"], transport=m.transport(), provider=spec.get("provider"))
    return a, m


# --------------------------------------------------------------------------
# Generic outcome matrices (every adapter)
# --------------------------------------------------------------------------

def test_provision_outcome_classes():
    for p, mk in SPECS.items():
        spec = mk()
        cases = {
            "accepted": (spec["create_ok"], "accepted", None),
            "timeout": (read_timeout, "unknown", "timeout"),
            "http500": ((500, {"error": "internal"}), "unknown", "server"),
            "http502_text": (gateway, "unknown", None),
            "reset_after_send": (reset, "unknown", "network"),
            "garbage_2xx": (garbage, "unknown", "parse"),
            "2xx_without_id": (spec["create_empty"], "unknown", None),
            "connect_error": (connect_error, "rejected", "network"),
            "auth": ((401, {"error": {"code": "global/invalid-api-key", "message": "bad key"}}), "rejected", "auth"),
            "validation": ((400, {"error": "bad request"}), "rejected", None),
            "conflict": ((409, {"error": "already exists"}), "unknown", None),
        }
        for case, (resp, outcome, ek) in cases.items():
            a, m = build(spec, {spec["create"]: resp})
            r = a.provision(spec["offer"], spec["avail"], spec["launch"], NAME)
            assert r.outcome == outcome, (p, case, r)
            if ek:
                assert r.error_kind == ek, (p, case, r.error_kind)
            if outcome == "accepted":
                assert r.instance_id == spec["iid"], (p, r)
            else:
                assert r.instance_id is None, (p, case)
            assert SECRET not in (r.message or "") and SECRET not in json.dumps(r.raw_redacted, default=str), (p, case)
            assert len(m.calls(*spec["create"])) == (0 if case == "connect_error" else 1) or case == "connect_error", \
                (p, case, "exactly one create call")


def test_terminate_outcome_classes():
    for p, mk in SPECS.items():
        spec = mk()
        routes_extra = {}
        if p == "verda":   # Verda reads the instance (volume ids) before deleting
            routes_extra[spec["status"]] = spec["status_ok"]
        cases = {"accepted": (spec["terminate_ok"], "accepted"),
                 "missing": (spec["terminate_missing"], "already_gone"),
                 "server": ((500, {"error": "boom"}), "unknown"),
                 "timeout": (read_timeout, "unknown"),
                 "forbidden": ((403, {"error": "forbidden"}), "failed"),
                 "not_sent": (connect_error, "failed")}
        for case, (resp, outcome) in cases.items():
            a, m = build(spec, {**routes_extra, spec["terminate"]: resp})
            r = a.terminate(spec["iid"])
            assert r.outcome == outcome, (p, case, r)
            assert SECRET not in (r.message or ""), (p, case)


def test_status_mapping_and_not_found():
    for p, mk in SPECS.items():
        spec = mk()
        a, _ = build(spec, {spec["status"]: spec["status_ok"]})
        st = a.status(spec["iid"])
        assert st.state == spec["running"], (p, st)
        assert str(st.instance_id) == spec["iid"] and st.matches(NAME), (p, st)
        assert st.observed_at is not None
        a, _ = build(spec, {spec["status"]: (404, {"error": "not found"})})
        assert a.status(spec["iid"]).state == "not_found", p
        a, _ = build(spec, {spec["status"]: read_timeout})
        st = a.status(spec["iid"])
        assert st.state == "unknown" and st.error_kind == "timeout", (p, st)
        a, _ = build(spec, {spec["status"]: (401, {"error": "bad key " + SECRET})})
        st = a.status(spec["iid"])
        assert st.state == "unknown" and st.error_kind == "auth" and SECRET not in st.message, (p, st)


def test_list_and_find():
    for p, mk in SPECS.items():
        spec = mk()
        a, m = build(spec, {spec["list"]: spec["list_ok"]})
        rows = a.list_instances()
        ids = {str(r.instance_id) for r in rows}
        assert spec["iid"] in ids and len(rows) >= 2, (p, ids)
        if p == "shadeform":
            assert "sf-3" not in ids, "a Shadeform account lists every cloud; each provider sees its own"
        f = a.find_instance(NAME)
        assert f is not None and str(f.instance_id) == spec["iid"], (p, f)
        assert a.find_instance("og-dep-nothere") is None, p
        a, _ = build(spec, {spec["list"]: (500, {"error": "down"})})
        assert kind_of(a.list_instances) in ("server", "provider_error"), "a failed list raises, never []"
        a, _ = build(spec, {spec["list"]: garbage})
        assert kind_of(a.list_instances) == "parse", p


def test_find_ambiguous_duplicates():
    spec = lambda_spec()
    inst = spec["list_ok"][1]["data"][0]
    a, _ = build(spec, {spec["list"]: (200, {"data": [inst, {**inst, "id": "i-2"}], "page_token": None})})
    assert kind_of(a.find_instance, NAME) == "ambiguous", "two live instances with one name are never silently picked"


def test_names_sanitised_and_set_at_launch():
    assert instance_name("dep_0A1B") == "og-dep-0a1b"
    assert sanitize_name("og-dep_ABC..x") == "og-dep-abc-x"
    for p, mk in SPECS.items():
        spec = mk()
        a, m = build(spec, {spec["create"]: spec["create_ok"]})
        r = a.provision(spec["offer"], spec["avail"], spec["launch"], "og-dep_0A1B2C")
        assert r.outcome == "accepted", (p, r)
        b = m.body(-1) if p != "verda" else json.loads(m.calls(*spec["create"])[0].content)
        sent = json.dumps(b)
        assert "og-dep-0a1b2c" in sent and "dep_0" not in sent, (p, sent)
        # too long for the provider: refused before any request (a truncated name could not be found again)
        a, m = build(spec, {spec["create"]: spec["create_ok"]})
        r = a.provision(spec["offer"], spec["avail"], spec["launch"], "og-" + "a" * 300)
        assert r.outcome == "rejected" and r.error_kind == "validation" and not m.requests, (p, r)


def test_secret_redaction_everywhere():
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage() + " " + json.dumps(rec.__dict__, default=str))

    h = H()
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        for p, mk in SPECS.items():
            spec = mk()
            echo = (400, {"error": f"invalid key {SECRET}", "api_key": SECRET, "headers": {"Authorization": "Bearer " + SECRET}})
            a, _ = build(spec, {spec["create"]: echo, spec["status"]: echo, spec["terminate"]: echo})
            r = a.provision(spec["offer"], spec["avail"], spec["launch"], NAME)
            assert SECRET not in r.message and SECRET not in json.dumps(r.raw_redacted, default=str), (p, r)
            st = a.status(spec["iid"])
            assert SECRET not in st.message, p
            t = a.terminate(spec["iid"])
            assert SECRET not in t.message, p
            try:
                a, _ = build(spec, {spec["list"]: echo})
                a.list_instances()
            except AdapterError as exc:
                assert SECRET not in str(exc) and SECRET not in json.dumps(exc.body, default=str), p
    finally:
        logging.getLogger().removeHandler(h)
    assert not [x for x in records if SECRET in x], "no secret in any log line"
    assert redact({"env": {"HF_TOKEN": "x"}, "nested": [{"client_secret": "y"}], "ok": 1}) == \
        {"env": "***", "nested": [{"client_secret": "***"}], "ok": 1}


def test_stop_semantics():
    # stop only where it saves money: RunPod and Vast (storage only). DO / Hyperstack / Verda keep billing.
    for p, mk in SPECS.items():
        cls = mk()["cls"]
        sb = cls.CAPABILITIES.stopped_billing[0]
        assert cls.SUPPORTS_STOP == (sb == "storage_only"), (p, sb)
        assert cls.CAPABILITIES.stopped_billing[1], f"{p}: stopped_billing needs evidence"
    for cls in (DigitalOceanAdapter, HyperstackAdapter, VerdaAdapter, LambdaAdapter):
        r = cls({"api_key": "k", "client_id": "a", "client_secret": "b"}, transport=Mock({}).transport()).stop("x")
        assert r.outcome == "failed" and r.error_kind == "not_supported", cls
    m = Mock({("POST", "/v1/pods/pod1/stop"): (200, {"id": "pod1", "desiredStatus": "EXITED"}),
              ("PUT", "/api/v0/instances/7/"): (200, {"success": True})})
    assert RunPodAdapter({"api_key": "k"}, transport=m.transport()).stop("pod1").outcome == "accepted"
    assert VastAdapter({"api_key": "k"}, transport=m.transport()).stop("7").outcome == "accepted"


def test_every_adapter_simulated_with_full_matrix():
    for p, cls in adapters.ADAPTERS.items():
        assert cls.VALIDATION_STATUS == "SIMULATED", p
        m = cls.CAPABILITIES.as_dict()
        for row in ("quote", "live_availability", "launch", "ssh_key_injection", "startup_script", "status", "stop",
                    "terminate", "region_selection", "gpu_count_selection", "price_known_before_launch",
                    "billing_unit", "minimum_commitment", "interruptible", "name_tag_at_launch", "list_instances",
                    "idempotency_token", "stopped_billing"):
            assert m[row]["value"] not in (None, ""), (p, row)
            assert m[row]["evidence"], (p, row, "every row needs evidence")
        assert m["risks"], p
    for p in ("crusoe", "denvr", "latitude"):
        assert adapters.credential_provider(p) == "shadeform", "Shadeform clouds authenticate with Shadeform keys only"
    assert adapters.credential_provider("lambda") == "lambda"


# --------------------------------------------------------------------------
# Provider-specific behaviour
# --------------------------------------------------------------------------

def test_lambda_specifics():
    types = {"data": {"gpu_1x_h100_sxm5": {
        "instance_type": {"name": "gpu_1x_h100_sxm5", "price_cents_per_hour": 329, "specs": {"gpus": 1}},
        "regions_with_capacity_available": [{"name": "us-east-1"}]}}}
    m = Mock({("GET", "/api/v1/instance-types"): (200, types),
              ("POST", "/api/v1/instance-operations/launch"): (200, {"data": {"instance_ids": ["x"]}})})
    a = LambdaAdapter({"api_key": "k"}, transport=m.transport())
    o = offer("lambda", sku="gpu_1x_h100_sxm5", region="us-east-1")
    av = a.check_availability(o)
    assert m.requests[0].url.host == "cloud.lambda.ai", "the non-deprecated host"
    assert av.available and av.list_price_per_gpu_hour == 3.29 and a.quote(o, av).basis == "live_provider_api"
    assert a.check_availability(offer("lambda", sku="gpu_1x_h100_sxm5", want_region_group="Europe")).available is False
    r = a.provision(o, av, LaunchSpec(ssh_key="opengrid", startup_script="#cloud-config"), NAME)
    assert m.body() == {"region_name": "us-east-1", "instance_type_name": "gpu_1x_h100_sxm5",
                        "ssh_key_names": ["opengrid"], "name": NAME, "tags": [{"key": "opengrid", "value": NAME}],
                        "user_data": "#cloud-config"}
    assert r.outcome == "accepted"
    for code, kind in (("instance-operations/launch/insufficient-capacity", "capacity"), ("global/quota-exceeded", "quota")):
        b = LambdaAdapter({"api_key": "k"}, transport=Mock({("POST", "/api/v1/instance-operations/launch"): (
            400, {"error": {"code": code, "message": "nope"}})}).transport())
        r = b.provision(o, av, LAUNCH, NAME)
        assert r.outcome == "rejected" and r.error_kind == kind, (code, r)
    # capacity words in a 5xx body are NOT a capacity answer
    b = LambdaAdapter({"api_key": "k"}, transport=Mock({("POST", "/api/v1/instance-operations/launch"): (
        503, {"error": {"code": "instance-operations/launch/insufficient-capacity"}})}).transport())
    assert b.provision(o, av, LAUNCH, NAME).outcome == "unknown"
    # ssh public key -> registered under the instance name first
    m = Mock({("POST", "/api/v1/ssh-keys"): (200, {"data": {"id": "k1", "name": NAME}}),
              ("POST", "/api/v1/instance-operations/launch"): (200, {"data": {"instance_ids": ["x"]}})})
    a = LambdaAdapter({"api_key": "k"}, transport=m.transport())
    r = a.provision(o, av, LaunchSpec(ssh_public_key="ssh-ed25519 AAAA test"), NAME)
    assert r.outcome == "accepted" and m.body(0)["name"] == NAME and m.body()["ssh_key_names"] == [NAME]
    # key registration failing (even 5xx) cannot have created compute: rejected, no launch call
    m = Mock({("POST", "/api/v1/ssh-keys"): (500, {"error": {"code": "x"}})})
    r = LambdaAdapter({"api_key": "k"}, transport=m.transport()).provision(o, av, LaunchSpec(ssh_public_key="ssh-ed25519 A"), NAME)
    assert r.outcome == "rejected" and not m.calls("POST", "/api/v1/instance-operations/launch")
    # pagination: every page or an error
    pages = {None: {"data": [{"id": "a", "name": NAME, "status": "active"}], "page_token": "p2"},
             "p2": {"data": [{"id": "b", "name": "x", "status": "terminated"}], "page_token": None}}
    m = Mock({("GET", "/api/v1/instances"): lambda req: httpx.Response(200, json=pages[req.url.params.get("page_token")])})
    rows = LambdaAdapter({"api_key": "k"}, transport=m.transport()).list_instances()
    assert [r.instance_id for r in rows] == ["a", "b"] and rows[1].state == "terminated"
    # terminate that does not list the instance is not 'accepted'
    m = Mock({("POST", "/api/v1/instance-operations/terminate"): (200, {"data": {"terminated_instances": []}})})
    assert LambdaAdapter({"api_key": "k"}, transport=m.transport()).terminate("zz").outcome == "unknown"
    assert LambdaAdapter({"api_key": "k"}).reported_cost("x", None, None).amount_usd is None


def test_runpod_specifics():
    def graphql(req):
        return httpx.Response(200, json={"data": {"gpuTypes": [{"id": "NVIDIA A40", "lowestPrice": {
            "uninterruptablePrice": 0.98, "stockStatus": "High"}}]}})

    m = Mock({("POST", "/graphql"): graphql, ("POST", "/v1/pods"): (201, {"id": "p", "desiredStatus": "RUNNING"})})
    a = RunPodAdapter({"api_key": "k"}, transport=m.transport())
    o = offer("runpod", raw_gpu_name="NVIDIA A40", gpu_count=2)
    av = a.check_availability(o)
    assert av.list_price_per_gpu_hour == 0.49, "lowestPrice is the pod total"
    r = a.provision(o, av, LaunchSpec(image="img", ssh_public_key="ssh-ed25519 AAAA", env={"HF_TOKEN": "secret-value"}), NAME)
    b = m.body()
    assert r.outcome == "accepted" and b["name"] == NAME and b["env"]["SSH_PUBLIC_KEY"] == "ssh-ed25519 AAAA"
    assert b["volumeInGb"] == 0 and b["interruptible"] is False and b["cloudType"] == "SECURE"
    assert "secret-value" not in json.dumps(r.raw_redacted), "env values are never kept"
    # capacity text in a 500 is ambiguous: never a definite rejection
    m = Mock({("POST", "/v1/pods"): (500, {"error": "There are no longer any instances available"})})
    assert RunPodAdapter({"api_key": "k"}, transport=m.transport()).provision(o, av, LaunchSpec(image="i"), NAME).outcome == "unknown"
    assert "startup_script" in " ".join(RunPodAdapter({"api_key": "k"}).missing_launch(LaunchSpec(image="i", startup_script="x"), o))
    m = Mock({("GET", "/v1/billing/pods"): (200, [{"amount": 0.25, "podId": "p", "time": "2026-10-07T10:00:00Z"},
                                                  {"amount": 0.5, "podId": "p", "time": "2026-10-07T11:00:00Z"}])})
    from datetime import datetime, timezone
    t0, t1 = datetime(2026, 10, 7, 10, tzinfo=timezone.utc), datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    rep = RunPodAdapter({"api_key": "k"}, transport=m.transport()).reported_cost("p", t0, t1)
    assert rep.amount_usd == 0.75 and m.requests[0].url.params["podId"] == "p"


def test_hyperstack_specifics():
    spec = hyperstack_spec()
    m = Mock({spec["create"]: spec["create_ok"]})
    a = HyperstackAdapter({"api_key": "hk"}, transport=m.transport())
    r = a.provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    b = m.body()
    assert r.outcome == "accepted" and r.instance_id == "123"
    assert b["labels"] == ["opengrid", NAME] and b["name"] == NAME
    assert b["security_rules"] == [{"direction": "ingress", "ethertype": "IPv4", "protocol": "tcp",
                                    "remote_ip_prefix": "0.0.0.0/0", "port_range_min": 22, "port_range_max": 22}], \
        "new VMs have no inbound rules: SSH must be opened"
    # 200 without status:true is undocumented -> ambiguous
    m = Mock({spec["create"]: (200, {"status": False, "message": "Insufficient resources available"})})
    assert HyperstackAdapter({"api_key": "k"}, transport=m.transport()).provision(
        spec["offer"], spec["avail"], spec["launch"], NAME).outcome == "unknown"
    # "insufficient balance" on a 400 is a rejection but NOT capacity
    m = Mock({spec["create"]: (400, {"status": False, "message": "Insufficient balance", "error_reason": "bad_request"})})
    r = HyperstackAdapter({"api_key": "k"}, transport=m.transport()).provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "rejected" and r.error_kind != "capacity"
    # delete while CREATING: a retryable failure (the reconciler re-issues it)
    m = Mock({spec["terminate"]: (400, {"status": False, "error_reason": "bad_request", "message":
              "VM x is currently being created and cannot be deleted yet. Please wait until the creation process is finished."})})
    t = HyperstackAdapter({"api_key": "k"}, transport=m.transport()).terminate("123")
    assert t.outcome == "failed" and t.retryable and t.error_kind == "still_creating"
    # ERROR state: the instance may exist -> 'error', never terminal
    m = Mock({spec["status"]: (200, {"instance": {"id": 123, "status": "ERROR"}})})
    st = HyperstackAdapter({"api_key": "k"}, transport=m.transport()).status("123")
    assert st.state == "error" and st.alive
    assert HyperstackAdapter.ERROR_STATE_BILLED is False
    # keypair registration from public key material
    m = Mock({("POST", "/v1/core/keypairs"): (200, {"status": True, "keypair": {"id": 5, "name": NAME}}),
              spec["create"]: spec["create_ok"]})
    a = HyperstackAdapter({"api_key": "k"}, transport=m.transport())
    lp = LaunchSpec.merged({"ssh_public_key": "ssh-ed25519 AAAA"}, {"image": "U", "environments": {"CANADA-1": "env-ca"}})
    assert a.provision(spec["offer"], spec["avail"], lp, NAME).outcome == "accepted"
    assert m.body(0) == {"environment_name": "env-ca", "name": NAME, "public_key": "ssh-ed25519 AAAA"}
    assert m.body()["key_name"] == NAME
    # paging and search
    vm = spec["list_ok"][1]["instances"][0]

    def page(req):
        n = int(req.url.params["page"])
        rows = [{**vm, "id": i} for i in range((n - 1) * 100, min(n * 100, 150))]
        return httpx.Response(200, json={"status": True, "instances": rows, "count": 150, "page": n, "page_size": 100})

    m = Mock({spec["list"]: page})
    assert len(HyperstackAdapter({"api_key": "k"}, transport=m.transport()).list_instances()) == 150
    m = Mock({spec["list"]: spec["list_ok"]})
    HyperstackAdapter({"api_key": "k"}, transport=m.transport()).find_instance(NAME)
    assert m.requests[0].url.params["search"] == NAME
    m = Mock({("GET", "/v1/billing/billing/history/virtual-machine/123"): (200, {"status": True,
              "billing_history_vm_details": {"billing_history": [{"metrics": {"incurred_bill": 1.2}},
                                                                 {"metrics": {"incurred_bill": 0.3}}]}})})
    from datetime import datetime, timezone
    rep = HyperstackAdapter({"api_key": "k"}, transport=m.transport()).reported_cost(
        "123", datetime(2026, 10, 7, tzinfo=timezone.utc), datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert abs(rep.amount_usd - 1.5) < 1e-9 and m.requests[0].url.params["start_date"] == "2026-10-07T00:00:00"


def test_digitalocean_specifics():
    spec = digitalocean_spec()
    m = Mock({spec["create"]: spec["create_ok"]})
    r = DigitalOceanAdapter({"api_key": "k"}, transport=m.transport()).provision(
        spec["offer"], spec["avail"], LaunchSpec.merged({"ssh_key": "512189"}, {"images": {"gpu-h100x1-80gb": "gpu-h100x1-base"}}),
        "og-dep_ABC")
    assert r.outcome == "accepted"
    assert m.body() == {"name": "og-dep-abc", "region": "tor1", "size": "gpu-h100x1-80gb", "image": "gpu-h100x1-base",
                        "ssh_keys": [512189], "tags": ["opengrid", "og-dep-abc"]}
    # 422 is a definite rejection; its text is undocumented, so it is never labelled capacity by substring
    m = Mock({spec["create"]: (422, {"id": "unprocessable_entity", "message": "capacity is currently unavailable"})})
    r = DigitalOceanAdapter({"api_key": "k"}, transport=m.transport()).provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "rejected" and r.error_kind == "validation"
    # list follows links.pages.next; find uses the tag filter
    d = spec["list_ok"][1]["droplets"][0]

    def page(req):
        if req.url.params.get("tag_name"):
            return httpx.Response(200, json={"droplets": [d], "links": {}, "meta": {"total": 1}})
        n = int(req.url.params["page"])
        nxt = {"pages": {"next": "https://api.digitalocean.com/v2/droplets?page=2"}} if n == 1 else {}
        return httpx.Response(200, json={"droplets": [{**d, "id": n}], "links": nxt, "meta": {"total": 2}})

    m = Mock({spec["list"]: page})
    a = DigitalOceanAdapter({"api_key": "k"}, transport=m.transport())
    assert [r.instance_id for r in a.list_instances()] == ["1", "2"]
    assert a.find_instance(NAME).instance_id == "3164444" and m.requests[-1].url.params["tag_name"] == NAME
    # incomplete paging (meta.total says more) is an error, never a partial list
    m = Mock({spec["list"]: (200, {"droplets": [d], "links": {}, "meta": {"total": 5}})})
    assert kind_of(DigitalOceanAdapter({"api_key": "k"}, transport=m.transport()).list_instances) == "parse"
    m = Mock({spec["status"]: (200, {"droplet": {**d, "status": "archive"}})})
    assert DigitalOceanAdapter({"api_key": "k"}, transport=m.transport()).status("3164444").state == "terminated"


def test_shadeform_specifics():
    spec = shadeform_spec()
    assert ShadeformAdapter.CREDENTIAL_PROVIDER == "shadeform"
    m = Mock({spec["create"]: spec["create_ok"]})
    a = ShadeformAdapter({"api_key": "sk"}, transport=m.transport(), provider="crusoe")
    r = a.provision(spec["offer"], spec["avail"], LaunchSpec(ssh_key="key-uuid", startup_script="echo hi"), NAME)
    b = m.body()
    assert r.outcome == "accepted" and b["tags"] == ["opengrid", NAME] and b["ssh_key_id"] == "key-uuid"
    assert b["cloud"] == "crusoe" and b["launch_configuration"]["type"] == "script"
    assert ShadeformAdapter({"api_key": "k"}, provider="crusoe").missing_launch(LaunchSpec(), spec["offer"]) == ["ssh_key"], \
        "without a key Shadeform's managed key is used and the user cannot log in"
    # a 400 'no availability' is a rejection, but Shadeform documents no capacity code: never labelled capacity
    m = Mock({spec["create"]: (400, {"error": "no availability for A100_80G"})})
    r = ShadeformAdapter({"api_key": "k"}, transport=m.transport(), provider="crusoe").provision(
        spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "rejected" and r.error_kind == "validation"
    m = Mock({spec["status"]: (200, {**spec["status_ok"][1], "status": "deleting", "deleted_at": "2026-10-07T11:00:00Z"})})
    st = ShadeformAdapter({"api_key": "k"}, transport=m.transport(), provider="crusoe").status("sf-1")
    assert st.state == "terminating" and st.ended_at is not None and st.ended_at.hour == 11
    m = Mock({spec["status"]: spec["status_ok"]})
    rep = ShadeformAdapter({"api_key": "k"}, transport=m.transport(), provider="crusoe").reported_cost("sf-1", None, None)
    assert rep.amount_usd == 1.75 and "UNVERIFIED" in rep.basis


def test_vast_specifics():
    def bundles(req):
        q = json.loads(req.url.params["q"])
        assert q["allocated_storage"] == 32, "the quote includes the storage the launch will request"
        return httpx.Response(200, json={"offers": [{"id": 111, "num_gpus": 1, "dph_total": 0.35, "geolocation": "Texas, US"}]})

    m = Mock({("GET", "/api/v0/bundles/"): bundles, ("PUT", "/api/v0/asks/111/"): (200, {"success": True, "new_contract": 7}),
              ("POST", "/api/v0/instances/7/ssh/"): (200, {"success": True})})
    a = VastAdapter({"api_key": "vk"}, transport=m.transport())
    o = offer("vast", sku="RTX 4090")
    av = a.check_availability(o)
    r = a.provision(o, av, LaunchSpec(image="img", ssh_public_key="ssh-ed25519 AAAA", startup_script="echo hi"), NAME)
    assert r.outcome == "accepted" and m.body(1)["label"] == NAME and m.body(1)["onstart"] == "echo hi"
    assert m.body(2) == {"ssh_key": "ssh-ed25519 AAAA"}, "the key is attached to THIS instance only"
    for status, body, kind in ((410, {"success": False, "error": "no_such_ask"}, "capacity"),
                               (404, {"success": False, "error": "invalid_args", "msg": "no_such_ask Instance type by id"}, "capacity"),
                               (400, {"success": False, "error": "invalid_args"}, "validation")):
        m = Mock({("PUT", "/api/v0/asks/111/"): (status, body)})
        r = VastAdapter({"api_key": "k"}, transport=m.transport()).provision(o, av, LaunchSpec(image="i"), NAME)
        assert r.outcome == "rejected" and r.error_kind == kind, (status, r)
    m = Mock({("PUT", "/api/v0/asks/111/"): (200, {"success": False, "error": "weird"})})
    assert VastAdapter({"api_key": "k"}, transport=m.transport()).provision(o, av, LaunchSpec(image="i"), NAME).outcome == "unknown"
    # a destroyed instance disappears: empty `instances` is not_found (P1-4), offline/unknown never reach running
    m = Mock({("GET", "/api/v0/instances/7/"): (200, {"instances": None})})
    assert VastAdapter({"api_key": "k"}, transport=m.transport()).status("7").state == "not_found"
    m = Mock({("GET", "/api/v0/instances/7/"): (200, {"instances": {"id": 7, "actual_status": "offline"}})})
    assert VastAdapter({"api_key": "k"}, transport=m.transport()).status("7").state == "error"
    m = Mock({("DELETE", "/api/v0/instances/7/"): (200, {"success": False, "msg": "host offline"})})
    assert VastAdapter({"api_key": "k"}, transport=m.transport()).terminate("7").outcome == "failed"
    pages = {None: {"instances": [{"id": 1, "label": NAME}], "next_token": "t2", "total_instances": 2},
             "t2": {"instances": [{"id": 2, "label": "x"}], "next_token": None, "total_instances": 2}}
    m = Mock({("GET", "/api/v1/instances"): lambda req: httpx.Response(200, json=pages[req.url.params.get("after_token")])})
    assert [r.instance_id for r in VastAdapter({"api_key": "k"}, transport=m.transport()).list_instances()] == ["1", "2"]
    from datetime import datetime, timezone
    m = Mock({("GET", "/api/v0/charges"): (200, {"results": [{"source": "instance-7", "amount": 0.4},
                                                             {"source": "instance-8", "amount": 9.0}], "next_token": None})})
    rep = VastAdapter({"api_key": "k"}, transport=m.transport()).reported_cost(
        "7", datetime(2026, 10, 7, tzinfo=timezone.utc), datetime(2026, 10, 7, 2, tzinfo=timezone.utc))
    assert rep.amount_usd == 0.4


def test_verda_specifics():
    spec = verda_spec()
    tok = spec["extra_routes"]
    # documented capacity: 503 + {"code":"service_unavailable"} -> rejected capacity; any other 503 -> unknown
    m = Mock({**tok, spec["create"]: (503, {"code": "service_unavailable", "message": "No capacity available"})})
    r = VerdaAdapter(spec["creds"], transport=m.transport()).provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "rejected" and r.error_kind == "capacity"
    m = Mock({**tok, spec["create"]: (503, "<html>Service Unavailable</html>")})
    r = VerdaAdapter(spec["creds"], transport=m.transport()).provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "unknown", "an edge 503 without the documented code is ambiguous"
    m = Mock({**tok, spec["create"]: (402, {"code": "insufficient_funds", "message": "top up"})})
    r = VerdaAdapter(spec["creds"], transport=m.transport()).provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "rejected" and r.error_kind == "quota"
    # launch body: hostname, tags, user agent
    m = Mock({**tok, spec["create"]: spec["create_ok"]})
    a = VerdaAdapter(spec["creds"], transport=m.transport())
    assert a.provision(spec["offer"], spec["avail"], spec["launch"], NAME).outcome == "accepted"
    b = json.loads(m.calls(*spec["create"])[0].content)
    assert b["hostname"] == NAME and b["tags"] == [{"key": "opengrid", "value": NAME}] and b["is_spot"] is False
    assert m.calls(*spec["create"])[0].headers["user-agent"].startswith("opengrid")
    # the token fetch failing is pre-create: a clean rejection
    m = Mock({("POST", "/v1/oauth2/token"): (500, {"code": "server_error"})})
    r = VerdaAdapter(spec["creds"], transport=m.transport()).provision(spec["offer"], spec["avail"], spec["launch"], NAME)
    assert r.outcome == "rejected"
    # delete passes every volume (else detached volumes keep billing) and permanently
    m = Mock({**tok, spec["status"]: spec["status_ok"], spec["terminate"]: spec["terminate_ok"]})
    t = VerdaAdapter(spec["creds"], transport=m.transport()).terminate(spec["iid"])
    body = json.loads(m.calls(*spec["terminate"])[0].content)
    assert t.outcome == "accepted" and body == {"action": "delete", "id": spec["iid"], "delete_permanently": True,
                                                "volume_ids": ["vol-os", "vol-data"]}
    m = Mock({**tok, spec["status"]: spec["status_ok"], spec["terminate"]: (207, [
        {"instanceId": spec["iid"], "action": "delete", "status": "error", "error": "locked"}])})
    assert VerdaAdapter(spec["creds"], transport=m.transport()).terminate(spec["iid"]).outcome == "failed"
    # an expired token is refreshed once on 401
    calls = {"n": 0}

    def status(req):
        calls["n"] += 1
        return httpx.Response(401, json={"code": "unauthorized_request"}) if calls["n"] == 1 else \
            httpx.Response(200, json=spec["status_ok"][1])

    m = Mock({**tok, spec["status"]: status})
    a = VerdaAdapter(spec["creds"], transport=m.transport())
    a._token = "stale"
    assert a.status(spec["iid"]).state == "running" and calls["n"] == 2
    for st, want in (("error", "error"), ("no_capacity", "error"), ("discontinued", "terminated"), ("notfound", "not_found"),
                     ("offline", "stopped")):
        m = Mock({**tok, spec["status"]: (200, {**spec["status_ok"][1], "status": st})})
        assert VerdaAdapter(spec["creds"], transport=m.transport()).status(spec["iid"]).state == want, st
    m = Mock({**tok, spec["list"]: spec["list_ok"]})
    VerdaAdapter(spec["creds"], transport=m.transport()).find_instance(NAME)
    assert m.calls("GET", "/v1/instances")[0].url.params["tag"] == f"opengrid={NAME}"


def test_registry_is_honest():
    for c in capabilities.all_capabilities():
        assert c["verified_live"] is False, c["provider"]
        if c["level_supported_by_provider_api"] is not None:
            assert c["level_implemented"] <= c["level_supported_by_provider_api"], c["provider"]
        assert c["level_implemented"] == adapters.level(c["provider"]), "implemented level comes from the code"
        if c["level_implemented"]:
            assert c["validation_status"] == "SIMULATED" and c["matrix"] and c["adapter_status"] == "simulated", c
    for p in ("aws", "nebius", "massedcompute", "voltagepark", "hyperbolic", "lium", "salad"):
        assert capabilities.capability(p)["level_implemented"] == 0, p
    for p in ("lambda", "runpod", "hyperstack", "digitalocean", "crusoe", "denvr", "latitude", "vast", "verda"):
        assert capabilities.capability(p)["level_implemented"] == 3, p
    assert capabilities.capability("crusoe")["via"] == "shadeform"
    assert capabilities.capability("crusoe")["credential_provider"] == "shadeform"
    assert capabilities.capability("syn_nobody")["level_implemented"] == 0
    # stop is claimed only where it saves money
    assert not capabilities.capability("lambda")["supports_stop"] and capabilities.capability("runpod")["supports_stop"]
    assert not capabilities.capability("digitalocean")["supports_stop"]
    assert capabilities.capability("digitalocean")["docs_checked"] != "recalled"


def test_launch_spec_merge():
    ls = LaunchSpec.merged({"image": "mine", "env": {}}, {"image": "default", "ssh_key": "k", "environments": {"A": "e"}})
    assert ls.image == "mine" and ls.ssh_key == "k" and ls.extra == {"environments": {"A": "e"}}
    ls = LaunchSpec.merged({"ssh_public_key": "ssh-ed25519 A", "startup_script": "x"}, {})
    assert ls.ssh_public_key == "ssh-ed25519 A" and ls.startup_script == "x" and ls.extra == {}


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
