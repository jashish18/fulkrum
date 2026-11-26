"""Phase 5: decisions written back into a copy of the report (spec 13, P5)."""
import io
import zipfile

import openpyxl
from openpyxl.xml.functions import tostring
import pytest

from core import impact, writeback
from core.report_parser import report_original_path
from tests.test_p4 import _decide, t35  # noqa: F401  (fixture)

STYLE_ATTRS = ("font", "fill", "border", "alignment", "protection", "number_format")


def _style(c, a):
    """Compare styles by their XML, since two loads give different proxy objects."""
    v = getattr(c, a)
    return v if isinstance(v, str) else tostring(v.to_tree())


def _cell_sig(c):
    return (c.value,) + tuple(_style(c, a) for a in STYLE_ATTRS)


def _sheet_sig(ws):
    return {
        "dims": {k: (d.width, d.hidden, d.outlineLevel) for k, d in ws.column_dimensions.items()},
        "rows": {k: (d.height, d.hidden, d.outlineLevel) for k, d in ws.row_dimensions.items()},
        "merged": sorted(str(r) for r in ws.merged_cells.ranges),
        "freeze": ws.freeze_panes,
        "dv": sorted((dv.formula1, str(dv.sqref)) for dv in ws.data_validations.dataValidation),
    }


def _decide_some(rid):
    st = impact.load_state(rid)
    _decide(st, "2.1", "2", scope=["6.13", "6.14", "6.15"], note="Strongest defence for Ravi")
    _decide(st, "2.12", "keep")
    _decide(st, "1b.13", "agree", note="Fix the record")
    _decide(st, "1b.1", "disagree")
    _decide(st, "1a.1", "disagree")
    _decide(st, "2.3", "2", source="expert")  # working call only: must not be written


def test_decisions_written_and_nothing_else_changes(t35):  # noqa: F811
    rid = t35
    _decide_some(rid)
    data, name, info = writeback.build_decisions_workbook(rid)
    assert name.startswith("batch_2_rework_checks_35 - by part - decisions ") and name.endswith(".xlsx")

    orig = openpyxl.load_workbook(report_original_path(rid))
    new = openpyxl.load_workbook(io.BytesIO(data))
    assert new.sheetnames == orig.sheetnames + [writeback.SUMMARY_SHEET]

    written = {w["cell"]: w["value"] for w in info["written"]}
    # The decisions are in the Part tabs.
    vals = set(written.values())
    assert "Option 2 (6.13–6.15)" in vals
    assert "Strongest defence for Ravi" in vals
    assert "Option 1" in vals          # 2.12 kept -> its keep-as-is option
    assert "Agree" in vals and "Disagree" in vals and "Hold" in vals
    assert not any(w["card"] == "2.3" for w in info["written"])
    for ref, v in written.items():
        sheet, addr = ref.split("!")
        assert sheet in ("Part 2", "Part 1b", "Part 1a")
        assert new[sheet][addr].value == v

    # Every other cell, and all formatting, is unchanged.
    diffs = []
    for sh in orig.sheetnames:
        a, b = orig[sh], new[sh]
        assert _sheet_sig(a) == _sheet_sig(b), sh
        rows = max(a.max_row, b.max_row)
        cols = max(a.max_column, b.max_column)
        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                ca, cb = a.cell(r, c), b.cell(r, c)
                key = f"{sh}!{ca.coordinate}"
                if key in written:
                    assert _style(cb, "font") == _style(ca, "font") and _style(cb, "fill") == _style(ca, "fill")
                    continue
                if _cell_sig(ca) != _cell_sig(cb):
                    diffs.append(key)
    assert diffs == []

    # Formulas on the overview sheet are still formulas.
    ov = new["Task 35"]
    assert any(isinstance(c.value, str) and c.value.startswith("=IF('Part 2'!") for row in ov.iter_rows()
               for c in row)


def test_original_untouched(t35):  # noqa: F811
    p = report_original_path(t35)
    before = p.read_bytes()
    _decide_some(t35)
    writeback.build_decisions_workbook(t35)
    assert p.read_bytes() == before


def test_summary_sheet(t35):  # noqa: F811
    _decide_some(t35)
    data, _, _ = writeback.build_decisions_workbook(t35)
    ws = openpyxl.load_workbook(io.BytesIO(data))[writeback.SUMMARY_SHEET]
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = next(i for i, r in enumerate(rows) if r and r[0] == "Card")
    hdr = rows[hdr_i]
    assert list(hdr) == ["Card", "Part", "Interpreted QA option", "Final decision", "Note", "Flags",
                         "Projected model %", "Projected gold %", "Re-run"]
    body = {r[0]: dict(zip(hdr, r)) for r in rows[hdr_i + 1:] if r and r[0]}
    assert len(body) == 39
    assert body["2.1"]["Interpreted QA option"] == "Option 2 (6.13, 6.14, 6.15)"
    assert body["2.1"]["Final decision"].startswith("Option 2")
    assert "CONFLICT" in body["1b.13"]["Flags"]
    assert body["1b.13"]["Re-run"] == "yes"


def test_formula_cells_left_alone(t35, monkeypatch):  # noqa: F811
    """If an Expert call cell holds a formula, it is skipped, not overwritten."""
    p = report_original_path(t35)
    wb = openpyxl.load_workbook(p)
    ws = wb["Part 2"]
    hdr = {c.value: c.column for c in ws[3]}
    ws.cell(5, hdr["Expert call"]).value = "=1+1"  # card 2.1's lead row
    wb.save(p)
    _decide_some(t35)
    data, _, info = writeback.build_decisions_workbook(t35)
    assert openpyxl.load_workbook(io.BytesIO(data))["Part 2"].cell(5, hdr["Expert call"]).value == "=1+1"
    assert any("formula" in s for s in info["skipped"])
