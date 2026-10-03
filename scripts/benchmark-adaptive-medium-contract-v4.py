#!/usr/bin/env python3
"""Post-fix medium benchmark: trusted Verify + dependency API handoff.

This v4 run is intentionally isolated from v3, which retained the historical
forced-contract-challenge failure after two ordinary Verify failures.
"""
import importlib.util
from pathlib import Path

HERE=Path(__file__).resolve().parent


def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner=load("medium_verified_benchmark_runner_v4","benchmark-adaptive-medium.py")
fixture=load("medium_verified_fixture_v4","create-adaptive-medium-verified-canary.py")
runner.fixture=fixture
runner.benchmark.fixture=fixture
runner.benchmark.RUN_ROOT=Path(
    "/home/bking/AI/a2-e2e/20261003-adaptive-medium-contract-v4-compare"
)
runner.benchmark.PORTS={"off":58559,"observe":58560,"enforce":58561}
runner.bench_root=runner.benchmark.RUN_ROOT


if __name__=="__main__":
    raise SystemExit(runner.main())
