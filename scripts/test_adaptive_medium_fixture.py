#!/usr/bin/env python3
"""Five-leaf medium fixture and held-out quality measurement guards."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parent


def load(filename,name):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


medium=load("create-adaptive-medium-canary.py","medium_fixture_tests")
runner=load("benchmark-adaptive-medium.py","medium_runner_tests")


class MediumBenchmarkTests(unittest.TestCase):
    def test_contract_has_two_independent_leaves_and_five_distinct_owned_sets(self):
        x=medium.structured_fixture()
        self.assertEqual(x["protocol"],"v2-structured-plan-v1")
        leaves=x["leaves"]
        self.assertEqual(len(leaves),5)
        self.assertEqual(leaves[0]["launch_deps"],[])
        self.assertEqual(leaves[1]["launch_deps"],[])
        self.assertEqual(set(leaves[2]["launch_deps"]),{"normalize","priority"})
        self.assertEqual(leaves[-1]["verify_command"],
                         ".opencode-v2/bin/run-checks")
        self.assertEqual({a for leaf in leaves for a in leaf["acceptance_ids"]},
                         {"A001","A002","A003","A004","A005"})
        owned=[path for leaf in leaves for path in leaf["owned_artifacts"]]
        self.assertEqual(len(owned),len(set(owned)))
        self.assertIn("bool is",medium.TASK_TEXT)
        self.assertNotIn("Jupiter",medium.TASK_TEXT)

    def test_current_compiler_accepts_exact_medium_fixture(self):
        with tempfile.TemporaryDirectory(prefix="medium-guard-test-") as td:
            root=Path(td);project=root/"project";project.mkdir()
            task=root/"TASK.md";task.write_text(medium.TASK_TEXT)
            x=medium.build(project,task)
            self.assertEqual(x["resume_phase"],"execution")
            self.assertEqual(x["eligible"],["D001","D002"])
            self.assertTrue(
                (project/".opencode-v2/IMPLEMENTATION_PLAN.ready").exists()
            )

    def test_heldout_grader_rejects_incorrect_priority_boolean_policy(self):
        with tempfile.TemporaryDirectory(prefix="medium-failing-grader-") as td:
            root=Path(td)
            (root/"normalize.py").write_text(
                "def normalize_name(v):\n if not isinstance(v,str) or not v.strip(): raise ValueError(v)\n return v.strip()\n"
            )
            (root/"priority.py").write_text(
                "def priority_band(v):\n if not isinstance(v,int) or not 1<=v<=5: raise ValueError(v)\n return 'high' if v>=4 else 'normal'\n"
            )
            (root/"summary.py").write_text(
                "from normalize import normalize_name\nfrom priority import priority_band\n"
                "def summarize(rows):\n result={'total':0,'high':0,'normal':0,'names':[]}\n"
                " for row in rows:\n  name=normalize_name(row['name']);band=priority_band(row['priority']);result['total']+=1;result[band]+=1;result['names'].append(name)\n"
                " result['names'].sort();return result\n"
            )
            (root/"cli.py").write_text(
                "import json,sys\nfrom summary import summarize\n"
                "with open(sys.argv[1],encoding='utf-8') as f:\n"
                " print(json.dumps(summarize(json.loads(line) for line in f),sort_keys=True,separators=(',',':')))\n"
            )
            result=runner.run_heldout(root)
            self.assertFalse(result["pass"])
            self.assertIn("FAIL",result["diagnostics"])


if __name__=="__main__":
    unittest.main()
