#!/usr/bin/env python
"""System 7 — real-time web dashboard (Bloomberg/Palantir style). Stdlib only.

  GET /              -> dense dark single-page dashboard (tabs: PANEL + MANOS)
  GET /api/state     -> JSON state (s7_test.db read-only + systemd health)
  GET /api/hands     -> summary of every hand (for the MANOS grid)
  GET /api/hand?key= -> one hand: event timeline + seat stacks + decisions (replayer)

Run:  uv run s7_dash.py        (binds 0.0.0.0:8787; access http://localhost:8787)
Read-only on the DB (WAL) -> never disturbs the running A/B services.
"""
import json
import os
import sqlite3
import subprocess
import threading
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("S7_STATS_DB", os.path.join(HERE, "s7_test.db"))
PORT = int(os.environ.get("S7_DASH_PORT", "8787"))
RANKS = "AKQJT98765432"


def _load_env():
    """Load OPENAI_* / S7_MODEL from .env so the COACH can call M3."""
    try:
        for ln in open(os.path.join(HERE, ".env")):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                if k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "S7_MODEL"):
                    os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_env()
_coach_cache = {"ts": 0.0, "txt": None, "hands": 0, "proposal": None, "version": None,
                "running": False, "err": None, "window": None}
# Strategy generator (pro-player persona) — async, mirrors the coach cache. data = full editable proposal.
_stratgen_cache = {"ts": 0.0, "data": None, "running": False, "err": None, "window": None, "mode": None}
# Shared strategy defaults/limits (mirror decide_system7) used by the builder + validation.
KN_DEFAULTS = {"open_size_bb": 2.5, "threebet_mult": 3, "value_eq": 0.62, "station_mult": 1.2,
               "cbet_bluff_frac": 0.33, "commit_spr": 3, "perejil_flop": 8, "perejil_turn": 10, "perejil_relief": 2}
KN_LIMITS = {"open_size_bb": (1.5, 5), "threebet_mult": (2, 5), "value_eq": (0.5, 0.85),
             "station_mult": (1.0, 2.0), "cbet_bluff_frac": (0.0, 1.0), "commit_spr": (1, 8),
             "perejil_flop": (4, 14), "perejil_turn": (6, 16), "perejil_relief": (0, 5)}
_POS6 = ["UTG", "MP", "CO", "BTN", "SB", "BB"]
_DEF_3BV = ["AA", "KK", "QQ", "JJ", "AKs", "AKo", "AQs"]
_DEF_3BB = ["A2s", "A3s", "A4s", "A5s", "K9s", "Q9s"]
try:
    import s7_strat
except Exception:
    s7_strat = None
try:
    import s7_mllm   # loads .env (provider keys) on import → provider_ready() works in the dash
except Exception:
    s7_mllm = None
import s7_jobs        # run backend: systemd on the LXC, plain subprocess in Docker (auto-detected)
import s7_api         # nuevos endpoints del rediseño (LAB · PRODUCCIÓN · TRACKER)
CLASIF_DIR = os.environ.get("S7_CLASIF_DIR", os.path.join(HERE, ".clasif"))
_cache = {}                       # state cache por game (cash/tournament)
_lock = threading.Lock()


def _ro():
    return sqlite3.connect(f"file:{s7_api._dbpath()}?mode=ro", uri=True, timeout=5)


def _svc(name):
    return s7_jobs.is_active(name)


