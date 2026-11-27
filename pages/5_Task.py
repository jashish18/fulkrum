"""Task page (spec 10): header with prompt, failure notes, scores and re-run badge, then tabs per artifact."""
import json

import streamlit as st

from core import impact, llm, settings, verify
from core import ops as opsmod
from core.auth import require_auth
from core.ingest import load_text
from core.ui import decision_label, flag_chips
from db.models import AuditLog, Card, File
from db.session import session_scope

st.set_page_config(page_title="Task", layout="wide")
user = require_auth()
st.title("Task")

def _pick_task():
    """Every task: with a rework report it opens the full page; with only a zip, a version view."""
    from db.models import Report, Task, Version

    with session_scope() as s:
        latest = {}
        for r in s.query(Report).order_by(Report.uploaded_at, Report.id):
            latest[r.task_id] = (r.id, r.flagged_version)
        opts = []
        for t in sorted(s.query(Task), key=lambda t: (len(t.task_code), t.task_code)):
            vs = sorted(t.versions, key=lambda v: v.uploaded_at)
            if t.id in latest:
                rid_, fv = latest[t.id]
                opts.append((f"Task {t.task_code} · flagged at {fv}", ("report", rid_)))
            elif vs:
                opts.append((f"Task {t.task_code} · {vs[-1].label} (no rework report yet)", ("version", vs[-1].id)))
    if not opts:
        st.info("Nothing uploaded yet. Add task zips (and their rework reports) on the **Upload** page.")
        st.stop()
    labels = [o[0] for o in opts]
    if st.session_state.get("task_pick") not in labels:  # first visit, or sent here from another page
        want = (("version", st.session_state["task_version_id"]) if st.session_state.get("task_version_id")
                else ("report", st.session_state.get("report_id")))
        st.session_state["task_pick"] = next((lab for lab, v in opts if v == want), labels[0])
    label = st.selectbox("Task", labels, key="task_pick")
    kind, ident = dict(opts)[label]
    if kind == "report":
        st.session_state["report_id"] = ident
        st.session_state.pop("task_version_id", None)
    else:
        st.session_state["task_version_id"] = ident
    return kind, ident


def _version_view(vid: int) -> None:
    """A task with only its zip: prompt, failure notes, rubric, files, grades and integrity checks."""
    from core import grading
    from db.models import Criterion, FailureNote, Version

    with session_scope() as s:
        v = s.get(Version, vid)
        label = v.label
        files = [(f.id, f.name, f.role, f.text_path) for f in s.query(File).filter_by(version_id=vid).order_by(File.name)]
        crits = [dict(Id=c.crit_id, Section=c.subtask, Points=c.points, Category=c.category or "", Criterion=c.text)
                 for c in s.query(Criterion).filter_by(version_id=vid).order_by(Criterion.id)]
        notes = [(n.idx, n.text) for n in s.query(FailureNote).filter_by(version_id=vid).order_by(FailureNote.idx)]
    st.info(f"Version {label}. Cards, decisions, flags and verification appear here once this task's rework "
            "report is uploaded (Upload → Rework report, or Bulk upload).")
    prompt = next((load_text(tp) for _, _, r, tp in files if r == "prompt"), "")
    with st.expander("Prompt", expanded=True):
        st.text(prompt or "(no prompt file)")

    c1, c2 = st.columns(2)
    for col, role, name in ((c1, "model_output", "Model"), (c2, "gold", "Gold")):
        fid = next((i for i, _, r, _ in files if r == role), None)
        if fid is None:
            col.metric(name, "no file")
            continue
        lines = grading.review_lines(vid, fid)["lines"]
        if any(ln["final"] or ln["suggested"] for ln in lines):
            sc = grading.file_score(vid, fid)
            col.metric(f"{name} score", f"{sc['score_pct']:.1%}")
        else:
            col.metric(f"{name} score", "not graded")
    st.page_link("pages/4_Grades.py", label="Grade the gold and model output →")

    st.subheader("Failure notes")
    for idx, text in notes:
        st.markdown(f"**Note {idx}**")
        st.caption(text[:600])
    if not notes:
        st.write("No failure notes found.")

    t_rub, t_files, t_int = st.tabs(["Rubric", "Files", "Integrity checks"])
    with t_rub:
        if crits:
            st.dataframe(crits, hide_index=True, width="stretch")
        else:
            st.write("No rubric criteria found.")
    with t_files:
        st.dataframe([{"File": n, "Role": r} for _, n, r, _ in files], hide_index=True, width="stretch")
        st.page_link("pages/2_Versions.py", label="Change roles or see extraction details →")
    with t_int:
        try:
            st.dataframe([{"": {True: "✅", False: "❌", None: "–"}[c["ok"]], "Check": c["check"],
                           "Detail": c["detail"]} for c in verify.integrity(vid)], hide_index=True, width="stretch")
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't run the integrity checks: {e}")


