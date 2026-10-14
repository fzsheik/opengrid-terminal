"""Workloads: request validation, template registry, hardware requirements -> eligible GPUs, workload-aware
selection (market_best vs execution_ready), cost bounds with provider billing units, readiness blockers.

Scratch database with crafted listings, synthetic `syn_*` providers backed by fake adapters (never called:
every provider verb raises). No real provider is ever called.

Run:  .venv/Scripts/python tests/test_workloads.py
"""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import scratchdb  # noqa: E402

import hardware  # noqa: E402
import normalize  # noqa: E402
from accounts.auth import Principal  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, control, scoring  # noqa: E402
from routing.adapters.base import Adapter  # noqa: E402
from routing.adapters.results import PER_DEPLOYMENT, Capabilities  # noqa: E402
from sqlalchemy import text  # noqa: E402
from tables import ComputeListingRow  # noqa: E402
from workloads import hardware_req, templates  # noqa: E402
from workloads.select import billed_minutes, billing, select  # noqa: E402
from workloads.spec import WorkloadError, validate  # noqa: E402

DB = "og_test_workloads"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
SCOPES = frozenset({"data:read", "route:preview", "route:execute", "deployments:read"})
A10, H100, T4, L4, R4090, R3090, V100 = ("NVIDIA A10 24GB PCIe", "NVIDIA H100 80GB SXM5", "NVIDIA T4 16GB",
                                         "NVIDIA L4 24GB", "NVIDIA RTX 4090 24GB", "NVIDIA RTX 3090 24GB",
                                         "NVIDIA Tesla V100 16GB SXM2")
A100 = "NVIDIA A100 40GB PCIe"
PK = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f me@laptop"
WHO = Principal(kind="api_key", account_id=7, key_id=70, scopes=SCOPES)


def caps(startup="YES", forces="NO", unit=("per minute", "test"), minimum=("NO", "test")) -> Capabilities:
    c = Capabilities()
    c.startup_script = (startup, "test fake")
    c.forces_account_ssh_key = (forces, "test fake")
    c.billing_unit = unit
    c.minimum_commitment = minimum
    return c


CALLS: list = []


class Fake(Adapter):
    """Never called: selection must not touch a provider."""
    LEVEL = 3
    CREDENTIALS = ()
    CHECK_NEEDS_CREDENTIALS = False
    SSH_KEY_REGISTRATION = PER_DEPLOYMENT
    CAPABILITIES = caps()

    def _no(self, *a, **k):
        CALLS.append((self.provider, a))
        raise AssertionError("selection made a provider call")

    check_availability = provision = status = terminate = list_instances = _no


class FakeHourly(Fake):
    CAPABILITIES = caps(unit=("per hour", "test: billed per started hour"), minimum=("1 hour", "test: 1 h minimum"))


class FakeNoScript(Fake):
    CAPABILITIES = caps(startup="NO")


class FakeForcedKey(Fake):
    CAPABILITIES = caps(forces="YES")


class FakeNeedsKey(Fake):
    CREDENTIALS = ("api_key",)


FAKES = {"syn_a": Fake, "syn_b": Fake, "syn_c": Fake, "syn_d": Fake, "syn_hr": FakeHourly,
         "syn_ns": FakeNoScript, "syn_fk": FakeForcedKey, "syn_nk": FakeNeedsKey}


def L(prov, gpu, price, *, lid=None, region="us-east", country="US", market="on_demand", interruptible=False,
      available=True, age_min=1, count=1, vcpu=None, ram=None, disk=None, sku=None):
    return ComputeListingRow(
        provider=prov, listing_id=lid or f"{prov}:{gpu}:{market}", sku=sku or f"{prov}-sku", raw_gpu_name=gpu,
        canonical_gpu_name=gpu, gpu_count=count, region=region, country=country,
        price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(price * count)), currency="USD",
        market_type=market, provider_tier=None, interruptible=interruptible, available=available, capacity=None,
        capacity_unit=None, vcpu=vcpu, ram_gb=ram, storage_gb=disk, observed_at=NOW - timedelta(minutes=age_min),
        first_seen_at=NOW - timedelta(days=3))