def _state():
    try:
        c = _ro()
    except Exception as e:
        return {"error": f"db unavailable: {e}", "ts": time.time(),
                "svc": {n: _svc(n) for n in ("arena-test", "arena-test-wide", "arena-report.timer", "arena-dash")}}

    def q(sql, args=()):
        try:
            return c.execute(sql, args).fetchall()
        except Exception:
            return []

    def one(sql, a=()):
        r = q(sql, a)
        return r[0][0] if r else 0

    total = one("select count(*) from decisions")
    hands = one("select count(distinct hand_key) from decisions")
    eng = dict(q("select engine,count(*) from decisions group by engine"))
    m3 = eng.get("M3", 0)

    pf = q("select hand_class,count(*),sum(voluntary),sum(preflop_raise) "
           "from decisions where street='preflop' and hand_class!='' group by hand_class")
    classes = {r[0]: {"n": r[1], "vpip": round(100 * r[2] / r[1]) if r[1] else 0,
                      "pfr": round(100 * r[3] / r[1]) if r[1] else 0} for r in pf}

    pos = q("select pos,count(*),sum(voluntary),sum(preflop_raise) "
            "from decisions where street='preflop' and pos!='' group by pos")
    order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
    bypos = sorted([{"pos": r[0], "n": r[1],
                     "vpip": round(100 * r[2] / r[1]) if r[1] else 0,
                     "pfr": round(100 * r[3] / r[1]) if r[1] else 0} for r in pos],
                   key=lambda x: order.get(x["pos"], 9))

    streets = []
    for st in ("flop", "turn", "river"):
        a = dict(q("select action,count(*) from decisions where street=? group by action", (st,)))
        tot = sum(a.values())
        if tot:
            streets.append({"street": st, "n": tot, "fold": a.get("fold", 0), "check": a.get("check", 0),
                            "call": a.get("call", 0), "agg": a.get("bet", 0) + a.get("raise", 0) + a.get("all-in", 0)})

    strength = [{"s": r[0], "n": r[1], "agg": r[2], "call": r[3], "pasv": r[4]} for r in q(
        "select strength,count(*),sum(case when action in ('bet','raise','all-in') then 1 else 0 end),"
        "sum(case when action='call' then 1 else 0 end),"
        "sum(case when action in ('check','fold') then 1 else 0 end) "
        "from decisions where street!='preflop' and strength!='' group by strength order by 2 desc")]

    m3street = dict(q("select street,count(*) from decisions where engine='M3' group by street"))
    m3str = dict(q("select strength,count(*) from decisions where engine='M3' and strength!='' group by strength"))
    arch = dict(q("select archetype,count(*) from decisions group by archetype"))
    _en = q("select count(*),avg(vpip),avg(pfr),avg(af),avg(wtsd),avg(wsd) from agent_stats")
    enemy = None
    if _en and _en[0][0]:
        enemy = {"n": _en[0][0], "vpip": _en[0][1], "pfr": _en[0][2], "af": _en[0][3],
                 "wtsd": _en[0][4], "wsd": _en[0][5]}
        enemy["archetype"] = _arch(enemy)
    # rendimiento por motor/modelo: cada mano etiquetada por el modelo LLM usado (o 'heur')
    erows = q("select d.hand_key, d.model, d.engine, hr.chip_delta from decisions d "
              "join hand_results hr on hr.table_id = substr(d.hand_key,1,instr(d.hand_key,':')-1)")
    _ph = {}
    for hk, model, engd, delta in erows:
        e = _ph.setdefault(hk, {"models": set(), "delta": 0})
        e["models"].add(model or ("M3" if engd == "M3" else "heur"))
        if delta is not None:
            e["delta"] = delta
    _eagg = {}
    for v in _ph.values():
        nonheur = sorted(m for m in v["models"] if m and m != "heur")
        tag = nonheur[0] if nonheur else "heur"
        a = _eagg.setdefault(tag, {"hands": 0, "delta": 0, "wins": 0})
        a["hands"] += 1
        d_ = v["delta"] or 0
        a["delta"] += d_
        a["wins"] += 1 if d_ > 0 else 0
    engines = sorted([{"model": t, "hands": a["hands"], "delta": a["delta"],
                       "winpct": round(100 * a["wins"] / a["hands"]) if a["hands"] else 0,
                       "bb100": round(50 * a["delta"] / a["hands"], 1) if a["hands"] else 0}
                      for t, a in _eagg.items()], key=lambda x: -x["hands"])
    recent = [{"ts": r[0], "pos": r[1], "hole": r[2], "street": r[3], "strength": r[4],
               "action": r[5], "amount": r[6], "engine": r[7], "label": r[8], "key": r[9]}
              for r in q("select ts,pos,hole,street,strength,action,amount,engine,run_label,hand_key "
                         "from decisions order by ts desc limit 26")]
    # Temporada vigente del agente (mismo patrón que _hands): acota la curva a la competición actual —
    # todos los deploys del Playground comparten run_label='playground', sin esto se mezclan seasons.
    _cc = None
    try:
        _my = _my_agent_id()
        _ccr = c.execute("select competition_id from decisions where agent_id=? and competition_id is not null "
                         "and competition_id!='' order by ts desc limit 1", (_my,)).fetchone()
        _cc = _ccr[0] if _ccr else None
    except Exception:
        pass
    eqrows = q("select run_label,hands,raw_chips,adj_chips,reentry,competition_id from equity order by ts")
    # label 'playground' → solo la temporada vigente; el resto de labels (Eval/bench, sin comp) pasan tal cual
    eqrows = [r for r in eqrows if r[0] != "playground" or (_cc and (r[5] or "") == _cc)]
    equity = {}
    for lbl in {r[0] for r in eqrows}:
        seg = [r for r in eqrows if r[0] == lbl]
        # stitch sawtooth: the cumulative counters reset to ~0 on each service restart
        stitched = []
        bh = 0.0; lh = lraw = ladj = 0.0   # bh = offset SOLO de manos; el neto NO se desplaza
        ph = None
        for (_, h, rw, ad, _re, _cp) in seg:
            h = h or 0; rw = rw or 0; ad = ad or 0
            if ph is not None and h < ph:      # restart: SOLO se reinician las manos (x); el neto (raw/adj) persiste con el stack del Arena → NO desplazar
                bh = lh
            lh, lraw, ladj = bh + h, rw, ad
            stitched.append((lh, lraw, ladj))
            ph = h
        step = max(1, len(stitched) // 160)
        equity[lbl] = [{"h": p[0], "raw": round(p[1], 1), "adj": round(p[2], 1)} for p in stitched[::step]]
    # equity EN VIVO: una curva NUEVA por cada re-entry (cada buy-in su propia P&L; neto relativo a ESA
    # entrada), sobre filas YA acotadas a la temporada vigente (reentry se re-ancla por season).
    live_equity = {}
    try:
        if _cc:
            by_re = {}
            for (rl, h, rw, ad, re, _cp) in eqrows:            # eqrows ya viene ordenado por ts
                if rl != "playground":
                    continue
                by_re.setdefault(int(re or 0), []).append((rw or 0, ad or 0))
            for re in sorted(by_re):
                pts = by_re[re]
                step2 = max(1, len(pts) // 160)
                sampled = pts[::step2]
                live_equity["entrada %d" % (re + 1)] = [{"h": i * step2, "raw": round(p[0], 1), "adj": round(p[1], 1)}
                                                        for i, p in enumerate(sampled)]
    except Exception:
        pass
    c.close()
    return {
        "ts": time.time(), "hands": hands, "decisions": total, "m3": m3, "heur": eng.get("heur", 0),
        "m3pct": round(100 * m3 / total, 1) if total else 0,
        "classes": classes, "ranks": list(RANKS), "bypos": bypos,
        "streets": streets, "strength": strength, "m3street": m3street, "m3str": m3str,
        "arch": arch, "recent": recent, "equity": equity, "live_equity": live_equity, "enemy": enemy,
        "svc": {n: _svc(n) for n in ("arena-test", "arena-test-wide", "arena-report.timer", "arena-dash")},
        "engines": engines,
    }


def _lj(s, default=None):
    """Safe json.loads -> default (list by default)."""
    if not s:
        return [] if default is None else default
    try:
        return json.loads(s)
    except Exception:
        return [] if default is None else default


def _hand(key):
    """One hand: event timeline + seat stacks + decisions (+M3 logs) + showdown result."""
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}
    he = None
    try:
        he = c.execute("select seat,hole,board,events,seats from hand_events where hand_key=?", (key,)).fetchone()
    except Exception:
        pass
    decs = c.execute(
        "select street,pos,hole,board,strength,texture,spr,pot,call_chips,adj_outs,action,amount,engine,m3_log "
        "from decisions where hand_key=? order by ts", (key,)).fetchall()
    tid = (key or "").split(":")[0]
    res = None
    try:
        rr = c.execute("select board,winners,seats_shown,payout,chip_delta,our_hand,replay_url "
                       "from hand_results where table_id=?", (tid,)).fetchone()
        if rr:
            res = {"board": rr[0], "winners": _lj(rr[1]), "seats_shown": _lj(rr[2]),
                   "payout": rr[3], "chip_delta": rr[4], "our_hand": rr[5],
                   "replay_url": (rr[6] if len(rr) > 6 else "")}
    except Exception:
        pass
    c.close()
    events = _lj(he[3]) if he else []
    seats = _lj(he[4]) if (he and len(he) > 4) else []
    hole = (he[1] if he else "") or (decs[0][2] if decs else "")
    board = (he[2] if he else "") or (decs[-1][3] if decs else "") or ((res or {}).get("board") or "")
    endStreet = None
    if res:
        bl = len((res.get("board") or "").split())
        if bl >= 5:
            our_river_fold = any(d[0] == "river" and d[10] == "fold" for d in decs)
            endStreet = "river" if (our_river_fold and len(res.get("winners") or []) <= 1) else "showdown"
        else:
            endStreet = {0: "preflop", 3: "flop", 4: "turn"}.get(bl)
    return {"key": key, "seat": he[0] if he else None, "hole": hole, "board": board,
            "events": events, "seats": seats, "result": res, "endStreet": endStreet,
            "decisions": [{"street": d[0], "pos": d[1], "strength": d[4], "texture": d[5], "spr": d[6],
                           "pot": d[7], "call": d[8], "outs": d[9], "action": d[10], "amount": d[11],
                           "engine": d[12], "m3": (_lj(d[13]) or None) if d[13] else None} for d in decs]}


# Filtro "solo mi agente autenticado": decisiones tagueadas con mi agentId, MÁS las del Playground
# (prod-* que NO son runs de Eval) que aún no estaban tagueadas. Excluye los s7t-* desechables del Eval.
_MY_DEC = ("(agent_id = ? OR (agent_id IS NULL AND run_label LIKE 'prod-%' "
           "AND run_label NOT IN (SELECT run_label FROM runs WHERE agent_id IS NOT NULL AND agent_id != ?)))")


def _my_agent_id():
    """agentId del agente autenticado/reclamado (.arena-pg-credentials) — unifica el dashboard a él."""
    try:
        return json.load(open(os.path.join(s7_api._DATA, ".arena-pg-credentials"))).get("agentId") or ""
    except Exception:
        return ""


def _hands(limit=600):
    """Summary of every recent hand (grouped from decisions) for the MANOS grid."""
    try:
        c = _ro()
    except Exception as e:
        return {"hands": [], "error": str(e)}
    my = _my_agent_id()
    _cc = None
    try:                                    # competición actual del agente (la más reciente) → unifica sus runs de esa season
        r = c.execute("select competition_id from decisions where agent_id=? and competition_id is not null "
                      "and competition_id!='' order by ts desc limit 1", (my,)).fetchone()
        _cc = r[0] if r else None
    except Exception:
        _cc = None
    _cond = _MY_DEC + (" AND competition_id = ?" if _cc else "")
    _args = (my, my) + ((_cc,) if _cc else ())
    rows = c.execute(
        "select ts,hand_key,street,pos,hole,hand_class,board,action,amount,engine,run_label,pot,spr "
        "from decisions where " + _cond + " order by ts desc limit ?", _args + (limit * 6,)).fetchall()
    resmap = {}
    try:
        for tid, delta, winners, rboard, fpot in c.execute("select table_id, chip_delta, winners, board, pot from hand_results"):
            wl = _lj(winners)
            resmap[tid] = (delta, (wl[0].get("agentName") or wl[0].get("agentId")) if wl else None,
                           len((rboard or "").split()), len(wl), fpot)
    except Exception:
        pass
    c.close()
    hands, order = {}, []
    for r in rows:
        k = r[1]
        if k not in hands:
            hands[k] = {"key": k, "ts": r[0], "pos": r[3], "hole": r[4], "hclass": r[5],
                        "board": r[6] or "", "label": r[10], "acts": [], "m3": 0, "pot": 0,
                        "streets": set(), "sprst": {}}
            order.append(k)
        h = hands[k]
        h["acts"].insert(0, {"st": r[2], "action": r[7], "amount": r[8]})
        if r[9] == "M3":
            h["m3"] += 1
        h["pot"] = max(h["pot"], r[11] or 0)
        h["streets"].add(r[2])
        if r[12] is not None:
            h["sprst"][r[2]] = r[12]
        if r[6] and len(r[6]) > len(h["board"]):
            h["board"] = r[6]
        if r[2] == "preflop":
            h["pos"], h["hole"], h["hclass"] = r[3], r[4], r[5]
    out = []
    for k in order[:limit]:
        h = hands[k]
        _slab = {"preflop": "PF", "flop": "F", "turn": "T", "river": "R"}
        _byst = {}
        for a in h["acts"]:
            _byst.setdefault(a["st"], []).append((a["action"] or "") + ((" " + str(a["amount"])) if a["amount"] else ""))
        moves = " · ".join(_slab.get(st, st) + ": " + ", ".join(_byst[st])
                           for st in ("preflop", "flop", "turn", "river") if st in _byst)
        rr = resmap.get(k.split(":")[0])
        dl, wn, bl, nw, fp = rr if rr else (None, None, None, None, None)
        if dl is not None or fp:                 # bote = max(observado, liquidado, 2×|neto|): el Arena no da el bote por mano, pero en HU el bote ≈ 2× lo que cambia de manos (cada uno aporta ~la mitad) → corrige los all-in donde el bote de las decisiones es de ANTES del jam
            h["pot"] = max(h["pot"], int(fp or 0), 2 * abs(int(dl or 0)))
        if bl is None:
            reached = next((s for s in ("river", "turn", "flop", "preflop") if s in h["streets"]), "preflop")
        elif bl >= 5:
            ourfold = any(a["st"] == "river" and a["action"] == "fold" for a in h["acts"])
            reached = "river" if (ourfold and (nw or 0) <= 1) else "showdown"
        else:
            reached = {0: "preflop", 3: "flop", 4: "turn"}.get(bl, "preflop")
        out.append({"key": k, "ts": h["ts"], "pos": h["pos"], "hole": h["hole"], "hclass": h["hclass"],
                    "board": h["board"], "label": h["label"], "reached": reached, "m3": h["m3"],
                    "pot": h["pot"], "n": len(h["acts"]), "moves": moves,
                    "act_pf": ", ".join(_byst.get("preflop", [])), "act_flop": ", ".join(_byst.get("flop", [])),
                    "act_turn": ", ".join(_byst.get("turn", [])), "act_river": ", ".join(_byst.get("river", [])),
                    "fold": next((st for st in ("preflop", "flop", "turn", "river")
                                  if any("fold" in (a or "") for a in _byst.get(st, []))), ""),
                    "spr_pf": h["sprst"].get("preflop"),
                    "spr_post": h["sprst"].get("flop") or h["sprst"].get("turn") or h["sprst"].get("river"),
                    "delta": dl, "winner": wn, "won": (dl is not None and dl > 0)})
    return {"hands": out, "count": len(order)}


def _arch(hud):
    if not hud or not hud.get("n"):
        return "?"
    v = hud.get("vpip") or 0
    v = v * 100 if v <= 1 else v
    pfr = hud.get("pfr") or 0
    pfr = pfr * 100 if pfr <= 1 else pfr
    af = hud.get("af") or 0
    if v < 15:
        return "NIT"
    if v >= 28 and af >= 2.5:
        return "LAG"
    if v >= 24 and af < 1.2:
        return "STATION"
    if af >= 3.5:
        return "MANIAC"
    if pfr >= 0.6 * v:
        return "TAG"
    return "REG"


def _players(limit=400):
    """Per-opponent analysis: official HUD + hands we've seen them show + win rate."""
    try:
        c = _ro()
    except Exception as e:
        return {"players": [], "error": str(e)}
    hud = {}
    try:
        import decide_system7 as _DS
        for r in c.execute("select opp_id,name,n,vpip,pfr,af,bluff_pct,wtsd,wsd,style,shown_hands,last_seen "
                           "from opp_profiles"):
            arc = _DS._archetype({"N": r[2], "vpip": r[3], "pfr": r[4], "af": r[5], "playingStyle": _lj(r[9], {})})
            hud[r[1]] = {"agent_id": r[0], "n": r[2], "vpip": r[3], "pfr": r[4], "af": r[5],
                         "bluff": r[6], "wtsd": r[7], "wsd": r[8], "style": _lj(r[9], {}),
                         "shown": r[10], "last_seen": r[11], "archetype": arc, "adapting": arc != "UNKNOWN"}
    except Exception:
        pass
    tidkey = {}
    try:
        for (hk,) in c.execute("select hand_key from hand_events"):
            tidkey[hk.split(":")[0]] = hk
    except Exception:
        pass
    players = {}
    try:
        rows = c.execute("select table_id,winners,seats_shown,board,chip_delta "
                         "from hand_results order by ts desc limit ?", (limit,)).fetchall()
    except Exception:
        rows = []
    for tid, winners, shown, board, delta in rows:
        winset = {(w.get("agentName") or w.get("agentId")) for w in _lj(winners)}
        for s in _lj(shown):
            nm = s.get("name")
            if not nm or nm == "S7 test":
                continue
            p = players.setdefault(nm, {"seen": 0, "wins": 0, "hands": []})
            p["seen"] += 1
            won = nm in winset
            if won:
                p["wins"] += 1
            if len(p["hands"]) < 40:
                p["hands"].append({"key": tidkey.get(tid), "hole": s.get("hole"),
                                   "hand": s.get("hand"), "won": won, "board": board})
    c.close()
    out = []
    for nm in (set(hud) | set(players)):
        hu = hud.get(nm, {})
        pl = players.get(nm, {"seen": 0, "wins": 0, "hands": []})
        out.append({"name": nm, "agent_id": hu.get("agent_id"), "hud": hu, "archetype": _arch(hu),
                    "seen": pl["seen"], "wins": pl["wins"], "hands": pl["hands"]})
    out.sort(key=lambda x: (-x["seen"], -(x["hud"].get("n") or 0)))
    return {"players": out}


COACH_NEED = 500       # HU S4: empezar a evaluar/coachear desde 500 manos


def _deployed_knobs():
    """Knobs de la estrategia REALMENTE desplegada (NO los defaults 6-max del proceso dashboard):
    el dashboard importa decide_system7 con KN por defecto; la estrategia HU se carga SOLO en el
    run_pvp del deploy → el coach debe leer los knobs del deploy ACTIVO, no `decide_system7.KN`."""
    kn = dict(KN_DEFAULTS)
    try:
        deploys = s7_api._jload(s7_api._DEPLOYS_PATH, {})
        agent = next((m.get("agent") for m in deploys.values()
                      if isinstance(m, dict) and m.get("agent")), None)
        strat = (s7_api.s7_agents.load(agent) or {}).get("strategy") if agent else ""
        if strat and s7_strat:
            cfg = s7_strat.load(strat) or {}
            kn.update({k: v for k, v in (cfg.get("knobs") or {}).items()
                       if k in KN_DEFAULTS and isinstance(v, (int, float))})
    except Exception:
        pass
    return kn


def _coach(window=None):
    """Rule-based analysis of our play vs optimal targets (gated at 5k hands).

    window: None/'all' = todas las manos; int (p.ej. 10000) = sólo las últimas N manos.
    Añade `vs_opt` (tu stat vs banda óptima 6-max + veredicto ✓/⚠/✗) y `vs_panel`
    (bb/100 agregado contra el panel near-GTO DeepCFR = distancia al óptimo real)."""
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}
    try:
        n = int(window) if window not in (None, "", "all") else 0
    except Exception:
        n = 0
    n = max(0, min(500000, n))

    def hk(col="hand_key"):
        """AND-clause limiting to the most-recent N hands (empty if no window)."""
        if n > 0:
            return (" AND %s IN (SELECT hand_key FROM decisions GROUP BY hand_key "
                    "ORDER BY MAX(ts) DESC LIMIT %d) " % (col, n))
        return " "

    def q(sql, a=()):
        try:
            return c.execute(sql, a).fetchall()
        except Exception:
            return []

    def verdict(v, lo, hi, soft):
        if v is None:
            return "—"
        if lo <= v <= hi:
            return "✓"
        if (lo - soft) <= v <= (hi + soft):
            return "⚠"
        return "✗"

    total_hands = (q("select count(distinct hand_key) from decisions") or [[0]])[0][0]
    if total_hands < COACH_NEED:
        c.close()
        return {"locked": True, "hands": total_hands, "need": COACH_NEED}
    win_hands = (q("select count(distinct hand_key) from decisions where 1=1" + hk()) or [[0]])[0][0]
    findings, advice, vs_opt = [], [], []

    v = q("select sum(voluntary),sum(preflop_raise),count(*) from decisions where street='preflop'" + hk())
    if v and v[0][2]:
        vol, pfr, npf = v[0]
        vp, pf = round(100 * (vol or 0) / npf), round(100 * (pfr or 0) / npf)
        gap = vp - pf
        findings.append({"k": "VPIP / PFR", "v": f"{vp}% / {pf}%", "ref": "HU ~75 / 47"})
        vs_opt.append({"k": "VPIP", "you": f"{vp}%", "target": "68–82%",
                       "verdict": verdict(vp, 68, 82, 6), "note": "HU: se juegan MUCHAS manos (SB abre ~85%, BB defiende ~65%)"})
        vs_opt.append({"k": "PFR", "you": f"{pf}%", "target": "40–54%",
                       "verdict": verdict(pf, 40, 54, 6), "note": "HU: el SB sube casi siempre; el BB sobre todo iguala"})
        vs_opt.append({"k": "Gap VPIP-PFR", "you": f"{gap}", "target": "18–34",
                       "verdict": verdict(gap, 18, 34, 6), "note": "HU: un gap alto es NORMAL (el BB defiende igualando)"})
        if vp < 62:
            advice.append(f"Demasiado tight para HU (VPIP {vp}%). El SB debe abrir ~80-90% y el BB defender ~60-70%.")
        elif vp > 88:
            advice.append(f"VPIP {vp}% excesivo incluso en HU; el BB no debe defender casi todo OOP.")
        if pf < 36:
            advice.append(f"PFR {pf}% bajo para HU; el SB debe subir-primero la mayoría de manos (menos limps).")
    cb = q("select sum(case when action in('bet','all-in') then 1 else 0 end),"
           "sum(case when action='check' then 1 else 0 end) from decisions "
           "where street='flop' and (call_chips is null or call_chips<=0)" + hk())
    if cb and cb[0][0] is not None and ((cb[0][0] or 0) + (cb[0][1] or 0)) >= 100:
        bets, checks = cb[0][0] or 0, cb[0][1] or 0
        cbp = round(100 * bets / (bets + checks))
        findings.append({"k": "C-bet flop", "v": f"{cbp}%", "ref": "HU 52–70%"})
        vs_opt.append({"k": "C-bet flop", "you": f"{cbp}%", "target": "52–70%",
                       "verdict": verdict(cbp, 52, 70, 8), "note": "HU: c-bet alto EN posición (SB); más bajo OOP (BB)"})
        if cbp > 85:
            advice.append(f"C-bet flop muy alto ({cbp}%); equilibra con más checks de protección de rango.")
        elif cbp < 45:
            advice.append(f"C-bet flop bajo ({cbp}%); aprovecha más la iniciativa como agresor preflop.")
    # Fold-to-cbet sólo si hay muestra suficiente (este bot suele ser el agresor → rara vez afronta apuesta).
    ff = q("select sum(case when action='fold' then 1 else 0 end),count(*) from decisions "
           "where street='flop' and call_chips>0" + hk())
    if ff and ff[0][1] and ff[0][1] >= 200:
        ftc = round(100 * (ff[0][0] or 0) / ff[0][1])
        findings.append({"k": "Fold-to-bet flop", "v": f"{ftc}%", "ref": "HU <50%"})
        vs_opt.append({"k": "Fold-to-cbet flop", "you": f"{ftc}%", "target": "38–50%",
                       "verdict": verdict(ftc, 38, 50, 6), "note": "HU: rangos anchos → foldear de más te explota"})
        if ftc > 54:
            advice.append(f"Foldeas demasiado al c-bet en flop ({ftc}%) para HU. Defiende más (flota/check-raise).")
    pos = q("select pos,count(*),sum(voluntary),sum(preflop_raise) from decisions "
            "where street='preflop' and pos!=''" + hk() + " group by pos")
    order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
    bypos = sorted([{"pos": r[0], "n": r[1], "vpip": round(100 * r[2] / r[1]) if r[1] else 0,
                     "pfr": round(100 * r[3] / r[1]) if r[1] else 0} for r in pos], key=lambda x: order.get(x["pos"], 9))
    for bp in bypos:                              # objetivos HU por posición (SB=botón que ataca / BB=defensa)
        if bp["pos"] == "SB" and bp["n"] >= 50:
            vs_opt.append({"k": "SB VPIP (botón)", "you": f"{bp['vpip']}%", "target": "78–92%",
                           "verdict": verdict(bp["vpip"], 78, 92, 6), "note": "el SB ataca casi siempre la BB"})
            vs_opt.append({"k": "SB PFR (sube 1º)", "you": f"{bp['pfr']}%", "target": "62–84%",
                           "verdict": verdict(bp["pfr"], 62, 84, 8), "note": "subir-primero > limpear en HU"})
            if bp["pfr"] < 55:
                advice.append(f"SB sube poco ({bp['pfr']}%): en HU el botón debe abrir-subiendo ~70-80%, no limpear.")
        if bp["pos"] == "BB" and bp["n"] >= 50:
            vs_opt.append({"k": "BB VPIP (defensa)", "you": f"{bp['vpip']}%", "target": "55–75%",
                           "verdict": verdict(bp["vpip"], 55, 75, 8), "note": "defender ancho vs el open del SB (call+3bet)"})
            vs_opt.append({"k": "BB 3bet (PFR)", "you": f"{bp['pfr']}%", "target": "12–26%",
                           "verdict": verdict(bp["pfr"], 12, 26, 6), "note": "3bet de valor + faroles polarizados"})
            if bp["vpip"] < 50:
                advice.append(f"BB defiende poco ({bp['vpip']}%): vs el open del SB hay que defender ~60-70% para no ser explotado.")
    buckets = q("select d.pos, sum(hr.chip_delta), count(distinct d.hand_key) from decisions d "
                "join hand_results hr on hr.table_id = substr(d.hand_key,1,instr(d.hand_key,':')-1) "
                "where d.street='preflop' and d.pos!=''" + hk("d.hand_key") + " group by d.pos")
    posres = sorted([{"pos": r[0], "delta": r[1] or 0, "n": r[2]} for r in buckets], key=lambda x: x["delta"])
    if posres and posres[0]["delta"] < 0:
        w = posres[0]
        advice.append(f"Pierdes más desde {w['pos']} ({w['delta']:+d} fichas en {w['n']} manos). Revisa rango/líneas ahí.")
    runs = q("select run_label,avg(adjusted_bb100),count(*) from runs where hands>=400 group by run_label")
    ab = sorted([{"label": r[0], "bb100": round(r[1], 1) if r[1] is not None else None, "n": r[2]} for r in runs],
                key=lambda x: -(x["bb100"] if x["bb100"] is not None else -999))
    if len(ab) >= 2 and ab[0]["bb100"] is not None and ab[-1]["bb100"] is not None:
        advice.append(f"A/B: lidera '{ab[0]['label']}' ({ab[0]['bb100']:+} bb/100, n={ab[0]['n']}) vs '{ab[-1]['label']}' ({ab[-1]['bb100']:+}).")
    pan = q("select count(*),avg(adjusted_bb100),sum(hands) from runs where hands>=400")
    vs_panel = None
    if pan and pan[0][0]:
        vs_panel = {"bb100": round(pan[0][1], 1) if pan[0][1] is not None else None,
                    "runs": pan[0][0], "hands": pan[0][2] or 0}
    # rivales HU REALES (opp_profiles, escritos por run_pvp durante el juego HU), en porcentaje
    opp = [{"name": o[0], "vpip": round((o[1] or 0) * (100 if (o[1] or 0) <= 1 else 1)),
            "pfr": round((o[2] or 0) * (100 if (o[2] or 0) <= 1 else 1)),
            "af": round(o[3], 1) if o[3] is not None else None}
           for o in q("select name,vpip,pfr,af from opp_profiles where n>=50 order by n desc limit 8")]
    since_change, stale = None, False           # manos desde el último cambio de ESTRATEGIA = mtime del JSON desplegado (no el deploy, que se reinicia sin cambiar nada)
    try:
        _dp = s7_api._jload(s7_api._DEPLOYS_PATH, {})
        _ag = next((m.get("agent") for m in _dp.values() if isinstance(m, dict) and m.get("agent")), None)
        _strat = (s7_api.s7_agents.load(_ag) or {}).get("strategy") if _ag else ""
        if _strat:
            _mt = os.path.getmtime(os.path.join(s7_strat.DIR, _strat + ".json"))
            since_change = (q("select count(distinct hand_key) from decisions where ts > ?", (_mt,)) or [[0]])[0][0]
            stale = since_change < 300
    except Exception:
        pass
    c.close()
    if not advice:
        advice.append("Sin leaks claros con esta muestra; sigue acumulando.")
    return {"locked": False, "hands": total_hands, "win_hands": win_hands, "window": (n or "all"),
            "vs_opt": vs_opt, "vs_panel": vs_panel, "findings": findings, "bypos": bypos,
            "posres": posres, "ab": ab, "advice": advice, "opp": opp,
            "since_change": since_change, "stale": stale}


