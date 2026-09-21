#!/usr/bin/env python3
import hashlib,json,multiprocessing,runpy,subprocess,sys,tempfile,unittest
from unittest import mock
from pathlib import Path
HERE=Path(__file__).resolve().parent; sys.path.insert(0,str(HERE))
import control_state,leaf_contract,stage_a_controller,supervisor,state_io,worker_sandbox,watchdog_telemetry

def ready_text(did,attempt=1,owner="supervisor",protocol=None):
    protocol=protocol or control_state.LEAF_READY_PROTOCOL
    return f"status=complete\ndeliverable={did}\nattempt={attempt}\nverified=true\nowner={owner}\nprotocol={protocol}\n"

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


class NativeChildBindingAndRestartRecoveryTests(unittest.TestCase):
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
                did,"opencode-server-restart-incomplete-session","infrastructure"
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
        (self.project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves":{"D001":{"launch_deps":[]}}}))
        (self.work/"attempts.json").write_text(json.dumps({"owner":"supervisor","deliverables":{"D001":{"count":1,"sessions":["s1"],"automatic_limit":3}}}))
    def tearDown(self): self.tmp.cleanup()
    def test_ready_requires_provenance_and_current_attempt(self):
        p=self.work/"D001.ready"; p.write_text("status=complete\ndeliverable=D001\nattempt=1\nverified=true\n"); self.assertFalse(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001")); self.assertTrue(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",2)); self.assertFalse(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",protocol="wrong")); self.assertFalse(control_state.ready_info(self.project,"D001"))
        p.write_text(ready_text("D001",owner="model")); self.assertFalse(control_state.ready_info(self.project,"D001"))
    def test_non_supervisor_ledger_owner_rejected(self):
        (self.work/"D001.ready").write_text(ready_text("D001")); data=json.loads((self.work/"attempts.json").read_text()); data["owner"]="model"; (self.work/"attempts.json").write_text(json.dumps(data)); self.assertFalse(control_state.ready_info(self.project,"D001"))

class SplitValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=supervisor.PROJECT; supervisor.PROJECT=self.tmp.name; ctrl=Path(self.tmp.name)/".opencode-v2"; (ctrl/"work").mkdir(parents=True)
        parent={"id":"D001","name":"parent","owned_artifacts":"`a.txt`, `b.txt`","owned_artifact_paths":["a.txt","b.txt"],"launch_deps":[],"contract_deps":[],"verify_deps":[],"verify_command":"test -f a.txt -a -f b.txt","role":"implementer","done_when":"done","acceptance_ids":["A001"],"parallel":"none","split_children":[]}
        (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,"leaves":{"D001":parent}}))
    def tearDown(self): supervisor.PROJECT=self.old; self.tmp.cleanup()
    def proposals(self):
        return [{"scope":"write all","owned_artifacts":"`a.txt`, `b.txt`","verify_command":"test -f a.txt","role":"implementer","depends_on_sibling":"","done_when":"files exist"},{"scope":"verify","owned_artifacts":"none","verify_command":"test -f b.txt","role":"tester","depends_on_sibling":"first","done_when":"verified"}]
    def test_shared_rules_apply_to_split(self):
        supervisor.validate_split_proposal("D001",self.proposals())
        bad=self.proposals(); bad[0]["verify_command"]="true"
        with self.assertRaisesRegex(ValueError,"non-verifying"): supervisor.validate_split_proposal("D001",bad)
        bad=self.proposals(); bad[0]["role"]="potato"
        with self.assertRaisesRegex(ValueError,"unknown implementation Role"): supervisor.validate_split_proposal("D001",bad)

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
            self.assertIn("supervisor re-runs the exact leaf Verify command",contract)
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
        supervisor.validate_dispatch=lambda agent,text: ("D001","")
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
        old_failure=supervisor.compaction_failure
        old_abort=supervisor.abort_session
        supervisor.ready_info=lambda _did:{}
        supervisor.compaction_failure=lambda _sid:""
        supervisor.abort_session=lambda *_args,**_kwargs:False
        try:
            with self.assertRaises(RuntimeError):
                supervisor.reconcile_compaction_event(
                    sid,"implementer",supervisor.MAX_IMPLEMENTATION_COMPACTIONS+1
                )
            self.assertEqual(supervisor.compaction_seen.get(sid,0),0)
        finally:
            supervisor.ready_info=old_ready
            supervisor.compaction_failure=old_failure
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
            "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves":self.leaves,
        }))
    def _write_ledger(self):
        (self.work/"attempts.json").write_text(json.dumps(self.ledger))
    def _ready(self,did,attempt=1):
        (self.work/f"{did}.ready").write_text(ready_text(did,attempt))

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
                "verify_command":"test -d fixtures/vectors/moons",
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

    def test_finalize_rejects_recorded_sandbox_violation(self):
        ctx=worker_sandbox.resolve_worker(self.project,"s1","","implementer")
        worker_sandbox.record_violation(
            self.project,ctx,"direct-tool-outside-ownership",
            {"tool":"edit","path":"other.txt"},
        )
        ok,detail=supervisor.post_session_finalize("D001","s1")
        self.assertFalse(ok)
        self.assertEqual(detail,"sandbox-ownership-violation")


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
            (project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves":{"D001":leaf}}))
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
