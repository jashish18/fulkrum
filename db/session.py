"""Database engine and session helpers."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core import config
from db.models import AuditLog, Base

_engine: Engine | None = None
_Session: sessionmaker | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _Session
    if _engine is None or url is not None:
        url = url or f"sqlite:///{config.data_path('app.db')}"
        _engine = create_engine(url, future=True)
        Base.metadata.create_all(_engine)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def audit(s: Session, user: str | None, entity: str, entity_id: Any, before: Any, after: Any) -> None:
    """Write one override to the audit log (spec section 15)."""
    s.add(
        AuditLog(
            user=user,
            entity=entity,
            entity_id=str(entity_id),
            before_json=json.dumps(before, default=str),
            after_json=json.dumps(after, default=str),
        )
    )
