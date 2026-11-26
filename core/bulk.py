"""Bulk upload: many task zips, rework reports and QA sheets at once, matched by task number.

A single "container" zip that holds the task zips and reports is opened too.
Each task's version label comes from its report's "flagged at vNN" line.
A task is saved straight away when its file roles are certain; otherwise it
waits for the person to confirm the roles.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from core.ingest import Staged, parse_task_code, stage_zip

REQUIRED_ROLES = ("prompt", "rubric", "gold", "model_output")
_REPORT_RX = re.compile(r"rework_checks_0*(\d+)", re.I)
_QA_RX = re.compile(r"qa_feedback_0*(\d+)", re.I)


@dataclass
class TaskPlan:
    task: str
    zip_name: str | None = None
    zip_data: bytes | None = None
    report_name: str | None = None
    report_data: bytes | None = None
    qa_name: str | None = None
    qa_data: bytes | None = None
    label: str | None = None
    label_from: str = ""
    staged: Staged | None = None
    report: Any = None           # ParsedReport
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unsure_roles: list[str] = field(default_factory=list)
    status: str = ""             # ready | check roles | needs label | error | saved | skipped
    version_id: int | None = None
    report_id: int | None = None


def kind_of(name: str) -> str | None:
    low = name.lower().rsplit("/", 1)[-1]
    if low.endswith(".xlsx") and _REPORT_RX.search(low):
        return "report"
    if low.endswith(".xlsx") and _QA_RX.search(low):
        return "qa"
    if low.endswith(".zip"):
        return "zip"
    return None


def _is_container(data: bytes) -> bool:
    """A zip of task zips and reports, rather than one task (which has a rubric/prompt)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
    except zipfile.BadZipFile:
        return False
    if any(n.rsplit("/", 1)[-1].lower() in ("rubric.json", "prompt", "prompt.txt", "prompt.md") for n in names):
        return False
    task_zips = [n for n in names if n.lower().endswith(".zip") and parse_task_code(n.rsplit("/", 1)[-1])]
    reports = [n for n in names if kind_of(n) == "report"]
    return len(task_zips) >= 2 or (task_zips and reports) or len(reports) >= 2


