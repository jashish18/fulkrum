"""Settings stored in the database, falling back to environment defaults."""
from __future__ import annotations

from core import config

DEFAULTS = {
    "MODEL_THRESHOLD": lambda: str(config.MODEL_THRESHOLD),
    "GOLD_THRESHOLD": lambda: str(config.GOLD_THRESHOLD),
    "SCORE_FORMULA": lambda: "net",
    "GRADER_MODEL": lambda: config.GRADER_MODEL,
    "LLM_ZDR": lambda: "true" if config.LLM_ZDR else "false",
}


def get(key: str) -> str:
    from db.models import Setting
    from db.session import session_scope

    with session_scope() as s:
        row = s.get(Setting, key)
        if row is not None and row.value not in (None, ""):
            return row.value
    return DEFAULTS[key]()


def get_float(key: str) -> float:
    try:
        return float(get(key))
    except ValueError:
        return float(DEFAULTS[key]())


def put(key: str, value: str, user: str | None = None) -> None:
    from db.models import Setting
    from db.session import audit, session_scope

    with session_scope() as s:
        row = s.get(Setting, key)
        before = row.value if row else None
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        if before != value:
            audit(s, user, "settings", key, before, value)


def grader_model() -> str:
    return get("GRADER_MODEL")


def zdr() -> bool:
    return get("LLM_ZDR").lower() in {"1", "true", "yes", "on"}
