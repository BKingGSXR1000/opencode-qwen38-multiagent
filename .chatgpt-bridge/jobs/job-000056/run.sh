#!/usr/bin/env bash
set -Eeuo pipefail

Q=/home/bking/AI/qwen38-27b-rtx3090-syv
R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
KEY_FILE="$Q/api_key.txt"

if [[ -s "$KEY_FILE" ]]; then
  export VLLM_API_KEY="$(tr -d '\r\n' < "$KEY_FILE")"
else
  export VLLM_API_KEY="local-multiagent-test"
fi

echo "=== GPU BEFORE ==="
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw,memory.used,utilization.gpu --format=csv || true

if curl -fsS --max-time 2 -H "Authorization: Bearer $VLLM_API_KEY" http://127.0.0.1:18030/v1/models >/dev/null 2>&1; then
  echo "VLLM_ALREADY_READY"
else
  echo "=== START VLLM ==="
  mkdir -p "$R/logs"
  nohup env CUDA_VISIBLE_DEVICES=0 PORT=18030 MAX_SEQS=3 GPU_UTIL=0.929 CTX=fast \
    "$Q/single-user/start_qwen.sh" \
    >"$R/logs/a2-canary-vllm-18030.log" 2>&1 </dev/null &
  echo "VLLM_PID=$!"
fi

for i in $(seq 1 300); do
  if curl -fsS --max-time 2 -H "Authorization: Bearer $VLLM_API_KEY" http://127.0.0.1:18030/v1/models >/dev/null 2>&1; then
    echo "VLLM_READY after ${i}s"
    break
  fi
  if (( i % 20 == 0 )); then
    echo "--- wait ${i}s ---"
    tail -n 8 "$R/logs/a2-canary-vllm-18030.log" 2>/dev/null || true
  fi
  sleep 1
done

curl -fsS -H "Authorization: Bearer $VLLM_API_KEY" http://127.0.0.1:18030/v1/models
echo
echo "=== GPU AFTER ==="
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw,memory.used,utilization.gpu --format=csv || true