def _validate_strat(cfg):
    """Sanitise an M3-proposed strategy config; return a safe dict or None."""
    if not isinstance(cfg, dict):
        return None
    out = {}
    orr = cfg.get("opening_ranges")
    if isinstance(orr, dict):
        clean = {str(p).upper(): [str(t) for t in toks][:169] for p, toks in orr.items()
                 if str(p).upper() in ("UTG", "MP", "CO", "BTN", "SB", "BB") and isinstance(toks, list)}
        if clean:
            out["opening_ranges"] = clean
    for k in ("threebet_value", "threebet_bluff"):
        if isinstance(cfg.get(k), list):
            out[k] = [str(t) for t in cfg[k]][:60]
    kn = cfg.get("knobs") if isinstance(cfg.get("knobs"), dict) else {}
    ck = {k: max(lo, min(hi, kn[k])) for k, (lo, hi) in KN_LIMITS.items() if isinstance(kn.get(k), (int, float))}
    if isinstance(kn.get("sizing"), dict):
        ck["sizing"] = kn["sizing"]
    if ck:
        out["knobs"] = ck
    if cfg.get("game") in ("cash", "tournament"):
        out["game"] = cfg["game"]
    if cfg.get("mode") in ("agr", "nit", "std"):
        out["mode"] = cfg["mode"]
    if isinstance(cfg.get("bb_buckets"), list) and len(cfg["bb_buckets"]) == 3:
        try:
            out["bb_buckets"] = [int(x) for x in cfg["bb_buckets"]]
        except Exception:
            pass
    if isinstance(cfg.get("tournament_ranges"), dict):
        tr = {}
        for b, pm in cfg["tournament_ranges"].items():
            if b in ("deep", "mid", "short", "push") and isinstance(pm, dict):
                tr[b] = {str(p).upper(): [str(t) for t in toks][:169] for p, toks in pm.items()
                         if str(p).upper() in ("UTG", "MP", "CO", "BTN", "SB", "BB") and isinstance(toks, list)}
        if tr:
            out["tournament_ranges"] = tr
    return out or None


