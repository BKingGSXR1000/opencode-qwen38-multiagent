#!/usr/bin/env python3
"""Isolated regression tests for the Stage-A stabilization boundary."""
from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import control_state
import control_query_views
import deterministic_dispatch
import stage_a_controller as controller
import stage_a_path_permissions as path_permissions
import supervisor


OWNED = ".opencode-v2/ACCEPTANCE.md"
RULES = [("*", "deny"), (OWNED, "allow")]


class LauncherReadinessTimeoutTests(unittest.TestCase):
    def test_stage_a_launcher_bounds_status_probe(self):
        text = Path(__file__).with_name("start-stage-a-run.sh").read_text()
        self.assertIn("--connect-timeout 1 --max-time 2", text)
        self.assertIn('http_status(){', text)


class PlannerContractTests(unittest.TestCase):
    def test_verify_command_is_explicitly_single_line(self):
        role = (
            Path(__file__).resolve().parent.parent
            / "xdg/config/opencode/agents/implementation-planner.md"
        ).read_text()
        self.assertIn("exactly one physical line", role)
        self.assertIn("no embedded\n  newline", role)
        self.assertIn("Never embed a multiline Python/JavaScript/shell program", role)
        self.assertIn("python3 -m py_compile", role)
        self.assertIn("bounded owned test/helper", role)


class FinalTestLeafContextTests(unittest.TestCase):
    def test_final_test_packet_contains_shared_schema_and_verify_dependency_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            ctrl = project / ".opencode-v2"
            ctrl.mkdir()
            (ctrl / "ACCEPTANCE.md").write_text(
                "Reference policy: internal\n\n"
                "- [ ] A020: final unittest command passes.\n"
            )
            manifest = {
                "leaves": {
                    "D007": {
                        "owned_artifact_paths": [".opencode-v2/probes/stdlib_check.py"],
                        "verify_deps": [],
                        "acceptance_ids": ["A019"],
                    },
                    "D008": {
                        "owned_artifact_paths": [".opencode-v2/TEST_CHECKS.json"],
                        "verify_deps": ["D007"],
                        "acceptance_ids": ["A020"],
                        "verify_command": ".opencode-v2/bin/run-checks",
                    },
                }
            }
            contexts = control_query_views.build_leaf_contexts(project, manifest)
            final = contexts["D008"]
            self.assertEqual(
                final["test_checks_contract"]["schema"],
                control_query_views.TEST_CHECKS_SCHEMA,
            )
            self.assertEqual(
                final["test_checks_contract"]["runner"],
                ".opencode-v2/bin/run-checks",
            )
            self.assertEqual(
                final["verify_dependency_artifacts"]["D007"],
                [".opencode-v2/probes/stdlib_check.py"],
            )
            self.assertNotIn("test_checks_contract", contexts["D007"])


class DirectOwnedWritePromptTests(unittest.TestCase):
    def test_write_capable_roles_match_runtime_direct_write_gate(self):
        agents = (
            "implementer",
            "core-builder",
            "feature-builder",
            "reasoning-builder",
            "integrator",
            "test-builder",
        )
        root = Path(__file__).resolve().parent.parent / "xdg/config/opencode/agents"
        for agent in agents:
            with self.subTest(agent=agent):
                role = (root / f"{agent}.md").read_text()
                self.assertIn("Runtime direct-owned-write gate", role)
                self.assertIn(
                    "the next\ntool call MUST directly create or update a declared owned artifact",
                    role,
                )
                self.assertIn("A no-op rewrite of identical\ncontent does not satisfy this gate", role)


class WorkerSandboxPythonSideEffectTests(unittest.TestCase):
    def test_worker_and_verify_disable_python_bytecode_side_effects(self):
        source = (
            Path(__file__).resolve().parent / "worker_sandbox.py"
        ).read_text()
        self.assertGreaterEqual(
            source.count('"--setenv","PYTHONDONTWRITEBYTECODE","1"'),
            2,
        )


