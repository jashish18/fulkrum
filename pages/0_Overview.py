"""Overview of every task (stage, open flags, parts) and every card."""
import streamlit as st

from core import overview
from core.auth import require_auth

st.set_page_config(page_title="Task Rework Dashboard", layout="wide")
user = require_auth()

st.title("Overview")
st.caption(f"Signed in as {user}")


@st.cache_data(show_spinner="Reading every task…")
def _build(_version):
    return overview.build()


tasks, cards = _build(overview.data_version())
if not tasks:
    st.info("No tasks yet. Go to **Upload** in the sidebar. The **Bulk upload** tab takes all your task zips "
            "and rework reports in one go.")
    st.stop()

# ---- headline numbers -----------------------------------------------------------
by_stage = {s: sum(1 for t in tasks if t.get("Stage") == s) for s in overview.STAGES}
cols = st.columns(len(overview.STAGES))
for col, s in zip(cols, overview.STAGES):
    col.metric(s.capitalize(), by_stage[s], help=overview.STAGE_HELP[s])
fcols = st.columns(len(overview.FLAGS) + 1)
fcols[0].metric("Tasks", len(tasks))
for col, f in zip(fcols[1:], overview.FLAGS):
    col.metric(f"Cards {f}", sum(t.get(f, 0) or 0 for t in tasks))

# ---- filters --------------------------------------------------------------------
with st.container(border=True):
    c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
    f_stage = c1.multiselect("Stage", overview.STAGES, placeholder="All stages")
    f_flags = c2.multiselect("Has open flag", overview.FLAGS, placeholder="Any")
    parts = sorted({c["Part"] for c in cards}, reverse=True)
    f_part = c3.multiselect("Part", parts, placeholder="All parts")
    f_task = c4.text_input("Task number")


def _task_ok(t):
    if f_stage and t.get("Stage") not in f_stage:
        return False
    if f_flags and not any((t.get(f) or 0) > 0 for f in f_flags):
        return False
    if f_task and f_task.strip() != t["Task"]:
        return False
    if f_part and not any(c["Task"] == t["Task"] and c["Part"] in f_part for c in cards):
        return False
    return True


shown = [t for t in tasks if _task_ok(t)]
st.subheader(f"Tasks ({len(shown)} of {len(tasks)})")
cols_order = ["Task", "Stage", "Flagged at", "Versions", "Cards", "Decided", "Final", *overview.FLAGS, "Parts",
              "Categories", "Model %", "Gold %", "Breaks", "Verification"]
st.dataframe([{k: t.get(k, "") for k in cols_order} for t in shown], hide_index=True, width="stretch",
             column_config={"Breaks": st.column_config.TextColumn(width="medium")})

report_of = {t["Task"]: t["report_id"] for t in shown if t.get("report_id")}


def _open_task():
    pick = st.session_state.get("ov_open")
    if pick:
        st.session_state["report_id"] = report_of[pick]
        st.session_state["ov_goto"] = True
        st.session_state["ov_open"] = ""


st.selectbox("Open a task", [""] + list(report_of), key="ov_open", on_change=_open_task,
             format_func=lambda x: x and f"Task {x}" or "Choose…")
if st.session_state.pop("ov_goto", False):
    st.switch_page("pages/5_Task.py")

# ---- every card ----------------------------------------------------------------
st.subheader("All cards")
c1, c2, c3 = st.columns(3)
rules = sorted({c["Rule"] for c in cards if c["Rule"]})
f_rule = c1.multiselect("Rule", rules, placeholder="All rules")
cats = sorted({c["Category"] for c in cards if c["Category"]})
f_cat = c2.multiselect("Category", cats, placeholder="All categories")
f_state = c3.selectbox("Decision", ["Any", "No final decision yet", "Final"])
task_set = {t["Task"] for t in shown}
rows = [c for c in cards if c["Task"] in task_set
        and (not f_part or c["Part"] in f_part)
        and (not f_flags or any(f in c["Flags"].split(", ") for f in f_flags))
        and (not f_rule or c["Rule"] in f_rule)
        and (not f_cat or c["Category"] in f_cat)
        and (f_state == "Any" or (f_state == "Final") == bool(c["Final"]))]
st.caption(f"{len(rows)} cards")
st.dataframe([{k: v for k, v in c.items() if k not in ("report_id",)} for c in rows], hide_index=True,
             width="stretch")
