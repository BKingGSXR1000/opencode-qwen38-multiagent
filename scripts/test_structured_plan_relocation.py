#!/usr/bin/env python3
import copy,unittest
import structured_plan

class StructuredPlanReservedHelperRelocationTests(unittest.TestCase):
    def leaf(self,path,verify=None,role="feature-builder"):
        return {
            "key":"scene","role":role,
            "owned_artifacts":["public/app.js",path],
            "verify_command":verify if verify is not None else path,
            "outcome":"runtime helper "+path,
            "done_when":"execute "+path,
        }

    def test_python_check_helper_moves_to_tests_and_gets_interpreter(self):
        raw={"leaves":[self.leaf(".opencode-v2/bin/check_scene.py")]}
        out,changes=structured_plan.relocate_reserved_worker_helpers(raw)
        leaf=out["leaves"][0]
        self.assertEqual(
            leaf["owned_artifacts"],["public/app.js","tests/check_scene.py"]
        )
        self.assertEqual(leaf["verify_command"],"python3 tests/check_scene.py")
        self.assertIn("tests/check_scene.py",leaf["done_when"])
        self.assertEqual(changes,[{
            "key":"scene","from":".opencode-v2/bin/check_scene.py",
            "to":"tests/check_scene.py",
        }])

    def test_embedded_helper_path_is_replaced_without_rewriting_command_shape(self):
        raw={"leaves":[self.leaf(
            ".opencode-v2/bin/verify_scene.py",
            "python3 .opencode-v2/bin/verify_scene.py --strict",
        )]}
        out,_=structured_plan.relocate_reserved_worker_helpers(raw)
        self.assertEqual(
            out["leaves"][0]["verify_command"],
            "python3 tests/verify_scene.py --strict",
        )

    def test_run_checks_and_nonhelper_production_paths_are_never_relocated(self):
        for path in (
            ".opencode-v2/bin/run-checks",
            ".opencode-v2/bin/server.py",
            ".opencode-v2/bin/tool",
        ):
            raw={"leaves":[self.leaf(path)]}
            before=copy.deepcopy(raw)
            out,changes=structured_plan.relocate_reserved_worker_helpers(raw)
            self.assertEqual(out,before)
            self.assertEqual(changes,[])

    def test_helper_not_used_by_verify_is_left_for_normal_validation(self):
        raw={"leaves":[self.leaf(
            ".opencode-v2/bin/check_scene.py",
            "node public/app.js",
        )]}
        before=copy.deepcopy(raw)
        out,changes=structured_plan.relocate_reserved_worker_helpers(raw)
        self.assertEqual(out,before)
        self.assertEqual(changes,[])

    def test_read_only_tester_is_not_rewritten(self):
        raw={"leaves":[self.leaf(
            ".opencode-v2/bin/check_scene.py",
            role="tester",
        )]}
        before=copy.deepcopy(raw)
        out,changes=structured_plan.relocate_reserved_worker_helpers(raw)
        self.assertEqual(out,before)
        self.assertEqual(changes,[])

if __name__=="__main__":
    unittest.main()
