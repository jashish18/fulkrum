"""All-tasks overview: one row per task (stage, flags, parts) and one row per card across tasks."""
from __future__ import annotations

import re
from typing import Any

from core import impact

STAGES = ["report only", "uploaded", "to grade", "deciding", "decided", "verifying", "verified"]
STAGE_HELP = {
    "report only": "a rework report with no task zip yet",
    "uploaded": "task zip saved, no rework report yet",
    "to grade": "report saved; grade the gold and model output",
    "deciding": "some cards still have no final decision",
    "decided": "every card has a final decision; upload the revised version to verify",
    "verifying": "a revised version was checked and some changes are still open",
    "verified": "every expected change is addressed and no unexplained change is left",
}
FLAGS = ["BREAK", "CONFLICT", "REVERSAL", "AMBIGUOUS", "RERUN", "AWAITING"]


def _category(card: impact.CardState) -> str:
    for r in card.rows:
        m = re.match(r"\s*(Category\s+\d+)", r.get("category_row") or "", re.I)
        if m:
            return m.group(1).title()
    return ""


def data_version() -> tuple:
    """Changes whenever anything that the overview shows changes (for caching)."""
    from sqlalchemy import func

    from db.models import AuditLog, Decision, Grade, Report, Verification, Version
    from db.session import session_scope

    with session_scope() as s:
        return tuple(tuple(int(x or 0) for x in s.query(func.count(m.id), func.max(m.id)).one()) for m in
                     (AuditLog, Decision, Grade, Report, Verification, Version))


def build() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(task rows, card rows) for every task."""
    from core import verify
    from db.models import Report, Task
    from db.session import session_scope

    with session_scope() as s:
        tasks = [(t.id, t.task_code, [v.label for v in sorted(t.versions, key=lambda v: v.uploaded_at)])
                 for t in s.query(Task)]
        latest: dict[int, int] = {}
        for r in s.query(Report).order_by(Report.uploaded_at, Report.id):
            latest[r.task_id] = r.id
    tasks.sort(key=lambda t: (len(t[1]), t[1]))
    task_rows, card_rows = [], []
    for tid, code, versions in tasks:
        row: dict[str, Any] = {"Task": code, "Versions": ", ".join(versions) or "–", "report_id": latest.get(tid)}
        rid = latest.get(tid)
        if rid is None:
            row.update(Stage="uploaded", Cards=0, Final=0)
            task_rows.append(row)
            continue
        impact.prepare(rid)
        st = impact.load_state(rid)
        fl = impact.compute_flags(st)
        decs = impact.current_decisions(st)
        finals = sum(1 for c in st.cards.values() if (c.decisions.get("final") or {}).get("option_key"))
        vp = None
        try:
            vp = verify.progress(rid)
        except Exception:  # noqa: BLE001 - the overview must still show
            vp = None
        if st.version_id is None:
            stage = "report only"
        elif not st.has_grades:
            stage = "to grade"
        elif finals < len(st.cards):
            stage = "deciding"
        elif vp is None:
            stage = "decided"
        elif vp["total"] and vp["addressed"] == vp["total"] and not vp["untracked"]:
            stage = "verified"
        else:
            stage = "verifying"
        parts: dict[str, int] = {}
        cats: dict[str, int] = {}
        for c in st.cards.values():
            parts[c.part] = parts.get(c.part, 0) + 1
            cat = _category(c)
            if cat:
                cats[cat] = cats.get(cat, 0) + 1
        row.update({
            "Stage": stage, "Flagged at": st.flagged_version or "",
            "Cards": len(st.cards), "Decided": sum(1 for d in decs.values() if d), "Final": finals,
            **{k: fl["counts"][k] for k in FLAGS},
            "Parts": " · ".join(f"{p}: {n}" for p, n in sorted(parts.items(), key=lambda kv: kv[0], reverse=True)),
            "Categories": " · ".join(f"{k}: {v}" for k, v in sorted(cats.items())),
            "Model %": (f"{fl['baseline']['model']['score_pct']:.1%} → {fl['projection']['model']['score_pct']:.1%}"
                        if st.has_grades else ""),
            "Gold %": f"{fl['projection']['gold']['score_pct']:.1%}" if st.has_grades else "",
            "Breaks": "; ".join(fl["task_breaks"]),
            "Verification": (f"{vp['label']}: {vp['addressed']}/{vp['total']} addressed"
                             + (f", {vp['untracked']} untracked" if vp["untracked"] else "")) if vp else "",
        })
        task_rows.append(row)
        for code_, c in st.cards.items():
            d = decs[code_]
            final = c.decisions.get("final")
            cf = fl["cards"][code_]
            card_rows.append({
                "Task": code, "Card": code_, "Part": c.part, "Category": _category(c), "Rule": c.rule or "",
                "Theme": c.theme or "", "Decision": _label(c, d), "Final": "yes" if final else "",
                "Flags": ", ".join(k for k in FLAGS if k in cf), "Stage": stage, "report_id": rid,
            })
    return task_rows, card_rows


def _label(card: impact.CardState, d: dict[str, Any] | None) -> str:
    from core.ui import decision_label

    return decision_label(card, d) if d else ""
