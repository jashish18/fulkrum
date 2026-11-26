"""Rework report import (spec section 3.2) and prior QA sheet (3.3).

`parse_report()` is pure: it reads the workbook and returns plain dicts.
`save_report()` writes them to the database.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl

from core.finding_parser import parse_defense, parse_finding

PART_SHEETS = {"2": "Part 2", "1b": "Part 1b", "1a": "Part 1a"}
COLUMNS = [
    "Card", "Part", "Category and row", "Settled with", "Theme", "Where", "Criteria",
    "Points at stake", "Suggested Finding", "Defense", "QA Comments", "Expert call",
    "Expert note", "Fixed by", "Rule", "Engine", "Also counts for", "Steps", "Task",
]
_SECTION_TITLE = re.compile(r"^\s*Part (2|1b|1a):")
_SUMMARY_RX = re.compile(
    r"Task\s+(?P<task>\d+),\s*flagged at\s+(?P<version>v\d+)[^:]*:\s*(?P<rows>\d+)\s+rows?\s+in\s+(?P<groups>\d+)\s+groups?",
    re.I,
)
_PART_COUNT_RX = re.compile(r"Part\s+(2|1b|1a):\s*(\d+)\s+groups?\s*\((\d+)\s+rows?\)", re.I)
_FOLDED_RX = re.compile(r"Folded under (Category \d+, row \d+)")
_TOGETHER_RX = re.compile(r"Decide together with (.*)", re.I)
_GROUP_REF_RX = re.compile(r"group\s+(\d[ab]?\.\d+)", re.I)


class ReportError(ValueError):
    """The report can't be imported; the message is shown to the user."""


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = str(v).strip()
    return t or None


def parse_summary(text: str) -> dict[str, Any]:
    m = _SUMMARY_RX.search(text or "")
    if not m:
        raise ReportError(f"Couldn't read the summary line: {text!r}")
    parts = {p.lower(): {"groups": int(g), "rows": int(r)} for p, g, r in _PART_COUNT_RX.findall(text)}
    return {
        "task": m.group("task"),
        "flagged_version": m.group("version"),
        "rows": int(m.group("rows")),
        "groups": int(m.group("groups")),
        "parts": parts,
    }


def _find_header(ws) -> tuple[int, dict[str, int]]:
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 40)):
        vals = [(_s(c.value) or "") for c in row]
        if "Card" in vals and "Suggested Finding" in vals:
            return row[0].row, {v: i for i, v in enumerate(vals) if v}
    raise ReportError(f"Sheet '{ws.title}' has no header row with both 'Card' and 'Suggested Finding'")


def _first_text(ws) -> str:
    for row in ws.iter_rows(min_row=1, max_row=5, values_only=True):
        for v in row:
            if _s(v):
                return str(v)
    return ""


@dataclass
class ParsedReport:
    filename: str
    task: str
    flagged_version: str
    expected: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    links: list[tuple[str, str, str]] = field(default_factory=list)  # (card, linked card, kind)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        out: dict[str, Any] = {"rows": len(self.rows), "groups": len(self.cards), "parts": {}}
        for p in PART_SHEETS:
            out["parts"][p] = {
                "groups": sum(1 for c in self.cards if c["part"] == p),
                "rows": sum(1 for r in self.rows if r["part"] == p),
            }
        return out


