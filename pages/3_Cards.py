"""Card detail (spec 10): options side by side with their effects, prior stance, QA input and the decision form."""
import json

import streamlit as st

from core import impact, llm
from core import ops as opsmod
from core.auth import require_auth
from core.ui import FLAG_STYLE, decision_label, flag_chips, pick_report
from db.models import PriorQAItem, Row
from db.session import session_scope

st.set_page_config(page_title="Cards", layout="wide")
user = require_auth()
st.title("Cards")

PART_NAMES = {"2": "Part 2 · expert calls", "1b": "Part 1b · fixes to confirm", "1a": "Part 1a · tool fixes"}
ART = {"prompt": "Prompt", "reference": "References", "rubric": "Rubric", "trajectory": "Trajectory",
       "gold": "Gold", "model_run": "Model run", "failure_notes": "Failure notes"}

rid = pick_report()
if rid is None:
    st.stop()
state = impact.load_state(rid)

# First visit: rule-based ops and QA interpretations, so the page is useful straight away.
if impact.prepare(rid, user):
    state = impact.load_state(rid)

fl = impact.compute_flags(state)
decs = impact.current_decisions(state)
if not state.has_grades:
    st.warning("No grades yet for the baseline version, so scores and failure-note backing can't be projected. "
               "Grade the gold and model output on the **Grades** page.")

rows = []
for code, card in state.cards.items():
    rows.append({"Card": code, "Part": card.part, "Theme": card.theme or "",
                 "Decision": decision_label(card, decs[code]),
                 "Source": (decs[code] or {}).get("source", ""),
                 "Flags": flag_chips(fl["cards"][code]), "QA input": (card.qa or "")[:70], "Rule": card.rule or ""})
f1, f2 = st.columns([2, 1])
parts = f1.multiselect("Parts", list(PART_NAMES), default=list(PART_NAMES), format_func=lambda p: PART_NAMES[p])
flag_pick = f2.multiselect("Has flag", list(FLAG_STYLE))
shown = [r for r in rows if r["Part"] in parts and all(f in fl["cards"][r["Card"]] for f in flag_pick)]
st.dataframe(shown, hide_index=True, width="stretch")
codes = [r["Card"] for r in shown]
if not codes:
    st.stop()
pre = st.session_state.get("card_code")
code = st.selectbox("Open card", codes, index=codes.index(pre) if pre in codes else 0)
st.session_state["card_code"] = code
card = state.cards[code]
cflags = fl["cards"][code]

with session_scope() as s:
    lead = s.get(Row, card.lead_row_id)
    sec = json.loads(lead.sections_json or "{}").get("sections", {})
    qa_items = {q.qa_no: q for q in s.query(PriorQAItem).filter_by(task_id=state.task_id)}
    raw_rows = [(r.category_row, r.finding_raw) for r in s.query(Row).filter_by(card_id=card.id).order_by(Row.id)]

st.header(f"Card {code}  {flag_chips(cflags)}")
st.caption(f"{PART_NAMES[card.part]} · {len(card.rows)} row(s) · rule {card.rule or '–'} · "
           f"current decision: {decision_label(card, decs[code])}")
for k, v in cflags.items():
    if k == "AWAITING":
        continue
    if k == "REVERSAL":
        v = f"refused before by QA #{', #'.join(str(x) for x in v['qa'])}" + \
            ("" if v["note_given"] else " · a note is required")
    st.markdown(f"{FLAG_STYLE[k][0]} **{k}**: {v if isinstance(v, str) else '; '.join(map(str, v))}")

for label in ("In short", "Context", "Issue"):
    if sec.get(label):
        st.markdown(f"**{label}**  \n{sec[label]}")