_REASONING_MODEL = {"deepseek": "deepseek-reasoner"}   # variante de razonamiento por proveedor (R1 en deepseek)


def _llm_route(reasoning=False):
    """(provider, model) del modelo configurado para los coaches LLM: default si lo hay, si no el LIVE
    (el que usa el Playground), si no minimax. Con reasoning=True usa la variante de razonamiento del
    proveedor (p.ej. deepseek-reasoner). Asegura las API keys en os.environ."""
    try:
        if hasattr(s7_api, "_apply_settings_keys"):
            s7_api._apply_settings_keys()
        s = json.load(open(os.path.join(s7_api._DATA, "settings.json")))
        cfg = s.get("default") or s.get("live") or {}
        prov, mdl = cfg.get("provider"), cfg.get("model")
        if prov and mdl:
            return prov, (_REASONING_MODEL.get(prov, mdl) if reasoning else mdl)
    except Exception:
        pass
    return "minimax", os.environ.get("S7_MODEL", "MiniMax-M3")


def _coach_hands(scope="lifetime"):
    """Top-10 + bottom-10 manos por rentabilidad (chip_delta). scope: 'lifetime' (todas) o
    'session' (último burst de juego — corte con hueco > 30 min)."""
    hs = [h for h in (_hands(5000).get("hands") or []) if h.get("delta") is not None]
    if scope == "session" and hs:
        hh = sorted(hs, key=lambda x: x.get("ts") or 0, reverse=True)
        sess = [hh[0]]
        for h in hh[1:]:
            if (sess[-1].get("ts") or 0) - (h.get("ts") or 0) > 1800 or len(sess) >= 200:   # corte: hueco > 30 min, o tope 200 (el bot juega 24/7 → sesión = slice reciente)
                break
            sess.append(h)
        hs = sess
    bys = sorted(hs, key=lambda x: x.get("delta") or 0)
    return {"scope": scope, "n": len(hs), "top": list(reversed(bys[-10:])), "bottom": bys[:10]}


_hands_coach_cache = {"ts": 0.0, "txt": None, "running": False, "kid": None, "err": None, "game": "cash"}


def _hands_coach_compute(keys):
    """LLM mano-a-mano de las manos SELECCIONADAS (por key) en background; escribe _hands_coach_cache."""
    try:
        os.environ["S7_STATS_DB"] = s7_api._dbpath(_hands_coach_cache.get("game"))
        by_key = {h["key"]: h for h in (_hands(5000).get("hands") or [])}
        sel = [by_key[k] for k in keys if k in by_key]
        if not sel:
            _hands_coach_cache.update(running=False, ts=time.time(), err="no encuentro esas manos.")
            return

        def _h(h):
            tag = "GANA" if (h.get("delta") or 0) >= 0 else "PIERDE"
            return "[%s %+d] %s | mano %s | board %s | %s | bote~%s" % (
                tag, h.get("delta") or 0, h.get("pos") or "?", h.get("hole") or "?",
                h.get("board") or "(preflop)", h.get("moves") or "(sin acciones)", h.get("pot"))
        lines = [_h(h) for h in sel]
        system = ("Eres un coach de poker NLHE HEADS-UP / torneo de élite (EducaPoker / GTO + explotación). "
                  "Te paso una selección de manos (GANA=ganaste fichas, PIERDE=las perdiste). Para CADA mano, en 1-2 frases: "
                  "¿se jugó BIEN o MAL? + el leak o acierto CONCRETO (rango/3bet preflop, sizing/barrel postflop, "
                  "commit/SPR, fold o call clave). Empieza cada línea con el resultado en fichas. Mano a mano, NO números "
                  "agregados. Español, directo, sin rodeos.")
        user = "MANOS:\n" + "\n".join(lines)
        prov, mdl = _llm_route(reasoning=True)   # análisis mano a mano → modelo de RAZONAMIENTO
        cerr = None
        if s7_mllm is not None:
            r = s7_mllm._chat(prov, mdl, system, user, max_tokens=9000)
            txt, cerr = (None if r.get("error") else (r.get("answer") or "")), r.get("error")
        else:
            txt = llm_system7._minimax_call(system, user, 6000, mdl)
        if not txt:
            _hands_coach_cache.update(running=False, ts=time.time(),
                                      err=("M3: " + cerr) if cerr else "M3 no devolvió respuesta (tokens/clave).")
            return
        _hands_coach_cache.update(ts=time.time(), txt=txt, model=mdl, err=None, running=False)
    except Exception as e:
        _hands_coach_cache.update(running=False, err=str(e), ts=time.time())