def seed(Session):
    rows = [
        L("syn_a", A10, 0.75), L("syn_b", A10, 0.80),
        L("syn_a", A10, 0.20, market="spot", interruptible=True),              # spot: never a candidate
        L("syn_a", H100, 2.90), L("syn_b", H100, 2.80), L("syn_c", H100, 1.80),  # syn_c cheap vs ITS market
        L("syn_mkt", T4, 0.30),                                                # market data only (no adapter)
        L("syn_b", T4, 0.25, age_min=180),                                     # stale
        L("syn_c", L4, 0.60, region="eu-west", country="DE"),                  # Europe
        L("syn_d", R4090, 0.40, region=None, country=None),                    # location unknown
        L("syn_a", R3090, 0.35, vcpu=2, ram=64, disk=200),                     # too few vCPU
        L("syn_a", V100, 2.50), L("syn_hr", V100, 2.50),                       # same price, different billing
        L("syn_ns", V100, 2.60), L("syn_fk", V100, 2.60), L("syn_nk", V100, 2.60),
        L("syn_a", "NVIDIA B200 180GB SXM", 1.00),                             # Blackwell: GPU ineligible
        L("syn_a", "AMD Instinct MI300X 192GB", 0.50),                         # AMD: GPU ineligible
        L("syn_a", "NVIDIA GTX 1060 6GB", 0.05),                               # Pascal: GPU ineligible
    ]
    from store.accounts import Account
    with Session.begin() as s:
        s.add_all(rows)
        for aid in range(7, 12):
            s.add(Account(id=aid, name=f"acct{aid}", status="active", plan="free", settings={}, is_operator=False))


def body(**over):
    b = copy.deepcopy(templates.get_template("pytorch-mnist")["preset"])
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            b[k].update(v)
        else:
            b[k] = v
    return b


def refused(code, b):
    try:
        validate(b)
    except WorkloadError as e:
        assert e.code == code, (code, e.code, e.message)
        assert e.status == 422 and e.as_dict()["code"] == code and e.message
        return e
    raise AssertionError(f"expected {code}")


def reset():
    settings.routing_live_provisioning = False
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM provider_execution_flags"))
        s.execute(text("DELETE FROM execution_controls"))
    scoring.history.cache_clear()


def supervised(*providers, validated=True, env=True, mode="SUPERVISED"):
    settings.routing_live_provisioning = env
    control.set_mode(mode, reason="test", by="test")
    for p in providers:
        if validated:
            control.mark_validated(p, "dep-test", {"test": "synthetic"}, "test")
        control.set_provider_flags(p, reason="test", by="test", supervised_enabled=True)


def codes(out, provider=None, gpu=None):
    return {e["code"] for e in out["exclusions"] if (provider is None or e["provider"] == provider)
            and (gpu is None or e["gpu"] == gpu)}


def reasons(c):
    return {r["code"] for r in c["readiness"]["reasons"]}


# --------------------------------------------------------------------------
# spec validation
# --------------------------------------------------------------------------

def test_preset_valid_and_idempotent():
    v = validate(body())
    r = v.request
    assert r["name"] == "opengrid-first-training-test" and r["template"] == "pytorch-mnist"
    assert r["config"] == {"epochs": 3, "batch_size": 128, "learning_rate": 0.001, "seed": 42}
    assert r["resources"]["gpu"] == "auto" and r["resources"]["region"] == "US" and r["resources"]["gpu_count"] == 1
    assert r["routing"]["strategy"] == "BALANCED"
    assert r["limits"] == {"max_price_per_gpu_hour_usd": 3.0, "max_total_cost_usd": 2.0, "max_runtime_minutes": 30}
    assert v.requirements["min_vram_gb"] == 8 and v.requirements["min_compute_capability"] == 7.0
    assert validate(r).request == r, "a normalised request re-validates to itself"
    # config defaults fill in; slugs resolve to canonical names; lowercase strategy / region normalise
    v2 = validate(body(config={}, resources={"gpu": "a10-24gb-pcie", "region": "us"}, routing={"strategy": "cheapest"}))
    assert v2.request["config"]["epochs"] == 3 and v2.request["resources"]["gpu"] == A10
    assert v2.request["resources"]["region"] == "US" and v2.request["routing"]["strategy"] == "CHEAPEST"
    v3 = validate(body(ssh_public_key=PK))
    assert v3.ssh_key_fingerprint.startswith("SHA256:") and v3.request["ssh_public_key"] == PK
    # a lower user VRAM minimum never weakens the template's
    v4 = validate(body(resources={"min_vram_gb": 2}))
    assert v4.requirements["min_vram_gb"] == 8 and any("below the template" in n for n in v4.notes)
    assert validate(body(resources={"min_vram_gb": 40})).requirements["min_vram_gb"] == 40


