#!/usr/bin/env python3
"""Regression tests for Stage-A's transport-only OpenCode root."""

from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stage_a_controller as controller
import supervisor


class TransportRootNoContinuationTests(unittest.TestCase):
    def assert_transport_subtask(self, part, expected_agent):
        self.assertEqual(part["type"], "subtask")
        self.assertEqual(part["agent"], expected_agent)
        self.assertNotIn(
            "command",
            part,
            "Stage-A transport-root SubtaskParts must omit command; "
            "OpenCode v1.18.31 otherwise resumes the v2noop parent",
        )

    def test_all_controller_subtask_builders_omit_command(self):
        implementation = controller.build_implementation_subtask(
            {"kind": "launch", "agent": "probe-builder", "deliverable": "D001"},
            "DELIVERABLE: D001\ncanonical",
        )
        self.assert_transport_subtask(implementation, "probe-builder")

        splitter = controller.build_task_splitter_subtask(
            {
                "kind": "launch",
                "agent": "task-splitter",
                "deliverable": "D001",
                "generation": 1,
            },
            {"parent_id": "D001", "generation": 1, "depth": 0},
        )
        self.assert_transport_subtask(splitter, "task-splitter")

        planner = controller.build_planner_subtask(
            {"kind": "launch", "agent": "implementation-planner", "mode": "fresh"}
        )
        self.assert_transport_subtask(planner, "implementation-planner")

        (semantic_agent, semantic_mode) = next(iter(controller.SEMANTIC_PROMPTS))
        semantic = controller.build_semantic_subtask(
            {"kind": "launch", "agent": semantic_agent, "mode": semantic_mode}
        )
        self.assert_transport_subtask(semantic, semantic_agent)

    def test_corrective_splitter_dispatch_omits_command(self):
        calls = []

        def fake_request(method, url, payload=None, timeout=None):
            calls.append((method, url, payload, timeout))
            return None

        with (
            mock.patch.object(supervisor, "PROJECT", "/tmp/stage-a-transport-root-test"),
            mock.patch.object(supervisor.http, "ensure", return_value=True),
            mock.patch.object(supervisor.http, "request", side_effect=fake_request),
            mock.patch.object(supervisor.http, "mode", "v1"),
        ):
            ok, detail = supervisor.dispatch_splitter_corrective_turn(
                "ses_root", "SPLIT_PARENT: D001\nSPLITTER_CORRECTIVE_ORDINAL: 1"
            )

        self.assertTrue(ok)
        self.assertEqual(detail, "accepted-after-root-idle")
        post_calls=[call for call in calls if call[0] == "POST"]
        self.assertEqual(len(post_calls), 1)
        method, url, payload, timeout = post_calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/session/ses_root/prompt_async?", url)
        self.assertEqual(timeout, 12)
        self.assertEqual(payload["agent"], "transport-root")
        self.assertEqual(
            payload["model"], {"providerID": "v2noop", "modelID": "root-noop"}
        )
        self.assertEqual(len(payload["parts"]), 1)
        self.assert_transport_subtask(payload["parts"][0], "task-splitter")
        self.assertIn(
            "SPLITTER_CORRECTIVE_ORDINAL: 1", payload["parts"][0]["prompt"]
        )




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

if __name__ == "__main__":
    unittest.main(verbosity=2)
