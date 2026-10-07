"""Watchlists and alerts. See methodology/alerts.md.

    watchlists  named sets of GPUs / providers / regions / indices an account follows
    rules       alert rule storage + validation, firings feed
    metrics     metric-resolver registry (unknown never fires)
    evaluator   edge-triggered evaluation with cooldown; job `alerts` every 3 minutes
    notifier    channels: in-app feed, signed webhooks; email is an interface only
"""
