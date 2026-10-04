#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path
import structured_plan

spec=importlib.util.spec_from_file_location(
    "control_guard_external_test",
    Path(__file__).resolve().parent/"control-guard.py",
)
control_guard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(control_guard)


class ExternalAcquisitionDetectionTests(unittest.TestCase):
    def assert_no_acquisition(self,text):
        self.assertIsNone(structured_plan.external_acquisition_match(text))
        self.assertIsNone(control_guard.external_acquisition_match(text))

    def assert_active_acquisition(self,text):
        self.assertIsNotNone(structured_plan.external_acquisition_match(text))
        self.assertIsNotNone(control_guard.external_acquisition_match(text))

    def test_r4_local_vendored_and_negated_external_text_is_not_acquisition(self):
        text=(
            "A local HTTP server copies the already-frozen locally-vendored "
            "three.js file from the env_probe-established path into public/vendor/ "
            "(a local file copy, not a fresh external acquisition), with no build "
            "step or external-service prerequisite."
        )
        self.assert_no_acquisition(text)

    def test_preexisting_inputs_are_not_active_acquisition(self):
        for text in (
            "Use the already downloaded fixture from reference/data.json.",
            "Consume the previously-vendored library from public/vendor/.",
            "Read the pre-existing researched contract from probes/api.json.",
            "Use a locally-vendored three.js file.",
            "Work without external download; use the local asset.",
        ):
            with self.subTest(text=text):
                self.assert_no_acquisition(text)

    def test_active_external_acquisition_remains_fail_closed(self):
        for text in (
            "Download three.js from external CDN and save it locally.",
            "Vendor three.js from jsdelivr into public/vendor/.",
            "Fetch data from https://example.com/reference.json.",
            "Research the authoritative JPL Horizons endpoint now.",
            "curl https://example.com/data.json",
        ):
            with self.subTest(text=text):
                self.assert_active_acquisition(text)

    def test_negation_does_not_hide_separate_active_acquisition(self):
        text=(
            "Do not perform a fresh external acquisition for three.js; "
            "download ephemeris data from https://example.com/live.json."
        )
        self.assert_active_acquisition(text)


if __name__=="__main__":
    unittest.main()
