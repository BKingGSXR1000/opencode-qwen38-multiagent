#!/usr/bin/env python3
"""Browser evidence only for positive visual/UI acceptance obligations."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT=Path(__file__).with_name("finalize-acceptance.py")
SPEC=importlib.util.spec_from_file_location("finalize_acceptance_browser_tests",SCRIPT)
finalizer=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalizer)


def acceptance(must, *, original="", should=""):
    rows=["# Acceptance Contract",original,"Reference policy: internal","",
          "## MUST checks"]
    rows += [f"- [ ] A{i:03}: {text}" for i,text in enumerate(must,1)]
    rows += ["","## SHOULD"]
    if should:
        rows += ["- [ ] A099: "+should]
    rows += ["<!-- ACCEPTANCE_COMPLETE -->",""]
    return "\n".join(rows)


class BrowserEvidenceGateTests(unittest.TestCase):
    def test_offline_cli_web_prohibition_is_not_a_browser_app(self):
        plan=acceptance(
            ["An offline CLI must make no network/web calls.",
             "Only Python standard-library imports, not socket or urllib.",
             "The JSONL CLI command prints one line of compact sorted JSON."],
            original="Build a tiny offline application without any web or browser access.",
            should="Add visual polish if desired.",
        )
        self.assertFalse(finalizer.requires_browser_evidence(plan))

    def test_visual_browser_ui_and_canvas_must_require_browser_evidence(self):
        for requirement in (
            "Render an HTML canvas showing a moving sky.",
            "A browser-based interactive dashboard displays the result.",
            "The web page contains controls and a rendered 3D view.",
            "The UI must show responsive graphs.",
            "The WebGL scene uses Three.js rendering.",
        ):
            with self.subTest(requirement=requirement):
                self.assertTrue(finalizer.requires_browser_evidence(
                    acceptance([requirement])
                ))

    def test_only_optional_ui_does_not_turn_cli_into_browser(self):
        self.assertFalse(finalizer.requires_browser_evidence(acceptance(
            ["A command-line report prints valid JSON"],
            original="A visual UI might be added in a future project.",
            should="Optional dashboard and browser UI.",
        )))

    def test_negative_browser_language_does_not_require_pixels(self):
        self.assertFalse(finalizer.requires_browser_evidence(acceptance([
            "No browser access is permitted.",
            "The service operates without HTML, CSS or UI dependencies.",
            "It never makes any web calls.",
        ])))

    def test_explicit_canvas_must_wins_over_unrelated_network_prohibition(self):
        self.assertTrue(finalizer.requires_browser_evidence(acceptance([
            "No web calls or external CDN are permitted.",
            "The HTML canvas renders visible moving planets in the UI.",
        ])))

    def _project(self, root, must):
        c=root/".opencode-v2";c.mkdir()
        (c/"ACCEPTANCE.md").write_text(acceptance([must]))
        (c/"acceptance-report.json").write_text(json.dumps({
            "protocol":"v2-acceptance-report-v1","result":"PASS",
            "checks":[{"id":"A001","status":"PASS","evidence":"Independent exact file existence check passed.","required_executable":True,
            "command":"test -e .opencode-v2/ACCEPTANCE.md","exit_code":0}],
        }))
        (c/"TEST_REPORT.json").write_text(json.dumps({
            "protocol":"v2-test-report-v1","status":"pass",
            "checks_run":1,"checks_passed":1,"missing_required_files":[],
            "checks":[{
                "name":"exact-file-check",
                "command":"test -e .opencode-v2/ACCEPTANCE.md",
                "exit_code":0,"timed_out":False,
            }],
        }))
        return c

    def test_offline_cli_finalizer_mints_provenance_without_browser_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);c=self._project(root,"No network/web calls by CLI.")
            with mock.patch.object(finalizer,"run_validator_bash",return_value=0):
                marker=finalizer.finalize(root)
            self.assertEqual(marker["result"],"PASS")
            self.assertEqual(marker["must_count"],1)
            self.assertEqual(marker["browser_evidence_sha256"],"")
            self.assertIsNone(marker["browser_summary"])
            self.assertTrue((c/"acceptance-pass.json").exists())

    def test_visual_must_still_fails_closed_without_browser_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);c=self._project(root,"An HTML canvas shows a visual sky.")
            with mock.patch.object(finalizer,"run_validator_bash",return_value=0):
                with self.assertRaisesRegex(RuntimeError,"missing-browser-evidence"):
                    finalizer.finalize(root)
            self.assertFalse((c/"acceptance-pass.json").exists())


if __name__=="__main__":
    unittest.main()
