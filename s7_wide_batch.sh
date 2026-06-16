#!/usr/bin/env bash
# Lanza TOTAL clasificatorias de una estrategia, en oleadas de MAXC concurrentes.
# Cada una = 500 manos Eval, reclamable (S7_SAVE_CREDS). Uso:
#   s7_wide_batch.sh [TOTAL] [MAXC] [STRAT] [ENGINE] [TAG]
set -u
cd /opt/arena-system7-llm || exit 1
TOTAL=${1:-20}; MAXC=${2:-4}; STRAT=${3:-wide}; ENGINE=${4:-hybrid}; TAG=${5:-wb}
echo "[batch] TOTAL=$TOTAL MAXC=$MAXC STRAT=$STRAT ENGINE=$ENGINE TAG=$TAG"
for i in $(seq 1 "$TOTAL"); do
  # espera a que haya un hueco (< MAXC unidades de este TAG activas)
  while [ "$(systemctl list-units --type=service --all --plain --no-legend "arena-run-clasif-${TAG}*" 2>/dev/null | awk '$3=="active"{c++} END{print c+0}')" -ge "$MAXC" ]; do
    sleep 20
  done
  L="clasif-${TAG}${i}-$RANDOM"
  systemctl reset-failed "arena-run-$L" 2>/dev/null
  systemd-run --unit="arena-run-$L" --working-directory=/opt/arena-system7-llm \
    --setenv=HOME=/opt/arena-system7-llm --setenv=PATH=/usr/local/bin:/usr/bin:/bin \
    --setenv=PYTHONUNBUFFERED=1 --setenv=S7_STATS_DB=/opt/arena-system7-llm/s7_test.db \
    --setenv=S7_RUN_LABEL="$L" --setenv=S7_STRAT="$STRAT" --setenv=S7_RANGES="$STRAT" \
    --setenv=S7_AGENT_NAME="S7-${TAG}${i}" --setenv=S7_SAVE_CREDS=1 \
    /usr/local/bin/uv run s7_test.py --engine "$ENGINE" --matches 1 \
    && echo "[batch] $i/$TOTAL lanzado: $L" || echo "[batch] $i/$TOTAL FALLO"
  sleep 10
done
echo "[batch] todos los $TOTAL lanzados"
