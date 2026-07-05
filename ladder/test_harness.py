#!/usr/bin/env python
"""Smoke + fuzz OFFLINE del bot estático de la HU ladder (el bundle ya generado).
Éxito = 0 excepciones, 0 acciones fuera de la lista legal, importes en rango,
config HU cargada (_HU=True) y RFI de SB en spot unopened dentro de 55-95%.
    docker compose exec -T dashboard uv run python /data/ladder-src/test_harness.py
"""
import os
import random
import sys

HAR = os.path.join(os.environ.get("S7_LADDER_DIR", "/data/ladder"), "build", "harness")
sys.path.insert(0, HAR)
import strategy  # noqa: E402

RANKS = "23456789TJQKA"
SUITS = "shdc"
DECK = [r + s for r in RANKS for s in SUITS]


def deal(n, used):
    out = []
    while len(out) < n:
        c = random.choice(DECK)
        if c not in used and c not in out:
            out.append(c)
    return out


def mk_hu_table(hero_sb=True, hole=None, board=None, facing=0, pot=None, stack=970,
                avail=None, with_events=True):
    """Mesa HU realista con el shape de /texas/pending-actions."""
    board = board or []
    hole = hole or deal(2, board)
    sb_seat, bb_seat = (0, 1) if hero_sb else (1, 0)
    me_bet = 1 if (hero_sb and not board and facing == 0) else 0
    opp_bet = facing if facing else (2 if (not board and hero_sb) else 0)
    if pot is None:
        pot = 3 if not board else 40
    if avail is None:
        if facing:
            avail = ["fold", "call", "raise", "all-in"]
        else:
            avail = ["fold", "check", "bet", "all-in"] if board else ["fold", "call", "raise", "all-in"]
    call_chips = max(0, (opp_bet - me_bet)) if not board else (facing or 0)
    events = []
    if with_events and not board:
        events = [{"type": "BlindPosted", "sequence": 1, "summary": {"seatNumber": sb_seat, "amount": 1}},
                  {"type": "BlindPosted", "sequence": 2, "summary": {"seatNumber": bb_seat, "amount": 2}}]
    hero_seat = 0 if hero_sb else 1
    t = {
        "tableId": "t-test", "selfSeatNumber": hero_seat, "boardCards": board,
        "potChips": pot, "bigBlindChips": 2, "smallBlindChips": 1,
        "recentEvents": events,
        "seats": [
            {"seatNumber": 0, "status": "active", "holeCards": hole if hero_seat == 0 else [],
             "stackChips": stack, "currentBetChips": me_bet if hero_seat == 0 else opp_bet,
             "totalCommittedChips": (me_bet if hero_seat == 0 else opp_bet) + (0 if not board else 15)},
            {"seatNumber": 1, "status": "active", "holeCards": hole if hero_seat == 1 else [],
             "stackChips": stack, "currentBetChips": opp_bet if hero_seat == 0 else me_bet,
             "totalCommittedChips": (opp_bet if hero_seat == 0 else me_bet) + (0 if not board else 15)},
        ],
        "allowedActions": {
            "availableActions": avail,
            "canFold": "fold" in avail, "canCheck": "check" in avail, "canCall": "call" in avail,
            "canBet": "bet" in avail, "canRaise": "raise" in avail, "canAllIn": "all-in" in avail,
            "callChips": call_chips, "callAmount": call_chips,
            "callToAmount": call_chips + me_bet if call_chips else None,
            "minBet": 2 if "bet" in avail else None, "minRaiseTo": (facing or 2) * 2 if "raise" in avail else None,
            "maxCommit": stack + me_bet, "allInToAmount": stack + me_bet,
            "betRange": {"min": 2, "max": stack + me_bet} if "bet" in avail else None,
            "raiseRange": {"min": (facing or 2) * 2, "max": stack + me_bet} if "raise" in avail else None,
            "amountSemantics": "toAmount",
        },
    }
    return t


def check(out, avail, stack_hi, ctx):
    assert out is not None, f"None en {ctx}"
    if isinstance(out, str):
        a, amt = out, None
    elif isinstance(out, dict):
        a, amt = out.get("action"), out.get("amount")
    elif isinstance(out, tuple):
        a, amt = out[0], (out[1] if len(out) > 1 else None)
    else:
        raise AssertionError(f"tipo raro {type(out)} en {ctx}")
    assert a in avail, f"ILEGAL {a!r} (avail={avail}) en {ctx}"
    if amt is not None:
        assert isinstance(amt, int), f"amount no-int {amt!r} en {ctx}"
        assert 0 < amt <= stack_hi + 10, f"amount fuera de rango {amt} en {ctx}"
    return a


