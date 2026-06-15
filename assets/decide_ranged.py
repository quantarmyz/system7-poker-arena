"""Reference implementation #2 — adds position-aware OPENING_RANGES.

This is what a Heuristic Learning agent typically produces from a
STRATEGY.md that specifies hand ranges. Pure data tables baked into
Python; no LLM at runtime.

To use:
  ./pokerkit run --agent assets/decide_ranged.py --max-hands 50

Or copy the OPENING_RANGES + decide() body into `examples/agent.py`.
"""
from typing import Optional

FALLBACK_REASONING = '{vr: "std", ke: "legal", pp: "pot control"}'

# 6-max NLHE opening ranges. Replace with your STRATEGY.md ranges.
OPENING_RANGES = {
    "UTG": {"AA","KK","QQ","JJ","TT","99","AKs","AKo","AQs"},
    "MP":  {"AA","KK","QQ","JJ","TT","99","88","AKs","AKo","AQs","AQo","AJs","KQs"},
    "CO":  {"AA","KK","QQ","JJ","TT","99","88","77","66","55",
            "AKs","AKo","AQs","AQo","AJs","AJo","ATs","KQs","KQo","KJs","KTs",
            "QJs","QTs","JTs","T9s","98s"},
    "BTN": {"AA","KK","QQ","JJ","TT","99","88","77","66","55","44","33","22",
            "AKs","AKo","AQs","AQo","AJs","AJo","ATs","ATo",
            "A9s","A8s","A7s","A6s","A5s","A4s","A3s","A2s",
            "KQs","KQo","KJs","KJo","KTs","K9s",
            "QJs","QTs","Q9s","JTs","J9s","T9s","98s","87s","76s"},
    "SB":  {"AA","KK","QQ","JJ","TT","99","88","AKs","AKo","AQs","AQo","AJs","ATs","KQs","KJs","QJs"},
    "BB":  {"AA","KK","QQ","JJ","TT","AKs","AKo","AQs"},
}

# 6-max seat → position heuristic (correct only for canonical seating;
# real implementations should read seat.position once the field lands).
SEAT_TO_POS = {1: "BTN", 2: "SB", 3: "BB", 4: "UTG", 5: "MP", 6: "CO"}


def _hand_class(hole: list[str]) -> str:
    """['As','Ks'] -> 'AKs'. Returns '' if unparseable."""
    if len(hole) != 2:
        return ""
    ranks = "23456789TJQKA"
    r1, s1 = hole[0][0].upper(), hole[0][-1].lower()
    r2, s2 = hole[1][0].upper(), hole[1][-1].lower()
    if r1 not in ranks or r2 not in ranks:
        return ""
    if ranks.index(r1) < ranks.index(r2):
        r1, r2 = r2, r1
        s1, s2 = s2, s1
    if r1 == r2:
        return r1 + r2
    return f"{r1}{r2}{'s' if s1 == s2 else 'o'}"


def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    available = allowed.get("availableActions") or []

    if deadline_s < 2.0:
        action = "check" if allowed.get("canCheck") else "fold"
        return {"action": action, "message": "deadline tight",
                "reasoning": FALLBACK_REASONING}

    self_seat_num = table.get("selfSeatNumber") or 0
    seats = table.get("seats") or []
    self_seat = next((s for s in seats if s.get("seatNumber") == self_seat_num), {})
    hole = list(self_seat.get("holeCards") or [])
    street = (table.get("street") or "Preflop").lower()
    call_chips = int(allowed.get("callChips") or 0)
    pot = int(table.get("potChips") or 0)

    cls = _hand_class(hole)
    pos = SEAT_TO_POS.get(self_seat_num, "MP")

    # Preflop: range-based open / fold.
    if street == "preflop" and call_chips == 0 and cls:
        if cls in OPENING_RANGES.get(pos, set()):
            rr = allowed.get("raiseRange") or {}
            lo = int(rr.get("min") or 4)
            hi = int(rr.get("max") or lo)
            amt = max(lo, min(int(allowed.get("callChips", 0) * 2 or lo * 2), hi))
            if "raise" in available:
                return {"action": "raise", "amount": amt,
                        "message": f"open {cls} from {pos}",
                        "reasoning": f'{{vr: "std", ke: "{cls} open", bf: [], pp: "IP barrel T", sr: "2.5bb open"}}'}
            if "bet" in available:
                return {"action": "bet", "amount": amt,
                        "message": f"open {cls} from {pos}",
                        "reasoning": f'{{vr: "std", ke: "{cls} open", bf: [], pp: "IP barrel T", sr: "2.5bb open"}}'}
        if "check" in available:
            return {"action": "check", "message": f"{cls} not in {pos} range",
                    "reasoning": FALLBACK_REASONING}
        return {"action": "fold", "message": f"{cls} not in {pos} range",
                "reasoning": FALLBACK_REASONING}

    # Preflop facing a bet: defend with pairs + suited broadways + AKo.
    if street == "preflop" and call_chips > 0 and cls:
        defend = OPENING_RANGES.get("BB", set()) | {"99","88","77","KQs","KJs","QJs","JTs"}
        if cls in defend and "call" in available:
            return {"action": "call", "message": f"defend {cls}",
                    "reasoning": f'{{vr: "ln:open", ke: "{cls} def", pp: "OOP flop"}}'}
        return {"action": "fold", "message": f"{cls} not in defend range",
                "reasoning": FALLBACK_REASONING}

    # Postflop: simple connection check. Replace with real equity / texture logic.
    board = list(table.get("boardCards") or [])
    pair_with_board = bool({c[0].upper() for c in hole} & {c[0].upper() for c in board})
    if call_chips == 0:
        if pair_with_board and "bet" in available:
            br = allowed.get("betRange") or {}
            lo = int(br.get("min") or 1)
            hi = int(br.get("max") or lo)
            return {"action": "bet", "amount": max(lo, min(int(pot * 0.5), hi)),
                    "message": "value bet pair", "reasoning": FALLBACK_REASONING}
        return {"action": "check" if "check" in available else "fold",
                "message": "no pair", "reasoning": FALLBACK_REASONING}
    if pair_with_board and "call" in available:
        return {"action": "call", "message": "call with pair",
                "reasoning": FALLBACK_REASONING}
    return {"action": "fold", "message": "no equity",
            "reasoning": FALLBACK_REASONING}
