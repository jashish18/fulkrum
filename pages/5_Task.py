"""Task page (spec 10): header with prompt, failure notes, scores and re-run badge, then tabs per artifact."""
import json

import streamlit as st

from core import impact, llm, settings
from core import ops as opsmod
from core.auth import require_auth
from core.ingest import load_text
from core.ui import decision_label, flag_chips, pick_report
from db.models import AuditLog, Card, File
from db.session import session_scope

st.set_page_config(page_title="Task", layout="wide")
user = require_auth()
st.title("Task")

rid = pick_report()
if rid is None:
    st.stop()
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


def changes_for(artifacts: set[str]):
    rows = []
    for code, card in state.cards.items():
        d = decs[code]
        dk = impact.resolve_key(card, d.get("option_key")) if d else None
        for k, lst in card.ops.items():
            for o in lst:
                if opsmod.ARTIFACT_OF_OP.get(o.get("type")) in artifacts:
                    rows.append({"Card": code, "Option": k, "Change": opsmod.describe(o),
                                 "Status": "decided" if k == dk else "possible"})
        for i in card.impacts:
            if i["artifact"] in artifacts and i.get("option_scope"):
                if not any(r["Card"] == code and r["Change"] == i["text"] for r in rows):
                    keys = i["option_scope"].split(",")
                    rows.append({"Card": code, "Option": i["option_scope"], "Change": i["text"],
                                 "Status": "decided" if dk in keys else "possible"})
    rows.sort(key=lambda r: (r["Status"] != "decided", r["Card"]))
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
    st.info("Verification of a revised version arrives in Phase 6.")

with tabs[9]:
    with session_scope() as s:
        card_ids = [str(c.id) for c in s.query(Card).filter_by(report_id=rid)]
        logs = (s.query(AuditLog).filter(AuditLog.entity_id.in_(card_ids + [str(state.version_id)]))
                .order_by(AuditLog.at.desc()).limit(200).all())
        st.dataframe([{"When": l.at, "Who": l.user, "What": l.entity, "Id": l.entity_id,
                       "Before": l.before_json, "After": l.after_json} for l in logs],
                     hide_index=True, width="stretch")
