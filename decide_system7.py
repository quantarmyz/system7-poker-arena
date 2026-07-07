"""System 7 — exploit-first No-Limit Hold'em engine (EducaPoker / GTO node-locking).

Deterministic `decide(table, deadline_s, research_context)` — NO LLM, NO network
at call time. Pure Python over the `table` dict from /texas/pending-actions.

Synthesises the user's methodology into code:
  - Hand-strength taxonomy ...... MMF / MF / MM / MD / AIR(+draws)   (_strength)
  - Board texture (4 grades) .... dry / semi / coord / extreme       (_texture)
  - Turn/River card dynamics .... scary(offensive) vs defensive       (_card_dynamic)
  - SPR commitment .............. SPR<=3 => commit MF+ (TPTK->MMF)     (_spr)
  - PME vs PER + adjusted outs .. pot odds vs adjusted-outs equity     (_pme,_adjusted_outs)
  - "Perejil Asesino" .......... conditional bluff-raise gating        (_perejil_ok)
  - HUD / node-locking ......... archetype deviations, N-gated         (_archetype, exploit)

`research_context` may carry the HUD that s7_reads.py builds:
    {"hud": {<villainSeatNumber:int>: {"N":int,"vpip":..,"pfr":..,"af":..,
             "bluffPct":..,"wtsd":..,"wsd":..,"archetype":"TAG"|...}},
     "aggressor_seat": <int|None>}
Absent / empty => GTO baseline (Directiva 1, N<100 = blind shell).

Amount semantics (see references/decide-function.md): bet/raise `amount` is the
TOTAL chips committed on this street AFTER acting (a "to-amount"), clamped to the
server's betRange/raiseRange. fold/check/call omit `amount`.
"""
from __future__ import annotations

import os
import random
from typing import Any, Optional

try:
    from treys import Card as _TCard, Evaluator as _TEval, Deck as _TDeck
    _HAS_TREYS = True
    _EVAL = _TEval()
except Exception:  # pragma: no cover
    _HAS_TREYS = False
    _EVAL = None

_RANKS = "23456789TJQKA"
FALLBACK_REASONING = '{vr: "std", ke: "legal", pp: "pot control"}'
VERSION = "1.5"   # v1.5: niveles de compromiso (supervivencia leaderboard) — stack-off por profundidad de re-subida + multiway
_LOG_PATH = os.environ.get("S7_LOG", "s7_decisions.log")

# ── Preflop ranges (EP100-style; extends assets/decide_ranged.py) ──────────────
OPENING_RANGES_STD = {
    "UTG": {"AA","KK","QQ","JJ","TT","99","AKs","AKo","AQs","AJs","KQs"},
    "MP":  {"AA","KK","QQ","JJ","TT","99","88","AKs","AKo","AQs","AQo","AJs","ATs","KQs","KJs","QJs"},
    "CO":  {"AA","KK","QQ","JJ","TT","99","88","77","66","55",
            "AKs","AKo","AQs","AQo","AJs","AJo","ATs","A5s","A4s","KQs","KQo","KJs","KTs",
            "QJs","QTs","JTs","T9s","98s"},
    "BTN": {"AA","KK","QQ","JJ","TT","99","88","77","66","55","44","33","22",
            "AKs","AKo","AQs","AQo","AJs","AJo","ATs","ATo",
            "A9s","A8s","A7s","A6s","A5s","A4s","A3s","A2s",
            "KQs","KQo","KJs","KJo","KTs","K9s",
            "QJs","QTs","Q9s","JTs","J9s","T9s","98s","87s","76s","65s"},
    "SB":  {"AA","KK","QQ","JJ","TT","99","88","77","AKs","AKo","AQs","AQo","AJs","ATs","A5s","KQs","KJs","QJs","JTs"},
    "BB":  {"AA","KK","QQ","JJ","TT","99","AKs","AKo","AQs"},
}
SEAT_TO_POS = {1: "BTN", 2: "SB", 3: "BB", 4: "UTG", 5: "MP", 6: "CO"}


def _expand(tokens):
    """Expand compact range tokens ('77+', 'A2s+', 'KTo+') into hand-class sets."""
    out = set()
    for t in tokens:
        if t.endswith("+"):
            base = t[:-1]
            if len(base) == 2 and base[0] == base[1]:          # pair+, e.g. 77+
                i = _RANKS.index(base[0])
                out |= {_RANKS[j] * 2 for j in range(i, 13)}
            elif len(base) == 3:                                # e.g. A2s+ / KTo+
                hi, lo, su = base[0], base[1], base[2]
                for j in range(_RANKS.index(lo), _RANKS.index(hi)):
                    out.add(f"{hi}{_RANKS[j]}{su}")
        else:
            out.add(t)
    return out


# A/B variant: clearly wider opens/defends (esp. late position). Toggle: S7_RANGES=wide.
OPENING_RANGES_WIDE = {
    "UTG": _expand(["66+", "AKs", "AQs", "AJs", "ATs", "A9s", "A5s", "AKo", "AQo", "AJo",
                    "KQs", "KJs", "KTs", "KQo", "QJs", "QTs", "JTs", "T9s", "98s"]),
    "MP":  _expand(["44+", "A2s+", "ATo", "AJo", "AQo", "AKo", "KTs", "KJs", "KQs", "KJo", "KQo",
                    "K9s", "QTs", "QJs", "Q9s", "JTs", "J9s", "T9s", "98s", "87s", "76s", "QJo"]),
    "CO":  _expand(["22+", "A2s+", "A5o", "A8o", "A9o", "ATo", "AJo", "AQo", "AKo",
                    "K6s", "K7s", "K8s", "K9s", "KTs", "KJs", "KQs", "KTo", "KJo", "KQo",
                    "Q8s", "Q9s", "QTs", "QJs", "QTo", "QJo", "J8s", "J9s", "JTs", "JTo",
                    "T8s", "T9s", "T9o", "97s", "98s", "86s", "87s", "75s", "76s", "65s", "54s"]),
    "BTN": _expand(["22+", "A2s+", "A2o+", "K2s+", "K9o+", "Q5s+", "Q9o+", "J7s+", "J9o+",
                    "T7s+", "T9o", "96s+", "98o", "85s+", "87o", "75s+", "76o", "64s+", "65o",
                    "53s+", "54o", "43s"]),
    "SB":  _expand(["22+", "A2s+", "A8o+", "K6s+", "KTo+", "Q8s+", "QTo+", "J8s+", "JTo",
                    "T8s+", "97s+", "87s", "76s", "65s", "54s", "KQo", "KJo", "QJo"]),
    "BB":  _expand(["22+", "A2s+", "A8o+", "K8s+", "KTo+", "Q8s+", "QTo+", "J8s+", "JTo",
                    "T8s+", "97s+", "86s+", "75s+", "65s", "54s", "KQo", "KJo", "QJo"]),
}

