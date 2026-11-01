"""A small in-process TTL cache for expensive reads.

The app runs as exactly one process (see railway.toml), so an in-process cache
is coherent. Keys are the function's arguments; values expire after `ttl`.

    @ttl_cache(60)
    def overview(hours): ...

    overview.cache_clear()   # e.g. after a job writes new aggregates
    clear_all()              # every ttl_cache in the process (tests that change the database under the app)
"""

import functools
import threading
import time

_CLEARS: list = []


def clear_all() -> None:
    for clear in list(_CLEARS):
        clear()


def ttl_cache(ttl_seconds: float, maxsize: int = 256):
    def wrap(fn):
        store: dict = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def cached(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit and now - hit[0] < ttl_seconds:
                    return hit[1]
            value = fn(*args, **kwargs)
            with lock:
                if len(store) >= maxsize:
                    store.pop(min(store, key=lambda k: store[k][0]))
                store[key] = (now, value)
            return value

        cached.cache_clear = lambda: store.clear()
        _CLEARS.append(cached.cache_clear)
        return cached

    return wrap
