#!/usr/bin/env python3
from __future__ import annotations
import os,unittest
from unittest import mock

import control_policy
import scheduler_policy
import supervisor

class SchedulerPolicyTests(unittest.TestCase):
    def test_default_preserves_proven_three_worker_limit(self):
        with mock.patch.dict(os.environ,{},clear=True):
            self.assertEqual(scheduler_policy.implementation_worker_limit(),3)
            self.assertEqual(
                scheduler_policy.implementation_worker_limit_source(),"default"
            )

    def test_explicit_limits_one_through_eight_are_valid(self):
        for value in (1,2,3,5,8):
            with self.subTest(value=value), mock.patch.dict(
                os.environ,{scheduler_policy.ENV_WORKER_LIMIT:str(value)},clear=True
            ):
                self.assertEqual(
                    scheduler_policy.implementation_worker_limit(),value
                )
                self.assertEqual(
                    scheduler_policy.implementation_worker_limit_source(),
                    "environment",
                )

    def test_invalid_limits_fail_closed(self):
        for value in ("0","9","-1","3.0","three","1e1"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ,{scheduler_policy.ENV_WORKER_LIMIT:value},clear=True
            ):
                with self.assertRaises(scheduler_policy.SchedulerPolicyError):
                    scheduler_policy.implementation_worker_limit()

    def test_available_slots_uses_validated_runtime_limit(self):
        with mock.patch.dict(
            os.environ,{scheduler_policy.ENV_WORKER_LIMIT:"5"},clear=False
        ), mock.patch.object(
            supervisor,"active_implementation_sessions",return_value=[]
        ), mock.patch.object(
            supervisor,"reserved_dispatch_slot_count",return_value=1
        ), mock.patch.object(
            supervisor,"unclassified_native_attempt_deliverables",
            return_value={"D004"},
        ):
            self.assertEqual(
                supervisor.available_implementation_slots({},strict=True),3
            )

    def test_invalid_runtime_limit_denies_preclaim_without_attempt_write(self):
        with mock.patch.dict(
            os.environ,{scheduler_policy.ENV_WORKER_LIMIT:"99"},clear=False
        ), mock.patch.object(
            supervisor,"validate_dispatch",return_value=("D001","")
        ), mock.patch.object(
            supervisor,"active_implementation_sessions",return_value=[]
        ), mock.patch.object(
            supervisor,"load_attempts",
            return_value={"owner":"supervisor","deliverables":{}},
        ), mock.patch.object(
            supervisor,"reserved_dispatch_deliverables",return_value=set()
        ), mock.patch.object(
            supervisor,"unclassified_native_attempt_deliverables",
            return_value=set(),
        ), mock.patch.object(supervisor,"claim_attempt") as claim:
            result=supervisor.preclaim_attempt("implementer","D001","token")
        self.assertEqual(result[:3],(
            "denied","D001","worker_scheduler_unavailable"
        ))
        claim.assert_not_called()

    def test_runtime_policy_source_is_supervisor_reexec_tracked(self):
        self.assertIn(
            "scheduler_policy.py",
            {p.name for p in control_policy.reexec_source_paths()},
        )

if __name__=="__main__":
    unittest.main()
