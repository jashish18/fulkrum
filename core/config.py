"""App settings, read from environment variables (and a local .env file if present)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional
    pass


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


DATA_DIR = Path(os.getenv("DATA_DIR") or "./data").resolve()
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or "https://openrouter.ai/api/v1"
LLM_API_KEY = os.getenv("LLM_API_KEY") or ""
GRADER_MODEL = os.getenv("GRADER_MODEL") or "anthropic/claude-opus-5.5"
LLM_ZDR = _bool("LLM_ZDR", True)
APP_PASSWORD = os.getenv("APP_PASSWORD") or ""
MODEL_THRESHOLD = _float("MODEL_THRESHOLD", 0.95)
GOLD_THRESHOLD = _float("GOLD_THRESHOLD", 0.99)


def data_path(*parts: str) -> Path:
    """Every stored path goes through DATA_DIR."""
    p = DATA_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
