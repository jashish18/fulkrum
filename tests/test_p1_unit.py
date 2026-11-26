"""Phase 1 unit tests on a synthetic task zip (no samples needed)."""
import datetime as dt
import io
import json
import zipfile

import openpyxl
import docx

from core.extract import extract_xlsx, looks_like_person, render_display
from core.ingest import save_version, split_failure_notes, stage_zip


def _xlsx_bytes(creator="Jane Smith", width=None, value=4261166.79):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Model"
    ws["A1"] = "Revenue"
    ws["B6"] = 100
    ws["C5"] = 0.05
    ws["B7"] = "=B6*(1+C5)"
    ws["D2"] = value
    ws["D2"].number_format = "#,##0"
    if width:
        ws.column_dimensions["D"].width = width
    hidden = wb.create_sheet("Calc")
    hidden.sheet_state = "hidden"
    hidden["A1"] = 1
    wb.properties.creator = creator
    wb.properties.lastModifiedBy = "openpyxl"
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _docx_bytes(author="Ravi Kumar"):
    d = docx.Document()
    d.add_paragraph("Reference memo")
    d.core_properties.author = author
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def _task_zip():
    rubric = {
        "total_points": 10,
        "rubric_sections": [
            {"subtask": "1", "title": "Build", "weight": 6, "criteria": [
                {"id": "1.1", "criterion": "Shows revenue", "points": 4, "category": "Accuracy",
                 "references": [{"source": "Gold Model.xlsx", "locator": "B7", "type": "gold_deliverable"}]},
                {"id": "1.2", "criterion": "Uses memo", "points": 3, "category": "Accuracy",
                 "references": [{"source": "Memo.docx", "locator": "p1", "type": "reference_file"}]},
                {"id": "1.3", "criterion": "Hardcodes", "points": -1, "category": "Penalty", "references": []},
            ]},
            {"subtask": "2", "title": "Explain", "weight": 4, "criteria": [
                {"id": "2.1", "criterion": "Explains", "points": 4, "category": "Clarity", "references": []},
            ]},
        ],
    }
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("Memo.docx", _docx_bytes())
        z.writestr("data.csv", "a,b\n1,2\n")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("Task 99 v12/prompt.txt", "Build the model.")
        z.writestr("Task 99 v12/rubric.json", json.dumps(rubric))
        z.writestr("Task 99 v12/task99-gold-trajectory.md", "Step 1.1 do it")
        z.writestr("Task 99 v12/where-model-failed.md", "1. Model missed revenue.\n2. Model hardcoded.\n")
        z.writestr("Task 99 v12/Gold Model.xlsx", _xlsx_bytes())
        z.writestr("Task 99 v12/Claude Output.xlsx", _xlsx_bytes(creator="Claude"))
        z.writestr("Task 99 v12/Reference files.zip", inner.getvalue())
        z.writestr("Task 99 v12/__MACOSX/._prompt.txt", "junk")
    return outer.getvalue()


def test_display_formats():
    assert render_display(4261166.79, "#,##0") == "4,261,167"
    assert render_display(0.1234, "0.0%") == "12.3%"
    assert render_display(0.125, "0%") == "13%"
    assert render_display(12.345, "0.00") == "12.35"
    assert render_display(1234.56, "#,##0.0") == "1,234.6"
    assert render_display(2.5, "0") == "3"
    assert render_display(dt.datetime(2026, 6, 1), "yyyy-mm-dd") == "2026-06-01"
    assert render_display("text", "General") == "text"
    assert render_display(7.0, "General") == "7"


def test_person_heuristic():
    assert looks_like_person("Jane Smith")
    assert looks_like_person("asmith")
    assert not looks_like_person("openpyxl")
    assert not looks_like_person("Microsoft Office User")
    assert not looks_like_person("")


def test_stage_and_roles():
    st = stage_zip(_task_zip(), "Task 99 v12.zip")
    assert st.suggested_label == "v12"
    assert st.suggested_task == "99"
    roles = {f.name: f.suggested_role for f in st.files}
    assert roles["prompt.txt"] == "prompt"
    assert roles["rubric.json"] == "rubric"
    assert roles["task99-gold-trajectory.md"] == "trajectory"
    assert roles["where-model-failed.md"] == "failure_notes"
    assert roles["Gold Model.xlsx"] == "gold"
    assert roles["Claude Output.xlsx"] == "model_output"
    assert roles["Reference files/Memo.docx"] == "reference"
    assert roles["Reference files/data.csv"] == "reference"
    assert not any("__MACOSX" in n for n in roles)


def test_save_version_and_metadata():
    from db.models import Criterion, FailureNote, File
    from db.session import session_scope

    st = stage_zip(_task_zip(), "Task 99 v12.zip")
    vid = save_version(st, "99", "v12", {f.name: f.role for f in st.files})
    with session_scope() as s:
        assert s.query(Criterion).filter_by(version_id=vid).count() == 4
        notes = s.query(FailureNote).filter_by(version_id=vid).all()
        assert len(notes) == 2
        files = {f.name: f for f in s.query(File).filter_by(version_id=vid)}
        gold_meta = json.loads(files["Gold Model.xlsx"].metadata_json)
        assert {"field": "creator", "value": "Jane Smith"} in gold_meta["personal_names"]
        memo_meta = json.loads(files["Reference files/Memo.docx"].metadata_json)
        assert memo_meta["personal_names"][0]["value"] == "Ravi Kumar"
        out_meta = json.loads(files["Claude Output.xlsx"].metadata_json)
        assert out_meta["personal_names"] == []
    # Same zip again reuses the version.
    assert save_version(stage_zip(_task_zip(), "Task 99 v12.zip"), "99", "v12",
                        {f.name: f.role for f in st.files}) == vid


def test_xlsx_extraction(tmp_path):
    p = tmp_path / "w.xlsx"
    p.write_bytes(_xlsx_bytes(width=4))
    ex = extract_xlsx(p)
    model = ex["sheets"][0]
    cells = {c["addr"]: c for c in model["cells"]}
    assert cells["B7"]["formula"] == "=B6*(1+C5)"
    assert cells["D2"]["display"] == "4,261,167"
    assert cells["D2"].get("probable_hashes")
    assert ex["hidden_sheets"] == ["Calc"]
    assert ex.get("recalculation_needed")  # openpyxl writes no cached values


def test_failure_note_split():
    assert len(split_failure_notes("1. a\n2. b\n3. c")) == 3
    assert len(split_failure_notes("- a\n- b")) == 2
    assert len(split_failure_notes("para one\n\npara two")) == 2


def test_bad_zip_message():
    import pytest

    with pytest.raises(ValueError, match="not a valid zip"):
        stage_zip(b"not a zip", "x.zip")
