#!/usr/bin/env python
"""System 7 — heuristic runner. Drives the deterministic decide_system7 engine
through the kit's (429/409-hardened) benchmark loop, with the agent-stats HUD
injected via the proper Auto-Research hook (so decide() stays pure / network-free).

    uv run run_system7.py --max-hands 50      # live Arena preview
    uv run run_system7.py --dry-run --max-hands 1
    S7_HUD=0 uv run run_system7.py ...         # disable opponent HUD

Identity comes from this dir's .arena-credentials (agent `system7`).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)

import agent          # noqa: E402  (kit loop + run_live_benchmark)
import s7_reads       # noqa: E402

# Inject the opponent HUD through the hook the loop already calls before decide().
agent.retrieve_solver_context = s7_reads.retrieve_solver_context

if __name__ == "__main__":
    decide_path = os.path.join(_HERE, "decide_system7.py")
    raise SystemExit(agent.main(["--agent", decide_path] + sys.argv[1:]))
