"""Upload page: task zip -> role confirm screen -> saved version."""
import json
import re

import streamlit as st

from core.auth import require_auth
from core.ingest import ROLES, Staged, StagedFile, rubric_summary, save_version, stage_zip

st.set_page_config(page_title="Upload", layout="wide")
user = require_auth()
st.title("Upload")

tab_bulk, tab_zip, tab_report, tab_qa = st.tabs(["Bulk upload", "Task zip", "Rework report", "Prior QA sheet"])

with tab_bulk:
    from core import bulk
    from core import llm as _llm

    st.caption("Drop all your task zips, rework reports and QA sheets here at once, or one zip that holds them "
               "all. Files are matched by the task number in their names (e.g. 35-sampling….zip and "
               "batch_2_rework_checks_35 - by part.xlsx). Each task's version label is taken from its report's "
               "\"flagged at\" line.")
    ups = st.file_uploader("Task zips, rework reports, QA sheets", type=["zip", "xlsx"],
                           accept_multiple_files=True, key="bulk_upl")
    sig = tuple((u.name, u.size) for u in ups or [])
    if ups and st.session_state.get("bulk_sig") != sig:
        if st.button("Check files", type="primary"):
            try:
                with st.spinner("Opening every file…"):
                    plans, unmatched = bulk.group([(u.name, u.getvalue()) for u in ups])
                    prog = st.progress(0.0)
                    for i, p_ in enumerate(plans.values(), 1):
                        prog.progress((i - 1) / max(len(plans), 1), text=f"Checking task {p_.task}…")
                        bulk.check(p_)
                    prog.progress(1.0, text="Done")
                st.session_state["bulk_plans"], st.session_state["bulk_unmatched"] = plans, unmatched
                st.session_state["bulk_sig"] = sig
                st.rerun()
            except Exception as e:  # noqa: BLE001 - never show a traceback
                st.error(f"Couldn't open the files: {e}. Click Check files again; if it keeps failing, "
                         "send a screenshot of this message.")
    plans = st.session_state.get("bulk_plans") if ups and st.session_state.get("bulk_sig") == sig else None
    if plans is not None:
        for n in st.session_state.get("bulk_unmatched") or []:
            st.warning(f"{n}: no task number found in the name, so it was skipped.")
        st.dataframe([{"Task": p_.task, "Status": p_.status, "Version label": p_.label or "",
                       "From": p_.label_from, "Zip": p_.zip_name or "—", "Report": p_.report_name or "—",
                       "QA sheet": p_.qa_name or ("inside zip" if p_.staged and any(
                           f.role == "qa_sheet" for f in p_.staged.files) else "—"),
                       "Notes": "; ".join(p_.problems + p_.unsure_roles + p_.notes)}
                      for p_ in plans.values()], hide_index=True, width="stretch")

        for p_ in plans.values():
            if p_.status not in ("needs label", "check roles"):
                continue
            with st.expander(f"Task {p_.task}: {p_.status}", expanded=True):
                if p_.status == "needs label" or not p_.label:
                    p_.label = st.text_input("Version label (required)", value=p_.label or "",
                                             placeholder="e.g. v20", key=f"bl_{p_.task}") or None
                if p_.staged and p_.unsure_roles:
                    st.write("Check the roles: " + "; ".join(p_.unsure_roles))
                    ed = st.data_editor(
                        [{"File": f.name, "Role": f.role, "Why": f.reason} for f in p_.staged.files],
                        column_config={"Role": st.column_config.SelectboxColumn("Role", options=ROLES, required=True),
                                       "File": st.column_config.TextColumn(disabled=True),
                                       "Why": st.column_config.TextColumn(disabled=True)},
                        hide_index=True, width="stretch", key=f"br_{p_.task}")
                    for f, r_ in zip(p_.staged.files, ed):
                        f.role = r_["Role"]
                    counts = {r: sum(1 for f in p_.staged.files if f.role == r) for r in bulk.REQUIRED_ROLES}
                    still = [f"no {r} file" for r, n in counts.items() if n == 0]
                    if still:
                        st.warning("Still missing: " + ", ".join(still) + ".")
                if p_.label and st.checkbox("These roles and the label are right", key=f"bok_{p_.task}"):
                    p_.status = "ready"
                    p_.unsure_roles = []

        ready = [p_ for p_ in plans.values() if p_.status == "ready"]
        use_llm = st.checkbox("Let the AI fill in report rows the rules couldn't read", value=False,
                              disabled=not _llm.is_configured(),
                              help="Off by default for speed. Rows the rules read fully don't need it.")
        replace = st.checkbox("Replace reports that are already saved (this clears their decisions)", value=False)
        if st.button(f"Save {len(ready)} ready task(s)", type="primary", disabled=not ready):
            prog = st.progress(0.0)
            for i, p_ in enumerate(ready, 1):
                prog.progress((i - 1) / len(ready), text=f"Saving task {p_.task}…")
                try:
                    bulk.save(p_, user, use_llm=use_llm, replace_reports=replace)
                except Exception as e:  # noqa: BLE001 - one bad task mustn't stop the rest
                    p_.status = "error"
                    p_.problems.append(f"save: {e}")
            prog.progress(1.0, text="Done")
            saved = [p_.task for p_ in ready if p_.status == "saved"]
            if saved:
                st.success(f"Saved {len(saved)} task(s): {', '.join(saved)}. The Overview page shows them all.")
            for p_ in ready:
                if p_.status == "error":
                    st.error(f"Task {p_.task}: {'; '.join(p_.problems)}")

