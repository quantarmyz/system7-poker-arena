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
_coach_cache = {"ts": 0.0, "txt": None, "hands": 0}
try:
    import s7_strat
except Exception:
    s7_strat = None
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)


def _svc(name):
    try:
        return subprocess.run(["systemctl", "is-active", name],
                              capture_output=True, text=True, timeout=3).stdout.strip() or "?"
    except Exception:
        return "?"


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


def _coach():
    """Rule-based analysis of our play + opponents (gated at 10k hands)."""
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}

    def q(sql, a=()):
        try:
            return c.execute(sql, a).fetchall()
        except Exception:
            return []

    hands = (q("select count(distinct hand_key) from decisions") or [[0]])[0][0]
    if hands < COACH_NEED:
        c.close()
        return {"locked": True, "hands": hands, "need": COACH_NEED}
    findings, advice = [], []
    v = q("select sum(voluntary),sum(preflop_raise),count(*) from decisions where street='preflop'")
    if v and v[0][2]:
        vol, pfr, n = v[0]
        vp, pf = round(100 * vol / n), round(100 * pfr / n)
        findings.append({"k": "VPIP / PFR global", "v": f"{vp}% / {pf}%", "ref": "~22 / 18"})
        if vp < 16:
            advice.append(f"Muy tight (VPIP {vp}%). Abre más en BTN/CO/SB.")
        elif vp > 30:
            advice.append(f"VPIP {vp}% alto; recorta manos marginales fuera de posición.")
        if vp - pf > 10:
            advice.append(f"Gap VPIP-PFR {vp-pf} grande → demasiado flat preflop; 3-betea o foldea más.")
    ff = q("select sum(case when action='fold' then 1 else 0 end),count(*) from decisions where street='flop' and call_chips>0")
    if ff and ff[0][1]:
        ftc = round(100 * ff[0][0] / ff[0][1])
        findings.append({"k": "Fold-to-bet flop", "v": f"{ftc}%", "ref": "<55%"})
        if ftc > 58:
            advice.append(f"Foldeas demasiado al c-bet en flop ({ftc}%). Defiende más (flota/raise).")
    pos = q("select pos,count(*),sum(voluntary),sum(preflop_raise) from decisions where street='preflop' and pos!='' group by pos")
    order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
    bypos = sorted([{"pos": r[0], "n": r[1], "vpip": round(100 * r[2] / r[1]) if r[1] else 0,
                     "pfr": round(100 * r[3] / r[1]) if r[1] else 0} for r in pos], key=lambda x: order.get(x["pos"], 9))
    buckets = q("select d.pos, sum(hr.chip_delta), count(distinct d.hand_key) from decisions d "
                "join hand_results hr on hr.table_id = substr(d.hand_key,1,instr(d.hand_key,':')-1) "
                "where d.street='preflop' and d.pos!='' group by d.pos")
    posres = sorted([{"pos": r[0], "delta": r[1] or 0, "n": r[2]} for r in buckets], key=lambda x: x["delta"])
    if posres and posres[0]["delta"] < 0:
        w = posres[0]
        advice.append(f"Pierdes más desde {w['pos']} ({w['delta']:+d} fichas en {w['n']} manos). Revisa rango/líneas ahí.")
    runs = q("select run_label,avg(adjusted_bb100),count(*) from runs where hands>=400 group by run_label")
    ab = sorted([{"label": r[0], "bb100": round(r[1], 1) if r[1] is not None else None, "n": r[2]} for r in runs],
                key=lambda x: -(x["bb100"] if x["bb100"] is not None else -999))
    if len(ab) >= 2 and ab[0]["bb100"] is not None and ab[-1]["bb100"] is not None:
        advice.append(f"A/B: lidera '{ab[0]['label']}' ({ab[0]['bb100']:+} bb/100, n={ab[0]['n']}) vs '{ab[-1]['label']}' ({ab[-1]['bb100']:+}).")
    opp = [{"name": o[0], "vpip": o[1], "pfr": o[2], "af": o[3]} for o in q("select name,vpip,pfr,af from agent_stats")]
    c.close()
    if not advice:
        advice.append("Sin leaks claros con esta muestra; sigue acumulando.")
    return {"locked": False, "hands": hands, "findings": findings, "bypos": bypos,
            "posres": posres, "ab": ab, "advice": advice, "opp": opp}


