#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/env.sh"
PIDFILE="$MA_ROOT/server.pid"
GPUPIDFILE="$MA_ROOT/gpu-logger.pid"
BASE="http://127.0.0.1:${MA_PORT}"
AUTH=(); [[ -n "${VLLM_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${VLLM_API_KEY}")
if curl -fsS --max-time 2 "${AUTH[@]}" "$BASE/v1/models" >/dev/null 2>&1; then echo "SYV server already ready."; exit 0; fi
if command -v ss >/dev/null && ss -ltn "sport = :$MA_PORT" 2>/dev/null | grep -q LISTEN; then echo "ERROR: port $MA_PORT is occupied" >&2; exit 1; fi

IFS=',' read -r TOTAL FREE USED < <(nvidia-smi -i "$MA_GPU_INDEX" --query-gpu=memory.total,memory.free,memory.used --format=csv,noheader,nounits | awk -F',' '{gsub(/ /,"",$1);gsub(/ /,"",$2);gsub(/ /,"",$3);print $1","$2","$3}')
GPU_UTIL="$(awk -v free="$FREE" -v total="$TOTAL" 'BEGIN{u=free/total-0.015;if(u>0.93)u=0.93;u=int(u*1000)/1000;printf "%.3f",u}')"
if awk -v u="$GPU_UTIL" 'BEGIN{exit !(u<0.88)}'; then echo "ERROR: too little free RTX3090 VRAM; free ${FREE}/${TOTAL} MiB" >&2; exit 1; fi

STAMP="$(date '+%Y%m%d-%H%M%S')"
SERVER_LOG="$MA_ROOT/logs/syv-mtp-${STAMP}.log"
GPU_LOG="$MA_ROOT/logs/gpu-${STAMP}.csv"
echo "$SERVER_LOG" > "$MA_ROOT/current-server-log.txt"
echo "$GPU_LOG" > "$MA_ROOT/current-gpu-log.txt"

echo "Starting Qwen3.8 MTP: CTX=fast, MAX_SEQS=8, PREFIX_CACHE=1, GPU_UTIL=$GPU_UTIL, port=$MA_PORT"
nvidia-smi -i "$MA_GPU_INDEX" --query-gpu=timestamp,index,name,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,power.limit,clocks.current.sm,clocks.current.memory,temperature.gpu --format=csv --loop-ms=500 > "$GPU_LOG" 2>&1 &
echo $! > "$GPUPIDFILE"

setsid env CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES="$MA_GPU_INDEX" PORT="$MA_PORT" CTX=fast SPEC=mtp DRAFT_TOKENS=4 MAX_SEQS=8 PREFIX_CACHE=1 GPU_UTIL="$GPU_UTIL" TOOLS=1 bash "$MA_SYV_REPO/single-user/start_qwen.sh" > "$SERVER_LOG" 2>&1 &
PID=$!; echo "$PID" > "$PIDFILE"
echo -n "Waiting for API"
for i in {1..600}; do
  if ! kill -0 "$PID" 2>/dev/null; then echo; tail -n 120 "$SERVER_LOG" || true; "$(dirname "$0")/stop-syv.sh" || true; exit 1; fi
  if curl -fsS --max-time 2 "${AUTH[@]}" "$BASE/v1/models" >/dev/null 2>&1 && curl -fsS --max-time 2 "${AUTH[@]}" "$BASE/metrics" >/dev/null 2>&1; then echo; echo "SYV MTP server ready."; exit 0; fi
  (( i % 10 == 0 )) && printf '.'
  sleep 1
done
echo; tail -n 120 "$SERVER_LOG" || true; "$(dirname "$0")/stop-syv.sh" || true; exit 1
