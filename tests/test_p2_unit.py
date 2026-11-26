"""Unit tests for the finding text parser."""
from core.finding_parser import extract_refs, option_scope, parse_defense, parse_finding, split_sections

TEXT = """In short: Something is off.

Context:
1) A thing.

Issue:
It matters. Hence:
- Option 1: Add a sentence to the prompt.
- Option 2: Accept both answers in [6.12] to [6.15].
- Option 3: Keep as is.

Impacted rubric criterion:
- [6.12] (6 points): "Warns Ravi."
- (Options 2 and 3) The row on Step 6.4 (Category 2, row 39) still needs rewording.

Impacted gold deliverable:
- Gold.xlsx 'Summary'!L5, L7 and E15:H15 hold the values; also D44.

Impacted prompt:
- Under Option 1 the sentence goes at the end.
"""


def test_sections_and_options():
    p = parse_finding(TEXT, "2", ["Gold deliverables/Gold.xlsx"])
    assert list(p["options"]) == ["1", "2", "3"]
    assert p["complete"]
    assert p["sections"]["In short"] == "Something is off."
    scopes = {i["text"][:12]: i["option_scope"] for i in p["impacts"]}
    assert scopes["[6.12] (6 po"] is None
    assert scopes["The row on S"] == "2,3"
    assert scopes["Under Option"] == "1"
    refs = p["refs"]
    assert refs["criteria"][0] == {"id": "6.12", "points": 6.0}
    assert ["6.12", "6.13", "6.14", "6.15"] in refs["criteria_ranges"]
    assert refs["steps"] == ["6.4"]
    assert {"sheet": "Summary", "ref": "L5"} in refs["cells"]
    assert {"sheet": "Summary", "ref": "E15:H15"} in refs["cells"]
    assert {"sheet": None, "ref": "D44"} in refs["cells"]
    assert refs["files"] == ["Gold deliverables/Gold.xlsx"]
    assert refs["row_xrefs"] == ["Category 2, row 39"]


def test_fix_rows():
    p = parse_finding("In short: x\nProposed fix: Lower it.\nWhy: y\nAsk the expert: Should we?", "1b")
    assert p["options"] == {"fix": "Lower it."} and p["close_call"] and p["complete"]
    p = parse_finding("In short: x\nFix: Split it.", "1a")
    assert p["options"] == {"fix": "Split it."} and not p["close_call"]
    assert not parse_finding("In short: nothing to do", "1b")["complete"]


def test_scope_prefix_variants():
    assert option_scope("(Option 2) x")[0] == "2"
    assert option_scope("(Options 1 and 3) x")[0] == "1,3"
    assert option_scope("x under Options 1 and 3")[0] == "1,3"
    assert option_scope("plain")[0] is None


def test_defense_both_quote_styles():
    raw = ('QA #81 (Status: Objected) · QA comment: “Kept it.”\n\n'
           'QA #85 (Status: Partly fixed) · QA comment: "Kept the note (as is)."')
    out = parse_defense(raw)
    assert [(d["qa_no"], d["status"]) for d in out] == [(81, "Objected"), (85, "Partly fixed")]
    assert out[1]["comment"] == "Kept the note (as is)."


def test_negative_points_and_no_false_cells():
    r = extract_refs("[7.19] (−5 points) in FY26 and Q1 for rule AG-B2")
    assert r["criteria"] == [{"id": "7.19", "points": -5.0}]
    assert r["cells"] == []


def test_hence_inline_only():
    s = split_sections("Issue:\nThe word Hence: appears inline.")
    assert "Hence" in s
