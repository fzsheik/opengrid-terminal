"""Adapter registry: provider name -> the adapter class that executes there.

`level(provider)` is the ONLY source of "what OpenGrid implements" (capabilities.py
reads it), so a provider without an adapter here is level 0 however capable its API is.
Crusoe, Denvr and Latitude all map to the one Shadeform adapter (an aggregator route).
"""

from routing.adapters.base import Adapter
from routing.adapters.digitalocean import DigitalOceanAdapter
from routing.adapters.hyperstack import HyperstackAdapter
from routing.adapters.lambda_labs import LambdaAdapter
from routing.adapters.runpod import RunPodAdapter
from routing.adapters.shadeform import ShadeformAdapter
from routing.adapters.vast import VastAdapter
from routing.adapters.verda import VerdaAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "lambda": LambdaAdapter,
    "runpod": RunPodAdapter,
    "hyperstack": HyperstackAdapter,
    "digitalocean": DigitalOceanAdapter,
    "crusoe": ShadeformAdapter,
    "denvr": ShadeformAdapter,
    "latitude": ShadeformAdapter,
    "vast": VastAdapter,
    "verda": VerdaAdapter,
}

# Tests swap in a transport (httpx.MockTransport) per provider here.
TRANSPORTS: dict = {}


def get(provider: str) -> type[Adapter] | None:
    return ADAPTERS.get(provider)


def level(provider: str) -> int:
    cls = ADAPTERS.get(provider)
    return cls.LEVEL if cls else 0


def build(provider: str, credentials: dict | None) -> Adapter | None:
    cls = ADAPTERS.get(provider)
    return None if cls is None else cls(credentials, transport=TRANSPORTS.get(provider), provider=provider)


def register(provider: str, cls: type[Adapter]) -> None:
    """For tests (synthetic providers). Production adapters are listed above."""
    ADAPTERS[provider] = cls


def unregister(provider: str) -> None:
    ADAPTERS.pop(provider, None)
