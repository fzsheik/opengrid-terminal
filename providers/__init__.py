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
from providers.arkane import ArkaneProvider
from providers.atlanticnet import AtlanticNetProvider
from providers.aws import AwsProvider
from providers.azure import AzureProvider
from providers.baseten import BasetenProvider
from providers.beam import BeamProvider
from providers.civo import CivoProvider
from providers.clore import CloreProvider
from providers.coreweave import CoreWeaveProvider
from providers.crusoe import CrusoeProvider
from providers.deepinfra import DeepInfraProvider
from providers.digitalocean import DigitalOceanProvider
from providers.e2e import E2EProvider
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
from providers.mithril import MithrilProvider
from providers.modal import ModalProvider
from providers.nebius import NebiusProvider
from providers.nosana import NosanaProvider
from providers.oracle import OracleProvider
from providers.ovhcloud import OvhCloudProvider
from providers.replicate import ReplicateProvider
from providers.runpod import RunPodProvider
from providers.salad import SaladProvider
from providers.simplepod import SimplePodProvider
from providers.shadeform import (
    BoostRunProvider,
    DenvrProvider,
    HorizonProvider,
    LatitudeProvider,
    PhyntecProvider,
    ScalewayProvider,
)
from providers.thundercompute import ThunderComputeProvider
from providers.together import TogetherProvider
from providers.vast import VastProvider
from providers.verda import VerdaProvider
from providers.vessl import VesslProvider
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
        ThunderComputeProvider,
        ArkaneProvider,
        VesslProvider,
        MithrilProvider,
        NosanaProvider,
        CivoProvider,
        AtlanticNetProvider,
        DeepInfraProvider,
        E2EProvider,
        SimplePodProvider,
        ModalProvider,
        ReplicateProvider,
        BasetenProvider,
        BeamProvider,
        CloreProvider,
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
    "ThunderComputeProvider",
    "ArkaneProvider",
    "VesslProvider",
    "MithrilProvider",
    "NosanaProvider",
    "CivoProvider",
    "AtlanticNetProvider",
    "DeepInfraProvider",
    "E2EProvider",
    "SimplePodProvider",
    "ModalProvider",
    "ReplicateProvider",
    "BasetenProvider",
    "BeamProvider",
    "CloreProvider",
    "find_endpoint",
    "gather_limited",
    "to_decimal",
]