def _hands_coach_llm(keys):
    """Async: análisis mano-a-mano de las manos SELECCIONADAS (por key). Mirror de _coach_llm."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return {"error": "selecciona al menos una mano"}
    kid = tuple(sorted(keys))
    c = _hands_coach_cache
    if c.get("running") and c.get("kid") == kid:
        return {"running": True}
    if c.get("txt") and c.get("kid") == kid and time.time() - c["ts"] < 900:
        return {"text": c["txt"], "model": c.get("model"), "cached": True}
    if c.get("err") and c.get("kid") == kid and time.time() - c["ts"] < 30:
        return {"error": c["err"]}
    c.update(running=True, kid=kid, game=s7_api.curgame())
    threading.Thread(target=_hands_coach_compute, args=(keys,), daemon=True).start()
    return {"running": True}


def _coach_compute(hands, window=None):
    """The slow M3 coaching call (runs in a background thread; writes _coach_cache)."""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(HERE, "examples"))
        _sys.path.insert(0, HERE)
        os.environ["S7_STATS_DB"] = s7_api._dbpath(_coach_cache.get("game"))
        import s7_report
        import llm_system7
        rep = s7_report.report()
        try:
            meth = open(os.path.join(HERE, "system7_prompt.md")).read()
        except Exception:
            meth = ""
        cur = _deployed_knobs()                  # knobs del DEPLOY activo (no los defaults 6-max del dashboard)
        diag = _coach(window)
        win_txt = ("últimas %s manos" % window) if window not in (None, "all", "") else "todas las manos"
        leaks = "; ".join("%s: %s vs objetivo %s [%s]" % (o["k"], o["you"], o["target"], o["verdict"])
                          for o in (diag.get("vs_opt") or []))
        rivals = "; ".join("%s (VPIP %s/PFR %s/AF %s)" % (o.get("name"), o.get("vpip"), o.get("pfr"), o.get("af"))
                           for o in (diag.get("opp") or [])[:6]) or "(aún sin lecturas de rivales)"
        system = ("Eres un coach de NLHE HEADS-UP (HU) de élite (metodología EducaPoker / GTO node-locking). "
                  "Jugamos HU cash (~100bb+) contra los bots de la Playground S4. Te paso el diagnóstico "
                  "tu-juego-vs-óptimo-HU, la config actual y los RIVALES HU que enfrentamos. "
                  "(1) Análisis ACCIONABLE de leaks HU: SB (botón) abrir/robar/sizing, BB defensa+3bet+juego OOP, "
                  "postflop por posición; explota a los rivales según sus stats. "
                  "(2) Propón UNA versión nueva partiendo de la actual. Termina con SOLO un bloque ```json``` con las "
                  "claves a CAMBIAR: opening_ranges {SB:[tokens '22+','A2s+','KTo+'], BB:[...]} (en HU SOLO importan SB y BB), "
                  "threebet_value, threebet_bluff, knobs {open_size_bb,threebet_mult,value_eq,station_mult,cbet_bluff_frac,"
                  "commit_spr,perejil_flop,perejil_turn,perejil_relief}. Incluye solo lo que cambies. Español, MUY conciso.\n\n" + meth[:400])
        user = ("VENTANA: %s\nDIAGNÓSTICO vs óptimo HU: %s\nRIVALES HU: %s\nCONFIG ACTUAL knobs: %s\n\nINFORME:\n%s"
                % (win_txt, leaks or "(sin datos)", rivals, json.dumps(cur), rep))
        _cerr = None
        if s7_mllm is not None:
            _r = s7_mllm._chat(*_llm_route(), system, user, max_tokens=10000)
            txt, _cerr = (None if _r.get("error") else (_r.get("answer") or "")), _r.get("error")
        else:
            txt = llm_system7._minimax_call(system, user, 6000, os.environ.get("S7_MODEL", "MiniMax-M3"))
        if not txt:
            _coach_cache.update(running=False, ts=time.time(),
                                err=("M3: " + _cerr) if _cerr else "M3 no devolvió respuesta (tokens/clave).")
            return
        prose, proposal, version = txt, None, None
        m = re.search(r"```json\s*(\{.*\})\s*```", txt, re.S) or re.search(r"(\{.*\})\s*$", txt, re.S)
        if m:
            try:
                cfg = _validate_strat(json.loads(m.group(1)))
                if cfg and s7_strat:
                    version = "coach-" + time.strftime("%m%d-%H%M")
                    s7_strat.save(version, cfg)
                    proposal, prose = cfg, txt[:m.start()].strip()
            except Exception:
                pass
        _coach_cache.update(ts=time.time(), txt=prose, hands=hands, proposal=proposal, version=version,
                            err=None, running=False, window=window)
    except Exception as e:
        _coach_cache.update(running=False, err=str(e), ts=time.time())


def _coach_llm(window=None):
    """Non-blocking: returns the cached analysis, or kicks off a background M3 run and reports progress.
    A change of `window` invalidates the cache so the narrative re-runs for the new sample."""
    hands = 0
    try:
        cc = _ro()
        hands = cc.execute("select count(distinct hand_key) from decisions").fetchone()[0]
        cc.close()
    except Exception:
        pass
    if hands < COACH_NEED:
        return {"locked": True, "hands": hands, "need": COACH_NEED}
    c = _coach_cache
    if c.get("running"):
        return {"running": True, "hands": hands}
    if c.get("txt") and c.get("window") == window and time.time() - c["ts"] < 900:
        return {"text": c["txt"], "proposal": c.get("proposal"), "version": c.get("version"), "cached": True}
    if c.get("err") and c.get("window") == window and time.time() - c["ts"] < 30:
        return {"error": c["err"]}
    c["running"] = True
    c["window"] = window
    c["game"] = s7_api.curgame()
    threading.Thread(target=_coach_compute, args=(hands, window), daemon=True).start()
    return {"running": True, "hands": hands, "started": True}


def _expand_cfg(cfg):
    """Expand a strat cfg into the FULL 13×13 grid (explicit combos) for the 6 positions.
    Positions not in the cfg fall back to the chosen base (std/wide). Uses decide_system7
    as the single source of truth for ranges + token expansion (never re-implemented in JS)."""
    import sys as _sys
    _sys.path.insert(0, HERE)
    import decide_system7 as _D
    base = (cfg or {}).get("base")
    mode = (cfg or {}).get("mode")
    _cash = {"agr": _D.OPENING_RANGES_AGR, "nit": _D.OPENING_RANGES_NIT, "std": _D.OPENING_RANGES_STD}
    src = _cash.get(mode) or (_D.OPENING_RANGES_WIDE if base == "wide" else _D.OPENING_RANGES_STD)
    cr = (cfg or {}).get("opening_ranges") or {}
    ranges = {}
    for pos in _POS6:
        toks = cr.get(pos)
        if isinstance(toks, list):
            try:
                combos = _D._expand([str(t) for t in toks])
            except Exception:
                combos = set(src.get(pos, set()))
        else:
            combos = set(src.get(pos, set()))
        ranges[pos] = sorted(combos)
    return ranges


def _expand_tourn(cfg):
    """Expand tournament ranges per BB-bucket (defaults + tournament_ranges override) for the builder."""
    import sys as _sys
    _sys.path.insert(0, HERE)
    import decide_system7 as _D
    ov = (cfg or {}).get("tournament_ranges") or {}
    out = {}
    for b in ("deep", "mid", "short", "push"):
        bd = _D.TOURN_RANGES_DEFAULT.get(b, {})
        bov = ov.get(b) or {}
        ranges = {}
        for pos in _POS6:
            toks = bov.get(pos)
            if isinstance(toks, list):
                try:
                    combos = _D._expand([str(t) for t in toks])
                except Exception:
                    combos = set(bd.get(pos, set()))
            else:
                combos = set(bd.get(pos, set()))
            ranges[pos] = sorted(combos)
        out[b] = ranges
    return out


def _sizing_template(cfg):
    """Sizing matrix (texture×street bet fraction): engine default + knobs.sizing override."""
    import decide_system7 as _D
    siz = {t: dict(v) for t, v in _D.SIZING.items()}
    ov = ((cfg or {}).get("knobs") or {}).get("sizing")
    if isinstance(ov, dict):
        for t, sm in ov.items():
            if t in siz and isinstance(sm, dict):
                for s in sm:
                    if s in siz[t]:
                        try:
                            siz[t][s] = float(sm[s])
                        except Exception:
                            pass
    return siz


def _expand_classes(tokens):
    """Expand 3bet token lists ('TT+','AQs+','AQ') to the explicit hand classes the engine
    matches literally (decide_system7 uses threebet_value/bluff as a set of `cls`, not ranges)."""
    import sys as _sys
    _sys.path.insert(0, HERE)
    import decide_system7 as _D
    out = set()
    for t in (tokens or []):
        t = str(t).strip()
        if not t:
            continue
        base = t[:-1] if t.endswith("+") else t
        if t.endswith("+") and (len(base) == 3 or (len(base) == 2 and base[0] == base[1])):
            out |= _D._expand([t])                 # TT+ / AQs+ / KTo+
        elif len(base) == 3 or (len(base) == 2 and base[0] == base[1]):
            out.add(base)                          # explicit AQs / AKo / pair AA
        elif len(base) == 2:
            out |= {base + "s", base + "o"}        # bare AQ / AQ+ -> suited + offsuit
        else:
            out.add(base)
    return sorted(out)


def _strat_template(base="std", name="", mode="", game=""):
    """Seed the builder: full expanded ranges + knobs + 3bet for a fresh base (std/wide)
    or an existing saved strategy (loaded + expanded) so the user can edit it visually."""
    cfg = {}
    if name and s7_strat and name in s7_strat.names():
        cfg = s7_strat.load(name) or {}
        base = cfg.get("base") or base
    if base not in ("std", "wide"):
        base = "std"
    if not cfg:
        cfg = {"base": base}
    if mode in ("agr", "nit", "std"):
        cfg["mode"] = mode
    if game in ("cash", "tournament"):
        cfg["game"] = game
    knobs = dict(KN_DEFAULTS)
    knobs.update({k: v for k, v in (cfg.get("knobs") or {}).items()
                  if k in KN_DEFAULTS and isinstance(v, (int, float))})
    return {"name": name or "", "base": base,
            "game": cfg.get("game") if cfg.get("game") in ("cash", "tournament") else "cash",
            "mode": cfg.get("mode") if cfg.get("mode") in ("agr", "nit", "std") else "std",
            "bb_buckets": cfg.get("bb_buckets") if (isinstance(cfg.get("bb_buckets"), list) and len(cfg.get("bb_buckets")) == 3) else [40, 20, 10],
            "ranges": _expand_cfg(cfg), "tournament_ranges": _expand_tourn(cfg), "sizing": _sizing_template(cfg),
            "threebet_value": _expand_classes(cfg.get("threebet_value") or _DEF_3BV),
            "threebet_bluff": _expand_classes(cfg.get("threebet_bluff") or _DEF_3BB),
            "knobs": knobs, "knob_limits": {k: list(v) for k, v in KN_LIMITS.items()}}


def _save_strat(body):
    """Persist a user-named, visually-edited strategy. Explicit combos per position + knobs.
    Blocks reserved names so the std/fijo/wide baselines stay intact."""
    name = str(body.get("name", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,24}", name):
        return {"error": "nombre inválido (a-z 0-9 _ - , máx 24)"}
    if name in ("std", "fijo", "wide"):
        return {"error": "nombre reservado; elige otro"}
    if not s7_strat:
        return {"error": "s7_strat no disponible"}
    base = body.get("base") if body.get("base") in ("std", "wide") else "std"
    cfg = {"base": base}
    cfg["game"] = body.get("game") if body.get("game") in ("cash", "tournament") else "cash"
    cfg["mode"] = body.get("mode") if body.get("mode") in ("agr", "nit", "std") else "std"
    if isinstance(body.get("bb_buckets"), list):
        cfg["bb_buckets"] = body["bb_buckets"]
    if isinstance(body.get("tournament_ranges"), dict):
        cfg["tournament_ranges"] = body["tournament_ranges"]
    orr = body.get("opening_ranges")
    if isinstance(orr, dict):
        cfg["opening_ranges"] = {p: v for p, v in orr.items() if isinstance(v, list)}
    for k in ("threebet_value", "threebet_bluff"):
        if isinstance(body.get(k), list):
            cfg[k] = _expand_classes(body[k])
    if isinstance(body.get("knobs"), dict):
        cfg["knobs"] = body["knobs"]
    clean = _validate_strat(cfg) or {}
    clean["base"] = base
    clean["game"] = cfg["game"]
    clean["mode"] = cfg["mode"]
    try:
        s7_strat.save(name, clean)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "name": name,
            "positions": {p: len(v) for p, v in (clean.get("opening_ranges") or {}).items()}}


def _stratgen_compute(window, mode):
    """Slow M3 call (background thread): a pro-player persona designs a COMPLETE strategy.
    mode='leaks' fixes the diagnosed leaks of the window; mode='scratch' = ideal from zero.
    Writes the editable proposal to _stratgen_cache; does NOT save (the user names + edits)."""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(HERE, "examples"))
        _sys.path.insert(0, HERE)
        os.environ["S7_STATS_DB"] = s7_api._dbpath(_stratgen_cache.get("game"))
        import llm_system7
        persona = (
            "Eres un jugador profesional de NLHE HEADS-UP (HU) online, ganador, con millones de manos y dominio "
            "de rangos GTO y de explotación en HU. Diseña una estrategia COMPLETA para un bot de HU cash 100bb+. "
            "Devuelve un resumen de 1-2 frases y DESPUÉS SOLO un bloque ```json``` con TODAS estas claves: "
            "opening_ranges con SB y BB (en HU SOLO juegan esas dos: SB=botón que abre, BB=defensa; cada una lista de "
            "tokens '22+','A2s+','KTo+' o combos 'AKs'); threebet_value (lista); threebet_bluff (lista); y knobs "
            "{open_size_bb,threebet_mult,value_eq,station_mult,cbet_bluff_frac,commit_spr,perejil_flop,perejil_turn,"
            "perejil_relief}. Rangos HU coherentes: SB abre ~80-90% (muy ancho), BB defiende ~60-70% (call+3bet). Español, conciso.")
        if mode == "scratch":
            user = ("Crea tu estrategia IDEAL de jugador pro de HEADS-UP cash 100bb+ contra los bots de la Playground. "
                    "No mires histórico: dame el rango del SB (botón, abre ancho) y el del BB (defensa), y tus knobs óptimos para HU.")
        else:
            diag = _coach(window)
            cur = _deployed_knobs()              # knobs del DEPLOY activo (no los defaults 6-max del dashboard)
            leaks = "; ".join("%s: tú %s vs objetivo %s [%s]" % (o["k"], o["you"], o["target"], o["verdict"])
                              for o in (diag.get("vs_opt") or []))
            adv = " ".join(diag.get("advice") or [])
            rivals = "; ".join("%s (VPIP %s/PFR %s)" % (o.get("name"), o.get("vpip"), o.get("pfr"))
                               for o in (diag.get("opp") or [])[:6]) or "(aún sin lecturas)"
            wt = ("últimas %s manos" % window) if window not in (None, "all", "") else "todas las manos"
            user = ("Parte de esta config y ARREGLA estos leaks de HU (ventana=%s).\n"
                    "CONFIG knobs actual: %s\nDIAGNÓSTICO vs óptimo HU: %s\nRIVALES HU: %s\nCONSEJOS: %s\n"
                    "Devuelve la estrategia HU corregida completa (rangos de SB y BB)." % (wt, json.dumps(cur), leaks, rivals, adv))
        txt, cerr = None, None
        if s7_mllm is not None:
            r = s7_mllm._chat(*_llm_route(), persona, user, max_tokens=10000)
            txt, cerr = (None if r.get("error") else (r.get("answer") or "")), r.get("error")
        else:
            txt = llm_system7._minimax_call(persona, user, 6000, os.environ.get("S7_MODEL", "MiniMax-M3"))
        if not txt:
            _stratgen_cache.update(running=False, ts=time.time(),
                                   err=("M3: " + cerr) if cerr else "M3 no devolvió respuesta (tokens/clave).")
            return
        m = re.search(r"```json\s*(\{.*\})\s*```", txt, re.S) or re.search(r"(\{.*\})\s*$", txt, re.S)
        cfg = None
        if m:
            try:
                cfg = _validate_strat(json.loads(m.group(1)))
            except Exception:
                cfg = None
        if not cfg:
            _stratgen_cache.update(running=False, ts=time.time(),
                                   err="No pude extraer una estrategia válida de la respuesta del modelo.")
            return
        prose = (txt[:m.start()].strip() if m else "")[:1200]
        base = "std"
        knobs = dict(KN_DEFAULTS)
        knobs.update({k: v for k, v in (cfg.get("knobs") or {}).items()
                      if k in KN_DEFAULTS and isinstance(v, (int, float))})
        data = {"ranges": _expand_cfg({"base": base, "opening_ranges": cfg.get("opening_ranges")}),
                "threebet_value": _expand_classes(cfg.get("threebet_value") or _DEF_3BV),
                "threebet_bluff": _expand_classes(cfg.get("threebet_bluff") or _DEF_3BB),
                "knobs": knobs, "knob_limits": {k: list(v) for k, v in KN_LIMITS.items()},
                "base": base, "prose": prose, "mode": mode}
        _stratgen_cache.update(running=False, ts=time.time(), err=None, data=data, window=window, mode=mode)
    except Exception as e:
        _stratgen_cache.update(running=False, err=str(e), ts=time.time())


def _stratgen_llm(window=None, mode="leaks"):
    """Non-blocking orchestrator for the pro-player strategy generator (mirrors _coach_llm)."""
    if mode not in ("leaks", "scratch"):
        mode = "leaks"
    if mode == "leaks":
        hands = 0
        try:
            cc = _ro()
            hands = cc.execute("select count(distinct hand_key) from decisions").fetchone()[0]
            cc.close()
        except Exception:
            pass
        if hands < COACH_NEED:
            return {"locked": True, "hands": hands, "need": COACH_NEED}
    c = _stratgen_cache
    if c.get("running"):
        return {"running": True}
    if c.get("data") and c.get("window") == window and c.get("mode") == mode and time.time() - c["ts"] < 900:
        return dict(c["data"], cached=True)
    if c.get("err") and time.time() - c["ts"] < 30:
        return {"error": c["err"]}
    c.update(running=True, window=window, mode=mode, game=s7_api.curgame())
    threading.Thread(target=_stratgen_compute, args=(window, mode), daemon=True).start()
    return {"running": True, "started": True}


def _runs():
    """Active trainings: fixed arms (systemd only) + transient arena-run-* + per-label progress."""
    out = []
    if s7_jobs.BACKEND == "systemd":            # fixed A/B arms are systemd units; omit in Docker
        out = [{"unit": "arena-test", "label": "std", "ranges": "std", "engine": "hybrid", "state": _svc("arena-test"), "fixed": True},
               {"unit": "arena-test-wide", "label": "wide", "ranges": "wide", "engine": "hybrid", "state": _svc("arena-test-wide"), "fixed": True}]
    try:
        for j in s7_jobs.list_jobs():
            out.append({"unit": j["unit"], "label": j["label"],
                        "ranges": "?", "engine": "?", "state": j["state"], "fixed": False})
    except Exception:
        pass
    seen = {o["label"] for o in out}            # keep claimable clasificatorias visible even if the job was GC'd
    try:
        for fn in sorted(os.listdir(CLASIF_DIR)):
            if fn.endswith(".json") and fn[:-5] not in seen:
                lbl = fn[:-5]
                out.append({"unit": "arena-run-" + lbl, "label": lbl,
                            "ranges": "wide" if "wide" in lbl else "?", "engine": "?",
                            "state": _svc("arena-run-" + lbl), "fixed": False, "claimable": True})
    except Exception:
        pass
    prog = {}
    try:
        c = _ro()
        for lbl, n, m in c.execute("select run_label,count(*),avg(adjusted_bb100) from runs group by run_label"):
            prog[lbl] = {"matches": n, "bb100": round(m, 1) if m is not None else None}
        c.close()
    except Exception:
        pass
    for o in out:
        o.update(prog.get(o["label"], {"matches": 0, "bb100": None}))
    return {"runs": out}


def _strats():
    names = s7_strat.names() if s7_strat else []
    res = {}
    try:
        c = _ro()
        for lbl, n, m in c.execute("select run_label,count(*),avg(adjusted_bb100) from runs group by run_label"):
            res[lbl] = {"runs": n, "bb100": round(m, 1) if m is not None else None}
        c.close()
    except Exception:
        pass
    return {"strats": [{"name": nm, "runs": res.get(nm, {}).get("runs", 0),
                        "bb100": res.get(nm, {}).get("bb100")} for nm in names]}


def _claim(label):
    """Fetch the dev.fun claim URL for a saved clasificatoria agent (links it to the user's account)."""
    if not re.fullmatch(r"[a-z0-9_-]{1,24}", label or ""):
        return {"error": "label inválida"}
    creds = None
    try:
        with open(os.path.join(CLASIF_DIR, label + ".json"), encoding="utf-8") as f:
            creds = json.load(f)
    except Exception:
        creds = None
    if creds is None:                       # agente PvP (Playground/torneo): juega con la identidad compartida
        try:
            dep = s7_api._jload(s7_api._DEPLOYS_PATH, {}).get(label, {})
        except Exception:
            dep = {}
        comp = str(dep.get("competition") or "")
        if dep and comp not in ("eval", "seed_poker_eval_s1", ""):
            try:
                with open(os.path.join(s7_api._DATA, ".arena-pg-credentials"), encoding="utf-8") as f:
                    creds = json.load(f)
            except Exception:
                creds = None
    if creds is None:
        return {"error": "sin credenciales para '" + str(label) + "'"}
    try:
        import urllib.request
        req = urllib.request.Request("https://arena.dev.fun/api/arena/auth/claim/status",
                                     headers={"x-arena-api-key": creds.get("apiKey", "")})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"error": "fallo al consultar claim: " + str(e)[:200]}
    url = None
    if isinstance(data, dict):
        url = (data.get("claimUrl") or data.get("url") or data.get("claim_url")
               or (data.get("data") or {}).get("claimUrl"))
    return {"label": label, "agentId": creds.get("agentId"), "name": creds.get("name"),
            "claim_url": url, "raw": data}


