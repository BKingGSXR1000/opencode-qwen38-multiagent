#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import supervisor


class ActionGuardVerifySourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name).resolve()
        (self.project / "spec_tests").mkdir()
        (self.project / "spec_tests/test_priority.py").write_text("# spec\n")
        self.old_project = supervisor.PROJECT
        self.old_log = supervisor.LOG
        supervisor.PROJECT = str(self.project)
        supervisor.LOG = self.project / "events.log"
        self.leaf = {
            "id": "D001",
            "role": "implementer",
            "owned_artifact_paths": ["priority.py"],
            "owned_artifacts": "priority.py",
            "verify_command": "python3 -m unittest spec_tests.test_priority -v",
            "done_when": "priority.py passes its immutable tests",
            "launch_deps": [],
            "contract_deps": [],
            "verify_deps": [],
            "split_children": [],
            "complexity": "S",
        }

    def tearDown(self):
        supervisor.PROJECT = self.old_project
        supervisor.LOG = self.old_log
        self.tmp.cleanup()

    def test_exact_unittest_module_maps_to_local_source(self):
        self.assertEqual(
            supervisor.implementation_verify_source_read_targets(self.leaf),
            ["spec_tests/test_priority.py"],
        )

    def test_compound_verify_resolves_multiple_local_unittest_modules(self):
        (self.project / "spec_tests/test_cli.py").write_text("# cli\n")
        leaf = dict(self.leaf)
        leaf["verify_command"] = (
            "python3 -m unittest spec_tests.test_priority -v && "
            "python3 -m unittest spec_tests.test_cli -v"
        )
        self.assertEqual(
            supervisor.implementation_verify_source_read_targets(leaf),
            ["spec_tests/test_priority.py", "spec_tests/test_cli.py"],
        )

    def test_prewrite_targets_include_verify_source_once(self):
        (self.project / "priority.py").write_text("old = True\n")
        targets = supervisor.implementation_prewrite_read_targets(self.leaf)
        self.assertEqual(
            targets,
            ["priority.py", "spec_tests/test_priority.py"],
        )

    def test_failed_verify_allows_unread_verify_source_once(self):
        args = {"filePath": str(self.project / "spec_tests/test_priority.py")}
        patches = [
            mock.patch.object(supervisor, "_session_agent_db", return_value="implementer"),
            mock.patch.object(supervisor, "first_user_text_db", return_value="DELIVERABLE: D001"),
            mock.patch.object(supervisor, "load_manifest", return_value={"leaves": {"D001": self.leaf}}),
            mock.patch.object(supervisor, "attempt_sequence_for_session", return_value=1),
            mock.patch.object(supervisor, "reconcile_implementation_progress_read_marker", return_value=True),
            mock.patch.object(supervisor, "implementation_progress_read_available", return_value=False),
            mock.patch.object(supervisor, "persisted_completed_tool_turns", return_value=5),
            mock.patch.object(supervisor, "ready_info", return_value={}),
            mock.patch.object(supervisor, "plan_contract_session_exact_verify_failure_count", return_value=1),
            mock.patch.object(supervisor, "_owned_artifact_changed_since_execution_baseline", return_value=(True, "changed")),
            mock.patch.object(supervisor, "plan_contract_reverify_pending", return_value=False),
            mock.patch.object(supervisor, "implementation_max_step_continuation_session_pending", return_value=False),
            mock.patch.object(supervisor, "session_owned_mutation_seen", return_value=True),
            mock.patch.object(supervisor, "plan_contract_session_exact_verify_state", return_value="failed"),
            mock.patch.object(supervisor, "persisted_exact_project_read_seen", return_value=False),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12], patches[13]:
            state, detail = supervisor.implementation_direct_write_gate_state(
                "ses-source", "read", args
            )
        self.assertEqual(state, "implementation-authoritative-read-once")
        self.assertIn("IMPLEMENTATION_VERIFY_SOURCE_READ", detail)


class ActionGuardEpisodeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.leaf = {
            "verify_command": "python3 -m unittest spec_tests.test_priority -v",
        }

    def test_owned_mutation_resets_prewrite_denial_budget(self):
        rows = [
            {"tool": "read", "input": {}, "status": "error",
             "error": "EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED one"},
            {"tool": "read", "input": {}, "status": "error",
             "error": "EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED two"},
            {"tool": "edit", "input": {"filePath": "priority.py"},
             "status": "completed", "error": ""},
            {"tool": "read", "input": {}, "status": "error",
             "error": "EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED one"},
        ]
        with mock.patch.object(
            supervisor, "session_completed_tool_records", return_value=rows
        ), mock.patch.object(
            supervisor, "_current_tool_mutates_owned_artifact",
            side_effect=lambda did, tool, args: tool == "edit",
        ), mock.patch.object(
            supervisor, "_tool_is_exact_leaf_verify", return_value=False
        ):
            result = supervisor.persisted_implementation_guard_denials(
                "ses", "D001", self.leaf,
                "implementation-exact-verify-required",
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["marker"],
            "EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED",
        )

    def test_exact_verify_resets_prior_exact_verify_denials(self):
        rows = [
            {"tool": "read", "input": {}, "status": "error",
             "error": "EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED one"},
            {"tool": "read", "input": {}, "status": "error",
             "error": "EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED two"},
            {"tool": "bash", "input": {"command": "canonical"},
             "status": "completed", "error": ""},
            {"tool": "read", "input": {}, "status": "error",
             "error": "EARLY_WRITE_IMPLEMENTATION_RETURN_REQUIRED one"},
        ]
        with mock.patch.object(
            supervisor, "session_completed_tool_records", return_value=rows
        ), mock.patch.object(
            supervisor, "_current_tool_mutates_owned_artifact", return_value=False
        ), mock.patch.object(
            supervisor, "_tool_is_exact_leaf_verify",
            side_effect=lambda leaf, tool, args, sid: tool == "bash",
        ):
            result = supervisor.persisted_implementation_guard_denials(
                "ses", "D001", self.leaf,
                "implementation-return-required",
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["marker"],
            "EARLY_WRITE_IMPLEMENTATION_RETURN_REQUIRED",
        )

    def test_repeated_guard_noncompliance_is_worker_behavior(self):
        reason = (
            "repeated_action_guard_noncompliance deliverable=D001 "
            "prior_denials=2 limit=2 required_state=implementation-write-required"
        )
        self.assertEqual(
            supervisor.worker_behavior_abort_reason(reason),
            reason,
        )
        self.assertEqual(
            supervisor.worker_behavior_abort_reason(
                "backend connection reset by peer"
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
