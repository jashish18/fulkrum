"""QA comment interpretation (spec 8.5).

`interpret()` uses the LLM when it is configured and falls back to a rule-based
reading of the guidance phrases in the spec. Either way the result is only an
editable suggestion: it is stored as a decision with source "qa_interpreted".
"""
from __future__ import annotations

import re
from typing import Any

REFUSING_STATUSES = {"objected", "defended", "kept"}
_STOP = set("the a an to of and or in on for is are be as it this that with we our by from at".split())


def is_keep_option(text: str | None) -> bool:
    return bool(re.match(r"\s*(keep|leave)\b", text or "", re.I))


def keep_option_key(options: dict[str, str]) -> str | None:
    for k in ("1", "2", "3"):
        if k in options and is_keep_option(options[k]):
            return k
    return None


def expand_scope(text: str) -> list[str]:
    """'6.13-6.15' / '6.13–6.15' / '6.13 to 6.15' / '6.12, 6.14' -> list of ids."""
    out: list[str] = []
    for m in re.finditer(r"(\d+)\.(\d+)\s*(?:-|–|—|to)\s*(?:(\d+)\.)?(\d+)", text):
        s1, a, s2, b = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4))
        if s1 == s2 and a <= b <= a + 60:
            out += [f"{s1}.{i}" for i in range(a, b + 1)]
    rest = re.sub(r"(\d+)\.(\d+)\s*(?:-|–|—|to)\s*(?:(\d+)\.)?(\d+)", " ", text)
    out += re.findall(r"\b\d+\.\d+\b", rest)
    return list(dict.fromkeys(out))


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in _STOP and len(w) > 2}


def _best_option(phrase: str, options: dict[str, str]) -> str | None:
    pt = _tokens(phrase)
    best, score = None, 0.0
    for k, txt in options.items():
        if k not in ("1", "2", "3"):
            continue
        head = " ".join(txt.split()[:12])
        ot = _tokens(head)
        if not ot or not pt:
            continue
        s = len(pt & ot) / len(pt | ot)
        if s > score:
            best, score = k, s
    return best if score >= 0.12 else None


