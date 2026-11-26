"""Option ops (spec 8.1): what each option or fix changes, as a list of typed operations.

Ops come from three places:
- `llm`: the LLM reads the option text, its scoped impact bullets and the rubric;
- `rule`: simple suggestions from the parsed impact bullets (prompt and reference edits),
  so the re-run badge works even without an LLM;
- `user`: added or edited on the card page.
The user can edit and confirm any of them.
"""
from __future__ import annotations

import json
import re
from typing import Any

OP_TYPES: dict[str, list[str]] = {
    "reweight": ["crit", "to"],
    "delete": ["crit"],
    "add": ["text", "points", "category"],
    "reword": ["crit", "new_text"],
    "accept_alternative": ["crit", "value"],
    "model_flip": ["crit", "to", "reason"],
    "gold_flip": ["crit", "to", "reason"],
    "prompt_edit": ["text"],
    "reference_edit": ["file", "location", "change", "metadata_only"],
    "gold_edit": ["location", "change"],
    "trajectory_edit": ["step", "change"],
    "failure_note_edit": ["idx", "change"],
}
ARTIFACT_OF_OP = {
    "reweight": "rubric", "delete": "rubric", "add": "rubric", "reword": "rubric",
    "accept_alternative": "rubric", "model_flip": "model_run", "gold_flip": "gold",
    "prompt_edit": "prompt", "reference_edit": "reference", "gold_edit": "gold",
    "trajectory_edit": "trajectory", "failure_note_edit": "failure_notes",
}
RERUN_OPS = {"prompt_edit", "reference_edit"}


def validate(op: dict[str, Any]) -> list[str]:
    t = op.get("type")
    if t not in OP_TYPES:
        return [f"unknown op type {t!r}"]
    errs = []
    for f in OP_TYPES[t]:
        if f in ("reason", "metadata_only", "category"):
            continue
        if op.get(f) in (None, ""):
            errs.append(f"{t} needs '{f}'")
    if t == "reweight":
        try:
            float(op["to"])
        except (TypeError, ValueError, KeyError):
            errs.append("reweight 'to' must be a number")
    if t in ("model_flip", "gold_flip") and op.get("to") not in ("pass", "fail", "triggered", "not_triggered"):
        errs.append(f"{t} 'to' must be pass/fail (or triggered/not_triggered for a penalty)")
    return errs


def describe(op: dict[str, Any]) -> str:
    t = op.get("type")
    if t == "reweight":
        return f"Reweight [{op.get('crit')}] to {op.get('to')} points"
    if t == "delete":
        return f"Delete [{op.get('crit')}]"
    if t == "add":
        return f"Add a {op.get('points')}-point line: {op.get('text')}"
    if t == "reword":
        return f"Reword [{op.get('crit')}]: {op.get('new_text')}"
    if t == "accept_alternative":
        return f"Accept {op.get('value')!r} in [{op.get('crit')}]"
    if t == "model_flip":
        return f"Model {op.get('to')}s [{op.get('crit')}]" + (f" ({op['reason']})" if op.get("reason") else "")
    if t == "gold_flip":
        return f"Gold {op.get('to')}s [{op.get('crit')}]" + (f" ({op['reason']})" if op.get("reason") else "")
    if t == "prompt_edit":
        return f"Prompt: {op.get('text')}"
    if t == "reference_edit":
        meta = " (metadata only)" if op.get("metadata_only") else ""
        return f"{op.get('file')} {op.get('location') or ''}: {op.get('change')}{meta}"
    if t == "gold_edit":
        return f"Gold {op.get('location')}: {op.get('change')}"
    if t == "trajectory_edit":
        return f"Step {op.get('step')}: {op.get('change')}"
    if t == "failure_note_edit":
        return f"Failure note {op.get('idx')}: {op.get('change')}"
    return json.dumps(op)


# ---------------------------------------------------------------------------
# Targets, for conflict detection (8.6)
# ---------------------------------------------------------------------------


