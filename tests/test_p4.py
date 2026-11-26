"""Phase 4 acceptance scenarios (spec 13, P4) on the sample tasks.

Baseline grades come from a stand-in grader (the real grader needs an OpenRouter
key). Ops are entered as a user would confirm them on the card page; the
failure-note maps follow the spec's own example for task 45.
"""
import pytest

from core import grading, impact
from core.ingest import save_version, stage_zip
from core.report_parser import parse_report, save_report
from tests.conftest import SAMPLES, sample_zip
from tests.test_p3 import T45_MODEL_FAILS, fake_grader

T35_MODEL_FAILS = {"6.12", "6.13", "6.14", "6.15"}


def _setup(task, label, model_fails):
    from db.models import File, Report
    from db.session import session_scope

    p = sample_zip(task)
    st = stage_zip(p.read_bytes(), p.name)
    vid = save_version(st, task, label, {f.name: f.role for f in st.files})
    with session_scope() as s:
        mf = s.query(File).filter_by(version_id=vid, role="model_output").first().id
        gf = s.query(File).filter_by(version_id=vid, role="gold").first().id
    grading.grade_file(vid, mf, call=fake_grader(fails=model_fails))
    grading.grade_file(vid, gf, call=fake_grader())
    rp = SAMPLES / f"batch_2_rework_checks_{task} - by part.xlsx"
    rid = save_report(parse_report(rp.read_bytes(), rp.name), use_llm=False)
    return vid, rid


def _op(state, code, key, op):
    impact.add_op(state.cards[code].option_ids[key], op, "user", "tester", confirmed=True)


def _decide(state, code, key, scope=None, source="final", note=None):
    impact.save_decision(state.cards[code].id, source, key, "tester", scope=scope, note=note)


@pytest.fixture
def t45():
    vid, rid = _setup("45", "v36", T45_MODEL_FAILS)
    impact.set_failure_map(vid, {1: ["5.21"], 2: ["1.21", "3.12", "3.13", "3.15", "3.16", "4.11", "4.12",
                                                  "4.14", "4.15"]}, "tester")
    st = impact.load_state(rid)
    for cid in ["1.21", "3.12", "3.13", "3.15", "3.16", "4.11", "4.12", "4.14", "4.15"]:
        _op(st, "2.2", "2", {"type": "model_flip", "crit": cid, "to": "pass",
                             "reason": "under Option 2 it would pass them"})
    _op(st, "1b.1", "fix", {"type": "reweight", "crit": "4.15", "to": 4})
    _op(st, "1b.1", "fix", {"type": "reweight", "crit": "4.13", "to": 4})
    return rid


@pytest.fixture
def t35():
    vid, rid = _setup("35", "v20", T35_MODEL_FAILS)
    impact.set_failure_map(vid, {4: ["6.13", "6.14", "6.15"]}, "tester")
    st = impact.load_state(rid)
    for cid in ["6.12", "6.13", "6.14", "6.15"]:
        _op(st, "2.1", "2", {"type": "accept_alternative", "crit": cid, "value": "no warning",
                             "model_result": "pass"})
    impact.suggest_rule_ops(st)  # prompt/reference edits from the parsed impacts
    impact.interpret_all(impact.load_state(rid), use_llm=False)
    return rid


def test_baselines(t45):
    st = impact.load_state(t45)
    base = impact.baseline(st)
    assert base["model"]["score_pct"] == pytest.approx(176 / 209)
    assert base["gold"]["score_pct"] == pytest.approx(1.0)
    assert base["backing"][2] == 27


def test_a_card_2_2_option_2_breaks(t45):
    st = impact.load_state(t45)
    _decide(st, "2.2", "2")
    st = impact.load_state(t45)
    f = impact.compute_flags(st)
    assert f["projection"]["model"]["score_pct"] == pytest.approx(203 / 209)  # about 97%
    assert round(f["projection"]["model"]["score_pct"] * 100) == 97
    assert f["projection"]["backing"][2] == 0
    assert "BREAK" in f["cards"]["2.2"]
    assert any("failure note 2 has zero backing" in b for b in f["task_breaks"])


