"""Tables beyond the original four, one module per domain.

Each module does `from tables import Base` and declares its tables; tables.py
imports this package at the bottom so Alembic and create_all see everything.
"""

from store import analytics, structure, events, quality, news, accounts, routing, frontend  # noqa: F401
from store import reconcile, metrics, security  # noqa: F401
from store import jobs  # noqa: F401  (0017_jobs: OpenGrid Jobs)
from store import settlement  # noqa: F401  (0018_settlement: official index fixings ledger)
from store import reservations  # noqa: F401  (0019_reservations: reserved-compute price-lock requests)
from store import intelligence  # noqa: F401  (0020_intelligence: performance observations for future learning)
from store import uptime  # noqa: F401  (0022_uptime: external UptimeProject control-plane probe data)
from store import capacity  # noqa: F401  (0025_capacity: provider-reported capacity)
from store import archive  # noqa: F401  (0025_archived_prices: archived price history, kind "archived")
from store import fx  # noqa: F401  (0026_fx: ECB euro reference rates + FX provenance on listings)
from store import freshness  # noqa: F401  (0028_changed_at: per-poll freshness log for /v1/freshness)
from store import cost  # noqa: F401  (0030_cost_components: raw snapshots of machine-readable cost sources)
from store import live  # noqa: F401  (0031_stream_events: change-only events from push feeds)
from store import context  # noqa: F401  (0032_context_series + 0033_equities: market context; internal equity bars)
from store import shifts  # noqa: F401  (0029_news_history: major shifts + their related items)
