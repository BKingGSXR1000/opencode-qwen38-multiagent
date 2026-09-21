#!/usr/bin/env python3
"""Isolated regression tests for the Stage-A stabilization boundary."""
from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import control_state
import deterministic_dispatch
import stage_a_controller as controller
import stage_a_path_permissions as path_permissions
import supervisor


OWNED = ".opencode-v2/ACCEPTANCE.md"
RULES = [("*", "deny"), (OWNED, "allow")]


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


class TaskSplitterOutputCapTests(unittest.TestCase):
    def test_task_splitter_has_a_dedicated_nonthinking_profile_with_unchanged_caps(self):
        config=json.loads((Path(__file__).parents[1] / "xdg/config/opencode/opencode.jsonc").read_text())
        models=config["provider"]["syv"]["models"]
        planner=models["qwen38-implementation-planner-48k"]
        splitter=models["qwen38-task-splitter-nothink"]
        self.assertEqual(splitter["id"],planner["id"])
        self.assertEqual(splitter["limit"],planner["limit"])
        self.assertEqual(splitter["options"]["v2_max_tokens"],planner["options"]["v2_max_tokens"])
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
        self.assertIn("## Output-cap execution rule — highest priority",role)
        self.assertIn("Do not narrate analysis",role)
        self.assertIn("must begin with `{`",role)
        self.assertLess(len(role), 15_000)


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
        self.assertEqual(controller.build_task_splitter_subtask(splitter)["prompt"], "SPLIT_PARENT: D001")
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


if __name__ == "__main__":
    unittest.main()
