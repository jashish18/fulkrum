"""File extraction (spec section 6).

Each extractor returns a plain dict that is saved as JSON under DATA_DIR and
referenced by files.text_path. Nothing here raises on a bad file: errors are
returned in the dict under "error" so the page can show a clear message.
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

DELIVERABLE_EXTS = {".xlsx", ".xlsm", ".docx", ".pptx", ".pdf"}
OFFICE_EXTS = {".xlsx", ".xlsm", ".docx", ".pptx"}
TEXT_EXTS = {".txt", ".md", ".json", ".csv", ""}

DEFAULT_COL_WIDTH = 8.43
MAX_SHEET_ROWS_FOR_LLM = 300


# ---------------------------------------------------------------------------
# Number-format rendering
# ---------------------------------------------------------------------------

_DATE_FORMATS = {
    "mm-dd-yy": "%m-%d-%y",
    "m/d/yy": "%-m/%-d/%y",
    "m/d/yyyy": "%-m/%-d/%Y",
    "d-mmm-yy": "%-d-%b-%y",
    "d-mmm": "%-d-%b",
    "mmm-yy": "%b-%y",
    "yyyy-mm-dd": "%Y-%m-%d",
    "dd/mm/yyyy": "%d/%m/%Y",
    "mm/dd/yyyy": "%m/%d/%Y",
    "d/m/yyyy": "%-d/%-m/%Y",
    "dd-mmm-yyyy": "%d-%b-%Y",
    "mmm d, yyyy": "%b %-d, %Y",
    "mmmm yyyy": "%B %Y",
    "mmm yyyy": "%b %Y",
    "h:mm": "%-H:%M",
    "h:mm:ss": "%-H:%M:%S",
    "m/d/yy h:mm": "%-m/%-d/%y %-H:%M",
    "yyyy-mm-dd h:mm:ss": "%Y-%m-%d %-H:%M:%S",
}


def _clean_fmt(fmt: str) -> str:
    """Strip colour codes, locale tags, quotes and escapes that don't change digits."""
    f = re.sub(r"\[[^\]]*\]", "", fmt)  # [Red], [$-409]
    f = f.replace("\\", "").replace('"', "")
    f = f.replace("_)", "").replace("_(", "").replace("* ", "")
    return f.strip()


def _round_half_up(x: float, nd: int) -> float:
    from decimal import ROUND_HALF_UP, Decimal

    q = Decimal(1).scaleb(-nd) if nd > 0 else Decimal(1)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def _fmt_number(x: float, section: str) -> str | None:
    """Render one number-format section such as '#,##0.0', '0%', '$#,##0'."""
    s = section
    pct = "%" in s
    m = re.search(r"([#0,]*0)(\.(0+))?", s)
    if not m:
        return None
    int_part = m.group(1)
    decimals = len(m.group(3) or "")
    comma = "," in int_part
    val = x * 100 if pct else x
    # trailing commas scale by 1000 ("#,##0,")
    trail = re.search(r"0(,+)(?![#0])", s)
    if trail:
        val = val / (1000 ** len(trail.group(1)))
        comma = "," in int_part[: -len(trail.group(1))]
    val = _round_half_up(val, decimals)
    body = f"{abs(val):,.{decimals}f}" if comma else f"{abs(val):.{decimals}f}"
    prefix = s[: m.start()].replace("%", "")
    suffix = s[m.end():]
    if trail:
        suffix = suffix[len(trail.group(1)):] if suffix.startswith(",") else suffix
    return f"{prefix}{body}{suffix}".strip(), val < 0  # type: ignore[return-value]


def render_display(value: Any, number_format: str | None) -> str:
    """Best-effort reproduction of what Excel shows in the cell."""
    if value is None:
        return ""
    fmt = (number_format or "General").strip()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        key = _clean_fmt(fmt).lower()
        pyfmt = _DATE_FORMATS.get(key)
        try:
            if pyfmt:
                return value.strftime(pyfmt)
            if isinstance(value, dt.datetime) and value.time() == dt.time(0):
                return value.strftime("%Y-%m-%d")
            return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
        except ValueError:
            return str(value)
    if isinstance(value, (int, float)):
        if fmt == "General" or fmt == "@":
            if isinstance(value, float):
                if value.is_integer() and abs(value) < 1e15:
                    return str(int(value))
                return f"{value:.10g}"
            return str(value)
        sections = fmt.split(";")
        if value < 0 and len(sections) > 1:
            sec = _clean_fmt(sections[1])
            out = _fmt_number(-value, sec)
            if out is None:
                return str(value)
            text, _ = out  # type: ignore[misc]
            return text
        if value == 0 and len(sections) > 2:
            sec = _clean_fmt(sections[2])
            if not re.search(r"[0#]", sec):
                return sec.strip()
            out = _fmt_number(value, sec)
            return out[0] if out else str(value)  # type: ignore[index]
        sec = _clean_fmt(sections[0])
        out = _fmt_number(value, sec)
        if out is None:
            return str(value)
        text, neg = out  # type: ignore[misc]
        return f"-{text}" if neg and text.strip("0.,%") else text
    return str(value)


