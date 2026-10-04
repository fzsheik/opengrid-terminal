"""The provider interface.

A provider owns both halves of its own integration in one file:

    fetch()      hits its endpoints and returns the responses untouched
    normalize()  turns stored raw snapshots into ComputeListing rows

normalize() is a staticmethod and never touches the network, so the normalizer
can be re-run over stored history without credentials.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from models import ComputeListing, RawResponse

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from tables import RawSnapshot


@dataclass(frozen=True)
class PollingPolicy:
    """How often, and how patiently, one provider gets polled.

    Declared per provider because the APIs are not alike: a Salad poll costs 44
    requests against a 240/min key limit, Hyperstack 5 against 500/min per IP,
    and Lambda 3 against a limit it does not publish.
    """

    interval_seconds: float
    timeout_seconds: float = 30.0


class Provider(ABC):
    """One provider integration: how to fetch it, how to read it, how often."""

    name: str
    polling: PollingPolicy

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    @abstractmethod
    async def fetch(self) -> list[RawResponse]:
        """Pull every endpoint we care about, in whatever order suits the API."""

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, **kw) -> RawResponse:
        """One HTTP call, recorded whether or not it worked."""
        started_wall = datetime.now(timezone.utc)
        started = time.perf_counter()
        request_body = kw.get("json")
        try:
            resp = await self._client.request(method, path, **kw)
        except Exception as exc:
            return RawResponse(
                provider=self.name,
                endpoint=path,
                method=method,
                fetched_at=started_wall,
                duration_ms=int((time.perf_counter() - started) * 1000),
                ok=False,
                request=request_body,
                error=f"{type(exc).__name__}: {exc}",
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        return RawResponse(
            provider=self.name,
            endpoint=path,
            method=method,
            fetched_at=started_wall,
            status_code=resp.status_code,
            duration_ms=elapsed,
            ok=resp.is_success,
            error=None if resp.is_success else resp.text[:500],
            request=request_body,
            payload=body,
        )

    async def get(self, path: str, **kw) -> RawResponse:
        return await self.call("GET", path, **kw)

    async def post(self, path: str, **kw) -> RawResponse:
        return await self.call("POST", path, **kw)


async def gather_limited(factories, limit: int = 4) -> list:
    """Run calls with a concurrency cap, so we stay under rate limits.

    Takes zero-arg callables rather than coroutines: a cancelled batch would
    otherwise leave the not-yet-started coroutines never awaited.
    """
    sem = asyncio.Semaphore(limit)

    async def run(make):
        async with sem:
            return await make()

    return await asyncio.gather(*(run(f) for f in factories))


    @staticmethod
    @abstractmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """Turn this provider's stored raw snapshots into listings.

        `by_endpoint` maps endpoint path -> the snapshots from the newest fetch
        round, so an endpoint called many times per fetch (Salad's per-class
        availability) arrives as a list.
        """


def reshaped(raw: RawResponse, payload, request=None) -> RawResponse:
    """The same response with its payload replaced by the fields we read.

    For providers whose bodies are huge or noisy (live GPU utilization, a whole
    HTML page), storing the body untouched would bloat the table and make every
    poll look different. The listing fields are kept verbatim; the rest is not.
    """
    update = {"payload": payload}
    if request is not None:
        update["request"] = request
    return raw.model_copy(update=update)


async def get_page(provider: "Provider", path: str, extract) -> RawResponse:
    """GET an HTML page and store what `extract(html)` pulls out of it.

    A page that changed shape raises inside `extract`; that is recorded as a
    failed response, never as a healthy provider with no prices.
    """
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        resp = await provider._client.get(path)
        resp.raise_for_status()
        payload = extract(resp.text)
    except Exception as exc:
        return RawResponse(
            provider=provider.name,
            endpoint=path,
            fetched_at=started_wall,
            duration_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    return RawResponse(
        provider=provider.name,
        endpoint=path,
        fetched_at=started_wall,
        status_code=resp.status_code,
        duration_ms=int((time.perf_counter() - started) * 1000),
        ok=True,
        payload=payload,
    )


def to_decimal(value) -> Decimal | None:
    """Parse a price without letting a bad value take down a whole poll."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        log.warning("could not read %r as a price", value)
        return None


def find_endpoint(
    by_endpoint: "dict[str, list[RawSnapshot]]", fragment: str
) -> "list[RawSnapshot]":
    """Snapshots whose endpoint contains `fragment`."""
    return [r for ep, rows in by_endpoint.items() if fragment in ep for r in rows]
