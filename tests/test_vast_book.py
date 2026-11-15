"""Vast.ai: the public search stops at 64 offers per query, so a longer book is read in pages (providers/vast.py).

A mock search behaves like Vast's: cheapest first, at most 64 offers, no offset, and a `dph_total` filter
(`gte` / `gt`). Covers: a book read to its end across pages; the median and capacity over the whole book; a
snapshot stored before paging (64 offers, no mark) still read as cut off; a later page failing; more than a page
of offers at one price; the page limit; two rounds inside the read window. No network.

Run:  .venv/bin/python tests/test_vast_book.py
"""

import asyncio
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from providers import vast  # noqa: E402
from providers.vast import ENDPOINT, VastProvider  # noqa: E402

NOW = datetime(2026, 11, 15, 8, tzinfo=timezone.utc)
MODEL = "RTX 4090"


def offer(i, price, gpus=1, model=MODEL):
    return {"id": 1000 + i, "machine_id": 5000 + i, "host_id": 70 + i % 9, "gpu_name": model, "num_gpus": gpus,
            "dph_total": price, "gpu_frac": 1.0, "cpu_name": "AMD EPYC 7443P 24-Core Processor", "cpu_cores": 48,
            "geolocation": "Texas, US", "public_ipaddr": "203.0.113.7", "hostname": "h"}


def server(books, fail_after=None):
    """books: gpu_name -> offers. Returns (handler, calls); calls records each query's (gpu_name, price filter)."""
    calls = []

    def handler(request):
        q = json.loads(request.url.params["q"])
        name, flt = q["gpu_name"]["eq"], q.get("dph_total")
        calls.append((name, flt))
        if fail_after is not None and len([c for c in calls if c[0] == name]) > fail_after:
            return httpx.Response(500, json={"error": "boom"})
        rows = sorted(books.get(name, []), key=lambda o: (o["dph_total"], o["id"]))
        if flt:
            (op, v), = flt.items()
            rows = [o for o in rows if (o["dph_total"] >= v if op == "gte" else o["dph_total"] > v)]
        return httpx.Response(200, json={"offers": rows[:vast.API_CAP], "truncated": False})
    return handler, calls


