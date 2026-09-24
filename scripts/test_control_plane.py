#!/usr/bin/env python3
"""Current deterministic OpenCode v1.18.31 / Stage-A regression entry point.

The pre-Stage-A beta-era suite is preserved at:
  scripts/legacy/control_plane_pre_stage_a_tests.py
It is intentionally not part of the active regression contract.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_stage_a_stabilization
import test_state_machine_invariants
import test_supervisor_live_status_atomic
import test_transport_root_no_continuation


def suite() -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    result = unittest.TestSuite()
    for module in (
        test_state_machine_invariants,
        test_stage_a_stabilization,
        test_supervisor_live_status_atomic,
        test_transport_root_no_continuation,
    ):
        result.addTests(loader.loadTestsFromModule(module))
    return result


if __name__ == "__main__":
    outcome = unittest.TextTestRunner(verbosity=2).run(suite())
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
