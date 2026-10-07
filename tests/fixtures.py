"""Synthetic market history for TESTS ONLY, written into a scratch database.

Never point this at a real database: every number here is invented. Rows are
tagged with providers named `syn_*` so they cannot be mistaken for real ones.

    import scratchdb, fixtures
    url = scratchdb.create("og_test_myarea")
    Session = fixtures.session(url)          # schema via create_all
    meta = fixtures.seed(Session, days=120)  # returns what it generated
"""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tables import Base, ComputeListingRow, ListingObservation, RawSnapshot

GPUS = {  # canonical name -> base price per GPU-hour
    "NVIDIA H100 80GB SXM5": 2.60,
    "NVIDIA H200 141GB SXM5": 3.40,
    "NVIDIA A100 80GB SXM4": 1.40,
    "NVIDIA L40S 48GB": 0.95,
    "NVIDIA B200 180GB SXM": 5.20,
}
PROVIDERS = ["syn_alpha", "syn_beta", "syn_gamma", "syn_delta", "syn_eps"]
REGIONS = ["us-east", "us-west", "eu-west", None]


def session(url: str):
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed(Session, days: int = 120, step_hours: float = 1.0, seed_value: int = 7,
         now: datetime | None = None, providers=PROVIDERS, gpus=GPUS) -> dict:
    """Random-walk prices and occasional sell-outs, change-only observations like the real pipeline."""
    rng = random.Random(seed_value)
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=days)
    listings, observations, snaps = [], [], []
    for p_i, prov in enumerate(providers):
        # Providers join at different times; the last one joins late on purpose.
        joined = start + timedelta(days=(days * 0.6 if p_i == len(providers) - 1 else p_i * 2))
        snaps.append(RawSnapshot(provider=prov, endpoint="/synthetic", fetched_at=joined, ok=True, status_code=200, payload={}))
        snaps.append(RawSnapshot(provider=prov, endpoint="/synthetic", fetched_at=now, ok=True, status_code=200, payload={}))
        for g_i, (gpu, base) in enumerate(gpus.items()):
            if rng.random() < 0.15:
                continue  # not every provider sells every GPU
            region = REGIONS[(p_i + g_i) % len(REGIONS)]
            lid = f"{prov}:{gpu}"
            price = base * (0.75 + 0.5 * rng.random())
            avail, last = True, None
            t = joined
            while t <= now:
                if rng.random() < 0.08:
                    price *= 1 + rng.gauss(0, 0.04)
                if rng.random() < 0.03:
                    avail = not avail
                cur = (round(price, 4), avail)
                if cur != last:
                    observations.append(ListingObservation(
                        provider=prov, listing_id=lid, observed_at=t,
                        price_per_gpu_hour=Decimal(str(cur[0])), price_per_instance_hour=Decimal(str(cur[0])),
                        available=avail, capacity=None, capacity_unit=None))
                    last = cur
                t += timedelta(hours=step_hours)
            listings.append(ComputeListingRow(
                provider=prov, listing_id=lid, sku=lid, raw_gpu_name=gpu, canonical_gpu_name=gpu, gpu_count=1,
                region=region, country="US" if region and region.startswith("us") else None,
                price_per_gpu_hour=Decimal(str(last[0])), price_per_instance_hour=Decimal(str(last[0])),
                currency="USD", market_type="on_demand", provider_tier=None, interruptible=False,
                available=last[1], capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None,
                observed_at=now, first_seen_at=joined))
    with Session.begin() as s:
        s.add_all(snaps)
        s.add_all(listings)
        s.flush()
        s.add_all(observations)
    return {"now": now, "start": start, "listings": len(listings), "observations": len(observations)}
