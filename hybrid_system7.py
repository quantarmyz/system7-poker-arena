"""System 7 — HYBRID agent.

Routes each decision: the deterministic engine (decide_system7) handles the many
trivial/clear spots instantly and for free; MiniMax M3 (via llm_system7) is called
ONLY on genuinely hard postflop decisions. This cuts LLM calls ~70-80%, making a
full Eval feasible despite M3's ~27s/decision latency. On any LLM failure the call
falls back to the deterministic engine (llm_system7 wires that fallback).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)

import decide_system7 as H      # noqa: E402  deterministic engine + cheap feature helpers
import llm_system7              # noqa: E402,F401  wires MiniMax M3 into llm_agent
import llm_agent                # noqa: E402


def _is_hard(table: dict, deadline_s: float) -> bool:
    """Cheap gate (no equity Monte-Carlo): True => worth a slow LLM call."""
    allowed = table.get("allowedActions") or {}
    # MiniMax M3 takes ~27s; only call it when the deadline comfortably allows it
    # (else it would auto-fold). Tunable for PvP via S7_LLM_MIN_DEADLINE.
    if deadline_s < float(os.environ.get("S7_LLM_MIN_DEADLINE", "30")):
        return False
    board = list(table.get("boardCards") or [])
    if not board:
        return False                       # preflop: position ranges suffice
    seat_n = table.get("selfSeatNumber")
    me = next((s for s in (table.get("seats") or []) if s.get("seatNumber") == seat_n), {})
    hole = list(me.get("holeCards") or [])
    if len(hole) != 2:
        return False
    texture = H._texture(board)
    strength = H._strength(hole, board, texture)
    outs = H._adjusted_outs(hole, board, texture)
    facing = int(allowed.get("callChips") or 0) > 0
    street = ("flop", "turn", "river")[max(0, min(len(board) - 3, 2))]
    if strength == "MMF":
        return False                       # clear value / commit
    if facing:
        return not (strength == "AIR" and outs == 0)        # not just a clear air-fold
    if strength == "MM" or (strength in ("AIR", "MD") and outs >= 4):
        return True                        # thin value / semi-bluff / give-up
    if street == "river" and strength in ("MF", "MM", "MD"):
        return True
    return False


def decide(table: dict, deadline_s: float = 10.0, research_context=None) -> dict:
    if _is_hard(table, deadline_s):
        a = llm_agent.llm_decide(table, deadline_s=deadline_s, research_context=research_context)
        a["message"] = ("[M3] " + str(a.get("message", "")))[:500]
        a["m3"] = llm_system7.get_last_m3()               # raw model log (thread-local; PvP-safe)
        llm_system7.clear_last_m3()
        return a
    return H.decide(table, deadline_s=deadline_s, research_context=research_context)
