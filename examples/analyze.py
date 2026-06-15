"""Failure analysis report for the Heuristic Learning loop.

Fetches your agent's recent settled hands from Arena, identifies losing
patterns by position and hand, and prints a formatted report you can
paste directly into Claude Code / Codex to guide decide() improvements.

Data sources (joined on tableId):
  - GET /texas/recent-tables?competitionId=&agentId=  → seats, hole cards, winners
  - GET /agent/{agentId}/replays                      → chipDelta per hand

Usage:
    pokerkit analyze                      # most recent competition
    pokerkit analyze --match <compId>     # specific competition
    pokerkit analyze --top 10             # show top N worst hands (default 10)
    pokerkit analyze --out report.txt     # save to file (default: stdout)

Heuristic Learning workflow:
    1. pokerkit run --max-hands 50         (get baseline bb/100)
    2. cp examples/STRATEGY.md.template STRATEGY.md   (describe your strategy)
    3. pokerkit analyze --out failure_report.txt       (find losing patterns)
    4. paste STRATEGY.md + failure_report.txt + HL prompt into Claude Code
    5. pokerkit test                        (no regressions)
    6. pokerkit run --max-hands 50          (compare bb/100 delta)
    7. repeat from step 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from arena_client import ArenaClient, ArenaError, DEFAULT_BASE, CREDS_PATH

# 6-max seat → approximate position label (heuristic, not authoritative)
_SEAT_POS = {1: "BTN", 2: "SB", 3: "BB", 4: "UTG", 5: "MP", 6: "CO"}


def _load_creds() -> tuple[Optional[str], Optional[str]]:
    """Load (api_key, agent_id) from .arena-credentials if present, else env."""
    if CREDS_PATH.exists():
        try:
            c = json.loads(CREDS_PATH.read_text())
            return c.get("apiKey"), c.get("agentId") or c.get("id")
        except Exception:
            pass
    return os.environ.get("ARENA_API_KEY"), os.environ.get("ARENA_AGENT_ID")


def _fetch_recent_tables(client: ArenaClient, agent_id: str,
                         competition_id: Optional[str], limit: int) -> list[dict]:
    """GET /texas/recent-tables — per-table seats, hole cards, board, winners."""
    parts = [f"limit={limit}", f"agentId={agent_id}"]
    if competition_id:
        parts.append(f"competitionId={competition_id}")
    qs = "?" + "&".join(parts)
    try:
        body = client.get(f"/texas/recent-tables{qs}")
    except ArenaError as e:
        print(f"[analyze] recent-tables fetch failed: {e}", file=sys.stderr)
        return []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    return []


def _fetch_replays(client: ArenaClient, agent_id: str, limit: int) -> dict[str, int]:
    """GET /agent/{id}/replays → {tableId: chipDelta}. Returns {} on failure.
    Server caps limit at 50."""
    capped = min(limit, 50)
    try:
        body = client.get(f"/agent/{agent_id}/replays?limit={capped}")
    except ArenaError as e:
        print(f"[analyze] replays fetch failed (will use payoutChips instead): {e}",
              file=sys.stderr)
        return {}
    rows = body if isinstance(body, list) else (
        body.get("data") if isinstance(body, dict) else [])
    out: dict[str, int] = {}
    for r in rows or []:
        tid = r.get("tableId") or r.get("handId")
        if tid is not None:
            out[tid] = int(r.get("chipDelta") or 0)
    return out


def _resolve_latest_competition(tables: list[dict]) -> Optional[str]:
    for t in tables:
        cid = t.get("competitionId")
        if cid:
            return cid
    return None


def analyze(tables: list[dict], chip_deltas: dict[str, int],
            self_agent_id: str, top_n: int = 10) -> str:
    """Build a plain-text failure report from recent-tables + replays."""
    if not tables:
        return (
            "No completed tables found.\n"
            "Run `pokerkit run --max-hands 50` first, then re-run\n"
            "`pokerkit analyze`.\n"
        )

    by_seat: dict[int, dict] = {}
    rows: list[dict] = []

    for t in tables:
        tid = t.get("id") or t.get("tableId")
        seats = t.get("seats") or []
        # Find OUR seat in this table.
        my_seat = next(
            (s for s in seats if s.get("agentId") == self_agent_id), None)
        if not my_seat:
            continue

        seat_num = my_seat.get("seatNumber") or 0
        hole = list(my_seat.get("holeCards") or [])
        payout = int(my_seat.get("payoutChips") or 0)
        stack_end = int(my_seat.get("stackChips") or 0)

        # Prefer chipDelta from /replays (precise); else infer from payout.
        delta = chip_deltas.get(tid)
        if delta is None:
            # Fall back to "did we get any payout this hand?". Coarse.
            delta = payout - 100  # rough proxy: assume 100 BB committed avg

        winner = (t.get("winners") or [{}])[0]
        winner_handle = winner.get("agentName") or winner.get("agentId") or "?"
        winner_hand = winner.get("handName") or ""
        board = " ".join(t.get("boardCards") or [])

        if seat_num:
            rec = by_seat.setdefault(seat_num,
                                     {"seat": seat_num, "total": 0, "delta_sum": 0})
            rec["total"] += 1
            rec["delta_sum"] += delta

        rows.append({
            "table_id": tid,
            "delta": delta,
            "seat": seat_num,
            "hole": hole,
            "board": board,
            "payout": payout,
            "stack_end": stack_end,
            "winner": winner_handle,
            "winner_hand": winner_hand,
        })

    if not rows:
        return ("No hands found where you were seated.\n"
                "Check that --match competitionId matches a comp you played.\n")

    rows.sort(key=lambda x: x["delta"])

    total = len(rows)
    wins = sum(1 for r in rows if r["delta"] > 0)
    losses = sum(1 for r in rows if r["delta"] < 0)
    pushes = total - wins - losses

    lines: list[str] = []
    sep = "=" * 62

    lines += [
        sep,
        "ARENA POKERKIT — FAILURE ANALYSIS REPORT",
        "(paste this into Claude Code alongside STRATEGY.md)",
        sep,
        f"Total hands       : {total}",
        f"Wins / Losses     : {wins} / {losses}"
        + (f" (push: {pushes})" if pushes else ""),
        "",
    ]

    # Position breakdown
    lines.append("POSITION BREAKDOWN  (worst → best avg chip delta):")
    seat_rows = sorted(
        by_seat.values(),
        key=lambda r: r["delta_sum"] / max(r["total"], 1),
    )
    for r in seat_rows:
        pos = _SEAT_POS.get(r["seat"], f"seat{r['seat']}")
        avg = r["delta_sum"] / max(r["total"], 1)
        bar = "▼" if avg < 0 else "▲"
        lines.append(
            f"  {bar} Seat {r['seat']} ({pos:3})  {avg:+.0f} chips avg"
            f"  ({r['total']} hands)"
        )
    lines.append("")

    # Worst N hands
    n = min(top_n, len(rows))
    lines.append(f"WORST {n} HANDS (by chip delta):")
    for i, h in enumerate(rows[:n], 1):
        pos = _SEAT_POS.get(h["seat"], f"s{h['seat']}")
        hole_str = " ".join(h["hole"]) if h["hole"] else "??"
        lines.append(
            f"  #{i:02d}  {hole_str:7s}  {pos:3}  "
            f"delta={h['delta']:+d}  "
            f"board=[{h['board']}]  won by {h['winner']}"
        )
    lines.append("")

    # Top 3 winning hands (for contrast)
    best = sorted(rows, key=lambda x: x["delta"], reverse=True)[:min(3, len(rows))]
    lines.append("BEST 3 HANDS (for contrast):")
    for i, h in enumerate(best, 1):
        pos = _SEAT_POS.get(h["seat"], f"s{h['seat']}")
        hole_str = " ".join(h["hole"]) if h["hole"] else "??"
        lines.append(
            f"  #{i}  {hole_str:7s}  {pos:3}  delta={h['delta']:+d}"
        )
    lines.append("")

    lines += [
        sep,
        "NEXT STEP — paste to Claude Code:",
        '  "Read STRATEGY.md and this report. Which patterns do you see',
        "   in the losing hands? Improve decide() in examples/agent.py.",
        '   Zero LLM calls at runtime. Bake ranges and rules into code."',
        sep,
    ]

    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a failure analysis report for the Heuristic Learning loop.\n"
            "Fetches recent tables + replays from Arena, ranks positions and hands\n"
            "by chip delta, outputs a paste-ready report for Claude Code / Codex."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--match", default=None,
        help="competitionId to analyse (default: most recent)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="Number of worst hands to show (default 10)",
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Max tables to fetch (default 100, server cap 100)",
    )
    parser.add_argument(
        "--out", default=None,
        help="Write report to file instead of stdout",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    api_key, agent_id = _load_creds()
    if not api_key:
        print(
            "ERROR: no API key found.\n"
            "Run `pokerkit run --max-hands 1` first to register,\n"
            "or set ARENA_API_KEY in .env.",
            file=sys.stderr,
        )
        return 2

    # Default to the configured competition if no --match given.
    competition_id = args.match or os.environ.get("ARENA_COMPETITION_ID")

    base = os.environ.get("ARENA_API_BASE", DEFAULT_BASE)
    client = ArenaClient(base, api_key=api_key)
    try:
        # Resolve agent_id from /agent/me if missing.
        if not agent_id:
            try:
                me = client.get("/agent/me")
                if isinstance(me, dict):
                    agent_id = me.get("id") or me.get("agentId")
            except ArenaError as e:
                print(f"[analyze] /agent/me failed: {e}", file=sys.stderr)
        if not agent_id:
            print("ERROR: could not resolve agentId.", file=sys.stderr)
            return 2

        tables = _fetch_recent_tables(client, agent_id, competition_id, args.limit)
        chip_deltas = _fetch_replays(client, agent_id, args.limit)
        report = analyze(tables, chip_deltas, agent_id, top_n=args.top)

        if args.out:
            Path(args.out).write_text(report)
            print(f"wrote → {args.out}")
        else:
            print(report, end="")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
