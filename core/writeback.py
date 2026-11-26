"""Decisions workbook (spec section 11).

A copy of the uploaded report gets each final decision written into its Part
tabs' "Expert call" and "Expert note" columns, plus a "Dashboard summary"
sheet. The uploaded original is never modified, and cells holding formulas
are left alone.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from core import impact
from core.report_parser import PART_SHEETS, _find_header, report_original_path

SUMMARY_SHEET = "Dashboard summary"


def expert_call_text(card: impact.CardState, d: dict[str, Any]) -> tuple[str | None, str | None]:
    """(call, note prefix) for a final decision, using the report's own dropdown words where possible."""
    key = d.get("option_key")
    scope = d.get("scope") or []
    scope_txt = f" ({_scope_label(scope)})" if scope else ""
    new_txt = (d.get("new_option_text") or "").strip()
    if card.part == "2":
        if key in ("1", "2", "3"):
            return f"Option {key}{scope_txt}", None
        if key in ("keep", "disagree"):
            from core.qa_interpret import keep_option_key

            k = keep_option_key(card.options)
            return (f"Option {k}{scope_txt}", None) if k else ("Dismiss", None)
        if key == "new":
            return "Edited", f"Edited: {new_txt}" if new_txt else None
        return None, None
    if card.part == "1b":
        if key in ("agree", "fix"):
            return f"Agree{scope_txt}", None
        if key in ("disagree", "keep"):
            return "Disagree", None
        if key == "new":
            return "Edited", f"Edited: {new_txt}" if new_txt else None
        return None, None
    # Part 1a: a blank call lets the fixer go ahead; "Hold" stops it.
    if key in ("disagree", "keep"):
        return "Hold", None
    if key == "new":
        return "Hold", f"Edited: {new_txt}" if new_txt else None
    return None, None


def _scope_label(scope: list[str]) -> str:
    """6.13, 6.14, 6.15 -> 6.13–6.15 when consecutive."""
    try:
        parts = [(int(a), int(b)) for a, b in (x.split(".") for x in scope)]
    except ValueError:
        return ", ".join(scope)
    if len({p[0] for p in parts}) == 1 and len(parts) > 2:
        nums = sorted(p[1] for p in parts)
        if nums == list(range(nums[0], nums[-1] + 1)):
            return f"{parts[0][0]}.{nums[0]}–{parts[0][0]}.{nums[-1]}"
    return ", ".join(scope)


def output_name(filename: str, when: dt.date | None = None) -> str:
    stem = re.sub(r"\.xlsx$", "", filename, flags=re.I)
    return f"{stem} - decisions {(when or dt.date.today()).isoformat()}.xlsx"


def build_decisions_workbook(report_id: int, when: dt.date | None = None) -> tuple[bytes, str, dict[str, Any]]:
    """Return (xlsx bytes, file name, summary of what was written)."""
    from db.models import Report, Row
    from db.session import session_scope

    src = report_original_path(report_id)
    if not src.exists():
        raise FileNotFoundError("The original report file isn't stored. Upload the report again to enable "
                                "write-back.")
    with session_scope() as s:
        rep = s.get(Report, report_id)
        filename = rep.filename
    row_pos: dict[int, tuple[str, int]] = {}
    state = impact.load_state(report_id)
    with session_scope() as s:
        for card in state.cards.values():
            r = s.get(Row, card.lead_row_id)
            meta = json.loads(r.sections_json or "{}")
            if meta.get("sheet") and meta.get("excel_row"):
                row_pos[card.lead_row_id] = (meta["sheet"], int(meta["excel_row"]))

    wb = openpyxl.load_workbook(src)  # formulas and styles kept
    flags = impact.compute_flags(state)
    decs = impact.current_decisions(state)
    written: list[dict[str, Any]] = []
    skipped: list[str] = []

    cols: dict[str, dict[str, int]] = {}
    for sheet in PART_SHEETS.values():
        _, c = _find_header(wb[sheet])
        cols[sheet] = c

    for code, card in state.cards.items():
        final = card.decisions.get("final")
        if not final or not final.get("option_key") or final["option_key"] == "unclear":
            continue
        call, note_prefix = expert_call_text(card, final)
        note = " ".join(x for x in (note_prefix, (final.get("note") or "").strip()) if x) or None
        pos = row_pos.get(card.lead_row_id)
        if pos is None:
            skipped.append(f"{code}: row position unknown")
            continue
        sheet, excel_row = pos
        ws = wb[sheet]
        for col_name, value in (("Expert call", call), ("Expert note", note)):
            ci = cols[sheet].get(col_name)
            if ci is None or value is None:
                continue
            cell = ws.cell(row=excel_row, column=ci + 1)
            if isinstance(cell.value, str) and cell.value.startswith("="):
                skipped.append(f"{code}: {sheet}!{cell.coordinate} holds a formula; left as is")
                continue
            cell.value = value
            written.append({"card": code, "cell": f"{sheet}!{cell.coordinate}", "value": value})

    _summary_sheet(wb, state, flags, decs)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), output_name(filename, when), {"written": written, "skipped": skipped}


def _summary_sheet(wb, state: impact.TaskState, flags: dict[str, Any], decs: dict[str, Any]) -> None:
    from core.ui import decision_label

    if SUMMARY_SHEET in wb.sheetnames:
        del wb[SUMMARY_SHEET]
    ws = wb.create_sheet(SUMMARY_SHEET)
    proj = flags["projection"]
    ws.append([f"Task {state.task_code}, flagged at {state.flagged_version}. Written by the Task Rework "
               f"Dashboard on {dt.date.today().isoformat()}."])
    if state.has_grades:
        ws.append([f"Projected with every current decision: model {proj['model']['score_pct']:.1%}, "
                   f"gold {proj['gold']['score_pct']:.1%}. Re-run required: "
                   f"{'yes' if flags['rerun'] else 'no'}."])
    else:
        ws.append(["No grades yet, so scores are not projected."])
    ws.append([])
    header = ["Card", "Part", "Interpreted QA option", "Final decision", "Note", "Flags",
              "Projected model %", "Projected gold %", "Re-run"]
    ws.append(header)
    hdr_row = ws.max_row
    for c in ws[hdr_row]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="FFD9D9D9")
    for code, card in state.cards.items():
        qi = card.decisions.get("qa_interpreted")
        final = card.decisions.get("final")
        cf = flags["cards"][code]
        # Projection with this card's final decision on top of every other current decision.
        p = impact.what_if(state, code, final["option_key"], final.get("scope")) \
            if (state.has_grades and final) else proj
        ws.append([
            code, card.part, decision_label(card, qi) if qi else "",
            decision_label(card, final) if final else "", (final or {}).get("note") or "",
            ", ".join(k for k in ("BREAK", "CONFLICT", "REVERSAL", "AMBIGUOUS", "RERUN", "AWAITING") if k in cf),
            round(p["model"]["score_pct"] * 100, 1) if state.has_grades else "",
            round(p["gold"]["score_pct"] * 100, 1) if state.has_grades else "",
            "yes" if "RERUN" in cf else "",
        ])
    widths = [8, 6, 26, 26, 50, 30, 16, 16, 8]
    for i, wdt in enumerate(widths):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = wdt
    for row in ws.iter_rows(min_row=hdr_row + 1):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