def main():
    random.seed(7)
    assert getattr(strategy.S7, "_HU", False) is True, "config HU NO cargada (_HU=False)"
    print("[test] engine v%s _HU=%s" % (getattr(strategy.S7, "VERSION", "?"), strategy.S7._HU))

    # ── casos dirigidos ──
    dir_cases = [
        ("SB unopened AA", mk_hu_table(True, ["As", "Ah"]), None),
        ("SB unopened 72o", mk_hu_table(True, ["7s", "2h"]), None),
        ("BB vs open", mk_hu_table(False, ["Kd", "9d"], facing=4), None),
        ("flop IP tras open", mk_hu_table(True, ["Qs", "Qd"], board=["Qh", "7c", "2s"]), None),
        ("turn OOP", mk_hu_table(False, ["Ad", "Kd"], board=["Qh", "7c", "2s", "9h"]), None),
        ("river facing bet", mk_hu_table(False, ["Ad", "Ac"], board=["Qh", "7c", "2s", "9h", "3d"], facing=30), None),
        ("solo call/fold", mk_hu_table(True, ["Ts", "9s"], facing=50, avail=["fold", "call"]), None),
        ("solo check", mk_hu_table(False, ["2c", "3d"], board=["Kh", "Kd", "5s"], avail=["check"]), "check"),
        ("solo fold (borde)", mk_hu_table(True, ["2c", "3d"], avail=["fold"]), "fold"),
        ("sin events", mk_hu_table(True, ["Ah", "Kh"], with_events=False), None),
    ]
    # mesa aplanada sin seats + booleans sin lista
    flat = {"holeCards": ["As", "Kd"], "board": [], "pot": 3, "bigBlind": 2, "stack": 998,
            "allowedActions": {"canFold": True, "canCall": True, "canRaise": True,
                               "callChips": 1, "minRaiseTo": 4, "maxCommit": 999}}
    dir_cases.append(("mesa aplanada + solo booleans", flat, None))
    nul = mk_hu_table(True, ["9h", "9c"])
    nul["allowedActions"]["raiseRange"] = None
    nul["allowedActions"]["betRange"] = None
    nul["allowedActions"]["minRaiseTo"] = None
    dir_cases.append(("ranges nulos", nul, None))

    for name, t, expect in dir_cases:
        avail = (t.get("allowedActions") or {}).get("availableActions") or \
                [a for a, k in (("fold", "canFold"), ("check", "canCheck"), ("call", "canCall"),
                                ("bet", "canBet"), ("raise", "canRaise"), ("all-in", "canAllIn"))
                 if (t.get("allowedActions") or {}).get(k)]
        out = strategy.choose_action(t)
        a = check(out, avail, 2000, name)
        if expect:
            assert a == expect, f"{name}: esperaba {expect}, salió {a}"
        print(f"[test] {name:32s} → {out if isinstance(out, str) else out.get('action'):8s} "
              f"{(out.get('amount') if isinstance(out, dict) else '') or ''}")

    # ── RFI de SB en unopened (rango HU ~78% → esperar 55-95%) ──
    n, opens = 400, 0
    for _ in range(n):
        t = mk_hu_table(True, deal(2, []))
        out = strategy.choose_action(t)
        a = out if isinstance(out, str) else out.get("action")
        if a in ("raise", "bet", "all-in"):
            opens += 1
        elif a == "call":       # limp cuenta como VPIP pero no como open-raise
            pass
    rfi = 100.0 * opens / n
    print(f"[test] SB RFI unopened = {rfi:.1f}% (esperado 55-95)")
    assert 55 <= rfi <= 95, f"RFI SB fuera de banda: {rfi:.1f}%"

    # ── fuzz ──
    fails = 0
    for i in range(2500):
        r = random.random()
        nb = random.choice([0, 3, 4, 5])
        board = deal(nb, [])
        hole = deal(2, board)
        subset = [a for a in ("fold", "check", "call", "bet", "raise", "all-in") if random.random() < 0.55]
        if not subset:
            subset = [random.choice(["fold", "check", "call"])]
        t = mk_hu_table(random.random() < 0.5, hole, board=board,
                        facing=random.choice([0, 0, 2, 10, 60, 300]),
                        stack=random.choice([15, 120, 970, 4000]),
                        avail=subset, with_events=random.random() < 0.7)
        if r < 0.12:            # mutilaciones: quitar campos al azar
            t.pop("seats", None)
            t["holeCards"] = hole; t["stackChips"] = 500; t["pot"] = 30
        if r < 0.06:
            t["allowedActions"] = {k: v for k, v in t.get("allowedActions", {}).items()
                                   if k in ("canFold", "canCheck", "canCall", "canRaise", "minRaiseTo")}
        if 0.06 <= r < 0.09:
            t["boardCards"] = None
        try:
            out = strategy.choose_action(t)
            al = t.get("allowedActions") or {}
            avail = al.get("availableActions") or \
                [a for a, k in (("fold", "canFold"), ("check", "canCheck"), ("call", "canCall"),
                                ("bet", "canBet"), ("raise", "canRaise"), ("all-in", "canAllIn")) if al.get(k)] \
                or ["fold"]
            check(out, avail, 99999, f"fuzz#{i}")
        except AssertionError as e:
            fails += 1
            print("[test] FALLO", e)
            if fails > 5:
                raise
    assert fails == 0, f"{fails} fallos en fuzz"
    print("[test] fuzz 2500 mesas: 0 excepciones, 0 ilegales ✔")
    print("[test] TODO OK")


if __name__ == "__main__":
    main()
