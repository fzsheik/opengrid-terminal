"""OpenGrid's own provider probes: raw results first, then rollups that distinguish regional from global.

    probes.targets    what is probed per provider (public, unauthenticated URLs) and which statuses count as up
    probes.measure    one probe: DNS / TCP / TLS / time-to-first-byte timings and a classified failure reason
    probes.consensus  pure rollup rules: results from several vantage points -> ok / regional / global
    probes.store      raw writes (probe_results), rollups, retention, and the reads behind /v1/probes
    probes.run        the in-process agent (`probe_local` job) and the `probe_rollup` job
    probes.agent      the standalone agent: run it in other regions, it posts to /v1/probes/ingest

Separate from the UptimeProject data (uptime/) on purpose; nothing here feeds routing or any score.
Methodology: methodology/probes.md.
"""
