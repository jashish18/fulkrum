import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLES = ROOT / "samples"


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    """Each test gets its own DATA_DIR and database."""
    from core import config
    import db.session as dbs

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    dbs._engine = None
    dbs._Session = None
    yield tmp_path / "data"
    dbs._engine = None


def sample_zip(task: str) -> Path:
    """Find the task zip for a task number in samples/ (skip the test if it's missing)."""
    if not SAMPLES.exists():
        pytest.skip("samples/ folder not present")
    cands = [p for p in SAMPLES.rglob("*.zip")
             if task in p.name and "rework" not in p.name.lower() and "qa_feedback" not in p.name.lower()]
    if not cands:
        pytest.skip(f"no task {task} zip in samples/")
    return sorted(cands, key=lambda p: -p.stat().st_size)[0]
