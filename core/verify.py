"""Step 2: verification of a revised version (spec 9) and integrity checks.

A revised version is paired with the baseline. Each artifact is diffed into
"hunks". The ops of the final decisions become expected changes, each with a
check. A hunk that no expected change explains is listed as untracked.
"""
from __future__ import annotations

import datetime as dt
import difflib
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from core import config, impact, settings
from core import ops as opsmod

RESULTS = ["addressed", "partial", "missing", "regressed", "needs_human"]
UNTRACKED_STATUSES = ["", "accepted", "flagged"]
CRIT_SIM = 85      # rubric crosswalk (spec 9)
PROMPT_SIM = 90    # prompt contains a sentence
MIN_STEPS, MAX_STEPS = 5, 15
SKIP_META = {"personal_names", "_error"}


# ---------------------------------------------------------------------------
# Version snapshot
# ---------------------------------------------------------------------------


@dataclass
class Snap:
    version_id: int
    label: str
    files: list[dict[str, Any]]
    criteria: dict[str, dict[str, Any]]
    crit_order: list[str]
    rubric: dict[str, Any] | None
    prompt: str
    trajectory: str
    notes: dict[int, str]
    gold_threshold: float = 0.99

    def by_role(self, role: str) -> list[dict[str, Any]]:
        return [f for f in self.files if f["role"] == role]

    def find(self, name: str) -> dict[str, Any] | None:
        want = _base(name)
        return next((f for f in self.files if f["name"] == name), None) or \
            next((f for f in self.files if _base(f["name"]) == want), None)


def _base(name: str | None) -> str:
    from core.ingest import _norm

    return _norm(name or "")


def snapshot(version_id: int, light: bool = False) -> Snap:
    """light=True skips the file extractions (enough for deriving expected changes)."""
    from core.ingest import load_extraction, load_rubric
    from db.models import Criterion, FailureNote, File, Version
    from db.session import session_scope

    with session_scope() as s:
        v = s.get(Version, version_id)
        label = v.label
        files = []
        for f in s.query(File).filter_by(version_id=version_id).order_by(File.name):
            d = load_extraction(f.text_path) if not light or f.role in ("prompt", "trajectory") else {}
            files.append({"id": f.id, "name": f.name, "role": f.role, "sha": f.sha256,
                          "meta": json.loads(f.metadata_json or "{}"), "parts": json.loads(f.parts_json or "{}"),
                          "ex": d.get("extraction") or {}, "text": d.get("text") or ""})
        crits, order = {}, []
        for c in s.query(Criterion).filter_by(version_id=version_id).order_by(Criterion.id):
            crits[c.crit_id] = {"points": c.points, "text": c.text, "subtask": c.subtask, "title": c.title,
                                "category": c.category, "references": json.loads(c.references_json or "[]")}
            order.append(c.crit_id)
        notes = {n.idx: n.text for n in s.query(FailureNote).filter_by(version_id=version_id)}
    rubric = None
    rf = next((f for f in files if f["role"] == "rubric"), None)
    if rf:
        p = config.data_path("versions", str(version_id), "files", rf["name"])
        try:
            rubric = load_rubric(p.read_bytes())
        except (OSError, ValueError):
            rubric = None
    text_of = {r: next((f["text"] for f in files if f["role"] == r), "") for r in ("prompt", "trajectory")}
    return Snap(version_id=version_id, label=label, files=files, criteria=crits, crit_order=order,
                rubric=rubric, prompt=text_of["prompt"], trajectory=text_of["trajectory"], notes=notes,
                gold_threshold=settings.get_float("GOLD_THRESHOLD"))


# ---------------------------------------------------------------------------
# Trajectory parsing
# ---------------------------------------------------------------------------

_SUB_RX = re.compile(r"^\s*Subtask\s+(\d+)\s*[:.\-–]\s*(.*)$", re.I)
_STEP_LINE_RX = re.compile(r"^\s*Step\s+(\d+)\.(\d+)\s*[:.\-–]?\s*(.*)$", re.I)


def parse_trajectory(text: str) -> dict[str, Any]:
    """{'subtasks': {n: title}, 'steps': {'1.2': {'subtask': '1', 'text': ...}}, 'order': [...]}"""
    subtasks: dict[str, str] = {}
    steps: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    cur_sub, cur_step = None, None
    for line in (text or "").splitlines():
        m = _SUB_RX.match(line)
        if m:
            cur_sub, cur_step = m.group(1), None
            subtasks[cur_sub] = m.group(2).strip()
            continue
        m = _STEP_LINE_RX.match(line)
        if m:
            sid = f"{m.group(1)}.{m.group(2)}"
            sub = cur_sub or m.group(1)
            if sid in steps:  # a repeated id: keep both, apart
                sid = f"{sid}#{sum(1 for k in steps if k.split('#')[0] == sid) + 1}"
            steps[sid] = {"subtask": sub, "text": m.group(3).strip()}
            order.append(sid)
            cur_step = sid
            continue
        if cur_step and line.strip():
            steps[cur_step]["text"] += "\n" + line.strip()
    return {"subtasks": subtasks, "steps": steps, "order": order}


def step_counts(traj: dict[str, Any]) -> dict[str, int]:
    out = {k: 0 for k in traj["subtasks"]}
    for st in traj["steps"].values():
        out[st["subtask"]] = out.get(st["subtask"], 0) + 1
    return out


def _norm_title(t: str | None) -> str:
    return re.sub(r"[\W_]+", " ", (t or "").lower()).strip()


def section_titles(snap: Snap) -> dict[str, str]:
    out = {}
    for sec in (snap.rubric or {}).get("rubric_sections", []):
        out[str(sec.get("subtask", ""))] = sec.get("title") or ""
    if not out:
        for c in snap.criteria.values():
            out.setdefault(str(c["subtask"]), c["title"] or "")
    return out


def title_mismatches(snap: Snap) -> list[str]:
    traj = parse_trajectory(snap.trajectory)
    secs = section_titles(snap)
    bad = []
    for n in sorted(set(traj["subtasks"]) | set(secs), key=lambda x: (len(x), x)):
        a, b = traj["subtasks"].get(n), secs.get(n)
        if a is None or b is None:
            bad.append(f"Subtask {n}: {'no trajectory subtask' if a is None else 'no rubric section'}")
        elif _norm_title(a) != _norm_title(b):
            bad.append(f"Subtask {n}: trajectory {a!r} vs rubric {b!r}")
    return bad


# ---------------------------------------------------------------------------
# Diffs (hunks)
# ---------------------------------------------------------------------------