def parse_report(data: bytes, filename: str, file_names: list[str] | None = None) -> ParsedReport:
    """Read a rework report. Raises ReportError if it can't be read or the counts don't match."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ReportError(f"{filename} is not a readable Excel file: {e}") from e

    missing = [s for s in PART_SHEETS.values() if s not in wb.sheetnames]
    if missing:
        raise ReportError(f"{filename} is missing sheet(s): {', '.join(missing)}")
    task_sheet = next((s for s in wb.sheetnames if re.fullmatch(r"Task\s*\d+", s)), None)
    summary_text = _first_text(wb[task_sheet]) if task_sheet else _first_text(wb[PART_SHEETS["2"]])
    expected = parse_summary(summary_text)
    rep = ParsedReport(filename=filename, task=expected["task"],
                       flagged_version=expected["flagged_version"], expected=expected)

    for part, sheet in PART_SHEETS.items():
        ws = wb[sheet]
        hdr_row, cols = _find_header(ws)
        lacking = [c for c in ("Card", "Category and row", "Suggested Finding") if c not in cols]
        if lacking:
            raise ReportError(f"Sheet '{sheet}' is missing column(s): {', '.join(lacking)}")
        for row in ws.iter_rows(min_row=hdr_row + 1):
            vals = {name: _s(row[i].value) if i < len(row) else None for name, i in cols.items()}
            card = vals.get("Card")
            if card and _SECTION_TITLE.match(card):
                continue
            if not vals.get("Category and row") and not vals.get("Suggested Finding"):
                continue
            rec = {k: vals.get(k) for k in COLUMNS}
            rec["part"] = part
            rec["sheet"] = sheet
            rec["excel_row"] = row[0].row
            parsed = parse_finding(rec["Suggested Finding"] or "", part, file_names)
            rec["parsed"] = parsed
            rec["prior_stances"] = parse_defense(rec["Defense"])
            rec["parse_method"] = "regex" if parsed["complete"] else "incomplete"
            rep.rows.append(rec)

    _build_cards(rep)
    _check_counts(rep)
    return rep


def _build_cards(rep: ParsedReport) -> None:
    by_catrow = {r["Category and row"]: r for r in rep.rows if r["Category and row"]}
    order: list[str] = []
    cards: dict[str, dict[str, Any]] = {}
    for r in rep.rows:
        code = r["Card"]
        settled = r["Settled with"] or ""
        fm = _FOLDED_RX.search(settled)
        if not code and fm and fm.group(1) in by_catrow:
            code = by_catrow[fm.group(1)]["Card"]  # inherit the lead row's card
        if not code:
            rep.warnings.append(f"{r['sheet']} row {r['excel_row']} has no card code; skipped")
            continue
        r["Card"] = code
        if code not in cards:
            cards[code] = {"code": code, "part": r["part"], "rows": [], "lead": None}
            order.append(code)
        c = cards[code]
        c["rows"].append(r)
        r["folded_under"] = fm.group(1) if fm else None
        if not fm and c["lead"] is None:
            c["lead"] = r["Category and row"]
    for code in order:
        c = cards[code]
        if c["lead"] is None:
            c["lead"] = c["rows"][0]["Category and row"]
        for r in c["rows"]:
            r["is_lead"] = r["Category and row"] == c["lead"]
            # A row folded under a row in a different card links the two cards.
            if r["folded_under"] and r["folded_under"] in by_catrow:
                other = by_catrow[r["folded_under"]]["Card"]
                if other != code:
                    rep.links.append((code, other, "folded"))
    rep.cards = [cards[c] for c in order]

    codes = set(cards)
    row_card = {r["Category and row"]: r["Card"] for r in rep.rows if r.get("Card")}
    seen: set[tuple[str, str, str]] = set()

    def add(a: str, b: str, kind: str) -> None:
        if a == b or (a, b, kind) in seen:
            return
        seen.add((a, b, kind))
        rep.links.append((a, b, kind))

    for r in rep.rows:
        code = r.get("Card")
        if not code:
            continue
        tm = _TOGETHER_RX.search(r["Settled with"] or "")
        if tm:
            for other in _GROUP_REF_RX.findall(tm.group(1)):
                if other in codes:
                    add(code, other, "decide_together")
                    add(other, code, "decide_together")
                else:
                    rep.warnings.append(f"Card {code} says decide together with {other}, which isn't in the report")
        for x in r["parsed"]["refs"]["row_xrefs"]:
            other = row_card.get(x)
            if other and other != code:
                add(code, other, "mentions")
    rep.links = list(dict.fromkeys(rep.links))


def _check_counts(rep: ParsedReport) -> None:
    exp = rep.expected
    got = rep.counts()
    problems = []
    if got["rows"] != exp["rows"] or got["groups"] != exp["groups"]:
        problems.append(f"expected {exp['rows']} rows in {exp['groups']} groups, "
                        f"found {got['rows']} rows in {got['groups']} groups")
    for p, e in exp["parts"].items():
        g = got["parts"].get(p, {"rows": 0, "groups": 0})
        if g != e:
            problems.append(f"Part {p}: expected {e['groups']} groups ({e['rows']} rows), "
                            f"found {g['groups']} groups ({g['rows']} rows)")
    if problems:
        raise ReportError("The report's rows don't match its summary line: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Prior QA sheet (3.3)
# ---------------------------------------------------------------------------

QA_COLUMNS = {"#": "qa_no", "Feedback": "feedback", "Comment": "comment",
              "Check code and location": "check_code_location", "QA comment": "qa_comment",
              "Status": "status"}


def parse_qa_sheet(data: bytes, filename: str) -> list[dict[str, Any]]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ReportError(f"{filename} is not a readable Excel file: {e}") from e
    if "Grouped feedback" not in wb.sheetnames:
        raise ReportError(f"{filename} has no 'Grouped feedback' sheet")
    ws = wb["Grouped feedback"]
    hdr_row = None
    cols: dict[str, int] = {}
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 20)):
        vals = [(_s(c.value) or "") for c in row]
        if "#" in vals and "Feedback" in vals:
            hdr_row = row[0].row
            cols = {v: i for i, v in enumerate(vals) if v}
            break
    if hdr_row is None:
        raise ReportError(f"{filename}: no header row with '#' and 'Feedback'")
    out = []
    for row in ws.iter_rows(min_row=hdr_row + 1, values_only=True):
        rec = {key: _s(row[cols[h]]) if h in cols and cols[h] < len(row) else None for h, key in QA_COLUMNS.items()}
        if not rec["qa_no"] or not re.fullmatch(r"\d+", rec["qa_no"]):
            continue  # group header rows have no number
        rec["qa_no"] = int(rec["qa_no"])
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Persisting
# ---------------------------------------------------------------------------


def save_report(rep: ParsedReport, task_code: str | None = None, use_llm: bool = True,
                data: bytes | None = None) -> int:
    """Write a parsed report to the database. Returns the report id.

    Rows the regex parser couldn't complete go to the LLM fallback when it is configured.
    """
    from db.models import Card, CardLink, Impact, Option, PriorStance, Report, Row, Task
    from db.session import session_scope

    if use_llm:
        _llm_fill(rep)

    task_code = task_code or rep.task
    with session_scope() as s:
        task = s.query(Task).filter_by(task_code=task_code).one_or_none()
        if task is None:
            task = Task(task_code=task_code)
            s.add(task)
            s.flush()
        existing = s.query(Report).filter_by(task_id=task.id, filename=rep.filename,
                                             flagged_version=rep.flagged_version).one_or_none()
        if existing is not None:
            _delete_report_children(s, existing.id)
            report = existing
        else:
            report = Report(task_id=task.id, filename=rep.filename, flagged_version=rep.flagged_version)
            s.add(report)
        report.expected_counts_json = json.dumps({"expected": rep.expected, "found": rep.counts(),
                                                  "warnings": rep.warnings})
        s.flush()

        card_ids: dict[str, int] = {}
        for c in rep.cards:
            card = Card(report_id=report.id, code=c["code"], part=c["part"])
            s.add(card)
            s.flush()
            card_ids[c["code"]] = card.id
            for r in c["rows"]:
                p = r["parsed"]
                row = Row(
                    card_id=card.id, category_row=r["Category and row"], settled_with=r["Settled with"],
                    theme=r["Theme"], where=r["Where"], criteria_list=r["Criteria"],
                    points_at_stake=r["Points at stake"], finding_raw=r["Suggested Finding"],
                    sections_json=json.dumps({"sections": p["sections"], "refs": p["refs"],
                                              "close_call": p["close_call"],
                                              "excel_row": r["excel_row"], "sheet": r["sheet"]}),
                    defense_raw=r["Defense"], qa_comment=r["QA Comments"], expert_call=r["Expert call"],
                    expert_note=r["Expert note"], fixed_by=r["Fixed by"], rule=r["Rule"],
                    engine=r["Engine"], also_counts_for=r["Also counts for"], steps=r["Steps"],
                    parse_method=r["parse_method"],
                )
                s.add(row)
                s.flush()
                if r.get("is_lead"):
                    card.lead_row_id = row.id
                for key, text in p["options"].items():
                    s.add(Option(row_id=row.id, key=key, text=text))
                for imp in p["impacts"]:
                    s.add(Impact(row_id=row.id, option_scope=imp["option_scope"], artifact=imp["artifact"],
                                 text=imp["text"], refs_json=json.dumps(imp["refs"])))
                for ps in r["prior_stances"]:
                    s.add(PriorStance(row_id=row.id, qa_no=ps["qa_no"], status=ps["status"],
                                      comment=ps["comment"]))
        for a, b, kind in rep.links:
            if a in card_ids and b in card_ids:
                s.merge(CardLink(card_id=card_ids[a], linked_card_id=card_ids[b], kind=kind))
        if data is not None:
            report_original_path(report.id).write_bytes(data)
        return report.id


def report_original_path(report_id: int):
    """The uploaded report, kept unmodified under DATA_DIR."""
    from core import config

    return config.data_path("reports", str(report_id), "original.xlsx")


def _delete_report_children(s, report_id: int) -> None:
    from db.models import Card, CardLink, Impact, Option, PriorStance, Row

    card_ids = [c.id for c in s.query(Card.id).filter_by(report_id=report_id)]
    if not card_ids:
        return
    row_ids = [r.id for r in s.query(Row.id).filter(Row.card_id.in_(card_ids))]
    for model in (Option, Impact, PriorStance):
        s.query(model).filter(model.row_id.in_(row_ids)).delete(synchronize_session=False)
    s.query(Row).filter(Row.card_id.in_(card_ids)).delete(synchronize_session=False)
    s.query(CardLink).filter(CardLink.card_id.in_(card_ids) | CardLink.linked_card_id.in_(card_ids)).delete(
        synchronize_session=False)
    s.query(Card).filter(Card.id.in_(card_ids)).delete(synchronize_session=False)


def _llm_fill(rep: ParsedReport) -> None:
    """LLM fallback for rows the regex parser left incomplete (spec section 4)."""
    from core import llm

    todo = [r for r in rep.rows if r["parse_method"] == "incomplete"]
    if not todo:
        return
    if not llm.is_configured():
        rep.warnings.append(f"{len(todo)} row(s) had no options or fix and the LLM isn't configured; "
                            "they were kept as raw text only")
        return
    for r in todo:
        try:
            out = llm.extract_options(r["Suggested Finding"] or "", r["part"])
        except llm.LLMError as e:
            rep.warnings.append(f"LLM fallback failed for {r['Category and row']}: {e}")
            continue
        opts = {str(o["key"]): o["text"] for o in out.get("options", []) if o.get("text")}
        if opts:
            r["parsed"]["options"].update(opts)
            r["parse_method"] = "llm"


def save_qa_items(task_code: str, items: list[dict[str, Any]], source: str) -> int:
    from db.models import PriorQAItem, Task
    from db.session import session_scope

    with session_scope() as s:
        task = s.query(Task).filter_by(task_code=task_code).one_or_none()
        if task is None:
            task = Task(task_code=task_code)
            s.add(task)
            s.flush()
        s.query(PriorQAItem).filter_by(task_id=task.id).delete()
        for it in items:
            s.add(PriorQAItem(task_id=task.id, source=source, **it))
        return len(items)
