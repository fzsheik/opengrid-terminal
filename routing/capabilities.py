"""What each provider's API allows, and what OpenGrid actually implements. Hand-maintained.

Integration levels
    0  market data only        OpenGrid reads prices; cannot check, quote live or launch
    1  availability check      a live, per-request stock check
    2  provisioning            OpenGrid can launch an instance through the API
    3  full lifecycle          launch, inspect status, terminate (stop where the API has it)

Three separate claims per provider, never merged:
    level_supported_by_provider_api   what the provider's public API documents (None = not established)
    level_implemented                 what OpenGrid's adapter does: read from routing.adapters.level(),
                                      so it cannot exceed the code
    verified_live                     False everywhere: no adapter has made a real call against a
                                      provider account yet; adapters are tested against mocked HTTP
                                      built from the documented request/response shapes
    availability_check_verified_live  True only where the check reads a public endpoint and was run
                                      against the real API (read-only): Vast, and Shadeform's catalogue

docs_checked says how the API facts were established:
    "docs (fetched 2026-10)"   the provider's API reference was read while building this
    "docs (search 2026-10)"    found via a web search of the provider's docs, not read end to end
    "recalled"                 from prior knowledge of the API; re-check before going live
"""

from __future__ import annotations

from config import settings
from routing import adapters

LEVELS = {0: "market data only", 1: "availability check", 2: "provisioning", 3: "full lifecycle"}

# Availability checks that read a PUBLIC endpoint and were exercised against the real API
# (read-only, no account) on 2026-10-07. Provisioning has been verified nowhere.
AVAILABILITY_CHECK_VERIFIED_LIVE = {"vast", "crusoe", "denvr", "latitude"}

OPENGRID_MANAGED, BYO, COMMERCIAL = "opengrid_managed_key", "byo", "commercial_agreement"

_REG: dict[str, dict] = {
    "lambda": dict(
        api=3, resource="vm", docs="https://docs.lambda.ai/api/cloud", docs_checked="docs (fetched 2026-10)",
        credential=OPENGRID_MANAGED, settings=["lambda_api_key"],
        notes="Launch needs region_name, instance_type_name and ssh_key_names (keys registered in the Lambda "
              "account). Status booting/active/unhealthy/terminating/terminated/preempted. No stop endpoint "
              "(terminate or restart only). Capacity error code instance-operations/launch/insufficient-capacity."),
    "runpod": dict(
        api=3, resource="container pod", docs="https://docs.runpod.io/api-reference",
        docs_checked="docs (fetched 2026-10)", credential=OPENGRID_MANAGED, settings=["runpod_api_key"],
        notes="Pods are containers, not VMs: a container image is required. REST v1 (rest.runpod.io) creates, "
              "gets, stops and deletes pods; stock comes from the GraphQL lowestPrice the poller already reads. "
              "Only SECURE cloud is routed (community cloud is interruptible and excluded from the market). "
              "desiredStatus is RunPod's target state, not proof the container is up."),
    "hyperstack": dict(
        api=3, resource="vm", docs="https://docs.hyperstack.cloud/docs/api-reference/",
        docs_checked="docs (search 2026-10)", credential=OPENGRID_MANAGED, settings=["hyperstack_api_key"],
        notes="A VM needs an environment (region-bound), a key pair registered in it, an image name and a flavor. "
              "Environments per region come from ROUTING_LAUNCH_DEFAULTS. Stop is offered; whether a stopped VM "
              "still bills is not verified (hibernate exists for that)."),
    "digitalocean": dict(
        api=3, resource="vm (GPU Droplet)",
        docs="https://docs.digitalocean.com/reference/api/digitalocean/#tag/Droplets", docs_checked="recalled",
        credential=OPENGRID_MANAGED, settings=["digitalocean_api_key"],
        notes="POST /v2/droplets with region, size slug, image (e.g. gpu-h100x1-base) and ssh key ids. "
              "Power-off is offered but a powered-off Droplet is still billed; only destroy ends spend."),
    "crusoe": dict(
        api=3, resource="vm", docs="https://docs.shadeform.ai/api-reference/instances/instances-create",
        docs_checked="docs (fetched 2026-10)", credential=OPENGRID_MANAGED, settings=["shadeform_api_key"],
        via="shadeform",
        notes="Routed through Shadeform (an aggregator): one Shadeform key, Shadeform is the counterparty and "
              "the price is Shadeform's listing. Crusoe's own Cloud API also supports full VM lifecycle but "
              "OpenGrid has no direct Crusoe account (would need one; not verified)."),
    "denvr": dict(
        api=3, resource="vm", docs="https://docs.shadeform.ai/api-reference/instances/instances-create",
        docs_checked="docs (fetched 2026-10)", credential=OPENGRID_MANAGED, settings=["shadeform_api_key"],
        via="shadeform",
        notes="Routed through Shadeform (an aggregator route; see crusoe). Denvr's own API is not used."),
    "latitude": dict(
        api=3, resource="vm / bare metal", docs="https://docs.shadeform.ai/api-reference/instances/instances-create",
        docs_checked="docs (fetched 2026-10)", credential=OPENGRID_MANAGED, settings=["shadeform_api_key"],
        via="shadeform",
        notes="Routed through Shadeform (an aggregator route; see crusoe). Latitude.sh's own server API is "
              "not used. Bare-metal types can take far longer to become active than VMs."),
    "vast": dict(
        api=3, resource="container instance", docs="https://docs.vast.ai/api-reference/instances/create-instance",
        docs_checked="docs (fetched 2026-10)", credential=OPENGRID_MANAGED, settings=["vast_api_key"],
        notes="Rents ONE host's ask: PUT /asks/{ask_id} needs an ask id from a live search plus a docker image. "
              "OpenGrid's Vast listing is the median ask, so the quote is the specific ask's price. Hosts vary in "
              "reliability; Vast's per-host reliability score is kept in provider metadata, not used in scoring."),
    "verda": dict(
        api=3, resource="vm", docs="https://api.verda.com/v1/docs", docs_checked="docs (fetched 2026-10)",
        credential=OPENGRID_MANAGED, settings=["verda_client_id", "verda_client_secret"],
        notes="OAuth2 client credentials (client id + secret), then POST /v1/instances with instance_type, image, "
              "location_code and ssh_key_ids; 503 means no capacity. Availability per location needs auth."),
    "aws": dict(
        api=3, resource="vm (EC2)", docs="https://docs.aws.amazon.com/AWSEC2/latest/APIReference/",
        docs_checked="recalled", credential=COMMERCIAL, settings=[],
        notes="EC2 RunInstances / DescribeInstances / TerminateInstances support the full lifecycle, but need IAM "
              "credentials and SigV4 request signing (boto3 deliberately not added). GPU capacity usually needs "
              "service-quota increases. OpenGrid reads the public price file only (list prices, no stock)."),
    "nebius": dict(
        api=3, resource="vm", docs="https://docs.nebius.com/compute/", docs_checked="recalled",
        credential=COMMERCIAL, settings=[],
        notes="Nebius AI Cloud has a compute API (gRPC, service-account key auth). OpenGrid reads only the public "
              "price page; no adapter."),
    "massedcompute": dict(
        api=3, resource="vm", docs="https://vm-docs.massedcompute.com/api/v1", docs_checked="docs (search 2026-10)",
        credential=COMMERCIAL, settings=[],
        notes="A VM API (Bearer token) and a partner Inventory API exist; tokens are issued on request "
              "(techadmin@massedcompute.com). OpenGrid reads only the public price page; no adapter."),
    "voltagepark": dict(
        api=3, resource="vm / bare metal", docs="https://docs.voltagepark.com/on-demand/api",
        docs_checked="docs (search 2026-10)", credential=BYO, settings=[],
        notes="On-Demand API (cloud-api.voltagepark.com/api/v1, Bearer token) deploys, power-cycles and terminates "
              "VMs and bare metal. OpenGrid reads its public endpoints only; no adapter yet."),
    "hyperbolic": dict(
        api=3, resource="vm / bare metal", docs="https://www.hyperbolic.ai/docs/on-demand/managing-instances",
        docs_checked="docs (search 2026-10)", credential=BYO, settings=[],
        notes="/v2/on-demand/* create, list and terminate rentals (the API moved from v1 recently). OpenGrid reads "
              "the public rental options only; no adapter."),
    "lium": dict(
        api=3, resource="container pod", docs="https://docs.lium.io/pod-users/api-keys",
        docs_checked="docs (search 2026-10)", credential=BYO, settings=[],
        notes="Bittensor-subnet marketplace; pods are rented via lium.io/api with an API key (CLI/SDK). Individual "
              "hosts, no SLA. OpenGrid reads public executors only; no adapter."),
    "salad": dict(
        api=3, resource="container group (not a VM)", docs="https://docs.salad.com/reference/",
        docs_checked="recalled", credential=OPENGRID_MANAGED, settings=["salad_api_key", "salad_org"],
        notes="SaladCloud's API deploys container groups on consumer GPUs, always interruptible. Excluded from "
              "on-demand routing by market eligibility; no adapter."),
}