def _hunk(artifact: str, kind: str, target: list, desc: str, old: Any = None, new: Any = None,
          file: str | None = None, loc: str | None = None) -> dict[str, Any]:
    return {"artifact": artifact, "kind": kind, "target": target, "desc": desc, "old": old, "new": new,
            "file": file, "loc": loc}


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _seq_diff(a: list[str], b: list[str], artifact: str, target: list, what: str,
              file: str | None = None) -> list[dict[str, Any]]:
    out = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old, new = "\n".join(a[i1:i2]), "\n".join(b[j1:j2])
        kind = {"replace": "changed", "delete": "removed", "insert": "added"}[tag]
        loc = f"{what} {i1 + 1}" + (f"–{i2}" if i2 - i1 > 1 else "")
        out.append(_hunk(artifact, kind, target, f"{what.capitalize()} {kind}: "
                         f"{(new or old)[:160]}", old or None, new or None, file, loc))
    return out


def _meta_diff(old: dict[str, Any] | None, new: dict[str, Any] | None, artifact: str,
               file: str) -> list[dict[str, Any]]:
    a = {k: v for k, v in (old or {}).items() if k not in SKIP_META}
    b = {k: v for k, v in (new or {}).items() if k not in SKIP_META}
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            kind = "cleared" if not b.get(k) else ("set" if not a.get(k) else "changed")
            out.append(_hunk(artifact, "metadata", ["meta", _base(file), k],
                             f"{file.rsplit('/', 1)[-1]} property {k} {kind}: {a.get(k)!r} → {b.get(k)!r}",
                             a.get(k), b.get(k), file, f"properties: {k}"))
    return out


def _xlsx_cells(ex: dict[str, Any]) -> dict[tuple[str, str], tuple]:
    out = {}
    for sh in ex.get("sheets", []):
        for c in sh.get("cells", []):
            out[(sh["name"], c["addr"])] = (c.get("formula"), c.get("value"), c.get("number_format"),
                                           c.get("display"))
    return out


def _xlsx_diff(old: dict[str, Any], new: dict[str, Any], artifact: str, file: str) -> list[dict[str, Any]]:
    out = []
    sa = [s["name"] for s in old.get("sheets", [])]
    sb = [s["name"] for s in new.get("sheets", [])]
    for n in sb:
        if n not in sa:
            out.append(_hunk(artifact, "sheet_added", ["sheet", _base(file), n], f"Sheet {n!r} added", None, n,
                             file, n))
    for n in sa:
        if n not in sb:
            out.append(_hunk(artifact, "sheet_removed", ["sheet", _base(file), n], f"Sheet {n!r} removed", n,
                             None, file, n))
    ca, cb = _xlsx_cells(old), _xlsx_cells(new)
    for key in sorted(set(ca) | set(cb)):
        if key[0] not in sa or key[0] not in sb:
            continue
        x, y = ca.get(key), cb.get(key)
        if x == y:
            continue
        x = x or (None, None, None, None)
        y = y or (None, None, None, None)
        what = [n for n, i in (("formula", 0), ("value", 1), ("format", 2)) if x[i] != y[i]] or ["display"]
        old_s = x[0] or x[3]
        new_s = y[0] or y[3]
        out.append(_hunk(artifact, "cell", ["cell", _base(file), key[0], key[1]],
                         f"{key[0]}!{key[1]} {'/'.join(what)}: {old_s!r} → {new_s!r}", old_s, new_s, file,
                         f"{key[0]}!{key[1]}"))
    return out


def _doc_units(ex: dict[str, Any], text: str) -> list[str]:
    kind = ex.get("kind")
    if kind == "docx":
        units = [p["text"] for p in ex.get("paragraphs", []) if p.get("text", "").strip()]
        for t in ex.get("tables", []):
            units += [" | ".join(r) for r in t.get("rows", [])]
        return units
    if kind == "pdf":
        return [ln.strip() for p in ex.get("pages", []) for ln in (p.get("text") or "").split("\n") if ln.strip()]
    if kind == "pptx":
        return [ln for sl in ex.get("slides", []) for ln in sl.get("text", "").split("\n") if ln.strip()]
    return [ln for ln in (text or "").split("\n") if ln.strip()]


def _file_diff(fa: dict[str, Any] | None, fb: dict[str, Any] | None, artifact: str) -> list[dict[str, Any]]:
    if fa is None:
        return [_hunk(artifact, "file_added", ["file", _base(fb["name"])], f"File added: {fb['name']}", None,
                      fb["name"], fb["name"])]
    if fb is None:
        return [_hunk(artifact, "file_removed", ["file", _base(fa["name"])], f"File removed: {fa['name']}",
                      fa["name"], None, fa["name"])]
    if fa["sha"] == fb["sha"]:
        return []
    name = fb["name"]
    out: list[dict[str, Any]] = []
    meta_a = dict(fa["meta"], **(fa["ex"].get("pdf_metadata") or {}))
    meta_b = dict(fb["meta"], **(fb["ex"].get("pdf_metadata") or {}))
    out += _meta_diff(meta_a, meta_b, artifact, name)
    if fa["ex"].get("kind") == "xlsx" and fb["ex"].get("kind") == "xlsx":
        out += _xlsx_diff(fa["ex"], fb["ex"], artifact, name)
        wa = set((fa["parts"] or {}).get("webextensions") or [])
        wb = set((fb["parts"] or {}).get("webextensions") or [])
        for p in sorted(wb - wa):
            out.append(_hunk(artifact, "webextension_added", ["webext", _base(name)], f"{p} added", None, p, name))
        for p in sorted(wa - wb):
            out.append(_hunk(artifact, "webextension_removed", ["webext", _base(name)], f"{p} removed", p, None,
                             name))
    else:
        out += _seq_diff(_doc_units(fa["ex"], fa["text"]), _doc_units(fb["ex"], fb["text"]), artifact,
                         ["file", _base(name)], "paragraph", name)
    if not out:
        out.append(_hunk(artifact, "binary", ["file", _base(name)],
                         f"{name.rsplit('/', 1)[-1]} changed (no difference in text, cells or properties)",
                         fa["sha"][:12], fb["sha"][:12], name))
    return out


