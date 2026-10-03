#!/usr/bin/env python3
"""Split progress-state projection and driver reconciliation stay aligned."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import control_state
import deterministic_dispatch

HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location(
    "split_progress_driver",HERE/"drive-stage-a-run.py"
)
driver=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class SplitProgressStateContractTests(unittest.TestCase):
    def test_native_corrective_states_are_recursive_split_not_blocked(self):
        for state in (
            "splitter-corrective-awaiting-output",
            "splitter-corrective-processing",
            "parent-finalize-retry",
        ):
            with self.subTest(state=state):
                self.assertIn(state,control_state.SPLIT_PROGRESS_STATES)
                value={
                    "acceptance":{"complete":True},
                    "plan":{"complete":True,"blocked":False},
                    "leaves":{
                        "D003":{
                            "complete":False,
                            "eligible":False,
                            "split_required":True,
                            "split_state":state,
                        }
                    },
                    "execution_blockers":[],
                    "tests":{"complete":False},
                    "acceptance_validation":{"complete":False},
                }
                self.assertEqual(
                    control_state.resume_phase(value),"recursive-split"
                )

    def test_driver_waits_for_every_central_progress_state(self):
        self.assertTrue(
            set(control_state.SPLIT_PROGRESS_STATES)
            <= set(driver.AUTONOMOUS_SPLIT_RECONCILIATION_STATES)
        )
        with tempfile.TemporaryDirectory(prefix="split-progress-driver-") as td:
            project=Path(td);work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            receipt={
                "outcome":"no-dispatch",
                "state_version":"state-x",
                "actions":[{
                    "kind":"blocked","deliverable":"D003",
                    "reason":"attempt_limit_reached",
                }],
            }
            for state in (
                "splitter-corrective-awaiting-output",
                "splitter-corrective-processing",
            ):
                with self.subTest(state=state):
                    (work/"D003.split-status.json").write_text(json.dumps({
                        "owner":"supervisor","parent_id":"D003",
                        "generation":1,"claim_count":1,"state":state,
                    }))
                    self.assertTrue(
                        driver.blocked_split_reconciliation_pending(project,receipt)
                    )

    def test_dispatcher_waits_for_nonlaunch_progress_states(self):
        launch={"split-required","split-retryable"}
        for state in sorted(set(control_state.SPLIT_PROGRESS_STATES)-launch):
            with self.subTest(state=state):
                decision={
                    "resume_phase":"recursive-split",
                    "split_required":[{
                        "deliverable":"D003",
                        "split_state":state,
                        "split_generation":1,
                        "split_claim_count":1,
                    }],
                    "eligible":[],
                    "eligible_roles":{},
                    "execution_blockers":[],
                    "scheduler":{
                        "active_workers":0,
                        "available_worker_slots":3,
                        "replayable_reserved_deliverables":[],
                    },
                }
                self.assertEqual(
                    deterministic_dispatch.select_actions(decision),
                    [{"kind":"wait"}],
                )

    def test_dispatcher_launches_only_launchable_split_states(self):
        for state,claim in (("split-required",0),("split-retryable",1)):
            with self.subTest(state=state):
                decision={
                    "resume_phase":"recursive-split",
                    "split_required":[{
                        "deliverable":"D003",
                        "split_state":state,
                        "split_generation":2,
                        "split_claim_count":claim,
                    }],
                    "eligible":[],
                    "eligible_roles":{},
                    "execution_blockers":[],
                    "scheduler":{
                        "active_workers":0,
                        "available_worker_slots":3,
                        "replayable_reserved_deliverables":[],
                    },
                }
                self.assertEqual(
                    deterministic_dispatch.select_actions(decision),
                    [{
                        "kind":"launch","agent":"task-splitter",
                        "deliverable":"D003","generation":2,
                        "claim":claim+1,
                    }],
                )

    def test_true_terminal_split_state_still_does_not_get_hidden(self):
        with tempfile.TemporaryDirectory(prefix="split-terminal-driver-") as td:
            project=Path(td);work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            receipt={
                "outcome":"no-dispatch",
                "state_version":"state-x",
                "actions":[{
                    "kind":"blocked","deliverable":"D003",
                    "reason":"attempt_limit_reached",
                }],
            }
            (work/"D003.split-status.json").write_text(json.dumps({
                "owner":"supervisor","parent_id":"D003",
                "generation":1,"claim_count":2,"state":"splitter-failed",
            }))
            self.assertFalse(
                driver.blocked_split_reconciliation_pending(project,receipt)
            )


if __name__=="__main__":
    unittest.main()
