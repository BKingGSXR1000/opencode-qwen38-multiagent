#!/usr/bin/env python3
import json
import re
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

    def test_planner_has_reasoning_watchdog_and_input_context_ceiling(self):
        self.assertEqual(
            supervisor.watchdog_limits("implementation-planner"),
            (300, 20000, 20000),
        )
        ceiling = supervisor.PLANNER_CONTEXT_INPUT_CEILING
        self.assertEqual(ceiling, 45000)
        self.assertEqual(
            supervisor.planner_context_reason(
                "implementation-planner", ceiling - 1
            ),
            "",
        )
        self.assertIn(
            "planner_context_input=45000",
            supervisor.planner_context_reason(
                "implementation-planner", ceiling
            ),
        )
        self.assertEqual(
            supervisor.planner_context_reason(
                "implementation-planner", ceiling, tool_running=True
            ),
            "",
        )
        self.assertEqual(
            supervisor.planner_context_reason("implementer", ceiling), ""
        )


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

    def test_todowrite_ui_mirroring_is_intentionally_disabled(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertNotRegex(root, r"(?m)^  todowrite: allow$")
        self.assertIn("TodoWrite UI mirroring is intentionally disabled", root)

    def test_resolved_permission_audit_requires_root_allow_and_worker_denies(self):
        editor_agents = {
            *agent_config_audit.WRITER_AGENTS,
            *agent_config_audit.PLANNER_EDIT_TARGETS,
        }
        payload = {"data": [
            *[
                {"name": name, "permissions": [
                    {"action": "edit", "resource": agent_config_audit.PLANNER_EDIT_TARGETS.get(name, "*"), "effect": "allow"},
                ]}
                for name in sorted(editor_agents)
            ],
        ]}
        self.assertEqual(agent_config_audit.audit_agents(payload), [])
        next(item for item in payload["data"] if item["name"] == "acceptance-planner")["permissions"][0]["resource"] = "*"
        self.assertTrue(agent_config_audit.audit_agents(payload))

    def test_planners_use_available_file_tools_for_contract_artifacts(self):
        expected = {
            "acceptance-planner.md": ".opencode-v2/ACCEPTANCE.md",
            "implementation-planner.md": ".opencode-v2/IMPLEMENTATION_PLAN.md",
        }
        for name, target in expected.items():
            text = (self.AGENTS / name).read_text()
            self.assertIn(f'    "{target}": allow', text, name)
            self.assertIn("`write` or `edit`", text, name)
            self.assertNotIn("`apply_patch`", text, name)

    def test_phase_ready_sentinels_are_guard_only(self):
        sentinels = ("ACCEPTANCE.ready", "IMPLEMENTATION_PLAN.ready")
        for path in self.AGENTS.glob("*.md"):
            for line in path.read_text().splitlines():
                if not any(sentinel in line for sentinel in sentinels):
                    continue
                if any(word in line.lower() for word in ("write", "create", "modify", "request")):
                    self.assertRegex(line, r"(?i)\b(?:never|do not)\b", f"{path.name}: {line}")
        guard = (Path(__file__).with_name("control-guard.py")).read_text()
        self.assertIn('ready = ctrl / "ACCEPTANCE.ready"', guard)
        self.assertIn('ready = ctrl / "IMPLEMENTATION_PLAN.ready"', guard)

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

    def test_control_protocol_is_project_local_for_planners_and_workers(self):
        names = (
            "implementation-planner.md", "probe-builder.md", "implementer.md",
            "core-builder.md", "feature-builder.md", "reasoning-builder.md",
            "integrator.md", "test-builder.md", "tester.md",
        )
        for name in names:
            text = (self.AGENTS / name).read_text()
            self.assertIn(".opencode-v2/CONTROL_CONTRACT.md", text, name)
            self.assertRegex(
                text, r"(?is)(?:never|do not).*?(?:read|inspect).*?harness", name
            )
            self.assertNotRegex(
                text,
                r"(?im)^(?!.*(?:never|do not)).*(?:read|inspect).*run-checks\.py",
                name,
            )
        planner = (self.AGENTS / "implementation-planner.md").read_text()
        self.assertIn("Do not add a\nDxxx probe merely to discover control protocol", planner)
        self.assertIn("Never guess or use a fallback\nmanifest schema", planner)

    def test_planner_progressively_externalizes_and_retries_by_reference(self):
        planner = (self.AGENTS / "implementation-planner.md").read_text()
        self.assertIn("Progressive externalization — mandatory", planner)
        self.assertIn("create `IMPLEMENTATION_PLAN.md` EARLY", planner)
        self.assertIn("Use bounded `edit` calls", planner)
        self.assertIn("150-300 lines preferred", planner)
        self.assertIn("400 physical lines is the hard protocol maximum", planner)
        self.assertIn("do not wait\nto write the complete file atomically", planner)

        root = (self.AGENTS / "orchestrator.md").read_text()
        retry_lines = (
            "`Continue implementation planning for this project.`",
            "`Read .opencode-v2/ACCEPTANCE.md.`",
            "`Read .opencode-v2/CONTROL_CONTRACT.md.`",
            "`Read .opencode-v2/IMPLEMENTATION_PLAN.md if present.`",
            "`Continue from durable file state using your progressive planner protocol.`",
        )
        positions = [root.index(line) for line in retry_lines]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("do not include the original request", root)
        self.assertIn("never request a shorter self-contained retry", root)


class ImplementationPlanSizeTests(unittest.TestCase):
    GUARD = Path(__file__).with_name("control-guard.py")

    def plan(self, lines):
        prefix = """# Plan
## Deliverables
### D001 — Tests
- Outcome: tests
- Owned artifacts: .opencode-v2/TEST_CHECKS.json
- Launch deps: (none)
- Contract deps: (none)
- Verify deps: (none)
- Acceptance IDs: A001
- Complexity: S
- Deep reasoning: no
- Role: tester
- Parallel-safe with: (none)
- Verify command: `python3 ~/AI/opencode-qwen38-multiagent-v2/scripts/run-checks.py --project .`
- Done when: report passes
## Execution Waves
- Wave 1: D001
""".splitlines()
        filler = ["<!-- compact plan padding -->"] * (lines - len(prefix) - 1)
        return "\n".join(prefix + filler + ["<!-- IMPLEMENTATION_PLAN_COMPLETE -->"]) + "\n"

    def validate(self, project):
        return subprocess.run(
            [sys.executable, str(self.GUARD), "--project", str(project), "--finalize-plan"],
            text=True,
            capture_output=True,
        )

    def test_400_lines_is_accepted_and_401_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"
            ctrl.mkdir()
            plan = ctrl / "IMPLEMENTATION_PLAN.md"
            plan.write_text(self.plan(400))
            accepted = self.validate(td)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertTrue((ctrl / "IMPLEMENTATION_PLAN.ready").exists())

            (ctrl / "IMPLEMENTATION_PLAN.ready").unlink()
            plan.write_text(self.plan(401))
            rejected = self.validate(td)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "hard maximum is 400",
                (ctrl / "IMPLEMENTATION_PLAN.guard-errors.txt").read_text(),
            )
            self.assertFalse((ctrl / "IMPLEMENTATION_PLAN.ready").exists())


