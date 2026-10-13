"""The price-lock request status machine (methodology/reservations.md).

    requested -> under_review -> offered -> accepted -> fulfilled
                                         +-> declined | expired | withdrawn
                                accepted -> cancelled

    requested / under_review   -> declined (operator), withdrawn (customer), expired (start window passed, no offer)
    offered                    -> offered (operator re-offers: new terms, offer_seq + 1), accepted (customer, naming
                                  the offer_seq it accepts), declined (operator retracts), withdrawn (customer
                                  declines the offer), expired (offer validity ended)
    accepted                   -> fulfilled (operator: the compute was delivered under a signed order form),
                                  cancelled (operator: the order form was not signed / was cancelled)

An operator offer made straight from `requested` records `under_review` first, so every request's event history
passes through review. Every transition is a reservation_events row (actor, reason, evidence). Terminal states
never move again.
"""

from __future__ import annotations

STATES = ("requested", "under_review", "offered", "accepted", "declined", "expired", "withdrawn", "fulfilled",
          "cancelled")
OPEN = ("requested", "under_review", "offered")            # still waiting on someone
TERMINAL = ("declined", "expired", "withdrawn", "fulfilled", "cancelled")

ALLOWED = {
    "requested": {"under_review", "offered", "declined", "withdrawn", "expired"},
    "under_review": {"offered", "declined", "withdrawn", "expired"},
    "offered": {"offered", "accepted", "declined", "withdrawn", "expired"},
    "accepted": {"fulfilled", "cancelled"},
    **{s: set() for s in TERMINAL},
}

# Who may cause each transition: the requesting customer, or the operator (admin scope), or the system (expiry).
BY = {
    "under_review": "admin", "offered": "admin", "declined": "admin", "fulfilled": "admin", "cancelled": "admin",
    "accepted": "owner", "withdrawn": "owner", "expired": "system",
}


def can(src: str, dst: str) -> bool:
    return dst in ALLOWED.get(src, set())


def is_terminal(s: str) -> bool:
    return s in TERMINAL