with tab_report:
    rup = st.file_uploader("Rework report (batch_2_rework_checks_<task> - by part.xlsx)", type=["xlsx"],
                           key="report_upl")
    if rup is not None:
        from core.report_parser import ReportError, parse_report, save_report
        from db.models import File, Task, Version
        from db.session import session_scope

        try:
            # File names from the task's latest version let the parser spot file references.
            names: list[str] = []
            m = re.search(r"rework_checks_0*(\d+)", rup.name)
            with session_scope() as s:
                if m:
                    task = s.query(Task).filter_by(task_code=m.group(1)).one_or_none()
                    if task and task.versions:
                        v = max(task.versions, key=lambda v: v.uploaded_at)
                        names = [f.name for f in s.query(File).filter_by(version_id=v.id)]
            rep = parse_report(rup.getvalue(), rup.name, names)
        except ReportError as e:
            st.error(str(e))
            rep = None
        except Exception as e:  # noqa: BLE001 - never crash the page
            st.error(f"Couldn't read {rup.name}: {e}")
            rep = None
        if rep is not None:
            c = rep.counts()
            st.success(f"Task {rep.task}, flagged at {rep.flagged_version}: {c['rows']} rows in "
                       f"{c['groups']} cards. The counts match the summary line.")
            st.dataframe(
                [{"Part": p, "Cards": v["groups"], "Rows": v["rows"]} for p, v in c["parts"].items()],
                hide_index=True,
            )
            for w in rep.warnings:
                st.warning(w)
            if st.button("Save report", type="primary", key="save_report"):
                try:
                    with st.spinner("Saving…"):
                        new_rid = save_report(rep, data=rup.getvalue())
                        from core import impact
                        impact.prepare(new_rid, user)
                    st.success("Report saved. Open **Cards** in the sidebar to see it.")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Saving failed: {e}")

with tab_qa:
    st.caption("Optional. A QA sheet inside the task zip is loaded automatically.")
    qup = st.file_uploader("Prior QA sheet (qa_feedback_<task>_grouped.xlsx)", type=["xlsx"], key="qa_upl")
    if qup is not None:
        from core.report_parser import ReportError, parse_qa_sheet, save_qa_items

        m = re.search(r"qa_feedback_0*(\d+)", qup.name)
        qtask = st.text_input("Task number", value=m.group(1) if m else "", key="qa_task")
        try:
            items = parse_qa_sheet(qup.getvalue(), qup.name)
            st.write(f"{len(items)} QA items found.")
            if st.button("Save QA sheet", disabled=not qtask):
                save_qa_items(qtask.strip(), items, qup.name)
                st.success("Saved.")
        except ReportError as e:
            st.error(str(e))

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
        except OSError as e:
            st.session_state.pop("staged", None)
            st.error(f"Couldn't unpack {up.name} ({e.strerror or e}). Try uploading it again.")

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
        label = col2.text_input("Version label (required)", value=staged.suggested_label or "",
                                placeholder="e.g. v20, the version the rework report was flagged at",
                                help="Prefilled from a v<number> in the zip name. Use a new label for a revised version, even if the zip is the same.")

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

        missing = [n for n, v in (("a task number", task_code), ("a version label", label)) if not v.strip()]
        if missing:
            st.info(f"Type {' and '.join(missing)} above to enable **Confirm and save**. Use the version the "
                    "rework report was flagged at (e.g. v20) for the baseline, and a new label for a revised "
                    "version. Press Enter after typing so the app picks it up.")
        if st.button("Confirm and save", type="primary", disabled=bool(missing)):
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