def _pair(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[tuple]:
    pairs, used = [], set()
    for fa in old:
        fb = next((f for f in new if f["name"] == fa["name"] and f["id"] not in used), None) or \
            next((f for f in new if _base(f["name"]) == _base(fa["name"]) and f["id"] not in used), None)
        if fb is None and len(old) == 1 and len(new) == 1:
            fb = new[0]
        if fb is not None:
            used.add(fb["id"])
        pairs.append((fa, fb))
    pairs += [(None, fb) for fb in new if fb["id"] not in used]
    return pairs


def crosswalk(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """old crit id -> new crit id (or None if deleted). Same id first, then text similarity ≥ 85."""
    out: dict[str, str | None] = {}
    taken: set[str] = set()
    for cid, c in old.items():
        if cid in new and fuzz.token_sort_ratio(c["text"], new[cid]["text"]) >= CRIT_SIM:
            out[cid] = cid
            taken.add(cid)
    for cid, c in old.items():
        if cid in out:
            continue
        best, score = None, 0.0
        for nid, n in new.items():
            if nid in taken:
                continue
            sc = fuzz.token_sort_ratio(c["text"], n["text"])
            if sc > score:
                best, score = nid, sc
        if best is not None and score >= CRIT_SIM:
            out[cid] = best
            taken.add(best)
    for cid in old:
        if cid not in out:
            if cid in new and cid not in taken:  # same id, text rewritten
                out[cid] = cid
                taken.add(cid)
            else:
                out[cid] = None
    return out


def _rubric_diff(a: Snap, b: Snap, cw: dict[str, str | None]) -> list[dict[str, Any]]:
    out = []
    for oid, nid in cw.items():
        oc = a.criteria[oid]
        if nid is None:
            out.append(_hunk("rubric", "deleted", ["crit", oid], f"[{oid}] deleted ({oc['points']:g} pts)",
                             oc["text"], None, loc=oid))
            continue
        nc = b.criteria[nid]
        if nid != oid:
            out.append(_hunk("rubric", "renumbered", ["crit", oid], f"[{oid}] renumbered to [{nid}]", oid, nid,
                             loc=oid))
        if abs(oc["points"] - nc["points"]) > 1e-9:
            out.append(_hunk("rubric", "reweighted", ["crit", oid],
                             f"[{oid}] reweighted {oc['points']:g} → {nc['points']:g} pts", oc["points"],
                             nc["points"], loc=oid))
        if oc["text"].strip() != nc["text"].strip():
            out.append(_hunk("rubric", "reworded", ["crit", oid], f"[{oid}] reworded: {nc['text'][:160]}",
                             oc["text"], nc["text"], loc=oid))
        if (oc["category"] or "") != (nc["category"] or ""):
            out.append(_hunk("rubric", "reworded", ["crit", oid],
                             f"[{oid}] category {oc['category']} → {nc['category']}", oc["category"],
                             nc["category"], loc=oid))
        if json.dumps(oc["references"], sort_keys=True) != json.dumps(nc["references"], sort_keys=True):
            out.append(_hunk("rubric", "references", ["crit", oid], f"[{oid}] references changed",
                             json.dumps(oc["references"]), json.dumps(nc["references"]), loc=oid))
    mapped = {v for v in cw.values() if v}
    for nid in b.crit_order:
        if nid not in mapped:
            nc = b.criteria[nid]
            out.append(_hunk("rubric", "added", ["crit_added", nid],
                             f"[{nid}] added ({nc['points']:g} pts): {nc['text'][:160]}", None, nc["text"], loc=nid))
    ta, tb = section_titles(a), section_titles(b)
    for n in sorted(set(ta) | set(tb)):
        if ta.get(n) != tb.get(n):
            out.append(_hunk("rubric", "section_title", ["subtask", n],
                             f"Section {n} title: {ta.get(n)!r} → {tb.get(n)!r}", ta.get(n), tb.get(n), loc=n))
    ra, rb = a.rubric or {}, b.rubric or {}
    if ra.get("total_points") != rb.get("total_points"):
        out.append(_hunk("rubric", "total_points", ["rubric_total"],
                         f"total_points {ra.get('total_points')} → {rb.get('total_points')}",
                         ra.get("total_points"), rb.get("total_points")))
    wa = {str(s.get("subtask")): s.get("weight") for s in ra.get("rubric_sections", [])}
    wb = {str(s.get("subtask")): s.get("weight") for s in rb.get("rubric_sections", [])}
    for n in sorted(set(wa) | set(wb)):
        if wa.get(n) != wb.get(n):
            out.append(_hunk("rubric", "section_weight", ["section_weight", n],
                             f"Section {n} weight {wa.get(n)} → {wb.get(n)}", wa.get(n), wb.get(n), loc=n))
    return out


def _trajectory_diff(a: Snap, b: Snap) -> list[dict[str, Any]]:
    ta, tb = parse_trajectory(a.trajectory), parse_trajectory(b.trajectory)
    if a.trajectory == b.trajectory:
        return []
    out = []
    sa, sb = ta["steps"], tb["steps"]
    cw = crosswalk({k: {"text": v["text"]} for k, v in sa.items()}, {k: {"text": v["text"]} for k, v in sb.items()})
    mapped = {v for v in cw.values() if v}
    for oid, nid in cw.items():
        sub = sa[oid]["subtask"]
        if nid is None:
            out.append(_hunk("trajectory", "step_removed", ["step", oid, sub], f"Step {oid} removed",
                             sa[oid]["text"], None, loc=f"Step {oid}"))
            continue
        if nid != oid:
            out.append(_hunk("trajectory", "step_renumbered", ["step", oid, sub], f"Step {oid} is now Step {nid}",
                             oid, nid, loc=f"Step {oid}"))
        if sa[oid]["text"] != sb[nid]["text"]:
            out.append(_hunk("trajectory", "step_changed", ["step", oid, sub],
                             f"Step {oid} reworded: {sb[nid]['text'][:160]}", sa[oid]["text"], sb[nid]["text"],
                             loc=f"Step {oid}"))
    for nid in tb["order"]:
        if nid not in mapped:
            out.append(_hunk("trajectory", "step_added", ["step", nid, sb[nid]["subtask"]],
                             f"Step {nid} added: {sb[nid]['text'][:160]}", None, sb[nid]["text"], loc=f"Step {nid}"))
    for n in sorted(set(ta["subtasks"]) | set(tb["subtasks"])):
        if ta["subtasks"].get(n) != tb["subtasks"].get(n):
            out.append(_hunk("trajectory", "subtask_title", ["subtask", n],
                             f"Subtask {n} title: {ta['subtasks'].get(n)!r} → {tb['subtasks'].get(n)!r}",
                             ta["subtasks"].get(n), tb["subtasks"].get(n), loc=f"Subtask {n}"))
    if not out:  # whitespace or text outside steps
        out += _seq_diff(a.trajectory.splitlines(), b.trajectory.splitlines(), "trajectory", ["trajectory"],
                         "line")
    return out


def diff_versions(a: Snap, b: Snap) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    hunks: list[dict[str, Any]] = []
    if a.prompt != b.prompt:
        hunks += _seq_diff(_sentences(a.prompt), _sentences(b.prompt), "prompt", ["prompt"], "sentence")
    for fa, fb in _pair(a.by_role("reference"), b.by_role("reference")):
        hunks += _file_diff(fa, fb, "reference")
    cw = crosswalk(a.criteria, b.criteria)
    hunks += _rubric_diff(a, b, cw)
    hunks += _trajectory_diff(a, b)
    for fa, fb in _pair(a.by_role("gold"), b.by_role("gold")):
        hunks += [dict(h, target=["gold"] + h["target"]) for h in _file_diff(fa, fb, "gold")]
    for idx in sorted(set(a.notes) | set(b.notes)):
        x, y = a.notes.get(idx), b.notes.get(idx)
        if (x or "").strip() != (y or "").strip():
            kind = "added" if x is None else "removed" if y is None else "changed"
            hunks.append(_hunk("failure_notes", kind, ["note", idx], f"Failure note {idx} {kind}: {(y or x)[:160]}",
                               x, y, loc=f"Note {idx}"))
    ma, mb = a.by_role("model_output"), b.by_role("model_output")
    if sorted(f["sha"] for f in ma) != sorted(f["sha"] for f in mb):
        hunks.append(_hunk("model_run", "changed", ["model_output"], "Model output changed (new run)",
                           ", ".join(f["name"] for f in ma), ", ".join(f["name"] for f in mb)))
    return hunks, cw


# ---------------------------------------------------------------------------
# Expected changes and their checks
# ---------------------------------------------------------------------------


def _prop_keys(fields: list[str]) -> list[str]:
    keys = []
    for f in fields:
        keys += opsmod.PROPERTY_FIELDS.get(f.strip().lower(), [f.strip()])
    return keys


def _props_in(location: str | None) -> list[str]:
    m = re.match(r"\s*properties?\s*:\s*(.+)$", location or "", re.I)
    return [p.strip() for p in re.split(r",|\band\b", m.group(1)) if p.strip()] if m else []


def _cells_in(location: str | None) -> list[str]:
    return [c.replace("$", "") for c in re.findall(r"\b[A-Z]{1,3}\$?\d+\b", location or "")]


def check_for_op(op: dict[str, Any], base: Snap) -> dict[str, Any]:
    """A check (what to test) and its targets (which hunks it explains) for one op."""
    t = op.get("type")
    crit = str(op.get("crit")) if op.get("crit") is not None else None
    if t == "reweight":
        return {"kind": "crit_points", "crit": crit, "equals": float(op["to"]), "targets": [["crit", crit]]}
    if t == "delete":
        return {"kind": "crit_deleted", "crit": crit, "targets": [["crit", crit]]}
    if t == "reword":
        return {"kind": "crit_text_like", "crit": crit, "text": op.get("new_text"), "targets": [["crit", crit]]}
    if t == "accept_alternative":
        return {"kind": "crit_text_contains", "crit": crit, "text": str(op.get("value")),
                "targets": [["crit", crit]]}
    if t == "add":
        return {"kind": "crit_added", "text": op.get("text"), "points": op.get("points"),
                "targets": [["crit_added"]]}
    if t == "prompt_edit":
        return {"kind": "prompt_contains", "text": op.get("text"), "targets": [["prompt"]]}
    if t == "reference_edit":
        f = op.get("file") or ""
        props = _props_in(op.get("location"))
        if op.get("metadata_only") and props:
            return {"kind": "metadata_empty", "file": f, "fields": props,
                    "targets": [["meta", _base(f), k] for k in _prop_keys(props)]}
        if op.get("metadata_only"):
            return {"kind": "no_personal_names", "file": f, "targets": [["meta", _base(f)]]}
        cells = _cells_in(op.get("location"))
        tg = [["cell", _base(f), None, c] for c in cells] or [["file", _base(f)], ["cell", _base(f)],
                                                              ["meta", _base(f)], ["sheet", _base(f)]]
        return {"kind": "changed", "file": f, "targets": tg}
    if t == "gold_edit":
        gold = (base.by_role("gold") or [{"name": "gold"}])[0]["name"]
        props = _props_in(op.get("location"))
        if props:
            return {"kind": "metadata_empty", "file": gold, "fields": props,
                    "targets": [["gold", "meta", _base(gold), k] for k in _prop_keys(props)]}
        cells = _cells_in(op.get("location"))
        tg = [["gold", "cell", _base(gold), None, c] for c in cells] or [["gold"]]
        return {"kind": "changed", "file": gold, "targets": tg}
    if t == "trajectory_edit":
        step = str(op.get("step") or "")
        sub = step.split(".")[0] if step else None
        change = (op.get("change") or "").lower()
        if re.search(r"\bsplit\b|\bsteps? per subtask\b|\bat least 5\b", change) and sub:
            return {"kind": "min_steps", "subtask": sub, "n": MIN_STEPS, "targets": [["step", None, sub]]}
        if re.search(r"\brename\b|\btitle\b", change) and sub:
            return {"kind": "titles_match", "subtask": sub, "targets": [["subtask", sub]]}
        return {"kind": "changed", "targets": [["step", step, None]] if step else [["trajectory"]]}
    if t == "failure_note_edit":
        return {"kind": "changed", "targets": [["note", op.get("idx")]]}
    if t == "model_flip":
        return {"kind": "grade", "who": "model", "crit": crit, "to": op.get("to"), "targets": [["model_output"]]}
    if t == "gold_flip":
        return {"kind": "grade", "who": "gold", "crit": crit, "to": op.get("to"), "targets": [["gold"]]}
    return {"kind": "manual", "targets": []}


def _hits(target: list, hunk_target: list) -> bool:
    """Does an expected-change target explain a hunk? None in a target is a wildcard."""
    if len(target) > len(hunk_target):
        return False
    if target[0] == "step" and hunk_target[0] == "step":
        _, sid, sub = (target + [None, None])[:3]
        if sid:
            return hunk_target[1] == sid or (hunk_target[1] or "").split("#")[0] == sid
        return sub is not None and hunk_target[2] == sub
    if target[0] == "subtask" and hunk_target[0] == "step":
        return hunk_target[2] == target[1]
    for x, y in zip(target, hunk_target):
        if x is not None and str(x) != str(y):
            return False
    return True


@dataclass
class Expected:
    card: str
    decision_id: int
    artifact: str
    description: str
    check: dict[str, Any]


def expected_changes(state: impact.TaskState, base: Snap) -> list[Expected]:
    """From the ops of each card's final decision. A change decision with no ops needs a person."""
    out: list[Expected] = []
    for code, card in state.cards.items():
        d = card.decisions.get("final")
        if not d or not d.get("option_key") or d["option_key"] == "unclear":
            continue
        if impact.is_keep(card, d["option_key"]):
            continue
        key = impact.resolve_key(card, d["option_key"])
        if key is None and d["option_key"] != "agree":
            continue
        ops = impact.decided_ops(card, d)
        if not ops:
            text = card.options.get(key or "", "") or d.get("new_option_text") or ""
            out.append(Expected(code, d["id"], "other", f"Card {code}: {text[:200] or d['option_key']}",
                                {"kind": "manual", "targets": []}))
            continue
        for o in ops:
            clean = {k: v for k, v in o.items() if not k.startswith("_")}
            out.append(Expected(code, d["id"], opsmod.ARTIFACT_OF_OP.get(o.get("type"), "other"),
                                opsmod.describe(clean), dict(check_for_op(clean, base), op=clean)))
    return out


def _new_crit(check: dict[str, Any], cw: dict[str, str | None], b: Snap) -> dict[str, Any] | None:
    nid = cw.get(check.get("crit")) if check.get("crit") in cw else check.get("crit")
    return b.criteria.get(nid) if nid else None


def _graded(version_id: int, role: str) -> dict[str, str] | None:
    from core import grading
    from db.models import File
    from db.session import session_scope

    with session_scope() as s:
        f = s.query(File).filter_by(version_id=version_id, role=role).first()
        fid = f.id if f else None
    if fid is None:
        return None
    lines = grading.review_lines(version_id, fid)["lines"]
    res = {ln["crit_id"]: ln["final"] or ln["suggested"] for ln in lines if ln["final"] or ln["suggested"]}
    return res or None


def evaluate(check: dict[str, Any], snap: Snap, cw: dict[str, str | None] | None = None) -> tuple[Any, str]:
    """(True | False | 'partial' | None, evidence). None means a deterministic check isn't possible."""
    k = check["kind"]
    cw = cw if cw is not None else {c: c for c in snap.criteria}
    if k in ("crit_points", "crit_deleted", "crit_text_like", "crit_text_contains"):
        c = _new_crit(check, cw, snap)
        cid = check.get("crit")
        if k == "crit_deleted":
            return (c is None, f"[{cid}] {'is gone' if c is None else 'is still there'}")
        if c is None:
            return False, f"[{cid}] not found"
        if k == "crit_points":
            return abs(c["points"] - check["equals"]) < 1e-9, f"[{cid}] is {c['points']:g} pts"
        if k == "crit_text_like":
            sc = fuzz.token_set_ratio(check.get("text") or "", c["text"])
            return sc >= CRIT_SIM, f"[{cid}] reads: {c['text'][:200]!r} (similarity {sc:.0f})"
        ok = (check.get("text") or "").lower() in c["text"].lower() or \
            fuzz.partial_ratio((check.get("text") or "").lower(), c["text"].lower()) >= CRIT_SIM
        return ok, f"[{cid}] reads: {c['text'][:200]!r}"
    if k == "crit_added":
        mapped = {v for v in cw.values() if v}
        best, sc = None, 0.0
        for nid in snap.crit_order:
            if nid in mapped:
                continue
            s_ = fuzz.token_set_ratio(check.get("text") or "", snap.criteria[nid]["text"])
            if s_ > sc:
                best, sc = nid, s_
        if best is None or sc < CRIT_SIM:
            return False, "no matching new criterion"
        pts = check.get("points")
        ok = pts is None or abs(float(pts) - snap.criteria[best]["points"]) < 1e-9
        return (True if ok else "partial"), f"[{best}] {snap.criteria[best]['points']:g} pts: " \
                                            f"{snap.criteria[best]['text'][:160]!r}"
    if k == "prompt_contains":
        sents = _sentences(snap.prompt)
        best = max(sents, key=lambda x: fuzz.partial_ratio(check.get("text") or "", x), default="")
        sc = fuzz.partial_ratio(check.get("text") or "", best) if best else 0
        return (True, f"Prompt: {best[:200]!r}") if sc >= PROMPT_SIM else (None, f"closest sentence: {best[:200]!r}")
    if k == "metadata_empty":
        f = snap.find(check["file"])
        if f is None:
            return False, f"{check['file']} not found"
        meta = dict(f["meta"], **(f["ex"].get("pdf_metadata") or {}))
        status = {}
        for fld in check["fields"]:
            keys = _prop_keys([fld])
            status[fld] = all(not (meta.get(x) or "").strip() for x in keys)
        done = [x for x, v in status.items() if v]
        left = [f"{x} = {next((meta[y] for y in _prop_keys([x]) if meta.get(y)), '')!r}"
                for x, v in status.items() if not v]
        ev = f"{f['name'].rsplit('/', 1)[-1]}: " + "; ".join(
            ([f"{', '.join(done)} empty"] if done else []) + ([f"still set: {', '.join(left)}"] if left else []))
        return (True if not left else "partial" if done else False), ev
    if k == "no_personal_names":
        f = snap.find(check["file"])
        if f is None:
            return False, f"{check['file']} not found"
        names = f["meta"].get("personal_names") or []
        return (not names), ("no personal names" if not names else
                             "; ".join(f"{n['field']} = {n['value']!r}" for n in names))
    if k == "min_steps":
        n = step_counts(parse_trajectory(snap.trajectory)).get(check["subtask"], 0)
        return n >= check["n"], f"Subtask {check['subtask']} has {n} steps"
    if k == "titles_match":
        bad = [b for b in title_mismatches(snap) if b.startswith(f"Subtask {check['subtask']}:")]
        return (not bad), (bad[0] if bad else f"Subtask {check['subtask']} titles match")
    if k == "grade":
        res = _graded(snap.version_id, "model_output" if check["who"] == "model" else "gold")
        if not res:
            return None, f"the new {'model output' if check['who'] == 'model' else 'gold'} isn't graded yet"
        nid = cw.get(check["crit"]) if check["crit"] in cw else check["crit"]
        got = res.get(nid or "")
        return got == check["to"], f"[{nid}] graded {got or 'not graded'}"
    return None, ""


def _llm_check(e: Expected, hunks: list[dict[str, Any]]) -> tuple[str, str] | None:
    from core import llm

    if not llm.is_configured():
        return None
    tmpl = llm.load_prompt("verify_change", "v1")
    diff = "\n\n".join(f"[{h['desc'][:80]}]\nBEFORE: {str(h['old'] or '')[:1500]}\nAFTER: {str(h['new'] or '')[:1500]}"
                       for h in hunks)[:8000]
    schema = {"type": "object", "additionalProperties": False, "required": ["verdict", "evidence"],
              "properties": {"verdict": {"type": "string", "enum": RESULTS}, "evidence": {"type": "string"}}}
    try:
        out = llm.call_json("verify_change", "v1", "Return only JSON that matches the schema.",
                            tmpl.format(change=e.description, diff=diff or "(no change found)"), schema)
    except llm.LLMError:
        return None
    return out["verdict"], "AI check: " + out["evidence"]


def judge(e: Expected, base: Snap, new: Snap, cw: dict[str, str | None], hunks: list[dict[str, Any]],
          use_llm: bool) -> tuple[str, str]:
    """Result for one expected change: addressed, partial, missing, regressed or needs_human."""
    check = e.check
    if check["kind"] == "manual":
        return "needs_human", "No automatic check for this decision; check the files by hand."
    if not hunks:
        before, ev_b = evaluate(check, base)
        note = " (it already held in the baseline)" if before is True else ""
        if check["kind"] == "grade" and check["who"] == "model":
            return "missing", "Model output unchanged: re-run pending."
        return "missing", f"No change found{note}. Baseline: {ev_b}".strip()
    now, ev = evaluate(check, new, cw)
    matched = "Changes: " + "; ".join(h["desc"][:120] for h in hunks[:5]) + (" …" if len(hunks) > 5 else "")
    if now is True:
        return "addressed", f"{ev}. {matched}"
    before, _ = evaluate(check, base)
    if before is True and now is False:
        return "regressed", f"{ev}. {matched}"
    if now == "partial":
        return "partial", f"{ev}. {matched}"
    if use_llm:
        got = _llm_check(e, hunks)
        if got:
            return got[0], f"{got[1]} {matched}"
    if now is False and check["kind"] in ("crit_points", "crit_deleted", "metadata_empty", "min_steps",
                                         "titles_match", "grade", "no_personal_names"):
        return "partial", f"Changed, but {ev}. {matched}"
    return "needs_human", f"{ev + '. ' if ev else ''}{matched}"


# ---------------------------------------------------------------------------
# Running a verification
# ---------------------------------------------------------------------------


def run(report_id: int, new_version_id: int, use_llm: bool = True, user: str | None = None) -> dict[str, Any]:
    """Diff, check every expected change, list untracked changes and store the results."""
    from db.models import ExpectedChange, UntrackedChange, Verification
    from db.session import audit, session_scope

    state = impact.load_state(report_id)
    if state.version_id is None:
        raise ValueError("The baseline version isn't uploaded.")
    if new_version_id == state.version_id:
        raise ValueError("Pick a revised version other than the baseline.")
    base, new = snapshot(state.version_id), snapshot(new_version_id)
    hunks, cw = diff_versions(base, new)
    exp = expected_changes(state, base)
    explained: set[int] = set()
    results = []
    for e in exp:
        mine = [i for i, h in enumerate(hunks) if any(_hits(t, h["target"]) for t in e.check.get("targets", []))]
        explained.update(mine)
        res, ev = judge(e, base, new, cw, [hunks[i] for i in mine], use_llm)
        results.append((e, res, ev, mine))

    with session_scope() as s:
        dec_ids = {e.decision_id for e in exp}
        known = {}
        for ec in s.query(ExpectedChange).all():
            known[(ec.decision_id, ec.artifact, ec.description, ec.check_json)] = ec
        for e, res, ev, mine in results:
            cj = json.dumps(e.check, sort_keys=True, default=str)
            ec = known.get((e.decision_id, e.artifact, e.description, cj))
            if ec is None:
                ec = ExpectedChange(decision_id=e.decision_id, artifact=e.artifact, description=e.description,
                                    check_json=cj)
                s.add(ec)
                s.flush()
            ver = s.query(Verification).filter_by(expected_change_id=ec.id, new_version_id=new_version_id).first()
            hunk_desc = json.dumps([hunks[i]["desc"] for i in mine])
            if ver is None:
                s.add(Verification(expected_change_id=ec.id, new_version_id=new_version_id, result=res,
                                   evidence=json.dumps({"text": ev, "auto": res, "hunks": hunk_desc})))
            else:
                prev = json.loads(ver.evidence or "{}") if (ver.evidence or "").startswith("{") else {}
                if ver.override_by:  # keep the person's call, refresh the automatic one beside it
                    ver.evidence = json.dumps(dict(prev, text=ev, auto=res, hunks=hunk_desc))
                else:
                    ver.result = res
                    ver.evidence = json.dumps({"text": ev, "auto": res, "hunks": hunk_desc})
        # untracked changes
        existing = {u.diff_json: u for u in s.query(UntrackedChange).filter_by(new_version_id=new_version_id)}
        keep = set()
        for i, h in enumerate(hunks):
            if i in explained:
                continue
            dj = json.dumps({k: h[k] for k in ("artifact", "kind", "target", "desc", "old", "new", "file", "loc")},
                            sort_keys=True, default=str)
            keep.add(dj)
            if dj not in existing:
                s.add(UntrackedChange(new_version_id=new_version_id, artifact=h["artifact"], diff_json=dj,
                                      status=None))
        for dj, u in existing.items():
            if dj not in keep and not u.status:
                s.delete(u)
        audit(s, user, "verification", str(new_version_id), None,
              {"report": report_id, "baseline": state.version_id, "expected": len(exp),
               "untracked": len(hunks) - len(explained)})
    return load(report_id, new_version_id) | {"hunks": hunks, "crosswalk": cw}


def load(report_id: int, new_version_id: int) -> dict[str, Any]:
    """Stored results for a report's final decisions against one revised version."""
    from db.models import ExpectedChange, UntrackedChange, Verification, Version
    from db.session import session_scope

    state = impact.load_state(report_id)
    finals = {c.decisions["final"]["id"]: code for code, c in state.cards.items() if c.decisions.get("final")}
    out_exp, out_unt = [], []
    with session_scope() as s:
        nv = s.get(Version, new_version_id)
        label = nv.label if nv else str(new_version_id)
        for ec in s.query(ExpectedChange).filter(ExpectedChange.decision_id.in_(list(finals) or [-1])) \
                .order_by(ExpectedChange.id):
            ver = s.query(Verification).filter_by(expected_change_id=ec.id, new_version_id=new_version_id).first()
            if ver is None:
                continue
            ev = json.loads(ver.evidence) if (ver.evidence or "").startswith("{") else {"text": ver.evidence}
            out_exp.append({"verification_id": ver.id, "card": finals[ec.decision_id], "artifact": ec.artifact,
                            "description": ec.description, "result": ver.result, "auto": ev.get("auto"),
                            "evidence": ev.get("text") or "", "override_by": ver.override_by,
                            "hunks": json.loads(ev.get("hunks") or "[]"), "check": json.loads(ec.check_json or "{}")})
        for u in s.query(UntrackedChange).filter_by(new_version_id=new_version_id).order_by(UntrackedChange.id):
            d = json.loads(u.diff_json)
            out_unt.append({"id": u.id, "artifact": u.artifact, "kind": d.get("kind"), "desc": d.get("desc"),
                            "file": d.get("file"), "loc": d.get("loc"), "old": d.get("old"), "new": d.get("new"),
                            "status": u.status or ""})
    # Only the expected changes of the current final decisions count.
    exp_keys = {(e.card, e.description) for e in expected_changes(state, snapshot(state.version_id, light=True))} \
        if state.version_id else set()
    out_exp = [e for e in out_exp if (e["card"], e["description"]) in exp_keys]
    counts = {r: sum(1 for e in out_exp if e["result"] == r) for r in RESULTS}
    return {"report_id": report_id, "baseline_version_id": state.version_id, "baseline_label": state.version_label,
            "new_version_id": new_version_id, "new_label": label, "expected": out_exp, "untracked": out_unt,
            "counts": counts, "rerun": rerun_status(state, new_version_id)}


def override(verification_id: int, result: str, user: str | None, note: str | None = None) -> None:
    from db.models import Verification
    from db.session import audit, session_scope

    if result not in RESULTS:
        raise ValueError(f"unknown result {result!r}")
    with session_scope() as s:
        v = s.get(Verification, verification_id)
        before = {"result": v.result, "override_by": v.override_by}
        ev = json.loads(v.evidence) if (v.evidence or "").startswith("{") else {"text": v.evidence}
        if note:
            ev["override_note"] = note
        v.result, v.override_by, v.evidence = result, user or "unknown", json.dumps(ev)
        audit(s, user, "verification", str(verification_id), before, {"result": result, "override_by": v.override_by})


def set_untracked_status(untracked_id: int, status: str, user: str | None) -> None:
    from db.models import UntrackedChange
    from db.session import audit, session_scope

    if status not in UNTRACKED_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with session_scope() as s:
        u = s.get(UntrackedChange, untracked_id)
        before = u.status
        u.status = status or None
        audit(s, user, "untracked_change", str(untracked_id), before, status)


def progress(report_id: int) -> dict[str, Any] | None:
    """Latest verified version for the portfolio: {'label', 'addressed', 'total'}."""
    from db.models import Verification, Version
    from db.session import session_scope

    state = impact.load_state(report_id)
    with session_scope() as s:
        vids = [v.id for v in s.query(Version).filter_by(task_id=state.task_id).order_by(Version.uploaded_at.desc())
                if v.id != state.version_id]
        vids = [v for v in vids if s.query(Verification).filter_by(new_version_id=v).first()]
    if not vids:
        return None
    r = load(report_id, vids[0])
    return {"label": r["new_label"], "addressed": r["counts"]["addressed"], "total": len(r["expected"]),
            "untracked": sum(1 for u in r["untracked"] if not u["status"])}


def verified_cards(report_id: int) -> set[str]:
    """Cards whose expected changes are all addressed in the latest verified version."""
    p = progress(report_id)
    if not p:
        return set()
    from db.models import Version
    from db.session import session_scope

    with session_scope() as s:
        state = impact.load_state(report_id)
        v = s.query(Version).filter_by(task_id=state.task_id, label=p["label"]).first()
    r = load(report_id, v.id)
    by: dict[str, list[str]] = {}
    for e in r["expected"]:
        by.setdefault(e["card"], []).append(e["result"])
    return {c for c, res in by.items() if all(x == "addressed" for x in res)}


# ---------------------------------------------------------------------------
# Re-run (spec 9)
# ---------------------------------------------------------------------------


def rerun_status(state: impact.TaskState, new_version_id: int) -> dict[str, Any]:
    from core import grading, scoring
    from db.models import Criterion, File
    from db.session import session_scope

    causes = impact.rerun(state)
    with session_scope() as s:
        old = sorted(f.sha256 for f in s.query(File).filter_by(version_id=state.version_id, role="model_output"))
        newf = s.query(File).filter_by(version_id=new_version_id, role="model_output").all()
        new = sorted(f.sha256 for f in newf)
        mfid = newf[0].id if newf else None
        gf = s.query(File).filter_by(version_id=new_version_id, role="gold").first()
        gfid = gf.id if gf else None
        crits = [dict(crit_id=c.crit_id, points=c.points, category=c.category)
                 for c in s.query(Criterion).filter_by(version_id=new_version_id)]
    out: dict[str, Any] = {"required": bool(causes), "cards": sorted({c["card"] for c in causes}),
                           "model_output_changed": old != new}
    if not causes:
        out["status"] = "not required"
    elif old == new:
        out["status"] = "re-run pending"
    else:
        out["status"] = "new output uploaded"
    formula = settings.get("SCORE_FORMULA")
    for who, fid in (("model", mfid), ("gold", gfid)):
        if fid is None:
            continue
        lines = grading.review_lines(new_version_id, fid)["lines"]
        res = {ln["crit_id"]: ln["final"] or ln["suggested"] for ln in lines}
        if any(res.values()):
            out[who] = scoring.score(crits, res, formula)
    if "model" in out:
        mt = settings.get_float("MODEL_THRESHOLD")
        out["model_breaks"] = out["model"]["score_pct"] >= mt
        # failure notes: the baseline map, carried to the new ids through the crosswalk
        cw = crosswalk(snapshot(state.version_id).criteria, snapshot(new_version_id).criteria)
        res = {ln["crit_id"]: ln["final"] or ln["suggested"] for ln in grading.review_lines(new_version_id, mfid)["lines"]}
        pts = {c["crit_id"]: c["points"] for c in crits}
        notes = {}
        for idx, cids in state.failure_map.items():
            backing = sum(abs(pts.get(cw.get(c) or "", 0)) for c in cids
                          if cw.get(c) and res.get(cw[c]) in ("fail", "triggered"))
            notes[idx] = backing
        out["failure_notes"] = notes
    if "gold" in out:
        out["gold_breaks"] = out["gold"]["score_pct"] < settings.get_float("GOLD_THRESHOLD")
    return out


# ---------------------------------------------------------------------------
# Integrity checks (spec 9), on any version
# ---------------------------------------------------------------------------


def _col_num(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n


def _cell_exists(ref: str, sheet: dict[str, Any]) -> bool:
    for part in ref.split(":"):
        m = re.fullmatch(r"([A-Z]{1,3})(\d+)", part.replace("$", ""))
        if not m:
            return True
        if _col_num(m.group(1)) > (sheet.get("max_col") or 0) or int(m.group(2)) > (sheet.get("max_row") or 0):
            return False
    return True


def _gold_cell_refs(snap: Snap) -> list[dict[str, Any]]:
    """Cell refs in the trajectory that point at the gold. Text right after another file's name
    (e.g. "Working_Hours.xlsx (footnote A250)") refers to that file, so it is left out."""
    from core.finding_parser import extract_refs

    others = sorted({f["name"].rsplit("/", 1)[-1] for f in snap.files if f["role"] not in ("gold",)},
                    key=len, reverse=True)
    golds = {f["name"].rsplit("/", 1)[-1] for f in snap.by_role("gold")}
    rx = re.compile("|".join(re.escape(n) for n in list(golds) + others), re.I) if others or golds else None
    out = []
    for line in snap.trajectory.splitlines():
        if rx is None:
            out += extract_refs(line)["cells"]
            continue
        pos, owner = 0, None
        for m in list(rx.finditer(line)) + [None]:
            seg = line[pos:m.start()] if m else line[pos:]
            if owner is None or owner.lower() in {g.lower() for g in golds}:
                out += extract_refs(seg)["cells"]
            if m:
                pos, owner = m.end(), m.group(0)
    return out


def integrity(version_id: int) -> list[dict[str, Any]]:
    """[{check, ok (True/False/None), detail}] for one version."""
    from core.ingest import rubric_summary

    snap = snapshot(version_id)
    out: list[dict[str, Any]] = []

    def add(check: str, ok: bool | None, detail: str) -> None:
        out.append({"check": check, "ok": ok, "detail": detail})

    if snap.rubric:
        rs = rubric_summary(snap.rubric)
        add("total_points = sum of section weights = net criteria points", rs["consistent"],
            f"total_points {rs['total_points']}, section weights {rs['sum_section_weights']:g}, "
            f"net criteria points {rs['net_criteria_points']:g}")
    else:
        add("total_points = sum of section weights = net criteria points", None, "no readable rubric.json")

    # ids unique and sequential within each section
    problems = []
    seen: dict[str, int] = {}
    by_sec: dict[str, list[str]] = {}
    for cid in snap.crit_order:
        seen[cid] = seen.get(cid, 0) + 1
        by_sec.setdefault(str(snap.criteria[cid]["subtask"]), []).append(cid)
    problems += [f"[{c}] appears {n} times" for c, n in seen.items() if n > 1]
    for sec, ids in by_sec.items():
        want = [f"{sec}.{i}" for i in range(1, len(ids) + 1)]
        if ids != want:
            bad = next(((a, b) for a, b in zip(ids, want) if a != b), None)
            problems.append(f"Section {sec}: " + (f"[{bad[0]}] where [{bad[1]}] was expected" if bad else
                                                  "ids out of order"))
    add("criterion ids unique and sequential within each section", not problems, "; ".join(problems) or "ok")

    n = len(snap.criteria)
    pen = sum(1 for c in snap.criteria.values() if c["points"] < 0 or (c["category"] or "").lower() == "penalty")
    share = pen / n if n else 0
    add("penalties are 6–15% of criteria by count", (0.06 <= share <= 0.15) if n else None,
        f"{pen} of {n} ({share:.2%})")

    names = {_base(f["name"]) for f in snap.files}
    missing = []
    for cid in snap.crit_order:
        for r in snap.criteria[cid]["references"] or []:
            typ = (r.get("type") or "").lower()
            src = r.get("source") or ""
            if typ in ("derived", "task_prompt") or src.lower() in ("derived", "task_prompt"):
                continue
            if _base(src) not in names:
                missing.append(f"[{cid}] {src}")
    add("every reference source exists in the zip", not missing,
        "; ".join(dict.fromkeys(missing)) or "ok")

    gold = snap.by_role("gold")
    if snap.trajectory and gold and gold[0]["ex"].get("kind") == "xlsx":
        sheets = {sh["name"].lower(): sh for g in gold for sh in g["ex"].get("sheets", [])}
        bad_refs = []
        for c in _gold_cell_refs(snap):
            if c["sheet"]:
                sh = sheets.get(c["sheet"].strip("'").lower())
                if sh is None or not _cell_exists(c["ref"], sh):
                    bad_refs.append(f"{c['sheet']}!{c['ref']}")
            elif not any(_cell_exists(c["ref"], sh) for sh in sheets.values()):
                bad_refs.append(c["ref"])
        add("cell refs in the trajectory exist in the gold", not bad_refs,
            "; ".join(dict.fromkeys(bad_refs)) or "ok")
    else:
        add("cell refs in the trajectory exist in the gold", None, "no trajectory or no gold workbook")

    gres = _graded(version_id, "gold")
    if gres:
        from core import scoring

        sc = scoring.score([dict(crit_id=k, points=v["points"], category=v["category"])
                            for k, v in snap.criteria.items()], gres, settings.get("SCORE_FORMULA"))
        add(f"gold ≥ gold threshold ({snap.gold_threshold:.0%})", sc["score_pct"] >= snap.gold_threshold,
            f"gold scores {sc['score_pct']:.1%}")
    else:
        add(f"gold ≥ gold threshold ({snap.gold_threshold:.0%})", None, "gold not graded yet")

    people = []
    for f in snap.files:
        if f["role"] in ("qa_sheet", "other"):
            continue
        for p in f["meta"].get("personal_names") or []:
            people.append(f"{f['name'].rsplit('/', 1)[-1]}: {p['field']} = {p['value']!r}")
    add("no personal names in file metadata", not people, "; ".join(people) or "ok")

    webext = [f"{f['name']}: {p}" for f in snap.files for p in (f["parts"] or {}).get("webextensions") or []]
    add("no xl/webextensions parts", not webext, "; ".join(webext) or "ok")

    bad = title_mismatches(snap) if snap.trajectory else ["no trajectory"]
    add("subtask titles match between trajectory and rubric", not bad, "; ".join(bad) or "ok")

    counts = step_counts(parse_trajectory(snap.trajectory))
    off = [f"Subtask {k}: {v}" for k, v in counts.items() if not MIN_STEPS <= v <= MAX_STEPS]
    add(f"each subtask has {MIN_STEPS}–{MAX_STEPS} steps", (not off) if counts else None,
        "; ".join(off) or (", ".join(f"{k}: {v}" for k, v in counts.items()) or "no steps found"))
    return out


# ---------------------------------------------------------------------------
# Verification workbook (spec 11)
# ---------------------------------------------------------------------------


def workbook_name(task_code: str, base_label: str | None, new_label: str, when: dt.date | None = None) -> str:
    safe = lambda x: re.sub(r'[\\/:*?"<>|]+', "-", str(x or "baseline"))  # noqa: E731
    return f"Task {task_code} verification {safe(base_label)} to {safe(new_label)} " \
           f"{(when or dt.date.today()).isoformat()}.xlsx"


def build_workbook(report_id: int, new_version_id: int) -> tuple[bytes, str]:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    r = load(report_id, new_version_id)
    state = impact.load_state(report_id)
    wb = openpyxl.Workbook()
    hdr_fill = PatternFill("solid", fgColor="FFD9D9D9")

    def sheet(title: str, header: list[str], rows: list[list[Any]], widths: list[int], first: bool = False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True), hdr_fill
        for row in rows:
            ws.append([("" if v is None else v) for v in row])
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        return ws

    sheet("Expected changes", ["Card", "Artifact", "Expected change", "Result", "Automatic result", "Override by",
                               "Evidence"],
          [[e["card"], e["artifact"], e["description"], e["result"], e["auto"], e["override_by"], e["evidence"]]
           for e in r["expected"]], [8, 12, 60, 12, 12, 12, 80], first=True)
    sheet("Untracked changes", ["Artifact", "Change", "File", "Where", "Before", "After", "Status"],
          [[u["artifact"], u["desc"], u["file"], u["loc"], str(u["old"] or "")[:2000], str(u["new"] or "")[:2000],
            u["status"]] for u in r["untracked"]], [12, 60, 30, 14, 40, 40, 10])
    integ_rows = []
    for vid, lab in ((state.version_id, r["baseline_label"]), (new_version_id, r["new_label"])):
        for c in integrity(vid):
            integ_rows.append([lab, c["check"], {True: "pass", False: "FAIL", None: "n/a"}[c["ok"]], c["detail"]])
    sheet("Integrity", ["Version", "Check", "Result", "Detail"], integ_rows, [10, 50, 8, 90])
    rr = r["rerun"]
    rows = [["Re-run required", "yes" if rr["required"] else "no"], ["Cards causing it", ", ".join(rr["cards"])],
            ["Status", rr["status"]]]
    if "model" in rr:
        rows.append(["New model score", f"{rr['model']['score_pct']:.1%}" + (" BREAK" if rr["model_breaks"] else "")])
        for idx, b in sorted(rr.get("failure_notes", {}).items()):
            rows.append([f"Failure note {idx} backing", f"{b:g} pts" + (" (gone)" if not b else "")])
    if "gold" in rr:
        rows.append(["New gold score", f"{rr['gold']['score_pct']:.1%}" + (" BREAK" if rr["gold_breaks"] else "")])
    sheet("Re-run", ["Item", "Value"], rows, [30, 60])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), workbook_name(state.task_code, r["baseline_label"], r["new_label"])