def _validate_strat(cfg):
    """Sanitise an M3-proposed strategy config; return a safe dict or None."""
    if not isinstance(cfg, dict):
        return None
    out = {}
    orr = cfg.get("opening_ranges")
    if isinstance(orr, dict):
        clean = {str(p).upper(): [str(t) for t in toks][:60] for p, toks in orr.items()
                 if str(p).upper() in ("UTG", "MP", "CO", "BTN", "SB", "BB") and isinstance(toks, list)}
        if clean:
            out["opening_ranges"] = clean
    for k in ("threebet_value", "threebet_bluff"):
        if isinstance(cfg.get(k), list):
            out[k] = [str(t) for t in cfg[k]][:30]
    lim = {"open_size_bb": (1.5, 5), "threebet_mult": (2, 5), "value_eq": (0.5, 0.85),
           "station_mult": (1.0, 2.0), "cbet_bluff_frac": (0.0, 1.0), "commit_spr": (1, 8),
           "perejil_flop": (4, 14), "perejil_turn": (6, 16), "perejil_relief": (0, 5)}
    kn = cfg.get("knobs") if isinstance(cfg.get("knobs"), dict) else {}
    ck = {k: max(lo, min(hi, kn[k])) for k, (lo, hi) in lim.items() if isinstance(kn.get(k), (int, float))}
    if isinstance(kn.get("sizing"), dict):
        ck["sizing"] = kn["sizing"]
    if ck:
        out["knobs"] = ck
    return out or None


def _coach_llm():
    """Narrative coaching + a proposed new strategy version via MiniMax M3 (cached, gated)."""
    hands = 0
    try:
        cc = _ro()
        hands = cc.execute("select count(distinct hand_key) from decisions").fetchone()[0]
        cc.close()
    except Exception:
        pass
    if hands < COACH_NEED:
        return {"locked": True, "hands": hands, "need": COACH_NEED}
    if _coach_cache["txt"] and time.time() - _coach_cache["ts"] < 600 and hands - _coach_cache["hands"] < 500:
        return {"text": _coach_cache["txt"], "cached": True}
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
        system = ("Eres un coach de NLHE 6-max de élite (metodología EducaPoker / GTO node-locking). "
                  "Te paso el informe de System 7 contra un panel near-GTO (DeepCFR) y su config actual. "
                  "(1) Análisis ACCIONABLE de leaks (preflop por posición + postflop). "
                  "(2) Propón UNA versión nueva partiendo de la actual. Termina con SOLO un bloque ```json``` con las "
                  "claves a CAMBIAR: opening_ranges {pos:[tokens '22+','A2s+','KTo+']}, threebet_value, threebet_bluff, "
                  "knobs {open_size_bb,threebet_mult,value_eq,station_mult,cbet_bluff_frac,commit_spr,perejil_flop,"
                  "perejil_turn,perejil_relief}. Incluye solo lo que cambies. Español, conciso.\n\n" + meth[:1600])
        user = "CONFIG ACTUAL knobs: " + json.dumps(cur) + "\n\nINFORME:\n" + rep
        txt = llm_system7._minimax_call(system, user, 2600, os.environ.get("S7_MODEL", "MiniMax-M3"))
        if not txt:
            return {"error": "M3 no devolvió respuesta (revisa OPENAI_API_KEY / tokens)."}
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
        _coach_cache.update(ts=time.time(), txt=prose, hands=hands)
        return {"text": prose, "proposal": proposal, "version": version, "cached": False}
    except Exception as e:
        return {"error": str(e)}


