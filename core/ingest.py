"""Task-zip ingest (spec section 3.1).

Flow:
1. `stage_zip()` extracts the zip recursively into DATA_DIR and suggests a role
   for every file. Nothing is written to the database yet.
2. The user confirms or corrects the roles on the confirm screen.
3. `save_version()` persists the version, files, criteria and failure notes.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from core import config
from core.extract import DELIVERABLE_EXTS, extract_file, text_view

ROLES = ["prompt", "rubric", "trajectory", "failure_notes", "gold", "reference", "model_output",
         "qa_sheet", "other"]

_JUNK = re.compile(r"(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|desktop\.ini|~\$[^/]*)(/|$)", re.I)
MAX_NESTING = 5


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Recursive extraction
# ---------------------------------------------------------------------------


@dataclass
class StagedFile:
    name: str  # path relative to the zip root, nested zips become folders
    path: str  # absolute path on disk
    sha256: str
    suggested_role: str
    reason: str
    role: str = ""

    def __post_init__(self) -> None:
        if not self.role:
            self.role = self.suggested_role


@dataclass
class Staged:
    zip_name: str
    zip_sha256: str
    root: str
    files: list[StagedFile] = field(default_factory=list)
    rubric: dict[str, Any] | None = None
    rubric_error: str | None = None
    suggested_label: str | None = None
    suggested_task: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _safe_member(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return None
    return p


def _extract_into(zf: zipfile.ZipFile, dest: Path, prefix: PurePosixPath, depth: int,
                  out: list[tuple[str, Path]], warnings: list[str]) -> None:
    for info in zf.infolist():
        if info.is_dir() or _JUNK.search(info.filename):
            continue
        rel = _safe_member(info.filename)
        if rel is None:
            warnings.append(f"Skipped unsafe path in zip: {info.filename}")
            continue
        data = zf.read(info)
        logical = prefix / rel
        if rel.suffix.lower() == ".zip":
            if depth >= MAX_NESTING:
                warnings.append(f"Nested zip too deep, not opened: {logical}")
            else:
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as inner:
                        _extract_into(inner, dest, logical.with_suffix(""), depth + 1, out, warnings)
                    continue
                except zipfile.BadZipFile:
                    warnings.append(f"Could not open nested zip: {logical}")
        target = dest / logical
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        out.append((str(logical), target))


def _strip_single_root(files: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """If every file sits under one top folder, drop it from the logical name."""
    tops = {PurePosixPath(n).parts[0] for n, _ in files if len(PurePosixPath(n).parts) > 1}
    if len(tops) == 1 and all(len(PurePosixPath(n).parts) > 1 for n, _ in files):
        return [(str(PurePosixPath(*PurePosixPath(n).parts[1:])), p) for n, p in files]
    return files


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


def load_rubric(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or "rubric_sections" not in data:
        raise ValueError("rubric.json has no rubric_sections")
    return data


def rubric_criteria(rubric: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for sec in rubric.get("rubric_sections", []):
        for c in sec.get("criteria", []):
            out.append({
                "crit_id": str(c.get("id")),
                "subtask": str(sec.get("subtask", "")),
                "title": sec.get("title"),
                "text": c.get("criterion", ""),
                "points": float(c.get("points", 0) or 0),
                "category": c.get("category"),
                "references": c.get("references") or [],
            })
    return out


def rubric_summary(rubric: dict[str, Any]) -> dict[str, Any]:
    crits = rubric_criteria(rubric)
    weights = sum(float(s.get("weight", 0) or 0) for s in rubric.get("rubric_sections", []))
    net = sum(c["points"] for c in crits)
    penalties = [c for c in crits if c["points"] < 0 or (c["category"] or "").lower() == "penalty"]
    return {
        "total_points": rubric.get("total_points"),
        "sum_section_weights": weights,
        "net_criteria_points": net,
        "criteria": len(crits),
        "penalties": len(penalties),
        "sections": len(rubric.get("rubric_sections", [])),
        "consistent": rubric.get("total_points") is not None
        and abs(float(rubric["total_points"]) - weights) < 1e-6
        and abs(weights - net) < 1e-6,
    }


def _norm(name: str) -> str:
    """Normalise a file name for matching: basename, lower case, no extension, collapse spaces/_/-."""
    base = PurePosixPath(name.replace("\\", "/")).name.lower()
    stem = re.sub(r"\.[a-z0-9]{1,5}$", "", base)
    return re.sub(r"[\s_\-]+", " ", stem).strip()


def _rubric_refs(rubric: dict[str, Any] | None) -> dict[str, set[str]]:
    """Map normalised source name -> set of reference types."""
    refs: dict[str, set[str]] = {}
    if not rubric:
        return refs
    for c in rubric_criteria(rubric):
        for r in c["references"]:
            src = r.get("source")
            if not src:
                continue
            refs.setdefault(_norm(str(src)), set()).add(str(r.get("type") or ""))
    return refs


# ---------------------------------------------------------------------------
# Role suggestion
# ---------------------------------------------------------------------------


def suggest_role(name: str, refs: dict[str, set[str]]) -> tuple[str, str]:
    p = PurePosixPath(name)
    base = p.name.lower()
    stem = p.stem.lower() if p.suffix else base
    ext = p.suffix.lower()
    n = _norm(name)
    types = refs.get(n, set())
    folders = " ".join(x.lower() for x in p.parts[:-1])

    if base == "rubric.json":
        return "rubric", "named rubric.json"
    if "gold-trajectory" in base or "gold_trajectory" in base or "gold trajectory" in base:
        return "trajectory", "name contains gold-trajectory"
    if "where-model-failed" in base or "where_model_failed" in base or "where model failed" in base:
        return "failure_notes", "name contains where-model-failed"
    if "qa_feedback" in base or "qa feedback" in folders:
        return "qa_sheet", "prior QA sheet (qa_feedback / QA feedback folder)"
    if stem == "prompt" and ext in {".txt", ".md", ".docx", ""}:
        return "prompt", "file named prompt"
    if "gold_deliverable" in types:
        return "gold", "named in rubric references as gold_deliverable"
    if "reference_file" in types:
        return "reference", "named in rubric references as reference_file"
    if "reference" in folders or "input" in folders:
        return "reference", "inside a reference/input folder"
    if ext in DELIVERABLE_EXTS:
        return "model_output", "deliverable-type file not referenced by the rubric"
    if ext in {".json", ".txt", ".md"} and ("rubric" in base or "trajectory" in base):
        return "other", "looks like task metadata"
    return "reference", "other input file"


def parse_label(zip_name: str) -> str | None:
    m = re.search(r"(?<![A-Za-z0-9])v(\d+)(?![0-9])", zip_name, re.I)
    return f"v{m.group(1)}" if m else None


def parse_task_code(zip_name: str) -> str | None:
    m = re.search(r"task[\s_\-]*0*(\d+)", zip_name, re.I) or re.match(r"0*(\d+)[\s_\-]", zip_name)
    return m.group(1) if m else None


def staging_base() -> Path:
    """Where zips are unpacked before saving: the system temp folder, outside OneDrive.

    OneDrive (and antivirus) can lock freshly written folders, so staging never deletes and
    reuses a folder: each upload gets a fresh one, and old ones are tidied up when possible.
    """
    import tempfile

    base = Path(os.getenv("STAGING_DIR") or Path(tempfile.gettempdir()) / "TaskReworkDashboard" / "staging")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _force_remove(func, path, _exc) -> None:
    """rmtree error handler: clear the read-only flag and try once more; give up quietly."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """Delete a folder if Windows lets us. Returns False (never raises) if something holds it."""
    for _ in range(3):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_force_remove)
            else:
                shutil.rmtree(path, onerror=_force_remove)
        except OSError:
            pass
        if not Path(path).exists():
            return True
        time.sleep(0.2)
    return False


def _new_staging_dir(digest: str) -> Path:
    base = staging_base()
    cutoff = time.time() - 24 * 3600
    for old in base.iterdir():  # tidy up folders from earlier days
        try:
            if old.is_dir() and old.stat().st_mtime < cutoff:
                remove_tree(old)
        except OSError:
            pass
    root = base / f"{digest[:16]}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    return root


def stage_zip(data: bytes, zip_name: str) -> Staged:
    """Extract a task zip and suggest roles. Raises ValueError for a non-zip."""
    digest = sha256_bytes(data)
    root = _new_staging_dir(digest)
    (root / "_original.zip").write_bytes(data)
    warnings: list[str] = []
    found: list[tuple[str, Path]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            _extract_into(zf, root / "files", PurePosixPath(""), 0, found, warnings)
    except zipfile.BadZipFile as e:
        raise ValueError(f"{zip_name} is not a valid zip file") from e
    tops = {PurePosixPath(n).parts[0] for n, _ in found}
    top = next(iter(tops)) if len(tops) == 1 else ""
    found = _strip_single_root(found)

    staged = Staged(zip_name=zip_name, zip_sha256=digest, root=str(root),
                    suggested_label=parse_label(zip_name) or parse_label(top),
                    suggested_task=parse_task_code(zip_name) or parse_task_code(top),
                    warnings=warnings)

    rubric_files = [(n, p) for n, p in found if PurePosixPath(n).name.lower() == "rubric.json"]
    if not rubric_files:
        staged.rubric_error = "No rubric.json found in the zip"
    else:
        if len(rubric_files) > 1:
            warnings.append(f"{len(rubric_files)} rubric.json files found; using {rubric_files[0][0]}")
        try:
            staged.rubric = load_rubric(rubric_files[0][1].read_bytes())
        except (ValueError, json.JSONDecodeError) as e:
            staged.rubric_error = f"rubric.json could not be read: {e}"

    refs = _rubric_refs(staged.rubric)
    for n, p in sorted(found):
        role, why = suggest_role(n, refs)
        staged.files.append(StagedFile(name=n, path=str(p), sha256=sha256_file(p),
                                       suggested_role=role, reason=why))

    # Rubric sources that we couldn't find in the zip.
    names = {_norm(f.name) for f in staged.files}
    missing = [s for s, t in refs.items() if s not in names and not t & {"derived", "task_prompt"}
               and s not in {"derived", "task prompt", "prompt"}]
    if missing:
        warnings.append("Rubric references not found in the zip: " + ", ".join(sorted(missing)))
    return staged


# ---------------------------------------------------------------------------
# Failure notes
# ---------------------------------------------------------------------------


def split_failure_notes(text: str) -> list[str]:
    """Split a where-model-failed file into items.

    Numbered items ("1.", "1)", "Failure 1:") win; otherwise top-level bullets;
    otherwise blank-line separated paragraphs.
    """
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    # JSON list?
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [d if isinstance(d, str) else json.dumps(d) for d in data]
        except json.JSONDecodeError:
            pass
    patterns = [
        r"^\s*(?:#+\s*)?(?:failure|issue|note)?\s*\d+\s*[.):\-]\s+",
        r"^\s*(?:#+\s*)?(?:failure|issue|note)\s*\d+\b",
        r"^\s*[-*•]\s+",
    ]
    for pat in patterns:
        rx = re.compile(pat, re.I | re.M)
        starts = [m.start() for m in rx.finditer(text)]
        if len(starts) >= 1:
            chunks = []
            head = text[: starts[0]].strip()
            for i, s in enumerate(starts):
                end = starts[i + 1] if i + 1 < len(starts) else len(text)
                chunks.append(text[s:end].strip())
            if head and len(chunks) >= 1 and len(head) > 200:
                chunks.insert(0, head)  # long preamble is likely its own note
            return [c for c in chunks if c]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paras


# ---------------------------------------------------------------------------
# Persisting a confirmed version
# ---------------------------------------------------------------------------


def save_version(staged: Staged, task_code: str, label: str, roles: dict[str, str],
                 title: str | None = None) -> int:
    """Persist a confirmed upload. Returns the version id.

    Re-uploading an identical zip under the same label reuses the existing version
    (and updates its roles if they were corrected). The same zip under a new label
    is saved as a new version, so it can be verified against the baseline.
    """
    from db.models import Criterion, FailureNote, File, Task, Version
    from db.session import session_scope

    with session_scope() as s:
        task = s.query(Task).filter_by(task_code=task_code).one_or_none()
        if task is None:
            task = Task(task_code=task_code, title=title)
            s.add(task)
            s.flush()
        existing = s.query(Version).filter_by(task_id=task.id, zip_sha256=staged.zip_sha256, label=label).one_or_none()
        if existing is not None:
            for f in existing.files:
                if f.name in roles and roles[f.name] != f.role:
                    f.role = roles[f.name]
            _refresh_failure_notes(s, existing)
            return existing.id
        clash = s.query(Version).filter_by(task_id=task.id, label=label).one_or_none()
        if clash is not None:
            raise ValueError(f"Task {task_code} already has a different version labelled {label}")

        version = Version(task_id=task.id, label=label, zip_sha256=staged.zip_sha256)
        s.add(version)
        s.flush()

        vdir = config.data_path("versions", str(version.id), "x").parent
        src_root = Path(staged.root)
        shutil.copy2(src_root / "_original.zip", vdir / "original.zip")
        for sf in staged.files:
            src = Path(sf.path)
            dest = vdir / "files" / sf.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            ex, meta, parts = extract_file(dest)
            text_path = vdir / "extract" / (sf.name + ".json")
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(json.dumps({"extraction": ex, "text": text_view(ex)}, default=str))
            s.add(File(version_id=version.id, role=roles.get(sf.name, sf.role), name=sf.name,
                       sha256=sf.sha256, text_path=str(text_path.relative_to(config.DATA_DIR)),
                       metadata_json=json.dumps(meta, default=str), parts_json=json.dumps(parts)))
        if staged.rubric:
            for c in rubric_criteria(staged.rubric):
                s.add(Criterion(version_id=version.id, crit_id=c["crit_id"], subtask=c["subtask"],
                                title=c["title"], text=c["text"], points=c["points"],
                                category=c["category"], references_json=json.dumps(c["references"])))
        s.flush()
        _refresh_failure_notes(s, version)
        vid = version.id
    _load_qa_sheets(staged, task_code, roles)
    return vid


def _load_qa_sheets(staged: Staged, task_code: str, roles: dict[str, str]) -> None:
    """A prior QA sheet inside the zip is loaded the same way as an uploaded one."""
    from core.report_parser import ReportError, parse_qa_sheet, save_qa_items

    for sf in staged.files:
        if roles.get(sf.name, sf.role) == "qa_sheet":
            try:
                items = parse_qa_sheet(Path(sf.path).read_bytes(), sf.name)
            except ReportError as e:
                staged.warnings.append(str(e))
                continue
            save_qa_items(task_code, items, sf.name)


def _refresh_failure_notes(s, version) -> None:
    from db.models import FailureNote, File

    s.flush()
    s.query(FailureNote).filter_by(version_id=version.id).delete()
    notes_files = s.query(File).filter_by(version_id=version.id, role="failure_notes").order_by(File.name).all()
    idx = 0
    for f in notes_files:
        text = load_text(f.text_path)
        for item in split_failure_notes(text):
            idx += 1
            s.add(FailureNote(version_id=version.id, idx=idx, text=item))


def load_extraction(text_path: str | None) -> dict[str, Any]:
    if not text_path:
        return {}
    p = config.DATA_DIR / text_path
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_text(text_path: str | None) -> str:
    return load_extraction(text_path).get("text", "")
