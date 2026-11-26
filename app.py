"""Task Rework Dashboard: portfolio (one row per task)."""
import streamlit as st

from core import impact
from core.auth import require_auth
from db.models import Report, Task
from db.session import session_scope

st.set_page_config(page_title="Task Rework Dashboard", layout="wide")
user = require_auth()

st.title("Portfolio")
st.caption(f"Signed in as {user}")

with session_scope() as s:
    tasks = s.query(Task).order_by(Task.task_code).all()
    reports = {}
    for r in s.query(Report).order_by(Report.uploaded_at):
        reports[r.task_id] = r.id  # latest report per task
    task_rows = [(t.id, t.task_code, [v.label for v in t.versions]) for t in tasks]

if not task_rows:
    st.info("No tasks yet. Go to **Upload** in the sidebar to add a task zip and its rework report.")
    st.stop()

rows = []
for tid, code, versions in task_rows:
    row = {"Task": code, "Version": ", ".join(versions) or "–"}
    rid = reports.get(tid)
    if rid is None:
        row.update({"Cards": "no report"})
        rows.append(row)
        continue
    impact.prepare(rid, user)
    stt = impact.load_state(rid)
    fl = impact.compute_flags(stt)
    decs = impact.current_decisions(stt)
    total = len(stt.cards)
    decided = sum(1 for d in decs.values() if d)
    row["Cards (total / decided / awaiting)"] = f"{total} / {decided} / {fl['counts']['AWAITING']}"
    for k in ("BREAK", "CONFLICT", "REVERSAL", "AMBIGUOUS", "RERUN"):
        row[k] = fl["counts"][k]
    if stt.has_grades:
        row["Model %"] = f"{fl['baseline']['model']['score_pct']:.1%} → {fl['projection']['model']['score_pct']:.1%}"
        row["Gold %"] = f"{fl['projection']['gold']['score_pct']:.1%}"
    else:
        row["Model %"] = row["Gold %"] = "not graded"
    row["Verification"] = "–"
    rows.append(row)

st.dataframe(rows, hide_index=True, width="stretch")
st.page_link("pages/5_Task.py", label="Open a task →")
