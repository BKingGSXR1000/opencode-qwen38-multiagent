set -Eeuo pipefail

BASE=/home/bking/snap/firefox/common/.mozilla/firefox

echo "=== SNAP FIREFOX PROFILES ==="
find "$BASE "-maxdepth 2 -type f \( -name 'extensions.json' -o -name 'prefs.js' -o -name 'extension-preferences.json' \) -print 2>/dev/null sort || true

echo
echo "=== USERSCRIPT MANAGER EXTENSIONS ==="
python3 - "$BASE" <<'PY'
import json, sys
from pathlib import Path
base=Path(sys.argv[1])
for p in base.glob('*/extensions.json'):
    try:
        d=json.loads(p.read_text())
    except Exception:
        continue
    print("PROFILE", p.parent)
    for addon in d.get('addons',[]):
        name=str(addon.get('defaultLocale',{}).get('name') or addon.get('name') or '')
        aid=str(addon.get('id') or '')
        blob=(name+' '+aid).lower()
        if any(x in blob for x in ['tamper','violent','grease','userscript']):
            print(json.dumps({
                'id':aid,'name':name,'path':addon.get('path'),
                'active':addon.get('active'),'version':addon.get('version'),
                'type':addon.get('type')
            },ensure_ascii=False))
PY

echo
echo "=== FIND BRIDGE STRINGS IN SNAP PROFILE ==="
grep -R -l -m1 -E 'AI-Beast ChatGPT Job Bridge|BRIDGE_VERSION.*2\.10|ai-beast-local' \
  "$BASE" 2>/dev/null | head -80 || true

echo
echo "=== SERVED HEADER / INTERNAL VERSION ==="
curl -fsS http://127.0.0.1:8767/bridge.user.js | \
  grep -E '^// @version|BRIDGE_VERSION|WAKE_NEXT_ENDPOINT' | head -20
