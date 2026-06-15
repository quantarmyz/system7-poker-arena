"""Reference implementation #3 — adds board-texture-aware sizing.

Builds on decide_ranged.py by adding: dry vs wet board detection,
in-position vs out-of-position adjustment, and sizing table per
texture. This is where most of the bb/100 lift comes from in a real
HL iteration.

To use:
  ./pokerkit run --agent assets/decide_textured.py --max-hands 50

Or copy into `examples/agent.py`.
"""
from typing import Optional

FALLBACK_REASONING = '{vr: "std", ke: "legal", pp: "pot control"}'

# Reuse OPENING_RANGES + SEAT_TO_POS + _hand_class from decide_ranged.py
# in a real edit; inlined here for self-containment.
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
SEAT_TO_POS = {1: "BTN", 2: "SB", 3: "BB", 4: "UTG", 5: "MP", 6: "CO"}

# Bet sizing per board texture (fraction of pot).
SIZING = {
    "dry":      {"flop": 0.33, "turn": 0.50, "river": 0.66},
    "wet":      {"flop": 0.66, "turn": 0.75, "river": 0.75},
    "neutral":  {"flop": 0.50, "turn": 0.60, "river": 0.66},
}


def _hand_class(hole):
    if len(hole) != 2: return ""
    ranks = "23456789TJQKA"
    r1, s1 = hole[0][0].upper(), hole[0][-1].lower()
    r2, s2 = hole[1][0].upper(), hole[1][-1].lower()
    if r1 not in ranks or r2 not in ranks: return ""
    if ranks.index(r1) < ranks.index(r2):
        r1, r2, s1, s2 = r2, r1, s2, s1
    if r1 == r2: return r1 + r2
    return f"{r1}{r2}{'s' if s1 == s2 else 'o'}"


def _board_texture(board: list[str]) -> str:
    """Classify board as dry / wet / neutral based on suits + connectedness."""
    if len(board) < 3:
        return "neutral"
    suits = [c[-1].lower() for c in board]
    ranks = sorted("23456789TJQKA".index(c[0].upper()) for c in board
                   if c[0].upper() in "23456789TJQKA")
    monotone = len(set(suits)) == 1
    two_tone = len(set(suits)) == 2 and max(suits.count(s) for s in set(suits)) >= 2
    connected = (max(ranks) - min(ranks)) <= 4 if ranks else False
    paired = len(set(c[0] for c in board)) < len(board)
    if monotone or (two_tone and connected):
        return "wet"
    if paired and not connected:
        return "dry"
    if connected:
        return "wet"
    return "dry"


def _street(boardCards: list[str]) -> str:
    return ("flop", "turn", "river")[max(0, min(len(boardCards) - 3, 2))] \
        if boardCards else "preflop"


def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    available = allowed.get("availableActions") or []

    if deadline_s < 2.0:
        return {"action": "check" if allowed.get("canCheck") else "fold",
                "message": "deadline tight", "reasoning": FALLBACK_REASONING}

    self_seat_num = table.get("selfSeatNumber") or 0
    seats = table.get("seats") or []
    self_seat = next((s for s in seats if s.get("seatNumber") == self_seat_num), {})
    hole = list(self_seat.get("holeCards") or [])
    board = list(table.get("boardCards") or [])
    street = _street(board)
    pos = SEAT_TO_POS.get(self_seat_num, "MP")
    cls = _hand_class(hole)
    pot = int(table.get("potChips") or 0)
    call_chips = int(allowed.get("callChips") or 0)

    # Preflop — same as ranged.
    if street == "preflop" and call_chips == 0 and cls in OPENING_RANGES.get(pos, set()):
        rr = allowed.get("raiseRange") or {}
        lo = int(rr.get("min") or 4)
        hi = int(rr.get("max") or lo)
        amt = max(lo, min(lo * 2, hi))
        action = "raise" if "raise" in available else ("bet" if "bet" in available else "check")
        if action in ("raise", "bet"):
            return {"action": action, "amount": amt,
                    "message": f"open {cls} from {pos}",
                    "reasoning": f'{{vr: "std", ke: "{cls} open", pp: "IP barrel T", sr: "2.5bb open"}}'}

    if street == "preflop":
        if call_chips > 0 and cls not in (OPENING_RANGES.get("BB", set()) | {"99","88","KQs","QJs","JTs"}):
            return {"action": "fold", "message": f"{cls} not in defend",
                    "reasoning": FALLBACK_REASONING}
        if call_chips > 0 and "call" in available:
            return {"action": "call", "message": f"defend {cls}",
                    "reasoning": FALLBACK_REASONING}
        if "check" in available:
            return {"action": "check", "message": "free preflop",
                    "reasoning": FALLBACK_REASONING}
        return {"action": "fold", "message": "no preflop play",
                "reasoning": FALLBACK_REASONING}

    # Postflop with texture-aware sizing.
    texture = _board_texture(board)
    pair_with_board = bool({c[0].upper() for c in hole} & {c[0].upper() for c in board})

    if call_chips == 0:
        if pair_with_board and ("bet" in available or "raise" in available):
            size = SIZING[texture][street]
            br = allowed.get("betRange") or allowed.get("raiseRange") or {}
            lo = int(br.get("min") or 1)
            hi = int(br.get("max") or lo)
            amt = max(lo, min(int(pot * size), hi))
            act = "bet" if "bet" in available else "raise"
            return {"action": act, "amount": amt,
                    "message": f"{texture} {street} cbet {int(size*100)}%",
                    "reasoning": f'{{vr: "std", ke: "{cls} pair", bf: [{texture}], pp: "IP barrel T", sr: "{int(size*100)}% pot"}}'}
        return {"action": "check" if "check" in available else "fold",
                "message": f"check {street} on {texture}", "reasoning": FALLBACK_REASONING}

    pot_odds = call_chips / max(pot + call_chips, 1)
    if pair_with_board and pot_odds < 0.4 and "call" in available:
        return {"action": "call", "message": f"call pair on {texture}",
                "reasoning": f'{{vr: "ln:bet", ke: "pot odds {int(pot_odds*100)}%", bf: [{texture}], pp: "showdown"}}'}
    return {"action": "fold", "message": f"fold to bet on {texture}",
            "reasoning": FALLBACK_REASONING}
