#!/usr/bin/env python
"""System 7 — Eval test-bench (the API's own PVE test vs the DeepCFR panel).

The Eval is one-shot per agent, so each match registers a FRESH throwaway agent
(free, instant) and plays 500 hands vs the fixed reference panel — real, position-
aware, back-to-back (no lobby queue). Records every decision + the bb/100 result
to s7_test.db. Use it to accumulate hands vs real bots and A/B your decide().

    uv run s7_test.py                          # 1 full 500-hand match (hybrid M3)
    uv run s7_test.py --matches 10             # 10 back-to-back matches
    uv run s7_test.py --engine heur --max-hands 50   # quick heuristic preview
    S7_RUN_LABEL=v2 uv run s7_test.py          # tag this batch for A/B in the report

Report:  S7_STATS_DB=s7_test.db uv run s7_report.py
"""
import argparse
import json
import os
import random
import secrets
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("S7_STATS_DB", os.path.join(_HERE, "s7_test.db"))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_HERE, ".env"))

EVAL = "seed_poker_eval_s1"
os.environ["ARENA_COMPETITION_ID"] = EVAL          # scope HUD/agent-stats to the Eval

import decide_system7 as H        # noqa: E402
import s7_reads                   # noqa: E402
import s7_stats                   # noqa: E402
from arena_client import ArenaClient, ArenaError, DEFAULT_BASE  # noqa: E402

_TERMINAL = {"completed", "cancelled", "failed", "Completed", "Cancelled", "Failed"}
_CUM = {"hands": 0, "raw": 0.0, "adj": 0.0}   # running totals across this process's matches


def _decide_fn(engine):
    if engine == "heur":
        return H.decide
    import hybrid_system7 as HY
    return HY.decide


def _features(table, action, research, run_label):
    seat_n = table.get("selfSeatNumber")
    seats = table.get("seats") or []
    me = next((s for s in seats if s.get("seatNumber") == seat_n), {})
    hole = list(me.get("holeCards") or [])
    board = list(table.get("boardCards") or [])
    texture = H._texture(board) if board else "preflop"
    try:
        pos, ip, _ = H._position(table)
    except Exception:
        pos, ip = "?", False
    strength = H._strength(hole, board, texture) if board else ""
    adj = H._adjusted_outs(hole, board, texture) if board else 0
    try:
        reads = H._villain_reads(table, research)
    except Exception:
        reads = {}
    pot = int(table.get("potChips") or 0)
    call = int((table.get("allowedActions") or {}).get("callChips") or 0)
    n_live = sum(1 for s in seats if str(s.get("status") or "").lower() not in ("folded", "out", "sittingout"))
    street = "preflop" if not board else ("flop", "turn", "river")[max(0, min(len(board) - 3, 2))]
    act = action.get("action")
    return {
        "ts": time.time(), "table_id": table.get("tableId"),
        "hand_key": f"{table.get('tableId')}:{table.get('handId') or table.get('handNumber') or ''}",
        "street": street, "pos": pos, "ip": int(bool(ip)), "hole": ",".join(hole),
        "hand_class": H._hand_class(hole), "board": ",".join(board), "texture": texture,
        "strength": strength, "spr": round(H._spr(table), 2), "pot": pot, "call_chips": call,
        "pot_odds": round(call / max(pot + call, 1), 3) if call else 0.0, "adj_outs": adj,
        "n_villains": max(1, n_live - 1), "archetype": reads.get("archetype", "UNKNOWN"),
        "engine": "M3" if str(action.get("message", "")).startswith("[M3]") else "heur",
        "action": act, "amount": action.get("amount"),
        "voluntary": int(street == "preflop" and act in ("call", "bet", "raise")),
        "preflop_raise": int(street == "preflop" and act in ("bet", "raise")),
        "run_label": run_label,
        "m3_log": json.dumps(action["m3"], default=str) if action.get("m3") else None,
        "model": ((action.get("m3") or {}).get("model")
                  or ("M3" if str(action.get("message", "")).startswith("[M3]")
                      else os.environ.get("S7_HEUR_NAME", "heur"))),
    }


