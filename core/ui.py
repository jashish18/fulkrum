"""Small shared helpers for the Streamlit pages."""
from __future__ import annotations

import streamlit as st

FLAG_STYLE = {
    "BREAK": ("🟥", "breaks the task"),
    "CONFLICT": ("🟧", "conflicts with another decision"),
    "REVERSAL": ("🟪", "reverses the prior stance"),
    "AMBIGUOUS": ("🟨", "QA input is ambiguous"),
    "RERUN": ("🟦", "needs a model re-run"),
    "AWAITING": ("⬜", "awaiting QA input or a decision"),
}


def flag_chips(flags: dict) -> str:
    return " ".join(f"{FLAG_STYLE[k][0]} {k}" for k in FLAG_STYLE if k in flags)


def pick_report(label: str = "Task / report") -> int | None:
    from db.models import Report, Task
    from db.session import session_scope

    with session_scope() as s:
        reps = s.query(Report, Task).join(Task).order_by(Task.task_code, Report.uploaded_at).all()
        choices = {f"Task {t.task_code} · flagged at {r.flagged_version}": r.id for r, t in reps}
    if not choices:
        st.info("No rework report yet. Upload a task zip and its report on the **Upload** page.")
        return None
    keys = list(choices)
    pre = st.session_state.get("report_id")
    idx = next((i for i, k in enumerate(keys) if choices[k] == pre), 0)
    rid = choices[st.selectbox(label, keys, index=idx)]
    st.session_state["report_id"] = rid
    return rid


def decision_label(card, d: dict | None) -> str:
    if not d:
        return "–"
    k = d.get("option_key")
    names = {"keep": "Keep", "disagree": "Disagree", "agree": "Agree (apply fix)", "fix": "Apply fix",
             "new": "New option", "unclear": "Unclear"}
    lab = f"Option {k}" if k in ("1", "2", "3") else names.get(k, k or "–")
    if d.get("scope"):
        lab += f" ({', '.join(d['scope'])})"
    return lab