class ImplementationRetryGenerationTests(unittest.TestCase):
    def test_preclaim_does_not_advance_generation_but_terminal_failure_does(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            work = project / ".opencode-v2/work"
            work.mkdir(parents=True)
            ledger = {
                "owner": "supervisor",
                "protocol": "v2-attempt-ledger-v1",
                "deliverables": {
                    "D001": {
                        "count": 1,
                        "sessions": ["ses-active"],
                        "automatic_limit": 2,
                    }
                },
            }
            (work / "attempts.json").write_text(json.dumps(ledger))
            self.assertEqual(controller.attempt_failure_generation(project, "D001"), 0)

            ledger["deliverables"]["D001"]["failure_history"] = [
                {
                    "attempt": 1,
                    "classification": "genuine",
                    "reason": "verify-failed-1",
                }
            ]
            (work / "attempts.json").write_text(json.dumps(ledger))
            self.assertEqual(controller.attempt_failure_generation(project, "D001"), 1)

    def test_retry_generation_changes_execution_id_without_changing_decision(self):
        action = {"kind": "launch", "agent": "implementer", "deliverable": "D001"}
        first = controller.execution_action_id("same-state", "ses-root", action, 0)
        replay = controller.execution_action_id("same-state", "ses-root", action, 0)
        retry = controller.execution_action_id("same-state", "ses-root", action, 1)
        self.assertEqual(first, replay)
        self.assertNotEqual(first, retry)


class ExactProjectPathPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.sibling = self.base / "project-sibling"
        self.other = self.base / "other"
        for item in (self.project, self.sibling, self.other):
            item.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_relative_owned_path_allows(self):
        relative = path_permissions.normalize_tool_path(self.project, OWNED)
        self.assertEqual(relative, OWNED)
        self.assertTrue(path_permissions.permission_allows(RULES, relative))

    def test_absolute_equivalent_under_exact_project_allows(self):
        relative = path_permissions.normalize_tool_path(self.project, str(self.project / OWNED))
        self.assertEqual(relative, OWNED)
        self.assertTrue(path_permissions.permission_allows(RULES, relative))

    def test_absolute_unowned_path_under_project_denies(self):
        relative = path_permissions.normalize_tool_path(self.project, str(self.project / ".opencode-v2/OTHER.md"))
        self.assertFalse(path_permissions.permission_allows(RULES, relative))

    def test_parent_escape_denies(self):
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, "../other/owned.txt")

    def test_other_project_denies(self):
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, str(self.other / OWNED))

    def test_similarly_prefixed_sibling_denies(self):
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, str(self.sibling / OWNED))

    def test_symlink_escape_denies(self):
        (self.project / "escape").symlink_to(self.other, target_is_directory=True)
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, "escape/owned.txt")

    def test_non_git_projection_is_worktree_relative_and_not_absolute(self):
        self.assertEqual(path_permissions.opencode_worktree(self.project), Path("/"))
        projected = path_permissions.effective_permission_pattern(self.project, Path("/"), OWNED)
        self.assertEqual(projected, str(self.project / OWNED).lstrip("/"))
        self.assertFalse(projected.startswith("/"))

    def test_git_and_nested_git_projection(self):
        git_root = self.base / "git-root"
        git_root.mkdir()
        subprocess.run(["git", "init", "-q", str(git_root)], check=True)
        self.assertEqual(path_permissions.opencode_worktree(git_root), git_root.resolve())
        self.assertEqual(
            path_permissions.effective_permission_pattern(git_root, git_root, OWNED), OWNED,
        )
        nested = git_root / "nested-project"
        nested.mkdir()
        self.assertEqual(path_permissions.opencode_worktree(nested), git_root.resolve())
        self.assertEqual(
            path_permissions.effective_permission_pattern(nested, git_root, OWNED),
            f"nested-project/{OWNED}",
        )

    def test_projection_does_not_broaden_owned_pattern(self):
        agent = '---\npermission:\n  edit:\n    "*": deny\n    ".opencode-v2/ACCEPTANCE.md": allow\n---\n'
        rendered = path_permissions.render_agent_with_projection(agent, self.project, Path("/"))
        self.assertIn(str(self.project / OWNED).lstrip("/"), rendered)
        self.assertNotIn(str(self.project / OWNED), rendered)
        self.assertNotIn(str(self.sibling / OWNED).lstrip("/"), rendered)


class TerminalSemanticChildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.root = "ses-root"
        self.action = {"kind": "launch", "agent": "acceptance-planner", "mode": "fresh"}
        self.result = {"state_version": "state-a", "actions": [self.action]}
        self.execution_id = controller.execution_action_id("state-a", self.root, self.action)
        controller.save_execution_ledger(self.project, {
            "owner": "stage-a-controller",
            "protocol": controller.EXECUTION_LEDGER_PROTOCOL,
            "executions": {
                self.execution_id: {
                    "execution_id": self.execution_id,
                    "state_version": "state-a",
                    "root_session": self.root,
                    "action": self.action,
                    "created_at_ms": int((time.time() - 30) * 1000),
                    "baseline_child_ids": [],
                }
            },
        })
        self.saved = {
            "evaluate": controller.evaluate,
            "resolve_root_session": controller.resolve_root_session,
            "child_snapshot": controller.child_snapshot,
            "session_is_active": controller.session_is_active,
        }
        controller.evaluate = lambda project: self.result
        controller.resolve_root_session = lambda project, base_url, explicit: self.root
        controller.child_snapshot = lambda project, base_url, root: [
            {"id": "ses-terminal", "parentID": self.root, "agent": "acceptance-planner"}
        ]

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(controller, name, value)
        self.temp.cleanup()

    def test_active_semantic_child_is_reconciled_without_second_dispatch(self):
        controller.session_is_active = lambda project, base_url, sid: True
        receipt = controller.execute_first_semantic(self.project, "http://127.0.0.1:1", self.result, self.root)
        self.assertTrue(receipt["replay_suppressed"])
        self.assertEqual(receipt["reconciliation"]["sessions"], ["ses-terminal"])

    def test_terminal_semantic_child_fails_closed_without_post(self):
        controller.session_is_active = lambda project, base_url, sid: False
        with self.assertRaisesRegex(controller.ControllerError, "SEMANTIC_CHILD_TERMINATED_WITHOUT_STATE_TRANSITION"):
            controller.execute_first_semantic(self.project, "http://127.0.0.1:1", self.result, self.root)

    def test_terminal_child_within_grace_period_remains_reconcilable(self):
        ledger = controller.load_execution_ledger(self.project)
        ledger["executions"][self.execution_id]["created_at_ms"] = int(time.time() * 1000)
        controller.save_execution_ledger(self.project, ledger)
        controller.session_is_active = lambda project, base_url, sid: False
        receipt = controller.execute_first_semantic(self.project, "http://127.0.0.1:1", self.result, self.root)
        self.assertTrue(receipt["replay_suppressed"])


class SemanticInfrastructureRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.root = "ses-root"
        self.action = {
            "kind": "launch",
            "agent": "acceptance-validator",
            "mode": "final",
        }
        self.result = {"state_version": "state-a", "actions": [self.action]}
        self.execution_id = controller.execution_action_id(
            "state-a", self.root, self.action
        )
        controller.save_execution_ledger(self.project, {
            "owner": "stage-a-controller",
            "protocol": controller.EXECUTION_LEDGER_PROTOCOL,
            "executions": {
                self.execution_id: {
                    "execution_id": self.execution_id,
                    "state_version": "state-a",
                    "root_session": self.root,
                    "action": self.action,
                    "semantic_generation": 0,
                    "created_at_ms": int((time.time() - 30) * 1000),
                    "baseline_child_ids": [],
                }
            },
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_terminal_semantic_child_can_receive_one_auditable_infrastructure_retry(self):
        child = {
            "id": "ses-validator-old",
            "parentID": self.root,
            "agent": "acceptance-validator",
        }
        with mock.patch.object(controller, "evaluate", return_value=self.result), \
             mock.patch.object(controller, "child_snapshot", return_value=[child]), \
             mock.patch.object(controller, "semantic_child_may_still_transition", return_value=False):
            receipt = controller.authorize_semantic_infrastructure_retry(
                self.project,
                "http://127.0.0.1:1",
                self.execution_id,
                "validator scratch was read-only",
            )
        self.assertEqual(receipt["next_generation"], 1)
        self.assertFalse(receipt["idempotent"])
        ledger = controller.load_execution_ledger(self.project)
        grant = ledger["executions"][self.execution_id]["semantic_infrastructure_retry"]
        self.assertEqual(grant["source"], "operator-controller")
        self.assertEqual(grant["child_sessions"], ["ses-validator-old"])
        generation, next_id = controller.semantic_execution_slot(
            ledger, "state-a", self.root, self.action
        )
        self.assertEqual(generation, 1)
        self.assertEqual(next_id, receipt["next_execution_id"])
        self.assertNotEqual(next_id, self.execution_id)

    def test_active_semantic_child_cannot_receive_infrastructure_retry(self):
        child = {
            "id": "ses-validator-live",
            "parentID": self.root,
            "agent": "acceptance-validator",
        }
        with mock.patch.object(controller, "evaluate", return_value=self.result), \
             mock.patch.object(controller, "child_snapshot", return_value=[child]), \
             mock.patch.object(controller, "semantic_child_may_still_transition", return_value=True):
            with self.assertRaisesRegex(
                controller.ControllerError, "while child may still transition"
            ):
                controller.authorize_semantic_infrastructure_retry(
                    self.project,
                    "http://127.0.0.1:1",
                    self.execution_id,
                    "should not be accepted",
                )

    def test_semantic_infrastructure_retry_remains_bounded_after_generation_three(self):
        execution_id = controller.execution_action_id(
            "state-a", self.root, self.action, 3
        )
        controller.save_execution_ledger(self.project, {
            "owner": "stage-a-controller",
            "protocol": controller.EXECUTION_LEDGER_PROTOCOL,
            "executions": {
                execution_id: {
                    "execution_id": execution_id,
                    "state_version": "state-a",
                    "root_session": self.root,
                    "action": self.action,
                    "semantic_generation": 3,
                    "created_at_ms": int((time.time() - 30) * 1000),
                    "baseline_child_ids": [],
                }
            },
        })
        child = {
            "id": "ses-validator-generation-three",
            "parentID": self.root,
            "agent": "acceptance-validator",
        }
        with mock.patch.object(controller, "evaluate", return_value=self.result), \
             mock.patch.object(controller, "child_snapshot", return_value=[child]), \
             mock.patch.object(controller, "semantic_child_may_still_transition", return_value=False):
            with self.assertRaisesRegex(
                controller.ControllerError, "retry limit reached"
            ):
                controller.authorize_semantic_infrastructure_retry(
                    self.project,
                    "http://127.0.0.1:1",
                    execution_id,
                    "generation four remains forbidden",
                )

    def test_terminal_acceptance_can_finalize_fresh_current_report(self):
        ctrl = self.project / ".opencode-v2"
        ctrl.mkdir(parents=True, exist_ok=True)
        report = ctrl / "acceptance-report.json"
        report.write_text('{"protocol":"v2-acceptance-report-v1","result":"PASS","checks":[]}\n')
        created_ms = int((time.time() - 1) * 1000)
        intent = {
            "action": self.action,
            "created_at_ms": created_ms,
        }

        def fake_run(*_args, **_kwargs):
            (ctrl / "acceptance-pass.json").write_text(
                '{"protocol":"v2-acceptance-pass-v1","result":"PASS"}\n'
            )
            return subprocess.CompletedProcess([], 0, stdout="ACCEPTANCE_GATE_PASS\n")

        with mock.patch.object(controller.subprocess, "run", side_effect=fake_run):
            result = controller.finalize_terminal_acceptance_report(
                self.project, intent
            )
        self.assertEqual(result["kind"], "terminal-acceptance-report-finalized")
        self.assertEqual(len(result["report_sha256"]), 64)
        self.assertEqual(len(result["acceptance_pass_sha256"]), 64)

    def test_terminal_acceptance_refuses_report_older_than_execution(self):
        ctrl = self.project / ".opencode-v2"
        ctrl.mkdir(parents=True, exist_ok=True)
        report = ctrl / "acceptance-report.json"
        report.write_text('{"protocol":"v2-acceptance-report-v1","result":"PASS","checks":[]}\n')
        old = time.time() - 60
        os.utime(report, (old, old))
        intent = {
            "action": self.action,
            "created_at_ms": int(time.time() * 1000),
        }
        with mock.patch.object(controller.subprocess, "run") as run:
            result = controller.finalize_terminal_acceptance_report(
                self.project, intent
            )
        self.assertIsNone(result)
        run.assert_not_called()


class DecisionStateVersionTests(unittest.TestCase):
    def base_snapshot(self):
        return {
            "state_error":False,
            "resume_phase":"acceptance",
            "acceptance":{"complete":False},
            "reference":{"policy":"internal"},
            "plan":{"complete":False,"blocked":False,"planner_failures":0},
            "scheduler":{"max_concurrent_workers":3,"active_workers":0,"reserved_workers":0,"available_worker_slots":3,"active_deliverables":[],"error":""},
            "leaves":{},
            "execution_blockers":[],
            "tests":{"complete":False},
            "acceptance_validation":{"complete":False},
        }

    def test_acceptance_guard_error_changes_decision_version_with_same_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/".opencode-v2").mkdir()
            snapshot=self.base_snapshot()
            rendered=json.dumps(snapshot,sort_keys=True)
            first,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            (project/".opencode-v2/ACCEPTANCE.guard-errors.txt").write_text("repair me\n")
            second,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            self.assertEqual(first["decision.json"]["acceptance"]["next_action"],"fresh")
            self.assertEqual(second["decision.json"]["acceptance"]["next_action"],"repair")
            self.assertNotEqual(first["decision.json"]["state_version"],second["decision.json"]["state_version"])

    def test_structured_plan_presence_changes_decision_version_with_same_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            ctrl.mkdir()
            snapshot=self.base_snapshot()
            snapshot["resume_phase"]="implementation-plan"
            snapshot["acceptance"]["complete"]=True
            rendered=json.dumps(snapshot,sort_keys=True)
            first,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text("{}\n")
            second,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            self.assertEqual(first["decision.json"]["plan"]["next_action"],"fresh")
            self.assertEqual(second["decision.json"]["plan"]["next_action"],"continue")
            self.assertNotEqual(first["decision.json"]["state_version"],second["decision.json"]["state_version"])

    def test_control_policy_epoch_changes_state_version_with_same_project_state(self):
        snapshot=self.base_snapshot()
        rendered=json.dumps(snapshot,sort_keys=True)
        with mock.patch.object(control_query_views,"CONTROL_POLICY_EPOCH","epoch-a"):
            first,_=control_query_views.build_query_views(snapshot,{},rendered)
        with mock.patch.object(control_query_views,"CONTROL_POLICY_EPOCH","epoch-b"):
            second,_=control_query_views.build_query_views(snapshot,{},rendered)
        self.assertEqual(first["decision.json"]["control_policy_epoch"],"epoch-a")
        self.assertEqual(second["decision.json"]["control_policy_epoch"],"epoch-b")
        self.assertNotEqual(first["decision.json"]["state_version"],second["decision.json"]["state_version"])


class AcceptanceValidatorBudgetTests(unittest.TestCase):
    def test_validator_reserves_budget_for_mandatory_report_write(self):
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/acceptance-validator.md").read_text()
        self.assertIn("steps: 12",role)
        self.assertIn("spend at most 6 tool-call rounds gathering evidence",role)
        self.assertIn("no later than your 8th assistant/tool step",role)
        self.assertIn("the report write is mandatory",role)


class ControllerShadowCoherenceTests(unittest.TestCase):
    def test_transient_shadow_mismatch_reloads_decision_until_exact_match(self):
        first={"state_version":"v1","resume_phase":"implementation-plan","actions":[{"kind":"launch","mode":"continue"}]}
        second={"state_version":"v2","resume_phase":"implementation-plan","actions":[{"kind":"launch","mode":"repair"}]}
        with mock.patch.object(controller,"evaluate",side_effect=[first,second]) as evaluate, \
             mock.patch.object(controller,"compare_supervisor_shadow",side_effect=[
                 (False,"action-mismatch transient"),
                 (True,"exact-match"),
             ]) as compare, \
             mock.patch.object(controller.time,"sleep") as sleep:
            result=controller.one_pass(Path("/tmp/project"),True,shadow_attempts=2,shadow_poll_seconds=0.01)
        self.assertEqual(result["state_version"],"v2")
        self.assertTrue(result["shadow_match"])
        self.assertEqual(evaluate.call_count,2)
        self.assertEqual(compare.call_count,2)
        sleep.assert_called_once_with(0.01)

    def test_persistent_shadow_mismatch_still_fails_closed(self):
        result={"state_version":"v1","resume_phase":"implementation-plan","actions":[{"kind":"launch","mode":"continue"}]}
        with mock.patch.object(controller,"evaluate",return_value=result), \
             mock.patch.object(controller,"compare_supervisor_shadow",return_value=(False,"action-mismatch persistent")), \
             mock.patch.object(controller.time,"sleep"):
            with self.assertRaisesRegex(controller.ControllerError,"action-mismatch persistent"):
                controller.one_pass(Path("/tmp/project"),True,shadow_attempts=2,shadow_poll_seconds=0.01)


class TaskSplitterOutputCapTests(unittest.TestCase):
    def test_task_splitter_keeps_its_small_json_cap_while_planner_can_emit_one_plan_write(self):
        config=json.loads((Path(__file__).parents[1] / "xdg/config/opencode/opencode.jsonc").read_text())
        models=config["provider"]["syv"]["models"]
        planner=models["qwen38-implementation-planner-48k"]
        splitter=models["qwen38-task-splitter-nothink"]
        self.assertEqual(splitter["id"],planner["id"])
        self.assertEqual(splitter["limit"]["context"],planner["limit"]["context"])
        self.assertEqual(planner["limit"]["output"],planner["options"]["v2_max_tokens"])
        self.assertEqual(planner["limit"]["output"],7168)
        self.assertEqual(splitter["limit"]["output"],1536)
        self.assertEqual(splitter["options"]["v2_max_tokens"],7168)
        self.assertEqual(
            splitter["options"]["chat_template_kwargs"],
            {"enable_thinking":False,"preserve_thinking":False},
        )
        self.assertNotIn("reasoning_effort",splitter["options"])
        self.assertNotIn("reasoningEffort",splitter["options"])
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/task-splitter.md").read_text()
        self.assertIn("model: syv/qwen38-task-splitter-nothink",role)

    def test_role_ends_with_a_no_analysis_json_only_output_rule(self):
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/task-splitter.md").read_text()
        self.assertIn("steps: 4",role)
        self.assertIn("read: deny",role)
        self.assertIn("CANONICAL_SPLIT_REQUEST_JSON_BEGIN",role)
        self.assertIn("## Output-cap execution rule — highest priority",role)
        self.assertIn("Do not narrate analysis",role)
        self.assertIn("must begin with `{`",role)
        self.assertIn("worker-created artifact contents",role)
        self.assertIn("it below 1000 characters",role)
        self.assertLess(len(role), 15_000)

    def test_direct_context_prevents_the_duplicate_read_step_loop_without_salvage(self):
        action={"kind":"launch","agent":"task-splitter","deliverable":"D001","generation":1}
        request={"parent_id":"D001","depth":0,"generation":1,"ownership_items":["a.txt"]}
        prompt=controller.build_task_splitter_subtask(action,request)["prompt"]
        self.assertTrue(prompt.startswith("SPLIT_PARENT: D001\nCANONICAL_SPLIT_REQUEST_JSON_BEGIN\n"))
        self.assertIn('"ownership_items":["a.txt"]',prompt)
        self.assertTrue(prompt.endswith("\nCANONICAL_SPLIT_REQUEST_JSON_END"))
        self.assertEqual(supervisor.parse_split_parent(prompt),"D001")
        stranded='Maximum steps reached. Intended final JSON: {"protocol":"v2-task-split-proposal-v2"}'
        self.assertIsNone(supervisor.parse_splitter_final_json(stranded))


class RecursiveSplitControllerIntegrationTests(unittest.TestCase):
    """Filesystem-only threshold-to-rejoin integration; never creates an LLM child."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.control = self.project / ".opencode-v2"
        (self.control / "work").mkdir(parents=True)
        self.old_root, self.old_project = supervisor.ROOT, supervisor.PROJECT
        supervisor.ROOT, supervisor.PROJECT = self.project, str(self.project)
        self.parent = {
            "id": "D001", "name": "bounded parent", "owned_artifacts": "`a.txt`, `b.txt`, `c.txt`, `d.txt`",
            "launch_deps": [], "contract_deps": [], "verify_command": "test -f a.txt -a -f b.txt -a -f c.txt -a -f d.txt",
            "role": "implementer", "done_when": "all files exist", "acceptance_ids": ["A001"], "parallel": "yes",
        }
        (self.control / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol": "V2.6.9", "project": str(self.project),
            "recursive_split_protocol": control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves": {"D001": self.parent},
        }))
        (self.control / "ACCEPTANCE.md").write_text("<!-- ACCEPTANCE_COMPLETE -->\n")
        (self.control / "IMPLEMENTATION_PLAN.md").write_text("<!-- IMPLEMENTATION_PLAN_COMPLETE -->\n")
        self.write_phase_ready("ACCEPTANCE.ready", "ACCEPTANCE.md", "ACCEPTANCE_COMPLETE")
        self.write_phase_ready("IMPLEMENTATION_PLAN.ready", "IMPLEMENTATION_PLAN.md", "IMPLEMENTATION_PLAN_COMPLETE")

    def tearDown(self):
        supervisor.ROOT, supervisor.PROJECT = self.old_root, self.old_project
        self.temp.cleanup()

    def write_phase_ready(self, ready: str, artifact: str, marker: str):
        digest = hashlib.sha256((self.control / artifact).read_bytes()).hexdigest()
        (self.control / ready).write_text(
            f"status=complete\nprotocol={control_state.PHASE_READY_PROTOCOL}\n"
            f"artifact={artifact}\nmarker={marker}\n"
            f"validated={control_state.PHASE_READY_VALIDATOR}\nartifact_sha256={digest}\n"
        )

    def proposal(self):
        return [
            {"scope": "first", "owned_artifacts": "`a.txt`, `b.txt`", "verify_command": "test -f a.txt -a -f b.txt", "role": "implementer", "depends_on_sibling": "", "done_when": "first files exist", "reads_existing": [], "creates_or_updates": ["a.txt", "b.txt"]},
            {"scope": "second", "owned_artifacts": "`c.txt`, `d.txt`", "verify_command": "test -f c.txt -a -f d.txt", "role": "core-builder", "depends_on_sibling": "", "done_when": "second files exist", "reads_existing": [], "creates_or_updates": ["c.txt", "d.txt"]},
        ]

    def fail_twice(self):
        self.assertEqual(supervisor.claim_attempt("parent-1", "D001"), ("claimed", 1))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify failed"), (True, "genuine-recorded"))
        self.assertEqual(supervisor.claim_attempt("parent-2", "D001"), ("claimed", 2))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify failed"), (True, "split-required"))

    def test_threshold_split_children_reconcile_and_rejoin(self):
        self.fail_twice()
        self.assertTrue(supervisor.split_request_path("D001").is_file())
        state = control_state.snapshot(self.project)
        state["split_required"] = [{"deliverable": "D001", "split_state": "split-required", "split_generation": 1}]
        actions = deterministic_dispatch.select_actions(state)
        splitter = {"kind": "launch", "agent": "task-splitter", "deliverable": "D001", "generation": 1}
        self.assertIn(splitter, actions)
        request={"parent_id":"D001","depth":0,"generation":1}
        self.assertIn("CANONICAL_SPLIT_REQUEST_JSON_BEGIN",controller.build_task_splitter_subtask(splitter,request)["prompt"])
        self.assertEqual(supervisor.claim_splitter("D001", "split-session"), (True, "claimed"))
        proposal = {
            "protocol": supervisor.SPLIT_PROPOSAL_PROTOCOL, "parent_id": "D001", "depth": 0,
            "generation": 1, "proposals": self.proposal(),
        }
        self.assertEqual(
            supervisor.complete_splitter("D001", "ses-split-child", "split-session", json.dumps(proposal)),
            (True, "accepted"),
        )
        split_state = control_state.snapshot(self.project)
        self.assertEqual(split_state["leaves"]["D001"]["split_children"], ["D001-A", "D001-B"])
        materialized = supervisor.load_manifest()["leaves"]
        self.assertEqual(materialized["D001-A"]["owned_artifact_paths"], ["a.txt", "b.txt"])
        self.assertEqual(materialized["D001-B"]["owned_artifact_paths"], ["c.txt", "d.txt"])
        for child, files, session in (("D001-A", ("a.txt", "b.txt"), "child-a"), ("D001-B", ("c.txt", "d.txt"), "child-b")):
            self.assertEqual(supervisor.claim_attempt(session, child)[0], "claimed")
            for name in files:
                (self.project / name).write_text(name, encoding="utf-8")
            self.assertEqual(supervisor.supervisor_finalize_ready(child), (True, "finalized"))
        self.assertEqual(supervisor.post_session_finalize("D001"), (True, "finalized"))
        rejoined = control_state.snapshot(self.project)
        self.assertTrue(rejoined["leaves"]["D001"]["complete"])
        self.assertNotEqual(rejoined["resume_phase"], "recursive-split")
        self.assertNotIn(splitter, deterministic_dispatch.select_actions(rejoined))

    def test_false_parent_contract_repair_recreates_exactly_one_claim_slot(self):
        self.fail_twice()
        status={
            "owner":"supervisor","parent_id":"D001","state":"splitter-active",
            "generation":1,"claim_count":6,"proposal_failures":5,
            "recovery_claim_budget":4,"recovery_history":[],
        }
        proposal={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"prerequisite_artifacts","reason":"missing owned output is not a contract defect",
        }
        supervisor.atomic_write_json(supervisor.split_status_path("D001"),status)
        supervisor.atomic_write_json(supervisor.split_proposal_path("D001"),proposal)
        archived=supervisor._archive_split_state_for_contract_repair("D001")
        supervisor._clear_split_request_state_for_contract_repair("D001")
        data=supervisor.load_attempts()
        entry=data["deliverables"]["D001"]
        entry.pop("split_required")
        for row in entry["failure_history"]:
            row["classification"]="bad-plan"
            row["reclassified_by"]="runtime-parent-contract-repair"
        entry["split_rearm_after_contract_repair"]={
            "generation":1,"field":"prerequisite_artifacts","structured_key":"parent",
        }
        supervisor.save_attempts(data)
        self.assertTrue(archived.is_file())
        original_fingerprint=supervisor.task_splitter_direct_context_fingerprint
        supervisor.task_splitter_direct_context_fingerprint=lambda: "current-direct-context"
        try:
            ok,detail=supervisor.recover_false_parent_contract_repair("D001")
        finally:
            supervisor.task_splitter_direct_context_fingerprint=original_fingerprint
        self.assertEqual((ok,detail),(True,"recovered-one-claim"))
        recovered=supervisor.load_split_status("D001")
        self.assertEqual(recovered["state"],"split-retryable")
        self.assertEqual((recovered["claim_count"],recovered["proposal_failures"]),(6,5))
        self.assertEqual(recovered["recovery_claim_budget"],5)
        self.assertEqual(supervisor.splitter_claim_limit(recovered),7)
        preserved=supervisor.load_attempts()["deliverables"]["D001"]
        self.assertTrue(all(row["classification"]=="bad-plan" for row in preserved["failure_history"]))
        self.assertTrue(supervisor.split_request_path("D001").is_file())
        repair={
            "protocol":"v2-structured-plan-repair-v1",
            "source":"runtime-split-parent-contract","whole_plan":False,
            "affected_keys":["parent"],
            "errors":[{"code":"runtime-parent-prerequisite-artifacts-missing"}],
        }
        repair_path=self.control/"IMPLEMENTATION_PLAN.repair.json"
        supervisor.atomic_write_json(repair_path,repair)
        original_finalizer=supervisor._finalize_current_plan_without_rearm
        supervisor._finalize_current_plan_without_rearm=lambda: True
        try:
            ok,detail=supervisor.resolve_false_parent_contract_repair("D001")
        finally:
            supervisor._finalize_current_plan_without_rearm=original_finalizer
        self.assertEqual((ok,detail),(True,"resolved-current-plan"))
        self.assertFalse(repair_path.exists())
        preserved=supervisor.load_attempts()["deliverables"]["D001"]
        self.assertNotIn("split_rearm_after_contract_repair",preserved)
        self.assertTrue(preserved["false_parent_contract_repair_recovery"]["resolution_archive"])
        self.assertEqual(supervisor.claim_splitter("D001","claim-seven"),(True,"claimed"))


if __name__ == "__main__":
    unittest.main()
