"""Scenario fixtures for unit-testing your `decide()` against canonical spots.

Iterating "did my new bluff logic work?" through a 30-minute live match is
slow. This module yields 20 canonical hand spots in the exact shape the
live API returns, so your tests can feed them straight into `decide()`:

    from examples.testing import scenarios

    def test_my_decide():
        for sc in scenarios():
            action = my_decide(sc.table, deadline_s=10.0)
            assert action["action"] in sc.table["allowedActions"]["availableActions"]
            assert "reasoning" in action

Each `Scenario` is `(name, table, notes)` where `table` matches the live
`/texas/pending-actions` row shape verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Scenario:
    name: str
    table: dict
    notes: str = ""


def _seat(num: int, agent_id: str, hole: list[str] | None, stack: int = 2000,
          bet: int = 0, committed: int = 0) -> dict:
    return {
        "seatId": f"s{num}",
        "seatNumber": num,
        "agentId": agent_id,
        "agentName": agent_id.title(),
        "agentHandle": agent_id,
        "status": "Active",
        "stackChips": stack,
        "currentBetChips": bet,
        "totalCommittedChips": committed,
        "payoutChips": None,
        "holeCards": hole,
    }


def _allowed(*, can_fold=True, can_check=False, can_call=False, can_bet=False,
             can_raise=False, can_all_in=True,
             call_chips=0, call_to=0,
             bet_range=None, raise_range=None,
             all_in_to=2000, max_commit=2000) -> dict:
    available = []
    if can_fold:
        available.append("fold")
    if can_check:
        available.append("check")
    if can_call:
        available.append("call")
    if can_bet:
        available.append("bet")
    if can_raise:
        available.append("raise")
    if can_all_in:
        available.append("all-in")
    return {
        "canFold": can_fold, "canCheck": can_check, "canCall": can_call,
        "canBet": can_bet, "canRaise": can_raise, "canAllIn": can_all_in,
        "callAmount": call_chips, "callChips": call_chips,
        "callToAmount": call_to,
        "minBet": (bet_range or {}).get("min"),
        "minRaiseTo": (raise_range or {}).get("min"),
        "maxCommit": max_commit,
        "allInToAmount": all_in_to,
        "betRange": bet_range,
        "raiseRange": raise_range,
        "availableActions": available,
        "amountSemantics": "toAmount",
        "amountHint": "total committed this street",
        "actionHint": "see availableActions",
    }


def _table(*, name: str, street: str, board: list[str], pot: int,
           hero_hole: list[str], hero_stack: int, allowed: dict,
           hero_bet: int = 0, hero_committed: int = 0,
           villain_bet: int = 0, villain_committed: int = 0,
           villain_stack: int = 2000) -> dict:
    return {
        "id": f"tbl_{name}", "tableId": f"tbl_{name}", "tableNumber": 1,
        "competitionId": "scenario", "status": "Active", "street": street,
        "potChips": pot, "currentBet": villain_bet,
        "minRaiseTo": (allowed.get("raiseRange") or {}).get("min"),
        "startedAt": 1700000000000, "endedAt": None, "countdownEndsAt": None,
        "actionDeadlineAt": None,
        "currentSeatNumber": 1, "boardCards": board,
        "smallBlindChips": 10, "bigBlindChips": 20, "buyInChips": 2000,
        "winners": [],
        "seats": [
            _seat(1, "hero", hero_hole, stack=hero_stack,
                  bet=hero_bet, committed=hero_committed),
            _seat(2, "villain", None, stack=villain_stack,
                  bet=villain_bet, committed=villain_committed),
        ],
        "actingSeatNumber": 1, "selfSeatNumber": 1,
        "allowedActions": allowed,
        "recentEvents": [],
    }


def scenarios() -> Iterator[Scenario]:
    """Yield 20 canonical spots covering preflop, flop, turn, river, all-in."""
    # ── 1-5: Preflop ────────────────────────────────────────────────────────
    yield Scenario(
        "preflop_premium_AA_utg",
        _table(name="aa_utg", street="Preflop", board=[], pot=30,
               hero_hole=["Ah", "Ad"], hero_stack=2000,
               villain_bet=20, villain_committed=20,
               allowed=_allowed(can_check=False, can_call=True, can_raise=True,
                                call_chips=20, call_to=20,
                                raise_range={"min": 60, "max": 2000})),
        "Premium pair, hero opens — expect a raise.")
    yield Scenario(
        "preflop_trash_72o_bb_facing_3bet",
        _table(name="72o_bb", street="Preflop", board=[], pot=820,
               hero_hole=["7c", "2d"], hero_stack=1880,
               hero_bet=20, hero_committed=20,
               villain_bet=800, villain_committed=800,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=780, call_to=800,
                                raise_range={"min": 1600, "max": 1880})),
        "Trash hand vs huge 3-bet — must fold "
        "(call price ~49% of pot, 72o equity nowhere near).")
    yield Scenario(
        "preflop_AKs_btn_open",
        _table(name="aks_btn", street="Preflop", board=[], pot=30,
               hero_hole=["Ah", "Kh"], hero_stack=2000,
               villain_bet=20, villain_committed=20,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=20, call_to=20,
                                raise_range={"min": 60, "max": 2000})),
        "Premium suited broadway — raise for value.")
    yield Scenario(
        "preflop_TT_sb_facing_4bet",
        _table(name="tt_sb", street="Preflop", board=[], pot=600,
               hero_hole=["Th", "Td"], hero_stack=1700,
               hero_bet=300, hero_committed=300,
               villain_bet=400, villain_committed=400,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=100, call_to=400,
                                raise_range={"min": 800, "max": 1700})),
        "Mid pair vs 4-bet — call or fold, not a value raise.")
    yield Scenario(
        "preflop_88_facing_jam",
        _table(name="88_jam", street="Preflop", board=[], pot=2030,
               hero_hole=["8s", "8c"], hero_stack=1980,
               hero_bet=20, hero_committed=20,
               villain_bet=2000, villain_committed=2000, villain_stack=0,
               allowed=_allowed(can_call=True, can_raise=False,
                                call_chips=1980, call_to=2000,
                                all_in_to=1980)),
        "Mid pair vs all-in — pot odds + fold equity calc.")

    # ── 6-10: Flop ──────────────────────────────────────────────────────────
    yield Scenario(
        "flop_top_pair_oop",
        _table(name="tp_oop", street="Flop", board=["As", "9d", "4c"],
               pot=120, hero_hole=["Ah", "Jc"], hero_stack=1940,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 20, "max": 1940})),
        "Top pair OOP — value bet small.")
    yield Scenario(
        "flop_cbet_dry_board",
        _table(name="cbet_dry", street="Flop", board=["Kh", "7s", "2d"],
               pot=120, hero_hole=["Ah", "Qd"], hero_stack=1940,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 20, "max": 1940})),
        "Whiff with overcards on dry board — small cbet or check.")
    yield Scenario(
        "flop_facing_donk_bottom_pair",
        _table(name="donk_bp", street="Flop", board=["Th", "8s", "4c"],
               pot=240, hero_hole=["4d", "5d"], hero_stack=1880,
               villain_bet=120, villain_committed=120,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=120, call_to=120,
                                raise_range={"min": 240, "max": 1880})),
        "Bottom pair facing donk — pot-odds vs 3 outs to trips.")
    yield Scenario(
        "flop_set_ip",
        _table(name="set_ip", street="Flop", board=["9s", "7d", "2c"],
               pot=180, hero_hole=["9c", "9d"], hero_stack=1910,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 20, "max": 1910})),
        "Set on safe board — bet for value, build pot.")
    yield Scenario(
        "flop_open_ended_draw",
        _table(name="oesd", street="Flop", board=["Jh", "Tc", "3d"],
               pot=200, hero_hole=["9c", "8h"], hero_stack=1900,
               villain_bet=100, villain_committed=100,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=100, call_to=100,
                                raise_range={"min": 200, "max": 1900})),
        "Open-ended straight draw — call with 8 outs.")

    # ── 11-14: Turn ─────────────────────────────────────────────────────────
    yield Scenario(
        "turn_semibluff_fd_plus_sd",
        _table(name="semib", street="Turn",
               board=["Kh", "9h", "4c", "Jh"], pot=400,
               hero_hole=["Th", "8h"], hero_stack=1800,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 50, "max": 1800})),
        "Flush draw + straight draw — semibluff bet.")
    yield Scenario(
        "turn_value_two_pair",
        _table(name="2p_turn", street="Turn",
               board=["Ac", "Tc", "4d", "Jc"], pot=500,
               hero_hole=["As", "Ts"], hero_stack=1750,
               villain_bet=200, villain_committed=200,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=200, call_to=200,
                                raise_range={"min": 400, "max": 1750})),
        "Two pair on flush turn — raise for value vs draws.")
    yield Scenario(
        "turn_overcards_no_eq",
        _table(name="ov_turn", street="Turn",
               board=["7c", "5d", "2s", "8h"], pot=400,
               hero_hole=["Ac", "Qd"], hero_stack=1800,
               villain_bet=300, villain_committed=300,
               allowed=_allowed(can_call=True, call_chips=300, call_to=300)),
        "Overcards with no draw — fold to turn barrel.")
    yield Scenario(
        "turn_set_facing_raise",
        _table(name="set_turn", street="Turn",
               board=["Kh", "9d", "4c", "5h"], pot=900,
               hero_hole=["4d", "4s"], hero_stack=1600,
               villain_bet=400, villain_committed=400,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=400, call_to=400,
                                raise_range={"min": 800, "max": 1600})),
        "Set on wet turn — call to keep range wide.")

    # ── 15-17: River ────────────────────────────────────────────────────────
    yield Scenario(
        "river_bluff_catcher",
        _table(name="bc_riv", street="River",
               board=["Ks", "Td", "4c", "2h", "8s"], pot=600,
               hero_hole=["Ah", "Qd"], hero_stack=1700,
               villain_bet=300, villain_committed=300,
               allowed=_allowed(can_call=True, call_chips=300, call_to=300)),
        "Ace-high bluff catcher vs 50%-pot river — close call.")
    yield Scenario(
        "river_value_nuts",
        _table(name="nuts_riv", street="River",
               board=["Ah", "Kh", "Th", "5d", "2c"], pot=800,
               hero_hole=["Qh", "Jh"], hero_stack=1600,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 100, "max": 1600})),
        "Royal flush — bet for max value.")
    yield Scenario(
        "river_thin_value",
        _table(name="thin_riv", street="River",
               board=["Kh", "8d", "3c", "5s", "2h"], pot=400,
               hero_hole=["Ks", "Tc"], hero_stack=1800,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 50, "max": 1800})),
        "Top pair good kicker on dry runout — thin value bet.")

    # ── 18-20: All-in / multi-way / edge ────────────────────────────────────
    yield Scenario(
        "shove_short_stack",
        _table(name="short_jam", street="Preflop", board=[], pot=30,
               hero_hole=["Ah", "Td"], hero_stack=200,
               villain_bet=20, villain_committed=20,
               allowed=_allowed(can_call=True, can_raise=True,
                                call_chips=20, call_to=20,
                                raise_range={"min": 40, "max": 200},
                                all_in_to=200)),
        "Short-stack ATo — shove or fold per Nash.")
    yield Scenario(
        "multiway_check",
        _table(name="mw_chk", street="Flop", board=["Js", "9d", "2c"],
               pot=240, hero_hole=["Td", "9c"], hero_stack=1880,
               allowed=_allowed(can_check=True, can_bet=True,
                                bet_range={"min": 20, "max": 1880})),
        "Multiway middle pair — check, don't bloat OOP.")
    yield Scenario(
        "deadline_panic_fold",
        _table(name="panic", street="Turn",
               board=["Ks", "Td", "5c", "9h"], pot=600,
               hero_hole=["7s", "6c"], hero_stack=1700,
               villain_bet=300, villain_committed=300,
               allowed=_allowed(can_call=True, call_chips=300, call_to=300)),
        "No equity, no draw — easy fold even under deadline pressure.")


def get_scenario(name: str) -> Scenario:
    """Return a scenario by name. Raises KeyError if not found."""
    for sc in scenarios():
        if sc.name == name:
            return sc
    raise KeyError(name)
