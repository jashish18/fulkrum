"""Phase 3: grading engine, review and scoring.

The stand-in grader replaces the LLM so these run without a key. The real-LLM
acceptance checks are at the bottom and run only when LLM_API_KEY is set.
"""
import re

import pytest

from core import grading, llm
from core.ingest import save_version, stage_zip
from core.scoring import score
from tests.conftest import sample_zip

T45_MODEL_FAILS = {"1.21", "3.12", "3.13", "3.15", "3.16", "4.11", "4.12", "4.14", "4.15", "5.14", "5.21"}


def _ingest(task):
    p = sample_zip(task)
    st = stage_zip(p.read_bytes(), p.name)
    vid = save_version(st, task, "v0", {f.name: f.role for f in st.files})
    return vid


def _file_id(vid, role):
    from db.models import File
    from db.session import session_scope

    with session_scope() as s:
        return s.query(File).filter_by(version_id=vid, role=role).first().id


def fake_grader(fails=(), triggered=(), disagree=(), low=()):
    calls = []

    def call(purpose, version, system, user, schema, model=None):
        calls.append((purpose, user))
        ids = re.findall(r"^- id (\S+) \((PENALTY|criterion)", user, re.M)
        p2 = purpose.endswith("p2")
        out = []
        for cid, kind in ids:
            if kind == "PENALTY":
                r = "triggered" if cid in triggered else "not_triggered"
            else:
                r = "fail" if cid in fails else "pass"
                if p2 and cid in disagree:
                    r = "pass" if r == "fail" else "fail"
            out.append({"id": cid, "result": r, "evidence": f"B7 shown 1,234 for {cid}",
                        "confidence": "low" if cid in low else "high", "note": ""})
        return {"grades": out}

    call.calls = calls
    return call


def test_score_formula():
    crits = [dict(crit_id="a", points=6, category="X"), dict(crit_id="b", points=4, category="X"),
             dict(crit_id="p", points=-2, category="Penalty")]
    r = score(crits, {"a": "pass", "b": "fail", "p": "triggered"})
    assert r["earned"] == 6 and r["penalty"] == 2 and r["positive_total"] == 10
    assert r["score_pct"] == pytest.approx(0.4)
    assert score(crits, {"a": "pass", "b": "fail", "p": "triggered"}, "earned_only")["score_pct"] == pytest.approx(0.6)


def test_task45_model_score_with_expected_fails():
    vid = _ingest("45")
    fid = _file_id(vid, "model_output")
    g = fake_grader(fails=T45_MODEL_FAILS)
    data = grading.grade_file(vid, fid, call=g)
    # 5 sections x 2 passes, second pass in reverse order
    assert len(g.calls) == 10
    first = re.findall(r"^- id (\S+)", g.calls[0][1], re.M)
    rev = re.findall(r"^- id (\S+)", g.calls[5][1], re.M)
    assert rev == list(reversed(first))
    assert len(data["lines"]) == 94
    assert not any(l["needs_review"] for l in data["lines"])
    sc = grading.file_score(vid, fid)
    assert set(sc["failed"]) == T45_MODEL_FAILS
    assert sc["score_pct"] == pytest.approx(176 / 209)  # about 84%
    assert round(sc["score_pct"] * 100) == 84


def test_review_flags_and_lock():
    from db.models import AuditLog
    from db.session import session_scope

    vid = _ingest("45")
    fid = _file_id(vid, "gold")
    grading.grade_file(vid, fid, call=fake_grader(fails={"1.1"}, disagree={"2.1"}, low={"3.1"}))
    lines = {l["crit_id"]: l for l in grading.review_lines(vid, fid)["lines"]}
    assert grading.GOLD_FAIL_LABEL in lines["1.1"]["reasons"]
    assert "passes disagree" in lines["2.1"]["reasons"]
    assert "low confidence" in lines["3.1"]["reasons"]
    with pytest.raises(ValueError):
        grading.lock_baseline(fid, "tester")
    n = grading.confirm_all_unreviewed(fid, "tester")
    assert n == 94 - 3
    grading.set_final(fid, "1.1", "pass", "tester")  # override: grader error
    grading.set_final(fid, "2.1", "pass", "tester")
    grading.set_final(fid, "3.1", "pass", "tester")
    sc = grading.file_score(vid, fid)
    assert sc["score_pct"] == pytest.approx(1.0)
    grading.lock_baseline(fid, "tester")
    assert grading.review_lines(vid, fid)["locked"]
    with pytest.raises(ValueError):
        grading.set_final(fid, "1.1", "fail", "tester")
    with session_scope() as s:
        assert s.query(AuditLog).filter_by(entity="grades.final_result").count() == 94


def test_settings_override_model(monkeypatch):
    from core import settings

    settings.put("GRADER_MODEL", "some/model", "tester")
    assert settings.grader_model() == "some/model"
    seen = {}

    def call(purpose, version, system, user, schema, model=None):
        seen["model"] = model
        return {"grades": []}

    vid = _ingest("45")
    grading.grade_file(vid, _file_id(vid, "model_output"), call=call)
    assert seen["model"] == "some/model"


# ---- real LLM acceptance (spec P3) -----------------------------------------
needs_llm = pytest.mark.skipif(not llm.is_configured(), reason="LLM_API_KEY not set")


@needs_llm
@pytest.mark.parametrize("task", ["45", "35"])
def test_real_gold_after_review(task):
    """Gold should score >= 99% once reviewed; here we check the grader's raw agreement."""
    vid = _ingest(task)
    fid = _file_id(vid, "gold")
    grading.grade_file(vid, fid)
    sc = grading.file_score(vid, fid)
    print(task, "gold", sc["score_pct"], sc["failed"], sc["triggered"])


@needs_llm
def test_real_task45_model():
    vid = _ingest("45")
    fid = _file_id(vid, "model_output")
    grading.grade_file(vid, fid)
    sc = grading.file_score(vid, fid)
    print("45 model", sc["score_pct"], sorted(sc["failed"]))
