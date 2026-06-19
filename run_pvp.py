#!/usr/bin/env python
"""System 7 — PvP runner for the Playground (continuous cash-style play).

Joins the Playground, plays with the HYBRID agent (deterministic engine +
MiniMax M3 on hard spots) and the agent-stats HUD, and records advanced stats to
SQLite (s7_stats). Built to run unattended for a long time (systemd Restart):
defensive everywhere, handles lobby queueing, rebuys, 409/429/400, reconnects.
Stops + leaves at S7_TARGET_HANDS.

Env: ARENA_COMPETITION_ID (Playground id), OPENAI_API_KEY (MiniMax via .env),
     S7_TARGET_HANDS (default 25000), S7_LLM_MIN_DEADLINE (M3 gate, default 30).
"""
import os
import sys
import time
import json
import random
import fcntl

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(_HERE, ".env"))

import decide_system7 as H        # noqa: E402  cheap feature helpers
import hybrid_system7 as HY       # noqa: E402  routing decide (heur + M3)
import s7_reads                   # noqa: E402  HUD
import s7_stats                   # noqa: E402  recorder
from arena_client import ArenaClient, ArenaError, DEFAULT_BASE  # noqa: E402

COMP = os.environ.get("ARENA_COMPETITION_ID") or ""   # resolved at runtime via list-active
TARGET = int(os.environ.get("S7_TARGET_HANDS", "25000"))


def _key():
    # S7_CREDS_FILE lets Docker/volumes provide creds (the image excludes .arena-credentials).
    for p in (os.environ.get("S7_CREDS_FILE"),
              os.path.join(_HERE, ".arena-credentials"),
              "/data/.arena-credentials"):
        if p and os.path.exists(p):
            with open(p) as f:
                return json.load(f)["apiKey"]
    raise SystemExit("no creds: set S7_CREDS_FILE or provide .arena-credentials")


_AID_CACHE = [None]


def _agent_id():
    """agentId de las creds (para /agent/{id}/replays → reproductor oficial); cacheado."""
    if _AID_CACHE[0] is None:
        _AID_CACHE[0] = ""
        for p in (os.environ.get("S7_CREDS_FILE"), os.path.join(_HERE, ".arena-credentials"), "/data/.arena-credentials"):
            if p and os.path.exists(p):
                try:
                    _AID_CACHE[0] = json.load(open(p)).get("agentId") or ""
                except Exception:
                    pass
                break
    return _AID_CACHE[0]


def _log(*a):
    print("[s7-pvp]", *a, flush=True)


def _single_instance_lock():
    """Refuse to start a 2nd PvP loop (the 7-copies/429 thrash we saw). flock on
    the shared /data volume coordinates even across separate containers."""
    path = os.environ.get("S7_PVP_LOCK") or (
        "/data/.pvp.lock" if os.path.isdir("/data") else os.path.join(_HERE, ".pvp.lock"))
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log(f"another PvP instance holds {path}; exiting (single-instance).")
        raise SystemExit(0)
    f.write(str(os.getpid()))
    f.flush()
    return f


def _resolve_comp(c):
    """Discover the live competition the official way (GET /competition/list-active).
    Honors ARENA_COMPETITION_ID when it's currently active; else picks the active
    'Playground' (free cash). NEVER auto-selects a buy-in Tournament (paid)."""
    forced = os.environ.get("ARENA_COMPETITION_ID") or ""
    prefer = os.environ.get("S7_COMP_PREFER", "Playground").lower()
    try:
        r = c.get("/competition/list-active")
        comps = r if isinstance(r, list) else (r.get("competitions") or r.get("data") or [])
    except Exception as e:
        _log(f"list-active failed ({e}); using ARENA_COMPETITION_ID={forced or '(none)'}")
        return forced
    poker = [x for x in comps if str(x.get("gameType")) == "TexasHoldem"]
    by_id = {x.get("id"): x for x in poker}
    if forced and forced in by_id:
        _log(f"using forced competition {by_id[forced].get('name')} ({forced})")
        return forced
    nontourney = [x for x in poker if "tournament" not in str(x.get("name", "")).lower()]
    pick = next((x for x in nontourney if prefer in str(x.get("name", "")).lower()), None) \
        or (nontourney[0] if nontourney else None)
    if pick:
        _log(f"discovered competition {pick.get('name')} ({pick.get('id')})")
        return pick.get("id")
    _log(f"no free TexasHoldem competition active; live poker = {[x.get('name') for x in poker]}")
    return forced


