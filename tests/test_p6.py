"""Phase 6: verification of a revised version and integrity checks (spec 9, 13 P6)."""
import io
import json
import re
import zipfile

import openpyxl

from core import impact, verify
from core.ingest import save_version, stage_zip
from tests.conftest import sample_zip
from tests.test_p4 import _decide, t35, t45  # noqa: F401  (fixtures)


def _finals_45(rid):
    impact.suggest_rule_ops(impact.load_state(rid))  # adds the property-clearing ops of 1a.4
    st = impact.load_state(rid)
    _decide(st, "1b.1", "agree")   # reweight [4.15] and [4.13] to 4
    _decide(st, "1a.4", "agree")   # clear Author / Last Modified By of four files
    return impact.load_state(rid)


def _rezip(data: bytes, edit, prefix: str = "") -> bytes:
    """Rebuild a zip (and the zips and office files inside it), letting `edit(name, bytes)` change any
    member. A member nothing changes keeps its exact bytes, as a person's hand edit would."""
    src = zipfile.ZipFile(io.BytesIO(data))
    members, changed = [], False
    for info in src.infolist():
        b0 = src.read(info.filename)
        b = b0
        name = prefix + info.filename
        if not info.is_dir():
            if info.filename.lower().endswith(".zip"):
                b = _rezip(b, edit)
            elif info.filename.lower().endswith((".docx", ".xlsx")):
                b = _rezip(b, edit, prefix=f"{info.filename}::")
            b = edit(name, b)
        changed |= b != b0
        members.append((info, b))
    if not changed:
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info, b in members:
            z.writestr(info, b)
    return out.getvalue()


def _new_version(data: bytes, label: str) -> int:
    st = stage_zip(data, f"45-surety v{label}.zip")
    return save_version(st, "45", label, {f.name: f.role for f in st.files})


def test_same_zip_everything_missing_and_nothing_untracked(t45):  # noqa: F811
    rid = t45
    state = _finals_45(rid)
    data = sample_zip("45").read_bytes()
    vid = _new_version(data, "v37")
    assert vid != state.version_id
    r = verify.run(rid, vid, use_llm=False, user="tester")
    assert r["expected"], "final decisions should give expected changes"
    assert {e["result"] for e in r["expected"]} == {"missing"}
    assert r["untracked"] == []
    assert r["hunks"] == []
    # 1b.1's two reweights and 1a.4's property clearing are among them.
    descs = [(e["card"], e["description"]) for e in r["expected"]]
    assert ("1b.1", "Reweight [4.15] to 4 points") in descs
    assert any(c == "1a.4" and "Surety Market.docx" in d for c, d in descs)
    assert r["rerun"]["status"] in ("re-run pending", "not required")


def test_hand_edits_detected_and_matched(t45):  # noqa: F811
    rid = t45
    _finals_45(rid)

    def edit(name, b):
        if name.endswith("/rubric.json"):
            rub = json.loads(b.decode("utf-8-sig"))
            for sec in rub["rubric_sections"]:
                for c in sec["criteria"]:
                    if str(c["id"]) == "4.15":
                        c["points"] = 4
            return json.dumps(rub, indent=2).encode()
        if name.endswith("Surety Market.docx::docProps/core.xml"):
            return re.sub(rb"<dc:creator>[^<]*</dc:creator>", b"<dc:creator></dc:creator>", b)
        return b

    vid = _new_version(_rezip(sample_zip("45").read_bytes(), edit), "v37")
    r = verify.run(rid, vid, use_llm=False, user="tester")
    kinds = {(h["artifact"], h["kind"], h["desc"]) for h in r["hunks"]}
    assert any(a == "rubric" and k == "reweighted" and "[4.15]" in d for a, k, d in kinds)
    assert any(a == "reference" and k == "metadata" and "creator" in d for a, k, d in kinds)
    assert len(r["hunks"]) == 2

    by = {(e["card"], e["description"]): e for e in r["expected"]}
    pts = by[("1b.1", "Reweight [4.15] to 4 points")]
    assert pts["result"] == "addressed" and any("[4.15] reweighted 11 → 4" in h for h in pts["hunks"])
    assert by[("1b.1", "Reweight [4.13] to 4 points")]["result"] == "missing"
    meta = next(e for (c, d), e in by.items() if c == "1a.4" and "Surety Market.docx" in d)
    assert meta["result"] == "partial"           # Author cleared, Last Modified By still set
    assert any("creator" in h for h in meta["hunks"])
    others = [e for (c, d), e in by.items() if c == "1a.4" and "Surety Market.docx" not in d]
    assert others and all(e["result"] == "missing" for e in others)
    assert r["untracked"] == []                  # both edits are explained by cards


def test_untracked_change_and_override(t45):  # noqa: F811
    rid = t45
    _finals_45(rid)

    def edit(name, b):
        if name.endswith("/prompt"):
            return b + b"\nAlso show the CAGR."
        return b

    vid = _new_version(_rezip(sample_zip("45").read_bytes(), edit), "v37")
    r = verify.run(rid, vid, use_llm=False, user="tester")
    assert len(r["untracked"]) == 1 and r["untracked"][0]["artifact"] == "prompt"
    verify.set_untracked_status(r["untracked"][0]["id"], "flagged", "tester")
    e = r["expected"][0]
    verify.override(e["verification_id"], "needs_human", "tester")
    r2 = verify.run(rid, vid, use_llm=False, user="tester")  # re-running keeps the person's calls
    assert r2["untracked"][0]["status"] == "flagged"
    again = next(x for x in r2["expected"] if x["verification_id"] == e["verification_id"])
    assert again["result"] == "needs_human" and again["override_by"] == "tester" and again["auto"] == "missing"

    data, name = verify.build_workbook(rid, vid)
    assert name.startswith("Task 45 verification v36 to v37")
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Expected changes", "Untracked changes", "Integrity", "Re-run"]
    assert wb["Untracked changes"].max_row == 2


def test_integrity_checks(t35):  # noqa: F811
    from core.ingest import rubric_summary  # noqa: F401

    st = impact.load_state(t35)
    res = {c["check"]: c for c in verify.integrity(st.version_id)}
    assert res["no personal names in file metadata"]["ok"] is False
    assert "Kartik Dalal" in res["no personal names in file metadata"]["detail"]
    assert res["no xl/webextensions parts"]["ok"] is True
    steps = res["each subtask has 5–15 steps"]
    assert steps["ok"] is False and "Subtask 1: 4" in steps["detail"]   # matches report card 1a.1
    assert res["criterion ids unique and sequential within each section"]["ok"] is True
    for c in res.values():
        assert c["ok"] in (True, False, None)


def test_trajectory_parse_and_crosswalk():
    t = "Subtask 1: Prep\nStep 1.1: Read files.\nmore\nStep 1.2: Build tabs.\nSubtask 2: Rates\nStep 2.1: Rate."
    p = verify.parse_trajectory(t)
    assert p["subtasks"] == {"1": "Prep", "2": "Rates"}
    assert verify.step_counts(p) == {"1": 2, "2": 1}
    old = {"1.1": {"text": "Uses GDP of 3,838,889"}, "1.2": {"text": "Sums the four lines"}}
    new = {"1.1": {"text": "Sums the four lines"}, "1.2": {"text": "Uses GDP of 3,838,889"}}
    assert verify.crosswalk(old, new) == {"1.1": "1.2", "1.2": "1.1"}
