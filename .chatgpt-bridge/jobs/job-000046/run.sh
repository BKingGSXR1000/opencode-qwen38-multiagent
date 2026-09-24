#!/usr/bin/env bash
set -euo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO"

echo "=== PRE ==="
date -Is
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short

python3 - <<'PY'
from pathlib import Path

p=Path("scripts/supervisor.py")
s=p.read_text()
start=s.index("def dispatch_splitter_corrective_turn(root,prompt):")
end=s.index("\n\ndef begin_splitter_corrective_turn", start)

new='''def quiesce_transport_root_for_corrective(
    root, settle_seconds=0.05, poll_seconds=0.05, timeout_seconds=2.0
):
    """Stop OpenCode's automatic parent resume before posting the corrective SubtaskPart.

    A completed TaskTool immediately resumes its parent assistant. The
    transport-root has no semantic work to do, and a corrective prompt posted
    while that automatic resume is active is persisted but not executed.
    Sample twice to cover the small completion/resume race; if the root is
    active, interrupt only that current turn and wait until the session leaves
    /session/status. Do not use abort_session(): this is transport quiescing,
    not retirement of the durable technical root.
    """
    try:
        status=http.get_status()
        if root not in status:
            if settle_seconds>0:
                time.sleep(settle_seconds)
            status=http.get_status()
        if root not in status:
            return True,"root-idle"

        if not http.interrupt(root):
            return False,"root-interrupt-failed"

        deadline=time.monotonic()+max(0.0,float(timeout_seconds))
        while True:
            if poll_seconds>0:
                time.sleep(poll_seconds)
            status=http.get_status()
            if root not in status:
                return True,"root-interrupted"
            if time.monotonic()>=deadline:
                return False,"root-interrupt-timeout"
    except Exception as exc:
        return False,f"root-quiesce-error:{exc!r}"


def dispatch_splitter_corrective_turn(root,prompt):
    if not http.ensure(): return False,"http-not-connected"
    if http.mode!="v1": return False,"splitter-corrective-requires-v1-runtime"

    ok,detail=quiesce_transport_root_for_corrective(root)
    if not ok:
        return False,detail

    try:
        query=urllib.parse.urlencode({"directory":PROJECT})
        http.request("POST",f"/session/{urllib.parse.quote(root)}/prompt_async?{query}",
            payload={"agent":"transport-root","model":{"providerID":"v2noop","modelID":"root-noop"},
                     "parts":[{"type":"subtask","prompt":prompt,"description":"Correct bounded split", "agent":"task-splitter"}]},timeout=12)
        return True,f"accepted-after-{detail}"
    except Exception as exc:
        return False,repr(exc)
'''
p.write_text(s[:start]+new+s[end:])

t=Path("scripts/test_transport_root_no_continuation.py")
x=t.read_text()
marker='class CorrectiveRootQuiesceTests(unittest.TestCase):'
if marker not in x:
    tests='''

import unittest
import supervisor


class CorrectiveRootQuiesceTests(unittest.TestCase):
    def setUp(self):
        self.old_project=supervisor.PROJECT
        self.old_get_status=supervisor.http.get_status
        self.old_interrupt=supervisor.http.interrupt
        supervisor.PROJECT="/tmp/test-project"

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.http.get_status=self.old_get_status
        supervisor.http.interrupt=self.old_interrupt

    def test_busy_root_is_interrupted_then_observed_idle(self):
        statuses=[
            {"ses_root":{"type":"busy"}},
            {},
        ]
        interrupts=[]
        supervisor.http.get_status=lambda: statuses.pop(0)
        supervisor.http.interrupt=lambda sid: interrupts.append(sid) or True
        ok,detail=supervisor.quiesce_transport_root_for_corrective(
            "ses_root",settle_seconds=0,poll_seconds=0,timeout_seconds=0.1
        )
        self.assertTrue(ok)
        self.assertEqual(detail,"root-interrupted")
        self.assertEqual(interrupts,["ses_root"])

    def test_idle_root_uses_guard_sample_without_interrupt(self):
        statuses=[{},{}]
        interrupts=[]
        supervisor.http.get_status=lambda: statuses.pop(0)
        supervisor.http.interrupt=lambda sid: interrupts.append(sid) or True
        ok,detail=supervisor.quiesce_transport_root_for_corrective(
            "ses_root",settle_seconds=0,poll_seconds=0,timeout_seconds=0.1
        )
        self.assertTrue(ok)
        self.assertEqual(detail,"root-idle")
        self.assertEqual(interrupts,[])

    def test_interrupt_failure_blocks_corrective_post(self):
        old_ensure=supervisor.http.ensure
        old_mode=supervisor.http.mode
        old_request=supervisor.http.request
        requests=[]
        try:
            supervisor.http.ensure=lambda: True
            supervisor.http.mode="v1"
            supervisor.http.get_status=lambda: {"ses_root":{"type":"busy"}}
            supervisor.http.interrupt=lambda _sid: False
            supervisor.http.request=lambda *a,**k: requests.append((a,k))
            ok,detail=supervisor.dispatch_splitter_corrective_turn(
                "ses_root","correct me"
            )
            self.assertFalse(ok)
            self.assertEqual(detail,"root-interrupt-failed")
            self.assertEqual(requests,[])
        finally:
            supervisor.http.ensure=old_ensure
            supervisor.http.mode=old_mode
            supervisor.http.request=old_request
'''
    main=x.find('\nif __name__ == "__main__":')
    if main < 0:
        x=x.rstrip()+"\n"+tests
    else:
        x=x[:main]+"\n"+tests+x[main:]
    t.write_text(x)
PY

echo "=== DIFF ==="
git diff -- scripts/supervisor.py scripts/test_transport_root_no_continuation.py

echo "=== TESTS ==="
python3 -m py_compile scripts/supervisor.py scripts/test_transport_root_no_continuation.py
python3 scripts/test_transport_root_no_continuation.py
python3 -m unittest scripts.test_state_machine_invariants.AbortIntentTests

echo "=== COMMIT ==="
git add scripts/supervisor.py scripts/test_transport_root_no_continuation.py
git diff --cached --check
git commit -m "Quiesce transport root before splitter correction"
git push origin HEAD:a2-v11831-integration

echo "=== POST ==="
git rev-parse HEAD
git status --short
