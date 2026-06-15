"""Reference implementation #1 — baseline pot-odds + equity.

This is the simplest workable `decide()`. Equivalent in spirit to the
default in `examples/agent.py`, condensed for clarity. Pure pattern:
read table → compute pot odds → equity heuristic → pick action.

To use as a starting point:
  ./pokerkit run --agent assets/decide_baseline.py --max-hands 50

Or copy into `examples/agent.py decide()` and adapt.
"""
from typing import Optional

FALLBACK_REASONING = '{vr: "std", ke: "legal", pp: "pot control"}'


def _hand_class_strength(hole: list[str]) -> float:
    """Coarse 0.0-1.0 strength estimate from hole cards alone.
    Replace with treys MC equity for serious play (see examples/agent.py)."""
    if len(hole) != 2:
        return 0.5
    ranks = "23456789TJQKA"
    r1, r2 = hole[0][0].upper(), hole[1][0].upper()
    if r1 not in ranks or r2 not in ranks:
        return 0.5
    i1, i2 = ranks.index(r1), ranks.index(r2)
    suited = len(hole[0]) > 1 and len(hole[1]) > 1 and hole[0][-1] == hole[1][-1]
    if r1 == r2:                                    # pair
        return 0.55 + 0.04 * i1                     # 22→0.55, AA→0.95
    high, low = max(i1, i2), min(i1, i2)
    base = 0.30 + 0.025 * high + 0.015 * low
    if suited:
        base += 0.05
    if abs(i1 - i2) <= 1:                            # connected
        base += 0.03
    return min(0.85, base)


def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    available = allowed.get("availableActions") or []

    if deadline_s < 2.0:
        action = "check" if allowed.get("canCheck") else "fold"
        return {"action": action, "message": "deadline tight",
                "reasoning": FALLBACK_REASONING}

    self_seat_num = table.get("selfSeatNumber")
    seats = table.get("seats") or []
    self_seat = next((s for s in seats if s.get("seatNumber") == self_seat_num), {})
    hole = list(self_seat.get("holeCards") or [])

    pot = int(table.get("potChips") or 0)
    call_chips = int(allowed.get("callChips") or 0)
    pot_odds = call_chips / max(pot + call_chips, 1) if call_chips else 0.0
    equity = _hand_class_strength(hole)

    if call_chips == 0:
        if equity > 0.7 and allowed.get("canBet"):
            br = allowed.get("betRange") or {}
            lo = int(br.get("min") or 1)
            hi = int(br.get("max") or lo)
            amt = max(lo, min(int(pot * 0.66), hi))
            return {"action": "bet", "amount": amt,
                    "message": f"value bet (eq~{int(equity*100)}%)",
                    "reasoning": f'{{vr: "std", ke: "{int(equity*100)}% eq", bf: [], pp: "IP barrel T", sr: "66% pot"}}'}
        return {"action": "check" if "check" in available else "fold",
                "message": "free option", "reasoning": FALLBACK_REASONING}

    if equity < pot_odds - 0.05 and "fold" in available:
        return {"action": "fold", "message": f"price too high (eq~{int(equity*100)}%)",
                "reasoning": FALLBACK_REASONING}
    if equity >= pot_odds + 0.05 and "call" in available:
        return {"action": "call", "message": f"calling (eq~{int(equity*100)}%)",
                "reasoning": f'{{vr: "std", ke: "{int(equity*100)}% eq", pp: "OOP showdown"}}'}
    return {"action": "check" if "check" in available else "fold",
            "message": "marginal spot", "reasoning": FALLBACK_REASONING}
