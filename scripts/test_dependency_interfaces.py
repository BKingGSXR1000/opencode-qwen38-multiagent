#!/usr/bin/env python3
"""Verified direct dependency API projection safety."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from dependency_interfaces import direct_verified_interfaces,python_public_interface,MAX_SOURCE_BYTES

class DependencyInterfaceTests(unittest.TestCase):
    def test_ast_names_and_digest_without_executing(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);raw=b"def zeta(x):\n return x+123\nasync def beta():\n pass\nclass Widget:pass\ndef _hidden():pass\n"
            (p/"mod.py").write_bytes(raw)
            d=python_public_interface(p,"mod.py")
            self.assertEqual(d["public_functions"],["beta","zeta"])
            self.assertEqual(d["public_classes"],["Widget"])
            self.assertEqual(d["sha256"],hashlib.sha256(raw).hexdigest())
            self.assertNotIn("x+123",str(d))

    def test_only_direct_verified_deps_and_bounded_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/"alpha.py").write_text("def actual_export():pass\n")
            (p/"beta.py").write_text("def wrong_export():pass\n")
            manifest={"leaves":{
                "D001":{"outcome":"x"*700,"done_when":"y"*800,"owned_artifact_paths":["alpha.py"]},
                "D002":{"outcome":"pending","owned_artifact_paths":["beta.py"]},
            }}
            leaf={"launch_deps":["D001"],"verify_deps":["D001","D002"]}
            r=direct_verified_interfaces(p,manifest,leaf,ready_lookup=lambda _p,d:d=="D001")
            self.assertEqual(len(r),1)
            self.assertEqual(r[0]["python_interfaces"][0]["public_functions"],["actual_export"])
            self.assertLessEqual(len(r[0]["outcome"]),420)
            self.assertLessEqual(len(r[0]["done_when"]),420)

    def test_no_attempt_ledger_never_claims_verified_interfaces(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            (p/"alpha.py").write_text("def alpha_fn():pass\n")
            manifest={"leaves":{"D001":{"owned_artifact_paths":["alpha.py"]}}}
            leaf={"launch_deps":["D001"]}
            self.assertEqual(direct_verified_interfaces(p,manifest,leaf),[])

    def test_symlink_traversal_syntax_and_size_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);root=p/"project";root.mkdir()
            outside=p/"outside.py";outside.write_text("def foreign():pass\n")
            (root/"alias.py").symlink_to(outside)
            (root/"broken.py").write_text("def broken(:\n")
            (root/"huge.py").write_bytes(b"x"*(MAX_SOURCE_BYTES+1))
            for path in ("../outside.py",str(outside),"alias.py","broken.py","huge.py"):
                with self.subTest(path=path):
                    self.assertEqual(python_public_interface(root,path),{})

if __name__=="__main__":
    unittest.main()
