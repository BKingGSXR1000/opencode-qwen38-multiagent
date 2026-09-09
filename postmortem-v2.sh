#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${1:-$PWD}"
DB="$ROOT/xdg/data/opencode/opencode.db"
OUT="$ROOT/logs/postmortem-v2-$(date +%Y%m%d-%H%M%S).txt"

[[ -f "$DB" ]] || { echo "No V2 OpenCode DB yet: $DB"; exit 1; }

{
echo "=== V2 VERSION ==="
cat "$ROOT/V2_VERSION.txt" 2>/dev/null || true

echo
echo "=== PROJECT ==="
echo "$PROJECT"

echo
echo "=== SESSIONS ==="
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
  s.id,
  coalesce(s.parent_id,'') AS parent,
  coalesce(s.agent,'') AS agent,
  substr(coalesce(s.title,''),1,55) AS title,
  coalesce(s.idle_outcome,'') AS outcome,
  s.tokens_input AS tok_in,
  s.tokens_output AS tok_out,
  (SELECT count(*) FROM session_message m
    WHERE m.session_id=s.id AND m.type='compaction') AS compactions,
  (SELECT count(*) FROM session_message m
    WHERE m.session_id=s.id AND m.type='compaction'
      AND json_extract(m.data,'$.status')='failed') AS compact_failed,
  datetime(s.time_created/1000,'unixepoch','localtime') AS created,
  datetime(s.time_updated/1000,'unixepoch','localtime') AS updated
FROM session_v2 s
WHERE s.directory='${PROJECT//\'/\'\'}'
ORDER BY s.time_created;
SQL

echo
echo "=== TOTALS BY AGENT ==="
sqlite3 "$DB" <<SQL
.headers on
.mode column
SELECT
  coalesce(agent,'') AS agent,
  count(*) AS sessions,
  sum(CASE WHEN idle_outcome='succeeded' THEN 1 ELSE 0 END) AS succeeded,
  sum(CASE WHEN idle_outcome='failed' THEN 1 ELSE 0 END) AS failed,
  sum(tokens_input) AS tok_in,
  sum(tokens_output) AS tok_out
FROM session_v2
WHERE directory='${PROJECT//\'/\'\'}'
GROUP BY agent
ORDER BY tok_out DESC;
SQL

echo
echo "=== COMPACTION SUCCESS BY WORKER ==="
sqlite3 "$DB" <<SQL
.headers on
.mode column
WITH x AS (
  SELECT
    s.id, s.agent, s.idle_outcome,
    (SELECT count(*) FROM session_message m
      WHERE m.session_id=s.id AND m.type='compaction') AS c
  FROM session_v2 s
  WHERE s.directory='${PROJECT//\'/\'\'}'
    AND s.parent_id IS NOT NULL
)
SELECT c AS compactions, count(*) AS sessions,
       sum(CASE WHEN idle_outcome='succeeded' THEN 1 ELSE 0 END) AS succeeded,
       sum(CASE WHEN idle_outcome='failed' THEN 1 ELSE 0 END) AS failed
FROM x
GROUP BY c
ORDER BY c;
SQL

echo
echo "=== LARGEST REASONING MESSAGES ==="
sqlite3 "$DB" <<SQL
.headers on
.mode column
WITH a AS (
  SELECT
    s.agent,
    substr(s.title,1,42) AS title,
    m.session_id,
    m.seq,
    length(m.data) AS bytes,
    coalesce((
      SELECT sum(length(coalesce(json_extract(j.value,'$.text'),'')))
      FROM json_each(m.data,'$.content') j
      WHERE json_extract(j.value,'$.type')='reasoning'
    ),0) AS reasoning_chars,
    coalesce((
      SELECT count(*)
      FROM json_each(m.data,'$.content') j
      WHERE json_extract(j.value,'$.type')='tool'
    ),0) AS tool_parts
  FROM session_message m
  JOIN session_v2 s ON s.id=m.session_id
  WHERE s.directory='${PROJECT//\'/\'\'}'
    AND m.type='assistant'
)
SELECT *
FROM a
ORDER BY reasoning_chars DESC
LIMIT 30;
SQL

echo
echo "=== V2 RUN METRICS FILES ==="
ls -lh "$ROOT"/logs/run-metrics-*.csv 2>/dev/null || true

} | tee "$OUT"

echo
echo "Saved: $OUT"
