"""System 7 — endpoints JSON nuevos del rediseño (LAB · PRODUCCIÓN · TRACKER).

Funciones puras de datos + lanzadores que reúsan s7_stats / s7_agents / s7_jobs / s7_batch /
run_pvp / s7_tracker. s7_dash.py las enruta. Se mantiene fuera del monolito para no engordarlo.
"""
import json
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import threading
# Bifurcación de la BD: cash vs torneo (DBs separadas). El runner escribe en la del game del perfil;
# el dashboard lee la del game activo (thread-local, fijado por petición desde ?game=).
_DATA = os.path.dirname(os.environ.get("S7_STATS_DB", "")) or os.path.join(HERE, "data")
DB_CASH = os.environ.get("S7_DB_CASH") or os.path.join(_DATA, "s7_cash.db")
DB_TOURNEY = os.environ.get("S7_DB_TOURNEY") or os.path.join(_DATA, "s7_tourney.db")
DB = DB_CASH                      # back-compat default
_ctx = threading.local()

import s7_stats     # noqa: E402
import s7_agents    # noqa: E402
import s7_jobs      # noqa: E402
try:
    import s7_strat
except Exception:
    s7_strat = None

# Tope de concurrencia del Eval (evita los 429 que vimos al correr 7 a la vez).
EVAL_MAXC = int(os.environ.get("S7_EVAL_MAXC", "3"))


def set_game(g):
    _ctx.game = "tournament" if str(g) == "tournament" else "cash"


def curgame():
    return getattr(_ctx, "game", "cash")


def _dbpath(g=None):
    return DB_TOURNEY if (g or curgame()) == "tournament" else DB_CASH


def _ro():
    return sqlite3.connect(f"file:{_dbpath()}?mode=ro", uri=True, timeout=5)


def _ci(bbs):
    """Media ± IC95 sobre la lista de bb/100 (N agentes del perfil). CI honesto vs el ±20 de 1 run."""
    n = len(bbs)
    if not n:
        return {"n_evals": 0, "mean": None, "ci": None}
    mean = sum(bbs) / n
    ci = None
    if n > 1:
        sd = (sum((x - mean) ** 2 for x in bbs) / (n - 1)) ** 0.5
        ci = 1.96 * sd / (n ** 0.5)
    return {"n_evals": n, "mean": round(mean, 1), "ci": (round(ci, 1) if ci is not None else None)}


def _label_matches(lbl, name):
    return bool(lbl) and (lbl == name or lbl.startswith("clasif-" + name) or lbl.startswith("lab-" + name))


# ── Agentes (perfiles) ───────────────────────────────────────────────────────
def agents_list():
    profs = [p for p in (s7_agents.load(n) for n in s7_agents.names())
             if p and p.get("game", "cash") == curgame()]
    runs = {}
    try:
        c = _ro()
        for lbl, bb, h in c.execute("select run_label,adjusted_bb100,hands from runs"):
            runs.setdefault(lbl, []).append((bb, h))
        c.close()
    except Exception:
        pass
    out = []
    for p in profs:
        bbs = [bb for lbl, vals in runs.items() if _label_matches(lbl, p["name"])
               for (bb, h) in vals if bb is not None and (h or 0) >= 400]
        out.append({**p, **_ci(bbs)})
    out.sort(key=lambda a: (a.get("mean") is None, -(a.get("mean") if a.get("mean") is not None else -1e9)))
    return {"agents": out, "strategies": (s7_strat.names() if s7_strat else [])}


def save_agent(body):
    try:
        return {"ok": True, "agent": s7_agents.save(body)}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)[:200]}


def delete_agent(name):
    if not re.fullmatch(r"[a-z0-9_-]{1,24}", str(name or "")):
        return {"error": "nombre inválido"}
    return {"ok": s7_agents.delete(name)}


def rank_delete(body):
    """Quita un agente del ranking: mueve su cred .clasif/<label>.json a .clasif/.trash (recuperable;
    la cred lleva el apiKey reclamable). _rank solo mira .clasif/*.json de primer nivel, así desaparece."""
    label = str(body.get("label", "")).strip()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", label):
        return {"error": "label inválido"}
    cd = os.environ.get("S7_CLASIF_DIR", os.path.join(_DATA, ".clasif"))
    src = os.path.join(cd, label + ".json")
    moved = False
    if os.path.exists(src):
        try:
            trash = os.path.join(cd, ".trash")
            os.makedirs(trash, exist_ok=True)
            os.replace(src, os.path.join(trash, label + ".json"))
            moved = True
        except Exception as e:
            return {"error": str(e)[:200]}
    d = _jload(_DEPLOYS_PATH, {})        # también quita la entrada de deploy (agentes PvP sin cred)
    if label in d:
        d.pop(label, None)
        _jwrite(_DEPLOYS_PATH, d)
        moved = True
    if not moved:
        return {"error": "no encontrado"}
    return {"ok": True, "label": label}


