"""Grading engine (spec section 7).

One LLM call per rubric subtask per graded file, run twice (the second pass
lists criteria in reverse). Lines where the passes disagree, confidence is low,
or the gold fails go to review. The review screen confirms or overrides each
line; confirming every line of a file locks its baseline.

Storage: one `grades` row per (file, criterion, pass). The pass-1 row also
carries the line's `final_result` and `reviewed_by` once reviewed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from core import llm, scoring, settings
from core.ingest import load_text

PROMPT_NAME = "grade"
PROMPT_VERSION = "v1"

GRADE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["grades"],
    "properties": {
        "grades": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "result", "evidence", "confidence", "note"],
                "properties": {
                    "id": {"type": "string"},
                    "result": {"type": "string", "enum": ["pass", "fail", "triggered", "not_triggered"]},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
            },
        }
    },
}

GOLD_FAIL_LABEL = "grader error or real gold issue"


def _crit_lines(crits: list[dict[str, Any]]) -> str:
    out = []
    for c in crits:
        kind = "PENALTY" if scoring.is_penalty(c["points"], c["category"]) else "criterion"
        out.append(f"- id {c['crit_id']} ({kind}, {c['points']:g} points, {c['category'] or '-'}): {c['text']}")
    return "\n".join(out)


def build_prompt(prompt_text: str, subtask: str, title: str, crits: list[dict[str, Any]],
                 file_name: str, file_text: str) -> str:
    tmpl = llm.load_prompt(PROMPT_NAME, PROMPT_VERSION)
    return tmpl.format(prompt=prompt_text, subtask=subtask, title=title or "", criteria=_crit_lines(crits),
                       file_name=file_name, file_text=file_text)


def cache_key(file_sha: str, rubric_sha: str, model: str) -> str:
    """sha256(file) + sha256(rubric) + model + prompt_version (spec section 7)."""
    return hashlib.sha256(f"{file_sha}|{rubric_sha}|{model}|{PROMPT_VERSION}".encode()).hexdigest()


def _normalise(result: str, is_pen: bool) -> str:
    r = (result or "").lower()
    if is_pen:
        return "triggered" if r in ("triggered", "fail") else "not_triggered"
    return "pass" if r in ("pass", "not_triggered") else "fail"


def grade_file(version_id: int, file_id: int,
               call: Callable[..., dict[str, Any]] | None = None,
               progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Grade one file of a version (both passes) and store the results.

    `call` defaults to llm.call_json; tests pass a stand-in. Re-grading the same
    file, rubric, model and prompt version reuses stored results.
    """
    from db.models import BaselineLock, Criterion, File, Grade
    from db.session import session_scope

    call = call or llm.call_json
    model = settings.grader_model()
    with session_scope() as s:
        f = s.get(File, file_id)
        if f is None or f.version_id != version_id:
            raise ValueError("That file isn't part of this version")
        if s.query(BaselineLock).filter_by(file_id=file_id).first():
            raise ValueError("This file's baseline is locked; unlock it before re-grading")
        prompt_file = s.query(File).filter_by(version_id=version_id, role="prompt").first()
        rubric_file = s.query(File).filter_by(version_id=version_id, role="rubric").first()
        crits = [dict(crit_id=c.crit_id, subtask=c.subtask, title=c.title, text=c.text, points=c.points,
                      category=c.category) for c in s.query(Criterion).filter_by(version_id=version_id)]
        file_name, file_sha, text_path = f.name, f.sha256, f.text_path
        prompt_text = load_text(prompt_file.text_path) if prompt_file else ""
        rubric_sha = rubric_file.sha256 if rubric_file else ""

    if not crits:
        raise ValueError("This version has no rubric criteria")
    # llm.call_json caches on a hash of the full request (file text, rubric text, model, prompt
    # version), which is the spec's cache key in effect; cache_key() names it for display.
    file_text = load_text(text_path)

    subtasks: dict[str, list[dict[str, Any]]] = {}
    for c in crits:
        subtasks.setdefault(c["subtask"], []).append(c)

    passes: dict[int, dict[str, dict[str, Any]]] = {1: {}, 2: {}}
    for pass_no in (1, 2):
        for sub, sc in subtasks.items():
            ordered = sc if pass_no == 1 else list(reversed(sc))
            if progress:
                progress(f"Pass {pass_no}, section {sub}")
            user = build_prompt(prompt_text, sub, sc[0]["title"], ordered, file_name, file_text)
            out = call(f"grade_p{pass_no}", PROMPT_VERSION,
                       "Return only JSON that matches the schema.", user,
                       GRADE_SCHEMA, model=model)
            for g in out.get("grades", []):
                passes[pass_no][str(g.get("id"))] = g

    with session_scope() as s:
        s.query(Grade).filter_by(file_id=file_id).delete()
        for c in crits:
            is_pen = scoring.is_penalty(c["points"], c["category"])
            for pass_no in (1, 2):
                g = passes[pass_no].get(c["crit_id"])
                s.add(Grade(
                    version_id=version_id, file_id=file_id, crit_id=c["crit_id"], pass_no=pass_no,
                    result=_normalise(g["result"], is_pen) if g else None,
                    evidence=json.dumps({"evidence": g.get("evidence", ""), "note": g.get("note", "")})
                    if g else json.dumps({"evidence": "", "note": "missing from the grader's answer"}),
                    confidence=(g.get("confidence") or "low") if g else "low",
                ))
    return review_lines(version_id, file_id)


