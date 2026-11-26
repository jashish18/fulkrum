"""Cards from a rework report: parsed sections, options, impacts, prior stances and links.

Phase 2 view. Projections, flags and the decision form arrive in Phase 4.
"""
import json

import streamlit as st

from core.auth import require_auth
from db.models import Card, CardLink, Impact, Option, PriorQAItem, PriorStance, Report, Row, Task
from db.session import session_scope

st.set_page_config(page_title="Cards", layout="wide")
user = require_auth()
st.title("Cards")

PART_NAMES = {"2": "Part 2 · expert calls", "1b": "Part 1b · fixes to confirm", "1a": "Part 1a · tool fixes"}
ARTIFACT_NAMES = {"prompt": "Prompt", "reference": "Reference files", "rubric": "Rubric",
                  "trajectory": "Trajectory", "gold": "Gold", "model_run": "Model run & failure notes",
                  "failure_notes": "Failure notes"}

with session_scope() as s:
    reports = s.query(Report, Task).join(Task).order_by(Task.task_code, Report.uploaded_at).all()
    choices = {f"Task {t.task_code} · flagged at {r.flagged_version} · {r.filename}": r.id for r, t in reports}

if not choices:
    st.info("No rework report yet. Upload one on the **Upload** page.")
    st.stop()

rid = choices[st.selectbox("Report", list(choices))]

with session_scope() as s:
    report = s.get(Report, rid)
    counts = json.loads(report.expected_counts_json or "{}")
    for w in counts.get("warnings", []):
        st.warning(w)
    cards = s.query(Card).filter_by(report_id=rid).order_by(Card.id).all()
    qa_items = {q.qa_no: q for q in s.query(PriorQAItem).filter_by(task_id=report.task_id)}
    code_by_id = {c.id: c.code for c in cards}

    summary = []
    for c in cards:
        lead = s.get(Row, c.lead_row_id) if c.lead_row_id else None
        nrows = s.query(Row).filter_by(card_id=c.id).count()
        summary.append({
            "Card": c.code, "Part": c.part, "Rows": nrows,
            "Theme": lead.theme if lead else "", "Criteria": lead.criteria_list if lead else "",
            "Points": lead.points_at_stake if lead else "",
            "QA input": (lead.qa_comment or "")[:80] if lead else "",
            "Rule": lead.rule if lead else "",
        })
    f1, f2 = st.columns([1, 2])
    part_filter = f1.multiselect("Parts", list(PART_NAMES), default=list(PART_NAMES),
                                 format_func=lambda p: PART_NAMES[p])
    only_qa = f2.checkbox("Only cards with QA input")
    shown = [r for r in summary if r["Part"] in part_filter and (r["QA input"] or not only_qa)]
    st.dataframe(shown, hide_index=True, width="stretch")

    codes = [r["Card"] for r in shown]
    if not codes:
        st.stop()
    pick = st.selectbox("Open card", codes)
    card = next(c for c in cards if c.code == pick)
    rows = s.query(Row).filter_by(card_id=card.id).order_by(Row.id).all()
    lead = next((r for r in rows if r.id == card.lead_row_id), rows[0])
    sec = json.loads(lead.sections_json or "{}").get("sections", {})

    st.header(f"Card {card.code}")
    st.caption(f"{PART_NAMES[card.part]} · {lead.category_row} leads · {len(rows)} row(s) · "
               f"rule {lead.rule or '–'} · parsed by {lead.parse_method}")
    for label in ("In short", "Context", "Issue"):
        if sec.get(label):
            st.markdown(f"**{label}**")
            st.markdown(sec[label])

    opts = s.query(Option).filter_by(row_id=lead.id).order_by(Option.key).all()
    if opts:
        st.subheader("Options" if card.part == "2" else "Proposed fix")
        cols = st.columns(len(opts))
        impacts = s.query(Impact).filter_by(row_id=lead.id).all()
        for col, o in zip(cols, opts):
            with col:
                st.markdown(f"**{'Option ' + o.key if o.key != 'fix' else 'Fix'}**")
                st.write(o.text)
                mine = [i for i in impacts if i.option_scope is None or o.key in (i.option_scope or "").split(",")]
                arts = sorted({i.artifact for i in mine})
                st.caption("Touches: " + (", ".join(ARTIFACT_NAMES.get(a, a) for a in arts) or "–"))
        for label in ("Why", "Ask the expert", "Clear fix"):
            if sec.get(label):
                st.markdown(f"**{label}:** {sec[label]}")

        with st.expander("Impacts by file"):
            for art in ARTIFACT_NAMES:
                items = [i for i in impacts if i.artifact == art]
                if not items:
                    continue
                st.markdown(f"**{ARTIFACT_NAMES[art]}**")
                for i in items:
                    scope = f"(Option {i.option_scope}) " if i.option_scope else "(every option) "
                    st.markdown(f"- {scope}{i.text}")

    st.subheader("Prior stance")
    stances = s.query(PriorStance).filter_by(row_id=lead.id).all()
    if not stances:
        st.write("None.")
    for p in stances:
        st.markdown(f"- **QA #{p.qa_no}** ({p.status}): {p.comment}")
        q = qa_items.get(p.qa_no)
        if q is not None:
            with st.expander(f"Full QA #{p.qa_no} from the prior QA sheet"):
                st.markdown(f"**Feedback:** {q.feedback}\n\n**Comment:** {q.comment}\n\n"
                            f"**Check code and location:** {q.check_code_location}\n\n"
                            f"**QA comment:** {q.qa_comment}\n\n**Status:** {q.status}")

    st.subheader("QA input")
    st.write(lead.qa_comment or "None yet.")
    if lead.expert_call or lead.expert_note:
        st.markdown(f"**Expert call:** {lead.expert_call or '–'}  \n**Expert note:** {lead.expert_note or '–'}")

    links = s.query(CardLink).filter_by(card_id=card.id).all()
    if links or len(rows) > 1:
        st.subheader("Linked and folded rows")
        for r in rows:
            if r.id != lead.id:
                st.markdown(f"- Folded: **{r.category_row}** · {r.theme or ''} · {r.where or ''}")
        for kind, label in (("folded", "Folded into"), ("decide_together", "Decide together with"),
                            ("mentions", "Mentions")):
            ks = [code_by_id.get(l.linked_card_id, "?") for l in links if l.kind == kind]
            if ks:
                st.markdown(f"- {label}: " + ", ".join(ks))

    with st.expander("Raw text of every row"):
        for r in rows:
            st.markdown(f"**{r.category_row}**")
            st.text(r.finding_raw or "")
