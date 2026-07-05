#!/usr/bin/env python
"""System 7 — motor tipo PokerTracker / Holdem Manager.

Cosecha, para TODOS nuestros agentes (desechables de Eval + producción), las manos y
estadísticas de la API del Arena y las guarda/agrega en s7_stats
(own_hands, opp_hands, opp_profiles, hand_results).

La API es por-agentId (no hay feed global): como PT/HM, solo vemos las manos en las que
jugamos. Fuentes por agente:
  GET /agent/{id}/replays           → chipDelta + replayUrl por mano
  GET /agent/submissions?agentId=   → NUESTRAS cartas/reasoning/payout/score por mano
  GET /texas/recent-tables?agentId= → cartas mostradas de rivales + winners + board
  GET /texas/agent-stats?agentId=   → HUD por rival (perfil agregado, alimenta el node-locking)

    uv run s7_tracker.py --once             # una pasada
    uv run s7_tracker.py --interval 600      # bucle cada 10 min (lo lanza Producción/los jobs)
"""
import argparse
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))
except Exception:
    pass

import s7_stats                                                   # noqa: E402
from arena_client import ArenaClient, ArenaError, DEFAULT_BASE    # noqa: E402

DATA = os.path.dirname(s7_stats.DB) or _HERE
CLASIF_DIR = os.environ.get("S7_CLASIF_DIR", os.path.join(_HERE, ".clasif"))


def _log(*a):
    print("[s7-tracker]", *a, flush=True)


def _active_comp():
    """Competición vigente: preferir meta.active_comp (run_pvp la actualiza en cada rollover)
    sobre el env ARENA_COMPETITION_ID, que queda congelado en el valor del deploy."""
    try:
        r = sqlite3.connect(s7_stats.DB, timeout=10).execute(
            "select v from meta where k='active_comp'").fetchone()
        v = json.loads(r[0]) if (r and r[0]) else ""
        if v:
            return v
    except Exception:
        pass
    return os.environ.get("ARENA_COMPETITION_ID") or ""


def _our_agents():
    """Enumera {agentId, apiKey, label} de cada agente que controlamos (creds en disco)."""
    out, seen, cand = [], set(), []
    for p in (os.environ.get("S7_CREDS_FILE"),
              os.path.join(DATA, ".arena-pg-credentials"),
              os.path.join(_HERE, ".arena-credentials")):
        if p and os.path.exists(p):
            cand.append(p)
    for d, pref in ((DATA, ".arena-pg-creds-"), (CLASIF_DIR, "")):
        try:
            for fn in os.listdir(d):
                if fn.endswith(".json") and fn.startswith(pref):
                    cand.append(os.path.join(d, fn))
        except Exception:
            pass
    for p in cand:
        try:
            cr = json.load(open(p))
        except Exception:
            continue
        aid, key = cr.get("agentId") or cr.get("id"), cr.get("apiKey")
        if aid and key and aid not in seen:
            seen.add(aid)
            out.append({"agentId": aid, "apiKey": key,
                        "label": cr.get("name") or os.path.basename(p)[:-5]})
    return out


def _replays_map(c, agent_id):
    """tableId -> {chip_delta, replay_url}."""
    out = {}
    try:
        rep = c.get(f"/agent/{agent_id}/replays?limit=50")
    except ArenaError:
        return out
    rows = rep if isinstance(rep, list) else ((rep or {}).get("data") if isinstance(rep, dict) else [])
    for r in rows or []:
        tid = r.get("tableId") or r.get("handId")
        if tid is not None:
            out[str(tid)] = {"chip_delta": r.get("chipDelta"),
                             "replay_url": r.get("replayUrl") or r.get("url") or ""}
    return out


def _harvest_own(c, agent_id, rmap):
    """/agent/submissions → own_hands (nuestras cartas/reasoning/payout/score)."""
    n = 0
    try:
        body = c.get(f"/agent/submissions?agentId={agent_id}&limit=50")
    except ArenaError:
        return 0
    rows = body.get("data") if isinstance(body, dict) else (body if isinstance(body, list) else [])
    for s in rows or []:
        data = s.get("data") or {}
        tid = s.get("tableId") or data.get("tableId") or s.get("id")
        chal = s.get("challenge") or {}
        rr = rmap.get(str(tid)) or rmap.get(str(s.get("id"))) or {}
        s7_stats.log_own_hand({
            "hand_id": str(s.get("id") or tid),
            "ts": s.get("submittedAt") or time.time(),
            "agent_id": agent_id,
            "competition_id": chal.get("competitionId") or chal.get("id") or "",
            "seat": data.get("seatNumber"),
            "hole": ",".join(data.get("holeCards") or []),
            "board": " ".join(data.get("boardCards") or []),
            "payout": data.get("payoutChips"),
            "committed": data.get("totalCommittedChips"),
            "stack": data.get("stackChips"),
            "score": s.get("score"),
            "chip_delta": rr.get("chip_delta"),
            "reasoning": data.get("reasoning"),
            "replay_url": rr.get("replay_url") or "",
        })
        n += 1
    return n


