#!/usr/bin/env python
"""Sube el bundle estático a la Heads-Up Ladder y polea hasta la activación PvP.
Lee las credenciales ÉL MISMO de S7_CREDS_FILE (apiKey del agente reclamado).
    docker compose exec -T -e S7_CREDS_FILE=/data/.arena-pg-credentials dashboard \
        uv run python /data/ladder-src/submit_ladder.py [--dry]
Registro JSONL en /data/ladder/submit-<ts>.jsonl. Éxito (exit 0) = pvp.status Active.
"""
import argparse
import json
import os
import sys
import time

import httpx

OUT = os.environ.get("S7_LADDER_DIR", "/data/ladder")


def log(*a):
    print("[ladder]", *a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=os.path.join(OUT, "bundle.zip"))
    ap.add_argument("--competition", default="", help="vacío → resolver por nombre en list-active")
    ap.add_argument("--base", default=os.environ.get("ARENA_API_BASE", "https://arena.dev.fun/api/arena"))
    ap.add_argument("--poll-timeout", type=int, default=1200)
    ap.add_argument("--dry", action="store_true", help="solo resolver competición y settings, sin subir")
    a = ap.parse_args()

    creds_path = os.environ.get("S7_CREDS_FILE", "/data/.arena-pg-credentials")
    try:
        key = json.load(open(creds_path)).get("apiKey")
    except Exception as e:
        log("no puedo leer creds:", e); sys.exit(2)
    if not key:
        log("sin apiKey en", creds_path); sys.exit(2)

    c = httpx.Client(base_url=a.base, headers={"x-arena-api-key": key}, timeout=90)
    rec_path = os.path.join(OUT, "submit-%d.jsonl" % int(time.time()))
    os.makedirs(OUT, exist_ok=True)

    def rec(kind, data):
        with open(rec_path, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "data": data}, ensure_ascii=False) + "\n")

    comp = a.competition
    if not comp:
        r = c.get("/competition/list-active"); r.raise_for_status()
        for it in r.json():
            if "heads-up ladder" in (it.get("name") or "").lower():
                comp = it["id"]
                log("competición:", it.get("name"), comp)
                break
    if not comp:
        log("no encuentro la heads-up ladder en list-active"); sys.exit(2)
    rec("competition", comp)

    try:
        s = c.get("/submissions/settings")
        log("settings:", s.status_code, s.text[:300])
        rec("settings", s.json() if s.status_code == 200 else s.text[:500])
    except Exception as e:
        log("settings error (sigo):", e)

    if a.dry:
        log("dry-run: no subo nada"); return

    with open(a.bundle, "rb") as fh:
        r = c.post("/submissions", data={"competitionId": comp, "template": "static-agent"},
                   files={"file": ("bundle.zip", fh, "application/zip")})
    log("submit:", r.status_code, r.text[:500])
    rec("submit", {"status": r.status_code, "body": r.text[:2000]})
    if r.status_code not in (200, 201, 202):
        sys.exit(3)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    sub = body.get("submission") or body
    sid = sub.get("id") or sub.get("submissionId")
    if not sid:
        log("sin submission id en la respuesta"); sys.exit(3)
    log("submission id:", sid)

    t0, delay = time.time(), 10
    last = ""
    while time.time() - t0 < a.poll_timeout:
        time.sleep(delay)
        delay = min(30, delay + 5)
        try:
            r = c.get(f"/submissions/{sid}")
            d = r.json() if r.status_code == 200 else {"error": r.status_code, "body": r.text[:300]}
        except Exception as e:
            d = {"error": str(e)}
        rec("poll", d)
        sub = d.get("submission") or d
        st = sub.get("status")
        pvp = sub.get("pvp") or {}
        line = f"status={st} val_hands={sub.get('completedHands')}/{sub.get('targetHands')} " \
               f"val_bb100={sub.get('rawBbPer100')} pvp={pvp.get('status')} pvp_err={pvp.get('error')} " \
               f"ts_mu={pvp.get('trueskillMu')} score={pvp.get('trueskillScore')}"
        if line != last:
            log(line); last = line
        if pvp.get("status") == "Active":
            log("ACTIVADO ✔ botId=", pvp.get("botId")); rec("active", pvp); return
        if pvp.get("status") in ("Failed", "Discarded"):
            log("PVP terminal:", pvp.get("status"), pvp.get("error")); sys.exit(4)
        if st in ("Failed", "Rejected", "Error"):
            log("submission terminal:", st, str(sub)[:400]); sys.exit(4)
    log("timeout de poll — sigue con: GET /submissions/%s" % sid)
    sys.exit(5)


if __name__ == "__main__":
    main()
