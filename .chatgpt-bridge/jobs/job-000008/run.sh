#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="$HOME/AI/chatgpt-wakeup.sh"
BACKUP="$TARGET.bak-$(date +%Y%m%d-%H%M%S)"

cp -a "$TARGET" "$BACKUP"

cat > "$TARGET" <<'WAKE_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

CHAT_PATH='/g/g-p-69da035a97188191b6a4d83a7679859b-ai-llms/c/6ab2c852-755c-83eb-8014-80306dcb28a0'

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 'message to send'"
    exit 2
fi

MESSAGE="$*"

mapfile -t WINDOWS < <(
    xdotool search --onlyvisible --class firefox 2>/dev/null | sort -u || true
)

if [[ ${#WINDOWS[@]} -eq 0 ]]; then
    echo "ERROR: no visible Firefox windows found"
    exit 1
fi

ORIGINAL_CLIP="$(xclip -selection clipboard -o 2>/dev/null || true)"
MATCHES=()

restore_clip() {
    printf '%s' "$ORIGINAL_CLIP" | xclip -selection clipboard 2>/dev/null || true
}
trap restore_clip EXIT

for WIN in "${WINDOWS[@]}"; do
    xdotool windowactivate --sync "$WIN" 2>/dev/null || continue
    sleep 0.20

    xdotool key --clearmodifiers ctrl+l
    sleep 0.10
    xdotool key --clearmodifiers ctrl+c
    sleep 0.12

    URL="$(xclip -selection clipboard -o 2>/dev/null || true)"
    xdotool key --clearmodifiers Escape
    sleep 0.05

    PATH_ONLY="$(
        python3 - "$URL" <<'PY'
import sys
from urllib.parse import urlsplit
try:
    print(urlsplit(sys.argv[1]).path)
except Exception:
    print("")
PY
    )"

    if [[ "$PATH_ONLY" == "$CHAT_PATH" ]]; then
        MATCHES+=("$WIN")
    fi
done

if [[ ${#MATCHES[@]} -ne 1 ]]; then
    echo "ERROR: expected exactly one Firefox window at target ChatGPT path, found ${#MATCHES[@]}"
    for WIN in "${WINDOWS[@]}"; do
        TITLE="$(xdotool getwindowname "$WIN" 2>/dev/null || true)"
        printf '  window=%s title=%q\n' "$WIN" "$TITLE"
    done
    exit 1
fi

WIN="${MATCHES[0]}"

printf '%s' "$MESSAGE" | xclip -selection clipboard

xdotool windowactivate --sync "$WIN"
sleep 0.35

eval "$(
    xdotool getwindowgeometry --shell "$WIN" \
      | grep -E '^(WIDTH|HEIGHT)='
)"

CLICK_X=$(( WIDTH * 55 / 100 ))
CLICK_Y=$(( HEIGHT - 90 ))

xdotool mousemove --window "$WIN" "$CLICK_X" "$CLICK_Y"
xdotool click 1
sleep 0.20

xdotool key --clearmodifiers ctrl+v
sleep 0.20
xdotool key --clearmodifiers Return

trap - EXIT
restore_clip

echo "SENT: $MESSAGE"
WAKE_EOF

chmod +x "$TARGET"
bash -n "$TARGET"

echo "PATCHED_WAKE_SCRIPT=$TARGET"
echo "BACKUP=$BACKUP"
echo "TARGET_PATH=/g/g-p-69da035a97188191b6a4d83a7679859b-ai-llms/c/6ab2c852-755c-83eb-8014-80306dcb28a0"
sed -n '1,260p' "$TARGET"
