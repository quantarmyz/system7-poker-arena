#!/usr/bin/env python
"""Run the HYBRID System 7 agent (deterministic engine + MiniMax M3 on hard spots)
through the kit's 429/409-hardened loop, with the agent-stats HUD injected.

    uv run run_hybrid_system7.py --max-hands 20    # bounded preview / measurement
    uv run run_hybrid_system7.py                   # full 500-hand Eval (slow: ~hours)

Needs OPENAI_API_KEY (MiniMax token plan) in .env. Identity = this dir's
.arena-credentials (a separate throwaway agent, distinct from system7).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)

import agent          # noqa: E402
import s7_reads       # noqa: E402
import llm_system7    # noqa: E402,F401  wires MiniMax M3

agent.retrieve_solver_context = s7_reads.retrieve_solver_context   # inject HUD

if __name__ == "__main__":
    os.environ.setdefault("OPENAI_BASE_URL", "https://api.minimax.io/v1")
    raise SystemExit(agent.main(["--agent", os.path.join(_HERE, "hybrid_system7.py")] + sys.argv[1:]))