# Cash NIT (tight ~14/12) and AGR (= wide). With STD (medio) → las 3 modalidades cash.
OPENING_RANGES_NIT = {
    "UTG": _expand(["88+", "AJs+", "KQs", "AKo"]),
    "MP":  _expand(["77+", "ATs+", "KJs+", "QJs", "AQo+"]),
    "CO":  _expand(["66+", "A9s+", "A5s", "KTs+", "QTs+", "JTs", "T9s", "AJo+", "KQo"]),
    "BTN": _expand(["22+", "A2s+", "K9s+", "Q9s+", "J9s+", "T9s", "98s", "ATo+", "KJo+", "QJo"]),
    "SB":  _expand(["66+", "A9s+", "A5s", "KTs+", "QTs+", "JTs", "AJo+", "KQo"]),
    "BB":  _expand(["88+", "AJs+", "KQs", "AKo"]),
}
OPENING_RANGES_AGR = OPENING_RANGES_WIDE   # cash agresivo = wide

# Rangos de TORNEO por profundidad de stack (BBs efectivas). El tramo más corto = push/fold.
TOURN_RANGES_DEFAULT = {
    "deep": OPENING_RANGES_STD,                        # >40bb: juego completo
    "mid": {                                           # 20-40bb: más tight, raise-fold
        "UTG": _expand(["77+", "ATs+", "KQs", "AQo+"]),
        "MP":  _expand(["66+", "A9s+", "KJs+", "QJs", "AJo+", "KQo"]),
        "CO":  _expand(["44+", "A7s+", "A5s", "KTs+", "QTs+", "JTs", "ATo+", "KJo+"]),
        "BTN": _expand(["22+", "A2s+", "K8s+", "Q9s+", "J9s+", "T8s+", "98s", "A9o+", "KTo+", "QJo"]),
        "SB":  _expand(["22+", "A2s+", "K9s+", "Q9s+", "J9s+", "T9s", "A9o+", "KTo+"]),
        "BB":  _expand(["66+", "A9s+", "KJs+", "QJs", "AJo+", "KQo"]),
    },
    "short": {                                         # 10-20bb: raise-or-fold tight
        "UTG": _expand(["88+", "AJs+", "AQo+"]),
        "MP":  _expand(["77+", "ATs+", "KQs", "AQo+"]),
        "CO":  _expand(["66+", "A9s+", "KTs+", "QJs", "AJo+", "KQo"]),
        "BTN": _expand(["44+", "A5s+", "K9s+", "QTs+", "JTs", "ATo+", "KJo+"]),
        "SB":  _expand(["44+", "A7s+", "K9s+", "QTs+", "JTs", "ATo+", "KQo"]),
        "BB":  _expand(["77+", "ATs+", "KQs", "AQo+"]),
    },
    "push": {                                          # <10bb: SHOVE/fold (rango de all-in)
        "UTG": _expand(["66+", "A9s+", "ATo+", "KQs"]),
        "MP":  _expand(["55+", "A8s+", "A9o+", "KJs+", "KQo"]),
        "CO":  _expand(["44+", "A5s+", "A8o+", "K9s+", "KJo+", "QJs"]),
        "BTN": _expand(["22+", "A2s+", "K7s+", "K9o+", "Q9s+", "QJo", "J9s+", "T9s"]),
        "SB":  _expand(["22+", "A2s+", "K8s+", "KTo+", "Q9s+", "QJo", "J9s+", "T9s"]),
        "BB":  _expand(["44+", "A5s+", "A8o+", "K9s+", "KJo+", "QJs"]),
    },
}

try:
    import s7_strat
    _CFG = s7_strat.load()
except Exception:
    _CFG = {}

# Tipo de juego + modalidad. base=wide → cash agr (retrocompat). S7_GAME/S7_RANGES override por entorno.
GAME = str(_CFG.get("game") or os.environ.get("S7_GAME") or "cash").lower()
MODE = str(_CFG.get("mode") or ("agr" if (_CFG.get("base") == "wide" or os.environ.get("S7_RANGES") == "wide") else "std")).lower()

_bbk = _CFG.get("bb_buckets")
BB_BUCKETS = _bbk if (isinstance(_bbk, list) and len(_bbk) == 3) else [40, 20, 10]
_CASH_SETS = {"agr": OPENING_RANGES_AGR, "nit": OPENING_RANGES_NIT, "std": OPENING_RANGES_STD}


def _apply_overrides(base_dict, override):
    d = {k: set(v) for k, v in base_dict.items()}
    if isinstance(override, dict):
        for _p, _t in override.items():
            try:
                d[str(_p).upper()] = _expand(_t)
            except Exception:
                pass
    return d


# Rango cash activo (modalidad + override opening_ranges) + rangos de torneo por tramo.
OPENING_RANGES = _apply_overrides(_CASH_SETS.get(MODE, OPENING_RANGES_STD), _CFG.get("opening_ranges"))
_TOURN = {b: _apply_overrides(TOURN_RANGES_DEFAULT.get(b, OPENING_RANGES_STD),
                              (_CFG.get("tournament_ranges") or {}).get(b))
          for b in ("deep", "mid", "short", "push")}
# Polarised 3-bet bluffs: weak suited-ace blockers (Directiva: A2s-A5s).
_3BET_BLUFF_BLOCKERS = set(_CFG.get("threebet_bluff") or ["A2s", "A3s", "A4s", "A5s", "K9s", "Q9s"])
_3BET_VALUE = set(_CFG.get("threebet_value") or ["AA", "KK", "QQ", "JJ", "AKs", "AKo", "AQs"])
# Manos HECHAS premium con las que SÍ se apila el stack preflop (4bet+/all-in). Frente a una
# re-subida solo continúan estas; faroles y especulativas foldean (no stack-off con basura).
_PRE_COMMIT = set(_CFG.get("commit_value") or ["AA", "KK", "QQ", "JJ", "TT", "AKs", "AKo", "AQs"])
# Niveles de compromiso (supervivencia de leaderboard): rangos con los que se apila el stack según la
# PROFUNDIDAD de re-subidas + nº de rivales comprometidos. set LITERAL de clases (NO expanden tokens '+').
# Tier-1 = _PRE_COMMIT (vs un único 3bet) · deep = vs 4bet · allin = vs 5bet+/jam o all-in multiway.
SURVIVAL = bool(_CFG.get("survival"))
_COMMIT_DEEP = set(_CFG.get("commit_value_deep") or ["AA", "KK", "QQ", "AKs"])
_COMMIT_ALLIN = set(_CFG.get("commit_value_allin") or ["AA", "KK"])

