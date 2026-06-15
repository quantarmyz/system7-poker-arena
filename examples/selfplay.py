"""Local headless self-play — fast feedback for `decide()` development.

NO network calls. NO Arena API. Just an in-process poker game where your
`decide()` function (from examples/agent.py or any custom file) plays N
hands against simple local opponents and prints bb/100.

Why: `pokerkit run` against Arena is the real benchmark, but each
50-hand preview takes ~3-5 minutes. For tight iteration on `decide()`
you want a 30-second feedback loop. This is that loop.

Caveat: local opponents are SIMPLE heuristic bots (random / tight-passive
/ loose-passive / always-call), NOT the DeepCFR reference panel that
Arena uses. Use selfplay to catch bugs and validate gross direction
("does my new range table actually open more hands?"). Use
`pokerkit run --max-hands 50` to confirm gains hold against DeepCFR.

Usage:
    pokerkit selfplay                              # 200 hands HU vs tight-passive
    pokerkit selfplay --hands 1000 --opponent random
    pokerkit selfplay --agent examples/agent.py    # explicit decide() source
    pokerkit selfplay --players 6                  # 6-max vs mixed bots
    pokerkit selfplay --seed 42                    # reproducible
"""
from __future__ import annotations

import argparse
import importlib.util
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pokerkit import (  # type: ignore
    Automation,
    BettingStructure,
    Deck,
    NoLimitTexasHoldem,
    Opening,
    State,
    Street,
)


# ─── Opponent bots (local decide-style functions) ───────────────────────────

def bot_always_check_call(table: dict, **_: Any) -> dict:
    """Free → check, else call. Never folds, never raises."""
    allowed = table.get("allowedActions") or {}
    avail = allowed.get("availableActions") or []
    if "check" in avail:
        return {"action": "check"}
    if "call" in avail:
        return {"action": "call"}
    return {"action": "fold"}


def bot_random(table: dict, **_: Any) -> dict:
    """Uniform random over legal actions; bet sizing 50% pot."""
    allowed = table.get("allowedActions") or {}
    avail = list(allowed.get("availableActions") or [])
    if not avail:
        return {"action": "fold"}
    pick = random.choice(avail)
    if pick == "bet":
        br = allowed.get("betRange") or {}
        lo, hi = int(br.get("min") or 1), int(br.get("max") or 1)
        pot = int(table.get("potChips") or 0)
        return {"action": "bet",
                "amount": max(lo, min(int(pot * 0.5), hi))}
    if pick == "raise":
        rr = allowed.get("raiseRange") or {}
        lo, hi = int(rr.get("min") or 1), int(rr.get("max") or 1)
        return {"action": "raise", "amount": max(lo, min(lo * 2, hi))}
    return {"action": pick}


def bot_tight_passive(table: dict, **_: Any) -> dict:
    """Folds weak hands preflop, calls/checks postflop with anything
    that connects (pair / draw)."""
    allowed = table.get("allowedActions") or {}
    avail = allowed.get("availableActions") or []
    call_chips = int(allowed.get("callChips") or 0)
    pot = int(table.get("potChips") or 0)

    self_seat_num = table.get("selfSeatNumber")
    seats = table.get("seats") or []
    self_seat = next((s for s in seats if s.get("seatNumber") == self_seat_num), {})
    hole = list(self_seat.get("holeCards") or [])
    board = list(table.get("boardCards") or [])

    ranks = "23456789TJQKA"
    def _rank(card: str) -> int:
        return ranks.index(card[0].upper()) if card and card[0].upper() in ranks else 0

    pair = len(hole) == 2 and hole[0][0] == hole[1][0]
    high_card = max((_rank(c) for c in hole), default=0)
    suited = len(hole) == 2 and len(hole[0]) > 1 and len(hole[1]) > 1 and hole[0][-1] == hole[1][-1]

    if not board:
        # Preflop: only continue with pair, high cards, or suited broadways.
        strong = pair or high_card >= 10 or (suited and high_card >= 9)
        if not strong and call_chips > 0:
            return {"action": "fold"}
        if call_chips == 0:
            return {"action": "check"} if "check" in avail else {"action": "fold"}
        return {"action": "call"}

    # Postflop: connect with board?
    board_ranks = {c[0].upper() for c in board}
    hole_ranks = {c[0].upper() for c in hole}
    connects = bool(hole_ranks & board_ranks) or pair
    if call_chips == 0:
        return {"action": "check"} if "check" in avail else {"action": "fold"}
    if connects:
        # call up to 50% pot
        if call_chips <= max(int(pot * 0.5), 1):
            return {"action": "call"}
    return {"action": "fold"}


