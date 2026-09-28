set -Eeuo pipefail

WAKE=/home/bking/AI/chatgpt-wakeup.sh
cp "$WAKE" "$WAKE.bak-$(date +%Y%m%d-%H%M%S)-multiwindow"

python3 - "$WAKE" <<'PY'
from pathlib import Path
import sys

p=Path(sys.argv[1])
s=p.read_text()
old=r'''if [[ ${#MATCHES[@]} -ne 1 ]]; then
    echo "ERROR: expected exactly one Firefox window at target ChatGPT path, found ${#MATCHES[@]}"
    for WIN in "${WINDOWS[@]}"; do
        TITLE="$(xdotool getwindowname "$WIN" 2>/dev/null || true)"
        printf '  window=%s title=%q\n' "$WIN" "$TITLE"
    done
    exit 1
fi

WIN="${MATCHES[0]}"'''
new=r'''# xdotool can expose both the real Firefox browser window and a generic
# auxiliary "Firefox" window with the same active-tab URL. Prefer the normal
# browser window without depending on the changing ChatGPT conversation title.
if [[ ${#MATCHES[@]} -gt 1 ]]; then
    REAL_MATCHES=()
    for WIN in "${MATCHES[@]}"; do
        TITLE="$(xdotool getwindowname "$WIN" 2>/dev/null || true)"
        if [[ "$TITLE" == *"— Mozilla Firefox" ]]; then
            REAL_MATCHES+=("$WIN")
        fi
    done
    if [[ ${#REAL_MATCHES[@]} -eq 1 ]]; then
        MATCHES=("${REAL_MATCHES[@]}")
    fi
fi

if [[ ${#MATCHES[@]} -ne 1 ]]; then
    echo "ERROR: expected exactly one Firefox window at target ChatGPT path, found ${#MATCHES[@]}"
    for WIN in "${WINDOWS[@]}"; do
        TITLE="$(xdotool getwindowname "$WIN" 2>/dev/null || true)"
        printf '  window=%s title=%q\n' "$WIN" "$TITLE"
    done
    exit 1
fi

WIN="${MATCHES[0]}"'''

if old not in s:
    raise SystemExit("PATCH_ANCHOR_NOT_FOUND")
p.write_text(s.replace(old,new,1))
PY

chmod +x "$WAKE"
bash -n "$WAKE"

echo "=== PATCHED MATCH LOGIC ==="
grep -n -A35 -B4 'xdotool can expose' "$WAKE"

echo
echo "=== MARK OLD WAKE BACKLOG HANDLED ==="
for j in job-000102 job-000103 job-000104 job-000105 job-000106 job-000107; do
    d="/home/bking/AI/github-chatgpt-runner/spool/$j"
    if [[ -d "$d" ]]; then
        printf 'superseded-after-wake-repair %s\n' "$(date -Is)" > "$d/WOKEN"
        echo "$j WOKEN"
    fi
done

echo
echo "=== DIRECT WAKE SCRIPT SELF-TEST ==="
"$WAKE" "WAKE_REPAIR_DIRECT_TEST" || {
    rc=$?
    echo "DIRECT_WAKE_FAILED rc=$rc"
    exit "$rc"
}