# ---- options side by side ---------------------------------------------------
keys = [k for k in ("1", "2", "3", "fix", "new") if k in card.options]
base = fl["baseline"]
if keys:
    st.subheader("Options" if card.part == "2" else "Proposed fix")
    cols = st.columns(len(keys))
    for col, k in zip(cols, keys):
        with col:
            title = {"fix": "Fix", "new": "New option"}.get(k, f"Option {k}")
            st.markdown(f"#### {title}")
            st.write(card.options[k])
            arts = sorted({opsmod.ARTIFACT_OF_OP[o["type"]] for o in card.ops.get(k, []) if o.get("type")} |
                          {i["artifact"] for i in card.impacts
                           if i.get("option_scope") and k in (i["option_scope"] or "").split(",")})
            st.caption("Touches: " + (" · ".join(ART.get(a, a) for a in arts) or "nothing listed"))
            if impact.option_needs_rerun(card, k):
                st.markdown("🟦 **re-run needed**")
            if state.has_grades:
                w = impact.what_if(state, code, k)
                dm = w["model"]["score_pct"] - fl["projection"]["model"]["score_pct"]
                st.metric("Model if chosen", f"{w['model']['score_pct']:.1%}", f"{dm:+.1%}", delta_color="inverse")
                for idx, b in w["backing"].items():
                    if state.failure_map.get(idx):
                        stt = impact.note_status(base["backing"][idx], b)
                        st.caption(f"Failure note {idx}: {base['backing'][idx]:g} → {b:g} pts ({stt})")
                br = impact.breaks(state, w)
                if br:
                    st.markdown("🟥 **breaks:** " + "; ".join(br))
            with st.expander(f"Changes (ops) · {len(card.ops.get(k, []))}"):
                for o in card.ops.get(k, []):
                    c1, c2, c3 = st.columns([6, 1, 1])
                    tick = "✅" if o["_confirmed"] else "·"
                    c1.write(f"{tick} {opsmod.describe(o)}  _({o['_source']})_")
                    if not o["_confirmed"] and c2.button("OK", key=f"ok{o['_id']}", help="Confirm"):
                        impact.update_op(o["_id"], None, user, confirmed=True)
                        st.rerun()
                    if c3.button("✕", key=f"del{o['_id']}", help="Remove"):
                        impact.update_op(o["_id"], None, user)
                        st.rerun()
                if llm.is_configured() and st.button("Suggest changes with AI", key=f"ai{k}"):
                    try:
                        n = impact.suggest_llm_ops(state, code, k)
                        st.success(f"{n} change(s) suggested; check and confirm them.")
                        st.rerun()
                    except llm.LLMError as e:
                        st.error(str(e))
                with st.form(f"addop{k}"):
                    st.caption("Add a change as JSON, e.g. "
                               '{"type": "reweight", "crit": "4.15", "to": 4} or '
                               '{"type": "model_flip", "crit": "1.21", "to": "pass"}')
                    txt = st.text_input("Op", key=f"optxt{k}")
                    if st.form_submit_button("Add"):
                        try:
                            op = json.loads(txt)
                            errs = opsmod.validate(op)
                            if errs:
                                st.error("; ".join(errs))
                            else:
                                impact.add_op(card.option_ids[k], op, "user", user, confirmed=True)
                                st.rerun()
                        except json.JSONDecodeError as e:
                            st.error(f"Not valid JSON: {e}")
    for label in ("Why", "Ask the expert", "Clear fix"):
        if sec.get(label):
            st.markdown(f"**{label}:** {sec[label]}")

# ---- prior stance, QA input ---------------------------------------------------
c1, c2 = st.columns(2)
with c1:
    st.subheader("Prior stance")
    if not card.stances:
        st.write("None.")
    for p in card.stances:
        st.markdown(f"- **QA #{p['qa_no']}** ({p['status']}): {p['comment']}")
        q = qa_items.get(p["qa_no"])
        if q is not None:
            with st.expander(f"Full QA #{p['qa_no']}"):
                st.markdown(f"**Feedback:** {q.feedback}\n\n**Comment:** {q.comment}\n\n"
                            f"**Location:** {q.check_code_location}\n\n**Status:** {q.status}")
