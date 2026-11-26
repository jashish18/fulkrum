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
    for needed in ("prompt", "rubric", "gold", "model_output", "trajectory", "failure_notes"):
        assert needed in roles.values(), f"no {needed} suggested: {roles}"


@pytest.mark.parametrize("task", ["45", "35"])
def test_metadata_names(task):
    _, vid = _ingest(task)
    assert _names_found(vid), "expected personal names in file properties"
