"""Skeleton agent: always folds (or checks if fold is illegal).

Use this to sanity-check that the submission pipeline works end-to-end
before you plug in your real model:

    pokerkit run --agent examples/skeletons/always_fold.py --max-hands 5

You will lose every hand, but the loop, registration, introspection,
action submission, and reasoning YAML are all exercised.
"""
from __future__ import annotations

from typing import Optional


def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    available = allowed.get("availableActions") or []
    action = "fold" if "fold" in available else (
        "check" if "check" in available else (available[0] if available else "fold"))
    return {
        "action": action,
        "message": "always-fold skeleton: pipeline sanity check",
        "reasoning": '{vr: "skip", ke: "0% eq", pp: "fold"}',
    }
