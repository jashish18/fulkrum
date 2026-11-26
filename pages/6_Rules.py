"""Batch by rule (spec 8.7): one decision for every card with the same rule code, reviewed task by task."""
import streamlit as st

from core import impact
from core.auth import require_auth
from core.ui import decision_label, flag_chips
from db.models import Report, Task
from db.session import session_scope

st.set_page_config(page_title="Rules", layout="wide")
user = require_auth()
st.title("Rules")

with session_scope() as s:
    reps = [(r.id, t.task_code) for r, t in s.query(Report, Task).join(Task).order_by(Task.task_code)]
if not reps:
    st.info("No reports yet.")
    st.stop()

states = {rid: impact.load_state(rid) for rid, _ in reps}
by_rule: dict[str, list] = {}
for rid, st_ in states.items():
    for code, card in st_.cards.items():
        by_rule.setdefault(card.rule or "(none)", []).append((rid, st_, code, card))

summary = [{"Rule": r, "Cards": len(v), "Tasks": len({x[1].task_code for x in v}),
            "Decided": sum(1 for x in v if impact.current_decision(x[3]))} for r, v in sorted(by_rule.items())]
st.dataframe(summary, hide_index=True, width="stretch")

rule = st.selectbox("Rule", sorted(by_rule))
items = by_rule[rule]
flags = {rid: impact.compute_flags(st_) for rid, st_ in states.items()}
st.dataframe([{"Task": st_.task_code, "Card": code, "Part": card.part,
               "Decision": decision_label(card, impact.current_decision(card)),
               "Flags": flag_chips(flags[rid]["cards"][code]), "Theme": card.theme or ""}
              for rid, st_, code, card in items], hide_index=True, width="stretch")

st.subheader(f"Apply one decision to every {rule} card")
st.caption("Saved as a working (not final) decision on each card, so you can review it task by task on the "
           "Cards page and tick Final there.")
choice = st.selectbox("Decision", ["agree", "disagree", "keep", "1", "2", "3"],
                      format_func=lambda k: {"agree": "Agree (apply the fix)", "disagree": "Disagree",
                                             "keep": "Keep as is"}.get(k, f"Option {k}"))
note = st.text_input("Note")
if st.button("Apply to all", type="primary"):
    n = skipped = 0
    for rid, st_, code, card in items:
        if choice in ("1", "2", "3") and choice not in card.options:
            skipped += 1
            continue
        if choice == "agree" and "fix" not in card.options:
            skipped += 1
            continue
        impact.save_decision(card.id, "expert", choice, user, note=note or f"batch decision for rule {rule}")
        n += 1
    st.success(f"Applied to {n} card(s)" + (f"; skipped {skipped} that have no such option" if skipped else "") + ".")
    st.rerun()
