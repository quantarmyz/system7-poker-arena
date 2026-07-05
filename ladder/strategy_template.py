"""System 7 — bot ESTÁTICO para la Heads-Up Ladder de dev.fun Arena (QuantArmy-7).

Generado por ladder/build_bundle.py — NO editar a mano (la config HU va embebida abajo).
Contrato del sandbox: choose_action(table) → acción legal de
table["allowedActions"]["availableActions"], amount = to-amount. Sin red, sin LLM.
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)                    # decide_system7.py + treys/ vendorizados junto a este fichero
os.environ["S7_LOG"] = os.devnull                # decide() appendea un log por decisión → al vacío en sandbox

_HU_CFG = %%CONFIG%%

# decide_system7 carga su estrategia vía s7_strat.load() EN IMPORT → inyectamos un módulo
# fake con la config embebida (cero dependencia del filesystem/env del sandbox).
_m = types.ModuleType("s7_strat")
_m.load = lambda name=None: dict(_HU_CFG)
_m.names = lambda: ["system7-hu"]
_m.save = lambda *a, **k: None
_m.DIR = _HERE
sys.modules["s7_strat"] = _m

import decide_system7 as S7    # noqa: E402   (→ _HU=True, rangos/knobs HU activos)

_ACTIONS = ("fold", "check", "call", "bet", "raise", "all-in")
_DEADLINE = float(os.environ.get("S7_LADDER_DEADLINE", "4.0"))


def _as_int(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _canon(a):
    a = str(a or "").strip().lower().replace("_", "-")
    return "all-in" if a in ("allin", "all-in", "shove") else a


def _norm_table(table):
    """Copia defensiva del table del sandbox → el shape que decide_system7 espera
    (el de /texas/pending-actions). Sintetiza lo que falte; nunca lanza."""
    t = dict(table) if isinstance(table, dict) else {}

    # ── allowedActions: lista ↔ booleans, importes y ranges ──
    al = t.get("allowedActions")
    if not isinstance(al, dict):
        al = {k: t.get(k) for k in (
            "availableActions", "canFold", "canCheck", "canCall", "canBet", "canRaise", "canAllIn",
            "callChips", "callAmount", "callToAmount", "minBet", "minRaiseTo", "maxCommit",
            "allInToAmount", "betRange", "raiseRange")}
    al = dict(al)
    avail = al.get("availableActions")
    if not isinstance(avail, list) or not avail:
        avail = [a for a, k in (("fold", "canFold"), ("check", "canCheck"), ("call", "canCall"),
                                ("bet", "canBet"), ("raise", "canRaise"), ("all-in", "canAllIn"))
                 if al.get(k)]
    avail = [x for x in (_canon(a) for a in avail) if x in _ACTIONS] or ["fold"]
    al["availableActions"] = avail
    for a in _ACTIONS:                            # booleans coherentes con la lista
        k = "can" + ("AllIn" if a == "all-in" else a.capitalize())
        al[k] = bool(al.get(k)) or (a in avail)
        if a not in avail:
            al[k] = False

    # ── seats / héroe (mesa aplanada → sintetizar HU de 2 asientos) ──
    seats = t.get("seats")
    if not isinstance(seats, list) or not seats:
        stack = _as_int(t.get("stackChips") or t.get("stack"), 1000)
        committed = _as_int(t.get("totalCommittedChips") or t.get("committed"), 0)
        curbet = _as_int(t.get("currentBetChips"), 0)
        hole = list(t.get("holeCards") or t.get("hole") or [])
        pot = _as_int(t.get("potChips") or t.get("pot"), 0)
        t["selfSeatNumber"] = 0
        seats = [
            {"seatNumber": 0, "status": "active", "holeCards": hole, "stackChips": stack,
             "currentBetChips": curbet, "totalCommittedChips": committed},
            {"seatNumber": 1, "status": "active", "holeCards": [], "stackChips": 1000,
             "currentBetChips": 0, "totalCommittedChips": max(0, pot - committed)},
        ]
        t["seats"] = seats
    if t.get("selfSeatNumber") is None:
        me = next((s for s in seats if s.get("holeCards")), None)
        t["selfSeatNumber"] = (me or seats[0] or {}).get("seatNumber", 0)

    # ── básicos ──
    if not t.get("boardCards"):
        t["boardCards"] = list(t.get("board") or [])
    t["potChips"] = _as_int(t.get("potChips") or t.get("pot"), 0)
    if not t.get("bigBlindChips"):
        blinds = t.get("blinds") if isinstance(t.get("blinds"), dict) else {}
        t["bigBlindChips"] = _as_int(t.get("bigBlind") or blinds.get("big"), 2) or 2
    if not t.get("smallBlindChips"):
        blinds = t.get("blinds") if isinstance(t.get("blinds"), dict) else {}
        t["smallBlindChips"] = _as_int(t.get("smallBlind") or blinds.get("small"),
                                       max(1, _as_int(t["bigBlindChips"], 2) // 2))

    # callChips: preferir el chips-to-call directo; si solo hay to-amount, restar lo ya puesto
    me = next((s for s in t["seats"] if s.get("seatNumber") == t.get("selfSeatNumber")), {})
    cc = al.get("callChips")
    if cc in (None, ""):
        cc = al.get("callAmount")
    if cc in (None, ""):
        cta = al.get("callToAmount")
        cc = (_as_int(cta, 0) - _as_int(me.get("currentBetChips"), 0)) if cta not in (None, "") else 0
    al["callChips"] = max(0, _as_int(cc, 0))

    hero_stack = _as_int(me.get("stackChips"), 1000)
    hero_bet = _as_int(me.get("currentBetChips"), 0)
    if not al.get("allInToAmount"):
        al["allInToAmount"] = _as_int(al.get("maxCommit"), hero_stack + hero_bet)
    for key, lo_key in (("raiseRange", "minRaiseTo"), ("betRange", "minBet")):
        rng = al.get(key)
        if not isinstance(rng, dict) or rng.get("min") in (None, "") or rng.get("max") in (None, ""):
            lo = _as_int((rng or {}).get("min") or al.get(lo_key) or t.get(lo_key), 0)
            hi = _as_int((rng or {}).get("max") or al.get("maxCommit") or al.get("allInToAmount"), 0)
            al[key] = {"min": lo, "max": hi} if (lo or hi) else None
    t["allowedActions"] = al
    return t


def choose_action(table):
    """Devuelve {"action", "amount"?, "reasoning_text"} SIEMPRE legal; nunca lanza."""
    try:
        t = _norm_table(table)
        al = t["allowedActions"]
        avail = al["availableActions"]
        try:
            d = S7.decide(t, deadline_s=_DEADLINE) or {}
        except Exception:
            d = {"action": "check" if "check" in avail else "fold", "message": "engine error"}
        a = _canon(d.get("action"))
        amt = d.get("amount")

        # degradar a una acción DE LA LISTA (una ilegal faultea el bot)
        if a not in avail:
            if a == "bet" and "raise" in avail:
                a = "raise"
            elif a == "raise" and "bet" in avail:
                a = "bet"
            elif a == "all-in":
                if "raise" in avail:
                    a, amt = "raise", al.get("allInToAmount")
                elif "bet" in avail:
                    a, amt = "bet", al.get("allInToAmount")
                elif "call" in avail:
                    a = "call"
        if a not in avail:
            a = ("check" if "check" in avail else
                 ("fold" if "fold" in avail else
                  ("call" if "call" in avail else avail[0])))

        out = {"action": a,
               "reasoning_text": str(d.get("message") or d.get("reasoning") or "System 7 HU")[:140]}
        if a in ("bet", "raise"):
            rng = al.get("raiseRange") if a == "raise" else al.get("betRange")
            lo = _as_int((rng or {}).get("min"), 0)
            hi = _as_int((rng or {}).get("max"), 0) or _as_int(al.get("allInToAmount"), 0)
            if hi and lo > hi:
                lo = hi
            amt = _as_int(amt, lo or hi)
            if lo or hi:
                amt = max(lo, min(amt, hi or amt))
            out["amount"] = int(amt)
        elif a == "all-in" and al.get("allInToAmount"):
            out["amount"] = int(al["allInToAmount"])
        return out
    except Exception:
        try:
            raw = (table.get("allowedActions") or {}) if isinstance(table, dict) else {}
            avail = raw.get("availableActions") or []
            if "check" in avail or raw.get("canCheck"):
                return "check"
        except Exception:
            pass
        return "fold"


act = choose_action        # alias por si el harness busca act(table)
