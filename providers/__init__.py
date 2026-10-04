"""Provider registry.

Adding a provider means writing one module with a Provider subclass, then
listing it here. Nothing else in the codebase needs to know its name.
"""

from providers.base import (
    PollingPolicy,
    Provider,
    find_endpoint,
    gather_limited,
    to_decimal,
)
from providers.aws import AwsProvider
from providers.digitalocean import DigitalOceanProvider
from providers.hyperbolic import HyperbolicProvider
from providers.hyperstack import HyperstackProvider
from providers.lambda_labs import LambdaProvider
from providers.lium import LiumProvider
from providers.massedcompute import MassedComputeProvider
from providers.nebius import NebiusProvider
from providers.runpod import RunPodProvider
from providers.salad import SaladProvider
from providers.shadeform import CrusoeProvider, DenvrProvider, LatitudeProvider
from providers.vast import VastProvider
from providers.verda import VerdaProvider
from providers.voltagepark import VoltageParkProvider

PROVIDERS: dict[str, type[Provider]] = {
    cls.name: cls
    for cls in (
        SaladProvider,
        HyperstackProvider,
        LambdaProvider,
        RunPodProvider,
        HyperbolicProvider,
        VoltageParkProvider,
        LiumProvider,
        VastProvider,
        NebiusProvider,
        MassedComputeProvider,
        DigitalOceanProvider,
        AwsProvider,
        VerdaProvider,
        CrusoeProvider,
        LatitudeProvider,
        DenvrProvider,
    )
}

__all__ = [
    "PROVIDERS",
    "PollingPolicy",
    "Provider",
    "SaladProvider",
    "HyperstackProvider",
    "LambdaProvider",
    "RunPodProvider",
    "HyperbolicProvider",
    "VoltageParkProvider",
    "LiumProvider",
    "VastProvider",
    "NebiusProvider",
    "MassedComputeProvider",
    "DigitalOceanProvider",
    "AwsProvider",
    "VerdaProvider",
    "CrusoeProvider",
    "LatitudeProvider",
    "DenvrProvider",
    "find_endpoint",
    "gather_limited",
    "to_decimal",
]
