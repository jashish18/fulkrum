"""Saved versions: files, roles, rubric totals, metadata names and extraction warnings."""
import json

import streamlit as st

from core.auth import require_auth
from core.ingest import ROLES, load_extraction
from db.models import Criterion, FailureNote, File, Task, Version
from db.session import audit, session_scope

st.set_page_config(page_title="Versions", layout="wide")
user = require_auth()
st.title("Saved versions")

with session_scope() as s:
    versions = (
        s.query(Version, Task).join(Task).order_by(Task.task_code, Version.uploaded_at).all()
    )
    choices = {f"Task {t.task_code} · {v.label}": v.id for v, t in versions}

if not choices:
    st.info("Nothing saved yet.")
    st.stop()

default = st.session_state.get("last_version_id")
keys = list(choices)
idx = next((i for i, k in enumerate(keys) if choices[k] == default), 0)
pick = st.selectbox("Version", keys, index=idx)
vid = choices[pick]

with session_scope() as s:
    files = s.query(File).filter_by(version_id=vid).order_by(File.name).all()
    crits = s.query(Criterion).filter_by(version_id=vid).all()
    notes = s.query(FailureNote).filter_by(version_id=vid).order_by(FailureNote.idx).all()

    positive = sum(c.points for c in crits if c.points > 0)
    penalties = [c for c in crits if c.points < 0 or (c.category or "").lower() == "penalty"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Criteria", len(crits))
    c2.metric("Penalties", len(penalties))
    c3.metric("Net points", f"{sum(c.points for c in crits):g}")
    c4.metric("Positive points", f"{positive:g}")

    tab_files, tab_meta, tab_crit, tab_notes = st.tabs(["Files", "Metadata", "Criteria", "Failure notes"])

    with tab_files:
        table = [{"id": f.id, "File": f.name, "Role": f.role} for f in files]
        edited = st.data_editor(
            table,
            column_config={"id": None,
                           "Role": st.column_config.SelectboxColumn("Role", options=ROLES, required=True),
                           "File": st.column_config.TextColumn(disabled=True)},
            hide_index=True, width="stretch", key=f"vroles_{vid}",
        )
        if st.button("Save role changes"):
            by_id = {f.id: f for f in files}
            changed = 0
            for r in edited:
                f = by_id[r["id"]]
                if f.role != r["Role"]:
                    audit(s, user, "files.role", f.id, f.role, r["Role"])
                    f.role = r["Role"]
                    changed += 1
            if changed:
                from core.ingest import _refresh_failure_notes
                _refresh_failure_notes(s, s.get(Version, vid))
            st.success(f"{changed} role(s) updated.")

        for f in files:
            ex = load_extraction(f.text_path).get("extraction", {})
            msgs = []
            if ex.get("error"):
                st.error(f"{f.name}: {ex['error']}")
            for w in ex.get("warnings", []):
                msgs.append(w)
            if ex.get("hidden_sheets"):
                msgs.append("hidden sheets: " + ", ".join(ex["hidden_sheets"]))
            if ex.get("probable_hashes"):
                msgs.append("probable #### (too wide for column): " + ", ".join(ex["probable_hashes"][:20]))
            parts = json.loads(f.parts_json or "{}")
            if parts.get("webextensions"):
                msgs.append("has xl/webextensions parts: " + ", ".join(parts["webextensions"]))
            if msgs:
                st.warning(f"**{f.name}** — " + "; ".join(msgs))

    with tab_meta:
        rows = []
        for f in files:
            meta = json.loads(f.metadata_json or "{}")
            names = meta.pop("personal_names", [])
            for n in names:
                rows.append({"File": f.name, "Field": n["field"], "Name found": n["value"]})
        if rows:
            st.error(f"{len(rows)} personal name(s) found in file properties")
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.success("No personal names found in file properties.")
        with st.expander("All file properties"):
            for f in files:
                meta = json.loads(f.metadata_json or "{}")
                meta.pop("personal_names", None)
                if meta:
                    st.write(f"**{f.name}**", meta)

    with tab_crit:
        st.dataframe(
            [{"id": c.crit_id, "Subtask": c.subtask, "Points": c.points, "Category": c.category,
              "Criterion": c.text} for c in crits],
            hide_index=True, width="stretch",
        )

    with tab_notes:
        if not notes:
            st.info("No failure notes found. Check that a file has the failure_notes role.")
        for n in notes:
            st.markdown(f"**Note {n.idx}**")
            st.text(n.text)

st.subheader("Integrity checks")
try:
    from core import verify

    st.dataframe([{"": {True: "✅", False: "❌", None: "–"}[c["ok"]], "Check": c["check"], "Detail": c["detail"]}
                  for c in verify.integrity(vid)], hide_index=True, width="stretch")
except Exception as e:  # noqa: BLE001 - never crash the page
    st.error(f"Couldn't run the integrity checks: {e}")
