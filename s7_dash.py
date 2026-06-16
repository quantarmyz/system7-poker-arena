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
CLASIF_DIR = os.environ.get("S7_CLASIF_DIR", os.path.join(HERE, ".clasif"))
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)


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

    runs = q("select run_label,hands,adjusted_bb100,m3_calls,ts from runs where hands>=400 order by ts")

    def arm(is_wide):
        xs = [r for r in runs if (r[0] == "wide") == is_wide]
        bb = [r[2] for r in xs if r[2] is not None]
        mean = sum(bb) / len(bb) if bb else None
        se = (sum((x - mean) ** 2 for x in bb) / len(bb)) ** 0.5 / (len(bb) ** 0.5) if bb and len(bb) > 1 else None
        return {"n": len(xs), "mean": round(mean, 1) if mean is not None else None,
                "ci": round(1.96 * se, 1) if se else None,
                "series": [round(x, 1) for x in bb], "last": round(bb[-1], 1) if bb else None}

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
    eqrows = q("select run_label,hands,raw_chips,adj_chips from equity order by ts")
    equity = {}
    for lbl in {r[0] for r in eqrows}:
        seg = [r for r in eqrows if r[0] == lbl]
        # stitch sawtooth: the cumulative counters reset to ~0 on each service restart
        stitched = []
        bh = braw = badj = 0.0   # base offset = sum of previous segments' ends
        lh = lraw = ladj = 0.0   # last stitched point
        ph = None
        for (_, h, rw, ad) in seg:
            h = h or 0; rw = rw or 0; ad = ad or 0
            if ph is not None and h < ph:      # reset -> new segment
                bh, braw, badj = lh, lraw, ladj
            lh, lraw, ladj = bh + h, braw + rw, badj + ad
            stitched.append((lh, lraw, ladj))
            ph = h
        step = max(1, len(stitched) // 160)
        equity[lbl] = [{"h": p[0], "raw": round(p[1], 1), "adj": round(p[2], 1)} for p in stitched[::step]]
    c.close()
    return {
        "ts": time.time(), "hands": hands, "decisions": total, "m3": m3, "heur": eng.get("heur", 0),
        "m3pct": round(100 * m3 / total, 1) if total else 0,
        "ab": {"std": arm(False), "wide": arm(True)},
        "classes": classes, "ranks": list(RANKS), "bypos": bypos,
        "streets": streets, "strength": strength, "m3street": m3street, "m3str": m3str,
        "arch": arch, "recent": recent, "equity": equity, "enemy": enemy,
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


def _hands(limit=600):
    """Summary of every recent hand (grouped from decisions) for the MANOS grid."""
    try:
        c = _ro()
    except Exception as e:
        return {"hands": [], "error": str(e)}
    rows = c.execute(
        "select ts,hand_key,street,pos,hole,hand_class,board,action,amount,engine,run_label,pot,spr "
        "from decisions order by ts desc limit ?", (limit * 6,)).fetchall()
    resmap = {}
    try:
        for tid, delta, winners, rboard in c.execute("select table_id, chip_delta, winners, board from hand_results"):
            wl = _lj(winners)
            resmap[tid] = (delta, (wl[0].get("agentName") or wl[0].get("agentId")) if wl else None,
                           len((rboard or "").split()), len(wl))
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
        dl, wn, bl, nw = rr if rr else (None, None, None, None)
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
        for r in c.execute("select agent_id,name,n,vpip,pfr,af,bluff_pct,wtsd,wsd,style from agent_stats"):
            hud[r[1]] = {"agent_id": r[0], "n": r[2], "vpip": r[3], "pfr": r[4], "af": r[5],
                         "bluff": r[6], "wtsd": r[7], "wsd": r[8], "style": _lj(r[9], {})}
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


COACH_NEED = 5000


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
        findings.append({"k": "VPIP / PFR", "v": f"{vp}% / {pf}%", "ref": "~22 / 18"})
        vs_opt.append({"k": "VPIP", "you": f"{vp}%", "target": "20–26%",
                       "verdict": verdict(vp, 20, 26, 4), "note": "manos jugadas voluntariamente"})
        vs_opt.append({"k": "PFR", "you": f"{pf}%", "target": "16–20%",
                       "verdict": verdict(pf, 16, 20, 4), "note": "frecuencia de subida preflop"})
        vs_opt.append({"k": "Gap VPIP-PFR", "you": f"{gap}", "target": "≤8",
                       "verdict": verdict(gap, 0, 8, 4), "note": "demasiado flat si es alto"})
        if vp < 16:
            advice.append(f"Muy tight (VPIP {vp}%). Abre más en BTN/CO/SB.")
        elif vp > 30:
            advice.append(f"VPIP {vp}% alto; recorta manos marginales fuera de posición.")
        if gap > 10:
            advice.append(f"Gap VPIP-PFR {gap} grande → demasiado flat preflop; 3-betea o foldea más.")
    cb = q("select sum(case when action in('bet','all-in') then 1 else 0 end),"
           "sum(case when action='check' then 1 else 0 end) from decisions "
           "where street='flop' and (call_chips is null or call_chips<=0)" + hk())
    if cb and cb[0][0] is not None and ((cb[0][0] or 0) + (cb[0][1] or 0)) >= 100:
        bets, checks = cb[0][0] or 0, cb[0][1] or 0
        cbp = round(100 * bets / (bets + checks))
        findings.append({"k": "C-bet flop", "v": f"{cbp}%", "ref": "55–72%"})
        vs_opt.append({"k": "C-bet flop", "you": f"{cbp}%", "target": "55–72%",
                       "verdict": verdict(cbp, 55, 72, 8), "note": "apuesta de continuación tras subir preflop"})
        if cbp > 85:
            advice.append(f"C-bet flop muy alto ({cbp}%); equilibra con más checks de protección de rango.")
        elif cbp < 45:
            advice.append(f"C-bet flop bajo ({cbp}%); aprovecha más la iniciativa como agresor preflop.")
    # Fold-to-cbet sólo si hay muestra suficiente (este bot suele ser el agresor → rara vez afronta apuesta).
    ff = q("select sum(case when action='fold' then 1 else 0 end),count(*) from decisions "
           "where street='flop' and call_chips>0" + hk())
    if ff and ff[0][1] and ff[0][1] >= 200:
        ftc = round(100 * (ff[0][0] or 0) / ff[0][1])
        findings.append({"k": "Fold-to-bet flop", "v": f"{ftc}%", "ref": "<55%"})
        vs_opt.append({"k": "Fold-to-cbet flop", "you": f"{ftc}%", "target": "45–55%",
                       "verdict": verdict(ftc, 45, 55, 6), "note": "te explotan si foldeas de más"})
        if ftc > 58:
            advice.append(f"Foldeas demasiado al c-bet en flop ({ftc}%). Defiende más (flota/raise).")
    pos = q("select pos,count(*),sum(voluntary),sum(preflop_raise) from decisions "
            "where street='preflop' and pos!=''" + hk() + " group by pos")
    order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
    bypos = sorted([{"pos": r[0], "n": r[1], "vpip": round(100 * r[2] / r[1]) if r[1] else 0,
                     "pfr": round(100 * r[3] / r[1]) if r[1] else 0} for r in pos], key=lambda x: order.get(x["pos"], 9))
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
    opp = [{"name": o[0], "vpip": o[1], "pfr": o[2], "af": o[3]} for o in q("select name,vpip,pfr,af from agent_stats")]
    c.close()
    if not advice:
        advice.append("Sin leaks claros con esta muestra; sigue acumulando.")
    return {"locked": False, "hands": total_hands, "win_hands": win_hands, "window": (n or "all"),
            "vs_opt": vs_opt, "vs_panel": vs_panel, "findings": findings, "bypos": bypos,
            "posres": posres, "ab": ab, "advice": advice, "opp": opp}


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
    return out or None


def _coach_compute(hands, window=None):
    """The slow M3 coaching call (runs in a background thread; writes _coach_cache)."""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(HERE, "examples"))
        _sys.path.insert(0, HERE)
        os.environ.setdefault("S7_STATS_DB", DB)
        import s7_report
        import llm_system7
        rep = s7_report.report()
        try:
            meth = open(os.path.join(HERE, "system7_prompt.md")).read()
        except Exception:
            meth = ""
        cur = {}
        try:
            import decide_system7 as _D
            cur = dict(_D.KN)
        except Exception:
            pass
        diag = _coach(window)
        win_txt = ("últimas %s manos" % window) if window not in (None, "all", "") else "todas las manos"
        leaks = "; ".join("%s: %s vs objetivo %s [%s]" % (o["k"], o["you"], o["target"], o["verdict"])
                          for o in (diag.get("vs_opt") or []))
        system = ("Eres un coach de NLHE 6-max de élite (metodología EducaPoker / GTO node-locking). "
                  "Te paso el informe de System 7 contra un panel near-GTO (DeepCFR), su config actual y un "
                  "diagnóstico tu-juego-vs-óptimo de la ventana analizada. "
                  "(1) Análisis ACCIONABLE de leaks (preflop por posición + postflop). "
                  "(2) Propón UNA versión nueva partiendo de la actual. Termina con SOLO un bloque ```json``` con las "
                  "claves a CAMBIAR: opening_ranges {pos:[tokens '22+','A2s+','KTo+']}, threebet_value, threebet_bluff, "
                  "knobs {open_size_bb,threebet_mult,value_eq,station_mult,cbet_bluff_frac,commit_spr,perejil_flop,"
                  "perejil_turn,perejil_relief}. Incluye solo lo que cambies. Español, MUY conciso, ve al grano "
                  "sin razonar de más.\n\n" + meth[:400])
        user = ("VENTANA: %s\nDIAGNÓSTICO vs óptimo: %s\nCONFIG ACTUAL knobs: %s\n\nINFORME:\n%s"
                % (win_txt, leaks or "(sin datos)", json.dumps(cur), rep))
        _cerr = None
        if s7_mllm is not None:
            _r = s7_mllm._chat("minimax", os.environ.get("S7_MODEL", "MiniMax-M3"), system, user, max_tokens=10000)
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
    src = _D.OPENING_RANGES_WIDE if base == "wide" else _D.OPENING_RANGES_STD
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


def _strat_template(base="std", name=""):
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
    knobs = dict(KN_DEFAULTS)
    knobs.update({k: v for k, v in (cfg.get("knobs") or {}).items()
                  if k in KN_DEFAULTS and isinstance(v, (int, float))})
    return {"name": name or "", "base": base, "ranges": _expand_cfg(cfg),
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
        os.environ.setdefault("S7_STATS_DB", DB)
        import llm_system7
        persona = (
            "Eres un jugador profesional de NLHE 6-max online, ganador, con millones de manos jugadas al año y "
            "dominio de rangos GTO y de explotación. Diseña una estrategia COMPLETA para un bot de cash 100bb. "
            "Devuelve un resumen de 1-2 frases y DESPUÉS SOLO un bloque ```json``` con TODAS estas claves: "
            "opening_ranges con las 6 posiciones UTG,MP,CO,BTN,SB,BB (cada una lista de tokens '22+','A2s+','KTo+' "
            "o combos explícitos 'AKs'); threebet_value (lista de clases); threebet_bluff (lista); y knobs "
            "{open_size_bb,threebet_mult,value_eq,station_mult,cbet_bluff_frac,commit_spr,perejil_flop,perejil_turn,"
            "perejil_relief}. Rangos coherentes y posicionales (UTG tight ~12%, BTN ancho ~45%). Español, conciso.")
        if mode == "scratch":
            user = ("Crea tu estrategia IDEAL de jugador pro para 6-max cash 100bb contra un panel near-GTO. "
                    "No mires ningún histórico: dame tu rango de apertura por posición y tus knobs óptimos.")
        else:
            diag = _coach(window)
            cur = dict(KN_DEFAULTS)
            try:
                import decide_system7 as _D
                cur = {k: _D.KN.get(k, KN_DEFAULTS[k]) for k in KN_DEFAULTS}
            except Exception:
                pass
            leaks = "; ".join("%s: tú %s vs objetivo %s [%s]" % (o["k"], o["you"], o["target"], o["verdict"])
                              for o in (diag.get("vs_opt") or []))
            adv = " ".join(diag.get("advice") or [])
            wt = ("últimas %s manos" % window) if window not in (None, "all", "") else "todas las manos"
            user = ("Parte de esta config y ARREGLA estos leaks detectados (ventana=%s).\n"
                    "CONFIG knobs actual: %s\nDIAGNÓSTICO vs óptimo: %s\nCONSEJOS: %s\n"
                    "Devuelve la estrategia corregida completa (las 6 posiciones)." % (wt, json.dumps(cur), leaks, adv))
        txt, cerr = None, None
        if s7_mllm is not None:
            r = s7_mllm._chat("minimax", os.environ.get("S7_MODEL", "MiniMax-M3"), persona, user, max_tokens=10000)
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
    c.update(running=True, window=window, mode=mode)
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
    try:
        with open(os.path.join(CLASIF_DIR, label + ".json"), encoding="utf-8") as f:
            creds = json.load(f)
    except Exception:
        return {"error": "sin credenciales guardadas para '" + str(label) + "' (sólo las clasificatorias lanzadas con la tarjeta tras esta versión son reclamables)"}
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
]


def _mllm_models():
    prov = {p: bool(s7_mllm and s7_mllm.provider_ready(p)) for p in ("minimax", "openrouter", "xiaomi")}
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
    """My launched clasificatoria agents, ranked by Eval bb/100 (for choosing which to claim)."""
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
    rows = []
    try:
        for fn in sorted(os.listdir(CLASIF_DIR)):
            if not fn.endswith(".json"):
                continue
            lbl = fn[:-5]
            try:
                with open(os.path.join(CLASIF_DIR, fn), encoding="utf-8") as f:
                    cr = json.load(f)
            except Exception:
                cr = {}
            strat = cr.get("strat") or (lbl.split("-")[1] if len(lbl.split("-")) >= 2 else "?")
            rows.append({"label": lbl, "name": cr.get("name") or lbl, "agentId": cr.get("agentId"),
                         "strategy": strat, "engine": cr.get("engine"), "hands": hands.get(lbl, 0),
                         "bb100": prog.get(lbl), "state": _svc("arena-run-" + lbl), "ts": cr.get("ts")})
    except Exception:
        pass
    rows.sort(key=lambda r: (r["bb100"] is None, -(r["bb100"] if r["bb100"] is not None else -1e9)))
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
    with _lock:
        if time.time() - _cache["ts"] > 2 or _cache["data"] is None:
            _cache["data"] = _state()
            _cache["ts"] = time.time()
        return _cache["data"]


HTML = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SYSTEM 7 · LIVE</title>
<style>
:root{--bg:#06080b;--pan:#0b0f15;--bd:#172230;--grn:#2ee6a6;--amb:#f5b73d;--red:#ff5d5d;--blu:#56b6ff;--dim:#5a6675;--txt:#cdd6e0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:13.5px/1.55 "Cantarell","Inter","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--blu)}
.top{display:flex;align-items:center;gap:14px;padding:7px 12px;border-bottom:1px solid var(--bd);background:linear-gradient(#0c1118,#080b10)}
.top b{color:var(--grn);letter-spacing:2px}
.live{color:var(--grn)}.live::before{content:"●";margin-right:5px;animation:bl 1.2s infinite}
@keyframes bl{50%{opacity:.25}}
.tabs{display:inline-flex;gap:3px}
.tab{cursor:pointer;padding:3px 12px;border:1px solid var(--bd);border-radius:4px;color:var(--dim);font-size:10px;letter-spacing:1.5px;user-select:none}
.tab.on{color:var(--grn);border-color:var(--grn);background:#0c1a16}
.tab:hover{color:var(--txt)}
.kpis{margin-left:auto;display:flex;gap:18px}
.kpi{text-align:right}.kpi .l{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:1px}
.kpi .v{font-size:24px;font-weight:700}
.kpi .l{font-size:11px}
.wrap{display:grid;grid-template-columns:1.15fr 1fr;gap:10px;padding:10px}
.pan{border:1px solid var(--bd);background:var(--pan);border-radius:4px;overflow:hidden}
.pan>h{display:block;padding:6px 10px;font-size:11.5px;letter-spacing:1px;text-transform:uppercase;font-weight:600;color:var(--dim);border-bottom:1px solid var(--bd);background:#0a0e13}
.pan .bd{padding:9px}
.span2{grid-column:1/3}
.pos{font-weight:700}.neg{color:var(--red);font-weight:700}.posv{color:var(--grn);font-weight:700}
.grid13{display:grid;grid-template-columns:repeat(13,1fr);gap:1px}
.cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:8.5px;color:#9fb0c0;border-radius:2px;background:#0a0e13;border:1px solid #0e141b}
.cell.p{outline:1px solid var(--amb)}
.row{display:flex;align-items:center;gap:8px;margin:3px 0}
.row .lab{width:42px;color:var(--dim)}
.track{flex:1;height:13px;background:#0a0e13;border:1px solid var(--bd);border-radius:3px;position:relative;overflow:hidden}
.fill{height:100%;position:absolute;left:0;top:0}
.fv{position:absolute;right:4px;top:0;font-size:10px}
.ab{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.armbox{border:1px solid var(--bd);border-radius:4px;padding:8px;background:#0a0e13}
.armbox .big{font-size:26px;font-weight:800}
.mut{color:var(--dim)}
table{width:100%;border-collapse:collapse}td,th{padding:3px 7px;text-align:right}th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;border-bottom:1px solid var(--bd)}td:first-child,th:first-child{text-align:left}
.tick{max-height:180px;overflow:auto;font-size:12.5px}
.tick .r{display:grid;grid-template-columns:64px 34px 62px 50px 1fr auto;gap:8px;padding:3px 5px;border-bottom:1px solid #0e141b;align-items:center}
.tick .pc{font-size:11px;padding:1px 3px;min-width:14px}
.eM3{color:var(--amb)}.eh{color:var(--grn)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.up{background:var(--grn)}.down{background:var(--red)}.warn{background:var(--amb)}
.foot{display:flex;gap:16px;padding:6px 12px;border-top:1px solid var(--bd);color:var(--dim);font-size:11px;flex-wrap:wrap}
.chip{padding:1px 6px;border:1px solid var(--bd);border-radius:10px}
svg{display:block}
.runlog,.m3body,.elog,pre{font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace}
.eqleg{display:flex;gap:18px;flex-wrap:wrap;margin-top:8px;font-size:12px;align-items:center}
.eqleg .posv{font-weight:700}
.eqctl{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.eqbtn{cursor:pointer;background:#0a0e13;border:1px solid var(--bd);color:var(--dim);border-radius:4px;padding:2px 13px;font:inherit;font-size:11px;letter-spacing:.5px}
.eqbtn:hover{color:var(--txt)}
.eqbtn.on{color:var(--c,var(--grn));border-color:var(--c,var(--grn));background:#0c1a16}
.hbar{display:flex;align-items:center;gap:12px;padding:9px 12px}
.hbar input{background:#0a0e13;border:1px solid var(--bd);color:var(--txt);border-radius:4px;padding:5px 10px;font:inherit;width:300px}
.hbar input:focus{outline:none;border-color:var(--grn)}
#hands{max-height:calc(100vh - 150px);overflow:auto;border-top:1px solid var(--bd)}
.htab{width:100%;border-collapse:collapse;font-size:12px}
.htab th{position:sticky;top:0;background:#0a0e13;color:var(--dim);text-transform:uppercase;font-size:10.5px;letter-spacing:.5px;padding:6px 9px;text-align:left;border-bottom:1px solid var(--bd);z-index:2;cursor:pointer;user-select:none;white-space:nowrap}
.htab th:hover{color:var(--txt)}
.htab td{padding:4px 9px;border-bottom:1px solid #0e141b;text-align:left;white-space:nowrap}
.htab tbody tr{cursor:pointer}
.htab tbody tr:hover{background:#11202b}
.hcards .pc{font-size:11px;padding:1px 4px}
.hmoves{color:var(--dim);max-width:380px;overflow:hidden;text-overflow:ellipsis}
.armtag{padding:0 6px;border-radius:8px;font-size:9px;border:1px solid var(--bd)}
.modal{display:none;position:fixed;inset:0;background:rgba(2,4,7,.82);align-items:center;justify-content:center;z-index:50}
.modal .card{background:var(--pan);border:1px solid var(--grn);border-radius:8px;width:min(760px,95vw);max-height:92vh;overflow:auto;box-shadow:0 0 50px rgba(46,230,166,.18)}
.modal .card.wide{width:min(1040px,96vw)}
.mh{padding:8px 12px;border-bottom:1px solid var(--bd);background:#0a0e13}
.mb{padding:12px}
.pc{display:inline-block;min-width:17px;padding:2px 4px;margin:0 1.5px;background:linear-gradient(#fff,#e7edf2);color:#15202b;border:1px solid #b9c2c9;border-radius:3px;font-weight:800;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.45)}
.big .pc{font-size:18px;padding:4px 8px;min-width:24px}
.ctl{margin:10px 0}.ctl button{background:#0a0e13;color:var(--txt);border:1px solid var(--bd);border-radius:4px;padding:4px 12px;cursor:pointer;margin-right:5px}
.ctl button:hover{border-color:var(--grn);color:var(--grn)}
.elog{max-height:240px;overflow:auto;border:1px solid var(--bd);border-radius:4px;margin-top:6px}
.ev{padding:3px 8px;border-bottom:1px solid #0e141b;cursor:pointer;font-size:11px}
.ev:hover{background:#0e151c}.ev.cur{background:#13202c;border-left:2px solid var(--grn)}.ev.mine{color:var(--grn)}
.ptable{position:relative;height:380px;margin:10px auto 8px;border-radius:46%/44%;background:radial-gradient(ellipse 70% 72% at 50% 40%,#1d6552 0%,#0f3d2e 58%,#0a2c21 100%);border:9px solid #2c1d12;box-shadow:inset 0 0 70px rgba(0,0,0,.65),inset 0 0 0 2px #0c3326,0 12px 34px rgba(0,0,0,.55)}
.ptable::after{content:"SYSTEM 7";position:absolute;left:50%;top:62%;transform:translate(-50%,-50%);color:rgba(255,255,255,.05);font-size:22px;font-weight:800;letter-spacing:6px;pointer-events:none}
.pcenter{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center;width:62%}
.pboard{min-height:26px}
.ppot{margin-top:8px;color:var(--amb);font-weight:700;font-size:12px}
.ppot::before{content:"\2641 ";opacity:.85}
.pstreet{font-size:9px;letter-spacing:2px;color:rgba(255,255,255,.4);text-transform:uppercase;margin-top:2px}
.pseat{position:absolute;transform:translate(-50%,-50%);width:98px;text-align:center;background:linear-gradient(#141b24,#0a0e13);border:1px solid #25323f;border-radius:9px;padding:5px 5px 6px;font-size:10px;transition:box-shadow .25s,opacity .25s,transform .25s;box-shadow:0 3px 8px rgba(0,0,0,.45);opacity:.92}
.pseat.me{border-color:var(--grn)}
.pseat.fold{opacity:.32;filter:grayscale(.6)}
.pseat.act{opacity:1;border-color:var(--grn);transform:translate(-50%,-50%) scale(1.09);z-index:6;animation:seatpulse 1.5s ease-in-out infinite}
@keyframes seatpulse{0%,100%{box-shadow:0 0 0 2px var(--grn),0 0 16px 3px rgba(46,230,166,.4)}50%{box-shadow:0 0 0 2px var(--grn),0 0 30px 8px rgba(46,230,166,.72)}}
.pn{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:2px;color:#dfe7ef}
.pbadge{background:#1d2836;color:var(--amb);border-radius:3px;padding:0 3px;font-size:8px;margin-left:3px}
.pstk{color:var(--amb);font-weight:700;font-size:11px}
.pstk::before{content:"\25CF";color:#caa23a;margin-right:3px;font-size:9px}
.pcards{margin:3px 0 1px;min-height:20px}
.cb{display:inline-block;width:13px;height:19px;border-radius:2px;margin:0 1px;vertical-align:middle;background:repeating-linear-gradient(45deg,#23344d,#23344d 3px,#2f4a6b 3px,#2f4a6b 6px);border:1px solid #0c1d33}
.pa{font-size:9px;color:var(--dim);min-height:11px}
.pa .won{color:#ffd877;font-weight:800;font-size:11px}
.pa .lost{color:#7c8896}
.pbet{margin-top:3px;display:inline-block;min-width:22px;padding:1px 8px;font-size:10px;font-weight:800;color:#1a1206;background:radial-gradient(circle at 30% 30%,#ffd877,#e0a72c);border:1px solid #b9851f;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.5)}
.pdealer{position:absolute;width:18px;height:18px;border-radius:50%;background:radial-gradient(circle at 35% 30%,#fff,#cfd6dc);color:#15202b;font-weight:800;font-size:10px;line-height:18px;text-align:center;border:1px solid #9aa3ab;box-shadow:0 1px 3px rgba(0,0,0,.6);transform:translate(-50%,-50%);z-index:7}
.potline{display:flex;align-items:center;justify-content:center;gap:7px;margin-top:6px;flex-wrap:wrap}
.potlbl{color:var(--amb);font-weight:800;font-size:15px;text-shadow:0 1px 3px #000}
.sprlbl{font-size:11px;color:#aebfce;letter-spacing:.5px}
.sprlbl b{color:var(--grn);font-size:13px}
.betchip{position:absolute;transform:translate(-50%,-50%);display:flex;align-items:flex-end;gap:4px;z-index:4}
.betamt{font-size:10px;font-weight:800;color:#ffe6a8;text-shadow:0 1px 2px #000,0 0 4px #000}
.chipstk{position:relative;display:inline-block;width:20px;vertical-align:bottom}
.chipstk i{position:absolute;left:0;width:20px;height:6px;border-radius:50%;border:1px solid rgba(0,0,0,.55);box-shadow:inset 0 1px 1px rgba(255,255,255,.45),0 1px 1px rgba(0,0,0,.45)}
.pseat.win{border-color:var(--amb);box-shadow:0 0 0 2px var(--amb),0 0 22px 5px rgba(245,183,61,.5)}
.sdbanner{background:linear-gradient(#10231b,#0a1612);border:1px solid #1d6552;border-radius:6px;padding:7px 10px;margin-bottom:8px;font-weight:700;color:#ffe6a8}
.sdreveal{font-size:11px;color:var(--dim);margin:-3px 0 8px;line-height:1.8}
.sdgap{background:#3a2a05;border:1px solid #6b5300;color:#ffd877;padding:6px 9px;border-radius:6px;margin-bottom:7px;font-size:11.5px;line-height:1.5}
.rlink{color:#7fd6ff;text-decoration:none;margin-left:10px;font-size:12px}
.rlink:hover{text-decoration:underline}
@keyframes tp{0%{color:#2ee6a6;text-shadow:0 0 8px rgba(46,230,166,.6)}100%{color:inherit;text-shadow:none}}
.tickpulse{animation:tp .6s ease-out}
.mlmodels{display:flex;flex-wrap:wrap;gap:5px}
.mlmodel{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--bd);border-radius:5px;padding:3px 7px;font-size:11px;cursor:pointer;background:#0a0e13}
.mlmodel.off{opacity:.4}
.mlhand{border-top:1px solid var(--bd);padding:5px 0;font-size:11px;line-height:1.9}
.sdreveal .pc{font-size:11px;padding:1px 4px}
.street{border:1px solid var(--bd);border-radius:5px;margin-top:8px;overflow:hidden}
.sthead{background:#0a0e13;padding:4px 9px;font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--bd)}
.sthead .pc{font-size:12px}
.sa{padding:2px 9px;font-size:11px;border-bottom:1px solid #0d1219}
.sa.mine{color:var(--grn)}
.sa .amt{color:var(--amb);font-weight:800}
.stpot{float:right;color:var(--amb);font-weight:700;letter-spacing:0}
.readline{padding:3px 9px;font-size:11px;background:#0c1620;border-top:1px solid #12202c;color:#bcd}
.m3box{margin:0 9px 5px;border:1px solid #2a2440;border-radius:4px;cursor:pointer;background:#0d0b16}
.m3box>.mut{display:block;padding:3px 8px;color:#b98bff}
.m3body{display:none;padding:6px 9px;font-size:10px;white-space:pre-wrap;color:#c8c2dd;border-top:1px solid #2a2440;max-height:240px;overflow:auto}
.m3box.open .m3body{display:block}
.pcard{border:1px solid var(--bd);background:var(--pan);border-radius:7px;padding:9px 11px;margin:9px}
.pcard h4{margin:0 0 6px;font-size:13px;color:var(--txt)}
.pcard .st{display:inline-block;margin:2px 10px 2px 0;font-size:11px;color:var(--dim)}
.pcard .st b{color:var(--txt)}
.pcard .holds{margin-top:7px;font-size:11px;line-height:2}
.phand{cursor:pointer;border:1px solid var(--bd);border-radius:4px;padding:1px 4px;margin:1px 3px 1px 0;display:inline-block}
.phand:hover{border-color:var(--grn)}.phand.won{border-color:var(--amb)}
.atag{padding:1px 7px;border:1px solid var(--amb);border-radius:8px;font-size:10px;color:var(--amb);margin-left:8px}
.cadv{padding:3px 0;font-size:12px;line-height:1.5}
.rform{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:11px;color:var(--dim)}
.rform input,.rform select{background:#0a0e13;border:1px solid var(--bd);color:var(--txt);border-radius:4px;padding:3px 7px;font:inherit}
#r-logunit{background:#0a0e13;border:1px solid var(--bd);color:var(--txt);border-radius:4px;padding:2px 6px;font:inherit;font-size:11px}
.runlog{background:#07090d;border:1px solid var(--bd);border-radius:4px;padding:8px;max-height:300px;overflow:auto;font-size:10px;line-height:1.45;white-space:pre-wrap;color:#9fb0c0;margin-top:6px}
.vsopt{width:100%;border-collapse:collapse;font-size:12px}
.vsopt th{text-align:left;color:var(--dim);text-transform:uppercase;font-size:10px;letter-spacing:.5px;padding:5px 8px;border-bottom:1px solid var(--bd)}
.vsopt td{padding:4px 8px;border-bottom:1px solid #0e141b}
.vsopt td.vd{font-weight:700;text-align:center;font-size:14px}
.chip{display:inline-block;border:1px solid var(--bd);border-radius:4px;padding:2px 9px;margin:0 6px 0 0;cursor:pointer;font-size:11px;color:var(--dim)}
.chip.on{background:#11202b;color:var(--txt);border-color:var(--blu)}
.rgwrap{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}
.rgpos{border:1px solid var(--bd);border-radius:6px;padding:6px;background:var(--bg)}
.rgpos h5{margin:0 0 4px;font-size:11px;color:var(--txt);display:flex;justify-content:space-between;gap:10px}
.rgpos h5 .mut{font-weight:400}
.rg{border-collapse:collapse}
.rg td{width:15px;height:15px;font-size:7px;text-align:center;border:1px solid #131a25;color:#5a6678;cursor:pointer;user-select:none;background:#0e1219;line-height:1}
.rg td.on{background:#b98bff;color:#0a0a0a;border-color:#b98bff;font-weight:700}
.rg td.pair{background:#161b26}
.rg td.pair.on{background:#b98bff;color:#0a0a0a}
.kgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:8px;margin-top:8px}
.kgrid label{font-size:11px;color:var(--dim);display:flex;flex-direction:column;gap:2px}
.kgrid input{background:#0a0e13;border:1px solid var(--bd);color:var(--txt);border-radius:4px;padding:3px 6px;font:inherit;font-size:12px}
.binp{background:#0a0e13;border:1px solid var(--bd);color:var(--txt);border-radius:4px;padding:4px 7px;font:inherit;font-size:12px}
</style></head><body>
<div class="top"><b>SYSTEM&nbsp;7</b><span class="live" id="live">LIVE</span>
<span class="tabs"><span class="tab on" id="tab-panel" onclick="showTab('panel')">PANEL</span><span class="tab" id="tab-hands" onclick="showTab('hands')">MANOS</span><span class="tab" id="tab-players" onclick="showTab('players')">PLAYERS</span><span class="tab" id="tab-coach" onclick="showTab('coach')">COACH</span><span class="tab" id="tab-run" onclick="showTab('run')">RUN</span><span class="tab" id="tab-rank" onclick="showTab('rank')">RANK</span><span class="tab" id="tab-mllm" onclick="showTab('mllm')">multiLLM</span></span>
<span class="mut" id="sub">Eval test-bench · panel DeepCFR</span>
<div class="kpis" id="kpis"></div></div>
<div id="panelview"><div class="wrap">
 <div class="pan span2"><h>A/B · std vs wide (bb/100)</h><div class="bd ab" id="ab"></div></div>
 <div class="pan span2"><h>Curva equity REAL vs EV · por estrategia</h><div class="bd"><div class="eqctl" id="eqctl"></div><div id="equity"></div></div></div>
 <div class="pan"><h>Rango preflop · VPIP heatmap (13×13)</h><div class="bd"><div class="grid13" id="grid"></div>
   <div class="mut" style="margin-top:6px">verde=VPIP% · contorno ámbar=par · hover=detalle</div></div></div>
 <div class="pan"><h>VPIP / PFR por posición</h><div class="bd" id="pos"></div></div>
 <div class="pan"><h>Postflop · acción por calle</h><div class="bd" id="streets"></div></div>
 <div class="pan"><h>Postflop · por fuerza de mano</h><div class="bd" id="strength"></div></div>
 <div class="pan"><h>Uso de MiniMax M3</h><div class="bd" id="m3"></div></div>
 <div class="pan"><h>Rivales · enemigo medio · histórico</h><div class="bd" id="misc"></div></div>
 <div class="pan span2"><h>Decisiones en vivo</h><div class="bd"><div class="tick" id="tick"></div></div></div>
</div></div>
<div id="handsview" style="display:none">
 <div class="hbar"><input id="hfilter" placeholder="filtrar:  AKs · BTN · river · M3 · wide …" oninput="renderHands()"><span class="mut" id="hcount"></span><span class="mut">· click en una fila → reproductor</span></div>
 <div id="hands"></div>
</div>
<div id="playersview" style="display:none"><div id="players"></div></div>
<div id="coachview" style="display:none"><div id="coach"></div></div>
<div id="runview" style="display:none"><div id="run"></div></div>
<div id="rankview" style="display:none"><div id="rankbox"></div></div>
<div id="mllmview" style="display:none"><div id="mllmbox"></div></div>
<div class="foot" id="foot"></div>
<div id="modal" class="modal" onclick="if(event.target===this)closeHand()"><div class="card"></div></div>
<script>
const $=s=>document.querySelector(s);
const sgn=v=>v==null?'<span class="mut">—</span>':(v>0?'<span class="posv">+'+v+'</span>':(v<0?'<spa'+'n class="neg">'+v+'</span>':'<span>0</span>'));
function spark(arr,w=180,h=34){if(!arr||!arr.length)return'<svg width="'+w+'" height="'+h+'"></svg>';
 const mn=Math.min(0,...arr),mx=Math.max(0,...arr),rng=(mx-mn)||1;
 const pts=arr.map((v,i)=>[ (arr.length<2?0:i/(arr.length-1))*(w-4)+2, h-2-((v-mn)/rng)*(h-4)]);
 const zy=h-2-((0-mn)/rng)*(h-4);
 const d=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
 let dots=pts.map(p=>'<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="1.7" fill="#2ee6a6"/>').join('');
 return '<svg width="'+w+'" height="'+h+'"><line x1="0" y1="'+zy.toFixed(1)+'" x2="'+w+'" y2="'+zy.toFixed(1)+'" stroke="#1d2836"/><path d="'+d+'" fill="none" stroke="#2ee6a6" stroke-width="1.3"/>'+dots+'</svg>';}
let LASTD=null;const EQOPT={ev:true,off:{}};
function eqchart(eqd){
 const labels=Object.keys(eqd||{}).filter(k=>(eqd[k]||[]).length>=2&&!EQOPT.off[k]).sort((a,b)=>(a=='wide')-(b=='wide')||a.localeCompare(b));
 if(!labels.length)return '<span class="mut">sin estrategia seleccionada (activa std o wide arriba)</span>';
 const sev=EQOPT.ev,COL={std:'#2ee6a6',wide:'#56b6ff'},PAL=['#2ee6a6','#56b6ff','#f5b73d','#b98bff','#ff6b6b'];
 const colOf=(lbl,i)=>COL[lbl]||PAL[i%PAL.length];
 const W=1000,H=260,L=56,R=16,T=14,B=26,x0=L,x1=W-R,y0=T,y1=H-B;
 let xmn=Infinity,xmx=-Infinity,ymn=0,ymx=0;
 labels.forEach(l=>(eqd[l]||[]).forEach(p=>{xmn=Math.min(xmn,p.h);xmx=Math.max(xmx,p.h);ymn=Math.min(ymn,p.raw,sev?p.adj:p.raw);ymx=Math.max(ymx,p.raw,sev?p.adj:p.raw);}));
 const padv=(ymx-ymn)*0.08||1;ymn-=padv;ymx+=padv;const xr=(xmx-xmn)||1,yr=(ymx-ymn)||1;
 const X=v=>x0+((v-xmn)/xr)*(x1-x0),Y=v=>y1-((v-ymn)/yr)*(y1-y0);
 const line=(s,k)=>s.map((p,i)=>(i?'L':'M')+X(p.h).toFixed(1)+' '+Y(p[k]).toFixed(1)).join(' ');
 let grid='',ylab='',xlab='';
 for(let i=0;i<=4;i++){const val=ymn+yr*i/4,yy=Y(val).toFixed(1);grid+='<line x1="'+x0+'" y1="'+yy+'" x2="'+x1+'" y2="'+yy+'" stroke="#12202c"/>';ylab+='<text x="'+(x0-8)+'" y="'+(+yy+4)+'" text-anchor="end" font-size="12" fill="#6b7787">'+Math.round(val)+'</text>';}
 for(let i=0;i<=4;i++){const hv=xmn+xr*i/4,xx=X(hv).toFixed(1);xlab+='<text x="'+xx+'" y="'+(H-8)+'" text-anchor="middle" font-size="12" fill="#6b7787">'+Math.round(hv)+'</text>';}
 const zy=Y(0).toFixed(1);
 let paths='',marks='',leg='';
 labels.forEach((lbl,i)=>{const s=eqd[lbl],c=colOf(lbl,i),last=s[s.length-1];
  if(sev)paths+='<path d="'+line(s,'adj')+'" fill="none" stroke="'+c+'" stroke-width="1.6" stroke-dasharray="6 5" opacity="0.7" vector-effect="non-scaling-stroke"/>';
  paths+='<path d="'+line(s,'raw')+'" fill="none" stroke="'+c+'" stroke-width="2.5" vector-effect="non-scaling-stroke"/>';
  marks+='<circle cx="'+X(last.h).toFixed(1)+'" cy="'+Y(last.raw).toFixed(1)+'" r="4" fill="'+c+'" stroke="#06080b" stroke-width="1.5"/>';
  leg+='<span style="color:'+c+';font-weight:700">'+lbl+': REAL '+last.raw+(sev?(' · EV '+last.adj):'')+'</span>';
 });
 return '<svg viewBox="0 0 '+W+' '+H+'" width="100%" style="width:100%;height:auto;display:block">'+
  grid+ylab+xlab+
  '<line x1="'+x0+'" y1="'+zy+'" x2="'+x1+'" y2="'+zy+'" stroke="#3a4a5c" stroke-width="1.5"/>'+
  paths+marks+
  '</svg><div class="eqleg">'+leg+'<span class="mut">sólido = REAL'+(sev?' · discontinua = EV':'')+' · X manos · Y chips</span></div>';
}
function drawEquity(){
 const eqd=(LASTD&&LASTD.equity)||{};
 const labels=Object.keys(eqd).filter(k=>(eqd[k]||[]).length>=2).sort((a,b)=>(a=='wide')-(b=='wide')||a.localeCompare(b));
 const COL={std:'#2ee6a6',wide:'#56b6ff'};
 const ctl=labels.map(l=>'<button class="eqbtn'+(EQOPT.off[l]?'':' on')+'" style="--c:'+(COL[l]||'#9fb0c0')+'" data-eq="'+l+'">'+l+'</button>').join('')+'<button class="eqbtn'+(EQOPT.ev?' on':'')+'" style="--c:#aebfce" data-eq="__ev">EV</button>';
 const ec=document.getElementById('eqctl');if(ec)ec.innerHTML=ctl;
 const eq=document.getElementById('equity');if(eq)eq.innerHTML=eqchart(eqd);
}
function bar(lab,val,max,color,suffix){const pc=Math.max(2,Math.min(100,100*val/(max||1)));
 return '<div class="row"><div class="lab">'+lab+'</div><div class="track"><div class="fill" style="width:'+pc+'%;background:'+color+'"></div><div class="fv">'+val+(suffix||'')+'</div></div></div>';}
function arm(name,a,color){return '<div class="armbox"><div class="mut">'+name+'</div>'+
 '<div class="big">'+sgn(a.mean)+'</div>'+
 '<div class="mut">n='+a.n+(a.ci!=null?' · IC ±'+a.ci:'')+(a.last!=null?' · últ '+a.last:'')+'</div>'+
 spark(a.series)+'</div>';}
function render(d){
 if(d.error){$('#sub').textContent='ERROR: '+d.error;}
 $('#kpis').innerHTML=[
  ['manos',d.hands,'kpi-hands'],['decisiones',d.decisions,'kpi-dec'],
  ['bb/100 std',sgn(d.ab.std.mean)],['bb/100 wide',sgn(d.ab.wide.mean)],
  ['M3 %',d.m3pct+'%','kpi-m3']
 ].map(k=>'<div class="kpi"><div class="l">'+k[0]+'</div><div class="v"'+(k[2]?' id="'+k[2]+'"':'')+'>'+k[1]+'</div></div>').join('');
 let verdict='';const s=d.ab.std.mean,w=d.ab.wide.mean;
 if(s!=null&&w!=null){const lead=w>s?'WIDE':'STD';verdict='<div class="mut" style="grid-column:1/3;text-align:center">▲ lidera <b>'+lead+'</b> ('+(w-s>0?'+':'')+(w-s).toFixed(1)+' bb/100) · '+(Math.abs(w-s)< (d.ab.wide.ci||20)?'dentro del ruido':'señal')+'</div>';}
 else verdict='<div class="mut" style="grid-column:1/3;text-align:center">wide aún sin muestra (n='+d.ab.wide.n+')</div>';
 $('#ab').innerHTML=arm('STD (rangos actuales)',d.ab.std,'#2ee6a6')+arm('WIDE (más anchos)',d.ab.wide,'#f5b73d')+verdict;
 LASTD=d;drawEquity();
 const R=d.ranks;let g='';
 for(let i=0;i<13;i++)for(let j=0;j<13;j++){
  let cls,pair=i==j;
  if(i==j)cls=R[i]+R[i]; else if(i<j)cls=R[i]+R[j]+'s'; else cls=R[j]+R[i]+'o';
  const c=d.classes[cls];const v=c?c.vpip:0;
  const L=v>0?(10+v*0.38):6; const bg=v>0?('hsl(157 70% '+L+'%)'):'#0a0e13';
  const fg=v>55?'#04110c':'#9fb0c0';
  g+='<div class="cell'+(pair?' p':'')+'" title="'+cls+'  VPIP '+v+'%  PFR '+(c?c.pfr:0)+'%  n='+(c?c.n:0)+'" style="background:'+bg+';color:'+fg+'">'+cls+'</div>';
 }
 $('#grid').innerHTML=g;
 $('#pos').innerHTML=d.bypos.map(p=>'<div style="margin-bottom:5px"><div class="mut">'+p.pos+' <span style="float:right">n='+p.n+'</span></div>'+
   bar('VPIP',p.vpip,60,'#2ee6a6','%')+bar('PFR',p.pfr,60,'#f5b73d','%')+'</div>').join('')||'<span class="mut">sin datos</span>';
 $('#streets').innerHTML='<table><tr><th>calle</th><th>n</th><th>fold</th><th>check</th><th>call</th><th>bet/raise</th></tr>'+
   d.streets.map(s=>{const p=x=>s.n?Math.round(100*x/s.n)+'%':'-';return '<tr><td>'+s.street+'</td><td>'+s.n+'</td><td>'+p(s.fold)+'</td><td>'+p(s.check)+'</td><td>'+p(s.call)+'</td><td class="posv">'+p(s.agg)+'</td></tr>';}).join('')+'</table>';
 $('#strength').innerHTML='<table><tr><th>fuerza</th><th>n</th><th>agg</th><th>call</th><th>chk/fold</th></tr>'+
   d.strength.map(s=>{const p=x=>s.n?Math.round(100*x/s.n)+'%':'-';return '<tr><td>'+s.s+'</td><td>'+s.n+'</td><td class="posv">'+p(s.agg)+'</td><td>'+p(s.call)+'</td><td>'+p(s.pasv)+'</td></tr>';}).join('')+'</table>';
 const mtot=d.m3||1;
 $('#m3').innerHTML='<div class="big" style="font-size:22px;color:#f5b73d">'+d.m3+' <span class="mut" style="font-size:12px">llamadas · '+d.m3pct+'%</span></div>'+
  '<div class="mut" style="margin:4px 0 2px">por calle</div>'+Object.entries(d.m3street).sort((a,b)=>b[1]-a[1]).map(e=>bar(e[0],e[1],mtot,'#f5b73d','')).join('')+
  '<div class="mut" style="margin:6px 0 2px">por fuerza</div>'+Object.entries(d.m3str).sort((a,b)=>b[1]-a[1]).map(e=>bar(e[0],e[1],mtot,'#8a6bd6','')).join('');
 const en=d.enemy,ep=x=>x==null?'—':((x<=1?x*100:x).toFixed(1)+'%');
 $('#misc').innerHTML=(en?('<div class="mut">enemigo medio · '+en.n+' rivales · <b style="color:var(--amb)">'+en.archetype+'</b></div>'+[['VPIP',ep(en.vpip)],['PFR',ep(en.pfr)],['AF',en.af!=null?en.af.toFixed(2):'—'],['WTSD',ep(en.wtsd)],['WSD',ep(en.wsd)]].map(s=>'<span class="chip" style="margin:3px 5px 3px 0;display:inline-block">'+s[0]+' <b>'+s[1]+'</b></span>').join('')):'<div class="mut">enemigo medio: sin datos aún (se llena al jugar)</div>')+
  '<div class="mut" style="margin-top:9px">arquetipos enfrentados</div>'+Object.entries(d.arch).sort((a,b)=>b[1]-a[1]).map(e=>'<span class="chip" style="margin:2px 4px 2px 0;display:inline-block">'+e[0]+' '+e[1]+'</span>').join('')+
  '<div class="mut" style="margin-top:9px">rendimiento por motor (manos jugadas)</div>'+((d.engines&&d.engines.length)?('<table><tr><th>motor</th><th>manos</th><th>win%</th><th>bb/100</th><th>chips</th></tr>'+d.engines.map(e=>'<tr><td>'+esc(e.model)+'</td><td>'+e.hands+'</td><td>'+e.winpct+'%</td><td class="'+(e.bb100>=0?'posv':'neg')+'">'+(e.bb100>=0?'+':'')+e.bb100+'</td><td>'+(e.delta>=0?'+':'')+e.delta+'</td></tr>').join('')+'</table>'):'<span class="mut">sin datos aún (se llena al jugar)</span>');
 $('#tick').innerHTML=d.recent.map(r=>{const t=new Date(r.ts*1000).toLocaleTimeString();const ec=r.engine=='M3'?'eM3':'eh';
   return '<div class="r" data-k="'+encodeURIComponent(r.key||'')+'" style="cursor:pointer" title="click: reproducir mano"><span class="mut">'+t+'</span><span>'+(r.pos||'')+'</span><span class="tkc">'+(chs(r.hole)||(r.hole||''))+'</span><span class="mut">'+(r.street||'')+'</span><span>'+(r.strength||'')+' · <b>'+r.action+'</b>'+(r.amount?(' '+r.amount):'')+'</span><span class="'+ec+'">'+r.engine+'</span></div>';}).join('');
 $('#foot').innerHTML=Object.entries(d.svc).map(e=>{const c=e[1]=='active'?'up':(e[1]=='activating'?'warn':'down');return '<span><span class="dot '+c+'"></span>'+e[0]+' <span class="mut">'+e[1]+'</span></span>';}).join('')+
   '<span style="margin-left:auto" class="mut">act '+new Date(d.ts*1000).toLocaleTimeString()+'</span>';
}
/* ---------- cards + hand replayer ---------- */
let HAND=null,STEP=0,TMR=null,EMBED=true,CURURL='';
function ch(c){if(!c)return'';let r=c.toUpperCase().startsWith('10')?'T':c[0].toUpperCase();const s=c.slice(-1).toLowerCase();const red=(s=='h'||s=='d');const su={h:'♥',d:'♦',s:'♠',c:'♣'}[s]||s;return '<span class="pc" style="color:'+(red?'#d42a32':'#15202b')+'">'+r+su+'</span>';}
function chs(str){return (str||'').split(/[,\s]+/).filter(Boolean).map(ch).join('');}
function tstate(evs,step){
 let seats={},order=[],base=0,board=[],curMax=0,acting=null,street='preflop',blinds=[];
 const see=sn=>{if(!(sn in seats)){seats[sn]={name:null,bet:0,inv:0,folded:false,action:null};order.push(sn);}return seats[sn];};
 for(let i=0;i<=step&&i<evs.length;i++){const e=evs[i],s=e.summary||{},sn=s.seatNumber;
  if(sn==null&&e.type!='StreetDealt')continue;
  if(e.type=='BlindPosted'){const o=see(sn);o.name=o.name||s.agentName;const a=s.amount||0;o.inv+=Math.max(0,a-o.bet);o.bet=a;curMax=Math.max(curMax,a);blinds.push({seat:sn,amount:a});}
  else if(e.type=='ActionTaken'){const o=see(sn);o.name=s.agentName||o.name;o.action=s.action;acting=sn;street=e.street||street;
   if(s.action=='fold')o.folded=true;
   else if(s.action=='check'){}
   else if(s.action=='call'){const a=Math.max(curMax,s.amount||0);o.inv+=Math.max(0,a-o.bet);o.bet=a;curMax=Math.max(curMax,a);}
   else{const a=(s.toAmount!=null?s.toAmount:(s.amount!=null?s.amount:o.bet));o.inv+=Math.max(0,a-o.bet);o.bet=a;curMax=Math.max(curMax,o.bet);}}
  else if(e.type=='StreetDealt'){for(const k in seats){base+=seats[k].bet||0;seats[k].bet=0;}curMax=0;if(s.boardCards)board=s.boardCards;street=e.street||street;}
 }
 let pot=base;for(const k in seats)pot+=seats[k].bet||0;order.sort((a,b)=>a-b);
 let sb=null,bb=null,btn=null;
 if(blinds.length){sb=blinds[0].seat;bb=blinds.length>1?blinds[1].seat:null;const si=order.indexOf(sb);if(si>=0)btn=order[(si-1+order.length)%order.length];if(order.length==2)btn=sb;}
 return {seats,order,pot,board,acting,street,sb,bb,btn};
}
function chipColor(a){if(a>=800)return['#ff9a9a','#b53636'];if(a>=400)return['#ffe08a','#c98f1f'];if(a>=150)return['#8ff0c8','#179e6f'];if(a>=50)return['#9fd0ff','#2b6fa8'];return['#eef2f6','#9aa3ab'];}
function chipstk(amt){amt=Math.max(0,Math.round(amt||0));if(!amt)return '<span class="chipstk" style="height:8px"></span>';const n=Math.max(1,Math.min(6,1+Math.floor(Math.log10(amt))));const c=chipColor(amt);let s='';for(let i=0;i<n;i++)s+='<i style="bottom:'+(i*4)+'px;background:linear-gradient(180deg,'+c[0]+','+c[1]+')"></i>';return '<span class="chipstk" style="height:'+(7+(n-1)*4)+'px">'+s+'</span>';}
function minitable(h,evs,step){
 const my=h.seat,isResult=step>=evs.length,estep=Math.min(step,evs.length-1);
 const st=tstate(evs,estep);
 const full=tstate(evs,evs.length-1);
 const chips={},nameBy={};
 (h.seats||[]).forEach(s=>{if(s&&s.seat!=null){chips[s.seat]=s.chips;if(s.name)nameBy[s.seat]=s.name;}});
 ((h.result&&h.result.seats_shown)||[]).forEach(s=>{if(s.seat!=null&&nameBy[s.seat]==null&&s.name)nameBy[s.seat]=s.name;});
 for(const k in full.seats){if(nameBy[k]==null&&full.seats[k].name)nameBy[k]=full.seats[k].name;}
 const _set=new Set();(h.seats||[]).forEach(s=>{if(s&&s.seat!=null)_set.add(+s.seat);});full.order.forEach(x=>_set.add(+x));((h.result&&h.result.seats_shown)||[]).forEach(s=>{if(s.seat!=null)_set.add(+s.seat);});
 const order=[..._set].sort((a,b)=>a-b),n=order.length||1;
 if(!order.length)return '';
 const stackOf=sn=>{if(chips[sn]==null)return null;const S=chips[sn]+((full.seats[sn]&&full.seats[sn].inv)||0);return Math.max(0,Math.round(S-((st.seats[sn]&&st.seats[sn].inv)||0)));};
 const mi=Math.max(0,order.indexOf(my)),cx=50,cy=50,rx=39,ry=35;
 const xy=sn=>{const k=order.indexOf(sn),rel=((k-mi)+n)%n,a=(90+rel*360/n)*Math.PI/180;return [cx+rx*Math.cos(a),cy+ry*Math.sin(a)];};
 let bcards=st.board.length?st.board:(h.board||'').split(/[,\s]+/).filter(Boolean);
 let eff=null;order.forEach(sn=>{const o=st.seats[sn]||{};if(!o.folded){const cur=stackOf(sn);if(cur!=null)eff=(eff==null)?cur:Math.min(eff,cur);}});
 const spr=(eff!=null&&st.pot>0)?(eff/st.pot).toFixed(1):'—';
 const reveal=isResult&&h.result&&(h.result.seats_shown||[]).length;
 const shownBy={};if(reveal)(h.result.seats_shown||[]).forEach(s=>{if(s.seat!=null)shownBy[s.seat]=s.hole;});
 const winSeats=new Set(((h.result&&h.result.winners)||[]).map(w=>w.seatNumber));
 const payoutBy={},winAmtBy={};let potEnd=0;((h.result&&h.result.seats_shown)||[]).forEach(s=>{if(s.seat!=null){payoutBy[s.seat]=s.payout||0;potEnd+=(s.payout||0);}});((h.result&&h.result.winners)||[]).forEach(w=>{if(w&&w.seatNumber!=null)winAmtBy[w.seatNumber]=w.amount;});
 if(reveal&&h.result&&h.result.board){const _rb=h.result.board.split(/[,\s]+/).filter(Boolean);if(_rb.length>bcards.length)bcards=_rb;}
 const potShown=(reveal&&potEnd)?potEnd:st.pot;
 let html='<div class="ptable"><div class="pcenter"><div class="pboard">'+(chs(bcards.join(','))||'<span style="color:rgba(255,255,255,.3)">— preflop —</span>')+'</div>'+
  '<div class="potline">'+chipstk(potShown)+'<span class="potlbl">BOTE '+potShown+'</span><span class="sprlbl">· SPR <b>'+spr+'</b> · '+(reveal?(h.endStreet||'showdown'):(st.street||'').toLowerCase())+'</span></div></div>';
 order.forEach(sn=>{const o=st.seats[sn]||{};if(!reveal&&o.bet>0&&!o.folded){const p=xy(sn),bx=cx+(p[0]-cx)*0.56,by=cy+(p[1]-cy)*0.56;html+='<div class="betchip" style="left:'+bx+'%;top:'+by+'%">'+chipstk(o.bet)+'<span class="betamt">'+o.bet+'</span></div>';}});
 order.forEach(sn=>{const p=xy(sn),o=st.seats[sn]||{},mine=(sn==my);
  const isWin=reveal&&(winSeats.has(sn)||payoutBy[sn]>0);
  const ef=reveal?!isWin:o.folded;
  let badge='';if(sn==st.sb)badge='SB';else if(sn==st.bb)badge='BB';
  const name=mine?'TÚ':(nameBy[sn]?String(nameBy[sn]).slice(0,9):('as.'+sn));
  const cur=stackOf(sn),stk=(cur!=null)?'<div class="pstk">'+cur+'</div>':'';
  let cards;
  if(mine)cards='<div class="pcards">'+chs(h.hole)+'</div>';
  else if(shownBy[sn]&&shownBy[sn].length)cards='<div class="pcards">'+chs(shownBy[sn].join(','))+'</div>';
  else cards='<div class="pcards">'+(ef?'<span class="mut" style="font-size:9px">fold</span>':'<span class="cb"></span><span class="cb"></span>')+'</div>';
  const pa=reveal?(isWin?'<span class="won">+'+(winAmtBy[sn]!=null?winAmtBy[sn]:payoutBy[sn])+'</span>':'<span class="lost">fold</span>'):(o.folded?'':(o.action||''));
  html+='<div class="pseat'+(ef?' fold':'')+(!reveal&&sn==st.acting?' act':'')+(mine?' me':'')+(isWin?' win':'')+'" style="left:'+p[0]+'%;top:'+p[1]+'%">'+
   '<div class="pn">'+name+(badge?'<span class="pbadge">'+badge+'</span>':'')+'</div>'+stk+cards+
   '<div class="pa">'+pa+'</div></div>';
 });
 if(st.btn!=null){const p=xy(st.btn),dx=p[0]+(cx-p[0])*0.30,dy=p[1]+(cy-p[1])*0.30;html+='<div class="pdealer" style="left:'+dx+'%;top:'+dy+'%">D</div>';}
 return html+'</div>';
}
function buildTimeline(h){
 const my=h.seat,nrm=x=>{x=String(x||'preflop').toLowerCase();return x=='predeal'?'preflop':x;};
 let ev=(h.events||[]).filter(e=>e.type&&e.type!='Joined'&&e.type!='TableStarted'&&e.type!='HoleCardsDealt');
 ev.sort((a,b)=>((a.sequence==null?0:a.sequence)-(b.sequence==null?0:b.sequence)));
 const haveOur={};ev.forEach(e=>{if(e.type=='ActionTaken'&&e.summary&&e.summary.seatNumber==my)haveOur[nrm(e.street)]=true;});
 (h.decisions||[]).forEach(d=>{const stn=nrm(d.street);if(haveOur[stn])return;haveOur[stn]=true;
  let pos=-1;for(let i=0;i<ev.length;i++)if(nrm(ev[i].street)==stn)pos=i;
  const isAgg=(d.action=='bet'||d.action=='raise'||d.action=='all-in');
  const node={type:'ActionTaken',street:d.street,_synthetic:true,
   sequence:(pos>=0&&ev[pos].sequence!=null?ev[pos].sequence+0.5:1e9),
   summary:{seatNumber:my,agentName:'S7 test',action:d.action,amount:(d.action=='call'?d.amount:null),toAmount:(isAgg?d.amount:null)}};
  if(pos>=0)ev.splice(pos+1,0,node);else ev.push(node);});
 return ev;
}
async function openHand(key){if(!key)return;try{const r=await fetch('/api/hand?key='+encodeURIComponent(key));HAND=await r.json();HAND._ev=buildTimeline(HAND);STEP=0;EMBED=true;document.getElementById('modal').style.display='flex';renderHand();}catch(e){}}
function closeHand(){clearInterval(TMR);TMR=null;const c=document.querySelector('#modal .card');if(c)c.innerHTML='';document.getElementById('modal').style.display='none';}
function copyShare(){if(!CURURL)return;const m=document.getElementById('copymsg');const ok=()=>{if(m){m.textContent='✓ copiado';setTimeout(()=>{if(m)m.textContent='';},1400);}};
 if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(CURURL).then(ok).catch(()=>prompt('Copia el enlace:',CURURL));
 else{const ta=document.createElement('textarea');ta.value=CURURL;ta.style.position='fixed';document.body.appendChild(ta);ta.select();try{document.execCommand('copy');ok();}catch(e){prompt('Copia el enlace:',CURURL);}document.body.removeChild(ta);}}
function evtxt(e){const s=e.summary||{},st=e.street||'';
 if(e.type=='BlindPosted')return st+' · ciega '+s.amount+' (as.'+s.seatNumber+')';
 if(e.type=='StreetDealt')return st+' · reparto '+chs((s.cards||[]).join(','));
 if(e.type=='ActionTaken')return st+' · as.'+s.seatNumber+(s.agentName?(' ('+s.agentName+')'):'')+': <b>'+s.action+'</b>'+(s.amount?(' '+s.amount):'')+(s.reasoning?(' <span class=mut>'+String(s.reasoning).slice(0,52)+'</span>'):'');
 if(e.type=='HoleCardsDealt')return '— cartas repartidas —';
 if(e.type=='TableStarted')return '— inicio de mano —';
 return e.type;}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function toggleM3(el){el.classList.toggle('open');}
function showdownBlock(h){
 const r=h.result;if(!r)return '';
 const ev=h._ev||[],rank={preflop:0,predeal:0,flop:1,turn:2,river:3,showdown:4};
 const last=ev.length?String(ev[ev.length-1].street||'preflop').toLowerCase():'preflop';
 const gap=(rank[last]||0)<(rank[String(h.endStreet||'showdown').toLowerCase()]||4);
 const weFolded=(h.decisions||[]).some(d=>d.action=='fold');
 const gapnote=gap?'<div class="sdgap">↪ '+(weFolded?'tras tu fold la mano siguió sin ti':'no vimos las acciones rivales posteriores a tu última jugada')+'; resultado final (la API no entrega ese tramo — usa ▶ repro oficial para verlo entero).</div>':'';
 const w=(r.winners&&r.winners[0])||null;
 const wn=w?(w.agentName||w.agentId||'?'):'—';
 const wa=w&&w.amount!=null?(' <b style="color:#ffd877">gana +'+w.amount+'</b>'):'';
 const wh=w&&w.handName?(' con '+esc(w.handName)):'';
 const d=r.chip_delta;
 const ds=(d==null)?'':' &nbsp;·&nbsp; tú <b style="color:'+(d>=0?'#2ee6a6':'#ff5d5d')+'">'+(d>=0?'+':'')+d+'</b> chips';
 const rev=(r.seats_shown||[]).map(s=>'<span style="margin-right:12px">'+(s.name=='S7 test'?'TÚ':esc(String(s.name||('as.'+s.seat)).slice(0,12)))+' '+chs((s.hole||[]).join(','))+(s.hand?(' <span class=mut>'+esc(s.hand)+'</span>'):'')+'</span>').join('');
 const bd=r.board?(' &nbsp;·&nbsp; <span class="big">'+chs(r.board)+'</span>'):'';
 return gapnote+'<div class="sdbanner">🏆 '+esc(wn)+wa+wh+bd+ds+'</div>'+(rev?'<div class="sdreveal">'+rev+'</div>':'');
}
function streetSections(h,evs){
 const my=h.seat;const decBy={};(h.decisions||[]).forEach(d=>{(decBy[d.street]=decBy[d.street]||[]).push(d);});
 const nrm=x=>{x=(x||'preflop').toLowerCase();return x=='predeal'?'preflop':x;};
 const amtOf=s=>{if(s.action=='bet'||s.action=='raise'||s.action=='all-in')return s.toAmount!=null?s.toAmount:s.amount;if(s.action=='call')return s.amount;return null;};
 // bote acumulado al cierre de cada calle (bet/raise via toAmount)
 const potBy={};{let run=0,sb={},cm=0,cs='preflop';
  evs.forEach(e=>{const s=e.summary||{},st=nrm(e.street),sn=s.seatNumber;
   if(e.type=='StreetDealt'){potBy[cs]=run;sb={};cm=0;cs=st;}
   else if(e.type=='BlindPosted'){const a=s.amount||0;run+=Math.max(0,a-(sb[sn]||0));sb[sn]=a;cm=Math.max(cm,a);cs=st;}
   else if(e.type=='ActionTaken'){cs=st;if(s.action=='fold'||s.action=='check'){}
    else if(s.action=='call'){const a=Math.max(cm,s.amount||0);run+=Math.max(0,a-(sb[sn]||0));sb[sn]=a;cm=Math.max(cm,a);}
    else{const a=(s.toAmount!=null?s.toAmount:(s.amount||0));run+=Math.max(0,a-(sb[sn]||0));sb[sn]=a;cm=Math.max(cm,a);}}});
  potBy[cs]=run;}
 let out='',boardSoFar=[];
 ['preflop','flop','turn','river'].forEach(stn=>{
  const deal=evs.find(e=>e.type=='StreetDealt'&&nrm(e.street)==stn);
  if(deal&&deal.summary&&deal.summary.boardCards)boardSoFar=deal.summary.boardCards;
  else if(stn!='preflop'&&h.result&&h.result.board){const _rb=h.result.board.split(/[,\s]+/).filter(Boolean),_need={flop:3,turn:4,river:5}[stn];if(_rb.length>=_need)boardSoFar=_rb.slice(0,_need);}
  const acts=evs.filter(e=>nrm(e.street)==stn&&(e.type=='ActionTaken'||e.type=='BlindPosted'));
  const dec=decBy[stn]||[];
  if(!acts.length&&!dec.length)return;
  const body=acts.map(e=>{const s=e.summary||{},mine=s.seatNumber!=null&&s.seatNumber==my;
   const who=mine?'TÚ':('as.'+s.seatNumber+(s.agentName?(' '+esc(String(s.agentName).slice(0,10))):''));
   const am=amtOf(s);
   const act=e.type=='BlindPosted'?('ciega '+s.amount):(esc(s.action||'')+(am!=null?' <span class="amt">'+am+'</span>':''));
   return '<div class="sa'+(mine?' mine':'')+'">'+who+' · <b>'+act+'</b></div>';}).join('');
  const reads=dec.map(d=>{
   const po=(d.call&&((d.pot||0)+d.call))?(' · po '+Math.round(100*d.call/((d.pot||0)+d.call))+'%'):'';
   let m3='';if(d.m3)m3='<div class="m3box" onclick="toggleM3(this)"><span class="mut">▾ respuesta M3 ('+esc(d.m3.model||'M3')+')</span><div class="m3body"><b>answer:</b>\n'+esc(d.m3.answer||'')+'\n\n<b>think:</b>\n'+esc(d.m3.think||'(sin think)')+'</div></div>';
   return '<div class="readline">▷ nuestra <b>'+esc(d.action||'')+(d.amount?(' '+d.amount):'')+'</b> · '+(d.strength||'?')+' · SPR '+d.spr+(d.outs?(' · '+d.outs+' outs'):'')+po+(d.engine=='M3'?' <span style="color:#f5b73d">[M3]</span>':'')+'</div>'+m3;}).join('');
  const pv=potBy[stn];const pothdr=(pv!=null)?'<span class="stpot">bote '+pv+'</span>':'';
  out+='<div class="street"><div class="sthead">'+stn+(stn!='preflop'&&boardSoFar.length?' <span class="big">'+chs(boardSoFar.join(','))+'</span>':'')+pothdr+'</div>'+body+reads+'</div>';
 });
 return out;
}
function renderHand(){const h=HAND;if(!h)return;
 const card=document.querySelector('#modal .card');
 const ru=(h.result&&h.result.replay_url)||'';CURURL=ru;
 const embed=!!(ru&&EMBED);
 const share=ru?(' <a class="rlink" href="'+esc(ru)+'" target="_blank" rel="noopener">🔗 abrir en dev.fun</a> <button class="eqbtn" onclick="copyShare()">copiar enlace</button> <button class="eqbtn" onclick="EMBED=!EMBED;renderHand()">'+(embed?'ver reconstrucción local':'ver repro oficial')+'</button> <span id="copymsg" class="mut"></span>'):'';
 const head='<div class="mh"><b>▶ Reproductor de mano</b> <span class="mut">'+(h.key||'')+'</span>'+share+'<span style="float:right;cursor:pointer" onclick="closeHand()">✕</span></div>';
 card.classList.toggle('wide',embed);
 if(embed){
  card.innerHTML=head+'<div class="mb" style="padding:6px"><iframe src="'+esc(ru)+'" allow="fullscreen" referrerpolicy="no-referrer" style="width:100%;height:74vh;border:0;border-radius:8px;background:#0a0e13"></iframe></div>';
  return;
 }
 const ev=h._ev||[];
 const hasRes=!!(h.result&&((h.result.seats_shown||[]).length||(h.result.winners||[]).length));
 const total=ev.length+(hasRes?1:0),maxStep=Math.max(0,total-1);
 if(STEP>maxStep)STEP=maxStep;if(STEP<0)STEP=0;
 const isResult=hasRes&&STEP>=ev.length,my=h.seat;
 const lbl=isResult?'<b style="color:#ffd877">RESULTADO</b>':(ev.length?'<span class="mut">'+evtxt(ev[STEP])+'</span>':'');
 const ctl=ev.length?('<div class="ctl"><button onclick="STEP=Math.max(0,STEP-1);renderHand()">◀ prev</button><button onclick="playHand()">▶ play</button><button onclick="STEP=Math.min('+maxStep+',STEP+1);renderHand()">next ▶▶</button> <span class="mut">paso '+(STEP+1)+' / '+total+'</span> &nbsp; '+lbl+'</div>'):'';
 const fb=ev.length?'':('<div>Tus cartas <span class="big">'+(chs(h.hole)||'?')+'</span> <span class="mut">· asiento '+(my||'?')+'</span></div><div style="margin:8px 0">Board <span class="big">'+(chs((h.board||'').split(/[,\s]+/).filter(Boolean).join(','))||'—')+'</span></div>'+(h.decisions||[]).map(d=>'<span class="chip" style="display:inline-block;margin:2px 3px 0 0">'+d.street+': '+(d.strength||'')+' → <b>'+esc(d.action||'')+'</b>'+(d.amount?(' '+d.amount):'')+'</span>').join(''));
 const note=ru?'':'<div class="sdgap">repro oficial no disponible para esta mano — reconstrucción local (limitada por la API).</div>';
 card.innerHTML=head+'<div class="mb">'+note+(isResult?showdownBlock(h):'')+(ev.length?minitable(h,ev,STEP):'')+ctl+(ev.length?streetSections(h,ev):fb)+'</div>';
}
function playHand(){clearInterval(TMR);const ev=HAND._ev||[],hasRes=!!(HAND.result&&((HAND.result.seats_shown||[]).length||(HAND.result.winners||[]).length)),max=ev.length+(hasRes?1:0)-1;TMR=setInterval(()=>{if(STEP>=max){clearInterval(TMR);return;}STEP++;renderHand();},850);}
/* ---------- MANOS tab ---------- */
let HANDS=[],lastHands=0,HSORT={col:'ts',dir:-1};
function showTab(t){
 ['panel','hands','players','coach','run','rank','mllm'].forEach(v=>{document.getElementById(v+'view').style.display=(v==t)?'':'none';document.getElementById('tab-'+v).classList.toggle('on',v==t);});
 if(t=='hands')loadHands();
 if(t=='players')loadPlayers();
 if(t=='coach')loadCoach();
 if(t=='run')loadRuns();
 if(t=='rank')loadRank();
 if(t=='mllm')loadMLLM();
}
let PLAYERS=[];
async function loadPlayers(){try{const r=await fetch('/api/players');const d=await r.json();PLAYERS=d.players||[];renderPlayers();}catch(e){}}
function renderPlayers(){
 const el=document.getElementById('players');
 if(!PLAYERS.length){el.innerHTML='<div class="mut" style="padding:16px">aún sin datos de rivales — se llena conforme se juegan manos con showdown…</div>';return;}
 const pct=x=>x==null?'—':((x<=1?x*100:x).toFixed(1)+'%');
 el.innerHTML=PLAYERS.map(p=>{const h=p.hud||{};
  const holds=(p.hands||[]).map(x=>'<span class="phand'+(x.won?' won':'')+'" data-k="'+encodeURIComponent(x.key||'')+'" title="'+esc((x.hand||'')+(x.board?(' · '+x.board):''))+'">'+(chs((x.hole||[]).join(','))||'?')+'</span>').join('');
  return '<div class="pcard"><h4>'+esc(p.name||'?')+'<span class="atag">'+p.archetype+'</span>'+(p.seen?'<span class="mut" style="font-size:11px;font-weight:400"> · vistas '+p.seen+' · ganó '+p.wins+'</span>':'')+'</h4>'+
   '<div><span class="st">N <b>'+(h.n!=null?Number(h.n).toLocaleString():'—')+'</b></span><span class="st">VPIP <b>'+pct(h.vpip)+'</b></span><span class="st">PFR <b>'+pct(h.pfr)+'</b></span><span class="st">AF <b>'+(h.af!=null?h.af.toFixed(1):'—')+'</b></span><span class="st">WTSD <b>'+pct(h.wtsd)+'</b></span><span class="st">WSD <b>'+pct(h.wsd)+'</b></span><span class="st">estilo <b>'+esc((h.style&&(h.style.label||h.style.tightness))||'?')+'</b></span></div>'+
   (holds?'<div class="holds"><span class="mut">manos que enseñó (click → reproductor):</span><br>'+holds+'</div>':'')+
  '</div>';}).join('');
}
async function loadHands(){try{const r=await fetch('/api/hands');const d=await r.json();HANDS=d.hands||[];lastHands=Date.now();renderHands();}catch(e){}}
function renderHands(){
 const f=(document.getElementById('hfilter').value||'').toLowerCase().trim();
 let rows=HANDS.filter(h=>!f||(h.pos||'').toLowerCase().includes(f)||(h.hole||'').toLowerCase().includes(f)||(h.hclass||'').toLowerCase().includes(f)||(h.reached||'').toLowerCase().includes(f)||(h.label||'').toLowerCase().includes(f)||(h.moves||'').toLowerCase().includes(f)||(f=='m3'&&h.m3>0)||(f=='win'&&h.won)||(f=='loss'&&h.delta!=null&&h.delta<0));
 const SC=HSORT.col,SD=HSORT.dir,STR={ts:1,label:1,pos:1,hole:1,board:1,reached:1,moves:1};
 rows=rows.slice().sort((a,b)=>{let x=a[SC],y=b[SC];if(STR[SC]){x=(x||'').toString();y=(y||'').toString();return SD*x.localeCompare(y);}x=(x==null?-Infinity:x);y=(y==null?-Infinity:y);return SD*((x>y)-(x<y));});
 document.getElementById('hcount').textContent=rows.length+' / '+HANDS.length+' manos';
 const cols=[['ts','hora'],['label','arm'],['pos','pos'],['hole','mano'],['board','board'],['reached','calle'],['spr_pf','SPR pf'],['spr_post','SPR post'],['moves','movimientos'],['m3','M3'],['pot','bote'],['delta','result']];
 const spf=v=>v==null?'<span class=mut>—</span>':(+v).toFixed(1);
 let html='<table class="htab"><thead><tr>'+cols.map(c=>'<th data-sort="'+c[0]+'">'+c[1]+(SC==c[0]?(SD>0?' ▲':' ▼'):'')+'</th>').join('')+'</tr></thead><tbody>';
 html+=rows.map(h=>{const t=new Date(h.ts*1000).toLocaleTimeString();const tag=h.label=='wide'?'#f5b73d':'#2ee6a6';
  const res=(h.delta==null)?'<span class=mut>·</span>':('<b style="color:'+(h.delta>=0?'#2ee6a6':'#ff5d5d')+'">'+(h.delta>=0?'+':'')+h.delta+'</b>');
  const lb=(h.delta==null||h.delta==0)?'':('border-left:2px solid '+(h.delta>0?'#2ee6a6':'#ff5d5d'));
  return '<tr data-k="'+encodeURIComponent(h.key)+'" style="'+lb+'"><td class="mut">'+t+'</td><td><span class="armtag" style="color:'+tag+';border-color:'+tag+'">'+(h.label||'·')+'</span></td><td>'+(h.pos||'')+'</td><td class="hcards">'+(chs(h.hole)||'<span class=mut>—</span>')+'</td><td class="hcards">'+(chs(h.board)||'<span class=mut>—</span>')+'</td><td>'+h.reached+'</td><td>'+spf(h.spr_pf)+'</td><td>'+spf(h.spr_post)+'</td><td class="hmoves">'+(h.moves||'')+'</td><td>'+(h.m3?('<span class="eM3">'+h.m3+'</span>'):'')+'</td><td>'+h.pot+'</td><td>'+res+'</td></tr>';}).join('');
 html+='</tbody></table>';
 document.getElementById('hands').innerHTML=html;
}
/* ---------- COACH tab ---------- */
let cwin='all';                 /* ventana del diagnóstico: 'all' | '10000' */
let brState=null;               /* estado del builder de estrategia */
const RKS="AKQJT98765432";
const POS6=['UTG','MP','CO','BTN','SB','BB'];
function combo(i,j){if(i==j)return RKS[i]+RKS[i];if(i<j)return RKS[i]+RKS[j]+'s';return RKS[j]+RKS[i]+'o';}
async function loadCoach(){
 const el=document.getElementById('coach');el.innerHTML='<div class="mut" style="padding:14px">cargando…</div>';
 try{const d=await (await fetch('/api/coach?window='+cwin)).json();
  let h='<div class="pcard"><h4>Diagnóstico · ventana</h4>'
    +'<span class="chip'+(cwin=='all'?' on':'')+'" data-win="all">todas las manos</span>'
    +'<span class="chip'+(cwin=='10000'?' on':'')+'" data-win="10000">últimas 10k</span>'
    +'<span class="mut" style="margin-left:8px">'+(d.win_hands!=null?('analizando '+Number(d.win_hands).toLocaleString()+' manos'):'')+'</span></div>';
  if(d.locked){const pc=Math.min(100,Math.round(100*d.hands/d.need));
   h+='<div class="pcard"><h4>Diagnóstico bloqueado 🔒</h4><div class="mut">Se necesitan '+d.need.toLocaleString()+' manos para el análisis de leaks. Llevas <b>'+d.hands.toLocaleString()+'</b>. (Puedes crear una estrategia «ideal desde cero» abajo.)</div><div class="track" style="margin-top:8px"><div class="fill" style="width:'+pc+'%;background:var(--grn)"></div><div class="fv">'+pc+'%</div></div></div>';
  }else{
   const vcol=v=>v=='✓'?'#2ee6a6':v=='⚠'?'#f5b73d':v=='✗'?'#ff5d5d':'#5a6675';
   let pan=d.vs_panel?('<div class="mut" style="margin-bottom:7px">Resultado real vs panel near-GTO (DeepCFR): <b class="'+((d.vs_panel.bb100||0)>=0?'posv':'neg')+'">'+((d.vs_panel.bb100||0)>=0?'+':'')+d.vs_panel.bb100+' bb/100</b> <span class="mut">('+d.vs_panel.runs+' runs · '+Number(d.vs_panel.hands||0).toLocaleString()+' manos)</span></div>'):'';
   h+='<div class="pcard"><h4>Tu juego vs el mejor juego</h4>'+pan
     +'<table class="vsopt"><thead><tr><th>métrica</th><th>tú</th><th>óptimo</th><th style="text-align:center">veredicto</th><th>nota</th></tr></thead><tbody>'
     +(d.vs_opt||[]).map(o=>'<tr><td><b>'+esc(o.k)+'</b></td><td>'+esc(o.you)+'</td><td class="mut">'+esc(o.target)+'</td><td class="vd" style="color:'+vcol(o.verdict)+'">'+esc(o.verdict)+'</td><td class="mut">'+esc(o.note)+'</td></tr>').join('')
     +'</tbody></table><div class="mut" style="margin-top:6px;font-size:11px">«Mejor juego» = bandas GTO 6-max estándar + resultado real contra el panel near-GTO (no hay solver exacto en el entorno).</div></div>';
   if((d.advice||[]).length)h+='<div class="pcard"><h4>Consejos (reglas)</h4>'+d.advice.map(a=>'<div class="cadv">▷ '+esc(a)+'</div>').join('')+'</div>';
   if((d.posres||[]).length)h+='<div class="pcard"><h4>Ganancia/pérdida por posición</h4>'+d.posres.map(p=>'<span class="st">'+p.pos+' <b style="color:'+((p.delta||0)>=0?"#2ee6a6":"#ff5d5d")+'">'+((p.delta||0)>=0?"+":"")+p.delta+'</b> <span class="mut">('+p.n+'m)</span></span>').join('')+'</div>';
   if((d.ab||[]).length)h+='<div class="pcard"><h4>A/B estrategias (bb/100)</h4>'+d.ab.map(a=>'<span class="st">'+esc(a.label)+' <b>'+(a.bb100==null?'—':(a.bb100>=0?'+':'')+a.bb100)+'</b> <span class="mut">(n'+a.n+')</span></span>').join('')+'</div>';
  }
  h+='<div class="pcard"><h4>Coach IA · análisis (MiniMax M3)</h4><button class="eqbtn on" style="--c:#b98bff" onclick="coachLLM()">🧠 pedir consejo a M3</button><div id="coachllm" style="margin-top:8px;white-space:pre-wrap;font-size:12px;line-height:1.5"></div></div>';
  h+='<div class="pcard"><h4>Crear estrategia · jugador pro 🃏</h4>'
    +'<div class="rform"><span>base IA:</span>'
      +'<label><input type="radio" name="sgmode" value="leaks" checked> corregir mis leaks</label>'
      +'<label><input type="radio" name="sgmode" value="scratch"> ideal desde cero</label>'
      +'<button class="eqbtn on" style="--c:#b98bff" onclick="genStrategy()">🧠 generar con IA</button>'
      +'<span style="margin-left:8px">o a mano:</span>'
      +'<button class="eqbtn" data-tpl="std">✎ plantilla std</button>'
      +'<button class="eqbtn" data-tpl="wide">✎ plantilla wide</button>'
      +'<span id="sg-msg" class="mut" style="margin-left:6px"></span></div>'
    +'<div class="mut" style="margin-top:5px;font-size:11px">La IA propone una estrategia completa; revísala y edítala en la rejilla (clic = añade/quita mano), ajusta las cualidades, ponle nombre y guárdala. Quedará disponible para el agente en RUN/RANK.</div>'
    +'<div id="builder" style="margin-top:10px"></div></div>';
  el.innerHTML=h;
 }catch(e){el.innerHTML='<div class="mut" style="padding:14px">error cargando coach</div>';}
}
async function coachLLM(){
 const o=document.getElementById('coachllm');if(!o)return;let n=0;
 const render=d=>{let h='<div style="white-space:pre-wrap">'+esc(d.text||'')+'</div>';
  if(d.version&&d.proposal)h+='<div class="pcard" style="margin-top:8px"><b>Propuesta de versión: '+esc(d.version)+'</b><pre class="runlog" style="max-height:220px">'+esc(JSON.stringify(d.proposal,null,1))+'</pre><button class="eqbtn on" data-launchv="'+esc(d.version)+'">▶ lanzar '+esc(d.version)+' (vs fijo)</button> <span id="cv-msg" class="mut"></span></div>';
  o.innerHTML=h;};
 const poll=async()=>{
  try{const d=await (await fetch('/api/coach/llm?window='+cwin)).json();
   if(d.locked){o.textContent='bloqueado: '+d.hands+'/'+d.need+' manos';return;}
   if(d.error){o.innerHTML='<span class="neg">error: '+esc(d.error)+'</span>';return;}
   if(d.running){n++;o.innerHTML='<span class="mut">⏳ M3 está analizando las manos… ('+(n*4)+'s · puede tardar ~60s, no cierres la pestaña)</span>';if(n<75)setTimeout(poll,4000);else o.innerHTML='<span class="neg">M3 tarda demasiado; reintenta.</span>';return;}
   render(d);
  }catch(e){o.textContent='error consultando M3';}
 };
 o.innerHTML='<span class="mut">⏳ pidiendo análisis a M3…</span>';poll();
}
function launchVersion(v){const m=document.getElementById('cv-msg');if(m)m.textContent='lanzando…';
 fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:v,strat:v,engine:'hybrid',matches:5})}).then(r=>r.json()).then(d=>{if(m)m.innerHTML=d.ok?'<span class="posv">lanzado '+esc(d.unit)+'</span>':'<span class="neg">'+esc(d.error||'error')+'</span>';}).catch(()=>{if(m)m.textContent='error';});
}
/* ---------- COACH · creador de estrategias ---------- */
async function genStrategy(){
 const mode=(document.querySelector('input[name=sgmode]:checked')||{}).value||'leaks';
 const msg=document.getElementById('sg-msg'),b=document.getElementById('builder');if(!msg)return;let n=0;
 const poll=async()=>{
  try{const d=await (await fetch('/api/coach/strategy?window='+cwin+'&mode='+mode)).json();
   if(d.locked){msg.innerHTML='<span class="neg">bloqueado: '+d.hands+'/'+d.need+' manos (usa «ideal desde cero»)</span>';return;}
   if(d.error){msg.innerHTML='<span class="neg">error: '+esc(d.error)+'</span>';return;}
   if(d.running){n++;msg.innerHTML='<span class="mut">⏳ el jugador pro está diseñando la estrategia… ('+(n*4)+'s · ~100s, no cierres)</span>';if(n<75)setTimeout(poll,4000);else msg.innerHTML='<span class="neg">M3 tarda demasiado; reintenta.</span>';return;}
   msg.innerHTML='<span class="posv">✓ propuesta lista — revísala, nómbrala y guárdala</span>';mountBuilder(d);
  }catch(e){msg.textContent='error consultando M3';}
 };
 msg.innerHTML='<span class="mut">⏳ pidiendo estrategia a M3…</span>';if(b)b.innerHTML='';poll();
}
async function loadTemplate(base,name){
 const msg=document.getElementById('sg-msg');if(msg)msg.innerHTML='<span class="mut">cargando plantilla…</span>';
 try{const d=await (await fetch('/api/strats/template?base='+encodeURIComponent(base||'std')+(name?('&name='+encodeURIComponent(name)):''))).json();
  if(msg)msg.innerHTML='';mountBuilder(d);
 }catch(e){if(msg)msg.textContent='error';}
}
function mountBuilder(d){
 brState={base:d.base||'std',ranges:{},knobs:Object.assign({},d.knobs||{}),limits:d.knob_limits||{},
          tbv:(d.threebet_value||[]).join(' '),tbb:(d.threebet_bluff||[]).join(' ')};
 POS6.forEach(p=>{brState.ranges[p]=new Set((d.ranges&&d.ranges[p])||[]);});
 renderBuilder(d.prose||'');
}
function gridHTML(p){
 const set=brState.ranges[p];let cells='';
 for(let i=0;i<13;i++){cells+='<tr>';for(let j=0;j<13;j++){const cmb=combo(i,j);const on=set.has(cmb);
  cells+='<td class="'+(i==j?'pair':'')+(on?' on':'')+'" data-pos="'+p+'" data-cmb="'+cmb+'">'+cmb.slice(0,2)+'</td>';}cells+='</tr>';}
 return '<div class="rgpos"><h5><span>'+p+'</span><span class="mut" id="cnt-'+p+'">'+set.size+'</span></h5><table class="rg"><tbody>'+cells+'</tbody></table></div>';
}
function renderBuilder(prose){
 const b=document.getElementById('builder');if(!b||!brState)return;let h='';
 if(prose)h+='<div class="mut" style="margin-bottom:8px;white-space:pre-wrap">'+esc(prose)+'</div>';
 h+='<div class="rform"><label>nombre <input id="b-name" class="binp" placeholder="ej: pro1" maxlength="24" style="width:120px"></label>'
   +'<label>base <select id="b-base" class="binp"><option value="std">std</option><option value="wide">wide</option></select></label>'
   +'<span class="mut">clic en una celda = añade/quita esa mano del rango</span></div>';
 h+='<div class="rgwrap">'+POS6.map(p=>gridHTML(p)).join('')+'</div>';
 const klab={open_size_bb:'tamaño apertura (bb)',threebet_mult:'multiplicador 3bet',value_eq:'equity de valor',station_mult:'mult. vs estación',cbet_bluff_frac:'frac. cbet farol',commit_spr:'SPR de commit',perejil_flop:'perejil flop',perejil_turn:'perejil turn',perejil_relief:'perejil alivio'};
 h+='<h5 style="margin:12px 0 0;font-size:11px;color:var(--txt)">Cualidades (knobs)</h5><div class="kgrid">'
   +Object.keys(klab).map(k=>{const lim=brState.limits[k]||[0,99];const v=brState.knobs[k];return '<label>'+klab[k]+' <span class="mut">('+lim[0]+'–'+lim[1]+')</span><input type="number" step="0.05" id="k-'+k+'" value="'+(v==null?'':v)+'" min="'+lim[0]+'" max="'+lim[1]+'"></label>';}).join('')+'</div>';
 h+='<div class="rform" style="margin-top:10px"><label>3bet valor <input id="tb-value" class="binp" style="width:300px" value="'+esc(brState.tbv)+'"></label>'
   +'<label>3bet farol <input id="tb-bluff" class="binp" style="width:300px" value="'+esc(brState.tbb)+'"></label></div>';
 h+='<div class="rform" style="margin-top:10px"><button class="eqbtn on" style="--c:#2ee6a6" onclick="saveStrategy()">💾 guardar estrategia</button><span id="sv-msg" class="mut"></span></div>';
 b.innerHTML=h;const bs=document.getElementById('b-base');if(bs)bs.value=brState.base;
}
async function saveStrategy(){
 if(!brState)return;const m=document.getElementById('sv-msg');
 const name=((document.getElementById('b-name')||{}).value||'').trim().toLowerCase();
 if(!/^[a-z0-9_-]{1,24}$/.test(name)){m.innerHTML='<span class="neg">nombre inválido (a-z 0-9 _ - , máx 24)</span>';return;}
 const base=(document.getElementById('b-base')||{}).value||'std';
 const opening={};POS6.forEach(p=>{opening[p]=Array.from(brState.ranges[p]);});
 const knobs={};Object.keys(brState.limits).forEach(k=>{const el=document.getElementById('k-'+k);if(el&&el.value!=='')knobs[k]=parseFloat(el.value);});
 const sp=s=>(s||'').split(/[\s,]+/).filter(Boolean);
 const body={name:name,base:base,opening_ranges:opening,knobs:knobs,threebet_value:sp((document.getElementById('tb-value')||{}).value),threebet_bluff:sp((document.getElementById('tb-bluff')||{}).value)};
 m.innerHTML='<span class="mut">guardando…</span>';
 try{const d=await (await fetch('/api/strats/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok)m.innerHTML='<span class="posv">✓ guardada «'+esc(d.name)+'» — disponible en RUN. </span><button class="eqbtn" data-test="'+esc(d.name)+'">▶ probar (clasificatoria 500)</button>';
  else m.innerHTML='<span class="neg">'+esc(d.error||'error')+'</span>';
 }catch(e){m.textContent='error';}
}
/* ---------- RUN tab ---------- */
function loadRuns(){
 document.getElementById('run').innerHTML='<div class="pcard"><h4>Lanzar entrenamiento</h4>'+
  '<div class="rform">etiqueta <input id="r-label" placeholder="ej: wide2" maxlength="24">'+
   'versión <select id="r-strat"><option value="">(usar rangos)</option></select>'+
   'rangos <select id="r-ranges"><option>std</option><option>wide</option></select>'+
   'motor <select id="r-engine"><option>hybrid</option><option>heur</option></select>'+
   'partidas <input id="r-matches" type="number" value="20" min="1" max="200" style="width:64px">'+
   'M3 tokens <input id="r-tok" type="number" placeholder="auto" style="width:74px">'+
   'M3 deadline <input id="r-dl" type="number" placeholder="30" style="width:64px">'+
   '<button class="eqbtn on" onclick="launchRun()">▶ lanzar</button></div>'+
  '<div id="r-msg" class="mut" style="margin-top:6px"></div></div>'+
  '<div class="pcard"><h4>🏁 Clasificatoria · 500 manos (Eval oficial)</h4>'+
   '<div class="rform">versión <select id="c-strat"><option value="std">std</option></select>'+
    'motor <select id="c-engine"><option>hybrid</option><option>heur</option></select>'+
    'nombre <input id="c-name" placeholder="System 7" maxlength="32" style="width:140px">'+
    '<button class="eqbtn on" onclick="launchClasif()">▶ jugar 500 clasificatorias</button></div>'+
   '<div class="mut" style="margin-top:5px">Juega UNA partida Eval de 500 manos (seed_poker_eval_s1) contra el panel near-GTO con la versión elegida, registrando un agente nuevo con ese nombre. El Eval es one-shot por agente; el resultado (bb/100) aparece abajo y en la curva de equity.</div>'+
   '<div id="c-msg" class="mut" style="margin-top:6px"></div></div>'+
  '<div class="pcard"><h4>🧪 Multi-run · lote de clasificatorias</h4>'+
   '<div class="rform">versión <select id="b-strat"><option value="wide">wide</option></select>'+
    'motor <select id="b-engine"><option>hybrid</option><option>heur</option></select>'+
    'nº runs <input id="b-total" type="number" value="20" min="1" max="300" style="width:64px">'+
    'a la vez <input id="b-maxc" type="number" value="4" min="1" max="8" style="width:52px">'+
    '<button class="eqbtn on" onclick="launchBatch()">▶ lanzar lote</button></div>'+
   '<div class="mut" style="margin-top:5px">Lanza N clasificatorias de 500 manos (reclamables) en oleadas, igual que por CLI. Aparecen en RANK conforme terminan. El runner «batch-…» sale abajo en la lista (botón parar para cortarlo).</div>'+
   '<div id="b-msg" class="mut" style="margin-top:6px"></div></div>'+
  '<div class="pcard"><h4>Entrenamientos</h4>'+
   '<div class="rform" style="margin:0 0 7px"><button class="eqbtn" data-clean="stopall">⏹ parar todas</button>'+
    '<button class="eqbtn" data-clean="failed">🧹 limpiar fallidas</button>'+
    '<button class="eqbtn" data-clean="completed">🧹 limpiar completadas</button>'+
    '<button class="eqbtn" data-clean="small">🗑 borrar &lt;50 manos</button> <span id="rc-msg" class="mut"></span></div>'+
   '<div id="r-list" class="mut">cargando…</div></div>'+
  '<div class="pcard"><h4>Debug en vivo · <select id="r-logunit" onchange="pollLog()"></select></h4><pre id="r-log" class="runlog">selecciona un entrenamiento…</pre></div>';
 refreshRuns();
 fetch('/api/strats').then(r=>r.json()).then(d=>{const opts=(d.strats||[]).map(x=>'<option>'+esc(x.name)+'</option>').join('');const s=document.getElementById('r-strat');if(s)s.innerHTML='<option value="">(usar rangos)</option>'+opts;const c=document.getElementById('c-strat');if(c)c.innerHTML=opts||'<option value="std">std</option>';const b=document.getElementById('b-strat');if(b){b.innerHTML=opts||'<option value="wide">wide</option>';b.value='wide';}}).catch(()=>{});
}
async function refreshRuns(){
 const el=document.getElementById('r-list');if(!el)return;
 try{const d=await (await fetch('/api/runs')).json();const runs=d.runs||[];
  el.innerHTML='<table class="htab"><thead><tr><th>run</th><th>rangos</th><th>motor</th><th>estado</th><th>partidas</th><th>bb/100</th><th></th></tr></thead><tbody>'+
   runs.map(r=>{const up=r.state=='active';return '<tr><td><b>'+esc(r.label)+'</b>'+(r.fixed?' <span class=mut>(fijo)</span>':'')+'</td><td>'+(r.ranges||'?')+'</td><td>'+(r.engine||'?')+'</td><td><span class="dot '+(up?'up':(r.state=='activating'?'warn':'down'))+'"></span>'+r.state+'</td><td>'+(r.matches||0)+'</td><td>'+(r.bb100==null?'—':(r.bb100>=0?'+':'')+r.bb100)+'</td><td>'+(up?'<button class="eqbtn" data-stop="'+esc(r.unit)+'">parar</button>':'')+(String(r.label||'').indexOf('clasif')==0?' <button class="eqbtn" data-claim="'+esc(r.label)+'">🏆 reclamar</button>':'')+'</td></tr>';}).join('')+'</tbody></table>';
  const sel=document.getElementById('r-logunit');
  if(sel){const cur=sel.value;sel.innerHTML=runs.map(r=>'<option value="'+esc(r.unit)+'">'+esc(r.label)+(r.fixed?'':' (run)')+'</option>').join('');if(cur&&runs.some(r=>r.unit==cur))sel.value=cur;pollLog();}
 }catch(e){el.textContent='error';}
}
async function pollLog(){
 const sel=document.getElementById('r-logunit'),pre=document.getElementById('r-log');if(!sel||!pre||!sel.value)return;
 try{const d=await (await fetch('/api/run/log?unit='+encodeURIComponent(sel.value)+'&n=80')).json();
  const atBottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-24;
  pre.textContent=d.log||d.error||'(sin salida todavía)';
  if(atBottom)pre.scrollTop=pre.scrollHeight;
 }catch(e){}
}
async function launchBatch(){
 const m=document.getElementById('b-msg');
 const body={strat:(document.getElementById('b-strat')||{}).value||'wide',engine:document.getElementById('b-engine').value,total:+document.getElementById('b-total').value||20,maxc:+document.getElementById('b-maxc').value||4};
 m.textContent='lanzando lote…';
 try{const d=await (await fetch('/api/run/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  m.innerHTML=d.ok?('<span class="posv">▶ lote lanzado: '+esc(d.unit)+' — '+d.total+' runs de <b>'+esc(body.strat)+'</b> ('+body.maxc+' a la vez)</span>'):('<span class="neg">'+esc(d.error||'error')+'</span>');
  if(d.ok)setTimeout(refreshRuns,1000);
 }catch(e){m.textContent='error';}
}
async function launchRun(){
 const m=document.getElementById('r-msg');
 const body={label:(document.getElementById('r-label').value||'').trim(),strat:(document.getElementById('r-strat')||{}).value||'',ranges:document.getElementById('r-ranges').value,engine:document.getElementById('r-engine').value,matches:+document.getElementById('r-matches').value,max_tokens:+document.getElementById('r-tok').value||0,min_deadline:+document.getElementById('r-dl').value||0};
 m.textContent='lanzando…';
 try{const d=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  m.innerHTML=d.ok?'<span class="posv">lanzado: '+esc(d.unit)+'</span>':'<span class="neg">'+esc(d.error||'error')+'</span>';
  if(d.ok)setTimeout(refreshRuns,900);
 }catch(e){m.textContent='error';}
}
async function launchClasif(){
 const m=document.getElementById('c-msg');
 const strat=(document.getElementById('c-strat')||{}).value||'std';
 const engine=document.getElementById('c-engine').value;
 const name=(document.getElementById('c-name').value||'').trim();
 const label=('clasif-'+strat).slice(0,18)+'-'+(Date.now()%10000);
 m.textContent='lanzando clasificatoria de 500 manos…';
 try{const d=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label,strat,ranges:'std',engine,matches:1,name})})).json();
  m.innerHTML=d.ok?'<span class="posv">🏁 clasificatoria lanzada: '+esc(d.unit)+' — 500 manos, versión <b>'+esc(strat)+'</b>'+(name?(' como «'+esc(name)+'»'):'')+'</span>':'<span class="neg">'+esc(d.error||'error')+'</span>';
  if(d.ok)setTimeout(refreshRuns,900);
 }catch(e){m.textContent='error';}
}
/* ---------- wiring ---------- */
document.addEventListener('keydown',e=>{if(e.key=='Escape')closeHand();});
document.getElementById('tick').addEventListener('click',e=>{const row=e.target.closest('[data-k]');if(row&&row.dataset.k)openHand(decodeURIComponent(row.dataset.k));});
document.getElementById('hands').addEventListener('click',e=>{const th=e.target.closest('th[data-sort]');if(th){const cc=th.dataset.sort;if(HSORT.col==cc)HSORT.dir*=-1;else{HSORT.col=cc;HSORT.dir=(cc=='ts'||cc=='delta'||cc=='pot')?-1:1;}renderHands();return;}const row=e.target.closest('tr[data-k]');if(row&&row.dataset.k)openHand(decodeURIComponent(row.dataset.k));});
document.getElementById('players').addEventListener('click',e=>{const el=e.target.closest('[data-k]');if(el&&el.dataset.k)openHand(decodeURIComponent(el.dataset.k));});
document.getElementById('coach').addEventListener('click',e=>{
 const cell=e.target.closest('td[data-cmb]');
 if(cell&&brState&&brState.ranges[cell.dataset.pos]){const p=cell.dataset.pos,cmb=cell.dataset.cmb,set=brState.ranges[p];
  if(set.has(cmb)){set.delete(cmb);cell.classList.remove('on');}else{set.add(cmb);cell.classList.add('on');}
  const cnt=document.getElementById('cnt-'+p);if(cnt)cnt.textContent=set.size;return;}
 const wb=e.target.closest('[data-win]');if(wb){cwin=wb.dataset.win;loadCoach();return;}
 const tp=e.target.closest('[data-tpl]');if(tp){loadTemplate(tp.dataset.tpl);return;}
 const tb=e.target.closest('[data-test]');
 if(tb){const nm=tb.dataset.test,label=('clasif-'+nm).slice(0,18)+'-'+(Date.now()%10000);tb.textContent='lanzando…';
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:label,strat:nm,ranges:'std',engine:'hybrid',matches:1,name:nm})}).then(r=>r.json()).then(d=>{tb.textContent=d.ok?('▶ lanzada ('+d.unit+')'):(d.error||'error');}).catch(()=>{tb.textContent='error';});return;}
 const b=e.target.closest('[data-launchv]');if(b)launchVersion(b.dataset.launchv);});
document.getElementById('eqctl').addEventListener('click',e=>{const b=e.target.closest('[data-eq]');if(!b)return;const k=b.dataset.eq;if(k=='__ev')EQOPT.ev=!EQOPT.ev;else EQOPT.off[k]=!EQOPT.off[k];drawEquity();});
document.getElementById('run').addEventListener('click',async e=>{
 const sb=e.target.closest('[data-stop]');
 if(sb){sb.textContent='…';try{await fetch('/api/run/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({unit:sb.dataset.stop})});}catch(_){}setTimeout(refreshRuns,700);return;}
 const xb=e.target.closest('[data-clean]');
 if(xb){const mode=xb.dataset.clean,rm=document.getElementById('rc-msg');if(rm)rm.textContent='…';
  try{const d=await (await fetch('/api/run/clean',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})).json();
   if(rm)rm.innerHTML=d.ok?('<span class="posv">'+(mode=='stopall'?'paradas':(mode=='small'?'borradas':'limpiadas'))+': '+d.count+'</span>'):('<span class="neg">'+esc(d.error||'error')+'</span>');
  }catch(_){if(rm)rm.textContent='error';}setTimeout(refreshRuns,900);return;}
 const cb=e.target.closest('[data-claim]');
 if(cb){const lab=cb.dataset.claim,m=document.getElementById('c-msg')||document.getElementById('r-msg');if(m)m.textContent='obteniendo enlace de claim…';
  try{const d=await (await fetch('/api/claim?label='+encodeURIComponent(lab))).json();
   if(m)m.innerHTML=d.claim_url?('🏆 <b>'+esc(lab)+'</b> — abre este enlace para reclamar el agente en tu cuenta dev.fun y entrar en la clasificación: <a class="rlink" href="'+esc(d.claim_url)+'" target="_blank" rel="noopener">'+esc(d.claim_url)+'</a>'):('<span class="neg">'+esc(d.error||'sin claim_url')+'</span>');
  }catch(_){if(m)m.textContent='error';}}
});
async function tick(){let d;try{const r=await fetch('/api/state');d=await r.json();}catch(e){$('#live').textContent='OFFLINE';$('#live').classList.remove('live');return;}
 try{render(d);$('#live').textContent='LIVE';$('#live').classList.add('live');}catch(e){$('#live').textContent='ERR';console.error('render error:',e);}
 if(document.getElementById('handsview').style.display!=='none'&&Date.now()-lastHands>10000)loadHands();
 if(document.getElementById('runview').style.display!=='none')refreshRuns();
 if(document.getElementById('rankview')&&document.getElementById('rankview').style.display!=='none')refreshRank();
 if(document.getElementById('mllmview')&&document.getElementById('mllmview').style.display!=='none')refreshMLLM();}
tick();setInterval(tick,3000);
async function liveTick(){try{const d=await (await fetch('/api/live')).json();if(d.error)return;
 const set=(id,v)=>{const el=$('#'+id);if(el&&el.textContent!=String(v)){el.textContent=v;el.classList.remove('tickpulse');void el.offsetWidth;el.classList.add('tickpulse');}};
 set('kpi-hands',d.hands);set('kpi-dec',d.decisions);if(d.decisions)set('kpi-m3',(Math.round(1000*d.m3/d.decisions)/10)+'%');
}catch(e){}}
liveTick();setInterval(liveTick,1000);
let MLLMMODELS=null,MLLMRUN='';
function loadMLLM(){
 document.getElementById('mllmbox').innerHTML=
  '<div class="pcard"><h4>🤖 multiLLM · benchmark de razonamiento sobre jugadas M3</h4><div id="ml-cfg" class="mut">cargando modelos…</div></div>'+
  '<div class="pcard"><h4>Benchmarks <span class="mut">(clic para ver resultados)</span></h4><div id="ml-runs" class="mut">cargando…</div></div>'+
  '<div class="pcard"><h4>Resultados <span class="mut" id="ml-rtitle"></span></h4><div id="ml-res" class="mut">selecciona un benchmark.</div></div>';
 fetch('/api/mllm/models').then(r=>r.json()).then(d=>{MLLMMODELS=d;renderMLLMConfig();}).catch(()=>{});
 refreshMLLM();
}
function renderMLLMConfig(){
 const el=document.getElementById('ml-cfg');if(!el||!MLLMMODELS)return;
 const prov=MLLMMODELS.providers||{};
 const presets=(MLLMMODELS.presets||[]).map(p=>{const ok=!!prov[p.provider];
  return '<label class="mlmodel'+(ok?'':' off')+'" title="'+esc(p.id)+(ok?'':' · falta llave en .env')+'"><input type="checkbox" data-mlm="'+esc(p.id)+'"'+(ok?(p.provider=='minimax'?' checked':''):' disabled')+'> '+esc(p.label)+' <span class="mut">'+esc(p.provider)+'</span></label>';}).join('');
 const judges='<option value="">(sin juez)</option>'+(MLLMMODELS.presets||[]).map(p=>'<option value="'+esc(p.id)+'">'+esc(p.label)+'</option>').join('');
 el.innerHTML='<div class="mut" style="margin-bottom:4px">Modelos a comparar (en gris = falta la llave en .env):</div><div class="mlmodels">'+presets+'</div>'+
  '<div class="rform" style="margin-top:8px">añadir <input id="ml-add" placeholder="openrouter:vendor/model" style="width:230px"> <button class="eqbtn" onclick="mllmAdd()">+ añadir</button></div>'+
  '<div class="rform" style="margin-top:8px">juez <select id="ml-judge">'+judges+'</select> nº manos <input id="ml-hands" type="number" value="10" min="1" max="200" style="width:58px"> reps <input id="ml-reps" type="number" value="3" min="1" max="20" style="width:46px"> <button class="eqbtn on" onclick="launchMLLM()">▶ run benchmark</button> <span id="ml-msg" class="mut"></span></div>'+
  '<div class="mut" style="margin-top:5px">Toma manos aleatorias con razonamiento M3, reconstruye la jugada y la plantea a los modelos N veces. Coste = manos × modelos × reps × (1+juez). Empieza pequeño.</div>';
}
function mllmAdd(){const i=document.getElementById('ml-add'),v=(i.value||'').trim();if(!v)return;
 if(!/^[A-Za-z0-9_.:\/-]{1,80}$/.test(v)){i.style.borderColor='#ff5d5d';return;}
 const cont=document.querySelector('#ml-cfg .mlmodels');
 if(cont){const l=document.createElement('label');l.className='mlmodel';l.innerHTML='<input type="checkbox" data-mlm="'+esc(v)+'" checked> '+esc(v)+' <span class="mut">+</span>';cont.appendChild(l);}
 i.value='';i.style.borderColor='';
}
async function launchMLLM(){
 const m=document.getElementById('ml-msg');
 const models=[].slice.call(document.querySelectorAll('#ml-cfg input[data-mlm]:checked')).map(x=>x.dataset.mlm);
 if(!models.length){m.innerHTML='<span class="neg">selecciona al menos un modelo</span>';return;}
 const judge=document.getElementById('ml-judge').value,hands=+document.getElementById('ml-hands').value||10,reps=+document.getElementById('ml-reps').value||3;
 const n=models.length*hands*reps*(judge?2:1);
 if(n>400&&!confirm('Son ~'+n+' llamadas LLM. ¿Continuar?'))return;
 m.textContent='lanzando benchmark…';
 try{const d=await (await fetch('/api/mllm/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({models,judge,hands,reps})})).json();
  m.innerHTML=d.ok?('<span class="posv">▶ '+esc(d.run_id)+' lanzado · '+models.length+' modelos × '+hands+' manos × '+reps+' reps</span>'):('<span class="neg">'+esc(d.error||'error')+'</span>');
  if(d.ok){MLLMRUN=d.run_id;setTimeout(refreshMLLM,1500);}
 }catch(e){m.textContent='error';}
}
function mlpc(v){return v==null?'—':v+'%';}
async function refreshMLLM(){
 const el=document.getElementById('ml-runs');if(!el)return;
 try{const d=await (await fetch('/api/mllm/runs')).json();const runs=d.runs||[];
  if(!runs.length){el.innerHTML='<span class="mut">aún no hay benchmarks; configura arriba y pulsa ▶ run.</span>';}
  else{if(!MLLMRUN)MLLMRUN=runs[0].run_id;
   el.innerHTML='<table class="htab"><thead><tr><th>run</th><th>estado</th><th>modelos</th><th>manos×reps</th><th>juez</th><th>filas</th></tr></thead><tbody>'+
    runs.map(r=>'<tr data-mlrun="'+esc(r.run_id)+'" style="cursor:pointer'+(r.run_id==MLLMRUN?';background:rgba(46,230,166,.08)':'')+'"><td><b>'+esc(r.run_id)+'</b></td><td><span class="dot '+(r.status=='running'?'warn':'up')+'"></span>'+esc(r.status)+'</td><td>'+(r.models||[]).length+'</td><td>'+r.n_hands+'×'+r.n_reps+'</td><td>'+(r.judge?'sí':'—')+'</td><td>'+r.results+'</td></tr>').join('')+'</tbody></table>';
  }
  renderMLLMResults();
 }catch(e){el.textContent='error';}
}
async function renderMLLMResults(){
 const el=document.getElementById('ml-res');if(!el||!MLLMRUN)return;
 const t=document.getElementById('ml-rtitle');if(t)t.textContent='· '+MLLMRUN;
 try{const d=await (await fetch('/api/mllm/results?run='+encodeURIComponent(MLLMRUN))).json();
  if(d.error){el.innerHTML='<span class="neg">'+esc(d.error)+'</span>';return;}
  const ms=d.models||[];
  if(!ms.length){el.innerHTML='<span class="mut">sin resultados todavía…</span>';return;}
  let h='<table class="htab"><thead><tr><th>modelo</th><th>juez 0-10</th><th>validez</th><th>autoconsist.</th><th>vs M3</th><th>vs consenso</th><th>latencia</th><th>tokens</th><th>n</th></tr></thead><tbody>'+
   ms.map(r=>'<tr><td><b>'+esc(r.model)+'</b></td><td><b class="'+((r.judge_avg||0)>=6?'posv':'neg')+'">'+(r.judge_avg==null?'—':r.judge_avg)+'</b></td><td>'+mlpc(r.valid_pct)+'</td><td>'+mlpc(r.selfcons_pct)+'</td><td>'+mlpc(r.vsM3_pct)+'</td><td>'+mlpc(r.vsCons_pct)+'</td><td>'+(r.lat_ms==null?'—':r.lat_ms+'ms')+'</td><td>'+(r.tokens==null?'—':r.tokens)+'</td><td>'+r.n+'</td></tr>').join('')+'</tbody></table>';
  h+='<div class="mut" style="margin:10px 0 4px">jugadas (acción de cada modelo; pasa el ratón por encima para el razonamiento):</div>';
  h+=(d.hands||[]).map(hd=>'<div class="mlhand"><span class="mut">'+esc((hd.hand_key||'').slice(0,18))+' · M3=<b>'+esc(hd.m3_action||'?')+'</b> · consenso=<b>'+esc(hd.consensus||'?')+'</b></span><br>'+
   Object.keys(hd.per||{}).map(mk=>{const p=hd.per[mk];return '<span class="chip" title="'+esc((p.reasoning||'')+(p.note?(' || juez: '+p.note):''))+'">'+esc(mk.split(':').pop().slice(0,16))+': <b>'+esc(p.action||'?')+'</b>'+(p.judge!=null?(' <span class="posv">'+p.judge+'</span>'):'')+'</span>';}).join('')+'</div>').join('');
  el.innerHTML=h;
 }catch(e){el.textContent='error';}
}
document.getElementById('mllmbox').addEventListener('click',e=>{const tr=e.target.closest('[data-mlrun]');if(tr){MLLMRUN=tr.dataset.mlrun;refreshMLLM();}});
function loadRank(){
 document.getElementById('rankbox').innerHTML='<div class="pcard"><h4>🏆 Mis agentes en el ranking <span class="mut">(clic en la cabecera para ordenar)</span></h4><div id="rank-list" class="mut">cargando…</div><div id="rank-msg" class="mut" style="margin-top:8px"></div></div>';
 refreshRank();
}
let RANKDATA=[],RSORT={col:'bb100',dir:-1};
function sortRank(arr){const c=RSORT.col,d=RSORT.dir;
 const val=r=>c=='fecha'?(r.ts||0):c=='bb100'?(r.bb100==null?-1e9:r.bb100):c=='manos'?(r.hands||0):c=='nombre'?String(r.name||r.label||'').toLowerCase():c=='estrategia'?String(r.strategy||'').toLowerCase():c=='estado'?String(r.state||''):0;
 return arr.slice().sort((a,b)=>{const x=val(a),y=val(b);return (x<y?-1:x>y?1:0)*d;});
}
function renderRankTable(){
 const el=document.getElementById('rank-list');if(!el)return;
 if(!RANKDATA.length){el.innerHTML='<span class="mut">aún no has lanzado clasificatorias reclamables. Lánzalas desde RUN → tarjeta 🏁.</span>';return;}
 const rankBy={};RANKDATA.filter(r=>r.bb100!=null).slice().sort((a,b)=>b.bb100-a.bb100).forEach((r,i)=>{rankBy[r.label]=i+1;});
 const a=sortRank(RANKDATA);
 const ar=c=>RSORT.col==c?(RSORT.dir<0?' ▼':' ▲'):'';
 const th=(c,l)=>'<th data-sort="'+c+'" style="cursor:pointer">'+l+ar(c)+'</th>';
 el.innerHTML='<table class="htab"><thead><tr><th>#</th>'+th('nombre','nombre')+th('estrategia','estrategia')+th('manos','manos')+th('bb100','bb/100')+th('estado','estado')+th('fecha','fecha')+'<th></th></tr></thead><tbody>'+
  a.map(r=>{const up=r.state=='active',done=r.bb100!=null;
   return '<tr><td>'+(rankBy[r.label]||'·')+'</td><td><b>'+esc(r.name||r.label)+'</b> <span class="mut" style="font-size:10px">'+esc(r.label)+'</span></td><td>'+esc(r.strategy||'?')+'</td><td>'+(r.hands||0)+'</td><td>'+(r.bb100==null?'<span class="mut">jugando…</span>':'<b class="'+(r.bb100>=0?'posv':'neg')+'">'+(r.bb100>=0?'+':'')+r.bb100+'</b>')+'</td><td><span class="dot '+(up?'warn':(done?'up':'down'))+'"></span>'+(up?'jugando':(done?'terminado':esc(r.state)))+'</td><td class="mut" style="font-size:11px;white-space:nowrap">'+(r.ts?new Date(r.ts*1000).toLocaleString([],{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}):"—")+'</td><td><button class="eqbtn" data-claim="'+esc(r.label)+'">🏆 reclamar</button></td></tr>';}).join('')+'</tbody></table>';
}
async function refreshRank(){
 const el=document.getElementById('rank-list');if(!el)return;
 try{const d=await (await fetch('/api/rank')).json();RANKDATA=d.agents||[];renderRankTable();
 }catch(e){el.textContent='error';}
}
document.getElementById('rankbox').addEventListener('click',async e=>{
 const th=e.target.closest('th[data-sort]');
 if(th){const c=th.dataset.sort;if(RSORT.col==c)RSORT.dir*=-1;else{RSORT.col=c;RSORT.dir=(c=='bb100'||c=='fecha'||c=='manos')?-1:1;}renderRankTable();return;}
 const cb=e.target.closest('[data-claim]');if(!cb)return;
 const lab=cb.dataset.claim,m=document.getElementById('rank-msg');if(m)m.textContent='obteniendo enlace de claim…';
 try{const d=await (await fetch('/api/claim?label='+encodeURIComponent(lab))).json();
  if(m)m.innerHTML=d.claim_url?('🏆 <b>'+esc(lab)+'</b> — abre para reclamar y entrar en la clasificación con tu cuenta dev.fun: <a class="rlink" href="'+esc(d.claim_url)+'" target="_blank" rel="noopener">'+esc(d.claim_url)+'</a>'):('<span class="neg">'+esc(d.error||'sin claim_url')+'</span>');
 }catch(_){if(m)m.textContent='error';}
});
</script></body></html>"""


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

    def do_GET(self):
        p = self.path
        if p == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
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
            self._send(json.dumps(_strat_template(base, name), default=str), "application/json")
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
        if p == "/" or p.startswith("/index"):
            self._send(HTML, "text/html; charset=utf-8")
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

        def reply(o):
            self._send(json.dumps(o), "application/json")

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
                    wc = sqlite3.connect(DB, timeout=10)
                    wc.execute("PRAGMA busy_timeout=8000")
                except Exception as e:
                    reply({"error": "db: " + str(e)}); return
                for u, active in units:
                    if active in ("active", "activating"):
                        continue
                    label = u[len("arena-run-"):].replace(".service", "")
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
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
