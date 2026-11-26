"""LLM client (OpenRouter through the openai SDK), with ZDR, call logging and a response cache.

Spec sections 7 and 15: every call is logged to llm_calls; outputs are stored and reused.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from core import config

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


class LLMError(RuntimeError):
    pass


def _model() -> str:
    try:
        from core import settings

        return settings.grader_model()
    except Exception:  # noqa: BLE001 - settings table may not exist yet
        return config.GRADER_MODEL


def _zdr() -> bool:
    try:
        from core import settings

        return settings.zdr()
    except Exception:  # noqa: BLE001
        return config.LLM_ZDR


def is_configured() -> bool:
    return bool(config.LLM_API_KEY and _model())


def load_prompt(name: str, version: str) -> str:
    return (PROMPTS_DIR / f"{name}_{version}.md").read_text()


def _client():
    from openai import OpenAI

    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)


def call_json(purpose: str, prompt_version: str, system: str, user: str, schema: dict[str, Any],
              model: str | None = None) -> dict[str, Any]:
    """One structured call. Cached by a hash of (model, prompt version, messages, schema)."""
    if not is_configured():
        raise LLMError("LLM_API_KEY and GRADER_MODEL must be set")
    model = model or _model()
    req = {"model": model, "prompt_version": prompt_version, "system": system, "user": user, "schema": schema}
    h = hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()
    cache = config.data_path("llm_cache", f"{h}.json")
    if cache.exists():
        return json.loads(cache.read_text())

    extra: dict[str, Any] = {"usage": {"include": True}}
    if _zdr():
        extra["provider"] = {"zdr": True, "data_collection": "deny"}
    t0 = time.time()
    try:
        resp = _client().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": purpose, "strict": True, "schema": schema}},
            extra_body=extra,
        )
    except Exception as e:  # noqa: BLE001
        raise LLMError(str(e)) from e
    latency = int((time.time() - t0) * 1000)
    content = resp.choices[0].message.content or "{}"
    try:
        out = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned invalid JSON: {content[:200]}") from e
    usage = getattr(resp, "usage", None)
    _log(purpose, model, prompt_version, usage, latency, h)
    cache.write_text(json.dumps(out))
    return out


def _log(purpose, model, prompt_version, usage, latency, h) -> None:
    from db.models import LLMCall
    from db.session import session_scope

    def g(name):
        return getattr(usage, name, None) if usage is not None else None

    with session_scope() as s:
        s.add(LLMCall(purpose=purpose, model=model, prompt_version=prompt_version,
                      tokens_in=g("prompt_tokens"), tokens_out=g("completion_tokens"),
                      cost=g("cost"), latency_ms=latency, request_hash=h))


OPTIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["options"],
    "properties": {
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "text"],
                "properties": {"key": {"type": "string", "enum": ["1", "2", "3", "fix"]},
                               "text": {"type": "string"}},
            },
        }
    },
}


def extract_options(text: str, part: str) -> dict[str, Any]:
    tmpl = load_prompt("extract_options", "v1")
    return call_json("extract_options", "v1", "Return only JSON that matches the schema.",
                     tmpl.format(part=part, text=text), OPTIONS_SCHEMA)


FALLBACK_MODEL = "anthropic/claude-opus-5.5"


def list_models() -> tuple[list[dict[str, Any]], str | None]:
    """Models the configured OpenRouter key can use, for the Settings dropdown.

    Tries /models/user (filtered by the account's provider settings), then /models.
    Returns (models, error). Each model: {id, name, prompt_price, completion_price}.
    """
    import urllib.error
    import urllib.request

    if not config.LLM_API_KEY:
        return [], "LLM_API_KEY isn't set, so the model list can't be fetched"
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    base = config.LLM_BASE_URL.rstrip("/")
    last_err = None
    for path in ("/models/user", "/models"):
        try:
            req = urllib.request.Request(base + path, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode()).get("data", [])
            out = []
            for m in data:
                pr = m.get("pricing") or {}
                out.append({"id": m.get("id"), "name": m.get("name") or m.get("id"),
                            "prompt_price": pr.get("prompt"), "completion_price": pr.get("completion")})
            out = [m for m in out if m["id"]]
            if out:
                return sorted(out, key=lambda m: m["name"].lower()), None
        except urllib.error.HTTPError as e:
            last_err = f"{path}: HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    return [], last_err or "No models returned"