# ── LAB: evaluar (lote concurrente capado) + report ──────────────────────────
def lab_eval(body):
    name = str(body.get("agent", "")).strip().lower()
    p = s7_agents.load(name)
    if not p:
        return {"error": "perfil no encontrado"}
    try:
        total = max(1, min(50, int(body.get("total", 6))))
    except Exception:
        total = 6
    try:
        maxc = max(1, min(EVAL_MAXC, int(body.get("maxc", 2))))
    except Exception:
        maxc = 2
    group = str(body.get("group", "") or name).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,24}", group):
        return {"error": "etiqueta inválida (a-z 0-9 _ - , máx 24)"}
    strat = p.get("strategy") or "std"
    engine = p.get("engine") or "hybrid"
    env = {"S7_STATS_DB": _dbpath(p.get("game", "cash"))}     # escribe en la DB del game del perfil
    env.update(s7_agents.env_for(p))          # S7_STRAT/S7_MODEL/S7_HUD/S7_TRACKER (heredado por los hijos)
    env["S7_POLL_INTERVAL"] = "0.5"           # Eval más rápido (sondeo más frecuente; se autoback-offea en 429)
    argv = s7_jobs.pyrun("s7_batch.py", total, maxc, strat, engine, group)   # tag = etiqueta del grupo
    try:
        unit = s7_jobs.launch("lab-" + group, argv, env)
    except Exception as e:
        return {"error": str(e)[:300]}
    g = _jload(_GROUPS_PATH, {})
    g[group] = {"agent": name, "game": p.get("game", "cash"), "total": total, "maxc": maxc, "ts": time.time()}
    _jwrite(_GROUPS_PATH, g)
    return {"ok": True, "unit": unit, "agent": name, "group": group, "total": total, "maxc": maxc}


def lab_report(agent):
    name = str(agent or "").strip().lower()
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}
    runs = []
    for r in c.execute("select run_label,agent_id,engine,hands,adjusted_bb100,raw_bb100,m3_calls,note,ts "
                       "from runs order by ts desc"):
        if name and not _label_matches(r[0], name):
            continue
        runs.append({"label": r[0], "agent_id": r[1], "engine": r[2], "hands": r[3],
                     "bb100": r[4], "raw_bb100": r[5], "m3": r[6], "note": r[7], "ts": r[8]})
    bbs = [x["bb100"] for x in runs if x["bb100"] is not None and (x["hands"] or 0) >= 400]
    labels = sorted({x["label"] for x in runs if x["label"]})
    bypos, worst = [], []
    if labels:
        ph = ",".join("?" * len(labels))
        try:
            order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
            for pos, agg, vol, n in c.execute(
                    "select pos,sum(case when action in('bet','raise','all-in') then 1 else 0 end),"
                    "sum(voluntary),count(*) from decisions where street='preflop' and pos!='' "
                    f"and run_label in ({ph}) group by pos", labels):
                bypos.append({"pos": pos, "n": n, "vpip": round(100 * (vol or 0) / n) if n else 0,
                              "agg": round(100 * (agg or 0) / n) if n else 0})
            bypos.sort(key=lambda x: order.get(x["pos"], 9))
        except Exception:
            pass
        try:
            for hk, delta, hole, board in c.execute(
                    "select d.hand_key, hr.chip_delta, d.hole, hr.board from decisions d "
                    "join hand_results hr on hr.table_id=substr(d.hand_key,1,instr(d.hand_key,':')-1) "
                    f"where d.run_label in ({ph}) and d.street='preflop' and hr.chip_delta is not null "
                    "order by hr.chip_delta asc limit 12", labels):
                worst.append({"key": hk, "delta": delta, "hole": hole, "board": board})
        except Exception:
            pass
    c.close()
    return {"agent": name, "runs": runs, "agg": _ci(bbs),
            "hands": sum((x["hands"] or 0) for x in runs), "bypos": bypos, "worst": worst}


# ── PRODUCCIÓN: 1 activo + cola de eventos dev.fun ───────────────────────────
_QUEUE_PATH = os.path.join(_DATA, "prod_queue.json")
_DEPLOYS_PATH = os.path.join(_DATA, "prod_deploys.json")
_GROUPS_PATH = os.path.join(_DATA, "lab_groups.json")
_qlock = threading.Lock()


