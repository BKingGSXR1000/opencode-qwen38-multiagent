#!/usr/bin/env python3
"""Sequential five-deliverable adaptive OFF/OBSERVE/ENFORCE comparison.

Kept separate from the initial short-task smoke benchmark. Includes held-out
behavioral checks authored before any model run and stored outside projects.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent


def load_script(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    value=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


benchmark=load_script("medium_benchmark_runner","benchmark-adaptive-healthy.py")
fixture=load_script("medium_coding_fixture","create-adaptive-medium-canary.py")
benchmark.fixture=fixture
benchmark.RUN_ROOT=Path("/home/bking/AI/a2-e2e/20261003-adaptive-medium-compare")
benchmark.PORTS={"off":58544,"observe":58545,"enforce":58546}
bench_root=benchmark.RUN_ROOT

HELDOUT=r'''import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import normalize
import priority
import summary

ROOT=Path.cwd()


class IndependentAcceptance(unittest.TestCase):
    def test_normalize_name(self):
        self.assertEqual(normalize.normalize_name("  Ada  "),"Ada")
        self.assertEqual(normalize.normalize_name(" B "),"B")
        for value in ("", "   ",None,2,False,[]):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    normalize.normalize_name(value)

    def test_priority_band_boundaries_and_boolean_rejection(self):
        for n,expected in [(1,"normal"),(2,"normal"),(3,"normal"),
                           (4,"high"),(5,"high")]:
            with self.subTest(n=n):
                self.assertEqual(priority.priority_band(n),expected)
        for value in (0,6,True,False,1.0,"4",None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    priority.priority_band(value)

    def test_summary_exact_sorted_and_input_immutable(self):
        rows=[
            {"name":" B ","priority":4},
            {"name":"A","priority":1},
            {"name":"B","priority":5},
        ]
        old=copy.deepcopy(rows)
        self.assertEqual(summary.summarize(rows),{
            "total":3,"high":2,"normal":1,"names":["A","B","B"],
        })
        self.assertEqual(rows,old)
        self.assertEqual(
            summary.summarize([
                {"name":"Zulu","priority":1},
                {"name":"Alpha","priority":5},
            ])["names"],
            ["Alpha","Zulu"],
        )
        self.assertEqual(summary.summarize([]),{
            "total":0,"high":0,"normal":0,"names":[],
        })

    def test_summary_invalid_records_raise_value_error(self):
        for rows in (
            [{"name":"A"}],
            [{"priority":4}],
            [None],
            [{"name":" ","priority":3}],
            [{"name":"A","priority":True}],
        ):
            with self.subTest(rows=repr(rows)):
                with self.assertRaises(ValueError):
                    summary.summarize(rows)

    def test_worker_authored_tests_use_subprocess_for_cli(self):
        import ast
        path=ROOT/"tests/test_summary.py"
        self.assertTrue(path.is_file())
        tree=ast.parse(path.read_text(encoding="utf-8"))
        calls=0
        for node in ast.walk(tree):
            if (
                isinstance(node,ast.Call)
                and isinstance(node.func,ast.Attribute)
                and isinstance(node.func.value,ast.Name)
                and node.func.value.id=="subprocess"
                and node.func.attr in {
                    "run","Popen","check_call","check_output","call"
                }
            ):
                calls+=1
        self.assertGreaterEqual(calls,1)

    def test_cli_compact_sorted_json_invalid_and_no_input_modification(self):
        with tempfile.TemporaryDirectory(prefix="heldout-medium-") as td:
            data=Path(td)/"rows.jsonl"
            original='{"name":" B ","priority":4}\n{"name":"A","priority":2}\n'
            data.write_text(original)
            run=subprocess.run([sys.executable,"cli.py",str(data)],
                cwd=ROOT,capture_output=True,text=True,timeout=14)
            self.assertEqual(run.returncode,0,run.stderr)
            expected=json.dumps({
                "total":2,"high":1,"normal":1,"names":["A","B"]
            },sort_keys=True,separators=(",",":"))+"\n"
            self.assertEqual(run.stdout,expected)
            self.assertEqual(data.read_text(),original)
            data.write_text("invalid-json\n")
            invalid=subprocess.run([sys.executable,"cli.py",str(data)],
                cwd=ROOT,capture_output=True,text=True,timeout=14)
            self.assertNotEqual(invalid.returncode,0)


if __name__=="__main__":
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(IndependentAcceptance)
    verdict=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if verdict.wasSuccessful() else 1)
'''


def run_heldout(project):
    import tempfile
    # The grader resides under a root-owned benchmark directory, never in
    # the child worker's file-ownership contract or project's source tree.
    with tempfile.TemporaryDirectory(prefix="adaptive-heldout-") as td:
        script=Path(td)/"heldout.py"
        script.write_text(HELDOUT)
        try:
            r=subprocess.run(
                [sys.executable,str(script)],cwd=project,
                env={**os.environ,"PYTHONPATH":str(project)},
                capture_output=True,text=True,timeout=65,
            )
            return {
                "pass":r.returncode==0,
                "returncode":r.returncode,
                "results":r.stdout[-2000:],
                "diagnostics":r.stderr[-3600:],
            }
        except subprocess.TimeoutExpired as exc:
            return {"pass":False,"error":"independent held-out timeout"}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--modes",nargs="+",choices=benchmark.MODES,
                    default=list(benchmark.MODES))
    ap.add_argument("--seconds-per-run",type=int,default=390)
    args=ap.parse_args()
    bench_root.mkdir(parents=True,exist_ok=True)
    summary=bench_root/"summary.json"
    if summary.exists():
        previous=json.loads(summary.read_text())
        if previous.get("protocol")!="adaptive-medium-healthy-v1":
            ap.error("existing summary belongs to a different protocol")
        results=list(previous["results"])
    else:
        results=[]
    if any((bench_root/mode).exists() for mode in args.modes):
        ap.error("refusing to overwrite existing medium coding result")
    for mode in args.modes:
        result=benchmark.run(mode,args.seconds_per_run)
        result["heldout"]=run_heldout(Path(result["project"]))
        benchmark.atomic_write_json(
            bench_root/mode/"result.json",result
        )
        results.append(result)
        benchmark.atomic_write_json(summary,{
            "protocol":"adaptive-medium-healthy-v1",
            "fixture":fixture.PROTOCOL,
            "same_guarded_plan":True,
            "run_order":[r["mode"] for r in results],
            "shared_backend":True,
            "results":results,
            "limitations":[
                "One medium-sized same-task run per mode: exploratory only.",
                "Qwen sampling trajectories and shared-GPU load are not fixed.",
                "Held-out functional tests are external to Qwen-owned project.",
            ],
        })
        print("MEDIUM_RESULT",mode,
              "ACCEPTANCE",result.get("accepted"),
              "MUSTS",str(result.get("musts_pass"))+"/"+str(result.get("musts_total")),
              "HELDOUT",result["heldout"]["pass"],
              "SECONDS",result["duration_seconds"],
              flush=True)
    print("MEDIUM_COMPARISON_FINISHED",flush=True)
    return 0 if all(r.get("accepted") and r["heldout"]["pass"]
                    for r in results) else 2


if __name__=="__main__":
    raise SystemExit(main())