def bot_loose_passive(table: dict, **_: Any) -> dict:
    """Calls almost anything. Won't fold to small bets, occasional fold to big."""
    allowed = table.get("allowedActions") or {}
    avail = allowed.get("availableActions") or []
    call_chips = int(allowed.get("callChips") or 0)
    pot = int(table.get("potChips") or 0)
    if call_chips == 0:
        return {"action": "check"} if "check" in avail else {"action": "fold"}
    # Fold only if bet is > 200% pot (huge overbet)
    if call_chips > pot * 2 and "fold" in avail:
        return {"action": "fold"}
    return {"action": "call"}


BOT_POOL = {
    "tight": bot_tight_passive,
    "loose": bot_loose_passive,
    "random": bot_random,
    "call": bot_always_check_call,
}


# ─── pokerkit state → arena table dict adapter ──────────────────────────────

def _street_label(state: State) -> str:
    # 0 cards → Preflop; 3 → Flop; 4 → Turn; 5 → River.
    n = len(state.board_cards)
    if n <= 0:
        return "Preflop"
    return ("Flop", "Turn", "River")[min(max(n - 3, 0), 2)]


def _build_table(state: State, hero_idx: int, table_id: str,
                 starting_stacks: list[int], small_blind: int,
                 big_blind: int) -> dict:
    """Convert a pokerkit State into the arena `table` dict shape that
    decide() expects. Only fields actually consumed by decide() are
    populated."""
    actor = state.actor_index
    is_my_turn = (actor == hero_idx)

    # Pot = sum of all pot amounts + sum of current-street bets.
    pot_total = sum(p.amount for p in state.pots) if state.pots else 0
    bets = list(state.bets) if state.bets else []
    pot_total += sum(bets)

    seats = []
    for i in range(len(starting_stacks)):
        hole = list(state.hole_cards[i]) if i < len(state.hole_cards) else []
        hole_strs = [repr(c) for c in hole] if (i == hero_idx and hole) else []
        seats.append({
            "seatNumber": i + 1,
            "agentId": f"local_seat_{i+1}",
            "agentHandle": "hero" if i == hero_idx else f"bot_{i+1}",
            "holeCards": hole_strs,
            "stackChips": int(state.stacks[i]),
        })

    # Decide what actions are legal.
    available: list[str] = []
    call_chips = 0
    call_to_amount = 0
    can_check = can_bet = can_raise = False
    bet_min = bet_max = 0
    raise_min = raise_max = 0

    if is_my_turn:
        if state.can_fold():
            available.append("fold")
        if state.can_check_or_call():
            call_chips = int(state.checking_or_calling_amount or 0)
            if call_chips == 0:
                available.append("check")
                can_check = True
            else:
                available.append("call")
                call_to_amount = (bets[hero_idx] if hero_idx < len(bets) else 0) + call_chips
        if state.can_complete_bet_or_raise_to():
            try:
                rmin = int(state.min_completion_betting_or_raising_to_amount or 0)
                rmax = int(state.max_completion_betting_or_raising_to_amount or 0)
            except Exception:
                rmin, rmax = 0, 0
            # If any opponent has voluntarily put in chips on this street
            # beyond the blind, treat as "raise"; otherwise it's a "bet".
            max_bet_so_far = max(bets) if bets else 0
            if max_bet_so_far > big_blind or call_chips > 0:
                available.append("raise")
                can_raise, raise_min, raise_max = True, rmin, rmax
            else:
                available.append("bet")
                can_bet, bet_min, bet_max = True, rmin, rmax

    return {
        "tableId": table_id,
        "potChips": int(pot_total),
        "street": _street_label(state),
        "boardCards": [repr(c) for c in state.board_cards],
        "selfSeatNumber": hero_idx + 1,
        "seats": seats,
        "allowedActions": {
            "availableActions": available,
            "callChips": int(call_chips),
            "callToAmount": int(call_to_amount),
            "canCheck": bool(can_check),
            "canBet": bool(can_bet),
            "canRaise": bool(can_raise),
            "betRange": {"min": int(bet_min), "max": int(bet_max)},
            "raiseRange": {"min": int(raise_min), "max": int(raise_max)},
        },
        "secondsUntilDeadline": 10.0,
    }