def review_lines(version_id: int, file_id: int) -> dict[str, Any]:
    """Per-criterion lines for the review screen, with the reasons each needs review."""
    from db.models import BaselineLock, Criterion, File, Grade
    from db.session import session_scope

    with session_scope() as s:
        f = s.get(File, file_id)
        is_gold = f.role == "gold"
        crits = s.query(Criterion).filter_by(version_id=version_id).order_by(Criterion.id).all()
        grades = s.query(Grade).filter_by(file_id=file_id).all()
        locked = s.query(BaselineLock).filter_by(file_id=file_id).first() is not None
        by = {(g.crit_id, g.pass_no): g for g in grades}
        lines = []
        for c in crits:
            g1, g2 = by.get((c.crit_id, 1)), by.get((c.crit_id, 2))
            if g1 is None and g2 is None:
                continue
            reasons = []
            r1 = g1.result if g1 else None
            r2 = g2.result if g2 else None
            if r1 != r2:
                reasons.append("passes disagree")
            if (g1 and g1.confidence == "low") or (g2 and g2.confidence == "low"):
                reasons.append("low confidence")
            if r1 is None or r2 is None:
                reasons.append("missing from a pass")
            suggested = r1 if r1 == r2 else (r1 or r2)
            if is_gold and "fail" in (r1, r2):
                reasons.append(GOLD_FAIL_LABEL)
            if is_gold and "triggered" in (r1, r2):
                reasons.append(f"penalty on gold: {GOLD_FAIL_LABEL}")
            ev = json.loads(g1.evidence) if g1 and g1.evidence else {}
            ev2 = json.loads(g2.evidence) if g2 and g2.evidence else {}
            lines.append({
                "crit_id": c.crit_id, "subtask": c.subtask, "text": c.text, "points": c.points,
                "category": c.category, "pass1": r1, "pass2": r2,
                "confidence": "/".join(x for x in ((g1.confidence if g1 else ""), (g2.confidence if g2 else ""))),
                "evidence": ev.get("evidence", ""), "note": ev.get("note", ""),
                "evidence2": ev2.get("evidence", ""),
                "suggested": suggested, "final": g1.final_result if g1 else None,
                "reviewed_by": g1.reviewed_by if g1 else None,
                "needs_review": bool(reasons), "reasons": reasons,
            })
    return {"file_id": file_id, "is_gold": is_gold, "locked": locked, "lines": lines}


def set_final(file_id: int, crit_id: str, result: str, user: str) -> None:
    """Confirm or override one line (audited)."""
    from db.models import BaselineLock, Grade
    from db.session import audit, session_scope

    with session_scope() as s:
        if s.query(BaselineLock).filter_by(file_id=file_id).first():
            raise ValueError("Baseline is locked")
        g = s.query(Grade).filter_by(file_id=file_id, crit_id=crit_id, pass_no=1).first()
        if g is None:
            raise ValueError(f"No grade for {crit_id}")
        before = {"final_result": g.final_result, "suggested": g.result}
        if g.final_result != result:
            g.final_result = result
            g.reviewed_by = user
            audit(s, user, "grades.final_result", f"{file_id}:{crit_id}", before, {"final_result": result})


def confirm_all_unreviewed(file_id: int, user: str, only_clean: bool = True) -> int:
    """Accept the suggested result for lines that don't need review (or all, if only_clean=False)."""
    data = review_lines(_version_of(file_id), file_id)
    n = 0
    for ln in data["lines"]:
        if ln["final"] is None and ln["suggested"] and (not ln["needs_review"] or not only_clean):
            set_final(file_id, ln["crit_id"], ln["suggested"], user)
            n += 1
    return n


def _version_of(file_id: int) -> int:
    from db.models import File
    from db.session import session_scope

    with session_scope() as s:
        return s.get(File, file_id).version_id


def lock_baseline(file_id: int, user: str) -> None:
    from db.models import BaselineLock
    from db.session import audit, session_scope

    data = review_lines(_version_of(file_id), file_id)
    open_lines = [ln["crit_id"] for ln in data["lines"] if ln["final"] is None]
    if open_lines:
        raise ValueError(f"{len(open_lines)} line(s) still need a confirmed result")
    with session_scope() as s:
        if not s.query(BaselineLock).filter_by(file_id=file_id).first():
            s.add(BaselineLock(version_id=_version_of(file_id), file_id=file_id, locked_by=user))
            audit(s, user, "baseline_locks", file_id, None, {"locked": True})


def unlock_baseline(file_id: int, user: str) -> None:
    from db.models import BaselineLock
    from db.session import audit, session_scope

    with session_scope() as s:
        s.query(BaselineLock).filter_by(file_id=file_id).delete()
        audit(s, user, "baseline_locks", file_id, {"locked": True}, None)


def file_score(version_id: int, file_id: int, use_final_only: bool = False) -> dict[str, Any]:
    """Score from final results where set, else the suggested (agreed) result."""
    from db.models import Criterion
    from db.session import session_scope

    data = review_lines(version_id, file_id)
    results = {}
    for ln in data["lines"]:
        results[ln["crit_id"]] = ln["final"] if (ln["final"] or use_final_only) else ln["suggested"]
    with session_scope() as s:
        crits = [dict(crit_id=c.crit_id, points=c.points, category=c.category)
                 for c in s.query(Criterion).filter_by(version_id=version_id)]
    return scoring.score(crits, results, settings.get("SCORE_FORMULA"))
