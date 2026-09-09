#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import control_state
import supervisor
import agent_config_audit


def message(parts=None, completed=None):
    info = {
        "id": "msg-1",
        "role": "assistant",
        "time": {"created": 1},
    }
    if completed is not None:
        info["time"]["completed"] = completed
    item = {"info": info}
    if parts is not None:
        item["parts"] = parts
    return item


class WatchdogShapeTests(unittest.TestCase):
    def setUp(self):
        supervisor.watch.clear()

    def test_actual_message_shape_counts_live_reasoning(self):
        shape = supervisor.message_shape(
            [message([{"type": "reasoning", "text": "x" * 8001}])],
            {"parentID": "root", "directory": "/tmp/project"},
        )
        self.assertTrue(shape["observable"])
        self.assertEqual(shape["reasoning"], 8001)
        self.assertFalse(shape["assistant_completed"])

    def test_missing_parts_waits_instead_of_aging(self):
        shape = supervisor.message_shape(
            [message()], {"parentID": "root", "directory": "/tmp/project"}
        )
        self.assertFalse(shape["observable"])
        age, _ = supervisor.watchdog_age("s", ("msg-1", ""), False, now=0)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", ""), False, now=200)
        self.assertEqual(age, 0)

    def test_tool_resets_then_later_reasoning_gets_new_window(self):
        age, _ = supervisor.watchdog_age("s", ("msg-1", ""), True, now=10)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", "tool-1"), False, now=90)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", "tool-1"), True, now=100)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", "tool-1"), True, now=110)
        self.assertEqual(age, 10)

    def test_completed_assistant_is_never_watchable(self):
        shape = supervisor.message_shape(
            [message([{"type": "reasoning", "text": "x" * 9000}], completed=2)],
            {"parentID": "root", "directory": "/tmp/project"},
        )
        self.assertTrue(shape["assistant_completed"])


class AttemptLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = supervisor.ROOT
        self.old_project = supervisor.PROJECT
        supervisor.ROOT = Path(self.tmp.name)
        supervisor.PROJECT = self.tmp.name
        supervisor.session_task.clear()

    def tearDown(self):
        supervisor.ROOT = self.old_root
        supervisor.PROJECT = self.old_project
        self.tmp.cleanup()

    def test_live_and_persisted_claims_cannot_create_attempt_four(self):
        self.assertEqual(supervisor.claim_attempt("live-1", "D005"), ("claimed", 1))
        self.assertEqual(supervisor.claim_attempt("db-2", "D005"), ("claimed", 2))
        self.assertEqual(supervisor.claim_attempt("live-3", "D005"), ("claimed", 3))
        self.assertEqual(supervisor.claim_attempt("db-4", "D005"), ("limit", 3))
        self.assertEqual(supervisor.claim_attempt("live-3", "D005"), ("existing", 3))
        ledger = json.loads(
            (Path(self.tmp.name) / ".opencode-v2/work/attempts.json").read_text()
        )
        self.assertEqual(ledger["owner"], "supervisor")
        self.assertEqual(ledger["deliverables"]["D005"]["count"], 3)
        self.assertEqual(len(ledger["deliverables"]["D005"]["sessions"]), 3)

    def test_existing_session_with_count_four_is_invalid_and_not_repaired(self):
        ledger_path = Path(self.tmp.name) / ".opencode-v2/work/attempts.json"
        ledger_path.parent.mkdir(parents=True)
        original = {
            "owner": "supervisor",
            "deliverables": {"D005": {"count": 4, "sessions": ["old-session"]}},
        }
        ledger_path.write_text(json.dumps(original))
        self.assertEqual(supervisor.claim_attempt("old-session", "D005"), ("invalid", 4))
        self.assertNotIn("old-session", supervisor.session_task)
        self.assertEqual(json.loads(ledger_path.read_text()), original)

    def test_existing_count_four_is_blocked_by_dispatch(self):
        ledger_path = Path(self.tmp.name) / ".opencode-v2/work/attempts.json"
        ledger_path.parent.mkdir(parents=True)
        ledger_path.write_text(json.dumps({
            "owner": "supervisor",
            "deliverables": {"D005": {"count": 4, "sessions": ["old-session"]}},
        }))
        old_plan_ready = supervisor.plan_ready
        old_load_manifest = supervisor.load_manifest
        old_ready_info = supervisor.ready_info
        old_abort = supervisor.abort_session
        aborts = []
        try:
            supervisor.plan_ready = lambda: True
            supervisor.load_manifest = lambda: {"leaves": {"D005": {"launch_deps": []}}}
            supervisor.ready_info = lambda did: {}
            supervisor.abort_session = lambda sid, reason, agent="": aborts.append((sid, reason, agent))
            supervisor.dispatch_seen.clear()
            supervisor.enforce_assignment("old-session", "implementer", "DELIVERABLE: D005")
        finally:
            supervisor.plan_ready = old_plan_ready
            supervisor.load_manifest = old_load_manifest
            supervisor.ready_info = old_ready_info
            supervisor.abort_session = old_abort
            supervisor.dispatch_seen.clear()
        self.assertEqual(len(aborts), 1)
        self.assertIn("attempt_ledger_invalid", aborts[0][1])


class DispatchPromptProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = supervisor.ROOT
        self.old_project = supervisor.PROJECT
        self.old_abort = supervisor.abort_session
        supervisor.ROOT = Path(self.tmp.name)
        supervisor.PROJECT = self.tmp.name
        supervisor.dispatch_seen.clear()
        self.aborts = []
        supervisor.abort_session = lambda sid, reason, agent="": self.aborts.append((sid, reason, agent))

    def tearDown(self):
        supervisor.ROOT = self.old_root
        supervisor.PROJECT = self.old_project
        supervisor.abort_session = self.old_abort
        supervisor.dispatch_seen.clear()
        self.tmp.cleanup()

    def test_short_exact_prompt_is_valid(self):
        prompt = (
            "DELIVERABLE: D005\n"
            "Read your D005 section in .opencode-v2/IMPLEMENTATION_PLAN.md.\n"
            "Read .opencode-v2/work/D005.progress.md if present.\n"
            "Continue from current project state and execute the deliverable."
        )
        self.assertEqual(supervisor.implementation_prompt_violation(prompt), "")
        self.assertEqual(supervisor.parse_deliverable(prompt), "D005")

    def test_oversized_prompt_is_rejected_before_dispatch(self):
        prompt = "DELIVERABLE: D005\n" + ("x" * supervisor.MAX_IMPLEMENTATION_PROMPT_CHARS)
        supervisor.enforce_assignment("oversized", "implementer", prompt)
        self.assertEqual(len(self.aborts), 1)
        self.assertIn("oversized_first_user_prompt", self.aborts[0][1])
        self.assertIn("oversized", supervisor.dispatch_seen)


