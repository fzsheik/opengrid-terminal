"""One normalize per provider at a time.

The poller and a live stream can both re-normalize the same provider (Lium). normalize.refresh reads the last
observation and then writes the next one, so two concurrent runs could record the same change twice. Both callers
go through refresh_provider(), which serialises runs per provider inside this process (the app runs one worker).
"""

from __future__ import annotations

import threading
from collections import defaultdict

_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_GUARD = threading.Lock()


def provider_lock(provider: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS[provider]


def refresh_provider(provider: str) -> dict:
    """normalize.refresh([provider]) under the provider's lock. Blocking: call it from a worker thread."""
    import normalize

    with provider_lock(provider):
        return normalize.refresh([provider])
