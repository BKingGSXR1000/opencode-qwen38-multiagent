#!/usr/bin/env python3
"""Deterministically guarded five-leaf, multi-file Stage-A coding fixture.

An isolated generic coding exercise, not a user-facing application product.
D001/D002 can execute concurrently; D003 integrates; D004 adds CLI, tests,
docs; D005 writes canonical final checks. Acceptance is independently verified.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location(
    "adaptive_medium_base_fixture",
    HERE/"create-restart-recovery-canary.py",
)
BASE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

PROTOCOL="v2-adaptive-medium-coding-canary-v1"

TASK_TEXT="""Build a standard-library-only, offline multicomponent Python record summarizer.

normalize.py: normalize_name(value) accepts a nonempty string, strips outer
whitespace, preserves case, and raises ValueError on invalid or blank input.

priority.py: priority_band(value) accepts only genuine integers 1-5 (bool is
invalid) and returns "high" for 4-5, "normal" for 1-3; raises ValueError otherwise.

summary.py: summarize(records) accepts an iterable of dictionaries, each with
name and priority. Validate via normalize_name and priority_band; malformed
records raise ValueError. Return a dict with total/high/normal counts and names
as a Python lexicographically sorted list of all normalized names (duplicates
retained). Do not mutate caller inputs.

cli.py: python3 cli.py INPUT.jsonl reads UTF-8 one JSON object per line and
prints exactly one compact sorted-key JSON object with the summarize result,
followed by a newline. Invalid data exits nonzero and input is never modified.

tests/test_summary.py: meaningful unittest coverage for every module, invalid
input, bool priority, mixed records, and one subprocess CLI integration test.
README.md: document the API, input format, one sample CLI invocation, and
exactly python3 -m unittest discover -s tests -v as the test command.

The deterministic final checks manifest must actually execute the existing
unittest suite. No third-party libraries, network use, external code or
unrelated files. This is a generic multi-agent coding harness benchmark.
"""


def acceptance_text():
    return """# Generic medium-sized coding benchmark acceptance

Reference policy: none

- [ ] A001: normalize_name strips nonempty string values and rejects missing/non-string or blank names.
- [ ] A002: priority_band accepts only integer 1-5, rejects bool, and returns the correct high/normal band.
- [ ] A003: summarize integrates normalization and priority classification without changing its inputs.
- [ ] A004: cli.py reads JSONL and prints compact sorted JSON; tests/test_summary.py and README.md are meaningful and present.
- [ ] A005: The final canonical TEST_CHECKS manifest executes the unit tests and passes.

<!-- ACCEPTANCE_COMPLETE -->
"""


def leaf(key,name,outcome,artifacts,deps,aid,complexity,verify,done,role="implementer",repeated=1):
    return {
        "key":key,
        "name":name,
        "outcome":outcome,
        "owned_artifacts":artifacts,
        "launch_deps":deps,
        "contract_deps":[],
        "verify_deps":deps,
        "acceptance_ids":[aid],
        "complexity":complexity,
        "repeated_operations":repeated,
        "deep_reasoning":False,
        "role":role,
        "verify_command":verify,
        "done_when":done,
    }


def structured_fixture():
    return {
        "protocol":"v2-structured-plan-v1",
        "status":"complete",
        "leaves":[
            leaf("normalize","Normalize record names",
                "normalize.py exports normalize_name for nonempty trimmed names.",
                ["normalize.py"],[],"A001","S",
                "python3 -m py_compile normalize.py",
                "Valid Python implementation of normalize_name."),
            leaf("priority","Classify integer priority",
                "priority.py exports priority_band with strict 1..5 validation.",
                ["priority.py"],[],"A002","S",
                "python3 -m py_compile priority.py",
                "Valid Python priority_band strict validation."),
            leaf("summary","Integrate summarization",
                "summary.py exports summarize combining normalization and priority.",
                ["summary.py"],["normalize","priority"],"A003","M",
                "python3 -c 'import summary; result=summary.summarize([dict(name=chr(65),priority=4)]); assert result.get(bytes((104,105,103,104)).decode()) == 1'",
                "Valid summarize returns total/high/normal/names and raises ValueError."),
            leaf("cli_tests","CLI, independent tests and documentation",
                "cli.py implements JSONL CLI; tests/test_summary.py verifies all modules and the CLI; README.md describes usage.",
                ["cli.py","tests/test_summary.py","README.md"],["summary"],
                "A004","M",
                "python3 -m unittest discover -s tests -v",
                "CLI, README and meaningful green unittest tests.",repeated=3),
            leaf("final_tests","Create final test manifest",
                "A canonical TEST_CHECKS.json executes the existing passing unittest suite.",
                [".opencode-v2/TEST_CHECKS.json"],["cli_tests"],
                "A005","S",".opencode-v2/bin/run-checks",
                "Canonical final checks manifest passes the test suite.",
                role="test-builder"),
        ],
    }


BASE.acceptance_text=acceptance_text
BASE.structured_fixture=structured_fixture


def build(project:Path,task_file:Path)->dict:
    result=BASE.build(project,task_file,expected_eligible=["D001","D002"])
    return {"protocol":"v2-adaptive-medium-coding-canary-v1",**result}


def selftest():
    with tempfile.TemporaryDirectory(prefix="adaptive-medium-fixture-") as td:
        root=Path(td)
        project=root/"project";project.mkdir()
        task=root/"task.md";task.write_text(TASK_TEXT)
        result=build(project,task)
        assert result["resume_phase"]=="execution",result
        assert set(result["eligible"])=={"D001","D002"},result
        guard=json.loads((project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").read_text())
        assert len(guard["leaves"])==5
        print("adaptive medium canary fixture selftest: OK")


if __name__=="__main__":
    if len(sys.argv)==2 and sys.argv[1]=="--selftest":
        selftest()
    elif len(sys.argv)==3:
        print(json.dumps(build(Path(sys.argv[1]),Path(sys.argv[2])),indent=2))
    else:
        raise SystemExit("usage: create-adaptive-medium-canary.py --selftest | PROJECT TASK_FILE")