def _our_invested(events, seat):
    """Chips WE put into the pot across the hand (from the event stream) = our contribution."""
    if seat is None:
        return 0
    total, bet, cmax = 0, {}, 0
    for e in sorted(events or [], key=lambda x: x.get("sequence") or 0):
        t = e.get("type")
        s = e.get("summary") or {}
        sn = s.get("seatNumber")
        if t == "StreetDealt":
            bet, cmax = {}, 0
            continue
        a = None
        if t == "BlindPosted":
            a = s.get("amount") or 0
        elif t == "ActionTaken":
            act = s.get("action")
            if act in ("bet", "raise", "all-in"):
                a = s.get("toAmount") if s.get("toAmount") is not None else s.get("amount")
            elif act == "call":
                a = cmax
        if a is None:
            continue
        d = max(0, a - bet.get(sn, 0))
        bet[sn] = a
        cmax = max(cmax, a)
        if sn == seat:
            total += d
    return total


def _events_for(tid):
    import sqlite3 as _sq
    try:
        return _sq.connect(s7_stats.DB, timeout=10).execute(
            "select events, seat from hand_events where hand_key like ? limit 1", (str(tid) + ":%",)).fetchone()
    except Exception:
        return None


def _capture_results(c, agent_id, players):
    """Poll settled-hand results (recent-tables + replays) + refresh rival HUDs.
    recent-tables gives winners + revealed holeCards + payouts per settled hand;
    replays gives the precise chipDelta. Upsert into hand_results (dedupe by tableId)."""
    try:
        rt = c.get(f"/texas/recent-tables?limit=100&agentId={agent_id}&competitionId={EVAL}")
    except Exception:
        rt = None
    urls = {}                                    # tableId -> official replay url (full faithful timeline)
    try:
        rep = c.get(f"/agent/{agent_id}/replays?limit=50")
        rows = rep if isinstance(rep, list) else ((rep or {}).get("data") if isinstance(rep, dict) else [])
        for r in rows or []:
            tid = r.get("tableId") or r.get("handId")
            url = r.get("replayUrl") or r.get("url") or r.get("replay")
            if tid is not None and url:
                urls[str(tid)] = url
    except Exception:
        pass
    data = (rt or {}).get("data") if isinstance(rt, dict) else (rt if isinstance(rt, list) else [])
    for t in (data or []):
        try:
            tid = t.get("id") or t.get("tableId")
            if not tid or not t.get("winners"):
                continue
            seats = t.get("seats") or []
            mine = next((s for s in seats if s.get("agentId") == agent_id), {})
            shown = [{"seat": s.get("seatNumber"), "name": s.get("agentName") or s.get("agentId"),
                      "hole": s.get("holeCards"), "payout": s.get("payoutChips"),
                      "committed": s.get("totalCommittedChips"), "hand": s.get("handName")}
                     for s in seats if s.get("holeCards")]
            payout = int(mine.get("payoutChips") or 0)
            inv = 0
            try:
                row = _events_for(tid)
                if row and row[0]:
                    inv = _our_invested(json.loads(row[0]), row[1])
            except Exception:
                inv = 0
            cd = payout - inv      # neto: lo que ganamos sin contar lo que ya pusimos
            s7_stats.log_hand_result(str(tid), " ".join(t.get("boardCards") or []),
                                     t.get("winners"), shown, payout, cd, mine.get("handName") or "",
                                     urls.get(str(tid), ""))
        except Exception:
            continue
    for aid, nm in list(players.items()):
        try:
            stx = s7_reads._fetch(aid)
            if stx:
                s7_stats.log_agent_stats(aid, nm, stx)
        except Exception:
            continue


