#!/usr/bin/env python3
"""Ensure benchmark-owned Verify oracles reject the discovered weak modules."""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location(
    "trusted_fixture_test",HERE/"create-adaptive-medium-verified-canary.py")
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class TrustedFixtureChecks(unittest.TestCase):
    def test_compiled_guard_has_five_leaves_and_two_parallel_starts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);p=root/"project";p.mkdir()
            task=root/"task.md";task.write_text(fixture.TASK_TEXT)
            receipt=fixture.build(p,task)
            self.assertEqual(receipt["eligible"],["D001","D002"])
            self.assertEqual(len(list((p/"spec_tests").glob("test_*.py"))),4)

    def test_weak_priority_semantics_fail_immutable_verify(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/".opencode-v2/work").mkdir(parents=True)
            fixture.seed_specs(p)
            (p/"priority.py").write_text(
                "def priority_band(n):\n"
                " if type(n) is not int or not 1<=n<=5: raise ValueError(n)\n"
                " return 'high' if n<=2 else 'normal'\n"
            )
            proc=subprocess.run(
                [sys.executable,"-m","unittest",
                 "spec_tests.test_priority","-q"],
                cwd=p,capture_output=True,text=True,timeout=9)
            self.assertNotEqual(proc.returncode,0)
            self.assertIn("FAIL",proc.stderr)


if __name__=="__main__":
    unittest.main()
