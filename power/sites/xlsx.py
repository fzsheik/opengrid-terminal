"""A streaming xlsx reader on the standard library (zip + iterparse), by sheet NAME.

    sheets(data) -> [sheet name, ...]
    rows(data, sheet) -> iterator of lists of strings, one per row, cells placed by their column letter so a
                         blank cell keeps its position (the files skip empty cells)
    table(data, sheet, must=(...)) -> (header, [dict, ...]): the first row holding every name in `must` is the
                         header; rows after it become dicts keyed by the normalised header (header_key)
    serial_date(v) -> date | None: an Excel day number ("46359") or an ISO / US date string

The EIA-860M Operating sheet is a 56 MB XML part for ~28k rows: iterparse with el.clear() keeps memory to the
shared strings plus one row. No third-party package (openpyxl is not a dependency).
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
EXCEL_EPOCH = date(1899, 12, 30)


def _zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not an xlsx: {exc}") from exc


def _targets(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> part path ("xl/worksheets/sheet6.xml"), through the workbook's relationships."""
    rels = {}
    for el in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(PNS + "Relationship"):
        t = el.get("Target", "")
        rels[el.get("Id")] = t.lstrip("/") if t.startswith("/") else "xl/" + t
    out = {}
    for el in ET.fromstring(z.read("xl/workbook.xml")).iter(NS + "sheet"):
        out[el.get("name")] = rels.get(el.get(RNS + "id"), "")
    return out


def sheets(data: bytes) -> list[str]:
    return list(_targets(_zip(data)))


def _strings(z: zipfile.ZipFile) -> list[str]:
    out: list[str] = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return out
    for _, el in ET.iterparse(z.open("xl/sharedStrings.xml")):
        if el.tag == NS + "si":
            out.append("".join(t.text or "" for t in el.iter(NS + "t")))
            el.clear()
    return out


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def rows(data: bytes, sheet: str):
    z = _zip(data)
    part = _targets(z).get(sheet)
    if not part or part not in z.namelist():
        raise ValueError(f"xlsx: no sheet {sheet!r}")
    strings = _strings(z)
    for _, el in ET.iterparse(z.open(part)):
        if el.tag != NS + "row":
            continue
        vals: list[str] = []
        for c in el.findall(NS + "c"):
            i = _col(c.get("r", "")) if c.get("r") else len(vals)
            t, v = c.get("t"), c.find(NS + "v")
            if t == "s" and v is not None:
                s = strings[int(v.text)]
            elif t == "inlineStr":
                s = "".join(x.text or "" for x in c.iter(NS + "t"))
            else:
                s = v.text if v is not None and v.text is not None else ""
            while len(vals) < i:
                vals.append("")
            vals.append(s)
        el.clear()
        yield vals


def header_key(h: str) -> str:
    """'Nameplate Capacity (MW)' -> 'nameplate_capacity_mw'; line breaks and repeated spaces collapse."""
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def table(data: bytes, sheet: str, must: tuple[str, ...]) -> tuple[list[str], list[dict]]:
    want = {header_key(m) for m in must}
    header: list[str] | None = None
    out: list[dict] = []
    for r in rows(data, sheet):
        if header is None:
            keys = [header_key(x) for x in r]
            if want <= set(keys):
                header = keys
            continue
        if not any((x or "").strip() for x in r):
            continue
        d = {}
        for i, k in enumerate(header):
            if k and k not in d:          # a repeated header name keeps its first column
                d[k] = (r[i] if i < len(r) else "").strip()
        out.append(d)
    if header is None:
        raise ValueError(f"xlsx: sheet {sheet!r} has no header row with {sorted(want)}")
    return header, out


def num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in (".", "-", "NA", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def serial_date(v) -> date | None:
    """An Excel serial day ("46359", "46359.5") or a written date. 1900-01-01 placeholders (ERCOT's "no actual
    date") and anything unreadable are None."""
    s = str(v or "").strip()
    if not s:
        return None
    x = num(s)
    if x is not None:
        if x < 2 or x > 80000:        # 1900-01-01 is serial 1; beyond year 2118 is not a date
            return None
        return EXCEL_EPOCH + timedelta(days=int(x))
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%y"):
        try:
            d = datetime.strptime(s[:19], fmt).date()
            return None if d.year <= 1900 else d
        except ValueError:
            continue
    return None
