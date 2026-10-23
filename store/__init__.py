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