kind, ident = _pick_task()
if kind == "version":
    _version_view(ident)
    st.stop()
rid = ident
impact.prepare(rid, user)
state = impact.load_state(rid)
fl = impact.compute_flags(state)
decs = impact.current_decisions(state)
base, proj = fl["baseline"], fl["projection"]
mt, gt = settings.get_float("MODEL_THRESHOLD"), settings.get_float("GOLD_THRESHOLD")

st.caption(f"Task {state.task_code} · report flagged at {state.flagged_version} · baseline version "
           f"{state.version_label or '(none uploaded)'}")
if state.version_id is None:
    st.error("Upload the task zip for this version first.")
    st.stop()

with st.expander("Prompt"):
    st.text(state.prompt_text or "(no prompt file)")

# ---- scores ---------------------------------------------------------------------
c1, c2, c3 = st.columns(3)
if state.has_grades:
    c1.metric(f"Model (breaks at ≥ {mt:.0%})", f"{base['model']['score_pct']:.1%} → {proj['model']['score_pct']:.1%}",
              "BREAK" if proj["model"]["score_pct"] >= mt else "OK",
              delta_color="inverse" if proj["model"]["score_pct"] >= mt else "normal")
    c2.metric(f"Gold (breaks below {gt:.0%})", f"{base['gold']['score_pct']:.1%} → {proj['gold']['score_pct']:.1%}",
              "BREAK" if proj["gold"]["score_pct"] < gt else "OK",
              delta_color="inverse" if proj["gold"]["score_pct"] < gt else "normal")
    c1.caption(f"Earned {proj['model']['earned']:g} / {proj['model']['positive_total']:g}")
    if not (state.baseline_locked["model"] and state.baseline_locked["gold"]):
        st.caption("Baseline grades aren't locked yet; projections use the grader's agreed results.")
else:
    c1.warning("No grades yet: grade the gold and model output on the Grades page.")
rr = fl["rerun"]
if rr:
    c3.markdown("🟦 **Re-run required**")
    for r in rr:
        c3.caption(f"Card {r['card']}: {r['label'][:90]}" + (" (metadata only)" if r["metadata_only"] else ""))
else:
    c3.markdown("No re-run needed")
for b in fl["task_breaks"]:
    st.error(f"BREAK: {b}")
for n in proj["notes"]:
    st.info(n)

