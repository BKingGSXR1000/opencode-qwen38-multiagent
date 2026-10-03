#!/usr/bin/env python3
"""Post-v6 medium benchmark with local unittest-target split validation."""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent


def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner=load("medium_verified_benchmark_runner_v7","benchmark-adaptive-medium.py")
fixture=load("medium_verified_fixture_v7","create-adaptive-medium-verified-canary.py")
runner.fixture=fixture
runner.benchmark.fixture=fixture
runner.benchmark.RUN_ROOT=Path(
    "/home/bking/AI/a2-e2e/20261003-adaptive-medium-contract-v7-compare"
)
runner.benchmark.PORTS={"off":58568,"observe":58569,"enforce":58570}
runner.bench_root=runner.benchmark.RUN_ROOT


if __name__=="__main__":
    raise SystemExit(runner.main())