def _harvest_tables(c, agent_id, rmap):
    """/texas/recent-tables → hand_results + opp_hands. Devuelve {oppId: {name, comp}}."""
    opps = {}
    comp_q = _active_comp()   # recent-tables EXIGE competitionId (si no, 400); sigue rollovers vía meta
    url = f"/texas/recent-tables?limit=100&agentId={agent_id}" + (f"&competitionId={comp_q}" if comp_q else "")
    try:
        rt = c.get(url)
    except ArenaError:
        return opps
    data = rt.get("data") if isinstance(rt, dict) else (rt if isinstance(rt, list) else [])
    for t in data or []:
        tid = t.get("id") or t.get("tableId")
        if not tid:
            continue
        board = " ".join(t.get("boardCards") or [])
        comp = t.get("competitionId") or ""
        winners = t.get("winners") or []
        winset = {w.get("agentId") for w in winners}
        seats = t.get("seats") or []
        shown = [{"seat": s.get("seatNumber"), "name": s.get("agentName") or s.get("agentId"),
                  "hole": s.get("holeCards"), "payout": s.get("payoutChips"),
                  "committed": s.get("totalCommittedChips"), "hand": s.get("handName")}
                 for s in seats if s.get("holeCards")]
        mine = next((s for s in seats if s.get("agentId") == agent_id), {})
        rr = rmap.get(str(tid)) or {}
        s7_stats.log_hand_result(str(tid), board, winners, shown,
                                 int(mine.get("payoutChips") or 0),
                                 rr.get("chip_delta") or 0, mine.get("handName") or "",
                                 rr.get("replay_url") or "", int(t.get("potChips") or 0))
        for s in seats:
            oid = s.get("agentId")
            if not oid or oid == agent_id:
                continue
            opps[oid] = {"name": s.get("agentName") or oid, "comp": comp}    # perfilar TODOS los rivales
            if s.get("holeCards"):                                           # archivar solo manos mostradas
                s7_stats.log_opp_hand(str(tid), oid, s.get("agentName") or oid,
                                      ",".join(s.get("holeCards") or []), board,
                                      s.get("handName") or "", s.get("payoutChips") or 0,
                                      oid in winset, comp)
    return opps


def _harvest_profiles(c, opps):
    """/texas/agent-stats por rival → opp_profiles. Acotado a S7_TRACK_MAX_PROFILES/pasada (anti-429):
    salta rivales con perfil <1h y procesa hasta el cap del resto (cubre distintos rivales por pasada)."""
    cap = int(os.environ.get("S7_TRACK_MAX_PROFILES", "40"))
    now = time.time()
    fresh = set()
    try:
        for oid, ls, n in sqlite3.connect(s7_stats.DB, timeout=20).execute(
                "select opp_id, last_seen, n from opp_profiles"):
            if ls and now - ls < 3600 and n is not None:   # no saltar perfiles fallidos (N null): reintentar
                fresh.add(str(oid))
    except Exception:
        pass
    todo = [(oid, info) for oid, info in opps.items() if str(oid) not in fresh][:cap]
    for oid, info in todo:
        name = info.get("name") or oid
        comp = info.get("comp") or _active_comp()   # agent-stats EXIGE comp; sigue rollovers vía meta
        url = f"/texas/agent-stats?agentId={oid}" + (f"&competitionId={comp}" if comp else "")
        try:
            raw = c.get(url)
        except ArenaError:
            raw = None
        hud = {}
        if isinstance(raw, dict) and raw.get("sampleSize") is not None:
            hud = {"N": raw.get("sampleSize"), "vpip": raw.get("vpip"), "pfr": raw.get("pfr"),
                   "af": raw.get("af"), "bluffPct": raw.get("bluffPct"), "wtsd": raw.get("wtsd"),
                   "wsd": raw.get("wsd"), "playingStyle": raw.get("playingStyle")}
        try:
            shown = sqlite3.connect(s7_stats.DB, timeout=20).execute(
                "select count(*) from opp_hands where opp_id=?", (str(oid),)).fetchone()[0]
        except Exception:
            shown = 0
        s7_stats.upsert_opp_profile(oid, name, hud, shown)


def run_once():
    s7_stats.init()
    agents = _our_agents()
    _log(f"agentes propios: {len(agents)}")
    base = os.environ.get("ARENA_API_BASE", DEFAULT_BASE)
    total_own, all_opps = 0, {}
    for a in agents:
        c = ArenaClient(base, api_key=a["apiKey"])
        try:
            rmap = _replays_map(c, a["agentId"])
            total_own += _harvest_own(c, a["agentId"], rmap)
            all_opps.update(_harvest_tables(c, a["agentId"], rmap))
        except Exception as e:
            _log(f"agente {a['label']}: {e}")
        finally:
            c.close()
    if agents and all_opps:
        pc = ArenaClient(base, api_key=agents[0]["apiKey"])
        try:
            _harvest_profiles(pc, all_opps)
        finally:
            pc.close()
    _log(f"hecho: own_hands+={total_own}, rivales={len(all_opps)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=0, help="segundos entre pasadas (0 = una vez)")
    a = ap.parse_args()
    if a.interval > 0:
        while True:
            try:
                run_once()
            except Exception as e:
                _log("error:", e)
            time.sleep(a.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
