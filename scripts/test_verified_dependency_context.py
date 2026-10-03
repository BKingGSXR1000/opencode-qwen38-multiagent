#!/usr/bin/env python3
"""Verified dependency API handoff in materialized worker context."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import control_query_views
from control_policy import reexec_source_paths

class VerifiedDependencyContextTests(unittest.TestCase):
    def test_packet_publishes_bounded_verified_interfaces_to_downstream(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/".opencode-v2").mkdir()
            (project/".opencode-v2/ACCEPTANCE.md").write_text(
                "Reference policy: none\n- [ ] A001: one\n- [ ] A002: two\n"
            )
            manifest={"leaves":{
                "D001":{"name":"upstream","role":"implementer",
                    "owned_artifacts":"alpha.py",
                    "owned_artifact_paths":["alpha.py"],
                    "verify_deps":[],"launch_deps":[],
                    "acceptance_ids":["A001"]},
                "D002":{"name":"downstream","role":"implementer",
                    "owned_artifacts":"beta.py",
                    "owned_artifact_paths":["beta.py"],
                    "verify_deps":["D001"],"launch_deps":["D001"],
                    "acceptance_ids":["A002"]},
            }}
            upstream=[{"deliverable":"D001","verified_ready":True,
                       "python_interfaces":[{"path":"alpha.py",
                           "public_functions":["actual_api"],"sha256":"test"}]}]
            with mock.patch.object(
                control_query_views,"direct_verified_interfaces",
                side_effect=lambda p,m,leaf: upstream
                    if leaf.get("name")=="downstream" else [],
            ):
                packets=control_query_views.build_leaf_contexts(project,manifest)
            self.assertEqual(
                packets["D002"]["verified_dependency_interfaces"],upstream
            )
            self.assertEqual(packets["D001"]["verified_dependency_interfaces"],[])
            self.assertEqual(
                packets["D002"]["verify_dependency_artifacts"],
                {"D001":["alpha.py"]},
            )

    def test_runtime_fingerprint_includes_new_safe_projection_source(self):
        names={p.name for p in reexec_source_paths()}
        self.assertIn("dependency_interfaces.py",names)
        self.assertIn("control_query_views.py",names)

if __name__=="__main__":
    unittest.main()