def rule_interpret(qa: str | None, part: str, options: dict[str, str],
                   stances: list[dict[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {"option_key": "unclear", "scope_criteria": [], "new_option_text": "",
                           "candidates": [], "confidence": "low", "ambiguity_reason": "",
                           "rationale": "", "method": "rule"}
    text = (qa or "").strip()
    if not text:
        res["ambiguity_reason"] = "no QA input"
        return res
    low = text.lower()
    keep_key = keep_option_key(options) if part == "2" else None

    m = re.search(r"\buse option\s+(\d)\b(?:\s+for\s+((?:[^.;\n]|(?<=\d)\.(?=\d))+?))?(?:\s+only\b|[;\n]|\.(?!\d)|$)", low)
    if m:
        res.update(option_key=m.group(1), confidence="medium",
                   rationale=f"The comment says to use Option {m.group(1)}"
                             + (f" for {m.group(2).strip()}" if m.group(2) else "") + ".")
        res["candidates"] = [m.group(1)]
        if m.group(2):
            res["scope_criteria"] = expand_scope(m.group(2))
        others = set(re.findall(r"\boption\s+(\d)\b", low)) - {m.group(1)}
        em = re.search(r"\beither\b(.+?)\bor\b(.+?)(?:[.\n]|$)", low, re.S)
        if em:
            for clause in (em.group(1), em.group(2)):
                k = _best_option(clause, options)
                if k:
                    others.add(k)
        others.discard(m.group(1))
        if others:
            res["candidates"] += sorted(others)
            res["confidence"] = "low"
            res["ambiguity_reason"] = ("The comment also points to Option " + " and ".join(sorted(others))
                                       + (" (\"either … or …\")" if em else ""))
        return res

    em = re.search(r"\beither\b(.+?)\bor\b(.+?)(?:[.\n]|$)", low, re.S)
    if em:
        cands = [k for k in (_best_option(em.group(1), options), _best_option(em.group(2), options)) if k]
        res.update(candidates=list(dict.fromkeys(cands)),
                   ambiguity_reason="The comment offers two choices (\"either … or …\")",
                   rationale="Either/or comment.")
        return res

    if re.search(r"\b(agree|agreed|aligned|align)\b.{0,15}\bdefen[cs]e\b", low):
        statuses = {(s.get("status") or "").strip().lower() for s in stances}
        if statuses and statuses <= REFUSING_STATUSES:
            if part == "2" and keep_key:
                res.update(option_key=keep_key, candidates=[keep_key], confidence="medium",
                           rationale="Agrees with a prior defence or objection, so keep as is "
                                     f"(Option {keep_key}).")
            elif part != "2":
                res.update(option_key="keep", candidates=["keep"], confidence="medium",
                           rationale="Agrees with a prior defence or objection, so the fix is not applied.")
            else:
                res.update(option_key="keep", candidates=["keep"], confidence="low",
                           ambiguity_reason="Agrees with the defence, but no option is a plain keep-as-is",
                           rationale="Keep inferred from the prior stance.")
        elif not stances:
            res["ambiguity_reason"] = "Refers to a defence, but this card has no prior stance"
        else:
            kinds = ", ".join(sorted({s.get("status") or "?" for s in stances}))
            res["ambiguity_reason"] = (f"Refers to the defence, but the prior stance is not a defence or "
                                       f"objection ({kinds}), so no option matches it")
            res["candidates"] = [k for k in ("1", "2", "3") if k in options]
        return res

    if re.search(r"\b(this is ok|is ok|is fine|keep( as is| it)?|no change|leave as is)\b", low):
        k = keep_key if (part == "2" and keep_key) else "keep"
        res.update(option_key=k, candidates=[k], confidence="high" if keep_key or part != "2" else "medium",
                   rationale="The comment accepts the current state.")
        return res

    if part != "2":
        if re.search(r"\bdisagree\b", low):
            res.update(option_key="keep", candidates=["keep"], confidence="high", rationale="Disagrees with the fix.")
            return res
        if re.search(r"\bagree\b", low):
            res.update(option_key="fix", candidates=["fix"], confidence="high", rationale="Agrees with the fix.")
            return res

    m = re.search(r"\boption\s+(\d)\b", low)
    if m and len(set(re.findall(r"\boption\s+(\d)\b", low))) == 1:
        res.update(option_key=m.group(1), candidates=[m.group(1)], confidence="medium",
                   rationale=f"Names Option {m.group(1)} only.")
        return res

    res["ambiguity_reason"] = "The comment doesn't name an option the rules can recognise"
    return res


def llm_interpret(code: str, part: str, qa: str, options: dict[str, str],
                  stances: list[dict[str, Any]]) -> dict[str, Any]:
    from core import llm

    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["option_key", "scope_criteria", "new_option_text", "candidates", "confidence",
                     "ambiguity_reason", "rationale"],
        "properties": {
            "option_key": {"type": "string", "enum": ["1", "2", "3", "fix", "keep", "new", "unclear"]},
            "scope_criteria": {"type": "array", "items": {"type": "string"}},
            "new_option_text": {"type": "string"},
            "candidates": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "ambiguity_reason": {"type": "string"},
            "rationale": {"type": "string"},
        },
    }
    opts = "\n".join(f"- {'Option ' + k if k != 'fix' else 'Proposed fix'}: {v}" for k, v in options.items())
    stance = "\n".join(f"- QA #{s['qa_no']} ({s['status']}): {s['comment']}" for s in stances) or "(none)"
    user = llm.load_prompt("interpret_qa", "v1").format(code=code, part=part, options=opts, stance=stance, qa=qa)
    out = llm.call_json("interpret_qa", "v1", "Return only JSON that matches the schema.", user, schema)
    out["method"] = "llm"
    return out


def interpret(code: str, part: str, qa: str | None, options: dict[str, str],
              stances: list[dict[str, Any]], use_llm: bool = True) -> dict[str, Any]:
    from core import llm

    rule = rule_interpret(qa, part, options, stances)
    if not (qa or "").strip() or not use_llm or not llm.is_configured():
        return rule
    try:
        out = llm_interpret(code, part, qa or "", options, stances)
    except llm.LLMError:
        return rule
    # Keep the rule reader's warning if the LLM found none: a second opinion on ambiguity is cheap.
    if rule.get("ambiguity_reason") and not out.get("ambiguity_reason") and rule["option_key"] != out["option_key"]:
        out["ambiguity_reason"] = f"Rule reading differs ({rule['option_key']}): {rule['ambiguity_reason']}"
    return out
