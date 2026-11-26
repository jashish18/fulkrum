"""Grades review: run the grader on a version's gold and model output, then confirm or override each line."""
import streamlit as st

from core import config, grading, llm, settings
from core.auth import require_auth
from db.models import BaselineLock, File, Task, Version
from db.session import session_scope

st.set_page_config(page_title="Grades", layout="wide")
user = require_auth()
st.title("Grades review")

with session_scope() as s:
    versions = s.query(Version, Task).join(Task).order_by(Task.task_code, Version.uploaded_at).all()
    vchoices = {f"Task {t.task_code} · {v.label}": v.id for v, t in versions}
if not vchoices:
    st.info("Upload a task zip first.")
    st.stop()
vid = vchoices[st.selectbox("Version", list(vchoices))]

with session_scope() as s:
    files = s.query(File).filter(File.version_id == vid, File.role.in_(["gold", "model_output"])).all()
    fchoices = {f"{'Gold' if f.role == 'gold' else 'Model output'}: {f.name}": f.id for f in files}
if not fchoices:
    st.warning("This version has no gold or model output file. Fix the roles on the Versions page.")
    st.stop()
fid = fchoices[st.radio("File", list(fchoices), horizontal=True)]

data = grading.review_lines(vid, fid)
model = settings.grader_model()

if not data["lines"]:
    if not llm.is_configured():
        st.warning("The grader isn't set up yet: add your OpenRouter key to `.env` and pick a model on "
                   "the **Settings** page.")
    st.write(f"Not graded yet. Grader: `{model or 'none chosen'}` · ZDR {'on' if settings.zdr() else 'off'}.")
    if st.button("Run grader (2 passes)", type="primary", disabled=not llm.is_configured()):
        box = st.empty()
        try:
            grading.grade_file(vid, fid, progress=lambda m: box.write(m))
            st.rerun()
        except (llm.LLMError, ValueError) as e:
            st.error(f"Grading failed: {e}")
    st.stop()

sc = grading.file_score(vid, fid)
threshold = settings.get_float("GOLD_THRESHOLD" if data["is_gold"] else "MODEL_THRESHOLD")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Score", f"{sc['score_pct']:.1%}")
c2.metric("Earned / positive total", f"{sc['earned']:g} / {sc['positive_total']:g} ({sc['earned_pct']:.1%})")
c3.metric("Penalties triggered", f"{sc['penalty']:g} pts")
if data["is_gold"]:
    c4.metric("Gold threshold", f"{threshold:.0%}", "OK" if sc["score_pct"] >= threshold else "BELOW",
              delta_color="normal" if sc["score_pct"] >= threshold else "inverse")
else:
    c4.metric("Model threshold", f"{threshold:.0%}", "breaks" if sc["score_pct"] >= threshold else "OK",
              delta_color="inverse" if sc["score_pct"] >= threshold else "normal")
st.caption("Fails: " + (", ".join(sc["failed"]) or "none") + " · Penalties: " + (", ".join(sc["triggered"]) or "none"))

lines = data["lines"]
open_n = sum(1 for l in lines if l["final"] is None)
need = sum(1 for l in lines if l["needs_review"] and l["final"] is None)
st.write(f"{len(lines)} lines · {need} flagged for review · {open_n} not yet confirmed")

if data["locked"]:
    st.success("Baseline locked for this file.")
    if st.button("Unlock baseline"):
        grading.unlock_baseline(fid, user)
        st.rerun()
else:
    b1, b2, b3 = st.columns(3)
    if b1.button("Confirm all unflagged lines"):
        n = grading.confirm_all_unreviewed(fid, user, only_clean=True)
        st.success(f"Confirmed {n} line(s).")
        st.rerun()
    if b2.button("Lock baseline", disabled=open_n > 0, type="primary"):
        grading.lock_baseline(fid, user)
        st.rerun()
    if b3.button("Re-run grader"):
        try:
            grading.grade_file(vid, fid)
            st.rerun()
        except (llm.LLMError, ValueError) as e:
            st.error(str(e))

only_flagged = st.checkbox("Show only lines that need review", value=need > 0)
for ln in lines:
    if only_flagged and not (ln["needs_review"] and ln["final"] is None):
        continue
    is_pen = ln["points"] < 0 or (ln["category"] or "").lower() == "penalty"
    choices = ["triggered", "not_triggered"] if is_pen else ["pass", "fail"]
    status = ln["final"] or f"suggested {ln['suggested']}"
    flag = " ⚠️ " + "; ".join(ln["reasons"]) if ln["needs_review"] else ""
    with st.expander(f"{ln['crit_id']} · {ln['points']:g} pts · {status}{flag}"):
        st.write(ln["text"])
        st.markdown(f"**Pass 1:** {ln['pass1']} · **Pass 2:** {ln['pass2']} · confidence {ln['confidence']}")
        st.markdown(f"**Evidence:** {ln['evidence'] or '–'}")
        if ln["evidence2"] and ln["evidence2"] != ln["evidence"]:
            st.markdown(f"**Evidence (pass 2):** {ln['evidence2']}")
        if ln["note"]:
            st.caption(ln["note"])
        if not data["locked"]:
            cur = ln["final"] or ln["suggested"] or choices[0]
            pick = st.radio("Result", choices, index=choices.index(cur) if cur in choices else 0,
                            horizontal=True, key=f"r_{fid}_{ln['crit_id']}")
            if st.button("Confirm", key=f"c_{fid}_{ln['crit_id']}"):
                grading.set_final(fid, ln["crit_id"], pick, user)
                st.rerun()
