"""Data quality: bad-data screening and quarantine, source schema watching, trust labels.

    quality.screen(listings, only)   called by normalize.refresh before anything is saved
    quality.normalizer_failed(p, e)  called by normalize.normalize_all when a normalizer raises

Both FAIL OPEN: if the quality layer itself breaks, ingestion carries on exactly as it
did before this package existed, and the failure is logged (and recorded if possible).
Submodules are imported lazily so normalize.py can import this package without a cycle.
"""

import logging

log = logging.getLogger(__name__)


def screen(listings, only=None):
    try:
        from quality import quarantine

        return quarantine.screen(listings, only)
    except Exception as exc:
        log.exception("quality layer failed; listings pass through unscreened")
        try:
            from quality import incidents

            incidents.record_now("quality_layer_error", severity="major",
                                 detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
        except Exception:
            pass
        return listings


def normalizer_failed(provider, exc) -> None:
    try:
        from quality import quarantine

        quarantine.normalizer_failed(provider, exc)
    except Exception:
        log.exception("could not record normalizer failure for %s", provider)
