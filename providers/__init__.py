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
from providers.azure import AzureProvider
from providers.coreweave import CoreWeaveProvider
from providers.digitalocean import DigitalOceanProvider
from providers.gcp import GcpProvider
from providers.hotaisle import HotAisleProvider
from providers.hyperbolic import HyperbolicProvider
from providers.hyperstack import HyperstackProvider
from providers.ibm import IbmProvider
from providers.jarvislabs import JarvislabsProvider
from providers.koyeb import KoyebProvider
from providers.lambda_labs import LambdaProvider
from providers.linode import LinodeProvider
from providers.lium import LiumProvider
from providers.massedcompute import MassedComputeProvider
from providers.nebius import NebiusProvider
from providers.oracle import OracleProvider
from providers.ovhcloud import OvhCloudProvider
from providers.runpod import RunPodProvider
from providers.salad import SaladProvider
from providers.shadeform import (
    BoostRunProvider,
    CrusoeProvider,
    DenvrProvider,
    HorizonProvider,
    LatitudeProvider,
    PhyntecProvider,
    ScalewayProvider,
)
from providers.together import TogetherProvider
from providers.vast import VastProvider
from providers.verda import VerdaProvider
from providers.voltagepark import VoltageParkProvider
from providers.vultr import VultrProvider

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
        BoostRunProvider,
        CoreWeaveProvider,
        TogetherProvider,
        JarvislabsProvider,
        HotAisleProvider,
        VultrProvider,
        GcpProvider,
        AzureProvider,
        HorizonProvider,
        PhyntecProvider,
        LinodeProvider,
        OvhCloudProvider,
        ScalewayProvider,
        KoyebProvider,
        OracleProvider,
        IbmProvider,
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
    "BoostRunProvider",
    "CoreWeaveProvider",
    "TogetherProvider",
    "JarvislabsProvider",
    "HotAisleProvider",
    "VultrProvider",
    "GcpProvider",
    "AzureProvider",
    "HorizonProvider",
    "PhyntecProvider",
    "LinodeProvider",
    "OvhCloudProvider",
    "ScalewayProvider",
    "KoyebProvider",
    "OracleProvider",
    "IbmProvider",
    "find_endpoint",
    "gather_limited",
    "to_decimal",
]
