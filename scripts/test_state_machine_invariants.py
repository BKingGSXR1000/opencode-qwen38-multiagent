#!/usr/bin/env python3
import hashlib,json,multiprocessing,os,runpy,sqlite3,subprocess,sys,tempfile,unittest
from unittest import mock
from pathlib import Path
HERE=Path(__file__).resolve().parent; sys.path.insert(0,str(HERE))
import acceptance_contract,control_query_views,control_state,deterministic_dispatch,leaf_contract,stage_a_controller,structured_plan,supervisor,state_io,worker_sandbox,watchdog_telemetry

def ready_text(did,attempt=1,owner="supervisor",protocol=None,verify_command=""):
    protocol=protocol or control_state.LEAF_READY_PROTOCOL
    text=f"status=complete\ndeliverable={did}\nattempt={attempt}\nverified=true\nowner={owner}\nprotocol={protocol}\n"
    if verify_command:
        text+=f"verify_sha256={hashlib.sha256(verify_command.encode()).hexdigest()}\n"
    return text

def mark_phase_ready(project,artifact,marker):
    ctrl=Path(project)/".opencode-v2"
    path=ctrl/artifact
    if not path.exists():
        path.write_text(f"test {artifact}\n")
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ready_name=artifact.removesuffix(".md")+".ready"
    (ctrl/ready_name).write_text(
        "status=complete\n"
        f"protocol={control_state.PHASE_READY_PROTOCOL}\n"
        f"artifact={artifact}\nmarker={marker}\n"
        f"validated={control_state.ACCEPTANCE_READY_VALIDATOR if artifact=='ACCEPTANCE.md' else control_state.PHASE_READY_VALIDATOR}\n"
        f"artifact_sha256={digest}\n"
    )

class AcceptanceContractMustParsingTests(unittest.TestCase):
    def test_should_a_ids_are_not_promoted_when_must_section_exists(self):
        text=(
            "# Acceptance Contract\n## MUST checks\n"
            "- [ ] A001: required.\n- [ ] A002: required too.\n"
            "## SHOULD checks\n- [ ] A003: optional.\n"
        )
        self.assertEqual(acceptance_contract.must_acceptance_ids(text),["A001","A002"])
        self.assertEqual(stage_a_controller.acceptance_must_ids(text),["A001","A002"])

    def test_legacy_contract_without_must_heading_remains_supported(self):
        text="- [ ] A001: required.\n- [ ] A002: required too.\n"
        self.assertEqual(acceptance_contract.must_acceptance_ids(text),["A001","A002"])


class SharedLeafContractTests(unittest.TestCase):
    def test_role_semantics_fail_closed(self):
        self.assertTrue(leaf_contract.validate_leaf_contract("bogus",["a.txt"],"test -f a.txt"))
        self.assertTrue(leaf_contract.validate_leaf_contract("implementer",[],"test -f a.txt"))
        self.assertTrue(leaf_contract.validate_leaf_contract("tester",["a.txt"],"test -f a.txt"))
        self.assertTrue(leaf_contract.validate_leaf_contract("implementer",["a.txt"],"true"))
        self.assertEqual(leaf_contract.validate_leaf_contract("tester",[],"test -f a.txt"),[])
    def test_python_subprocess_stdout_requires_capture(self):
        bad=(
            "python3 -c \"import subprocess; "
            "p=subprocess.run(['printf','ok']); "
            "assert p.stdout.decode()=='ok'\""
        )
        errors=leaf_contract.validate_verify_command(bad)
        self.assertTrue(any("without capture_output=True" in e for e in errors),errors)

        good=(
            "python3 -c \"import subprocess; "
            "p=subprocess.run(['printf','ok'],stdout=subprocess.PIPE); "
            "assert p.stdout.decode()=='ok'\""
        )
        self.assertEqual(leaf_contract.validate_verify_command(good),[])

        text_bad=(
            "python3 -c \"import subprocess; "
            "p=subprocess.run(['printf','ok'],capture_output=True,text=True); "
            "assert p.stdout.decode()=='ok'\""
        )
        errors=leaf_contract.validate_verify_command(text_bad)
        self.assertTrue(any("text mode" in e for e in errors),errors)

    def test_behavioral_done_when_rejects_static_proxy_verify(self):
        errors=leaf_contract.validate_verify_adequacy(
            "tests/harness.js exports reusable utilities to drive the served app, "
            "load frozen fixtures, and assert acceptance properties.",
            "node --check tests/harness.js && test -s tests/harness.js",
        )
        self.assertTrue(errors)
        self.assertIn("static-proxy-only",errors[0])

    def test_behavioral_done_when_accepts_runtime_execution_verify(self):
        errors=leaf_contract.validate_verify_adequacy(
            "tests/harness.js drives the app and asserts acceptance properties.",
            "node tests/harness.js",
        )
        self.assertEqual(errors,[])

    def test_behavioral_done_when_accepts_runtime_in_command_substitution(self):
        command=(
            "out=$(python3 -m miniutils 'hello   world' 1 2 3) && "
            "[ \"$out\" = \"$(printf 'Text: hello world\\nCount: 3\\nMean: 2.00')\" ]"
        )
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(
                "python3 -m miniutils exits 0 and prints exactly the rendered output.",
                command,
            ),
            [],
        )
        quoted=(
            "expected='python3 -m miniutils hello 1 2 3'; "
            "test -n \"$expected\""
        )
        errors=leaf_contract.validate_verify_adequacy(
            "python3 -m miniutils exits 0 and prints exactly the rendered output.",
            quoted,
        )
        self.assertTrue(errors)
        self.assertIn("static-proxy-only",errors[0])

    def test_behavioral_done_when_accepts_python_c_runtime_execution(self):
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(
                "render_report returns exactly the required three-line output.",
                "python3 -c \"from app import render_report; assert render_report() == 'ok'\"",
            ),
            [],
        )
        errors=leaf_contract.validate_verify_adequacy(
            "render_report returns exactly the required three-line output.",
            "python3 -m py_compile app.py",
        )
        self.assertTrue(errors)
        self.assertIn("static-proxy-only",errors[0])

    def test_behavioral_done_when_accepts_python_module_test_runners(self):
        done=(
            "The unittest suite runs and passes, including a subprocess CLI "
            "test that asserts exit code 0 and exact stdout."
        )
        for command in (
            "python3 -m unittest discover -s tests",
            "python3 -m unittest discover -s tests -v",
            "python3 -m unittest tests.test_units -v",
            "python -m pytest -q",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    leaf_contract.validate_verify_adequacy(done,command),[]
                )
        for command in (
            "python3 -m py_compile tests/test_units.py",
            "python3 -m compileall -q miniutils",
        ):
            errors=leaf_contract.validate_verify_adequacy(done,command)
            self.assertTrue(errors)
            self.assertIn("static-proxy-only",errors[0])

    def test_test_runner_summary_presentation_is_not_a_behavior_contract(self):
        done=(
            "The unittest suite passes and covers normalization, numeric summary, "
            "report formatting, and CLI behavior."
        )
        bad=(
            "python3 -c \"import subprocess; "
            "r=subprocess.run(['python3','-m','unittest','discover','-s','tests'],"
            "capture_output=True,text=True); "
            "assert r.returncode==0 and r.stderr.endswith('OK (')\""
        )
        errors=leaf_contract.validate_verify_adequacy(done,bad)
        self.assertTrue(errors)
        self.assertIn("human-readable test-runner summary",errors[0])

        bare_ran=(
            "python3 -c \"import subprocess,sys; "
            "p=subprocess.run([sys.executable,'-m','unittest','discover','-v'],"
            "capture_output=True,text=True); "
            "assert p.returncode==0,p.stdout+p.stderr; "
            "assert 'Ran' in p.stdout\""
        )
        errors=leaf_contract.validate_verify_adequacy(done,bare_ran)
        self.assertTrue(errors)
        self.assertIn("human-readable test-runner summary",errors[0])

        good=(
            "python3 -c \"import subprocess; "
            "r=subprocess.run(['python3','-m','unittest','discover','-s','tests']); "
            "assert r.returncode==0\""
        )
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(done,good),
            [],
        )

    def test_explicit_runner_output_contract_may_check_presentation(self):
        done="unittest stderr must end with the literal summary line OK."
        command=(
            "python3 -c \"import subprocess; "
            "r=subprocess.run(['python3','-m','unittest','discover'],"
            "capture_output=True,text=True); "
            "assert r.returncode==0 and r.stderr.endswith('OK')\""
        )
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(done,command),
            [],
        )

    def test_server_done_when_requires_service_exercise_not_metadata_probe(self):
        errors=leaf_contract.validate_verify_adequacy(
            "npm start launches a localhost HTTP server that serves index.html.",
            """node --check server.js && node -e 'const p=require("./package.json"); if(!p.scripts.start)process.exit(1)'""",
        )
        self.assertTrue(any("server/service" in e for e in errors))

    def test_service_done_when_accepts_executable_test_script(self):
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(
                "node tests/state_tests.js launches the served localhost app and asserts HTTP 200.",
                "node tests/state_tests.js",
            ),
            [],
        )

    def test_nonbehavioral_file_contract_allows_static_verify(self):
        self.assertEqual(
            leaf_contract.validate_verify_adequacy(
                "app.txt exists and is non-empty.","test -s app.txt"
            ),
            [],
        )

    def test_reserved_control_state_rejected(self):
        paths,error=leaf_contract.strict_owned_artifact_paths("`.opencode-v2/work/D001.ready`")
        self.assertEqual(paths,[]); self.assertIn("supervisor-reserved",error)
    def test_overlap_split_ancestor_exception(self):
        good={"D001":{"owned_artifact_paths":["public/"]},"D001-A":{"parent":"D001","owned_artifact_paths":["public/a.js"]},"D001-B":{"parent":"D001","owned_artifact_paths":["public/b.js"]}}
        self.assertEqual(leaf_contract.ownership_overlap_errors(good),[])
        bad=dict(good); bad["D001-B"]={"parent":"D001","owned_artifact_paths":["public/a.js"]}
        self.assertTrue(leaf_contract.ownership_overlap_errors(bad))
        self.assertTrue(leaf_contract.ownership_overlap_errors({"D001":{"owned_artifact_paths":["src/"]},"D002":{"owned_artifact_paths":["src/app.js"]}}))

class StructuredPlanAcceptanceCoverageTests(unittest.TestCase):
    def base_plan(self):
        return {
            "protocol":structured_plan.PROTOCOL,
            "status":"complete",
            "leaves":[
                {
                    "key":"app",
                    "name":"app",
                    "outcome":"Create app.txt.",
                    "owned_artifacts":["app.txt"],
                    "launch_deps":[],
                    "contract_deps":[],
                    "verify_deps":[],
                    "acceptance_ids":["A001"],
                    "complexity":"S",
                    "repeated_operations":1,
                    "deep_reasoning":False,
                    "role":"implementer",
                    "verify_command":"test -f app.txt",
                    "done_when":"app.txt exists.",
                },
                {
                    "key":"final_tests",
                    "name":"final tests",
                    "outcome":"Run the canonical final test manifest.",
                    "owned_artifacts":[".opencode-v2/TEST_CHECKS.json"],
                    "launch_deps":["app"],
                    "contract_deps":["app"],
                    "verify_deps":[],
                    "acceptance_ids":["A003"],
                    "complexity":"S",
                    "repeated_operations":1,
                    "deep_reasoning":False,
                    "role":"test-builder",
                    "verify_command":structured_plan.RUN_CHECKS_COMMAND,
                    "done_when":"canonical final tests pass.",
                },
            ],
        }

    def make_project(self, base: Path) -> Path:
        project=base/"project"
        ctrl=project/".opencode-v2"
        ctrl.mkdir(parents=True)
        (ctrl/"ACCEPTANCE.md").write_text(
            "# Acceptance Contract\n"
            "- [ ] A001: app artifact exists.\n"
            "- [ ] A002: app behavior is correct.\n"
            "- [ ] A003: final tests pass.\n"
        )
        (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(
            json.dumps(self.base_plan())
        )
        return project

    def test_missing_must_id_fails_closed_with_whole_plan_repair(self):
        with tempfile.TemporaryDirectory() as td:
            project=self.make_project(Path(td))
            ok,errors=structured_plan.compile_plan(project)
            self.assertFalse(ok)
            coverage=[e for e in errors if e.get("code")=="acceptance-coverage"]
            self.assertEqual(len(coverage),1)
            self.assertIn("missing=A002",coverage[0]["message"])
            repair=json.loads(
                (project/".opencode-v2/IMPLEMENTATION_PLAN.repair.json").read_text()
            )
            self.assertTrue(repair["whole_plan"])
            self.assertEqual(repair["affected_keys"],[])

    def test_existing_leaf_can_absorb_missing_must_without_key_changes(self):
        with tempfile.TemporaryDirectory() as td:
            project=self.make_project(Path(td))
            path=project/".opencode-v2/IMPLEMENTATION_PLAN.structured.json"
            plan=json.loads(path.read_text())
            plan["leaves"][1]["acceptance_ids"]=["A002","A003"]
            path.write_text(json.dumps(plan))
            ok,errors=structured_plan.compile_plan(project)
            self.assertTrue(ok,errors)
            mapping=json.loads(
                (project/".opencode-v2/IMPLEMENTATION_PLAN.structured-map.json").read_text()
            )
            self.assertEqual(set(mapping["key_to_id"]),{"app","final_tests"})

    def test_should_ids_do_not_enter_structured_must_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            project=self.make_project(Path(td))
            ctrl=project/".opencode-v2"
            (ctrl/"ACCEPTANCE.md").write_text(
                "# Acceptance Contract\n"
                "## MUST checks\n"
                "- [ ] A001: app artifact exists.\n"
                "- [ ] A003: final tests pass.\n"
                "## SHOULD checks\n"
                "- [ ] A002: optional enhancement.\n"
            )
            ok,errors=structured_plan.compile_plan(project)
            self.assertTrue(ok,errors)


class ControlGuardAcceptanceCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard=runpy.run_path(str(HERE/"control-guard.py"))

    def test_exact_plan_acceptance_coverage_passes(self):
        leaves={
            "D001":{"acceptance_ids":["A001"]},
            "D002":{"acceptance_ids":["A002"]},
        }
        acceptance=(
            "- [ ] A001: first must.\n"
            "- [ ] A002: second must.\n"
        )
        self.assertEqual(
            self.guard["plan_acceptance_coverage_errors"](leaves,acceptance),[]
        )

    def test_missing_and_extra_plan_acceptance_ids_fail_closed(self):
        leaves={
            "D001":{"acceptance_ids":["A001","A999"]},
        }
        acceptance=(
            "- [ ] A001: first must.\n"
            "- [ ] A002: second must.\n"
        )
        errors=self.guard["plan_acceptance_coverage_errors"](leaves,acceptance)
        self.assertEqual(len(errors),1)
        self.assertIn("missing=A002",errors[0])
        self.assertIn("extra=A999",errors[0])

    def test_should_ids_do_not_enter_guard_must_coverage(self):
        leaves={"D001":{"acceptance_ids":["A001"]}}
        acceptance=(
            "## MUST checks\n- [ ] A001: required.\n"
            "## SHOULD checks\n- [ ] A002: optional.\n"
        )
        self.assertEqual(
            self.guard["plan_acceptance_coverage_errors"](leaves,acceptance),[]
        )

    def test_verify_path_provenance_rejects_missing_unowned_helper(self):
        extract=self.guard["verify_referenced_paths"]
        self.assertEqual(
            extract("node --check src/app.js && node tests/time_control_check.js"),
            ["src/app.js","tests/time_control_check.js"],
        )
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/"src").mkdir()
            (project/"src/app.js").write_text("ok\n")
            leaves={
                "D001":{
                    "owned_artifact_paths":["src/app.js"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":"node --check src/app.js && node tests/time_control_check.js",
                }
            }
            errors=self.guard["verify_path_provenance_errors"](project,leaves)
            self.assertEqual(len(errors),1)
            self.assertIn("tests/time_control_check.js",errors[0])
            self.assertIn("missing/unowned",errors[0])

    def test_split_handoff_verify_may_reference_own_supervisor_progress(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            leaves={
                "D009-A":{
                    "owned_artifact_paths":[],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "split_handoff_only":True,
                    "verify_command":(
                        "python3 -c \"from pathlib import Path; "
                        "t=Path('.opencode-v2/work/D009-A.progress.md').read_text(); "
                        "assert 'HANDOFF_READY: true' in t\""
                    ),
                }
            }
            self.assertEqual(
                self.guard["verify_path_provenance_errors"](project,leaves),[]
            )
            leaves["D009-A"]["verify_command"]=(
                "python3 -c \"from pathlib import Path; "
                "Path('.opencode-v2/work/D009-B.progress.md').read_text()\""
            )
            errors=self.guard["verify_path_provenance_errors"](project,leaves)
            self.assertEqual(len(errors),1)
            self.assertIn("D009-B.progress.md",errors[0])
            self.assertIn("missing/unowned",errors[0])

    def test_verify_path_provenance_extracts_inline_python_file_inputs(self):
        extract=self.guard["verify_referenced_paths"]
        self.assertEqual(
            extract(
                "python3 -c \"from pathlib import Path; "
                "print(Path('tests/helper.py').read_text()); "
                "print(open('README.md').read())\""
            ),
            ["tests/helper.py","README.md"],
        )
        self.assertEqual(
            extract(
                "python3 -c \"import glob; "
                "assert glob.glob('tests/*.py')\""
            ),
            ["tests/*.py"],
        )

    def test_inline_python_verify_requires_declared_dependency_owner(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/"tests").mkdir()
            (project/"src").mkdir()
            (project/"tests/helper.py").write_text("ok\n")
            leaves={
                "D001":{
                    "owned_artifact_paths":["src/app.py"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":(
                        "python3 -c \"print(open('tests/helper.py').read())\""
                    ),
                },
                "D002":{
                    "owned_artifact_paths":["tests/helper.py"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":"python3 tests/helper.py",
                },
            }
            errors=self.guard["verify_path_provenance_errors"](project,leaves)
            self.assertEqual(len(errors),1)
            self.assertIn("tests/helper.py",errors[0])
            self.assertIn("not a declared dependency",errors[0])
            leaves["D001"]["verify_deps"]=["D002"]
            self.assertEqual(
                self.guard["verify_path_provenance_errors"](project,leaves),[]
            )

    def test_inline_python_glob_expands_before_dependency_check(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/"tests").mkdir()
            (project/"src").mkdir()
            (project/"tests/helper.py").write_text("ok\n")
            leaves={
                "D001":{
                    "owned_artifact_paths":["src/app.py"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":(
                        "python3 -c \"import glob; "
                        "assert glob.glob('tests/*.py')\""
                    ),
                },
                "D002":{
                    "owned_artifact_paths":["tests/helper.py"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":"python3 tests/helper.py",
                },
            }
            errors=self.guard["verify_path_provenance_errors"](project,leaves)
            self.assertEqual(len(errors),1)
            self.assertIn("tests/helper.py",errors[0])
            leaves["D001"]["launch_deps"]=["D002"]
            self.assertEqual(
                self.guard["verify_path_provenance_errors"](project,leaves),[]
            )

    def test_verify_path_provenance_accepts_declared_dependency_owner(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            leaves={
                "D001":{
                    "owned_artifact_paths":["src/app.js"],
                    "launch_deps":["D002"],"contract_deps":[],"verify_deps":[],
                    "verify_command":"node tests/helper.js",
                },
                "D002":{
                    "owned_artifact_paths":["tests/helper.js"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":"test -s tests/helper.js",
                },
            }
            self.assertEqual(
                self.guard["verify_path_provenance_errors"](project,leaves),[]
            )

    def test_supervisor_validates_marker_even_when_not_final_line(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"ACCEPTANCE.md"
            marker="<!-- ACCEPTANCE_COMPLETE -->"
            path.write_text(
                "# Acceptance\n"
                + marker
                + "\n## Evidence Strategy\nmore text after marker\n"
            )
            self.assertTrue(
                supervisor.control_candidate_contains_marker(path,marker)
            )
            path.write_text("# Acceptance\nno marker yet\n")
            self.assertFalse(
                supervisor.control_candidate_contains_marker(path,marker)
            )

    def test_internal_reference_guard_distinguishes_negation_from_requirement(self):
        check=self.guard["internal_external_reference_violation"]
        self.assertFalse(check(
            "No external reference, network, or externally maintained source is\nrequired."
        ))
        self.assertFalse(check(
            "The contract contains no dependence on externally maintained truth."
        ))
        self.assertFalse(check(
            "No external authoritative source, measurement, or network call "
            "participates in the correctness claim."
        ))
        self.assertFalse(check(
            "The check does not require an externally maintained source."
        ))
        self.assertFalse(check(
            "The result is computed without an externally maintained source."
        ))
        self.assertFalse(check(
            "It does not use an official external dataset."
        ))
        self.assertTrue(check(
            "An externally maintained source is required for the expected truth."
        ))
        self.assertTrue(check(
            "No local fixture is needed, but an externally maintained source is required."
        ))


class RuntimePlanRepairTests(unittest.TestCase):
    def test_runtime_repair_requires_a_changed_affected_leaf(self):
        guard=runpy.run_path(str(HERE/"control-guard.py"))
        with tempfile.TemporaryDirectory() as td:
            ctrl=Path(td)/".opencode-v2"; ctrl.mkdir()
            leaf={"key":"producer","name":"before"}
            source={"protocol":"v2-structured-plan-v1","leaves":[leaf]}
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps(source))
            digest=hashlib.sha256(json.dumps(leaf,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
            (ctrl/"IMPLEMENTATION_PLAN.repair.json").write_text(json.dumps({
                "source":"runtime-split-parent-contract",
                "baseline":{"affected_leaf_sha256":{"producer":digest}},
            }))
            self.assertIn("no affected structured leaf changed",guard["runtime_repair_change_errors"](ctrl)[0])
            source["leaves"][0]["name"]="after"
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps(source))
            self.assertEqual(guard["runtime_repair_change_errors"](ctrl),[])

    def test_control_guard_mixed_keyed_and_global_errors_force_whole_plan_repair(self):
        guard=runpy.run_path(str(HERE/"control-guard.py"))
        with tempfile.TemporaryDirectory() as td:
            ctrl=Path(td)/".opencode-v2"
            ctrl.mkdir()
            (ctrl/"IMPLEMENTATION_PLAN.structured-map.json").write_text(
                json.dumps({"id_to_key":{"D005":"cli_main"}})
            )
            guard["write_plan_repair_packet"](
                ctrl,
                [
                    "D005: Verify Python reads subprocess.run().stdout without capture",
                    "plan acceptance_ids must cover every and only MUST Axxx IDs; missing=A013",
                ],
                source="control-guard",
            )
            packet=json.loads(
                (ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
            )
            self.assertTrue(packet["whole_plan"])
            self.assertEqual(packet["affected_keys"],[])
            self.assertEqual(len(packet["errors"]),2)
            self.assertEqual(packet["errors"][0]["keys"],["cli_main"])
            self.assertEqual(packet["errors"][1]["keys"],[])

    def test_control_policy_revalidation_freezes_baselines(self):
        guard=runpy.run_path(str(HERE/"control-guard.py"))
        with tempfile.TemporaryDirectory() as td:
            ctrl=Path(td)/".opencode-v2"
            work=ctrl/"work"
            work.mkdir(parents=True)
            leaf={"key":"cli_main","verify_command":"python3 -c 'assert False'"}
            source={"protocol":"v2-structured-plan-v1","leaves":[leaf]}
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(
                json.dumps(source)
            )
            (ctrl/"IMPLEMENTATION_PLAN.structured-map.json").write_text(
                json.dumps({"id_to_key":{"D005":"cli_main"}})
            )
            (work/"planner-restarts.json").write_text(json.dumps({
                "owner":"supervisor","count":3,"counted_sessions":["a","b","c"]
            }))
            errors=["D005: Verify Python reads subprocess.run().stdout without capture"]
            guard["write_plan_repair_packet"](
                ctrl,errors,source="control-policy-revalidation"
            )
            packet=json.loads(
                (ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
            )
            self.assertEqual(packet["source"],"control-policy-revalidation")
            self.assertEqual(packet["planner_restart_baseline"],3)
            self.assertIn(
                "cli_main",packet["baseline"]["affected_leaf_sha256"]
            )
            self.assertIn(
                "no affected structured leaf changed",
                guard["runtime_repair_change_errors"](ctrl)[0],
            )

            (work/"planner-restarts.json").write_text(json.dumps({
                "owner":"supervisor","count":4,
                "counted_sessions":["a","b","c","d"],
            }))
            guard["write_plan_repair_packet"](
                ctrl,errors,source="control-guard"
            )
            preserved=json.loads(
                (ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
            )
            self.assertEqual(
                preserved["source"],"control-policy-revalidation"
            )
            self.assertEqual(preserved["planner_restart_baseline"],3)

    def test_leaf_contract_challenge_requires_a_changed_affected_leaf(self):
        guard=runpy.run_path(str(HERE/"control-guard.py"))
        with tempfile.TemporaryDirectory() as td:
            ctrl=Path(td)/".opencode-v2"; ctrl.mkdir()
            leaf={"key":"text_module","verify_command":"python3 -c 'assert False'"}
            source={"protocol":"v2-structured-plan-v1","leaves":[leaf]}
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps(source))
            digest=hashlib.sha256(
                json.dumps(
                    leaf,sort_keys=True,separators=(",",":"),ensure_ascii=True
                ).encode()
            ).hexdigest()
            (ctrl/"IMPLEMENTATION_PLAN.repair.json").write_text(json.dumps({
                "source":"runtime-leaf-contract-challenge",
                "baseline":{"affected_leaf_sha256":{"text_module":digest}},
                "challenge":{
                    "reason":(
                        "Exact Verify requires whitespace-only input to become "
                        "one space, while Acceptance A003 requires the empty string."
                    )
                },
            }))
            errors=guard["runtime_repair_change_errors"](ctrl)
            self.assertIn("no affected structured leaf changed",errors[0])
            source["leaves"][0]["verify_command"]="python3 -c 'assert True'"
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps(source))
            self.assertEqual(guard["runtime_repair_change_errors"](ctrl),[])


class LeafContractChallengeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        self.old_log=supervisor.LOG
        supervisor.PROJECT=str(self.project)
        supervisor.LOG=self.project/"events.log"
        self.did="D001"
        self.sid="ses-contract-challenge"
        leaf={
            "id":self.did,
            "name":"text",
            "outcome":"normalize text",
            "owned_artifacts":"`miniutils/text.py`",
            "owned_artifact_paths":["miniutils/text.py"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "acceptance_ids":["A002","A003"],
            "complexity":"S","repeated_operations":1,
            "role":"implementer",
            "verify_command":"python3 -c \"assert False\"",
            "done_when":"Whitespace-only input normalizes to the empty string.",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:leaf},
        }))
        (self.ctrl/"IMPLEMENTATION_PLAN.structured-map.json").write_text(json.dumps({
            "protocol":"v2-structured-plan-map-v1",
            "id_to_key":{self.did:"text_module"},
        }))
        (self.ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps({
            "protocol":"v2-structured-plan-v1",
            "status":"complete",
            "leaves":[{
                "key":"text_module","verify_command":"python3 -c \"assert False\""
            }],
        }))
        (self.ctrl/"IMPLEMENTATION_PLAN.ready").write_text("stale-ready\n")

    def tearDown(self):
        supervisor.post_finalize_seen.discard(self.sid)
        supervisor.PROJECT=self.old_project
        supervisor.LOG=self.old_log
        self.tmp.cleanup()

    def test_parser_requires_one_bounded_single_line_challenge(self):
        self.assertEqual(
            supervisor.parse_contract_challenge(
                "CONTRACT_CHALLENGE: exact Verify requires a space but A003 requires empty."
            ),
            "exact Verify requires a space but A003 requires empty.",
        )
        self.assertEqual(
            supervisor.parse_contract_challenge("CONTRACT_CHALLENGE: too short"),
            "",
        )
        self.assertEqual(
            supervisor.parse_contract_challenge(
                "CONTRACT_CHALLENGE: first sufficiently long contradiction here\n"
                "CONTRACT_CHALLENGE: second sufficiently long contradiction here"
            ),
            "",
        )
        self.assertEqual(
            supervisor.parse_contract_challenge(
                "CONTRACT_CHALLENGE: None confirmed - the exact Verify failure "
                "appears to be an implementation issue, not a contradiction "
                "between Verify and Acceptance A003."
            ),
            "",
        )

    def test_verify_reporting_rule_rejects_underspecification_as_challenge(self):
        rule=supervisor.verify_reporting_rule()
        self.assertIn("less detail",rule)
        self.assertIn("NOT a contradiction",rule)
        self.assertIn("fix owned code",rule)
        self.assertIn("mutually exclusive",rule)

    def test_parser_accepts_explicit_cannot_both_hold_conflict(self):
        text=(
            "CONTRACT_CHALLENGE: verify_command asserts pure-newline input "
            "must be preserved, but Acceptance A002 requires leading/trailing "
            "whitespace stripped and newlines collapsed, so the two cannot "
            "both hold."
        )
        parsed=supervisor.parse_contract_challenge(text)
        self.assertIn("cannot both hold",parsed)
        self.assertEqual(
            leaf_contract.validate_contract_challenge_reason(parsed),[]
        )

    def test_parser_accepts_verify_command_cannot_pass_conflict(self):
        text=(
            "CONTRACT_CHALLENGE: verify_command calls the imported module object "
            "directly, and no implementation of miniutils/numbers.py can pass "
            "the exact unmodified verify_command."
        )
        parsed=supervisor.parse_contract_challenge(text)
        self.assertIn("no implementation",parsed)
        self.assertEqual(
            leaf_contract.validate_contract_challenge_reason(parsed),[]
        )

    def test_request_records_planner_restart_baseline(self):
        (self.work/"planner-restarts.json").write_text(json.dumps({
            "owner":"supervisor",
            "count":3,
            "counted_sessions":["p1","p2","p3"],
        }))
        ok,detail=supervisor.request_leaf_contract_challenge_repair(
            self.did,self.sid,
            "Exact Verify requires a newline but Acceptance A002 requires "
            "whitespace collapsed, so the two cannot both hold.",
        )
        self.assertTrue(ok,detail)
        repair=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
        )
        self.assertEqual(repair["planner_restart_baseline"],3)

    def test_request_rejects_negated_non_challenge(self):
        ok,detail=supervisor.request_leaf_contract_challenge_repair(
            self.did,self.sid,
            "None confirmed - the exact Verify failure appears to be an "
            "implementation issue, not a contradiction between Verify and "
            "Acceptance A003.",
        )
        self.assertFalse(ok)
        self.assertIn("contract-challenge-reason-invalid",detail)
        self.assertFalse(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").exists()
        )

    def test_stale_invalid_challenge_is_archived_and_cleared(self):
        reason=(
            "None confirmed - the exact Verify failure appears to be an "
            "implementation issue, not a contradiction between Verify and "
            "Acceptance A003."
        )
        repair={
            "protocol":"v2-structured-plan-repair-v1",
            "source":"runtime-leaf-contract-challenge",
            "whole_plan":False,
            "affected_keys":["text_module"],
            "errors":[],
            "baseline":{"affected_leaf_sha256":{"text_module":"abc"}},
            "challenge":{
                "deliverable":self.did,
                "session":self.sid,
                "acceptance_ids":["A002","A003"],
                "verify_sha256":"deadbeef",
                "reason":reason,
            },
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").write_text(
            json.dumps(repair)
        )
        challenge_path=self.work/f"{self.did}.contract-challenge.json"
        challenge_path.write_text(json.dumps({
            "owner":"supervisor",
            "protocol":"v2-leaf-contract-challenge-v1",
            "deliverable":self.did,
            "reason":reason,
        }))
        with mock.patch.object(
            supervisor,"_finalize_current_plan_without_rearm",
            return_value=True,
        ) as finalize:
            self.assertTrue(
                supervisor.reconcile_invalid_leaf_contract_challenge_repair()
            )
        finalize.assert_called_once_with()
        self.assertFalse(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").exists()
        )
        record=json.loads(challenge_path.read_text())
        self.assertEqual(record["state"],"rejected")
        self.assertTrue(record["validation_errors"])
        archives=list(
            self.work.glob(
                f"{self.did}.contract-challenge.rejected-*.json"
            )
        )
        self.assertEqual(len(archives),1)

    def test_control_guard_rejects_negated_runtime_challenge_packet(self):
        guard=runpy.run_path(str(HERE/"control-guard.py"))
        reason=(
            "None confirmed - the exact Verify failure is not a contradiction "
            "between Verify and Acceptance A003."
        )
        (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").write_text(json.dumps({
            "source":"runtime-leaf-contract-challenge",
            "baseline":{"affected_leaf_sha256":{"text_module":"abc"}},
            "challenge":{"deliverable":self.did,"reason":reason},
        }))
        errors=guard["runtime_repair_change_errors"](self.ctrl)
        self.assertTrue(errors)
        self.assertIn("runtime leaf contract challenge invalid",errors[0])

    def test_request_persists_targeted_repair_without_granting_retry(self):
        ok,detail=supervisor.request_leaf_contract_challenge_repair(
            self.did,self.sid,
            "Exact Verify requires whitespace-only input to become one space, "
            "while acceptance A003 requires the empty string.",
        )
        self.assertTrue(ok)
        self.assertEqual(detail,"contract-challenge-repair")
        repair=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
        )
        self.assertEqual(repair["source"],"runtime-leaf-contract-challenge")
        self.assertEqual(repair["affected_keys"],["text_module"])
        self.assertEqual(repair["challenge"]["deliverable"],self.did)
        self.assertEqual(repair["challenge"]["acceptance_ids"],["A002","A003"])
        self.assertFalse((self.ctrl/"IMPLEMENTATION_PLAN.ready").exists())
        self.assertTrue((self.work/f"{self.did}.contract-challenge.json").exists())

    def test_terminal_challenge_requires_failed_exact_verify(self):
        text=(
            "CONTRACT_CHALLENGE: exact Verify conflicts with authoritative "
            "acceptance behavior for whitespace-only input."
        )
        with mock.patch.object(
            supervisor,"last_assistant_text_db",return_value=text
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",return_value="not-attempted"
        ), mock.patch.object(
            supervisor,"request_leaf_contract_challenge_repair"
        ) as request:
            self.assertFalse(
                supervisor.handle_leaf_contract_challenge(
                    self.sid,"implementer",self.did
                )
            )
            request.assert_not_called()

    def test_terminal_challenge_requires_supervisor_failed_recheck(self):
        text=(
            "CONTRACT_CHALLENGE: exact Verify conflicts with authoritative "
            "acceptance behavior for whitespace-only input."
        )
        with mock.patch.object(
            supervisor,"last_assistant_text_db",return_value=text
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",return_value="failed"
        ), mock.patch.object(
            supervisor,"durable_worker_execution",return_value=True
        ), mock.patch.object(
            supervisor,"post_session_finalize",
            return_value=(False,"verify-failed-1")
        ), mock.patch.object(
            supervisor,"request_leaf_contract_challenge_repair",
            return_value=(True,"contract-challenge-repair")
        ) as request, mock.patch.object(
            supervisor,"meaningful_worker_execution",return_value=""
        ), mock.patch.object(
            supervisor,"worker_sandbox_cleanup_session"
        ):
            self.assertTrue(
                supervisor.handle_leaf_contract_challenge(
                    self.sid,"implementer",self.did
                )
            )
            request.assert_called_once()
            self.assertIn(self.sid,supervisor.post_finalize_seen)



    def _mock_terminal_recovery(self,later_tool=False):
        command="python3 -c \"assert False\""
        summary=(
            "Maximum steps for this agent have been reached.\n"
            "**Contract-conflict reasoning:**\n"
            f"- Verify command: {command}.\n"
            "Therefore, within the owned-file-only constraint, the exact Verify "
            "cannot pass unless the expected check is weakened, which "
            "contradicts the acceptance contract. This is a conflict between "
            "the verify command and contract authority's repair scope.\n"
        )
        guard={
            "type":"tool","tool":"edit",
            "state":{"status":"error","error":(
                "EARLY_WRITE_IMPLEMENTATION_CONTRACT_CHALLENGE_REQUIRED "
                f"session={self.sid} CONTRACT_CHALLENGE_REQUIRED "
                f"deliverable={self.did} exact_verify_failures=2 "
                "next_action=return-single-line-CONTRACT_CHALLENGE "
                "no_more_tools=true"
            )},
        }
        parts=[(json.dumps(guard),)]
        if later_tool:
            parts.append((json.dumps({
                "type":"tool","tool":"bash",
                "state":{"status":"completed"},
            }),))
        db=mock.Mock()
        db.execute.return_value.fetchall.return_value=parts
        patches=(
            mock.patch.object(supervisor,"max_step_terminal_summary_db",
                              return_value=summary),
            mock.patch.object(supervisor,"v1_runtime_enabled",
                              return_value=True),
            mock.patch.object(supervisor,"db_connect",return_value=db),
        )
        return patches

    def test_recursive_split_challenge_requires_explicit_parent_repair(self):
        with mock.patch.object(
            supervisor,"_structured_plan_symbolic_key",
            side_effect=ValueError("no structured-plan key for D001-A"),
        ):
            ok,detail=supervisor.request_leaf_contract_challenge_repair(
                self.did,self.sid,
                "verify_command cannot pass within the contract ownership "
                "scope without contradicting Acceptance A002.",
            )
        self.assertFalse(ok)
        self.assertEqual(
            detail,"contract-challenge-structured-key-missing"
        )
        self.assertFalse(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").exists()
        )

    def test_max_step_recovery_requires_persisted_guard_and_worker_conflict(self):
        patches=self._mock_terminal_recovery()
        with patches[0],patches[1],patches[2]:
            reason=supervisor.recover_max_step_contract_challenge_reason(
                self.sid,self.did
            )
        self.assertIn("verify_command",reason)
        self.assertIn("contract",reason)
        self.assertIn("cannot pass",reason)

    def test_max_step_recovery_rejects_later_tool(self):
        patches=self._mock_terminal_recovery(later_tool=True)
        with patches[0],patches[1],patches[2]:
            self.assertEqual(
                supervisor.recover_max_step_contract_challenge_reason(
                    self.sid,self.did
                ),"",
            )

    def test_max_step_recovery_handler_requests_targeted_repair(self):
        patches=self._mock_terminal_recovery()
        with patches[0],patches[1],patches[2],mock.patch.object(
            supervisor,"last_assistant_text_db",return_value=""
        ),mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ),mock.patch.object(
            supervisor,"durable_worker_execution",return_value=True
        ),mock.patch.object(
            supervisor,"post_session_finalize",
            return_value=(False,"verify-failed-1")
        ),mock.patch.object(
            supervisor,"meaningful_worker_execution",return_value=""
        ),mock.patch.object(
            supervisor,"worker_sandbox_cleanup_session"
        ):
            self.assertTrue(supervisor.handle_leaf_contract_challenge(
                self.sid,"implementer",self.did
            ))
        repair=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
        )
        self.assertEqual(repair["source"],"runtime-leaf-contract-challenge")
        self.assertIn("cannot pass",repair["challenge"]["reason"])
        self.assertIn(
            "LEAF_CONTRACT_CHALLENGE_MAX_STEP_RECOVERED",
            supervisor.LOG.read_text(),
        )



class NestedLeafContractChallengeTests(unittest.TestCase):
    """Synthetic handoff->writer->handoff->writer; never modify Proof2 assets."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        ctrl=self.root/".opencode-v2"
        (ctrl/"work").mkdir(parents=True)
        self.ctrl=ctrl
        self.did="D001-B2"
        self.sid="ses-nested-challenge"
        self.reason=(
            "verify_command exact checks conflict with the contract repair "
            "scope: README.md is missing a required string, but the terminal "
            "writer owns only miniutils/text.py and cannot pass its Verify "
            "without changing an upstream-owned artifact."
        )
        self.orig_project=supervisor.PROJECT
        self.orig_log=supervisor.LOG
        supervisor.PROJECT=str(self.root)
        supervisor.LOG=self.root/"events.log"
        self.verify="python3 -c \"assert False\""
        self.producer_verify="python3 -c \"assert True\""
        self.producer={
            "id":"D002","verify_command":self.producer_verify,
            "owned_artifact_paths":["README.md"],"role":"implementer",
            "launch_deps":[],"acceptance_ids":["A002"],
        }
        self.original={
            "id":"D001","verify_command":self.verify,
            "owned_artifact_paths":["miniutils/text.py"],"role":"test-builder",
            "launch_deps":["D002"],"acceptance_ids":["A002"],
            "split_children":["D001-A","D001-B"],
        }
        first={
            "id":"D001-A","split_handoff_only":True,
            "verify_command":"SUPERVISOR_HANDOFF_PROGRESS",
            "owned_artifact_paths":[],"launch_deps":["D002"],
        }
        second={
            "id":"D001-B","split_handoff_only":False,
            "split_handoff_source":"D001-A",
            "verify_command":self.verify,
            "owned_artifact_paths":["miniutils/text.py"],
            "launch_deps":["D002","D001-A"],
        }
        self.mid=dict(second,split_children=["D001-B1","D001-B2"])
        h1={
            "id":"D001-B1","split_handoff_only":True,
            "verify_command":"SUPERVISOR_HANDOFF_PROGRESS",
            "owned_artifact_paths":[],"launch_deps":["D002","D001-A"],
        }
        terminal={
            "id":"D001-B2","split_handoff_only":False,
            "split_handoff_source":"D001-B1",
            "verify_command":self.verify,
            "owned_artifact_paths":["miniutils/text.py"],
            "launch_deps":["D002","D001-A","D001-B1"],
        }
        self.manifest={
            "D001":self.original,"D002":self.producer,
            "D001-A":first,"D001-B":self.mid,
            "D001-B1":h1,"D001-B2":terminal,
        }
        self.txns={
            "D001":{
                "state":"committed","transaction_id":"outer",
                "children":["D001-A","D001-B"],
                "child_defs":{"D001-A":first,"D001-B":second},
            },
            "D001-B":{
                "state":"committed","transaction_id":"inner",
                "children":["D001-B1","D001-B2"],
                "child_defs":{"D001-B1":h1,"D001-B2":terminal},
            },
        }
        self.overlay={"parents":{
            k:{"transaction_id":t["transaction_id"],
               "children":t["children"],"child_defs":t["child_defs"]}
            for k,t in self.txns.items()
        }}
        (ctrl/"IMPLEMENTATION_PLAN.structured-map.json").write_text(json.dumps({
            "protocol":"v2-structured-plan-map-v1",
            "id_to_key":{"D001":"consumer","D002":"readme"},
        }))
        (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps({
            "protocol":"v2-structured-plan-v1",
            "leaves":[
                {"key":"consumer","verify_command":self.verify,
                 "owned_artifacts":["miniutils/text.py"]},
                {"key":"readme","verify_command":self.producer_verify,
                 "owned_artifacts":["README.md"]},
            ],
        }))

    def tearDown(self):
        supervisor.PROJECT=self.orig_project
        supervisor.LOG=self.orig_log
        self.tmp.cleanup()

    def _mock_lineage(self):
        return (
            mock.patch.object(supervisor,"load_manifest",return_value={"leaves":self.manifest}),
            mock.patch.object(supervisor,"load_split_transaction",
                              side_effect=lambda p:self.txns.get(p,{})),
            mock.patch.object(supervisor,"load_split_leaf_overlay",
                              return_value=self.overlay),
        )

    def test_nested_owner_resolution_requires_trusted_transaction_chain(self):
        patches=self._mock_lineage()
        with patches[0],patches[1],patches[2]:
            target,detail=supervisor._nested_contract_challenge_target(
                self.did,self.reason
            )
        self.assertEqual(detail,"")
        self.assertEqual(target["root"],"D001")
        self.assertEqual(target["producer"],"D002")
        self.assertEqual(target["producer_path"],"README.md")
        self.assertEqual(
            [x["transaction_id"] for x in target["lineage"]],
            ["outer","inner"],
        )

    def test_nested_owner_identity_survives_only_valid_upstream_verify_change(self):
        patches=self._mock_lineage()
        with patches[0],patches[1],patches[2]:
            old,err=supervisor._nested_contract_challenge_target(
                self.did,self.reason
            )
            self.assertEqual(err,"")
            self.manifest["D002"]["verify_command"]="python3 -c 'assert 1 + 1 == 2'"
            current,err=supervisor._nested_contract_challenge_target(
                self.did,self.reason
            )
        self.assertEqual(err,"")
        self.assertNotEqual(
            current["producer_verify_sha256"],old["producer_verify_sha256"]
        )
        self.assertEqual(
            {k:v for k,v in current.items() if k!="producer_verify_sha256"},
            {k:v for k,v in old.items() if k!="producer_verify_sha256"},
        )

    def test_nested_owner_resolution_rejects_tampered_split_overlay(self):
        self.overlay["parents"]["D001-B"]["transaction_id"]="foreign"
        patches=self._mock_lineage()
        with patches[0],patches[1],patches[2]:
            target,detail=supervisor._nested_contract_challenge_target(
                self.did,self.reason
            )
        self.assertFalse(target)
        self.assertEqual(detail,"split-challenge-lineage-invalid")

    def test_nested_owner_resolution_rejects_unmentioned_producer(self):
        patches=self._mock_lineage()
        with patches[0],patches[1],patches[2]:
            target,detail=supervisor._nested_contract_challenge_target(
                self.did,
                "verify_command cannot pass within the contract ownership scope",
            )
        self.assertFalse(target)
        self.assertEqual(detail,"split-challenge-upstream-owner-not-unique")

    def _prepare_nested_challenge_repair(self):
        patches=self._mock_lineage()
        with patches[0],patches[1],patches[2]:
            ok,detail=supervisor.request_leaf_contract_challenge_repair(
                self.did,self.sid,self.reason
            )
        self.assertTrue(ok,detail)
        repair=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").read_text()
        )
        self.assertEqual(repair["affected_keys"],["readme"])
        nested=repair["challenge"]["nested_repair"]
        self.assertEqual(nested["producer_path"],"README.md")
        marker=json.loads(
            (self.ctrl/"work"/f"{self.did}.contract-challenge.json").read_text()
        )
        self.assertEqual(marker["state"],"planner-pending")
        self.assertEqual(marker["nested_repair"]["root"],"D001")
        return nested

    def test_nested_challenge_produces_upstream_only_repair(self):
        self._prepare_nested_challenge_repair()

    def test_nested_guard_changes_only_producer_verify_and_preserves_final(self):
        self._prepare_nested_challenge_repair()
        guard=runpy.run_path(str(HERE/"control-guard.py"))
        check=guard["runtime_repair_change_errors"]
        self.assertIn("upstream producer",check(self.ctrl)[0])
        src=self.ctrl/"IMPLEMENTATION_PLAN.structured.json"
        data=json.loads(src.read_text())
        data["leaves"][1]["verify_command"]="python3 -c 'assert 1 + 1 == 2'"
        src.write_text(json.dumps(data))
        self.assertEqual(check(self.ctrl),[])
        data["leaves"][0]["verify_command"]="python3 -c 'assert True'"
        src.write_text(json.dumps(data))
        self.assertIn("original final Verify",check(self.ctrl)[0])
        data["leaves"][0]["verify_command"]=self.verify
        data["leaves"][1]["owned_artifacts"]=["foreign.md"]
        src.write_text(json.dumps(data))
        self.assertIn("ownership",check(self.ctrl)[0])

    def test_upgraded_producer_must_be_ready_before_nested_reverify(self):
        self._prepare_nested_challenge_repair()
        self.manifest["D002"]["verify_command"]="python3 -c 'assert 1 + 1 == 2'"
        patches=self._mock_lineage()
        with patches[0],patches[1],patches[2], mock.patch.object(
            supervisor,"plan_ready",return_value=True
        ),mock.patch.object(
            supervisor,"ready_info",side_effect=lambda did:did!="D002"
        ),mock.patch.object(
            supervisor,"_register_nested_dependency_repair"
        ) as authorize:
            self.assertEqual(
                supervisor.reconcile_nested_contract_challenge_repairs(),[]
            )
            authorize.assert_not_called()
        with patches[0],patches[1],patches[2], mock.patch.object(
            supervisor,"plan_ready",return_value=True
        ),mock.patch.object(
            supervisor,"ready_info",return_value=True
        ),mock.patch.object(
            supervisor,"_register_nested_dependency_repair",
            return_value=(True,"dependency-contract-reverify-authorized")
        ) as authorize:
            self.assertEqual(
                supervisor.reconcile_nested_contract_challenge_repairs(),
                [self.did],
            )
            authorize.assert_called_once()
            self.assertEqual(authorize.call_args.args[:2],(self.sid,self.did))
        marker=json.loads(
            (self.ctrl/"work"/f"{self.did}.contract-challenge.json").read_text()
        )
        self.assertEqual(marker["state"],"upstream-repaired")

    def test_dependency_contract_credit_is_bounded_by_unique_transition(self):
        import hashlib
        old="a"*64
        new="b"*64
        consumer="c"*64
        row={
            "protocol":"v2-dependency-contract-replacement-v1",
            "source":"supervisor-dependency-contract-repair",
            "attempt":4,"session":self.sid,"root":"D001","producer":"D002",
            "producer_previous_sha256":old,
            "producer_current_sha256":new,
            "consumer_verify_sha256":consumer,
            "previous_result":"verify-failed-1",
        }
        entry={"plan_contract_revisions":[row,dict(row)]}
        self.assertEqual(control_state._plan_contract_revision_credit_count(entry,4),1)
        invalid=dict(row,producer_current_sha256=old)
        self.assertEqual(control_state._plan_contract_revision_credit_count(
            {"plan_contract_revisions":[invalid]},4
        ),0)



class VersionSkewZeroWorkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        self.old_log=supervisor.LOG
        supervisor.PROJECT=str(self.project)
        supervisor.LOG=self.project/"events.log"
        leaf={
            "id":"D001","name":"owned","outcome":"repair owned",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":"test -s owned.txt",
            "role":"implementer","done_when":"owned exists",
            "acceptance_ids":["A001"],"parallel":"none",
            "split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9",
            "project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":leaf},
        }))
        mark_phase_ready(
            self.project,"IMPLEMENTATION_PLAN.md","IMPLEMENTATION_PLAN_COMPLETE"
        )
        self.sid="ses-version-skew"
        self.prompt=supervisor.implementation_runtime_prompt(
            "D001","implementer"
        )
        self.history=[
            {"attempt":1,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":2,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":3,"classification":"infrastructure",
             "reason":"immediate-runtime-cancel zero-token-zero-tool aborted"},
        ]
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":3,
                    "count":4,
                    "sessions":["s1","s2","s3",self.sid],
                    "failure_history":self.history,
                    "infrastructure_retry_grants":1,
                    "infrastructure_failures":[{
                        "timestamp":"2026-09-25T00:00:00Z",
                        "grant":1,
                        "source":"supervisor",
                        "kind":"runtime-cancel",
                        "session":"s3",
                        "evidence":"no-owned-artifact-or-progress",
                        "reason":"test",
                    }],
                    "unmaterialized_dispatch_sequence":4,
                    "unmaterialized_dispatch_replays":1,
                }
            },
        }))
        supervisor.LOG.write_text(
            f"DISPATCH_DENY session={self.sid} agent=implementer "
            "noncanonical_runtime_handoff\n"
            f"DISPATCH_ALLOW session={self.sid} agent=implementer "
            "deliverable=D001 attempt=4\n"
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.LOG=self.old_log
        self.tmp.cleanup()

    def test_rearms_same_zero_work_attempt_without_new_count(self):
        with mock.patch.object(
            supervisor,"meaningful_worker_execution",return_value=""
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=0
        ), mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value=self.prompt
        ):
            ok,detail=supervisor.recover_version_skew_zero_work_dispatch(
                "D001"
            )
        self.assertEqual((ok,detail),(True,"rearmed-same-attempt"))
        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        self.assertEqual(entry["count"],4)
        self.assertEqual(entry["failure_history"],self.history)
        self.assertTrue(entry["sessions"][-1].startswith(
            "dispatch:version-skew-recovery:"
        ))
        self.assertEqual(entry["unmaterialized_dispatch_sequence"],4)
        self.assertEqual(entry["unmaterialized_dispatch_replays"],0)
        self.assertEqual(
            entry["version_skew_zero_work_recoveries"][0]["session"],
            self.sid,
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])

    def test_refuses_when_current_attempt_has_meaningful_execution(self):
        with mock.patch.object(
            supervisor,"meaningful_worker_execution",return_value="owned"
        ):
            ok,detail=supervisor.recover_version_skew_zero_work_dispatch(
                "D001"
            )
        self.assertEqual(
            (ok,detail),
            (False,"version-skew-recovery-meaningful-execution-present"),
        )

    def test_infrastructure_abort_uses_attempt_delta_not_preexisting_file(self):
        (self.project/"owned.txt").write_text("preexisting\n")
        supervisor.write_execution_baseline("D001",4)
        self.assertFalse(supervisor.durable_worker_execution("D001",self.sid))
        with mock.patch.object(supervisor,"ready_info",return_value={}):
            ok,detail=supervisor.record_infrastructure_abort(
                self.sid,"D001","zero-work transport failure","child-binding-failure"
            )
        self.assertEqual((ok,detail),(True,"granted"))
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        self.assertEqual(
            entry["infrastructure_failures"][-1]["evidence"],
            "no-owned-artifact-or-progress",
        )
        self.assertTrue(control_state.attempt_state(entry)["valid"])

    def test_infrastructure_abort_repairs_preclaimed_successor_race(self):
        reason="child_compaction count=2; limit=1"
        entry={
            "automatic_limit":2,
            "count":4,
            "sessions":["s1","s2","s3","dispatch:next:D001"],
            "failure_history":[
                {"attempt":1,"classification":"infrastructure","reason":"infra1"},
                {"attempt":2,"classification":"infrastructure","reason":"infra2"},
                {
                    "attempt":3,
                    "classification":"infrastructure",
                    "reason":reason,
                    "session":"s3",
                },
            ],
            "infrastructure_retry_grants":2,
            "infrastructure_failures":[
                {
                    "timestamp":"2026-09-25T00:00:00Z","grant":1,
                    "source":"supervisor","kind":"runtime-cancel",
                    "session":"s1","evidence":"durable-partial-state-preserved",
                    "reason":"infra1",
                },
                {
                    "timestamp":"2026-09-25T00:01:00Z","grant":1,
                    "source":"supervisor","kind":"runtime-cancel",
                    "session":"s2","evidence":"durable-partial-state-preserved",
                    "reason":"infra2",
                },
            ],
        }
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{"D001":entry},
        }))
        self.assertFalse(control_state.attempt_state(entry)["valid"])
        with mock.patch.object(supervisor,"ready_info",return_value={}):
            ok,detail=supervisor.record_infrastructure_abort(
                "s3","D001",reason,"supervisor-compaction-retire"
            )
        self.assertEqual(
            (ok,detail),(True,"granted-preclaimed-successor-recovery")
        )
        repaired=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        state=control_state.attempt_state(repaired)
        self.assertTrue(state["valid"])
        self.assertEqual(repaired["count"],4)
        self.assertEqual(repaired["infrastructure_retry_grants"],3)
        self.assertEqual(len(repaired["failure_history"]),3)
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(repaired["infrastructure_failures"][-1]["session"],"s3")

    def test_late_existing_grant_reclassifies_genuine_failure_atomically(self):
        reason="max-step-short-marker-unrecognized-after-owned-post-verify-repair"
        entry={
            "automatic_limit":3,
            "count":4,
            "sessions":["s1","s2","s3","s4"],
            "failure_history":[
                {"attempt":1,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s1","source":"supervisor"},
                {"attempt":2,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s2","source":"supervisor"},
                {"attempt":3,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s3","source":"supervisor"},
                {"attempt":4,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s4","source":"supervisor"},
            ],
            "infrastructure_retry_grants":1,
            "infrastructure_failures":[{
                "timestamp":"2026-09-28T10:17:11Z","grant":1,
                "source":"supervisor","kind":"max-step-terminal-marker-variant",
                "session":"s1","evidence":"durable-partial-state-preserved",
                "reason":reason,
            }],
        }
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{"D001":entry},
        }))
        self.assertFalse(control_state.attempt_state(entry)["valid"])
        with mock.patch.object(supervisor,"ready_info",return_value={}),              mock.patch.object(supervisor,"durable_worker_execution",return_value=True):
            ok,detail=supervisor.record_infrastructure_abort(
                "s1","D001",reason,"max-step-terminal-marker-variant"
            )
        self.assertEqual((ok,detail),(True,"already-recorded-late-reclassified"))
        repaired=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        row=repaired["failure_history"][0]
        self.assertEqual(row["classification"],"infrastructure")
        self.assertEqual(row["original_classification"],"genuine")
        self.assertEqual(row["original_reason"],"verify-failed-1")
        self.assertEqual(repaired["infrastructure_retry_grants"],1)
        self.assertEqual(len(repaired["infrastructure_failures"]),1)
        self.assertTrue(control_state.attempt_state(repaired)["valid"])
        with mock.patch.object(supervisor,"ready_info",return_value={}),              mock.patch.object(supervisor,"durable_worker_execution",return_value=True):
            again=supervisor.record_infrastructure_abort(
                "s1","D001",reason,"max-step-terminal-marker-variant"
            )
        self.assertEqual(again,(False,"already-recorded"))
        same=json.loads((self.work/"attempts.json").read_text())["deliverables"]["D001"]
        self.assertEqual(same["infrastructure_retry_grants"],1)
        self.assertEqual(len(same["infrastructure_failures"]),1)

    def test_first_late_grant_reclassifies_existing_genuine_failure(self):
        reason="late transport evidence"
        entry={
            "automatic_limit":3,
            "count":3,
            "sessions":["s1","s2","s3"],
            "failure_history":[
                {"attempt":1,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s1","source":"supervisor"},
                {"attempt":2,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s2","source":"supervisor"},
                {"attempt":3,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s3","source":"supervisor"},
            ],
        }
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{"D001":entry},
        }))
        self.assertTrue(control_state.attempt_state(entry)["valid"])
        with mock.patch.object(supervisor,"ready_info",return_value={}),              mock.patch.object(supervisor,"durable_worker_execution",return_value=True):
            ok,detail=supervisor.record_infrastructure_abort(
                "s1","D001",reason,"runtime-cancel"
            )
        self.assertEqual((ok,detail),(True,"granted"))
        repaired=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        self.assertEqual(repaired["failure_history"][0]["classification"],"infrastructure")
        self.assertEqual(repaired["infrastructure_retry_grants"],1)
        self.assertTrue(control_state.attempt_state(repaired)["valid"])

    def test_late_reclassification_refuses_ambiguous_failure_history(self):
        reason="late transport evidence"
        entry={
            "automatic_limit":3,
            "count":1,
            "sessions":["s1"],
            "failure_history":[
                {"attempt":1,"classification":"genuine","reason":"verify-failed-1",
                 "session":"s1","source":"supervisor"},
                {"attempt":1,"classification":"genuine","reason":"verify-failed-2",
                 "session":"s1","source":"supervisor"},
            ],
            "infrastructure_retry_grants":1,
            "infrastructure_failures":[{
                "timestamp":"2026-09-28T10:00:00Z","grant":1,
                "source":"supervisor","kind":"runtime-cancel",
                "session":"s1","evidence":"durable-partial-state-preserved",
                "reason":reason,
            }],
        }
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{"D001":entry},
        }))
        with mock.patch.object(supervisor,"ready_info",return_value={}),              mock.patch.object(supervisor,"durable_worker_execution",return_value=True):
            ok,detail=supervisor.record_infrastructure_abort(
                "s1","D001",reason,"runtime-cancel"
            )
        self.assertFalse(ok)
        self.assertIn("multiple-failure-rows",detail)
        unchanged=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        self.assertEqual(
            [r["classification"] for r in unchanged["failure_history"]],
            ["genuine","genuine"],
        )


class SilentNonzeroWorkerFeedbackRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        self.old_log=supervisor.LOG
        supervisor.PROJECT=str(self.project)
        supervisor.LOG=self.project/"events.log"
        self.did="D001"
        self.sid="s2"
        self.verify="test -s owned.txt && false"
        (self.project/"owned.txt").write_text("unchanged\n")
        leaf={
            "id":self.did,"name":"owned","outcome":"owned correct",
            "owned_artifacts":"owned.txt","owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"runtime behavior passes","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:leaf},
        }))
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                self.did:{
                    "automatic_limit":2,"count":2,
                    "sessions":["s1",self.sid],
                    "failure_history":[
                        {"attempt":1,"classification":"genuine",
                         "reason":"verify-failed-1","session":"s1",
                         "source":"supervisor"},
                        {"attempt":2,"classification":"genuine",
                         "reason":"verify-failed-1","session":self.sid,
                         "source":"supervisor"},
                    ],
                    "split_required":{
                        "generation":1,
                        "reason":"genuine-failure-threshold",
                    },
                }
            },
        }))
        (self.work/f"{self.did}.verify-evidence.json").write_text(json.dumps({
            "owner":"supervisor",
            "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
            "deliverable":self.did,
            "entries":[],
            "latest":{
                "attempt":2,"session":self.sid,"command":self.verify,
                "executed":True,"exit_code":1,"result":"verify-failed-1",
                "stdout":"","stderr":"","error":"",
            },
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.LOG=self.old_log
        self.tmp.cleanup()

    def patches(self,output="(no output)"):
        return (
            mock.patch.object(supervisor,"v1_session_status_snapshot",return_value={}),
            mock.patch.object(supervisor,"_session_agent_db",return_value="implementer"),
            mock.patch.object(supervisor,"session_owned_mutation_seen",return_value=False),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged"),
            ),
            mock.patch.object(
                supervisor,"worker_sandbox_has_fatal_violation",return_value=False
            ),
            mock.patch.object(supervisor,"ownership_violations",return_value=[]),
            mock.patch.object(
                supervisor,"persisted_model_bash_commands",
                return_value=[{
                    "command":self.verify,"status":"completed","exit_code":1,
                    "output":output,"error":"",
                }],
            ),
            mock.patch.object(
                supervisor,"last_assistant_text_db",
                return_value="Exit code 0: exact Verify passed.",
            ),
            mock.patch.object(
                supervisor,"silent_nonzero_feedback_fix_installed",return_value=True
            ),
        )

    def test_rearms_same_attempt_and_clears_false_split(self):
        ps=self.patches()
        with ps[0],ps[1],ps[2],ps[3],ps[4],ps[5],ps[6],ps[7],ps[8]:
            self.assertEqual(
                supervisor.recover_silent_nonzero_worker_feedback(self.did),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],2)
        self.assertTrue(entry["sessions"][-1].startswith(
            "dispatch:silent-nonzero-recovery:"
        ))
        self.assertNotIn("split_required",entry)
        self.assertFalse(any(
            int(row.get("attempt") or 0)==2
            for row in entry.get("failure_history",[])
        ))
        recovery=entry["silent_nonzero_feedback_recoveries"][-1]
        self.assertEqual(recovery["verify_exit_code"],1)
        self.assertTrue(recovery["model_false_success_claim"])
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])

    def test_refuses_when_new_exit_marker_was_visible(self):
        ps=self.patches(output="V2_WORKER_COMMAND_EXIT=1")
        with ps[0],ps[1],ps[2],ps[3],ps[4],ps[5],ps[6],ps[7],ps[8]:
            self.assertEqual(
                supervisor.recover_silent_nonzero_worker_feedback(self.did),
                (False,"silent-nonzero-recovery-exit-marker-was-visible"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["sessions"][-1],self.sid)
        self.assertIn("split_required",entry)


class ImplementationMaxStepContinuationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        self.old_log=supervisor.LOG
        supervisor.PROJECT=str(self.project)
        supervisor.LOG=self.project/"events.log"
        self.did="D001"
        self.sid="s3"
        self.verify="node test.js"
        leaf={
            "id":self.did,"name":"owned","outcome":"owned correct",
            "owned_artifacts":"owned.js","owned_artifact_paths":["owned.js"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"runtime behavior passes","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:leaf},
        }))
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                self.did:{
                    "automatic_limit":2,
                    "count":3,
                    "sessions":["s1","s2",self.sid],
                    "failure_history":[
                        {"attempt":1,"classification":"genuine","reason":"verify-failed-1"},
                        {"attempt":2,"classification":"genuine","reason":"verify-failed-1"},
                        {"attempt":3,"classification":"genuine","reason":"verify-failed-1",
                         "session":self.sid,"source":"supervisor"},
                    ],
                    "operator_retry_grants":1,
                    "operator_overrides":[{
                        "grant":1,"source":"operator-cli",
                        "timestamp":"2026-09-26T00:00:00Z","reason":"retry",
                    }],
                    "operator_retry_attempts":[{
                        "sequence":3,"session":self.sid,
                        "state":"consumed","outcome":"meaningful_execution",
                        "consumes_operator_grant":True,
                        "evidence":"completed-worker-tool-action",
                        "source":"supervisor",
                    }],
                }
            },
        }))
        (self.work/f"{self.did}.verify-evidence.json").write_text(json.dumps({
            "owner":"supervisor",
            "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
            "deliverable":self.did,
            "entries":[],
            "latest":{
                "attempt":3,"session":self.sid,"command":self.verify,
                "executed":True,"exit_code":1,"result":"verify-failed-1",
                "stdout":"","stderr":"","error":"",
            },
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.LOG=self.old_log
        self.tmp.cleanup()

    def recovery_patches(self,last="Maximum steps for this agent have been reached."):
        return (
            mock.patch.object(supervisor,"v1_session_status_snapshot",return_value={}),
            mock.patch.object(supervisor,"last_assistant_text_db",return_value=last),
            mock.patch.object(supervisor,"durable_worker_execution",return_value=True),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(True,"changed")
            ),
            mock.patch.object(
                supervisor,"worker_sandbox_has_fatal_violation",return_value=False
            ),
            mock.patch.object(supervisor,"ownership_violations",return_value=[]),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=7
            ),
        )

    def test_rearms_same_exhausted_attempt_without_new_grant(self):
        patches=self.recovery_patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],3)
        self.assertEqual(entry["operator_retry_grants"],1)
        self.assertTrue(entry["sessions"][-1].startswith(
            "dispatch:max-step-continuation:"
        ))
        self.assertFalse(any(
            int(row.get("attempt") or 0)==3
            for row in entry.get("failure_history",[])
        ))
        op=entry["operator_retry_attempts"][-1]
        self.assertEqual(op["state"],"reserved")
        self.assertFalse(op["consumes_operator_grant"])
        self.assertEqual(
            op["rearmed_by"],
            supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
        )
        recovery=entry["implementation_max_step_continuations"][-1]
        self.assertEqual(recovery["attempt"],3)
        self.assertEqual(recovery["session"],self.sid)
        self.assertEqual(recovery["recovery_kind"],"initial")
        self.assertEqual(recovery["owned_state"],"changed")
        self.assertEqual(recovery["verify_result"],"verify-failed-1")
        progress=self.work/f"{self.did}.progress.md"
        self.assertTrue(progress.is_file())
        self.assertIn("Maximum steps for this agent have been reached.",progress.read_text())
        self.assertEqual(
            recovery["handoff_progress_sha256"],
            hashlib.sha256(progress.read_bytes()).hexdigest(),
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["operator_grants_reserved"],1)

    def test_rearms_nonoperator_attempt_without_operator_grant(self):
        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"][self.did]
        entry.pop("operator_retry_grants",None)
        entry.pop("operator_overrides",None)
        entry.pop("operator_retry_attempts",None)
        entry["plan_contract_revisions"]=[{
            "attempt":1,
            "source":"supervisor-plan-contract-revision",
            "previous_verify_sha256":"a"*64,
            "current_verify_sha256":"b"*64,
        }]
        entry["split_required"]={
            "generation":1,
            "reason":"genuine-failure-threshold",
        }
        (self.work/"attempts.json").write_text(json.dumps(data))
        patches=self.recovery_patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],3)
        self.assertNotIn("split_required",entry)
        self.assertNotIn("operator_retry_attempts",entry)
        recovery=entry["implementation_max_step_continuations"][-1]
        self.assertIsNone(recovery["original_operator_record"])
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["operator_grants_reserved"],0)

    def test_rearms_nonzero_verify_exit_two_with_matching_evidence(self):
        data=json.loads((self.work/"attempts.json").read_text())
        failure=data["deliverables"][self.did]["failure_history"][-1]
        failure["reason"]="verify-failed-2"
        (self.work/"attempts.json").write_text(json.dumps(data))
        evidence=json.loads(
            (self.work/f"{self.did}.verify-evidence.json").read_text()
        )
        evidence["latest"]["exit_code"]=2
        evidence["latest"]["result"]="verify-failed-2"
        (self.work/f"{self.did}.verify-evidence.json").write_text(
            json.dumps(evidence)
        )
        diagnostic="canonical stderr: unparseable numeric epoch"
        patches=self.recovery_patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did,diagnostic=diagnostic
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        recovery=entry["implementation_max_step_continuations"][-1]
        self.assertEqual(recovery["verify_result"],"verify-failed-2")
        self.assertEqual(recovery["verify_exit_code"],2)
        self.assertEqual(recovery["supervisor_diagnostic"],diagnostic)
        self.assertEqual(
            recovery["supervisor_diagnostic_sha256"],
            hashlib.sha256(diagnostic.encode()).hexdigest(),
        )
        progress=(self.work/f"{self.did}.progress.md").read_text()
        self.assertIn("EXACT_VERIFY_RESULT: verify-failed-2",progress)
        self.assertIn("SUPERVISOR_DIAGNOSTIC: "+diagnostic,progress)

    def test_auto_extracts_supervisor_verify_diagnostic_for_continuation(self):
        evidence=json.loads(
            (self.work/f"{self.did}.verify-evidence.json").read_text()
        )
        evidence["latest"]["stdout"]=json.dumps({
            "protocol":"v2-test-report-v1",
            "status":"fail",
            "checks_run":1,
            "checks_passed":0,
            "checks":[{
                "name":"stdlib-only imports",
                "exit_code":1,
                "timed_out":False,
                "diagnostic":{
                    "stdout":"non-stdlib: ./pkg/a.py:pkg",
                    "stderr":"",
                },
            }],
        })
        (self.work/f"{self.did}.verify-evidence.json").write_text(
            json.dumps(evidence)
        )
        patches=self.recovery_patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        recovery=entry["implementation_max_step_continuations"][-1]
        self.assertIn(
            "stdlib-only imports: stdout=non-stdlib: ./pkg/a.py:pkg",
            recovery["supervisor_diagnostic"],
        )
        progress=(self.work/f"{self.did}.progress.md").read_text()
        self.assertIn(
            "SUPERVISOR_DIAGNOSTIC: stdlib-only imports: "
            "stdout=non-stdlib: ./pkg/a.py:pkg",
            progress,
        )

    def test_allows_second_continuation_only_for_trusted_verify_visibility_bug(self):
        data=json.loads((self.work/"attempts.json").read_text())
        progress=self.work/f"{self.did}.progress.md"
        progress.write_text("first continuation handoff\n")
        data["deliverables"][self.did][
            "implementation_max_step_continuations"
        ]=[{
            "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
            "recovery_kind":"initial",
            "attempt":3,
            "session":"prior-session",
            "replacement":"dispatch:max-step-continuation:prior:D001",
            "handoff_progress_path":f".opencode-v2/work/{self.did}.progress.md",
            "handoff_progress_sha256":hashlib.sha256(
                progress.read_bytes()
            ).hexdigest(),
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        proof={
            "agent":"implementer",
            "trusted_exact_verify_failures":1,
            "post_verify_exact_required_denials":2,
            "post_verify_owned_edit_denials":1,
            "current_classifier_state":"failed",
            "owned_mutation_seen":False,
            "maximum_steps_reached":True,
        }
        patches=self.recovery_patches()
        with (
            patches[0],patches[1],patches[2],patches[3],
            patches[4],patches[5],patches[6],
            mock.patch.object(
                supervisor,"trusted_verify_reverify_visibility_evidence",
                return_value=proof,
            ),
        ):
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],3)
        self.assertEqual(len(entry["implementation_max_step_continuations"]),2)
        latest=entry["implementation_max_step_continuations"][-1]
        self.assertEqual(latest["recovery_kind"],"trusted-verify-visibility")
        self.assertEqual(latest["previous_continuation_session"],"prior-session")
        self.assertEqual(
            latest["trusted_verify_visibility_evidence"][
                "trusted_exact_verify_failures"
            ],
            1,
        )
        self.assertEqual(
            latest["trusted_verify_visibility_evidence"][
                "post_verify_exact_required_denials"
            ],
            2,
        )
        self.assertTrue(
            control_state.attempt_state(entry)["unmaterialized_dispatch_reusable"]
        )

    def test_allows_second_continuation_when_latest_verify_has_clean_tail(self):
        data=json.loads((self.work/"attempts.json").read_text())
        progress=self.work/f"{self.did}.progress.md"
        progress.write_text("first continuation handoff\n")
        data["deliverables"][self.did][
            "implementation_max_step_continuations"
        ]=[{
            "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
            "recovery_kind":"initial",
            "attempt":3,
            "session":"prior-session",
            "replacement":"dispatch:max-step-continuation:prior:D001",
            "handoff_progress_path":f".opencode-v2/work/{self.did}.progress.md",
            "handoff_progress_sha256":hashlib.sha256(
                progress.read_bytes()
            ).hexdigest(),
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        proof={
            "agent":"implementer",
            "trusted_exact_verify_failures":2,
            "post_verify_exact_required_denials":0,
            "post_verify_owned_edit_denials":0,
            "post_verify_owned_edits":1,
            "post_latest_verify_owned_edits":0,
            "current_classifier_state":"failed",
            "owned_mutation_seen":True,
            "maximum_steps_reached":True,
        }
        patches=self.recovery_patches()
        with (
            patches[0],patches[1],patches[2],patches[3],
            patches[4],patches[5],patches[6],
            mock.patch.object(
                supervisor,"trusted_verify_reverify_visibility_evidence",
                return_value=proof,
            ),
        ):
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],3)
        self.assertEqual(len(entry["implementation_max_step_continuations"]),2)
        latest=entry["implementation_max_step_continuations"][-1]
        self.assertEqual(latest["recovery_kind"],"latest-verify-clean-tail")
        self.assertEqual(latest["previous_continuation_session"],"prior-session")
        evidence=latest["latest_verify_clean_tail_evidence"]
        self.assertEqual(evidence["trusted_exact_verify_failures"],2)
        self.assertEqual(evidence["post_verify_owned_edits"],1)
        self.assertEqual(evidence["post_latest_verify_owned_edits"],0)
        self.assertTrue(
            control_state.attempt_state(entry)["unmaterialized_dispatch_reusable"]
        )

    def test_allows_third_continuation_only_for_reverify_single_write_cadence_bug(self):
        data=json.loads((self.work/"attempts.json").read_text())
        progress=self.work/f"{self.did}.progress.md"
        progress.write_text("second continuation handoff\n")
        data["deliverables"][self.did][
            "implementation_max_step_continuations"
        ]=[
            {
                "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
                "recovery_kind":"initial",
                "attempt":3,
                "session":"initial-session",
                "handoff_progress_path":f".opencode-v2/work/{self.did}.progress.md",
                "handoff_progress_sha256":"first",
            },
            {
                "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
                "recovery_kind":"trusted-verify-visibility",
                "attempt":3,
                "session":"visibility-session",
                "handoff_progress_path":f".opencode-v2/work/{self.did}.progress.md",
                "handoff_progress_sha256":hashlib.sha256(
                    progress.read_bytes()
                ).hexdigest(),
            },
        ]
        (self.work/"attempts.json").write_text(json.dumps(data))
        proof={
            "agent":"implementer",
            "trusted_exact_verify_failures":1,
            "post_verify_exact_required_denials":0,
            "post_verify_owned_edit_denials":0,
            "post_verify_owned_edits":2,
            "current_classifier_state":"failed",
            "owned_mutation_seen":True,
            "maximum_steps_reached":True,
        }
        patches=self.recovery_patches()
        with (
            patches[0],patches[1],patches[2],patches[3],
            patches[4],patches[5],patches[6],
            mock.patch.object(
                supervisor,"trusted_verify_reverify_visibility_evidence",
                return_value=proof,
            ),
        ):
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],3)
        history=entry["implementation_max_step_continuations"]
        self.assertEqual(len(history),3)
        latest=history[-1]
        self.assertEqual(
            latest["recovery_kind"],"reverify-single-write-cadence"
        )
        self.assertEqual(
            latest["previous_continuation_session"],"visibility-session"
        )
        self.assertEqual(
            latest["reverify_single_write_cadence_evidence"][
                "post_verify_owned_edits"
            ],
            2,
        )
        self.assertTrue(
            control_state.attempt_state(entry)["unmaterialized_dispatch_reusable"]
        )

    def test_refuses_second_continuation_for_same_attempt(self):
        data=json.loads((self.work/"attempts.json").read_text())
        progress=self.work/f"{self.did}.progress.md"
        progress.write_text("modern handoff\n")
        data["deliverables"][self.did][
            "implementation_max_step_continuations"
        ]=[{
            "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
            "attempt":3,"session":"prior",
            "handoff_progress_path":f".opencode-v2/work/{self.did}.progress.md",
            "handoff_progress_sha256":hashlib.sha256(progress.read_bytes()).hexdigest(),
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        self.assertEqual(
            supervisor.recover_implementation_max_step_continuation(self.did),
            (False,"max-step-continuation-limit"),
        )

    def test_legacy_missing_handoff_allows_one_compatibility_continuation(self):
        data=json.loads((self.work/"attempts.json").read_text())
        data["deliverables"][self.did][
            "implementation_max_step_continuations"
        ]=[{
            "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
            "attempt":3,"session":"legacy-prior",
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        patches=self.recovery_patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],3)
        history=entry["implementation_max_step_continuations"]
        self.assertEqual(len(history),2)
        latest=history[-1]
        self.assertEqual(latest["recovery_kind"],"legacy-missing-handoff")
        self.assertEqual(latest["previous_continuation_session"],"legacy-prior")
        progress=self.work/f"{self.did}.progress.md"
        self.assertTrue(progress.is_file())
        self.assertIn("RECOVERY_KIND: legacy-missing-handoff",progress.read_text())
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["operator_grants_reserved"],1)

    def test_max_step_marker_survives_later_text_only_compaction_tail(self):
        db_path=self.project/"parts.sqlite"
        con=sqlite3.connect(db_path)
        con.execute(
            "create table part (id text, session_id text, time_created integer, data text)"
        )
        con.executemany(
            "insert into part values (?,?,?,?)",
            [
                ("p1",self.sid,10,json.dumps({
                    "type":"text",
                    "text":"Maximum steps for this agent have been reached.\nrepair summary",
                })),
                ("p2",self.sid,20,json.dumps({
                    "type":"compaction","auto":True,
                })),
                ("p3",self.sid,30,json.dumps({
                    "type":"text","text":"post-compaction summary",
                })),
            ],
        )
        con.commit(); con.close()
        with (
            mock.patch.object(supervisor,"last_assistant_text_db",return_value="post-compaction summary"),
            mock.patch.object(supervisor,"v1_runtime_enabled",return_value=True),
            mock.patch.object(
                supervisor,"db_connect",
                side_effect=lambda: sqlite3.connect(db_path),
            ),
        ):
            summary=supervisor.max_step_terminal_summary_db(self.sid)
        self.assertIn("Maximum steps for this agent have been reached.",summary)
        self.assertIn("post-compaction summary",summary)

    def test_max_step_short_terminal_phrase_is_recognized(self):
        terminal=(
            "Max steps reached for this agent session.\n\n"
            "Summary of work done:\nowned repair completed"
        )
        with mock.patch.object(
            supervisor,"last_assistant_text_db",return_value=terminal
        ):
            self.assertEqual(
                supervisor.max_step_terminal_summary_db(self.sid),terminal
            )
        self.assertTrue(supervisor.MAX_STEP_TERMINAL_RE.search(terminal))

    def test_max_step_marker_is_not_terminal_if_later_tool_executes(self):
        db_path=self.project/"parts-later-tool.sqlite"
        con=sqlite3.connect(db_path)
        con.execute(
            "create table part (id text, session_id text, time_created integer, data text)"
        )
        con.executemany(
            "insert into part values (?,?,?,?)",
            [
                ("p1",self.sid,10,json.dumps({
                    "type":"text",
                    "text":"Maximum steps for this agent have been reached.",
                })),
                ("p2",self.sid,20,json.dumps({
                    "type":"tool","tool":"bash","state":{"status":"completed"},
                })),
                ("p3",self.sid,30,json.dumps({
                    "type":"text","text":"later work summary",
                })),
            ],
        )
        con.commit(); con.close()
        with (
            mock.patch.object(supervisor,"last_assistant_text_db",return_value="later work summary"),
            mock.patch.object(supervisor,"v1_runtime_enabled",return_value=True),
            mock.patch.object(
                supervisor,"db_connect",
                side_effect=lambda: sqlite3.connect(db_path),
            ),
        ):
            self.assertEqual(supervisor.max_step_terminal_summary_db(self.sid),"")

    def test_refuses_without_max_step_terminal(self):
        patches=self.recovery_patches(last="ordinary completion")
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_implementation_max_step_continuation(
                    self.did
                ),
                (False,"max-step-continuation-no-max-step-terminal"),
            )


class ControllerReplayEvidenceZeroWorkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        self.old_log=supervisor.LOG
        supervisor.PROJECT=str(self.project)
        supervisor.LOG=self.project/"events.log"
        self.sid="ses-controller-replay-zero"
        leaf={
            "id":"D001","name":"owned","outcome":"owned correct",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":"test -s owned.txt",
            "role":"implementer","done_when":"owned exists",
            "acceptance_ids":["A001"],"parallel":"none",
            "split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9",
            "project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":leaf},
        }))
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":2,
                    "count":3,
                    "sessions":["s1","s2",self.sid],
                    "failure_history":[
                        {"attempt":1,"classification":"genuine","reason":"g1"},
                        {"attempt":2,"classification":"genuine","reason":"g2"},
                    ],
                    "operator_retry_grants":1,
                    "operator_overrides":[{
                        "grant":1,"source":"operator-cli",
                        "timestamp":"2026-09-26T00:00:00Z",
                        "reason":"operator retry",
                    }],
                    "operator_retry_attempts":[{
                        "sequence":3,
                        "session":self.sid,
                        "state":"infrastructure_blocked",
                        "outcome":"infrastructure_abort",
                        "consumes_operator_grant":False,
                        "source":"supervisor",
                        "evidence":"immediate-runtime-cancel zero-token-zero-tool aborted",
                        "resolved_at":"2026-09-26T00:01:00Z",
                        "timestamp":"2026-09-26T00:00:30Z",
                    }],
                }
            },
        }))
        supervisor.LOG.write_text(
            f"INTERRUPT session={self.sid} agent=implementer "
            "reason=dispatch_guard attempt_ledger_invalid "
            "deliverable=D001 count=3\n"
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.LOG=self.old_log
        self.tmp.cleanup()

    def patches(self,meaningful=""):
        return (
            mock.patch.object(
                supervisor,"v1_session_status_snapshot",return_value={}
            ),
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"meaningful_worker_execution",
                return_value=meaningful
            ),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=0
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
            mock.patch.object(
                supervisor,"controller_replay_evidence_fix_installed",
                return_value=True
            ),
        )

    def test_rearms_same_blocked_zero_work_sequence_without_new_grant(self):
        patches=self.patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5]:
            self.assertEqual(
                supervisor.recover_controller_replay_evidence_zero_work(
                    "D001"
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        self.assertEqual(entry["count"],3)
        self.assertTrue(entry["sessions"][-1].startswith(
            "dispatch:controller-replay-recovery:"
        ))
        op=entry["operator_retry_attempts"][0]
        self.assertEqual(op["sequence"],3)
        self.assertEqual(op["state"],"reserved")
        self.assertFalse(op["consumes_operator_grant"])
        recovery=entry["controller_replay_evidence_recoveries"][0]
        self.assertEqual(recovery["session"],self.sid)
        self.assertEqual(
            recovery["original_operator_record"]["state"],
            "infrastructure_blocked",
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["count"],3)

    def test_refuses_when_blocked_attempt_did_real_work(self):
        patches=self.patches(meaningful="completed-worker-tool-action")
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5]:
            ok,detail=supervisor.recover_controller_replay_evidence_zero_work(
                "D001"
            )
        self.assertEqual(
            (ok,detail),
            (
                False,
                "controller-replay-recovery-meaningful-execution-present",
            ),
        )


class V2612OperatorRetryCompatibilityTests(unittest.TestCase):
    def entry(self):
        return {
            "automatic_limit":2,
            "count":5,
            "sessions":["s1","s2","s3","s4","s5"],
            "failure_history":[
                {"attempt":1,"classification":"infrastructure","reason":"infra1"},
                {"attempt":2,"classification":"infrastructure","reason":"infra2"},
                {"attempt":3,"classification":"infrastructure","reason":"infra3"},
                {"attempt":4,"classification":"genuine","reason":"false-old-failure"},
            ],
            "infrastructure_retry_grants":3,
            "infrastructure_failures":[
                {
                    "timestamp":"2026-09-25T00:00:00Z","grant":1,
                    "source":"supervisor","kind":"runtime-cancel",
                    "session":"s1","evidence":"durable-partial-state-preserved",
                    "reason":"infra1",
                },
                {
                    "timestamp":"2026-09-25T00:01:00Z","grant":1,
                    "source":"supervisor","kind":"runtime-cancel",
                    "session":"s2","evidence":"durable-partial-state-preserved",
                    "reason":"infra2",
                },
                {
                    "timestamp":"2026-09-25T00:02:00Z","grant":1,
                    "source":"supervisor","kind":"runtime-cancel",
                    "session":"s3","evidence":"durable-partial-state-preserved",
                    "reason":"infra3",
                },
            ],
            "operator_retry_grants":1,
            "operator_overrides":[{
                "timestamp":"2026-09-25T00:03:00Z",
                "grant":1,
                "source":"operator-cli",
                "reason":"restore retry lost to fixed harness failure",
            }],
        }

    def test_repaired_infrastructure_ledger_accepts_auditable_unused_operator_grant(self):
        state=control_state.attempt_state(self.entry())
        self.assertTrue(state["valid"],state)
        self.assertTrue(state["v2612_infrastructure_repair"])
        self.assertEqual(state["allowed_attempts"],6)
        self.assertEqual(state["operator_retry_grants"],1)
        self.assertEqual(state["operator_grants_remaining"],1)
        self.assertFalse(state["operator_authorized_attempt"])

    def test_repaired_infrastructure_ledger_accepts_reserved_operator_attempt(self):
        entry=self.entry()
        entry["failure_history"].append(
            {"attempt":5,"classification":"genuine","reason":"functional-failed"}
        )
        entry["count"]=6
        entry["sessions"].append("s6")
        entry["operator_retry_attempts"]=[{
            "sequence":6,
            "session":"s6",
            "state":"reserved",
            "consumes_operator_grant":False,
            "source":"supervisor",
            "timestamp":"2026-09-25T00:04:00Z",
        }]
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"],state)
        self.assertEqual(state["allowed_attempts"],6)
        self.assertEqual(state["operator_grants_reserved"],1)
        self.assertEqual(state["operator_grants_remaining"],0)
        self.assertTrue(state["operator_authorized_attempt"])

    def test_repaired_infrastructure_ledger_accepts_refunded_operator_abort(self):
        entry=self.entry()
        entry["operator_retry_attempts"]=[{
            "sequence":5,
            "session":"s5",
            "state":"infrastructure_abort",
            "outcome":"infrastructure_abort",
            "consumes_operator_grant":False,
            "evidence":"late supervisor infrastructure attribution",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:04:00Z",
        }]
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"],state)
        self.assertEqual(state["operator_grants_remaining"],1)
        self.assertEqual(state["operator_grants_used"],0)
        self.assertEqual(state["allowed_attempts"],6)

    def test_refunded_operator_abort_failure_row_does_not_require_supervisor_grant(self):
        entry=self.entry()
        entry["failure_history"].append({
            "attempt":5,
            "classification":"infrastructure",
            "reason":"zero-work operator dispatch transport failure",
            "session":"s5",
        })
        entry["operator_retry_attempts"]=[{
            "sequence":5,
            "session":"s5",
            "state":"infrastructure_abort",
            "outcome":"infrastructure_abort",
            "consumes_operator_grant":False,
            "evidence":"zero-work operator dispatch transport failure",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:04:00Z",
        }]
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"],state)
        self.assertEqual(state["infrastructure_retry_grants"],3)
        self.assertEqual(state["operator_infrastructure_aborted"],0)
        self.assertEqual(state["operator_grants_remaining"],1)
        self.assertEqual(state["allowed_attempts"],6)

    def test_late_infrastructure_abort_refunds_consumed_operator_record(self):
        entry=self.entry()
        entry["operator_retry_attempts"]=[{
            "sequence":5,
            "session":"s5",
            "state":"consumed",
            "outcome":"meaningful_execution",
            "consumes_operator_grant":True,
            "evidence":"completed-worker-tool-action",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:04:00Z",
            "consumed_at":"2026-09-25T00:04:10Z",
        }]
        self.assertTrue(
            supervisor.normalize_operator_attempt_after_infrastructure_abort(
                entry,"s5","late supervisor infrastructure attribution",
                "2026-09-25T00:05:00Z",
            )
        )
        row=entry["operator_retry_attempts"][0]
        self.assertEqual(row["state"],"infrastructure_abort")
        self.assertEqual(row["outcome"],"infrastructure_abort")
        self.assertFalse(row["consumes_operator_grant"])
        self.assertEqual(
            row["normalized_by"],
            "supervisor-infrastructure-authority-v1",
        )

    def test_operator_authorized_attempt_may_fail_genuinely_without_invalidating_ledger(self):
        entry=self.entry()
        entry["failure_history"].append(
            {"attempt":5,"classification":"genuine","reason":"functional-failed"}
        )
        entry["count"]=6
        entry["sessions"].append("s6")
        entry["failure_history"].append(
            {"attempt":6,"classification":"genuine","reason":"functional-failed-again"}
        )
        entry["operator_retry_attempts"]=[{
            "sequence":6,
            "session":"s6",
            "state":"consumed",
            "outcome":"meaningful_execution",
            "consumes_operator_grant":True,
            "evidence":"completed-worker-tool-action",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:05:00Z",
            "consumed_at":"2026-09-25T00:05:10Z",
        }]
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"],state)
        self.assertEqual(state["allowed_attempts"],6)
        self.assertEqual(state["count"],6)
        self.assertEqual(state["operator_grants_used"],1)
        self.assertEqual(state["operator_grants_remaining"],0)

    def test_repaired_infrastructure_ledger_rejects_unaudited_operator_override(self):
        entry=self.entry()
        entry["operator_overrides"][0]["source"]="model"
        state=control_state.attempt_state(entry)
        self.assertFalse(state["valid"],state)


class SandboxWrapperHistoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.sid="ses-wrapper-poison"
        self.verify="test -s owned.txt"
        leaf={
            "id":"D001","name":"owned","outcome":"repair owned",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"owned exists","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":leaf},
        }))
        (self.project/"owned.txt").write_text("partial\n")
        history=[
            {"attempt":1,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":2,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":3,"classification":"infrastructure","reason":"runtime"},
            {"attempt":4,"classification":"infrastructure","reason":"runtime"},
            {"attempt":5,"classification":"genuine","reason":"verify-failed-1"},
        ]
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{"D001":{
                "automatic_limit":3,"count":5,
                "sessions":["s1","s2","s3","s4",self.sid],
                "failure_history":history,
                "infrastructure_retry_grants":2,
                "infrastructure_failures":[
                    {"grant":1,"source":"supervisor","kind":"runtime-cancel",
                     "session":"s3","evidence":"durable-partial-state-preserved",
                     "reason":"runtime"},
                    {"grant":1,"source":"supervisor","kind":"runtime-cancel",
                     "session":"s4","evidence":"durable-partial-state-preserved",
                     "reason":"runtime"},
                ],
            }},
        }))
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"still incomplete\n"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001",self.sid,self.verify,checked,"verify-failed-1"
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def proof(self):
        return {
            "agent":"implementer","persisted_wrapper_calls":11,
            "manual_wrapper_calls":1,"manual_wrapper_failures":1,
            "maximum_steps_reached":True,"completed_tool_turns":22,
            "meaningful_execution":True,
        }

    def test_reclassifies_only_contaminated_attempt_and_grants_one_slot(self):
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"sandbox_wrapper_history_evidence",
            return_value=self.proof(),
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value={},
        ):
            ok,detail=supervisor.recover_sandbox_wrapper_history_poison(
                "D001"
            )
        self.assertEqual((ok,detail),(True,"recovered"))
        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        self.assertEqual(entry["count"],5)
        self.assertEqual(entry["infrastructure_retry_grants"],3)
        latest=entry["failure_history"][-1]
        self.assertEqual(latest["attempt"],5)
        self.assertEqual(latest["classification"],"infrastructure")
        self.assertEqual(latest["original_reason"],"verify-failed-1")
        self.assertEqual(
            latest["reclassified_by"],
            "runtime-sandbox-wrapper-history-repair",
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["allowed_attempts"],6)
        self.assertEqual(
            entry["sandbox_wrapper_history_recoveries"][0]["attempt"],5
        )

    def test_v2612_partial_state_repair_projects_new_plan_credit(self):
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"sandbox_wrapper_history_evidence",
            return_value=self.proof(),
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value={},
        ):
            self.assertEqual(
                supervisor.recover_sandbox_wrapper_history_poison("D001"),
                (True,"recovered"),
            )
        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        entry["count"]=6
        entry["sessions"].append("s6")
        stronger=hashlib.sha256(b"stronger verify").hexdigest()
        entry["plan_contract_revisions"]=[{
            "attempt":6,
            "source":"supervisor-plan-contract-revision",
            "previous_verify_sha256":"old",
            "current_verify_sha256":stronger,
            "timestamp":"2026-09-25T00:00:00Z",
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["v2612_infrastructure_repair"])
        self.assertEqual(state["infrastructure_retry_grants"],3)
        self.assertEqual(state["infrastructure_grants_remaining"],0)
        self.assertEqual(state["plan_contract_retry_grants"],1)
        self.assertEqual(state["allowed_attempts"],7)

    def test_refuses_without_nested_wrapper_failure(self):
        proof=self.proof()
        proof["manual_wrapper_failures"]=0
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"sandbox_wrapper_history_evidence",
            return_value=proof,
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value={},
        ):
            ok,detail=supervisor.recover_sandbox_wrapper_history_poison(
                "D001"
            )
        self.assertEqual(
            (ok,detail),
            (False,"wrapper-history-recovery-no-wrapper-failure"),
        )

    def test_recovers_trusted_verify_shadow_and_reflection_contamination(self):
        trusted={
            "agent":"implementer",
            "canonical_verify":self.verify,
            "trusted_verify_wrapper_calls":2,
            "verify_shadow_required_file_failures":1,
            "trusted_verify_wrapper_reflection_denials":1,
            "maximum_steps_reached":True,
            "completed_tool_turns":7,
            "meaningful_execution":True,
        }
        legacy={
            "agent":"implementer","persisted_wrapper_calls":0,
            "manual_wrapper_calls":0,"manual_wrapper_failures":0,
            "maximum_steps_reached":False,"completed_tool_turns":7,
            "meaningful_execution":True,
        }
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"sandbox_wrapper_history_evidence",return_value=legacy,
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value=trusted,
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_fix_installed",
            return_value=True,
        ):
            ok,detail=supervisor.recover_sandbox_wrapper_history_poison("D001")
        self.assertEqual((ok,detail),(True,"recovered"))
        entry=json.loads((self.work/"attempts.json").read_text())["deliverables"]["D001"]
        self.assertEqual(entry["infrastructure_retry_grants"],3)
        row=entry["failure_history"][-1]
        self.assertEqual(row["classification"],"infrastructure")
        self.assertEqual(row["original_reason"],"verify-failed-1")
        marker=entry["sandbox_wrapper_history_recoveries"][-1]
        self.assertEqual(marker["mode"],"trusted-verify-wrapper-history")
        self.assertEqual(marker["trusted_verify_wrapper_calls"],2)
        self.assertEqual(marker["verify_shadow_required_file_failures"],1)
        self.assertEqual(marker["trusted_verify_wrapper_reflection_denials"],1)
        self.assertTrue(control_state.attempt_state(entry)["valid"])
        self.assertEqual(control_state.attempt_state(entry)["allowed_attempts"],6)

    def test_refuses_trusted_verify_contamination_without_reflection_denial(self):
        trusted={
            "agent":"implementer",
            "canonical_verify":self.verify,
            "trusted_verify_wrapper_calls":2,
            "verify_shadow_required_file_failures":1,
            "trusted_verify_wrapper_reflection_denials":0,
            "maximum_steps_reached":True,
            "completed_tool_turns":7,
            "meaningful_execution":True,
        }
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"sandbox_wrapper_history_evidence",return_value={},
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value=trusted,
        ):
            self.assertEqual(
                supervisor.recover_sandbox_wrapper_history_poison("D001"),
                (False,"wrapper-history-recovery-proof-mismatch"),
            )


class RunChecksDiagnosticRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        leaf={
            "id":"D001","name":"manifest","outcome":"repair checks",
            "owned_artifacts":".opencode-v2/TEST_CHECKS.json",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":supervisor.RUN_CHECKS_COMMAND,
            "role":"test-builder","done_when":"checks pass",
            "acceptance_ids":["A001"],"parallel":"none",
            "split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":leaf},
        }))
        sessions=["s1","s2","s3","s4"]
        history=[
            {"attempt":1,"classification":"genuine","reason":"verify-failed-1","session":"s1","source":"supervisor"},
            {"attempt":2,"classification":"genuine","reason":"verify-failed-1","session":"s2","source":"supervisor"},
            {"attempt":3,"classification":"genuine","reason":"verify-failed-1","session":"s3","source":"supervisor"},
            {"attempt":4,"classification":"genuine","reason":"verify-failed-1","session":"s4","source":"supervisor"},
        ]
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{"D001":{
                "automatic_limit":3,"count":4,"sessions":sessions,
                "failure_history":history,
                "infrastructure_retry_grants":1,
                "infrastructure_failures":[{
                    "timestamp":"2026-09-28T00:00:00Z",
                    "grant":1,"source":"supervisor",
                    "kind":"verification-sandbox","session":"s3",
                    "evidence":"durable-partial-state-preserved",
                    "reason":"trusted-verify-shadow-required-file-false-escape",
                }],
            }},
        }))
        no_diag=json.dumps({
            "protocol":"v2-test-report-v1","status":"fail",
            "checks_run":4,"checks_passed":2,
            "checks":[{"name":"bad","exit_code":125,"timed_out":False}],
        },indent=2)
        entries=[
            {"attempt":1,"session":"s1","command":supervisor.RUN_CHECKS_COMMAND,
             "executed":True,"exit_code":1,"result":"verify-failed-1","stdout":no_diag,"stderr":"","error":""},
            {"attempt":2,"session":"s2","command":supervisor.RUN_CHECKS_COMMAND,
             "executed":True,"exit_code":1,"result":"verify-failed-1","stdout":no_diag,"stderr":"","error":""},
            {"attempt":3,"session":"s3","command":supervisor.RUN_CHECKS_COMMAND,
             "executed":True,"exit_code":1,"result":"verify-failed-1","stdout":"","stderr":"","error":""},
            {"attempt":4,"session":"s4","command":supervisor.RUN_CHECKS_COMMAND,
             "executed":True,"exit_code":1,"result":"verify-failed-1",
             "stdout":no_diag.replace('"checks": [','"diagnostic": {"stderr":"unsafe test command"}, "checks": ['),
             "stderr":"","error":""},
        ]
        (self.work/"D001.verify-evidence.json").write_text(json.dumps({
            "protocol":"v2-supervisor-verify-evidence-v1",
            "owner":"supervisor","deliverable":"D001",
            "entries":entries,"latest":entries[-1],
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def trusted_proof(self):
        return {
            "agent":"test-builder",
            "canonical_verify":supervisor.RUN_CHECKS_COMMAND,
            "trusted_verify_wrapper_calls":2,
            "verify_shadow_required_file_failures":1,
            "trusted_verify_wrapper_reflection_denials":1,
            "maximum_steps_reached":True,
            "completed_tool_turns":7,
            "meaningful_execution":True,
        }

    def test_reconciles_existing_grant_without_adding_retry(self):
        before=json.loads((self.work/"attempts.json").read_text())["deliverables"]["D001"]
        self.assertFalse(control_state.attempt_state(before)["valid"])
        with mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value=self.trusted_proof(),
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_fix_installed",
            return_value=True,
        ):
            self.assertEqual(
                supervisor.reconcile_trusted_verify_infrastructure_history("D001"),
                (True,"reconciled"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())["deliverables"]["D001"]
        self.assertEqual(entry["infrastructure_retry_grants"],1)
        self.assertEqual(entry["failure_history"][2]["classification"],"infrastructure")
        self.assertEqual(entry["failure_history"][3]["classification"],"genuine")
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["allowed_attempts"],4)

    def test_refunds_only_historical_failures_missing_diagnostics(self):
        with mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_evidence",
            return_value=self.trusted_proof(),
        ), mock.patch.object(
            supervisor,"trusted_verify_wrapper_contamination_fix_installed",
            return_value=True,
        ):
            self.assertEqual(
                supervisor.reconcile_trusted_verify_infrastructure_history("D001"),
                (True,"reconciled"),
            )
        self.assertEqual(
            supervisor.recover_run_checks_diagnostic_omissions("D001"),
            (True,"recovered-2"),
        )
        entry=json.loads((self.work/"attempts.json").read_text())["deliverables"]["D001"]
        self.assertEqual(entry["infrastructure_retry_grants"],3)
        self.assertEqual(
            [x["classification"] for x in entry["failure_history"]],
            ["infrastructure","infrastructure","infrastructure","genuine"],
        )
        self.assertEqual(
            [x["attempt"] for x in entry["run_checks_diagnostic_recoveries"]],
            [1,2],
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["allowed_attempts"],6)


class SupervisorPlanReplacementNormalizationTests(unittest.TestCase):
    def test_consumed_operator_retry_is_refunded_by_later_plan_revision(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            old_project=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                entry={
                    "automatic_limit":2,
                    "count":6,
                    "sessions":["s1","s2","s3","s4","s5","s6"],
                    "failure_history":[
                        {"attempt":1,"classification":"infrastructure","reason":"i1"},
                        {"attempt":2,"classification":"infrastructure","reason":"i2"},
                        {"attempt":3,"classification":"infrastructure","reason":"i3"},
                        {"attempt":4,"classification":"genuine","reason":"g1"},
                        {"attempt":5,"classification":"genuine","reason":"g2"},
                    ],
                    "infrastructure_retry_grants":3,
                    "infrastructure_failures":[
                        {"timestamp":"2026-09-25T00:00:00Z","grant":1,
                         "source":"supervisor","kind":"runtime-cancel",
                         "session":"s1","evidence":"durable-partial-state-preserved",
                         "reason":"i1"},
                        {"timestamp":"2026-09-25T00:01:00Z","grant":1,
                         "source":"supervisor","kind":"runtime-cancel",
                         "session":"s2","evidence":"durable-partial-state-preserved",
                         "reason":"i2"},
                        {"timestamp":"2026-09-25T00:02:00Z","grant":1,
                         "source":"supervisor","kind":"runtime-cancel",
                         "session":"s3","evidence":"durable-partial-state-preserved",
                         "reason":"i3"},
                    ],
                    "operator_retry_grants":1,
                    "operator_overrides":[{
                        "timestamp":"2026-09-25T00:03:00Z","grant":1,
                        "source":"operator-cli","reason":"human retry",
                    }],
                    "operator_retry_attempts":[{
                        "sequence":6,"session":"s6","state":"consumed",
                        "outcome":"meaningful_execution",
                        "consumes_operator_grant":True,
                        "evidence":"completed-worker-tool-action",
                        "source":"supervisor",
                        "timestamp":"2026-09-25T00:04:00Z",
                        "consumed_at":"2026-09-25T00:04:10Z",
                    }],
                    "plan_contract_revisions":[{
                        "attempt":6,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":"old",
                        "current_verify_sha256":"new",
                        "timestamp":"2026-09-26T00:00:00Z",
                    }],
                }
                (work/"attempts.json").write_text(json.dumps({
                    "owner":"supervisor","deliverables":{"D001":entry}
                }))
                self.assertFalse(control_state.attempt_state(entry)["valid"])
                self.assertTrue(
                    supervisor.normalize_supervisor_replacement_record("D001")
                )
                entry=json.loads((work/"attempts.json").read_text())[
                    "deliverables"
                ]["D001"]
                row=entry["operator_retry_attempts"][0]
                self.assertEqual(row["state"],"plan_contract_replacement")
                self.assertFalse(row["consumes_operator_grant"])
                self.assertEqual(
                    row["normalized_by"],"supervisor-credit-authority-v1"
                )
                state=control_state.attempt_state(entry)
                self.assertTrue(state["valid"],state)
                self.assertEqual(state["plan_contract_retry_grants"],1)
                self.assertEqual(state["operator_grants_remaining"],1)
                self.assertEqual(state["allowed_attempts"],7)
            finally:
                supervisor.PROJECT=old_project

    def test_multiple_hash_refinements_same_attempt_are_one_credit(self):
        entry={
            "count":6,
            "plan_contract_revisions":[
                {"attempt":6,"source":"supervisor-plan-contract-revision",
                 "current_verify_sha256":"one"},
                {"attempt":6,"source":"supervisor-plan-contract-revision",
                 "current_verify_sha256":"two"},
            ],
        }
        self.assertEqual(
            control_state._plan_contract_revision_credit_count(entry,6),1
        )

    def test_identical_transition_across_attempts_is_one_credit(self):
        entry={
            "count":7,
            "plan_contract_revisions":[
                {
                    "attempt":6,
                    "source":"supervisor-plan-contract-revision",
                    "previous_verify_sha256":"old",
                    "current_verify_sha256":"new",
                },
                {
                    "attempt":7,
                    "source":"supervisor-plan-contract-revision",
                    "previous_verify_sha256":"old",
                    "current_verify_sha256":"new",
                },
            ],
        }
        self.assertEqual(
            control_state._plan_contract_revision_credit_count(entry,7),1
        )

    def test_plan_revision_terminalizes_prior_attempt_before_dispatch_placeholder(self):
        entry={
            "count":2,
            "sessions":["ses-revised","dispatch:tool:D001"],
            "automatic_limit":2,
            "plan_contract_revisions":[{
                "attempt":1,
                "source":"supervisor-plan-contract-revision",
                "previous_verify_sha256":"a"*64,
                "current_verify_sha256":"b"*64,
                "previous_result":"verify-failed-1",
            }],
        }
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"],state)
        self.assertEqual(state["plan_contract_retry_grants"],1)
        self.assertTrue(state["unmaterialized_dispatch_reusable"])

    def test_reconcile_does_not_append_same_transition_on_later_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            work=ctrl/"work"
            work.mkdir(parents=True)
            old_project=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                old_command="test -s owned.txt"
                new_command="test -s owned.txt && grep -q ok owned.txt"
                old_digest=hashlib.sha256(old_command.encode()).hexdigest()
                new_digest=hashlib.sha256(new_command.encode()).hexdigest()
                (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(
                    json.dumps({
                        "leaves":{
                            "D001":{
                                "verify_command":new_command,
                                "owned_artifact_paths":["owned.txt"],
                                "role":"implementer",
                            }
                        }
                    })
                )
                entry={
                    "automatic_limit":2,
                    "count":7,
                    "sessions":["s1","s2","s3","s4","s5","s6","s7"],
                    "plan_contract_revisions":[{
                        "attempt":6,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":old_digest,
                        "current_verify_sha256":new_digest,
                        "timestamp":"2026-09-26T00:00:00Z",
                    }],
                }
                (work/"attempts.json").write_text(json.dumps({
                    "owner":"supervisor","deliverables":{"D001":entry}
                }))
                (work/"D001.verify-evidence.json").write_text(json.dumps({
                    "owner":"supervisor",
                    "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
                    "deliverable":"D001",
                    "entries":[],
                    "latest":{
                        "attempt":6,"session":"s6",
                        "timestamp":"2026-09-25T00:00:00Z",
                        "command":old_command,"executed":True,
                        "exit_code":0,"result":"verified",
                        "stdout":"","stderr":"","error":"",
                    },
                }))
                self.assertEqual(
                    supervisor.reconcile_plan_contract_revisions(),[]
                )
                entry=json.loads((work/"attempts.json").read_text())[
                    "deliverables"
                ]["D001"]
                self.assertEqual(len(entry["plan_contract_revisions"]),1)
                self.assertEqual(
                    control_state._plan_contract_revision_credit_count(
                        entry,7
                    ),
                    1,
                )
            finally:
                supervisor.PROJECT=old_project

    def test_failed_verify_contract_revision_grants_one_replacement_and_clears_split(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            work=ctrl/"work"
            work.mkdir(parents=True)
            old_project=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                old_command="python3 -m unittest discover -s tests"
                new_command=(
                    "python3 -c \"import subprocess; "
                    "r=subprocess.run(['python3','-m','unittest','discover','-s','tests']); "
                    "assert r.returncode==0\""
                )
                (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                    "leaves":{
                        "D001":{
                            "verify_command":new_command,
                            "owned_artifact_paths":["tests/test_app.py"],
                            "role":"test-builder",
                            "split_children":[],
                        }
                    }
                }))
                entry={
                    "automatic_limit":2,
                    "count":2,
                    "sessions":["s1","s2"],
                    "failure_history":[
                        {"attempt":1,"session":"s1","classification":"genuine",
                         "reason":"verify-failed-1","source":"supervisor"},
                        {"attempt":2,"session":"s2","classification":"genuine",
                         "reason":"verify-failed-1","source":"supervisor"},
                    ],
                    "split_required":{
                        "generation":1,
                        "reason":"genuine-failure-threshold",
                    },
                }
                (work/"attempts.json").write_text(json.dumps({
                    "owner":"supervisor","deliverables":{"D001":entry}
                }))
                (work/"D001.verify-evidence.json").write_text(json.dumps({
                    "owner":"supervisor",
                    "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
                    "deliverable":"D001",
                    "entries":[],
                    "latest":{
                        "attempt":2,"session":"s2",
                        "timestamp":"2026-09-27T00:00:00Z",
                        "command":old_command,"executed":True,
                        "exit_code":1,"result":"verify-failed-1",
                        "stdout":"","stderr":"","error":"",
                    },
                }))
                changed=supervisor.reconcile_plan_contract_revisions()
                self.assertIn("D001",changed)
                repaired=json.loads((work/"attempts.json").read_text())[
                    "deliverables"
                ]["D001"]
                self.assertNotIn("split_required",repaired)
                self.assertEqual(len(repaired["plan_contract_revisions"]),1)
                row=repaired["plan_contract_revisions"][0]
                self.assertEqual(row["previous_result"],"verify-failed-1")
                state=control_state.attempt_state(repaired)
                self.assertTrue(state["valid"],state)
                self.assertEqual(state["plan_contract_retry_grants"],1)
                self.assertEqual(state["allowed_attempts"],3)
            finally:
                supervisor.PROJECT=old_project

    def test_over_normalized_duplicate_transition_reverts_to_reservation(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            old_project=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                entry={
                    "automatic_limit":2,
                    "count":7,
                    "sessions":[
                        "s1","s2","s3","s4","s5","s6","dispatch:t7"
                    ],
                    "failure_history":[
                        {"attempt":1,"classification":"infrastructure","reason":"i1"},
                        {"attempt":2,"classification":"infrastructure","reason":"i2"},
                        {"attempt":3,"classification":"infrastructure","reason":"i3"},
                        {"attempt":4,"classification":"genuine","reason":"g1"},
                        {"attempt":5,"classification":"genuine","reason":"g2"},
                    ],
                    "infrastructure_retry_grants":3,
                    "infrastructure_failures":[
                        {
                            "timestamp":"2026-09-25T00:00:00Z","grant":1,
                            "source":"supervisor","kind":"runtime-cancel",
                            "session":"s1",
                            "evidence":"no-owned-artifact-or-progress",
                            "reason":"i1",
                        },
                        {
                            "timestamp":"2026-09-25T00:01:00Z","grant":1,
                            "source":"supervisor","kind":"runtime-cancel",
                            "session":"s2",
                            "evidence":"no-owned-artifact-or-progress",
                            "reason":"i2",
                        },
                        {
                            "timestamp":"2026-09-25T00:02:00Z","grant":1,
                            "source":"supervisor","kind":"runtime-cancel",
                            "session":"s3",
                            "evidence":"no-owned-artifact-or-progress",
                            "reason":"i3",
                        },
                    ],
                    "operator_retry_grants":1,
                    "operator_overrides":[{
                        "timestamp":"2026-09-25T00:03:00Z","grant":1,
                        "source":"operator-cli","reason":"human retry",
                    }],
                    "plan_contract_revisions":[
                        {
                            "attempt":6,
                            "source":"supervisor-plan-contract-revision",
                            "previous_verify_sha256":"old",
                            "current_verify_sha256":"new",
                            "timestamp":"2026-09-26T00:00:00Z",
                        },
                        {
                            "attempt":7,
                            "source":"supervisor-plan-contract-revision",
                            "previous_verify_sha256":"old",
                            "current_verify_sha256":"new",
                            "timestamp":"2026-09-26T00:01:00Z",
                        },
                    ],
                    "operator_retry_attempts":[
                        {
                            "sequence":6,"session":"s6",
                            "state":"plan_contract_replacement",
                            "outcome":"plan_contract_replacement",
                            "consumes_operator_grant":False,
                            "source":"supervisor",
                            "normalized_by":"supervisor-credit-authority-v1",
                            "normalized_at":"2026-09-26T00:00:10Z",
                            "timestamp":"2026-09-25T00:04:00Z",
                        },
                        {
                            "sequence":7,"session":"dispatch:t7",
                            "state":"plan_contract_replacement",
                            "outcome":"plan_contract_replacement",
                            "consumes_operator_grant":False,
                            "source":"supervisor",
                            "normalized_by":"supervisor-credit-authority-v1",
                            "normalized_at":"2026-09-26T00:01:10Z",
                            "timestamp":"2026-09-26T00:01:05Z",
                        },
                    ],
                }
                (work/"attempts.json").write_text(json.dumps({
                    "owner":"supervisor","deliverables":{"D001":entry}
                }))
                self.assertTrue(
                    supervisor.normalize_supervisor_replacement_record("D001")
                )
                entry=json.loads((work/"attempts.json").read_text())[
                    "deliverables"
                ]["D001"]
                first,second=entry["operator_retry_attempts"]
                self.assertEqual(first["state"],"plan_contract_replacement")
                self.assertEqual(second["state"],"reserved")
                self.assertFalse(second["consumes_operator_grant"])
                self.assertNotIn("normalized_by",second)
                self.assertNotIn("outcome",second)
                state=control_state.attempt_state(entry)
                self.assertTrue(state["valid"],state)
                self.assertEqual(state["plan_contract_retry_grants"],1)
                self.assertEqual(state["operator_grants_reserved"],1)
                self.assertEqual(state["allowed_attempts"],7)
                self.assertTrue(state["unmaterialized_dispatch_reusable"])
            finally:
                supervisor.PROJECT=old_project


class ContextDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.sid="ses-context-truncated"
        self.verify="test -s owned.txt"
        self.leaf={
            "id":"D001","name":"owned","outcome":"repair owned",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"owned exists","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.leaf},
        }))
        (self.project/"owned.txt").write_text("partial\n")
        progress=self.work/"D001.progress.md"
        progress.write_text("supervisor correction with required parameters\n")
        progress_sha=hashlib.sha256(progress.read_bytes()).hexdigest()
        self.progress_baseline=f"file:{progress_sha}"
        (self.work/"D001.attempt-7.execution-baseline.json").write_text(
            json.dumps({
                "owner":"supervisor","deliverable":"D001","attempt":7,
                "files":{
                    ".opencode-v2/work/D001.progress.md":
                        self.progress_baseline,
                    "owned.txt":"file:"+hashlib.sha256(
                        (self.project/"owned.txt").read_bytes()
                    ).hexdigest(),
                },
            })
        )
        history=[
            {"attempt":1,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":2,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":3,"classification":"infrastructure","reason":"runtime"},
            {"attempt":4,"classification":"infrastructure","reason":"runtime"},
            {"attempt":5,"classification":"infrastructure","reason":"wrapper"},
            {"attempt":6,"classification":"genuine","reason":"verify-failed-1"},
            {"attempt":7,"classification":"genuine","reason":"verify-failed-1"},
        ]
        infra=[
            {"grant":1,"source":"supervisor","kind":"runtime-cancel",
             "session":"s3","evidence":"durable-partial-state-preserved",
             "reason":"runtime"},
            {"grant":1,"source":"supervisor","kind":"runtime-cancel",
             "session":"s4","evidence":"durable-partial-state-preserved",
             "reason":"runtime"},
            {"grant":1,"source":"supervisor","kind":"runtime-cancel",
             "session":"s5","evidence":"durable-partial-state-preserved",
             "reason":"wrapper"},
        ]
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{"D001":{
                "automatic_limit":3,"count":7,
                "sessions":["s1","s2","s3","s4","s5","s6",self.sid],
                "failure_history":history,
                "infrastructure_retry_grants":3,
                "infrastructure_failures":infra,
                "plan_contract_revisions":[{
                    "attempt":6,
                    "source":"supervisor-plan-contract-revision",
                    "previous_verify_sha256":"old",
                    "current_verify_sha256":hashlib.sha256(
                        self.verify.encode()
                    ).hexdigest(),
                    "timestamp":"2026-09-25T00:00:00Z",
                }],
            }},
        }))
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"still incomplete\n"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001",self.sid,self.verify,checked,"verify-failed-1"
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def proof(self):
        return {
            "context_reads":1,
            "truncated_context_reads":1,
            "context_verify_visible":False,
            "progress_read_denials":2,
            "write_required_denials":13,
            "maximum_steps_reached":True,
            "completed_tool_turns":23,
            "progress_baseline":self.progress_baseline,
            "context_output_sha256":"a"*64,
        }

    def test_reclassifies_only_context_delivery_failure_and_grants_one_slot(self):
        before=json.loads(
            (self.work/"D001.verify-evidence.json").read_text()
        )
        initial=json.loads((self.work/"attempts.json").read_text())
        initial_state=control_state.attempt_state(
            initial["deliverables"]["D001"]
        )
        self.assertTrue(initial_state["valid"])
        self.assertEqual(initial_state["allowed_attempts"],7)
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"context_delivery_failure_evidence",
            return_value=self.proof(),
        ), mock.patch.object(
            supervisor,"context_delivery_fixes_installed",return_value=True
        ):
            ok,detail=supervisor.recover_context_delivery_failure("D001")
        self.assertEqual((ok,detail),(True,"recovered"))

        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        self.assertEqual(entry["count"],7)
        self.assertEqual(
            entry["sessions"],
            ["s1","s2","s3","s4","s5","s6",self.sid],
        )
        self.assertEqual(entry["infrastructure_retry_grants"],3)
        latest=entry["failure_history"][-1]
        self.assertEqual(latest["attempt"],7)
        self.assertEqual(latest["classification"],"infrastructure")
        self.assertEqual(latest["original_reason"],"verify-failed-1")
        self.assertEqual(
            latest["reclassified_by"],
            "runtime-context-delivery-repair",
        )
        recovery=entry["context_delivery_recoveries"][0]
        self.assertEqual(
            recovery["source"],"supervisor-context-delivery-repair"
        )
        self.assertEqual(recovery["grant"],1)
        self.assertEqual(recovery["progress_baseline"],self.progress_baseline)

        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["v2612_infrastructure_repair"])
        self.assertEqual(state["infrastructure_retry_grants"],3)
        self.assertEqual(state["context_delivery_retry_grants"],1)
        self.assertEqual(state["plan_contract_retry_grants"],1)
        self.assertEqual(state["allowed_attempts"],8)
        self.assertEqual(state["automatic_attempts_consumed"],2)
        self.assertEqual(
            json.loads((self.work/"D001.verify-evidence.json").read_text()),
            before,
        )

        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ):
            self.assertEqual(
                supervisor.recover_context_delivery_failure("D001"),
                (True,"already-recovered"),
            )

    def test_context_credit_claim_does_not_create_operator_reservation(self):
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"context_delivery_failure_evidence",
            return_value=self.proof(),
        ), mock.patch.object(
            supervisor,"context_delivery_fixes_installed",return_value=True
        ):
            self.assertEqual(
                supervisor.recover_context_delivery_failure("D001"),
                (True,"recovered"),
            )

        status,sequence=supervisor.claim_attempt(
            "dispatch:context-credit-test","D001"
        )
        self.assertEqual((status,sequence),("claimed",8))
        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        self.assertEqual(entry["count"],8)
        self.assertEqual(
            entry["sessions"][-1],"dispatch:context-credit-test"
        )
        self.assertFalse(entry.get("operator_retry_attempts"))
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["allowed_attempts"],8)

    def test_legacy_covered_reservation_is_ignored_and_reusable(self):
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"context_delivery_failure_evidence",
            return_value=self.proof(),
        ), mock.patch.object(
            supervisor,"context_delivery_fixes_installed",return_value=True
        ):
            self.assertEqual(
                supervisor.recover_context_delivery_failure("D001"),
                (True,"recovered"),
            )

        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        entry["count"]=8
        entry["sessions"].append("dispatch:legacy-covered")
        entry["operator_retry_attempts"]=[{
            "sequence":8,
            "session":"dispatch:legacy-covered",
            "state":"reserved",
            "consumes_operator_grant":False,
            "source":"supervisor",
            "timestamp":"2026-09-25T00:00:00Z",
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["v2612_infrastructure_repair"])
        self.assertFalse(state["operator_authorized_attempt"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["allowed_attempts"],8)

    def test_refuses_without_denied_progress_read(self):
        proof=self.proof()
        proof["progress_read_denials"]=0
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"context_delivery_failure_evidence",
            return_value=proof,
        ), mock.patch.object(
            supervisor,"context_delivery_fixes_installed",return_value=True
        ):
            ok,detail=supervisor.recover_context_delivery_failure("D001")
        self.assertEqual(
            (ok,detail),
            (False,"context-delivery-recovery-progress-read-not-denied"),
        )

    def test_evidence_detects_compact_truncation_and_progress_denial(self):
        context=str(
            self.project/".opencode-v2/query/leaves/D001-context.json"
        )
        progress=str(self.work/"D001.progress.md")
        parts=[
            {"type":"tool","tool":"read","state":{
                "status":"completed",
                "input":{"filePath":context},
                "output":(
                    "<content>\n1: {\"current_progress\":\"facts... "
                    "(line truncated to 2000 chars)\n\n"
                    "(End of file - total 1 lines)\n</content>"
                ),
            }},
            {"type":"tool","tool":"read","state":{
                "status":"error",
                "input":{"filePath":progress},
                "error":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED "
                        "IMPLEMENTATION_WRITE_REQUIRED deliverable=D001",
            }},
        ]
        for _ in range(3):
            parts.append({"type":"tool","tool":"bash","state":{
                "status":"error","input":{"command":"pwd"},
                "error":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED "
                        "IMPLEMENTATION_WRITE_REQUIRED deliverable=D001",
            }})
        records=[{"id":"m1","data":{"role":"assistant"}}]
        with mock.patch.object(
            supervisor,"_v1_message_records",return_value=records
        ), mock.patch.object(
            supervisor,"_v1_message_parts",return_value=parts
        ), mock.patch.object(
            supervisor,"last_assistant_text_db",
            return_value="Maximum steps for this agent have been reached."
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=8
        ):
            evidence=supervisor.context_delivery_failure_evidence(
                self.sid,"D001",7
            )
        self.assertEqual(evidence["context_reads"],1)
        self.assertEqual(evidence["truncated_context_reads"],1)
        self.assertFalse(evidence["context_verify_visible"])
        self.assertEqual(evidence["progress_read_denials"],1)
        self.assertEqual(evidence["write_required_denials"],4)
        self.assertTrue(evidence["maximum_steps_reached"])
        self.assertEqual(evidence["progress_baseline"],self.progress_baseline)
        self.assertEqual(len(evidence["context_output_sha256"]),64)

    def test_progress_read_marks_even_in_first_parallel_tool_batch(self):
        progress=str(self.work/"D001.progress.md")
        prompt="DELIVERABLE: D001\n"
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value=prompt
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=0
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=7
        ), mock.patch.object(
            supervisor,"persisted_implementation_progress_read_seen",
            return_value=False
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":progress}
            )
        self.assertEqual(state,"implementation-progress-read-once")
        self.assertIn("next_tool=bounded-state-read-or-write",detail)
        marker=supervisor.implementation_progress_read_marker("D001",7)
        self.assertTrue(marker.is_file())
        payload=json.loads(marker.read_text())
        self.assertEqual(payload["source"],"current-tool-preexecution")

    def test_split_writer_can_read_required_handoff_and_owned_artifact_once(self):
        self.leaf["split_handoff_source"]="D000"
        (self.work/"D000.progress.md").write_text("HANDOFF_READY: true\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.leaf},
        }))
        prompt="DELIVERABLE: D001\n"
        common=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",return_value=prompt
            ),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=1
            ),
            mock.patch.object(
                supervisor,"attempt_sequence_for_session",return_value=7
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
            mock.patch.object(
                supervisor,"persisted_exact_project_read_seen",
                return_value=False
            ),
        )
        handoff=str(self.work/"D000.progress.md")
        owned=str(self.project/"owned.txt")
        with common[0],common[1],common[2],common[3],common[4],common[5]:
            state,detail=supervisor.enforce_early_write_gate(
                self.sid,"read",{"filePath":handoff}
            )
            self.assertEqual(state,"implementation-bounded-read-once")
            self.assertIn("D000.progress.md",detail)
            state,detail=supervisor.enforce_early_write_gate(
                self.sid,"read",{"filePath":owned}
            )
            self.assertEqual(state,"implementation-bounded-read-once")
            self.assertIn("owned.txt",detail)

    def test_split_writer_repeated_or_unrelated_read_is_denied(self):
        self.leaf["split_handoff_source"]="D000"
        (self.work/"D000.progress.md").write_text("HANDOFF_READY: true\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.leaf},
        }))
        prompt="DELIVERABLE: D001\n"
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value=prompt
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=2
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=7
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ), mock.patch.object(
            supervisor,"persisted_exact_project_read_seen",return_value=True
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.work/"D000.progress.md")}
            )
            self.assertEqual(state,"implementation-write-required")
            self.assertIn("IMPLEMENTATION_WRITE_REQUIRED",detail)
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"unrelated.txt")}
            )
            self.assertEqual(state,"implementation-write-required")

    def test_completed_progress_read_reconciles_missing_marker(self):
        progress=str(self.work/"D001.progress.md")
        records=[{"id":"m-progress","data":{"role":"assistant"}}]
        parts=[{"type":"tool","tool":"read","state":{
            "status":"completed",
            "input":{"filePath":progress},
            "output":"progress body",
        }}]
        with mock.patch.object(
            supervisor,"_v1_message_records",return_value=records
        ), mock.patch.object(
            supervisor,"_v1_message_parts",return_value=parts
        ):
            self.assertTrue(
                supervisor.reconcile_implementation_progress_read_marker(
                    self.sid,"D001",7
                )
            )
        marker=supervisor.implementation_progress_read_marker("D001",7)
        self.assertTrue(marker.is_file())
        payload=json.loads(marker.read_text())
        self.assertEqual(
            payload["source"],"persisted-completed-read-reconciliation"
        )
        self.assertFalse(
            supervisor.implementation_progress_read_available("D001",7)
        )

    def test_effective_tool_turns_ignore_only_recoverable_steering_denials(self):
        rows=[
            ("m-steer","assistant",1),
            ("m-genuine-error","assistant",2),
            ("m-success","assistant",3),
            ("m-steer-batch","assistant",4),
        ]
        parts={
            "m-steer":[{
                "type":"tool","tool":"read","state":{
                    "status":"error",
                    "error":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED next_tool=write",
                },
            }],
            "m-genuine-error":[{
                "type":"tool","tool":"bash","state":{
                    "status":"error","error":"real tool failure",
                },
            }],
            "m-success":[{
                "type":"tool","tool":"read","state":{
                    "status":"completed","output":"ok",
                },
            }],
            "m-steer-batch":[
                {
                    "type":"tool","tool":"read","state":{
                        "status":"error",
                        "error":"EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED",
                    },
                },
                {
                    "type":"tool","tool":"glob","state":{
                        "status":"error",
                        "error":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED",
                    },
                },
            ],
        }
        with mock.patch.object(
            supervisor,"v1_runtime_enabled",return_value=True
        ), mock.patch.object(
            supervisor,"_v1_message_rows",return_value=rows
        ), mock.patch.object(
            supervisor,"_v1_message_parts",
            side_effect=lambda mid: parts[mid]
        ):
            self.assertEqual(
                supervisor.persisted_effective_tool_turns(self.sid),2
            )

    def test_progress_read_is_one_shot_and_hard_deadline_is_reachable(self):
        progress=str(self.work/"D001.progress.md")
        prompt="DELIVERABLE: D001\n"
        patches=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",return_value=prompt
            ),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=4
            ),
            mock.patch.object(
                supervisor,"persisted_effective_tool_turns",return_value=4
            ),
            mock.patch.object(
                supervisor,"attempt_sequence_for_session",return_value=7
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
        )
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5]:
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":progress}
            )
            self.assertEqual(state,"implementation-progress-read-once")
            self.assertIn("next_tool=bounded-state-read-or-write",detail)
            marker=supervisor.implementation_progress_read_marker("D001",7)
            self.assertTrue(marker.is_file())
            with mock.patch.object(
                supervisor,"set_abort_intent"
            ) as abort:
                state,detail=supervisor.enforce_early_write_gate(
                    self.sid,"bash",{"command":"pwd"}
                )
            self.assertEqual(state,"deny")
            self.assertIn("implementation_direct_write_noncompliance",detail)
            abort.assert_called_once()


    def test_post_write_refocus_denies_discovery_and_allows_owned_write_or_exact_verify(self):
        common=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",
                return_value="DELIVERABLE: D001\n"
            ),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=3
            ),
            mock.patch.object(
                supervisor,"attempt_sequence_for_session",return_value=7
            ),
            mock.patch.object(
                supervisor,"reconcile_implementation_progress_read_marker",
                return_value=True
            ),
            mock.patch.object(
                supervisor,"implementation_progress_read_available",
                return_value=False
            ),
            mock.patch.object(
                supervisor,"plan_contract_session_exact_verify_failure_count",
                return_value=0
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(True,"changed")
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_pending",return_value=False
            ),
            mock.patch.object(
                supervisor,"implementation_max_step_continuation_session_pending",
                return_value=False
            ),
            mock.patch.object(
                supervisor,"session_owned_mutation_seen",return_value=True
            ),
            mock.patch.object(
                supervisor,"plan_contract_session_exact_verify_state",
                return_value="not-attempted"
            ),
        )
        with common[0],common[1],common[2],common[3],common[4],common[5],common[6],common[7],common[8],common[9],common[10],common[11]:
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"unrelated.txt")}
            )
            self.assertEqual(state,"implementation-exact-verify-required")
            self.assertIn("IMPLEMENTATION_POST_WRITE_VERIFY_REQUIRED",detail)
            self.assertIn("no_discovery=true",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"edit",{
                    "filePath":str(self.project/"owned.txt"),
                    "oldString":"partial\n",
                    "newString":"fixed\n",
                }
            )
            self.assertEqual(state,"implementation-write-only")
            self.assertIn("OWNED_WRITE_BATCH_CONTINUE",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"bash",{"command":self.verify}
            )
            self.assertEqual(state,"implementation-exact-verify")
            self.assertIn("IMPLEMENTATION_POST_WRITE_VERIFY",detail)

    def test_post_write_refocus_returns_after_passing_exact_verify(self):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",
            return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=3
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=7
        ), mock.patch.object(
            supervisor,"reconcile_implementation_progress_read_marker",
            return_value=True
        ), mock.patch.object(
            supervisor,"implementation_progress_read_available",
            return_value=False
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_failure_count",
            return_value=0
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(True,"changed")
        ), mock.patch.object(
            supervisor,"plan_contract_reverify_pending",return_value=False
        ), mock.patch.object(
            supervisor,"implementation_max_step_continuation_session_pending",
            return_value=False
        ), mock.patch.object(
            supervisor,"session_owned_mutation_seen",return_value=True
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="passed"
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"unrelated.txt")}
            )
        self.assertEqual(state,"implementation-return-required")
        self.assertIn("POST_WRITE_EXACT_VERIFY_PASSED",detail)


class ImplementationGuardDenialBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.old_project=supervisor.PROJECT
        self.old_log=supervisor.LOG
        supervisor.PROJECT=str(self.project)
        supervisor.LOG=self.project/"events.log"
        self.sid="ses-denial-budget"
        self.did="D001"

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.LOG=self.old_log
        self.tmp.cleanup()

    def _tool_parts(self,markers):
        return [
            {
                "type":"tool","id":f"part-{i}","tool":"read",
                "state":{
                    "status":"error",
                    "error":f"{marker} session={self.sid} detail",
                },
            }
            for i,marker in enumerate(markers,1)
        ]

    def test_only_canonical_recoverable_guard_errors_are_counted(self):
        parts=self._tool_parts([
            "EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED",
            "EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED",
        ])+[{
            "type":"tool","id":"other","tool":"bash",
            "state":{"status":"error","error":"ordinary test command failed"},
        }]
        with mock.patch.object(
            supervisor,"_v1_message_records",
            return_value=[{"id":"m1","data":{"role":"assistant"}}],
        ), mock.patch.object(
            supervisor,"_v1_message_parts",return_value=parts,
        ):
            rows=supervisor.persisted_implementation_guard_denials(self.sid)
        self.assertEqual(len(rows),2)
        self.assertEqual(
            [x["marker"] for x in rows],
            [
                "EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED",
                "EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED",
            ],
        )

    def test_third_forbidden_action_requests_interrupt(self):
        prior=self._tool_parts([
            "EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED",
            "EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED",
        ])
        with mock.patch.object(
            supervisor,"implementation_direct_write_gate_state",
            return_value=(
                "implementation-write-required",
                "IMPLEMENTATION_WRITE_REQUIRED deliverable=D001",
            ),
        ), mock.patch.object(
            supervisor,"persisted_implementation_guard_denials",
            return_value=[
                {"marker":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED"},
                {"marker":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED"},
            ],
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001",
        ), mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer",
        ), mock.patch.object(supervisor,"set_abort_intent") as intent:
            state,detail=supervisor.enforce_early_write_gate(
                self.sid,"read",{"filePath":"unrelated.txt"}
            )
        self.assertEqual(state,"deny")
        self.assertIn("repeated_action_guard_noncompliance",detail)
        self.assertIn("prior_denials=2",detail)
        intent.assert_called_once()
        self.assertEqual(intent.call_args.args[3],"requested")

    def test_two_denials_do_not_block_required_owned_write(self):
        with mock.patch.object(
            supervisor,"implementation_direct_write_gate_state",
            return_value=("implementation-write-only","owned write allowed"),
        ), mock.patch.object(
            supervisor,"persisted_implementation_guard_denials",
            return_value=[
                {"marker":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED"},
                {"marker":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED"},
            ],
        ):
            state,detail=supervisor.enforce_early_write_gate(
                self.sid,"edit",{"filePath":"owned.txt"}
            )
        self.assertEqual(state,"implementation-write-only")
        self.assertEqual(detail,"owned write allowed")

    def test_two_denials_do_not_block_exact_verify(self):
        with mock.patch.object(
            supervisor,"implementation_direct_write_gate_state",
            return_value=("implementation-exact-verify","exact verify allowed"),
        ), mock.patch.object(
            supervisor,"persisted_implementation_guard_denials",
            return_value=[
                {"marker":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED"},
                {"marker":"EARLY_WRITE_IMPLEMENTATION_EXACT_VERIFY_REQUIRED"},
            ],
        ):
            state,detail=supervisor.enforce_early_write_gate(
                self.sid,"bash",{"command":"canonical verify"}
            )
        self.assertEqual(state,"implementation-exact-verify")
        self.assertEqual(detail,"exact verify allowed")


class PlanContractReverifyDirectWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.did="D001"
        self.sid="ses-reverify"
        self.old_verify="test -s owned.txt"
        self.verify="test -s owned.txt && grep -q '^ok$' owned.txt"
        self.leaf={
            "id":self.did,"name":"owned","outcome":"owned correct",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"owned.txt contains ok","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.project/"owned.txt").write_text("ok\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:self.leaf},
        }))
        digest=hashlib.sha256(self.verify.encode()).hexdigest()
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{self.did:{
                "automatic_limit":2,"count":2,
                "sessions":["s1",self.sid],
                "plan_contract_revisions":[{
                    "attempt":1,
                    "source":"supervisor-plan-contract-revision",
                    "previous_verify_sha256":hashlib.sha256(
                        self.old_verify.encode()
                    ).hexdigest(),
                    "current_verify_sha256":digest,
                    "timestamp":"2026-09-26T00:00:00Z",
                }],
            }},
        }))
        evidence={
            "owner":"supervisor",
            "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
            "deliverable":self.did,
            "entries":[],
            "latest":{
                "attempt":1,"session":"s1",
                "timestamp":"2026-09-25T00:00:00Z",
                "command":self.old_verify,"executed":True,
                "exit_code":0,"result":"verified",
                "stdout":"","stderr":"","error":"",
            },
        }
        (self.work/f"{self.did}.verify-evidence.json").write_text(
            json.dumps(evidence)
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def test_exact_new_verify_is_allowed_without_forced_product_mutation(self):
        self.assertTrue(
            supervisor.plan_contract_reverify_pending(self.did,self.leaf)
        )
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=3
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"bash",{"command":self.verify}
            )
            self.assertEqual(state,"implementation-exact-verify")
            self.assertIn("PLAN_CONTRACT_EXACT_VERIFY",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"bash",{"command":self.verify+"; echo altered"}
            )
            self.assertEqual(state,"implementation-exact-verify-required")
            self.assertIn("do_not_mutate=true",detail)

    def test_max_step_continuation_requires_exact_reverify_before_historical_delta(self):
        common=[
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
            ),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=3
            ),
            mock.patch.object(
                supervisor,"attempt_sequence_for_session",return_value=2
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(True,"changed")
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_pending",return_value=False
            ),
            mock.patch.object(
                supervisor,"implementation_max_step_continuation_session_pending",
                return_value=True
            ),
            mock.patch.object(
                supervisor,"plan_contract_session_exact_verify_failure_count",
                return_value=0
            ),
            mock.patch.object(
                supervisor,"session_owned_mutation_seen",return_value=False
            ),
        ]
        with common[0],common[1],common[2],common[3],common[4],common[5],common[6],common[7],common[8], mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="not-attempted"
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"unrelated.txt")}
            )
            self.assertEqual(state,"implementation-exact-verify-required")
            self.assertIn("max-step-continuation",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"bash",{"command":self.verify}
            )
            self.assertEqual(state,"implementation-exact-verify")
            self.assertIn("max-step-continuation",detail)

    def test_max_step_continuation_failed_verify_allows_only_owned_repair(self):
        common=[
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
            ),
            mock.patch.object(
                supervisor,"persisted_completed_tool_turns",return_value=4
            ),
            mock.patch.object(
                supervisor,"attempt_sequence_for_session",return_value=2
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(True,"changed")
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_pending",return_value=False
            ),
            mock.patch.object(
                supervisor,"implementation_max_step_continuation_session_pending",
                return_value=True
            ),
            mock.patch.object(
                supervisor,"plan_contract_session_exact_verify_failure_count",
                return_value=1
            ),
            mock.patch.object(
                supervisor,"session_owned_mutation_seen",return_value=False
            ),
            mock.patch.object(
                supervisor,"plan_contract_session_exact_verify_state",
                return_value="failed"
            ),
            mock.patch.object(
                supervisor,"implementation_repair_authoritative_read_targets",
                return_value=[]
            ),
        ]
        with common[0],common[1],common[2],common[3],common[4],common[5],common[6],common[7],common[8],common[9],common[10]:
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"unrelated.txt")}
            )
            self.assertEqual(state,"implementation-write-required")
            self.assertIn("PLAN_CONTRACT_REPAIR_WRITE_REQUIRED",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"edit",{
                    "filePath":str(self.project/"owned.txt"),
                    "oldString":"ok\n","newString":"fixed\n",
                }
            )
            self.assertEqual(state,"implementation-write-only")
            self.assertIn("PLAN_CONTRACT_OWNED_REPAIR",detail)

    def test_helper_identifies_materialized_same_attempt_continuation(self):
        attempts=json.loads((self.work/"attempts.json").read_text())
        entry=attempts["deliverables"][self.did]
        entry["implementation_max_step_continuations"]=[{
            "protocol":supervisor.IMPLEMENTATION_MAX_STEP_CONTINUATION_PROTOCOL,
            "attempt":2,
            "session":"s1",
            "replacement":"dispatch:max-step:test:D001",
        }]
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        self.assertTrue(
            supervisor.implementation_max_step_continuation_session_pending(
                self.sid,self.did,2
            )
        )
        self.assertFalse(
            supervisor.implementation_max_step_continuation_session_pending(
                "s1",self.did,2
            )
        )

    def test_failed_old_verify_revision_also_requires_exact_reverify_first(self):
        attempts=json.loads((self.work/"attempts.json").read_text())
        revision=attempts["deliverables"][self.did]["plan_contract_revisions"][0]
        revision["previous_result"]="verify-failed-1"
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        evidence=json.loads(
            (self.work/f"{self.did}.verify-evidence.json").read_text()
        )
        evidence["latest"].update({
            "exit_code":1,
            "result":"verify-failed-1",
        })
        (self.work/f"{self.did}.verify-evidence.json").write_text(
            json.dumps(evidence)
        )
        self.assertTrue(
            supervisor.plan_contract_reverify_pending(self.did,self.leaf)
        )

    def test_passed_exact_verify_requires_return_without_another_tool(self):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=3
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="passed"
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":"unrelated.txt"}
            )
        self.assertEqual(state,"implementation-return-required")
        self.assertIn("return-without-tools",detail)

    def test_failed_exact_verify_unlocks_owned_write_only(self):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=3
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ):
            state,_=supervisor.implementation_direct_write_gate_state(
                self.sid,"write",{
                    "filePath":str(self.project/"owned.txt"),
                    "content":"fixed\n",
                }
            )
            self.assertEqual(state,"implementation-write-only")
            state,_=supervisor.implementation_direct_write_gate_state(
                self.sid,"bash",{"command":"pwd"}
            )
            self.assertEqual(state,"implementation-write-required")

    def test_session_exact_verify_state_uses_persisted_exit_code(self):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"persisted_model_bash_commands",
            return_value=[{
                "command":self.verify,"status":"completed",
                "exit_code":0,"output":"(no output)","error":"",
            }]
        ):
            self.assertEqual(
                supervisor.plan_contract_session_exact_verify_state(
                    self.sid,self.leaf
                ),
                "passed",
            )
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"persisted_model_bash_commands",
            return_value=[{
                "command":self.verify,"status":"completed",
                "exit_code":1,"output":"","error":"",
            }]
        ):
            self.assertEqual(
                supervisor.plan_contract_session_exact_verify_state(
                    self.sid,self.leaf
                ),
                "failed",
            )

    def test_trusted_verify_wrapper_is_visible_to_reverify_state_machine(self):
        wrapped=worker_sandbox.replacement_worker_verify_command(
            self.project,
            {"session":self.sid,"agent":"implementer"},
            self.verify,
        )
        records=[{"id":"m1","data":{"role":"assistant"}}]
        parts=[{
            "type":"tool",
            "tool":"bash",
            "state":{
                "status":"completed",
                "input":{"command":wrapped},
                "output":"V2_WORKER_COMMAND_EXIT=1",
                "error":"",
                "metadata":{"exit":1},
            },
        }]
        with (
            mock.patch.object(supervisor,"_v1_message_records",return_value=records),
            mock.patch.object(supervisor,"_v1_message_parts",return_value=parts),
            mock.patch.object(supervisor,"_session_agent_db",return_value="implementer"),
        ):
            rows=supervisor.persisted_model_bash_commands(
                self.sid,"implementer"
            )
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["command"],self.verify)
            self.assertEqual(rows[0]["exit_code"],1)
            self.assertEqual(
                supervisor.plan_contract_session_exact_verify_state(
                    self.sid,self.leaf
                ),
                "failed",
            )

    def test_exact_verify_recognizer_accepts_current_trusted_wrapper_and_path_quoting_only(self):
        ctx={"session":self.sid,"agent":"implementer"}
        wrapped=worker_sandbox.replacement_worker_verify_command(
            self.project,ctx,self.verify
        )
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ):
            self.assertTrue(supervisor._tool_is_exact_leaf_verify(
                self.leaf,"bash",{"command":wrapped},self.sid
            ))
            wrong=worker_sandbox.replacement_worker_verify_command(
                self.project,
                {"session":"other-session","agent":"implementer"},
                self.verify,
            )
            self.assertFalse(supervisor._tool_is_exact_leaf_verify(
                self.leaf,"bash",{"command":wrong},self.sid
            ))

        path_leaf=dict(self.leaf)
        path_leaf["verify_command"]=".opencode-v2/bin/run-checks"
        self.assertTrue(supervisor._tool_is_exact_leaf_verify(
            path_leaf,"bash",{"command":"'.opencode-v2/bin/run-checks'"},self.sid
        ))
        self.assertFalse(supervisor._tool_is_exact_leaf_verify(
            path_leaf,"bash",
            {"command":".opencode-v2/bin/run-checks && true"},
            self.sid,
        ))

    def test_exact_verify_failure_counter_counts_only_completed_nonzero_exact_runs(self):
        rows=[
            {"command":self.verify,"status":"completed","exit_code":1},
            {"command":"echo other","status":"completed","exit_code":1},
            {"command":self.verify,"status":"completed","exit_code":2},
            {"command":self.verify,"status":"completed","exit_code":0},
            {"command":self.verify,"status":"running","exit_code":1},
        ]
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"persisted_model_bash_commands",return_value=rows
        ):
            self.assertEqual(
                supervisor.plan_contract_session_exact_verify_failure_count(
                    self.sid,self.leaf
                ),
                2,
            )

    def test_two_failed_exact_verifies_end_attempt_without_forced_contract_challenge(self):
        rows=[
            {"command":self.verify,"status":"completed","exit_code":1},
            {"command":self.verify,"status":"completed","exit_code":1},
        ]
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=8
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"persisted_model_bash_commands",return_value=rows
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"edit",{
                    "filePath":str(self.project/"owned.txt"),
                    "oldString":"ok\n","newString":"different\n",
                }
            )
        self.assertEqual(state,"implementation-return-required")
        self.assertIn("EXACT_VERIFY_FAILURE_LIMIT",detail)
        self.assertIn("exact_verify_failures=2",detail)
        self.assertIn("return-without-tools",detail)
        self.assertIn("finalize_failed_verify=true",detail)
        self.assertNotIn("CONTRACT_CHALLENGE_REQUIRED",detail)

    def test_failed_reverify_allows_only_explicit_authoritative_source_reads(self):
        (self.project/"package.json").write_text("{}\n")
        (self.project/"src").mkdir()
        (self.project/"src/dep.js").write_text("export const x=1;\n")
        context=self.ctrl/"query/leaves/D001-context.json"
        context.parent.mkdir(parents=True)
        context.write_text(json.dumps({
            "supervisor_execution_correction":{
                "authoritative_sources":[
                    "package.json","src/dep.js","https://example.invalid/x",
                    "../escape","/absolute",".opencode-v2/secret",
                ]
            },
            "parent_supervisor_execution_correction":{},
        }))
        self.assertEqual(
            supervisor.implementation_repair_authoritative_read_targets(self.did),
            ["package.json","src/dep.js"],
        )
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=4
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ), mock.patch.object(
            supervisor,"persisted_exact_project_read_seen",return_value=False
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"src/dep.js")}
            )
            self.assertEqual(state,"implementation-authoritative-read-once")
            self.assertIn("PLAN_CONTRACT_REPAIR_SOURCE_READ",detail)

            state,_=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"not-authorized.js")}
            )
            self.assertEqual(state,"implementation-write-required")

    def test_failed_reverify_historical_delta_does_not_bypass_current_session_repair_gate(self):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=6
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(True,"changed")
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ), mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=[]
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"unrelated.txt")}
            )
            self.assertEqual(state,"implementation-write-required")
            self.assertIn("PLAN_CONTRACT_REPAIR_WRITE_REQUIRED",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"edit",{
                    "filePath":str(self.project/"owned.txt"),
                    "oldString":"ok\n","newString":"fixed\n",
                }
            )
            self.assertEqual(state,"implementation-write-only")
            self.assertIn("PLAN_CONTRACT_OWNED_REPAIR",detail)

    def test_authorized_failed_reverify_source_read_survives_top_level_gate(self):
        (self.project/"package.json").write_text("{}\n")
        context=self.ctrl/"query/leaves/D001-context.json"
        context.parent.mkdir(parents=True,exist_ok=True)
        context.write_text(json.dumps({
            "supervisor_execution_correction":{
                "authoritative_sources":["package.json"]
            },
            "parent_supervisor_execution_correction":{},
        }))
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=4
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(True,"changed")
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ), mock.patch.object(
            supervisor,"persisted_exact_project_read_seen",return_value=False
        ), mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=[]
        ):
            state,detail=supervisor.enforce_early_write_gate(
                self.sid,"read",{"filePath":str(self.project/"package.json")}
            )
        self.assertEqual(state,"implementation-authoritative-read-once")
        self.assertIn("PLAN_CONTRACT_REPAIR_SOURCE_READ",detail)

    def test_post_write_reverify_requires_exact_verify_before_more_reads(self):
        mutation={
            "tool":"edit","status":"completed","error":"",
            "input":{
                "filePath":str(self.project/"owned.txt"),
                "oldString":"ok\n","newString":"fixed\n",
            },
        }
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"persisted_completed_tool_turns",return_value=5
        ), mock.patch.object(
            supervisor,"attempt_sequence_for_session",return_value=2
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(True,"changed")
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ), mock.patch.object(
            supervisor,"session_completed_tool_records",
            return_value=[mutation]
        ):
            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"read",{"filePath":str(self.project/"owned.txt")}
            )
            self.assertEqual(state,"implementation-exact-verify-required")
            self.assertIn("POST_WRITE_VERIFY_REQUIRED",detail)
            self.assertIn("no_dependency_reread=true",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"bash",{"command":self.verify}
            )
            self.assertEqual(state,"implementation-exact-verify")
            self.assertIn("POST_WRITE_VERIFY",detail)

            state,detail=supervisor.implementation_direct_write_gate_state(
                self.sid,"edit",{
                    "filePath":str(self.project/"owned.txt"),
                    "oldString":"fixed\n","newString":"fixed2\n",
                }
            )
            self.assertEqual(state,"implementation-exact-verify-required")
            self.assertIn("one_repair_per_verify_cycle=true",detail)

    def test_runtime_prompt_explains_exact_verify_only_exception(self):
        prompt=supervisor.implementation_runtime_prompt(
            self.did,"implementer"
        )
        self.assertIn("PLAN-CONTRACT REVERIFY ORDER — EXACT",prompt)
        self.assertIn("verify_command UNCHANGED",prompt)
        self.assertIn("Do NOT mutate any owned artifact before this exact Verify",prompt)
        self.assertIn("STOP using tools and return immediately",prompt)
        self.assertIn("authoritative_sources",prompt)
        self.assertLessEqual(len(prompt),supervisor.MAX_IMPLEMENTATION_PROMPT_CHARS)
        self.assertNotIn(
            "NEXT tool-bearing response MUST write/edit an owned artifact. Start with",
            prompt,
        )


class PlanContractAuthoritativeReadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.did="D001"
        self.sid="s8"
        self.verify="node tests/check.js"
        leaf={
            "id":self.did,"name":"owned","outcome":"owned correct",
            "owned_artifacts":"`owned.txt`, `tests/check.js`",
            "owned_artifact_paths":["owned.txt","tests/check.js"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"runtime check proves behavior",
            "acceptance_ids":["A001"],"parallel":"none",
            "split_children":[],"complexity":"S",
        }
        (self.project/"owned.txt").write_text("old\n")
        (self.project/"tests").mkdir()
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:leaf},
        }))
        entry={
            "automatic_limit":2,"count":8,
            "sessions":["s1","s2","s3","s4","s5","s6","s7",self.sid],
            "failure_history":[
                {"attempt":1,"classification":"infrastructure",
                 "reason":"i1","session":"s1"},
                {"attempt":2,"classification":"infrastructure",
                 "reason":"i2","session":"s2"},
                {"attempt":3,"classification":"infrastructure",
                 "reason":"i3","session":"s3"},
                {"attempt":4,"classification":"genuine",
                 "reason":"g1","session":"s4",
                 "recovered_by":"runtime-ownership-attribution-repair"},
                {"attempt":5,"classification":"genuine",
                 "reason":"g2","session":"s5",
                 "recovered_by":"runtime-ownership-attribution-repair"},
                {"attempt":8,"classification":"genuine",
                 "reason":"owned-artifacts-missing","session":self.sid,
                 "source":"supervisor"},
            ],
            "infrastructure_retry_grants":3,
            "infrastructure_failures":[
                {"grant":1,"source":"supervisor","kind":"runtime-cancel",
                 "session":"s1","timestamp":"2026-09-26T00:00:01Z",
                 "evidence":"no-owned-artifact-or-progress","reason":"i1"},
                {"grant":1,"source":"supervisor","kind":"runtime-cancel",
                 "session":"s2","timestamp":"2026-09-26T00:00:02Z",
                 "evidence":"no-owned-artifact-or-progress","reason":"i2"},
                {"grant":1,"source":"supervisor","kind":"runtime-cancel",
                 "session":"s3","timestamp":"2026-09-26T00:00:03Z",
                 "evidence":"durable-partial-state-preserved","reason":"i3"},
            ],
            "operator_retry_grants":1,
            "operator_overrides":[{
                "grant":1,"source":"operator-cli",
                "timestamp":"2026-09-26T00:00:10Z","reason":"test",
            }],
            "plan_contract_revisions":[
                {"attempt":6,"source":"supervisor-plan-contract-revision",
                 "previous_verify_sha256":"a"*64,
                 "current_verify_sha256":"b"*64,
                 "timestamp":"2026-09-26T00:01:00Z"},
                {"attempt":7,"source":"supervisor-plan-contract-revision",
                 "previous_verify_sha256":"a"*64,
                 "current_verify_sha256":"b"*64,
                 "timestamp":"2026-09-26T00:02:00Z"},
            ],
            "operator_retry_attempts":[
                {"sequence":6,"session":"s6",
                 "state":"plan_contract_replacement",
                 "outcome":"plan_contract_replacement",
                 "consumes_operator_grant":False,"source":"supervisor",
                 "normalized_by":"supervisor-credit-authority-v1",
                 "normalized_at":"2026-09-26T00:01:10Z"},
                {"sequence":7,"session":"s7",
                 "state":"infrastructure_abort",
                 "outcome":"infrastructure_abort",
                 "consumes_operator_grant":False,"source":"supervisor",
                 "evidence":"immediate-runtime-cancel zero-token-zero-tool aborted"},
                {"sequence":8,"session":self.sid,
                 "state":"consumed","outcome":"meaningful_execution",
                 "consumes_operator_grant":True,"source":"supervisor",
                 "evidence":"completed-worker-tool-action"},
            ],
        }
        self.assertTrue(control_state.attempt_state(entry)["valid"])
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{self.did:entry},
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def patches(self,denied=True):
        evidence={
            "exact_verify_state":"failed",
            "authorized_sources":["src/dep.js"],
            "denied_authoritative_reads":["src/dep.js"] if denied else [],
            "invalid_write_attempts":1,
        }
        return (
            mock.patch.object(
                supervisor,"v1_session_status_snapshot",return_value={}
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_pending",return_value=True
            ),
            mock.patch.object(
                supervisor,"plan_contract_authoritative_read_gate_evidence",
                return_value=evidence
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
            mock.patch.object(
                supervisor,"plan_contract_authoritative_read_fix_installed",
                return_value=True
            ),
        )

    def test_rearms_same_operator_attempt_without_new_grant(self):
        patches=self.patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4]:
            self.assertEqual(
                supervisor.recover_plan_contract_authoritative_read_gate_failure(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],8)
        self.assertEqual(entry["infrastructure_retry_grants"],3)
        self.assertEqual(entry["operator_retry_grants"],1)
        self.assertFalse(any(
            row.get("attempt")==8
            for row in entry.get("failure_history",[])
        ))
        self.assertTrue(entry["sessions"][-1].startswith(
            "dispatch:plan-reverify-authoritative-read-recovery:"
        ))
        op=entry["operator_retry_attempts"][-1]
        self.assertEqual(op["sequence"],8)
        self.assertEqual(op["state"],"reserved")
        self.assertFalse(op["consumes_operator_grant"])
        recovery=entry["plan_contract_authoritative_read_recoveries"][-1]
        self.assertEqual(recovery["session"],self.sid)
        self.assertEqual(
            recovery["original_failure_record"]["reason"],
            "owned-artifacts-missing",
        )
        self.assertEqual(
            recovery["original_operator_record"]["state"],"consumed"
        )
        self.assertEqual(
            recovery["denied_authoritative_reads"],["src/dep.js"]
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["count"],8)
        self.assertEqual(state["operator_grants_reserved"],1)

    def test_evidence_reads_invalid_write_parse_error_from_input_payload(self):
        records=[
            {
                "tool":"read","status":"completed",
                "input":{"filePath":str(self.project/"src/dep.js")},
                "error":"",
            },
            {
                "tool":"invalid","status":"error",
                "input":{"error":"Invalid input for tool write: JSON parsing failed: truncated"},
                "error":"EARLY_WRITE_IMPLEMENTATION_WRITE_REQUIRED IMPLEMENTATION_WRITE_REQUIRED",
            },
        ]
        with mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="failed"
        ), mock.patch.object(
            supervisor,"implementation_repair_authoritative_read_targets",
            return_value=["src/dep.js"]
        ), mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=records
        ):
            evidence=supervisor.plan_contract_authoritative_read_gate_evidence(
                self.sid,self.did
            )
        self.assertEqual(evidence["completed_authoritative_reads"],["src/dep.js"])
        self.assertEqual(evidence["invalid_write_attempts"],1)
        self.assertEqual(evidence["write_required_denials"],1)

    def test_rearms_one_write_serialization_failure_after_bounded_reads(self):
        evidence={
            "exact_verify_state":"failed",
            "authorized_sources":["src/dep.js"],
            "completed_authoritative_reads":["src/dep.js"],
            "denied_authoritative_reads":[],
            "invalid_write_attempts":1,
            "write_required_denials":1,
        }
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"plan_contract_reverify_pending",return_value=True
        ), mock.patch.object(
            supervisor,"plan_contract_authoritative_read_gate_evidence",
            return_value=evidence
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ), mock.patch.object(
            supervisor,"plan_contract_small_write_fix_installed",
            return_value=True
        ):
            self.assertEqual(
                supervisor.recover_plan_contract_authoritative_read_gate_failure(
                    self.did
                ),
                (True,"rearmed-same-attempt"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["count"],8)
        self.assertTrue(entry["sessions"][-1].startswith(
            "dispatch:plan-reverify-write-serialization-recovery:"
        ))
        self.assertFalse(any(
            row.get("attempt")==8
            for row in entry.get("failure_history",[])
        ))
        op=entry["operator_retry_attempts"][-1]
        self.assertEqual(
            op["rearmed_by"],
            supervisor.PLAN_CONTRACT_WRITE_SERIALIZATION_RECOVERY_PROTOCOL,
        )
        recovery=entry["plan_contract_authoritative_read_recoveries"][-1]
        self.assertEqual(recovery["recovery_kind"],"write-serialization")
        self.assertEqual(
            recovery["protocol"],
            supervisor.PLAN_CONTRACT_WRITE_SERIALIZATION_RECOVERY_PROTOCOL,
        )
        self.assertEqual(
            recovery["completed_authoritative_reads"],["src/dep.js"]
        )
        self.assertEqual(recovery["invalid_write_attempts"],1)
        self.assertEqual(recovery["write_required_denials"],1)
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertEqual(state["operator_grants_reserved"],1)

    def test_refuses_second_write_serialization_recovery_same_attempt(self):
        data=json.loads((self.work/"attempts.json").read_text())
        data["deliverables"][self.did][
            "plan_contract_authoritative_read_recoveries"
        ]=[{
            "attempt":8,
            "session":"prior-session",
            "recovery_kind":"write-serialization",
        }]
        (self.work/"attempts.json").write_text(json.dumps(data))
        evidence={
            "exact_verify_state":"failed",
            "authorized_sources":["src/dep.js"],
            "completed_authoritative_reads":["src/dep.js"],
            "denied_authoritative_reads":[],
            "invalid_write_attempts":1,
            "write_required_denials":1,
        }
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"plan_contract_reverify_pending",return_value=True
        ), mock.patch.object(
            supervisor,"plan_contract_authoritative_read_gate_evidence",
            return_value=evidence
        ):
            self.assertEqual(
                supervisor.recover_plan_contract_authoritative_read_gate_failure(
                    self.did
                ),
                (
                    False,
                    "authoritative-read-recovery-write-serialization-limit",
                ),
            )

    def test_refuses_without_denied_authoritative_read(self):
        patches=self.patches(denied=False)
        with patches[0],patches[1],patches[2],patches[3],patches[4]:
            self.assertEqual(
                supervisor.recover_plan_contract_authoritative_read_gate_failure(
                    self.did
                ),
                (
                    False,
                    "authoritative-read-recovery-denied-source-proof-missing",
                ),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        self.assertEqual(entry["sessions"][-1],self.sid)
        self.assertTrue(any(
            row.get("attempt")==8
            for row in entry.get("failure_history",[])
        ))


class ExactVerifyShellSemanticsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.did="D001"
        self.sid="s3"
        self.verify="test -s owned.txt && true"
        self.leaf={
            "id":self.did,"name":"owned","outcome":"owned remains correct",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"owned.txt remains valid","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.project/"owned.txt").write_text("ok\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:self.leaf},
        }))
        current_sha=hashlib.sha256(self.verify.encode()).hexdigest()
        entry={
            "automatic_limit":2,
            "count":3,
            "sessions":["s1","s2",self.sid],
            "failure_history":[
                {
                    "attempt":1,"classification":"genuine",
                    "reason":"verify-failed-1","session":"s1",
                    "source":"supervisor",
                },
                {
                    "attempt":2,"classification":"infrastructure",
                    "reason":"old-runtime","session":"s2",
                    "source":"supervisor",
                },
                {
                    "attempt":3,"classification":"genuine",
                    "reason":"verify-failed-143","session":self.sid,
                    "source":"supervisor",
                },
            ],
            "infrastructure_retry_grants":1,
            "infrastructure_failures":[{
                "timestamp":"2026-09-25T00:00:00Z",
                "grant":1,"source":"supervisor","kind":"runtime-cancel",
                "session":"s2","evidence":"no-owned-artifact-or-progress",
                "reason":"old-runtime",
            }],
            "plan_contract_revisions":[{
                "attempt":2,
                "source":"supervisor-plan-contract-revision",
                "previous_verify_sha256":"old",
                "current_verify_sha256":current_sha,
                "timestamp":"2026-09-26T00:00:00Z",
            }],
            "split_required":{
                "generation":1,
                "reason":"genuine-failure-threshold",
                "timestamp":"2026-09-26T00:00:00Z",
            },
        }
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{self.did:entry},
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def recovery_patches(self,worker_exact=True):
        commands=(
            [{"command":self.verify,"status":"completed","output":"","error":"","exit_code":0}]
            if worker_exact else []
        )
        verify={
            "attempt":3,"session":self.sid,"executed":True,
            "result":"verify-failed-143","exit_code":143,
            "command":self.verify,
        }
        return (
            mock.patch.object(
                supervisor,"v1_session_status_snapshot",return_value={}
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_pending",return_value=True
            ),
            mock.patch.object(
                supervisor,"persisted_model_bash_commands",
                return_value=commands
            ),
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="implementer"
            ),
            mock.patch.object(
                supervisor,"load_supervisor_verify_evidence",
                return_value={"latest":verify}
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
            mock.patch.object(
                supervisor,"exact_verify_shell_semantics_fix_installed",
                return_value=True
            ),
        )

    def test_reclassifies_only_proven_exit_143_and_clears_false_split(self):
        patches=self.recovery_patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            self.assertEqual(
                supervisor.recover_exact_verify_shell_semantics_failure(
                    self.did
                ),
                (True,"recovered"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        failure=next(
            row for row in entry["failure_history"]
            if row["attempt"]==3
        )
        self.assertEqual(failure["classification"],"infrastructure")
        self.assertEqual(
            failure["reclassified_by"],
            "runtime-exact-verify-shell-semantics-repair",
        )
        self.assertEqual(entry["infrastructure_retry_grants"],2)
        self.assertNotIn("split_required",entry)
        recovery=entry["exact_verify_shell_semantics_recoveries"][0]
        self.assertEqual(recovery["session"],self.sid)
        self.assertEqual(recovery["supervisor_exit_code"],143)
        self.assertTrue(recovery["owned_state_restored"])
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertGreater(state["allowed_attempts"],entry["count"])

    def test_refuses_without_persisted_exact_worker_success(self):
        patches=self.recovery_patches(worker_exact=False)
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5],patches[6]:
            ok,detail=supervisor.recover_exact_verify_shell_semantics_failure(
                self.did
            )
        self.assertFalse(ok)
        self.assertEqual(
            detail,"exact-verify-shell-recovery-worker-exact-pass-missing"
        )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        failure=next(
            row for row in entry["failure_history"]
            if row["attempt"]==3
        )
        self.assertEqual(failure["classification"],"genuine")
        self.assertIn("split_required",entry)

    def test_recurrence_can_use_prior_same_command_recovery_proof(self):
        first=self.recovery_patches()
        with first[0],first[1],first[2],first[3],first[4],first[5],first[6]:
            self.assertEqual(
                supervisor.recover_exact_verify_shell_semantics_failure(
                    self.did
                ),
                (True,"recovered"),
            )

        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"][self.did]
        entry["count"]=4
        entry["sessions"].append("s4")
        entry["failure_history"].append({
            "attempt":4,"classification":"genuine",
            "reason":"verify-failed-143","session":"s4",
            "source":"supervisor",
        })
        entry["split_required"]={
            "generation":1,"reason":"genuine-failure-threshold",
            "timestamp":"2026-09-26T01:00:00Z",
        }
        (self.work/"attempts.json").write_text(json.dumps(data))

        prior_verify={
            "attempt":3,"session":self.sid,"executed":True,
            "result":"verify-failed-143","exit_code":143,
            "command":self.verify,
        }
        current_verify={
            "attempt":4,"session":"s4","executed":True,
            "result":"verify-failed-143","exit_code":143,
            "command":self.verify,
        }

        def bash_history(sid,agent):
            if sid==self.sid:
                return [{
                    "command":self.verify,"status":"completed",
                    "output":"","error":"","exit_code":0,
                }]
            return []

        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"plan_contract_reverify_pending",return_value=True
        ), mock.patch.object(
            supervisor,"persisted_model_bash_commands",
            side_effect=bash_history
        ), mock.patch.object(
            supervisor,"_session_agent_db",return_value="implementer"
        ), mock.patch.object(
            supervisor,"load_supervisor_verify_evidence",
            return_value={
                "entries":[prior_verify,current_verify],
                "latest":current_verify,
            }
        ), mock.patch.object(
            supervisor,"_owned_artifact_changed_since_execution_baseline",
            return_value=(False,"unchanged")
        ), mock.patch.object(
            supervisor,"exact_verify_shell_semantics_fix_installed",
            return_value=True
        ):
            self.assertEqual(
                supervisor.recover_exact_verify_shell_semantics_failure(
                    self.did
                ),
                (True,"recovered"),
            )

        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        failure=next(
            row for row in entry["failure_history"]
            if row["attempt"]==4
        )
        self.assertEqual(failure["classification"],"infrastructure")
        recovery=entry["exact_verify_shell_semantics_recoveries"][-1]
        self.assertEqual(recovery["worker_exact_completed"],0)
        self.assertEqual(recovery["recurrence_prior_attempt"],3)
        self.assertEqual(recovery["prior_worker_exact_completed"],1)
        self.assertEqual(entry["infrastructure_retry_grants"],3)
        self.assertNotIn("split_required",entry)


class PlanContractReverifySteeringRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.did="D001"
        self.sid="s3"
        self.verify="test -s owned.txt && true"
        self.leaf={
            "id":self.did,"name":"owned","outcome":"owned remains correct",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"owned.txt remains valid","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        (self.project/"owned.txt").write_text("ok\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{self.did:self.leaf},
        }))
        current_sha=hashlib.sha256(self.verify.encode()).hexdigest()
        entry={
            "automatic_limit":2,
            "count":3,
            "sessions":["s1","s2",self.sid],
            "failure_history":[
                {
                    "attempt":1,"classification":"genuine",
                    "reason":"verify-failed-1","session":"s1",
                    "source":"supervisor",
                },
                {
                    "attempt":2,"classification":"infrastructure",
                    "reason":"old-runtime","session":"s2",
                    "source":"supervisor",
                },
                {
                    "attempt":3,"classification":"genuine",
                    "reason":"verify-failed-143","session":self.sid,
                    "source":"supervisor",
                },
            ],
            "infrastructure_retry_grants":1,
            "infrastructure_failures":[{
                "timestamp":"2026-09-25T00:00:00Z",
                "grant":1,"source":"supervisor","kind":"runtime-cancel",
                "session":"s2","evidence":"no-owned-artifact-or-progress",
                "reason":"old-runtime",
            }],
            "plan_contract_revisions":[{
                "attempt":2,
                "source":"supervisor-plan-contract-revision",
                "previous_verify_sha256":"old",
                "current_verify_sha256":current_sha,
                "timestamp":"2026-09-26T00:00:00Z",
            }],
            "split_required":{
                "generation":1,
                "reason":"genuine-failure-threshold",
                "timestamp":"2026-09-26T00:00:00Z",
            },
        }
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{self.did:entry},
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def patches(self,evidence=None):
        if evidence is None:
            evidence={
                "write_required_denials":1,
                "forced_owned_mutations":2,
                "exact_verify_attempts":0,
                "model_followed_write_steering":True,
            }
        verify={
            "attempt":3,"session":self.sid,"executed":True,
            "result":"verify-failed-143","exit_code":143,
            "command":self.verify,
        }
        return (
            mock.patch.object(
                supervisor,"v1_session_status_snapshot",return_value={}
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_pending",return_value=True
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_steering_evidence",
                return_value=evidence
            ),
            mock.patch.object(
                supervisor,"load_supervisor_verify_evidence",
                return_value={"latest":verify}
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
            mock.patch.object(
                supervisor,"plan_contract_reverify_steering_fix_installed",
                return_value=True
            ),
        )

    def test_refunds_only_proven_forced_write_steering_and_clears_split(self):
        patches=self.patches()
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5]:
            self.assertEqual(
                supervisor.recover_plan_contract_reverify_steering_failure(
                    self.did
                ),
                (True,"recovered"),
            )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        failure=next(
            row for row in entry["failure_history"]
            if row["attempt"]==3
        )
        self.assertEqual(failure["classification"],"infrastructure")
        self.assertEqual(
            failure["reclassified_by"],
            "runtime-plan-reverify-steering-repair",
        )
        self.assertEqual(entry["infrastructure_retry_grants"],2)
        self.assertNotIn("split_required",entry)
        recovery=entry["plan_contract_reverify_steering_recoveries"][0]
        self.assertEqual(recovery["write_required_denials"],1)
        self.assertEqual(recovery["forced_owned_mutations"],2)
        self.assertTrue(recovery["owned_state_restored"])
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertGreater(state["allowed_attempts"],entry["count"])

    def test_refuses_if_exact_verify_was_already_attempted(self):
        evidence={
            "write_required_denials":1,
            "forced_owned_mutations":1,
            "exact_verify_attempts":1,
            "model_followed_write_steering":True,
        }
        patches=self.patches(evidence)
        with patches[0],patches[1],patches[2],patches[3],patches[4],patches[5]:
            ok,detail=supervisor.recover_plan_contract_reverify_steering_failure(
                self.did
            )
        self.assertFalse(ok)
        self.assertEqual(
            detail,
            "plan-reverify-steering-recovery-exact-verify-was-attempted",
        )
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ][self.did]
        failure=next(
            row for row in entry["failure_history"]
            if row["attempt"]==3
        )
        self.assertEqual(failure["classification"],"genuine")


class OwnershipPrefixFirewallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.sid="ses-prefix-denial"
        self.verify="test -s reference/reference_fixtures_manifest.json"
        self.leaf={
            "id":"D001","name":"fixtures","outcome":"write fixture directory",
            "owned_artifacts":"`reference/fixtures`, `reference/reference_fixtures_manifest.json`",
            "owned_artifact_paths":[
                "reference/fixtures",
                "reference/reference_fixtures_manifest.json",
            ],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"core-builder",
            "done_when":"fixture files exist","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"M",
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.leaf},
        }))
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{"D001":{
                "automatic_limit":2,
                "count":1,
                "sessions":[self.sid],
                "failure_history":[{
                    "attempt":1,
                    "classification":"genuine",
                    "reason":"verify-failed-1",
                    "session":self.sid,
                    "source":"supervisor",
                }],
            }},
        }))
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"",
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001",self.sid,self.verify,checked,"verify-failed-1"
        )
        self.violation=supervisor.worker_sandbox_violation_path(
            self.project,"D001",self.sid
        )
        self.violation.parent.mkdir(parents=True,exist_ok=True)

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def write_violation(self,path):
        self.violation.write_text(json.dumps({
            "agent":"core-builder",
            "attempt":1,
            "deliverable":"D001",
            "kind":"direct-tool-outside-ownership",
            "session":self.sid,
            "detail":{
                "tool":"write",
                "path":path,
                "owned":[
                    "reference/fixtures",
                    "reference/reference_fixtures_manifest.json",
                ],
            },
        })+"\n")

    def test_reclassifies_canonical_owned_descendant_denial(self):
        denied="reference/fixtures/epoch-0001.json"
        self.write_violation(denied)
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ):
            self.assertEqual(
                supervisor.recover_ownership_prefix_firewall_failure("D001"),
                (True,"recovered"),
            )

        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        failure=entry["failure_history"][0]
        self.assertEqual(failure["classification"],"infrastructure")
        self.assertEqual(failure["original_reason"],"verify-failed-1")
        self.assertEqual(
            failure["reclassified_by"],
            "runtime-ownership-prefix-firewall-repair",
        )
        self.assertEqual(entry["infrastructure_retry_grants"],1)
        self.assertEqual(
            entry["ownership_prefix_firewall_recoveries"][0]["denied_paths"],
            [denied],
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"],state)
        self.assertGreater(state["allowed_attempts"],entry["count"])

    def test_refuses_genuinely_unowned_denial(self):
        self.write_violation("reference/generate_fixtures.py")
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ):
            self.assertEqual(
                supervisor.recover_ownership_prefix_firewall_failure("D001"),
                (False,"ownership-prefix-recovery-no-canonical-owned-denial"),
            )


class ExternalExecutionContractRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        (self.ctrl/"ACCEPTANCE.md").write_text(
            "# Acceptance\n\nReference policy: external-required\n"
        )
        self.verify="test -s owned.txt"
        self.leaf={
            "id":"D001","name":"external fixture",
            "outcome":"capture external fixture",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":self.verify,"role":"implementer",
            "done_when":"owned exists","acceptance_ids":["A001"],
            "parallel":"none","split_children":[],"complexity":"S",
        }
        manifest={
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.leaf},
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(
            json.dumps(manifest)
        )
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{"D001":{
                "automatic_limit":3,
                "count":3,
                "sessions":["s1","s2","s3"],
                "failure_history":[
                    {"attempt":1,"classification":"genuine",
                     "reason":"verify-failed-1"},
                    {"attempt":2,"classification":"genuine",
                     "reason":"verify-failed-1"},
                    {"attempt":3,"classification":"genuine",
                     "reason":"verify-failed-1"},
                ],
            }},
        }))
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"still incomplete\n"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001","s3",self.verify,checked,"verify-failed-1"
        )
        self.correction=self.project/"correction.json"
        self.correction_text=(
            "Authoritative external API documentation and a live probe prove "
            "the inherited query parameter names are invalid. Use COMMAND, "
            "EPHEM_TYPE=VECTORS, explicit CENTER, START_TIME/STOP_TIME/STEP_SIZE, "
            "TIME_TYPE, REF_SYSTEM, OUT_UNITS, VEC_TABLE and VEC_CORR. Preserve "
            "the canonical owned artifacts and exact Verify."
        )
        self.correction.write_text(json.dumps({
            "protocol":
                supervisor.EXTERNAL_EXECUTION_CONTRACT_CORRECTION_PROTOCOL,
            "deliverable":"D001",
            "correction":self.correction_text,
            "authoritative_sources":[
                "https://ssd-api.jpl.nasa.gov/doc/horizons.html"
            ],
            "evidence":{"live_probe":"HTTP 200 with non-error vectors"},
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def test_reclassifies_terminal_external_contract_and_injects_correction(self):
        initial=json.loads((self.work/"attempts.json").read_text())
        self.assertEqual(
            control_state.attempt_state(
                initial["deliverables"]["D001"]
            )["allowed_attempts"],
            3,
        )
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ):
            self.assertEqual(
                supervisor.recover_external_execution_contract(
                    "D001",str(self.correction)
                ),
                (True,"recovered"),
            )

        data=json.loads((self.work/"attempts.json").read_text())
        entry=data["deliverables"]["D001"]
        self.assertEqual(entry["count"],3)
        latest=entry["failure_history"][-1]
        self.assertEqual(latest["classification"],"bad-plan")
        self.assertEqual(
            latest["reclassified_by"],
            "runtime-external-execution-contract-repair",
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["external_contract_retry_grants"],1)
        self.assertEqual(state["allowed_attempts"],4)

        correction_path=(
            self.work/"D001.execution-contract-correction.json"
        )
        correction=json.loads(correction_path.read_text())
        self.assertEqual(correction["owner"],"supervisor")
        self.assertEqual(
            correction["correction_sha256"],
            hashlib.sha256(self.correction_text.encode()).hexdigest(),
        )

        import control_query_views
        manifest=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text()
        )
        packet=control_query_views.build_leaf_contexts(
            self.project,manifest
        )["D001"]
        projected=packet["supervisor_execution_correction"]
        self.assertEqual(projected["correction"],self.correction_text)
        self.assertEqual(
            projected["authoritative_sources"],
            ["https://ssd-api.jpl.nasa.gov/doc/horizons.html"],
        )

        status,sequence=supervisor.claim_attempt(
            "dispatch:external-contract-credit","D001"
        )
        self.assertEqual((status,sequence),("claimed",4))
        data=json.loads((self.work/"attempts.json").read_text())
        self.assertFalse(
            data["deliverables"]["D001"].get("operator_retry_attempts")
        )

    def test_split_child_projects_parent_execution_correction(self):
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ):
            self.assertEqual(
                supervisor.recover_external_execution_contract(
                    "D001",str(self.correction)
                ),
                (True,"recovered"),
            )

        import control_query_views
        manifest=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text()
        )
        child=dict(self.leaf)
        child.update({
            "id":"D001-A",
            "parent":"D001",
            "owned_artifacts":"none",
            "owned_artifact_paths":[],
            "split_handoff_only":True,
            "split_handoff_source":"",
            "split_reads_existing":["owned.txt"],
            "split_creates_or_updates":[],
        })
        manifest["leaves"]["D001-A"]=child

        packet=control_query_views.build_leaf_contexts(
            self.project,manifest
        )["D001-A"]
        self.assertEqual(packet["supervisor_execution_correction"],{})
        inherited=packet["parent_supervisor_execution_correction"]
        self.assertEqual(inherited["correction"],self.correction_text)
        self.assertEqual(
            inherited["authoritative_sources"],
            ["https://ssd-api.jpl.nasa.gov/doc/horizons.html"],
        )

    def test_recovery_is_bounded_and_requires_external_policy(self):
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ):
            self.assertEqual(
                supervisor.recover_external_execution_contract(
                    "D001",str(self.correction)
                ),
                (True,"recovered"),
            )
            self.assertEqual(
                supervisor.recover_external_execution_contract(
                    "D001",str(self.correction)
                ),
                (True,"already-recovered"),
            )
        (self.ctrl/"ACCEPTANCE.md").write_text(
            "# Acceptance\n\nReference policy: internal\n"
        )
        self.assertEqual(
            supervisor.recover_external_execution_contract(
                "D001",str(self.correction)
            ),
            (False,"external-contract-recovery-reference-policy-mismatch"),
        )


class HistoricalParentContractRepairResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        (self.work/"contract-repair-history").mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        mark_phase_ready(
            self.project,"IMPLEMENTATION_PLAN.md","IMPLEMENTATION_PLAN_COMPLETE"
        )
        (self.ctrl/"IMPLEMENTATION_PLAN.structured-map.json").write_text(
            json.dumps({
                "protocol":"v2-structured-plan-map-v1",
                "id_to_key":{"D004":"fixture_manifest"},
            })
        )
        self.history=[
            {
                "attempt":2,
                "classification":"bad-plan",
                "reason":"early_write_deadline_no_owned_artifact_delta",
                "reclassified_by":"runtime-parent-contract-repair",
                "source":"supervisor",
                "timestamp":"2026-09-21T00:00:00Z",
            },
            {
                "attempt":3,
                "classification":"bad-plan",
                "reason":"verify-failed-1",
                "reclassified_by":"runtime-parent-contract-repair",
                "source":"supervisor",
                "timestamp":"2026-09-21T00:01:00Z",
            },
        ]
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D004":{
                    "automatic_limit":3,
                    "count":3,
                    "sessions":["s1","s2","s3"],
                    "failure_history":self.history,
                }
            },
        }))
        (self.work/"contract-repair-history"/"D004.1.json").write_text(
            json.dumps({
                "owner":"supervisor",
                "protocol":"v2-contract-repair-history-v1",
                "parent_id":"D004",
                "archived_at":"2026-09-21T00:02:00Z",
                "records":{"D004.split-status.json":"{}"},
            })
        )
        (self.work/"contract-repair-events.log").write_text(
            "[2026-09-21T00:02:00Z] "
            "PARENT_CONTRACT_REPAIR_REQUESTED deliverable=D004 "
            "key=fixture_manifest field=prerequisite_artifacts reason=test\n"
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def test_recovery_restores_credit_without_rewriting_history(self):
        before=json.loads((self.work/"attempts.json").read_text())
        ok,detail=supervisor.recover_historical_parent_contract_repair_resolution(
            "D004"
        )
        self.assertTrue(ok,detail)
        self.assertEqual(detail,"resolved")
        after=json.loads((self.work/"attempts.json").read_text())
        entry=after["deliverables"]["D004"]
        self.assertEqual(entry["count"],3)
        self.assertEqual(entry["sessions"],["s1","s2","s3"])
        self.assertEqual(entry["failure_history"],self.history)
        marker=entry["parent_contract_repair_resolution"]
        self.assertEqual(marker["structured_key"],"fixture_manifest")
        self.assertEqual(marker["reclassified_attempts"],[2,3])
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["bad_plan_retry_grants"],2)
        self.assertEqual(state["allowed_attempts"],5)
        self.assertEqual(
            before["deliverables"]["D004"]["failure_history"],
            entry["failure_history"],
        )
        ok,detail=supervisor.recover_historical_parent_contract_repair_resolution(
            "D004"
        )
        self.assertTrue(ok,detail)
        self.assertEqual(detail,"already-resolved")

    def test_recovery_fails_closed_without_supervisor_event(self):
        (self.work/"contract-repair-events.log").write_text("")
        ok,detail=supervisor.recover_historical_parent_contract_repair_resolution(
            "D004"
        )
        self.assertFalse(ok)
        self.assertEqual(detail,"historical-parent-repair-event-missing")


class NativeChildBindingAndRestartRecoveryTests(unittest.TestCase):
    def test_materialization_receipt_accepts_supervisor_log_prefix(self):
        stage_a_controller.validate_materialization_output(
            "[2026-09-25T18:35:15+0200] DISPATCH_ALLOW session=child "
            "agent=implementer deliverable=D001 attempt=1\n"
            "DISPATCH_MATERIALIZED session=child deliverable=D001 attempt=1\n",
            "child",
        )

    def test_materialization_receipt_rejects_wrong_or_duplicate_session(self):
        with self.assertRaisesRegex(
            stage_a_controller.ControllerError,"session mismatch"
        ):
            stage_a_controller.validate_materialization_output(
                "DISPATCH_MATERIALIZED session=other deliverable=D001 attempt=1\n",
                "child",
            )
        with self.assertRaisesRegex(
            stage_a_controller.ControllerError,"unexpected output"
        ):
            stage_a_controller.validate_materialization_output(
                "DISPATCH_MATERIALIZED session=child deliverable=D001 attempt=1\n"
                "DISPATCH_MATERIALIZED session=child deliverable=D001 attempt=1\n",
                "child",
            )

    def test_exact_derived_runtime_prompt_may_exceed_transport_cap(self):
        prompt="DELIVERABLE: D001\n" + ("x" * supervisor.MAX_IMPLEMENTATION_PROMPT_CHARS)
        with mock.patch.object(supervisor,"implementation_runtime_prompt",return_value=prompt):
            self.assertEqual(
                supervisor.implementation_runtime_prompt_violation("probe-builder",prompt),""
            )
            self.assertIn(
                "oversized_first_user_prompt",
                supervisor.implementation_runtime_prompt_violation("probe-builder",prompt+"x"),
            )

    def test_controller_binds_exactly_one_observed_child_to_preclaim(self):
        intent={
            "root_session":"root", "baseline_child_ids":[],
            "action":{"agent":"probe-builder","deliverable":"D001"},
        }
        attempts={"count":1,"sessions":["dispatch:token"]}
        children=[{"id":"child","parentID":"root","agent":"probe-builder"}]
        bound={"count":1,"sessions":["child"]}
        with mock.patch.object(stage_a_controller,"materialize_native_child") as materialize, \
             mock.patch.object(stage_a_controller,"attempt_snapshot",return_value=bound):
            actual=stage_a_controller.bind_unbound_native_child(
                Path("/tmp/project"),"http://127.0.0.1:1",intent,"D001",
                "probe-builder",attempts,children,
            )
        self.assertEqual(actual,bound)
        materialize.assert_called_once_with(
            Path("/tmp/project"),"http://127.0.0.1:1","child","probe-builder"
        )

    def test_controller_refuses_multiple_unbound_children(self):
        intent={
            "root_session":"root", "baseline_child_ids":[],
            "action":{"agent":"probe-builder","deliverable":"D001"},
        }
        children=[
            {"id":"child-a","parentID":"root","agent":"probe-builder"},
            {"id":"child-b","parentID":"root","agent":"probe-builder"},
        ]
        with self.assertRaisesRegex(stage_a_controller.ControllerError,"multiple unbound"):
            stage_a_controller.bind_unbound_native_child(
                Path("/tmp/project"),"http://127.0.0.1:1",intent,"D001",
                "probe-builder",{"count":1,"sessions":["dispatch:token"]},children,
            )

    def test_splitter_reconcile_uses_native_child_without_attempt_binding(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            action={"kind":"launch","agent":"task-splitter","deliverable":"D001","generation":1}
            eid="splitter-execution"
            stage_a_controller.save_execution_ledger(project,{
                "owner":"stage-a-controller",
                "protocol":stage_a_controller.EXECUTION_LEDGER_PROTOCOL,
                "executions":{eid:{
                    "execution_id":eid,"state_version":"state","root_session":"root",
                    "action":action,"baseline_attempt":{"count":0,"sessions":[]},
                    "baseline_child_ids":[],
                }},
            })
            child=[{"id":"split-child","parentID":"root","agent":"task-splitter"}]
            with mock.patch.object(stage_a_controller,"child_snapshot",return_value=child), \
                 mock.patch.object(stage_a_controller,"materialize_native_child") as materialize:
                receipt=stage_a_controller.reconcile_execution(project,"http://127.0.0.1:1",eid)
            self.assertTrue(receipt["replay_suppressed"])
            self.assertEqual(receipt["reconciliation"],{
                "kind":"native-child","sessions":["split-child"]
            })
            materialize.assert_not_called()

    def test_busy_pre_restart_pending_child_is_not_an_orphan(self):
        old_start=supervisor.START_MS
        supervisor.START_MS=2000
        try:
            self.assertFalse(
                supervisor.restart_orphan_candidate(
                    "busy-child", {"busy-child"}, {"busy-child"}, 1000
                )
            )
            self.assertTrue(
                supervisor.restart_orphan_candidate(
                    "lost-child", set(), {"lost-child"}, 1000
                )
            )
            self.assertFalse(
                supervisor.restart_orphan_candidate(
                    "not-pending", set(), set(), 1000
                )
            )
            self.assertFalse(
                supervisor.restart_orphan_candidate(
                    "new-child", set(), {"new-child"}, 3000
                )
            )
        finally:
            supervisor.START_MS=old_start

    def test_restart_orphan_is_infrastructure_not_genuine(self):
        sid="orphan"; did="D001"
        old_project=supervisor.PROJECT
        supervisor.PROJECT=tempfile.mkdtemp()
        supervisor.session_task[sid]=(did,1)
        supervisor.post_finalize_seen.discard(sid)
        try:
            with mock.patch.object(supervisor,"ready_info",return_value={}), \
                 mock.patch.object(supervisor,"record_infrastructure_abort",return_value=(True,"granted")) as infra, \
                 mock.patch.object(supervisor,"record_leaf_failure") as failure, \
                 mock.patch.object(supervisor,"release_operator_reservation") as release, \
                 mock.patch.object(supervisor,"worker_sandbox_cleanup_session") as cleanup, \
                 mock.patch.object(supervisor,"log"), \
                 mock.patch.object(supervisor,"csv"):
                supervisor.reconcile_restart_orphaned_implementation_session(sid,"probe-builder")
            infra.assert_called_once_with(
                sid,did,"opencode-server-restart-incomplete-session",
                "opencode-server-restart",
            )
            failure.assert_called_once_with(
                did,"opencode-server-restart-incomplete-session","infrastructure",
                sid=sid,
            )
            release.assert_called_once()
            cleanup.assert_called_once_with(sid)
            self.assertIn(sid,supervisor.post_finalize_seen)
        finally:
            supervisor.session_task.pop(sid,None)
            supervisor.post_finalize_seen.discard(sid)
            supervisor.PROJECT=old_project

class PlannerTargetedRepairToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        ctrl=self.project/".opencode-v2"
        ctrl.mkdir(parents=True)
        (ctrl/"IMPLEMENTATION_PLAN.repair.json").write_text(json.dumps({
            "protocol":"v2-structured-plan-repair-v1",
            "source":"control-guard",
            "whole_plan":False,
            "affected_keys":["app"],
            "errors":[{"message":"D001: Verify command is static-proxy-only","deliverables":["D001"],"keys":["app"]}],
        }))
        (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text("{}\n")
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.sid="planner-repair"
        self.prompt="Repair structured implementation planning for this project.\n"

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def boundary(self,tool,args,seen=(),records=()):
        with mock.patch.object(supervisor,"_session_agent_db",return_value="implementation-planner"), \
             mock.patch.object(supervisor,"first_user_text_db",return_value=self.prompt), \
             mock.patch.object(supervisor,"persisted_exact_project_read_seen",side_effect=lambda _sid,target: target in set(seen)), \
             mock.patch.object(supervisor,"session_completed_tool_records",return_value=list(records)):
            return supervisor.planner_tool_boundary_state(self.sid,tool,args)

    def test_targeted_repair_allows_only_canonical_reads_before_edit(self):
        state,_=self.boundary("read",{"filePath":".opencode-v2/IMPLEMENTATION_PLAN.repair.json"})
        self.assertEqual(state,"allow")
        state,_=self.boundary("read",{"filePath":".opencode-v2/IMPLEMENTATION_PLAN.structured.json"})
        self.assertEqual(state,"allow")
        state,detail=self.boundary("read",{"filePath":"tests/harness.js"})
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_READ_DENY",detail)

    def test_targeted_repair_requires_both_reads_then_only_structured_edit(self):
        required={
            ".opencode-v2/IMPLEMENTATION_PLAN.repair.json",
            ".opencode-v2/IMPLEMENTATION_PLAN.structured.json",
        }
        state,detail=self.boundary(
            "edit",{"filePath":".opencode-v2/IMPLEMENTATION_PLAN.structured.json"},
            seen={".opencode-v2/IMPLEMENTATION_PLAN.repair.json"},
        )
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_CONTEXT_REQUIRED",detail)
        state,_=self.boundary(
            "edit",{"filePath":".opencode-v2/IMPLEMENTATION_PLAN.structured.json"},seen=required
        )
        self.assertEqual(state,"allow")
        state,detail=self.boundary(
            "write",{"filePath":".opencode-v2/IMPLEMENTATION_PLAN.structured.json"},seen=required
        )
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_WRITE_DENY",detail)
        state,detail=self.boundary("edit",{"filePath":"package.json"},seen=required)
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_EDIT_DENY",detail)

    def test_reread_is_allowed_only_after_structured_edit_progress(self):
        structured=".opencode-v2/IMPLEMENTATION_PLAN.structured.json"
        prior_read={
            "tool":"read","input":{"filePath":structured},
            "status":"completed","error":"",
        }
        state,detail=self.boundary(
            "read",{"filePath":structured},records=[prior_read]
        )
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_READ_ALREADY_COMPLETE",detail)

        prior_edit={
            "tool":"edit","input":{"filePath":structured},
            "status":"completed","error":"",
        }
        state,detail=self.boundary(
            "read",{"filePath":structured},records=[prior_read,prior_edit]
        )
        self.assertEqual(state,"allow")
        self.assertIn("targeted-repair-required-read",detail)

    def test_acceptance_read_is_not_allowed_for_unrelated_verify_repair(self):
        state,detail=self.boundary("read",{"filePath":".opencode-v2/ACCEPTANCE.md"})
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_READ_DENY",detail)

    def test_structured_reread_requires_an_intervening_edit_attempt(self):
        structured=".opencode-v2/IMPLEMENTATION_PLAN.structured.json"
        read={
            "tool":"read","input":{"filePath":structured},
            "status":"completed","error":"",
        }
        edit={
            "tool":"edit","input":{"filePath":structured},
            "status":"error","error":"oldString mismatch",
        }
        with mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=[read]
        ):
            self.assertFalse(
                supervisor.planner_repair_read_refresh_allowed(
                    self.sid,structured,structured
                )
            )
        with mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=[read,edit]
        ):
            self.assertTrue(
                supervisor.planner_repair_read_refresh_allowed(
                    self.sid,structured,structured
                )
            )
        with mock.patch.object(
            supervisor,"session_completed_tool_records",
            return_value=[read,edit,read],
        ):
            self.assertFalse(
                supervisor.planner_repair_read_refresh_allowed(
                    self.sid,structured,structured
                )
            )

    def test_targeted_repair_may_read_existing_affected_owned_file_once(self):
        ctrl=self.project/".opencode-v2"
        (self.project/"src").mkdir()
        (self.project/"src/app.js").write_text("ok\n")
        (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text(json.dumps({
            "leaves":[{
                "key":"app","owned_artifacts":["src/app.js"],
            }]
        }))
        state,detail=self.boundary("read",{"filePath":"src/app.js"})
        self.assertEqual(state,"allow")
        self.assertIn("owned-artifact-read-once",detail)
        state,detail=self.boundary("read",{"filePath":"package.json"})
        self.assertEqual(state,"deny")
        self.assertIn("PLANNER_REPAIR_READ_DENY",detail)


class PlannerInfrastructureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        (self.project/".opencode-v2/work").mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.sid="planner-stalled"
        supervisor.planner_restart_path().write_text(json.dumps({
            "owner":"supervisor",
            "count":3,
            "retired_session":self.sid,
            "reason":"planner_plan_progress_stalled elapsed=120s limit=120s",
            "counted_sessions":["planner-old-1","planner-old-2",self.sid],
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def recover(self,tools=(),records=None):
        if records is None:
            records=[
                {"tool":tool,"input":args,"status":"completed","error":""}
                for tool,args in tools
            ]
        with mock.patch.object(supervisor,"_session_agent_db",return_value="implementation-planner"), \
             mock.patch.object(supervisor,"_v1_active_session_ids",return_value=set()), \
             mock.patch.object(supervisor,"session_completed_tool_records",return_value=list(records)):
            return supervisor.recover_planner_infrastructure_failure(
                self.sid,"targeted repair read-boundary harness defect"
            )

    def write_runtime_repair(self, *, baseline=None, source="runtime-leaf-contract-challenge"):
        payload={
            "protocol":"v2-structured-plan-repair-v1",
            "source":source,
            "whole_plan":False,
            "affected_keys":["text_module"],
            "errors":[],
            "baseline":{"affected_leaf_sha256":{"text_module":"a"*64}},
        }
        if baseline is not None:
            payload["planner_restart_baseline"]=baseline
        (
            self.project/".opencode-v2/IMPLEMENTATION_PLAN.repair.json"
        ).write_text(json.dumps(payload))

    def test_runtime_repair_gets_one_bounded_planner_failure_beyond_exhausted_base(self):
        self.write_runtime_repair(baseline=3)
        self.assertEqual(control_state.planner_restart_limit(self.project),4)
        self.assertEqual(supervisor.planner_restart_limit(),4)
        self.assertTrue(
            supervisor.record_planner_restart(
                "planner-runtime-repair","runtime repair failed"
            )
        )
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(data["count"],4)
        self.assertEqual(control_state.planner_restart_limit(self.project),4)
        self.assertFalse(
            supervisor.record_planner_restart(
                "planner-runtime-repair-2","must remain bounded"
            )
        )
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(data["count"],4)

    def test_control_policy_revalidation_gets_one_bounded_planner_slot(self):
        self.write_runtime_repair(
            baseline=3,source="control-policy-revalidation"
        )
        self.assertEqual(control_state.planner_restart_limit(self.project),4)
        self.assertEqual(supervisor.planner_restart_limit(),4)

    def test_legacy_runtime_repair_without_baseline_cannot_extend_recursively(self):
        self.write_runtime_repair()
        self.assertEqual(control_state.planner_restart_limit(self.project),4)
        data=json.loads(supervisor.planner_restart_path().read_text())
        data["count"]=4
        data["counted_sessions"].append("planner-runtime-repair")
        supervisor.planner_restart_path().write_text(json.dumps(data))
        self.assertEqual(control_state.planner_restart_limit(self.project),4)
        self.assertEqual(supervisor.planner_restart_limit(),4)

    def test_nonruntime_repair_does_not_extend_planner_limit(self):
        self.write_runtime_repair(source="structured-compiler")
        self.assertEqual(control_state.planner_restart_limit(self.project),3)
        self.assertEqual(supervisor.planner_restart_limit(),3)

    def test_recovery_refunds_exact_counted_session_once_and_prevents_recharge(self):
        self.assertEqual(self.recover(),(True,"recovered"))
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(data["count"],2)
        self.assertNotIn(self.sid,data["counted_sessions"])
        self.assertEqual(data["infrastructure_recoveries"][0]["session"],self.sid)
        self.assertEqual(data["infrastructure_recoveries"][0]["source"],"operator-controller")
        self.assertEqual(self.recover(),(True,"already-recovered"))
        self.assertFalse(supervisor.record_planner_restart(self.sid,"another failure"))
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(data["count"],2)

    def test_recovery_refuses_session_that_mutated_structured_plan(self):
        ok,detail=self.recover(tools=[(
            "edit",{"filePath":".opencode-v2/IMPLEMENTATION_PLAN.structured.json"}
        )])
        self.assertFalse(ok)
        self.assertEqual(detail,"planner-infrastructure-recovery-structured-plan-was-mutated")
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(data["count"],3)

    def test_recovery_refunds_repair_packet_lifecycle_deadlock(self):
        structured=".opencode-v2/IMPLEMENTATION_PLAN.structured.json"
        records=[
            {"tool":"read","input":{"filePath":structured},
             "status":"completed","error":""},
            {"tool":"edit","input":{"filePath":structured},
             "status":"completed","error":""},
            {"tool":"edit","input":{"filePath":structured},
             "status":"error",
             "error":"PLANNER_REPAIR_PACKET_INVALID StateCorruptionError"},
            {"tool":"read","input":{"filePath":structured},
             "status":"error",
             "error":"PLANNER_REPAIR_PACKET_INVALID StateCorruptionError"},
        ]
        with mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=records
        ):
            self.assertTrue(
                supervisor.planner_repair_packet_lifecycle_deadlock_evidence(
                    self.sid,structured
                )
            )
        self.assertEqual(self.recover(records=records),(True,"recovered"))
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(
            data["infrastructure_recoveries"][0]["recovery_kind"],
            "repair-packet-lifecycle-deadlock",
        )

    def test_recovery_refunds_partial_repair_blocked_by_old_reread_guard(self):
        structured=".opencode-v2/IMPLEMENTATION_PLAN.structured.json"
        records=[
            {"tool":"read","input":{"filePath":structured},
             "status":"completed","error":""},
            {"tool":"edit","input":{"filePath":structured},
             "status":"completed","error":""},
            {"tool":"read","input":{"filePath":structured},
             "status":"error","error":"PLANNER_REPAIR_READ_ALREADY_COMPLETE"},
        ]
        with mock.patch.object(
            supervisor,"session_completed_tool_records",return_value=records
        ):
            self.assertTrue(
                supervisor.planner_structured_reread_deadlock_evidence(
                    self.sid,structured
                )
            )
        self.assertEqual(self.recover(records=records),(True,"recovered"))
        data=json.loads(supervisor.planner_restart_path().read_text())
        self.assertEqual(data["count"],2)
        self.assertNotIn(self.sid,data["counted_sessions"])


class ReferenceFoundationToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        ctrl=self.project/".opencode-v2"
        (ctrl/"acceptance").mkdir(parents=True)
        (ctrl/"ACCEPTANCE.md").write_text("acceptance\n")
        (ctrl/"REFERENCE_FOUNDATION.md").write_text("foundation\n")
        (ctrl/"acceptance/reference-evidence.json").write_text("{}\n")
        self.work=ctrl/"acceptance/reference-work.json"
        self.work.write_text("{}\n")
        self.sid="ses-reference-foundation-boundary"

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def state(self,tool,args,seen=(),records=()):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="reference-researcher"
        ), mock.patch.object(
            supervisor,"reference_session_mode",return_value="foundation"
        ), mock.patch.object(
            supervisor,"persisted_exact_project_read_seen",
            side_effect=lambda _sid,target: target in set(seen)
        ), mock.patch.object(
            supervisor,"session_completed_tool_records",
            return_value=list(records)
        ):
            return supervisor.reference_validation_tool_state(
                self.sid,tool,args
            )

    def test_foundation_local_reads_are_control_files_only(self):
        allowed=[
            ".opencode-v2/ACCEPTANCE.md",
            ".opencode-v2/acceptance/reference-evidence.json",
            ".opencode-v2/acceptance/reference-work.json",
            ".opencode-v2/REFERENCE_FOUNDATION.md",
        ]
        for rel in allowed:
            state,detail=self.state(
                "read",{"filePath":str(self.project/rel)}
            )
            self.assertEqual(state,"allow",rel)
            self.assertIn("reference-foundation-control-read",detail)
        state,detail=self.state(
            "read",{"filePath":str(self.project/"src/app.py")}
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_LOCAL_READ_DENY",detail)
        state,detail=self.state("glob",{"pattern":"**/*"})
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_TOOL_DENY",detail)

    def test_foundation_repeated_control_read_is_denied(self):
        rel=".opencode-v2/ACCEPTANCE.md"
        state,detail=self.state(
            "read",{"filePath":str(self.project/rel)},seen=[rel]
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_REPEAT_READ_DENY",detail)

    def test_foundation_writes_are_limited_to_declared_control_outputs(self):
        for rel in (
            ".opencode-v2/acceptance/reference-work.json",
            ".opencode-v2/acceptance/reference-evidence.json",
            ".opencode-v2/REFERENCE_FOUNDATION.md",
        ):
            state,detail=self.state(
                "write",{"filePath":str(self.project/rel),"content":"{}"}
            )
            self.assertEqual(state,"allow",rel)
        state,detail=self.state(
            "write",{"filePath":str(self.project/"src/app.py"),"content":"x"}
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_WRITE_DENY",detail)

    def test_foundation_external_calls_require_checkpoint_and_are_bounded(self):
        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/source"}
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_CHECKPOINT_REQUIRED",detail)

        self.work.write_text(json.dumps({
            "mode":"foundation","status":"in_progress",
            "current_item":"verify-docs",
        }))
        calls=[
            {
                "tool":"webfetch","status":"completed",
                "input":{"url":f"https://example.invalid/{i}"},
            }
            for i in range(supervisor.MAX_REFERENCE_FOUNDATION_WEB_CALLS)
        ]
        progress={
            "tool":"write","status":"completed",
            "input":{
                "filePath":str(
                    self.project/".opencode-v2/acceptance/reference-evidence.json"
                ),
                "content":"{}",
            },
        }

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/third"},
            records=calls[:2],
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_PROGRESS_CHECKPOINT_REQUIRED",detail)
        state,detail=self.state(
            "write",progress["input"],records=calls[:2],
        )
        self.assertEqual(state,"allow")

        five_calls=[*calls[:2],progress,*calls[2:4],progress,calls[4]]
        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/sixth"},
            records=five_calls,
        )
        self.assertEqual(state,"allow")
        self.assertIn(
            f"web_calls={supervisor.MAX_REFERENCE_FOUNDATION_WEB_CALLS-1}/"
            f"{supervisor.MAX_REFERENCE_FOUNDATION_WEB_CALLS}",
            detail,
        )

        six_calls=[*five_calls,calls[5]]
        state,detail=self.state(
            "websearch",{"query":"official docs"},
            records=six_calls,
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_FOUNDATION_WEB_BUDGET_EXHAUSTED",detail)

        denied={
            "tool":"webfetch","status":"error",
            "input":{"url":"https://example.invalid/denied"},
            "error":"REFERENCE_VALIDATION_TOOL_DENY "
                    "REFERENCE_FOUNDATION_WEB_BUDGET_EXHAUSTED",
        }
        with mock.patch.object(
            supervisor,"session_completed_tool_records",
            return_value=[*six_calls,denied],
        ):
            self.assertEqual(
                supervisor.reference_foundation_web_call_count(self.sid),
                supervisor.MAX_REFERENCE_FOUNDATION_WEB_CALLS,
            )


class ReferenceValidationCheckpointToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        ctrl=self.project/".opencode-v2"
        items=ctrl/"acceptance/reference-items"
        items.mkdir(parents=True)
        (ctrl/"ACCEPTANCE.md").write_text("acceptance\n")
        (ctrl/"REFERENCE_FOUNDATION.md").write_text("foundation\n")
        (items/"V1-done.json").write_text("{}\n")
        (ctrl/"acceptance/reference-evidence.json").write_text(json.dumps({
            "result":"PARTIAL",
            "resolved_items":{"V1-done":{"status":"complete"}},
            "missing":["V2-next: fetch one bounded item","V3-later: later"],
        }))
        self.work=ctrl/"acceptance/reference-work.json"
        self.work.write_text(json.dumps({
            "mode":"validation","status":"complete",
            "last_completed_item":"V1-done",
            "next_item":"V2-next",
        }))
        self.sid="ses-reference-boundary"

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def state(self,tool,args,seen=(),records=()):
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="reference-researcher"
        ), mock.patch.object(
            supervisor,"reference_session_mode",return_value="validation"
        ), mock.patch.object(
            supervisor,"persisted_exact_project_read_seen",
            side_effect=lambda _sid,target: target in set(seen)
        ), mock.patch.object(
            supervisor,"session_completed_tool_records",
            return_value=list(records)
        ):
            return supervisor.reference_validation_tool_state(
                self.sid,tool,args
            )

    def test_precheckpoint_allows_only_bounded_control_reads_and_work_write(self):
        allowed=[
            ".opencode-v2/ACCEPTANCE.md",
            ".opencode-v2/REFERENCE_FOUNDATION.md",
            ".opencode-v2/acceptance/reference-evidence.json",
            ".opencode-v2/acceptance/reference-work.json",
            ".opencode-v2/acceptance/reference-items/V1-done.json",
        ]
        for rel in allowed:
            state,detail=self.state(
                "read",{"filePath":str(self.project/rel)}
            )
            self.assertEqual(state,"allow",rel)
        state,detail=self.state(
            "read",{"filePath":str(self.project)}
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_VALIDATION_CHECKPOINT_REQUIRED",detail)
        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/"}
        )
        self.assertEqual(state,"deny")
        self.assertIn("next_tool=write",detail)
        state,detail=self.state(
            "write",{
                "filePath":str(self.work),
                "content":"{}",
            }
        )
        self.assertEqual(state,"allow")
        self.assertIn("required_item=V2-next",detail)

    def test_precheckpoint_repeated_control_read_is_denied(self):
        rel=".opencode-v2/ACCEPTANCE.md"
        state,detail=self.state(
            "read",{"filePath":str(self.project/rel)},seen=[rel]
        )
        self.assertEqual(state,"deny")
        self.assertIn("repeated_read",detail)

    def test_only_exact_first_missing_item_unlocks_normal_research(self):
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next",
            "next_item":"V2-next",
        }))
        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/"}
        )
        self.assertEqual(state,"allow")
        self.assertIn("external-action",detail)

        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V3-later",
            "next_item":"V3-later",
        }))
        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/"}
        )
        self.assertEqual(state,"deny")
        self.assertIn("item=V2-next",detail)

    def test_resumed_checkpoint_allows_only_control_reads_before_external_action(self):
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
            "authoritative_source":"https://example.invalid/source",
            "next_action":"fetch authoritative source",
            "last_completed_item":"V1-done",
        }))
        state,detail=self.state(
            "read",{"filePath":str(self.project/".opencode-v2/ACCEPTANCE.md")},
            records=[],
        )
        self.assertEqual(state,"allow")
        self.assertIn("resume-control-read",detail)

        state,detail=self.state(
            "read",{"filePath":str(self.project/"reference/reference.py")},
            records=[],
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_VALIDATION_RESUME_EXTERNAL_ACTION_REQUIRED",detail)

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/source"},
            records=[],
        )
        self.assertEqual(state,"allow")
        self.assertIn("external-action",detail)

    def test_resumed_checkpoint_requires_existing_item_read_before_external_action(self):
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
        }))
        item=self.project/".opencode-v2/acceptance/reference-items/V2-next.json"
        item.write_text(json.dumps({"item":"V2-next","status":"partial"}))

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/source"},
            records=[],
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_VALIDATION_CURRENT_ITEM_READ_REQUIRED",detail)

        rel=".opencode-v2/acceptance/reference-items/V2-next.json"
        state,detail=self.state(
            "read",{"filePath":str(item)},
            records=[],
        )
        self.assertEqual(state,"allow")
        self.assertIn("resume-control-read",detail)

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/source"},
            seen=[rel],records=[],
        )
        self.assertEqual(state,"allow")
        self.assertIn("external-action",detail)

    def test_resume_semantic_prompt_uses_sanitized_durable_request_hints(self):
        last_url="https://api.example.invalid/v1/items?id=alpha"
        next_url="https://api.example.invalid/v1/items?id=beta"
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
            "authoritative_source":"https://example.invalid/source",
            "next_action":"fetch authoritative source",
            "last_successful_external_request_url":last_url,
            "next_external_request_url":next_url,
        }))
        item=self.project/".opencode-v2/acceptance/reference-items/V2-next.json"
        item.write_text(json.dumps({"item":"V2-next","status":"partial"}))

        part=stage_a_controller.build_semantic_subtask({
            "kind":"launch","agent":"reference-researcher","mode":"validation",
        },self.project)
        prompt=part["prompt"]
        self.assertIn("RESUME CHECKPOINT ALREADY EXISTS",prompt)
        self.assertIn("Do NOT rewrite the initial checkpoint",prompt)
        self.assertIn(
            "first non-control action MUST be an authoritative web call",prompt
        )
        self.assertIn(
            "recorded_next_action=fetch authoritative source",prompt
        )
        self.assertIn("Existing current-item evidence exists",prompt)
        self.assertIn("Durable external-request resume hints",prompt)
        self.assertIn(f"resume_request_1={last_url}",prompt)
        self.assertIn(f"resume_request_2={next_url}",prompt)

    def test_resume_request_urls_are_explicit_sanitized_durable_state_only(self):
        safe="https://api.example.invalid/v1/items?id=alpha"
        secret="https://api.example.invalid/v1/items?api_key=secret"
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
            "last_successful_external_request_url":safe,
            "next_external_request_url":secret,
        }))
        self.assertEqual(
            stage_a_controller.reference_resume_external_request_urls(
                self.project
            ),
            [safe],
        )

        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
            "last_successful_external_request_url":"file:///tmp/data",
            "next_external_request_url":"https://user:pass@example.invalid/x",
        }))
        self.assertEqual(
            stage_a_controller.reference_resume_external_request_urls(
                self.project
            ),
            [],
        )

    def test_postcheckpoint_local_inspection_is_bounded_until_external_progress(self):
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
        }))
        checkpoint={
            "tool":"write","status":"completed",
            "input":{"filePath":str(self.work),"content":"{}"},
        }
        local=[
            {
                "tool":"read","status":"completed",
                "input":{"filePath":str(self.project/f"local-{i}.txt")},
            }
            for i in range(supervisor.MAX_REFERENCE_POSTCHECKPOINT_LOCAL_TOOLS)
        ]
        state,detail=self.state(
            "read",{"filePath":str(self.project/"another.txt")},
            records=[checkpoint,*local],
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_VALIDATION_EXTERNAL_ACTION_REQUIRED",detail)
        self.assertIn("next_tool=webfetch-or-write-evidence",detail)

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/"},
            records=[checkpoint,*local],
        )
        self.assertEqual(state,"allow")
        self.assertIn("external-action",detail)

        item=self.project/".opencode-v2/acceptance/reference-items/V2-next.json"
        state,detail=self.state(
            "write",{"filePath":str(item),"content":"{}"},
            records=[checkpoint,*local],
        )
        self.assertEqual(state,"allow")
        self.assertIn("durable-progress-write",detail)

        external={
            "tool":"webfetch","status":"completed",
            "input":{"url":"https://example.invalid/"},
        }
        state,detail=self.state(
            "read",{"filePath":str(self.project/"after-web.txt")},
            records=[checkpoint,*local,external],
        )
        self.assertEqual(state,"allow")
        self.assertIn("postcheckpoint-progress",detail)

    def test_postcheckpoint_web_budget_is_fail_closed_and_requires_durable_write(self):
        self.work.write_text(json.dumps({
            "mode":"validation","status":"in_progress",
            "current_item":"V2-next","next_item":"V2-next",
            "budget":{"web_calls_allowed":4,"web_calls_used":0},
        }))
        checkpoint={
            "tool":"write","status":"completed",
            "input":{"filePath":str(self.work),"content":"{}"},
        }
        calls=[
            {
                "tool":"webfetch",
                "status":"error" if i==0 else "completed",
                "input":{"url":f"https://example.invalid/{i}"},
            }
            for i in range(4)
        ]

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/next"},
            records=[checkpoint,*calls[:3]],
        )
        self.assertEqual(state,"allow")
        self.assertIn("web_calls=3/4",detail)

        state,detail=self.state(
            "webfetch",{"url":"https://example.invalid/fifth"},
            records=[checkpoint,*calls],
        )
        self.assertEqual(state,"deny")
        self.assertIn("REFERENCE_VALIDATION_WEB_BUDGET_EXHAUSTED",detail)
        self.assertIn("web_calls=4/4",detail)

        state,detail=self.state(
            "read",{"filePath":str(self.project/"anything.txt")},
            records=[checkpoint,*calls],
        )
        self.assertEqual(state,"deny")
        self.assertIn("write-evidence-or-work-result",detail)

        item=self.project/".opencode-v2/acceptance/reference-items/V2-next.json"
        state,detail=self.state(
            "write",{"filePath":str(item),"content":"{}"},
            records=[checkpoint,*calls],
        )
        self.assertEqual(state,"allow")
        self.assertIn("web-budget-durable-write",detail)

        denied={
            "tool":"webfetch","status":"error",
            "input":{"url":"https://example.invalid/denied"},
            "error":"REFERENCE_VALIDATION_TOOL_DENY "
                    "REFERENCE_VALIDATION_WEB_BUDGET_EXHAUSTED",
        }
        with mock.patch.object(
            supervisor,"session_completed_tool_records",
            return_value=[checkpoint,*calls,denied],
        ):
            self.assertEqual(
                supervisor.reference_validation_web_call_count(self.sid),4
            )

    def test_plugin_invokes_reference_checkpoint_guard_before_generic_worker_gate(self):
        root=Path(__file__).resolve().parent.parent
        text=(root/"xdg/config/opencode/plugins/v2-bounded-subagent.js").read_text()
        self.assertIn("--reference-validation-tool-check",text)
        before=text.index("guardReferenceValidationCheckpoint(directory, event, output);")
        generic=text.index("await guardEarlyWrite(directory, event, output, compatApi);")
        self.assertLess(before,generic)


class ReadyTrustBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.project=Path(self.tmp.name); self.work=self.project/".opencode-v2/work"; self.work.mkdir(parents=True)
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves":{"D001":{"launch_deps":[],"verify_command":"test -f a.txt"}}}))
        (self.work/"attempts.json").write_text(json.dumps({"owner":"supervisor","deliverables":{"D001":{"count":1,"sessions":["s1"],"automatic_limit":3}}}))
    def tearDown(self): self.tmp.cleanup()
    def test_ready_requires_provenance_and_current_attempt(self):
        p=self.work/"D001.ready"; p.write_text("status=complete\ndeliverable=D001\nattempt=1\nverified=true\n"); self.assertFalse(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",verify_command="test -f a.txt")); self.assertTrue(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",2,verify_command="test -f a.txt")); self.assertFalse(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",protocol="wrong",verify_command="test -f a.txt")); self.assertFalse(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",owner="model",verify_command="test -f a.txt")); self.assertFalse(control_state.ready_info(self.project,"D001"))
    def test_non_supervisor_ledger_owner_rejected(self):
        (self.work/"D001.ready").write_text(ready_text("D001",verify_command="test -f a.txt")); data=json.loads((self.work/"attempts.json").read_text()); data["owner"]="model"; (self.work/"attempts.json").write_text(json.dumps(data)); self.assertFalse(control_state.ready_info(self.project,"D001"))

    def test_provenance_bound_ready_is_revoked_when_owned_artifact_changes(self):
        command="test -s a.txt"
        leaf={
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "owned_artifact_paths":["a.txt"],
            "verify_command":command,
            "done_when":"a.txt exists and is non-empty.",
        }
        (self.project/"a.txt").write_text("verified\n")
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(
            json.dumps({"leaves":{"D001":leaf}})
        )
        provenance=control_state.ready_provenance(self.project,"D001",leaf)
        body=ready_text("D001",verify_command=command)
        body=body.replace(
            f"protocol={control_state.LEAF_READY_PROTOCOL}\n",
            "".join(f"{k}={v}\n" for k,v in provenance.items())
            + f"protocol={control_state.LEAF_READY_PROTOCOL}\n",
        )
        (self.work/"D001.ready").write_text(body)
        self.assertTrue(control_state.ready_info(self.project,"D001"))
        (self.project/"a.txt").write_text("changed after verify\n")
        self.assertFalse(control_state.ready_info(self.project,"D001"))

    def test_ready_is_revoked_when_current_completion_contract_has_weak_verify(self):
        manifest={
            "leaves":{
                "D001":{
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":"node --check tests/harness.js && test -s tests/harness.js",
                    "done_when":"tests/harness.js drives the app and asserts acceptance properties.",
                }
            }
        }
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        command=manifest["leaves"]["D001"]["verify_command"]
        (self.work/"D001.ready").write_text(ready_text("D001",verify_command=command))
        self.assertFalse(control_state.ready_info(self.project,"D001"))

    def test_upstream_ready_revocation_cascades_to_downstream_ready(self):
        manifest={
            "leaves":{
                "D001":{
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "verify_command":"test -s a.txt","done_when":"a.txt exists and is non-empty.",
                },
                "D002":{
                    "launch_deps":["D001"],"contract_deps":[],"verify_deps":[],
                    "verify_command":"test -s b.txt","done_when":"b.txt exists and is non-empty.",
                },
            }
        }
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        attempts=json.loads((self.work/"attempts.json").read_text())
        attempts["deliverables"]["D002"]={"count":1,"sessions":["s2"],"automatic_limit":3}
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        (self.work/"D001.ready").write_text(ready_text("D001",verify_command="test -s a.txt"))
        (self.work/"D002.ready").write_text(ready_text("D002",verify_command="test -s b.txt"))
        self.assertTrue(control_state.ready_info(self.project,"D002"))

        manifest["leaves"]["D001"]["verify_command"]="node --check tests/harness.js && test -s tests/harness.js"
        manifest["leaves"]["D001"]["done_when"]="tests/harness.js drives the app and asserts acceptance properties."
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        (self.work/"D001.ready").write_text(
            ready_text("D001",verify_command=manifest["leaves"]["D001"]["verify_command"])
        )
        self.assertFalse(control_state.ready_info(self.project,"D001"))
        self.assertFalse(control_state.ready_info(self.project,"D002"))

    def test_ready_projection_memoizes_dependency_provenance(self):
        manifest={
            "leaves":{
                "D001":{
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "owned_artifact_paths":["a.txt"],
                    "verify_command":"test -s a.txt",
                    "done_when":"a.txt exists and is non-empty.",
                },
                "D002":{
                    "launch_deps":["D001"],"contract_deps":[],"verify_deps":[],
                    "owned_artifact_paths":["b.txt"],
                    "verify_command":"test -s b.txt",
                    "done_when":"b.txt exists and is non-empty.",
                },
            }
        }
        (self.project/"a.txt").write_text("a\n")
        (self.project/"b.txt").write_text("b\n")
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(
            json.dumps(manifest)
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        attempts["deliverables"]["D002"]={
            "count":1,"sessions":["s2"],"automatic_limit":3
        }
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        for did,leaf in manifest["leaves"].items():
            provenance=control_state.ready_provenance(self.project,did,leaf)
            body=ready_text(did,verify_command=leaf["verify_command"])
            body=body.replace(
                f"protocol={control_state.LEAF_READY_PROTOCOL}\n",
                "".join(f"{k}={v}\n" for k,v in provenance.items())
                + f"protocol={control_state.LEAF_READY_PROTOCOL}\n",
            )
            (self.work/f"{did}.ready").write_text(body)

        ledger=control_state.load_attempts(self.project)
        memo={}
        provenance_cache={}
        real=control_state.ready_provenance
        with mock.patch.object(
            control_state,"ready_provenance",wraps=real
        ) as hashed:
            self.assertTrue(control_state._ready_info_cached(
                self.project,"D002",manifest,ledger,memo,set(),provenance_cache
            ))
            self.assertTrue(control_state._ready_info_cached(
                self.project,"D001",manifest,ledger,memo,set(),provenance_cache
            ))
            self.assertTrue(control_state._ready_info_cached(
                self.project,"D002",manifest,ledger,memo,set(),provenance_cache
            ))
        self.assertEqual(hashed.call_count,2)

class SplitValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=supervisor.PROJECT; supervisor.PROJECT=self.tmp.name; ctrl=Path(self.tmp.name)/".opencode-v2"; (ctrl/"work").mkdir(parents=True)
        parent={"id":"D001","name":"parent","owned_artifacts":"`a.txt`, `b.txt`","owned_artifact_paths":["a.txt","b.txt"],"launch_deps":[],"contract_deps":[],"verify_deps":[],"verify_command":"test -f a.txt -a -f b.txt","role":"implementer","done_when":"done","acceptance_ids":["A001"],"parallel":"none","split_children":[]}
        (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,"leaves":{"D001":parent}}))
    def tearDown(self): supervisor.PROJECT=self.old; self.tmp.cleanup()
    def proposals(self):
        return [
            {"scope":"write all","owned_artifacts":"`a.txt`, `b.txt`","verify_command":"test -f a.txt","role":"implementer","depends_on_sibling":"","done_when":"files exist","reads_existing":[],"creates_or_updates":["a.txt","b.txt"]},
            {"scope":"verify","owned_artifacts":"none","verify_command":"test -f b.txt","role":"tester","depends_on_sibling":"first","done_when":"verified","reads_existing":[],"creates_or_updates":[]},
        ]
    def request(self):
        return {
            "failed_attempts":[{"classification":"genuine","reason":"verify-failed-1"}],
            "parent_contract":{"verify_command":"test -f a.txt -a -f b.txt"},
        }
    def test_shared_rules_apply_to_split(self):
        supervisor.validate_split_proposal("D001",self.proposals(),request=self.request())
        bad=self.proposals(); bad[0]["verify_command"]="true"
        with self.assertRaisesRegex(ValueError,"non-verifying"): supervisor.validate_split_proposal("D001",bad,request=self.request())
        bad=self.proposals(); bad[0]["role"]="potato"
        with self.assertRaisesRegex(ValueError,"unknown implementation Role"): supervisor.validate_split_proposal("D001",bad,request=self.request())

class LegacyCompletionTests(unittest.TestCase):
    def test_leaf_complete_refuses(self):
        r=subprocess.run([str(HERE/"leaf-complete.sh"),"D001"],cwd=tempfile.gettempdir(),text=True,capture_output=True)
        self.assertNotEqual(r.returncode,0); self.assertIn("supervisor-owned",r.stderr)

class BootstrapTrustTests(unittest.TestCase):
    def test_bootstrap_exposes_no_leaf_complete_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            result=subprocess.run([sys.executable,str(HERE/"run-checks.py"),"--project",td,"--bootstrap-control-contract"],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((project/".opencode-v2/bin/run-checks").exists())
            self.assertTrue((project/".opencode-v2/bin/control-status").exists())
            self.assertFalse((project/".opencode-v2/bin/leaf-complete").exists())
            contract=(project/".opencode-v2/CONTROL_CONTRACT.md").read_text()
            normalized=" ".join(contract.split())
            self.assertIn("supervisor re-runs the exact leaf Verify command",normalized)
            self.assertNotIn("Complete a verified leaf with",contract)

    def test_state_writer_cannot_write_arbitrary_control_state(self):
        text=(HERE.parent/"xdg/config/opencode/agents/state-writer.md").read_text()
        self.assertIn('".opencode-v2/STATE.md": allow',text)
        self.assertNotIn('".opencode-v2/**": allow',text)
        self.assertNotIn("write `.opencode-v2/work/<Dxxx>.ready`",text)


class ReferenceGateInfrastructureAccountingTests(unittest.TestCase):
    def test_audited_semantic_infrastructure_retry_does_not_consume_stagnation(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            (work/"stage-a-controller-executions.json").write_text(json.dumps({
                "executions":{
                    "e1":{
                        "action":{
                            "agent":"reference-researcher",
                            "kind":"launch",
                            "mode":"validation",
                        },
                        "semantic_infrastructure_retry":{
                            "protocol":supervisor.REFERENCE_SEMANTIC_RETRY_PROTOCOL,
                            "source":"operator-controller",
                            "child_sessions":["s-infra"],
                            "next_generation":1,
                        },
                    }
                }
            }))
            old=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                with mock.patch.object(
                    supervisor,"_reference_sessions",
                    return_value=[("s-infra",1),("s-genuine",1)]
                ), mock.patch.object(
                    supervisor,"reference_session_made_progress",
                    return_value=False
                ):
                    completed,active,productive,stagnant,infrastructure=(
                        supervisor._reference_progress_stats("validation")
                    )
            finally:
                supervisor.PROJECT=old
            self.assertEqual(completed,["s-genuine"])
            self.assertEqual(active,[])
            self.assertEqual(productive,0)
            self.assertEqual(stagnant,1)
            self.assertEqual(infrastructure,["s-infra"])

    def test_unaudited_retry_metadata_does_not_get_reference_gate_exemption(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            (work/"stage-a-controller-executions.json").write_text(json.dumps({
                "executions":{
                    "e1":{
                        "action":{
                            "agent":"reference-researcher",
                            "kind":"launch",
                            "mode":"validation",
                        },
                        "semantic_infrastructure_retry":{
                            "protocol":supervisor.REFERENCE_SEMANTIC_RETRY_PROTOCOL,
                            "source":"not-controller",
                            "child_sessions":["s1"],
                        },
                    }
                }
            }))
            old=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                with mock.patch.object(
                    supervisor,"_reference_sessions",return_value=[("s1",1)]
                ), mock.patch.object(
                    supervisor,"reference_session_made_progress",return_value=False
                ):
                    completed,_,_,stagnant,infrastructure=(
                        supervisor._reference_progress_stats("validation")
                    )
            finally:
                supervisor.PROJECT=old
            self.assertEqual(completed,["s1"])
            self.assertEqual(stagnant,1)
            self.assertEqual(infrastructure,[])


class CrashConsistencyTests(unittest.TestCase):
    def test_canonical_json_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td); work=project/".opencode-v2/work"; work.mkdir(parents=True)
            (project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text("{broken")
            with self.assertRaises(state_io.StateCorruptionError):
                control_state.load_manifest(project)
            (project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text("{}")
            (work/"attempts.json").write_text("{broken")
            with self.assertRaises(state_io.StateCorruptionError):
                control_state.load_attempts(project)

    def test_stale_attempt_lock_file_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as td:
            old=supervisor.PROJECT; supervisor.PROJECT=td
            try:
                work=Path(td)/".opencode-v2/work"; work.mkdir(parents=True)
                (work/"attempts.json.lock").write_text("pid=dead\n")
                with supervisor.attempt_lock():
                    self.assertTrue((work/"attempts.json.lock").exists())
            finally:
                supervisor.PROJECT=old

    def test_control_status_error_overwrites_stale_status(self):
        with tempfile.TemporaryDirectory() as td:
            old_project=supervisor.PROJECT
            old_snapshot=supervisor.normalized_state_snapshot
            supervisor.PROJECT=td
            ctrl=Path(td)/".opencode-v2"; ctrl.mkdir()
            status=ctrl/"control-status.json"
            status.write_text('{"resume_phase":"execution","stale":true}\n')
            def boom(_):
                raise state_io.StateCorruptionError("broken ledger")
            supervisor.normalized_state_snapshot=boom
            try:
                payload=supervisor.sync_control_status_snapshot()
                saved=json.loads(status.read_text())
                self.assertTrue(payload["state_error"])
                self.assertTrue(saved["state_error"])
                self.assertEqual(saved["resume_phase"],"execution-blocked")
                self.assertEqual(saved["execution_blockers"][0]["reason"],"control_state_error")
            finally:
                supervisor.normalized_state_snapshot=old_snapshot
                supervisor.PROJECT=old_project

    def test_pre_restart_current_unclassified_session_is_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            old=supervisor.PROJECT; supervisor.PROJECT=td
            try:
                work=Path(td)/".opencode-v2/work"; work.mkdir(parents=True)
                (work/"attempts.json").write_text(json.dumps({
                    "owner":"supervisor","deliverables":{
                        "D001":{"count":1,"sessions":["old-session"],"automatic_limit":3}
                    }
                }))
                self.assertIn("old-session",supervisor.pending_ledger_session_ids())
                data=json.loads((work/"attempts.json").read_text())
                data["deliverables"]["D001"]["failure_history"]=[
                    {"attempt":1,"classification":"genuine","reason":"x"}
                ]
                (work/"attempts.json").write_text(json.dumps(data))
                self.assertNotIn("old-session",supervisor.pending_ledger_session_ids())
            finally:
                supervisor.PROJECT=old

    def test_dispatch_seen_only_after_claim_transition(self):
        sid="fault-dispatch"
        supervisor.dispatch_seen.discard(sid)
        old_validate=supervisor.validate_dispatch
        old_claim=supervisor.claim_attempt
        supervisor.validate_dispatch=lambda agent,text,runtime=False: ("D001","")
        def fail_claim(sid,did):
            raise RuntimeError("fault after validation before claim")
        supervisor.claim_attempt=fail_claim
        try:
            with self.assertRaises(RuntimeError):
                supervisor.enforce_assignment(sid,"implementer","DELIVERABLE: D001")
            self.assertNotIn(sid,supervisor.dispatch_seen)
        finally:
            supervisor.validate_dispatch=old_validate
            supervisor.claim_attempt=old_claim
            supervisor.dispatch_seen.discard(sid)

    def test_idle_seen_only_after_reconciliation_succeeds(self):
        sid="fault-idle"
        supervisor.post_finalize_seen.discard(sid)
        supervisor.session_task[sid]=("D001",1)
        names={
            "plan_ready":supervisor.plan_ready,
            "immediate_runtime_abort":supervisor.immediate_runtime_abort,
            "persisted_abort_reason":supervisor.persisted_abort_reason,
            "ready_info":supervisor.ready_info,
            "meaningful_worker_execution":supervisor.meaningful_worker_execution,
        }
        supervisor.plan_ready=lambda:True
        supervisor.immediate_runtime_abort=lambda _sid:""
        supervisor.persisted_abort_reason=lambda _sid:""
        supervisor.ready_info=lambda _did:{}
        def boom(_sid,_did):
            raise RuntimeError("fault before durable classification")
        supervisor.meaningful_worker_execution=boom
        try:
            with self.assertRaises(RuntimeError):
                supervisor.reconcile_idle_implementation_session(sid,"implementer")
            self.assertNotIn(sid,supervisor.post_finalize_seen)
        finally:
            for name,value in names.items(): setattr(supervisor,name,value)
            supervisor.session_task.pop(sid,None)
            supervisor.post_finalize_seen.discard(sid)

    def test_active_worker_is_quiesced_when_plan_readiness_is_revoked(self):
        with mock.patch.object(supervisor,"plan_ready",return_value=False):
            self.assertEqual(
                supervisor.active_implementation_plan_revocation_reason(
                    "implementer","D001"
                ),
                "dispatch_protocol_violation plan_not_ready",
            )
            self.assertEqual(
                supervisor.active_implementation_plan_revocation_reason(
                    "implementation-planner","D001"
                ),
                "",
            )
            self.assertEqual(
                supervisor.active_implementation_plan_revocation_reason(
                    "implementer",""
                ),
                "",
            )
        with mock.patch.object(supervisor,"plan_ready",return_value=True):
            self.assertEqual(
                supervisor.active_implementation_plan_revocation_reason(
                    "implementer","D001"
                ),
                "",
            )

    def test_idle_implementation_reconciliation_defers_while_plan_not_ready(self):
        sid="plan-repair-idle"
        supervisor.post_finalize_seen.discard(sid)
        supervisor.session_task[sid]=("D001",1)
        try:
            with mock.patch.object(supervisor,"plan_ready",return_value=False), \
                 mock.patch.object(supervisor,"post_session_finalize") as finalize, \
                 mock.patch.object(supervisor,"record_infrastructure_abort") as infra, \
                 mock.patch.object(supervisor,"record_leaf_failure") as failure:
                supervisor.reconcile_idle_implementation_session(sid,"implementer")
            finalize.assert_not_called()
            infra.assert_not_called()
            failure.assert_not_called()
            self.assertNotIn(sid,supervisor.post_finalize_seen)
        finally:
            supervisor.session_task.pop(sid,None)
            supervisor.post_finalize_seen.discard(sid)

    def test_compaction_seen_only_after_interrupt_succeeds(self):
        sid="fault-compaction"
        supervisor.compaction_seen.pop(sid,None)
        supervisor.session_task[sid]=("D001",1)
        old_ready=supervisor.ready_info
        old_latest=supervisor.latest_compaction_state
        old_abort=supervisor.abort_session
        supervisor.ready_info=lambda _did:{}
        supervisor.latest_compaction_state=lambda _sid:{"seq":1,"status":"completed","error_type":""}
        supervisor.abort_session=lambda *_args,**_kwargs:False
        try:
            with self.assertRaises(RuntimeError):
                supervisor.reconcile_compaction_event(
                    sid,"implementer",supervisor.MAX_IMPLEMENTATION_COMPACTIONS+1
                )
            self.assertEqual(supervisor.compaction_seen.get(sid,0),0)
        finally:
            supervisor.ready_info=old_ready
            supervisor.latest_compaction_state=old_latest
            supervisor.abort_session=old_abort
            supervisor.session_task.pop(sid,None)
            supervisor.compaction_seen.pop(sid,None)


class AbortIntentTests(unittest.TestCase):
    def test_abort_intent_persisted_before_and_after_interrupt(self):
        with tempfile.TemporaryDirectory() as td:
            old_project=supervisor.PROJECT
            old_interrupt=supervisor.http.interrupt
            old_log=supervisor.log
            old_csv=supervisor.csv
            supervisor.PROJECT=td
            supervisor.http.interrupt=lambda _sid:True
            supervisor.log=lambda *_a,**_k:None
            supervisor.csv=lambda *_a,**_k:None
            try:
                self.assertTrue(supervisor.abort_session("s1","watchdog","implementer"))
                data=json.loads(
                    (Path(td)/".opencode-v2/work/abort-intents.json").read_text()
                )
                self.assertEqual(data["sessions"]["s1"]["state"],"confirmed")
                self.assertEqual(data["sessions"]["s1"]["reason"],"watchdog")
            finally:
                supervisor.PROJECT=old_project
                supervisor.http.interrupt=old_interrupt
                supervisor.log=old_log
                supervisor.csv=old_csv
                supervisor.supervisor_abort_reasons.pop("s1",None)

    def test_requested_intent_recovers_if_db_observes_abort(self):
        with tempfile.TemporaryDirectory() as td:
            old_project=supervisor.PROJECT
            old_terminal=supervisor.session_terminal_aborted
            supervisor.PROJECT=td
            try:
                supervisor.set_abort_intent("s2","compaction","implementer","requested")
                supervisor.session_terminal_aborted=lambda _sid:True
                self.assertEqual(supervisor.persisted_abort_reason("s2"),"compaction")
            finally:
                supervisor.PROJECT=old_project
                supervisor.session_terminal_aborted=old_terminal



class VerificationSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=self.tmp.name
        supervisor.verify_wait_log_state.clear()
        self.leaves={
            "D001":{
                "id":"D001","name":"consumer",
                "owned_artifacts":"`a.txt`","owned_artifact_paths":["a.txt"],
                "launch_deps":[],"contract_deps":[],"verify_deps":[],
                "verify_command":"test -f a.txt","role":"implementer",
                "done_when":"a is valid","acceptance_ids":["A001"],
                "parallel":"none","split_children":[],
            },
            "D002":{
                "id":"D002","name":"dependency",
                "owned_artifacts":"`b.txt`","owned_artifact_paths":["b.txt"],
                "launch_deps":[],"contract_deps":[],"verify_deps":[],
                "verify_command":"test -f b.txt","role":"implementer",
                "done_when":"b is valid","acceptance_ids":["A001"],
                "parallel":"none","split_children":[],
            },
        }
        self._write_manifest()
        mark_phase_ready(self.project,"IMPLEMENTATION_PLAN.md","IMPLEMENTATION_PLAN_COMPLETE")
        self.ledger={
            "owner":"supervisor",
            "deliverables":{
                "D001":{"count":1,"sessions":["s1"],"automatic_limit":3},
                "D002":{"count":1,"sessions":["s2"],"automatic_limit":3},
            },
        }
        self._write_ledger()
        (self.project/"a.txt").write_text("a\n")
        (self.project/"b.txt").write_text("b\n")
    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor.verify_wait_log_state.clear()
        self.tmp.cleanup()
    def _write_manifest(self):
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9",
            "project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":self.leaves,
        }))
    def _write_ledger(self):
        (self.work/"attempts.json").write_text(json.dumps(self.ledger))
    def _ready(self,did,attempt=1):
        command=self.leaves[did]["verify_command"]
        (self.work/f"{did}.ready").write_text(ready_text(did,attempt,verify_command=command))

    def _functional_correction(self,did,command,stdout_json):
        correction="test functional execution correction"
        (self.work/f"{did}.execution-contract-correction.json").write_text(
            json.dumps({
                "owner":"supervisor",
                "protocol":"v2-external-execution-contract-correction-v1",
                "deliverable":did,
                "correction":correction,
                "correction_sha256":hashlib.sha256(
                    correction.encode("utf-8")
                ).hexdigest(),
                "functional_diagnostic":{
                    "command":command,
                    "stdout_json":stdout_json,
                },
            })
        )

    def test_contract_dep_is_hard_dispatch_barrier_until_ready(self):
        self.leaves["D001"]["contract_deps"]=["D002"]
        self._write_manifest()
        state=control_state.snapshot(self.project)
        self.assertEqual(state["leaves"]["D001"]["contract_deps_missing"],["D002"])
        self.assertFalse(state["leaves"]["D001"]["eligible"])
        self._ready("D002")
        state=control_state.snapshot(self.project)
        self.assertEqual(state["leaves"]["D001"]["contract_deps_missing"],[])
        self.assertTrue(state["leaves"]["D001"]["eligible"])

    def test_verify_dep_defers_without_consuming_or_redispatching(self):
        self.leaves["D001"]["verify_deps"]=["D002"]
        self._write_manifest()
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertEqual(detail,"verify-deps-pending:D002")
        self.assertTrue((self.work/"D001.verify-wait.json").exists())
        state=control_state.snapshot(self.project)
        leaf=state["leaves"]["D001"]
        self.assertTrue(leaf["verification_pending"])
        self.assertEqual(leaf["verify_deps_missing"],["D002"])
        self.assertFalse(leaf["eligible"])
        self.assertEqual(self.ledger["deliverables"]["D001"]["count"],1)
        self._ready("D002")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertTrue(ok,detail)
        self.assertTrue(control_state.ready_info(self.project,"D001"))
        self.assertFalse((self.work/"D001.verify-wait.json").exists())

    def test_supervisor_control_plane_churn_is_not_worker_ownership(self):
        supervisor.write_ownership_baseline("D001")
        (self.ctrl/"IMPLEMENTATION_PLAN.md").write_text("generated plan\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text("{}\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.structured-map.json").write_text("{}\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard-errors.txt").write_text("repair\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.repair.json").write_text("{}\n")
        (self.ctrl/"TEST_REPORT.json").write_text('{"status":"pass"}\n')
        logs=self.ctrl/"test-logs"
        logs.mkdir()
        (logs/"A001.log").write_text("ok\n")
        self.assertEqual(supervisor.ownership_violations("D001","s1"),[])

    def test_pipefail_semantics_reject_hidden_pipeline_failure(self):
        self.leaves["D001"]["verify_command"]="false | true"
        self._write_manifest()
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertEqual(detail,"verify-failed-1")
        self.assertFalse(control_state.ready_info(self.project,"D001"))

    def test_functional_diagnostic_blocks_empty_json_output(self):
        self._functional_correction(
            "D001",
            "printf ''",
            {
                "required_tokens":["Io","Europa","Ganymede","Callisto"],
                "required_key_substring_groups":[
                    ["offset","arcsec"],["order","side"],["occlu"],["shadow"],
                ],
            },
        )
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertEqual(detail,"functional-diagnostic-empty-json-output")
        self.assertFalse(control_state.ready_info(self.project,"D001"))
        evidence=json.loads(
            (self.work/"D001.functional-diagnostic-evidence.json").read_text()
        )
        self.assertEqual(evidence["result"],detail)

    def test_functional_diagnostic_allows_matching_json_output(self):
        payload=(
            '{"moons":{'
            '"Io":{"offset_arcsec":1,"side":"leading","occluded":false,'
            '"transit_shadow":false},'
            '"Europa":{},"Ganymede":{},"Callisto":{}}}'
        )
        self._functional_correction(
            "D001",
            "printf '%s\\n' '"+payload+"'",
            {
                "required_tokens":["Io","Europa","Ganymede","Callisto"],
                "required_key_substring_groups":[
                    ["offset","arcsec"],["order","side"],["occlu"],["shadow"],
                ],
            },
        )
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertTrue(ok,detail)
        self.assertEqual(detail,"finalized")
        self.assertTrue(control_state.ready_info(self.project,"D001"))
        evidence=json.loads(
            (self.work/"D001.functional-diagnostic-evidence.json").read_text()
        )
        self.assertEqual(evidence["result"],"functional-verified")

    def test_split_writer_inherits_parent_functional_diagnostic(self):
        self.leaves["D003"]={
            "id":"D003","name":"parent",
            "owned_artifacts":"`a.txt`","owned_artifact_paths":["a.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":"test -f a.txt","role":"implementer",
            "done_when":"parent functional output is valid",
            "acceptance_ids":["A001"],"parallel":"none",
            "split_children":["D002","D001"],
        }
        self.leaves["D001"]["parent"]="D003"
        self.leaves["D001"]["split_handoff_source"]="D002"
        self._write_manifest()
        self._functional_correction(
            "D003",
            "printf ''",
            {
                "required_tokens":["Io","Europa","Ganymede","Callisto"],
                "required_key_substring_groups":[
                    ["offset","arcsec"],["order","side"],["occlu"],["shadow"],
                ],
            },
        )
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertEqual(detail,"functional-diagnostic-empty-json-output")
        self.assertFalse(control_state.ready_info(self.project,"D001"))
        evidence=json.loads(
            (self.work/"D001.functional-diagnostic-evidence.json").read_text()
        )
        self.assertEqual(evidence["contract_source_deliverable"],"D003")
        self.assertEqual(evidence["result"],detail)

    def test_verify_evidence_preserves_fail_then_pass_for_same_attempt_session(self):
        command=self.leaves["D001"]["verify_command"]
        failed=type("Checked",(),{"returncode":1,"stdout":"","stderr":"first failure\n"})()
        passed=type("Checked",(),{"returncode":0,"stdout":"later pass\n","stderr":""})()
        supervisor.persist_supervisor_verify_evidence(
            "D001","s1",command,failed,"verify-failed-1"
        )
        supervisor.persist_supervisor_verify_evidence(
            "D001","s1",command,passed,"verified"
        )
        evidence=supervisor.load_supervisor_verify_evidence("D001")
        self.assertEqual(
            [(row["attempt"],row["session"],row["result"],row["exit_code"])
             for row in evidence["entries"]],
            [(1,"s1","verify-failed-1",1),(1,"s1","verified",0)],
        )
        self.assertEqual(evidence["entries"][0]["stderr"],"first failure\n")
        self.assertEqual(evidence["latest"]["stdout"],"later pass\n")

    def test_masking_verify_command_is_rejected_before_execution(self):
        errors=leaf_contract.validate_verify_command("test -f missing || true")
        self.assertTrue(any("can mask a failed check" in e for e in errors),errors)
        self.leaves["D001"]["verify_command"]="test -f missing || true"
        self._write_manifest()
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("verify-command-unsafe:"),detail)

    def test_verify_cannot_repair_its_owned_artifact(self):
        self.leaves["D001"]["verify_command"]="printf changed > a.txt"
        self._write_manifest()
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("verify-mutated-owned-artifacts:a.txt"),detail)
        self.assertFalse(control_state.ready_info(self.project,"D001"))

    def test_verify_side_effect_outside_ownership_fails_post_check(self):
        self.leaves["D001"]["verify_command"]="touch evil.txt"
        self._write_manifest()
        supervisor.write_ownership_baseline("D001")
        ok,detail=supervisor.post_session_finalize("D001",sid="s1")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("ownership-violation-after-verify:evil.txt"),detail)
        self.assertFalse(control_state.ready_info(self.project,"D001"))

    def test_python_bytecode_cache_is_not_an_ownership_violation(self):
        supervisor.write_ownership_baseline("D001")
        cache=self.project/"reference"/"__pycache__"
        cache.mkdir(parents=True)
        (cache/"reference.cpython-314.pyc").write_bytes(b"generated bytecode")
        self.assertEqual(supervisor.ownership_violations("D001","s1"),[])
        fingerprints=supervisor.project_fingerprints()
        self.assertNotIn(
            "reference/__pycache__/reference.cpython-314.pyc",
            fingerprints,
        )

    def test_historical_python_cache_entry_in_baseline_is_ignored(self):
        supervisor.write_ownership_baseline("D001")
        path=supervisor.ownership_baseline_path("D001")
        data=json.loads(path.read_text())
        data["files"]["reference/__pycache__/reference.cpython-314.pyc"]="old"
        path.write_text(json.dumps(data))
        self.assertEqual(supervisor.ownership_violations("D001","s1"),[])

    def test_sibling_ownership_survives_supervisor_restart_window(self):
        supervisor.write_ownership_baseline("D001")
        (self.project/"b.txt").write_text("changed by D002\n")

        class FakeCursor:
            def __init__(self,owner):
                self.owner=owner
            def fetchall(self):
                return [("s2",500)]
        class FakeConnection:
            def __init__(self):
                self.params=None
            def execute(self,_query,params):
                self.params=params
                return FakeCursor(self)
            def close(self):
                pass

        con=FakeConnection()
        with mock.patch.object(
            supervisor,"session_window",return_value=(1000,2000)
        ), mock.patch.object(
            supervisor,"v1_runtime_enabled",return_value=True
        ), mock.patch.object(
            supervisor,"_v1_active_session_ids",return_value=set()
        ), mock.patch.object(
            supervisor,"db_connect",return_value=con
        ), mock.patch.object(
            supervisor,"_v1_session_terminal",return_value=True
        ), mock.patch.object(
            supervisor,"_v1_session_end_ms",return_value=1500
        ), mock.patch.object(
            supervisor,"first_user_text_db",
            side_effect=lambda sid: "DELIVERABLE: D002" if sid=="s2" else "",
        ), mock.patch.object(
            supervisor,"load_manifest",return_value={"leaves":self.leaves}
        ), mock.patch.object(
            supervisor,"session_explicitly_mutated_path",return_value=False
        ):
            self.assertEqual(supervisor.ownership_violations("D001","s1"),[])
        self.assertEqual(con.params,(str(self.project),"s1"))

    def test_false_ownership_recovery_finalizes_same_attempt_and_clears_unstarted_split(self):
        supervisor.write_ownership_baseline("D001")
        supervisor.write_execution_baseline("D001",1)
        attempts=json.loads((self.work/"attempts.json").read_text())
        entry=attempts["deliverables"]["D001"]
        entry["failure_history"]=[{
            "attempt":1,
            "classification":"genuine",
            "reason":"ownership-violation:b.txt",
            "session":"s1",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:00:00Z",
        }]
        entry["split_required"]={
            "generation":1,
            "reason":"genuine-failure-threshold",
            "timestamp":"2026-09-25T00:00:01Z",
        }
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        supervisor.atomic_write_json(
            supervisor.split_request_path("D001"),
            {"protocol":"v2-task-split-proposal-v2","parent_id":"D001","generation":1},
        )
        supervisor.atomic_write_json(
            supervisor.split_status_path("D001"),
            {
                "owner":"supervisor","parent_id":"D001",
                "generation":1,"state":"split-required",
            },
        )

        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"durable_worker_execution",return_value=True
        ), mock.patch.object(
            supervisor,"worker_sandbox_has_fatal_violation",return_value=False
        ):
            ok,detail=supervisor.recover_false_ownership_finalize("D001")

        self.assertTrue(ok,detail)
        self.assertEqual(detail,"finalized")
        self.assertTrue((self.work/"D001.ready").exists())
        after=json.loads((self.work/"attempts.json").read_text())
        entry=after["deliverables"]["D001"]
        self.assertEqual(entry["failure_history"][0]["reason"],"ownership-violation:b.txt")
        marker=entry["false_ownership_finalize_recovery"]
        self.assertEqual(marker["state"],"finalized")
        self.assertEqual(marker["attempt"],1)
        self.assertEqual(marker["session"],"s1")
        self.assertNotIn("split_required",entry)
        self.assertFalse(supervisor.split_request_path("D001").exists())
        self.assertFalse(supervisor.split_status_path("D001").exists())

    def test_false_ownership_recovery_normalizes_same_attempt_verify_failure(self):
        supervisor.write_ownership_baseline("D001")
        attempts=json.loads((self.work/"attempts.json").read_text())
        entry=attempts["deliverables"]["D001"]
        entry["failure_history"]=[{
            "attempt":1,
            "classification":"genuine",
            "reason":"ownership-violation:.opencode-v2/IMPLEMENTATION_PLAN.md",
            "session":"s1",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:00:00Z",
        }]
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"durable_worker_execution",return_value=True
        ), mock.patch.object(
            supervisor,"worker_sandbox_has_fatal_violation",return_value=False
        ), mock.patch.object(
            supervisor,"ownership_violations",return_value=[]
        ), mock.patch.object(
            supervisor,"post_session_finalize",
            return_value=(False,"verify-failed-1")
        ):
            ok,detail=supervisor.recover_false_ownership_finalize("D001")
        self.assertFalse(ok)
        self.assertEqual(detail,"verify-failed-1")
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        row=entry["failure_history"][0]
        self.assertEqual(row["reason"],"verify-failed-1")
        self.assertEqual(
            row["original_reason"],
            "ownership-violation:.opencode-v2/IMPLEMENTATION_PLAN.md",
        )
        self.assertEqual(
            row["reclassified_by"],
            "runtime-false-ownership-verify-recheck",
        )
        marker=entry["false_ownership_finalize_recovery"]
        self.assertTrue(marker["ownership_disproven"])
        self.assertEqual(marker["terminal_reason_normalized"],"verify-failed-1")

    def test_false_ownership_recovery_refuses_still_violating_session(self):
        supervisor.write_ownership_baseline("D001")
        attempts=json.loads((self.work/"attempts.json").read_text())
        entry=attempts["deliverables"]["D001"]
        entry["failure_history"]=[{
            "attempt":1,
            "classification":"genuine",
            "reason":"ownership-violation:evil.txt",
            "session":"s1",
            "source":"supervisor",
            "timestamp":"2026-09-25T00:00:00Z",
        }]
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        (self.project/"evil.txt").write_text("still foreign\n")
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"durable_worker_execution",return_value=True
        ), mock.patch.object(
            supervisor,"worker_sandbox_has_fatal_violation",return_value=False
        ):
            ok,detail=supervisor.recover_false_ownership_finalize("D001")
        self.assertFalse(ok)
        self.assertTrue(
            detail.startswith("false-ownership-recovery-still-violating:evil.txt"),
            detail,
        )
        self.assertFalse((self.work/"D001.ready").exists())

    def test_recovers_false_ownership_without_split_status(self):
        self.ledger["deliverables"]["D001"]={
            "count":1,
            "sessions":["s1"],
            "automatic_limit":3,
            "failure_history":[{
                "attempt":1,
                "classification":"genuine",
                "reason":"ownership-violation:b.txt",
                "session":"s1",
                "source":"supervisor",
                "timestamp":"2026-09-25T00:01:00Z",
            }],
        }
        self._write_ledger()
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"overlapping_other_owned_paths",return_value={"b.txt"}
        ), mock.patch.object(
            supervisor,"session_explicitly_mutated_path",return_value=False
        ), mock.patch.object(
            supervisor,"ownership_violations",return_value=[]
        ):
            proofs,archives=supervisor.recover_ownership_attribution_failures(
                ["D001"]
            )
        self.assertEqual([row["attempt"] for row in proofs["D001"]],[1])
        self.assertEqual(archives,{})
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        self.assertEqual(
            entry["failure_history"][0].get("recovered_by"),
            supervisor.OWNERSHIP_ATTRIBUTION_RECOVERY_MARKER,
        )

    def test_recovers_proven_false_ownership_rows_and_clears_unclaimed_split(self):
        self.ledger["deliverables"]["D001"]={
            "count":2,
            "sessions":["s-old","s1"],
            "automatic_limit":3,
            "failure_history":[
                {
                    "attempt":1,
                    "classification":"genuine",
                    "reason":"ownership-violation:reference/__pycache__/x.pyc",
                    "session":"s-old",
                    "source":"supervisor",
                    "timestamp":"2026-09-25T00:00:00Z",
                },
                {
                    "attempt":2,
                    "classification":"genuine",
                    "reason":"ownership-violation:b.txt",
                    "session":"s1",
                    "source":"supervisor",
                    "timestamp":"2026-09-25T00:01:00Z",
                },
            ],
            "split_required":{
                "generation":1,
                "reason":"genuine-failure-threshold",
                "timestamp":"2026-09-25T00:01:00Z",
            },
        }
        self._write_ledger()
        (self.work/"D001.split-request.json").write_text('{"parent_id":"D001"}\n')
        supervisor.save_split_status("D001","split-required",generation=1)
        with mock.patch.object(
            supervisor,"v1_session_status_snapshot",return_value={}
        ), mock.patch.object(
            supervisor,"overlapping_other_owned_paths",return_value={"b.txt"}
        ), mock.patch.object(
            supervisor,"session_explicitly_mutated_path",return_value=False
        ), mock.patch.object(
            supervisor,"ownership_violations",return_value=[]
        ):
            proofs,archives=supervisor.recover_ownership_attribution_failures(
                ["D001"]
            )
        self.assertEqual(
            [row["attempt"] for row in proofs["D001"]],[1,2]
        )
        self.assertTrue(archives["D001"])
        entry=json.loads((self.work/"attempts.json").read_text())[
            "deliverables"
        ]["D001"]
        self.assertNotIn("split_required",entry)
        self.assertEqual(
            [row["classification"] for row in entry["failure_history"]],
            ["genuine","genuine"],
        )
        self.assertTrue(all(
            row.get("recovered_by")==
                supervisor.OWNERSHIP_ATTRIBUTION_RECOVERY_MARKER
            for row in entry["failure_history"]
        ))
        self.assertFalse((self.work/"D001.split-request.json").exists())
        self.assertFalse((self.work/"D001.split-status.json").exists())
        self.assertEqual(
            sum(
                1 for row in entry["failure_history"]
                if supervisor.failure_counts_as_genuine(row)
            ),
            0,
        )

    def test_corrupt_verify_wait_fails_closed(self):
        (self.work/"D001.verify-wait.json").write_text("{broken")
        with self.assertRaises(state_io.StateCorruptionError):
            control_state.snapshot(self.project)


class SplitStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=self.tmp.name
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.parent={
            "id":"D001","name":"parent",
            "owned_artifacts":"`a.txt`, `b.txt`",
            "owned_artifact_paths":["a.txt","b.txt"],
            "launch_deps":[],"contract_deps":["D009"],"verify_deps":["D010"],
            "verify_command":"test -f a.txt -a -f b.txt",
            "role":"implementer","done_when":"both files are valid",
            "acceptance_ids":["A001","A002"],"parallel":"none","split_children":[],
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9",
            "project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.parent},
        }))
        for artifact,marker in (
            ("ACCEPTANCE.md","ACCEPTANCE_COMPLETE"),
            ("IMPLEMENTATION_PLAN.md","IMPLEMENTATION_PLAN_COMPLETE"),
        ):
            path=self.ctrl/artifact
            path.write_text(f"test {artifact}\n")
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            ready_name=artifact.removesuffix(".md")+".ready"
            (self.ctrl/ready_name).write_text(
                "status=complete\n"
                f"protocol={control_state.PHASE_READY_PROTOCOL}\n"
                f"artifact={artifact}\nmarker={marker}\n"
                f"validated={control_state.ACCEPTANCE_READY_VALIDATOR if artifact=='ACCEPTANCE.md' else control_state.PHASE_READY_VALIDATOR}\n"
                f"artifact_sha256={digest}\n"
            )
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":2,"sessions":["s1","s2"],"automatic_limit":2,
                    "failure_history":[
                        {"attempt":1,"classification":"genuine","reason":"first",
                         "timestamp":"2026-09-13T00:00:00Z","source":"supervisor"}
                    ],
                }
            },
        }))
    def tearDown(self):
        supervisor.PROJECT=self.old_project
        supervisor._split_parent_finalize_next.clear()
        self.tmp.cleanup()

    def proposals(self):
        return [
            {
                "scope":"write a",
                "owned_artifacts":"`a.txt`",
                "verify_command":"test -f a.txt -a -f b.txt",
                "role":"implementer",
                "depends_on_sibling":"",
                "done_when":"a exists",
                "reads_existing":[],
                "creates_or_updates":["a.txt"],
            },
            {
                "scope":"write b",
                "owned_artifacts":"`b.txt`",
                "verify_command":"test -f a.txt -a -f b.txt",
                "role":"implementer",
                "depends_on_sibling":"",
                "done_when":"b exists",
                "reads_existing":[],
                "creates_or_updates":["b.txt"],
            },
        ]

    def test_failure_to_split_edge_survives_request_materialization_failure(self):
        old=supervisor.split_request
        supervisor.split_request=lambda _did: (_ for _ in ()).throw(RuntimeError("fault after ledger commit"))
        try:
            with self.assertRaises(RuntimeError):
                supervisor.record_leaf_failure("D001","second","genuine")
        finally:
            supervisor.split_request=old
        ledger=json.loads((self.work/"attempts.json").read_text())
        self.assertIsInstance(ledger["deliverables"]["D001"].get("split_required"),dict)
        self.assertFalse((self.work/"D001.split-request.json").exists())
        supervisor.reconcile_required_splits()
        self.assertTrue((self.work/"D001.split-request.json").exists())

    def test_split_request_contains_full_parent_contract(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        req=json.loads((self.work/"D001.split-request.json").read_text())
        contract=req["parent_contract"]
        self.assertEqual(contract["acceptance_ids"],["A001","A002"])
        self.assertEqual(contract["contract_deps"],["D009"])
        self.assertEqual(contract["verify_deps"],["D010"])
        self.assertEqual(contract["done_when"],"both files are valid")

    def test_split_request_carries_authoritative_failed_verify_evidence(self):
        checked=type("Checked",(),{"returncode":1,"stdout":"","stderr":"missing b.txt\n"})()
        supervisor.persist_supervisor_verify_evidence(
            "D001","s2",self.parent["verify_command"],checked,"verify-failed-1"
        )
        supervisor.record_leaf_failure("D001","second","genuine")
        req=json.loads((self.work/"D001.split-request.json").read_text())
        evidence=req["supervisor_verify_evidence"][-1]
        self.assertEqual(evidence["command"],self.parent["verify_command"])
        self.assertEqual(evidence["exit_code"],1)
        self.assertEqual(evidence["result"],"verify-failed-1")
        self.assertEqual(evidence["stderr"],"missing b.txt\n")

    def test_functional_diagnostic_failure_enables_split_recovery_and_refresh(self):
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"runtime json failed\n"
        })()
        supervisor.persist_functional_diagnostic_evidence(
            "D001","s2",
            "python3 reference.py sample",
            checked,
            "functional-diagnostic-verify-failed-1",
        )
        self.assertEqual(
            supervisor.record_leaf_failure(
                "D001","functional-diagnostic-verify-failed-1","genuine"
            ),
            (True,"split-required"),
        )
        path=self.work/"D001.split-request.json"
        req=json.loads(path.read_text())
        self.assertTrue(
            req["decomposition_policy"]["verification_recovery_allowed"]
        )
        self.assertEqual(
            req["supervisor_functional_diagnostic_evidence"]["result"],
            "functional-diagnostic-verify-failed-1",
        )

        # Simulate a request materialized by the pre-functional policy, then
        # prove restart reconciliation refreshes it from supervisor evidence.
        req["decomposition_policy"]["verification_recovery_allowed"]=False
        req.pop("supervisor_functional_diagnostic_evidence",None)
        req["evidence_precedence"]="legacy"
        path.write_text(json.dumps(req))
        self.assertEqual(
            supervisor.split_request("D001"),
            (True,"split-required"),
        )
        refreshed=json.loads(path.read_text())
        self.assertTrue(
            refreshed["decomposition_policy"]["verification_recovery_allowed"]
        )
        self.assertEqual(
            refreshed["supervisor_functional_diagnostic_evidence"]["session"],
            "s2",
        )
        self.assertIn(
            "failed supervisor_functional_diagnostic_evidence",
            refreshed["decomposition_policy"]["parent_verify_invalid_precedence"],
        )

    def test_split_rejects_nonexistent_local_unittest_module_target(self):
        (self.project/"tests").mkdir(exist_ok=True)
        (self.project/"tests"/"test_summary.py").write_text(
            "import unittest\n"
        )
        proposals=self.proposals()
        proposals[0]["verify_command"]="python3 -m unittest test_summary.SummarizeTests -v"
        proposals[0]["done_when"]="summary behavior passes"
        with self.assertRaisesRegex(
            ValueError,
            "definitely local but has no existing/prospective importable module",
        ):
            supervisor.validate_split_proposal("D001",proposals,request={})

    def test_split_accepts_discover_and_prospective_owned_unittest_module(self):
        (self.project/"tests").mkdir(exist_ok=True)
        (self.project/"tests"/"test_summary.py").write_text("import unittest\nclass SummarizeTests(unittest.TestCase):\n    def test_ok(self): pass\n")
        proposals=self.proposals()
        proposals[0]["verify_command"]="python3 -m unittest discover -s tests -v"
        proposals[0]["done_when"]="discovered tests pass"
        supervisor.validate_split_proposal("D001",proposals,request={})

        self.assertEqual(
            supervisor._split_unittest_target_reference_errors(
                "python3 -m unittest test_new.NewTests -v",
                ["test_new.py"],
            ),
            [],
        )
        self.assertEqual(
            supervisor._split_unittest_target_reference_errors(
                "python3 -m unittest tests.test_summary.SummarizeTests -v",
                [],
            ),
            [],
        )

    def test_split_rejects_invalid_existing_unittest_attribute_path(self):
        root=self.project/"spec_tests"
        root.mkdir(exist_ok=True)
        (root/"__init__.py").write_text("")
        (root/"test_summary.py").write_text(
            "import unittest\n"
            "class SummaryTests(unittest.TestCase):\n"
            "    def test_mixed(self): pass\n"
        )
        bad=(
            "python3 -m unittest "
            "spec_tests.test_summary.test_summary.SummaryTests.test_mixed -v"
        )
        errors=supervisor._split_unittest_target_reference_errors(bad,[])
        self.assertEqual(len(errors),1)
        self.assertIn("invalid local attribute path",errors[0])
        self.assertIn("missing local unittest attribute: test_summary",errors[0])

        good=(
            "python3 -m unittest "
            "spec_tests.test_summary.SummaryTests.test_mixed -v"
        )
        self.assertEqual(
            supervisor._split_unittest_target_reference_errors(good,[]),
            [],
        )

    def test_final_test_split_writer_cannot_weaken_run_checks_verify(self):
        self.parent.update({
            "owned_artifacts":"`.opencode-v2/TEST_CHECKS.json`",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
            "verify_command":supervisor.RUN_CHECKS_COMMAND,
            "role":"test-builder",
            "done_when":"canonical final checks pass",
        })
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9",
            "project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.parent},
        }))
        proposals=[
            {
                "scope":"inspect final test contract",
                "owned_artifacts":"none",
                "verify_command":supervisor.SPLIT_HANDOFF_VERIFY_SENTINEL,
                "role":"probe-builder",
                "depends_on_sibling":"",
                "done_when":"handoff ready",
                "reads_existing":[],
                "creates_or_updates":[],
            },
            {
                "scope":"write final test manifest",
                "owned_artifacts":"`.opencode-v2/TEST_CHECKS.json`",
                "verify_command":"python3 -c \"import json; assert json.load(open('.opencode-v2/TEST_CHECKS.json')).get('checks')\"",
                "role":"test-builder",
                "depends_on_sibling":"first",
                "done_when":"manifest exists",
                "reads_existing":[],
                "creates_or_updates":[".opencode-v2/TEST_CHECKS.json"],
            },
        ]
        with self.assertRaisesRegex(
            ValueError,
            "final-test manifest child must preserve exact parent Verify",
        ):
            supervisor.validate_split_proposal("D001",proposals,request={})
        proposals[1]["verify_command"]=supervisor.RUN_CHECKS_COMMAND
        expected,children=supervisor.validate_split_proposal("D001",proposals,request={})
        self.assertEqual(expected,["D001-A","D001-B"])
        self.assertEqual(children[1]["verify_command"],supervisor.RUN_CHECKS_COMMAND)

    def test_progress_handoff_writer_preserves_exact_parent_verify(self):
        proposals=[
            {
                "scope":"diagnose exact failed parent verify",
                "owned_artifacts":"none",
                "verify_command":supervisor.SPLIT_HANDOFF_VERIFY_SENTINEL,
                "role":"probe-builder",
                "depends_on_sibling":"",
                "done_when":"handoff ready",
                "reads_existing":[],
                "creates_or_updates":[],
            },
            {
                "scope":"repair all parent artifacts",
                "owned_artifacts":"`a.txt`, `b.txt`",
                "verify_command":"test -f a.txt",
                "role":"implementer",
                "depends_on_sibling":"first",
                "done_when":"both files are valid",
                "reads_existing":[],
                "creates_or_updates":["a.txt","b.txt"],
            },
        ]
        with self.assertRaisesRegex(
            ValueError,
            "writer after progress handoff must preserve exact parent Verify",
        ):
            supervisor.validate_split_proposal("D001",proposals,request={})
        proposals[1]["verify_command"]=self.parent["verify_command"]
        expected,children=supervisor.validate_split_proposal(
            "D001",proposals,request={}
        )
        self.assertEqual(expected,["D001-A","D001-B"])
        self.assertEqual(
            children[1]["verify_command"],self.parent["verify_command"]
        )

    def test_exhausted_splitter_uses_deterministic_probe_writer_fallback(self):
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"missing b.txt\n"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001","s2",self.parent["verify_command"],checked,"verify-failed-1"
        )
        self.assertEqual(
            supervisor.record_leaf_failure("D001","verify-failed-1","genuine"),
            (True,"split-required"),
        )
        supervisor.save_split_status(
            "D001","split-validation-failed",
            claim_count=supervisor.MAX_SPLITTER_ATTEMPTS,
            proposal_failures=3,
            corrective_turn_count=1,
            corrective_dispatch_state="native-child-completed",
        )
        ok,detail=supervisor.recover_exhausted_splitter_deterministic_handoff("D001")
        self.assertEqual((ok,detail),(True,"accepted"))
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["claim_count"],supervisor.MAX_SPLITTER_ATTEMPTS)
        self.assertEqual(status["corrective_turn_count"],1)
        fallback=status["deterministic_splitter_fallback"]
        self.assertEqual(fallback["protocol"],"v2-deterministic-splitter-fallback-v1")
        self.assertEqual(fallback["children"],["D001-A","D001-B"])
        leaves=supervisor.load_manifest()["leaves"]
        self.assertTrue(leaves["D001-A"]["split_handoff_only"])
        self.assertEqual(leaves["D001-A"]["owned_artifact_paths"],[])
        self.assertEqual(
            leaves["D001-B"]["owned_artifact_paths"],
            self.parent["owned_artifact_paths"],
        )
        self.assertEqual(
            leaves["D001-B"]["verify_command"],
            self.parent["verify_command"],
        )
        self.assertEqual(leaves["D001-B"]["split_handoff_source"],"D001-A")

    def test_reconcile_auto_applies_exhausted_splitter_fallback(self):
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"missing b.txt\n"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001","s2",self.parent["verify_command"],checked,"verify-failed-1"
        )
        self.assertEqual(
            supervisor.record_leaf_failure("D001","verify-failed-1","genuine"),
            (True,"split-required"),
        )
        supervisor.save_split_status(
            "D001","split-validation-failed",
            claim_count=supervisor.MAX_SPLITTER_ATTEMPTS,
            proposal_failures=3,
            corrective_turn_count=1,
            corrective_dispatch_state="native-child-completed",
        )
        supervisor.reconcile_split_proposals()
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"accepted")
        self.assertEqual(
            status["deterministic_splitter_fallback"]["protocol"],
            "v2-deterministic-splitter-fallback-v1",
        )
        self.assertEqual(
            supervisor.leaf_children("D001"),["D001-A","D001-B"]
        )

    def test_deterministic_splitter_fallback_accepts_functional_failure(self):
        checked=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"runtime json failed\n"
        })()
        supervisor.persist_functional_diagnostic_evidence(
            "D001","s2",
            "python3 reference.py sample",
            checked,
            "functional-diagnostic-verify-failed-1",
        )
        self.assertEqual(
            supervisor.record_leaf_failure(
                "D001","functional-diagnostic-verify-failed-1","genuine"
            ),
            (True,"split-required"),
        )
        request=json.loads((self.work/"D001.split-request.json").read_text())
        self.assertEqual(request["supervisor_verify_evidence"],[])
        self.assertEqual(
            request["supervisor_functional_diagnostic_evidence"]["result"],
            "functional-diagnostic-verify-failed-1",
        )
        supervisor.save_split_status(
            "D001","split-validation-failed",
            claim_count=supervisor.MAX_SPLITTER_ATTEMPTS,
            proposal_failures=3,
            corrective_turn_count=1,
            corrective_dispatch_state="native-child-completed",
        )
        self.assertEqual(
            supervisor.recover_exhausted_splitter_deterministic_handoff("D001"),
            (True,"accepted"),
        )
        leaves=supervisor.load_manifest()["leaves"]
        self.assertTrue(leaves["D001-A"]["split_handoff_only"])
        self.assertEqual(
            leaves["D001-B"]["owned_artifact_paths"],
            self.parent["owned_artifact_paths"],
        )

    def test_deterministic_splitter_fallback_requires_completed_corrective(self):
        checked=type("Checked",(),{"returncode":1,"stdout":"","stderr":""})()
        supervisor.persist_supervisor_verify_evidence(
            "D001","s2",self.parent["verify_command"],checked,"verify-failed-1"
        )
        supervisor.record_leaf_failure("D001","verify-failed-1","genuine")
        supervisor.save_split_status(
            "D001","split-validation-failed",
            claim_count=supervisor.MAX_SPLITTER_ATTEMPTS,
            proposal_failures=2,
        )
        self.assertEqual(
            supervisor.recover_exhausted_splitter_deterministic_handoff("D001"),
            (False,"deterministic-fallback-requires-corrective-turn"),
        )
        self.assertEqual(supervisor.leaf_children("D001"),[])

    def test_parent_contract_invalid_reason_remains_bounded_and_strict(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        payload={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"verify_command","reason":"x"*1201,
        }
        with self.assertRaisesRegex(ValueError,"reason must be 20..1200"):
            supervisor.request_parent_contract_repair(
                "D001",payload,json.loads((self.work/"D001.split-request.json").read_text())
            )

    def test_historical_failed_claim_gets_exactly_one_same_claim_corrective_turn(self):
        self.assertEqual(
            supervisor.record_leaf_failure("D001","second","genuine"),
            (True,"split-required"),
        )
        payload={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"verify_command","reason":"x"*1201,
        }
        archive=self.work/"D001.split-proposal.failed-2.json"
        archive.write_text(json.dumps(payload))
        supervisor.save_split_status(
            "D001","split-validation-failed",
            claim_count=2,proposal_failures=2,
            session="historical-primary",
            archived_proposal=str(archive.relative_to(self.project)),
            reason="parent-contract-invalid reason must be 20..1200 chars",
        )
        stage_a_controller.save_execution_ledger(self.project,{
            "owner":"stage-a-controller",
            "protocol":stage_a_controller.EXECUTION_LEDGER_PROTOCOL,
            "executions":{"historical-execution":{
                "execution_id":"historical-execution",
                "state_version":"historical-state",
                "root_session":"technical-root",
                "action":{
                    "kind":"launch","agent":"task-splitter",
                    "deliverable":"D001","generation":1,
                },
                "transport":"prompt_async+SubtaskPart",
                "transport_may_have_been_attempted":True,
            }},
        })
        with mock.patch.object(
            supervisor,"last_assistant_text_db",return_value=json.dumps(payload)
        ), mock.patch.object(
            supervisor,"splitter_primary_transport_binding",
            return_value=("technical-root","historical-token"),
        ), mock.patch.object(
            supervisor,"dispatch_splitter_corrective_turn",
            return_value=(True,"accepted-after-root-idle"),
        ) as dispatch:
            ok,detail=supervisor.recover_historical_splitter_corrective_turn("D001")
        self.assertEqual((ok,detail),(True,"splitter-corrective-turn-pending"))
        dispatch.assert_called_once()
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["claim_count"],2)
        self.assertEqual(status["corrective_turn_count"],1)
        self.assertEqual(status["corrective_dispatch_state"],"post-accepted")
        recovery=status["historical_corrective_recovery"]
        self.assertEqual(recovery["primary_session"],"historical-primary")
        self.assertEqual(recovery["primary_dispatch_token"],"historical-token")
        self.assertEqual(recovery["primary_root_session"],"technical-root")
        self.assertEqual(recovery["claim_count"],2)
        self.assertEqual(
            recovery["deterministic_rejection"],
            "parent-contract-invalid reason must be 20..1200 chars",
        )
        with mock.patch.object(supervisor,"dispatch_splitter_corrective_turn") as again:
            self.assertEqual(
                supervisor.recover_historical_splitter_corrective_turn("D001"),
                (False,"historical-corrective-requires-split-validation-failed"),
            )
        again.assert_not_called()

    def test_legacy_handoff_writer_verify_upgrade_preserves_history(self):
        handoff_verify=supervisor.split_handoff_verify_command("D001-A")
        child_a=dict(self.parent)
        child_a.update({
            "id":"D001-A","parent":"D001","split_children":[],
            "owned_artifacts":"none","owned_artifact_paths":[],
            "role":"probe-builder","verify_command":handoff_verify,
            "acceptance_ids":[],"verify_deps":[],
            "launch_deps":[],"contract_deps":[],"split_depth":1,
            "split_handoff_only":True,"split_handoff_source":"",
            "split_reads_existing":[],"split_creates_or_updates":[],
            "done_when":"Durable progress handoff records HANDOFF_READY, Findings, Evidence, and Next step; no project artifact is modified.",
        })
        old_writer_verify="test -f a.txt"
        child_b=dict(self.parent)
        child_b.update({
            "id":"D001-B","parent":"D001","split_children":[],
            "owned_artifacts":"`a.txt`, `b.txt`",
            "owned_artifact_paths":["a.txt","b.txt"],
            "role":"implementer","verify_command":old_writer_verify,
            "launch_deps":["D001-A"],"contract_deps":[],"verify_deps":[],"split_depth":1,
            "split_handoff_only":False,"split_handoff_source":"D001-A",
            "split_reads_existing":[],"split_creates_or_updates":["a.txt","b.txt"],
        })

        raw=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        raw["leaves"]["D001"]["split_children"]=["D001-A","D001-B"]
        raw["leaves"]["D001"]["split_depth"]=0
        raw["leaves"]["D001-A"]=child_a
        raw["leaves"]["D001-B"]=child_b
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(raw))

        old_txn={
            "owner":"supervisor","protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"committed","parent_id":"D001","generation":1,
            "children":["D001-A","D001-B"],
            "child_defs":{"D001-A":child_a,"D001-B":child_b},
            "transaction_id":"legacy-transaction",
            "prepared_at":"2026-09-20T00:00:00Z",
            "committed_at":"2026-09-20T00:01:00Z",
        }
        (self.work/"D001.split-transaction.json").write_text(json.dumps(old_txn))
        supervisor.save_split_leaf_overlay({
            "owner":"supervisor","protocol":"v2-split-leaf-overlay-v1",
            "parents":{"D001":{
                "children":["D001-A","D001-B"],
                "child_defs":{"D001-A":child_a,"D001-B":child_b},
                "transaction_id":"legacy-transaction",
                "timestamp":"2026-09-20T00:00:00Z",
            }},
        })
        supervisor.save_split_status(
            "D001","parent-finalize-failed",
            generation=1,children=["D001-A","D001-B"],
            transaction_id="legacy-transaction",
            parent_finalize_failures=supervisor.MAX_SPLIT_PARENT_FINALIZE_FAILURES,
            parent_finalize_last_result="verify-failed-1",
            reason="verify-failed-1",
        )

        attempts=supervisor.load_attempts()
        attempts["deliverables"]["D001-A"]={
            "automatic_limit":3,"count":1,"sessions":["sa"],
            "failure_history":[],
        }
        attempts["deliverables"]["D001-B"]={
            "automatic_limit":3,"count":3,
            "sessions":["sb1","sb2","sb3"],
            "failure_history":[
                {"attempt":1,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T01:00:00Z","source":"supervisor"},
                {"attempt":2,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T02:00:00Z","source":"supervisor"},
            ],
        }
        supervisor.save_attempts(attempts)
        parent_before=json.loads(json.dumps(attempts["deliverables"]["D001"]))
        writer_before=json.loads(json.dumps(attempts["deliverables"]["D001-B"]))

        checked_ok=type("Checked",(),{"returncode":0,"stdout":"","stderr":""})()
        checked_fail=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"missing vectors"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001-B","",old_writer_verify,checked_ok,"verified"
        )
        supervisor.persist_supervisor_verify_evidence(
            "D001","",self.parent["verify_command"],checked_fail,
            "verify-failed-1"
        )
        self.assertEqual(
            supervisor.supervisor_finalize_ready("D001-A",handoff_verify),
            (True,"finalized"),
        )
        self.assertEqual(
            supervisor.supervisor_finalize_ready("D001-B",old_writer_verify),
            (True,"finalized"),
        )

        ok,detail=supervisor.recover_legacy_handoff_writer_verify("D001")
        self.assertEqual(
            (ok,detail),(True,"writer-contract-replacement-ready")
        )

        after=supervisor.load_attempts()
        parent_after=after["deliverables"]["D001"]
        writer_after=after["deliverables"]["D001-B"]
        self.assertEqual(parent_after["count"],parent_before["count"])
        self.assertEqual(parent_after["sessions"],parent_before["sessions"])
        self.assertEqual(
            parent_after["failure_history"],parent_before["failure_history"]
        )
        self.assertEqual(writer_after["count"],writer_before["count"])
        self.assertEqual(writer_after["sessions"],writer_before["sessions"])
        self.assertEqual(
            writer_after["failure_history"],writer_before["failure_history"]
        )
        state=control_state.attempt_state(writer_after)
        self.assertTrue(state["valid"])
        self.assertEqual(state["plan_contract_retry_grants"],1)
        self.assertEqual(state["allowed_attempts"],4)

        self.assertTrue((self.work/"D001-A.ready").exists())
        self.assertFalse((self.work/"D001-B.ready").exists())
        evidence=json.loads(
            (self.work/"D001-B.verify-evidence.json").read_text()
        )
        self.assertEqual(evidence["latest"]["command"],old_writer_verify)
        self.assertEqual(evidence["latest"]["result"],"verified")

        txn=json.loads((self.work/"D001.split-transaction.json").read_text())
        self.assertEqual(txn["generation"],2)
        self.assertEqual(txn["replaces_transaction_id"],"legacy-transaction")
        self.assertEqual(
            txn["child_defs"]["D001-B"]["verify_command"],
            self.parent["verify_command"],
        )
        self.assertNotEqual(txn["transaction_id"],"legacy-transaction")

        guard=json.loads(
            (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text()
        )
        self.assertEqual(
            guard["leaves"]["D001-B"]["verify_command"],
            self.parent["verify_command"],
        )
        overlay=supervisor.load_split_leaf_overlay()["parents"]["D001"]
        self.assertEqual(
            overlay["child_defs"]["D001-B"]["verify_command"],
            self.parent["verify_command"],
        )
        scope=(self.work/"D001-B.scope.md").read_text()
        self.assertIn(
            f"Child Verify command: `{self.parent['verify_command']}`",
            scope,
        )
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"accepted")
        self.assertEqual(status["generation"],2)
        self.assertEqual(status["parent_finalize_failures"],0)

        recovery=parent_after[
            "legacy_handoff_writer_verify_upgrades"
        ][-1]
        self.assertEqual(recovery["state"],"committed")
        self.assertEqual(
            recovery["prior_transaction_id"],"legacy-transaction"
        )
        self.assertTrue((self.project/recovery["archive"]).is_file())

    def test_nested_handoff_writer_verify_upgrade_preserves_history(self):
        parent_verify=self.parent["verify_command"]
        stale_verify="test -f a.txt"
        handoff_a=supervisor.split_handoff_verify_command("D001-A")
        handoff_b1=supervisor.split_handoff_verify_command("D001-B1")

        child_a=dict(self.parent)
        child_a.update({
            "id":"D001-A","parent":"D001","split_children":[],
            "owned_artifacts":"none","owned_artifact_paths":[],
            "role":"probe-builder","verify_command":handoff_a,
            "acceptance_ids":[],"verify_deps":[],"launch_deps":[],
            "split_depth":1,"split_handoff_only":True,
            "split_handoff_source":"","split_reads_existing":[],
            "split_creates_or_updates":[],
        })
        outer_writer_def=dict(self.parent)
        outer_writer_def.update({
            "id":"D001-B","parent":"D001","split_children":[],
            "owned_artifacts":"`a.txt`, `b.txt`",
            "owned_artifact_paths":["a.txt","b.txt"],
            "role":"implementer","verify_command":stale_verify,
            "launch_deps":["D001-A"],"split_depth":1,
            "split_handoff_only":False,"split_handoff_source":"D001-A",
            "split_reads_existing":[],"split_creates_or_updates":["a.txt","b.txt"],
        })
        live_outer_writer=dict(outer_writer_def)
        live_outer_writer["split_children"]=["D001-B1","D001-B2"]

        nested_first=dict(outer_writer_def)
        nested_first.update({
            "id":"D001-B1","parent":"D001-B","split_children":[],
            "owned_artifacts":"none","owned_artifact_paths":[],
            "role":"probe-builder","verify_command":handoff_b1,
            "acceptance_ids":[],"verify_deps":[],
            "launch_deps":["D001-A"],"split_depth":2,
            "split_handoff_only":True,"split_handoff_source":"",
            "split_reads_existing":[],"split_creates_or_updates":[],
        })
        nested_writer=dict(outer_writer_def)
        nested_writer.update({
            "id":"D001-B2","parent":"D001-B","split_children":[],
            "launch_deps":["D001-A","D001-B1"],"split_depth":2,
            "split_handoff_only":False,"split_handoff_source":"D001-B1",
            "verify_command":stale_verify,
        })

        raw=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        raw["leaves"]["D001"]["split_children"]=["D001-A","D001-B"]
        raw["leaves"]["D001-A"]=child_a
        raw["leaves"]["D001-B"]=live_outer_writer
        raw["leaves"]["D001-B1"]=nested_first
        raw["leaves"]["D001-B2"]=nested_writer
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(raw))

        outer_txn={
            "owner":"supervisor","protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"committed","parent_id":"D001","generation":1,
            "children":["D001-A","D001-B"],
            "child_defs":{"D001-A":child_a,"D001-B":outer_writer_def},
            "transaction_id":"outer-old","prepared_at":"2026-09-20T00:00:00Z",
            "committed_at":"2026-09-20T00:01:00Z",
        }
        nested_txn={
            "owner":"supervisor","protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"committed","parent_id":"D001-B","generation":1,
            "children":["D001-B1","D001-B2"],
            "child_defs":{"D001-B1":nested_first,"D001-B2":nested_writer},
            "transaction_id":"nested-old","prepared_at":"2026-09-20T01:00:00Z",
            "committed_at":"2026-09-20T01:01:00Z",
        }
        (self.work/"D001.split-transaction.json").write_text(json.dumps(outer_txn))
        (self.work/"D001-B.split-transaction.json").write_text(json.dumps(nested_txn))
        supervisor.save_split_status(
            "D001","accepted",generation=1,children=["D001-A","D001-B"],
            transaction_id="outer-old",
        )
        supervisor.save_split_status(
            "D001-B","accepted",generation=1,
            children=["D001-B1","D001-B2"],transaction_id="nested-old",
        )
        supervisor.save_split_leaf_overlay({
            "owner":"supervisor","protocol":"v2-split-leaf-overlay-v1",
            "parents":{
                "D001":{
                    "children":["D001-A","D001-B"],
                    "child_defs":{"D001-A":child_a,"D001-B":outer_writer_def},
                    "transaction_id":"outer-old","timestamp":"2026-09-20T00:00:00Z",
                },
                "D001-B":{
                    "children":["D001-B1","D001-B2"],
                    "child_defs":{"D001-B1":nested_first,"D001-B2":nested_writer},
                    "transaction_id":"nested-old","timestamp":"2026-09-20T01:00:00Z",
                },
            },
        })

        attempts=supervisor.load_attempts()
        attempts["deliverables"]["D001-B"]={
            "automatic_limit":2,"count":2,"sessions":["wb1","wb2"],
            "failure_history":[
                {"attempt":1,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T02:00:00Z","source":"supervisor"},
                {"attempt":2,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T03:00:00Z","source":"supervisor"},
            ],
        }
        attempts["deliverables"]["D001-B2"]={
            "automatic_limit":3,"count":3,"sessions":["tb1","tb2","tb3"],
            "failure_history":[
                {"attempt":1,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T04:00:00Z","source":"supervisor"},
                {"attempt":2,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T05:00:00Z","source":"supervisor"},
                {"attempt":3,"classification":"genuine","reason":"verify-failed-1",
                 "timestamp":"2026-09-20T06:00:00Z","source":"supervisor"},
            ],
        }
        supervisor.save_attempts(attempts)
        before=json.loads(json.dumps(attempts["deliverables"]["D001-B2"]))
        checked_fail=type("Checked",(),{
            "returncode":1,"stdout":"","stderr":"false positive\n"
        })()
        supervisor.persist_supervisor_verify_evidence(
            "D001-B2","tb3",stale_verify,checked_fail,"verify-failed-1"
        )

        ready={"D001-A","D001-B1"}
        with mock.patch.object(
            supervisor,"ready_info",side_effect=lambda did: (
                {"status":"complete"} if did in ready else None
            )
        ):
            ok,detail=supervisor.recover_nested_handoff_writer_verify("D001")
        self.assertEqual(
            (ok,detail),(True,"nested-writer-contract-replacement-ready")
        )

        manifest=supervisor.load_manifest()["leaves"]
        self.assertEqual(manifest["D001-B"]["verify_command"],parent_verify)
        self.assertEqual(manifest["D001-B2"]["verify_command"],parent_verify)
        after=supervisor.load_attempts()
        terminal=after["deliverables"]["D001-B2"]
        self.assertEqual(terminal["count"],before["count"])
        self.assertEqual(terminal["sessions"],before["sessions"])
        self.assertEqual(terminal["failure_history"],before["failure_history"])
        state=control_state.attempt_state(terminal)
        self.assertTrue(state["valid"])
        self.assertEqual(state["plan_contract_retry_grants"],1)
        self.assertEqual(state["allowed_attempts"],4)

        outer=json.loads((self.work/"D001.split-transaction.json").read_text())
        nested=json.loads((self.work/"D001-B.split-transaction.json").read_text())
        self.assertEqual(outer["generation"],2)
        self.assertEqual(nested["generation"],2)
        self.assertEqual(
            outer["child_defs"]["D001-B"]["verify_command"],parent_verify
        )
        self.assertEqual(
            nested["child_defs"]["D001-B2"]["verify_command"],parent_verify
        )
        recovery=after["deliverables"]["D001"][
            "nested_handoff_writer_verify_upgrades"
        ][-1]
        self.assertEqual(recovery["state"],"committed")
        self.assertTrue((self.project/recovery["outer_archive"]).is_file())
        self.assertTrue((self.project/recovery["nested_archive"]).is_file())

    def test_historical_split_child_completion_uses_old_transaction_contract(self):
        child=dict(self.parent)
        child.update({
            "id":"D001-B",
            "verify_command":"test -f a.txt",
            "owned_artifacts":"`a.txt`",
            "owned_artifact_paths":["a.txt"],
        })
        command=child["verify_command"]
        digest=hashlib.sha256(command.encode()).hexdigest()
        (self.work/"D001-B.ready").write_text(
            "status=complete\n"
            "deliverable=D001-B\n"
            "attempt=2\n"
            "verified=true\n"
            "owner=supervisor\n"
            f"verify_sha256={digest}\n"
            "protocol=v2-leaf-ready-v1\n"
        )
        (self.work/"D001-B.verify-evidence.json").write_text(json.dumps({
            "owner":"supervisor",
            "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
            "deliverable":"D001-B",
            "entries":[],
            "latest":{
                "attempt":2,"session":"old-writer",
                "timestamp":"2026-09-20T00:00:00Z",
                "command":command,"executed":True,
                "exit_code":0,"result":"verified",
                "stdout":"","stderr":"","error":"",
            },
        }))
        with mock.patch.object(supervisor,"ready_info",return_value={}):
            self.assertTrue(
                supervisor.historical_split_child_completion("D001-B",child)
            )
            child_bad=dict(child)
            child_bad["verify_command"]="test -f b.txt"
            self.assertFalse(
                supervisor.historical_split_child_completion(
                    "D001-B",child_bad
                )
            )

    def test_stale_split_contract_retires_projection_but_preserves_history(self):
        child_a=dict(self.parent)
        child_a.update({
            "id":"D001-A","parent":"D001","split_children":[],
            "owned_artifacts":"none","owned_artifact_paths":[],
            "role":"probe-builder","verify_command":"true",
        })
        child_b=dict(self.parent)
        child_b.update({
            "id":"D001-B","parent":"D001","split_children":[],
            "owned_artifacts":"`a.txt`","owned_artifact_paths":["a.txt"],
            "role":"implementer","verify_command":"test -f a.txt",
        })
        raw=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        raw["leaves"]["D001"]["split_children"]=["D001-A","D001-B"]
        raw["leaves"]["D001"]["split_depth"]=0
        raw["leaves"]["D001-A"]=child_a
        raw["leaves"]["D001-B"]=child_b
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(raw))

        txn={
            "owner":"supervisor","protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"committed","parent_id":"D001","generation":1,
            "children":["D001-A","D001-B"],
            "child_defs":{"D001-A":child_a,"D001-B":child_b},
            "transaction_id":"old-transaction",
            "prepared_at":"2026-09-20T00:00:00Z",
            "committed_at":"2026-09-20T00:01:00Z",
        }
        (self.work/"D001.split-transaction.json").write_text(json.dumps(txn))
        supervisor.save_split_status(
            "D001","accepted",
            generation=1,children=["D001-A","D001-B"],
            transaction_id="old-transaction",
            parent_finalize_failures=0,
            parent_finalize_last_result="finalized",
        )
        supervisor.save_split_leaf_overlay({
            "owner":"supervisor","protocol":"v2-split-leaf-overlay-v1",
            "parents":{"D001":{
                "children":["D001-A","D001-B"],
                "child_defs":{"D001-A":child_a,"D001-B":child_b},
                "transaction_id":"old-transaction",
                "timestamp":"2026-09-20T00:00:00Z",
            }},
        })

        attempts=supervisor.load_attempts()
        before_entry=json.loads(json.dumps(attempts["deliverables"]["D001"]))
        current_sha=hashlib.sha256(
            self.parent["verify_command"].encode()
        ).hexdigest()
        entry=attempts["deliverables"]["D001"]
        entry["plan_contract_revisions"]=[{
            "attempt":2,
            "source":"supervisor-plan-contract-revision",
            "previous_verify_sha256":"old",
            "current_verify_sha256":current_sha,
            "timestamp":"2026-09-21T00:00:00Z",
        }]
        entry["split_required"]={
            "generation":1,
            "reason":"genuine-failure-threshold",
            "timestamp":"2026-09-20T00:00:00Z",
        }
        supervisor.save_attempts(attempts)

        with mock.patch.object(
            supervisor,"ready_info",
            side_effect=lambda did: {"status":"complete"} if did in {"D001-A","D001-B"} else {},
        ):
            ok,detail=supervisor.recover_stale_split_parent_contract("D001")
        self.assertEqual((ok,detail),(True,"contract-replacement-ready"))

        after=supervisor.load_attempts()["deliverables"]["D001"]
        self.assertEqual(after["count"],before_entry["count"])
        self.assertEqual(after["sessions"],before_entry["sessions"])
        self.assertEqual(after["failure_history"],before_entry["failure_history"])
        self.assertNotIn("split_required",after)
        recovery=after["stale_split_contract_recoveries"][-1]
        self.assertEqual(recovery["state"],"committed")
        self.assertEqual(recovery["transaction_id"],"old-transaction")
        self.assertEqual(recovery["missing_current_ownership"],["b.txt"])
        self.assertTrue((self.project/recovery["archive"]).is_file())

        guard=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        self.assertEqual(guard["leaves"]["D001"]["split_children"],[])
        self.assertNotIn("D001-A",guard["leaves"])
        self.assertNotIn("D001-B",guard["leaves"])
        overlay=supervisor.load_split_leaf_overlay()
        self.assertNotIn("D001",overlay["parents"])
        self.assertFalse((self.work/"D001.split-status.json").exists())
        self.assertFalse((self.work/"D001.split-transaction.json").exists())
        state=control_state.attempt_state(after)
        self.assertTrue(state["valid"])
        self.assertEqual(state["allowed_attempts"],3)
        self.assertEqual(state["plan_contract_retry_grants"],1)

    def test_stale_split_contract_retires_nested_projection(self):
        child_a=dict(self.parent)
        child_a.update({
            "id":"D001-A","parent":"D001","split_children":[],
            "owned_artifacts":"none","owned_artifact_paths":[],
            "role":"probe-builder","verify_command":"true",
        })
        child_b=dict(self.parent)
        child_b.update({
            "id":"D001-B","parent":"D001","split_children":[],
            "owned_artifacts":"`a.txt`","owned_artifact_paths":["a.txt"],
            "role":"implementer","verify_command":"test -f a.txt",
        })
        child_b1=dict(child_b)
        child_b1.update({
            "id":"D001-B1","parent":"D001-B","split_children":[],
            "owned_artifacts":"none","owned_artifact_paths":[],
            "role":"probe-builder","split_handoff_only":True,
            "split_handoff_source":"","verify_command":"true",
        })
        child_b2=dict(child_b)
        child_b2.update({
            "id":"D001-B2","parent":"D001-B","split_children":[],
            "split_handoff_only":False,"split_handoff_source":"D001-B1",
        })
        live_b=dict(child_b)
        live_b["split_children"]=["D001-B1","D001-B2"]

        raw=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        raw["leaves"]["D001"]["split_children"]=["D001-A","D001-B"]
        raw["leaves"]["D001"]["split_depth"]=0
        raw["leaves"].update({
            "D001-A":child_a,"D001-B":live_b,
            "D001-B1":child_b1,"D001-B2":child_b2,
        })
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(raw))

        outer={
            "owner":"supervisor","protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"committed","parent_id":"D001","generation":1,
            "children":["D001-A","D001-B"],
            "child_defs":{"D001-A":child_a,"D001-B":child_b},
            "transaction_id":"outer-old",
        }
        nested={
            "owner":"supervisor","protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"committed","parent_id":"D001-B","generation":1,
            "children":["D001-B1","D001-B2"],
            "child_defs":{"D001-B1":child_b1,"D001-B2":child_b2},
            "transaction_id":"nested-old",
        }
        (self.work/"D001.split-transaction.json").write_text(json.dumps(outer))
        (self.work/"D001-B.split-transaction.json").write_text(json.dumps(nested))
        supervisor.save_split_status(
            "D001","accepted",generation=1,
            children=outer["children"],transaction_id="outer-old",
            parent_finalize_failures=0,parent_finalize_last_result="finalized",
        )
        supervisor.save_split_status(
            "D001-B","accepted",generation=1,
            children=nested["children"],transaction_id="nested-old",
            parent_finalize_failures=0,parent_finalize_last_result="finalized",
        )
        supervisor.save_split_leaf_overlay({
            "owner":"supervisor","protocol":"v2-split-leaf-overlay-v1",
            "parents":{
                "D001":{
                    "children":outer["children"],
                    "child_defs":outer["child_defs"],
                    "transaction_id":"outer-old",
                    "timestamp":"2026-09-20T00:00:00Z",
                },
                "D001-B":{
                    "children":nested["children"],
                    "child_defs":nested["child_defs"],
                    "transaction_id":"nested-old",
                    "timestamp":"2026-09-20T00:00:00Z",
                },
            },
        })

        def mark_old_ready(did,command):
            digest=hashlib.sha256(command.encode()).hexdigest()
            (self.work/f"{did}.ready").write_text(
                "status=complete\n"
                f"deliverable={did}\n"
                "attempt=1\nverified=true\nowner=supervisor\n"
                f"verify_sha256={digest}\nprotocol=v2-leaf-ready-v1\n"
            )
            (self.work/f"{did}.verify-evidence.json").write_text(json.dumps({
                "owner":"supervisor",
                "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
                "deliverable":did,"entries":[],
                "latest":{
                    "attempt":1,"session":"old","timestamp":"2026-09-20T00:00:00Z",
                    "command":command,"executed":True,"exit_code":0,
                    "result":"verified","stdout":"","stderr":"","error":"",
                },
            }))
        for did,definition in {
            "D001-A":child_a,"D001-B":child_b,
            "D001-B1":child_b1,"D001-B2":child_b2,
        }.items():
            mark_old_ready(did,definition["verify_command"])

        attempts=supervisor.load_attempts()
        entry=attempts["deliverables"]["D001"]
        current_sha=hashlib.sha256(
            self.parent["verify_command"].encode()
        ).hexdigest()
        entry["plan_contract_revisions"]=[{
            "attempt":2,"source":"supervisor-plan-contract-revision",
            "previous_verify_sha256":"old","current_verify_sha256":current_sha,
            "timestamp":"2026-09-21T00:00:00Z",
        }]
        entry["split_required"]={
            "generation":1,"reason":"genuine-failure-threshold",
            "timestamp":"2026-09-20T00:00:00Z",
        }
        supervisor.save_attempts(attempts)

        ok,detail=supervisor.recover_stale_split_parent_contract("D001")
        self.assertEqual((ok,detail),(True,"contract-replacement-ready"))
        guard=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        self.assertEqual(guard["leaves"]["D001"]["split_children"],[])
        for did in ("D001-A","D001-B","D001-B1","D001-B2"):
            self.assertNotIn(did,guard["leaves"])
            self.assertTrue((self.work/f"{did}.ready").is_file())
        overlay=supervisor.load_split_leaf_overlay()
        self.assertNotIn("D001",overlay["parents"])
        self.assertNotIn("D001-B",overlay["parents"])
        self.assertFalse((self.work/"D001.split-transaction.json").exists())
        self.assertFalse((self.work/"D001-B.split-transaction.json").exists())
        recovery=supervisor.load_attempts()["deliverables"]["D001"][
            "stale_split_contract_recoveries"
        ][-1]
        self.assertEqual(recovery["state"],"committed")
        self.assertIn("D001-B",recovery["nested_archives"])
        self.assertTrue((self.project/recovery["archive"]).is_file())
        self.assertTrue(
            (self.project/recovery["nested_archives"]["D001-B"]).is_file()
        )

    def test_valid_failed_parent_rejects_model_prerequisite_repair(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        payload={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"prerequisite_artifacts",
            "reason":"a parent-owned output is missing after exact Verify failed",
        }
        (self.work/"D001.split-proposal.json").write_text(json.dumps(payload))
        ok,detail=supervisor.process_split_proposal("D001",session="splitter")
        self.assertFalse(ok)
        self.assertEqual(detail,"split-retryable")
        self.assertFalse((self.ctrl/"IMPLEMENTATION_PLAN.repair.json").exists())
        self.assertIn(
            "prerequisite artifacts are split-recoverable",
            supervisor.load_split_status("D001")["reason"],
        )

    def test_deterministically_inadequate_verify_is_valid_parent_contract_claim(self):
        bad=(
            "python3 -c \"import subprocess,sys; "
            "p=subprocess.run([sys.executable,'-m','unittest','discover','-v'],"
            "capture_output=True,text=True); "
            "assert p.returncode==0,p.stdout+p.stderr; "
            "assert 'Ran' in p.stdout\""
        )
        request={
            "generation":1,
            "parent_contract":{
                "verify_command":bad,
                "done_when":"The unittest suite passes with exit 0.",
            },
        }
        payload={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"verify_command",
            "reason":(
                "unittest human-readable Ran summary is checked in stdout "
                "even though the runner writes it to stderr"
            ),
        }
        field,reason,errors=supervisor._validate_parent_contract_invalid_payload(
            "D001",payload,request
        )
        self.assertEqual(field,"verify_command")
        self.assertEqual(reason,payload["reason"])
        self.assertTrue(errors)
        self.assertIn("human-readable test-runner summary",errors[0])

    def test_valid_verify_command_rejects_model_contract_repair(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        payload={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"verify_command",
            "reason":"the model claims the valid parent command is contradictory",
        }
        with self.assertRaisesRegex(ValueError,"lacks a deterministic contract defect"):
            supervisor.request_parent_contract_repair(
                "D001",payload,json.loads((self.work/"D001.split-request.json").read_text())
            )

    def _false_parent_invalid(self):
        return {
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"verify_command",
            "reason":"the model incorrectly claims this structurally valid Verify command is invalid",
        }

    def _claim_false_parent_invalid(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        self.assertEqual(supervisor.claim_splitter("D001","claim-1"),(True,"claimed"))
        (self.work/"stage-a-controller-executions.json").write_text(json.dumps({
            "executions":{"primary":{"root_session":"technical-root", "action":{
                "agent":"task-splitter","deliverable":"D001"}}}}))
        sent=[]
        old=supervisor.dispatch_splitter_corrective_turn
        supervisor.dispatch_splitter_corrective_turn=lambda session,text: (sent.append((session,text)) or (True,"accepted"))
        self.addCleanup(setattr,supervisor,"dispatch_splitter_corrective_turn",old)
        outcome=supervisor.complete_splitter(
            "D001","splitter-session","claim-1",json.dumps(self._false_parent_invalid()),
        )
        self.assertEqual(outcome,(True,"splitter-corrective-turn-pending"))
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0][0],"technical-root")
        self.assertIn("SPLITTER_CORRECTIVE_ORDINAL: 1",sent[0][1])
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"splitter-corrective-awaiting-output")
        self.assertEqual((status["claim_count"],status.get("proposal_failures",0)),(1,0))
        self.assertEqual(status["corrective_turn_count"],1)
        supervisor.save_split_status("D001","splitter-corrective-awaiting-output",
            claim_count=1,corrective_session="corrective-session",
            corrective_dispatch_token="corrective-token")
        self.assertTrue((self.work/"D001.split-proposal.corrective-rejected-1.json").is_file())
        return sent

    def test_false_parent_invalid_gets_one_corrective_turn_and_valid_reply_accepts_same_claim(self):
        self._claim_false_parent_invalid()
        valid={
            "protocol":supervisor.SPLIT_PROPOSAL_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "proposals":self.proposals(),
        }
        old=supervisor.last_assistant_text_db
        supervisor.last_assistant_text_db=lambda session: json.dumps(valid)
        self.addCleanup(setattr,supervisor,"last_assistant_text_db",old)
        supervisor.reconcile_split_proposals()
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"accepted")
        self.assertEqual(status["claim_count"],1)
        self.assertEqual(status.get("proposal_failures",0),0)
        self.assertEqual(status["corrective_turn_count"],1)
        self.assertEqual(status["children"],["D001-A","D001-B"])

    def test_second_invalid_corrective_reply_fails_claim_without_a_third_turn(self):
        sent=self._claim_false_parent_invalid()
        old=supervisor.last_assistant_text_db
        supervisor.last_assistant_text_db=lambda session: json.dumps(self._false_parent_invalid())
        self.addCleanup(setattr,supervisor,"last_assistant_text_db",old)
        # Same canonical object proves the duplicate-output guard waits; a
        # distinct invalid object then consumes the claim normally.
        supervisor.reconcile_split_proposals()
        self.assertEqual(len(sent),1)
        second=dict(self._false_parent_invalid())
        second["reason"]="the second response repeats an unsupported claim despite the deterministic result"
        supervisor.last_assistant_text_db=lambda session: json.dumps(second)
        supervisor.reconcile_split_proposals()
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"split-retryable")
        self.assertEqual((status["claim_count"],status["proposal_failures"]),(1,1))
        self.assertEqual(status["corrective_turn_count"],1)
        self.assertEqual(len(sent),1)

    def test_deterministically_invalid_parent_uses_existing_repair_without_corrective_turn(self):
        self.parent["verify_command"]=""
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol":"V2.6.9","project":str(self.project),
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":self.parent},
        }))
        supervisor.record_leaf_failure("D001","second","genuine")
        self.assertEqual(supervisor.claim_splitter("D001","claim-1"),(True,"claimed"))
        sent=[]
        old_dispatch=supervisor.dispatch_splitter_corrective_turn
        old_repair=supervisor._request_parent_contract_repair
        supervisor.dispatch_splitter_corrective_turn=lambda *args: (sent.append(args) or (True,"accepted"))
        supervisor._request_parent_contract_repair=lambda *args: (True,"parent-contract-repair")
        self.addCleanup(setattr,supervisor,"dispatch_splitter_corrective_turn",old_dispatch)
        self.addCleanup(setattr,supervisor,"_request_parent_contract_repair",old_repair)
        self.assertEqual(supervisor.complete_splitter(
            "D001","splitter-session","claim-1",json.dumps(self._false_parent_invalid()),
        ),(True,"parent-contract-repair"))
        self.assertEqual(sent,[])

    def test_missing_owned_artifacts_with_valid_verify_gets_one_corrective_split_turn(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        self.assertEqual(supervisor.claim_splitter("D001","claim-1"),(True,"claimed"))
        (self.work/"stage-a-controller-executions.json").write_text(json.dumps({
            "executions":{"primary":{"root_session":"technical-root", "action":{
                "agent":"task-splitter","deliverable":"D001"}}}}))
        payload={
            **self._false_parent_invalid(),"field":"prerequisite_artifacts",
            "reason":"a parent-owned artifact is absent after a valid Verify failure",
        }
        sent=[]
        old=supervisor.dispatch_splitter_corrective_turn
        supervisor.dispatch_splitter_corrective_turn=lambda *args: (sent.append(args) or (True,"accepted"))
        self.addCleanup(setattr,supervisor,"dispatch_splitter_corrective_turn",old)
        self.assertEqual(supervisor.complete_splitter(
            "D001","splitter-session","claim-1",json.dumps(payload),
        ),(True,"splitter-corrective-turn-pending"))
        self.assertEqual(len(sent),1)
        self.assertFalse((self.ctrl/"IMPLEMENTATION_PLAN.repair.json").exists())

    def test_missing_bare_json_gets_one_corrective_turn_then_valid_response_accepts(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        self.assertEqual(supervisor.claim_splitter("D001","claim-1"),(True,"claimed"))
        (self.work/"stage-a-controller-executions.json").write_text(json.dumps({
            "executions":{"primary":{"root_session":"technical-root", "action":{
                "agent":"task-splitter","deliverable":"D001"}}}}))
        sent=[]
        old_dispatch=supervisor.dispatch_splitter_corrective_turn
        old_last=supervisor.last_assistant_text_db
        supervisor.dispatch_splitter_corrective_turn=lambda *args: (sent.append(args) or (True,"accepted"))
        supervisor.last_assistant_text_db=lambda session: ""
        self.addCleanup(setattr,supervisor,"dispatch_splitter_corrective_turn",old_dispatch)
        self.addCleanup(setattr,supervisor,"last_assistant_text_db",old_last)
        self.assertEqual(supervisor.complete_splitter(
            "D001","splitter-session","claim-1","Maximum steps reached",
        ),(False,"completion-pending"))
        self.assertEqual(sent,[])
        supervisor.last_assistant_text_db=lambda session: "Maximum steps reached"
        supervisor.reconcile_split_proposals()
        self.assertEqual(len(sent),1)
        self.assertIn("required bare JSON",sent[0][1])
        supervisor.save_split_status("D001","splitter-corrective-awaiting-output",
            claim_count=1,corrective_session="corrective-session",
            corrective_dispatch_token="corrective-token")
        supervisor.last_assistant_text_db=lambda session: json.dumps({
            "protocol":supervisor.SPLIT_PROPOSAL_PROTOCOL,"parent_id":"D001",
            "depth":0,"generation":1,"proposals":self.proposals(),
        })
        supervisor.reconcile_split_proposals()
        status=supervisor.load_split_status("D001")
        self.assertEqual((status["state"],status["claim_count"]),("accepted",1))

    def test_mixed_invalid_types_do_not_get_a_second_corrective_turn(self):
        self._claim_false_parent_invalid()
        sent=[]
        old_dispatch=supervisor.dispatch_splitter_corrective_turn
        old_last=supervisor.last_assistant_text_db
        supervisor.dispatch_splitter_corrective_turn=lambda *args: (sent.append(args) or (True,"accepted"))
        supervisor.last_assistant_text_db=lambda session: "not bare JSON"
        self.addCleanup(setattr,supervisor,"dispatch_splitter_corrective_turn",old_dispatch)
        self.addCleanup(setattr,supervisor,"last_assistant_text_db",old_last)
        supervisor.reconcile_split_proposals()
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["proposal_failures"],1)
        self.assertEqual(status["corrective_turn_count"],1)
        self.assertEqual(sent,[])

    def test_read_only_parent_reaches_finite_terminal_state(self):
        manifest=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        leaf=manifest["leaves"]["D001"]
        leaf["role"]="tester"
        leaf["owned_artifacts"]="none"
        leaf["owned_artifact_paths"]=[]
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        ok,detail=supervisor.record_leaf_failure("D001","second","genuine")
        self.assertFalse(ok)
        self.assertEqual(detail,"split-unavailable-read-only-parent")
        status=json.loads((self.work/"D001.split-status.json").read_text())
        self.assertEqual(status["state"],"split-unavailable-read-only-parent")
        snap=control_state.snapshot(self.project)
        reasons=[item["reason"] for item in snap["execution_blockers"]]
        self.assertIn("split-unavailable-read-only-parent",reasons)
        self.assertEqual(snap["resume_phase"],"execution-blocked")

    def test_stale_splitter_lease_is_recoverable_once_then_bounded(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        supervisor.save_split_status(
            "D001","splitter-active",claim_count=1,
            lease_until_epoch=1,dispatch_token="old"
        )
        ok,detail=supervisor.claim_splitter("D001","new")
        self.assertTrue(ok); self.assertEqual(detail,"claimed")
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["claim_count"],2)
        supervisor.save_split_status(
            "D001","splitter-active",claim_count=2,
            lease_until_epoch=1,dispatch_token="new"
        )
        ok,detail=supervisor.claim_splitter("D001","third")
        self.assertFalse(ok); self.assertEqual(detail,"splitter-failed")

    def test_profile_recovery_requires_a_changed_profile_and_is_bounded(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        supervisor.save_split_status(
            "D001","splitter-failed",claim_count=3,proposal_failures=3,
            reason="splitter-completed-without-json-proposal",lease_until_epoch=0,
        )
        with mock.patch.object(supervisor,"task_splitter_model_ref",return_value="syv/new"), \
             mock.patch.object(
                 supervisor,"task_splitter_profile_fingerprint",
                 side_effect=lambda model: {"syv/new":"new-fingerprint","syv/old":"old-fingerprint"}[model],
             ):
            ok,detail=supervisor.recover_splitter_profile_change("D001","syv/old")
        self.assertTrue(ok); self.assertEqual(detail,"recovered")
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"split-retryable")
        self.assertEqual(status["recovery_claim_budget"],1)
        self.assertEqual(status["profile_recovery_fingerprints"],["new-fingerprint"])
        self.assertEqual(status["recovery_history"][-1]["prior_model"],"syv/old")
        supervisor.save_split_status(
            "D001","splitter-failed",reason="splitter-completed-without-json-proposal"
        )
        with mock.patch.object(supervisor,"task_splitter_model_ref",return_value="syv/new"), \
             mock.patch.object(
                 supervisor,"task_splitter_profile_fingerprint",
                 side_effect=lambda model: {"syv/new":"new-fingerprint","syv/old":"old-fingerprint"}[model],
             ):
            ok,detail=supervisor.recover_splitter_profile_change("D001","syv/old")
        self.assertFalse(ok); self.assertEqual(detail,"profile-recovery-already-used")

    def test_profile_recovery_denies_an_unchanged_profile(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        supervisor.save_split_status(
            "D001","splitter-failed",claim_count=3,proposal_failures=3,
            reason="splitter-completed-without-json-proposal",lease_until_epoch=0,
        )
        with mock.patch.object(supervisor,"task_splitter_model_ref",return_value="syv/same"), \
             mock.patch.object(supervisor,"task_splitter_profile_fingerprint",return_value="same-fingerprint"):
            ok,detail=supervisor.recover_splitter_profile_change("D001","syv/same")
        self.assertFalse(ok); self.assertEqual(detail,"profile-unchanged")

    def test_execution_contract_recovery_is_adjacent_once_and_preserves_counts(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        supervisor.save_split_status(
            "D001","splitter-failed",claim_count=4,proposal_failures=4,
            recovery_claim_budget=2,reason="splitter-completed-without-json-proposal",
            lease_until_epoch=0,
        )
        with mock.patch.object(supervisor,"task_splitter_steps",return_value=4), \
             mock.patch.object(
                 supervisor,"task_splitter_execution_contract_fingerprint",
                 side_effect=lambda steps=None: "step4-fingerprint" if steps is None else "step3-fingerprint",
             ):
            ok,detail=supervisor.recover_splitter_execution_contract("D001",3)
        self.assertTrue(ok); self.assertEqual(detail,"recovered")
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["claim_count"],4)
        self.assertEqual(status["proposal_failures"],4)
        self.assertEqual(status["recovery_claim_budget"],3)
        self.assertEqual(status["execution_contract_recovery_fingerprints"],["step4-fingerprint"])
        self.assertEqual(status["recovery_history"][-1]["prior_steps"],3)
        supervisor.save_split_status(
            "D001","splitter-failed",reason="splitter-completed-without-json-proposal"
        )
        with mock.patch.object(supervisor,"task_splitter_steps",return_value=4), \
             mock.patch.object(
                 supervisor,"task_splitter_execution_contract_fingerprint",
                 side_effect=lambda steps=None: "step4-fingerprint" if steps is None else "step3-fingerprint",
             ):
            ok,detail=supervisor.recover_splitter_execution_contract("D001",3)
        self.assertFalse(ok); self.assertEqual(detail,"execution-contract-recovery-already-used")

    def test_execution_contract_recovery_rejects_nonadjacent_step_claims(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        with mock.patch.object(supervisor,"task_splitter_steps",return_value=4):
            ok,detail=supervisor.recover_splitter_execution_contract("D001",2)
        self.assertFalse(ok); self.assertEqual(detail,"prior-step-contract-not-adjacent")

    def test_direct_context_recovery_is_exact_once_and_preserves_counts(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        supervisor.save_split_status(
            "D001","split-validation-failed",claim_count=5,proposal_failures=5,
            recovery_claim_budget=3,
            reason="creates_or_updates path is outside child ownership: fixtures/vectors/moons",
            lease_until_epoch=0,
        )
        with mock.patch.object(
            supervisor,"task_splitter_direct_context_fingerprint",return_value="direct-context-fingerprint"
        ):
            ok,detail=supervisor.recover_splitter_direct_context_contract("D001")
        self.assertTrue(ok); self.assertEqual(detail,"recovered")
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"split-retryable")
        self.assertEqual(status["claim_count"],5)
        self.assertEqual(status["proposal_failures"],5)
        self.assertEqual(status["recovery_claim_budget"],4)
        self.assertEqual(status["direct_context_recovery_fingerprints"],["direct-context-fingerprint"])
        self.assertEqual(status["recovery_history"][-1]["reason"],"task-splitter-direct-context-contract-recovery")
        supervisor.save_split_status(
            "D001","split-validation-failed",
            reason="creates_or_updates path is outside child ownership: fixtures/vectors/moons",
        )
        with mock.patch.object(
            supervisor,"task_splitter_direct_context_fingerprint",return_value="direct-context-fingerprint"
        ):
            ok,detail=supervisor.recover_splitter_direct_context_contract("D001")
        self.assertFalse(ok); self.assertEqual(detail,"direct-context-recovery-already-used")

    def test_progress_handoff_writer_accepts_canonical_directory_root_target(self):
        manifest=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        leaf=manifest["leaves"]["D001"]
        leaf["owned_artifacts"]="`.opencode-v2/probes/moons.json`, `fixtures/vectors/moons/`"
        leaf["owned_artifact_paths"]=[".opencode-v2/probes/moons.json","fixtures/vectors/moons/"]
        leaf["verify_command"]="test -s .opencode-v2/probes/moons.json -a -d fixtures/vectors/moons"
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        (self.ctrl/"probes").mkdir()
        (self.ctrl/"probes"/"moons.json").write_text("{}")
        (self.project/"fixtures/vectors/moons").mkdir(parents=True)
        supervisor.record_leaf_failure("D001","second","genuine")
        proposals=[
            {
                "scope":"diagnose the existing moon fixture inputs",
                "owned_artifacts":"none",
                "verify_command":"SUPERVISOR_HANDOFF_PROGRESS",
                "role":"probe-builder","depends_on_sibling":"",
                "done_when":"HANDOFF_READY: true records the missing fixture facts",
                "reads_existing":[".opencode-v2/probes/moons.json","fixtures/vectors/moons/"],
                "creates_or_updates":[],
            },
            {
                "scope":"write the validated moon fixture outputs",
                "owned_artifacts":"`.opencode-v2/probes/moons.json`, `fixtures/vectors/moons/`",
                "verify_command":leaf["verify_command"],
                "role":"implementer","depends_on_sibling":"first",
                "done_when":"the fixture directory contains the required records",
                "reads_existing":[".opencode-v2/probes/moons.json","fixtures/vectors/moons/"],
                "creates_or_updates":[".opencode-v2/probes/moons.json","fixtures/vectors/moons/"],
            },
        ]
        expected,children=supervisor.validate_split_proposal("D001",proposals)
        self.assertEqual(expected,["D001-A","D001-B"])
        self.assertEqual(children[1]["owned_artifact_paths"],[".opencode-v2/probes/moons.json","fixtures/vectors/moons/"])
        self.assertEqual(children[1]["split_creates_or_updates"],[".opencode-v2/probes/moons.json","fixtures/vectors/moons"])

    def test_malformed_proposal_gets_one_bounded_fresh_retry(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        ok,_=supervisor.claim_splitter("D001","claim1")
        self.assertTrue(ok)
        (self.work/"D001.split-proposal.json").write_text("{broken")
        ok,detail=supervisor.process_split_proposal("D001",session="s1",require_proposal=True)
        self.assertFalse(ok); self.assertEqual(detail,"split-retryable")
        self.assertFalse((self.work/"D001.split-proposal.json").exists())
        ok,_=supervisor.claim_splitter("D001","claim2")
        self.assertTrue(ok)
        (self.work/"D001.split-proposal.json").write_text("{broken again")
        ok,detail=supervisor.process_split_proposal("D001",session="s2",require_proposal=True)
        self.assertFalse(ok); self.assertEqual(detail,"split-validation-failed")

    def test_expired_pending_splitter_completion_recovers_without_dispatch(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        supervisor.save_split_status(
            "D001","splitter-active",claim_count=1,dispatch_token="split-1",
            lease_until_epoch=0,completion_pending_session="no-json-session",
            completion_pending_token="split-1",completion_pending_deadline_epoch=0,
        )
        before=json.loads((self.work/"attempts.json").read_text())
        result=supervisor.reconcile_splits_once()
        after=json.loads((self.work/"attempts.json").read_text())
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"split-retryable")
        self.assertEqual(status["reason"],"splitter-completed-without-json-proposal")
        self.assertEqual(status["claim_count"],1)
        self.assertEqual(status["proposal_failures"],1)
        self.assertFalse((self.work/"D001.split-proposal.json").exists())
        self.assertEqual(before,after)
        self.assertEqual(result["splits"]["D001"]["state"],"split-retryable")

    def test_split_transaction_is_idempotent_and_history_not_duplicated(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        first=supervisor.persist_split("D001",self.proposals())
        second=supervisor.persist_split("D001",self.proposals())
        self.assertEqual(first,["D001-A","D001-B"])
        self.assertEqual(second,first)
        history=json.loads((self.work/"splits.json").read_text())
        self.assertEqual(len(history["splits"]),1)
        txn=json.loads((self.work/"D001.split-transaction.json").read_text())
        self.assertEqual(txn["state"],"committed")

    def test_prepared_split_transaction_replays_after_partial_crash(self):
        supervisor.record_leaf_failure("D001","second","genuine")
        expected,children=supervisor.validate_split_proposal("D001",self.proposals())
        request=json.loads((self.work/"D001.split-request.json").read_text())
        child_defs={child["id"]:child for child in children}
        txn={
            "owner":"supervisor",
            "protocol":supervisor.SPLIT_TRANSACTION_PROTOCOL,
            "state":"prepared","parent_id":"D001",
            "generation":request["generation"],
            "children":expected,"child_defs":child_defs,
            "transaction_id":supervisor.split_transaction_id("D001",request["generation"],child_defs),
            "prepared_at":"2026-09-13T00:00:00Z",
        }
        supervisor.atomic_write_json(self.work/"D001.split-transaction.json",txn)
        # Simulate a crash after only the main manifest mutation.
        manifest=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        manifest["leaves"]["D001"]["split_children"]=expected
        for child in children:
            manifest["leaves"][child["id"]]=child
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        supervisor.reconcile_split_transactions()
        txn2=json.loads((self.work/"D001.split-transaction.json").read_text())
        self.assertEqual(txn2["state"],"committed")
        overlay=json.loads((self.work/"split-leaves.json").read_text())
        self.assertEqual(overlay["parents"]["D001"]["children"],expected)

    def test_parent_finalize_failure_becomes_finite_blocker(self):
        manifest=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        manifest["leaves"]["D001"]["verify_deps"]=[]
        manifest["leaves"]["D001"]["split_children"]=["D001-A","D001-B"]
        manifest["leaves"]["D001-A"]=dict(self.parent,id="D001-A",parent="D001",split_children=[])
        manifest["leaves"]["D001-B"]=dict(self.parent,id="D001-B",parent="D001",split_children=[])
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        supervisor.save_split_status("D001","accepted",children=["D001-A","D001-B"])
        old_ready=supervisor.ready_info
        old_finalize=supervisor.post_session_finalize
        supervisor.ready_info=lambda did: {"status":"complete"} if did in {"D001-A","D001-B"} else {}
        supervisor.post_session_finalize=lambda did: (False,"verify-failed-1")
        try:
            for _ in range(supervisor.MAX_SPLIT_PARENT_FINALIZE_FAILURES):
                supervisor._split_parent_finalize_next["D001"]=0
                supervisor.reconcile_split_parent_completions()
        finally:
            supervisor.ready_info=old_ready
            supervisor.post_session_finalize=old_finalize
        status=supervisor.load_split_status("D001")
        self.assertEqual(status["state"],"parent-finalize-failed")
        self.assertEqual(status["parent_finalize_failures"],supervisor.MAX_SPLIT_PARENT_FINALIZE_FAILURES)

    def test_resume_phase_honors_explicit_execution_blocker(self):
        state={
            "acceptance":{"complete":True},
            "plan":{"complete":True,"blocked":False},
            "leaves":{"D001":{"complete":False,"split_required":False,"attempt_limit_reached":False}},
            "execution_blockers":[{"deliverable":"D001","reason":"parent-finalize-failed"}],
            "tests":{"complete":False},
            "acceptance_validation":{"complete":False},
        }
        self.assertEqual(control_state.resume_phase(state),"execution-blocked")

    def test_resume_phase_runs_eligible_prerequisite_before_downstream_blocker(self):
        state={
            "acceptance":{"complete":True},
            "plan":{"complete":True,"blocked":False},
            "leaves":{
                "D001":{"complete":False,"split_required":False,"eligible":True},
                "D002":{"complete":False,"split_required":False,"eligible":False},
            },
            "execution_blockers":[
                {"deliverable":"D002","reason":"attempt_limit_reached"}
            ],
            "tests":{"complete":False},
            "acceptance_validation":{"complete":False},
        }
        self.assertEqual(control_state.resume_phase(state),"execution")



class WorkerSandboxTrustBoundaryTests(unittest.TestCase):
    def setUp(self):
        worker_sandbox.cleanup_session("s1")
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        leaf={
            "id":"D001","name":"owned file",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":"test -s owned.txt",
            "role":"implementer","done_when":"owned exists",
            "acceptance_ids":["A001"],"parallel":"none","split_children":[],
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":leaf},
        }))
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{"D001":{"count":1,"sessions":["s1"],"automatic_limit":2}},
        }))
    def tearDown(self):
        supervisor.PROJECT=self.old
        worker_sandbox.cleanup_session("s1")
        self.tmp.cleanup()

    def test_exact_packet_verify_routes_to_trusted_verify_sandbox(self):
        decision=worker_sandbox.hook_guard(
            self.project,"s1","","implementer","bash",
            {"command":"test -s owned.txt"},
        )
        self.assertTrue(decision.get("trusted_verify"))
        self.assertIn("run-worker-verify",decision["command"])
        self.assertNotIn("'run-bash'",decision["command"])

    def test_near_match_verify_stays_under_normal_worker_sandbox(self):
        decision=worker_sandbox.hook_guard(
            self.project,"s1","","implementer","bash",
            {"command":"test -s owned.txt && true"},
        )
        self.assertFalse(decision.get("trusted_verify",False))
        self.assertIn("'run-bash'",decision["command"])
        self.assertNotIn("run-worker-verify",decision["command"])

    def test_reflected_trusted_verify_wrapper_normalizes_back_to_exact_verify(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        wrapped=worker_sandbox.replacement_worker_verify_command(
            self.project,ctx,"test -s owned.txt"
        )
        decision=worker_sandbox.hook_guard(
            self.project,"s1","","implementer","bash",{"command":wrapped}
        )
        self.assertTrue(decision.get("trusted_verify"))
        self.assertIn("run-worker-verify",decision["command"])
        self.assertEqual(
            worker_sandbox.normalize_worker_bash_command(
                self.project,ctx,decision["command"]
            ),
            "test -s owned.txt",
        )

    def test_tampered_trusted_verify_wrapper_is_denied_as_manual_wrapper(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        wrapped=worker_sandbox.replacement_worker_verify_command(
            self.project,ctx,"test -s owned.txt"
        )
        with self.assertRaises(worker_sandbox.SandboxError) as raised:
            worker_sandbox.hook_guard(
                self.project,"s1","","implementer","bash",
                {"command":wrapped+" ; true"},
            )
        self.assertIn("manual sandbox wrapper forbidden",str(raised.exception))

    def test_verify_shadow_snapshots_test_checks_as_regular_file_for_required_files(self):
        checks=self.ctrl/"TEST_CHECKS.json"
        checks.write_text(json.dumps({
            "checks":[{"name":"ok","command":"test -s .opencode-v2/TEST_CHECKS.json"}],
            "required_files":[".opencode-v2/TEST_CHECKS.json"],
        }))
        shadow=self.project/"verify-shadow"
        lower_root="/v2-lower-test"
        worker_sandbox._prepare_verify_shadow(self.project,shadow,lower_root)
        copied=shadow/".opencode-v2/TEST_CHECKS.json"
        self.assertTrue(copied.is_file())
        self.assertFalse(copied.is_symlink())
        self.assertEqual(json.loads(copied.read_text())["required_files"],[
            ".opencode-v2/TEST_CHECKS.json"
        ])

    def test_trusted_worker_verify_bootstraps_runtime_without_prior_worker_bash(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        passed=subprocess.CompletedProcess(args=["bash"],returncode=0)
        with (
            mock.patch.object(worker_sandbox,"session_runtime_state",return_value={}),
            mock.patch.object(worker_sandbox,"_mark_runtime_state") as mark_runtime,
            mock.patch.object(worker_sandbox,"_session_tmpdir") as make_tmp,
            mock.patch.object(
                worker_sandbox,"run_verify_bash",return_value=(passed,[])
            ) as run_verify,
        ):
            rc=worker_sandbox.run_worker_exact_verify(
                self.project,ctx,"test -s owned.txt"
            )
        self.assertEqual(rc,0)
        mark_runtime.assert_called_once_with(self.project,ctx)
        make_tmp.assert_called_once_with("s1")
        run_verify.assert_called_once()

    def test_trusted_worker_verify_preserves_exit_code_and_stages_only_canonical_report(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        failed=subprocess.CompletedProcess(
            args=["bash"],returncode=7,
            stdout="verify-out\n",stderr="verify-err\n",
        )
        with mock.patch.object(
            worker_sandbox,"run_verify_bash",return_value=(failed,[])
        ) as run_verify, mock.patch("builtins.print") as emit:
            rc=worker_sandbox.run_worker_exact_verify(
                self.project,ctx,"test -s owned.txt"
            )
        self.assertEqual(rc,7)
        self.assertFalse(run_verify.call_args.kwargs["stage_test_report"])
        emit.assert_any_call("verify-out\n",end="")
        emit.assert_any_call(
            "verify-err\n",end="",file=worker_sandbox.sys.stderr
        )

        manifest=json.loads((self.ctrl/"IMPLEMENTATION_PLAN.guard.json").read_text())
        manifest["leaves"]["D001"]["verify_command"]=".opencode-v2/bin/run-checks"
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        passed=subprocess.CompletedProcess(args=["bash"],returncode=0)
        with mock.patch.object(
            worker_sandbox,"run_verify_bash",return_value=(passed,[])
        ) as run_verify:
            rc=worker_sandbox.run_worker_exact_verify(
                self.project,ctx,".opencode-v2/bin/run-checks"
            )
        self.assertEqual(rc,0)
        self.assertTrue(run_verify.call_args.kwargs["stage_test_report"])

    def test_trusted_worker_verify_rejects_nonexact_command(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        with self.assertRaises(worker_sandbox.SandboxError):
            worker_sandbox.run_worker_exact_verify(
                self.project,ctx,"test -s owned.txt && true"
            )

    def test_trusted_verify_sandbox_failure_recovery_uses_bounded_infrastructure_grant(self):
        attempts=json.loads((self.work/"attempts.json").read_text())
        attempts["deliverables"]["D001"]["failure_history"]=[{
            "attempt":1,
            "classification":"genuine",
            "reason":"sandbox-ownership-violation",
            "session":"s1",
            "source":"supervisor",
            "timestamp":"2026-09-28T00:00:00Z",
        }]
        attempts["deliverables"]["D001"]["split_required"]={
            "generation":1,"reason":"genuine-failure-threshold",
        }
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        audit=worker_sandbox.violation_path(self.project,"D001","s1")
        audit.parent.mkdir(parents=True,exist_ok=True)
        audit.write_text(json.dumps({
            "kind":"sandbox-outside-ownership",
            "deliverable":"D001",
            "session":"s1",
            "detail":{
                "command":"test -s owned.txt",
                "exit_code":1,
                "paths":[
                    ".opencode-v2/TEST_REPORT.json",
                    ".opencode-v2/test-logs",
                ],
            },
        })+"\n")
        with mock.patch.object(supervisor,"v1_session_status_snapshot",return_value={}):
            ok,detail=supervisor.recover_trusted_verify_sandbox_failure("D001")
        self.assertTrue(ok,detail)
        after=json.loads((self.work/"attempts.json").read_text())
        entry=after["deliverables"]["D001"]
        row=entry["failure_history"][0]
        self.assertEqual(row["classification"],"infrastructure")
        self.assertEqual(row["original_reason"],"sandbox-ownership-violation")
        self.assertEqual(entry["infrastructure_retry_grants"],1)
        self.assertNotIn("split_required",entry)
        self.assertEqual(
            entry["trusted_verify_sandbox_recoveries"][0]["protocol"],
            supervisor.TRUSTED_VERIFY_SANDBOX_RECOVERY_PROTOCOL,
        )

    def test_finalize_rejects_actual_sandbox_escape(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        worker_sandbox.record_violation(
            self.project,ctx,"sandbox-outside-ownership",
            {"paths":["other.txt"],"command":"touch other.txt","exit_code":0},
        )
        ok,detail=supervisor.post_session_finalize("D001","s1")
        self.assertFalse(ok)
        self.assertEqual(detail,"sandbox-ownership-violation")

    def test_denied_direct_write_is_audited_but_does_not_poison_finalize(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        supervisor.write_ownership_baseline("D001")
        (self.project/"owned.txt").write_text("valid\n")
        worker_sandbox.record_violation(
            self.project,ctx,"direct-tool-outside-ownership",
            {"tool":"write","path":"helper.py","owned":["owned.txt"]},
        )
        self.assertFalse(
            worker_sandbox.has_fatal_violation(self.project,"D001","s1")
        )
        ok,detail=supervisor.post_session_finalize("D001","s1")
        self.assertTrue(ok,detail)
        self.assertEqual(detail,"finalized")

    def test_denied_only_terminal_attempt_can_recover_same_attempt(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        supervisor.write_ownership_baseline("D001")
        supervisor.write_execution_baseline("D001",1)
        (self.project/"owned.txt").write_text("valid\n")
        worker_sandbox.record_violation(
            self.project,ctx,"direct-tool-outside-ownership",
            {"tool":"write","path":"helper.py","owned":["owned.txt"]},
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        attempts["deliverables"]["D001"]["failure_history"]=[{
            "attempt":1,
            "classification":"genuine",
            "reason":"sandbox-ownership-violation",
            "timestamp":"2026-09-25T00:00:00Z",
            "source":"supervisor",
        }]
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        ok,detail=supervisor.recover_denied_tool_finalize("D001")
        self.assertTrue(ok,detail)
        self.assertEqual(detail,"finalized")
        self.assertTrue((self.work/"D001.ready").exists())
        after=json.loads((self.work/"attempts.json").read_text())
        entry=after["deliverables"]["D001"]
        self.assertEqual(
            entry["failure_history"][0]["reason"],
            "sandbox-ownership-violation",
        )
        marker=entry["denied_tool_finalize_recovery"]
        self.assertEqual(marker["state"],"finalized")
        self.assertEqual(marker["attempt"],1)
        self.assertEqual(marker["session"],"s1")

    def test_denied_tool_recovery_refuses_actual_sandbox_escape(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        supervisor.write_execution_baseline("D001",1)
        (self.project/"owned.txt").write_text("valid\n")
        worker_sandbox.record_violation(
            self.project,ctx,"sandbox-outside-ownership",
            {"paths":["other.txt"],"command":"touch other.txt","exit_code":0},
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        attempts["deliverables"]["D001"]["failure_history"]=[{
            "attempt":1,
            "classification":"genuine",
            "reason":"sandbox-ownership-violation",
            "timestamp":"2026-09-25T00:00:00Z",
            "source":"supervisor",
        }]
        (self.work/"attempts.json").write_text(json.dumps(attempts))
        self.assertEqual(
            supervisor.recover_denied_tool_finalize("D001"),
            (False,"denied-tool-recovery-audit-not-denied-only"),
        )
        self.assertFalse((self.work/"D001.ready").exists())


class AttemptScopedExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        self.work=self.ctrl/"work"
        self.work.mkdir(parents=True)
        self.old=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        leaf={
            "id":"D001","name":"owned",
            "owned_artifacts":"`owned.txt`",
            "owned_artifact_paths":["owned.txt"],
            "launch_deps":[],"contract_deps":[],"verify_deps":[],
            "verify_command":"test -s owned.txt","role":"implementer",
            "done_when":"owned","acceptance_ids":["A001"],"parallel":"none",
            "split_children":[],
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":{"D001":leaf},
        }))
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{
                "D001":{"count":2,"sessions":["old","current"],"automatic_limit":3,
                        "failure_history":[{"attempt":1,"classification":"genuine"}]}
            }
        }))
    def tearDown(self):
        supervisor.PROJECT=self.old
        self.tmp.cleanup()

    def test_stale_artifact_does_not_count_as_current_attempt_execution(self):
        (self.project/"owned.txt").write_text("from attempt one\n")
        supervisor.write_execution_baseline("D001",2)
        self.assertFalse(supervisor.durable_worker_execution("D001","current"))
        (self.project/"owned.txt").write_text("changed by attempt two\n")
        self.assertTrue(supervisor.durable_worker_execution("D001","current"))

    def test_progress_change_counts_for_current_attempt(self):
        (self.project/"owned.txt").write_text("stale\n")
        supervisor.write_execution_baseline("D001",2)
        (self.work/"D001.progress.md").write_text("new progress\n")
        self.assertTrue(supervisor.durable_worker_execution("D001","current"))


class CanonicalUnmaterializedDispatchProjectionTests(unittest.TestCase):
    def test_exhausted_but_unmaterialized_dispatch_is_canonically_reusable(self):
        entry={
            "count":3,"sessions":["s1","s2","dispatch:t3"],"automatic_limit":3,
            "failure_history":[
                {"attempt":1,"classification":"genuine"},
                {"attempt":2,"classification":"genuine"},
            ],
        }
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        entry["unmaterialized_dispatch_sequence"]=3
        entry["unmaterialized_dispatch_replays"]=control_state.MAX_UNMATERIALIZED_DISPATCH_REPLAYS
        state=control_state.attempt_state(entry)
        self.assertFalse(state["unmaterialized_dispatch_reusable"])

    def test_snapshot_uses_same_reusable_projection_without_supervisor_override(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td); work=project/".opencode-v2/work"; work.mkdir(parents=True)
            leaf={
                "id":"D001","name":"leaf","owned_artifacts":"`a.txt`",
                "owned_artifact_paths":["a.txt"],"launch_deps":[],
                "contract_deps":[],"verify_deps":[],"verify_command":"test -f a.txt",
                "role":"implementer","done_when":"a","acceptance_ids":["A001"],
                "parallel":"none","split_children":[],
            }
            (project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "protocol":"V2.6.9",
                "project":str(project),
                "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
                "leaves":{"D001":leaf},
            }))
            mark_phase_ready(project,"IMPLEMENTATION_PLAN.md","IMPLEMENTATION_PLAN_COMPLETE")
            (work/"attempts.json").write_text(json.dumps({
                "owner":"supervisor","deliverables":{
                    "D001":{
                        "count":3,"sessions":["s1","s2","dispatch:t3"],
                        "automatic_limit":3,"failure_history":[
                            {"attempt":1,"classification":"genuine"},
                            {"attempt":2,"classification":"genuine"},
                        ],
                    }
                }
            }))
            state=control_state.snapshot(project)["leaves"]["D001"]
            self.assertTrue(state["unmaterialized_dispatch_reusable"])
            self.assertFalse(state["attempt_limit_reached"])
            self.assertTrue(state["eligible"])


def _scheduler_preclaim_child(project,did,token,queue):
    import supervisor as child_supervisor
    child_supervisor.PROJECT=project
    child_supervisor.validate_dispatch=lambda _agent,text: (text,"")
    child_supervisor.active_implementation_sessions=lambda strict=False: []
    child_supervisor.write_ownership_baseline=lambda _did: None
    child_supervisor.ensure_execution_baseline=lambda _did,_attempt: None
    child_supervisor.recursive_split_enabled=lambda: False
    result=child_supervisor.preclaim_attempt("implementer",did,token)
    queue.put(result)


class AtomicSchedulerReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        (self.project/".opencode-v2/work").mkdir(parents=True)
        self.old=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
    def tearDown(self):
        supervisor.PROJECT=self.old
        self.tmp.cleanup()

    def test_reserved_placeholders_consume_slots(self):
        data={"owner":"supervisor","deliverables":{}}
        for n in range(3):
            did=f"D{n+1:03d}"
            data["deliverables"][did]={
                "count":1,"sessions":[f"dispatch:t{n}"],"automatic_limit":3,
            }
        (self.project/".opencode-v2/work/attempts.json").write_text(json.dumps(data))
        self.assertEqual(supervisor.reserved_dispatch_slot_count(data),3)

    def test_four_concurrent_preclaims_never_reserve_more_than_three_slots(self):
        ctx=multiprocessing.get_context("spawn")
        queue=ctx.Queue()
        procs=[]
        for n in range(4):
            did=f"D{n+1:03d}"
            p=ctx.Process(
                target=_scheduler_preclaim_child,
                args=(str(self.project),did,f"t{n}",queue),
            )
            p.start(); procs.append(p)
        for p in procs:
            p.join(10)
            self.assertEqual(p.exitcode,0)
        results=[queue.get(timeout=2) for _ in range(4)]
        claimed=[r for r in results if r[0]=="claimed"]
        denied=[r for r in results if r[0]=="denied"]
        self.assertEqual(len(claimed),3,results)
        self.assertEqual(len(denied),1,results)
        self.assertEqual(denied[0][2],"worker_slots_full")
        ledger=json.loads((self.project/".opencode-v2/work/attempts.json").read_text())
        self.assertEqual(supervisor.reserved_dispatch_slot_count(ledger),3)

    def test_native_unclassified_attempt_blocks_second_preclaim_when_status_lags(self):
        data={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":1,
                    "sessions":["session-one"],
                    "automatic_limit":2,
                }
            },
        }
        path=self.project/".opencode-v2/work/attempts.json"
        path.write_text(json.dumps(data))
        old_validate=supervisor.validate_dispatch
        old_active=supervisor.active_implementation_sessions
        old_own=supervisor.write_ownership_baseline
        old_exec=supervisor.ensure_execution_baseline
        old_split=supervisor.recursive_split_enabled
        try:
            supervisor.validate_dispatch=lambda _agent,text: (text,"")
            supervisor.active_implementation_sessions=lambda strict=False: []
            supervisor.write_ownership_baseline=lambda _did: None
            supervisor.ensure_execution_baseline=lambda _did,_attempt: None
            supervisor.recursive_split_enabled=lambda: False
            self.assertEqual(
                supervisor.unclassified_native_attempt_deliverables(data),
                {"D001"},
            )
            result=supervisor.preclaim_attempt(
                "implementer","D001","second-token"
            )
        finally:
            supervisor.validate_dispatch=old_validate
            supervisor.active_implementation_sessions=old_active
            supervisor.write_ownership_baseline=old_own
            supervisor.ensure_execution_baseline=old_exec
            supervisor.recursive_split_enabled=old_split
        self.assertEqual(
            result,
            ("denied","D001","deliverable_attempt_inflight",1),
        )
        after=json.loads(path.read_text())
        entry=after["deliverables"]["D001"]
        self.assertEqual(entry["count"],1)
        self.assertEqual(entry["sessions"],["session-one"])

    def test_terminal_operator_infrastructure_state_is_not_latent_worker(self):
        for terminal in ("infrastructure_abort","infrastructure_blocked"):
            data={
                "owner":"supervisor",
                "deliverables":{
                    "D001":{
                        "count":1,
                        "sessions":["session-one"],
                        "automatic_limit":2,
                        "operator_retry_attempts":[{
                            "sequence":1,
                            "session":"session-one",
                            "state":terminal,
                            "consumes_operator_grant":False,
                        }],
                    }
                },
            }
            with mock.patch.object(
                supervisor,"attempt_state",return_value={"valid":True}
            ):
                self.assertEqual(
                    supervisor.unclassified_native_attempt_deliverables(data),
                    set(),
                )

        live={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":1,
                    "sessions":["session-one"],
                    "automatic_limit":2,
                    "operator_retry_attempts":[{
                        "sequence":1,
                        "session":"session-one",
                        "state":"consumed",
                        "consumes_operator_grant":True,
                    }],
                }
            },
        }
        with mock.patch.object(
            supervisor,"attempt_state",return_value={"valid":True}
        ):
            self.assertEqual(
                supervisor.unclassified_native_attempt_deliverables(live),
                {"D001"},
            )

    def test_current_automatic_attempt_superseded_by_plan_revision_is_not_latent(self):
        data={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":2,
                    "sessions":["session-one","session-two"],
                    "automatic_limit":2,
                    "failure_history":[
                        {
                            "attempt":1,
                            "classification":"genuine",
                            "reason":"verify-failed-1",
                        }
                    ],
                    "plan_contract_revisions":[{
                        "attempt":2,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":"a"*64,
                        "current_verify_sha256":"b"*64,
                        "previous_result":"verify-failed-1",
                    }],
                }
            },
        }
        entry=data["deliverables"]["D001"]
        self.assertTrue(control_state.attempt_state(entry)["valid"])
        self.assertEqual(
            supervisor.unclassified_native_attempt_deliverables(data),
            set(),
        )

    def test_plan_revision_for_older_attempt_does_not_hide_live_current_attempt(self):
        data={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":2,
                    "sessions":["session-one","session-two"],
                    "automatic_limit":2,
                    "plan_contract_revisions":[{
                        "attempt":1,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":"a"*64,
                        "current_verify_sha256":"b"*64,
                    }],
                }
            },
        }
        with mock.patch.object(
            supervisor,"attempt_state",return_value={"valid":True}
        ):
            self.assertEqual(
                supervisor.unclassified_native_attempt_deliverables(data),
                {"D001"},
            )

    def test_supervisor_replaced_native_attempt_does_not_stay_inflight(self):
        data={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":3,
                    "sessions":["s1","s2","session-three"],
                    "automatic_limit":2,
                    "failure_history":[
                        {"attempt":1,"classification":"genuine","reason":"g1"},
                        {"attempt":2,"classification":"genuine","reason":"g2"},
                    ],
                    "operator_retry_grants":1,
                    "operator_overrides":[{
                        "grant":1,
                        "source":"operator-cli",
                        "timestamp":"2026-09-26T00:00:10Z",
                        "reason":"test replacement continuation",
                    }],
                    "plan_contract_revisions":[{
                        "attempt":3,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":"a"*64,
                        "current_verify_sha256":"b"*64,
                        "timestamp":"2026-09-26T00:00:00Z",
                    }],
                    "operator_retry_attempts":[{
                        "sequence":3,
                        "session":"session-three",
                        "state":"plan_contract_replacement",
                        "outcome":"plan_contract_replacement",
                        "consumes_operator_grant":False,
                        "source":"supervisor",
                        "normalized_by":"supervisor-credit-authority-v1",
                        "normalized_at":"2026-09-26T00:01:00Z",
                        "timestamp":"2026-09-26T00:00:30Z",
                    }],
                }
            },
        }
        path=self.project/".opencode-v2/work/attempts.json"
        path.write_text(json.dumps(data))
        self.assertTrue(control_state.attempt_state(data["deliverables"]["D001"])["valid"])
        self.assertEqual(
            supervisor.unclassified_native_attempt_deliverables(data),
            set(),
        )

        old_validate=supervisor.validate_dispatch
        old_active=supervisor.active_implementation_sessions
        old_own=supervisor.write_ownership_baseline
        old_exec=supervisor.ensure_execution_baseline
        old_split=supervisor.recursive_split_enabled
        try:
            supervisor.validate_dispatch=lambda _agent,text: (text,"")
            supervisor.active_implementation_sessions=lambda strict=False: []
            supervisor.write_ownership_baseline=lambda _did: None
            supervisor.ensure_execution_baseline=lambda _did,_attempt: None
            supervisor.recursive_split_enabled=lambda: False
            result=supervisor.preclaim_attempt(
                "implementer","D001","replacement-next"
            )
        finally:
            supervisor.validate_dispatch=old_validate
            supervisor.active_implementation_sessions=old_active
            supervisor.write_ownership_baseline=old_own
            supervisor.ensure_execution_baseline=old_exec
            supervisor.recursive_split_enabled=old_split
        self.assertEqual(result[:2],("claimed","D001"))
        self.assertEqual(result[3],4)

    def test_v2612_repair_accepts_terminal_replacement_plus_current_reservation(self):
        entry={
            "automatic_limit":2,
            "count":7,
            "sessions":["s1","s2","s3","s4","s5","s6","dispatch:t7"],
            "failure_history":[
                {"attempt":1,"classification":"infrastructure","reason":"i1"},
                {"attempt":2,"classification":"infrastructure","reason":"i2"},
                {"attempt":3,"classification":"infrastructure","reason":"i3"},
                {"attempt":4,"classification":"genuine","reason":"g1"},
                {"attempt":5,"classification":"genuine","reason":"g2"},
            ],
            "infrastructure_retry_grants":3,
            "infrastructure_failures":[
                {
                    "grant":1,"source":"supervisor","kind":"runtime-cancel",
                    "session":"s1","timestamp":"2026-09-26T00:00:01Z",
                    "evidence":"no-owned-artifact-or-progress",
                },
                {
                    "grant":1,"source":"supervisor","kind":"runtime-cancel",
                    "session":"s2","timestamp":"2026-09-26T00:00:02Z",
                    "evidence":"no-owned-artifact-or-progress",
                },
                {
                    "grant":1,"source":"supervisor","kind":"runtime-cancel",
                    "session":"s3","timestamp":"2026-09-26T00:00:03Z",
                    "evidence":"durable-partial-state-preserved",
                },
            ],
            "operator_retry_grants":1,
            "operator_overrides":[{
                "grant":1,"source":"operator-cli",
                "timestamp":"2026-09-26T00:00:10Z",
                "reason":"test operator continuation",
            }],
            "plan_contract_revisions":[{
                "attempt":6,
                "source":"supervisor-plan-contract-revision",
                "previous_verify_sha256":"a"*64,
                "current_verify_sha256":"b"*64,
                "timestamp":"2026-09-26T00:01:00Z",
            }],
            "operator_retry_attempts":[
                {
                    "sequence":6,"session":"s6",
                    "state":"plan_contract_replacement",
                    "outcome":"plan_contract_replacement",
                    "consumes_operator_grant":False,
                    "source":"supervisor",
                    "normalized_by":"supervisor-credit-authority-v1",
                    "normalized_at":"2026-09-26T00:01:10Z",
                    "timestamp":"2026-09-26T00:01:05Z",
                },
                {
                    "sequence":7,"session":"dispatch:t7",
                    "state":"reserved",
                    "consumes_operator_grant":False,
                    "source":"supervisor",
                    "timestamp":"2026-09-26T00:02:00Z",
                },
            ],
        }
        self.assertFalse(
            control_state._attempt_state_v2612_original(entry)["valid"]
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["v2612_infrastructure_repair"])
        self.assertEqual(state["plan_contract_retry_grants"],1)
        self.assertEqual(state["operator_grants_reserved"],1)
        self.assertEqual(state["allowed_attempts"],7)
        self.assertTrue(state["unmaterialized_dispatch_reusable"])
        self.assertTrue(supervisor.retryable_unmaterialized_dispatch_entry(entry))

    def test_v2612_repair_accepts_terminal_operator_abort_plus_current_reservation(self):
        entry={
            "automatic_limit":2,
            "count":8,
            "sessions":[
                "s1","s2","s3","s4","s5","s6","s7","dispatch:t8"
            ],
            "failure_history":[
                {"attempt":1,"classification":"infrastructure","reason":"i1"},
                {"attempt":2,"classification":"infrastructure","reason":"i2"},
                {"attempt":3,"classification":"infrastructure","reason":"i3"},
                {"attempt":4,"classification":"genuine","reason":"g1"},
                {"attempt":5,"classification":"genuine","reason":"g2"},
            ],
            "infrastructure_retry_grants":3,
            "infrastructure_failures":[
                {
                    "grant":1,"source":"supervisor","kind":"runtime-cancel",
                    "session":"s1","timestamp":"2026-09-26T00:00:01Z",
                    "evidence":"no-owned-artifact-or-progress",
                },
                {
                    "grant":1,"source":"supervisor","kind":"runtime-cancel",
                    "session":"s2","timestamp":"2026-09-26T00:00:02Z",
                    "evidence":"no-owned-artifact-or-progress",
                },
                {
                    "grant":1,"source":"supervisor","kind":"runtime-cancel",
                    "session":"s3","timestamp":"2026-09-26T00:00:03Z",
                    "evidence":"durable-partial-state-preserved",
                },
            ],
            "operator_retry_grants":1,
            "operator_overrides":[{
                "grant":1,"source":"operator-cli",
                "timestamp":"2026-09-26T00:00:10Z",
                "reason":"test operator continuation",
            }],
            "plan_contract_revisions":[{
                "attempt":6,
                "source":"supervisor-plan-contract-revision",
                "previous_verify_sha256":"a"*64,
                "current_verify_sha256":"b"*64,
                "timestamp":"2026-09-26T00:01:00Z",
            }],
            "operator_retry_attempts":[
                {
                    "sequence":6,"session":"s6",
                    "state":"plan_contract_replacement",
                    "outcome":"plan_contract_replacement",
                    "consumes_operator_grant":False,
                    "source":"supervisor",
                    "normalized_by":"supervisor-credit-authority-v1",
                    "normalized_at":"2026-09-26T00:01:10Z",
                    "timestamp":"2026-09-26T00:01:05Z",
                },
                {
                    "sequence":7,"session":"s7",
                    "state":"infrastructure_abort",
                    "outcome":"infrastructure_abort",
                    "consumes_operator_grant":False,
                    "source":"supervisor",
                    "resolved_at":"2026-09-26T00:01:30Z",
                    "timestamp":"2026-09-26T00:01:20Z",
                },
                {
                    "sequence":8,"session":"dispatch:t8",
                    "state":"reserved",
                    "consumes_operator_grant":False,
                    "source":"supervisor",
                    "timestamp":"2026-09-26T00:02:00Z",
                },
            ],
        }
        self.assertFalse(
            control_state._attempt_state_v2612_original(entry)["valid"]
        )
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertTrue(state["v2612_infrastructure_repair"])
        self.assertEqual(state["operator_infrastructure_aborted"],1)
        self.assertEqual(state["operator_grants_reserved"],1)
        self.assertEqual(state["operator_grants_remaining"],0)
        self.assertEqual(state["allowed_attempts"],8)
        self.assertTrue(state["unmaterialized_dispatch_reusable"])

    def test_normalized_state_marks_status_lag_native_attempt_inflight(self):
        data={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":1,
                    "sessions":["session-one"],
                    "automatic_limit":2,
                }
            },
        }
        (self.project/".opencode-v2/work/attempts.json").write_text(
            json.dumps(data)
        )
        base={
            "leaves":{
                "D001":{
                    "eligible":True,
                    "attempt_limit_reached":True,
                    "role":"implementer",
                }
            },
            "execution_blockers":[{
                "deliverable":"D001",
                "reason":"attempt_limit_reached",
            }],
            "resume_phase":"execution-blocked",
            "acceptance":{"complete":False},
            "plan":{"complete":True},
        }
        with mock.patch.object(
            supervisor,"state_snapshot",return_value=base
        ), mock.patch.object(
            supervisor,"active_implementation_sessions",return_value=[]
        ), mock.patch.object(
            supervisor,"apply_root_continuation_block",
            side_effect=lambda value:value
        ):
            result=supervisor.normalized_state_snapshot(str(self.project))
        leaf=result["leaves"]["D001"]
        sched=result["scheduler"]
        self.assertTrue(leaf["running"])
        self.assertFalse(leaf["eligible"])
        self.assertFalse(leaf.get("reserved",False))
        self.assertEqual(sched["pending_workers"],1)
        self.assertEqual(sched["pending_deliverables"],["D001"])
        self.assertEqual(sched["available_worker_slots"],2)
        self.assertEqual(result["execution_blockers"],[])
        self.assertEqual(result["resume_phase"],"execution")

    def test_scheduler_runtime_db_and_project_are_late_bound(self):
        db=self.project/"late-bound-opencode.db"
        con=sqlite3.connect(db)
        con.execute("create table probe(value integer)")
        con.execute("insert into probe values (7)")
        con.commit()
        con.close()
        with mock.patch.object(
            supervisor,"DB",self.project/"missing-import-time.db"
        ), mock.patch.object(
            supervisor,"PROJECT","/stale/import-time/project"
        ), mock.patch.dict(
            os.environ,{
                "V2_OPENCODE_DB":str(db),
                "V2_PROJECT":str(self.project),
            },clear=False
        ):
            self.assertEqual(
                supervisor.runtime_project(),str(self.project)
            )
            check=supervisor.db_connect()
            try:
                self.assertEqual(
                    check.execute("select value from probe").fetchone()[0],7
                )
            finally:
                check.close()

    def test_supervisor_cli_bootstrap_fills_missing_stage_a_runtime(self):
        root=self.project/"harness"
        db=root/"xdg/data-v11831-a2/opencode/opencode.db"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).close()
        with mock.patch.object(
            supervisor,"ROOT",root
        ), mock.patch.object(
            supervisor,"PROJECT",""
        ), mock.patch.object(
            supervisor,"discover_runtime_base_url",
            return_value="http://127.0.0.1:58508"
        ), mock.patch.dict(
            os.environ,{},clear=True
        ):
            bound=supervisor.configure_cli_runtime(
                str(self.project),""
            )
            self.assertEqual(bound["project"],str(self.project.resolve()))
            self.assertEqual(bound["db"],str(db))
            self.assertEqual(bound["session_table"],"session")
            self.assertEqual(
                bound["base_url"],"http://127.0.0.1:58508"
            )

    def test_scheduler_database_failure_denies_new_reservation_fail_closed(self):
        old_validate=supervisor.validate_dispatch
        old_active=supervisor.active_implementation_sessions
        try:
            supervisor.validate_dispatch=lambda _agent,text: (text,"")
            def broken(strict=False):
                if strict:
                    raise RuntimeError("db unavailable")
                return []
            supervisor.active_implementation_sessions=broken
            result=supervisor.preclaim_attempt("implementer","D001","tok")
        finally:
            supervisor.validate_dispatch=old_validate
            supervisor.active_implementation_sessions=old_active
        self.assertEqual(result[0],"denied")
        self.assertEqual(result[2],"worker_scheduler_unavailable")
        self.assertFalse((self.project/".opencode-v2/work/attempts.json").exists())

    def test_reusable_reservation_is_not_marked_running_and_stays_replayable(self):
        data={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "count":1,
                    "sessions":["dispatch:t1"],
                    "automatic_limit":3,
                }
            },
        }
        (self.project/".opencode-v2/work/attempts.json").write_text(
            json.dumps(data)
        )
        base={
            "leaves":{
                "D001":{
                    "eligible":True,
                    "attempt_limit_reached":False,
                    "role":"implementer",
                }
            },
            "execution_blockers":[],
            "resume_phase":"execution",
            "acceptance":{"complete":False},
            "plan":{"complete":True},
        }
        with mock.patch.object(
            supervisor,"state_snapshot",return_value=base
        ), mock.patch.object(
            supervisor,"active_implementation_sessions",return_value=[]
        ), mock.patch.object(
            supervisor,"apply_root_continuation_block",
            side_effect=lambda value:value
        ):
            result=supervisor.normalized_state_snapshot(str(self.project))
        leaf=result["leaves"]["D001"]
        sched=result["scheduler"]
        self.assertTrue(leaf["eligible"])
        self.assertFalse(leaf["running"])
        self.assertTrue(leaf["reserved"])
        self.assertTrue(leaf["reservation_replay"])
        self.assertEqual(sched["reserved_workers"],1)
        self.assertEqual(
            sched["replayable_reserved_deliverables"],["D001"]
        )
        self.assertEqual(sched["available_worker_slots"],2)

    def test_replayable_reservation_dispatches_without_free_new_slot(self):
        decision={
            "resume_phase":"execution",
            "eligible":["D001","D002"],
            "eligible_roles":{
                "D001":"implementer",
                "D002":"feature-builder",
            },
            "scheduler":{
                "active_workers":2,
                "available_worker_slots":0,
                "replayable_reserved_deliverables":["D001"],
            },
        }
        self.assertEqual(
            deterministic_dispatch.select_actions(decision),
            [{"kind":"launch","agent":"implementer","deliverable":"D001"}],
        )

    def test_existing_reservation_can_be_reused_even_when_all_slots_are_full(self):
        data={"owner":"supervisor","deliverables":{}}
        for n in range(3):
            did=f"D{n+1:03d}"
            data["deliverables"][did]={
                "count":1,"sessions":[f"dispatch:t{n}"],"automatic_limit":3,
            }
        (self.project/".opencode-v2/work/attempts.json").write_text(json.dumps(data))
        old_validate=supervisor.validate_dispatch
        old_active=supervisor.active_implementation_sessions
        old_own=supervisor.write_ownership_baseline
        old_exec=supervisor.ensure_execution_baseline
        old_split=supervisor.recursive_split_enabled
        try:
            supervisor.validate_dispatch=lambda _agent,text: (text,"")
            supervisor.active_implementation_sessions=lambda strict=False: []
            supervisor.write_ownership_baseline=lambda _did: None
            supervisor.ensure_execution_baseline=lambda _did,_attempt: None
            supervisor.recursive_split_enabled=lambda: False
            result=supervisor.preclaim_attempt("implementer","D001","replacement")
        finally:
            supervisor.validate_dispatch=old_validate
            supervisor.active_implementation_sessions=old_active
            supervisor.write_ownership_baseline=old_own
            supervisor.ensure_execution_baseline=old_exec
            supervisor.recursive_split_enabled=old_split
        self.assertEqual(result[0],"claimed",result)
        ledger=json.loads((self.project/".opencode-v2/work/attempts.json").read_text())
        self.assertEqual(supervisor.reserved_dispatch_slot_count(ledger),3)


    def test_terminal_failure_binds_to_session_attempt_after_newer_preclaim(self):
        ledger={
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":2,
                    "count":2,
                    "sessions":["session-one","session-two"],
                    "infrastructure_retry_grants":1,
                    "infrastructure_failures":[{
                        "grant":1,
                        "source":"supervisor",
                        "kind":"runtime-cancel",
                        "session":"session-one",
                        "evidence":"no-owned-artifact-or-progress",
                        "reason":"runtime-cancel",
                    }],
                }
            },
        }
        path=self.project/".opencode-v2/work/attempts.json"
        path.write_text(json.dumps(ledger))
        self.assertEqual(
            supervisor.record_leaf_failure(
                "D001","runtime-cancel","infrastructure",
                sid="session-one",
            ),
            (True,"infrastructure"),
        )
        entry=json.loads(path.read_text())["deliverables"]["D001"]
        self.assertEqual(
            entry["failure_history"],
            [{
                "attempt":1,
                "classification":"infrastructure",
                "reason":"runtime-cancel",
                "timestamp":entry["failure_history"][0]["timestamp"],
                "source":"supervisor",
                "session":"session-one",
            }],
        )
        self.assertFalse(any(
            row.get("attempt")==2
            for row in entry["failure_history"]
            if isinstance(row,dict)
        ))
        state=control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["count"],2)
        self.assertEqual(state["allowed_attempts"],3)
        self.assertIn("session-two",supervisor.pending_ledger_session_ids())

    def test_session_bound_failure_rejects_unknown_or_duplicate_binding(self):
        path=self.project/".opencode-v2/work/attempts.json"
        path.write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":2,
                    "count":2,
                    "sessions":["same","same"],
                }
            },
        }))
        self.assertEqual(
            supervisor.record_leaf_failure(
                "D001","verify-failed-1","genuine",sid="same"
            ),
            (False,"session-attempt-binding-invalid"),
        )
        self.assertEqual(
            supervisor.record_leaf_failure(
                "D001","verify-failed-1","genuine",sid="missing"
            ),
            (False,"session-attempt-binding-invalid"),
        )



class LeafContextEvidenceAuthorityTests(unittest.TestCase):
    def test_split_child_context_inlines_parent_supervisor_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            work=ctrl/"work"
            work.mkdir(parents=True)
            (ctrl/"ACCEPTANCE.md").write_text(
                "# Acceptance\n## Reference policy: internal\n"
                "## MUST checks\n- [ ] A001: required behavior.\n"
                "<!-- ACCEPTANCE_COMPLETE -->\n"
            )
            (work/"D001.verify-evidence.json").write_text(json.dumps({
                "owner":"supervisor",
                "protocol":"v2-supervisor-verify-evidence-v1",
                "deliverable":"D001",
                "entries":[
                    {
                        "attempt":1,"command":"check-one","executed":True,
                        "exit_code":1,"result":"verify-failed-1",
                        "stdout":"x"*2000,"stderr":"first error",
                    },
                    {
                        "attempt":2,"command":"check-two","executed":True,
                        "exit_code":2,"result":"verify-failed-2",
                        "stdout":"second output","stderr":"second error",
                    },
                ],
            }))
            (work/"D001.functional-diagnostic-evidence.json").write_text(
                json.dumps({
                    "owner":"supervisor",
                    "protocol":"v2-functional-diagnostic-evidence-v1",
                    "deliverable":"D001","executed":True,
                    "command":"diagnose","exit_code":1,
                    "result":"functional-diagnostic-verify-failed-1",
                    "stderr":"diagnostic evidence",
                })
            )
            (work/"D001-A.scope.md").write_text(
                "# D001-A split-child scope\nChild scope: diagnose failure\n"
            )
            manifest={"leaves":{
                "D001":{
                    "id":"D001","name":"parent","outcome":"parent outcome",
                    "role":"test-builder",
                    "owned_artifacts":"result.json",
                    "owned_artifact_paths":["result.json"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "acceptance_ids":["A001"],"complexity":"S",
                    "repeated_operations":1,"deep_reasoning":False,
                    "verify_command":"check-parent","done_when":"parent passes",
                    "split_children":["D001-A","D001-B"],
                },
                "D001-A":{
                    "id":"D001-A","parent":"D001","name":"probe",
                    "outcome":"diagnose","parent_outcome_context":"parent outcome",
                    "role":"probe-builder","owned_artifacts":"none",
                    "owned_artifact_paths":[],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                    "acceptance_ids":[],"complexity":"S",
                    "repeated_operations":1,"deep_reasoning":False,
                    "verify_command":"handoff-check","done_when":"handoff ready",
                    "split_handoff_only":True,
                    "split_reads_existing":["result.json"],
                    "split_creates_or_updates":[],
                },
            }}
            packet=control_query_views.build_leaf_contexts(
                project,manifest
            )["D001-A"]
            self.assertEqual(
                [row["exit_code"] for row in
                 packet["parent_supervisor_verify_evidence"]],
                [1,2],
            )
            self.assertLessEqual(
                len(packet["parent_supervisor_verify_evidence"][0]["stdout"]),
                control_query_views.MAX_PARENT_EVIDENCE_TEXT_CHARS+30,
            )
            self.assertEqual(
                packet["parent_supervisor_functional_diagnostic_evidence"]
                ["exit_code"],1,
            )
            self.assertEqual(
                packet["parent_owned_artifact_paths"],["result.json"]
            )
            authority=packet["evidence_authority"]
            self.assertTrue(
                any("Rejected splitter/model outputs" in rule
                    for rule in authority["rules"])
            )
            self.assertTrue(
                any("split-proposal.failed" in item
                    for item in authority["non_authoritative"])
            )
            self.assertTrue(
                any("current_progress" in item
                    for item in authority["non_authoritative"])
            )
            self.assertTrue(
                any("provisional continuity scratch" in rule
                    for rule in authority["rules"])
            )


class ProgressHandoffBatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        (self.project/".opencode-v2/work").mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.did="D001"
        self.sid="s1"
        self.progress=self.project/".opencode-v2/work/D001.progress.md"
        self.progress.write_text(
            "HANDOFF_READY: false\n\n"
            "Findings:\nknown fact\n\n"
            "Evidence:\nmeasured evidence\n\n"
            "Next step:\nread bounded inputs\n"
        )

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def _records(self,*ids):
        return [
            {"id":mid,"data":{"role":"assistant"}}
            for mid in ids
        ]

    def test_parallel_sibling_reads_share_one_open_discovery_response(self):
        progress=str(self.progress)
        parts={
            "m-progress":[{
                "type":"tool","tool":"write",
                "state":{
                    "status":"completed",
                    "input":{"filePath":progress},
                },
            }],
            "m-discovery":[
                {
                    "type":"tool","tool":"read",
                    "state":{
                        "status":"completed",
                        "input":{"filePath":str(self.project/"a.txt")},
                    },
                },
                {
                    "type":"tool","tool":"read",
                    "state":{
                        "status":"pending",
                        "input":{"filePath":str(self.project/"b.txt")},
                    },
                },
            ],
        }
        with mock.patch.object(
            supervisor,"v1_runtime_enabled",return_value=True
        ), mock.patch.object(
            supervisor,"_v1_message_records",
            return_value=self._records("m-progress","m-discovery"),
        ), mock.patch.object(
            supervisor,"_v1_message_parts",
            side_effect=lambda mid:parts[mid],
        ):
            self.assertTrue(
                supervisor._v1_progress_discovery_batch_open(
                    self.sid,self.did
                )
            )

    def test_later_assistant_response_requires_checkpoint(self):
        progress=str(self.progress)
        parts={
            "m-progress":[{
                "type":"tool","tool":"write",
                "state":{
                    "status":"completed",
                    "input":{"filePath":progress},
                },
            }],
            "m-discovery":[{
                "type":"tool","tool":"read",
                "state":{
                    "status":"completed",
                    "input":{"filePath":str(self.project/"a.txt")},
                },
            }],
            "m-later":[{
                "type":"tool","tool":"read",
                "state":{
                    "status":"pending",
                    "input":{"filePath":str(self.project/"b.txt")},
                },
            }],
        }
        with mock.patch.object(
            supervisor,"v1_runtime_enabled",return_value=True
        ), mock.patch.object(
            supervisor,"_v1_message_records",
            return_value=self._records(
                "m-progress","m-discovery","m-later"
            ),
        ), mock.patch.object(
            supervisor,"_v1_message_parts",
            side_effect=lambda mid:parts[mid],
        ):
            self.assertFalse(
                supervisor._v1_progress_discovery_batch_open(
                    self.sid,self.did
                )
            )

    def test_progress_ready_allows_only_exact_handoff_verify(self):
        progress=str(self.progress)
        verify="python3 -c \"print('ok')\""
        self.progress.write_text(
            "HANDOFF_READY: true\n\n"
            "Findings:\nknown fact\n\n"
            "Evidence:\nmeasured evidence\n\n"
            "Next step:\nWriter updates only the parent-owned manifest.\n"
        )
        history=[("write",{"filePath":progress})]
        child={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
            "verify_command":verify,
        }
        parent={
            "id":"D009",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
        }
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="probe-builder"
        ), mock.patch.object(
            supervisor,"first_user_text_db",
            return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":child,"D009":parent}}
        ), mock.patch.object(
            supervisor,"session_completed_tool_inputs",
            return_value=history
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="not-attempted"
        ):
            self.assertEqual(
                supervisor.progress_handoff_tool_state(
                    self.sid,"bash",{"command":verify}
                ),
                ("allow","progress-ready-exact-verify"),
            )
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"read",
                {"filePath":str(self.project/"anything.txt")},
            )
            self.assertEqual(state,"deny")
            self.assertIn("PROGRESS_READY_IMMUTABLE",detail)
            self.assertIn("no-rewrite=true",detail)
            self.assertIn("no-discovery=true",detail)
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"write",
                {
                    "filePath":progress,
                    "content":self.progress.read_text(),
                },
            )
            self.assertEqual(state,"deny")
            self.assertIn("PROGRESS_READY_IMMUTABLE",detail)

    def test_progress_ready_accepts_exact_worker_verify_wrapper(self):
        import base64
        import shlex
        progress=str(self.progress)
        verify="python3 -c \"print('ok')\""
        self.progress.write_text(
            "HANDOFF_READY: true\n\n"
            "Findings:\nknown fact\n\n"
            "Evidence:\nmeasured evidence\n\n"
            "Next step:\nWriter updates only the parent-owned manifest.\n"
        )
        history=[("write",{"filePath":progress})]
        child={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
            "verify_command":verify,
        }
        parent={
            "id":"D009",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
        }
        wrapper=str((supervisor.ROOT/"scripts"/"worker_sandbox.py").resolve())
        command=shlex.join([
            "python3",wrapper,"run-worker-verify",
            "--project",str(self.project.resolve()),
            "--session",self.sid,"--agent","probe-builder",
            "--command-b64",base64.b64encode(verify.encode()).decode(),
        ])
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="probe-builder"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":child,"D009":parent}}
        ), mock.patch.object(
            supervisor,"session_completed_tool_inputs",return_value=history
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="not-attempted"
        ):
            self.assertEqual(
                supervisor.progress_handoff_tool_state(
                    self.sid,"bash",{"command":command}
                ),
                ("allow","progress-ready-exact-verify"),
            )

    def test_progress_ready_passed_verify_requires_return_without_tools(self):
        progress=str(self.progress)
        verify="python3 -c \"print('ok')\""
        self.progress.write_text(
            "HANDOFF_READY: true\n\n"
            "Findings:\nknown fact\n\n"
            "Evidence:\nmeasured evidence\n\n"
            "Next step:\nWriter updates only the parent-owned manifest.\n"
        )
        history=[("write",{"filePath":progress})]
        child={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
            "verify_command":verify,
        }
        parent={
            "id":"D009",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
        }
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="probe-builder"
        ), mock.patch.object(
            supervisor,"first_user_text_db",return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":child,"D009":parent}}
        ), mock.patch.object(
            supervisor,"session_completed_tool_inputs",return_value=history
        ), mock.patch.object(
            supervisor,"plan_contract_session_exact_verify_state",
            return_value="passed"
        ):
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"read",{"filePath":str(self.project/"anything.txt")}
            )
        self.assertEqual(state,"return-required")
        self.assertIn("PROGRESS_EXACT_VERIFY_PASSED",detail)
        self.assertIn("return-without-tools",detail)

    def test_progress_ready_denies_unowned_writer_delta(self):
        progress=str(self.progress)
        history=[("write",{"filePath":progress})]
        child={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
        }
        parent={
            "id":"D009",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
        }
        common=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="probe-builder"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",
                return_value="DELIVERABLE: D001\n"
            ),
            mock.patch.object(
                supervisor,"load_manifest",
                return_value={"leaves":{"D001":child,"D009":parent}}
            ),
            mock.patch.object(
                supervisor,"session_completed_tool_inputs",
                return_value=history
            ),
        )
        tick=chr(96)
        bad=(
            "HANDOFF_READY: true\n\nFindings:\nmissing helper\n\n"
            "Evidence:\nconfirmed absent\n\nNext step:\n"
            "Writer must CREATE "+tick+
            ".opencode-v2/bin/check-imports.py"+tick+" now.\n"
        )
        good=(
            "HANDOFF_READY: true\n\nFindings:\nmissing helper\n\n"
            "Evidence:\nconfirmed absent\n\nNext step:\n"
            "Writer must UPDATE "+tick+
            ".opencode-v2/TEST_CHECKS.json"+tick+
            " to remove the missing dependency.\n"
        )
        good_with_code_examples=(
            "HANDOFF_READY: true\n\nFindings:\nwrong import\n\n"
            "Evidence:\nconfirmed mismatch\n\nNext step:\n"
            "Writer must edit .opencode-v2/TEST_CHECKS.json line 9, replacing "+
            tick+"from src.text import normalize_text"+tick+" with "+
            tick+"from miniutils.text import normalize_text"+tick+
            ", then rerun "+tick+"python3 -m unittest tests.test_text -v"+tick+
            ".\n"
        )
        good_with_negated_unowned_actions=(
            "HANDOFF_READY: true\n\nFindings:\nstale requirements\n\n"
            "Evidence:\nconfirmed mismatch\n\nNext step:\n"
            "Do not create or edit any unowned artifacts; "
            "writer must update "+tick+".opencode-v2/TEST_CHECKS.json"+tick+
            " to remove or replace stale requirements.\n"
        )
        with common[0],common[1],common[2],common[3]:
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"write",
                {"filePath":progress,"content":bad},
            )
            self.assertEqual(state,"deny")
            self.assertIn("UNOWNED_REPAIR_TARGET",detail)
            self.assertIn(
                "allowed_targets=.opencode-v2/TEST_CHECKS.json",detail
            )
            self.assertIn(
                "do-not-tell-writer-to-create/write/edit/update-unowned-artifacts",
                detail,
            )
        common=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="probe-builder"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",
                return_value="DELIVERABLE: D001\n"
            ),
            mock.patch.object(
                supervisor,"load_manifest",
                return_value={"leaves":{"D001":child,"D009":parent}}
            ),
            mock.patch.object(
                supervisor,"session_completed_tool_inputs",
                return_value=history
            ),
        )
        with common[0],common[1],common[2],common[3]:
            self.assertEqual(
                supervisor.progress_handoff_tool_state(
                    self.sid,"write",
                    {"filePath":progress,"content":good},
                ),
                ("allow","checkpoint-write"),
            )
        with mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":child,"D009":parent}}
        ):
            self.assertEqual(
                supervisor._progress_handoff_unowned_repair_targets(
                    "D001",good_with_code_examples
                ),
                [],
            )
            self.assertEqual(
                supervisor._progress_handoff_unowned_repair_targets(
                    "D001",good_with_negated_unowned_actions
                ),
                [],
            )

    def test_false_checkpoint_may_mention_future_ready_without_ready_guard(self):
        progress=str(self.progress)
        history=[("write",{"filePath":progress})]
        child={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
        }
        parent={
            "id":"D009",
            "owned_artifact_paths":[".opencode-v2/TEST_CHECKS.json"],
        }
        tick=chr(96)
        content=(
            "HANDOFF_READY: false\n\n"
            "Findings:\nmissing helper\n\n"
            "Evidence:\nconfirmed absent\n\n"
            "Next step:\n"
            "Inspect current manifest; only later set HANDOFF_READY: true. "
            "Do not CREATE "+tick+".opencode-v2/bin/check-imports.py"+tick+"; "
            "writer must UPDATE "+tick+".opencode-v2/TEST_CHECKS.json"+tick+".\n"
        )
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="probe-builder"
        ), mock.patch.object(
            supervisor,"first_user_text_db",
            return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":child,"D009":parent}}
        ), mock.patch.object(
            supervisor,"session_completed_tool_inputs",
            return_value=history
        ):
            self.assertEqual(
                supervisor.progress_handoff_tool_state(
                    self.sid,"write",
                    {"filePath":progress,"content":content},
                ),
                ("allow","checkpoint-write"),
            )

    def test_progress_gate_denies_rejected_splitter_history(self):
        progress=str(self.progress)
        history=[("write",{"filePath":progress})]
        leaf={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
        }
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="probe-builder"
        ), mock.patch.object(
            supervisor,"first_user_text_db",
            return_value="DELIVERABLE: D001\n"
        ), mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":leaf}}
        ), mock.patch.object(
            supervisor,"session_completed_tool_inputs",return_value=history
        ):
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"read",{
                    "filePath":str(
                        self.project/
                        ".opencode-v2/work/D009.split-proposal.failed-2.json"
                    )
                },
            )
            self.assertEqual(state,"deny")
            self.assertIn("NONAUTHORITATIVE_HISTORY",detail)
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"read",
                {"filePath":str(self.project/"current-artifact.json")},
            )
            self.assertEqual(
                (state,detail),
                ("allow","one-discovery-after-checkpoint"),
            )

    def test_progress_runtime_prompt_refocuses_on_authoritative_evidence(self):
        leaf={
            "id":"D001","parent":"D009",
            "split_handoff_only":True,"owned_artifact_paths":[],
        }
        with mock.patch.object(
            supervisor,"load_manifest",
            return_value={"leaves":{"D001":leaf}}
        ):
            prompt=supervisor.implementation_runtime_prompt(
                "D001","probe-builder"
            )
        self.assertIn("EVIDENCE AUTHORITY — EXACT",prompt)
        self.assertIn("parent_supervisor_verify_evidence",prompt)
        self.assertIn("parent_owned_artifact_paths",prompt)
        self.assertIn("NON-AUTHORITATIVE",prompt)
        self.assertIn("provisional continuity scratch",prompt)
        self.assertIn("do not search alternate locations",prompt)

    def test_progress_gate_allows_same_open_discovery_response_only(self):
        progress=str(self.progress)
        history=[
            ("write",{"filePath":progress}),
            ("read",{"filePath":str(self.project/"a.txt")}),
        ]
        leaf={
            "id":"D001",
            "split_handoff_only":True,
            "owned_artifact_paths":[],
        }
        common=[
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="probe-builder"
            ),
            mock.patch.object(
                supervisor,"first_user_text_db",
                return_value="DELIVERABLE: D001\n"
            ),
            mock.patch.object(
                supervisor,"load_manifest",
                return_value={"leaves":{"D001":leaf}}
            ),
            mock.patch.object(
                supervisor,"session_completed_tool_inputs",
                return_value=history
            ),
        ]
        for patcher in common:
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(
            supervisor,"_v1_progress_discovery_batch_open",return_value=True
        ):
            self.assertEqual(
                supervisor.progress_handoff_tool_state(
                    self.sid,"read",
                    {"filePath":str(self.project/"b.txt")},
                ),
                ("allow","same-discovery-response"),
            )
        with mock.patch.object(
            supervisor,"_v1_progress_discovery_batch_open",return_value=False
        ):
            state,detail=supervisor.progress_handoff_tool_state(
                self.sid,"read",
                {"filePath":str(self.project/"b.txt")},
            )
            self.assertEqual(state,"deny")
            self.assertIn("PROGRESS_CHECKPOINT_REQUIRED",detail)
            self.assertIn(
                "next_tool=write:.opencode-v2/work/D001.progress.md",detail
            )
            self.assertIn("no-discovery=true",detail)


class ProgressAwareWatchdogTests(unittest.TestCase):
    def setUp(self):
        supervisor.watch.clear()

    def test_visible_watchdog_resets_on_reasoning_progress(self):
        key=("m1","")
        age,st=supervisor.watchdog_age("s1",key,True,progress_marker=("m1","",0),now=100.0)
        self.assertEqual(age,0)
        age,st=supervisor.watchdog_age("s1",key,True,progress_marker=("m1","",0),now=219.0)
        self.assertEqual(age,119.0)
        age,st=supervisor.watchdog_age("s1",key,True,progress_marker=("m1","",1),now=220.0)
        self.assertEqual(age,0)
        age,st=supervisor.watchdog_age("s1",key,True,progress_marker=("m1","",1),now=339.0)
        self.assertEqual(age,119.0)

    def test_live_sse_reasoning_delta_counts_as_progress(self):
        state={"reasoning":0,"text":0,"tool_running":False,"progress_seq":0}
        supervisor.reduce_live_event(
            state,{"type":"session.next.reasoning.delta","data":{"delta":"abc"}},now=50.0
        )
        self.assertEqual(state["reasoning"],3)
        self.assertEqual(state["progress_seq"],1)
        self.assertEqual(state["last_progress"],50.0)

    def test_prometheus_metrics_parser(self):
        text = """
# HELP vllm:num_requests_running ...
vllm:num_requests_running{model_name="qwen"} 1
vllm:num_requests_waiting{model_name="qwen"} 0
vllm:prompt_tokens_total{model_name="qwen"} 1234
vllm:generation_tokens_total{model_name="qwen"} 77
vllm:kv_cache_usage_perc{model_name="qwen"} 0.42
"""
        parsed=watchdog_telemetry.parse_prometheus_metrics(text)
        self.assertEqual(parsed["running"],1)
        self.assertEqual(parsed["prompt_tokens"],1234)
        self.assertEqual(parsed["generation_tokens"],77)
        self.assertAlmostEqual(parsed["kv_usage"],0.42)

    def test_backend_compute_extends_invisible_session(self):
        snapshot={
            "metrics_available":True,"running":1,"waiting":0,
            "backend_progress_age":2.0,"gpu_util":95.0,
        }
        d=watchdog_telemetry.invisible_watchdog_decision(700,snapshot)
        self.assertFalse(d["abort"])
        self.assertEqual(d["limit"],watchdog_telemetry.INVISIBLE_EXCLUSIVE_EXTENSION_SECONDS)

    def test_shared_backend_progress_has_finite_extension(self):
        snapshot={
            "metrics_available":True,"running":3,"waiting":0,
            "backend_progress_age":2.0,"gpu_util":95.0,
        }
        d=watchdog_telemetry.invisible_watchdog_decision(901,snapshot)
        self.assertTrue(d["abort"])
        self.assertEqual(d["limit"],watchdog_telemetry.INVISIBLE_SHARED_EXTENSION_SECONDS)

    def test_idle_backend_aborts_at_old_600_second_boundary(self):
        snapshot={
            "metrics_available":True,"running":0,"waiting":0,
            "backend_progress_age":999.0,"gpu_util":0.0,
        }
        d=watchdog_telemetry.invisible_watchdog_decision(600,snapshot)
        self.assertTrue(d["abort"])
        self.assertEqual(d["phase"],"backend-idle")


    def test_active_implementation_reasoning_allows_nine_k_but_remains_bounded(self):
        state={
            "reasoning":9000,
            "text":0,
            "tool_running":False,
        }
        self.assertEqual(
            supervisor.event_watchdog_reason("integrator",state),""
        )
        state["reasoning"]=supervisor.HARD_REASONING_CHARS
        self.assertEqual(
            supervisor.event_watchdog_reason("integrator",state),
            f"sse_reasoning_chars={supervisor.HARD_REASONING_CHARS}",
        )
        self.assertEqual(supervisor.HARD_REASONING_CHARS,20000)

    def test_visible_shared_decode_extends_no_tool_age_but_stays_finite(self):
        snapshot={
            "metrics_available":True,
            "running":2,
            "waiting":0,
            "backend_progress_age":1.0,
            "prompt_progress_age":99.0,
            "generation_progress_age":1.0,
            "gpu_util":95.0,
        }
        d=watchdog_telemetry.visible_watchdog_decision(
            121,snapshot,base_seconds=120
        )
        self.assertFalse(d["abort"])
        self.assertEqual(
            d["limit"],
            watchdog_telemetry.VISIBLE_SHARED_EXTENSION_SECONDS,
        )
        d=watchdog_telemetry.visible_watchdog_decision(
            watchdog_telemetry.VISIBLE_SHARED_EXTENSION_SECONDS,
            snapshot,base_seconds=120
        )
        self.assertTrue(d["abort"])
        self.assertIn("shared", "shared")
        self.assertIn("visible-extension-exhausted",d["reason"])

    def test_visible_exclusive_decode_gets_stronger_bounded_extension(self):
        snapshot={
            "metrics_available":True,
            "running":1,
            "waiting":0,
            "backend_progress_age":1.0,
            "prompt_progress_age":99.0,
            "generation_progress_age":1.0,
            "gpu_util":95.0,
        }
        d=watchdog_telemetry.visible_watchdog_decision(
            299,snapshot,base_seconds=120
        )
        self.assertFalse(d["abort"])
        self.assertEqual(
            d["limit"],
            watchdog_telemetry.VISIBLE_EXCLUSIVE_EXTENSION_SECONDS,
        )
        d=watchdog_telemetry.visible_watchdog_decision(
            watchdog_telemetry.VISIBLE_EXCLUSIVE_EXTENSION_SECONDS,
            snapshot,base_seconds=120
        )
        self.assertTrue(d["abort"])

    def test_visible_idle_or_unknown_backend_keeps_120_second_fail_safe(self):
        idle={
            "metrics_available":True,
            "running":0,
            "waiting":0,
            "backend_progress_age":999.0,
            "prompt_progress_age":999.0,
            "generation_progress_age":999.0,
            "gpu_util":0.0,
        }
        d=watchdog_telemetry.visible_watchdog_decision(
            120,idle,base_seconds=120
        )
        self.assertTrue(d["abort"])
        self.assertEqual(d["limit"],120)
        self.assertEqual(d["reason"],"backend-not-progressing")
        d=watchdog_telemetry.visible_watchdog_decision(
            120,{"metrics_available":False},base_seconds=120
        )
        self.assertTrue(d["abort"])
        self.assertEqual(d["reason"],"backend-telemetry-unavailable")

    def test_backend_phase_distinguishes_prefill_and_decode(self):
        prefill={
            "metrics_available":True,"running":1,"waiting":0,
            "backend_progress_age":1.0,"prompt_progress_age":1.0,
            "generation_progress_age":None,"gpu_util":90.0,
        }
        decode={
            "metrics_available":True,"running":1,"waiting":0,
            "backend_progress_age":1.0,"prompt_progress_age":99.0,
            "generation_progress_age":1.0,"gpu_util":90.0,
        }
        self.assertEqual(watchdog_telemetry.backend_phase(prefill),"backend-prefill")
        self.assertEqual(watchdog_telemetry.backend_phase(decode),"backend-decode")

    def test_unknown_backend_preserves_old_fail_safe_boundary(self):
        d=watchdog_telemetry.invisible_watchdog_decision(
            600,{"metrics_available":False}
        )
        self.assertTrue(d["abort"])
        self.assertEqual(d["reason"],"backend-telemetry-unavailable")

class ControlPolicyRotationLifecycleTests(unittest.TestCase):
    def test_same_policy_keeps_running(self):
        with mock.patch.object(
            supervisor,"control_policy_fingerprint",
            return_value=supervisor.START_CONTROL_POLICY_FINGERPRINT,
        ), mock.patch.object(supervisor.os,"execv") as execv:
            self.assertTrue(supervisor.ensure_running_control_policy_current())
            execv.assert_not_called()

    def test_policy_rotation_reexecs_same_supervisor_command(self):
        rotated="f"*64
        with mock.patch.object(
            supervisor,"control_policy_fingerprint",return_value=rotated
        ), mock.patch.object(
            supervisor,"log"
        ), mock.patch.object(
            supervisor,"csv"
        ), mock.patch.object(
            supervisor.os,"execv",side_effect=RuntimeError("exec-called")
        ) as execv:
            with self.assertRaisesRegex(RuntimeError,"exec-called"):
                supervisor.ensure_running_control_policy_current()
        execv.assert_called_once()
        executable,argv=execv.call_args.args
        self.assertEqual(executable,sys.executable)
        self.assertEqual(argv[0],sys.executable)
        self.assertEqual(Path(argv[1]).resolve(),Path(supervisor.__file__).resolve())
        self.assertEqual(argv[2:],sys.argv[1:])

    def test_supervisor_runtime_rotation_reexecs_without_policy_change(self):
        rotated="e"*64
        with mock.patch.object(
            supervisor,"control_policy_fingerprint",
            return_value=supervisor.START_CONTROL_POLICY_FINGERPRINT,
        ), mock.patch.object(
            supervisor,"supervisor_runtime_fingerprint",return_value=rotated
        ), mock.patch.object(
            supervisor,"log"
        ) as log, mock.patch.object(
            supervisor,"csv"
        ), mock.patch.object(
            supervisor.os,"execv",side_effect=RuntimeError("exec-called")
        ) as execv:
            with self.assertRaisesRegex(RuntimeError,"exec-called"):
                supervisor.ensure_running_control_policy_current()
        execv.assert_called_once()
        self.assertTrue(
            any(
                "SUPERVISOR_RUNTIME_ROTATION_REEXEC" in str(call.args[0])
                for call in log.call_args_list
            )
        )

    def test_rotation_preflight_compile_failure_keeps_old_supervisor_alive(self):
        rotated="d"*64
        checked=type("Checked",(),{
            "returncode":1,
            "stdout":"",
            "stderr":"SyntaxError: candidate source invalid",
        })()
        with mock.patch.object(
            supervisor,"control_policy_fingerprint",return_value=rotated
        ), mock.patch.object(
            supervisor,"log"
        ) as log, mock.patch.object(
            supervisor,"csv"
        ), mock.patch.object(
            supervisor.subprocess,"run",return_value=checked
        ) as run, mock.patch.object(
            supervisor.os,"execv"
        ) as execv:
            self.assertFalse(
                supervisor.ensure_running_control_policy_current()
            )
        run.assert_called_once()
        self.assertIn("py_compile",run.call_args.args[0])
        execv.assert_not_called()
        self.assertTrue(any(
            "CONTROL_POLICY_REEXEC_PREFLIGHT_BLOCKED" in str(call.args[0])
            for call in log.call_args_list
        ))

    def test_unreadable_policy_blocks_work_without_reexec(self):
        with mock.patch.object(
            supervisor,"control_policy_fingerprint",
            side_effect=OSError("mid-update"),
        ), mock.patch.object(
            supervisor,"log"
        ), mock.patch.object(supervisor.os,"execv") as execv:
            self.assertFalse(supervisor.ensure_running_control_policy_current())
            execv.assert_not_called()

    def test_restart_adopts_old_active_session_until_terminal_reconcile(self):
        old_start=supervisor.START_MS
        old_adopted=set(supervisor.restart_adopted_sessions)
        try:
            supervisor.START_MS=1000
            supervisor.restart_adopted_sessions.clear()
            self.assertTrue(
                supervisor.restart_reconcile_session_allowed(
                    "ses-active",500,{"ses-active"},set()
                )
            )
            self.assertIn("ses-active",supervisor.restart_adopted_sessions)
            # Once adopted while active, the same pre-restart session remains
            # eligible for terminal reconciliation after leaving active state.
            self.assertTrue(
                supervisor.restart_reconcile_session_allowed(
                    "ses-active",500,set(),set()
                )
            )
            self.assertFalse(
                supervisor.restart_reconcile_session_allowed(
                    "ses-historical",500,set(),set()
                )
            )
            self.assertTrue(
                supervisor.restart_reconcile_session_allowed(
                    "ses-pending",500,set(),{"ses-pending"}
                )
            )
            self.assertTrue(
                supervisor.restart_reconcile_session_allowed(
                    "ses-new",1500,set(),set()
                )
            )
        finally:
            supervisor.START_MS=old_start
            supervisor.restart_adopted_sessions.clear()
            supervisor.restart_adopted_sessions.update(old_adopted)


class RootSessionDurableRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        (self.project/".opencode-v2/work").mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def write_ledger(self,roots):
        payload={
            "owner":"stage-a-controller",
            "protocol":"v2-stage-a-controller-execution-ledger-v1",
            "executions":{
                f"e{i}":{"root_session":root}
                for i,root in enumerate(roots,1)
            },
        }
        (
            self.project/".opencode-v2/work/stage-a-controller-executions.json"
        ).write_text(json.dumps(payload))

    def test_supervisor_recovers_unanimous_root_from_controller_ledger(self):
        self.write_ledger(["ses-root","ses-root"])
        with mock.patch.object(
            supervisor,"_valid_root_session",return_value=True
        ):
            self.assertEqual(
                supervisor._root_session_from_controller_ledger(),
                "ses-root",
            )

    def test_supervisor_conflicting_roots_fail_closed(self):
        self.write_ledger(["ses-a","ses-b"])
        with self.assertRaises(state_io.StateCorruptionError):
            supervisor._root_session_from_controller_ledger()

    def test_root_orchestrator_recreates_missing_tracker_from_ledger(self):
        fake_con=mock.MagicMock()
        fake_con.execute.return_value.fetchone.return_value=None
        with mock.patch.object(supervisor,"db_connect",return_value=fake_con), \
             mock.patch.object(
                 supervisor,"_root_session_from_controller_ledger",
                 return_value="ses-root",
             ):
            self.assertEqual(supervisor.root_orchestrator_id(),"ses-root")
        tracker=json.loads(
            (self.project/".opencode-v2/work/root-session.json").read_text()
        )
        self.assertEqual(tracker["owner"],"supervisor")
        self.assertEqual(tracker["session"],"ses-root")


class AcceptanceValidatorTrustBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        (self.ctrl/"work").mkdir(parents=True)
        self.old_project=supervisor.PROJECT
        supervisor.PROJECT=str(self.project)
        self.sid="ses-acceptance-validator"
        (self.ctrl/"ACCEPTANCE.md").write_text(
            "# Acceptance Contract\n"
            "## MUST Checks\n"
            "- [ ] A008: README contains a valid CLI example.\n"
            "<!-- ACCEPTANCE_COMPLETE -->\n"
        )
        self.valid_report=json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"PASS",
            "checks":[{
                "id":"A008",
                "status":"PASS",
                "evidence":"README CLI example verified by canonical evidence.",
                "required_executable":True,
                "command":"test -f README.md",
                "exit_code":0,
            }],
        })+"\n"
        (self.ctrl/"TEST_REPORT.json").write_text(json.dumps({
            "protocol":"v2-test-report-v1",
            "status":"pass",
            "checks_run":1,
            "checks_passed":1,
            "missing_required_files":[],
            "checks":[{
                "name":"readme",
                "command":"test -f README.md",
                "exit_code":0,
                "timed_out":False,
            }],
        }))

    def tearDown(self):
        supervisor.PROJECT=self.old_project
        self.tmp.cleanup()

    def test_validator_allows_only_first_exact_report_write(self):
        target=str(self.ctrl/"acceptance-report.json")
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="acceptance-validator"
        ), mock.patch.object(
            supervisor,"acceptance_validator_report_records",return_value=[]
        ):
            state,detail=supervisor.acceptance_validator_tool_state(
                self.sid,"write",{"filePath":target,"content":self.valid_report}
            )
            self.assertEqual(state,"allow")
            self.assertIn("first-valid-write",detail)
            state,detail=supervisor.acceptance_validator_tool_state(
                self.sid,"read",{"filePath":str(self.ctrl/"ACCEPTANCE.md")}
            )
            self.assertEqual(state,"deny")
            self.assertIn("FIRST_TOOL_REPORT_WRITE_REQUIRED",detail)

        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="acceptance-validator"
        ), mock.patch.object(
            supervisor,"acceptance_validator_report_records",
            return_value=[{
                "content":self.valid_report,
                "sha256":hashlib.sha256(self.valid_report.encode()).hexdigest(),
            }],
        ):
            state,detail=supervisor.acceptance_validator_tool_state(
                self.sid,"write",{"filePath":target,"content":"{}"}
            )
            self.assertEqual(state,"deny")
            self.assertIn("REPORT_IMMUTABLE",detail)
            self.assertIn("return_exact_bare_ACCEPTANCE_PASS_now",detail)

    def test_validator_rejects_malformed_executable_before_durable_write(self):
        target=str(self.ctrl/"acceptance-report.json")
        malformed=json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"PASS",
            "checks":[{
                "id":"A008",
                "status":"PASS",
                "evidence":"Canonical evidence says the executable check passed.",
                "required_executable":True,
                "command":"python3 -c \"assert 'unterminated\"",
                "exit_code":0,
            }],
        })+"\n"
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="acceptance-validator"
        ), mock.patch.object(
            supervisor,"acceptance_validator_report_records",return_value=[]
        ):
            state,detail=supervisor.acceptance_validator_tool_state(
                self.sid,"write",{"filePath":target,"content":malformed}
            )
            self.assertEqual(state,"deny")
            self.assertIn("REPORT_INVALID",detail)
            self.assertIn("A008-unsafe-command",detail)
            self.assertIn("corrected-write",detail)

            state,detail=supervisor.acceptance_validator_tool_state(
                self.sid,"write",{"filePath":target,"content":self.valid_report}
            )
            self.assertEqual(state,"allow")
            self.assertIn("first-valid-write",detail)

    def test_validator_rejects_safe_but_unobserved_executable_command(self):
        target=str(self.ctrl/"acceptance-report.json")
        report=json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"PASS",
            "checks":[{
                "id":"A008",
                "status":"PASS",
                "evidence":"Claimed executable evidence is not canonical.",
                "required_executable":True,
                "command":"test -s README.md",
                "exit_code":0,
            }],
        })+"\n"
        with (
            mock.patch.object(
                supervisor,"_session_agent_db",
                return_value="acceptance-validator",
            ),
            mock.patch.object(
                supervisor,"acceptance_validator_report_records",
                return_value=[],
            ),
        ):
            state,detail=supervisor.acceptance_validator_tool_state(
                self.sid,"write",{"filePath":target,"content":report}
            )
        self.assertEqual(state,"deny")
        self.assertIn("REPORT_INVALID",detail)
        self.assertIn(
            "A008-command-not-exact-observed-evidence",detail
        )

    def test_terminal_fail_restores_first_report_after_legacy_overwrite(self):
        first=json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"FAIL",
            "checks":[{
                "id":"A008","status":"FAIL",
                "evidence":"README example contradicts implemented CLI usage.",
            }],
        })+"\n"
        later=json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"PASS",
            "checks":[{
                "id":"A008","status":"PASS",
                "evidence":"later unsupported reinterpretation of same evidence",
            }],
        })+"\n"
        (self.ctrl/"acceptance-report.json").write_text(later)
        (self.ctrl/"acceptance-pass.json").write_text("{}\n")
        records=[
            {
                "content":first,
                "sha256":hashlib.sha256(first.encode()).hexdigest(),
            },
            {
                "content":later,
                "sha256":hashlib.sha256(later.encode()).hexdigest(),
            },
        ]
        with mock.patch.object(
            supervisor,"_session_agent_db",return_value="acceptance-validator"
        ), mock.patch.object(
            supervisor,"_v1_active_session_ids",return_value={}
        ), mock.patch.object(
            supervisor,"last_assistant_text_db",
            return_value="ACCEPTANCE_FAIL\nA008: README example is invalid.",
        ), mock.patch.object(
            supervisor,"acceptance_validator_report_records",
            return_value=records,
        ), mock.patch.object(supervisor,"log"), mock.patch.object(supervisor,"csv"):
            ok,detail=supervisor.recover_final_acceptance_failure(self.sid)

        self.assertTrue(ok)
        self.assertEqual(detail,"fail-report-restored")
        restored=json.loads((self.ctrl/"acceptance-report.json").read_text())
        self.assertEqual(restored["result"],"FAIL")
        self.assertFalse((self.ctrl/"acceptance-pass.json").exists())
        audit=json.loads(
            (self.ctrl/"work"/f"acceptance-validator-terminal-{self.sid}.json")
            .read_text()
        )
        self.assertTrue(audit["legacy_multiwrite_recovered"])
        self.assertEqual(audit["completed_report_writes"],2)
        self.assertEqual(audit["failed_acceptance_ids"],["A008"])

    def test_terminal_pass_requires_one_matching_report_write(self):
        report=json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"PASS",
            "checks":[{
                "id":"A008","status":"PASS","evidence":"deterministic evidence",
            }],
        })+"\n"
        (self.ctrl/"acceptance-report.json").write_text(report)
        row={
            "content":report,
            "sha256":hashlib.sha256(report.encode()).hexdigest(),
        }
        common=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="acceptance-validator"
            ),
            mock.patch.object(supervisor,"_v1_active_session_ids",return_value={}),
            mock.patch.object(
                supervisor,"last_assistant_text_db",
                return_value="All checks are supported by durable evidence.\n\nACCEPTANCE_PASS",
            ),
        )
        with common[0],common[1],common[2], mock.patch.object(
            supervisor,"acceptance_validator_report_records",
            return_value=[row,row],
        ):
            self.assertEqual(
                supervisor.reconcile_terminal_acceptance_validator(self.sid),
                (False,"pass-report-write-count:2"),
            )

        common=(
            mock.patch.object(
                supervisor,"_session_agent_db",return_value="acceptance-validator"
            ),
            mock.patch.object(supervisor,"_v1_active_session_ids",return_value={}),
            mock.patch.object(
                supervisor,"last_assistant_text_db",
                return_value="All checks are supported by durable evidence.\n\nACCEPTANCE_PASS",
            ),
        )
        with common[0],common[1],common[2], mock.patch.object(
            supervisor,"acceptance_validator_report_records",return_value=[row]
        ):
            self.assertEqual(
                supervisor.reconcile_terminal_acceptance_validator(self.sid),
                (True,"pass-ok"),
            )

    def test_terminal_max_step_recovers_one_valid_immutable_pass_report(self):
        report=self.valid_report
        (self.ctrl/"acceptance-report.json").write_text(report)
        row={
            "content":report,
            "sha256":hashlib.sha256(report.encode()).hexdigest(),
        }
        with (
            mock.patch.object(
                supervisor,"_session_agent_db",
                return_value="acceptance-validator",
            ),
            mock.patch.object(
                supervisor,"_v1_active_session_ids",return_value={},
            ),
            mock.patch.object(
                supervisor,"last_assistant_text_db",
                return_value=(
                    "Maximum steps for this agent have been reached.\n"
                    "The validated report was already written."
                ),
            ),
            mock.patch.object(
                supervisor,"acceptance_validator_report_records",
                return_value=[row],
            ),
            mock.patch.object(
                supervisor,"validate_acceptance_report_write_content",
                return_value=[],
            ),
            mock.patch.object(supervisor,"log"),
            mock.patch.object(supervisor,"csv"),
        ):
            self.assertEqual(
                supervisor.reconcile_terminal_acceptance_validator(self.sid),
                (True,"max-step-valid-report-recovered"),
            )
        audit=json.loads(
            (self.ctrl/"work"/f"acceptance-validator-terminal-{self.sid}.json")
            .read_text()
        )
        self.assertEqual(audit["terminal_verdict"],"PASS")
        self.assertEqual(
            audit["recovery_kind"],
            "max-step-after-valid-immutable-report",
        )

    def test_terminal_max_step_recovery_refuses_multiple_report_writes(self):
        report=self.valid_report
        (self.ctrl/"acceptance-report.json").write_text(report)
        row={
            "content":report,
            "sha256":hashlib.sha256(report.encode()).hexdigest(),
        }
        with (
            mock.patch.object(
                supervisor,"_session_agent_db",
                return_value="acceptance-validator",
            ),
            mock.patch.object(
                supervisor,"_v1_active_session_ids",return_value={},
            ),
            mock.patch.object(
                supervisor,"last_assistant_text_db",
                return_value="Maximum steps for this agent have been reached.",
            ),
            mock.patch.object(
                supervisor,"acceptance_validator_report_records",
                return_value=[row,row],
            ),
        ):
            ok,detail=supervisor.reconcile_terminal_acceptance_validator(
                self.sid
            )
        self.assertFalse(ok)
        self.assertIn("max-step-report-write-count:2",detail)

    def test_terminal_pass_rejects_any_fail_marker_even_if_last_line_is_pass(self):
        with mock.patch.object(
            supervisor,"last_assistant_text_db",
            return_value="ACCEPTANCE_FAIL A008 unresolved\nACCEPTANCE_PASS",
        ):
            verdict,_=supervisor._acceptance_terminal_verdict(self.sid)
        self.assertEqual(verdict,"fail")


if __name__=="__main__": unittest.main()
