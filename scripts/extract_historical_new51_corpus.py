import json, glob, re, sys
from pathlib import Path

sys.path.insert(0, "/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831/scripts")
from deterministic_dispatch import select_actions

RUNS = ["New51p2","New51p3","New51r","New51s","New51t","New51u","New51v","New51w","New51y","New51z"]

def parse_read(content):
    state = content.get("state") or {}
    inp = state.get("input") or content.get("input") or {}
    path = str(inp.get("path") or "")
    if not path.endswith("/query/decision.json") and not path.endswith(".opencode-v2/query/decision.json"):
        return None
    blocks = state.get("content") or content.get("content") or []
    text = "\n".join(str(x.get("text") or "") for x in blocks if isinstance(x, dict))
    for line in text.splitlines():
        if line.startswith("1: "):
            try:
                return json.loads(line[3:])
            except Exception:
                pass
    return None

def observed_action(content):
    if content.get("type") == "text":
        text = str(content.get("text") or "").strip()
        if text == "WAIT":
            return {"kind": "wait"}
        m = re.fullmatch(r"IMPLEMENTATION_BLOCKED(?:\s+(D[0-9A-Z-]+))?", text)
        if m:
            return {"kind": "blocked", "deliverable": m.group(1) or ""}
        if text == "ACCEPTANCE_PASS":
            return {"kind": "complete", "result": "ACCEPTANCE_PASS"}
        return None

    tool = content.get("tool") or content.get("name")
    if tool != "subagent":
        return None
    state = content.get("state") or {}
    inp = state.get("input") or content.get("input") or {}
    agent = str(inp.get("agent") or "")
    prompt = str(inp.get("prompt") or "")
    description = str(inp.get("description") or "")
    out = {"kind": "launch", "agent": agent}
    m = re.search(r"(?:DELIVERABLE:|SPLIT_PARENT:)\s*(D[0-9A-Z-]+)", prompt)
    if m:
        out["deliverable"] = m.group(1)
    if agent == "implementation-planner":
        out["mode"] = "repair" if ("Repair" in description or "repair" in prompt[:160]) else "fresh"
    if agent == "reference-researcher":
        out["mode"] = "validation" if "VALIDATION" in prompt else "foundation"
    if agent == "acceptance-planner":
        out["mode"] = "fresh"
    return out

cases = []
for run in RUNS:
    dirs = sorted(glob.glob(f"/home/bking/AI/v2-run-analysis-gametest{run}-*/opencode-sessions"))
    if not dirs:
        continue
    root = Path(dirs[-1])
    sessions = json.load(open(root / "sessions.json"))
    for session in sessions:
        if session.get("agent") != "orchestrator":
            continue
        rows = json.load(open(root / f"{session['id']}.json"))
        pending = None
        actions = []
        ordinal = 0
        for row in rows:
            if row.get("type") != "assistant":
                continue
            try:
                data = json.loads(row.get("data") or "{}")
            except Exception:
                continue
            for content in data.get("content", []):
                decision = parse_read(content) if isinstance(content, dict) else None
                if decision is not None:
                    if pending is not None:
                        cases.append({
                            "run": run,
                            "session": session["id"],
                            "ordinal": ordinal,
                            "decision": pending,
                            "observed_original_actions": actions.copy(),
                        })
                        ordinal += 1
                    pending = decision
                    actions = []
                    continue
                action = observed_action(content) if isinstance(content, dict) else None
                if action is not None and pending is not None:
                    actions.append(action)
        if pending is not None:
            cases.append({
                "run": run,
                "session": session["id"],
                "ordinal": ordinal,
                "decision": pending,
                "observed_original_actions": actions.copy(),
            })

phases = {
    "implementation-plan", "execution", "recursive-split", "execution-blocked",
    "implementation-blocked", "final-tests", "acceptance-validation", "complete",
}
kept = []
for case in cases:
    decision = case["decision"]
    if decision.get("resume_phase") not in phases:
        continue
    if not case["observed_original_actions"]:
        continue
    case["expected_current_actions"] = select_actions(decision)
    kept.append(case)

keys = ("kind","agent","deliverable","mode","result")
def norm(actions):
    return [
        {k: a.get(k) for k in keys if a.get(k) not in (None, "")}
        for a in actions
    ]

diffs = []
for case in kept:
    if norm(case["observed_original_actions"]) != norm(case["expected_current_actions"]):
        diffs.append(case)

print("TOTAL", len(kept))
from collections import Counter
print("BYRUN", dict(Counter(c["run"] for c in kept)))
print("BYPHASE", dict(Counter(c["decision"].get("resume_phase") for c in kept)))
print("DIFF", len(diffs))
for case in diffs:
    d = case["decision"]
    print(
        case["run"], case["session"], case["ordinal"], d.get("resume_phase"),
        "OLD", norm(case["observed_original_actions"]),
        "CUR", norm(case["expected_current_actions"]),
        "eligible", d.get("eligible"),
        "active", (d.get("scheduler") or {}).get("active_deliverables"),
        "blockers", d.get("execution_blockers"),
        "split", d.get("split_required"),
    )

Path("/tmp/new51-historical-corpus.json").write_text(json.dumps({
    "protocol": "v2-historical-orchestrator-replay-v1",
    "source_runs": RUNS,
    "case_count": len(kept),
    "cases": kept,
}, indent=2, sort_keys=True) + "\n")