def _jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _jwrite(path, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception:
        pass


def _any_key():
    """An apiKey usable for read-only Arena calls (listing competitions)."""
    for p in (os.path.join(_DATA, ".arena-pg-credentials"), os.path.join(HERE, ".arena-credentials")):
        try:
            return json.load(open(p)).get("apiKey")
        except Exception:
            pass
    cd = os.environ.get("S7_CLASIF_DIR", os.path.join(_DATA, ".clasif"))
    try:
        for fn in sorted(os.listdir(cd)):
            if fn.endswith(".json"):
                k = json.load(open(os.path.join(cd, fn))).get("apiKey")
                if k:
                    return k
    except Exception:
        pass
    return None


def production_competitions():
    """Eventos dev.fun jugables: Eval + competiciones activas (torneos/playground) vía list-active."""
    out = [{"id": "eval", "name": "Poker Eval S1 · 500 manos (gratis)", "kind": "eval", "paid": False}]
    key = _any_key()
    if not key:
        return {"competitions": out, "note": "sin credenciales para listar torneos"}
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(HERE, "examples"))
        from arena_client import ArenaClient, DEFAULT_BASE
        c = ArenaClient(os.environ.get("ARENA_API_BASE", DEFAULT_BASE), api_key=key)
        try:
            r = c.get("/competition/list-active")
        finally:
            c.close()
        comps = r if isinstance(r, list) else (r.get("competitions") or r.get("data") or [])
        for x in comps:
            if str(x.get("gameType")) != "TexasHoldem":
                continue
            nm = str(x.get("name", "")) or str(x.get("id"))
            paid = bool(x.get("buyIn") or x.get("entryFee"))
            kind = "tournament" if "tournament" in nm.lower() else "playground"
            out.append({"id": x.get("id"), "name": nm, "kind": kind, "paid": paid})
    except Exception as e:
        out.append({"id": "", "name": "(no se pudieron listar torneos: %s)" % str(e)[:80], "kind": "error", "paid": False})
    return {"competitions": out}


def _prod_active():
    return [j for j in s7_jobs.list_jobs() if j["label"].startswith("prod-") and j["state"] == "active"]


def _is_continuous(competition):
    """Playground/torneo (run_pvp) = bucle continuo (no acaba solo); Eval = one-shot."""
    return str(competition or "") not in ("eval", "seed_poker_eval_s1", "")


def _prod_launch(agent, competition):
    p = s7_agents.load(agent)
    if not p:
        return {"error": "perfil no encontrado"}
    import secrets as _sec
    game = p.get("game", "cash")
    env = {"S7_STATS_DB": _dbpath(game)}
    env.update(s7_agents.env_for(p))
    label = "prod-" + _sec.token_hex(4)
    if competition in ("eval", "seed_poker_eval_s1", ""):
        env["S7_SAVE_CREDS"] = "1"; env["S7_RUN_LABEL"] = label; env["S7_AGENT_NAME"] = agent
        env["S7_POLL_INTERVAL"] = "0.5"        # Eval más rápido
        env["S7_MATCH_TIMEOUT"] = "9000"       # 150 min: deja completar las 500 manos en híbrido (M3 es lento)
        argv = s7_jobs.pyrun("s7_test.py", "--engine", p.get("engine", "hybrid"), "--matches", "1")
        compname = "Eval S1"
    else:
        env["ARENA_COMPETITION_ID"] = competition
        env["S7_CREDS_FILE"] = os.path.join(_DATA, ".arena-pg-credentials")
        env["S7_RUN_LABEL"] = label
        # M3 en vivo: llamadas no bloqueantes (pool) + acotadas por reloj (run_pvp async path)
        env["S7_PVP_ASYNC"] = "1"
        env["S7_PVP_WORKERS"] = "4"
        env["S7_PVP_SUBMIT_MARGIN"] = "3"
        # Modelo RÁPIDO en vivo: M3 (~30s, medido) NO cabe en el reloj del Playground (~28.6s);
        # MiniMax-M2 (~9s, medido) sí, con razonamiento completo. El Eval sigue con el modelo del perfil.
        _apply_settings_keys()                              # keys de Settings (settings.json) -> os.environ
        env.update(_settings().get("keys") or {})           # y al env del run_pvp/tracker
        _lv = _settings().get("live") or {}                 # modelo en vivo desde Settings
        try:
            import s7_mllm
            _lbase, _lkey = s7_mllm._resolve(_lv.get("provider") or "minimax")
            if _lbase:
                env["OPENAI_BASE_URL"] = _lbase
            if _lkey:
                env["OPENAI_API_KEY"] = _lkey
        except Exception:
            pass
        env["S7_MODEL"] = _lv.get("model") or os.environ.get("S7_PVP_MODEL", "MiniMax-M2")
        env["S7_LLM_MIN_DEADLINE"] = "12"      # M2 cabe hasta en deadlines cortos -> más cobertura LLM
        env["S7_LLM_TIMEOUT"] = "18"           # corta un spike raro (M2 ~9s)
        env["S7_MAX_TOKENS"] = "1500"          # M2 da answer completo en ~1500 tok
        env["S7_HUD"] = "1"                     # node-locking por rival (HUD)
        env["S7_TRACKER"] = "1"                # leer opp_profiles (tracker) antes que el live
        env["S7_HUD_MIN_N"] = os.environ.get("S7_HUD_MIN_N", "3500")   # adapta solo con >=3500 manos del Arena
        argv = s7_jobs.pyrun("run_pvp.py")
        compname = competition
        try:                                   # tracker sidecar: opp_profiles (HUD) + opp_hands durante el juego
            s7_jobs.launch("tracker-" + label, s7_jobs.pyrun("s7_tracker.py", "--interval", "300"),
                           {**env, "S7_TRACK_MAX_PROFILES": "40"})
        except Exception:
            pass
    try:
        unit = s7_jobs.launch(label, argv, env)
    except Exception as e:
        return {"error": str(e)[:300]}
    d = _jload(_DEPLOYS_PATH, {})
    d[label] = {"agent": agent, "competition": competition, "compname": compname, "game": game, "ts": time.time()}
    _jwrite(_DEPLOYS_PATH, d)
    return {"ok": True, "unit": unit, "label": label, "agent": agent}


