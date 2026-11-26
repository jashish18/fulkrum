"""Task Rework Dashboard: home page."""
import streamlit as st

from core.auth import require_auth
from db.models import Task, Version
from db.session import session_scope

st.set_page_config(page_title="Task Rework Dashboard", layout="wide")
user = require_auth()

st.title("Task Rework Dashboard")
st.caption(f"Signed in as {user}")

with session_scope() as s:
    tasks = s.query(Task).order_by(Task.task_code).all()
    rows = [
        {"Task": t.task_code, "Versions": ", ".join(v.label for v in t.versions)}
        for t in tasks
    ]

if rows:
    st.subheader("Tasks loaded")
    st.dataframe(rows, hide_index=True, width="stretch")
else:
    st.info("No tasks yet. Go to **Upload** in the sidebar to add a task zip.")
