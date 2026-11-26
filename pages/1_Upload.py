"""Upload page: task zip -> role confirm screen -> saved version."""
import json

import streamlit as st

from core.auth import require_auth
from core.ingest import ROLES, Staged, StagedFile, rubric_summary, save_version, stage_zip

st.set_page_config(page_title="Upload", layout="wide")
user = require_auth()
st.title("Upload")

tab_zip, tab_report, tab_qa = st.tabs(["Task zip", "Rework report", "Prior QA sheet"])

with tab_report:
    st.info("Rework report import arrives in Phase 2.")
with tab_qa:
    st.info("Prior QA sheet import arrives in Phase 2.")

with tab_zip:
    up = st.file_uploader("Task zip", type=["zip"], key="zip_upl")
    if up is not None and st.session_state.get("staged_name") != (up.name, up.size):
        try:
            with st.spinner("Extracting…"):
                staged = stage_zip(up.getvalue(), up.name)
            st.session_state["staged"] = staged
            st.session_state["staged_name"] = (up.name, up.size)
        except ValueError as e:
            st.session_state.pop("staged", None)
            st.error(str(e))

    staged: Staged | None = st.session_state.get("staged")
    if staged is not None:
        st.subheader(f"Confirm files in {staged.zip_name}")
        for w in staged.warnings:
            st.warning(w)
        if staged.rubric_error:
            st.error(staged.rubric_error)
        elif staged.rubric:
            rs = rubric_summary(staged.rubric)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Criteria", rs["criteria"])
            c2.metric("Penalties", rs["penalties"])
            c3.metric("total_points", rs["total_points"])
            c4.metric("Sum of section weights", rs["sum_section_weights"])
            if not rs["consistent"]:
                st.warning(
                    f"Rubric totals don't line up: total_points {rs['total_points']}, section weights "
                    f"{rs['sum_section_weights']}, net criteria points {rs['net_criteria_points']}."
                )

        col1, col2 = st.columns(2)
        task_code = col1.text_input("Task number", value=staged.suggested_task or "")
        label = col2.text_input("Version label", value=staged.suggested_label or "",
                                help="Prefilled from a v<number> in the zip name.")

        table = [
            {"File": f.name, "Role": f.role, "Suggested": f.suggested_role, "Why": f.reason}
            for f in staged.files
        ]
        edited = st.data_editor(
            table,
            column_config={
                "Role": st.column_config.SelectboxColumn("Role", options=ROLES, required=True),
                "File": st.column_config.TextColumn(disabled=True),
                "Suggested": st.column_config.TextColumn(disabled=True),
                "Why": st.column_config.TextColumn(disabled=True),
            },
            hide_index=True,
            width="stretch",
            key=f"roles_{staged.zip_sha256}",
        )
        roles = {r["File"]: r["Role"] for r in edited}
        counts = {r: sum(1 for v in roles.values() if v == r) for r in ROLES}
        problems = [f"no {r} file" for r in ("prompt", "rubric", "gold", "model_output") if counts[r] == 0]
        if problems:
            st.warning("Check the roles: " + ", ".join(problems) + ".")

        if st.button("Confirm and save", type="primary", disabled=not (task_code and label)):
            try:
                with st.spinner("Extracting file contents…"):
                    vid = save_version(staged, task_code.strip(), label.strip(), roles)
                st.success(f"Saved task {task_code} {label}.")
                st.session_state.pop("staged", None)
                st.session_state["last_version_id"] = vid
            except ValueError as e:
                st.error(str(e))
            except Exception as e:  # noqa: BLE001 - never crash the page
                st.error(f"Saving failed: {e}")

st.divider()
st.page_link("pages/2_Versions.py", label="See saved versions →")
