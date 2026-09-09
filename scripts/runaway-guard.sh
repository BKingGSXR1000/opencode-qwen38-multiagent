#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$ROOT/xdg/data/opencode/opencode.db"
CSV="$ROOT/logs/runaway-guard-$(date +%Y%m%d-%H%M%S).csv"
EVENT="$ROOT/logs/runaway-events.log"
STATE="$ROOT/logs/runaway-state.tsv"
MODE="${RUNAWAY_GUARD_MODE:-abort}"
POLL="${RUNAWAY_GUARD_POLL:-2}"

mkdir -p "$ROOT/logs"
echo "timestamp,session,agent,seq,age_s,reasoning_chars,tool_parts,event,rescue_number,result" > "$CSV"
: > "$STATE"

WRAP="$ROOT/scripts/opencode2-auto.sh"
REAL_BIN=""
if [[ -f "$WRAP" ]]; then
  REAL_BIN="$(sed -nE 's#^exec "([^"]+)" --auto "\$@"$#\1#p' "$WRAP" | head -1)"
fi
[[ -n "$REAL_BIN" && -x "$REAL_BIN" ]] || REAL_BIN="$(command -v opencode2 2>/dev/null || command -v opencode 2>/dev/null || true)"

declare -A SOFTED
declare -A HARDED
declare -A ABORTS

abort_session(){
  local sid="$1"
  [[ -n "$REAL_BIN" && -x "$REAL_BIN" ]] || { echo "no-opencode-binary"; return 1; }
  local out rc
  out="$("$REAL_BIN" api POST "/session/$sid/abort" 2>&1)"; rc=$?
  printf '%s' "$out" | tr '\n' ' ' | cut -c1-160
  return "$rc"
}

while [[ ! -f "$DB" ]]; do sleep "$POLL"; done

while true; do
  rows="$(
    sqlite3 -separator '|' "$DB" <<'SQL' 2>/dev/null || true
.timeout 1000
WITH latest AS (
  SELECT m.session_id,max(m.seq) seq
  FROM session_message m
  JOIN session_v2 s ON s.id=m.session_id
  WHERE s.parent_id IS NOT NULL
    AND m.type='assistant'
    AND coalesce(s.idle_outcome,'')=''
  GROUP BY m.session_id
)
SELECT m.session_id,coalesce(s.agent,''),m.seq,
       CAST(strftime('%s','now')-(m.time_created/1000) AS INTEGER),
       coalesce((SELECT sum(length(coalesce(json_extract(j.value,'$.text'),'')))
                 FROM json_each(m.data,'$.content') j
                 WHERE json_extract(j.value,'$.type')='reasoning'),0),
       coalesce((SELECT count(*)
                 FROM json_each(m.data,'$.content') j
                 WHERE json_extract(j.value,'$.type')='tool'),0)
FROM latest l
JOIN session_message m ON m.session_id=l.session_id AND m.seq=l.seq
JOIN session_v2 s ON s.id=m.session_id;
SQL
  )"

  while IFS='|' read -r sid agent seq age chars tools; do
    [[ -n "$sid" ]] || continue
    age="${age:-0}"; chars="${chars:-0}"; tools="${tools:-0}"
    (( tools == 0 )) || continue

    key="$sid:$seq"

    # Soft threshold is common to all child roles.
    if [[ "${SOFTED[$key]:-0}" != 1 ]] && (( age >= 60 )); then
      SOFTED[$key]=1
      now="$(date -Is)"
      n="${ABORTS[$sid]:-0}"
      echo "$now,$sid,$agent,$seq,$age,$chars,$tools,SOFT_WARNING,$n,informational" >> "$CSV"
      echo "[$now] SOFT_WARNING session=$sid agent=$agent seq=$seq age=${age}s reasoning=$chars tools=0" >> "$EVENT"
    fi

    hard_age=120
    hard_chars=8000

    if [[ "${HARDED[$key]:-0}" != 1 ]] && (( age >= hard_age || chars >= hard_chars )); then
      HARDED[$key]=1
      ABORTS[$sid]=$(( ${ABORTS[$sid]:-0} + 1 ))
      n="${ABORTS[$sid]}"
      action="HARD_MONITOR"
      result="threshold-hit"

      if [[ "$MODE" == abort ]]; then
        action="HARD_ABORT"
        result="$(abort_session "$sid" || true)"
      fi

      now="$(date -Is)"
      echo "$sid"$'\t'"$agent"$'\t'"$n"$'\t'"$now"$'\t'"$age"$'\t'"$chars" >> "$STATE"
      echo "$now,$sid,$agent,$seq,$age,$chars,$tools,$action,$n,\"${result//\"/\"\"}\"" >> "$CSV"
      echo "[$now] $action session=$sid agent=$agent seq=$seq rescue_number=$n age=${age}s reasoning=$chars tools=0 result=$result" >> "$EVENT"
    fi
  done <<< "$rows"

  sleep "$POLL"
done
