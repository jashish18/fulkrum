"""Step 1 impact assessment (spec section 8): decisions, projection, re-run and flags.

`load_state(report_id)` reads everything a report's task page needs into plain
Python objects; the projection and flag functions below are pure functions of
that state, so they're quick to re-run for every what-if.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core import ops as opsmod
from core import scoring, settings
from core.qa_interpret import REFUSING_STATUSES, expand_scope, is_keep_option, keep_option_key

APPLY_ORDER = ["delete", "add", "reweight", "accept_alternative", "model_flip", "gold_flip"]
KEEP_KEYS = {"keep", "disagree"}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class CardState:
    id: int
    code: str
    part: str
    rule: str | None
    lead_row_id: int
    options: dict[str, str]  # key -> text (1/2/3/fix/new)
    option_ids: dict[str, int]
    ops: dict[str, list[dict[str, Any]]]  # option key -> ops (with "_id", "_source", "_confirmed")
    impacts: list[dict[str, Any]]
    stances: list[dict[str, Any]]
    qa: str | None
    expert_call: str | None
    expert_note: str | None
    decisions: dict[str, dict[str, Any]]  # source -> latest decision
    links: list[tuple[str, str]]  # (linked card code, kind)
    rows: list[dict[str, Any]]
    criteria_list: str | None = None
    theme: str | None = None


@dataclass
class TaskState:
    report_id: int
    task_id: int
    task_code: str
    flagged_version: str | None
    version_id: int | None
    version_label: str | None
    criteria: dict[str, dict[str, Any]]  # crit_id -> {points, category, text, subtask}
    crit_order: list[str]
    model_results: dict[str, str]
    gold_results: dict[str, str]
    model_evidence: dict[str, str]
    model_file_id: int | None
    gold_file_id: int | None
    baseline_locked: dict[str, bool]
    failure_notes: dict[int, str]
    failure_map: dict[int, list[str]]
    cards: dict[str, CardState] = field(default_factory=dict)
    file_names: list[str] = field(default_factory=list)
    prompt_text: str = ""

    @property
    def has_grades(self) -> bool:
        return bool(self.model_results)


def _baseline_version(s, task_id: int, flagged: str | None):
    from db.models import Version

    vs = s.query(Version).filter_by(task_id=task_id).order_by(Version.uploaded_at).all()
    if not vs:
        return None
    for v in vs:
        if flagged and v.label.lower() == flagged.lower():
            return v
    return vs[-1]


def _results(s, file_id: int | None) -> tuple[dict[str, str], dict[str, str]]:
    """Final results, else the agreed result of the two passes, else pass 1."""
    from db.models import Grade

    if file_id is None:
        return {}, {}
    res: dict[str, str] = {}
    ev: dict[str, str] = {}
    by: dict[str, dict[int, Any]] = {}
    for g in s.query(Grade).filter_by(file_id=file_id):
        by.setdefault(g.crit_id, {})[g.pass_no] = g
    for cid, d in by.items():
        g1, g2 = d.get(1), d.get(2)
        if g1 is not None and g1.final_result:
            res[cid] = g1.final_result
        elif g1 is not None and (g2 is None or g1.result == g2.result):
            res[cid] = g1.result
        elif g2 is not None:
            res[cid] = g2.result if g1 is None else g1.result
        src = g1 or g2
        if src is not None and src.evidence:
            try:
                ev[cid] = json.loads(src.evidence).get("evidence", "")
            except json.JSONDecodeError:
                ev[cid] = src.evidence
    return res, ev


def load_state(report_id: int) -> TaskState:
    from core.ingest import load_text
    from db.models import (BaselineLock, Card, CardLink, Criterion, Decision, FailureMap, FailureNote, File,
                           Impact, Op, Option, PriorStance, Report, Row, Task)
    from db.session import session_scope

    with session_scope() as s:
        rep = s.get(Report, report_id)
        task = s.get(Task, rep.task_id)
        v = _baseline_version(s, task.id, rep.flagged_version)
        crits: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        files: list = []
        model_f = gold_f = None
        notes: dict[int, str] = {}
        fmap: dict[int, list[str]] = {}
        prompt = ""
        if v is not None:
            for c in s.query(Criterion).filter_by(version_id=v.id).order_by(Criterion.id):
                crits[c.crit_id] = {"points": c.points, "category": c.category, "text": c.text,
                                    "subtask": c.subtask}
                order.append(c.crit_id)
            files = s.query(File).filter_by(version_id=v.id).all()
            model_f = next((f for f in files if f.role == "model_output"), None)
            gold_f = next((f for f in files if f.role == "gold"), None)
            pf = next((f for f in files if f.role == "prompt"), None)
            prompt = load_text(pf.text_path) if pf else ""
            notes = {n.idx: n.text for n in s.query(FailureNote).filter_by(version_id=v.id)}
            for m in s.query(FailureMap).filter_by(version_id=v.id):
                fmap.setdefault(m.note_idx, []).append(m.crit_id)
        mres, mev = _results(s, model_f.id if model_f else None)
        gres, _ = _results(s, gold_f.id if gold_f else None)
        locks = {fid: True for (fid,) in s.query(BaselineLock.file_id)}
        st = TaskState(
            report_id=report_id, task_id=task.id, task_code=task.task_code, flagged_version=rep.flagged_version,
            version_id=v.id if v else None, version_label=v.label if v else None, criteria=crits,
            crit_order=order, model_results=mres, gold_results=gres, model_evidence=mev,
            model_file_id=model_f.id if model_f else None, gold_file_id=gold_f.id if gold_f else None,
            baseline_locked={"model": bool(model_f and locks.get(model_f.id)),
                             "gold": bool(gold_f and locks.get(gold_f.id))},
            failure_notes=notes, failure_map=fmap, file_names=[f.name for f in files], prompt_text=prompt,
        )
        cards = s.query(Card).filter_by(report_id=report_id).order_by(Card.id).all()
        code_by_id = {c.id: c.code for c in cards}
        for c in cards:
            rows = s.query(Row).filter_by(card_id=c.id).order_by(Row.id).all()
            lead = next((r for r in rows if r.id == c.lead_row_id), rows[0])
            options, option_ids, ops = {}, {}, {}
            for o in s.query(Option).filter_by(row_id=lead.id):
                options[o.key] = o.text
                option_ids[o.key] = o.id
                ops[o.key] = []
                for op in s.query(Op).filter_by(option_id=o.id).order_by(Op.id):
                    d = json.loads(op.op_json)
                    d.update(_id=op.id, _source=op.source, _confirmed=op.confirmed)
                    ops[o.key].append(d)
            impacts = [{"artifact": i.artifact, "option_scope": i.option_scope, "text": i.text,
                        "refs": json.loads(i.refs_json or "{}")}
                       for i in s.query(Impact).filter_by(row_id=lead.id)]
            stances = [{"qa_no": p.qa_no, "status": p.status, "comment": p.comment}
                       for p in s.query(PriorStance).filter_by(row_id=lead.id)]
            decs: dict[str, dict[str, Any]] = {}
            for d in s.query(Decision).filter_by(card_id=c.id).order_by(Decision.id):
                decs[d.source] = {
                    "id": d.id, "source": d.source, "option_key": d.option_key,
                    "scope": json.loads(d.scope_json or "[]"), "new_option_text": d.new_option_text,
                    "confidence": d.confidence, "flags": json.loads(d.flags_json or "{}"), "note": d.note,
                    "decided_by": d.decided_by, "decided_at": d.decided_at,
                }
            links = [(code_by_id.get(l.linked_card_id, "?"), l.kind)
                     for l in s.query(CardLink).filter_by(card_id=c.id)]
            st.cards[c.code] = CardState(
                id=c.id, code=c.code, part=c.part, rule=lead.rule, lead_row_id=lead.id, options=options,
                option_ids=option_ids, ops=ops, impacts=impacts, stances=stances, qa=lead.qa_comment,
                expert_call=lead.expert_call, expert_note=lead.expert_note, decisions=decs, links=links,
                rows=[{"id": r.id, "category_row": r.category_row, "theme": r.theme, "where": r.where}
                      for r in rows],
                criteria_list=lead.criteria_list, theme=lead.theme,
            )
    return st


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def current_decision(card: CardState) -> dict[str, Any] | None:
    """Precedence: final, else expert call, else the QA-interpreted option, else none (8.3)."""
    for src in ("final", "expert", "qa_interpreted"):
        d = card.decisions.get(src)
        if d and d.get("option_key") and d["option_key"] != "unclear":
            return d
    return None


def resolve_key(card: CardState, key: str | None) -> str | None:
    """Map a decision key to an option key whose ops apply (None = no change)."""
    if key is None:
        return None
    if key in KEEP_KEYS:
        return None
    if key == "agree":
        return "fix" if "fix" in card.options else None
    if key in card.options:
        return key
    return None


def is_keep(card: CardState, key: str | None) -> bool:
    if key in KEEP_KEYS:
        return True
    if key in card.options and key != "fix" and is_keep_option(card.options[key]):
        return True
    return False


def decided_ops(card: CardState, decision: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not decision:
        return []
    k = resolve_key(card, decision.get("option_key"))
    if k is None:
        return []
    out = list(card.ops.get(k, []))
    scope = decision.get("scope") or []
    if scope:
        out = [o for o in out if not o.get("crit") or str(o["crit"]) in scope]
    return out


def parse_expert_call(text: str | None, part: str) -> dict[str, Any] | None:
    """Read the report's Expert call cell: 'Option 2 (6.13–6.15)', 'Agree', 'Disagree', 'Edited: …'."""
    t = (text or "").strip()
    if not t:
        return None
    m = re.match(r"option\s+(\d)\s*(?:\((.*)\))?", t, re.I)
    if m:
        return {"option_key": m.group(1), "scope": expand_scope(m.group(2) or "")}
    low = t.lower()
    if low.startswith("edited"):
        return {"option_key": "new", "new_option_text": t.split(":", 1)[-1].strip()}
    if low.startswith("disagree") or low.startswith("hold"):
        return {"option_key": "disagree"}
    if low.startswith("agree") or low in ("yes", "ok"):
        return {"option_key": "agree" if part != "2" else "keep"}
    if low.startswith("keep"):
        return {"option_key": "keep"}
    return None