def test_templates_registry():
    ts = templates.list_templates()
    assert [t["id"] for t in ts] == ["pytorch-mnist"]
    t = templates.get_template("pytorch-mnist")
    assert t["title"] == "Train Your First AI Model: PyTorch MNIST"
    assert t["framework"]["torch"] == "2.4.1" and t["framework"]["torchvision"] == "0.19.1" and t["framework"]["cuda"] == "12.1"
    assert t["expected_runtime"]["kind"] == "estimated"
    assert templates.expected_minutes(t, {"epochs": 3}) == (6, 15)
    assert templates.get_template("nope") is None
    t["title"] = "mutated"
    assert templates.get_template("pytorch-mnist")["title"] != "mutated", "registry returns copies"


def test_spec_invalid_and_missing():
    b = body()
    del b["limits"]
    refused("missing_field", b)
    b = body()
    del b["limits"]["max_total_cost_usd"]
    refused("missing_field", b)
    b = body()
    del b["job_type"]
    refused("missing_field", b)
    refused("unknown_field", body(command="rm -rf /"))
    refused("unknown_field", body(resources={"image": "evil/image"}))
    refused("invalid_job_type", body(job_type="mining"))
    refused("missing_field", body(template=None))
    refused("unknown_template", body(template="pytorch-imagenet"))
    refused("invalid_request", "not an object")
    refused("invalid_name", body(name="Bad Name!"))


def test_unsupported_job_types():
    for jt in ("finetuning", "inference", "batch_compute", "custom_container"):
        e = refused("unsupported_workload_type", body(job_type=jt))
        assert jt in e.message and "not supported" in e.message and e.extra["job_type"] == jt


def test_bounds():
    for cfg in ({"epochs": 0}, {"epochs": 11}, {"batch_size": 16}, {"batch_size": 1024}, {"learning_rate": 1.0},
                {"learning_rate": 1e-7}, {"seed": -1}):
        refused("config_out_of_bounds", body(config=cfg))
    for cfg in ({"epochs": "three"}, {"epochs": 2.5}, {"epochs": True}, {"learning_rate": "fast"},
                {"dropout": 0.5}):
        refused("invalid_config", body(config=cfg))
    assert validate(body(config={"epochs": 10, "batch_size": 32, "learning_rate": 0.1})).request["config"]["epochs"] == 10
    refused("gpu_count_out_of_bounds", body(resources={"gpu_count": 2}))
    refused("limit_out_of_bounds", body(limits={"max_runtime_minutes": 4}))
    refused("limit_out_of_bounds", body(limits={"max_runtime_minutes": 61}))
    refused("limit_out_of_bounds", body(limits={"max_price_per_gpu_hour_usd": 0}))
    refused("limit_out_of_bounds", body(limits={"max_price_per_gpu_hour_usd": -1}))
    refused("limit_out_of_bounds", body(limits={"max_total_cost_usd": 0}))
    assert validate(body(limits={"max_runtime_minutes": 5})).request["limits"]["max_runtime_minutes"] == 5
    old = settings.runtime_hard_max_minutes
    settings.runtime_hard_max_minutes = 20   # the platform hard max caps the template's 60
    try:
        refused("limit_out_of_bounds", body(limits={"max_runtime_minutes": 30}))
        assert validate(body(limits={"max_runtime_minutes": 20}))
    finally:
        settings.runtime_hard_max_minutes = old
    v = validate(body(config={"epochs": 10}, limits={"max_runtime_minutes": 10}))
    assert any("expected runtime band" in n for n in v.notes)


