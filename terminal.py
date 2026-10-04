"""OpenGrid terminal: live GPU prices with a regex-registered command bar."""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Header, Input, Static

import mapping
import normalize
import raw_store
from db import init_db
from fetch import fetch_all, save
from poller import Ingest


@dataclass(frozen=True)
class Command:
    pattern: re.Pattern
    handler: Callable[["OpenGrid", re.Match], None]
    usage: str


COMMANDS: list[Command] = []


def command(pattern: str, usage: str):
    """Register a handler for input that fully matches `pattern` (case-insensitive).

    Commands are tried in registration order, first match wins.
    """

    def register(fn):
        COMMANDS.append(Command(re.compile(pattern, re.I), fn, usage))
        return fn

    return register


@command(r"help|\?", "help                this list")
def _help(app, m):
    app.notify("\n".join(c.usage for c in COMMANDS if c.usage), title="commands", timeout=25)


@command(r"(?:listings?|l)(?:\s+(?P<q>.+))?", "listings [filter]   normalized listings, cheapest first")
def _listings(app, m):
    app.show("listings", q=m["q"])


@command(r"(?:avail(?:able)?|a)(?:\s+(?P<q>.+))?", "avail [filter]      only listings with stock")
def _avail(app, m):
    app.show("listings", q=m["q"], only_available=True)


@command(r"cheapest(?:\s+(?P<n>\d+))?", "cheapest [n]        n cheapest in stock (default 10)")
def _cheapest(app, m):
    app.show("listings", only_available=True, limit=int(m["n"] or 10))


@command(r"(?:history|h)(?:\s+(?P<q>.+))?", "history [filter]    recorded changes, newest first")
def _history(app, m):
    app.show("history", q=m["q"])


@command(r"(?:refs?|reference)(?:\s+(?P<q>.+))?", "refs [filter]       priced entries that are not listings")
def _refs(app, m):
    app.show("refs", q=m["q"])


@command(r"(?:mapping|map)(?:\s+(?P<q>.+))?", "mapping [provider]  how each API fills ComputeListing")
def _mapping(app, m):
    app.show("mapping", q=m["q"])


@command(r"quirks?(?:\s+(?P<q>.+))?", "quirks [provider]   per-provider gotchas worth knowing")
def _quirks(app, m):
    app.show("quirks", q=m["q"])


@command(r"unmapped", "unmapped            raw GPU names with no canonical name")
def _unmapped(app, m):
    app.show("unmapped")


@command(r"(?:raw)(?:\s+(?P<q>.+))?", "raw [provider]      recent raw fetches")
def _raw(app, m):
    app.show("raw", q=m["q"])


@command(r"(?:summary|s)", "summary             per endpoint: fetches, distinct bodies, latency")
def _summary(app, m):
    app.show("summary")


@command(r"(?:fetch|f)", "fetch               pull every provider, then normalize")
def _fetch(app, m):
    app.run_fetch()


@command(r"(?:normalize|n)", "normalize           re-run the normalizer over stored raw")
def _normalize(app, m):
    app.run_normalize()


@command(r"(?:reset|clear)", "reset               clear filters")
def _reset(app, m):
    app.show("listings")


@command(r"(?:quit|exit|q)", "quit")
def _quit(app, m):
    app.exit()


@command(r"(?P<q>[\w .\-()+,/]+)", "")
def _filter(app, m):
    app.show("listings", q=m["q"])


