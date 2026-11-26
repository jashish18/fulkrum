"""Phase 2 acceptance tests against the sample rework reports (spec section 13, P2)."""
import json

import pytest

from core.report_parser import ReportError, parse_qa_sheet, parse_report, save_report
from tests.conftest import SAMPLES


def _report_path(task):
    if not SAMPLES.exists():
        pytest.skip("samples/ folder not present")
    p = SAMPLES / f"batch_2_rework_checks_{task} - by part.xlsx"
    if not p.exists():
        pytest.skip(f"no rework report for task {task}")
    return p


def _parse(task):
    p = _report_path(task)
    return parse_report(p.read_bytes(), p.name)


def test_task45_counts():
    r = _parse("45")
    assert r.flagged_version == "v36"
    c = r.counts()
    assert (c["rows"], c["groups"]) == (36, 25)
    assert c["parts"] == {"2": {"groups": 10, "rows": 11}, "1b": {"groups": 11, "rows": 17},
                          "1a": {"groups": 4, "rows": 8}}


def test_task35_counts():
    r = _parse("35")
    assert r.flagged_version == "v20"
    c = r.counts()
    assert (c["rows"], c["groups"]) == (59, 39)
    assert c["parts"] == {"2": {"groups": 12, "rows": 17}, "1b": {"groups": 21, "rows": 26},
                          "1a": {"groups": 6, "rows": 16}}


def _qa_cards(r):
    return [c["code"] for c in r.cards if c["part"] == "2" and any(x["QA Comments"] for x in c["rows"])]


def test_qa_input():
    assert len(_qa_cards(_parse("35"))) == 12
    assert _qa_cards(_parse("45")) == []


def test_task35_card_2_1():
    r = _parse("35")
    card = next(c for c in r.cards if c["code"] == "2.1")
    assert len(card["rows"]) == 3
    lead = next(x for x in card["rows"] if x["is_lead"])
    assert lead["Category and row"] == "Category 1, row 2"
    assert sorted(k for k in lead["parsed"]["options"] if k in "123") == ["1", "2", "3"]
    stances = lead["prior_stances"]
    assert sorted(s["qa_no"] for s in stances) == [81, 85, 96, 97, 98, 99]
    assert {s["status"] for s in stances} == {"Objected"}


def test_close_calls_and_links():
    r = _parse("35")
    close = [c["code"] for c in r.cards if c["part"] == "1b"
             and next(x for x in c["rows"] if x["is_lead"])["parsed"]["close_call"]]
    assert len(close) == 13  # "13 close calls" in the Part 1b title row
    assert ("1b.2", "1b.10", "decide_together") in r.links
    assert ("1b.10", "1b.2", "decide_together") in r.links


def test_every_row_parsed_without_llm():
    for t in ("45", "35"):
        r = _parse(t)
        assert all(x["parse_method"] == "regex" for x in r.rows)


def test_count_mismatch_fails(tmp_path):
    import io
    import openpyxl

    p = _report_path("45")
    wb = openpyxl.load_workbook(p)
    ws = wb["Part 1a"]
    ws.delete_rows(ws.max_row)  # drop one row
    b = io.BytesIO()
    wb.save(b)
    with pytest.raises(ReportError, match="don't match"):
        parse_report(b.getvalue(), p.name)


def test_save_and_reload():
    from db.models import Card, CardLink, Option, PriorStance, Row
    from db.session import session_scope

    r = _parse("35")
    rid = save_report(r, use_llm=False)
    with session_scope() as s:
        cards = s.query(Card).filter_by(report_id=rid).all()
        assert len(cards) == 39
        card = next(c for c in cards if c.code == "2.1")
        rows = s.query(Row).filter_by(card_id=card.id).all()
        assert len(rows) == 3
        lead = s.get(Row, card.lead_row_id)
        assert lead.category_row == "Category 1, row 2"
        assert s.query(Option).filter_by(row_id=lead.id).count() == 3
        assert s.query(PriorStance).filter_by(row_id=lead.id).count() == 6
        assert s.query(CardLink).filter_by(kind="decide_together").count() == 6
    # Importing again replaces, never duplicates.
    assert save_report(_parse("35"), use_llm=False) == rid
    with session_scope() as s:
        assert s.query(Card).filter_by(report_id=rid).count() == 39


def test_prior_qa_sheet():
    import zipfile

    z = SAMPLES / "35-sampling-department-review.zip"
    if not z.exists():
        pytest.skip("no task 35 zip")
    with zipfile.ZipFile(z) as zf:
        name = next(n for n in zf.namelist() if n.endswith("qa_feedback_35_grouped.xlsx"))
        items = parse_qa_sheet(zf.read(name), name)
    by_no = {i["qa_no"]: i for i in items}
    assert by_no[81]["status"] == "Objected"
    assert by_no[81]["qa_comment"].startswith("Kept 'warned'")
