"""Tables beyond the original four, one module per domain.

Each module does `from tables import Base` and declares its tables; tables.py
imports this package at the bottom so Alembic and create_all see everything.
"""

from store import analytics, structure, events, quality, news, accounts, routing, frontend  # noqa: F401