def _deadline_s(table):
    dl = table.get("actionDeadlineAt")
    try:
        return max(0.0, float(dl) / 1000.0 - time.time()) if dl else 10.0
    except Exception:
        return 10.0


def _features(table, action, research):
    """Cheap per-decision feature snapshot for stats (no equity Monte-Carlo)."""
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
    n_live = sum(1 for s in seats if str(s.get("status") or "").lower()
                 not in ("folded", "out", "sittingout"))
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
        "run_label": os.environ.get("S7_RUN_LABEL"),
    }


def _self_stack(table):
    seat_n = table.get("selfSeatNumber")
    for s in (table.get("seats") or []):
        if s.get("seatNumber") == seat_n:
            return int(s.get("stackChips") or 0)
    return None


def main():
    _lock = _single_instance_lock()   # noqa: F841 — held for the process lifetime
    s7_stats.init()
    s7_stats.set_meta("started_at", time.time())
    s7_stats.set_meta("target_hands", TARGET)
    c = ArenaClient(os.environ.get("ARENA_API_BASE", DEFAULT_BASE), api_key=_key())

    global COMP
    COMP = _resolve_comp(c)
    if not COMP:
        _log("no active competition to join (set ARENA_COMPETITION_ID or wait). exiting.")
        return
    _log(f"strategy={os.environ.get('S7_STRAT') or 'std'} competition={COMP}")

    hands_done = 0
    rebuys = 0
    last_hole = {}          # tableId -> hole string (hand-change detector)
    last_seen_table = 0.0
    last_join = 0.0
    last_beat = 0.0
    last_stack = None
    dumped = False

    def ensure_joined():
        nonlocal last_join
        try:
            r = c.post("/texas/join", {"competitionId": COMP})
            last_join = time.time()
            k = r.get("kind") if isinstance(r, dict) else None
            lob = (r.get("lobby") or {}) if isinstance(r, dict) else {}
            _log(f"join kind={k} lobbyPos={lob.get('position')}/{lob.get('total')}")
            return r
        except ArenaError as e:
            if e.status == 402:
                _log(f"JOIN 402 entry fee (unexpected for Playground): {e.body} — stopping")
                raise SystemExit(3)
            if e.status == 403:
                try:
                    cs = c.get("/auth/claim/status")
                    _log("JOIN 403 — agent must be CLAIMED/verified via X. Open %s (token %s); "
                         "after verifying it will auto-join." % (cs.get("claimUrl"), cs.get("claimToken")))
                except Exception:
                    _log(f"JOIN 403 (claim/verify required): {e.body}")
            elif e.status == 409:
                pass  # already seated (1-table concurrency) — normal liveness check, not an error
            else:
                _log(f"join error {e.status}: {str(e.body)[:160]}")
            return None

    ensure_joined()

    while hands_done < TARGET:
        # Re-join if we've gone idle for a while (dropped / lobby churn).
        if time.time() - last_seen_table > 120 and time.time() - last_join > 90:
            ensure_joined()
            # opportunistic rebuy if available
            try:
                rs = c.get(f"/texas/rebuy-status?competitionId={COMP}")
                if isinstance(rs, dict) and (rs.get("canRebuy") or rs.get("needed") or rs.get("available")):
                    c.post("/texas/rebuy", {"competitionId": COMP})
                    rebuys += 1
                    _log(f"rebuy #{rebuys}")
            except ArenaError:
                pass

        try:
            pending = c.get(f"/texas/pending-actions?competitionId={COMP}")
        except ArenaError as e:
            if e.status in (401, 403):
                _log(f"auth error {e.status} on pending — stopping"); break
            if e.status == 429:
                time.sleep(2.0); continue
            time.sleep(2.0); continue

        tables = []
        if isinstance(pending, dict) and isinstance(pending.get("tables"), list):
            tables = [t for t in pending["tables"] if isinstance(t, dict) and t.get("tableId")]
        tables.sort(key=lambda t: t.get("actionDeadlineAt") or 0)

        if not dumped and tables:
            try:
                with open(os.path.join(_HERE, "first_table.json"), "w") as f:
                    json.dump(tables[0], f, indent=2, default=str)
                _log("dumped first_table.json")
            except Exception:
                pass
            dumped = True

        if not tables:
            time.sleep(2.0 + random.uniform(0, 0.6))
        else:
            last_seen_table = time.time()
            for table in tables:
                tid = table.get("tableId")
                # hand-change detection (hole cards change each new hand)
                seat_n = table.get("selfSeatNumber")
                me = next((s for s in (table.get("seats") or []) if s.get("seatNumber") == seat_n), {})
                hole_str = ",".join(me.get("holeCards") or [])
                if hole_str and last_hole.get(tid) != hole_str:
                    last_hole[tid] = hole_str
                    hands_done += 1
                last_stack = _self_stack(table) or last_stack

                dl = _deadline_s(table)
                try:
                    research = s7_reads.retrieve_solver_context(table)
                except Exception:
                    research = {}
                try:
                    action = HY.decide(table, deadline_s=dl, research_context=research)
                except Exception as e:
                    _log(f"decide error: {e}; folding")
                    action = {"action": "fold", "message": "decide error",
                              "reasoning": '{vr: "std", ke: "legal", pp: "pot control"}'}
                try:
                    s7_stats.log_decision(_features(table, action, research))
                except Exception:
                    pass

                payload = {"tableId": tid, "action": action.get("action"),
                           "message": action.get("message", ""), "reasoning": action.get("reasoning", "")}
                if action.get("amount") is not None and action.get("action") in ("bet", "raise", "all-in"):
                    payload["amount"] = int(action["amount"])
                try:
                    c.post("/texas/action", payload)
                except ArenaError as e:
                    if e.status == 409:
                        time.sleep(0.3); continue
                    if e.status == 429:
                        time.sleep(2.0); continue
                    if e.status == 400:
                        try:
                            c.post("/texas/action", {"tableId": tid, "action": "fold",
                                   "message": "fallback", "reasoning": '{vr: "std", ke: "legal", pp: "pot control"}'})
                        except ArenaError:
                            pass
                        continue
                    if e.status in (401, 403):
                        _log(f"auth error {e.status} on action — stopping"); return
                    _log(f"action error {e.status}: {str(e.body)[:120]}")

        # heartbeat + bankroll snapshot ~every 60s
        if time.time() - last_beat > 60:
            try:
                s7_stats.log_bankroll(last_stack, hands_done, rebuys)
                _rl = os.environ.get("S7_RUN_LABEL")
                if _rl and last_stack is not None:
                    _net = last_stack - 1000 * (1 + rebuys)         # neto vs bankroll inicial + rebuys
                    s7_stats.log_equity(_rl, hands_done, _net, _net)
                _aid = _agent_id()                                  # captura replay_url → reproductor oficial
                if _aid:
                    _rep = c.get(f"/agent/{_aid}/replays?limit=50")
                    _rows = _rep if isinstance(_rep, list) else ((_rep or {}).get("data") or [])
                    for _r in (_rows or []):
                        _tid = _r.get("tableId") or _r.get("handId")
                        _url = _r.get("replayUrl") or _r.get("url")
                        if _tid and _url:
                            _wh = _r.get("winnerHandle") or _r.get("winner")
                            s7_stats.log_hand_result(str(_tid), "", ([{"agentName": _wh}] if _wh else []),
                                                     [], 0, _r.get("chipDelta"), "", _url)
            except Exception:
                pass
            _log(f"hands={hands_done}/{TARGET} rebuys={rebuys} lastStack={last_stack}")
            last_beat = time.time()

    _log(f"target reached: {hands_done} hands. leaving.")
    s7_stats.set_meta("finished_at", time.time())
    s7_stats.set_meta("hands_done", hands_done)
    try:
        c.post("/texas/leave", {"competitionId": COMP})
    except ArenaError:
        pass
    c.close()


if __name__ == "__main__":
    main()
