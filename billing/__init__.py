"""Billing MODELS: usage records, configurable fee policies, charges, credits, draft invoices.

No payment processing exists or is simulated. See methodology/billing.md.

    policy     fee policies (versioned, composable components, per-account overrides)
    usage      record_usage(): the contract routing calls per deployment period
    invoices   credits and draft invoice generation
"""