class AgentConfigurationAndPromptAuditTests(unittest.TestCase):
    AGENTS = Path(__file__).parents[1] / "xdg/config/opencode/agents"

    def test_root_todowrite_is_allowed_and_subagents_are_explicitly_denied(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertRegex(root, r"(?m)^  todowrite: allow$")
        for path in self.AGENTS.glob("*.md"):
            if path.name == "orchestrator.md":
                continue
            self.assertRegex(path.read_text(), r"(?m)^  todowrite: deny$", path.name)

    def test_resolved_permission_audit_requires_root_allow_and_worker_denies(self):
        payload = {"data": [
            {"name": "orchestrator", "permissions": [{"action": "todowrite", "effect": "allow"}]},
            *[
                {"name": name, "permissions": [{"action": "todowrite", "effect": "deny"}]}
                for name in agent_config_audit.IMPLEMENTATION_AGENTS
            ],
        ]}
        self.assertEqual(agent_config_audit.audit_agents(payload), [])
        payload["data"][1]["permissions"][0]["effect"] = "allow"
        self.assertTrue(agent_config_audit.audit_agents(payload))

    def test_success_output_protocol_is_unconditional_and_bare(self):
        required = (
            "On success, your entire final response\n"
            "MUST be the exact bare text ACCEPTANCE_PASS, with no Markdown, emoji, heading,"
        )
        for name in ("orchestrator.md", "acceptance-validator.md"):
            text = (self.AGENTS / name).read_text()
            self.assertIn(required, text, name)
            self.assertNotIn("When success is required", text, name)
            self.assertNotIn("**ACCEPTANCE_PASS**", text, name)


class StatusTests(unittest.TestCase):
    def test_snapshot_is_derived_from_authoritative_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".opencode-v2"
            (root / "work").mkdir(parents=True)
            (root / "ACCEPTANCE.ready").write_text("status=complete\nartifact=ACCEPTANCE.md\nmarker=ACCEPTANCE_COMPLETE\nvalidated=deterministic-test\n")
            (root / "IMPLEMENTATION_PLAN.ready").write_text("status=complete\nartifact=IMPLEMENTATION_PLAN.md\nmarker=IMPLEMENTATION_PLAN_COMPLETE\nvalidated=deterministic-test\n")
            (root / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves": {"D001": {"launch_deps": []}, "D002": {"launch_deps": ["D001"]}}}))
            (root / "work/D001.ready").write_text("status=complete\ndeliverable=D001\nverified=true\n")
            (root / "work/attempts.json").write_text(json.dumps({"deliverables": {"D002": {"count": 2}}}))
            (root / "TEST_REPORT.json").write_text(json.dumps({"status": "pass", "checks_run": 2}))
            state = control_state.snapshot(td)
            self.assertTrue(state["acceptance"]["complete"])
            self.assertTrue(state["leaves"]["D001"]["complete"])
            self.assertTrue(state["leaves"]["D002"]["eligible"])
            self.assertTrue(state["tests"]["complete"])


class LessonsApiTests(unittest.TestCase):
    def test_lessons_uses_current_session_create_then_prompt_routes(self):
        class Fake(supervisor.OpenCodeHTTP):
            def __init__(self):
                self.calls = []
            def ensure(self):
                return True
            def request(self, method, path, payload=None, timeout=3):
                self.calls.append((method, path, payload, timeout))
                return {"id": "lessons-1"} if path == "/api/session" else {}

        fake = Fake()
        old_project = supervisor.PROJECT
        supervisor.PROJECT = "/tmp/project"
        try:
            self.assertEqual(fake.start_lessons_session("retrospective"), (True, "lessons-1"))
        finally:
            supervisor.PROJECT = old_project
        self.assertEqual(fake.calls[0][0:2], ("POST", "/api/session"))
        self.assertEqual(fake.calls[0][2]["agent"], "lessons-learner")
        self.assertEqual(fake.calls[1][0:2], ("POST", "/api/session/lessons-1/prompt"))
        self.assertEqual(fake.calls[1][2], {"text": "retrospective", "delivery": "steer"})


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_nonzero_required_executable_can_never_finalize_as_pass(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"
            ctrl.mkdir()
            (ctrl / "ACCEPTANCE.md").write_text(
                "# Acceptance Contract\n- [ ] A001: command-backed requirement\n"
            )
            report = {
                "result": "PASS",
                "checks": [{
                    "id": "A001", "status": "PASS", "evidence": "validator prose",
                    "required_executable": True, "command": "false", "exit_code": 1,
                }],
            }
            (ctrl / "acceptance-report.json").write_text(json.dumps(report))
            finalizer = Path(__file__).with_name("finalize-acceptance.py")
            failed = subprocess.run([sys.executable, str(finalizer), td], text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("A001=executable-exit-1", failed.stderr)
            self.assertFalse((ctrl / "acceptance-pass.json").exists())
            report["checks"][0]["exit_code"] = 0
            (ctrl / "acceptance-report.json").write_text(json.dumps(report))
            passed = subprocess.run([sys.executable, str(finalizer), td], text=True, capture_output=True)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertTrue((ctrl / "acceptance-pass.json").exists())


if __name__ == "__main__":
    unittest.main()
