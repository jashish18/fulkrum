"""Score from per-criterion results (spec section 7)."""
from __future__ import annotations

from typing import Iterable

FORMULAS = {
    "net": "(earned − penalty) / positive_total",
    "earned_only": "earned / positive_total",
}


def is_penalty(points: float, category: str | None) -> bool:
    return points < 0 or (category or "").lower() == "penalty"


def score(criteria: Iterable[dict], results: dict[str, str | None], formula: str = "net") -> dict:
    """criteria: dicts with crit_id, points, category. results: crit_id -> pass/fail/triggered/not_triggered."""
    earned = penalty = positive_total = 0.0
    failed: list[str] = []
    triggered: list[str] = []
    ungraded: list[str] = []
    for c in criteria:
        pts = float(c["points"])
        r = results.get(c["crit_id"])
        if is_penalty(pts, c.get("category")):
            if r == "triggered":
                penalty += abs(pts)
                triggered.append(c["crit_id"])
            elif r is None:
                ungraded.append(c["crit_id"])
            continue
        positive_total += pts
        if r == "pass":
            earned += pts
        elif r == "fail":
            failed.append(c["crit_id"])
        else:
            ungraded.append(c["crit_id"])
    if formula == "earned_only":
        pct = earned / positive_total if positive_total else 0.0
    else:
        pct = (earned - penalty) / positive_total if positive_total else 0.0
    return {
        "earned": earned, "penalty": penalty, "positive_total": positive_total,
        "score_pct": pct, "earned_pct": earned / positive_total if positive_total else 0.0,
        "failed": failed, "triggered": triggered, "ungraded": ungraded, "formula": formula,
    }
