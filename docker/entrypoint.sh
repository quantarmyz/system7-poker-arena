#!/usr/bin/env bash
# System 7 container entrypoint. Seeds the /data volume, then dispatches a role.
#   dashboard  -> the real-time web dashboard (default; launches runs as subprocesses)
#   bench      -> a continuous Eval test-bench worker (clasificatorias vs the panel)
#   pvp        -> the PvP Playground loop (run_pvp.py)
#   <other>    -> uv run <other> <args...>
set -euo pipefail

mkdir -p /data/strategies /data/.clasif /data/jobs /data/agents

# Seed bundled strategies into the volume on first run (std/wide/tag/lag/value/s7-opus).
if [ -z "$(ls -A /data/strategies 2>/dev/null)" ]; then
  cp -n /app/strategies/*.json /data/strategies/ 2>/dev/null || true
fi

cmd="${1:-dashboard}"; shift || true
case "$cmd" in
  dashboard)
    for _db in "${S7_DB_CASH:-/data/s7_cash.db}" "${S7_DB_TOURNEY:-/data/s7_tourney.db}"; do
      S7_STATS_DB="$_db" uv run python -c "import s7_stats; s7_stats.init()" 2>/dev/null || true
    done
    exec uv run s7_dash.py
    ;;
  bench)
    exec uv run s7_test.py --engine "${S7_ENGINE:-hybrid}" --matches "${S7_MATCHES:-50}"
    ;;
  pvp)
    exec uv run run_pvp.py
    ;;
  *)
    exec uv run "$cmd" "$@"
    ;;
esac