# Cbet sizing fraction of pot, per texture+street.
SIZING = ((_CFG.get("knobs") or {}).get("sizing")) or {
    "dry":     {"flop": 0.33, "turn": 0.50, "river": 0.60},
    "semi":    {"flop": 0.50, "turn": 0.60, "river": 0.66},
    "coord":   {"flop": 0.66, "turn": 0.75, "river": 0.75},
    "extreme": {"flop": 0.75, "turn": 0.80, "river": 0.80},
}
# Tunable postflop/preflop knobs (defaults = std; overridable per strategy config).
KN = {"open_size_bb": 2.5, "threebet_mult": 3, "value_eq": 0.62, "station_mult": 1.2,
      "cbet_bluff_frac": 0.33, "commit_spr": 3, "perejil_flop": 8, "perejil_turn": 10, "perejil_relief": 2,
      "commit_deep_frac": 0.35, "commit_allin_frac": 0.6, "commit_multi_n": 2,
      "commit_4bet_bb": 8, "commit_5bet_bb": 22}
KN.update({k: v for k, v in (_CFG.get("knobs") or {}).items() if k in KN and isinstance(v, (int, float))})
_PREFLOP_EQ = {
    "AA":.85,"KK":.82,"QQ":.80,"JJ":.77,"TT":.75,"99":.72,"88":.69,"77":.66,
    "66":.63,"55":.60,"44":.57,"33":.54,"22":.50,
    "AKs":.67,"AKo":.65,"AQs":.66,"AQo":.64,"AJs":.65,"AJo":.63,"ATs":.64,"ATo":.61,
    "KQs":.63,"KQo":.61,"KJs":.62,"QJs":.60,"JTs":.58,"T9s":.54,"98s":.52,
}


# ── card helpers ───────────────────────────────────────────────────────────────
def _to_treys(c: str) -> str:
    if not c:
        return "2c"
    r = c[0].upper()
    if c.startswith("10"):
        return "T" + (c[2].lower() if len(c) > 2 else "c")
    return r + c[-1].lower()


def _rank_idx(c: str) -> int:
    r = c[0].upper()
    if c.startswith("10"):
        r = "T"
    return _RANKS.index(r) if r in _RANKS else -1


def _hand_class(hole) -> str:
    if len(hole) != 2:
        return ""
    r1, s1 = hole[0][0].upper(), hole[0][-1].lower()
    r2, s2 = hole[1][0].upper(), hole[1][-1].lower()
    if r1 not in _RANKS or r2 not in _RANKS:
        return ""
    if _RANKS.index(r1) < _RANKS.index(r2):
        r1, r2, s1, s2 = r2, r1, s2, s1
    if r1 == r2:
        return r1 + r2
    return f"{r1}{r2}{'s' if s1 == s2 else 'o'}"


def _equity(hole, board, n_villains=1, sims=160, deadline_s=10.0) -> float:
    """Monte-Carlo showdown equity vs N random villains (treys): hero wins the
    pot only if it beats EVERY opponent. Multiway-aware — crucial, since a hand
    that is +EV in isolation is often crushed against multiple ranges."""
    nv = max(1, min(int(n_villains), 5))
    cls = _hand_class(hole)
    if not _HAS_TREYS or deadline_s < 2.5 or not board:
        base = _PREFLOP_EQ.get(cls, 0.42)
        return base if nv <= 1 else max(0.05, base ** nv)
    try:
        hero = [_TCard.new(_to_treys(c)) for c in hole]
        bt = [_TCard.new(_to_treys(c)) for c in board]
        used = set(hero) | set(bt)
        rng = random.Random(sum(ord(ch) for c in hole + board for ch in c) or 7)   # semilla por-mano (no fija): menos sesgo entre manos
        wins = ties = 0
        need = 5 - len(bt)
        for _ in range(sims):
            deck = [c for c in _TDeck().cards if c not in used]
            rng.shuffle(deck)
            opps = [[deck.pop(), deck.pop()] for _ in range(nv)]
            full = bt + [deck.pop() for _ in range(need)]
            hr = _EVAL.evaluate(full, hero)
            best_opp = min(_EVAL.evaluate(full, o) for o in opps)
            if hr < best_opp:
                wins += 1
            elif hr == best_opp:
                ties += 1
        return (wins + 0.5 * ties) / max(sims, 1)
    except Exception:
        base = _PREFLOP_EQ.get(cls, 0.42)
        return base if nv <= 1 else base ** nv


# ── board texture (4 grades) ────────────────────────────────────────────────────
def _texture(board) -> str:
    if len(board) < 3:
        return "dry"
    suits = [c[-1].lower() for c in board]
    idx = sorted({_rank_idx(c) for c in board if _rank_idx(c) >= 0})
    maxsuit = max(suits.count(s) for s in set(suits))
    paired = len(set(c[0].upper() for c in board)) < len(board)
    # connectedness: count gaps among the 3-5 closest ranks
    spread = (idx[-1] - idx[0]) if idx else 9
    three_seq = any(idx[i+2] - idx[i] <= 2 for i in range(max(0, len(idx) - 2)))
    if maxsuit >= 4 or (len(idx) >= 4 and spread <= 4):
        return "extreme"
    if maxsuit >= 3 or three_seq:
        return "coord"
    if maxsuit == 2 or spread <= 4 or paired:
        return "semi"
    return "dry"


def _card_dynamic(board, aggressor_is_hero: bool) -> str:
    """Classify the most-recent board card (turn/river) as offensive/defensive/static."""
    if len(board) < 4:
        return "static"
    new = board[-1]
    prev = board[:-1]
    ni = _rank_idx(new)
    prev_hi = max((_rank_idx(c) for c in prev), default=0)
    suits_prev = [c[-1].lower() for c in prev]
    completes_flush = (suits_prev.count(new[-1].lower()) >= 2 and
                       max(suits_prev.count(s) for s in set(suits_prev)) >= 2)
    pairs_board = any(c[0].upper() == new[0].upper() for c in prev)
    if ni > prev_hi and ni >= _RANKS.index("Q"):     # overcard A/K/Q
        return "offensive"        # favours preflop aggressor's range
    if completes_flush or (pairs_board and ni <= _RANKS.index("Q")):
        return "defensive"        # favours the passive caller
    return "static"