# ---- write-back (spec 11) -------------------------------------------------------
with st.expander("Download decisions workbook"):
    n_final = sum(1 for c in state.cards.values() if (c.decisions.get("final") or {}).get("option_key"))
    st.caption(f"A copy of the uploaded report with the {n_final} final decision(s) written into the "
               "Expert call and Expert note columns, plus a Dashboard summary sheet. Working calls "
               "that aren't ticked Final are not written. The uploaded original is never changed.")
    if st.button("Build workbook", key="wb_build"):
        from core import writeback

        try:
            data, fname, info = writeback.build_decisions_workbook(rid)
            st.session_state["wb_out"] = (rid, data, fname, info)
        except FileNotFoundError as e:
            st.error(str(e))
    out = st.session_state.get("wb_out")
    if out and out[0] == rid:
        _, data, fname, info = out
        st.download_button(f"Download {fname}", data, file_name=fname,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.write(f"{len(info['written'])} cell(s) written.")
        for s_ in info["skipped"]:
            st.warning(s_)

# ---- failure notes --------------------------------------------------------------
st.subheader("Failure notes")
for idx, text in sorted(state.failure_notes.items()):
    mapped = state.failure_map.get(idx, [])
    b0, b1 = base["backing"].get(idx, 0), proj["backing"].get(idx, 0)
    status = impact.note_status(b0, b1) if mapped else "not mapped yet"
    icon = {"holds": "🟩", "weakened": "🟨", "gone": "🟥"}.get(status, "⬜")
    st.markdown(f"{icon} **Note {idx}** · backing {b0:g} → {b1:g} pts · {status}")
    st.caption(text[:400])

tabs = st.tabs(["Cards", "Prompt & References", "Rubric", "Trajectory", "Gold", "Failure notes & Model run",
                "No change (keep/defend)", "Grades", "Verification", "History"])


try:
    verified = verify.verified_cards(rid)
except Exception:  # noqa: BLE001 - a status badge must never break the page
    verified = set()


def _status(code: str, decided: bool) -> str:
    if not decided:
        return "possible"
    return "verified" if code in verified else "decided"


def changes_for(artifacts: set[str]):
    rows = []
    for code, card in state.cards.items():
        d = decs[code]
        dk = impact.resolve_key(card, d.get("option_key")) if d else None
        for k, lst in card.ops.items():
            for o in lst:
                if opsmod.ARTIFACT_OF_OP.get(o.get("type")) in artifacts:
                    rows.append({"Card": code, "Option": k, "Change": opsmod.describe(o),
                                 "Status": _status(code, k == dk)})
        for i in card.impacts:
            if i["artifact"] in artifacts and i.get("option_scope"):
                if not any(r["Card"] == code and r["Change"] == i["text"] for r in rows):
                    keys = i["option_scope"].split(",")
                    rows.append({"Card": code, "Option": i["option_scope"], "Change": i["text"],
                                 "Status": _status(code, dk in keys)})
    rows.sort(key=lambda r: (r["Status"] == "possible", r["Card"]))
    return rows


with tabs[0]:
    data = []
    for code, card in state.cards.items():
        contrib = impact.card_contribution(state, code, proj) if decs[code] and state.has_grades else None
        data.append({"Card": code, "Part": card.part, "Decision": decision_label(card, decs[code]),
                     "Model pts": f"{contrib['model_points']:+g}" if contrib else "",
                     "Flags": flag_chips(fl["cards"][code]), "Theme": card.theme or ""})
    st.dataframe(data, hide_index=True, width="stretch")
    st.page_link("pages/3_Cards.py", label="Open a card →")

for tab, arts in ((tabs[1], {"prompt", "reference"}), (tabs[2], {"rubric"}), (tabs[3], {"trajectory"}),
                  (tabs[4], {"gold"}), (tabs[5], {"failure_notes", "model_run"})):
    with tab:
        rows = changes_for(arts)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.write("No changes listed.")

with tabs[5]:
    st.markdown("#### Failure-note map")
    st.caption("Which failing criteria back each failure note. The AI can propose it; you confirm.")
    failing = [c for c in state.crit_order if state.model_results.get(c) in ("fail", "triggered")]
    with st.form("fmap"):
        new_map = {}
        for idx in sorted(state.failure_notes):
            new_map[idx] = st.multiselect(f"Note {idx}", options=sorted(set(failing) | set(state.criteria)),
                                          default=state.failure_map.get(idx, []), key=f"fm{idx}")
        if st.form_submit_button("Save map"):
            impact.set_failure_map(state.version_id, new_map, user)
            st.rerun()
    if llm.is_configured() and failing and st.button("Propose map with AI"):
        try:
            prop = impact.suggest_failure_map(state)
            impact.set_failure_map(state.version_id, prop, user, confirmed=False)
            st.success("Proposed. Check it above and save to confirm.")
            st.rerun()
        except llm.LLMError as e:
            st.error(str(e))

with tabs[6]:
    rows = [{"Card": c, "Decision": decision_label(card, decs[c]), "Prior stance":
             "; ".join(f"QA #{s['qa_no']} {s['status']}" for s in card.stances)}
            for c, card in state.cards.items() if decs[c] and impact.is_keep(card, decs[c].get("option_key"))]
    st.dataframe(rows, hide_index=True, width="stretch") if rows else st.write("No keep or defend decisions yet.")

with tabs[7]:
    st.page_link("pages/4_Grades.py", label="Open the grades review →")
    if state.has_grades:
        st.write(f"Model fails: {', '.join(base['model']['failed']) or 'none'}")
        st.write(f"Gold fails: {', '.join(base['gold']['failed']) or 'none'}")

with tabs[8]:
    from db.models import Version

    with session_scope() as s:
        all_versions = [(v.id, v.label) for v in
                        s.query(Version).filter_by(task_id=state.task_id).order_by(Version.uploaded_at)]
    labels = {vid_: lab for vid_, lab in all_versions}
    st.markdown(f"**Baseline:** {state.version_label}. The report's flagged version is "
                f"{state.flagged_version or 'not stated'}.")
    with st.expander("Use another baseline"):
        b_pick = st.selectbox("Baseline version", [v for v, _ in all_versions],
                              index=[v for v, _ in all_versions].index(state.version_id),
                              format_func=lambda v: labels[v], key="baseline_pick")
        if b_pick != state.version_id and st.button("Set as baseline"):
            settings.put(impact.baseline_key(rid), str(b_pick), user)
            st.rerun()
    others = [v for v, _ in all_versions if v != state.version_id]
    if not others:
        st.info("Upload the revised task zip on the Upload page (as a new version label) to verify it here.")
    else:
        new_vid = st.selectbox("Revised version", others, index=len(others) - 1, format_func=lambda v: labels[v],
                               key="verify_new")
        use_ai = st.checkbox("Use the AI for changes a rule can't check", value=llm.is_configured(),
                             disabled=not llm.is_configured(), key="verify_ai")
        if st.button("Run verification", type="primary"):
            try:
                with st.spinner("Comparing the versions…"):
                    verify.run(rid, new_vid, use_llm=use_ai, user=user)
            except Exception as e:  # noqa: BLE001
                st.error(f"Verification failed: {e}")
        res = verify.load(rid, new_vid)
        if not res["expected"] and not res["untracked"]:
            st.write("Not run yet for this version." if not any(
                c.decisions.get("final") for c in state.cards.values())
                else "Run the verification to see the results.")
        else:
            cnt = res["counts"]
            st.markdown(" · ".join(f"**{k}** {v}" for k, v in cnt.items()) +
                        f" · **untracked** {len(res['untracked'])}")
            rr = res["rerun"]
            if rr["required"]:
                msg = f"Re-run required (cards {', '.join(rr['cards'])}): {rr['status']}."
                if "model" in rr:
                    msg += f" New model score {rr['model']['score_pct']:.1%}" + \
                        (" — BREAK." if rr["model_breaks"] else ".")
                    gone = [str(i) for i, b in rr.get("failure_notes", {}).items() if not b]
                    if gone:
                        msg += f" Failure note(s) {', '.join(gone)} have no failing criterion left."
                elif rr["status"] == "new output uploaded":
                    msg += " Grade the new model output on the Grades page to re-check the thresholds."
                (st.warning if rr["status"] != "not required" else st.info)(msg)

            st.markdown("#### Expected changes")
            st.caption("Change a result to override it. Overrides are kept when you run the check again.")
            table = [{"Id": e["verification_id"], "Card": e["card"], "Artifact": e["artifact"],
                      "Expected change": e["description"], "Result": e["result"],
                      "Automatic": e["auto"] or "", "Override by": e["override_by"] or "",
                      "Evidence": e["evidence"]} for e in res["expected"]]
            edited = st.data_editor(
                table, hide_index=True, width="stretch", key=f"ver_{rid}_{new_vid}",
                column_config={"Result": st.column_config.SelectboxColumn("Result", options=verify.RESULTS,
                                                                          required=True),
                               "Id": None},
                disabled=["Card", "Artifact", "Expected change", "Automatic", "Override by", "Evidence"])
            changed = [(r_["Id"], r_["Result"]) for r_, o in zip(edited, table) if r_["Result"] != o["Result"]]
            if changed and st.button(f"Save {len(changed)} override(s)"):
                for vid_, r_ in changed:
                    verify.override(vid_, r_, user)
                st.rerun()

            st.markdown("#### Changes no card explains")
            if not res["untracked"]:
                st.write("None.")
            else:
                ut = [{"Id": u["id"], "Artifact": u["artifact"], "Change": u["desc"], "Where": u["loc"] or "",
                       "Status": u["status"]} for u in res["untracked"]]
                ued = st.data_editor(
                    ut, hide_index=True, width="stretch", key=f"unt_{rid}_{new_vid}",
                    column_config={"Status": st.column_config.SelectboxColumn(
                        "Status", options=verify.UNTRACKED_STATUSES), "Id": None},
                    disabled=["Artifact", "Change", "Where"])
                uchg = [(r_["Id"], r_["Status"] or "") for r_, o in zip(ued, ut) if (r_["Status"] or "") != o["Status"]]
                if uchg and st.button(f"Save {len(uchg)} status change(s)"):
                    for uid, stt_ in uchg:
                        verify.set_untracked_status(uid, stt_, user)
                    st.rerun()

            st.markdown("#### Integrity checks")
            for vid_ in (state.version_id, new_vid):
                st.caption(f"Version {labels[vid_]}")
                st.dataframe([{"": {True: "✅", False: "❌", None: "–"}[c["ok"]], "Check": c["check"],
                               "Detail": c["detail"]} for c in verify.integrity(vid_)],
                             hide_index=True, width="stretch")
            data_, fname_ = verify.build_workbook(rid, new_vid)
            st.download_button(f"Download {fname_}", data_, file_name=fname_,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tabs[9]:
    with session_scope() as s:
        card_ids = [str(c.id) for c in s.query(Card).filter_by(report_id=rid)]
        logs = (s.query(AuditLog).filter(AuditLog.entity_id.in_(card_ids + [str(state.version_id)]))
                .order_by(AuditLog.at.desc()).limit(200).all())
        st.dataframe([{"When": l.at, "Who": l.user, "What": l.entity, "Id": l.entity_id,
                       "Before": l.before_json, "After": l.after_json} for l in logs],
                     hide_index=True, width="stretch")