# ---------------------------------------------------------------------------
# Office metadata and package parts
# ---------------------------------------------------------------------------

_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
}

# Values that are tools or placeholders rather than people.
_GENERIC_AUTHORS = {
    "openpyxl", "python-docx", "python-pptx", "microsoft office user", "microsoft office",
    "apache poi", "author", "user", "admin", "administrator", "unknown", "owner",
    "microsoft excel", "microsoft word", "microsoft powerpoint", "libreoffice", "calc",
    "writer", "google", "docs", "sheets", "excel", "word", "windows user", "pc", "",
    "xlsxwriter", "aspose", "pandoc", "anonymous", "claude", "none",
}


def looks_like_person(value: str | None) -> bool:
    """Heuristic: a non-generic value of 1-4 alphabetic words is treated as a name.

    False positives are fine (the user reviews them); misses are worse.
    """
    if not value:
        return False
    v = value.strip()
    if v.lower() in _GENERIC_AUTHORS:
        return False
    if re.search(r"(microsoft|office|libreoffice|openpyxl|python|adobe|acrobat|pdf|"
                 r"word|excel|powerpoint|quartz|skia|chrome|reportlab|ghostscript|"
                 r"latex|tex|macos|windows|version|\d)", v, re.I):
        return False
    words = re.split(r"[\s,._-]+", v)
    words = [w for w in words if w]
    if not 1 <= len(words) <= 4:
        return False
    return all(re.fullmatch(r"[A-Za-zÀ-ÿ'’]+", w) for w in words)