class TestChecksControlContractTests(unittest.TestCase):
    RUNNER = Path(__file__).with_name("run-checks.py")

    def run_runner(self, project, *args):
        return subprocess.run(
            [sys.executable, str(self.RUNNER), "--project", str(project), *args],
            text=True,
            capture_output=True,
        )

    def test_bootstrap_exposes_exact_current_schema_and_full_control_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            bootstrapped = self.run_runner(project, "--bootstrap-control-contract")
            self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stderr)
            contract = (project / ".opencode-v2/CONTROL_CONTRACT.md").read_text()
            match = re.search(r"```json\n(.*?)\n```", contract, re.DOTALL)
            self.assertIsNotNone(match)
            exposed_schema = json.loads(match.group(1))
            printed = subprocess.run(
                [sys.executable, str(self.RUNNER), "--print-test-checks-schema"],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(exposed_schema, json.loads(printed.stdout))
            for required in (
                "ACCEPTANCE.ready", "IMPLEMENTATION_PLAN.ready", "Dxxx.ready",
                "attempts.json", "Dxxx.progress.md", "TEST_REPORT.json",
                "ACCEPTANCE_PASS",
            ):
                self.assertIn(required, contract)
            launch = (Path(__file__).parents[1] / "run.sh").read_text()
            self.assertIn("--bootstrap-control-contract", launch)

    def test_manifest_conforming_to_exposed_schema_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (ctrl / "TEST_CHECKS.json").write_text(json.dumps({
                "required_files": [".opencode-v2/CONTROL_CONTRACT.md"],
                "checks": [{
                    "name": "project-local-contract",
                    "command": "test -f .opencode-v2/CONTROL_CONTRACT.md",
                    "timeout_seconds": 5,
                }],
            }))
            result = self.run_runner(project)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((ctrl / "TEST_REPORT.json").read_text())
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["checks_run"], 1)

    def test_malformed_manifest_is_rejected_before_commands_run(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            ctrl = project / ".opencode-v2"
            ctrl.mkdir()
            (ctrl / "TEST_CHECKS.json").write_text(json.dumps({
                "checks": [{"name": "bad", "command": 7}],
            }))
            result = self.run_runner(project)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid TEST_CHECKS.json", result.stderr)
            self.assertFalse((ctrl / "TEST_REPORT.json").exists())


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
