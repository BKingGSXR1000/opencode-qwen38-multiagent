#!/usr/bin/env python3
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

project = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
root = project / ".opencode-v2"
plan_p = root / "ACCEPTANCE.md"
report_p = root / "acceptance-report.json"
pass_p = root / "acceptance-pass.json"
evidence_p = root / "browser-evidence.json"

# Never allow a stale pass marker to survive a new finalization attempt.
pass_p.unlink(missing_ok=True)

def fail(msg):
    print(f"ACCEPTANCE_GATE_FAIL: {msg}", file=sys.stderr)
    raise SystemExit(2)

if not plan_p.exists():
    fail("missing .opencode-v2/ACCEPTANCE.md")
if not report_p.exists():
    fail("missing .opencode-v2/acceptance-report.json")

plan = plan_p.read_text(errors="replace")
must = re.findall(r"(?m)^- \[ \] (A\d{3}):\s+.+$", plan)
if not must:
    fail("acceptance contract contains no machine-readable MUST Axxx checks")
if len(must) != len(set(must)):
    fail("duplicate MUST IDs in acceptance contract")

try:
    report = json.loads(report_p.read_text())
except Exception as e:
    fail(f"invalid acceptance-report.json: {e}")

checks = report.get("checks")
if not isinstance(checks, list):
    fail("report.checks must be an array")

by_id = {}
for c in checks:
    if not isinstance(c, dict):
        fail("every report check must be an object")
    cid = c.get("id")
    if cid in by_id:
        fail(f"duplicate report check {cid}")
    by_id[cid] = c

if set(by_id) != set(must):
    missing = sorted(set(must) - set(by_id))
    extra = sorted(set(by_id) - set(must))
    fail(f"report IDs do not exactly match MUST IDs; missing={missing} extra={extra}")

bad = []
for cid in must:
    c = by_id[cid]
    status = c.get("status")
    evidence = str(c.get("evidence") or "").strip()
    if status != "PASS":
        bad.append(f"{cid}={status}")
    if len(evidence) < 8:
        bad.append(f"{cid}=insufficient-evidence")
    # A validator may use non-executable evidence for a requirement, but once a
    # required executable check is declared it is mechanically binding. This
    # prevents a non-zero result from being explained away in prose.
    executable = c.get("required_executable") is True or "command" in c or "exit_code" in c
    if executable:
        command = c.get("command")
        exit_code = c.get("exit_code")
        if not isinstance(command, str) or not command.strip():
            bad.append(f"{cid}=missing-executable-command")
        if not isinstance(exit_code, int):
            bad.append(f"{cid}=missing-executable-exit-code")
        elif exit_code != 0:
            bad.append(f"{cid}=executable-exit-{exit_code}")

if report.get("result") != "PASS":
    bad.append(f"report.result={report.get('result')!r}")

browserish = bool(re.search(
    r"\b(browser|web|render|visual|canvas|ui|page|three\.?js|webgl|animation|button|control)\b",
    plan,
    re.I
))

browser_summary = None
if browserish:
    if not evidence_p.exists():
        bad.append("missing-browser-evidence")
    else:
        try:
            ev = json.loads(evidence_p.read_text())
            browser_summary = {
                "http_status": ev.get("http_status"),
                "console_errors": len(ev.get("console_errors") or []),
                "page_errors": len(ev.get("page_errors") or []),
                "failed_requests": len(ev.get("failed_requests") or []),
                "screenshot_unique_colors": ((ev.get("screenshot_stats") or {}).get("quantized_unique_colors")),
                "canvases": len(((ev.get("page") or {}).get("canvases") or [])),
            }
            status = ev.get("http_status")
            if not isinstance(status, int) or status >= 400:
                bad.append(f"browser-http={status}")
            if ev.get("page_errors"):
                bad.append("browser-page-errors")
            if ev.get("console_errors"):
                bad.append("browser-console-errors")

            colors = (ev.get("screenshot_stats") or {}).get("quantized_unique_colors")
            if not isinstance(colors, int) or colors < 8:
                bad.append(f"effectively-blank-page colors={colors}")

            # If visible canvases exist, at least one must have non-trivial visual
            # variation. This catches "server works but Three.js scene is blank".
            visible_canvases = [
                c for c in ((ev.get("page") or {}).get("canvases") or [])
                if c.get("visible") and (c.get("clientWidth") or 0) >= 100 and (c.get("clientHeight") or 0) >= 100
            ]
            if visible_canvases:
                stats = [
                    x.get("stats") or {}
                    for x in (ev.get("canvas_evidence") or [])
                    if isinstance(x, dict) and "stats" in x
                ]
                if not stats:
                    bad.append("visible-canvas-without-pixel-evidence")
                elif max((s.get("quantized_unique_colors") or 0) for s in stats) < 8:
                    bad.append("effectively-blank-canvas")
        except Exception as e:
            bad.append(f"invalid-browser-evidence:{e}")

if bad:
    fail("; ".join(bad))

marker = {
    "result": "PASS",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "must_checks": must,
    "must_count": len(must),
    "acceptance_sha256": hashlib.sha256(plan_p.read_bytes()).hexdigest(),
    "report_sha256": hashlib.sha256(report_p.read_bytes()).hexdigest(),
    "browser_summary": browser_summary,
}
pass_p.write_text(json.dumps(marker, indent=2) + "\n")
print(f"ACCEPTANCE_GATE_PASS: {len(must)} MUST checks passed")
