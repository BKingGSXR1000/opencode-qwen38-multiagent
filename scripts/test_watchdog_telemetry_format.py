#!/usr/bin/env python3
"""Historic literal-separator telemetry migration, no destructive rewrite."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import supervisor
import importlib.util
from pathlib import Path
_spec=importlib.util.spec_from_file_location('watchdog_report_script',Path(__file__).with_name('watchdog-report.py'))
watchdog_report_placeholder=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog_report_placeholder)


class WatchdogTelemetryFormatTests(unittest.TestCase):
    def test_writer_emits_actual_jsonl_not_backslash_n(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"events.jsonl"
            with mock.patch.object(supervisor,"WATCHDOG_TELEMETRY",path), \
                 mock.patch.object(supervisor,"watchdog_telemetry_last",{}):
                for i in range(2):
                    supervisor.emit_watchdog_telemetry(
                        "ses-a",{"session":"ses-a","test":i},
                        {"running":1},force=True,
                    )
            text=path.read_text()
            self.assertEqual(len(text.splitlines()),2)
            self.assertNotIn("\\n{",text)
            self.assertEqual([x["test"] for x in watchdog_report_placeholder.load_rows(path)],[0,1])

    def test_reader_handles_legacy_concatenation_and_mixed_new_rows(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"legacy.jsonl"
            rows=[
                {"session":"ses-a","reason":"a }\\n{ embedded diagnostic"},
                {"session":"ses-b","reason":"old backslash-separated"},
                {"session":"ses-c","reason":"new actual newline"},
            ]
            path.write_text(
                json.dumps(rows[0])+r"\n"+json.dumps(rows[1])+r"\n"
                +json.dumps(rows[2])+"\n"
            )
            result=watchdog_report_placeholder.load_rows(path)
            self.assertEqual(result,rows)

    def test_corrupt_row_skipped_while_next_valid_row_survives(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"corrupt.jsonl"
            row={"session":"ses-good","reason":"healthy"}
            p.write_text('NOT_JSON\\n'+json.dumps(row)+"\n")
            self.assertEqual(watchdog_report_placeholder.load_rows(p),[row])

    def test_report_groups_adaptive_candidates_by_episode(self):
        rows=[
            {
                "session":"ses-a","agent":"implementer","deliverable":"D001",
                "epoch":100+i*5,"watchdog_phase":"reasoning",
                "adaptive_reasoning_mode":"observe",
                "adaptive_reasoning":{
                    "profile":"NORMAL","gate":"budget" if bad else "within-budget",
                    "action_age":i*5,"reasoning_chars_since_action":i*400,
                    "abort":bad,"reason":"reasoning-budget-without-tool" if bad else "",
                },
            }
            for i,bad in enumerate((False,True,True,False,True))
        ]
        result=watchdog_report_placeholder.summarize(rows)
        self.assertEqual(len(result),1)
        report=result[0]["adaptive_reasoning"]
        self.assertEqual(report["mode"],"observe")
        self.assertEqual(report["profile"],"NORMAL")
        self.assertEqual(report["candidate_episodes"],2)
        self.assertEqual(report["max_action_age"],20)
        self.assertEqual(report["max_reasoning_chars_since_action"],1600)

    def test_cli_project_filter_excludes_other_canary_sessions(self):
        import contextlib
        import io
        import sys
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            log=root/"events.jsonl"
            log.write_text(
                json.dumps({"session":"ses-good","directory":"/project/good","epoch":1})+"\n"
                +json.dumps({"session":"ses-other","directory":"/project/other","epoch":2})+"\n"
            )
            saved=sys.argv
            out=io.StringIO()
            try:
                sys.argv=[
                    "watchdog-report.py","--log",str(log),
                    "--live",str(root/"no-live.json"),
                    "--db",str(root/"no-db.sqlite"),
                    "--project","/project/good","--json",
                ]
                with contextlib.redirect_stdout(out):
                    self.assertEqual(watchdog_report_placeholder.main(),0)
            finally:
                sys.argv=saved
            result=json.loads(out.getvalue())
            self.assertEqual([x["session"] for x in result["sessions"]],["ses-good"])

    def test_existing_record_without_session_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"mixed.jsonl"
            p.write_text(json.dumps({"unknown":1})+"\n"+json.dumps({"session":"ses-a"})+"\n")
            self.assertEqual(watchdog_report_placeholder.load_rows(p),[{"session":"ses-a"}])


if __name__ == "__main__":
    unittest.main()
