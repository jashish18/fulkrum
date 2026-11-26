"""Parse one Suggested Finding text and one Defense cell (spec section 4)."""
from __future__ import annotations

import re
from typing import Any

# Order matters: longer labels first so "Proposed fix:" wins over "Fix:".
SECTION_LABELS = [
    "Impacted model run and failure notes",
    "Impacted reference files",
    "Impacted gold deliverable",
    "Impacted trajectory steps",
    "Impacted rubric criterion",
    "Impacted prompt",
    "Settled together",
    "Ask the expert",
    "Proposed fix",
    "Clear fix",
    "In short",
    "Context",
    "Issue",
    "Hence",
    "Fix",
    "Why",
]

IMPACT_ARTIFACT = {
    "Impacted rubric criterion": "rubric",
    "Impacted trajectory steps": "trajectory",
    "Impacted gold deliverable": "gold",
    "Impacted reference files": "reference",
    "Impacted prompt": "prompt",
    "Impacted model run and failure notes": "model_run",
}

# A label starts a section at the start of the text or of a line. "Hence:" also
# appears at the end of the Issue paragraph, so it may follow a space.
_LABEL_RX = re.compile(
    r"(?:(?<=^)|(?<=\n))[ \t]*(?P<label>" + "|".join(re.escape(l) for l in SECTION_LABELS) + r"):"
    r"|(?<=[\s.;:])(?P<inline>Hence):",
)


def split_sections(text: str) -> dict[str, str]:
    """Split on the known labels. Returns {label: body}; text before any label goes under "_preamble"."""
    text = (text or "").replace("\r\n", "\n")
    out: dict[str, str] = {}
    matches = list(_LABEL_RX.finditer(text))
    if not matches:
        return {"_preamble": text.strip()} if text.strip() else {}
    if text[: matches[0].start()].strip():
        out["_preamble"] = text[: matches[0].start()].strip()
    for i, m in enumerate(matches):
        label = m.group("label") or m.group("inline")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if label in out:  # repeated label: keep both
            out[label] = out[label] + "\n" + body
        else:
            out[label] = body
    return out


_OPTION_RX = re.compile(r"(?:^|\n)\s*-\s*Option\s+(\d)\s*:\s*", re.I)


def parse_options(hence: str) -> dict[str, str]:
    """Inside "Hence:", each option starts "- Option N:"."""
    out: dict[str, str] = {}
    ms = list(_OPTION_RX.finditer(hence or ""))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(hence)
        out[m.group(1)] = hence[m.end():end].strip()
    return out


def split_bullets(body: str) -> list[str]:
    """Impacted sections hold bullets that start "- " (on their own line, or " - " inline)."""
    body = (body or "").strip()
    if not body:
        return []
    parts = re.split(r"(?:^|\n)\s*-\s+|\s+-\s+(?=[\[(A-Z])", body)
    return [p.strip() for p in parts if p and p.strip()]


_SCOPE_PREFIX = re.compile(r"^\((Options?\s+[\d,\sand]+)\)\s*", re.I)
_SCOPE_PHRASE = re.compile(r"\b[Uu]nder\s+(Options?\s+\d(?:(?:\s*,\s*|\s+and\s+|\s+or\s+)\d)*)")


def option_scope(bullet: str) -> tuple[str | None, str]:
    """Return ("2" | "1,3" | None, bullet without the prefix). None = applies to every option."""
    m = _SCOPE_PREFIX.match(bullet)
    if m:
        return ",".join(re.findall(r"\d", m.group(1))), bullet[m.end():].strip()
    m = _SCOPE_PHRASE.search(bullet)
    if m:
        return ",".join(re.findall(r"\d", m.group(1))), bullet
    return None, bullet


# --- references --------------------------------------------------------------

_CRIT_RX = re.compile(r"\[(\d+\.\d+)\](?:\s*\(\s*([−-])?(\d+(?:\.\d+)?)\s+points?\))?")
_CRIT_RANGE_RX = re.compile(r"\[(\d+)\.(\d+)\]\s*(?:to|-|–)\s*\[(\d+)\.(\d+)\]")
_STEP_RX = re.compile(r"\bSteps?\s+(\d+\.\d+)")
_SHEET_CELL_RX = re.compile(
    r"'(?P<sheet>[^']+)'!(?P<refs>\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?"
    r"(?:(?:\s*,\s*|\s+and\s+)\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)*)"
)
_CELL_RX = re.compile(r"(?<![A-Za-z0-9_#'\-.])\$?([A-Z]{1,3})\$?([1-9]\d{0,6})(?::\$?([A-Z]{1,3})\$?([1-9]\d{0,6}))?(?![A-Za-z0-9_%\-])")
_NOT_CELLS = re.compile(r"^(FY|CY|Q|H|V|AG|HP|AL|PC|DR|CT|CP|TS|PW|SS|S|R|QA|G|F|P|T|AW|ID|NO)$")
_ROW_XREF_RX = re.compile(r"Category\s+(\d),\s+rows?\s+((?:\d+)(?:(?:\s*,\s*|\s+and\s+)\d+)*)")