def production_deploy(body):
    agent = str(body.get("agent", "")).strip().lower()
    comp = str(body.get("competition", "") or "")
    if not s7_agents.load(agent):
        return {"error": "perfil no encontrado"}
    deploys = _jload(_DEPLOYS_PATH, {})
    active = _prod_active()
    # dedupe: mismo agente+evento ya activo o en cola → no duplicar
    for j in active:
        m = deploys.get(j["label"], {})
        if m.get("agent") == agent and str(m.get("competition") or "") == comp:
            return {"error": "ese agente ya está jugando ese evento"}
    if any(it.get("agent") == agent and str(it.get("competition") or "") == comp
           for it in _jload(_QUEUE_PATH, [])):
        return {"error": "ese agente+evento ya está en la cola"}
    if active:                                               # 1 a la vez → a la cola
        with _qlock:
            q = _jload(_QUEUE_PATH, [])
            q.append({"agent": agent, "competition": comp, "ts": time.time()})
            _jwrite(_QUEUE_PATH, q)
        warn = ("el activo es un evento continuo (PvP/Playground): la cola no avanzará hasta que lo pares"
                if any(_is_continuous(deploys.get(j["label"], {}).get("competition")) for j in active) else None)
        return {"ok": True, "queued": True, "agent": agent, "queue_len": len(q), "warn": warn}
    return _prod_launch(agent, comp)


def production_status():
    deploys = _jload(_DEPLOYS_PATH, {})
    jobs = [j for j in s7_jobs.list_jobs() if j["label"].startswith("prod-")]
    active = [j for j in jobs if j["state"] == "active"]
    snap = None
    hpl = {}
    try:
        c = _ro()
        for lbl, n in c.execute("select run_label,count(distinct hand_key) from decisions group by run_label"):
            hpl[lbl] = n
        r = c.execute("select table_chips,hands,rebuys,ts from bankroll where table_chips is not null "
                      "order by ts desc limit 1").fetchone()
        if r and (time.time() - (r[3] or 0) < 600):      # solo si el snapshot es reciente (PvP en vivo, no uno viejo)
            snap = {"stack": r[0], "hands": r[1], "rebuys": r[2], "ts": r[3]}
        c.close()
    except Exception:
        pass

    def mk(j):
        m = deploys.get(j["label"], {})
        return {"label": j["label"], "unit": j["unit"], "state": j["state"],
                "agent": m.get("agent"), "competition": m.get("compname") or m.get("competition"),
                "continuous": _is_continuous(m.get("competition")),
                "hands": hpl.get(j["label"], 0)}      # manos reales del agente (= conteo del ranking)
    return {"running": bool(active), "active": [mk(j) for j in active],
            "jobs": [mk(j) for j in jobs], "queue": _jload(_QUEUE_PATH, []), "bankroll": snap}


def production_stop(body):
    unit = str(body.get("unit", "") or "")
    if not re.fullmatch(r"arena-run-prod-[a-f0-9]{8}", unit):
        return {"error": "unit inválida"}
    try:
        s7_jobs.stop(unit)
    except Exception as e:
        return {"error": str(e)}
    try:                                    # parar también el tracker sidecar de ese deploy
        s7_jobs.stop(unit.replace("arena-run-prod-", "arena-run-tracker-prod-"))
    except Exception:
        pass
    return {"ok": True}


