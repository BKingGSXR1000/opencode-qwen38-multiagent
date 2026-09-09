#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$HOME/AI/opencode-qwen38-multiagent"
source "$ROOT/scripts/env.sh"

INTERVAL="${1:-1}"
STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT="$ROOT/logs/live-stats-${STAMP}.csv"

AUTH=()
[[ -n "${VLLM_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${VLLM_API_KEY}")

URL="http://127.0.0.1:${MA_PORT}/metrics"

metric() {
    local name="$1"
    awk -v n="$name" '
      $1 ~ ("^" n "(\\{|$)") {
          s += $2
          found=1
      }
      END {
          if (found) printf "%.10f", s
          else print "0"
      }'
}

echo "timestamp,running,waiting,output_tps,prompt_tps,kv_pct,mtp_accept_pct,gpu_util_pct,vram_used_mib,power_w,temp_c" > "$OUT"

PREV_GEN=""
PREV_PROMPT=""
PREV_ACC=""
PREV_DRAFT=""
PREV_TIME=""

echo
echo "Recording to:"
echo "  $OUT"
echo
echo "Ctrl+C stops the monitor; the CSV is kept."
echo
printf "%-9s %7s %7s %10s %10s %8s %8s %7s %8s %7s\n" \
       "TIME" "RUN" "WAIT" "OUT t/s" "IN t/s" "KV %" "MTP %" "GPU %" "VRAM" "WATT"

while true; do
    NOW="$(date +%s.%N)"
    TS="$(date '+%H:%M:%S')"

    METRICS="$(curl -fsS --max-time 2 "${AUTH[@]}" "$URL" 2>/dev/null || true)"

    if [[ -z "$METRICS" ]]; then
        echo "$TS  vLLM metrics unavailable"
        sleep "$INTERVAL"
        continue
    fi

    RUNNING="$(printf '%s\n' "$METRICS" | metric 'vllm:num_requests_running')"
    WAITING="$(printf '%s\n' "$METRICS" | metric 'vllm:num_requests_waiting')"
    KV="$(printf '%s\n' "$METRICS" | metric 'vllm:kv_cache_usage_perc')"

    GEN="$(printf '%s\n' "$METRICS" | metric 'vllm:generation_tokens_total')"
    PROMPT="$(printf '%s\n' "$METRICS" | metric 'vllm:prompt_tokens_total')"

    ACC="$(printf '%s\n' "$METRICS" | metric 'vllm:spec_decode_num_accepted_tokens_total')"
    DRAFT="$(printf '%s\n' "$METRICS" | metric 'vllm:spec_decode_num_draft_tokens_total')"

    OUT_TPS="0.0"
    IN_TPS="0.0"
    MTP="0.0"

    if [[ -n "$PREV_TIME" ]]; then
        read -r OUT_TPS IN_TPS MTP < <(
            awk \
              -v now="$NOW" -v pt="$PREV_TIME" \
              -v g="$GEN" -v pg="$PREV_GEN" \
              -v p="$PROMPT" -v pp="$PREV_PROMPT" \
              -v a="$ACC" -v pa="$PREV_ACC" \
              -v d="$DRAFT" -v pd="$PREV_DRAFT" '
            BEGIN {
                dt=now-pt
                if (dt <= 0) dt=1

                gt=(g-pg)/dt
                it=(p-pp)/dt

                dd=d-pd
                da=a-pa
                ar=(dd>0 ? 100*da/dd : 0)

                printf "%.1f %.1f %.1f\n", gt,it,ar
            }'
        )
    fi

    KV_PCT="$(awk -v x="$KV" 'BEGIN { printf "%.1f", x*100 }')"

    IFS=',' read -r GPUUTIL VRAM POWER TEMP < <(
        nvidia-smi -i "$MA_GPU_INDEX" \
          --query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu \
          --format=csv,noheader,nounits |
        awk -F',' '{
            for(i=1;i<=NF;i++) gsub(/^ +| +$/,"",$i)
            print $1","$2","$3","$4
        }'
    )

    printf "%-9s %7.0f %7.0f %10.1f %10.1f %8.1f %8.1f %7s %8s %7s\n" \
           "$TS" "$RUNNING" "$WAITING" "$OUT_TPS" "$IN_TPS" \
           "$KV_PCT" "$MTP" "$GPUUTIL" "$VRAM" "$POWER"

    echo "$(date --iso-8601=seconds),$RUNNING,$WAITING,$OUT_TPS,$IN_TPS,$KV_PCT,$MTP,$GPUUTIL,$VRAM,$POWER,$TEMP" \
        >> "$OUT"

    PREV_TIME="$NOW"
    PREV_GEN="$GEN"
    PREV_PROMPT="$PROMPT"
    PREV_ACC="$ACC"
    PREV_DRAFT="$DRAFT"

    sleep "$INTERVAL"
done
