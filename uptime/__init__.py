"""External control-plane uptime and latency from UptimeProject (CC BY 4.0). Separate from OpenGrid's own
deployment evidence: nothing here feeds routing/reliability.py or any score. See methodology/uptime.md.

    uptime.sources   which UptimeProject services belong to which OpenGrid provider
    uptime.fetch     HTTP client + response parsing (never raises past its caller's try)
    uptime.store     writes, retention, and the read side behind /v1/uptime
    uptime.poll      the `uptime` background job
"""
