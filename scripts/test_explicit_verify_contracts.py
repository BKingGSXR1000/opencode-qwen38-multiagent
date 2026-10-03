#!/usr/bin/env python3
"""Explicit Done-when commands must survive into real Verify execution."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import leaf_contract

HERE=Path(__file__).resolve().parent
_spec=importlib.util.spec_from_file_location("run_checks_contract_test",HERE/"run-checks.py")
run_checks=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_checks)


class ExplicitVerifyContractTests(unittest.TestCase):
    def test_literal_done_when_command_is_mandatory_for_normal_leaf(self):
        tick=chr(96)
        required="python3 -m unittest discover -s tests -v"
        done="Worker-authored tests pass via "+tick+required+tick+"."
        self.assertEqual(
            leaf_contract.explicit_done_when_commands(done),[required]
        )
        missing=leaf_contract.validate_verify_adequacy(
            done,"python3 -m unittest spec_tests.test_cli -v"
        )
        self.assertTrue(
            any("explicitly requires command" in item for item in missing),
            missing,
        )
        complete=leaf_contract.validate_verify_adequacy(
            done,
            "python3 -m unittest spec_tests.test_cli -v && "+required,
        )
        self.assertEqual(complete,[])

    def test_prose_without_literal_command_is_not_guessed(self):
        self.assertEqual(
            leaf_contract.explicit_done_when_commands(
                "Run all worker-authored tests and verify the CLI."
            ),
            [],
        )

    def test_run_checks_requires_nested_commands_promised_by_final_leaf(self):
        tick=chr(96)
        spec_cmd="python3 -m unittest discover -s spec_tests -v"
        worker_cmd="python3 -m unittest discover -s tests -v"
        with tempfile.TemporaryDirectory(prefix="final-manifest-contract-") as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            ctrl.mkdir()
            (project/"spec_tests").mkdir()
            (project/"tests").mkdir()
            (project/"spec_tests"/"test_ok.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                " def test_ok(self): self.assertTrue(True)\n"
            )
            (project/"tests"/"test_ok.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                " def test_ok(self): self.assertEqual(1,1)\n"
            )
            guard={
                "leaves":{
                    "D005":{
                        "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
                        "done_when":(
                            "Final runner executes both "
                            +tick+spec_cmd+tick+" and "+tick+worker_cmd+tick+"."
                        ),
                    }
                }
            }
            (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(
                json.dumps(guard)
            )
            # A safe but incomplete manifest must fail even though its only
            # included test itself passes.
            (ctrl/"TEST_CHECKS.json").write_text(json.dumps({
                "checks":[{"name":"spec","command":spec_cmd}]
            }))
            report,rc=run_checks.run_checks(project,persist=False)
            self.assertEqual(rc,1)
            self.assertEqual(report["status"],"fail")
            self.assertEqual(report["missing_required_commands"],[worker_cmd])
            self.assertEqual(report["checks_passed"],1)

            (ctrl/"TEST_CHECKS.json").write_text(json.dumps({
                "checks":[
                    {"name":"spec","command":spec_cmd},
                    {"name":"worker","command":worker_cmd},
                ]
            }))
            report,rc=run_checks.run_checks(project,persist=False)
            self.assertEqual(rc,0,report)
            self.assertEqual(report["missing_required_commands"],[])
            self.assertEqual(report["checks_passed"],2)

    def test_canonical_runner_itself_is_allowed_to_delegate_nested_requirements(self):
        tick=chr(96)
        required="python3 -m unittest discover -s tests -v"
        done="Final runner executes "+tick+required+tick+"."
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(
                done,".opencode-v2/bin/run-checks"
            ),
            [],
        )


if __name__=="__main__":
    unittest.main()
