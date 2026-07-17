"""Evolution system — automatic coach → proposal → store for human approval.

The engine runs inside the dashboard process (_evolve_loop in s7_dash, every 30s).
It NEVER deploys anything — only diagnoses, proposes, and stores for human review.

Workflow:
1. Check: is there already a pending proposal? -> skip (wait for human)
2. Check: enough hands since last coach? (S7_EVOLVE_INTERVAL, default 1000)
3. Run rule-based diagnosis (_coach) -> get leaks
4. Run LLM coach (_coach_compute) -> get proposed strategy changes
5. Validate -> save to strategies/
6. INSERT into proposals table with status="pending"
7. Set _evolve_pending = True -> dashboard shows badge

All state lives in:
- DB: proposals table (s7_stats.db)
- File: strategies/coach-<timestamp>.json
- In-memory: _evolve_pending flag, _last_evolve_hand counter
"""
import json
import os
import re
import time
import threading
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import s7_stats

sys_path_inserted = False

S7_EVOLVE_INTERVAL = int(os.environ.get("S7_EVOLVE_INTERVAL", "1000"))
COACH_MIN_HANDS = int(os.environ.get("COACH_MIN_HANDS", "500"))

_evolve_pending = False
_last_evolve_hand = 0
_evolve_lock = threading.Lock()
_evolve_cache = {"ts": 0.0, "txt": None, "hands": 0, "proposal": None, "version": None,
                 "running": False, "err": None, "window": None, "leaks": []}
_evolve_game = "cash"
_evolve_agent = ""


def _interval():
    """Manos entre propuestas: settings.json (UI) pisa S7_EVOLVE_INTERVAL (env)."""
    try:
        import s7_api
        v = int((s7_api._settings().get("evolve_interval")) or 0)
        if v >= 100:
            return v
    except Exception:
        pass
    return S7_EVOLVE_INTERVAL


def _ro():
    return sqlite3.connect(f"file:{s7_stats.DB}?mode=ro", uri=True, timeout=5)


def _has_pending():
    try:
        c = _ro()
        r = c.execute("SELECT id FROM proposals WHERE status='pending' AND game=? LIMIT 1", (_evolve_game,)).fetchone()
        c.close()
        return r is not None
    except Exception:
        return False


def _get_total_hands():
    try:
        c = _ro()
        r = c.execute("SELECT count(DISTINCT hand_key) FROM decisions").fetchone()
        c.close()
        return r[0] if r else 0
    except Exception:
        return 0


def _get_last_proposal_hand():
    try:
        c = _ro()
        r = c.execute("SELECT ts FROM proposals ORDER BY ts DESC LIMIT 1").fetchone()
        c.close()
        return int(r[0]) if r and r[0] else 0
    except Exception:
        return 0


def _get_proposal(id):
    try:
        c = _ro()
        r = c.execute(
            "SELECT id, ts, type, status, version, config, prose, agent, game, "
            "by, approved_by, approved_at, rejected_at, rejected_reason, note "
            "FROM proposals WHERE id=?", (str(id),)).fetchone()
        c.close()
        if not r:
            return None
        return {
            "id": r[0], "ts": r[1], "type": r[2], "status": r[3], "version": r[4],
            "config": json.loads(r[5]) if r[5] else None, "prose": r[6],
            "agent": r[7], "game": r[8], "by": r[9], "approved_by": r[10],
            "approved_at": r[11], "rejected_at": r[12], "rejected_reason": r[13],
            "note": r[14]
        }
    except Exception:
        return None


def _get_pending_list():
    try:
        c = _ro()
        rows = c.execute(
            "SELECT id, ts, type, version, prose, agent, game, by, note "
            "FROM proposals WHERE status='pending' ORDER BY ts DESC").fetchall()
        c.close()
        out = []
        for r in rows:
            leak_lines = []
            if r[4]:
                for line in str(r[4]).split("\n"):
                    line = line.strip()
                    if line.startswith("- ") or line.startswith("* ") or line.startswith("✗") or line.startswith("⚠"):
                        leak_lines.append(line)
                    if len(leak_lines) >= 5:
                        break
            out.append({
                "id": r[0], "ts": r[1], "type": r[2], "version": r[3],
                "leak_summary": leak_lines, "agent": r[5], "game": r[6],
                "by": r[7], "note": r[8]
            })
        return out
    except Exception:
        return []


