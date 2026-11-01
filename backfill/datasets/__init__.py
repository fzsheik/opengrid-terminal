"""Open third-party datasets of past GPU prices, ingested as archived points (data kind 'archived').

    names      dataset provider / GPU labels -> OpenGrid provider ids and canonical GPU names (explicit tables)
    dedupe     never double count: a dataset point that repeats a primary source's point for the same provider,
               listing and date becomes corroboration on that row
    fsdl       Full Stack Deep Learning's cloud-gpus.csv, every revision in its git history (MIT, from 2022-08)
    huang      Huang (2026) GPU Rental Value Curves on Zenodo: Vast monthly median rental rates (CC BY 4.0, 2022-10)
    skypilot   SkyPilot's catalogue git history: DISABLED until SkyPilot confirm a licence (settings flag)

Every point records its licence and attribution (archived_prices.licence / attribution) and the exact revision it
came from (source_url). Downloads are cached on disk under CACHE and made one at a time.
"""
from __future__ import annotations

from backfill.wayback import DEFAULT_CACHE

CACHE = DEFAULT_CACHE.parent / "datasets"
