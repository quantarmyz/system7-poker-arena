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
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

COMP = os.environ.get("ARENA_COMPETITION_ID") or ""   # resolved at runtime via list-active
TARGET = int(os.environ.get("S7_TARGET_HANDS", "25000"))


def _key():
    # S7_CREDS_FILE lets Docker/volumes provide creds (the image excludes .arena-credentials).
    for p in (os.environ.get("S7_CREDS_FILE"),
              os.path.join(_HERE, ".arena-credentials"),
              "/data/.arena-credentials"):
        if p and os.path.exists(p):
            try:
                with open(p) as f:
                    k = (json.load(f) or {}).get("apiKey")
                if k:
                    return k
            except Exception:
                pass
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
        "m3_log": json.dumps(action["m3"], default=str) if action.get("m3") else None,
        "model": (action.get("m3") or {}).get("model"),
        "agent_id": _agent_id(),
        "competition_id": os.environ.get("ARENA_COMPETITION_ID", ""),
    }


def _self_stack(table):
    seat_n = table.get("selfSeatNumber")
    for s in (table.get("seats") or []):
        if s.get("seatNumber") == seat_n:
            return int(s.get("stackChips") or 0)
    return None


_RFB = '{vr: "std", ke: "legal", pp: "pot control"}'   # reasoning fallback (YAML-ish)


def _decision_key(table):
    """Stable signature of the spot we're asked to act on (dedupes an in-flight M3 across polls)."""
    seat_n = table.get("selfSeatNumber")
    me = next((s for s in (table.get("seats") or []) if s.get("seatNumber") == seat_n), {})
    allowed = table.get("allowedActions") or {}
    return "|".join(str(x) for x in (
        table.get("tableId"), table.get("handId") or table.get("handNumber") or "",
        ",".join(me.get("holeCards") or []), ",".join(table.get("boardCards") or []),
        allowed.get("callChips"), table.get("potChips")))


def _decide_worker(table, budget, research):
    """Runs in a pool thread: the (possibly slow) hybrid decide. M3 log is thread-local => PvP-safe."""
    try:
        return HY.decide(table, deadline_s=budget, research_context=research)
    except Exception as e:                              # never let a worker kill the action
        return {"action": "fold", "message": ("decide error: " + str(e))[:200], "reasoning": _RFB}


