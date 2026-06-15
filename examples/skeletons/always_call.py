"""Skeleton agent: always checks if legal, else calls, else folds.

Use this to sanity-check that the submission pipeline works:

    pokerkit run --agent examples/skeletons/always_call.py --max-hands 5

You will burn chips on bad calls, but the loop runs.
"""
from __future__ import annotations

from typing import Optional


def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    available = allowed.get("availableActions") or []
    if "check" in available:
        action = "check"
    elif "call" in available:
        action = "call"
    else:
        action = "fold" if "fold" in available else (available[0] if available else "fold")
    return {
        "action": action,
        "message": "always-call skeleton: pipeline sanity check",
        "reasoning": '{vr: "skip", ke: "pre-bot", pp: "calling station"}',
    }