MLLM_PRESETS = [
    {"id": "minimax:MiniMax-M3", "provider": "minimax", "label": "MiniMax M3 (actual)"},
    {"id": "openrouter:openai/gpt-4o", "provider": "openrouter", "label": "GPT-4o"},
    {"id": "openrouter:openai/gpt-4.1", "provider": "openrouter", "label": "GPT-4.1"},
    {"id": "openrouter:anthropic/claude-3.7-sonnet", "provider": "openrouter", "label": "Claude 3.7 Sonnet"},
    {"id": "openrouter:deepseek/deepseek-r1", "provider": "openrouter", "label": "DeepSeek R1"},
    {"id": "openrouter:google/gemini-2.5-pro", "provider": "openrouter", "label": "Gemini 2.5 Pro"},
    {"id": "openrouter:meta-llama/llama-3.3-70b-instruct", "provider": "openrouter", "label": "Llama 3.3 70B"},
    {"id": "openrouter:qwen/qwen-2.5-72b-instruct", "provider": "openrouter", "label": "Qwen 2.5 72B"},
    {"id": "xiaomi:MiMo-7B-RL", "provider": "xiaomi", "label": "Xiaomi MiMo"},
    {"id": "minimax:MiniMax-M2", "provider": "minimax", "label": "MiniMax M2 (rápido · vivo)"},
    {"id": "deepseek:deepseek-chat", "provider": "deepseek", "label": "DeepSeek V3 (rápido)"},
    {"id": "deepseek:deepseek-reasoner", "provider": "deepseek", "label": "DeepSeek R1 (lento)"},
]


def _mllm_models():
    prov = {p: bool(s7_mllm and s7_mllm.provider_ready(p)) for p in ("minimax", "openrouter", "xiaomi", "deepseek")}
    return {"presets": MLLM_PRESETS, "providers": prov}


def _mllm_runs():
    out = []
    try:
        c = _ro()
        for rid, ts, status, models, judge, nh, nr in c.execute(
                "select run_id,ts,status,models,judge,n_hands,n_reps from mllm_runs order by ts desc limit 50"):
            n = c.execute("select count(*) from mllm_results where run_id=?", (rid,)).fetchone()[0]
            out.append({"run_id": rid, "ts": ts, "status": status, "models": _lj(models) or [],
                        "judge": judge, "n_hands": nh, "n_reps": nr, "results": n})
        c.close()
    except Exception:
        pass
    return {"runs": out}