def age(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def num(v, places: int = 0) -> str:
    return "-" if v is None else f"{v:.{places}f}"


def price_arrow(current, previous) -> Text:
    """Which way the price moved since the previous observation.

    Up is red because it is worse for a buyer. Blank means no recorded change
    yet, which is not the same as a flat price.
    """
    if current is None or previous is None:
        return Text("", style="dim")
    if current > previous:
        return Text("^", style="red")
    if current < previous:
        return Text("v", style="green")
    return Text("=", style="dim")


class OpenGrid(App):
    TITLE = "OpenGrid"
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; color: $text-muted; }
    DataTable { height: 1fr; }
    Input { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.ingest = Ingest()
        self.view = "listings"
        self.q: str | None = None
        self.only_available = False
        self.limit: int | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="status")
        yield DataTable(zebra_stripes=True, cursor_type="row")
        yield Input(placeholder="type a GPU, or: help", id="cmd")

    async def on_mount(self) -> None:
        init_db()
        self.ingest.start()
        # Redraw periodically so polled data appears without a keystroke.
        self.set_interval(5, self.render_view)
        self.query_one(Input).focus()
        self.render_view()

    async def on_unmount(self) -> None:
        await self.ingest.stop()

    def show(self, view: str, q: str | None = None, only_available: bool = False, limit: int | None = None):
        self.view, self.q, self.only_available, self.limit = view, q, only_available, limit
        self.render_view()

    def run_fetch(self) -> None:
        if self.busy:
            self.notify("already running")
            return
        self.busy = True
        self.notify("fetching...")
        self.run_worker(self._fetch_worker(), exclusive=True)

    async def _fetch_worker(self) -> None:
        try:
            responses = await fetch_all()
            await asyncio.to_thread(save, responses)
            failed = sum(1 for r in responses if not r.ok)
            result = await asyncio.to_thread(self._normalize)
            self.notify(
                f"{len(responses)} responses ({failed} failed) - "
                f"{result['listings']} listings, {result['observations']} changes recorded"
            )
        except Exception as exc:
            self.notify(f"fetch failed: {exc}", severity="error")
        finally:
            self.busy = False
            self.render_view()

    def run_normalize(self) -> None:
        try:
            result = self._normalize()
            self.notify(
                f"{result['listings']} listings, {result['observations']} changes, "
                f"{result['reference_prices']} reference prices"
            )
        except Exception as exc:
            self.notify(f"normalize failed: {exc}", severity="error")
        self.render_view()

    @staticmethod
    def _normalize() -> dict:
        return normalize.refresh()

    def render_view(self) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        count = {
            "listings": self._render_listings,
            "history": self._render_history,
            "mapping": self._render_mapping,
            "quirks": self._render_quirks,
            "refs": self._render_refs,
            "unmapped": self._render_unmapped,
            "raw": self._render_raw,
            "summary": self._render_summary,
        }[self.view](table)
        parts = [self.view]
        if self.q:
            parts.append(f"filter: {self.q}")
        if self.only_available:
            parts.append("in stock only")
        parts.append(f"{count} rows")
        if self.busy:
            parts.append("fetching...")
        parts.append(f"polling: {self.ingest.describe()}")
        self.query_one("#status", Static).update(" \u00b7 ".join(parts))

    def _render_listings(self, table: DataTable) -> int:
        rows = normalize.listings(self.q, self.only_available, self.limit)
        table.add_columns(
            "provider", "canonical gpu", "x", "region", "market", "tier", "sku",
            "vcpu", "ram", "disk", "$/gpu-hr", "d", "prev", "$/inst-hr",
            "stock", "cap", "unit", "age"
        )
        for r in rows:
            table.add_row(
                r["provider"],
                r["canonical_gpu_name"] or Text(f"? {r['raw_gpu_name']}", style="yellow"),
                str(r["gpu_count"]),
                r["region"] or "-",
                r["market_type"] or "-",
                r["provider_tier"] or "-",
                r["sku"],
                num(r["vcpu"]),
                num(r["ram_gb"]),
                num(r["storage_gb"]),
                Text(num(r["price_per_gpu_hour"], 3), justify="right"),
                price_arrow(r["price_per_gpu_hour"], r["previous_price_per_gpu_hour"]),
                Text(num(r["previous_price_per_gpu_hour"], 3), style="dim", justify="right"),
                Text(num(r["price_per_instance_hour"], 2), justify="right"),
                Text("*", style="green") if r["available"] else Text("o", style="red"),
                num(r["capacity"]),
                r["capacity_unit"] or "-",
                age(r["observed_at"]),
            )
        return len(rows)

    def _render_history(self, table: DataTable) -> int:
        rows = normalize.history(self.q)
        table.add_columns(
            "when", "provider", "canonical gpu", "x", "region", "market", "tier",
            "sku", "$/gpu-hr", "$/inst-hr", "stock", "cap", "unit"
        )
        for r in rows:
            table.add_row(
                f"{r['observed_at']:%m-%d %H:%M:%S}",
                r["provider"],
                r["canonical_gpu_name"] or Text(f"? {r['raw_gpu_name']}", style="yellow"),
                str(r["gpu_count"]),
                r["region"] or "-",
                r["market_type"] or "-",
                r["provider_tier"] or "-",
                r["sku"],
                Text(num(r["price_per_gpu_hour"], 3), justify="right"),
                Text(num(r["price_per_instance_hour"], 2), justify="right"),
                Text("*", style="green") if r["available"] else Text("o", style="red"),
                num(r["capacity"]),
                r["capacity_unit"] or "-",
            )
        return len(rows)

    def _render_refs(self, table: DataTable) -> int:
        rows = normalize.reference_prices(self.q)
        table.add_columns("provider", "name", "kind", "listed", "value", "was", "discount", "age")
        for r in rows:
            table.add_row(
                r["provider"],
                r["name"],
                r["kind"] or Text("?", style="yellow"),
                Text("listing", style="green") if r["is_listed"] else Text("-", style="dim"),
                Text(num(r["value"], 6), justify="right"),
                Text(num(r["original_value"], 6), justify="right"),
                "yes" if r["discount_applied"] else "-",
                age(r["observed_at"]),
            )
        return len(rows)

    KIND_STYLES = {
        mapping.RAW: "green",
        mapping.DERIVED: "cyan",
        mapping.CONSTANT: "yellow",
        mapping.LOOKUP: "magenta",
        mapping.ABSENT: "red",
    }

    def _wanted_providers(self) -> list[str]:
        if not self.q:
            return list(mapping.PROVIDER_MAPPINGS)
        hit = [p for p in mapping.PROVIDER_MAPPINGS if self.q.lower() in p.lower()]
        return hit or list(mapping.PROVIDER_MAPPINGS)

    def _render_mapping(self, table: DataTable) -> int:
        """Side by side, one row per ComputeListing field.

        All providers: compact, so three columns stay readable. One provider:
        the notes as well. A field the providers disagree on is marked.
        """
        wanted = self._wanted_providers()
        detail = len(wanted) == 1
        table.add_columns(
            "", "field", *(wanted if not detail else [wanted[0], "note"])
        )
        for row in mapping.comparison_rows(wanted):
            marker = Text("!", style="bold yellow") if row["differs"] else Text("")
            cells = [marker, Text(row["field"], style="bold" if row["differs"] else "")]
            for provider in wanted:
                fm = row.get(provider)
                if fm is None:
                    cells.append(Text("(undocumented)", style="red"))
                else:
                    cells.append(
                        Text(f"{fm.kind}: {fm.source}", style=self.KIND_STYLES.get(fm.kind, ""))
                    )
                    if detail:
                        cells.append(Text(fm.note, style="dim"))
            table.add_row(*cells)
        return len(mapping.MODEL_FIELDS)

    def _render_quirks(self, table: DataTable) -> int:
        rows = mapping.quirk_rows(self._wanted_providers())
        table.add_columns("provider", "gotcha")
        for r in rows:
            table.add_row(r["provider"], r["quirk"])
        return len(rows)

    def _render_unmapped(self, table: DataTable) -> int:
        rows = normalize.unmapped_gpus()
        table.add_columns("provider", "raw gpu name", "reason")
        for r in rows:
            table.add_row(r["provider"], r["raw_gpu_name"], r["reason"])
        return len(rows)

    def _render_raw(self, table: DataTable) -> int:
        rows = raw_store.snapshots(q=self.q)
        table.add_columns("provider", "endpoint", "method", "status", "ms", "bytes", "age")
        for r in rows:
            table.add_row(
                r["provider"], r["endpoint"], r["method"],
                Text(str(r["status_code"] or "-"), style="green" if r["ok"] else "red"),
                str(r["duration_ms"] or "-"), f"{(r['bytes'] or 0):,}", age(r["fetched_at"]),
            )
        return len(rows)

    def _render_summary(self, table: DataTable) -> int:
        rows = raw_store.endpoint_summary()
        table.add_columns("provider", "endpoint", "fetches", "distinct", "avg ms", "fails", "last")
        for r in rows:
            table.add_row(
                r["provider"], r["endpoint"], str(r["fetches"]), str(r["distinct_bodies"]),
                str(int(r["avg_ms"] or 0)),
                Text(str(r["failures"]), style="red" if r["failures"] else "dim"),
                age(r["last_fetch"]),
            )
        return len(rows)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        for cmd in COMMANDS:
            if m := cmd.pattern.fullmatch(text):
                cmd.handler(self, m)
                return
        self.notify(f"unknown command: {text}", severity="warning")


def main() -> None:
    log_dir = Path.home() / ".opengrid"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(filename=log_dir / "opengrid.log", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    OpenGrid().run()


if __name__ == "__main__":
    main()
