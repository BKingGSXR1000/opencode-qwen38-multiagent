#!/usr/bin/env python3
import hashlib,json,multiprocessing,runpy,subprocess,sys,tempfile,unittest
from unittest import mock
from pathlib import Path
HERE=Path(__file__).resolve().parent; sys.path.insert(0,str(HERE))
import acceptance_contract,control_state,leaf_contract,stage_a_controller,structured_plan,supervisor,state_io,worker_sandbox,watchdog_telemetry

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
        f"validated={control_state.PHASE_READY_VALIDATOR}\n"
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

    def test_internal_reference_guard_distinguishes_negation_from_requirement(self):
        check=self.guard["internal_external_reference_violation"]
        self.assertFalse(check(
            "No external reference, network, or externally maintained source is\nrequired."
        ))
        self.assertFalse(check(
            "The contract contains no dependence on externally maintained truth."
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
        ):
            ok,detail=supervisor.recover_sandbox_wrapper_history_poison(
                "D001"
            )
        self.assertEqual(
            (ok,detail),
            (False,"wrapper-history-recovery-no-wrapper-failure"),
        )


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
                supervisor,"attempt_sequence_for_session",return_value=7
            ),
            mock.patch.object(
                supervisor,"_owned_artifact_changed_since_execution_baseline",
                return_value=(False,"unchanged")
            ),
        )
        with patches[0],patches[1],patches[2],patches[3],patches[4]:
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
            "immediate_runtime_abort":supervisor.immediate_runtime_abort,
            "persisted_abort_reason":supervisor.persisted_abort_reason,
            "ready_info":supervisor.ready_info,
            "meaningful_worker_execution":supervisor.meaningful_worker_execution,
        }
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
                f"validated={control_state.PHASE_READY_VALIDATOR}\n"
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
            "launch_deps":[],"split_depth":1,
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
            "launch_deps":["D001-A"],"split_depth":1,
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
            "D001","parent-finalize-failed",
            generation=1,children=["D001-A","D001-B"],
            transaction_id="old-transaction",
            parent_finalize_failures=3,
            parent_finalize_last_result="durable-progress-incomplete",
            reason="durable-progress-incomplete",
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



class WorkerSandboxTrustBoundaryTests(unittest.TestCase):
    def setUp(self):
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
        self.tmp.cleanup()

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
            self.assertEqual(
                supervisor.progress_handoff_tool_state(
                    self.sid,"read",
                    {"filePath":str(self.project/"b.txt")},
                ),
                ("deny","PROGRESS_CHECKPOINT_REQUIRED"),
            )


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

if __name__=="__main__": unittest.main()
