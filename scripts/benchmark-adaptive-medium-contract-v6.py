#!/usr/bin/env python3
"""Post centralized split-dispatch fix medium benchmark."""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner=load("medium_verified_benchmark_runner_v6","benchmark-adaptive-medium.py")
fixture=load("medium_verified_fixture_v6","create-adaptive-medium-verified-canary.py")
runner.fixture=fixture
runner.benchmark.fixture=fixture
runner.benchmark.RUN_ROOT=Path(
    "/home/bking/AI/a2-e2e/20261003-adaptive-medium-contract-v6-compare"
)
runner.benchmark.PORTS={"off":58565,"observe":58566,"enforce":58567}
runner.bench_root=runner.benchmark.RUN_ROOT

if __name__=="__main__":
    raise SystemExit(runner.main())