def test_other_fields():
    refused("spot_not_supported", body(resources={"allow_spot": True}))
    refused("invalid_region", body(resources={"region": "Mars"}))
    refused("invalid_region", body(resources={"region": None, "strict_region": True}))
    refused("cuda_required", body(resources={"cuda_required": False}))
    refused("invalid_resources", body(resources={"min_vram_gb": -4}))
    refused("unknown_gpu", body(resources={"gpu": "quantum-9000"}))
    e = refused("gpu_family_not_allowed", body(resources={"gpu": "h100"}))
    assert "different products" in e.message
    refused("invalid_strategy", body(routing={"strategy": "YOLO"}))
    refused("invalid_strategy", body(routing={"strategy": "USER_DEFINED", "weights": None}))
    refused("invalid_strategy", body(routing={"strategy": "BALANCED", "weights": {"price": 1}}))
    assert validate(body(routing={"strategy": "USER_DEFINED", "weights": {"price": 2, "availability_now": 1}}))
    refused("ssh_private_key_rejected", body(ssh_public_key="-----BEGIN OPENSSH PRIVATE KEY-----\nabc"))
    refused("ssh_public_key_invalid", body(ssh_public_key="ssh-ed25519 notbase64!!"))
    refused("unsupported_artifact_destination", body(artifact_destination="s3://my-bucket"))
    refused("dataset_not_allowed", body(dataset_ref="https://evil.example/data.tar"))
    assert validate(body(dataset_ref="mnist")).request["dataset_ref"] == "mnist"
    assert validate(body(sandbox=True)).request["sandbox"] is True


# --------------------------------------------------------------------------
# hardware requirements
# --------------------------------------------------------------------------

def test_auto_gpu_resolution():
    req = validate(body()).requirements
    r = hardware_req.resolve(req)
    ok = {g["gpu"] for g in r["eligible"]}
    bad = {g["gpu"]: g for g in r["ineligible"]}
    for g in (A10, H100, T4, L4, R4090, V100, "NVIDIA A100 80GB SXM4", "NVIDIA RTX 2070 8GB"):
        assert g in ok, g
    assert bad["AMD Instinct MI300X 192GB"]["code"] == "wrong_vendor"
    assert bad["Intel Gaudi 2 96GB"]["code"] == "wrong_vendor"
    assert bad["NVIDIA GTX 1060 6GB"]["code"] == "compute_capability_too_low"
    assert bad["NVIDIA GTX 1660 6GB"]["code"] == "insufficient_vram"
    assert bad["NVIDIA B200 180GB SXM"]["code"] == "compute_capability_unsupported"
    assert bad["NVIDIA RTX 5090 32GB"]["code"] == "compute_capability_unsupported"
    assert bad["NVIDIA GH200 96GB"]["code"] == "host_cpu_unsupported"
    assert not ok & set(bad)
    # compute capability per architecture (variants of one architecture differ: A100 8.0 vs A10 8.6)
    cc = {g: hardware_req.compute_capability(g)[0] for g in (V100, T4, A100, A10, L4, H100, "NVIDIA B200 180GB SXM",
                                                             "NVIDIA RTX 5090 32GB")}
    assert cc == {V100: 7.0, T4: 7.5, A100: 8.0, A10: 8.6, L4: 8.9, H100: 9.0, "NVIDIA B200 180GB SXM": 10.0,
                  "NVIDIA RTX 5090 32GB": 12.0}, cc
    # VRAM requirement
    r40 = hardware_req.resolve({**req, "min_vram_gb": 40})
    ok40 = {g["gpu"] for g in r40["eligible"]}
    assert H100 in ok40 and A100 in ok40 and A10 not in ok40 and T4 not in ok40


def test_unknown_spec_excluded():
    req = validate(body()).requirements
    assert hardware_req.check_gpu("NVIDIA Imaginary 99GB", req)["code"] == "hardware_spec_unknown"
    hardware._SPECS["NVIDIA Test Rubin 288GB"] = {**hardware._SPECS[H100], "architecture": "Rubin"}
    hardware._SPECS["NVIDIA Test NoVRAM"] = {**hardware._SPECS[A10], "vram_gb": None}
    try:
        r = hardware_req.check_gpu("NVIDIA Test Rubin 288GB", req)
        assert not r["eligible"] and r["code"] == "hardware_spec_unknown" and "hardware spec unknown" in r["reason"]
        r = hardware_req.check_gpu("NVIDIA Test NoVRAM", req)
        assert not r["eligible"] and r["code"] == "hardware_spec_unknown"
        inel = {g["gpu"]: g["code"] for g in hardware_req.resolve(req)["ineligible"]}
        assert inel["NVIDIA Test Rubin 288GB"] == "hardware_spec_unknown"
    finally:
        hardware._SPECS.pop("NVIDIA Test Rubin 288GB", None)
        hardware._SPECS.pop("NVIDIA Test NoVRAM", None)