with c2:
    st.subheader("QA input")
    st.write(card.qa or "None yet.")
    qi = card.decisions.get("qa_interpreted")
    if qi:
        fl_qi = qi.get("flags") or {}
        st.markdown(f"**Read as:** {decision_label(card, qi)} · confidence {qi.get('confidence') or '–'} · "
                    f"by {fl_qi.get('method', '?')}")
        if fl_qi.get("rationale"):
            st.caption(fl_qi["rationale"])
        if fl_qi.get("ambiguity_reason"):
            st.markdown(f"🟨 {fl_qi['ambiguity_reason']}")
        if fl_qi.get("candidates"):
            st.caption("Could mean: " + ", ".join(fl_qi["candidates"]))
    if card.qa and st.button("Re-read QA input" + (" with AI" if llm.is_configured() else "")):
        impact.clear_decision(card.id, "qa_interpreted", user)
        impact.interpret_all(impact.load_state(rid), user)
        st.rerun()
    if card.expert_call or card.expert_note:
        st.markdown(f"**Expert call in report:** {card.expert_call or '–'}  \n**Note:** {card.expert_note or '–'}")

# ---- linked rows ----------------------------------------------------------------
if card.links or len(card.rows) > 1:
    st.subheader("Linked and folded rows")
    for r in card.rows:
        if r["id"] != card.lead_row_id:
            st.markdown(f"- Folded: **{r['category_row']}** · {r['theme'] or ''} · {r['where'] or ''}")
    for kind, label in (("folded", "Folded into"), ("decide_together", "Decide together with"),
                        ("mentions", "Mentions")):
        ks = [c for c, kd in card.links if kd == kind]
        if ks:
            st.markdown(f"- {label}: " + ", ".join(
                f"{c} ({decision_label(state.cards[c], decs.get(c))})" if c in state.cards else c for c in ks))

# ---- decision form ------------------------------------------------------------------
st.subheader("Decision")
choices = [k for k in ("1", "2", "3") if k in card.options]
choices += ["agree", "disagree"] if card.part != "2" else ["keep"]
choices += ["new"]
names = {"agree": "Agree (apply the fix)", "disagree": "Disagree (keep as is)", "keep": "Keep as is",
         "new": "New option (write it below)"}
cur = decs[code] or {}
with st.form("decision"):
    key = st.radio("Option", choices, horizontal=True,
                   index=choices.index(cur["option_key"]) if cur.get("option_key") in choices else 0,
                   format_func=lambda k: f"Option {k}" if k in ("1", "2", "3") else names[k])
    scope_txt = st.text_input("Scope (criteria, e.g. 6.13–6.15; blank = whole card)",
                              value=", ".join(cur.get("scope") or []))
    new_text = st.text_area("New option text (only for a new option)", value=cur.get("new_option_text") or "")
    note = st.text_area("Note", value=cur.get("note") or "")
    final = st.checkbox("Final", value=cur.get("source") == "final")
    if st.form_submit_button("Save decision", type="primary"):
        from core.qa_interpret import expand_scope

        scope = expand_scope(scope_txt)
        st_after = impact.load_state(rid)
        st_after.cards[code].decisions["final"] = {"option_key": key, "scope": scope, "note": note,
                                                   "source": "final"}
        rev = impact.compute_flags(st_after)["cards"][code].get("REVERSAL")
        if rev and not note.strip():
            st.error("This decision reverses the prior stance (QA #" + ", #".join(map(str, rev["qa"])) +
                     "). Add a note saying why.")
        else:
            if key == "new":
                impact.ensure_new_option(card.lead_row_id, new_text or "(new option)")
            if not final:
                impact.clear_decision(card.id, "final", user)
            impact.save_decision(card.id, "final" if final else "expert", key, user, scope=scope, note=note,
                                 new_option_text=new_text or None)
            st.success("Saved.")
            st.rerun()

with st.expander("Raw text of every row"):
    for cat, raw in raw_rows:
        st.markdown(f"**{cat}**")
        st.text(raw or "")
