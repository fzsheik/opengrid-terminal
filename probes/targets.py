"""What OpenGrid probes. Every URL is public and unauthenticated, and was seen answering when it was added
(2026-10-23). A probe sends one GET and stops at the response headers (some of these pages are hundreds of KB).

kind
    api      a real API route that answers without a key (200) or refuses without one (401/403): either proves the
             service is up. `accept` lists exactly the statuses that count as up.
    host     the API host answers, but no route we know of does anything useful unauthenticated (proves DNS, TCP,
             TLS and the web tier only)
    website  the provider's public site. Weakest signal: a marketing site can be up while the API is down.

`region` is the PROVIDER region the endpoint serves (AWS us-east-1, OVH ca), not where the probe runs; where it runs
is the probe's `location`. A provider with several regional targets lets the page say which provider region failed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    id: str
    provider: str
    name: str
    url: str
    kind: str = "api"
    accept: tuple[int, ...] = (200,)
    region: str | None = None
    interval_s: int = 300
    note: str = ""

    def public(self) -> dict:
        return {"id": self.id, "provider": self.provider, "name": self.name, "url": self.url, "kind": self.kind,
                "accept": list(self.accept), "region": self.region, "interval_s": self.interval_s, "note": self.note}


def _aws(region: str) -> Target:
    # EC2 answers an unsigned DescribeRegions with 400 (MissingAuthenticationToken) when the regional endpoint is up
    return Target(f"aws:ec2-{region}", "aws", f"EC2 API {region}",
                  f"https://ec2.{region}.amazonaws.com/?Action=DescribeRegions&Version=2016-11-15", accept=(400,), region=region)


TARGETS: tuple[Target, ...] = (
    _aws("us-east-1"), _aws("us-west-2"), _aws("eu-west-1"), _aws("ap-southeast-1"),
    Target("azure:arm-metadata", "azure", "Resource Manager metadata", "https://management.azure.com/metadata/endpoints?api-version=2020-01-01"),
    Target("gcp:discovery", "gcp", "Google APIs discovery", "https://www.googleapis.com/discovery/v1/apis?name=compute"),
    Target("oracle:iaas-us-ashburn-1", "oracle", "OCI Core API us-ashburn-1", "https://iaas.us-ashburn-1.oraclecloud.com/20160918/shapes", accept=(401,), region="us-ashburn-1"),
    Target("oracle:iaas-eu-frankfurt-1", "oracle", "OCI Core API eu-frankfurt-1", "https://iaas.eu-frankfurt-1.oraclecloud.com/20160918/shapes", accept=(401,), region="eu-frankfurt-1"),
    Target("ibm:iam", "ibm", "IBM Cloud IAM", "https://iam.cloud.ibm.com/identity/.well-known/openid-configuration"),
    Target("ibm:catalog", "ibm", "Global Catalog", "https://globalcatalog.cloud.ibm.com/api/v1?_limit=1"),
    Target("lambda:api", "lambda", "Cloud API", "https://cloud.lambdalabs.com/api/v1/instance-types", accept=(401,)),
    Target("runpod:graphql", "runpod", "GraphQL API", "https://api.runpod.io/graphql", accept=(400,)),
    Target("runpod:rest", "runpod", "REST API", "https://rest.runpod.io/v1/pods", accept=(401,)),
    Target("vast:console-api", "vast", "Console API", "https://console.vast.ai/api/v0/users/current/", accept=(401, 403)),
    Target("salad:api-host", "salad", "API host", "https://api.salad.com/", kind="host", accept=(404,), note="the host's root has no route; 404 proves the web tier answers"),
    Target("hyperstack:api", "hyperstack", "InfraHub API", "https://infrahub-api.nexgencloud.com/v1/pricebook", accept=(401,)),
    Target("hyperbolic:api", "hyperbolic", "API", "https://api.hyperbolic.ai/v1/models", accept=(401,)),
    Target("nebius:website", "nebius", "Website", "https://nebius.com/", kind="website"),
    Target("digitalocean:api", "digitalocean", "API", "https://api.digitalocean.com/v2/sizes", accept=(401,)),
    Target("lium:website", "lium", "Website", "https://lium.io/", kind="website"),
    Target("massedcompute:website", "massedcompute", "Website", "https://massedcompute.com/", kind="website"),
    Target("verda:api", "verda", "API", "https://api.verda.com/v1/instance-types"),
    Target("voltagepark:api", "voltagepark", "Cloud API", "https://cloud-api.voltagepark.com/api/v1/locations/"),
    Target("crusoe:api", "crusoe", "Cloud API", "https://api.crusoecloud.com/v1alpha5/locations", accept=(401,)),
    Target("latitude:api", "latitude", "API", "https://api.latitude.sh/plans", accept=(401,)),
    Target("denvr:website", "denvr", "Website", "https://www.denvr.com/", kind="website"),
    Target("boostrun:website", "boostrun", "Website", "https://boostrun.com/", kind="website"),
    Target("horizon:website", "horizon", "Website", "https://horizoncompute.com/", kind="website"),
    Target("phyntec:website", "phyntec", "Website", "https://www.phyntec.com/", kind="website"),
    Target("scaleway:api-fr-par-1", "scaleway", "Instance API fr-par-1", "https://api.scaleway.com/instance/v1/zones/fr-par-1/products/servers", region="fr-par-1"),
    Target("coreweave:website", "coreweave", "Website", "https://www.coreweave.com/", kind="website"),
    Target("together:api", "together", "API", "https://api.together.xyz/v1/models", accept=(401,)),
    Target("jarvislabs:website", "jarvislabs", "Website", "https://jarvislabs.ai/", kind="website"),
    Target("hotaisle:website", "hotaisle", "Website", "https://hotaisle.xyz/", kind="website"),
    Target("vultr:api", "vultr", "API", "https://api.vultr.com/v2/plans"),
    Target("linode:api", "linode", "API", "https://api.linode.com/v4/regions"),
    Target("ovhcloud:api-ca", "ovhcloud", "API (CA)", "https://ca.api.ovh.com/1.0/auth/time", region="ca"),
    Target("ovhcloud:api-us", "ovhcloud", "API (US)", "https://api.us.ovhcloud.com/1.0/auth/time", region="us"),
    Target("koyeb:api", "koyeb", "API", "https://app.koyeb.com/v1/catalog/instances"),
)

BY_ID: dict[str, Target] = {t.id: t for t in TARGETS}
assert len(BY_ID) == len(TARGETS), "duplicate target id"


def for_provider(provider: str) -> list[Target]:
    return [t for t in TARGETS if t.provider == provider]