# ── Settings: API keys de proveedores LLM (.env) + modelo vivo/default ──────────
_SETTINGS_PATH = os.path.join(_DATA, "settings.json")
_ENV_PATH = os.path.join(HERE, ".env")
_PROV_KEY_ENV = {"minimax": "OPENAI_API_KEY", "xiaomi": "XIAOMI_API_KEY",
                 "openrouter": "OPENROUTER_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
_PROV_BASE_ENV = {"minimax": "OPENAI_BASE_URL", "xiaomi": "XIAOMI_BASE_URL",
                  "openrouter": "OPENROUTER_BASE_URL", "deepseek": "DEEPSEEK_BASE_URL"}


def _settings():
    return _jload(_SETTINGS_PATH, {"live": {}, "default": {}})


def _settings_save(d):
    try:
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def _env_upsert(var, val):
    """Reemplaza/añade VAR=val en .env (preserva el resto) + os.environ."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", var or ""):
        return False
    try:
        lines = open(_ENV_PATH, encoding="utf-8").read().splitlines()
    except Exception:
        lines = []
    out, found = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == var:
            out.append("%s=%s" % (var, val)); found = True
        else:
            out.append(ln)
    if not found:
        out.append("%s=%s" % (var, val))
    try:
        with open(_ENV_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except Exception:
        return False
    os.environ[var] = val
    return True


def settings_get():
    """Modelo vivo/default + estado por proveedor (ENMASCARADO: nunca devuelve el valor de la key)."""
    _apply_settings_keys()
    st = _settings()
    keys = {p: {"configured": bool(os.environ.get(_PROV_KEY_ENV[p])),
                "base": os.environ.get(_PROV_BASE_ENV[p]) or ""} for p in _PROV_KEY_ENV}
    return {"live": st.get("live") or {}, "default": st.get("default") or {}, "keys": keys}


def _apply_settings_keys():
    """Carga las keys persistidas en settings.json (volumen /data) a os.environ (ganan sobre .env)."""
    for k, v in (_settings().get("keys") or {}).items():
        if v:
            os.environ[k] = str(v)


def settings_save_key(body):
    provider = str(body.get("provider", ""))
    if provider not in _PROV_KEY_ENV:
        return {"error": "proveedor desconocido"}
    key = str(body.get("key", "") or "").strip()
    base = str(body.get("base", "") or "").strip()
    st = _settings()
    keys = st.setdefault("keys", {})       # persistente en /data/settings.json (sobrevive rebuilds)
    if key:
        keys[_PROV_KEY_ENV[provider]] = key
        os.environ[_PROV_KEY_ENV[provider]] = key
    if base:
        keys[_PROV_BASE_ENV[provider]] = base
        os.environ[_PROV_BASE_ENV[provider]] = base
    _settings_save(st)
    return {"ok": True, "provider": provider, "configured": bool(os.environ.get(_PROV_KEY_ENV[provider]))}


def settings_set_model(body):
    scope = str(body.get("scope", "live"))
    mid = str(body.get("model", "") or "").strip()         # "provider:model"
    if scope not in ("live", "default") or not mid:
        return {"error": "scope o modelo inválido"}
    prov, model = (mid.split(":", 1) if ":" in mid else ("minimax", mid))
    st = _settings()
    st[scope] = {"provider": prov, "model": model, "id": mid}
    _settings_save(st)
    if scope == "default":                                  # aplica ya al dashboard (Coach/stratgen)
        try:
            import s7_mllm
            base, key = s7_mllm._resolve(prov)
            if base:
                os.environ["OPENAI_BASE_URL"] = base
            if key:
                os.environ["OPENAI_API_KEY"] = key
            os.environ["S7_MODEL"] = model
        except Exception:
            pass
    return {"ok": True, "scope": scope, "provider": prov, "model": model}


def settings_apply_live(body=None):
    """Re-despliega el Playground activo con el modelo live nuevo (lo lee _prod_launch)."""
    deploys = _jload(_DEPLOYS_PATH, {})
    pvp = next((a for a in (production_status().get("active") or []) if a.get("continuous")), None)
    if not pvp:
        return {"ok": True, "note": "sin Playground activo; el modelo se aplicará al desplegar"}
    dp = deploys.get(pvp.get("label"), {})
    agent, comp = dp.get("agent"), dp.get("competition")
    if not agent or not comp:
        return {"error": "no se pudo resolver el agente/evento activo"}
    production_stop({"unit": pvp.get("unit")})
    time.sleep(1)
    return _prod_launch(agent, comp)


def production_queue_remove(body):
    i = body.get("index")
    with _qlock:
        q = _jload(_QUEUE_PATH, [])
        if isinstance(i, int) and 0 <= i < len(q):
            q.pop(i)
            _jwrite(_QUEUE_PATH, q)
    return {"ok": True, "queue": _jload(_QUEUE_PATH, [])}


def _my_agent_id():
    """agentId del agente autenticado (.arena-pg-credentials) — unifica las vistas a él."""
    try:
        return json.load(open(os.path.join(_DATA, ".arena-pg-credentials"))).get("agentId") or ""
    except Exception:
        return ""


def production_session(label=""):
    """Stats de la sesión en juego, filtradas por run_label (un agente) o globales (todos):
    rejilla preflop 13×13 (qué manos jugamos), reparto de decisiones postflop por calle y KPIs."""
    label = str(label or "")
    if label and not re.fullmatch(r"[a-z0-9_-]{1,40}", label):
        return {"error": "label inválido"}
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}
    if label:
        cond, A = "run_label like ?", (label + "%",)
    else:                                   # sin label: unificar al agente autenticado
        _my = _my_agent_id()
        cond = ("(agent_id = ? OR (agent_id IS NULL AND run_label LIKE 'prod-%' "
                "AND run_label NOT IN (SELECT run_label FROM runs WHERE agent_id IS NOT NULL AND agent_id != ?)))")
        A = (_my, _my)

    def q(cols, extra="", extra_args=()):
        try:
            return c.execute("select %s from decisions where %s%s" % (cols, cond, extra), A + extra_args).fetchall()
        except Exception:
            return []

    total = (q("count(*)") or [[0]])[0][0] or 0
    hands = (q("count(distinct hand_key)") or [[0]])[0][0] or 0
    m3 = (q("count(*)", " and engine='M3'") or [[0]])[0][0] or 0
    pf = q("hand_class,count(*),sum(voluntary),sum(preflop_raise)",
           " and street='preflop' and hand_class!='' group by hand_class")
    classes = {r[0]: {"n": r[1], "vpip": round(100 * (r[2] or 0) / r[1]) if r[1] else 0,
                      "pfr": round(100 * (r[3] or 0) / r[1]) if r[1] else 0} for r in pf}
    order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
    pos = q("pos,count(*),sum(voluntary),sum(preflop_raise)", " and street='preflop' and pos!='' group by pos")
    bypos = sorted([{"pos": r[0], "n": r[1], "vpip": round(100 * (r[2] or 0) / r[1]) if r[1] else 0,
                     "pfr": round(100 * (r[3] or 0) / r[1]) if r[1] else 0} for r in pos],
                   key=lambda x: order.get(x["pos"], 9))
    post = {}
    for st in ("flop", "turn", "river"):
        d = {a: n for a, n in q("action,count(*)", " and street=? group by action", (st,))}
        post[st] = {"bet": (d.get("bet", 0) + d.get("raise", 0) + d.get("all-in", 0)),
                    "call": d.get("call", 0), "check": d.get("check", 0), "fold": d.get("fold", 0)}
    try:
        if label:
            runs = c.execute("select adjusted_bb100,hands from runs where run_label like ?", (label + "%",)).fetchall()
        else:
            runs = c.execute("select adjusted_bb100,hands from runs").fetchall()
    except Exception:
        runs = []
    bbs = [r[0] for r in runs if r[0] is not None and (r[1] or 0) >= 400]
    c.close()
    return {"label": label, "hands": hands, "decisions": total,
            "m3pct": round(100 * m3 / total, 1) if total else 0,
            "classes": classes, "ranks": list("AKQJT98765432"), "bypos": bypos,
            "postflop": post, "agg": _ci(bbs)}


def production_account():
    """Agente reclamado de la cuenta en uso (data/.arena-pg-credentials) + su posición en el
    leaderboard de cada evento (Eval + competiciones activas). Pagina el leaderboard buscando su agentId."""
    import urllib.request as _u
    base = os.environ.get("ARENA_API_BASE", "https://arena.dev.fun/api/arena")
    try:
        creds = json.load(open(os.path.join(_DATA, ".arena-pg-credentials")))
    except Exception as e:
        return {"error": "sin creds de cuenta: " + str(e)[:80]}
    aid = creds.get("agentId")
    key = creds.get("apiKey") or ""
    if not aid:
        return {"error": "creds sin agentId"}

    def api(path, auth=False):
        try:
            h = {"x-arena-api-key": key} if auth else {}
            return json.loads(_u.urlopen(_u.Request(base + path, headers=h), timeout=12).read())
        except Exception:
            return None

    cs = api("/auth/claim/status", auth=True) or {}
    agent = {"agentId": aid, "handle": creds.get("handle"), "name": creds.get("name"),
             "claimed": cs.get("claimed"), "owner": cs.get("xHandle")}
    comps, seen = [], set()

    def add(cid, nm):
        if cid and cid not in seen:
            seen.add(cid)
            comps.append((cid, nm))
    add("seed_poker_eval_s1", "Eval S1")
    la = api("/competition/list-active")
    for c in (la if isinstance(la, list) else (la or {}).get("competitions") or (la or {}).get("data") or []):
        if isinstance(c, dict) and str(c.get("gameType")) == "TexasHoldem":
            add(c.get("id"), c.get("name") or c.get("id"))
    events = []
    for cid, nm in comps:
        rank = total = score = None
        for off in range(0, 3500, 100):                 # pagina buscando nuestro agentId
            lb = api("/competition/leaderboard?competitionId=%s&limit=100&offset=%d" % (cid, off))
            if not lb:
                break
            total = lb.get("total") or total
            data = lb.get("data") or []
            m = next((x for x in data if x.get("agent", {}).get("id") == aid), None)
            if m:
                rank, score = m.get("rank"), m.get("totalScore")
                if not agent.get("handle"):              # coge el handle/nombre del propio leaderboard
                    agent["handle"] = m.get("agent", {}).get("handle") or agent.get("handle")
                    agent["name"] = agent.get("name") or m.get("agent", {}).get("name")
                break
            if not data or off + 100 >= (total or 0):
                break
            time.sleep(0.08)                             # suave con el rate-limit
        events.append({"name": nm, "rank": rank, "total": total,
                       "score": (round(score, 1) if isinstance(score, (int, float)) else None),
                       "registered": rank is not None})
    return {"agent": agent, "events": events}


def queue_tick_once():
    """Si no hay producción activa y la cola no está vacía, lanza el siguiente (1 a la vez)."""
    with _qlock:
        if _prod_active():
            return
        q = _jload(_QUEUE_PATH, [])
        if not q:
            return
        item = q.pop(0)
        _jwrite(_QUEUE_PATH, q)
    _prod_launch(item.get("agent"), item.get("competition"))


def queue_loop():
    while True:
        try:
            queue_tick_once()
        except Exception:
            pass
        time.sleep(8)


# ── TRACKER (PokerTracker/HM) ─────────────────────────────────────────────────
def tracker_opponents(limit=200):
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}
    out = []
    _thr = int(os.environ.get("S7_HUD_MIN_N", "3500"))   # umbral de adaptación (= el del motor)
    try:
        import decide_system7 as _DS
    except Exception:
        _DS = None
    try:
        for r in c.execute("select opp_id,name,n,vpip,pfr,af,bluff_pct,wtsd,wsd,style,shown_hands,last_seen "
                           "from opp_profiles order by (n is null), n desc limit ?", (limit,)):
            sty = (json.loads(r[9]) if r[9] else None)
            arc = "UNKNOWN"
            if _DS:
                try:
                    arc = _DS._archetype({"N": r[2], "vpip": r[3], "pfr": r[4], "af": r[5], "playingStyle": sty})
                except Exception:
                    pass
            out.append({"agent_id": r[0], "name": r[1], "n": r[2], "vpip": r[3], "pfr": r[4],
                        "af": r[5], "bluff": r[6], "wtsd": r[7], "wsd": r[8], "style": sty,
                        "shown_hands": r[10], "last_seen": r[11],
                        "archetype": arc, "adapting": (r[2] or 0) >= _thr})
    except Exception:
        pass
    c.close()
    return {"opponents": out}


def tracker_own(limit=300):
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}
    out = []
    try:
        for r in c.execute("select hand_id,ts,agent_id,competition_id,seat,hole,board,payout,committed,"
                           "score,chip_delta,reasoning,replay_url from own_hands order by ts desc limit ?",
                           (limit,)):
            out.append({"hand_id": r[0], "ts": r[1], "agent_id": r[2], "competition": r[3], "seat": r[4],
                        "hole": r[5], "board": r[6], "payout": r[7], "committed": r[8], "score": r[9],
                        "chip_delta": r[10], "reasoning": r[11], "replay_url": r[12]})
    except Exception:
        pass
    c.close()
    return {"hands": out}


def tracker_harvest():
    """Lanza una pasada del harvester como job de fondo."""
    try:
        unit = s7_jobs.launch("tracker", s7_jobs.pyrun("s7_tracker.py", "--once"), {"S7_STATS_DB": _dbpath()})
    except Exception as e:
        return {"error": str(e)[:300]}
    return {"ok": True, "unit": unit}


# ── LAB: monitor por tarea de evaluación ─────────────────────────────────────
def lab_task(agent):
    """Datos de UNA tarea (filtrados por run_label clasif-<agent>%): contadores, distribución de
    bb/100, VPIP/PFR por posición, heatmap preflop, fuerza postflop, y jobs activos de la tarea."""
    name = str(agent or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,24}", name):
        return {"error": "tarea inválida"}
    like = "clasif-" + name + "%"
    try:
        c = _ro()
    except Exception as e:
        return {"error": str(e)}

    def q(sql, a=(like,)):
        try:
            return c.execute(sql, a).fetchall()
        except Exception:
            return []

    def one(sql, a=(like,)):
        r = q(sql, a)
        return (r[0][0] or 0) if r else 0

    total = one("select count(*) from decisions where run_label like ?")
    hands = one("select count(distinct hand_key) from decisions where run_label like ?")
    eng = dict(q("select engine,count(*) from decisions where run_label like ? group by engine"))
    m3 = eng.get("M3", 0)
    runs = q("select run_label,hands,adjusted_bb100,note,ts from runs where run_label like ? order by ts desc")
    bbs = [r[2] for r in runs if r[2] is not None and (r[1] or 0) >= 400]
    order = {"UTG": 0, "MP": 1, "CO": 2, "BTN": 3, "SB": 4, "BB": 5}
    pos = q("select pos,count(*),sum(voluntary),sum(preflop_raise) from decisions "
            "where street='preflop' and pos!='' and run_label like ? group by pos")
    bypos = sorted([{"pos": r[0], "n": r[1], "vpip": round(100 * (r[2] or 0) / r[1]) if r[1] else 0,
                     "pfr": round(100 * (r[3] or 0) / r[1]) if r[1] else 0} for r in pos],
                   key=lambda x: order.get(x["pos"], 9))
    pf = q("select hand_class,count(*),sum(voluntary),sum(preflop_raise) from decisions "
           "where street='preflop' and hand_class!='' and run_label like ? group by hand_class")
    classes = {r[0]: {"n": r[1], "vpip": round(100 * (r[2] or 0) / r[1]) if r[1] else 0,
                      "pfr": round(100 * (r[3] or 0) / r[1]) if r[1] else 0} for r in pf}
    strength = [{"s": r[0], "n": r[1], "agg": r[2] or 0, "call": r[3] or 0, "pasv": r[4] or 0} for r in q(
        "select strength,count(*),sum(case when action in('bet','raise','all-in') then 1 else 0 end),"
        "sum(case when action='call' then 1 else 0 end),"
        "sum(case when action in('check','fold') then 1 else 0 end) from decisions "
        "where street!='preflop' and strength!='' and run_label like ? group by strength order by 2 desc")]
    c.close()
    jobs = [j for j in s7_jobs.list_jobs() if j["label"] == "lab-" + name or j["label"].startswith("clasif-" + name)]
    return {"agent": name, "hands": hands, "decisions": total, "m3": m3,
            "m3pct": round(100 * m3 / total, 1) if total else 0,
            "agg": _ci(bbs), "samples": [round(x, 1) for x in bbs],
            "bypos": bypos, "classes": classes, "ranks": list("AKQJT98765432"), "strength": strength,
            "runs": [{"label": r[0], "hands": r[1], "bb100": r[2], "note": r[3]} for r in runs[:50]],
            "active": sum(1 for j in jobs if j["state"] == "active"),
            "jobs": [{"unit": j["unit"], "label": j["label"], "state": j["state"]} for j in jobs]}


def lab_stop(body):
    """Para una tarea de evaluación: el lote lab-<agent> + todos sus hijos clasif-<agent>* activos."""
    name = str(body.get("agent", "")).strip().lower()
    n = 0
    for j in s7_jobs.list_jobs():
        if (j["label"] == "lab-" + name or j["label"].startswith("clasif-" + name)) and j["state"] == "active":
            try:
                s7_jobs.stop(j["unit"]); n += 1
            except Exception:
                pass
    return {"ok": True, "stopped": n}


def lab_groups():
    """Grupos de evaluación (etiquetas) del game activo, para el selector del monitor."""
    g = _jload(_GROUPS_PATH, {})
    jobs = s7_jobs.list_jobs()
    prog = {}
    try:
        c = _ro()
        for lbl, n in c.execute("select run_label, count(distinct hand_key) from decisions group by run_label"):
            prog[lbl] = n
        c.close()
    except Exception:
        pass
    cg = curgame()
    out = []
    for grp, meta in g.items():
        if meta.get("game", "cash") != cg:
            continue
        active = any((j["label"] == "lab-" + grp or j["label"].startswith("clasif-" + grp))
                     and j["state"] == "active" for j in jobs)
        hands = sum(v for k, v in prog.items() if k and k.startswith("clasif-" + grp))
        out.append({"group": grp, "agent": meta.get("agent"), "ts": meta.get("ts"),
                    "active": active, "hands": hands})
    out.sort(key=lambda x: -(x["ts"] or 0))
    return {"groups": out}
