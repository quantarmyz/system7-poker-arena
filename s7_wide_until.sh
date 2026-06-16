#!/usr/bin/env bash
# Lanza clasificatorias de STRAT (oleadas de MAXC) hasta que la tabla `runs`
# tenga >= TARGET runs de la familia wide, luego se para solo. Uso:
#   s7_wide_until.sh [TARGET] [MAXC] [STRAT] [ENGINE] [TAG]
set -u
cd /opt/arena-system7-llm || exit 1
TARGET=${1:-50}; MAXC=${2:-4}; STRAT=${3:-wide}; ENGINE=${4:-hybrid}; TAG=${5:-wd}

done_count(){ python3 - <<'PYEOF'
import sqlite3
c=sqlite3.connect("file:s7_test.db?mode=ro",uri=True)
print(c.execute("select count(*) from runs where run_label='wide' or run_label like 'clasif-w%'").fetchone()[0])
PYEOF
}
active_count(){ systemctl list-units --type=service --all --plain --no-legend "arena-run-clasif-w*" 2>/dev/null | awk '$3=="active"{c++} END{print c+0}'; }

i=0
echo "[until] objetivo: $TARGET runs wide (STRAT=$STRAT ENGINE=$ENGINE TAG=$TAG MAXC=$MAXC)"
while true; do
  d=$(done_count 2>/dev/null || echo 0)
  echo "[until] wide completados: $d / $TARGET"
  if [ "${d:-0}" -ge "$TARGET" ]; then echo "[until] OBJETIVO ALCANZADO ($d)"; break; fi
  while [ "$(active_count)" -ge "$MAXC" ]; do sleep 25; done
  i=$((i+1)); L="clasif-${TAG}${i}-$RANDOM"
  systemctl reset-failed "arena-run-$L" 2>/dev/null
  systemd-run --unit="arena-run-$L" --working-directory=/opt/arena-system7-llm \
    --setenv=HOME=/opt/arena-system7-llm --setenv=PATH=/usr/local/bin:/usr/bin:/bin \
    --setenv=PYTHONUNBUFFERED=1 --setenv=S7_STATS_DB=/opt/arena-system7-llm/s7_test.db \
    --setenv=S7_RUN_LABEL="$L" --setenv=S7_STRAT="$STRAT" --setenv=S7_RANGES="$STRAT" \
    --setenv=S7_AGENT_NAME="S7-${TAG}${i}" --setenv=S7_SAVE_CREDS=1 \
    /usr/local/bin/uv run s7_test.py --engine "$ENGINE" --matches 1 \
    && echo "[until] lanzado $L" || echo "[until] FALLO $L"
  sleep 14
done
echo "[until] fin"
