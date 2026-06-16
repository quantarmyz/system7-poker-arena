"""System 7 — versioned strategy config (editable by the COACH, launched via S7_STRAT).

A strategy is strategies/<name>.json with optional keys:
  base:           "std" | "wide"          (inherit a built-in opening range; default std)
  opening_ranges: {pos: [tokens]}         (explicit ranges; tokens like "22+","A2s+","KTo+")
  threebet_value: [hand_class]            threebet_bluff: [hand_class]
  knobs: {open_size_bb, threebet_mult, value_eq, station_mult, cbet_bluff_frac,
          commit_spr, perejil_flop, perejil_turn, perejil_relief, sizing:{texture:{street:frac}}}
Missing keys fall back to the std defaults in decide_system7 -> identical behaviour.
Select the active version with env S7_STRAT=<name>.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# S7_STRAT_DIR lets the strategies live on a volume (Docker); defaults to ./strategies.
DIR = os.environ.get("S7_STRAT_DIR") or os.path.join(HERE, "strategies")


def load(name=None):
    """Return the raw config dict for <name> (or env S7_STRAT), {} if none/invalid."""
    name = name if name is not None else os.environ.get("S7_STRAT", "")
    if not name:
        return {}
    try:
        with open(os.path.join(DIR, str(name) + ".json"), encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def names():
    try:
        return sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    except Exception:
        return []


def save(name, cfg):
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, str(name) + ".json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