def _apply_action(state: State, action: dict, big_blind: int) -> None:
    """Apply the chosen action to the pokerkit state."""
    name = (action.get("action") or "").lower()
    amount = action.get("amount")
    try:
        if name == "fold":
            state.fold()
        elif name in ("check", "call"):
            state.check_or_call()
        elif name in ("bet", "raise"):
            try:
                lo = int(state.min_completion_betting_or_raising_to_amount or big_blind)
                hi = int(state.max_completion_betting_or_raising_to_amount or lo)
            except Exception:
                lo, hi = big_blind, big_blind * 100
            if amount is None:
                amount = lo
            amount = max(lo, min(int(amount), hi))
            state.complete_bet_or_raise_to(amount)
        else:
            # Unknown action → fold as safe fallback.
            state.fold()
    except Exception as e:
        # Illegal at the engine level — fold to keep the hand moving.
        print(f"  [selfplay] WARN: action {name}/{amount} rejected ({e}); folding",
              file=sys.stderr)
        try:
            state.fold()
        except Exception:
            pass


# ─── Decide loader (mirror agent.py's --agent loader) ────────────────────────

def _load_decide_from_path(path: str) -> Callable:
    p = Path(path).resolve()
    if not p.exists():
        raise SystemExit(f"--agent path not found: {p}")
    spec = importlib.util.spec_from_file_location("user_agent", str(p))
    if not spec or not spec.loader:
        raise SystemExit(f"could not load module from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "decide"):
        raise SystemExit(f"{p} does not define decide()")
    return mod.decide


# ─── Selfplay engine ────────────────────────────────────────────────────────

def play_one_hand(decide_fn: Callable, opponents: list[Callable],
                  starting_stack: int, small_blind: int, big_blind: int,
                  hand_id: int, hero_idx: int = 0,
                  max_actions: int = 200) -> int:
    """Play a single hand; return hero's chip delta (final stack -
    starting stack)."""
    n = 1 + len(opponents)
    stacks = [starting_stack] * n
    state: State = NoLimitTexasHoldem.create_state(
        automations=(
            Automation.ANTE_POSTING,
            Automation.BET_COLLECTION,
            Automation.BLIND_OR_STRADDLE_POSTING,
            Automation.CARD_BURNING,
            Automation.HOLE_DEALING,
            Automation.BOARD_DEALING,
            Automation.RUNOUT_COUNT_SELECTION,
            Automation.HOLE_CARDS_SHOWING_OR_MUCKING,
            Automation.HAND_KILLING,
            Automation.CHIPS_PUSHING,
            Automation.CHIPS_PULLING,
        ),
        ante_trimming_status=True,
        raw_antes=0,
        raw_blinds_or_straddles=(small_blind, big_blind) + (0,) * (n - 2),
        min_bet=big_blind,
        raw_starting_stacks=tuple(stacks),
        player_count=n,
    )

    table_id = f"local-{hand_id:05d}"
    steps = 0
    while state.status and state.actor_index is not None and steps < max_actions:
        actor = state.actor_index
        table = _build_table(state, actor, table_id, stacks, small_blind, big_blind)
        # Pick the deciding function for this seat.
        if actor == hero_idx:
            fn = decide_fn
        else:
            opp_idx = actor if actor < hero_idx else actor - 1
            fn = opponents[opp_idx % len(opponents)]
        try:
            action = fn(table, deadline_s=10.0)
        except TypeError:
            action = fn(table)
        if not isinstance(action, dict):
            action = {"action": "fold"}
        _apply_action(state, action, big_blind)
        steps += 1

    final = int(state.stacks[hero_idx])
    return final - starting_stack


def run_selfplay(decide_fn: Callable, n_hands: int, opponent_label: str,
                 n_players: int, starting_stack: int, small_blind: int,
                 big_blind: int, seed: Optional[int] = None) -> dict:
    """Run N hands and return aggregate stats."""
    if seed is not None:
        random.seed(seed)
    # Build opponents list (length = n_players - 1).
    if opponent_label == "mixed":
        rotation = [bot_tight_passive, bot_loose_passive, bot_random,
                    bot_always_check_call, bot_tight_passive]
        opponents = rotation[: n_players - 1]
    else:
        bot = BOT_POOL.get(opponent_label) or bot_tight_passive
        opponents = [bot] * (n_players - 1)

    deltas: list[int] = []
    t0 = time.time()
    for i in range(n_hands):
        try:
            d = play_one_hand(decide_fn, opponents,
                              starting_stack=starting_stack,
                              small_blind=small_blind, big_blind=big_blind,
                              hand_id=i + 1)
        except Exception as e:
            print(f"  [selfplay] WARN: hand {i+1} failed ({e}); counted as 0",
                  file=sys.stderr)
            d = 0
        deltas.append(d)
        if (i + 1) % max(1, n_hands // 10) == 0:
            print(f"  ... {i+1}/{n_hands} hands  net={sum(deltas):+d} chips")
    elapsed = time.time() - t0

    net = sum(deltas)
    wins = sum(1 for d in deltas if d > 0)
    losses = sum(1 for d in deltas if d < 0)
    pushes = n_hands - wins - losses
    bb_per_100 = (net / big_blind) / max(n_hands, 1) * 100

    return {
        "hands": n_hands,
        "opponent": opponent_label,
        "players": n_players,
        "net_chips": net,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "bb_per_100": bb_per_100,
        "elapsed_s": elapsed,
        "hands_per_s": n_hands / max(elapsed, 0.001),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run local self-play hands against simple bots for fast decide() "
            "iteration. No network. No Arena API. NOT a replacement for "
            "`pokerkit run` (which scores you against DeepCFR)."
        ),
    )
    parser.add_argument("--hands", type=int, default=200,
                        help="Number of hands to play (default 200)")
    parser.add_argument("--players", type=int, default=2,
                        help="Total players including hero, 2-6 (default 2 = HU)")
    parser.add_argument("--opponent", default="tight",
                        choices=["tight", "loose", "random", "call", "mixed"],
                        help="Opponent profile (default tight)")
    parser.add_argument("--agent", default=None,
                        help="Path to a Python file defining decide(); "
                             "default uses examples/agent.py")
    parser.add_argument("--starting-stack", type=int, default=200,
                        help="Starting stack per player in chips (default 200 = 100 BB)")
    parser.add_argument("--small-blind", type=int, default=1)
    parser.add_argument("--big-blind", type=int, default=2)
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed for reproducible runs")
    args = parser.parse_args(argv)

    if args.players < 2 or args.players > 6:
        print("ERROR: --players must be between 2 and 6.", file=sys.stderr)
        return 2

    # Load decide().
    if args.agent:
        decide_fn = _load_decide_from_path(args.agent)
        src = args.agent
    else:
        # Default — import from sibling agent.py.
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        import agent  # noqa: WPS433
        decide_fn = agent.decide
        src = "examples/agent.py"

    print(f"[selfplay] hero=decide() from {src}")
    print(f"[selfplay] opponents={args.opponent} x{args.players - 1}  "
          f"players={args.players}  stacks={args.starting_stack}  "
          f"blinds={args.small_blind}/{args.big_blind}  seed={args.seed}")
    print(f"[selfplay] playing {args.hands} hands ...")

    stats = run_selfplay(
        decide_fn=decide_fn,
        n_hands=args.hands,
        opponent_label=args.opponent,
        n_players=args.players,
        starting_stack=args.starting_stack,
        small_blind=args.small_blind,
        big_blind=args.big_blind,
        seed=args.seed,
    )

    sep = "─" * 56
    print("")
    print(sep)
    print(f"  hands       : {stats['hands']}")
    print(f"  opponent    : {stats['opponent']} x{stats['players'] - 1}")
    print(f"  wins/losses : {stats['wins']}/{stats['losses']}  "
          f"(push: {stats['pushes']})")
    print(f"  net chips   : {stats['net_chips']:+d}")
    print(f"  bb/100      : {stats['bb_per_100']:+.1f}")
    print(f"  elapsed     : {stats['elapsed_s']:.1f}s  "
          f"({stats['hands_per_s']:.0f} hands/s)")
    print(sep)
    print("")
    print("  Reminder: this is vs SIMPLE local bots, NOT DeepCFR.")
    print("  Confirm improvements with `pokerkit run --max-hands 50` on Arena.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
