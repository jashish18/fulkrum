"""Phase 1 acceptance tests against the real samples (spec section 13, P1).

Skipped automatically when samples/ is not present.
"""
import json

import pytest

from core.ingest import rubric_summary, save_version, stage_zip
from tests.conftest import sample_zip


def _ingest(task):
    p = sample_zip(task)
    st = stage_zip(p.read_bytes(), p.name)
    assert st.rubric is not None, st.rubric_error
    vid = save_version(st, task, st.suggested_label or "v0", {f.name: f.role for f in st.files})
    return st, vid


def _names_found(vid):
    from db.models import File
    from db.session import session_scope

    with session_scope() as s:
        out = []
        for f in s.query(File).filter_by(version_id=vid):
            out += json.loads(f.metadata_json or "{}").get("personal_names", [])
        return out


def _roles(st):
    return {f.name: f.suggested_role for f in st.files}


def test_task45_rubric():
    st, _ = _ingest("45")
    rs = rubric_summary(st.rubric)
    assert rs["criteria"] == 94
    assert rs["total_points"] == 193
    assert rs["sum_section_weights"] == 193


def test_task35_rubric():
    st, _ = _ingest("35")
    rs = rubric_summary(st.rubric)
    assert rs["criteria"] == 117
    assert rs["penalties"] == 7
    assert rs["total_points"] == 280


@pytest.mark.parametrize("task", ["45", "35"])
def test_roles(task):
    st, _ = _ingest(task)
    roles = _roles(st)
    for needed in ("prompt", "rubric", "gold", "model_output", "trajectory", "failure_notes", "qa_sheet"):
        assert needed in roles.values(), f"no {needed} suggested: {roles}"
    assert st.suggested_task == task
    gold = [n for n, r in roles.items() if r == "gold"]
    model = [n for n, r in roles.items() if r == "model_output"]
    expected = {"45": ("Surety Bond Projections.xlsx", "Claude Output.xlsx"),
                "35": ("Sampling dept. summary.xlsx", "Rise_Apparel_Sampling_Review_Jun2026.xlsx")}[task]
    assert [g.rsplit("/", 1)[-1] for g in gold] == [expected[0]]
    assert [m.rsplit("/", 1)[-1] for m in model] == [expected[1]]


@pytest.mark.parametrize("task,count", [("45", 2), ("35", 4)])
def test_failure_notes(task, count):
    from db.models import FailureNote
    from db.session import session_scope

    _, vid = _ingest(task)
    with session_scope() as s:
        assert s.query(FailureNote).filter_by(version_id=vid).count() == count


@pytest.mark.parametrize("task", ["45", "35"])
def test_metadata_names(task):
    _, vid = _ingest(task)
    assert _names_found(vid), "expected personal names in file properties"