def expand(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Open container zips (one level) and drop files we don't recognise."""
    out = []
    for name, data in files:
        if name.lower().endswith(".zip") and _is_container(data):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for n in z.namelist():
                    if n.endswith("/") or "__MACOSX" in n or n.rsplit("/", 1)[-1].startswith("."):
                        continue
                    if kind_of(n):
                        out.append((n.rsplit("/", 1)[-1], z.read(n)))
        elif kind_of(name):
            out.append((name.rsplit("/", 1)[-1], data))
    return out


def group(files: list[tuple[str, bytes]]) -> tuple[dict[str, TaskPlan], list[str]]:
    """Match files to tasks by the task number in their names. Returns (plans, unmatched names)."""
    plans: dict[str, TaskPlan] = {}
    unmatched: list[str] = []
    for name, data in expand(files):
        k = kind_of(name)
        if k == "report":
            t = _REPORT_RX.search(name).group(1)
        elif k == "qa":
            t = _QA_RX.search(name).group(1)
        else:
            t = parse_task_code(name)
        if not t:
            unmatched.append(name)
            continue
        p = plans.setdefault(t, TaskPlan(task=t))
        slot = {"report": "report", "qa": "qa", "zip": "zip"}[k]
        if getattr(p, f"{slot}_name"):
            p.problems.append(f"two {slot} files: {getattr(p, f'{slot}_name')} and {name}; using the first")
            continue
        setattr(p, f"{slot}_name", name)
        setattr(p, f"{slot}_data", data)
    return dict(sorted(plans.items(), key=lambda kv: (len(kv[0]), kv[0]))), unmatched


def check(p: TaskPlan) -> TaskPlan:
    """Stage the zip and parse the report, then decide the plan's status."""
    from core.report_parser import ReportError, parse_report

    p.problems = [x for x in p.problems if x.startswith("two ")]
    p.unsure_roles = []
    if p.zip_data:
        try:
            p.staged = stage_zip(p.zip_data, p.zip_name)
        except ValueError as e:
            p.problems.append(f"zip: {e}")
            p.staged = None
    if p.report_data:
        names = [f.name for f in p.staged.files] if p.staged else []
        try:
            p.report = parse_report(p.report_data, p.report_name, names)
        except ReportError as e:
            p.problems.append(f"report: {e}")
        except Exception as e:  # noqa: BLE001
            p.problems.append(f"report: couldn't read it ({e})")
    if p.report is not None and p.report.flagged_version:
        p.label, p.label_from = p.report.flagged_version, "report"
    elif p.staged and p.staged.suggested_label:
        p.label, p.label_from = p.staged.suggested_label, "zip name"
    if p.staged:
        if p.staged.rubric_error:
            p.problems.append(p.staged.rubric_error)
        counts = {r: sum(1 for f in p.staged.files if f.role == r) for r in REQUIRED_ROLES}
        p.unsure_roles = [f"no {r} file" for r, n in counts.items() if n == 0]
        p.unsure_roles += [f"{n} {r} files" for r, n in counts.items() if n > 1 and r != "gold"]
        p.unsure_roles += [f"{f.name}: role not recognised" for f in p.staged.files if f.role == "other"]
    if not p.zip_data:
        p.notes.append("no task zip; the report can only be read once the zip is uploaded")
    if not p.report_data:
        p.notes.append("no rework report")
    if any(x.startswith(("zip:", "report:")) for x in p.problems):
        p.status = "error"
    elif p.zip_data and not p.label:
        p.status = "needs label"
    elif p.unsure_roles:
        p.status = "check roles"
    else:
        p.status = "ready"
    return p


def existing_report(task: str, filename: str, flagged: str | None) -> int | None:
    from db.models import Report, Task
    from db.session import session_scope

    with session_scope() as s:
        t = s.query(Task).filter_by(task_code=task).one_or_none()
        if t is None:
            return None
        r = s.query(Report).filter_by(task_id=t.id, filename=filename, flagged_version=flagged).one_or_none()
        return r.id if r else None


def save(p: TaskPlan, user: str | None, roles: dict[str, str] | None = None, use_llm: bool = True,
         replace_reports: bool = False) -> TaskPlan:
    """Save the version, the report (re-read with the saved file names) and the QA sheet."""
    from core import impact
    from core.ingest import save_version
    from core.report_parser import ReportError, parse_qa_sheet, parse_report, save_qa_items, save_report
    from db.models import File
    from db.session import session_scope

    if p.staged is not None:
        if not p.label:
            raise ValueError("a version label is needed")
        roles = roles or {f.name: f.role for f in p.staged.files}
        p.version_id = save_version(p.staged, p.task, p.label, roles)
        p.notes.append(f"saved version {p.label}")
    if p.report_data:
        rid = existing_report(p.task, p.report_name, p.report.flagged_version if p.report else None)
        if rid is not None and not replace_reports:
            # Saving again would wipe the decisions already made on it.
            p.report_id = rid
            p.notes.append("report already saved; kept as it is (decisions are untouched)")
        else:
            names: list[str] = []
            if p.version_id:
                with session_scope() as s:
                    names = [f.name for f in s.query(File).filter_by(version_id=p.version_id)]
            try:
                rep = parse_report(p.report_data, p.report_name, names)
                p.report_id = save_report(rep, task_code=p.task, use_llm=use_llm, data=p.report_data)
                impact.prepare(p.report_id, user)
                p.notes.append("saved report")
            except ReportError as e:
                p.problems.append(f"report: {e}")
    if p.qa_data:
        try:
            n = save_qa_items(p.task, parse_qa_sheet(p.qa_data, p.qa_name), p.qa_name)
            p.notes.append(f"saved {n} QA items")
        except ReportError as e:
            p.problems.append(f"QA sheet: {e}")
    p.status = "saved" if not any(x.startswith(("report:", "QA sheet:")) for x in p.problems) else "error"
    return p
