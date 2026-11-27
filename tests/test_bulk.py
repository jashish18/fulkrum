"""Bulk upload and the all-tasks overview."""
import io
import zipfile

from core import bulk, overview
from tests.conftest import SAMPLES, sample_zip


def _sample_files():
    sample_zip("35")  # skips without samples/
    return [(p.name, p.read_bytes()) for p in sorted(SAMPLES.iterdir()) if p.suffix in (".zip", ".xlsx")]


def test_bulk_matches_saves_and_overview():
    files = _sample_files()
    plans, unmatched = bulk.group(files)
    assert unmatched == [] and set(plans) == {"35", "45"}
    for p in plans.values():
        bulk.check(p)
        assert p.status == "ready", p.problems + p.unsure_roles
    assert plans["35"].label == "v20" and plans["45"].label == "v36"
    assert plans["35"].label_from == "report"
    for p in plans.values():
        bulk.save(p, "tester", use_llm=False)
        assert p.status == "saved" and p.version_id and p.report_id

    import pickle

    pickle.dumps(overview.data_version())  # the Overview's cache key must be plain data
    tasks, cards = overview.build()
    assert [t["Task"] for t in tasks] == ["35", "45"]
    assert all(t["Stage"] == "to grade" for t in tasks)   # nothing graded yet
    t35 = tasks[0]
    assert t35["Cards"] == 39 and t35["Flagged at"] == "v20"
    assert "2:" in t35["Parts"] and "Category" in t35["Categories"]
    assert len([c for c in cards if c["Task"] == "35"]) == 39
    assert {c["Part"] for c in cards} >= {"2", "1b", "1a"}

    # Uploading the same files again keeps the saved reports (and their decisions).
    again, _ = bulk.group(files)
    p = bulk.check(again["35"])
    bulk.save(p, "tester", use_llm=False)
    assert p.report_id == plans["35"].report_id
    assert any("already saved" in n for n in p.notes)


def test_container_zip_and_unsure_roles():
    files = _sample_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files:
            z.writestr(f"all tasks/{n}", b)
    plans, unmatched = bulk.group([("all tasks.zip", buf.getvalue())])
    assert set(plans) == {"35", "45"} and all(p.zip_name and p.report_name for p in plans.values())

    # A task zip without a report and without a v-number needs a label.
    lone, _ = bulk.group([("35-sampling-department-review.zip", dict(files)["35-sampling-department-review.zip"])])
    p = bulk.check(lone["35"])
    assert p.status == "needs label" and "no rework report" in p.notes