def _runs():
    """Active trainings: fixed arms + transient arena-run-* + per-label progress."""
    out = [{"unit": "arena-test", "label": "std", "ranges": "std", "engine": "hybrid", "state": _svc("arena-test"), "fixed": True},
           {"unit": "arena-test-wide", "label": "wide", "ranges": "wide", "engine": "hybrid", "state": _svc("arena-test-wide"), "fixed": True}]
    try:
        r = subprocess.run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "arena-run-*"],
                           capture_output=True, text=True, timeout=4).stdout
        for line in r.splitlines():
            parts = line.split()
            if parts and parts[0].startswith("arena-run-"):
                unit = parts[0]
                out.append({"unit": unit, "label": unit[len("arena-run-"):].replace(".service", ""),
                            "ranges": "?", "engine": "?", "state": _svc(unit), "fixed": False})
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
</style></head><body>
<div class="top"><b>SYSTEM&nbsp;7</b><span class="live" id="live">LIVE</span>
<span class="tabs"><span class="tab on" id="tab-panel" onclick="showTab('panel')">PANEL</span><span class="tab" id="tab-hands" onclick="showTab('hands')">MANOS</span><span class="tab" id="tab-players" onclick="showTab('players')">PLAYERS</span><span class="tab" id="tab-coach" onclick="showTab('coach')">COACH</span><span class="tab" id="tab-run" onclick="showTab('run')">RUN</span></span>
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
  ['manos',d.hands],['decisiones',d.decisions],
  ['bb/100 std',sgn(d.ab.std.mean)],['bb/100 wide',sgn(d.ab.wide.mean)],
  ['M3 %',d.m3pct+'%']
 ].map(k=>'<div class="kpi"><div class="l">'+k[0]+'</div><div class="v">'+k[1]+'</div></div>').join('');
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
let HAND=null,STEP=0,TMR=null;
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
async function openHand(key){if(!key)return;try{const r=await fetch('/api/hand?key='+encodeURIComponent(key));HAND=await r.json();HAND._ev=buildTimeline(HAND);STEP=0;document.getElementById('modal').style.display='flex';renderHand();}catch(e){}}
function closeHand(){clearInterval(TMR);TMR=null;document.getElementById('modal').style.display='none';}
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
 const ev=h._ev||[];
 const hasRes=!!(h.result&&((h.result.seats_shown||[]).length||(h.result.winners||[]).length));
 const total=ev.length+(hasRes?1:0),maxStep=Math.max(0,total-1);
 if(STEP>maxStep)STEP=maxStep;if(STEP<0)STEP=0;
 const isResult=hasRes&&STEP>=ev.length,my=h.seat;
 const lbl=isResult?'<b style="color:#ffd877">RESULTADO</b>':(ev.length?'<span class="mut">'+evtxt(ev[STEP])+'</span>':'');
 const ctl=ev.length?('<div class="ctl"><button onclick="STEP=Math.max(0,STEP-1);renderHand()">◀ prev</button><button onclick="playHand()">▶ play</button><button onclick="STEP=Math.min('+maxStep+',STEP+1);renderHand()">next ▶▶</button> <span class="mut">paso '+(STEP+1)+' / '+total+'</span> &nbsp; '+lbl+'</div>'):'';
 const fb=ev.length?'':('<div>Tus cartas <span class="big">'+(chs(h.hole)||'?')+'</span> <span class="mut">· asiento '+(my||'?')+'</span></div><div style="margin:8px 0">Board <span class="big">'+(chs((h.board||'').split(/[,\s]+/).filter(Boolean).join(','))||'—')+'</span></div>'+(h.decisions||[]).map(d=>'<span class="chip" style="display:inline-block;margin:2px 3px 0 0">'+d.street+': '+(d.strength||'')+' → <b>'+esc(d.action||'')+'</b>'+(d.amount?(' '+d.amount):'')+'</span>').join(''));
 const rlink=(h.result&&h.result.replay_url)?(' <a class="rlink" href="'+esc(h.result.replay_url)+'" target="_blank" rel="noopener">▶ repro oficial</a>'):'';
 document.querySelector('#modal .card').innerHTML=
  '<div class="mh"><b>▶ Reproductor de mano</b> <span class="mut">'+(h.key||'')+'</span>'+rlink+'<span style="float:right;cursor:pointer" onclick="closeHand()">✕</span></div>'+
  '<div class="mb">'+(isResult?showdownBlock(h):'')+(ev.length?minitable(h,ev,STEP):'')+ctl+(ev.length?streetSections(h,ev):fb)+'</div>';
}
function playHand(){clearInterval(TMR);const ev=HAND._ev||[],hasRes=!!(HAND.result&&((HAND.result.seats_shown||[]).length||(HAND.result.winners||[]).length)),max=ev.length+(hasRes?1:0)-1;TMR=setInterval(()=>{if(STEP>=max){clearInterval(TMR);return;}STEP++;renderHand();},850);}
/* ---------- MANOS tab ---------- */
let HANDS=[],lastHands=0,HSORT={col:'ts',dir:-1};
function showTab(t){
 ['panel','hands','players','coach','run'].forEach(v=>{document.getElementById(v+'view').style.display=(v==t)?'':'none';document.getElementById('tab-'+v).classList.toggle('on',v==t);});
 if(t=='hands')loadHands();
 if(t=='players')loadPlayers();
 if(t=='coach')loadCoach();
 if(t=='run')loadRuns();
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
async function loadCoach(){
 const el=document.getElementById('coach');el.innerHTML='<div class="mut" style="padding:14px">cargando…</div>';
 try{const d=await (await fetch('/api/coach')).json();
  if(d.locked){const pc=Math.min(100,Math.round(100*d.hands/d.need));
   el.innerHTML='<div class="pcard"><h4>COACH bloqueado 🔒</h4><div class="mut">Se necesitan '+d.need.toLocaleString()+' manos para activar el análisis. Llevas <b>'+d.hands.toLocaleString()+'</b>.</div><div class="track" style="margin-top:8px"><div class="fill" style="width:'+pc+'%;background:var(--grn)"></div><div class="fv">'+pc+'%</div></div></div>';return;}
  let h='<div class="pcard"><h4>Consejos (reglas)</h4>'+(d.advice||[]).map(a=>'<div class="cadv">▷ '+esc(a)+'</div>').join('')+'</div>';
  if((d.findings||[]).length)h+='<div class="pcard"><h4>Métricas clave</h4>'+d.findings.map(f=>'<span class="st">'+esc(f.k)+' <b>'+esc(f.v)+'</b> <span class="mut">(ref '+esc(f.ref)+')</span></span>').join('')+'</div>';
  if((d.posres||[]).length)h+='<div class="pcard"><h4>Ganancia/pérdida por posición</h4>'+d.posres.map(p=>'<span class="st">'+p.pos+' <b style="color:'+((p.delta||0)>=0?"#2ee6a6":"#ff5d5d")+'">'+((p.delta||0)>=0?"+":"")+p.delta+'</b> <span class="mut">('+p.n+'m)</span></span>').join('')+'</div>';
  if((d.ab||[]).length)h+='<div class="pcard"><h4>A/B estrategias (bb/100)</h4>'+d.ab.map(a=>'<span class="st">'+esc(a.label)+' <b>'+(a.bb100==null?'—':(a.bb100>=0?'+':'')+a.bb100)+'</b> <span class="mut">(n'+a.n+')</span></span>').join('')+'</div>';
  h+='<div class="pcard"><h4>Coach IA · MiniMax M3</h4><button class="eqbtn on" style="--c:#b98bff" onclick="coachLLM()">🧠 pedir consejo a M3</button><div id="coachllm" style="margin-top:8px;white-space:pre-wrap;font-size:12px;line-height:1.5"></div></div>';
  el.innerHTML=h;
 }catch(e){el.innerHTML='<div class="mut" style="padding:14px">error cargando coach</div>';}
}
async function coachLLM(){
 const o=document.getElementById('coachllm');if(!o)return;o.innerHTML='<span class="mut">consultando a M3 (~30s)…</span>';
 try{const d=await (await fetch('/api/coach/llm')).json();
  if(d.locked){o.textContent='bloqueado: '+d.hands+'/'+d.need;return;}
  if(d.error){o.textContent='error: '+d.error;return;}
  let h='<div style="white-space:pre-wrap">'+esc(d.text||'')+'</div>';
  if(d.version&&d.proposal)h+='<div class="pcard" style="margin-top:8px"><b>Propuesta de versión: '+esc(d.version)+'</b><pre class="runlog" style="max-height:220px">'+esc(JSON.stringify(d.proposal,null,1))+'</pre><button class="eqbtn on" data-launchv="'+esc(d.version)+'">▶ lanzar '+esc(d.version)+' (vs fijo)</button> <span id="cv-msg" class="mut"></span></div>';
  o.innerHTML=h;
 }catch(e){o.textContent='error consultando M3';}
}
function launchVersion(v){const m=document.getElementById('cv-msg');if(m)m.textContent='lanzando…';
 fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:v,strat:v,engine:'hybrid',matches:5})}).then(r=>r.json()).then(d=>{if(m)m.innerHTML=d.ok?'<span class="posv">lanzado '+esc(d.unit)+'</span>':'<span class="neg">'+esc(d.error||'error')+'</span>';}).catch(()=>{if(m)m.textContent='error';});
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
  '<div class="pcard"><h4>Entrenamientos</h4><div id="r-list" class="mut">cargando…</div></div>'+
  '<div class="pcard"><h4>Debug en vivo · <select id="r-logunit" onchange="pollLog()"></select></h4><pre id="r-log" class="runlog">selecciona un entrenamiento…</pre></div>';
 refreshRuns();
 fetch('/api/strats').then(r=>r.json()).then(d=>{const s=document.getElementById('r-strat');if(s)s.innerHTML='<option value="">(usar rangos)</option>'+(d.strats||[]).map(x=>'<option>'+esc(x.name)+'</option>').join('');}).catch(()=>{});
}
async function refreshRuns(){
 const el=document.getElementById('r-list');if(!el)return;
 try{const d=await (await fetch('/api/runs')).json();const runs=d.runs||[];
  el.innerHTML='<table class="htab"><thead><tr><th>run</th><th>rangos</th><th>motor</th><th>estado</th><th>partidas</th><th>bb/100</th><th></th></tr></thead><tbody>'+
   runs.map(r=>{const up=r.state=='active';return '<tr><td><b>'+esc(r.label)+'</b>'+(r.fixed?' <span class=mut>(fijo)</span>':'')+'</td><td>'+(r.ranges||'?')+'</td><td>'+(r.engine||'?')+'</td><td><span class="dot '+(up?'up':(r.state=='activating'?'warn':'down'))+'"></span>'+r.state+'</td><td>'+(r.matches||0)+'</td><td>'+(r.bb100==null?'—':(r.bb100>=0?'+':'')+r.bb100)+'</td><td>'+(up?'<button class="eqbtn" data-stop="'+esc(r.unit)+'">parar</button>':'')+'</td></tr>';}).join('')+'</tbody></table>';
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
async function launchRun(){
 const m=document.getElementById('r-msg');
 const body={label:(document.getElementById('r-label').value||'').trim(),strat:(document.getElementById('r-strat')||{}).value||'',ranges:document.getElementById('r-ranges').value,engine:document.getElementById('r-engine').value,matches:+document.getElementById('r-matches').value,max_tokens:+document.getElementById('r-tok').value||0,min_deadline:+document.getElementById('r-dl').value||0};
 m.textContent='lanzando…';
 try{const d=await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  m.innerHTML=d.ok?'<span class="posv">lanzado: '+esc(d.unit)+'</span>':'<span class="neg">'+esc(d.error||'error')+'</span>';
  if(d.ok)setTimeout(refreshRuns,900);
 }catch(e){m.textContent='error';}
}
/* ---------- wiring ---------- */
document.addEventListener('keydown',e=>{if(e.key=='Escape')closeHand();});
document.getElementById('tick').addEventListener('click',e=>{const row=e.target.closest('[data-k]');if(row&&row.dataset.k)openHand(decodeURIComponent(row.dataset.k));});
document.getElementById('hands').addEventListener('click',e=>{const th=e.target.closest('th[data-sort]');if(th){const cc=th.dataset.sort;if(HSORT.col==cc)HSORT.dir*=-1;else{HSORT.col=cc;HSORT.dir=(cc=='ts'||cc=='delta'||cc=='pot')?-1:1;}renderHands();return;}const row=e.target.closest('tr[data-k]');if(row&&row.dataset.k)openHand(decodeURIComponent(row.dataset.k));});
document.getElementById('players').addEventListener('click',e=>{const el=e.target.closest('[data-k]');if(el&&el.dataset.k)openHand(decodeURIComponent(el.dataset.k));});
document.getElementById('coach').addEventListener('click',e=>{const b=e.target.closest('[data-launchv]');if(b)launchVersion(b.dataset.launchv);});
document.getElementById('eqctl').addEventListener('click',e=>{const b=e.target.closest('[data-eq]');if(!b)return;const k=b.dataset.eq;if(k=='__ev')EQOPT.ev=!EQOPT.ev;else EQOPT.off[k]=!EQOPT.off[k];drawEquity();});
document.getElementById('run').addEventListener('click',async e=>{const b=e.target.closest('[data-stop]');if(!b)return;b.textContent='…';try{await fetch('/api/run/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({unit:b.dataset.stop})});}catch(_){}setTimeout(refreshRuns,700);});
async function tick(){let d;try{const r=await fetch('/api/state');d=await r.json();}catch(e){$('#live').textContent='OFFLINE';$('#live').classList.remove('live');return;}
 try{render(d);$('#live').textContent='LIVE';$('#live').classList.add('live');}catch(e){$('#live').textContent='ERR';console.error('render error:',e);}
 if(document.getElementById('handsview').style.display!=='none'&&Date.now()-lastHands>10000)loadHands();
 if(document.getElementById('runview').style.display!=='none')refreshRuns();}
tick();setInterval(tick,3000);
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
        if p.startswith("/api/coach/llm"):
            self._send(json.dumps(_coach_llm(), default=str), "application/json")
            return
        if p.startswith("/api/coach"):
            self._send(json.dumps(_coach(), default=str), "application/json")
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
            try:
                out = subprocess.run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "cat"],
                                     capture_output=True, text=True, timeout=5).stdout
            except Exception as e:
                out = "error: " + str(e)
            self._send(json.dumps({"unit": unit, "log": out}), "application/json")
            return
        if p.startswith("/api/strats"):
            self._send(json.dumps(_strats(), default=str), "application/json")
            return
        if p.startswith("/api/runs"):
            self._send(json.dumps(_runs(), default=str), "application/json")
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

        if p.startswith("/api/run/stop"):
            unit = str(body.get("unit", ""))
            if re.fullmatch(r"arena-(run-[a-z0-9_-]{1,24}|test|test-wide)(\.service)?", unit):
                try:
                    subprocess.run(["systemctl", "stop", unit], timeout=8)
                    reply({"ok": True})
                except Exception as e:
                    reply({"error": str(e)})
            else:
                reply({"error": "unit inválida"})
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
            cmd = ["systemd-run", "--unit=arena-run-" + label, "--working-directory=" + HERE,
                   "--setenv=HOME=" + HERE, "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
                   "--setenv=PYTHONUNBUFFERED=1", "--setenv=S7_STATS_DB=" + DB,
                   "--setenv=S7_RUN_LABEL=" + label, "--setenv=S7_RANGES=" + ranges]
            if strat:
                cmd.append("--setenv=S7_STRAT=" + strat)
            try:
                mt = int(body.get("max_tokens") or 0)
                if mt > 0:
                    cmd.append("--setenv=S7_MAX_TOKENS=" + str(mt))
            except Exception:
                pass
            try:
                md = int(body.get("min_deadline") or 0)
                if md > 0:
                    cmd.append("--setenv=S7_LLM_MIN_DEADLINE=" + str(md))
            except Exception:
                pass
            cmd += ["/usr/local/bin/uv", "run", "s7_test.py", "--engine", engine, "--matches", str(matches)]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
                if r.returncode != 0:
                    reply({"error": (r.stderr or "systemd-run falló")[:300]}); return
            except Exception as e:
                reply({"error": str(e)}); return
            reply({"ok": True, "unit": "arena-run-" + label})
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    print(f"[s7-dash] serving http://0.0.0.0:{PORT}  db={DB}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
