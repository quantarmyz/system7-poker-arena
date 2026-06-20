#!/usr/bin/env python
"""System 7 — multiLLM benchmark runner.

Samples random hands where M3 reasoning was used, rebuilds the situation, and asks
several LLMs the SAME play. Records per (model x hand x rep): action, validity,
latency, tokens, reasoning, and an optional LLM-judge score 0-10. Read it back in
the dashboard's multiLLM tab.

    uv run s7_mllm.py --run-id r1 --models "minimax:MiniMax-M3,openrouter:openai/gpt-4o" \
        --judge "openrouter:anthropic/claude-3.5-sonnet" --hands 5 --reps 3

Credentials come from .env (load_dotenv): OPENAI_API_KEY/OPENAI_BASE_URL (minimax),
OPENROUTER_API_KEY (+ optional OPENROUTER_BASE_URL), XIAOMI_BASE_URL/XIAOMI_API_KEY.
Provider/model are never hardcoded.
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

import s7_stats  # noqa: E402

# provider -> (base_url_env, api_key_env, default_base)
PROVIDERS = {
    "minimax":    ("OPENAI_BASE_URL",     "OPENAI_API_KEY",     "https://api.minimax.io/v1"),
    "openrouter": ("OPENROUTER_BASE_URL", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "xiaomi":     ("XIAOMI_BASE_URL",     "XIAOMI_API_KEY",     None),
    "deepseek":   ("DEEPSEEK_BASE_URL",   "DEEPSEEK_API_KEY",   "https://api.deepseek.com/v1"),
}


def provider_ready(p):
    cfg = PROVIDERS.get(p)
    if not cfg:
        return False
    return bool((os.environ.get(cfg[0]) or cfg[2]) and os.environ.get(cfg[1]))


def _resolve(p):
    cfg = PROVIDERS.get(p) or ("", "", None)
    return (os.environ.get(cfg[0]) or cfg[2], os.environ.get(cfg[1]))


def _split(model):
    return tuple(model.split(":", 1)) if ":" in model else ("openrouter", model)


def _extract_json(text):
    """Return the last balanced {...} object in text, or None."""
    end = (text or "").rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    return text[i:end + 1]
        end = text.rfind("}", 0, end)
    return None


def _parse(answer, avail):
    s = _extract_json(answer or "")
    if not s:
        return None
    try:
        o = json.loads(s)
    except Exception:
        return None
    act = str(o.get("action", "")).lower().strip().replace("_", "-")
    if act not in avail:
        return None
    amt = o.get("amount")
    try:
        amt = int(amt) if amt is not None else None
    except Exception:
        amt = None
    return {"action": act, "amount": amt,
            "reasoning": str(o.get("reasoning") or o.get("message") or "")[:500]}


def _chat(provider, model, system, user, max_tokens=6000):
    base, key = _resolve(provider)
    if not base or not key:
        return {"error": "proveedor sin credenciales: " + str(provider)}
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=key, base_url=base, timeout=280)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        dt = int((time.time() - t0) * 1000)
        txt = resp.choices[0].message.content or ""
        tm = re.search(r"<think>(.*?)</think>", txt, re.S)
        think = tm.group(1).strip()[:2000] if tm else ""
        answer = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
        u = getattr(resp, "usage", None)
        return {"answer": answer[:4000], "think": think, "latency_ms": dt,
                "prompt_tokens": getattr(u, "prompt_tokens", None) if u else None,
                "completion_tokens": getattr(u, "completion_tokens", None) if u else None}
    except Exception as e:
        return {"error": str(e)[:300]}


def _system_prompt():
    try:
        with open(os.path.join(HERE, "system7_prompt.md"), encoding="utf-8") as f:
            base = f.read().strip()
    except Exception:
        base = "Eres un agente de NLHE explotador de élite (EducaPoker / GTO node-locking)."
    return base + ("\n\n=== TAREA (benchmark) ===\nRecibes una SITUACIÓN de póker NLHE 6-max. "
                   "Elige UNA acción de availableActions y da un razonamiento EV breve. "
                   "Output: SOLO un objeto JSON en la última línea: "
                   '{"action":"<de availableActions>","amount":<int total a la calle si '
                   'bet/raise/all-in>,"reasoning":"<=200 chars"}. Nada después del JSON.')


def _situation(d):
    facing = (d.get("call_chips") or 0) > 0
    avail = ["fold", "call", "raise", "all-in"] if facing else ["check", "bet", "all-in"]
    reads = ("rival(es) arquetipo=%s" % d.get("archetype")) if d.get("archetype") else "sin reads de rival"
    user = (
        "SITUACIÓN (NLHE 6-max cash). Decide la mejor acción y razona en EV.\n"
        "- Calle: %s\n- Posición Hero: %s (%s)\n- Tus cartas: %s\n- Board: %s\n"
        "- Bote: %s · SPR: %s · fichas para igualar: %s\n"
        "- Fuerza: %s (clase %s) · textura: %s · outs ajustados: %s · pot-odds: %s\n"
        "- Rivales activos: %s · %s\navailableActions: %s\n"
    ) % (d.get("street"), d.get("pos"), ("IP" if d.get("ip") else "OOP"),
         d.get("hole"), (d.get("board") or "(preflop)"),
         d.get("pot"), d.get("spr"), d.get("call_chips"),
         d.get("strength"), d.get("hand_class"), d.get("texture"),
         d.get("adj_outs"), d.get("pot_odds"), d.get("n_villains"), reads, ", ".join(avail))
    return user, set(avail)


def _judge(judge, situation, action, reasoning):
    if not judge:
        return (None, "")
    jp, jm = _split(judge)
    sysj = ("Eres un coach de NLHE de élite y juez imparcial. Puntúa la CALIDAD del razonamiento de una "
            "decisión de póker (solidez del análisis EV / explotador y coherencia con la situación, NO si "
            'coincide con tu acción preferida). Output: SOLO un JSON {"score":<entero 0-10>,"note":"<=120 chars"}.')
    uj = "SITUACIÓN:\n%s\nDECISIÓN: action=%s\nRAZONAMIENTO: %s\n\nPuntúa 0-10 la calidad del razonamiento." % (
        situation, action, reasoning or "(vacío)")
    r = _chat(jp, jm, sysj, uj, max_tokens=1500)
    if r.get("error"):
        return (None, r["error"][:160])
    s = _extract_json(r.get("answer") or "")
    if not s:
        return (None, "")
    try:
        o = json.loads(s)
        return (float(o.get("score")), str(o.get("note") or "")[:200])
    except Exception:
        return (None, "")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--models", required=True, help="coma-separado provider:model")
    ap.add_argument("--judge", default="")
    ap.add_argument("--hands", type=int, default=10)
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args(argv)

    s7_stats.init()
    system = _system_prompt()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    hands_n = max(1, min(200, a.hands))
    reps = max(1, min(20, a.reps))

    cols = ["ts", "hand_key", "street", "pos", "ip", "hole", "hand_class", "board", "texture",
            "strength", "spr", "pot", "call_chips", "pot_odds", "adj_outs", "n_villains",
            "archetype", "action"]
    with s7_stats._conn() as c:
        rows = c.execute(
            "SELECT " + ",".join(cols) + " FROM decisions WHERE engine='M3' AND m3_log IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?", (hands_n,)).fetchall()
    hands = []
    for r in rows:
        d = dict(zip(cols, r))
        d["m3_action"] = d.pop("action")
        hands.append(d)

    s7_stats.mllm_start(a.run_id, models, a.judge, len(hands), reps)
    print("[mllm] run %s · %d hands · %d models · %d reps · judge=%s" % (
        a.run_id, len(hands), len(models), reps, a.judge or "—"), flush=True)
    for hi, h in enumerate(hands):
        user, avail = _situation(h)
        for model in models:
            prov, mid = _split(model)
            for rep in range(reps):
                r = _chat(prov, mid, system, user)
                valid, action, amount, reasoning = 0, None, None, ""
                if not r.get("error"):
                    p = _parse(r.get("answer"), avail)
                    if p:
                        valid, action, amount, reasoning = 1, p["action"], p["amount"], p["reasoning"]
                js, jn = (None, "")
                if a.judge and valid:
                    js, jn = _judge(a.judge, user, action, reasoning)
                s7_stats.log_mllm_result({
                    "run_id": a.run_id, "model": model, "provider": prov, "hand_key": h["hand_key"],
                    "dec_ts": h["ts"], "rep": rep, "action": action, "amount": amount, "valid": valid,
                    "latency_ms": r.get("latency_ms"), "prompt_tokens": r.get("prompt_tokens"),
                    "completion_tokens": r.get("completion_tokens"), "answer": r.get("answer", ""),
                    "reasoning": reasoning, "think": r.get("think", ""), "judge_score": js,
                    "judge_note": (jn or r.get("error", "")), "m3_action": h["m3_action"]})
                print("[mllm] h%d/%d %s rep%d -> %s valid=%d %sms%s" % (
                    hi + 1, len(hands), model, rep, action, valid, r.get("latency_ms") or "?",
                    (" judge=%s" % js if js is not None else "")), flush=True)
    s7_stats.mllm_finish(a.run_id)
    print("[mllm] DONE run=%s" % a.run_id, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
