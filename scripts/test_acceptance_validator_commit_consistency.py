#!/usr/bin/env python3
"""One immutable acceptance report; post-write contradictions fail closed."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stage_a_controller
import supervisor


class AcceptanceCommitConsistencyTests(unittest.TestCase):
    def test_role_and_controller_require_complete_prewrite_judgment(self):
        role=(Path(__file__).parents[1]/
              "xdg/config/opencode/agents/acceptance-validator.md").read_text()
        prompt=stage_a_controller.SEMANTIC_PROMPTS[
            ("acceptance-validator","final")
        ]
        self.assertIn("BEFORE invoking write",role)
        self.assertIn("NEVER reconsider a MUST",role)
        self.assertIn("BEFORE that write",prompt)
        self.assertIn("cannot\nedit or override the first report",prompt)

    def test_later_fail_cannot_convert_immutable_pass_into_silent_success(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td);ctrl=project/".opencode-v2";ctrl.mkdir()
            (ctrl/"ACCEPTANCE.md").write_text(
                "Reference policy: none\n- [ ] A001: Valid behavior.\n"
            )
            report=json.dumps({
                "protocol":"v2-acceptance-report-v1",
                "result":"PASS",
                "checks":[{"id":"A001","status":"PASS",
                           "evidence":"functional behavior executed"}],
            })
            (ctrl/"acceptance-report.json").write_text(report)
            row={"content":report,"sha256":hashlib.sha256(report.encode()).hexdigest()}
            with mock.patch.object(supervisor,"PROJECT",str(project)), \
                 mock.patch.object(supervisor,"_session_agent_db",
                                   return_value="acceptance-validator"), \
                 mock.patch.object(supervisor,"_v1_active_session_ids",
                                   return_value=set()), \
                 mock.patch.object(supervisor,"last_assistant_text_db",
                                   return_value="ACCEPTANCE_FAIL\nA001: reconsidered"), \
                 mock.patch.object(supervisor,"acceptance_validator_report_records",
                                   return_value=[row]):
                result=supervisor.reconcile_terminal_acceptance_validator("ses_test")
            self.assertEqual(result,(False,"acceptance-failure-first-report-not-fail"))
            self.assertFalse((ctrl/"acceptance-pass.json").exists())


if __name__=="__main__":
    unittest.main()