def _get_history(game=None):
    try:
        c = _ro()
        q = ("SELECT id, ts, type, status, version, prose, agent, game, by, "
             "approved_by, approved_at, rejected_at, rejected_reason, note "
             "FROM proposals WHERE status IN ('approved','rejected','deployed')")
        A = ()
        if game:
            q += " AND game=?"
            A = (str(game),)
        q += " ORDER BY ts DESC LIMIT 100"
        rows = c.execute(q, A).fetchall()
        c.close()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "ts": r[1], "type": r[2], "status": r[3], "version": r[4],
                "prose": r[5], "agent": r[6], "game": r[7], "by": r[8],
                "approved_by": r[9], "approved_at": r[10], "rejected_at": r[11],
                "rejected_reason": r[12], "note": r[13]
            })
        return out
    except Exception:
        return []


def _get_status():
    try:
        c = _ro()
        pending = c.execute("SELECT count(*) FROM proposals WHERE status='pending'").fetchone()[0]
        approved = c.execute("SELECT count(*) FROM proposals WHERE status='approved'").fetchone()[0]
        rejected = c.execute("SELECT count(*) FROM proposals WHERE status='rejected'").fetchone()[0]
        total = c.execute("SELECT count(*) FROM proposals").fetchone()[0]
        c.close()
        return {
            "pending": pending, "approved": approved, "rejected": rejected,
            "total": total, "evolve_pending": _evolve_pending,
            "interval": _interval(), "hands": _get_total_hands()
        }
    except Exception:
        return {"pending": 0, "approved": 0, "rejected": 0, "total": 0,
                "evolve_pending": False, "interval": _interval(), "hands": 0}


def _approve(id, by="user", note=""):
    try:
        c = sqlite3.connect(s7_stats.DB, timeout=60)
        c.execute("UPDATE proposals SET status='approved', approved_by=?, approved_at=?, note=? WHERE id=?",
                  (by, time.time(), note or "", id))
        c.execute("INSERT INTO proposal_actions(proposal_id, action, by, at, note) VALUES(?,?,?,?,?)",
                  (id, "approve", by, time.time(), note or ""))
        c.commit()
        c.close()
        global _evolve_pending
        _evolve_pending = _has_pending()
        return {"ok": True, "id": id}
    except Exception as e:
        return {"error": str(e)[:200]}


def _reject(id, by="user", reason="", note=""):
    try:
        c = sqlite3.connect(s7_stats.DB, timeout=60)
        c.execute("UPDATE proposals SET status='rejected', rejected_at=?, rejected_reason=?, note=? WHERE id=?",
                  (time.time(), reason or "", note or "", id))
        c.execute("INSERT INTO proposal_actions(proposal_id, action, by, at, note) VALUES(?,?,?,?,?)",
                  (id, "reject", by, time.time(), note or ""))
        c.commit()
        c.close()
        global _evolve_pending
        _evolve_pending = _has_pending()
        return {"ok": True, "id": id}
    except Exception as e:
        return {"error": str(e)[:200]}


def _run_evolution_cycle():
    """Main evolution engine — called from _evolve_loop every 30s."""
    global _evolve_pending, _last_evolve_hand, _evolve_game, _evolve_agent

    with _evolve_lock:
        try:
            import s7_api
            deploys = s7_api._jload(s7_api._DEPLOYS_PATH, {})
            for label, info in deploys.items():
                if isinstance(info, dict) and info.get("agent"):
                    _evolve_game = info.get("game", "cash")
                    _evolve_agent = str(info.get("agent") or "")
                    break
        except Exception:
            pass

        if _has_pending():
            _evolve_pending = True
            return None

        hands = _get_total_hands()
        if hands < COACH_MIN_HANDS:
            return None

        last_hand = _get_last_proposal_hand()
        if hands - last_hand < _interval():
            return None

        result = _run_coach_pipeline(hands)
        _last_evolve_hand = hands
        return result