def test_b_card_1b_1_fix(t45):
    st = impact.load_state(t45)
    _decide(st, "1b.1", "agree")
    st = impact.load_state(t45)
    f = impact.compute_flags(st)
    assert f["baseline"]["backing"][2] == 27
    assert f["projection"]["backing"][2] == 20
    assert f["projection"]["model"]["score_pct"] < 0.95
    assert "BREAK" not in f["cards"]["1b.1"]
    # What-if gives the same answer without saving a decision.
    st2 = impact.load_state(t45)
    assert impact.what_if(st2, "1b.1", "fix")["backing"][2] == 20


def test_c_card_2_1_qa_input(t35):
    st = impact.load_state(t35)
    qi = st.cards["2.1"].decisions["qa_interpreted"]
    assert qi["option_key"] == "2"
    assert qi["scope"] == ["6.13", "6.14", "6.15"]
    f = impact.compute_flags(st)
    c = f["cards"]["2.1"]
    assert "AMBIGUOUS" in c and "Option 1" in c["AMBIGUOUS"]
    assert f["projection"]["backing"][4] == 0
    assert "BREAK" in c
    assert {97, 98, 99} <= set(c["REVERSAL"]["qa"])
    assert 96 not in c["REVERSAL"]["qa"]  # QA #96 is about 6.12, which stays as is
    assert f["projection"]["model_results"]["6.12"] == "fail"


def test_d_card_2_12_keep_vs_1b_13_agree(t35):
    st = impact.load_state(t35)
    assert st.cards["2.12"].decisions["qa_interpreted"]["option_key"] == "1"  # "This is OK" -> keep
    _decide(st, "1b.13", "agree")
    st = impact.load_state(t35)
    f = impact.compute_flags(st)
    assert "CONFLICT" in f["cards"]["2.12"]
    assert "CONFLICT" in f["cards"]["1b.13"]


def test_e_card_2_5_aligned_with_defense(t35):
    f = impact.compute_flags(impact.load_state(t35))
    assert "AMBIGUOUS" in f["cards"]["2.5"]


def test_f_reference_option_reruns(t35):
    st = impact.load_state(t35)
    assert impact.option_needs_rerun(st.cards["1b.13"], "fix")
    _decide(st, "1b.13", "agree")
    f = impact.compute_flags(impact.load_state(t35))
    assert "RERUN" in f["cards"]["1b.13"]
    assert "1b.13" in {r["card"] for r in f["rerun"]}


def test_awaiting_and_precedence(t45):
    st = impact.load_state(t45)
    f = impact.compute_flags(st)
    assert "AWAITING" in f["cards"]["2.2"]  # no QA input in task 45, no decision yet
    _decide(st, "2.2", "2", source="expert")
    _decide(st, "2.2", "3", source="final")
    st = impact.load_state(t45)
    assert impact.current_decision(st.cards["2.2"])["option_key"] == "3"


def test_linked_cards_inconsistent(t35):
    st = impact.load_state(t35)
    _decide(st, "1b.2", "agree")
    _decide(st, "1b.10", "disagree")
    f = impact.compute_flags(impact.load_state(t35))
    assert any("linked" in m for m in f["cards"]["1b.2"]["CONFLICT"])


def test_expert_call_parse():
    assert impact.parse_expert_call("Option 2 (6.13–6.15)", "2") == {"option_key": "2",
                                                                    "scope": ["6.13", "6.14", "6.15"]}
    assert impact.parse_expert_call("Agree", "1b")["option_key"] == "agree"
    assert impact.parse_expert_call("Edited: lower to 2", "1b")["new_option_text"] == "lower to 2"


def test_compare_alternative():
    assert impact.compare_alternative("G28 shown 10.0%", "15.0 or 10.0 percent") == "pass"
    assert impact.compare_alternative("C9 shown 12", "17 or 12") == "pass"
    assert impact.compare_alternative("C9 shown 13", "17 or 12") == "fail"
    assert impact.compare_alternative("no numbers here", "17") is None