def _mllm_results(run):
    from collections import Counter, defaultdict
    try:
        c = _ro()
        rows = c.execute(
            "select model,provider,hand_key,rep,action,amount,valid,latency_ms,prompt_tokens,"
            "completion_tokens,reasoning,judge_score,judge_note,m3_action from mllm_results "
            "where run_id=?", (run,)).fetchall()
        c.close()
    except Exception as e:
        return {"error": str(e)}
    by_hand = defaultdict(list)
    for r in rows:
        by_hand[r[2]].append(r)
    consensus = {}
    for hk, rs in by_hand.items():
        cnt = Counter(r[4] for r in rs if r[6] and r[4])
        consensus[hk] = cnt.most_common(1)[0][0] if cnt else None
    agg = defaultdict(lambda: {"n": 0, "valid": 0, "lat": [], "tok": [], "judge": [], "vsM3": 0,
                               "vsCons": 0, "actions": Counter(), "byhand": defaultdict(Counter)})
    for model, prov, hk, rep, action, amount, valid, lat, pt, ct, reason, js, jn, m3a in rows:
        a = agg[model]
        a["n"] += 1
        if valid:
            a["valid"] += 1
            a["actions"][action] += 1
            a["byhand"][hk][action] += 1
            if action == m3a:
                a["vsM3"] += 1
            if action == consensus.get(hk):
                a["vsCons"] += 1
        if lat is not None:
            a["lat"].append(lat)
        if pt or ct:
            a["tok"].append((pt or 0) + (ct or 0))
        if js is not None:
            a["judge"].append(js)
    models = []
    for m, x in agg.items():
        cf = []
        for hk, cnt in x["byhand"].items():
            tot = sum(cnt.values())
            if tot:
                cf.append(max(cnt.values()) / tot)
        avg = lambda L: round(sum(L) / len(L), 2) if L else None
        models.append({
            "model": m, "n": x["n"],
            "valid_pct": round(100 * x["valid"] / x["n"], 1) if x["n"] else None,
            "selfcons_pct": round(100 * sum(cf) / len(cf), 1) if cf else None,
            "vsM3_pct": round(100 * x["vsM3"] / x["valid"], 1) if x["valid"] else None,
            "vsCons_pct": round(100 * x["vsCons"] / x["valid"], 1) if x["valid"] else None,
            "judge_avg": avg(x["judge"]),
            "lat_ms": round(sum(x["lat"]) / len(x["lat"])) if x["lat"] else None,
            "tokens": round(sum(x["tok"]) / len(x["tok"])) if x["tok"] else None,
            "actions": dict(x["actions"])})
    models.sort(key=lambda z: (z["judge_avg"] is None, -(z["judge_avg"] if z["judge_avg"] is not None else -1)))
    hands = []
    for hk, rs in by_hand.items():
        per = {}
        for r in rs:
            mdl = r[0]
            if mdl not in per or (r[6] and not per[mdl]["valid"]):
                per[mdl] = {"action": r[4], "amount": r[5], "valid": bool(r[6]),
                            "reasoning": r[10], "judge": r[11], "note": r[12]}
        hands.append({"hand_key": hk, "m3_action": rs[0][13], "consensus": consensus.get(hk), "per": per})
    return {"run": run, "models": models, "hands": hands[:80]}


