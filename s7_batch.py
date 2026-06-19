#!/usr/bin/env python
"""System 7 — launch TOTAL clasificatorias of a strategy in waves of MAXC.

Backend-agnostic replacement for s7_wide_batch.sh: uses s7_jobs, so it works the
same under systemd (LXC) and as plain subprocesses (Docker). Launched by the
dashboard's /api/run/batch as a tracked job.

    s7_batch.py TOTAL MAXC STRAT ENGINE TAG
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s7_jobs  # noqa: E402


def _active(tag):
    return sum(1 for j in s7_jobs.list_jobs()
               if j["label"].startswith("clasif-" + tag) and j["state"] == "active")


def main(argv):
    total = max(1, min(300, int(argv[0])))
    maxc = max(1, min(8, int(argv[1])))
    strat, engine, tag = argv[2], argv[3], argv[4]
    db = os.environ.get("S7_STATS_DB", "")
    print("[batch] TOTAL=%d MAXC=%d STRAT=%s ENGINE=%s TAG=%s" % (total, maxc, strat, engine, tag), flush=True)
    for i in range(1, total + 1):
        waited = 0
        while _active(tag) >= maxc:
            time.sleep(20)
            waited += 20
            if waited > 6000:        # ~100 min: a slot frees within S7_MATCH_TIMEOUT; never wedge forever
                print("[batch] gate wait exceeded; proceeding", flush=True)
                break
        label = "clasif-%s%d-%d" % (tag, i, random.randint(1000, 32767))
        env = {"S7_RUN_LABEL": label, "S7_STRAT": strat, "S7_RANGES": strat,
               "S7_AGENT_NAME": "S7-%s%d" % (tag, i), "S7_SAVE_CREDS": "1"}
        if db:
            env["S7_STATS_DB"] = db
        try:
            s7_jobs.launch(label, s7_jobs.pyrun("s7_test.py", "--engine", engine, "--matches", "1"), env)
            print("[batch] %d/%d lanzado: %s" % (i, total, label), flush=True)
        except Exception as e:
            print("[batch] %d/%d FALLO: %s" % (i, total, e), flush=True)
        time.sleep(10)
    print("[batch] todos los %d lanzados" % total, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