# ---------------------------------------------------------------------------
# Projection (8.3)
# ---------------------------------------------------------------------------


def _decimals(s: str) -> int:
    m = re.search(r"\.(\d+)", s)
    return len(m.group(1)) if m else 0


def _numbers(s: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", s or "") if x not in ("-",)]


def compare_alternative(evidence: str, value: Any) -> str | None:
    """Does the model's graded value match the alternative at display precision? pass/fail/None (unclear)."""
    vals = [v.strip() for v in re.split(r"\bor\b|/|,(?!\d{3})", str(value)) if v.strip()]
    nums = _numbers(evidence)
    if not nums:
        return None
    for v in vals:
        target = _numbers(v)
        if not target:
            continue
        d = _decimals(v.replace(",", ""))
        pct = "%" in v
        for n in nums:
            for cand in ((n, n * 100) if pct else (n,)):
                if round(cand, d) == round(target[0], d):
                    return "pass"
    return "fail"


def project(state: TaskState, decisions: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Apply the ops of the given decisions (card code -> decision) to the baseline grades."""
    crits = {k: dict(v) for k, v in state.criteria.items()}
    model = dict(state.model_results)
    gold = dict(state.gold_results)
    all_ops: list[tuple[str, dict[str, Any]]] = []
    for code, d in decisions.items():
        card = state.cards.get(code)
        if card is None:
            continue
        for o in decided_ops(card, d):
            all_ops.append((code, o))
    notes: list[str] = []
    added = 0
    for t in APPLY_ORDER:
        for code, o in all_ops:
            if o.get("type") != t:
                continue
            cid = str(o.get("crit")) if o.get("crit") is not None else None
            if t == "delete" and cid in crits:
                crits.pop(cid)
            elif t == "add":
                added += 1
                nid = f"new-{code}-{added}"
                try:
                    pts = float(o.get("points") or 0)
                except (TypeError, ValueError):
                    pts = 0.0
                crits[nid] = {"points": pts, "category": o.get("category"), "text": o.get("text"), "subtask": ""}
                pen = scoring.is_penalty(pts, o.get("category"))
                model[nid] = o.get("model_result") or ("not_triggered" if pen else "fail")
                gold[nid] = o.get("gold_result") or ("not_triggered" if pen else "pass")
                notes.append(f"Card {code} adds a line; assumed the model {model[nid]}s and the gold "
                             f"{gold[nid]}s it (edit the op to change this)")
            elif t == "reweight" and cid in crits:
                try:
                    crits[cid]["points"] = float(o.get("to"))
                except (TypeError, ValueError):
                    pass
            elif t == "accept_alternative" and cid in crits:
                if o.get("model_result") in ("pass", "fail"):
                    model[cid] = o["model_result"]
                elif model.get(cid) == "fail":
                    r = compare_alternative(state.model_evidence.get(cid, ""), o.get("value"))
                    if r is None:
                        notes.append(f"Card {code}: can't tell if the model's value for [{cid}] matches "
                                     f"{o.get('value')!r}; set the op's model result")
                    else:
                        model[cid] = r
            elif t == "model_flip" and cid in crits:
                model[cid] = o.get("to")
            elif t == "gold_flip" and cid in crits:
                gold[cid] = o.get("to")
    formula = settings.get("SCORE_FORMULA")
    cl = [dict(crit_id=k, points=v["points"], category=v["category"]) for k, v in crits.items()]
    ms = scoring.score(cl, model, formula)
    gs = scoring.score(cl, gold, formula)
    backing = {}
    for idx in state.failure_notes:
        total = 0.0
        for cid in state.failure_map.get(idx, []):
            c = crits.get(cid)
            if not c:
                continue
            r = model.get(cid)
            if scoring.is_penalty(c["points"], c["category"]):
                if r == "triggered":
                    total += abs(c["points"])
            elif r == "fail":
                total += c["points"]
        backing[idx] = total
    return {"model": ms, "gold": gs, "backing": backing, "notes": notes, "criteria": crits,
            "model_results": model, "gold_results": gold, "ops": all_ops}


def baseline(state: TaskState) -> dict[str, Any]:
    return project(state, {})


def current_decisions(state: TaskState) -> dict[str, dict[str, Any] | None]:
    return {code: current_decision(c) for code, c in state.cards.items()}


def breaks(state: TaskState, proj: dict[str, Any]) -> list[str]:
    out = []
    mt, gt = settings.get_float("MODEL_THRESHOLD"), settings.get_float("GOLD_THRESHOLD")
    if not state.has_grades:
        return out
    if proj["model"]["score_pct"] >= mt:
        out.append(f"model {proj['model']['score_pct']:.1%} ≥ {mt:.0%}")
    if state.gold_results and proj["gold"]["score_pct"] < gt:
        out.append(f"gold {proj['gold']['score_pct']:.1%} < {gt:.0%}")
    for idx, b in proj["backing"].items():
        if state.failure_map.get(idx) and b <= 0:
            out.append(f"failure note {idx} has zero backing")
    return out


def note_status(base: float, now: float) -> str:
    if now <= 0:
        return "gone"
    if now < base:
        return "weakened"
    return "holds"


def what_if(state: TaskState, code: str, option_key: str, scope: list[str] | None = None) -> dict[str, Any]:
    """The projection with this one option on top of every other current decision (8.3)."""
    decs = current_decisions(state)
    decs[code] = {"option_key": option_key, "scope": scope or []}
    return project(state, decs)


def card_contribution(state: TaskState, code: str, proj_all: dict[str, Any] | None = None) -> dict[str, float]:
    decs = current_decisions(state)
    with_all = proj_all or project(state, decs)
    decs[code] = None
    without = project(state, decs)
    return {
        "model_points": (with_all["model"]["earned"] - with_all["model"]["penalty"])
        - (without["model"]["earned"] - without["model"]["penalty"]),
        "model_pct": with_all["model"]["score_pct"] - without["model"]["score_pct"],
    }


# ---------------------------------------------------------------------------
# Re-run (8.4)
# ---------------------------------------------------------------------------


def rerun(state: TaskState) -> list[dict[str, Any]]:
    out = []
    for code, card in state.cards.items():
        for o in decided_ops(card, current_decision(card)):
            if o.get("type") in opsmod.RERUN_OPS:
                out.append({"card": code, "op": o, "metadata_only": bool(o.get("metadata_only")),
                            "label": opsmod.describe(o)})
    return out


def option_needs_rerun(card: CardState, key: str) -> bool:
    return any(o.get("type") in opsmod.RERUN_OPS for o in card.ops.get(key, []))


# ---------------------------------------------------------------------------
# Flags (8.6)
# ---------------------------------------------------------------------------


def _keep_targets(card: CardState) -> list[tuple]:
    """What a keep decision refuses to change: the targets of the card's other options."""
    out: list[tuple] = []
    for k, lst in card.ops.items():
        if is_keep(card, k):
            continue
        for o in lst:
            out += opsmod.targets(o)
    return out


def _direction(card: CardState, d: dict[str, Any] | None) -> str | None:
    if not d or not d.get("option_key") or d["option_key"] == "unclear":
        return None
    return "keep" if is_keep(card, d["option_key"]) else "change"


def _stance_crits(comment: str) -> set[str]:
    return set(expand_scope(comment or ""))


def compute_flags(state: TaskState) -> dict[str, Any]:
    """Flags per card and for the task."""
    decs = current_decisions(state)
    proj = project(state, decs)
    base = baseline(state)
    task_breaks = breaks(state, proj)
    flags: dict[str, dict[str, Any]] = {code: {} for code in state.cards}

    # BREAK: the card's decision is part of what breaks the task.
    if task_breaks:
        for code, d in decs.items():
            if not d or not decided_ops(state.cards[code], d):
                continue
            alt = dict(decs)
            alt[code] = None
            if len(breaks(state, project(state, alt))) < len(task_breaks):
                flags[code]["BREAK"] = task_breaks

    # CONFLICT (a): decided ops on different cards touch the same target with different effects.
    touched: list[tuple[str, tuple, str, dict]] = []
    for code, d in decs.items():
        for o in decided_ops(state.cards[code], d):
            for t in opsmod.targets(o):
                touched.append((code, t, opsmod.effect(o), o))
    for i, (c1, t1, e1, o1) in enumerate(touched):
        for c2, t2, e2, o2 in touched[i + 1:]:
            if c1 != c2 and opsmod.targets_overlap(t1, t2) and e1 != e2:
                msg = f"{opsmod.describe(o1)} vs card {c2}: {opsmod.describe(o2)}"
                flags[c1].setdefault("CONFLICT", []).append(msg)
                flags[c2].setdefault("CONFLICT", []).append(f"{opsmod.describe(o2)} vs card {c1}: "
                                                            f"{opsmod.describe(o1)}")
    # CONFLICT (b): one card keeps what another card's decision changes.
    for code, d in decs.items():
        card = state.cards[code]
        if _direction(card, d) != "keep":
            continue
        kt = _keep_targets(card)
        if not kt:
            continue
        for c2, t2, _e, o2 in touched:
            if c2 == code:
                continue
            if any(opsmod.targets_overlap(t, t2) for t in kt):
                flags[code].setdefault("CONFLICT", []).append(
                    f"keeps what card {c2} changes: {opsmod.describe(o2)}")
                flags[c2].setdefault("CONFLICT", []).append(
                    f"changes what card {code} keeps: {opsmod.describe(o2)}")
    # CONFLICT (c): linked cards decided inconsistently.
    for code, card in state.cards.items():
        for other, kind in card.links:
            if kind not in ("folded", "decide_together") or other not in state.cards:
                continue
            d1, d2 = _direction(card, decs[code]), _direction(state.cards[other], decs[other])
            if d1 and d2 and d1 != d2:
                flags[code].setdefault("CONFLICT", []).append(
                    f"linked ({kind.replace('_', ' ')}) card {other} is decided '{d2}', this one '{d1}'")
    for code in flags:
        if "CONFLICT" in flags[code]:
            flags[code]["CONFLICT"] = list(dict.fromkeys(flags[code]["CONFLICT"]))

    for code, card in state.cards.items():
        d = decs[code]
        # REVERSAL: adopts a change the prior stance refused.
        if d and _direction(card, d) == "change":
            refused = [s for s in card.stances if (s.get("status") or "").strip().lower() in REFUSING_STATUSES]
            if refused:
                touched_crits = {str(o["crit"]) for o in decided_ops(card, d) if o.get("crit")}
                scope = set(d.get("scope") or []) or touched_crits
                hits = []
                for s in refused:
                    sc = _stance_crits(s.get("comment") or "")
                    if not sc or not scope or sc & scope:
                        hits.append(s["qa_no"])
                if hits:
                    flags[code]["REVERSAL"] = {"qa": sorted(hits), "note_given": bool((d.get("note") or "").strip())}
        # AMBIGUOUS: the QA input couldn't be mapped to one option.
        qi = card.decisions.get("qa_interpreted")
        if qi and (qi.get("option_key") == "unclear" or (qi.get("flags") or {}).get("ambiguity_reason")):
            flags[code]["AMBIGUOUS"] = (qi.get("flags") or {}).get("ambiguity_reason") or "unclear"
        # RERUN
        rr = [o for o in decided_ops(card, d) if o.get("type") in opsmod.RERUN_OPS]
        if rr:
            flags[code]["RERUN"] = [opsmod.describe(o) for o in rr]
        # AWAITING
        if not (card.qa or "").strip() and d is None:
            flags[code]["AWAITING"] = True

    counts = {k: sum(1 for f in flags.values() if k in f)
              for k in ("BREAK", "CONFLICT", "REVERSAL", "AMBIGUOUS", "RERUN", "AWAITING")}
    return {"cards": flags, "task_breaks": task_breaks, "projection": proj, "baseline": base,
            "rerun": rerun(state), "counts": counts}


# ---------------------------------------------------------------------------
# Writing decisions and ops
# ---------------------------------------------------------------------------


def save_decision(card_id: int, source: str, option_key: str | None, user: str | None,
                  scope: list[str] | None = None, note: str | None = None, new_option_text: str | None = None,
                  confidence: str | None = None, flags: dict[str, Any] | None = None) -> int:
    from db.models import Decision
    from db.session import audit, session_scope

    with session_scope() as s:
        prev = (s.query(Decision).filter_by(card_id=card_id, source=source).order_by(Decision.id.desc()).first())
        d = Decision(card_id=card_id, source=source, option_key=option_key, scope_json=json.dumps(scope or []),
                     new_option_text=new_option_text, confidence=confidence, flags_json=json.dumps(flags or {}),
                     note=note, decided_by=user)
        s.add(d)
        s.flush()
        audit(s, user, f"decisions.{source}", card_id,
              {"option_key": prev.option_key, "scope": prev.scope_json, "note": prev.note} if prev else None,
              {"option_key": option_key, "scope": scope, "note": note})
        return d.id


def clear_decision(card_id: int, source: str, user: str | None) -> None:
    from db.models import Decision
    from db.session import audit, session_scope

    with session_scope() as s:
        q = s.query(Decision).filter_by(card_id=card_id, source=source)
        n = q.count()
        if n:
            q.delete()
            audit(s, user, f"decisions.{source}", card_id, {"deleted": n}, None)


def ensure_new_option(lead_row_id: int, text: str) -> int:
    from db.models import Option
    from db.session import session_scope

    with session_scope() as s:
        o = s.query(Option).filter_by(row_id=lead_row_id, key="new").first()
        if o is None:
            o = Option(row_id=lead_row_id, key="new", text=text)
            s.add(o)
        else:
            o.text = text
        s.flush()
        return o.id


def add_op(option_id: int, op: dict[str, Any], source: str, user: str | None, confirmed: bool = False) -> int:
    from db.models import Op
    from db.session import audit, session_scope

    clean = {k: v for k, v in op.items() if not k.startswith("_")}
    with session_scope() as s:
        o = Op(option_id=option_id, op_json=json.dumps(clean), source=source, confirmed=confirmed)
        s.add(o)
        s.flush()
        if source == "user":
            audit(s, user, "ops", o.id, None, clean)
        return o.id


def update_op(op_id: int, op: dict[str, Any] | None, user: str | None, confirmed: bool | None = None) -> None:
    """Edit, confirm or (with op=None and confirmed=None) delete an op. Audited."""
    from db.models import Op
    from db.session import audit, session_scope

    with session_scope() as s:
        o = s.get(Op, op_id)
        if o is None:
            return
        before = {"op": json.loads(o.op_json), "confirmed": o.confirmed, "source": o.source}
        if op is None and confirmed is None:
            s.delete(o)
            audit(s, user, "ops", op_id, before, None)
            return
        if op is not None:
            o.op_json = json.dumps({k: v for k, v in op.items() if not k.startswith("_")})
            o.source = "user"
        if confirmed is not None:
            o.confirmed = confirmed
        audit(s, user, "ops", op_id, before, {"op": json.loads(o.op_json), "confirmed": o.confirmed})


def suggest_rule_ops(state: TaskState) -> int:
    """Add rule-based prompt/reference ops for every option that has none from rules yet."""
    n = 0
    for card in state.cards.values():
        for key, text in card.options.items():
            if key == "new":
                continue
            if any(o.get("_source") == "rule" for o in card.ops.get(key, [])):
                continue
            for op in opsmod.rule_suggestions(key, card.part, card.impacts, text, state.file_names):
                add_op(card.option_ids[key], op, "rule", None)
                n += 1
    return n


def suggest_llm_ops(state: TaskState, code: str, key: str) -> int:
    card = state.cards[code]
    lines = "\n".join(f"- {cid} ({c['points']:g}): {c['text']}" for cid, c in state.criteria.items())
    label = f"Option {key}" if key not in ("fix", "new") else ("Proposed fix" if key == "fix" else "New option")
    scoped = [i for i in card.impacts if not i.get("option_scope") or key in (i["option_scope"] or "").split(",")]
    n = 0
    for op in opsmod.llm_suggestions(label, card.options[key], scoped, lines):
        if not opsmod.validate(op):
            add_op(card.option_ids[key], op, "llm", None)
            n += 1
    return n


def interpret_all(state: TaskState, user: str | None = None, use_llm: bool = True, force: bool = False) -> int:
    """Store a qa_interpreted decision for every card with QA input (8.5), and import expert calls."""
    from core.qa_interpret import interpret

    n = 0
    for code, card in state.cards.items():
        if (card.qa or "").strip() and (force or "qa_interpreted" not in card.decisions):
            r = interpret(code, card.part, card.qa, {k: v for k, v in card.options.items() if k != "new"},
                          card.stances, use_llm=use_llm)
            save_decision(card.id, "qa_interpreted", r["option_key"], "interpreter",
                          scope=r.get("scope_criteria") or [], new_option_text=r.get("new_option_text") or None,
                          confidence=r.get("confidence"),
                          flags={"ambiguity_reason": r.get("ambiguity_reason") or "",
                                 "candidates": r.get("candidates") or [], "rationale": r.get("rationale") or "",
                                 "method": r.get("method")})
            n += 1
        ec = parse_expert_call(card.expert_call, card.part)
        if ec and "expert" not in card.decisions:
            save_decision(card.id, "expert", ec["option_key"], "report", scope=ec.get("scope") or [],
                          note=card.expert_note, new_option_text=ec.get("new_option_text"))
    return n


def set_failure_map(version_id: int, mapping: dict[int, list[str]], user: str | None, confirmed: bool = True) -> None:
    from db.models import FailureMap
    from db.session import audit, session_scope

    with session_scope() as s:
        before = {}
        for m in s.query(FailureMap).filter_by(version_id=version_id):
            before.setdefault(m.note_idx, []).append(m.crit_id)
        s.query(FailureMap).filter_by(version_id=version_id).delete()
        for idx, crits in mapping.items():
            for cid in crits:
                s.add(FailureMap(version_id=version_id, note_idx=int(idx), crit_id=str(cid), confirmed=confirmed))
        audit(s, user, "failure_map", version_id, before, mapping)


def suggest_failure_map(state: TaskState) -> dict[int, list[str]]:
    """LLM proposal of which failing criteria back each failure note (8.2)."""
    from core import llm

    failing = [cid for cid in state.crit_order if state.model_results.get(cid) in ("fail", "triggered")]
    schema = {"type": "object", "additionalProperties": False, "required": ["notes"],
              "properties": {"notes": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["idx", "criteria"],
                  "properties": {"idx": {"type": "integer"},
                                 "criteria": {"type": "array", "items": {"type": "string"}}}}}}}
    notes = "\n".join(f"{i}. {t}" for i, t in sorted(state.failure_notes.items()))
    crits = "\n".join(f"- {cid} ({state.criteria[cid]['points']:g}): {state.criteria[cid]['text']}" for cid in failing)
    user = llm.load_prompt("failure_map", "v1").format(notes=notes, criteria=crits)
    out = llm.call_json("failure_map", "v1", "Return only JSON that matches the schema.", user, schema)
    return {int(n["idx"]): [c for c in n["criteria"] if c in failing] for n in out.get("notes", [])}


def prepare(report_id: int, user: str | None = None) -> bool:
    """First look at a report: rule-based ops and QA interpretations. Returns True if it did anything."""
    st = load_state(report_id)
    if any(o.get("_source") == "rule" for c in st.cards.values() for lst in c.ops.values() for o in lst) or \
            any("qa_interpreted" in c.decisions for c in st.cards.values()):
        return False
    suggest_rule_ops(st)
    interpret_all(load_state(report_id), user)
    return True
