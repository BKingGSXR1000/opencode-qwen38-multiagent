#!/usr/bin/env python3
"""Post-v8 medium benchmark with bounded final-Acceptance remediation.

Uses the same generic trusted Verify + dependency API handoff fixture, now with
stronger lexicographic/subprocess evidence and one audited final-acceptance
repair credit per exhausted terminal leaf.
"""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner=load("medium_verified_benchmark_runner_v9","benchmark-adaptive-medium.py")
fixture=load("medium_verified_fixture_v9","create-adaptive-medium-verified-canary.py")
runner.fixture=fixture
runner.benchmark.fixture=fixture
runner.benchmark.RUN_ROOT=Path(
    "/home/bking/AI/a2-e2e/20261004-adaptive-medium-contract-v11-compare"
)
runner.benchmark.PORTS={"off":58580,"observe":58581,"enforce":58582}
runner.bench_root=runner.benchmark.RUN_ROOT

if __name__=="__main__":
    raise SystemExit(runner.main())