def test_explicit_gpu():
    reset()
    for g in ("rx-7900-xtx-24gb", "gtx-1060-6gb", "b200-180gb-sxm", "gtx-1650-4gb"):
        try:
            select(body(resources={"gpu": g}), WHO, now=NOW)
        except WorkloadError as e:
            assert e.code == "gpu_incompatible" and "cannot run this workload" in e.message, e.message
        else:
            raise AssertionError(f"{g} accepted")
    out = select(body(resources={"gpu": "a10-24gb-pcie"}), WHO, now=NOW)
    assert out["gpu_selection"] == "explicit" and [g["gpu"] for g in out["eligible_gpus"]] == [A10]
    assert out["market_best"]["gpu"] == A10
    try:
        select(body(resources={"gpu": "a10-24gb-pcie", "min_vram_gb": 40}), WHO, now=NOW)
    except WorkloadError as e:
        assert e.code == "gpu_incompatible" and "VRAM" in e.message
    else:
        raise AssertionError("A10 accepted with 40 GB minimum")


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def test_balanced_prefers_cheap_compatible_gpu():
    reset()
    out = select(body(), WHO, now=NOW)
    mb = out["market_best"]
    assert mb["gpu"] == A10 and mb["provider"] == "syn_a", (mb["gpu"], mb["provider"], mb["explanation"])
    # within its OWN market the cheap H100 scores higher than the A10: rank_listings alone would pick it
    rh = scoring.rank_listings(H100, region_group="US", max_price=3, mode="BALANCED", now=NOW)["candidates"][0]
    ra = scoring.rank_listings(A10, region_group="US", max_price=3, mode="BALANCED", now=NOW)["candidates"][0]
    assert rh["provider"] == "syn_c" and rh["score"] > ra["score"]
    assert all(c["score"] <= mb["score"] for c in out["alternatives"])
    assert "median compatible-GPU price" in mb["explanation"] and mb["why"].startswith("BALANCED: rank 1")
    assert out["workload_market"]["median"] > 0
    assert mb["factors"]["price"]["raw"]["gpu_market_median"] is not None
    for k in ("template", "requirements", "eligible_gpus", "ineligible_gpus", "market_best", "execution_ready",
              "alternatives", "exclusions", "blockers", "estimate", "strategy", "as_of", "market_data_freshness"):
        assert k in out, k
    assert CALLS == []


