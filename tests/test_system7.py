"""Unit tests for the System 7 exploit engine (decide_system7)."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "examples"))

import decide_system7 as d  # noqa: E402


def _mk(hole, board=None, pot=10, call=0, avail=None, **kw):
    t = {
        "selfSeatNumber": 4, "potChips": pot, "bigBlindChips": 2,
        "boardCards": board or [],
        "seats": [
            {"seatNumber": 4, "holeCards": hole, "stackChips": 200, "status": "active"},
            {"seatNumber": 1, "holeCards": [], "stackChips": 200, "status": "active"},
        ],
        "allowedActions": {"availableActions": avail or [], "callChips": call},
    }
    t["allowedActions"].update(kw.pop("allowed", {}))
    t.update(kw)
    return t


# ── taxonomy ──────────────────────────────────────────────────────────────────
def test_strength_taxonomy():
    assert d._strength(["7h", "7d"], ["7c", "2d", "9s"], "dry") == "MMF"   # set
    assert d._strength(["As", "Kd"], ["Ah", "7c", "2d"], "dry") == "MF"    # TPTK
    assert d._strength(["Qs", "Qd"], ["7h", "2c", "9s"], "dry") == "MF"    # overpair
    assert d._strength(["As", "3d"], ["Ah", "7c", "2d"], "dry") == "MM"    # top pair weak kicker
    assert d._strength(["2s", "3d"], ["Ah", "Kc", "2d"], "dry") == "MD"    # bottom pair
    assert d._strength(["5s", "4d"], ["Ah", "Kc", "9d"], "dry") == "AIR"   # nothing
    # set degrades to MM on extreme board
    assert d._strength(["7h", "7d"], ["7c", "8s", "9s"], "extreme") == "MM"
    # two pair on a PAIRED board: underpair riding the board pair is a bluff-catcher (no commit), real two pair stays strong
    assert d._strength(["9h", "9d"], ["5d", "Jh", "Ks", "Kc"], "semi") == "MM"   # 99 on 5JKK (underpair) -> MM, not jam
    assert d._strength(["Qs", "Qd"], ["Ks", "Kc", "Jh", "5d"], "semi") == "MF"   # QQ over the board on KKJ5 -> MF
    assert d._strength(["Ad", "Jc"], ["Ks", "Kc", "Jh", "5d"], "semi") == "MF"   # AJ = top two (KK+JJ) on paired board -> MF


def test_texture_grades():
    assert d._texture(["Kc", "8h", "3d"]) == "dry"
    assert d._texture(["9h", "8h", "2c"]) == "semi"        # two-tone
    assert d._texture(["9h", "8c", "7d"]) == "coord"       # 3 connected
    assert d._texture(["Ah", "Kh", "Qh", "Jh"]) == "extreme"  # 4-flush + connected


def test_card_dynamic():
    assert d._card_dynamic(["7h", "8c", "2d", "As"], False) == "offensive"   # A overcard
    assert d._card_dynamic(["7h", "8h", "2d", "9h"], False) == "defensive"   # flush completes
    assert d._card_dynamic(["Kh", "8c", "2d", "5s"], False) == "static"


# ── adjusted outs ─────────────────────────────────────────────────────────────
def test_adjusted_outs():
    assert d._adjusted_outs(["Ts", "6s"], ["As", "7s", "2d"], "semi") == 9    # nut flush draw
    assert d._adjusted_outs(["9c", "8d"], ["7h", "Tc", "2s"], "dry") == 8     # OESD
    assert d._adjusted_outs(["9c", "6d"], ["7h", "Tc", "2s"], "dry") == 4     # gutshot (need 8)
    # OESD penalised on a flush-draw board
    assert d._adjusted_outs(["9c", "8c"], ["7h", "Th", "2h"], "coord") <= 5
    assert d._adjusted_outs(["Ad", "Kd"], ["7h", "2c", "9s"], "dry") == 4     # two overcards (dry)
    assert d._adjusted_outs(["Ad", "Kd"], ["7h", "8h", "9h"], "extreme") == 0  # overcards dead


# ── PME / PER / Perejil ───────────────────────────────────────────────────────
def test_pme_per():
    assert abs(d._pme(5, 10) - 0.3333) < 0.01     # half-pot bet
    assert abs(d._per(8, 3) - 0.32) < 0.001       # 8 outs on flop (x4)
    assert abs(d._per(8, 4) - 0.16) < 0.001       # 8 outs on turn (x2)


def test_perejil_thresholds():
    assert d._perejil_ok(8, 3, 1, False) is True      # flop, 8 outs HU
    assert d._perejil_ok(7, 3, 1, False) is False     # flop needs 8
    assert d._perejil_ok(10, 4, 1, False) is True     # turn needs 10
    assert d._perejil_ok(8, 3, 2, False) is False     # +1 for extra villain
    assert d._perejil_ok(6, 3, 1, True) is True       # overfolder: -2 relax


# ── HUD archetypes ────────────────────────────────────────────────────────────
def test_archetypes():
    assert d._archetype({"N": 50, "vpip": 80, "pfr": 0}) == "UNKNOWN"   # N<100 blind
    assert d._archetype({"N": 600, "vpip": 12, "pfr": 10, "af": 2}) == "NIT"
    assert d._archetype({"N": 600, "vpip": 22, "pfr": 18, "af": 2.5}) == "TAG"
    assert d._archetype({"N": 600, "vpip": 28, "pfr": 24, "af": 3}) == "LAG"
    assert d._archetype({"N": 600, "vpip": 34, "pfr": 12, "af": 0.8}) == "STATION"
    assert d._archetype({"N": 600, "vpip": 46, "pfr": 36, "af": 4}) == "MANIAC"
    # API ships fractions (0.216 = 21.6%) + a playingStyle dict — must read as balanced TAG
    assert d._archetype({"N": 846127, "vpip": 0.216, "pfr": 0.164, "af": 1.9,
                         "wtsd": 0.928, "playingStyle": {"label": "balanced"}}) == "TAG"


# ── decide() integrations ─────────────────────────────────────────────────────
def test_preflop_open_fold():
    a = d.decide(_mk(["As", "Ks"], avail=["fold", "call", "raise"], call=0,
                     allowed={"canRaise": True, "raiseRange": {"min": 4, "max": 200}}))
    assert a["action"] == "raise" and a["amount"] >= 4
    a = d.decide(_mk(["7s", "2d"], avail=["fold", "call", "raise"], call=0,
                     allowed={"canRaise": True, "raiseRange": {"min": 4, "max": 200}}))
    assert a["action"] == "fold"


def test_value_and_fold():
    a = d.decide(_mk(["As", "Kd"], ["Ah", "7c", "2d"], pot=10, call=0,
                     avail=["check", "bet"], allowed={"canCheck": True, "canBet": True,
                                                       "betRange": {"min": 2, "max": 190}}))
    assert a["action"] == "bet"
    a = d.decide(_mk(["7s", "2d"], ["Ah", "Kc", "9d"], pot=10, call=6,
                     avail=["fold", "call", "raise"], allowed={"raiseRange": {"min": 12, "max": 190}}))
    assert a["action"] == "fold"


def test_draw_call_by_per():
    a = d.decide(_mk(["Ts", "6s"], ["As", "7s", "2d"], pot=10, call=2,
                     avail=["fold", "call", "raise"], allowed={"raiseRange": {"min": 12, "max": 190}}))
    assert a["action"] == "call"   # 9 outs, PER 36% >= PME ~17%


def test_spr_commit_set():
    a = d.decide(_mk(["7h", "7d"], ["Ah", "7c", "2d"], pot=120, call=40,
                     avail=["fold", "call", "raise"],
                     allowed={"canAllIn": True, "allInToAmount": 160, "raiseRange": {"min": 80, "max": 160}}))
    assert a["action"] in ("all-in", "raise")


def test_station_sizes_up():
    ctx = {"hud": {1: {"N": 600, "vpip": 40, "pfr": 12, "af": 0.8, "wtsd": 40, "playingStyle": "station"}},
           "aggressor_seat": None}
    a = d.decide(_mk(["As", "Qd"], ["Ah", "7c", "2d"], pot=10, call=0,
                     avail=["check", "bet"], allowed={"canCheck": True, "canBet": True,
                                                      "betRange": {"min": 2, "max": 190}}),
                 research_context=ctx)
    assert a["action"] == "bet" and a["amount"] >= 10   # >=100% pot punitive vs station


def test_never_crashes_on_empty():
    a = d.decide(_mk([], [], pot=0, call=0, avail=["check"], allowed={"canCheck": True}))
    assert a["action"] in ("check", "fold")


# ── position inference + open/complete/defend ──────────────────────────────────
def _pf(hole, seat, sb_seat=1, bb_seat=2, n=6, call=2, opp_bet=2, bb=2):
    seats = []
    for i in range(1, n + 1):
        cb = 1 if i == sb_seat else (2 if i == bb_seat else (opp_bet if opp_bet > 2 else 0))
        seats.append({"seatNumber": i, "holeCards": hole if i == seat else [],
                      "stackChips": 200, "status": "active",
                      "currentBetChips": cb, "totalCommittedChips": cb})
    return {"selfSeatNumber": seat, "potChips": 6, "bigBlindChips": bb, "smallBlindChips": 1,
            "boardCards": [], "seats": seats,
            "recentEvents": [{"type": "BlindPosted", "summary": {"amount": 1, "seatNumber": sb_seat}},
                             {"type": "BlindPosted", "summary": {"amount": 2, "seatNumber": bb_seat}}],
            "allowedActions": {"availableActions": ["fold", "call", "raise"], "callChips": call,
                               "canRaise": True, "raiseRange": {"min": 6, "max": 200}}}


def test_position_from_blinds():
    assert d._position(_pf(["As", "Ks"], seat=3))[0] == "UTG"
    assert d._position(_pf(["As", "Ks"], seat=4))[0] == "MP"
    pos, ip, known = d._position(_pf(["As", "Ks"], seat=6))
    assert pos == "BTN" and ip and known
    # no blind signal => conservative, not known
    assert d._position({"selfSeatNumber": 1, "boardCards": [],
                        "seats": [{"seatNumber": 1, "status": "active"}]}) == ("UTG", False, False)


def test_preflop_open_complete_defend():
    # UTG first-in (only blinds posted) with AKs -> open-raise
    assert d.decide(_pf(["As", "Ks"], seat=3, call=2, opp_bet=2))["action"] == "raise"
    # SB facing a complete (folded around), playable hand -> complete/raise, not fold
    assert d.decide(_pf(["Ks", "Ts"], seat=1, call=1, opp_bet=2))["action"] in ("call", "raise")
    # facing a real raise with trash -> fold
    assert d.decide(_pf(["7s", "2d"], seat=4, call=8, opp_bet=8))["action"] == "fold"
