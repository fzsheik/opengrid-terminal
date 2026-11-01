"""Live market feeds: push sources ingested as they change (methodology/live-feeds.md).

    live.lium_stream   Lium's public executor WebSocket: book, change-only events, compacted snapshots
    live.locks         one normalize at a time per provider (the poller and a stream share it)

A stream never replaces its provider's poller: the poller keeps running as the fallback and reconcile.
"""
