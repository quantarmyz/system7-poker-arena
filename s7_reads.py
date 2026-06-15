"""System 7 — opponent HUD via /texas/agent-stats, wired as the Auto-Research hook.

`retrieve_solver_context(table)` returns the research context decide_system7 expects:
    {"hud": {<villainSeatNumber:int>: {N,vpip,pfr,af,bluffPct,wtsd,wsd,playingStyle}},
     "aggressor_seat": <int|None>}

Each villain's stats are fetched ONCE per match and cached (TTL), so the per-action
latency cost is paid only the first time a villain is seen. Read-only; never raises
(returns {} on any error → decide falls back to the GTO baseline). Disable with
S7_HUD=0. Plugged into the kit's loop via run_system7.py / llm_system7.py, which
monkeypatch `agent.retrieve_solver_context` to this — so decide() itself stays pure.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

_CACHE: dict[str, tuple[float, Optional[dict]]] = {}   # agentId -> (ts, stats|None)
_TTL = 1800.0
_CLIENT = None
_COMP: Optional[str] = None


def _client():
    global _CLIENT, _COMP
    if _CLIENT is None:
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, os.path.join(here, "examples"))
        from arena_client import ArenaClient, DEFAULT_BASE  # type: ignore
        key = os.environ.get("ARENA_API_KEY")
        try:
            key = json.load(open(os.path.join(here, ".arena-credentials"))).get("apiKey") or key
        except Exception:
            pass
        _CLIENT = ArenaClient(os.environ.get("ARENA_API_BASE", DEFAULT_BASE), api_key=key)
        _COMP = os.environ.get("ARENA_COMPETITION_ID")
    return _CLIENT


def _fetch(agent_id: str) -> Optional[dict]:
    now = time.time()
    hit = _CACHE.get(agent_id)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    stats = None
    try:
        comp = _COMP or os.environ.get("ARENA_COMPETITION_ID")
        raw = _client().get(
            f"/texas/agent-stats?competitionId={comp}&agentId={agent_id}")
        if isinstance(raw, dict) and raw.get("sampleSize") is not None:
            stats = {
                "N": raw.get("sampleSize"), "vpip": raw.get("vpip"),
                "pfr": raw.get("pfr"), "af": raw.get("af"),
                "bluffPct": raw.get("bluffPct"), "wtsd": raw.get("wtsd"),
                "wsd": raw.get("wsd"), "playingStyle": raw.get("playingStyle"),
            }
    except Exception:
        stats = None
    _CACHE[agent_id] = (now, stats)
    return stats


def retrieve_solver_context(table: dict) -> dict:
    """Auto-Research hook: build {hud, aggressor_seat} from agent-stats. {} when blind."""
    if os.environ.get("S7_HUD", "1") == "0":
        return {}
    seats = table.get("seats") or []
    me = table.get("selfSeatNumber")
    hud: dict = {}
    for s in seats:
        sn = s.get("seatNumber")
        aid = s.get("agentId") or s.get("agentHandle")
        if sn == me or not aid:
            continue
        if str(s.get("status") or "").lower() in ("folded", "out", "sittingout"):
            continue
        st = _fetch(str(aid))
        if st:
            hud[sn] = st
    agg = None
    for e in reversed(table.get("recentEvents") or []):
        summ = e.get("summary") or {}
        if summ.get("action") in ("bet", "raise", "all-in"):
            agg = summ.get("seatNumber")
            break
    return {"hud": hud, "aggressor_seat": agg} if hud else {}