def run_match(c, decide, engine, max_hands, label):
    h = f"s7t-{secrets.token_hex(4)}"
    body = c.post("/auth/register", {"handle": h, "name": os.environ.get("S7_AGENT_NAME") or "S7 test",
                                      "quote": "bench", "description": ""})
    c.api_key = body.get("apiKey")
    agent_id = body.get("agentId") or body.get("id")
    if os.environ.get("S7_SAVE_CREDS"):           # clasificatoria: persist creds so the agent can be claimed later
        try:
            _cd = os.environ.get("S7_CLASIF_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clasif")
            os.makedirs(_cd, exist_ok=True)
            with open(os.path.join(_cd, label + ".json"), "w", encoding="utf-8") as _f:
                json.dump({"handle": h, "name": os.environ.get("S7_AGENT_NAME") or "S7 test",
                           "agentId": agent_id, "apiKey": c.api_key, "competition": EVAL,
                           "strat": os.environ.get("S7_STRAT") or "std", "engine": engine,
                           "ts": time.time()}, _f)
        except Exception:
            pass
    print(f"[s7-test] agent {h} ({agent_id})", flush=True)
    start = c.post("/texas/benchmark/start", {"competitionId": EVAL})
    m = (start or {}).get("match") or {}
    if m.get("phase") in _TERMINAL or m.get("status") in _TERMINAL:
        print("[s7-test] match terminal at start:", m, flush=True)
        return None
    m3 = 0
    completed = 0
    bb = None
    last_status = 0.0
    last_eq = 0
    last_rt = 0.0
    players = {}
    _t0 = time.time()
    _timeout = float(os.environ.get("S7_MATCH_TIMEOUT", "4500"))   # wall-clock cap (75 min)
    _poll = max(0.3, float(os.environ.get("S7_POLL_INTERVAL", "1.0")))   # intervalo de sondeo en vacío (s); más bajo = reacciona antes a tu turno
    while True:
        if time.time() - _t0 > _timeout:
            print(f"[s7-test] match TIMEOUT {int(time.time()-_t0)}s hands={completed}", flush=True)
            s7_stats.log_run(label, agent_id, engine, completed, bb, None, None, m3, note="timeout")
            return bb
        try:
            pend = c.get(f"/texas/pending-actions?competitionId={EVAL}")
        except ArenaError as e:
            if e.status != 429:
                print(f"[s7-test] poll error {e.status}", flush=True)
            time.sleep(2 if e.status == 429 else 1)
            pend = None
        tables = [t for t in ((pend or {}).get("tables") or []) if isinstance(t, dict) and t.get("tableId")]
        tables.sort(key=lambda t: t.get("actionDeadlineAt") or 0)
        for table in tables:
            dl = max(0.0, (table.get("actionDeadlineAt") or 0) / 1000 - time.time()) or 10.0
            try:
                research = s7_reads.retrieve_solver_context(table)
            except Exception:
                research = {}
            action = decide(table, deadline_s=dl, research_context=research)
            if str(action.get("message", "")).startswith("[M3]"):
                m3 += 1
            try:
                feat = _features(table, action, research, label)
                s7_stats.log_decision(feat)
                seats_snap = [{"seat": s.get("seatNumber"),
                               "chips": s.get("chips") if s.get("chips") is not None
                               else (s.get("stackChips") if s.get("stackChips") is not None else s.get("stack")),
                               "name": s.get("agentName") or s.get("name"), "status": s.get("status")}
                              for s in (table.get("seats") or [])]
                s7_stats.log_hand_events(feat["hand_key"], table.get("selfSeatNumber"),
                                         feat["hole"], feat["board"], table.get("recentEvents"), seats_snap)
                for s in (table.get("seats") or []):
                    aid = s.get("agentId") or s.get("agentHandle")
                    if aid and s.get("seatNumber") != table.get("selfSeatNumber"):
                        players[str(aid)] = s.get("agentName") or s.get("name") or str(aid)
            except Exception:
                pass
            payload = {"tableId": table["tableId"], "action": action.get("action"),
                       "message": action.get("message", ""), "reasoning": action.get("reasoning", "")}
            if action.get("amount") is not None and action.get("action") in ("bet", "raise", "all-in"):
                payload["amount"] = int(action["amount"])
            try:
                c.post("/texas/action", payload)
            except ArenaError as e:
                if e.status == 409:
                    time.sleep(0.3)
                    continue
                if e.status == 429:
                    time.sleep(2)
                    continue
                if e.status == 400:
                    try:
                        c.post("/texas/action", {"tableId": table["tableId"], "action": "fold",
                               "message": "fallback", "reasoning": '{vr: "std", ke: "legal", pp: "pot control"}'})
                    except ArenaError:
                        pass
                    continue
        now = time.time()
        if now - last_rt >= 28:                       # settled-hand results + rival HUDs
            last_rt = now
            _capture_results(c, agent_id, players)
        if (not tables) or (now - last_status >= 8):
            try:
                st = c.get(f"/texas/benchmark/status?competitionId={EVAL}")
            except ArenaError:
                st = None
            last_status = now
            mm = (st or {}).get("match") or {}
            completed = int(mm.get("completedHands") or completed)
            bb = mm.get("adjustedBbPer100")
            raw_cd = mm.get("rawChipDelta") or 0
            adj_cd = mm.get("adjustedChipDelta") or 0
            if completed > last_eq:                      # cumulative real vs EV curve
                try:
                    s7_stats.log_equity(label, _CUM["hands"] + completed,
                                        _CUM["raw"] + raw_cd, _CUM["adj"] + adj_cd)
                except Exception:
                    pass
                last_eq = completed
            terminal = mm.get("phase") in _TERMINAL or mm.get("status") in _TERMINAL
            if terminal or (max_hands and completed >= max_hands):
                print(f"[s7-test] {'DONE' if terminal else 'max-hands'} hands={completed} "
                      f"adjBb/100={bb} m3={m3}", flush=True)
                s7_stats.log_run(label, agent_id, engine, completed, bb,
                                 mm.get("rawBbPer100"), mm.get("rawChipDelta"), m3,
                                 note="" if terminal else "partial")
                _CUM["hands"] += completed
                _CUM["raw"] += raw_cd
                _CUM["adj"] += adj_cd
                return bb
        if not tables:
            time.sleep(_poll + random.uniform(0, 0.3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=("hybrid", "heur"), default="hybrid")
    ap.add_argument("--matches", type=int, default=1)
    ap.add_argument("--max-hands", type=int, default=0)
    a = ap.parse_args()
    s7_stats.init()
    decide = _decide_fn(a.engine)
    label = os.environ.get("S7_RUN_LABEL", f"{a.engine}-{time.strftime('%m%d-%H%M')}")
    try:  # continue the equity curve across restarts (avoid the sawtooth reset)
        import sqlite3 as _sq
        _r = _sq.connect(s7_stats.DB).execute(
            "select hands,raw_chips,adj_chips from equity where run_label=? order by ts desc limit 1",
            (label,)).fetchone()
        if _r:
            _CUM["hands"], _CUM["raw"], _CUM["adj"] = int(_r[0] or 0), float(_r[1] or 0), float(_r[2] or 0)
            print(f"[s7-test] equity seeded: hands={_CUM['hands']} raw={_CUM['raw']}", flush=True)
    except Exception:
        pass
    c = ArenaClient(os.environ.get("ARENA_API_BASE", DEFAULT_BASE))
    results = []
    for i in range(a.matches):
        print(f"=== match {i+1}/{a.matches} (engine={a.engine}, label={label}) ===", flush=True)
        try:
            results.append(run_match(c, decide, a.engine, a.max_hands, label))
        except Exception as e:
            import traceback
            print("[s7-test] match ERROR:", e, "\n" + traceback.format_exc(), flush=True)
    c.close()
    ok = [r for r in results if isinstance(r, (int, float))]
    if ok:
        print(f"[s7-test] {len(ok)} matches, bb/100 medio {sum(ok)/len(ok):+.1f}", flush=True)


if __name__ == "__main__":
    main()
