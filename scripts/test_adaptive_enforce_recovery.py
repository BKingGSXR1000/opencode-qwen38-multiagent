#!/usr/bin/env python3
"""Isolated enforce -> durable abort -> genuine failure -> bounded retry proof.

Uses a real compiled and guarded Stage-A fixture and real supervisor ledger,
not a mocked failure classifier. Only the external HTTP interrupt is replaced:
no Qwen session and no other live project is ever touched.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive_reasoning_watchdog as policy
import control_state
import supervisor

SPEC = importlib.util.spec_from_file_location(
    "restart_fixture", Path(__file__).with_name("create-restart-recovery-canary.py")
)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class AdaptiveEnforceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="a2-watchdog-recovery-")
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        task = root / "TASK.md"
        task.write_text("Create restart_probe.txt containing restart-recovery-ok.\n")
        result = FIXTURE.build(self.project, task)
        self.assertEqual(result["resume_phase"], "execution")
        self.old_project = supervisor.PROJECT
        supervisor.PROJECT = str(self.project)
        self.old_mode = supervisor.ADAPTIVE_REASONING_MODE
        supervisor.ADAPTIVE_REASONING_MODE = "enforce"
        self.sid = "ses-adaptive-stalled"
        self.did = "D001"
        supervisor.adaptive_watch.pop(self.sid, None)
        supervisor.post_finalize_seen.discard(self.sid)
        supervisor.supervisor_abort_reasons.pop(self.sid, None)
        self.assertEqual(supervisor.claim_attempt(self.sid, self.did), ("claimed", 1))
        self.work = self.project / ".opencode-v2" / "work"

    def tearDown(self):
        supervisor.ADAPTIVE_REASONING_MODE = self.old_mode
        supervisor.PROJECT = self.old_project
        supervisor.adaptive_watch.pop(self.sid, None)
        supervisor.session_task.pop(self.sid, None)
        supervisor.post_finalize_seen.discard(self.sid)
        supervisor.supervisor_abort_reasons.pop(self.sid, None)
        self.tmp.cleanup()

    def _drive_enforce(self, abort, sid=None):
        sid = sid or self.sid
        watch = {"aborted_key": None}
        shape = {"message_id": "msg-stall", "last_tool_id": ""}
        live = {
            "connected": True, "action_seq": 0, "tool_successes": 0,
            "action_reasoning_chars": 0
        }
        with mock.patch.object(supervisor, "http") as http, \
             mock.patch.object(supervisor, "log"), \
             mock.patch.object(supervisor, "csv"), \
             mock.patch.object(supervisor, "emit_watchdog_telemetry"):
            http.interrupt.side_effect = abort
            initial = supervisor.adaptive_reasoning_step(
                sid, "implementer", self.did, ("msg-stall", ""), shape,
                live, 0, can_watch=True, tool_running=False,
                compaction_active=False, backend_snapshot={},
                watch_state=watch, now_mono=100
            )
            live["action_reasoning_chars"] = 7500
            stalled = supervisor.adaptive_reasoning_step(
                sid, "implementer", self.did, ("msg-stall", ""), shape,
                live, 7500, can_watch=True, tool_running=False,
                compaction_active=False, backend_snapshot={"running": 1},
                watch_state=watch, now_mono=180
            )
            assert not initial["abort"]
            assert stalled["abort"], stalled
            return stalled, watch, http.interrupt.call_count

    def test_enforce_persists_real_genuine_failure_and_allows_one_bounded_retry(self):
        observed = []
        def interrupt(sid):
            observed.append(sid)
            # External interrupt must NEVER precede the atomic durable intent.
            entry = json.loads(
                (self.work / "abort-intents.json").read_text()
            )["sessions"][sid]
            self.assertEqual(entry["state"], "requested")
            self.assertEqual(entry["agent"], "implementer")
            return True

        stalled, watch, calls = self._drive_enforce(interrupt)
        self.assertEqual(stalled["reason"], "reasoning-budget-without-tool")
        self.assertEqual(calls, 1)
        self.assertEqual(observed, [self.sid])
        self.assertEqual(watch["aborted_key"], ("msg-stall", ""))

        intent = json.loads(
            (self.work / "abort-intents.json").read_text()
        )["sessions"][self.sid]
        self.assertEqual(intent["state"], "confirmed")
        self.assertTrue(
            intent["reason"].startswith("runaway adaptive_reasoning session=")
        )
        self.assertEqual(
            supervisor.worker_behavior_abort_reason(intent["reason"]),
            intent["reason"],
        )
        self.assertEqual(
            supervisor.persisted_abort_reason(self.sid), intent["reason"]
        )

        with mock.patch.object(
            supervisor, "plan_ready", return_value=True
        ), mock.patch.object(
            supervisor, "handle_leaf_contract_challenge", return_value=False
        ), mock.patch.object(
            supervisor, "immediate_runtime_abort", return_value=""
        ), mock.patch.object(
            supervisor, "meaningful_worker_execution", return_value=""
        ), mock.patch.object(
            supervisor, "durable_worker_execution", return_value=False
        ), mock.patch.object(
            supervisor, "worker_sandbox_cleanup_session"
        ), mock.patch.object(supervisor, "log"), mock.patch.object(
            supervisor, "csv"
        ), mock.patch.object(
            supervisor, "release_operator_reservation"
        ):
            supervisor.reconcile_idle_implementation_session(
                self.sid, "implementer"
            )
            # Crash replay must not classify the same failed attempt twice.
            supervisor.post_finalize_seen.discard(self.sid)
            supervisor.reconcile_idle_implementation_session(
                self.sid, "implementer"
            )

        ledger = json.loads(
            (self.work / "attempts.json").read_text()
        )["deliverables"][self.did]
        history = ledger["failure_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["attempt"], 1)
        self.assertEqual(history[0]["session"], self.sid)
        self.assertEqual(history[0]["classification"], "genuine")
        self.assertEqual(ledger.get("infrastructure_failures", []), [])
        self.assertEqual(ledger.get("infrastructure_retry_grants", 0), 0)
        intent = json.loads(
            (self.work / "abort-intents.json").read_text()
        )["sessions"][self.sid]
        self.assertEqual(intent["state"], "resolved")
        self.assertEqual(intent["outcome"], "genuine-worker-behavior-failure")
        self.assertEqual(
            supervisor.claim_attempt("ses-adaptive-retry", self.did),
            ("claimed", 2),
        )
        state = control_state.attempt_state(
            json.loads((self.work / "attempts.json").read_text())
            ["deliverables"][self.did]
        )
        self.assertTrue(state["valid"])
        self.assertEqual(state["count"], 2)
        # No third retry when the S-class's ordinary two automatic attempts
        # are exhausted; the existing normal split protocol handles failure.
        self.assertEqual(
            supervisor.claim_attempt("ses-adaptive-third", self.did)[0],
            "limit",
        )
        supervisor.session_task.pop("ses-adaptive-retry", None)

    def test_http_abort_failure_does_not_create_genuine_failure_or_grant(self):
        stalled, watch, calls = self._drive_enforce(lambda sid: False)
        self.assertTrue(stalled["abort"])
        self.assertEqual(calls, 1)
        self.assertIsNone(watch["aborted_key"])
        intent = json.loads(
            (self.work / "abort-intents.json").read_text()
        )["sessions"][self.sid]
        self.assertEqual(intent["state"], "failed")
        self.assertFalse(supervisor.persisted_abort_reason(self.sid))
        ledger = json.loads(
            (self.work / "attempts.json").read_text()
        )["deliverables"][self.did]
        self.assertEqual(ledger.get("failure_history", []), [])
        self.assertEqual(ledger.get("infrastructure_failures", []), [])

    def test_nonadaptive_abort_keeps_existing_infrastructure_behavior(self):
        self.assertEqual(
            supervisor.worker_behavior_abort_reason(
                "opencode-server-restart-incomplete-session"
            ), "",
        )
        self.assertEqual(
            supervisor.worker_behavior_abort_reason(
                "child_compaction count=2; limit=1"
            ), "",
        )
        self.assertEqual(
            supervisor.worker_behavior_abort_reason(
                "runaway adaptive_reasoning"  # missing trusted session signature
            ), "",
        )


if __name__ == "__main__":
    unittest.main()
