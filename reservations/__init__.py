"""Reserved-compute price-lock requests: a request / offer workflow (RFQ), never a binding price.

    states.py     the status machine (pure)
    service.py    validation, the INDICATIVE reference, transitions, tenant isolation, expiry job

methodology/reservations.md. Tables: store/reservations.py (migration 0019_reservations). API: api/reservations.py.
"""
