#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import supervisor


M=supervisor.REFERENCE_FOUNDATION_MARKER


class ReferenceFoundationMarkerTests(unittest.TestCase):
    def test_exact_final_marker_is_valid(self):
        self.assertTrue(
            supervisor.reference_foundation_marker_complete(
                "# Foundation\nready\n"+M+"\n"
            )
        )

    def test_single_inline_code_wrapped_final_marker_is_valid(self):
        self.assertTrue(
            supervisor.reference_foundation_marker_complete(
                "# Foundation\nready\n"+"`"+M+"`\n"
            )
        )

    def test_marker_must_be_final_and_unique(self):
        for text in (
            M+"\ntrailing prose\n",
            M+"\n"+M+"\n",
            "`"+M+"`\n"+M+"\n",
            "```\n"+M+"\n```\n",
            "prefix "+M+" suffix\n",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    supervisor.reference_foundation_marker_complete(text)
                )

    def test_r7_shape_is_ready_even_at_foundation_attempt_limit(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            acceptance=ctrl/"acceptance"
            acceptance.mkdir(parents=True)
            (ctrl/"ACCEPTANCE.md").write_text(
                "# Acceptance\nReference policy: external-required\n"
            )
            (ctrl/"REFERENCE_FOUNDATION.md").write_text(
                "# Foundation\n`"+M+"`\n"
            )
            (acceptance/"reference-evidence.json").write_text(json.dumps({
                "result":"PARTIAL",
                "foundation_result":"READY",
                "missing":["later validation fixture"],
            }))
            old=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                with mock.patch.object(
                    supervisor,
                    "_reference_progress_stats",
                    return_value=(["s1","s2","s3"],[],3,0,[]),
                ):
                    gate=supervisor.reference_gate_snapshot()
            finally:
                supervisor.PROJECT=old
            self.assertEqual(gate["state"],"ready")
            self.assertEqual(gate["attempts"],3)
            self.assertEqual(gate["foundation_result"],"READY")


if __name__=="__main__":
    unittest.main()
