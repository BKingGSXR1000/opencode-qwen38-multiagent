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
            self.assertEqual(len(list((p/"spec_tests").glob("test_*.py"))),5)

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


    def test_summary_spec_rejects_priority_grouping_instead_of_lexicographic(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/".opencode-v2/work").mkdir(parents=True)
            fixture.seed_specs(p)
            (p/"normalize.py").write_text(
                "def normalize_name(v):\n"
                " if not isinstance(v,str) or not v.strip():raise ValueError(v)\n"
                " return v.strip()\n"
            )
            (p/"priority.py").write_text(
                "def priority_band(v):\n"
                " if type(v) is not int or not 1<=v<=5:raise ValueError(v)\n"
                " return 'high' if v>=4 else 'normal'\n"
            )
            (p/"summary.py").write_text(
                "from normalize import normalize_name\n"
                "from priority import priority_band\n"
                "def summarize(rows):\n"
                " items=[];high=normal=0\n"
                " for i,row in enumerate(rows):\n"
                "  name=normalize_name(row['name']);band=priority_band(row['priority']);"
                "high+=band=='high';normal+=band=='normal';items.append((band,i,name))\n"
                " items.sort(key=lambda x:(0 if x[0]=='normal' else 1,x[1]))\n"
                " return {'total':len(items),'high':high,'normal':normal,'names':[x[2] for x in items]}\n"
            )
            proc=subprocess.run(
                [sys.executable,"-m","unittest","spec_tests.test_summary","-q"],
                cwd=p,capture_output=True,text=True,timeout=10,
            )
            self.assertNotEqual(proc.returncode,0)

    def test_worker_test_contract_rejects_inprocess_only_cli_test(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/".opencode-v2/work").mkdir(parents=True)
            fixture.seed_specs(p)
            (p/"tests").mkdir()
            (p/"tests/test_summary.py").write_text(
                "import unittest\n"
                "import cli\n"
                "class T(unittest.TestCase):\n"
                " def test_cli(self): self.assertTrue(callable(cli.main))\n"
            )
            proc=subprocess.run(
                [
                    sys.executable,"-m","unittest",
                    "spec_tests.test_worker_test_contract","-q",
                ],
                cwd=p,capture_output=True,text=True,timeout=10,
            )
            self.assertNotEqual(proc.returncode,0)

if __name__=="__main__":
    unittest.main()