def _capture_opp(table):
    """Archive the hand's action timeline (incl. opponents) + any shown opponent cards (PvP HUD)."""
    try:
        tid = table.get("tableId")
        seat_n = table.get("selfSeatNumber")
        me = next((s for s in (table.get("seats") or []) if s.get("seatNumber") == seat_n), {})
        board = ",".join(table.get("boardCards") or [])
        hkey = "%s:%s" % (tid, table.get("handId") or table.get("handNumber") or "")
        s7_stats.log_hand_events(hkey, seat_n, ",".join(me.get("holeCards") or []), board,
                                 table.get("recentEvents") or [], seats=table.get("seats"))
        for s in (table.get("seats") or []):
            if s.get("seatNumber") != seat_n and s.get("holeCards"):
                s7_stats.log_opp_hand(str(tid), s.get("agentId") or s.get("agentName") or "?",
                                      s.get("agentName") or s.get("agentHandle") or "",
                                      ",".join(s.get("holeCards")), board, s.get("handName") or "",
                                      s.get("payoutChips"), 1 if (s.get("payoutChips") or 0) > 0 else 0, COMP)
    except Exception:
        pass


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
    try:
        s7_stats.set_meta("active_comp", COMP)   # el tracker sidecar sigue la comp por meta (rollover-safe)
    except Exception:
        pass

    hands_done = 0
    rebuys = 0
    try:                    # anclar al rebuyCount REAL del servidor → sobrevive re-deploys y rebuys manuales (la curva de equity descuenta TODOS los buy-ins, no solo los de este run)
        _rs0 = c.get(f"/texas/rebuy-status?competitionId={COMP}")
        if isinstance(_rs0, dict) and _rs0.get("rebuyCount") is not None:
            rebuys = int(_rs0["rebuyCount"])
            _log(f"rebuys ancladas al servidor: {rebuys}")
    except Exception:
        pass
    last_hole = {}          # tableId -> hole string (hand-change detector)
    last_seen_table = 0.0
    last_join = 0.0
    last_beat = 0.0
    last_stack = None
    dumped = False
    last_eq_pt = [None, None]   # [hands, stack] del último punto de equity → en idle no se loguea
    ROLL_IDLE = int(os.environ.get("S7_ROLLOVER_IDLE_S", "600"))
    t_start = time.time()
    last_roll = time.time()

    ASYNC = os.environ.get("S7_PVP_ASYNC", "") not in ("", "0", "false", "False", "no")
    MARGIN = float(os.environ.get("S7_PVP_SUBMIT_MARGIN", "3.0"))
    pool = ThreadPoolExecutor(max_workers=int(os.environ.get("S7_PVP_WORKERS", "4"))) if ASYNC else None
    inflight = {}          # tableId -> {"key","future","deadline","research"}; in-flight M3 decisions
    m3_disp = m3_hit = m3_to = 0
    dls = []               # sample of deadline-at-decision (s), for tuning the gate/budget
    if ASYNC:
        _log("PvP async M3 ON workers=%s margin=%ss gate=%ss timeout=%ss maxtok=%s" % (
            os.environ.get("S7_PVP_WORKERS", "4"), MARGIN, os.environ.get("S7_LLM_MIN_DEADLINE", "30"),
            os.environ.get("S7_LLM_TIMEOUT", "90"), os.environ.get("S7_MAX_TOKENS", "3000")))

    allin_ctx = {}              # tid -> (hole, board, aporte) cuando hero queda all-in → para la EV ajustada
    ev_state = {"adj": 0.0}     # ajuste acumulado (EV − real) de los all-in a showdown
    _meta_flag = {}

    def _submit_action(table, action, research):
        """Log the decision + POST /texas/action. Returns False on auth error (caller should stop)."""
        tid = table.get("tableId")
        try:
            s7_stats.log_decision(_features(table, action, research))
        except Exception:
            pass
        _capture_opp(table)                 # archivo de manos/acciones de rivales (HUD)
        try:                                # EV all-in: si hero compromete su stack, guarda hole/board/aporte para ajustar la varianza
            _se = table.get("selfSeatNumber")
            _me = next((s for s in (table.get("seats") or []) if s.get("seatNumber") == _se), {})
            _stk = int(_me.get("stackChips") or 0)
            _cm = int(_me.get("totalCommittedChips") or 0)
            _cb = int(_me.get("currentBetChips") or 0)
            _cc = int((table.get("allowedActions") or {}).get("callChips") or 0)
            _ac = action.get("action"); _am = int(action.get("amount") or 0)
            if _stk > 0 and ((_ac == "all-in") or (_ac == "call" and _cc >= _stk)
                             or (_ac in ("bet", "raise") and _am - _cb >= _stk)):
                allin_ctx[tid] = (list(_me.get("holeCards") or []), list(table.get("boardCards") or []), _cm + _stk)
            if not _meta_flag.get("bb"):    # captura la ciega grande una vez (para bb/100 en vivo)
                _bb = table.get("bigBlindChips") or table.get("bigBlind") or (table.get("blinds") or {}).get("big")
                if _bb:
                    s7_stats.set_meta("big_blind", int(_bb)); _meta_flag["bb"] = True
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
                time.sleep(0.3)
            elif e.status == 429:
                time.sleep(2.0)
            elif e.status == 400:
                try:
                    c.post("/texas/action", {"tableId": tid, "action": "fold",
                           "message": "fallback", "reasoning": _RFB})
                except ArenaError:
                    pass
            elif e.status in (401, 403):
                _log("auth error %s on action — stopping" % e.status)
                return False
            else:
                _log("action error %s: %s" % (e.status, str(e.body)[:120]))
        return True

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
                _log(f"JOIN 402 x402 (registra la entrada del torneo en dev.fun; el sponsor-ticket está fondeado): {str(e.body)[:200]} — back-off 30s, auto-une al registrarse")
                time.sleep(30)   # back-off: la entrada x402 (sponsored) se confirma en la web dev.fun; NO morir ni spamear → se auto-une cuando esté registrada
                return None
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
                if isinstance(rs, dict) and rs.get("canRebuyNow"):   # campo REAL de la API (antes miraba canRebuy/needed/available → nunca rebuy → se quedaba busteado)
                    c.post("/texas/rebuy", {"competitionId": COMP})
                    rebuys += 1
                    _log(f"rebuy #{rebuys}")
            except ArenaError:
                pass

        # Watchdog de rollover: mucho tiempo sin ver mesa = la temporada pudo morir → re-descubrir
        # la competición activa (_resolve_comp ya ignora la forzada si no está viva) y re-anclar.
        if time.time() - max(last_seen_table, t_start) > ROLL_IDLE and time.time() - last_roll > 300:
            last_roll = time.time()
            try:
                _nc = _resolve_comp(c)
            except Exception:
                _nc = None
            if _nc and _nc != COMP:
                _log(f"season rollover: {COMP} -> {_nc}; re-anclando estado")
                COMP = _nc
                os.environ["ARENA_COMPETITION_ID"] = _nc     # _features etiqueta decisions por env
                try:
                    s7_stats.set_meta("active_comp", _nc)    # realinea el tracker sidecar
                except Exception:
                    pass
                rebuys = 0
                try:                                          # re-anclar al rebuyCount del servidor en la comp nueva
                    _rs = c.get(f"/texas/rebuy-status?competitionId={COMP}")
                    if isinstance(_rs, dict) and _rs.get("rebuyCount") is not None:
                        rebuys = int(_rs["rebuyCount"])
                except Exception:
                    pass
                hands_done = 0
                last_hole.clear()
                last_stack = None
                ev_state["adj"] = 0.0
                allin_ctx.clear()
                last_eq_pt[:] = [None, None]
                _meta_flag.pop("bb", None)                   # recapturar big_blind de la season nueva
                ensure_joined()

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

        if not tables and not inflight:
            time.sleep(2.0 + random.uniform(0, 0.6))
        else:
            if tables:
                last_seen_table = time.time()
            if not ASYNC:
                for table in tables:
                    tid = table.get("tableId")
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
                        action = {"action": "fold", "message": "decide error", "reasoning": _RFB}
                    if not _submit_action(table, action, research):
                        return
            else:
                pend = {t.get("tableId"): t for t in tables}
                submitted = set()                           # tids acted on this poll (avoid re-dispatch)
                for tid in list(inflight.keys()):           # (1) resolve in-flight M3 decisions
                    info = inflight[tid]
                    table = pend.get(tid)
                    if table is None or _decision_key(table) != info["key"]:
                        inflight.pop(tid, None)             # spot gone / changed -> drop stale future
                        continue
                    if info["future"].done():
                        try:
                            action = info["future"].result()
                        except Exception as e:
                            action = {"action": "fold", "message": ("worker " + str(e))[:120], "reasoning": _RFB}
                        if action.get("m3"):
                            m3_hit += 1
                        ok = _submit_action(table, action, info["research"])
                        inflight.pop(tid, None)
                        submitted.add(tid)
                        if not ok:
                            return
                    elif info["deadline"] - time.time() <= MARGIN:
                        m3_to += 1                          # out of time -> instant heuristic
                        left = max(info["deadline"] - time.time(), 0.0)
                        action = H.decide(table, deadline_s=left, research_context=info["research"])
                        ok = _submit_action(table, action, info["research"])
                        inflight.pop(tid, None)
                        submitted.add(tid)
                        if not ok:
                            return
                for table in tables:                        # (2) dispatch / handle new decisions
                    tid = table.get("tableId")
                    seat_n = table.get("selfSeatNumber")
                    me = next((s for s in (table.get("seats") or []) if s.get("seatNumber") == seat_n), {})
                    hole_str = ",".join(me.get("holeCards") or [])
                    if hole_str and last_hole.get(tid) != hole_str:
                        last_hole[tid] = hole_str
                        hands_done += 1
                    last_stack = _self_stack(table) or last_stack
                    if tid in inflight or tid in submitted:
                        continue
                    dl = _deadline_s(table)
                    if len(dls) < 300:
                        dls.append(round(dl, 1))
                    try:
                        research = s7_reads.retrieve_solver_context(table)
                    except Exception:
                        research = {}
                    if pool is not None and HY._is_hard(table, dl - MARGIN):
                        m3_disp += 1                        # hard spot + budget -> background M3
                        fut = pool.submit(_decide_worker, table, dl - MARGIN, research)
                        inflight[tid] = {"key": _decision_key(table), "future": fut,
                                         "deadline": time.time() + dl, "research": research}
                    else:
                        try:
                            action = H.decide(table, deadline_s=dl, research_context=research)
                        except Exception as e:
                            _log(f"decide error: {e}; folding")
                            action = {"action": "fold", "message": "decide error", "reasoning": _RFB}
                        if not _submit_action(table, action, research):
                            return
                time.sleep(1.0)

        # heartbeat + bankroll snapshot ~every 60s
        if time.time() - last_beat > 60:
            try:
                s7_stats.log_bankroll(last_stack, hands_done, rebuys)
                _rl = os.environ.get("S7_RUN_LABEL")
                _aid = _agent_id()                                  # captura replay_url + chipDelta por mano
                if _aid:
                    _rep = c.get(f"/agent/{_aid}/replays?limit=50")
                    _rows = _rep if isinstance(_rep, list) else ((_rep or {}).get("data") or [])
                    for _r in (_rows or []):
                        _tid = _r.get("tableId") or _r.get("handId")
                        _url = _r.get("replayUrl") or _r.get("url")
                        _cd = _r.get("chipDelta")
                        if _tid and _tid in allin_ctx and _cd is not None:   # EV all-in: suaviza la varianza del flip
                            _hl, _bd, _C = allin_ctx.pop(_tid)
                            if _C > 0 and abs(_cd) >= 0.5 * _C:              # la villana PAGÓ el all-in (showdown), no fold
                                try:                                        # EV = aporte×(2·equity−1); ajuste = EV − real
                                    _eq = H._equity(_hl, _bd, 1, deadline_s=4.0)
                                    ev_state["adj"] += _C * (2.0 * _eq - 1.0) - _cd
                                except Exception:
                                    pass
                        if _tid and _url:
                            _wh = _r.get("winnerHandle") or _r.get("winner")
                            s7_stats.log_hand_result(str(_tid), "", ([{"agentName": _wh}] if _wh else []),
                                                     [], 0, _cd, "", _url)
                    if len(allin_ctx) > 300:                                 # poda manos que nunca casaron un replay
                        for _k in list(allin_ctx)[:-150]:
                            allin_ctx.pop(_k, None)
                if _rl and last_stack is not None and [hands_done, last_stack] != last_eq_pt:
                    _net = last_stack - 1000                        # neto POR ENTRADA (relativo al buy-in actual; cada re-entry parte de ~0)
                    s7_stats.log_equity(_rl, hands_done, _net, _net + int(round(ev_state["adj"])), reentry=rebuys,
                                        competition_id=COMP)        # una serie por re-entry, acotada a su temporada
                    last_eq_pt[:] = [hands_done, last_stack]        # idle (sin manos ni cambio de stack) → no ensuciar la curva
            except Exception:
                pass
            if ASYNC:
                _sd = sorted(dls)
                _pp = lambda q: (_sd[min(len(_sd) - 1, int(q * len(_sd)))] if _sd else 0)
                _log("hands=%s/%s rebuys=%s lastStack=%s | dl p50=%s p95=%s n=%s | M3 disp=%s hit=%s timeout=%s inflight=%s"
                     % (hands_done, TARGET, rebuys, last_stack, _pp(0.5), _pp(0.95), len(_sd),
                        m3_disp, m3_hit, m3_to, len(inflight)))
            else:
                _log(f"hands={hands_done}/{TARGET} rebuys={rebuys} lastStack={last_stack}")
            last_beat = time.time()

    _log(f"target reached: {hands_done} hands. leaving.")
    s7_stats.set_meta("finished_at", time.time())
    s7_stats.set_meta("hands_done", hands_done)
    try:
        c.post("/texas/leave", {"competitionId": COMP})
    except ArenaError:
        pass
    if pool is not None:
        pool.shutdown(wait=False)
    c.close()


if __name__ == "__main__":
    main()