def _configured(entry: dict) -> bool:
    return bool(entry["settings"]) and all(getattr(settings, s, None) for s in entry["settings"])


def capability(provider: str) -> dict:
    """One provider's row. Unknown providers are level 0 with nothing established."""
    e = _REG.get(provider)
    impl = adapters.level(provider)
    cls = adapters.get(provider)
    if e is None:
        return {"provider": provider, "level_supported_by_provider_api": None, "level_implemented": impl,
                "level_implemented_label": LEVELS[impl], "verified_live": False,
                "availability_check_verified_live": False, "via": None,
                "resource": None, "credential_requirement": None, "credential_settings": [],
                "credentials_configured": False, "supports_stop": bool(cls and cls.SUPPORTS_STOP),
                "docs_url": None, "docs_checked": None, "notes": "not in the capability registry"}
    return {
        "provider": provider,
        "level_supported_by_provider_api": e["api"],
        "level_supported_label": None if e["api"] is None else LEVELS[e["api"]],
        "level_implemented": impl,
        "level_implemented_label": LEVELS[impl],
        "verified_live": False,
        "availability_check_verified_live": provider in AVAILABILITY_CHECK_VERIFIED_LIVE,
        "via": e.get("via"),
        "resource": e["resource"],
        "credential_requirement": e["credential"],
        "credential_settings": e["settings"],
        # Only whether OpenGrid-managed settings are present; BYO keys are per account.
        "credentials_configured": _configured(e),
        "supports_stop": bool(cls and cls.SUPPORTS_STOP),
        "docs_url": e["docs"],
        "docs_checked": e["docs_checked"],
        "notes": e["notes"],
    }


def all_capabilities() -> list[dict]:
    from providers import PROVIDERS

    names = sorted(set(PROVIDERS) | set(_REG))
    return [capability(p) for p in names]


def can_provision(provider: str) -> bool:
    return adapters.level(provider) >= 2
