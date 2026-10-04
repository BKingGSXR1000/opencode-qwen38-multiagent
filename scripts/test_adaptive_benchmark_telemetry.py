#!/usr/bin/env python3
from __future__ import annotations
import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location(
    "adaptive_benchmark_for_telemetry",
    HERE/"benchmark-adaptive-healthy.py",
)
bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)


class AdaptiveBenchmarkTelemetryTests(unittest.TestCase):
    def test_project_telemetry_does_not_call_zero_signal_evaluable(self):
        rows=[
            {
                "session":"s1","directory":"/project",
                "adaptive_reasoning_mode":"observe",
                "adaptive_reasoning":{
                    "profile":"NORMAL","gate":"no-reasoning",
                    "action_age":7.5,"reasoning_chars_since_action":0,
                    "abort":False,"reason":"",
                },
            }
        ]
        fake=type("M",(),{"load_rows":staticmethod(lambda path: rows)})
        with mock.patch.object(bench.importlib.util,"spec_from_file_location") as find,              mock.patch.object(bench.importlib.util,"module_from_spec",return_value=fake):
            find.return_value=mock.Mock()
            result=bench.project_telemetry(Path("/project"))
        self.assertFalse(result["adaptive_reasoning_evaluable"])
        self.assertEqual(
            result["adaptive_reasoning_evaluation_status"],
            "no-visible-reasoning-signal",
        )
        self.assertEqual(result["visible_reasoning_signal_records"],0)
        self.assertEqual(result["maximum_visible_reasoning_since_tool"],0)

    def test_project_telemetry_counts_signal_sessions(self):
        rows=[
            {
                "session":"s1","directory":"/project",
                "adaptive_reasoning_mode":"observe",
                "adaptive_reasoning":{
                    "profile":"NORMAL","gate":"within-budget",
                    "action_age":3,"reasoning_chars_since_action":500,
                    "abort":False,"reason":"",
                },
            },
            {
                "session":"s1","directory":"/project",
                "adaptive_reasoning_mode":"observe",
                "adaptive_reasoning":{
                    "profile":"NORMAL","gate":"within-budget",
                    "action_age":4,"reasoning_chars_since_action":700,
                    "abort":False,"reason":"",
                },
            },
        ]
        fake=type("M",(),{"load_rows":staticmethod(lambda path: rows)})
        with mock.patch.object(bench.importlib.util,"spec_from_file_location") as find,              mock.patch.object(bench.importlib.util,"module_from_spec",return_value=fake):
            find.return_value=mock.Mock()
            result=bench.project_telemetry(Path("/project"))
        self.assertTrue(result["adaptive_reasoning_evaluable"])
        self.assertEqual(result["visible_reasoning_signal_records"],2)
        self.assertEqual(result["visible_reasoning_signal_sessions"],1)
        self.assertEqual(result["maximum_visible_reasoning_since_tool"],700)


if __name__=="__main__":
    unittest.main()