# ── made-hand strength taxonomy ─────────────────────────────────────────────────
def _strength(hole, board, texture) -> str:
    """Return MMF / MF / MM / MD / AIR for a postflop made hand."""
    if len(board) < 3 or not _HAS_TREYS:
        return "MM"
    try:
        hero = [_TCard.new(_to_treys(c)) for c in hole]
        bt = [_TCard.new(_to_treys(c)) for c in board]
        rc = _EVAL.get_rank_class(_EVAL.evaluate(bt, hero))  # 1 best .. 9 high
    except Exception:
        return "MM"
    bi = sorted((_rank_idx(c) for c in board), reverse=True)
    hi = sorted((_rank_idx(c) for c in hole), reverse=True)
    top = bi[0] if bi else -1
    pocket = len(hole) == 2 and hi and hi[0] == hi[-1]
    if rc <= 3:                       # full house, quads, straight flush
        return "MMF"
    if rc in (4, 5):                  # flush / straight
        return "MMF" if texture != "extreme" else "MF"
    if rc == 6:                       # trips / set
        return "MMF" if texture != "extreme" else "MM"
    if rc == 7:                       # two pair
        board_paired = len(set(bi)) < len(bi)
        if board_paired:              # en board emparejado la "doble pareja" suele usar la pareja del board → vulnerable a trips/boat
            unp = [r for r in set(bi) if bi.count(r) == 1]
            top_unp = max(unp) if unp else -1
            if pocket and hi[0] < top_unp:   # underpair cabalgando la pareja del board (99 en KKJ5) → bluff-catcher, NO comprometer
                return "MM"
            return "MF"               # doble pareja real en board emparejado: fuerte, pero no monstruo
        return "MMF" if texture in ("dry", "semi") else "MF"
    if rc == 8:                       # one pair — refine
        if pocket and hi[0] > top:
            return "MF"               # overpair
        if top in hi:                 # top pair
            kick = max((r for r in hi if r != top), default=-1)
            return "MF" if kick >= _RANKS.index("Q") else "MM"   # TPTK vs weak kicker
        if len(bi) >= 2 and bi[1] in hi:
            return "MM"               # 2nd pair
        return "MD"                   # bottom / underpair to board
    # high card
    return "MD" if (hi and hi[0] >= _RANKS.index("A")) else "AIR"


# ── draws + adjusted outs (EducaPoker discount tables) ──────────────────────────
def _adjusted_outs(hole, board, texture) -> int:
    """Best-draw adjusted outs after texture penalties (0 if no live draw)."""
    if len(board) < 3 or len(board) >= 5 or len(hole) != 2:
        return 0
    allc = [_to_treys(c) for c in hole + board]
    suits = [c[-1] for c in allc]
    ranks = sorted({_RANKS.index(c[0]) for c in allc})
    hole_idx = {_rank_idx(c) for c in hole}
    board_idx = {_rank_idx(c) for c in board}
    outs = 0
    # flush draw (need exactly 4 of a suit, with >=1 hole card of it)
    for s in set(suits):
        if suits.count(s) == 4 and any(_to_treys(c)[-1] == s for c in hole):
            o = 9
            # higher-flush risk: -1 per overcard of the suit a villain could hold (approx -1 on coord+)
            if texture in ("coord", "extreme"):
                o -= 1
            outs = max(outs, o)
    # straight draw — count distinct completing ranks (2 => OESD/8, 1 => gutshot/4)
    rset = set(ranks)

    def _makes_straight(extra: int) -> bool:
        s = rset | {extra}
        return any(all((i + k) in s for k in range(5)) for i in range(0, 9))

    completers = [r for r in range(13) if r not in rset and _makes_straight(r)]
    sd = min(8, 4 * len(completers))
    if sd:
        o = sd
        if texture in ("coord", "extreme"):        # flush present devalues straight outs
            o = max(0, o - (4 if sd == 8 else 2))
        outs = max(outs, o)
    # overcards (two cards over the board, unpaired) — speculative
    if not (hole_idx & board_idx) and hole_idx and min(hole_idx) > max(board_idx):
        ov = {"dry": 4, "semi": 3, "coord": 2, "extreme": 0}[texture]
        outs = max(outs, ov)
    return max(0, outs)


# ── SPR + stacks ────────────────────────────────────────────────────────────────
def _eff_stack(table) -> int:
    seats = table.get("seats") or []
    live = [int(s.get("stackChips") or 0) for s in seats
            if str(s.get("status") or "").lower() not in ("folded", "out", "sittingout")]
    me = next((int(s.get("stackChips") or 0) for s in seats
               if s.get("seatNumber") == table.get("selfSeatNumber")), 0)
    others = [v for v in live if v != me] or [me]
    return max(0, min(me, max(others)))


def _spr(table) -> float:
    pot = int(table.get("potChips") or 0)
    return (_eff_stack(table) / pot) if pot > 0 else 99.0


# ── position (inferred from the blind structure) ────────────────────────────────
def _blind_seats(table: dict):
    """Infer (sb_seat, bb_seat) from BlindPosted events, else from committed chips."""
    sb = int(table.get("smallBlindChips") or 0)
    bb = int(table.get("bigBlindChips") or 0)
    sb_seat = bb_seat = None
    for e in (table.get("recentEvents") or []):
        if e.get("type") == "BlindPosted":
            s = e.get("summary") or {}
            if s.get("amount") == sb and sb_seat is None:
                sb_seat = s.get("seatNumber")
            elif s.get("amount") == bb and bb_seat is None:
                bb_seat = s.get("seatNumber")
    if sb_seat is None or bb_seat is None:
        for s in (table.get("seats") or []):
            tc = int(s.get("totalCommittedChips") or 0)
            if tc == sb and sb_seat is None:
                sb_seat = s.get("seatNumber")
            elif tc == bb and bb_seat is None:
                bb_seat = s.get("seatNumber")
    return sb_seat, bb_seat


_POS_BY_N = {
    2: ["SB", "BB"], 3: ["SB", "BB", "BTN"], 4: ["SB", "BB", "CO", "BTN"],
    5: ["SB", "BB", "UTG", "CO", "BTN"], 6: ["SB", "BB", "UTG", "MP", "CO", "BTN"],
}


def _position(table: dict):
    """(pos_name, in_position, known) for hero from the blind structure. Falls back
    to ('UTG', False, False) when there is no positional signal (e.g. the kit's
    self-play, which omits blinds and pins hero to the SB seat)."""
    seat_n = table.get("selfSeatNumber")
    seats = [s for s in (table.get("seats") or [])
             if str(s.get("status") or "").lower() not in ("out", "sittingout")]
    nums = sorted(s.get("seatNumber") for s in seats if s.get("seatNumber") is not None)
    if not table.get("bigBlindChips") or not nums or seat_n not in nums:
        return ("UTG", False, False)
    sb_seat, _ = _blind_seats(table)
    if sb_seat not in nums:
        return (SEAT_TO_POS.get(seat_n, "MP"), seat_n == nums[-1], True)
    rot = nums[nums.index(sb_seat):] + nums[:nums.index(sb_seat)]   # [SB, BB, ..., BTN]
    off = rot.index(seat_n)
    n = len(rot)
    names = _POS_BY_N.get(n, ["SB", "BB", "UTG", "MP", "CO", "BTN"])
    return (names[off] if off < len(names) else "BTN", off == n - 1, True)


# ── game type / stack-depth range selection (cash modalities vs tournament BBs) ──
def _eff_bb(table: dict) -> float:
    bb = int(table.get("bigBlindChips") or 0) or 1
    try:
        return _eff_stack(table) / bb
    except Exception:
        return 100.0