def _run_coach_pipeline(hands):
    """Run coach -> LLM -> proposal pipeline."""
    try:
        import s7_dash as _SD
        import s7_report
        import s7_strat as _SS
        import s7_mllm
    except Exception:
        return None

    # 1. Rule-based diagnosis
    diag = _SD._coach(window=hands)
    if diag.get("locked"):
        return None

    # 2. Extract leaks
    leaks = []
    for o in (diag.get("vs_opt") or []):
        if o.get("verdict") in ("✗", "⚠"):
            leaks.append({"k": o["k"], "you": o["you"], "target": o["target"],
                          "verdict": o["verdict"], "note": o.get("note", "")})
    if not leaks:
        leaks = [{"k": "No leaks", "you": "all green", "target": "optimal", "verdict": "✓"}]

    # 3. Build coach prompt
    try:
        rep = s7_report.report()
    except Exception:
        rep = "(no report available)"
    try:
        meth = open(os.path.join(HERE, "system7_prompt.md")).read()
    except Exception:
        meth = ""
    cur = _SD._deployed_knobs()

    win_txt = "ultimas %s manos" % hands
    leaks_str = "; ".join("%s: %s vs objetivo %s [%s]" % (o["k"], o["you"], o["target"], o["verdict"])
                          for o in (diag.get("vs_opt") or []))
    rivals = "; ".join("%s (VPIP %s/PFR %s/AF %s)" % (o.get("name"), o.get("vpip"), o.get("pfr"), o.get("af"))
                       for o in (diag.get("opp") or [])[:6]) or "(sin lecturas de rivales)"

    system = ("Eres un coach de NLHE HEADS-UP (HU) de elite. Te paso el diagnostico "
              "tu-juego-vs-optimo-HU, la config actual y los RIVALES HU. "
              "(1) Analisis ACCIONABLE de leaks HU: SB abrir/robar/sizing, BB defensa+3bet. "
              "(2) Propón UNA versión nueva partiendo de la actual. Termina con SOLO un bloque ```json``` con las "
              "claves a CAMBIAR: opening_ranges {SB:[...], BB:[...]}, threebet_value, threebet_bluff, knobs. "
              "Incluye solo lo que cambies. Español, MUY conciso.\n\n" + meth[:400])
    user = ("VENTANA: %s\nDIAGNOSTICO vs optimo HU: %s\nRIVALES HU: %s\nCONFIG ACTUAL knobs: %s\n\nINFORME:\n%s"
            % (win_txt, leaks_str or "(sin datos)", rivals, json.dumps(cur), rep))

    # 4. Call LLM
    provider, model = _SD._llm_route()
    r = s7_mllm._chat(provider, model, system, user, max_tokens=10000)

    if r.get("error") or not r.get("answer"):
        return None

    txt = r.get("answer", "")

    # 5. Extract JSON proposal
    m = re.search(r"```json\s*(\{.*\})\s*```", txt, re.S) or re.search(r"(\{.*\})\s*$", txt, re.S)
    if not m:
        return None

    try:
        cfg = _SD._validate_strat(json.loads(m.group(1)))
    except Exception:
        return None

    if not cfg:
        return None

    # 6. Save strategy file
    version = "coach-" + time.strftime("%m%d-%H%M")
    try:
        _SS.save(version, cfg)
    except Exception:
        return None

    # 7. Store proposal in DB
    prose = txt[:m.start()].strip() if m else txt
    try:
        c = sqlite3.connect(s7_stats.DB, timeout=60)
        c.execute("INSERT INTO proposals(id, ts, type, status, version, config, prose, agent, game, by) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (version, time.time(), "coach", "pending", version,
                   json.dumps(cfg, default=str), prose,
                   _evolve_agent, _evolve_game, "llm"))
        c.commit()
        c.close()
    except Exception as e:
        print("[s7-evolve] ERROR persistiendo propuesta %s: %s" % (version, str(e)[:200]), flush=True)
        return None

    global _evolve_pending
    _evolve_pending = True

    _evolve_cache.update(
        ts=time.time(), txt=prose, hands=hands, proposal=cfg, version=version,
        err=None, running=False, window=hands, leaks=leaks
    )

    return {"ok": True, "version": version, "leaks": leaks, "hands": hands, "prose": prose[:500]}


def get_evolve_status():
    status = _get_status()
    status["evolve_pending"] = _evolve_pending
    status["last_proposal"] = _evolve_cache.get("version")
    status["last_error"] = _evolve_cache.get("err")
    return status


def get_pending():
    return _get_pending_list()


def get_pending_count():
    return len(_get_pending_list())


def get_proposal_detail(id):
    return _get_proposal(id)


def get_history(game=None):
    return _get_history(game)