def test_cheapest_market_best_vs_execution_ready():
    reset()
    supervised("syn_d", "syn_a")
    out = select(body(routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    mb, er = out["market_best"], out["execution_ready"]
    assert mb["provider"] == "syn_mkt" and mb["gpu"] == T4 and not mb["executable"]
    assert "not_provisionable" in reasons(mb) and "NOT executable now" in mb["why"]
    assert er["provider"] == "syn_d" and er["gpu"] == R4090 and er["executable"]
    assert er["readiness"]["approval_mode"] == "SUPERVISED" and "not executable now" in er["why"]
    assert er["route_spec"]["preferences"] == {"include_providers": ["syn_d"]} and er["route_spec"]["gpu"] == R4090
    assert er["route_spec"]["listing_id"] == er["listing_id"] and er["limit_violations"] == []
    assert all(a["executable"] and a["label"] == "executable" for a in out["alternatives"])
    assert [a["provider"] for a in out["alternatives"]][:1] == ["syn_a"]
    assert out["blockers"] == [] and out["estimate"]["for"] == "execution_ready"
    assert out["estimate"]["provider"] == "syn_d"


def test_insufficient_vram():
    reset()
    out = select(body(resources={"min_vram_gb": 40}), WHO, now=NOW)
    gpus = {g["gpu"] for g in out["eligible_gpus"]}
    assert A10 not in gpus and T4 not in gpus and H100 in gpus
    assert any(g["gpu"] == A10 and g["code"] == "insufficient_vram" for g in out["ineligible_gpus"])
    assert out["market_best"]["gpu"] == H100 and out["market_best"]["provider"] == "syn_c"


def test_region_strict_and_non_strict():
    reset()
    out = select(body(routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    assert "wrong_region" in codes(out, "syn_c", L4)
    assert any(c["provider"] == "syn_d" for c in [out["market_best"]] + out["alternatives"]), "unknown location kept"
    out = select(body(routing={"strategy": "CHEAPEST"}, resources={"strict_region": True}), WHO, now=NOW)
    assert "region_unconfirmed" in codes(out, "syn_d", R4090)
    allc = [out["market_best"]] + out["alternatives"]
    assert all(c["provider"] != "syn_d" for c in allc)
    out = select(body(resources={"region": "Europe"}), WHO, now=NOW)   # non-strict: unknown location stays in
    assert {c["provider"] for c in [out["market_best"]] + out["alternatives"]} == {"syn_c", "syn_d"}
    out = select(body(resources={"region": "Europe", "strict_region": True}), WHO, now=NOW)
    assert out["market_best"]["gpu"] == L4 and out["market_best"]["provider"] == "syn_c"
    assert out["alternatives"] == []
    out = select(body(resources={"region": None}, routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    assert out["market_best"]["provider"] == "syn_mkt"


def test_spot_and_stale_excluded():
    reset()
    out = select(body(routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    spot = [e for e in out["exclusions"] if e["listing_id"] == f"syn_a:{A10}:spot"]
    assert spot and spot[0]["code"] == "not_on_demand"
    stale = [e for e in out["exclusions"] if e["provider"] == "syn_b" and e["gpu"] == T4]
    assert stale and stale[0]["code"] == "stale" and "stale" in stale[0]["reason"]
    assert out["market_data_freshness"]["stale_excluded"] >= 1
    allc = [out["market_best"]] + out["alternatives"]
    assert all(c["price_per_gpu_hour"] not in (0.20, 0.25) for c in allc)
    # ineligible GPUs' listings never appear at all
    assert not any(e["gpu"] in ("NVIDIA B200 180GB SXM", "AMD Instinct MI300X 192GB") for e in out["exclusions"])


def test_resource_minimums():
    reset()
    out = select(body(routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    e = [e for e in out["exclusions"] if e["provider"] == "syn_a" and e["gpu"] == R3090]
    assert e and e[0]["code"] == "insufficient_vcpu" and "2 vCPU" in e[0]["reason"]
    mb = out["market_best"]
    assert mb["resources"]["vcpu"] is None and "not reported" in mb["resources"]["note"]


def test_unvalidated_never_execution_ready():
    reset()
    supervised("syn_a", validated=False)   # supervised flag on, adapter still simulated
    supervised("syn_d")
    out = select(body(), WHO, now=NOW)
    mb, er = out["market_best"], out["execution_ready"]
    assert mb["provider"] == "syn_a" and not mb["executable"]
    r = [x for x in mb["readiness"]["reasons"] if x["code"] == "adapter_not_validated"]
    assert r and r[0]["reason"] == "syn_a: adapter simulated; run the supervised validation first"
    assert er is not None and er["provider"] == "syn_d" and er["provider"] != "syn_a"
    # nobody validated -> nothing executable, blockers explain each provider
    reset()
    supervised(validated=False)
    out = select(body(), WHO, now=NOW)
    assert out["execution_ready"] is None and out["market_best"] is not None
    bl = {(b["code"], b.get("provider")) for b in out["blockers"]}
    assert ("adapter_not_validated", "syn_a") in bl and ("supervised_not_enabled", "syn_a") in bl
    nb = [b for b in out["blockers"] if b["code"] == "not_provisionable"]
    assert nb and nb[0]["providers"] == ["syn_mkt"]
    assert len(bl) == len(out["blockers"]), "blockers are deduplicated"
    assert all(a["label"] == "not executable" for a in out["alternatives"])
    assert out["estimate"]["for"] == "market_best"


def test_killed_provider():
    reset()
    supervised("syn_a", "syn_b", "syn_d")
    control.kill_provider("syn_a", "incident", "test")
    out = select(body(), WHO, now=NOW)
    a = out["market_best"]
    assert a["provider"] == "syn_a" and "provider_killed" in reasons(a)
    assert out["execution_ready"]["provider"] != "syn_a"


def test_mode_and_env_blockers():
    reset()
    supervised("syn_a", env=False)          # stored SUPERVISED, env ceiling off -> effective PREVIEW_ONLY
    out = select(body(), WHO, now=NOW)
    assert out["execution_mode"] == "PREVIEW_ONLY" and out["execution_ready"] is None
    bl = {b["code"] for b in out["blockers"]}
    assert "env_ceiling" in bl and "mode_preview_only" not in bl
    assert any("ROUTING_LIVE_PROVISIONING" in b["reason"] for b in out["blockers"])
    settings.routing_live_provisioning = True
    control.set_mode("PREVIEW_ONLY", reason="test", by="test")
    out = select(body(), WHO, now=NOW)
    assert {b["code"] for b in out["blockers"]} >= {"mode_preview_only"} and out["execution_ready"] is None
    assert "env_ceiling" not in {b["code"] for b in out["blockers"]}
    control.set_mode("DISABLED", reason="test", by="test")
    out = select(body(), WHO, now=NOW)
    assert "mode_disabled" in {b["code"] for b in out["blockers"]} and out["execution_ready"] is None
    control.set_mode("SUPERVISED", reason="test", by="test")
    out = select(body(), WHO, now=NOW)
    assert out["execution_ready"]["provider"] == "syn_a" and out["blockers"] == []
    # LIVE mode with a live-enabled provider -> approval_mode LIVE
    control.set_mode("LIVE", reason="test", by="test")
    control.set_provider_flags("syn_a", reason="test", by="test", live_enabled=True)
    out = select(body(), WHO, now=NOW)
    assert out["execution_ready"]["readiness"]["approval_mode"] == "LIVE"


def test_billing_minimum_over_budget():
    reset()
    supervised("syn_a", "syn_hr")
    out = select(body(resources={"gpu": "tesla-v100-16gb-sxm2"}), WHO, now=NOW)
    e = [x for x in out["exclusions"] if x["provider"] == "syn_hr"]
    assert e and e[0]["code"] == "over_budget", e
    assert "$2.50" in e[0]["reason"] and "1 h billed" in e[0]["reason"] and "$2.00" in e[0]["reason"], e[0]["reason"]
    er = out["execution_ready"]
    assert er["provider"] == "syn_a" and er["cost"]["billed_minutes"] == 30 and er["cost"]["est_max_total"] == 1.25
    est = out["estimate"]
    assert est["est_max_total"] == 1.25 and est["billed_minutes"] == 30 and est["est_typical_total"] == round(2.5 * 15 / 60, 4)
    assert est["est_typical_range"] == [0.25, 0.625] and "billing unit" in est["basis"]
    # billing-unit rounding and assumptions
    assert billed_minutes(30, {"unit_minutes": 60, "minimum_minutes": 0}) == 60
    assert billed_minutes(30, {"unit_minutes": 10, "minimum_minutes": 0}) == 30
    assert billed_minutes(31, {"unit_minutes": 10, "minimum_minutes": 0}) == 40
    assert billed_minutes(5, {"unit_minutes": 1 / 60, "minimum_minutes": 1}) == 5
    assert billed_minutes(0.5, {"unit_minutes": 1 / 60, "minimum_minutes": 1}) == 1
    b = billing("syn_mkt")
    assert b["assumed"] and b["unit_minutes"] == 60 and "assumed" in b["basis"]
    assert billing("lambda")["unit_minutes"] == 1 and billing("digitalocean")["minimum_minutes"] == 1
    assert billing("verda")["unit_minutes"] == 10
    # the market-only T4 at $0.30 is billed per (assumed) hour: max $0.30 <= $2 so it stays
    out = select(body(routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    assert out["market_best"]["cost"]["billed_minutes"] == 60 and out["market_best"]["cost"]["billing_assumed"]


def test_capability_and_credential_blockers():
    reset()
    supervised("syn_ns", "syn_fk", "syn_nk")
    out = select(body(resources={"gpu": "tesla-v100-16gb-sxm2"}, routing={"strategy": "CHEAPEST"}), WHO, now=NOW)
    by = {c["provider"]: c for c in [out["market_best"]] + out["alternatives"]}
    assert out["execution_ready"] is None
    allc = {}
    for b in out["blockers"]:
        allc.setdefault(b.get("provider"), set()).add(b["code"])
    assert "no_startup_script" in allc["syn_ns"]
    assert "forces_account_ssh_key" in allc["syn_fk"]
    assert "no_credentials" in allc["syn_nk"]
    assert "adapter_not_validated" in allc["syn_a"] and "adapter_not_validated" not in allc.get("syn_ns", set())
    assert by  # candidates were listed


def test_lambda_readiness_without_provider_calls():
    """The real LambdaAdapter: no key -> no_credentials; with a key + validated + supervised -> executable,
    and the job's own key is registered per deployment (no account key). No HTTP happens."""
    reset()
    with normalize.SessionLocal.begin() as s:
        s.add(L("lambda", A100, 1.29, lid="lambda:gpu_1x_a100", region="us-east-1", country=None, sku="gpu_1x_a100"))
    old = settings.lambda_api_key
    try:
        settings.lambda_api_key = None
        supervised("lambda")
        out = select(body(resources={"gpu": "a100-40gb-pcie"}), WHO, now=NOW)
        mb = out["market_best"]
        assert mb["provider"] == "lambda" and "no_credentials" in reasons(mb) and out["execution_ready"] is None
        settings.lambda_api_key = "test-not-real"
        out = select(body(resources={"gpu": "a100-40gb-pcie"}, ssh_public_key=PK), WHO, now=NOW)
        er = out["execution_ready"]
        assert er is not None and er["provider"] == "lambda", out["blockers"]
        assert er["cost"]["billing_unit"] == "per minute" and er["cost"]["billed_minutes"] == 30
        assert er["readiness"]["credential_source"] == "opengrid"
        reset()
        settings.routing_live_provisioning = True
        control.set_mode("SUPERVISED", reason="test", by="test")
        out = select(body(resources={"gpu": "a100-40gb-pcie"}), WHO, now=NOW)
        r = [x for x in out["market_best"]["readiness"]["reasons"] if x["code"] == "adapter_not_validated"]
        assert r and r[0]["reason"] == "lambda: adapter simulated; run the supervised validation first"
    finally:
        settings.lambda_api_key = old
        with normalize.SessionLocal.begin() as s:
            s.execute(text("DELETE FROM compute_listings WHERE provider = 'lambda'"))


def test_no_eligible_provider():
    reset()
    out = select(body(limits={"max_total_cost_usd": 0.01}), WHO, now=NOW)
    assert out["market_best"] is None and out["execution_ready"] is None and out["estimate"] is None
    assert [b["code"] for b in out["blockers"]] == ["no_eligible_listing"]
    assert "over_budget" in out["blockers"][0]["reason"] and out["exclusions_by_code"]["over_budget"] >= 1
    out = select(body(resources={"gpu": "rtx-a4000-16gb"}), WHO, now=NOW)
    assert out["eligible_gpus"][0]["listings"] == 0 and out["blockers"][0]["code"] == "no_eligible_listing"
    out = select(body(limits={"max_price_per_gpu_hour_usd": 0.1}), WHO, now=NOW)
    assert out["market_best"] is None and "over_max_price" in out["exclusions_by_code"]


def test_unsupported_type_in_select():
    try:
        select(body(job_type="inference"), WHO, now=NOW)
    except WorkloadError as e:
        assert e.code == "unsupported_workload_type"
    else:
        raise AssertionError("inference accepted")


def test_deterministic():
    reset()
    supervised("syn_a", "syn_d")
    a = select(body(), WHO, now=NOW)
    b = select(body(), WHO, now=NOW)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    ex = [(e["gpu"], e["provider"], str(e["listing_id"]), e["code"]) for e in a["exclusions"]]
    assert ex == sorted(ex)
    assert [g["gpu"] for g in a["eligible_gpus"]] == sorted(g["gpu"] for g in a["eligible_gpus"])
    json.dumps(a)   # JSON-serialisable as is
    assert CALLS == []


# --------------------------------------------------------------------------

def main():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    seed(Session)
    scoring.history.cache_clear()
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:  # noqa: BLE001
        pass
    for p, cls in FAKES.items():
        adapters.register(p, cls)
    try:
        tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
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
        if failed:
            sys.exit(1)
    finally:
        for p in FAKES:
            adapters.unregister(p)
        settings.routing_live_provisioning = False
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    main()