def _rank():
    """Agentes lanzados, etiquetados por tipo: Eval (cred reclamable en .clasif) + deploys PvP
    (Playground/Torneo, de prod_deploys). Así no se confunde un resultado de Eval con juego PvP."""
    prog, hands = {}, {}
    try:
        c = _ro()
        for lbl, m in c.execute("select run_label,avg(adjusted_bb100) from runs group by run_label"):
            prog[lbl] = round(m, 1) if m is not None else None
        for lbl, h in c.execute("select run_label,count(distinct hand_key) from decisions group by run_label"):
            hands[lbl] = h
        c.close()
    except Exception:
        pass
    creds = {}
    try:
        for fn in sorted(os.listdir(CLASIF_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(CLASIF_DIR, fn), encoding="utf-8") as f:
                        creds[fn[:-5]] = json.load(f)
                except Exception:
                    creds[fn[:-5]] = {}
    except Exception:
        pass
    try:
        deploys = s7_api._jload(s7_api._DEPLOYS_PATH, {})
    except Exception:
        deploys = {}
    _names = {"cmqf827h30u7dfca3x2aqvzjv": "Playground", "cmqggiv9k37am11ydmppz466e": "Torneo"}
    my = _my_agent_id()
    rows = []
    for lbl in set(creds) | set(deploys):
        cr = creds.get(lbl, {})
        dp = deploys.get(lbl, {})
        if not ((lbl in deploys) or (my and cr.get("agentId") == my)):   # solo el agente autenticado
            continue
        comp = str(cr.get("competition") or dp.get("competition") or "")
        is_eval = comp == "seed_poker_eval_s1" or "eval" in comp.lower()
        rows.append({"label": lbl, "name": dp.get("agent") or cr.get("name") or lbl,
                     "agentId": cr.get("agentId"), "strategy": cr.get("strat") or dp.get("agent") or "?",
                     "engine": cr.get("engine") or "", "hands": hands.get(lbl, 0), "bb100": prog.get(lbl),
                     "kind": "eval" if is_eval else "pvp",
                     "type": "Eval" if is_eval else _names.get(comp, dp.get("compname") or "PvP"),
                     "claimable": (lbl in creds) or (not is_eval and bool(dp)), "ts": cr.get("ts") or dp.get("ts")})
    rows.sort(key=lambda r: (r["kind"] != "eval", r["bb100"] is None,
                             -(r["bb100"] if r["bb100"] is not None else -1e9)))
    return {"agents": rows}


def _live():
    """Lightweight live counters for the header (cheap, polled ~1s)."""
    try:
        c = _ro()
        dec = c.execute("select count(*) from decisions").fetchone()[0]
        h = c.execute("select count(distinct hand_key) from decisions").fetchone()[0]
        m3 = c.execute("select count(*) from decisions where engine='M3'").fetchone()[0]
        c.close()
        return {"hands": h, "decisions": dec, "m3": m3}
    except Exception as e:
        return {"error": str(e)}


def state_cached():
    g = s7_api.curgame()
    with _lock:
        ent = _cache.get(g)
        if not ent or time.time() - ent["ts"] > 2:
            ent = {"data": _state(), "ts": time.time()}
            _cache[g] = ent
        return ent["data"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(b)

    _CT = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
           ".js": "application/javascript; charset=utf-8", ".json": "application/json",
           ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2", ".png": "image/png"}

    def _serve_static(self, urlpath):
        """Serve web/ static files (the new 3-zone frontend). Path-traversal safe."""
        rel = urlpath.split("?", 1)[0].lstrip("/")
        base = os.path.join(HERE, "web")
        full = os.path.normpath(os.path.join(HERE, rel))
        if full != base and not full.startswith(base + os.sep):
            self.send_response(403); self.end_headers(); return
        if not os.path.isfile(full):
            self.send_response(404); self.end_headers(); return
        try:
            with open(full, "rb") as f:
                body = f.read()
        except Exception:
            self.send_response(500); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", self._CT.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        from urllib.parse import urlparse as _up, parse_qs as _pq
        s7_api.set_game(_pq(_up(p).query).get("game", ["cash"])[0])
        if p == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            return
        if p.startswith("/api/agents"):
            self._send(json.dumps(s7_api.agents_list(), default=str), "application/json")
            return
        if p.startswith("/api/lab/groups"):
            self._send(json.dumps(s7_api.lab_groups(), default=str), "application/json")
            return
        if p.startswith("/api/lab/task"):
            from urllib.parse import urlparse, parse_qs
            ag = parse_qs(urlparse(p).query).get("agent", [""])[0]
            self._send(json.dumps(s7_api.lab_task(ag), default=str), "application/json")
            return
        if p.startswith("/api/lab/report"):
            from urllib.parse import urlparse, parse_qs
            ag = parse_qs(urlparse(p).query).get("agent", [""])[0]
            self._send(json.dumps(s7_api.lab_report(ag), default=str), "application/json")
            return
        if p.startswith("/api/production/competitions"):
            self._send(json.dumps(s7_api.production_competitions(s7_api.curgame()), default=str), "application/json")
            return
        if p.startswith("/api/production/account"):
            self._send(json.dumps(s7_api.production_account(), default=str), "application/json")
            return
        if p.startswith("/api/production/session"):
            from urllib.parse import urlparse, parse_qs
            lb = parse_qs(urlparse(p).query).get("label", [""])[0]
            self._send(json.dumps(s7_api.production_session(lb), default=str), "application/json")
            return
        if p.startswith("/api/production/status"):
            self._send(json.dumps(s7_api.production_status(), default=str), "application/json")
            return
        if p.startswith("/api/tracker/opponents"):
            self._send(json.dumps(s7_api.tracker_opponents(), default=str), "application/json")
            return
        if p.startswith("/api/opponent"):
            _oid = _pq(_up(p).query).get("id", [""])[0]
            self._send(json.dumps(s7_api.opponent_detail(_oid), default=str), "application/json")
            return
        if p.startswith("/api/tracker/own"):
            self._send(json.dumps(s7_api.tracker_own(), default=str), "application/json")
            return
        if p.startswith("/api/players"):
            self._send(json.dumps(_players(), default=str), "application/json")
            return
        if p.startswith("/api/hands"):
            self._send(json.dumps(_hands(), default=str), "application/json")
            return
        if p.startswith("/api/hand"):
            from urllib.parse import urlparse, parse_qs
            key = parse_qs(urlparse(p).query).get("key", [""])[0]
            self._send(json.dumps(_hand(key), default=str), "application/json")
            return
        if p.startswith("/api/coach/strategy"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(p).query)
            win = qs.get("window", ["all"])[0]
            win = None if win in ("all", "", None) else win
            mode = qs.get("mode", ["leaks"])[0]
            self._send(json.dumps(_stratgen_llm(win, mode), default=str), "application/json")
            return
        if p.startswith("/api/coach/llm"):
            from urllib.parse import urlparse, parse_qs
            win = parse_qs(urlparse(p).query).get("window", ["all"])[0]
            win = None if win in ("all", "", None) else win
            self._send(json.dumps(_coach_llm(win), default=str), "application/json")
            return
        if p.startswith("/api/coach/hands-llm"):
            from urllib.parse import urlparse, parse_qs
            ks = parse_qs(urlparse(p).query).get("keys", [""])[0]
            self._send(json.dumps(_hands_coach_llm([k for k in ks.split(",") if k]), default=str), "application/json")
            return
        if p.startswith("/api/coach/hands"):
            from urllib.parse import urlparse, parse_qs
            sc = parse_qs(urlparse(p).query).get("scope", ["lifetime"])[0]
            self._send(json.dumps(_coach_hands("session" if sc == "session" else "lifetime"), default=str), "application/json")
            return
        if p.startswith("/api/coach"):
            from urllib.parse import urlparse, parse_qs
            win = parse_qs(urlparse(p).query).get("window", ["all"])[0]
            win = None if win in ("all", "", None) else win
            self._send(json.dumps(_coach(win), default=str), "application/json")
            return
        if p.startswith("/api/run/log"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(p).query)
            unit = qs.get("unit", [""])[0]
            try:
                n = max(5, min(300, int(qs.get("n", ["80"])[0])))
            except Exception:
                n = 80
            if not re.fullmatch(r"arena-(run-[a-z0-9_-]{1,24}|test|test-wide)(\.service)?", unit):
                self._send(json.dumps({"error": "unit inválida"}), "application/json")
                return
            self._send(json.dumps({"unit": unit, "log": s7_jobs.logs(unit, n)}), "application/json")
            return
        if p.startswith("/api/strats/template"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(p).query)
            base = qs.get("base", ["std"])[0]
            name = qs.get("name", [""])[0]
            mode = qs.get("mode", [""])[0]
            gametype = qs.get("gametype", [""])[0]
            self._send(json.dumps(_strat_template(base, name, mode, gametype), default=str), "application/json")
            return
        if p.startswith("/api/strats"):
            self._send(json.dumps(_strats(), default=str), "application/json")
            return
        if p.startswith("/api/claim"):
            from urllib.parse import urlparse, parse_qs
            lab = parse_qs(urlparse(p).query).get("label", [""])[0]
            self._send(json.dumps(_claim(lab), default=str), "application/json")
            return
        if p.startswith("/api/runs"):
            self._send(json.dumps(_runs(), default=str), "application/json")
            return
        if p.startswith("/api/mllm/models"):
            self._send(json.dumps(_mllm_models(), default=str), "application/json")
            return
        if p.startswith("/api/settings"):
            _s = s7_api.settings_get(); _s.update(_mllm_models())   # live/default/keys + presets/providers
            self._send(json.dumps(_s, default=str), "application/json")
            return
        if p.startswith("/api/mllm/runs"):
            self._send(json.dumps(_mllm_runs(), default=str), "application/json")
            return
        if p.startswith("/api/mllm/results"):
            from urllib.parse import urlparse, parse_qs
            run = parse_qs(urlparse(p).query).get("run", [""])[0]
            self._send(json.dumps(_mllm_results(run), default=str), "application/json")
            return
        if p.startswith("/api/rank"):
            self._send(json.dumps(_rank(), default=str), "application/json")
            return
        if p.startswith("/api/live"):
            self._send(json.dumps(_live(), default=str), "application/json")
            return
        if p.startswith("/api/state"):
            self._send(json.dumps(state_cached(), default=str), "application/json")
            return
        if p.startswith("/web/"):
            self._serve_static(p)
            return
        if p == "/" or p.startswith("/index"):
            self._serve_static("/web/index.html")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        p = self.path
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or "{}") if ln else {}
        except Exception:
            body = {}
        s7_api.set_game(body.get("game", "cash"))

        def reply(o):
            self._send(json.dumps(o), "application/json")

        if p.startswith("/api/agent/save"):
            reply(s7_api.save_agent(body)); return
        if p.startswith("/api/agent/delete"):
            reply(s7_api.delete_agent(body.get("name"))); return
        if p.startswith("/api/rank/delete"):
            reply(s7_api.rank_delete(body)); return
        if p.startswith("/api/lab/stop"):
            reply(s7_api.lab_stop(body)); return
        if p.startswith("/api/lab/eval"):
            reply(s7_api.lab_eval(body)); return
        if p.startswith("/api/production/queue-remove"):
            reply(s7_api.production_queue_remove(body)); return
        if p.startswith("/api/production/deploy"):
            reply(s7_api.production_deploy(body)); return
        if p.startswith("/api/production/stop"):
            reply(s7_api.production_stop(body)); return
        if p.startswith("/api/tracker/harvest"):
            reply(s7_api.tracker_harvest()); return
        if p.startswith("/api/settings/key"):
            reply(s7_api.settings_save_key(body)); return
        if p.startswith("/api/settings/model"):
            reply(s7_api.settings_set_model(body)); return
        if p.startswith("/api/settings/apply"):
            reply(s7_api.settings_apply_live(body)); return
        if p.startswith("/api/strats/save"):
            reply(_save_strat(body))
            return
        if p.startswith("/api/run/stop"):
            unit = str(body.get("unit", ""))
            if re.fullmatch(r"arena-(run-[a-z0-9_-]{1,24}|test|test-wide)(\.service)?", unit):
                try:
                    s7_jobs.stop(unit)
                    reply({"ok": True})
                except Exception as e:
                    reply({"error": str(e)})
            else:
                reply({"error": "unit inválida"})
            return
        if p.startswith("/api/run/clean"):
            mode = str(body.get("mode", ""))
            if mode not in ("failed", "completed", "stopall", "small"):
                reply({"error": "mode inválido"}); return
            units = [(j["unit"], j["state"]) for j in s7_jobs.list_jobs()]
            if mode == "small":                    # borrar runs NO activas con < 50 manos (job + sus datos)
                done, purged = 0, []
                try:
                    wc = sqlite3.connect(s7_api._dbpath(), timeout=10)
                    wc.execute("PRAGMA busy_timeout=8000")
                except Exception as e:
                    reply({"error": "db: " + str(e)}); return
                for u, active in units:
                    if active in ("active", "activating"):
                        continue
                    label = u[len("arena-run-"):].replace(".service", "")
                    if label == "playground":          # label compartido de los deploys PvP: borrar por run_label arrasaría TODAS las seasons
                        continue
                    try:
                        h = wc.execute("select count(distinct hand_key) from decisions where run_label=?", (label,)).fetchone()[0]
                    except Exception:
                        h = 0
                    if h >= 50:
                        continue
                    s7_jobs.stop(u)
                    s7_jobs.cleanup(u)
                    try:
                        for _t in ("decisions", "equity", "runs"):
                            wc.execute("delete from " + _t + " where run_label=?", (label,))
                        wc.commit()
                    except Exception:
                        pass
                    try:
                        os.remove(os.path.join(CLASIF_DIR, label + ".json"))
                    except Exception:
                        pass
                    purged.append(label); done += 1
                wc.close()
                reply({"ok": True, "mode": mode, "count": done, "labels": purged})
                return
            want = {"failed": ("failed",), "completed": ("inactive",), "stopall": ("active", "activating")}[mode]
            done = 0
            for u, active in units:
                if active not in want:
                    continue
                s7_jobs.stop(u)
                if mode != "stopall":
                    s7_jobs.cleanup(u)
                done += 1
            reply({"ok": True, "mode": mode, "count": done})
            return
        if p.startswith("/api/mllm/run"):
            models = body.get("models") or []
            judge = str(body.get("judge", "")).strip()
            try:
                hands = max(1, min(200, int(body.get("hands", 10))))
            except Exception:
                hands = 10
            try:
                reps = max(1, min(20, int(body.get("reps", 3))))
            except Exception:
                reps = 3
            if not isinstance(models, list) or not models:
                reply({"error": "selecciona al menos un modelo"}); return
            clean = [str(m).strip() for m in models if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", str(m).strip())]
            if not clean:
                reply({"error": "modelos inválidos"}); return
            if judge and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", judge):
                reply({"error": "juez inválido"}); return
            rid = "mllm-" + time.strftime("%m%d-%H%M%S")
            argv = s7_jobs.pyrun("s7_mllm.py", "--run-id", rid, "--models", ",".join(clean),
                                 "--hands", hands, "--reps", reps)
            if judge:
                argv += ["--judge", judge]
            try:
                unit = s7_jobs.launch(rid, argv, {"S7_STATS_DB": DB})
            except Exception as e:
                reply({"error": str(e)[:300]}); return
            reply({"ok": True, "run_id": rid, "unit": unit})
            return
        if p.startswith("/api/run/batch"):
            strat = str(body.get("strat", "")).strip().lower()
            engine = str(body.get("engine", "hybrid"))
            try:
                total = max(1, min(300, int(body.get("total", 20))))
            except Exception:
                total = 20
            try:
                maxc = max(1, min(8, int(body.get("maxc", 4))))
            except Exception:
                maxc = 4
            if engine not in ("hybrid", "heur"):
                reply({"error": "engine inválido"}); return
            if not strat or (s7_strat and strat not in s7_strat.names()):
                reply({"error": "versión inválida o inexistente"}); return
            tag = "b" + ("%05x" % (int(time.time() * 1000) % (16 ** 5)))
            argv = s7_jobs.pyrun("s7_batch.py", total, maxc, strat, engine, tag)
            try:
                unit = s7_jobs.launch("batch-" + tag, argv, {"S7_STATS_DB": DB})
            except Exception as e:
                reply({"error": str(e)[:300]}); return
            reply({"ok": True, "unit": unit, "tag": tag, "total": total})
            return
        if p.startswith("/api/run"):
            label = str(body.get("label", "")).strip().lower()
            ranges, engine = str(body.get("ranges", "std")), str(body.get("engine", "hybrid"))
            try:
                matches = max(1, min(200, int(body.get("matches", 10))))
            except Exception:
                matches = 10
            strat = str(body.get("strat", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,24}", label):
                reply({"error": "label inválida (a-z 0-9 _ - , máx 24)"}); return
            if ranges not in ("std", "wide") or engine not in ("hybrid", "heur"):
                reply({"error": "ranges/engine inválidos"}); return
            if strat and (not re.fullmatch(r"[a-z0-9_-]{1,24}", strat) or (s7_strat and strat not in s7_strat.names())):
                reply({"error": "strat inválida o inexistente"}); return
            name = str(body.get("name", "")).strip()
            if name and not re.fullmatch(r"[A-Za-z0-9 ._-]{1,32}", name):
                reply({"error": "nombre inválido (A-Z 0-9 espacio . _ - , máx 32)"}); return
            env = {"S7_STATS_DB": DB, "S7_RUN_LABEL": label, "S7_RANGES": ranges}
            if strat:
                env["S7_STRAT"] = strat
            if name:
                env["S7_AGENT_NAME"] = name
            if label.startswith("clasif"):        # clasificatoria → persist creds for the claim flow
                env["S7_SAVE_CREDS"] = "1"
            try:
                mt = int(body.get("max_tokens") or 0)
                if mt > 0:
                    env["S7_MAX_TOKENS"] = str(mt)
            except Exception:
                pass
            try:
                md = int(body.get("min_deadline") or 0)
                if md > 0:
                    env["S7_LLM_MIN_DEADLINE"] = str(md)
            except Exception:
                pass
            argv = s7_jobs.pyrun("s7_test.py", "--engine", engine, "--matches", matches)
            try:
                unit = s7_jobs.launch(label, argv, env)
            except Exception as e:
                reply({"error": str(e)[:300]}); return
            reply({"ok": True, "unit": unit})
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"[s7-dash] serving http://0.0.0.0:{PORT}  db={DB}", flush=True)
    threading.Thread(target=s7_api.queue_loop, daemon=True).start()   # procesa la cola de producción (1 a la vez)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
