#!/usr/bin/env python
"""System 7 — LLM realization (Level 5).

Drives an LLM at every action using the user's EducaPoker / GTO-node-locking
System Prompt (system7_prompt.md) + the live agent-stats HUD as research context.
Falls back to the deterministic decide_system7 engine on any failure / timeout /
deadline<3s / missing key. Reuses the kit's L5 scaffold (examples/llm_agent.py:
parse, validate, model-agnostic call) and the 429/409-hardened loop.

    uv run llm_system7.py --max-hands 50          # live; needs ANTHROPIC_API_KEY in .env
    uv run llm_system7.py --dry-run --mock-llm    # offline mock (free)
    S7_MODEL=claude-opus-4-8 uv run llm_system7.py ...   # stronger model

Identity comes from this dir's .arena-credentials (agent `system7`).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "examples"))
sys.path.insert(0, _HERE)

import agent          # noqa: E402
import llm_agent      # noqa: E402
import s7_reads       # noqa: E402
import decide_system7 # noqa: E402

# The Arena needs a strict machine action; this OVERRIDES the human "console
# report" format described in the user's prompt (that goes into `message`).
_OUTPUT_CONTRACT = """=== OUTPUT CONTRACT (overrides any output format above) ===
You receive a JSON table state (+ AUTO-RESEARCH CONTEXT with opponent HUD when
available: N, vpip, pfr, af, bluffPct, wtsd, wsd, playingStyle). Pick exactly ONE
legal action from allowedActions.availableActions. Output ONLY one JSON object on
the last line, nothing after it:
  {"action":"<name>","amount":<int?>,"message":"<=500 chars","reasoning":"<=150 chars YAML"}
- action MUST be in availableActions.
- For bet/raise/all-in, `amount` is the TOTAL chips committed on this street AFTER
  acting (a to-amount), within allowedActions.{betRange,raiseRange,allInToAmount}.
- For fold/check/call, OMIT amount.
- reasoning = YAML flow, <=150 chars: {vr:"<range>", ke:"<num+unit>", bf:[<feat>], pp:"<plan>", sr:"<size>"}
- Put your brief EV justification (the Directiva final) in `message`. Never reveal hole cards.
"""


def _load_prompt() -> str:
    try:
        with open(os.path.join(_HERE, "system7_prompt.md"), encoding="utf-8") as f:
            base = f.read().strip()
    except Exception:
        base = "Eres un agente de NLHE explotador de élite (metodología EducaPoker)."
    return base + "\n\n" + _OUTPUT_CONTRACT


# Wire System 7 into the kit's L5 scaffold.
# ── MiniMax M3 (OpenAI-compatible) — the user's token plan ─────────────────────
import re as _re  # noqa: E402
import time as _time  # noqa: E402

_orig_call_llm = llm_agent._call_llm
LAST_M3 = None   # last MiniMax M3 response {model,think,answer,ts}; read by hybrid_system7


def _minimax_call(system, user, max_tokens, model_hint, mock_mod=None):
    """Route the LLM call to MiniMax M3 (OpenAI-compatible chat/completions).
    Strips the <think>...</think> chain-of-thought MiniMax M2/M3 emit in the
    content. The --dry-run --mock-llm path is left untouched. Returns text or
    None (None => llm_decide falls back to the deterministic engine)."""
    global LAST_M3
    LAST_M3 = None
    if mock_mod is not None:
        return _orig_call_llm(system, user, max_tokens, model_hint, mock_mod)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = os.environ.get("OPENAI_BASE_URL") or "https://api.minimax.io/v1"
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=key, base_url=base, timeout=90)
        model = model_hint or os.environ.get("S7_MODEL", "MiniMax-M3")
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max(int(max_tokens or 0), int(os.environ.get("S7_MAX_TOKENS", "3000"))),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        txt = resp.choices[0].message.content or ""
        answer = _re.sub(r"<think>.*?</think>", "", txt, flags=_re.S).strip()
        tm = _re.search(r"<think>(.*?)</think>", txt, _re.S)
        LAST_M3 = {"model": model, "think": (tm.group(1).strip()[:2000] if tm else ""),
                   "answer": answer[:2000], "ts": _time.time()}
        return answer
    except Exception:
        return None


llm_agent._call_llm = _minimax_call                                # route to MiniMax M3
llm_agent.SYSTEM_PROMPT = _load_prompt()
agent.retrieve_solver_context = s7_reads.retrieve_solver_context   # inject HUD
llm_agent.heuristic_decide = decide_system7.decide                 # fallback = our engine


if __name__ == "__main__":
    os.environ.setdefault("OPENAI_BASE_URL", "https://api.minimax.io/v1")
    argv = sys.argv[1:]
    if not any(a == "--model" or a.startswith("--model=") for a in argv):
        argv = ["--model", os.environ.get("S7_MODEL", "MiniMax-M3")] + argv
    raise SystemExit(llm_agent.main(argv))