def _tourn_bucket(eff_bb: float) -> str:
    hi, mid, lo = BB_BUCKETS
    if eff_bb > hi:
        return "deep"
    if eff_bb > mid:
        return "mid"
    if eff_bb > lo:
        return "short"
    return "push"


def _active_ranges(table: dict):
    """Return (opening_ranges_dict, bucket). Cash → fixed modality; tournament → by eff BBs.
    bucket=='push' (tournament short) switches preflop to shove/fold."""
    if GAME == "tournament":
        b = _tourn_bucket(_eff_bb(table))
        return _TOURN.get(b, OPENING_RANGES), b
    return OPENING_RANGES, "cash"


# ── HUD / archetype (Directiva 1) ───────────────────────────────────────────────
def _pct(x) -> float:
    """agent-stats ships rates as fractions (0.216 = 21.6%). Normalize to %.
    Leaves already-percentage values (>1) untouched, so synthetic test data works."""
    x = float(x or 0)
    return x * 100.0 if 0 < x <= 1.0 else x


def _archetype(st: dict) -> str:
    """Classify a villain from agent-stats. N-gated by sampleSize. Trusts the
    API's own playingStyle label when decisive."""
    n = int(st.get("N") or st.get("sampleSize") or 0)
    if n < int(os.environ.get("S7_HUD_MIN_N", "500")):      # adapta solo con muestra fiable del Arena
        return "UNKNOWN"
    vpip, pfr, af = _pct(st.get("vpip")), _pct(st.get("pfr")), float(st.get("af") or 0)
    gap = vpip - pfr
    style = st.get("playingStyle")
    if isinstance(style, dict):
        lab = " ".join(str(style.get(k, "")) for k in
                       ("label", "tightness", "aggression", "archetype", "tagline")).lower()
    else:
        lab = str(style or "").lower()
    if "station" in lab or (vpip >= 30 and gap >= 15 and af <= 1.2):
        return "STATION"
    if "manic" in lab or "maniac" in lab or (vpip >= 40 and pfr >= 30):
        return "MANIAC"
    if "nit" in lab or (vpip <= 15 and gap <= 6):
        return "NIT"
    if "lag" in lab or (vpip >= 26 and pfr >= 20):
        return "LAG"
    return "TAG"


def _villain_reads(table, ctx) -> dict:
    """Pick the most relevant villain HUD: the current bettor/aggressor if known,
    else the loosest classified opponent. Returns {} when blind (N<100)."""
    hud = (ctx or {}).get("hud") or {}
    if not hud:
        return {}
    agg = (ctx or {}).get("aggressor_seat")
    best = {}
    for seat, st in hud.items():
        arc = _archetype(st)
        if arc == "UNKNOWN":
            continue
        st = {**st, "archetype": arc}
        if agg is not None and str(seat) == str(agg):
            return st
        best = st
    return best


# ── PME / PER ───────────────────────────────────────────────────────────────────
def _pme(call_chips: int, pot: int) -> float:
    tot = pot + call_chips
    return (call_chips / tot) if tot > 0 else 1.0


def _per(adj_outs: int, board_len: int) -> float:
    mult = 4 if board_len == 3 else (2 if board_len == 4 else 0)
    return min(0.95, adj_outs * mult / 100.0)


def _range_discount(pot_odds: float, board_len: int, bluffy: bool, honest: bool) -> float:
    """Descuento a la equity-vs-ALEATORIO: quien apuesta tiene un rango más fuerte que aleatorio. Más
    descuento cuanto mayor la apuesta y más tardía la calle; menos vs bluffers (rango ancho), más vs honestos."""
    d = 0.06 + 0.20 * max(0.0, pot_odds - 0.25) + 0.04 * max(0, board_len - 3)
    return d * (0.4 if bluffy else (1.5 if honest else 1.0))


def _river_blocker(hole, board) -> bool:
    """¿Tenemos un bloqueador del valor del rival para SELECCIONAR un farol de river? (carta del palo de
    color con 3+ en mesa → bloquea el color; o carta alta Q+ → bloquea valor broadway/overpairs)."""
    if len(hole) != 2 or len(board) < 3:
        return False
    suits = [c[-1].lower() for c in board]
    fs = next((s for s in set(suits) if suits.count(s) >= 3), None)
    for c in hole:
        if fs and c[-1].lower() == fs:
            return True
        if _rank_idx(c) >= _RANKS.index("Q"):
            return True
    return False


def _perejil_ok(adj_outs: int, board_len: int, n_villains: int, overfolder: bool) -> bool:
    """Conditional bluff-raise gate: +8 outs flop / +10 turn, +1/extra villain, -2 vs overfolder."""
    req = KN["perejil_flop"] if board_len == 3 else (KN["perejil_turn"] if board_len == 4 else 99)
    req += max(0, n_villains - 1)
    if overfolder:
        req -= KN["perejil_relief"]
    return adj_outs >= req


# ── niveles de compromiso preflop (supervivencia de leaderboard) ─────────────────
def _commit_tier(seats, seat_n, hero_bet, call_chips, eff, bb):
    """Profundidad del compromiso de fichas → cuánto stack arriesgar.
    'shallow' (1er 3bet) → 'deep' (4bet) → 'allin' (5bet+/jam o all-in multiway).
    Devuelve (tier, n_committed)."""
    thr = max(KN["commit_4bet_bb"] * bb, 0.2 * eff)
    n_committed = 0
    for s in seats:
        if s.get("seatNumber") == seat_n:
            continue
        if str(s.get("status") or "").lower() in ("folded", "out", "sittingout"):
            continue
        allin = int(s.get("stackChips") or 0) == 0 or "all" in str(s.get("status") or "").lower()
        if allin or int(s.get("currentBetChips") or 0) >= thr:
            n_committed += 1
    risk = call_chips / max(eff, 1)              # fracción de stack efectivo para continuar
    hb = (hero_bet / bb) if bb else 0            # profundidad de la re-subida del héroe (en bb)
    if n_committed >= KN["commit_multi_n"] or risk >= KN["commit_allin_frac"] or hb >= KN["commit_5bet_bb"]:
        return "allin", n_committed
    if hb >= KN["commit_4bet_bb"] or risk >= KN["commit_deep_frac"]:
        return "deep", n_committed
    return "shallow", n_committed


# ── reasoning + report ──────────────────────────────────────────────────────────
def _reasoning(action, eq, pot_odds, strength, texture, n_outs) -> str:
    ke = f"{int(round(eq*100))}% eq" if eq else f"{n_outs} outs"
    sr = (f"po {int(round(pot_odds*100))}%" if action in ("call",) else
          (f"{strength} val" if action in ("bet", "raise", "all-in") else ""))
    parts = [f'vr: "exploit"', f'ke: "{ke[:20]}"', f'bf: [{texture}]', f'pp: "{strength}"']
    if sr:
        parts.append(f'sr: "{sr[:20]}"')
    y = "{" + ", ".join(parts) + "}"
    return y if len(y) <= 150 else FALLBACK_REASONING


