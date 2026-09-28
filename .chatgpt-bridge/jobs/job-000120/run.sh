set -Eeuo pipefail
WAKE=/home/bking/AI/chatgpt-wakeup.sh
LEGACY=/home/bking/AI/chatgpt-wakeup-xdotool.sh
QUEUE=/home/bking/AI/chatgpt-job-ingress/wake-queue

cp -a "$WAKE" "$LEGACY"

cat > "$WAKE" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

QUEUE=/home/bking/AI/chatgpt-job-ingress/wake-queue
LEGACY=/home/bking/AI/chatgpt-wakeup-xdotool.sh

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 'message to send'"
    exit 2
fi

MESSAGE="$*"
mkdir -p "$QUEUE"

WAKE_ID="$(
python3 - "$MESSAGE" <<'PY'
import hashlib, os, sys, time
msg=sys.argv[1]
stamp=str(time.time_ns())
suffix=hashlib.sha256(
    (stamp+"\0"+msg+"\0"+str(os.getpid())).encode()
).hexdigest()[:16]
print(f"{stamp}-{suffix}")
PY
)"

TARGET="$QUEUE/$WAKE_ID.json"
TMP="$QUEUE/.$WAKE_ID.tmp"

python3 - "$WAKE_ID" "$MESSAGE" "$TMP" <<'PY'
import json, sys
from pathlib import Path
wake_id,msg,tmp=sys.argv[1:]
Path(tmp).write_text(json.dumps({
    "id":wake_id,
    "message":msg,
}, ensure_ascii=False, sort_keys=True)+"\n")
PY
mv -f "$TMP" "$TARGET"

for _ in $(seq 1 12); do
    if [[ ! -e "$TARGET" ]]; then
        echo "QUEUED_AND_ACKED: $MESSAGE"
        exit 0
    fi
    sleep 1
done

echo "QUEUE_NOT_ACKED; using legacy xdotool fallback" >&2
rm -f "$TARGET"
exec "$LEGACY" "$MESSAGE"
EOF

chmod +x "$WAKE"

echo "=== NEW WAKE SCRIPT ==="
sed -n '1,110p' "$WAKE"

echo
echo "=== DIRECT TRANSITION TEST ==="
"$WAKE" "WAKE_V211_TRANSITION_TEST"

echo
echo "=== QUEUE AFTER TEST ==="
find "$QUEUE" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort || true
