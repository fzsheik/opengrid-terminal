"""Lium's market-data feeds beyond the price poll (methodology/lium-data.md; tables in store/lium.py).

providers/lium.py reads Lium's prices and fleet (listings, capacity) and stores the public stats feed raw. This
package reads everything else Lium publishes about its market, raw first and change-only, every row INTERNAL by
default (provider_meta.INTERNAL_SOURCES; redistribution is not granted by Lium's terms):

    feeds.py     the registry: every feed, its path, whether it needs the data-partner key, its data kind
    raw.py       fetch one feed, classify the answer (ok / empty / forbidden / not_found ...), store it change-only
    public.py    the keyless feeds every 15 min (job `lium_public`): launch success and time to SSH, open renter
                 requests (404 while Lium has the feature off), rented/total per GPU type, spot idle pay, idle-pay
                 capacity buckets, reference prices; plus Lium's published daily GMV / GPU-hours / rentals and
                 30-day median paid price per model, parsed from the stats snapshots providers/lium.py stores
    partner.py   the data-partner feeds (job `lium_partner`, only with LIUM_PARTNER_API_KEY): completed rentals
                 (trades.csv, kind transaction), hourly GPU usage, daily VWAP; the nightly dumps for the backfill
    reads.py     what the admin endpoints serve (api/lium.py): status, trade statistics, current public values

CLI: python -m lium_data status | poll-public | poll-partner | backfill --since YYYY-MM-DD | reparse-stats
"""
