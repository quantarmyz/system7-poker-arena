"""Skeleton agent: picks a random legal action.

Useful for sanity-checking the submission pipeline AND for testing your
own evaluator against an unpredictable opponent. Bet/raise sizings are
random within the allowed range.

    pokerkit run --agent examples/skeletons/random_action.py --max-hands 5
"""
from __future__ import annotations

import random
from typing import Optional

_RNG = random.Random(2026)


def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    available = list(allowed.get("availableActions") or ["fold"])
    if not available:
        available = ["fold"]
    action = _RNG.choice(available)
    payload: dict = {
        "action": action,
        "message": f"random skeleton: chose {action}",
        "reasoning": '{vr: "rng", ke: "n/a", pp: "monkey"}',
    }
    if action == "bet":
        br = allowed.get("betRange") or {}
        lo, hi = int(br.get("min") or 1), int(br.get("max") or 1)
        if hi >= lo:
            payload["amount"] = _RNG.randint(lo, hi)
    elif action == "raise":
        rr = allowed.get("raiseRange") or {}
        lo, hi = int(rr.get("min") or 1), int(rr.get("max") or 1)
        if hi >= lo:
            payload["amount"] = _RNG.randint(lo, hi)
    elif action == "all-in":
        all_in_to = allowed.get("allInToAmount")
        if all_in_to is not None:
            payload["amount"] = int(all_in_to)
    return payload