def _norm_file(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return re.sub(r"\.[a-z0-9]{1,5}$", "", base).strip()


def _rows_in(location: str | None) -> set[int]:
    loc = location or ""
    rows = {int(r) for r in re.findall(r"\b[A-Z]{1,3}\$?(\d+)\b", loc)}
    rows |= {int(r) for r in re.findall(r"\brows?\s+(\d+)", loc, re.I)}
    return rows


def targets(op: dict[str, Any]) -> list[tuple]:
    """Things an op touches. Two ops conflict when they share a target with different effects."""
    t = op.get("type")
    if t in ("reweight", "delete", "reword", "accept_alternative"):
        return [("crit", str(op.get("crit")))]
    if t == "model_flip":
        return [("model", str(op.get("crit")))]
    if t == "gold_flip":
        return [("goldgrade", str(op.get("crit")))]
    if t == "prompt_edit":
        return [("prompt",)]
    if t == "reference_edit":
        f = _norm_file(op.get("file"))
        rows = _rows_in(op.get("location"))
        return [("ref", f, r) for r in rows] if rows else [("ref", f, None)]
    if t == "gold_edit":
        rows = _rows_in(op.get("location"))
        return [("gold", r) for r in rows] if rows else [("gold", None)]
    if t == "trajectory_edit":
        return [("step", str(op.get("step")))]
    if t == "failure_note_edit":
        return [("note", str(op.get("idx")))]
    return []


def targets_overlap(a: tuple, b: tuple) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "ref":
        return a[1] == b[1] and (a[2] is None or b[2] is None or a[2] == b[2])
    if a[0] == "gold":
        return a[1] is None or b[1] is None or a[1] == b[1]
    return a == b


def effect(op: dict[str, Any]) -> str:
    """A comparable summary of what an op does to its target."""
    keys = [k for k in OP_TYPES.get(op.get("type", ""), []) if k not in ("reason",)]
    return json.dumps([op.get("type")] + [op.get(k) for k in keys], default=str)


# ---------------------------------------------------------------------------
# Rule-based suggestions from parsed impacts
# ---------------------------------------------------------------------------


def rule_suggestions(option_key: str, part: str, impacts: list[dict[str, Any]], option_text: str,
                     file_names: list[str]) -> list[dict[str, Any]]:
    """Prompt and reference edits that the parsed impacts already state.

    A bullet scoped to this option counts. For a 1b/1a fix, unscoped bullets count
    too, because the fix is the only change on offer.
    """
    out: list[dict[str, Any]] = []
    for imp in impacts:
        scope = imp.get("option_scope")
        in_scope = (scope and option_key in scope.split(",")) or (not scope and option_key == "fix")
        if not in_scope:
            continue
        text = imp["text"]
        if imp["artifact"] == "prompt":
            out.append({"type": "prompt_edit", "text": text})
        elif imp["artifact"] == "reference":
            files = (imp.get("refs") or {}).get("files") or []
            if not files:
                low = text.lower()
                files = [f for f in file_names if f.rsplit("/", 1)[-1].lower().rsplit(".", 1)[0] in low]
            meta = bool(re.search(r"\b(metadata|author|creator|lastModifiedBy|properties)\b", text, re.I))
            if not files:
                out.append({"type": "reference_edit", "file": "(unnamed reference file)", "location": "",
                            "change": text, "metadata_only": meta})
            for f in files:
                cells = list(dict.fromkeys(c["ref"] for c in (imp.get("refs") or {}).get("cells", [])))
                # "row 364" is a location; "Category 1, rows 8, 9" is a report row reference.
                rows = re.findall(r"(?<!Category \d, )\brows?\s+\d+", text)
                out.append({"type": "reference_edit", "file": f, "location": ", ".join(cells + rows),
                            "change": text, "metadata_only": meta})
    if option_key != "fix" and re.match(r"\s*Add\b.*\bprompt", option_text or "", re.I) and \
            not any(o["type"] == "prompt_edit" for o in out):
        out.append({"type": "prompt_edit", "text": option_text})
    return out


# ---------------------------------------------------------------------------
# LLM derivation
# ---------------------------------------------------------------------------

OPS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ops"],
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "crit", "to", "text", "points", "category", "new_text", "value",
                             "reason", "file", "location", "change", "metadata_only", "step", "idx"],
                "properties": {
                    "type": {"type": "string", "enum": list(OP_TYPES)},
                    "crit": {"type": ["string", "null"]}, "to": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]}, "points": {"type": ["number", "null"]},
                    "category": {"type": ["string", "null"]}, "new_text": {"type": ["string", "null"]},
                    "value": {"type": ["string", "null"]}, "reason": {"type": ["string", "null"]},
                    "file": {"type": ["string", "null"]}, "location": {"type": ["string", "null"]},
                    "change": {"type": ["string", "null"]}, "metadata_only": {"type": ["boolean", "null"]},
                    "step": {"type": ["string", "null"]}, "idx": {"type": ["integer", "null"]},
                },
            },
        }
    },
}


def llm_suggestions(option_label: str, option_text: str, impacts: list[dict[str, Any]],
                    rubric_lines: str) -> list[dict[str, Any]]:
    from core import llm

    tmpl = llm.load_prompt("derive_ops", "v1")
    bullets = "\n".join(f"- [{i['artifact']}{' / Option ' + i['option_scope'] if i.get('option_scope') else ''}] "
                        f"{i['text']}" for i in impacts) or "(none)"
    user = tmpl.format(option_label=option_label, option_text=option_text, impacts=bullets, rubric=rubric_lines)
    out = llm.call_json("derive_ops", "v1", "Return only JSON that matches the schema.", user, OPS_SCHEMA)
    ops = []
    for o in out.get("ops", []):
        clean = {k: v for k, v in o.items() if v is not None and k in (["type"] + OP_TYPES.get(o.get("type"), []))}
        if clean.get("type") == "reweight" and "to" in clean:
            try:
                clean["to"] = float(clean["to"])
            except ValueError:
                pass
        ops.append(clean)
    return ops