def _log(report: str) -> None:
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(report + "\n")
    except Exception:
        pass


def _act(name, amount, allowed, msg, reasoning) -> dict:
    out = {"action": name, "message": str(msg)[:500], "reasoning": reasoning}
    if amount is not None and name in ("bet", "raise", "all-in"):
        out["amount"] = int(amount)
    return out


def _clamp(amount, rng, lo_default, hi_default) -> int:
    lo = int((rng or {}).get("min") or lo_default)
    hi = int((rng or {}).get("max") or hi_default or lo)
    return max(lo, min(int(amount), hi))


# ── main decision ────────────────────────────────────────────────────────────────
def decide(table: dict, deadline_s: float = 10.0,
           research_context: Optional[dict] = None) -> dict:
    allowed = table.get("allowedActions") or {}
    avail = allowed.get("availableActions") or []

    if deadline_s < 2.0:
        if allowed.get("canCheck"):
            return _act("check", None, allowed, "deadline tight", FALLBACK_REASONING)
        return _act("fold", None, allowed, "deadline tight", FALLBACK_REASONING)

    seat_n = table.get("selfSeatNumber")
    seats = table.get("seats") or []
    me = next((s for s in seats if s.get("seatNumber") == seat_n), {})
    hole = list(me.get("holeCards") or [])
    board = list(table.get("boardCards") or [])
    pot = int(table.get("potChips") or 0)
    call_chips = int(allowed.get("callChips") or 0)
    bb = int(table.get("bigBlindChips") or 2) or 2
    pos, ip, pos_known = _position(table)
    cls = _hand_class(hole)
    texture = _texture(board)
    n_live = sum(1 for s in seats if str(s.get("status") or "").lower() not in ("folded", "out", "sittingout"))
    n_villains = max(1, n_live - 1)
    hu_now = False  # removed: position-aware postflop was HU-specific
    reads = _villain_reads(table, research_context)
    arc = reads.get("archetype", "UNKNOWN")
    _wtsd = _pct(reads.get("wtsd"))
    _af = float(reads.get("af") or 0)
    _bluff = _pct(reads.get("bluffPct"))
    station = arc == "STATION"
    maniac = arc == "MANIAC"
    # overfolder robusto: NIT, o WTSD bajo PERO pasivo (un agresivo con WTSD bajo barrelea, no foldea)
    overfolder = arc == "NIT" or (0 < _wtsd < 22 and _af <= 1.8 and arc != "STATION")
    # lecturas explotadoras (stats que el motor recogía pero NO usaba en la decisión):
    bluffy = maniac or arc == "LAG" or _af >= 3.0 or _bluff >= 12      # agresivo/bluffer → pagar ancho, no farolearle
    honest = arc in ("NIT", "TAG") and _af <= 1.3 and 0 <= _bluff < 6  # pasivo/honesto → respetar sus apuestas

    # ── PREFLOP ──────────────────────────────────────────────────────────────
    if not board:
        ranges, _bkt = _active_ranges(table)
        if _bkt == "push":            # torneo corto (<Xbb): shove/fold
            in_range = cls in ranges.get(pos, set())
            if call_chips == 0:
                if in_range and allowed.get("canAllIn"):
                    return _act("all-in", int(allowed.get("allInToAmount") or 0), allowed,
                                f"shove {cls} {pos} (<{BB_BUCKETS[2]}bb)", FALLBACK_REASONING)
                if "check" in avail:
                    return _act("check", None, allowed, "check BB short", FALLBACK_REASONING)
                return _act("fold", None, allowed, f"fold {cls} short", FALLBACK_REASONING)
            if in_range and allowed.get("canAllIn"):
                return _act("all-in", int(allowed.get("allInToAmount") or 0), allowed,
                            f"re-shove {cls}", FALLBACK_REASONING)
            if in_range and "call" in avail and call_chips <= _eff_stack(table):
                return _act("call", None, allowed, f"call shove {cls}", FALLBACK_REASONING)
            if "check" in avail:
                return _act("check", None, allowed, "check short", FALLBACK_REASONING)
            return _act("fold", None, allowed, f"fold {cls} short", FALLBACK_REASONING)
        in_open = cls in ranges.get(pos, set())
        if call_chips == 0:  # unopened pot or in BB
            if in_open and ("raise" in avail or "bet" in avail):
                act = "raise" if "raise" in avail else "bet"
                rng = allowed.get("raiseRange") or allowed.get("betRange") or {}
                amt = _clamp(round(KN["open_size_bb"] * bb), rng, 2 * bb, pot + 4 * bb)
                return _act(act, amt, allowed, f"open {cls} {pos}",
                            f'{{vr: "exploit", ke: "{cls} open", bf: [pf], pp: "{pos} IP", sr: "2.5bb"}}')
            if "check" in avail:
                return _act("check", None, allowed, "check BB", FALLBACK_REASONING)
            return _act("fold", None, allowed, f"{cls} fold {pos}", FALLBACK_REASONING)
        # facing chips to call preflop — 3-bet, or distinguish open/complete vs real raise
        opp_bets = [int(s.get("currentBetChips") or 0) for s in seats
                    if s.get("seatNumber") != seat_n
                    and str(s.get("status") or "").lower() not in ("folded", "out", "sittingout")]
        raised = (max(opp_bets) if opp_bets else 0) > bb
        can3 = "raise" in avail
        # ¿OPEN (1ª subida) o RE-subida (4bet+/squeeze)? Si nosotros YA subimos esta calle (hero_bet>bb) y aún
        # hay que pagar, nos re-subieron; o si el call es enorme (>3.5bb, o ≥18% del stack efectivo) es un
        # 3bet+/compromiso. Frente a eso NO se re-farolea: solo manos HECHAS premium apilan/continúan.
        hero_bet = next((int(s.get("currentBetChips") or 0) for s in seats if s.get("seatNumber") == seat_n), 0)
        eff = _eff_stack(table) or 0
        facing_reraise = (hero_bet > bb) or (call_chips > 3.5 * bb) or (eff > 0 and call_chips >= 0.18 * eff)
        if raised and facing_reraise:
            # Nivel de compromiso: cuanto más profunda la re-subida / más rivales comprometidos, más tight
            # el rango con el que se apila el stack (supervivencia de leaderboard). Sin SURVIVAL = como antes.
            if SURVIVAL:
                tier, n_committed = _commit_tier(seats, seat_n, hero_bet, call_chips, eff, bb)
                commit_set = {"shallow": _PRE_COMMIT, "deep": _COMMIT_DEEP, "allin": _COMMIT_ALLIN}[tier]
            else:
                tier, commit_set = "shallow", _PRE_COMMIT
            if cls in commit_set and can3:                       # apilar fichas SOLO con premium del tier
                rng = allowed.get("raiseRange") or {}
                amt = _clamp(int(call_chips * KN["threebet_mult"]), rng, call_chips * 2, pot + call_chips * 3)
                if allowed.get("canAllIn") and eff and amt >= 0.6 * eff:     # casi comprometidos → jam directo
                    amt = int(allowed.get("allInToAmount") or amt)
                return _act("raise", amt, allowed, f"4bet+ value {cls} ({tier})",
                            f'{{vr: "premium", ke: "{cls} 4bet+", pp: "{pos}", sr: "{tier}"}}')
            if cls in commit_set and "call" in avail:
                return _act("call", None, allowed, f"call re-raise {cls} ({tier})", FALLBACK_REASONING)
            if "check" in avail:
                return _act("check", None, allowed, "check vs re-raise", FALLBACK_REASONING)
            return _act("fold", None, allowed, f"fold {cls} vs re-raise ({tier})", FALLBACK_REASONING)
        bluff3 = (cls in _3BET_BLUFF_BLOCKERS) and overfolder and not station
        if raised and can3 and (cls in _3BET_VALUE or bluff3):
            rng = allowed.get("raiseRange") or {}
            amt = _clamp(int(call_chips * KN["threebet_mult"]), rng, call_chips * 2, pot + call_chips * 3)
            why = "value" if cls in _3BET_VALUE else "blocker"
            return _act("raise", amt, allowed, f"3bet {cls} ({why})",
                        f'{{vr: "exploit", ke: "{cls} 3b", bf: [pf], pp: "{pos}", sr: "3x {why}"}}')
        if not raised:
            if in_open and can3:                       # first-in open / isolate limpers
                rng = allowed.get("raiseRange") or {}
                amt = _clamp(round(3 * bb), rng, call_chips * 2, pot + 4 * bb)
                return _act("raise", amt, allowed, f"open/iso {cls} {pos}",
                            f'{{vr: "exploit", ke: "{cls} open", bf: [pf], pp: "{pos}", sr: "3bb"}}')
            complete = (ranges.get("CO", set()) | ranges.get("BB", set())
                        | {"A9o", "KTo", "QTo", "J9s", "T8s", "97s", "86s", "75s", "65s", "54s"})
            if pos in ("SB", "BB") and cls in complete and "call" in avail:
                return _act("call", None, allowed, f"complete {cls} {pos}", FALLBACK_REASONING)
            if "check" in avail:
                return _act("check", None, allowed, "check option", FALLBACK_REASONING)
            return _act("fold", None, allowed, f"fold {cls} unraised", FALLBACK_REASONING)
        # facing a real raise — position-aware defend / fold
        defend = (ranges.get(pos if pos in ("BB", "SB") else "BB", set())
                  | {"99", "88", "77", "AJo", "ATs", "KQo", "KQs", "KJs", "QJs", "JTs"})
        cheap = call_chips <= 3 * bb
        if ((not station and cls in defend and cheap) or
                (station and cls in (defend | ranges.get("CO", set())))) and "call" in avail:
            return _act("call", None, allowed, f"defend {cls} vs raise ({arc})", FALLBACK_REASONING)
        if "check" in avail:
            return _act("check", None, allowed, "check vs raise", FALLBACK_REASONING)
        return _act("fold", None, allowed, f"fold {cls} vs raise", FALLBACK_REASONING)

    # ── POSTFLOP ─────────────────────────────────────────────────────────────
    board_len = len(board)
    street = ("flop", "turn", "river")[max(0, min(board_len - 3, 2))]
    eq = _equity(hole, board, n_villains=n_villains, sims=(400 if deadline_s > 6 else 200), deadline_s=deadline_s)
    strength = _strength(hole, board, texture)
    adj_outs = _adjusted_outs(hole, board, texture)
    spr = _spr(table)
    dyn = _card_dynamic(board, aggressor_is_hero=(research_context or {}).get("aggressor_seat") == seat_n)
    pot_odds = _pme(call_chips, pot) if call_chips else 0.0
    per = _per(adj_outs, board_len)
    # Equity realista (v1.3): el agresor NO tiene mano aleatoria → descontar al afrontar bet/raise.
    eq_eff = max(0.0, eq - _range_discount(pot_odds, board_len, bluffy, honest)) if call_chips > 0 else eq

    # SPR<=3 commitment: TPTK/overpair (MF) graduates to MMF.
    if spr <= KN["commit_spr"] and strength in ("MF", "MMF"):
        # supervivencia: en bote MULTIWAY no apilar UNA pareja (MF: TPTK/overpair); solo dos pares+ (MMF) commit.
        if SURVIVAL and n_villains >= 2 and strength == "MF":
            strength_eff = "MF"
        else:
            strength_eff = "MMF"
    else:
        strength_eff = strength

    report = (f"[S7] {street} pos={pos} hand={'/'.join(hole)} board={'/'.join(board)} "
              f"tex={texture} dyn={dyn} SPR={spr:.1f} str={strength}->{strength_eff} "
              f"eq={eq:.2f} adjOuts={adj_outs} PER={per:.2f} PME={pot_odds:.2f} "
              f"villains={n_villains} read={arc}")

    # value sizing helper (calling-station => size up; node-locking)
    def value_bet():
        frac = SIZING[texture][street]
        if station:
            frac = max(frac, KN["station_mult"])  # punitive vs inelastic caller
        br = allowed.get("betRange") or allowed.get("raiseRange") or {}
        amt = _clamp(int(pot * frac) or bb, br, bb, pot * 3)
        if (spr <= KN["commit_spr"] or station) and strength_eff == "MMF" and allowed.get("canAllIn"):
            amt = int(allowed.get("allInToAmount") or amt)
            return _act("all-in", amt, allowed, f"jam {strength} ({arc})",
                        _reasoning("all-in", eq, 0, strength_eff, texture, adj_outs))
        act = "bet" if "bet" in avail else ("raise" if "raise" in avail else None)
        if act is None:
            return None
        return _act(act, amt, allowed, f"value {strength} {int(frac*100)}% ({arc})",
                    _reasoning(act, eq, 0, strength_eff, texture, adj_outs))

    decision: Optional[dict] = None

    if call_chips == 0:
        # We have the betting lead option.
        if strength_eff in ("MMF", "MF") or (eq > KN["value_eq"] and not station):
            decision = value_bet()
        elif strength_eff == "MM":
            if ((street == "river" and station)) and ("bet" in avail or "raise" in avail):   # value fino en river vs station
                decision = value_bet()
            else:
                decision = _act("check", None, allowed, f"pot control {strength}", FALLBACK_REASONING) \
                    if "check" in avail else value_bet()
        else:
            # AIR / draw: Perejil bluff vs weakness; cbet-bluff on overfolder.
            weak_spot = dyn == "static" and not station
            river_ok = board_len < 5 or _river_blocker(hole, board)              # en river, solo farol con bloqueador (selección de farol; sin él → give-up)
            if not station and river_ok and (_perejil_ok(adj_outs, board_len, n_villains, overfolder) or
                                (overfolder and texture in ("dry", "semi"))):
                br = allowed.get("betRange") or {}
                frac = KN["cbet_bluff_frac"] if overfolder else SIZING[texture][street]
                amt = _clamp(int(pot * frac) or bb, br, bb, pot * 2)
                act = "bet" if "bet" in avail else None
                if act:
                    decision = _act(act, amt, allowed,
                                    f"perejil bluff {adj_outs}o ({arc})",
                                    _reasoning(act, 0, 0, "AIR", texture, adj_outs))
            if decision is None:
                decision = _act("check", None, allowed, f"check {strength}", FALLBACK_REASONING) \
                    if "check" in avail else _act("fold", None, allowed, "give up", FALLBACK_REASONING)
    else:
        # Facing a bet: PME vs PER + node-locking.
        big_bet = pot_odds >= 0.33
        scary = dyn == "defensive"
        if strength_eff == "MMF":
            if "raise" in avail and not (scary and strength == "MF"):
                rng = allowed.get("raiseRange") or {}
                if (spr <= KN["commit_spr"] or station) and allowed.get("canAllIn"):
                    decision = _act("all-in", int(allowed.get("allInToAmount") or 0), allowed,
                                    f"commit {strength} ({arc})",
                                    _reasoning("all-in", eq, pot_odds, strength_eff, texture, adj_outs))
                else:
                    amt = _clamp(int((pot + call_chips) * (1.2 if station else 0.8)) + call_chips,
                                 rng, call_chips * 2, pot * 3)
                    decision = _act("raise", amt, allowed, f"raise value {strength} ({arc})",
                                    _reasoning("raise", eq, pot_odds, strength_eff, texture, adj_outs))
            elif "call" in avail:
                decision = _act("call", None, allowed, f"call value {strength}",
                                _reasoning("call", eq, pot_odds, strength_eff, texture, adj_outs))
        elif strength_eff == "MF":
            if scary and big_bet and "fold" in avail and not station and not bluffy:
                decision = _act("fold", None, allowed, f"fold MF to scary {dyn}", FALLBACK_REASONING)
            elif honest and big_bet and "fold" in avail:                  # no pagar a un honesto que sobreapuesta
                decision = _act("fold", None, allowed, "fold MF vs honest big bet", FALLBACK_REASONING)
            elif "call" in avail and (eq_eff >= pot_odds + 0.03 or station or bluffy):   # bluffy → pagar ancho (catch bluffs)
                decision = _act("call", None, allowed, f"call MF ({arc})",
                                _reasoning("call", eq, pot_odds, "MF", texture, adj_outs))
            elif "fold" in avail:
                decision = _act("fold", None, allowed, "fold MF no price", FALLBACK_REASONING)
        elif strength_eff == "MM":
            mm_margin = -0.03 if (bluffy or station) else (0.08 if honest else 0.02)   # lectura: ancho vs bluffer, estrecho vs honesto
            if "call" in avail and eq_eff >= pot_odds + mm_margin and not (scary and big_bet and not bluffy):
                decision = _act("call", None, allowed, f"showdown MM ({arc})",
                                _reasoning("call", eq, pot_odds, "MM", texture, adj_outs))
            else:
                decision = _act("fold", None, allowed, "fold MM", FALLBACK_REASONING) \
                    if "fold" in avail else _act("check", None, allowed, "check MM", FALLBACK_REASONING)
        else:
            # MD / AIR with possible draw: PER vs PME, then Perejil bluff-raise.
            # Anti-guerra de re-subidas (postflop): si YA apostamos esta calle y nos re-suben (o la apuesta es
            # enorme), NO se re-faroleá con aire — la acción del rival manda sobre la etiqueta del HUD. Y el
            # bluff-raise de explotación exige un mínimo de equity (≥4 outs ajustadas), nunca aire de 0 outs.
            hero_bet_st = next((int(s.get("currentBetChips") or 0) for s in (table.get("seats") or [])
                                if s.get("seatNumber") == seat_n), 0)
            reraise_war = hero_bet_st > 0 or call_chips >= 0.5 * pot
            if (bluffy and strength_eff == "MD" and not reraise_war and not big_bet
                    and "call" in avail and eq_eff >= pot_odds - 0.02):   # bluff-catch a agresivos con MD/A-alto
                decision = _act("call", None, allowed, f"bluff-catch MD vs {arc}",
                                _reasoning("call", eq, pot_odds, "MD", texture, adj_outs))
            elif per >= pot_odds and "call" in avail:
                decision = _act("call", None, allowed, f"draw call {adj_outs}o PER{int(per*100)}",
                                _reasoning("call", per, pot_odds, "draw", texture, adj_outs))
            elif (not station and not bluffy and not reraise_war and "raise" in avail and
                  _perejil_ok(adj_outs, board_len, n_villains, overfolder)):
                rng = allowed.get("raiseRange") or {}
                amt = _clamp(int((pot + call_chips) * 0.8) + call_chips, rng, call_chips * 2, pot * 3)
                decision = _act("raise", amt, allowed, f"perejil bluff-raise {adj_outs}o ({arc})",
                                _reasoning("raise", 0, pot_odds, "AIR", texture, adj_outs))
            elif (overfolder and not reraise_war and adj_outs >= 4 and "raise" in avail
                  and texture in ("dry", "semi") and not station):
                rng = allowed.get("raiseRange") or {}
                amt = _clamp(int((pot + call_chips) * 0.7) + call_chips, rng, call_chips * 2, pot * 3)
                decision = _act("raise", amt, allowed, f"bluff-raise vs overfold {adj_outs}o ({arc})",
                                _reasoning("raise", 0, pot_odds, "AIR", texture, adj_outs))
            else:
                decision = _act("fold", None, allowed, f"fold {strength} (PER<PME, war={int(reraise_war)})",
                                FALLBACK_REASONING)

    if decision is None:  # safety net
        if "check" in avail:
            decision = _act("check", None, allowed, "fallback check", FALLBACK_REASONING)
        elif "call" in avail and pot_odds < 0.25:
            decision = _act("call", None, allowed, "fallback call", FALLBACK_REASONING)
        else:
            decision = _act("fold", None, allowed, "fallback fold", FALLBACK_REASONING)

    _log(report + f" => {decision['action']}"
                  + (f" {decision.get('amount')}" if decision.get("amount") else ""))
    return decision
