"""System 7 — agent profiles (the LAB "bundle": estrategia + motor + modelo LLM + HUD/tracker + nombre).

Un perfil es `data/agents/<name>.json`:
  {name, strategy, engine, model, provider, hud, tracker, note, ts}
- strategy : nombre de un `strategies/<strategy>.json` (rangos/knobs); "" = std.
- engine   : "heur" | "hybrid".
- model    : id del modelo LLM para el motor híbrido (p.ej. "MiniMax-M3"); "" = por defecto.
- provider : "minimax" | "openrouter" | "xiaomi" (igual que s7_mllm.PROVIDERS) → enruta base/clave.
- hud      : bool — HUD de rival on/off (S7_HUD).
- tracker  : bool — usar la BD del tracker (perfiles agregados) como HUD.

`env_for(perfil)` lo convierte en el dict de variables de entorno que leen los runners
(S7_STRAT/S7_MODEL/S7_HUD/S7_TRACKER/OPENAI_BASE_URL/OPENAI_API_KEY...). El `engine` se pasa como
argumento `--engine` a s7_test/s7_batch (no por env).
"""
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_STRAT_DIR = os.environ.get("S7_STRAT_DIR") or os.path.join(HERE, "strategies")
# Por defecto, hermano de strategies/ (en Docker: /data/agents junto a /data/strategies).
DIR = os.environ.get("S7_AGENTS_DIR") or os.path.join(os.path.dirname(_STRAT_DIR), "agents")

VALID_ENGINES = ("heur", "hybrid")
_NAME_RE = re.compile(r"[a-z0-9_-]{1,24}")


def _path(name):
    return os.path.join(DIR, str(name) + ".json")


def names():
    try:
        return sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    except Exception:
        return []


def load(name):
    """Return the profile dict for <name>, or None."""
    try:
        with open(_path(name), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else None
    except Exception:
        return None


def clean(profile: dict) -> dict:
    """Validate + normalise a profile. Raises ValueError on a bad name."""
    name = str(profile.get("name", "")).strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise ValueError("nombre inválido (a-z 0-9 _ - , máx 24)")
    return {
        "name": name,
        "strategy": str(profile.get("strategy", "") or ""),
        "game": profile.get("game") if profile.get("game") in ("cash", "tournament") else "cash",
        "engine": profile.get("engine") if profile.get("engine") in VALID_ENGINES else "hybrid",
        "model": str(profile.get("model", "") or ""),
        "provider": str(profile.get("provider", "") or ""),
        "hud": bool(profile.get("hud", True)),
        "tracker": bool(profile.get("tracker", True)),
        "title": str(profile.get("title", "") or "")[:60],
        "note": str(profile.get("note", "") or "")[:400],
        "ts": time.time(),
    }


def save(profile: dict) -> dict:
    """Persist a validated profile atomically. Returns the cleaned profile."""
    c = clean(profile)
    os.makedirs(DIR, exist_ok=True)
    tmp = _path(c["name"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _path(c["name"]))
    return c


def delete(name):
    try:
        os.remove(_path(name))
        return True
    except Exception:
        return False


def env_for(name_or_profile) -> dict:
    """Env vars the runners read, derived from a profile (name or dict)."""
    p = name_or_profile if isinstance(name_or_profile, dict) else (load(name_or_profile) or {})
    env = {"S7_HUD": "1" if p.get("hud", True) else "0",
           "S7_TRACKER": "1" if p.get("tracker", True) else "0",
           "S7_ENGINE": p.get("engine", "hybrid")}
    if p.get("strategy"):
        env["S7_STRAT"] = p["strategy"]
    if p.get("model"):
        env["S7_MODEL"] = p["model"]
    prov = p.get("provider")
    if prov and prov != "minimax":          # enruta el motor híbrido al endpoint del proveedor
        try:
            import s7_mllm
            base_env, key_env, default_base = s7_mllm.PROVIDERS.get(prov, (None, None, None))
            base = (os.environ.get(base_env) if base_env else None) or default_base
            key = os.environ.get(key_env) if key_env else None
            if base:
                env["OPENAI_BASE_URL"] = base
            if key:
                env["OPENAI_API_KEY"] = key
        except Exception:
            pass
    return env