def office_metadata(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "docProps/core.xml" in names:
                root = ET.fromstring(z.read("docProps/core.xml"))
                for tag, key in (("dc:creator", "creator"), ("cp:lastModifiedBy", "lastModifiedBy"),
                                 ("dc:title", "title")):
                    el = root.find(tag, _NS)
                    if el is not None and el.text:
                        meta[key] = el.text.strip()
            if "docProps/app.xml" in names:
                root = ET.fromstring(z.read("docProps/app.xml"))
                for tag, key in (("ep:Company", "Company"), ("ep:Manager", "Manager"),
                                 ("ep:Application", "Application")):
                    el = root.find(tag, _NS)
                    if el is not None and el.text:
                        meta[key] = el.text.strip()
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
        meta["_error"] = str(e)
    return meta


def personal_names(meta: dict[str, Any]) -> list[dict[str, str]]:
    fields = ("creator", "lastModifiedBy", "Company", "Manager", "Author")
    return [
        {"field": f, "value": str(meta[f])}
        for f in fields
        if f in meta and looks_like_person(str(meta[f]))
    ]


def package_parts(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as z:
            parts = sorted(z.namelist())
    except zipfile.BadZipFile:
        return {"parts": [], "webextensions": []}
    return {"parts": parts, "webextensions": [p for p in parts if p.startswith("xl/webextensions/")]}


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------


def _col_width(ws, col_letter: str) -> float:
    dim = ws.column_dimensions.get(col_letter)
    if dim is not None and dim.width:
        return float(dim.width)
    default = getattr(ws.sheet_format, "defaultColWidth", None)
    if default:
        return float(default)
    base = getattr(ws.sheet_format, "baseColWidth", None)
    return float(base) + 0.71 if base else DEFAULT_COL_WIDTH


def _merged_width(ws, cell, merged_lookup: dict[str, float]) -> float | None:
    return merged_lookup.get(cell.coordinate)


def extract_xlsx(path: Path) -> dict[str, Any]:
    import openpyxl
    from openpyxl.utils import get_column_letter

    out: dict[str, Any] = {"kind": "xlsx", "sheets": [], "warnings": []}
    try:
        wb_f = openpyxl.load_workbook(path, data_only=False)
        wb_v = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:  # noqa: BLE001 - any openpyxl failure is a parse failure
        return {"kind": "xlsx", "error": f"Could not open workbook: {e}"}

    missing_cache = 0
    for ws in wb_f.worksheets:
        wv = wb_v[ws.title]
        merged: dict[str, float] = {}
        for rng in ws.merged_cells.ranges:
            total = sum(_col_width(ws, get_column_letter(c)) for c in range(rng.min_col, rng.max_col + 1))
            merged[f"{get_column_letter(rng.min_col)}{rng.min_row}"] = total
        widths = {}
        for letter, dim in ws.column_dimensions.items():
            if dim.width:
                widths[letter] = dim.width
        sheet: dict[str, Any] = {
            "name": ws.title,
            "state": ws.sheet_state,  # visible / hidden / veryHidden
            "max_row": ws.max_row,
            "max_col": ws.max_column,
            "column_widths": widths,
            "cells": [],
        }
        for row in ws.iter_rows():
            for c in row:
                if c.value is None and c.comment is None:
                    continue
                v_cell = wv[c.coordinate]
                formula = None
                raw = c.value
                if isinstance(raw, str) and raw.startswith("="):
                    formula = raw
                elif raw is not None and type(raw).__name__ in ("ArrayFormula", "DataTableFormula"):
                    formula = getattr(raw, "text", str(raw))
                value = v_cell.value if formula else raw
                if formula and value is None:
                    missing_cache += 1
                fmt = c.number_format or "General"
                display = render_display(value, fmt)
                rec: dict[str, Any] = {
                    "addr": c.coordinate,
                    "formula": formula,
                    "value": value if isinstance(value, (int, float, str, bool)) or value is None else str(value),
                    "number_format": fmt,
                    "display": display,
                }
                if c.comment is not None:
                    rec["comment"] = c.comment.text
                if isinstance(value, (int, float, dt.date, dt.datetime)) and not isinstance(value, bool):
                    width = merged.get(c.coordinate) or _col_width(ws, c.column_letter)
                    # Excel shows #### only for numbers/dates that don't fit; text spills over.
                    if len(display) > width + 1:
                        rec["probable_hashes"] = True
                sheet["cells"].append(rec)
        out["sheets"].append(sheet)
    if missing_cache:
        out["warnings"].append(f"recalculation needed: {missing_cache} formula cells have no cached value")
        out["recalculation_needed"] = True
    out["hidden_sheets"] = [s["name"] for s in out["sheets"] if s["state"] != "visible"]
    out["probable_hashes"] = [
        f"{s['name']}!{c['addr']}" for s in out["sheets"] for c in s["cells"] if c.get("probable_hashes")
    ]
    return out


def recalc_with_libreoffice(path: Path, outdir: Path) -> Path | None:
    """Recalculate a workbook with headless LibreOffice, if installed. Returns the new path."""
    import shutil
    import subprocess

    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", "xlsx", "--outdir", str(outdir), str(path)],
            check=True, capture_output=True, timeout=180,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    new = outdir / (path.stem + ".xlsx")
    return new if new.exists() else None


def xlsx_text_view(ex: dict[str, Any], full: bool = False) -> str:
    """One line per cell for the LLM: `B7 | =B6*(1+C5) | 4261166.79 | shown 4,261,167 | fmt #,##0`."""
    if ex.get("error"):
        return f"[unreadable workbook: {ex['error']}]"
    lines: list[str] = []
    for s in ex["sheets"]:
        state = "" if s["state"] == "visible" else f" ({s['state']})"
        lines.append(f"### Sheet: {s['name']}{state}")
        cells = s["cells"]
        if not full and s["max_row"] > MAX_SHEET_ROWS_FOR_LLM:
            header = [c for c in cells if _row_of(c["addr"]) == _first_row(cells)]
            lines.extend(_cell_line(c) for c in header)
            lines.append(f"... {s['max_row']} rows x {s['max_col']} columns, {len(cells)} non-empty cells "
                         f"(full data available for targeted lookups)")
            continue
        lines.extend(_cell_line(c) for c in cells)
    return "\n".join(lines)


def _row_of(addr: str) -> int:
    return int(re.sub(r"[A-Z]+", "", addr))


def _first_row(cells: list[dict]) -> int:
    return min((_row_of(c["addr"]) for c in cells), default=1)


def _cell_line(c: dict[str, Any]) -> str:
    formula = c["formula"] or ""
    parts = [c["addr"], formula, "" if c["value"] is None else str(c["value"]),
             f"shown {c['display']}", f"fmt {c['number_format']}"]
    line = " | ".join(parts)
    if c.get("comment"):
        line += f" | comment: {c['comment']!r}"
    return line


# ---------------------------------------------------------------------------
# docx / pptx / pdf / text
# ---------------------------------------------------------------------------


def extract_docx(path: Path) -> dict[str, Any]:
    try:
        import docx

        d = docx.Document(str(path))
    except Exception as e:  # noqa: BLE001
        return {"kind": "docx", "error": f"Could not open document: {e}"}
    paras = [{"id": f"p{i}", "style": p.style.name if p.style is not None else None, "text": p.text}
             for i, p in enumerate(d.paragraphs, 1)]
    tables = []
    for ti, t in enumerate(d.tables, 1):
        tables.append({"id": f"t{ti}", "rows": [[cell.text for cell in r.cells] for r in t.rows]})
    return {"kind": "docx", "paragraphs": paras, "tables": tables}


def extract_pptx(path: Path) -> dict[str, Any]:
    """Slide text via the XML (python-pptx isn't in the stack)."""
    slides = []
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted(
                (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
            )
            for n in names:
                root = ET.fromstring(z.read(n))
                texts = [t.text for t in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t") if t.text]
                slides.append({"id": f"s{len(slides) + 1}", "text": "\n".join(texts)})
    except (zipfile.BadZipFile, ET.ParseError) as e:
        return {"kind": "pptx", "error": f"Could not open presentation: {e}"}
    return {"kind": "pptx", "slides": slides}


def extract_pdf(path: Path) -> dict[str, Any]:
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            pages = [{"id": f"page{i}", "text": p.extract_text() or ""} for i, p in enumerate(pdf.pages, 1)]
            meta = {k: str(v) for k, v in (pdf.metadata or {}).items()
                    if k in ("Author", "Creator", "Producer", "Title")}
    except Exception as e:  # noqa: BLE001
        return {"kind": "pdf", "error": f"Could not open PDF: {e}"}
    return {"kind": "pdf", "pages": pages, "pdf_metadata": meta}


def extract_text(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as e:
        return {"kind": "text", "error": str(e)}
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return {"kind": "text", "text": raw.decode(enc)}
        except UnicodeDecodeError:
            continue
    return {"kind": "text", "error": "Could not decode text"}


def extract_file(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return (extraction, metadata, parts) for any supported file."""
    ext = path.suffix.lower()
    meta: dict[str, Any] = {}
    parts: dict[str, Any] = {}
    if ext in (".xlsx", ".xlsm"):
        ex = extract_xlsx(path)
    elif ext == ".docx":
        ex = extract_docx(path)
    elif ext == ".pptx":
        ex = extract_pptx(path)
    elif ext == ".pdf":
        ex = extract_pdf(path)
        meta = dict(ex.get("pdf_metadata", {}))
    elif ext in TEXT_EXTS or ext in {".xml", ".html", ".htm", ".yaml", ".yml"}:
        ex = extract_text(path)
    else:
        ex = {"kind": "binary", "note": f"No text extraction for {ext or 'this'} files"}
    if ext in OFFICE_EXTS:
        meta = office_metadata(path)
        parts = package_parts(path)
    meta["personal_names"] = personal_names(meta)
    return ex, meta, parts


def text_view(ex: dict[str, Any]) -> str:
    """Plain-text view of any extraction, used by the LLM and diffing."""
    if ex.get("error"):
        return f"[unreadable: {ex['error']}]"
    kind = ex.get("kind")
    if kind == "xlsx":
        return xlsx_text_view(ex)
    if kind == "docx":
        lines = [f"{p['id']}: {p['text']}" for p in ex["paragraphs"] if p["text"].strip()]
        for t in ex["tables"]:
            lines.append(f"[table {t['id']}]")
            lines.extend(" | ".join(r) for r in t["rows"])
        return "\n".join(lines)
    if kind == "pptx":
        return "\n\n".join(f"[{s['id']}]\n{s['text']}" for s in ex["slides"])
    if kind == "pdf":
        return "\n\n".join(f"[{p['id']}]\n{p['text']}" for p in ex["pages"])
    if kind == "text":
        return ex["text"]
    return ""
