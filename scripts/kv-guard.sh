#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${VLLM_METRICS_URL:-http://127.0.0.1:18030/metrics}"
STATE="$ROOT/logs/kv-state.txt"
CSV="$ROOT/logs/run-metrics-$(date +%Y%m%d-%H%M%S).csv"

mkdir -p "$ROOT/logs"
echo "timestamp,running,waiting,kv_pct,preemptions,output_tps,prompt_tps,status" > "$CSV"

metric() {
  local body="$1" regex="$2"
  awk -v re="$regex" '$0 ~ re && $1 !~ /^#/ {print $2; exit}' <<<"$body"
}

while true; do
  body="$(curl -fsS --max-time 1 "$URL" 2>/dev/null || true)"
  now="$(date '+%Y-%m-%dT%H:%M:%S%z')"

  if [[ -z "$body" ]]; then
    tmp="${STATE}.tmp"
    {
      echo "timestamp=$now"
      echo "status=STALE"
      echo "reason=vllm_metrics_unavailable"
    } > "$tmp"
    mv "$tmp" "$STATE"
    sleep 2
    continue
  fi

  run="$(metric "$body" '^vllm:num_requests_running\{' )"; run="${run:-0}"
  wait="$(metric "$body" '^vllm:num_requests_waiting\{' )"; wait="${wait:-0}"
  kv="$(metric "$body" '^vllm:kv_cache_usage_perc\{' )"; kv="${kv:-0}"
  pre="$(metric "$body" '^vllm:num_preemptions_total\{' )"; pre="${pre:-0}"

  # Depending on vLLM build, throughput metrics may use these names.
  out="$(metric "$body" '^vllm:avg_generation_throughput_toks_per_s' )"
  [[ -n "$out" ]] || out="$(metric "$body" '^vllm:generation_tokens_total' )"
  out="${out:-0}"

  prompt="$(metric "$body" '^vllm:avg_prompt_throughput_toks_per_s' )"
  prompt="${prompt:-0}"

  kvpct="$(awk -v k="$kv" 'BEGIN{printf "%.1f", k*100}')"
  status="$(awk -v k="$kv" -v w="$wait" -v r="$run" 'BEGIN{
      if (w+0 > 0 || k+0 >= 0.93) print "RED";
      else if (k+0 >= 0.85 || r+0 >= 2) print "YELLOW";
      else print "GREEN";
  }')"

  tmp="${STATE}.tmp"
  {
    echo "timestamp=$now"
    echo "status=$status"
    echo "running=$run"
    echo "waiting=$wait"
    echo "kv_pct=$kvpct"
    echo "preemptions=$pre"
    echo "policy=GREEN (<85% KV, no wait) requires parallel use when independent work exists; YELLOW/RED means no new second heavy worker"
  } > "$tmp"
  mv "$tmp" "$STATE"

  echo "$now,$run,$wait,$kvpct,$pre,$out,$prompt,$status" >> "$CSV"
  sleep 2
done
