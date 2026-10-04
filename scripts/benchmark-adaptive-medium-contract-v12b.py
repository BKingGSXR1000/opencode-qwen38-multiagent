#!/usr/bin/env python3
"""Medium adaptive comparison after action-loop and dispatch-race hardening.

Same verified five-leaf fixture and independent held-out grader as v11.
The harness now includes bounded repeated action-guard denials, native child
materialization replay grace, and static rejection of dead-end read-only
split-tester unittest discovery commands.
"""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner=load("medium_verified_benchmark_runner_v12b","benchmark-adaptive-medium.py")
fixture=load("medium_verified_fixture_v12b","create-adaptive-medium-verified-canary.py")
runner.fixture=fixture
runner.benchmark.fixture=fixture
runner.benchmark.RUN_ROOT=Path(
    "/home/bking/AI/a2-e2e/20261004-adaptive-medium-contract-v12b-compare"
)
runner.benchmark.PORTS={"off":58588,"observe":58589,"enforce":58590}
runner.bench_root=runner.benchmark.RUN_ROOT

if __name__=="__main__":
    raise SystemExit(runner.main())