def _col_ok(col: str) -> bool:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n <= 16384


def extract_refs(text: str, file_names: list[str] | None = None) -> dict[str, Any]:
    text = text or ""
    crits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _CRIT_RX.finditer(text):
        cid = m.group(1)
        pts = None
        if m.group(3):
            pts = float(m.group(3)) * (-1 if m.group(2) else 1)
        if cid in seen:
            if pts is not None:
                for c in crits:
                    if c["id"] == cid and c["points"] is None:
                        c["points"] = pts
            continue
        seen.add(cid)
        crits.append({"id": cid, "points": pts})
    ranges = []
    for m in _CRIT_RANGE_RX.finditer(text):
        s1, a, s2, b = (int(x) for x in m.groups())
        if s1 == s2 and a < b <= a + 60:
            ranges.append([f"{s1}.{i}" for i in range(a, b + 1)])
    steps = sorted(set(_STEP_RX.findall(text)), key=lambda s: tuple(int(x) for x in s.split(".")))

    cells: list[dict[str, str | None]] = []
    covered: list[tuple[int, int]] = []
    for m in _SHEET_CELL_RX.finditer(text):
        covered.append(m.span())
        for ref in re.split(r"\s*,\s*|\s+and\s+", m.group("refs")):
            cells.append({"sheet": m.group("sheet"), "ref": ref.replace("$", "")})
    for m in _CELL_RX.finditer(text):
        if any(a <= m.start() < b for a, b in covered):
            continue
        col, row, col2, row2 = m.groups()
        if _NOT_CELLS.match(col) or not _col_ok(col) or (col2 and not _col_ok(col2)):
            continue
        ref = f"{col}{row}" + (f":{col2}{row2}" if col2 else "")
        cells.append({"sheet": None, "ref": ref})

    files = []
    if file_names:
        low = text.lower()
        for fn in file_names:
            base = fn.rsplit("/", 1)[-1]
            if base and base.lower() in low:
                files.append(fn)
    xrefs = []
    for m in _ROW_XREF_RX.finditer(text):
        for n in re.findall(r"\d+", m.group(2)):
            xrefs.append(f"Category {m.group(1)}, row {n}")
    return {
        "criteria": crits,
        "criteria_ranges": ranges,
        "steps": steps,
        "cells": cells,
        "files": files,
        "row_xrefs": list(dict.fromkeys(xrefs)),
    }


# --- defense column ----------------------------------------------------------

# The spec names curly quotes; the sample reports use straight ones, so accept both.
_DEFENSE_RX = re.compile(
    r"QA #(\d+) \(Status: ([^)]+)\) · QA comment: [“\"](.*?)[”\"](?=\s*(?:$|QA #))", re.S
)


def parse_defense(text: str | None) -> list[dict[str, Any]]:
    return [
        {"qa_no": int(m.group(1)), "status": m.group(2).strip(), "comment": m.group(3).strip()}
        for m in _DEFENSE_RX.finditer(text or "")
    ]


# --- whole finding -----------------------------------------------------------


def parse_finding(text: str, part: str, file_names: list[str] | None = None) -> dict[str, Any]:
    """Parse a finding into sections, options and impacts.

    Returns {"sections", "options", "impacts", "refs", "close_call", "complete"}.
    `complete` is False when a Part 2 row has no options or a 1b/1a row has no fix,
    which is the cue for the LLM fallback.
    """
    sections = split_sections(text)
    options: dict[str, str] = {}
    if "Hence" in sections:
        options = parse_options(sections["Hence"])
    fix = sections.get("Proposed fix") or sections.get("Clear fix") or sections.get("Fix")
    if fix:
        options["fix"] = fix
    impacts = []
    for label, artifact in IMPACT_ARTIFACT.items():
        for b in split_bullets(sections.get(label, "")):
            scope, clean = option_scope(b)
            impacts.append({"artifact": artifact, "option_scope": scope, "text": clean,
                            "refs": extract_refs(clean, file_names)})
    if part == "2":
        # A folded Part 2 row can carry a proposed fix instead of options.
        complete = any(k in options for k in ("1", "2", "3", "fix"))
    else:
        complete = "fix" in options
    return {
        "sections": sections,
        "options": options,
        "impacts": impacts,
        "refs": extract_refs(text, file_names),
        "close_call": "Ask the expert" in sections,
        "complete": complete,
    }
