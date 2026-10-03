#!/usr/bin/env python3
"""Five-leaf coding fixture with trusted preseeded behavioral Verify oracles.

Supersedes the intentionally retained weak-contract diagnostic fixture.
Production harness stays generic; tests are disposable exact-project inputs
owned by the benchmark author, not a Jupiter/product implementation.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location(
    "verified_medium_base_fixture",HERE/"create-adaptive-medium-canary.py")
medium=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(medium)

PROTOCOL="v2-adaptive-medium-trusted-behavioral-verify-v1"
TASK_TEXT=medium.TASK_TEXT+"""
BEFORE any worker runs, the benchmark author provides immutable authoritative
functional tests under spec_tests/. The deterministic Verify for every leaf
MUST execute the matching spec tests, not a mere syntax compilation. The
workers may read these tests but may not edit or remove them. D005's final
test-manifest must execute both the independently seeded spec_tests suite
and the worker-authored tests suite. All modules remain owned only by the
corresponding worker deliverable; do not change Verify contracts.
"""

SPEC_FILES={
    "test_normalize.py":r"""import unittest
from normalize import normalize_name

class NormalizeTests(unittest.TestCase):
    def test_normalizes_preserves_case(self):
        self.assertEqual(normalize_name("  Ada  "),"Ada")
        self.assertEqual(normalize_name(" B "),"B")

    def test_invalid_types_raise_valueerror(self):
        for value in (None,42,True,[],{}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    normalize_name(value)

    def test_blank_names_raise_valueerror(self):
        for value in (""," ","\t\n"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    normalize_name(value)

if __name__=="__main__":
    unittest.main()
""",
    "test_priority.py":r"""import unittest
from priority import priority_band

class PriorityTests(unittest.TestCase):
    def test_all_valid_boundaries(self):
        for value,expected in ((1,"normal"),(2,"normal"),(3,"normal"),
                               (4,"high"),(5,"high")):
            with self.subTest(value=value):
                self.assertEqual(priority_band(value),expected)

    def test_invalid_rejected_as_valueerror_not_typeerror(self):
        for value in (0,6,True,False,1.0,"4",None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    priority_band(value)

if __name__=="__main__":
    unittest.main()
""",
    "test_summary.py":r"""import copy
import unittest
from summary import summarize

class SummaryTests(unittest.TestCase):
    def test_mixed_and_duplicate_names(self):
        rows=[
            {"name":" B ","priority":4},
            {"name":"A","priority":2},
            {"name":"B","priority":5},
        ]
        original=copy.deepcopy(rows)
        self.assertEqual(summarize(rows),{
            "total":3,"high":2,"normal":1,"names":["A","B","B"],
        })
        self.assertEqual(rows,original)

    def test_names_are_globally_lexicographic_not_priority_grouped(self):
        rows=[
            {"name":"Zulu","priority":1},
            {"name":"Alpha","priority":5},
        ]
        self.assertEqual(
            summarize(rows)["names"],
            ["Alpha","Zulu"],
        )

    def test_empty_and_generator(self):
        expected={"total":0,"high":0,"normal":0,"names":[]}
        self.assertEqual(summarize([]),expected)
        self.assertEqual(summarize(iter([])),expected)

    def test_malformed_rejected(self):
        for records in (
            [{"name":"A"}],
            [{"priority":4}],
            [None],
            [{"name":" ","priority":3}],
            [{"name":"A","priority":True}],
        ):
            with self.subTest(records=repr(records)):
                with self.assertRaises(ValueError):
                    summarize(records)

if __name__=="__main__":
    unittest.main()
""",
    "test_cli.py":r"""import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

class CLITests(unittest.TestCase):
    def test_exact_compact_output_and_unchanged_input(self):
        with tempfile.TemporaryDirectory(prefix="spec-cli-") as td:
            path=Path(td)/"records.jsonl"
            original='{"name":" B ","priority":4}\n{"name":"A","priority":2}\n'
            path.write_text(original)
            run=subprocess.run(
                [sys.executable,"cli.py",str(path)],
                capture_output=True,text=True,timeout=15,
            )
            self.assertEqual(run.returncode,0,run.stderr)
            expected=json.dumps(
                {"total":2,"high":1,"normal":1,"names":["A","B"]},
                sort_keys=True,separators=(",",":"),
            )+"\n"
            self.assertEqual(run.stdout,expected)
            self.assertEqual(path.read_text(),original)

    def test_invalid_jsonl_exits_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="spec-cli-") as td:
            path=Path(td)/"broken.jsonl"
            path.write_text("not-json\n")
            run=subprocess.run(
                [sys.executable,"cli.py",str(path)],
                capture_output=True,text=True,timeout=15,
            )
            self.assertNotEqual(run.returncode,0)

if __name__=="__main__":
    unittest.main()
""",
    "test_worker_test_contract.py":r"""import ast
import pathlib
import unittest

class WorkerTestContract(unittest.TestCase):
    def test_worker_suite_contains_real_subprocess_cli_integration(self):
        path=pathlib.Path("tests/test_summary.py")
        self.assertTrue(path.is_file(),path)
        tree=ast.parse(path.read_text(encoding="utf-8"))
        imported=False
        subprocess_calls=0
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                imported=imported or any(a.name=="subprocess" for a in node.names)
            if isinstance(node,ast.ImportFrom) and node.module=="subprocess":
                imported=True
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
                if (
                    isinstance(node.func.value,ast.Name)
                    and node.func.value.id=="subprocess"
                    and node.func.attr in {
                        "run","Popen","check_call","check_output","call"
                    }
                ):
                    subprocess_calls+=1
        self.assertTrue(imported,"tests/test_summary.py must import subprocess")
        self.assertGreaterEqual(
            subprocess_calls,1,
            "tests/test_summary.py must execute the CLI through subprocess",
        )

if __name__=="__main__":
    unittest.main()
""",
}


def seed_specs(project:Path):
    target=project/"spec_tests"
    if target.exists():
        raise ValueError("trusted spec_tests directory already exists")
    target.mkdir()
    (target/"__init__.py").write_text("")
    for filename,content in SPEC_FILES.items():
        (target/filename).write_text(content.rstrip()+"\n",encoding="utf-8")
    manifest={
        "owner":"benchmark-author",
        "protocol":"v2-trusted-behavioral-tests-v1",
        "files":{
            filename:hashlib.sha256(
                (content.rstrip()+"\n").encode()
            ).hexdigest()
            for filename,content in SPEC_FILES.items()
        },
    }
    (project/".opencode-v2/work").mkdir(parents=True,exist_ok=True)
    (project/".opencode-v2/work/benchmark-trusted-specs.json").write_text(
        json.dumps(manifest,indent=2,sort_keys=True)+"\n"
    )


def structured_fixture():
    value=copy.deepcopy(medium.structured_fixture())
    leaves=value["leaves"]
    assert [x["key"] for x in leaves]==[
        "normalize","priority","summary","cli_tests","final_tests"
    ]
    for leaf,pattern in zip(leaves[:4],[
        "test_normalize.py","test_priority.py","test_summary.py","test_cli.py",
    ]):
        leaf["verify_command"]=(
            "python3 -m unittest spec_tests."+pattern.removesuffix(".py")+" -v"
        )
        leaf["done_when"]+=(
            " The immutable corresponding spec_tests functional checks pass."
        )
    worker_tests="python3 -m unittest discover -s tests -v"
    leaves[3]["verify_command"]+=(
        " && python3 -m unittest spec_tests.test_worker_test_contract -v"
        f" && {worker_tests}"
    )
    tick=chr(96)
    leaves[3]["done_when"]+=(
        f" Worker-authored tests also pass via {tick}{worker_tests}{tick}."
    )
    leaves[4]["outcome"]=(
        "Canonical TEST_CHECKS.json executes BOTH existing spec_tests and "
        "worker-authored tests without weakening either."
    )
    spec_suite="python3 -m unittest discover -s spec_tests -v"
    tick=chr(96)
    leaves[4]["done_when"]=(
        "The exact final runner executes and passes both "
        f"{tick}{spec_suite}{tick} and {tick}{worker_tests}{tick}."
    )
    return value


medium.BASE.structured_fixture=structured_fixture


def build(project:Path,task_file:Path)->dict:
    receipt=medium.BASE.build(
        project,task_file,
        expected_eligible=["D001","D002"],
        after_bootstrap=seed_specs,
    )
    return {"protocol":PROTOCOL,**receipt}


def selftest():
    with tempfile.TemporaryDirectory(prefix="medium-trusted-verify-") as td:
        root=Path(td);project=root/"project";project.mkdir()
        task=root/"task.md";task.write_text(TASK_TEXT)
        result=build(project,task)
        assert result["resume_phase"]=="execution"
        assert result["eligible"]==["D001","D002"]
        ctrl=project/".opencode-v2"
        manifest=json.loads((ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        assert len(manifest["leaves"])==5
        for leaf in manifest["leaves"].values():
            if leaf["id"]!="D005":
                assert "spec_tests" in leaf["verify_command"]
        trusted=json.loads((ctrl/"work/benchmark-trusted-specs.json").read_text())
        for filename,expected in trusted["files"].items():
            actual=hashlib.sha256((project/"spec_tests"/filename).read_bytes()).hexdigest()
            assert actual==expected
        print("adaptive medium trusted Verify fixture selftest: OK")


if __name__=="__main__":
    if len(sys.argv)==2 and sys.argv[1]=="--selftest":
        selftest()
    elif len(sys.argv)==3:
        print(json.dumps(build(Path(sys.argv[1]),Path(sys.argv[2])),indent=2))
    else:
        raise SystemExit("usage: create-adaptive-medium-verified-canary.py --selftest | PROJECT TASK_FILE")