def read(books, model=MODEL, **kw):
    handler, calls = server(books, **kw)
    pause = vast.RATE_PAUSE_SECONDS
    vast.RATE_PAUSE_SECONDS = 0
    try:
        prov = VastProvider(httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
        raw = asyncio.run(prov._one(model))
    finally:
        vast.RATE_PAUSE_SECONDS = pause
    return raw, [c for c in calls if c[0] == model]


def listings(raw, at=NOW):
    snap = SimpleNamespace(endpoint=ENDPOINT, payload=raw.payload, request=raw.request, fetched_at=at)
    return {l.listing_id: l for l in VastProvider.normalize({ENDPOINT: [snap]})}


def test_a_long_book_is_read_to_its_end():
    book = [offer(i, round(0.30 + i * 0.01, 2), gpus=1 + i % 2) for i in range(150)]
    raw, calls = read({MODEL: book})
    assert raw.ok and raw.request == {"gpu_name": MODEL, "book": vast.FULL_BOOK}
    assert len(raw.payload) == 150 and {o["id"] for o in raw.payload} == {o["id"] for o in book}, "no offer lost or doubled"
    # Page 1 has no price filter; each next page starts at the last price seen (inclusive: ties are not skipped).
    assert [c[1] for c in calls] == [None, {"gte": 0.93}, {"gte": 1.56}], calls
    assert "public_ipaddr" not in raw.payload[0] and "hostname" not in raw.payload[0], "host IP and name stay out"
    out = listings(raw)
    asks = sorted(D(str(o["dph_total"])) / o["num_gpus"] for o in book)
    assert out[f"{MODEL}:median"].price_per_gpu_hour == D(str(statistics.median(asks))), "the median of the whole book"
    assert out[f"{MODEL}:cheapest"].price_per_gpu_hour == asks[0]
    total = sum(o["num_gpus"] for o in book)
    assert (out[f"{MODEL}:median"].capacity, out[f"{MODEL}:median"].capacity_unit) == (total, "gpu"), \
        "a book read to its end is whole: its GPU count is stated"
    # Over the 64 cheapest only, the median would have been lower: the bias paging removes.
    low = statistics.median(sorted(asks)[:64])
    assert low < out[f"{MODEL}:median"].price_per_gpu_hour


def test_short_and_empty_books_take_one_request():
    raw, calls = read({MODEL: [offer(i, 0.5 + i * 0.01) for i in range(12)]})
    assert len(calls) == 1 and len(raw.payload) == 12 and raw.request["book"] == vast.FULL_BOOK
    assert listings(raw)[f"{MODEL}:median"].capacity == 12
    raw, calls = read({MODEL: []})
    assert len(calls) == 1 and raw.payload == [] and raw.request == {"gpu_name": MODEL, "book": vast.FULL_BOOK}
    assert listings(raw) == {}
    # Exactly one full page: a second request finds nothing new and ends the book.
    raw, calls = read({MODEL: [offer(i, 0.5 + i * 0.01) for i in range(64)]})
    assert len(calls) == 2 and len(raw.payload) == 64 and raw.request["book"] == vast.FULL_BOOK
    assert listings(raw)[f"{MODEL}:median"].capacity == 64


def test_a_snapshot_from_before_paging_is_still_cut_off():
    old = SimpleNamespace(endpoint=ENDPOINT, payload=[vast._keep(offer(i, 0.3 + i * 0.01)) for i in range(64)],
                          request={"gpu_name": MODEL}, fetched_at=NOW)
    out = {l.listing_id: l for l in VastProvider.normalize({ENDPOINT: [old]})}
    assert out[f"{MODEL}:median"].capacity is None and out[f"{MODEL}:median"].capacity_unit is None, \
        "64 offers without the full-book mark: a cut-off book, capacity withheld (history re-reads the same)"


def test_a_failed_later_page_keeps_what_was_read_as_cut_off():
    book = [offer(i, round(0.30 + i * 0.01, 2)) for i in range(150)]
    raw, calls = read({MODEL: book}, fail_after=1)
    assert raw.ok and len(raw.payload) == 64 and raw.request == {"gpu_name": MODEL}, "no full-book mark"
    assert len(calls) == 2
    assert listings(raw)[f"{MODEL}:median"].capacity is None
    raw, _ = read({MODEL: book}, fail_after=0)
    assert not raw.ok and raw.payload is None and raw.request == {"gpu_name": MODEL}, "page 1 failing fails the model"


def test_more_than_a_page_at_one_price_is_cut_off_not_looped():
    book = [offer(i, 0.50) for i in range(70)] + [offer(100 + i, 0.90) for i in range(5)]
    raw, calls = read({MODEL: book})
    assert len(calls) == 2, "the second page repeats the first: stop instead of asking forever"
    assert len(raw.payload) == 64 and "book" not in raw.request
    assert listings(raw)[f"{MODEL}:median"].capacity is None


def test_page_limit():
    book = [offer(i, round(0.10 + i * 0.001, 3)) for i in range(64 * (vast.MAX_PAGES + 2))]
    raw, calls = read({MODEL: book})
    assert len(calls) == vast.MAX_PAGES and "book" not in raw.request, "stored as far as read, treated as cut off"
    assert len(raw.payload) > 64 * (vast.MAX_PAGES - 1)


def test_fetch_reads_every_model_and_two_rounds_do_not_double():
    books = {n: [offer(i, 1.0 + i * 0.01, model=n) for i in range(3)] for n in vast.GPU_NAMES}
    books["RTX 5090"] = [offer(i, round(0.4 + i * 0.01, 2), model="RTX 5090") for i in range(130)]
    handler, calls = server(books)
    pause = vast.RATE_PAUSE_SECONDS
    vast.RATE_PAUSE_SECONDS = 0
    try:
        prov = VastProvider(httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
        out = asyncio.run(prov.fetch())
    finally:
        vast.RATE_PAUSE_SECONDS = pause
    assert [r.request["gpu_name"] for r in out] == vast.GPU_NAMES, "one stored response per model, in order"
    assert len(calls) == len(vast.GPU_NAMES) + 2, "only the long book costs extra requests"
    assert all(r.request.get("book") == vast.FULL_BOOK for r in out)
    assert len(next(r for r in out if r.request["gpu_name"] == "RTX 5090").payload) == 130

    # A deploy can leave two rounds inside normalize's read window: the newest book of a model is the only one read.
    newer = SimpleNamespace(endpoint=ENDPOINT, payload=[vast._keep(offer(1, 2.0))],
                            request={"gpu_name": MODEL, "book": vast.FULL_BOOK}, fetched_at=NOW)
    older = SimpleNamespace(endpoint=ENDPOINT, payload=[vast._keep(offer(2, 9.0))], request={"gpu_name": MODEL},
                            fetched_at=NOW - timedelta(seconds=60))
    ls = VastProvider.normalize({ENDPOINT: [newer, older]})
    assert sorted(l.listing_id for l in ls) == [f"{MODEL}:cheapest", f"{MODEL}:median"]
    assert all(l.price_per_gpu_hour == D("2.0") for l in ls)


if __name__ == "__main__":
    for t in (test_a_long_book_is_read_to_its_end, test_short_and_empty_books_take_one_request,
              test_a_snapshot_from_before_paging_is_still_cut_off, test_a_failed_later_page_keeps_what_was_read_as_cut_off,
              test_more_than_a_page_at_one_price_is_cut_off_not_looped, test_page_limit,
              test_fetch_reads_every_model_and_two_rounds_do_not_double):
        t()
        print(t.__name__, "ok")
    print("all passed")
