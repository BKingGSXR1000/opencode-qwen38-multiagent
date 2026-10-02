#!/usr/bin/env python3
"""Isolated regression tests for the Stage-A stabilization boundary."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import hashlib
import inspect
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import control_state
import control_query_views
import control_policy
import deterministic_dispatch
import stage_a_controller as controller
import stage_a_path_permissions as path_permissions
import runtime_contract
import supervisor
import worker_sandbox


OWNED = ".opencode-v2/ACCEPTANCE.md"
RULES = [("*", "deny"), (OWNED, "allow")]


class ReferenceResearcherModelTests(unittest.TestCase):
    def test_reference_researcher_uses_dedicated_nonthinking_model(self):
        root=Path(__file__).resolve().parent.parent
        role=(root/"xdg/config/opencode/agents/reference-researcher.md").read_text()
        config=(root/"xdg/config/opencode/opencode.jsonc").read_text()
        self.assertIn("model: syv/qwen38-reference-nothink",role)
        self.assertNotIn("model: syv/qwen38-reasoning-48k",role)
        anchor='"qwen38-reference-nothink"'
        start=config.index(anchor)
        block=config[start:start+900]
        self.assertIn('"context": 32768',block)
        self.assertIn('"output": 4096',block)
        self.assertIn('"enable_thinking": false',block)


class LauncherReadinessTimeoutTests(unittest.TestCase):
    def test_stage_a_launcher_bounds_status_probe(self):
        text = Path(__file__).with_name("start-stage-a-run.sh").read_text()
        self.assertIn("--connect-timeout 1 --max-time 2", text)
        self.assertIn('http_status(){', text)

    def test_server_launcher_kills_child_if_runtime_setup_fails(self):
        text = Path(__file__).with_name("run-a2-v11831-server.sh").read_text()
        self.assertIn('SERVER_PID=""', text)
        self.assertIn('trap cleanup_runtime EXIT', text)
        self.assertIn('kill -0 "$SERVER_PID"', text)
        self.assertIn('kill "$SERVER_PID"', text)
        self.assertLess(
            text.index('"$BIN" serve --hostname 127.0.0.1 --port "$PORT" &'),
            text.index('python3 "$ROOT/scripts/runtime_contract.py"'),
        )

    def test_normal_entrypoints_require_consolidated_preflight_proof(self):
        scripts = Path(__file__).resolve().parent
        launcher = (scripts / "start-stage-a-run.sh").read_text()
        tick = (scripts / "run-stage-a-tick.py").read_text()
        driver = (scripts / "drive-stage-a-run.py").read_text()

        first_preflight = launcher.index('python3 "$ROOT/scripts/stage_a_preflight.py"')
        bootstrap = launcher.index('python3 "$ROOT/scripts/bootstrap-stage-a-project.py"')
        second_preflight = launcher.index(
            'python3 "$ROOT/scripts/stage_a_preflight.py"', first_preflight + 1
        )
        driver_exec = launcher.index('exec python3 "$ROOT/scripts/drive-stage-a-run.py"')
        self.assertLess(first_preflight, bootstrap)
        self.assertLess(second_preflight, driver_exec)
        self.assertIn('--write-proof "$PREFLIGHT_PROOF"', launcher)
        self.assertIn('--preflight-proof "$PREFLIGHT_PROOF"', launcher)
        self.assertIn("ns.preflight_proof is None", tick)
        self.assertIn("preflight.verify_proof(", tick)
        self.assertIn("ns.preflight_proof is None", driver)
        self.assertIn("preflight.verify_proof(proof, project, base_url, root_session)", driver)


class ControlPolicyProvenanceTests(unittest.TestCase):
    def test_policy_fingerprint_includes_its_own_source(self):
        self.assertIn("control_policy.py",control_policy._POLICY_FILES)

    def test_phase_ready_validators_are_artifact_scoped(self):
        plan=control_policy.phase_ready_validator_id()
        acceptance=control_policy.phase_ready_validator_id("ACCEPTANCE.md")
        self.assertEqual(control_state.PHASE_READY_VALIDATOR,plan)
        self.assertEqual(control_state.ACCEPTANCE_READY_VALIDATOR,acceptance)
        self.assertRegex(plan,r"^deterministic-policy-[0-9a-f]{20}$")
        self.assertRegex(
            acceptance,r"^deterministic-acceptance-[0-9a-f]{20}$"
        )
        self.assertNotEqual(plan,acceptance)
        self.assertEqual(
            control_policy._ACCEPTANCE_POLICY_FILES,
            ("acceptance_contract.py",),
        )
        self.assertNotIn(
            "leaf_contract.py",control_policy._ACCEPTANCE_POLICY_FILES
        )

    def test_materialized_control_epoch_is_same_policy_fingerprint(self):
        current=control_policy.control_policy_epoch()
        self.assertEqual(control_query_views.CONTROL_POLICY_EPOCH,current)
        self.assertRegex(
            current,r"^v2-control-policy-[0-9a-f]{20}$"
        )


class RootSessionLedgerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        (self.project/".opencode-v2/work").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_ledger(self,roots):
        executions={
            f"e{i}":{
                "execution_id":f"e{i}",
                "root_session":root,
                "action":{"kind":"launch","agent":"feature-builder","deliverable":"D001"},
            }
            for i,root in enumerate(roots,1)
        }
        controller.save_execution_ledger(self.project,{
            "owner":"stage-a-controller",
            "protocol":controller.EXECUTION_LEDGER_PROTOCOL,
            "executions":executions,
        })

    def test_missing_tracker_recovers_unanimous_root_from_execution_ledger(self):
        self.write_ledger(["ses-root","ses-root"])
        self.assertEqual(
            controller.root_session_from_execution_ledger(self.project),
            "ses-root",
        )
        info={
            "id":"ses-root","agent":"transport-root",
            "parentID":None,"directory":str(self.project),
        }
        with mock.patch.object(
            controller,"http_json",return_value=(200,info)
        ):
            self.assertEqual(
                controller.resolve_root_session(
                    self.project,"http://127.0.0.1:1"
                ),
                "ses-root",
            )

    def test_conflicting_ledger_roots_fail_closed(self):
        self.write_ledger(["ses-a","ses-b"])
        with self.assertRaisesRegex(
            controller.ControllerError,"conflicting root_session"
        ):
            controller.root_session_from_execution_ledger(self.project)


class RuntimeContractBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        base=Path(self.tmp.name)
        self.root=base/"harness"
        self.project=base/"project"
        self.overlay=base/"overlay"
        self.project.mkdir()
        opencode=self.root/"xdg/config/opencode"
        (opencode/"agents").mkdir(parents=True)
        (opencode/"plugins").mkdir()
        (self.root/"scripts").mkdir()
        (opencode/"opencode.jsonc").write_text("{}\n")
        (opencode/"AGENTS.md").write_text("stable global instructions\n")
        (opencode/"plugins/v2-bounded-subagent.js").write_text(
            "export const marker = true;\n"
        )
        (opencode/"agents/tester.md").write_text(
            "---\ndescription: test\nmode: subagent\n---\n"
        )
        (self.root/"scripts/stage_a_path_permissions.py").write_text(
            "# permission projection contract\n"
        )
        path_permissions.create_overlay(
            self.project,self.root/"xdg/config",self.overlay
        )
        self.base_url="http://127.0.0.1:59999"

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self):
        return runtime_contract.write_state(
            self.root,self.project,self.base_url,os.getpid(),self.overlay
        )

    def test_runtime_marker_binds_canonical_and_actual_overlay(self):
        state=self.write_state()
        self.assertEqual(
            state["overlay_contract_sha256"],
            runtime_contract.overlay_contract_sha256(self.overlay),
        )
        verified=runtime_contract.verify_state(
            self.root,self.project,self.base_url
        )
        self.assertEqual(
            verified["canonical_contract_sha256"],
            runtime_contract.contract_sha256(self.root),
        )

    def test_old_runtime_marker_protocol_fails_closed(self):
        self.write_state()
        path=runtime_contract.state_path(self.root,self.base_url)
        data=json.loads(path.read_text())
        data["protocol"]="v2-opencode-runtime-contract-v1"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(
            ValueError,"runtime contract marker protocol mismatch"
        ):
            runtime_contract.verify_state(
                self.root,self.project,self.base_url
            )

    def test_controller_stale_runtime_contract_fails_closed(self):
        with mock.patch.object(
            controller,"verify_runtime_server_state",
            side_effect=ValueError("stale marker"),
        ):
            with self.assertRaisesRegex(
                controller.ControllerError,"OPENCODE_RUNTIME_CONTRACT_STALE"
            ):
                controller.require_current_runtime_contract(
                    self.project,self.base_url
                )

    def test_all_native_dispatchers_gate_runtime_before_root_idle(self):
        for fn in (
            controller.execute_first_planner,
            controller.execute_first_semantic,
            controller.execute_first_implementation,
            controller.execute_first_task_splitter,
        ):
            source=inspect.getsource(fn)
            gate=source.index(
                "require_current_runtime_contract(project, base_url)"
            )
            idle=source.index("ensure_root_idle(project, base_url, root)")
            self.assertLess(gate,idle,fn.__name__)

    def test_overlay_mutation_fails_closed(self):
        self.write_state()
        (self.overlay/"opencode/agents/tester.md").write_text(
            "---\ndescription: mutated\nmode: subagent\n---\n"
        )
        with self.assertRaisesRegex(
            ValueError,"runtime contract overlay mismatch"
        ):
            runtime_contract.verify_state(
                self.root,self.project,self.base_url
            )

    def test_global_agents_change_invalidates_running_contract(self):
        self.write_state()
        (self.root/"xdg/config/opencode/AGENTS.md").write_text(
            "changed global instructions\n"
        )
        with self.assertRaisesRegex(
            ValueError,"runtime contract mismatch: canonical_contract_sha256"
        ):
            runtime_contract.verify_state(
                self.root,self.project,self.base_url
            )


class DriverReportingTests(unittest.TestCase):
    def load_driver(self):
        path = Path(__file__).with_name("drive-stage-a-run.py")
        spec = importlib.util.spec_from_file_location("stage_a_driver_test", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_driver_binds_runtime_environment_from_verified_server(self):
        driver=self.load_driver()
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)/"project"
            project.mkdir()
            db=Path(td)/"opencode.db"
            sqlite3.connect(db).close()
            source={
                "V2_OPENCODE_BASE_URL":"http://127.0.0.1:58508",
                "V2_OPENCODE_DB":str(db),
                "V2_ROOT":str(driver.HARNESS_ROOT),
                "V2_OPENCODE_SESSION_TABLE":"session",
            }
            with mock.patch.object(
                driver.runtime_contract,"verify_state",
                return_value={"server_pid":12345},
            ), mock.patch.object(
                driver,"read_process_environment",return_value=source,
            ), mock.patch.dict(driver.os.environ,{},clear=True):
                bound=driver.configure_runtime_environment(
                    project,"http://127.0.0.1:58508"
                )
                self.assertEqual(bound["V2_PROJECT"],str(project.resolve()))
                self.assertEqual(driver.os.environ["V2_OPENCODE_DB"],str(db))
                self.assertEqual(
                    driver.os.environ["V2_OPENCODE_SESSION_TABLE"],"session"
                )

    def test_driver_cannot_execute_tick_when_preflight_proof_fails(self):
        driver = self.load_driver()
        with mock.patch.object(
            driver.preflight, "verify_proof", side_effect=driver.preflight.PreflightError("bad proof")
        ), mock.patch.object(driver.tick, "execute_one") as execute:
            with self.assertRaises(driver.preflight.PreflightError):
                driver.drive(Path("/tmp/project"), "http://127.0.0.1:1", "root", Path("/tmp/proof"), 0.01, 0)
        execute.assert_not_called()

    def test_terminal_and_blocked_reporting_is_compact_and_deduplicated(self):
        driver = self.load_driver()
        complete = {
            "outcome": "no-dispatch",
            "state_version": "complete-state",
            "actions": [{"kind": "complete"}],
        }
        blocked = {
            "outcome": "no-dispatch",
            "state_version": "blocked-state",
            "actions": [{"kind": "blocked", "reason": "attempt_limit_reached"}],
        }

        out = io.StringIO()
        with mock.patch.object(driver.preflight, "verify_proof"), mock.patch.object(
            driver.tick, "execute_one", return_value=complete
        ), mock.patch.object(driver.time, "sleep"), contextlib.redirect_stdout(out):
            self.assertEqual(
                driver.drive(Path("/tmp/project"), "http://127.0.0.1:1", "root", Path("/tmp/proof"), 0.01, 0),
                0,
            )
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {"event": "no-dispatch", "actions": [{"kind": "complete"}]})

        out = io.StringIO()
        with mock.patch.object(driver.preflight, "verify_proof"), mock.patch.object(
            driver.tick, "execute_one", side_effect=[blocked, blocked, blocked]
        ), mock.patch.object(driver.time, "sleep"), contextlib.redirect_stdout(out):
            self.assertEqual(
                driver.drive(Path("/tmp/project"), "http://127.0.0.1:1", "root", Path("/tmp/proof"), 0.01, 0),
                2,
            )
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), {"event": "no-dispatch", "actions": blocked["actions"]})

    def test_blocked_attempt_limit_waits_for_autonomous_split_reconciliation(self):
        driver=self.load_driver()
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            (work/"D011.split-status.json").write_text(json.dumps({
                "owner":"supervisor",
                "parent_id":"D011",
                "generation":1,
                "claim_count":1,
                "state":"split-retryable",
            }))
            blocked={
                "outcome":"no-dispatch",
                "state_version":"blocked-state",
                "actions":[{
                    "kind":"blocked",
                    "deliverable":"D011",
                    "reason":"attempt_limit_reached",
                }],
            }
            complete={
                "outcome":"no-dispatch",
                "state_version":"complete-state",
                "actions":[{"kind":"complete"}],
            }
            with mock.patch.object(
                driver.preflight,"verify_proof"
            ), mock.patch.object(
                driver.tick,"execute_one",
                side_effect=[blocked,blocked,blocked,complete],
            ), mock.patch.object(driver.time,"sleep"):
                self.assertEqual(
                    driver.drive(
                        project,"http://127.0.0.1:1","root",
                        project/"proof",0.01,0
                    ),
                    0,
                )

    def test_terminal_split_state_does_not_mask_attempt_limit_block(self):
        driver=self.load_driver()
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            (work/"D011.split-status.json").write_text(json.dumps({
                "owner":"supervisor",
                "parent_id":"D011",
                "generation":1,
                "claim_count":2,
                "state":"splitter-failed",
            }))
            receipt={
                "outcome":"no-dispatch",
                "state_version":"blocked-state",
                "actions":[{
                    "kind":"blocked",
                    "deliverable":"D011",
                    "reason":"attempt_limit_reached",
                }],
            }
            self.assertFalse(
                driver.blocked_split_reconciliation_pending(project,receipt)
            )


class StageATickPreflightSafetyTests(unittest.TestCase):
    def test_tick_cannot_dispatch_when_preflight_proof_is_rejected(self):
        path=Path(__file__).with_name("run-stage-a-tick.py")
        spec=importlib.util.spec_from_file_location("stage_a_tick_safety_test",path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        tick=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)

        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            proof=project/"proof.json"
            proof.write_text("{}")
            argv=[
                "run-stage-a-tick.py",
                "--project",str(project),
                "--base-url","http://127.0.0.1:1",
                "--root-session","ses-root",
                "--preflight-proof",str(proof),
            ]
            with mock.patch.object(sys,"argv",argv), mock.patch.object(
                tick.preflight,"verify_proof",
                side_effect=tick.preflight.PreflightError("rejected proof"),
            ), mock.patch.object(tick,"execute_one") as execute:
                with self.assertRaisesRegex(tick.TickError,"rejected proof"):
                    tick.main()
                execute.assert_not_called()


class PlannerContractTests(unittest.TestCase):
    def test_verify_command_is_explicitly_single_line(self):
        role = (
            Path(__file__).resolve().parent.parent
            / "xdg/config/opencode/agents/implementation-planner.md"
        ).read_text()
        self.assertIn("exactly one physical line", role)
        self.assertIn("no embedded\n  newline", role)
        self.assertIn("Never embed a multiline Python/JavaScript/shell program", role)
        self.assertIn("python3 -m py_compile", role)
        self.assertIn("bounded owned test/helper", role)

    def test_behavioral_done_when_requires_behavioral_verify_in_role_and_repair_prompt(self):
        role = (
            Path(__file__).resolve().parent.parent
            / "xdg/config/opencode/agents/implementation-planner.md"
        ).read_text()
        self.assertIn("Verify MUST\n  execute that relevant behavior", role)
        prompt=controller.PLANNER_PROMPTS["repair"]
        self.assertIn("MUST\nactually execute the relevant behavior",prompt)
        self.assertIn("Syntax checks, file existence, grep",prompt)

    def test_plugin_enforces_targeted_planner_tool_boundary(self):
        plugin=(
            Path(__file__).resolve().parent.parent
            / "xdg/config/opencode/plugins/v2-bounded-subagent.js"
        ).read_text()
        self.assertIn("function guardPlannerToolBoundary",plugin)
        self.assertIn('"--planner-tool-check", sessionID',plugin)
        self.assertIn("guardPlannerToolBoundary(directory, event, output);",plugin)


class FinalTestLeafContextTests(unittest.TestCase):
    def test_final_test_packet_contains_shared_schema_and_verify_dependency_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            ctrl = project / ".opencode-v2"
            ctrl.mkdir()
            (ctrl / "ACCEPTANCE.md").write_text(
                "Reference policy: internal\n\n"
                "- [ ] A020: final unittest command passes.\n"
            )
            manifest = {
                "leaves": {
                    "D007": {
                        "owned_artifact_paths": [".opencode-v2/probes/stdlib_check.py"],
                        "verify_deps": [],
                        "acceptance_ids": ["A019"],
                    },
                    "D008": {
                        "owned_artifact_paths": [".opencode-v2/TEST_CHECKS.json"],
                        "verify_deps": ["D007"],
                        "acceptance_ids": ["A020"],
                        "verify_command": ".opencode-v2/bin/run-checks",
                    },
                }
            }
            contexts = control_query_views.build_leaf_contexts(project, manifest)
            final = contexts["D008"]
            self.assertEqual(
                final["test_checks_contract"]["schema"],
                control_query_views.TEST_CHECKS_SCHEMA,
            )
            self.assertEqual(
                final["test_checks_contract"]["runner"],
                ".opencode-v2/bin/run-checks",
            )
            self.assertEqual(
                final["verify_dependency_artifacts"]["D007"],
                [".opencode-v2/probes/stdlib_check.py"],
            )
            self.assertNotIn("test_checks_contract", contexts["D007"])


class DirectOwnedWritePromptTests(unittest.TestCase):
    def test_write_capable_roles_match_runtime_direct_write_gate(self):
        agents = (
            "implementer",
            "core-builder",
            "feature-builder",
            "reasoning-builder",
            "integrator",
            "test-builder",
        )
        root = Path(__file__).resolve().parent.parent / "xdg/config/opencode/agents"
        for agent in agents:
            with self.subTest(agent=agent):
                role = (root / f"{agent}.md").read_text()
                self.assertIn("Runtime direct-owned-write gate", role)
                self.assertIn(
                    "the next\ntool call MUST directly create or update a declared owned artifact",
                    role,
                )
                self.assertIn("A no-op rewrite of identical\ncontent does not satisfy this gate", role)


    def test_runtime_prompt_forbids_manual_sandbox_wrapping(self):
        manifest={
            "leaves":{
                "D001":{
                    "owned_artifact_paths":["owned.txt"],
                    "owned_artifacts":"`owned.txt`",
                    "complexity":"S",
                }
            }
        }
        with mock.patch.object(supervisor,"load_manifest",return_value=manifest):
            prompt=supervisor.implementation_runtime_prompt(
                "D001","implementer"
            )
        self.assertIn(
            "Never invoke worker_sandbox.py, run-bash, bubblewrap, or any sandbox wrapper",
            prompt,
        )
        self.assertIn("the runtime wraps bash automatically",prompt)

    def test_split_child_prompt_applies_parent_correction_before_child_correction(self):
        prompt=supervisor.implementation_prompt("D010-B")
        self.assertIn("parent_supervisor_execution_correction",prompt)
        self.assertIn("supervisor_execution_correction",prompt)
        self.assertIn("child-specific correction",prompt)
        self.assertIn(
            "Neither correction may change ownership, Verify, dependencies",
            prompt,
        )

    def test_progress_handoff_prompt_requires_symlink_following_discovery(self):
        manifest={
            "leaves":{
                "D010-A":{
                    "owned_artifact_paths":[],
                    "owned_artifacts":"none",
                    "complexity":"M",
                    "split_handoff_only":True,
                }
            }
        }
        with mock.patch.object(supervisor,"load_manifest",return_value=manifest):
            prompt=supervisor.implementation_runtime_prompt(
                "D010-A","probe-builder"
            )
        self.assertIn("SANDBOX DISCOVERY RULE",prompt)
        self.assertIn("multiple independent read/grep calls in parallel",prompt)
        self.assertIn("find -L",prompt)
        self.assertIn(
            "recursive find/glob that does not follow symlinks is NOT evidence",
            prompt,
        )

    def test_runtime_prompt_requires_small_parseable_first_write(self):
        manifest={
            "leaves":{
                "D010":{
                    "owned_artifact_paths":["reference/reference.py"],
                    "owned_artifacts":"`reference/reference.py`",
                    "complexity":"M",
                }
            }
        }
        with mock.patch.object(supervisor,"load_manifest",return_value=manifest):
            prompt=supervisor.implementation_runtime_prompt(
                "D010","core-builder"
            )
        self.assertIn("Start with a SMALL, parseable, contract-shaped artifact",prompt)
        self.assertIn("minimal executable/exportable skeleton",prompt)
        self.assertIn("repair it with SMALL TARGETED EDITS",prompt)
        self.assertIn("do not replace the whole file with one large write",prompt)
        self.assertIn("continue with bounded edits rather than monolithic",prompt)
        self.assertIn("bounded edits rather than monolithic rewrites",prompt)
        self.assertLessEqual(
            len(prompt),supervisor.MAX_IMPLEMENTATION_PROMPT_CHARS
        )


class WorkerSandboxPythonSideEffectTests(unittest.TestCase):
    def test_worker_and_verify_disable_python_bytecode_side_effects(self):
        source = (
            Path(__file__).resolve().parent / "worker_sandbox.py"
        ).read_text()
        self.assertGreaterEqual(
            source.count('"--setenv","PYTHONDONTWRITEBYTECODE","1"'),
            2,
        )

    def test_manual_sandbox_wrapper_is_denied_but_exact_reflection_is_unwrapped(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/".opencode-v2/work").mkdir(parents=True)
            ctx={
                "worker":True,
                "did":"D001",
                "session":"ses-1",
                "agent":"implementer",
                "owned":["owned.txt"],
            }
            malformed=(
                "python3 '/tmp/worker_sandbox.py' 'run-bash' "
                "'--project' '/tmp/project"
            )
            with mock.patch.object(
                worker_sandbox,"resolve_worker",return_value=ctx
            ):
                with self.assertRaisesRegex(
                    worker_sandbox.SandboxError,
                    "manual sandbox wrapper forbidden",
                ):
                    worker_sandbox.hook_guard(
                        project,"ses-1","call-1","implementer","bash",
                        {"command":malformed},
                    )

                ordinary=worker_sandbox.hook_guard(
                    project,"ses-1","call-2","implementer","bash",
                    {"command":"printf ok"},
                )
                self.assertEqual(ordinary["action"],"replace-bash")

                reflected=worker_sandbox.replacement_command(
                    project,ctx,"printf ok"
                )
                collapsed=worker_sandbox.hook_guard(
                    project,"ses-1","call-3","implementer","bash",
                    {"command":reflected},
                )
                self.assertEqual(collapsed["action"],"replace-bash")
                self.assertEqual(
                    collapsed["command"],
                    worker_sandbox.replacement_command(project,ctx,"printf ok"),
                )

            violation=worker_sandbox.violation_path(
                project,"D001","ses-1"
            )
            rows=[
                json.loads(line)
                for line in violation.read_text().splitlines()
                if line.strip()
            ]
            self.assertEqual(rows[-1]["kind"],"manual-sandbox-wrapper")
            self.assertFalse(
                worker_sandbox.has_fatal_violation(
                    project,"D001","ses-1"
                )
            )


class WorkerSandboxPluginHistoryTests(unittest.TestCase):
    def test_plugin_sanitizes_persisted_wrapper_before_model_history(self):
        plugin=(
            Path(__file__).resolve().parent.parent
            / "xdg/config/opencode/plugins/v2-bounded-subagent.js"
        ).read_text()
        transform=plugin.index(
            '"experimental.chat.messages.transform": async (_input, output) => {'
        )
        sanitize=plugin.index(
            'sanitizeSandboxHistoryForModel(output?.messages, directory);',
            transform,
        )
        before=plugin.index('"tool.execute.before": async (event, output) => {')
        self.assertLess(transform,sanitize)
        self.assertLess(sanitize,before)
        self.assertIn(
            'function unwrapPersistedSandboxCommand(command, directory)',
            plugin,
        )
        self.assertIn(
            'const original = unwrapPersistedSandboxCommand(input.command, directory);',
            plugin,
        )
        self.assertIn(
            'state.input = serialized ? JSON.stringify(input) : input;',
            plugin,
        )
        self.assertNotIn('captureOriginalSandboxCommand(event, output);',plugin)
        self.assertNotIn('restoreOriginalSandboxCommand(event, output);',plugin)


class PlannerRetryGenerationTests(unittest.TestCase):
    def test_infrastructure_refund_advances_generation_without_raising_failure_count(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            path=work/"planner-restarts.json"
            path.write_text(json.dumps({
                "owner":"supervisor",
                "count":2,
                "counted_sessions":["p1","p2"],
            }))
            self.assertEqual(controller.planner_dispatch_generation(project),2)
            path.write_text(json.dumps({
                "owner":"supervisor",
                "count":2,
                "counted_sessions":["p1","p2"],
                "infrastructure_recoveries":[{
                    "session":"p3",
                    "source":"operator-controller",
                    "reason":"proven harness defect",
                    "timestamp":"2026-09-26T16:34:14Z",
                }],
            }))
            self.assertEqual(controller.planner_dispatch_generation(project),3)

    def test_refund_generation_changes_planner_execution_id_at_same_decision(self):
        action={"kind":"launch","agent":"implementation-planner","mode":"repair"}
        old=controller.execution_action_id("same-state","ses-root",action,2)
        replay=controller.execution_action_id("same-state","ses-root",action,2)
        recovered=controller.execution_action_id("same-state","ses-root",action,3)
        self.assertEqual(old,replay)
        self.assertNotEqual(old,recovered)


class ImplementationRetryGenerationTests(unittest.TestCase):
    def test_preclaim_does_not_advance_generation_but_terminal_failure_does(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            work = project / ".opencode-v2/work"
            work.mkdir(parents=True)
            ledger = {
                "owner": "supervisor",
                "protocol": "v2-attempt-ledger-v1",
                "deliverables": {
                    "D001": {
                        "count": 1,
                        "sessions": ["ses-active"],
                        "automatic_limit": 2,
                    }
                },
            }
            (work / "attempts.json").write_text(json.dumps(ledger))
            self.assertEqual(controller.attempt_failure_generation(project, "D001"), 0)

            ledger["deliverables"]["D001"]["failure_history"] = [
                {
                    "attempt": 1,
                    "classification": "genuine",
                    "reason": "verify-failed-1",
                }
            ]
            (work / "attempts.json").write_text(json.dumps(ledger))
            self.assertEqual(controller.attempt_failure_generation(project, "D001"), 1)

    def test_plan_contract_revision_advances_generation_without_failure_row(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            work=project/".opencode-v2/work"
            work.mkdir(parents=True)
            ledger={
                "owner":"supervisor",
                "protocol":"v2-attempt-ledger-v1",
                "deliverables":{
                    "D001":{
                        "count":1,
                        "sessions":["ses-contract"],
                        "automatic_limit":2,
                        "plan_contract_revisions":[{
                            "attempt":1,
                            "source":"supervisor-plan-contract-revision",
                            "previous_verify_sha256":"a"*64,
                            "current_verify_sha256":"b"*64,
                            "previous_result":"verify-failed-1",
                        }],
                    }
                },
            }
            (work/"attempts.json").write_text(json.dumps(ledger))
            self.assertEqual(
                controller.attempt_failure_generation(project,"D001"),1
            )

    def test_retry_generation_changes_execution_id_without_changing_decision(self):
        action = {"kind": "launch", "agent": "implementer", "deliverable": "D001"}
        first = controller.execution_action_id("same-state", "ses-root", action, 0)
        replay = controller.execution_action_id("same-state", "ses-root", action, 0)
        retry = controller.execution_action_id("same-state", "ses-root", action, 1)
        self.assertEqual(first, replay)
        self.assertNotEqual(first, retry)


class ImplementationLogicalDispatchIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.work=self.project/".opencode-v2/work"
        self.work.mkdir(parents=True)
        self.root="ses-root"
        self.runtime_contract_patch=mock.patch.object(
            controller,"require_current_runtime_contract",return_value={}
        )
        self.runtime_contract_patch.start()
        self.action={"kind":"launch","agent":"feature-builder","deliverable":"D001"}
        self.result={"state_version":"state-b","actions":[self.action]}
        self.prior_id=controller.execution_action_id(
            "state-a",self.root,self.action,0
        )
        controller.save_execution_ledger(self.project,{
            "owner":"stage-a-controller",
            "protocol":controller.EXECUTION_LEDGER_PROTOCOL,
            "executions":{
                self.prior_id:{
                    "execution_id":self.prior_id,
                    "state_version":"state-a",
                    "root_session":self.root,
                    "action":self.action,
                    "dispatch_generation":0,
                    "transport":"prompt_async+SubtaskPart",
                    "transport_may_have_been_attempted":True,
                    "created_at_ms":1000,
                    "baseline_attempt":{"count":0,"sessions":[]},
                    "baseline_child_ids":[],
                }
            },
        })

    def tearDown(self):
        self.runtime_contract_patch.stop()
        self.tmp.cleanup()

    def _write_attempts(self,count,sessions):
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{"automatic_limit":2,"count":count,"sessions":sessions}
            },
        }))

    def test_state_version_churn_reconciles_prior_logical_dispatch(self):
        self._write_attempts(1,["ses-child"])
        child={"id":"ses-child","parentID":self.root,"agent":"feature-builder"}
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=self.result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(controller,"http_json") as http:
            receipt=controller.execute_first_implementation(
                self.project,"http://127.0.0.1:1",self.result,self.root
            )
        self.assertTrue(receipt["replay_suppressed"])
        self.assertEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(
            receipt["reconciliation"],
            {"kind":"bound-native-child","sessions":["ses-child"]},
        )
        http.assert_not_called()
        self.assertEqual(
            len(controller.load_execution_ledger(self.project)["executions"]),1
        )

    def test_unmaterialized_prior_logical_dispatch_waits_without_post(self):
        self._write_attempts(0,[])
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=self.result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[]
        ), mock.patch.object(controller,"http_json") as http:
            with self.assertRaisesRegex(
                controller.ControllerError,
                "LOGICAL_IMPLEMENTATION_DISPATCH_SETTLING",
            ):
                controller.execute_first_implementation(
                    self.project,"http://127.0.0.1:1",self.result,self.root
                )
        http.assert_not_called()
        self.assertEqual(
            len(controller.load_execution_ledger(self.project)["executions"]),1
        )

    def test_reusable_reservation_reposts_after_terminal_zero_work_orphan(self):
        self._write_attempts(1,["dispatch:old"])
        result={
            **self.result,
            "scheduler":{
                "replayable_reserved_deliverables":["D001"],
            },
        }
        child={
            "id":"ses-zero",
            "parentID":self.root,
            "agent":"feature-builder",
            "tokens":{"input":0,"output":0,"reasoning":0},
            "summary":{"additions":0,"deletions":0,"files":0},
        }
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(
            controller,"session_is_active",return_value=False
        ), mock.patch.object(
            controller,"ensure_root_idle",return_value=None
        ), mock.patch.object(
            controller,"http_json",return_value=(204,{})
        ) as http:
            receipt=controller.execute_first_implementation(
                self.project,"http://127.0.0.1:1",result,self.root
            )
        self.assertFalse(receipt["replay_suppressed"])
        self.assertNotEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(http.call_count,1)
        ledger=controller.load_execution_ledger(self.project)
        self.assertEqual(len(ledger["executions"]),2)
        prior=ledger["executions"][self.prior_id]
        self.assertEqual(
            prior["zero_work_orphan_replays"][-1]["sessions"],
            ["ses-zero"],
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        self.assertEqual(attempts["deliverables"]["D001"]["count"],1)
        self.assertEqual(
            attempts["deliverables"]["D001"]["sessions"],
            ["dispatch:old"],
        )

    def test_prior_execution_id_reposts_supervisor_superseded_child(self):
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":2,
                    "count":1,
                    "sessions":["dispatch:rearmed"],
                    "unmaterialized_dispatch_history":[{
                        "sequence":1,
                        "replaced":"ses-superseded",
                        "replacement":"dispatch:rearmed",
                        "source":"supervisor-test-recovery",
                        "timestamp":"2026-09-26T00:00:00Z",
                    }],
                }
            },
        }))
        result={
            **self.result,
            "scheduler":{
                "replayable_reserved_deliverables":["D001"],
            },
        }
        child={
            "id":"ses-superseded",
            "parentID":self.root,
            "agent":"feature-builder",
            "tokens":{"input":100,"output":20,"reasoning":0},
            "summary":{"additions":2,"deletions":0,"files":1},
        }
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(
            controller,"session_is_active",return_value=False
        ), mock.patch.object(
            controller,"ensure_root_idle",return_value=None
        ), mock.patch.object(
            controller,"http_json",return_value=(204,{})
        ) as http:
            receipt=controller.execute_first_implementation(
                self.project,"http://127.0.0.1:1",result,self.root
            )
        self.assertFalse(receipt["replay_suppressed"])
        self.assertNotEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(http.call_count,1)
        ledger=controller.load_execution_ledger(self.project)
        prior=ledger["executions"][self.prior_id]
        self.assertEqual(
            prior["superseded_child_replays"][-1]["sessions"],
            ["ses-superseded"],
        )
        self.assertEqual(
            prior["superseded_child_replays"][-1]["reason"],
            "reusable-reservation-supervisor-superseded-child",
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        self.assertEqual(
            attempts["deliverables"]["D001"]["sessions"],
            ["dispatch:rearmed"],
        )

    def test_same_execution_id_reposts_replayable_zero_work_orphan(self):
        self._write_attempts(1,["dispatch:old"])
        result={
            "state_version":"state-a",
            "actions":[self.action],
            "scheduler":{
                "replayable_reserved_deliverables":["D001"],
            },
        }
        child={
            "id":"ses-zero",
            "parentID":self.root,
            "agent":"feature-builder",
            "tokens":{"input":0,"output":0,"reasoning":0},
            "summary":{"additions":0,"deletions":0,"files":0},
        }
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(
            controller,"session_is_active",return_value=False
        ), mock.patch.object(
            controller,"ensure_root_idle",return_value=None
        ), mock.patch.object(
            controller,"http_json",return_value=(204,{})
        ) as http:
            receipt=controller.execute_first_implementation(
                self.project,"http://127.0.0.1:1",result,self.root
            )
        self.assertFalse(receipt["replay_suppressed"])
        self.assertEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(http.call_count,1)
        ledger=controller.load_execution_ledger(self.project)
        self.assertEqual(len(ledger["executions"]),1)
        current=ledger["executions"][self.prior_id]
        self.assertEqual(
            current["zero_work_orphan_replays"][-1]["sessions"],
            ["ses-zero"],
        )
        self.assertEqual(
            current["zero_work_orphan_replays"][-1]["reason"],
            "same-execution-reusable-reservation-terminal-zero-work-child",
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        self.assertEqual(
            attempts["deliverables"]["D001"]["sessions"],
            ["dispatch:old"],
        )

    def test_same_execution_id_reposts_supervisor_superseded_child(self):
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":2,
                    "count":1,
                    "sessions":["dispatch:rearmed"],
                    "unmaterialized_dispatch_history":[{
                        "sequence":1,
                        "replaced":"ses-superseded",
                        "replacement":"dispatch:rearmed",
                        "source":"supervisor-test-recovery",
                        "timestamp":"2026-09-26T00:00:00Z",
                    }],
                }
            },
        }))
        result={
            "state_version":"state-a",
            "actions":[self.action],
            "scheduler":{
                "replayable_reserved_deliverables":["D001"],
            },
        }
        child={
            "id":"ses-superseded",
            "parentID":self.root,
            "agent":"feature-builder",
            "tokens":{"input":100,"output":20,"reasoning":0},
            "summary":{"additions":2,"deletions":0,"files":1},
        }
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(
            controller,"session_is_active",return_value=False
        ), mock.patch.object(
            controller,"ensure_root_idle",return_value=None
        ), mock.patch.object(
            controller,"http_json",return_value=(204,{})
        ) as http:
            receipt=controller.execute_first_implementation(
                self.project,"http://127.0.0.1:1",result,self.root
            )
        self.assertFalse(receipt["replay_suppressed"])
        self.assertEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(http.call_count,1)
        ledger=controller.load_execution_ledger(self.project)
        self.assertEqual(len(ledger["executions"]),1)
        current=ledger["executions"][self.prior_id]
        self.assertEqual(
            current["superseded_child_replays"][-1]["sessions"],
            ["ses-superseded"],
        )
        self.assertEqual(
            current["superseded_child_replays"][-1]["reason"],
            "same-execution-reusable-reservation-supervisor-superseded-child",
        )
        attempts=json.loads((self.work/"attempts.json").read_text())
        self.assertEqual(
            attempts["deliverables"]["D001"]["sessions"],
            ["dispatch:rearmed"],
        )

    def test_terminal_native_attempt_does_not_suppress_next_authorized_launch(self):
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D001":{
                    "automatic_limit":2,
                    "count":1,
                    "sessions":["ses-terminal"],
                    "operator_retry_attempts":[{
                        "sequence":1,
                        "session":"ses-terminal",
                        "state":"infrastructure_abort",
                        "outcome":"infrastructure_abort",
                        "consumes_operator_grant":False,
                    }],
                }
            },
        }))
        child={
            "id":"ses-terminal",
            "parentID":self.root,
            "agent":"feature-builder",
            "tokens":{"input":0,"output":0,"reasoning":0},
            "summary":{"additions":0,"deletions":0,"files":0},
        }
        with mock.patch.object(
            controller,"canonical_implementation_prompt",return_value="prompt"
        ), mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=self.result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(
            controller,"ensure_root_idle",return_value=None
        ), mock.patch.object(
            controller,"http_json",return_value=(204,{})
        ) as http:
            receipt=controller.execute_first_implementation(
                self.project,"http://127.0.0.1:1",self.result,self.root
            )
        self.assertFalse(receipt["replay_suppressed"])
        self.assertNotEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(http.call_count,1)

    def test_evaluate_preserves_replayable_scheduler_evidence(self):
        decision={
            "state_version":"replay-state",
            "resume_phase":"execution",
            "eligible":["D001"],
            "eligible_roles":{"D001":"feature-builder"},
            "scheduler":{
                "active_workers":0,
                "available_worker_slots":0,
                "replayable_reserved_deliverables":["D001"],
            },
        }
        path=controller.decision_path(self.project)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(decision))
        result=controller.evaluate(self.project)
        self.assertEqual(
            result["actions"],
            [{"kind":"launch","agent":"feature-builder","deliverable":"D001"}],
        )
        self.assertEqual(
            result["scheduler"]["replayable_reserved_deliverables"],
            ["D001"],
        )

    def test_prior_intent_filter_excludes_older_completed_attempt(self):
        older=dict(
            controller.load_execution_ledger(self.project)["executions"][
                self.prior_id
            ]
        )
        older["baseline_attempt"]={"count":5,"sessions":[]}
        matches=controller.prior_logical_implementation_intents(
            {"old":older},
            "new",
            self.root,
            self.action,
            0,
            7,
        )
        self.assertEqual(matches,[])


class TaskSplitterLogicalDispatchIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.work=self.project/".opencode-v2/work"
        self.work.mkdir(parents=True)
        self.root="ses-root"
        self.action={
            "kind":"launch","agent":"task-splitter",
            "deliverable":"D007","generation":1,"claim":1,
        }
        self.result={"state_version":"state-b","actions":[self.action]}
        self.prior_id=controller.execution_action_id(
            "state-a",self.root,self.action
        )
        (self.work/"attempts.json").write_text(json.dumps({
            "owner":"supervisor",
            "deliverables":{
                "D007":{
                    "automatic_limit":2,
                    "count":2,
                    "sessions":["ses-one","ses-two"],
                }
            },
        }))
        controller.save_execution_ledger(self.project,{
            "owner":"stage-a-controller",
            "protocol":controller.EXECUTION_LEDGER_PROTOCOL,
            "executions":{
                self.prior_id:{
                    "execution_id":self.prior_id,
                    "state_version":"state-a",
                    "root_session":self.root,
                    "action":self.action,
                    "transport":"prompt_async+SubtaskPart",
                    "transport_may_have_been_attempted":True,
                    "created_at_ms":1000,
                    "baseline_attempt":{
                        "count":2,
                        "sessions":["ses-one","ses-two"],
                    },
                    "baseline_child_ids":[],
                }
            },
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_version_churn_reconciles_prior_splitter_child(self):
        child={
            "id":"ses-splitter",
            "parentID":self.root,
            "agent":"task-splitter",
        }
        with mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=self.result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[child]
        ), mock.patch.object(controller,"http_json") as http:
            receipt=controller.execute_first_task_splitter(
                self.project,"http://127.0.0.1:1",self.result,self.root
            )
        self.assertTrue(receipt["replay_suppressed"])
        self.assertEqual(receipt["execution_id"],self.prior_id)
        self.assertEqual(
            receipt["reconciliation"],
            {"kind":"native-child","sessions":["ses-splitter"]},
        )
        http.assert_not_called()
        self.assertEqual(
            len(controller.load_execution_ledger(self.project)["executions"]),1
        )

    def test_split_retryable_selector_advances_claim_ordinal(self):
        decision={
            "resume_phase":"recursive-split",
            "split_required":[{
                "deliverable":"D007",
                "split_state":"split-retryable",
                "split_generation":1,
                "split_claim_count":1,
            }],
            "eligible":[],
            "eligible_roles":{},
            "scheduler":{
                "available_worker_slots":0,
                "replayable_reserved_deliverables":[],
            },
        }
        self.assertEqual(
            deterministic_dispatch.select_actions(decision),
            [{
                "kind":"launch","agent":"task-splitter",
                "deliverable":"D007","generation":1,"claim":2,
            }],
        )

    def test_retry_claim_ordinal_is_a_distinct_logical_splitter_attempt(self):
        retry={
            "kind":"launch","agent":"task-splitter",
            "deliverable":"D007","generation":1,"claim":2,
        }
        retry_id=controller.execution_action_id(
            "state-b",self.root,retry
        )
        self.assertNotEqual(retry_id,self.prior_id)
        ledger=controller.load_execution_ledger(self.project)["executions"]
        self.assertEqual(
            controller.prior_logical_task_splitter_intents(
                ledger,retry_id,self.root,retry
            ),
            [],
        )

    def test_unmaterialized_prior_splitter_waits_without_second_post(self):
        with mock.patch.object(
            controller,"resolve_root_session",return_value=self.root
        ), mock.patch.object(
            controller,"evaluate",return_value=self.result
        ), mock.patch.object(
            controller,"child_snapshot",return_value=[]
        ), mock.patch.object(controller,"http_json") as http:
            with self.assertRaisesRegex(
                controller.ControllerError,
                "LOGICAL_TASK_SPLITTER_DISPATCH_SETTLING",
            ):
                controller.execute_first_task_splitter(
                    self.project,"http://127.0.0.1:1",self.result,self.root
                )
        http.assert_not_called()
        self.assertEqual(
            len(controller.load_execution_ledger(self.project)["executions"]),1
        )


class ExactProjectPathPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.sibling = self.base / "project-sibling"
        self.other = self.base / "other"
        for item in (self.project, self.sibling, self.other):
            item.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_relative_owned_path_allows(self):
        relative = path_permissions.normalize_tool_path(self.project, OWNED)
        self.assertEqual(relative, OWNED)
        self.assertTrue(path_permissions.permission_allows(RULES, relative))

    def test_absolute_equivalent_under_exact_project_allows(self):
        relative = path_permissions.normalize_tool_path(self.project, str(self.project / OWNED))
        self.assertEqual(relative, OWNED)
        self.assertTrue(path_permissions.permission_allows(RULES, relative))

    def test_absolute_unowned_path_under_project_denies(self):
        relative = path_permissions.normalize_tool_path(self.project, str(self.project / ".opencode-v2/OTHER.md"))
        self.assertFalse(path_permissions.permission_allows(RULES, relative))

    def test_parent_escape_denies(self):
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, "../other/owned.txt")

    def test_other_project_denies(self):
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, str(self.other / OWNED))

    def test_similarly_prefixed_sibling_denies(self):
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, str(self.sibling / OWNED))

    def test_symlink_escape_denies(self):
        (self.project / "escape").symlink_to(self.other, target_is_directory=True)
        with self.assertRaises(path_permissions.PathPermissionError):
            path_permissions.normalize_tool_path(self.project, "escape/owned.txt")

    def test_non_git_projection_is_worktree_relative_and_not_absolute(self):
        self.assertEqual(path_permissions.opencode_worktree(self.project), Path("/"))
        projected = path_permissions.effective_permission_pattern(self.project, Path("/"), OWNED)
        self.assertEqual(projected, str(self.project / OWNED).lstrip("/"))
        self.assertFalse(projected.startswith("/"))

    def test_git_and_nested_git_projection(self):
        git_root = self.base / "git-root"
        git_root.mkdir()
        subprocess.run(["git", "init", "-q", str(git_root)], check=True)
        self.assertEqual(path_permissions.opencode_worktree(git_root), git_root.resolve())
        self.assertEqual(
            path_permissions.effective_permission_pattern(git_root, git_root, OWNED), OWNED,
        )
        nested = git_root / "nested-project"
        nested.mkdir()
        self.assertEqual(path_permissions.opencode_worktree(nested), git_root.resolve())
        self.assertEqual(
            path_permissions.effective_permission_pattern(nested, git_root, OWNED),
            f"nested-project/{OWNED}",
        )

    def test_projection_does_not_broaden_owned_pattern(self):
        agent = '---\npermission:\n  edit:\n    "*": deny\n    ".opencode-v2/ACCEPTANCE.md": allow\n---\n'
        rendered = path_permissions.render_agent_with_projection(agent, self.project, Path("/"))
        self.assertIn(str(self.project / OWNED).lstrip("/"), rendered)
        self.assertNotIn(str(self.project / OWNED), rendered)
        self.assertNotIn(str(self.sibling / OWNED).lstrip("/"), rendered)


class AcceptanceRepairPromptTests(unittest.TestCase):
    def test_repair_prompt_names_canonical_guard_error_file(self):
        part=controller.build_semantic_subtask({
            "kind":"launch","agent":"acceptance-planner","mode":"repair",
        })
        prompt=part["prompt"]
        self.assertIn(".opencode-v2/ACCEPTANCE.guard-errors.txt",prompt)
        self.assertIn("do not guess alternate guard-error names",prompt)
        self.assertNotIn("and any\nguard-error artifact",prompt)


class ImplementationPlannerRepairPromptTests(unittest.TestCase):
    def test_python_exception_verify_uses_one_line_stdlib_pattern(self):
        prompt=controller.PLANNER_PROMPTS["repair"]
        self.assertIn("unittest.TestCase().assertRaises",prompt)
        self.assertIn("semicolon-compressed try/except",prompt)

    def test_planner_prompts_forbid_human_summary_as_incidental_contract(self):
        for mode in ("fresh","repair"):
            prompt=controller.PLANNER_PROMPTS[mode]
            normalized=" ".join(prompt.split())
            self.assertIn("human-readable",normalized)
            self.assertIn("exit status",normalized)
            self.assertIn("Done-when explicitly requires",normalized)


class TerminalSemanticChildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.root = "ses-root"
        self.action = {"kind": "launch", "agent": "acceptance-planner", "mode": "fresh"}
        self.result = {"state_version": "state-a", "actions": [self.action]}
        self.execution_id = controller.execution_action_id("state-a", self.root, self.action)
        semantic_part = controller.build_semantic_subtask(self.action)
        self.semantic_prompt_sha256 = hashlib.sha256(
            semantic_part["prompt"].encode("utf-8")
        ).hexdigest()
        controller.save_execution_ledger(self.project, {
            "owner": "stage-a-controller",
            "protocol": controller.EXECUTION_LEDGER_PROTOCOL,
            "executions": {
                self.execution_id: {
                    "execution_id": self.execution_id,
                    "state_version": "state-a",
                    "root_session": self.root,
                    "action": self.action,
                    "semantic_prompt_sha256": self.semantic_prompt_sha256,
                    "created_at_ms": int((time.time() - 30) * 1000),
                    "baseline_child_ids": [],
                }
            },
        })
        self.saved = {
            "evaluate": controller.evaluate,
            "resolve_root_session": controller.resolve_root_session,
            "child_snapshot": controller.child_snapshot,
            "session_is_active": controller.session_is_active,
        }
        controller.evaluate = lambda project: self.result
        controller.resolve_root_session = lambda project, base_url, explicit: self.root
        controller.child_snapshot = lambda project, base_url, root: [
            {"id": "ses-terminal", "parentID": self.root, "agent": "acceptance-planner"}
        ]

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(controller, name, value)
        self.temp.cleanup()

    def test_active_semantic_child_is_reconciled_without_second_dispatch(self):
        controller.session_is_active = lambda project, base_url, sid: True
        receipt = controller.execute_first_semantic(self.project, "http://127.0.0.1:1", self.result, self.root)
        self.assertTrue(receipt["replay_suppressed"])
        self.assertEqual(receipt["reconciliation"]["sessions"], ["ses-terminal"])

    def test_terminal_semantic_child_fails_closed_without_post(self):
        controller.session_is_active = lambda project, base_url, sid: False
        with self.assertRaisesRegex(controller.ControllerError, "SEMANTIC_CHILD_TERMINATED_WITHOUT_STATE_TRANSITION"):
            controller.execute_first_semantic(self.project, "http://127.0.0.1:1", self.result, self.root)

    def test_terminal_child_within_grace_period_remains_reconcilable(self):
        ledger = controller.load_execution_ledger(self.project)
        ledger["executions"][self.execution_id]["created_at_ms"] = int(time.time() * 1000)
        controller.save_execution_ledger(self.project, ledger)
        controller.session_is_active = lambda project, base_url, sid: False
        receipt = controller.execute_first_semantic(self.project, "http://127.0.0.1:1", self.result, self.root)
        self.assertTrue(receipt["replay_suppressed"])

    def test_long_running_child_gets_grace_from_native_terminal_update(self):
        controller.child_snapshot = lambda project, base_url, root: [{
            "id":"ses-terminal",
            "parentID":self.root,
            "agent":"acceptance-planner",
            "time":{"updated":int(time.time()*1000)},
        }]
        controller.session_is_active = lambda project, base_url, sid: False
        receipt=controller.execute_first_semantic(
            self.project,"http://127.0.0.1:1",self.result,self.root
        )
        self.assertTrue(receipt["replay_suppressed"])
        self.assertGreater(
            int(receipt["reconciliation"]["child_updated_at_ms"]),0
        )


class FinalAcceptanceIndependentExecutionTests(unittest.TestCase):
    def load_finalizer(self):
        path=Path(__file__).with_name("finalize-acceptance.py")
        spec=importlib.util.spec_from_file_location("finalize_acceptance_test",path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def make_project(self):
        td=tempfile.TemporaryDirectory()
        project=Path(td.name)
        ctrl=project/".opencode-v2"
        ctrl.mkdir(parents=True)
        (ctrl/"ACCEPTANCE.md").write_text(
            "# Acceptance Contract\n"
            "## MUST checks\n"
            "- [ ] A001: command must actually succeed.\n"
            "<!-- ACCEPTANCE_COMPLETE -->\n"
        )
        command="test -e .opencode-v2/ACCEPTANCE.md"
        (ctrl/"acceptance-report.json").write_text(json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"PASS",
            "checks":[{
                "id":"A001",
                "status":"PASS",
                "evidence":"validator claims the command succeeded",
                "required_executable":True,
                "command":command,
                "exit_code":0,
            }],
        }))
        (ctrl/"TEST_REPORT.json").write_text(json.dumps({
            "protocol":"v2-test-report-v1",
            "status":"pass",
            "checks_run":1,
            "checks_passed":1,
            "missing_required_files":[],
            "checks":[{
                "name":"gate",
                "command":command,
                "exit_code":0,
                "timed_out":False,
            }],
        }))
        return td,project

    def test_finalizer_does_not_trust_validator_reported_exit_code(self):
        finalizer=self.load_finalizer()
        td,project=self.make_project()
        try:
            with mock.patch.object(finalizer,"run_validator_bash",return_value=1):
                with self.assertRaisesRegex(
                    RuntimeError,"gate-executable-exit-1"
                ):
                    finalizer.finalize(project)
            self.assertFalse(
                (project/".opencode-v2/acceptance-pass.json").exists()
            )
        finally:
            td.cleanup()


class SemanticInfrastructureRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.root = "ses-root"
        self.action = {
            "kind": "launch",
            "agent": "acceptance-validator",
            "mode": "final",
        }
        self.result = {"state_version": "state-a", "actions": [self.action]}
        self.execution_id = controller.execution_action_id(
            "state-a", self.root, self.action
        )
        controller.save_execution_ledger(self.project, {
            "owner": "stage-a-controller",
            "protocol": controller.EXECUTION_LEDGER_PROTOCOL,
            "executions": {
                self.execution_id: {
                    "execution_id": self.execution_id,
                    "state_version": "state-a",
                    "root_session": self.root,
                    "action": self.action,
                    "semantic_generation": 0,
                    "created_at_ms": int((time.time() - 30) * 1000),
                    "baseline_child_ids": [],
                }
            },
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_terminal_semantic_child_can_receive_one_auditable_infrastructure_retry(self):
        child = {
            "id": "ses-validator-old",
            "parentID": self.root,
            "agent": "acceptance-validator",
        }
        with mock.patch.object(controller, "evaluate", return_value=self.result), \
             mock.patch.object(controller, "child_snapshot", return_value=[child]), \
             mock.patch.object(controller, "semantic_child_may_still_transition", return_value=False):
            receipt = controller.authorize_semantic_infrastructure_retry(
                self.project,
                "http://127.0.0.1:1",
                self.execution_id,
                "validator scratch was read-only",
            )
        self.assertEqual(receipt["next_generation"], 1)
        self.assertFalse(receipt["idempotent"])
        ledger = controller.load_execution_ledger(self.project)
        grant = ledger["executions"][self.execution_id]["semantic_infrastructure_retry"]
        self.assertEqual(grant["source"], "operator-controller")
        self.assertEqual(grant["child_sessions"], ["ses-validator-old"])
        generation, next_id = controller.semantic_execution_slot(
            ledger, "state-a", self.root, self.action
        )
        self.assertEqual(generation, 1)
        self.assertEqual(next_id, receipt["next_execution_id"])
        self.assertNotEqual(next_id, self.execution_id)

    def test_final_acceptance_prompt_hash_changes_semantic_execution_identity(self):
        token_a=controller.semantic_prompt_work_token(self.action,"a"*64)
        token_b=controller.semantic_prompt_work_token(self.action,"b"*64)
        self.assertNotEqual(token_a,token_b)
        id_a=controller.execution_action_id(
            "same-state",self.root,self.action,None,token_a,None
        )
        id_b=controller.execution_action_id(
            "same-state",self.root,self.action,None,token_b,None
        )
        self.assertNotEqual(id_a,id_b)
        self.assertEqual(
            controller.semantic_prompt_work_token(
                {"kind":"launch","agent":"reference-researcher","mode":"validation"},
                "c"*64,
            ),
            "",
        )

    def test_current_acceptance_work_token_reconstructs_prompt_bound_identity(self):
        prompt="canonical acceptance packet"
        expected=controller.semantic_prompt_work_token(
            self.action,hashlib.sha256(prompt.encode()).hexdigest()
        )
        with mock.patch.object(
            controller,"build_semantic_subtask",
            return_value={"prompt":prompt},
        ):
            self.assertEqual(
                controller.current_semantic_work_token(
                    self.project,self.action
                ),
                expected,
            )

    def test_acceptance_retry_uses_prompt_bound_current_work_token(self):
        prompt="canonical acceptance packet"
        token=controller.semantic_prompt_work_token(
            self.action,hashlib.sha256(prompt.encode()).hexdigest()
        )
        execution_id=controller.execution_action_id(
            "state-a",self.root,self.action,None,token,None
        )
        controller.save_execution_ledger(self.project,{
            "owner":"stage-a-controller",
            "protocol":controller.EXECUTION_LEDGER_PROTOCOL,
            "executions":{
                execution_id:{
                    "execution_id":execution_id,
                    "state_version":"state-a",
                    "root_session":self.root,
                    "action":self.action,
                    "semantic_generation":0,
                    "semantic_work_token":token,
                    "created_at_ms":int((time.time()-30)*1000),
                    "baseline_child_ids":[],
                }
            },
        })
        child={
            "id":"ses-validator-old",
            "parentID":self.root,
            "agent":"acceptance-validator",
        }
        with mock.patch.object(controller,"evaluate",return_value=self.result), \
             mock.patch.object(controller,"child_snapshot",return_value=[child]), \
             mock.patch.object(
                 controller,"semantic_child_may_still_transition",
                 return_value=False,
             ), \
             mock.patch.object(
                 controller,"current_semantic_work_token",
                 return_value=token,
             ):
            receipt=controller.authorize_semantic_infrastructure_retry(
                self.project,
                "http://127.0.0.1:1",
                execution_id,
                "validator accepted malformed executable evidence",
            )
        self.assertEqual(receipt["next_generation"],1)
        self.assertFalse(receipt["idempotent"])

    def test_reference_validation_item_changes_semantic_execution_identity(self):
        action={"kind":"launch","agent":"reference-researcher","mode":"validation"}
        work=self.project/".opencode-v2/acceptance/reference-work.json"
        work.parent.mkdir(parents=True)
        work.write_text(json.dumps({
            "mode":"validation","status":"complete",
            "last_completed_item":"V1-frozen-de-fetch",
            "next_item":"V2-frozen-horizons-vectors",
        }))
        token=controller.semantic_work_item_token(self.project,action)
        self.assertEqual(token,"reference-validation:V2-frozen-horizons-vectors")
        legacy_id=controller.execution_action_id("state-a",self.root,action)
        ledger={"executions":{
            legacy_id:{
                "execution_id":legacy_id,"state_version":"state-a",
                "root_session":self.root,"action":action,"semantic_generation":0,
            }
        }}
        generation,new_id=controller.semantic_execution_slot(
            ledger,"state-a",self.root,action,token
        )
        self.assertEqual(generation,0)
        self.assertNotEqual(new_id,legacy_id)

    def test_reference_validation_attempt_slot_advances_only_with_gate_attempts(self):
        action={"kind":"launch","agent":"reference-researcher","mode":"validation"}
        ctrl=self.project/".opencode-v2"
        work=ctrl/"acceptance/reference-work.json"
        work.parent.mkdir(parents=True)
        work.write_text(json.dumps({
            "mode":"validation","status":"complete",
            "last_completed_item":"V1-frozen-de-fetch",
            "next_item":"V2-frozen-horizons-vectors",
        }))
        gate=ctrl/"reference-validation-gate.json"
        gate.write_text(json.dumps({
            "owner":"supervisor","phase":"validation","state":"pending",
            "attempts":2,"max_attempts":8,"active_sessions":[],
        }))
        token=controller.semantic_work_item_token(self.project,action)
        slot=controller.semantic_validation_attempt_slot(self.project,action)
        self.assertEqual(slot,3)
        execution_id=controller.execution_action_id(
            "state-a",self.root,action,None,token,slot
        )
        ledger={"executions":{
            execution_id:{
                "execution_id":execution_id,
                "state_version":"state-a",
                "root_session":self.root,
                "action":action,
                "semantic_generation":0,
                "semantic_work_token":token,
                "semantic_attempt_slot":slot,
            }
        }}
        generation,same_id=controller.semantic_execution_slot(
            ledger,"state-a",self.root,action,token,slot
        )
        self.assertEqual(generation,0)
        self.assertEqual(same_id,execution_id)

        # Active children are excluded from the completed-attempt counter, so
        # the same gate count remains the same idempotency slot.
        gate.write_text(json.dumps({
            "owner":"supervisor","phase":"validation","state":"pending",
            "attempts":2,"max_attempts":8,"active_sessions":["ses-live"],
        }))
        self.assertEqual(
            controller.semantic_validation_attempt_slot(self.project,action),3
        )

        # Once that child becomes terminal the supervisor increments attempts;
        # the next semantic launch gets a fresh ordinary-validation slot.
        gate.write_text(json.dumps({
            "owner":"supervisor","phase":"validation","state":"pending",
            "attempts":3,"max_attempts":8,"active_sessions":[],
        }))
        next_slot=controller.semantic_validation_attempt_slot(self.project,action)
        self.assertEqual(next_slot,4)
        next_generation,next_id=controller.semantic_execution_slot(
            ledger,"state-a",self.root,action,token,next_slot
        )
        self.assertEqual(next_generation,0)
        self.assertNotEqual(next_id,execution_id)

    def _blocked_reference_retry_fixture(self,last_session="ses-reference-length"):
        action={"kind":"launch","agent":"reference-researcher","mode":"validation"}
        ctrl=self.project/".opencode-v2"
        work=ctrl/"acceptance/reference-work.json"
        work.parent.mkdir(parents=True,exist_ok=True)
        work.write_text(json.dumps({
            "mode":"validation","status":"complete",
            "last_completed_item":"V1-frozen-de-fetch",
            "next_item":"V2-frozen-horizons-vectors",
        }))
        gate=ctrl/"reference-validation-gate.json"
        gate.write_text(json.dumps({
            "owner":"supervisor","phase":"validation","state":"blocked",
            "attempts":3,"max_attempts":8,"productive_sessions":1,
            "stagnant_tail":2,"active_sessions":[],
            "session_ids":["ses-v1","ses-v2-stagnant",last_session],
        }))
        token=controller.semantic_work_item_token(self.project,action)
        slot=3
        execution_id=controller.execution_action_id(
            "state-a",self.root,action,None,token,slot
        )
        controller.save_execution_ledger(self.project,{
            "owner":"stage-a-controller",
            "protocol":controller.EXECUTION_LEDGER_PROTOCOL,
            "executions":{
                execution_id:{
                    "execution_id":execution_id,
                    "state_version":"state-a",
                    "root_session":self.root,
                    "action":action,
                    "semantic_generation":0,
                    "semantic_work_token":token,
                    "semantic_attempt_slot":slot,
                    "created_at_ms":int((time.time()-30)*1000),
                    "baseline_child_ids":[],
                }
            },
        })
        blocked={
            "state_version":"state-blocked",
            "resume_phase":"acceptance-validation",
            "actions":[{"kind":"blocked","reason":"reference-validation-blocked"}],
        }
        child={
            "id":"ses-reference-length",
            "parentID":self.root,
            "agent":"reference-researcher",
        }
        return action,execution_id,blocked,child

    def test_blocked_reference_gate_can_refund_last_infrastructure_session(self):
        action,execution_id,blocked,child=self._blocked_reference_retry_fixture()
        with mock.patch.object(controller,"evaluate",return_value=blocked), \
             mock.patch.object(controller,"child_snapshot",return_value=[child]), \
             mock.patch.object(controller,"semantic_child_may_still_transition",return_value=False):
            receipt=controller.authorize_semantic_infrastructure_retry(
                self.project,"http://127.0.0.1:1",execution_id,
                "reference researcher hit model length limit before durable checkpoint",
            )
        self.assertEqual(receipt["next_generation"],1)
        ledger=controller.load_execution_ledger(self.project)
        grant=ledger["executions"][execution_id]["semantic_infrastructure_retry"]
        self.assertTrue(grant["blocked_gate_recovery"])
        self.assertEqual(grant["child_sessions"],["ses-reference-length"])

    def test_blocked_reference_gate_refund_rejects_nonlast_session(self):
        action,execution_id,blocked,child=self._blocked_reference_retry_fixture(
            last_session="ses-other"
        )
        with mock.patch.object(controller,"evaluate",return_value=blocked), \
             mock.patch.object(controller,"child_snapshot",return_value=[child]), \
             mock.patch.object(controller,"semantic_child_may_still_transition",return_value=False):
            with self.assertRaisesRegex(
                controller.ControllerError,"same current deterministic work item"
            ):
                controller.authorize_semantic_infrastructure_retry(
                    self.project,"http://127.0.0.1:1",execution_id,
                    "should remain blocked",
                )

    def test_active_semantic_child_cannot_receive_infrastructure_retry(self):
        child = {
            "id": "ses-validator-live",
            "parentID": self.root,
            "agent": "acceptance-validator",
        }
        with mock.patch.object(controller, "evaluate", return_value=self.result), \
             mock.patch.object(controller, "child_snapshot", return_value=[child]), \
             mock.patch.object(controller, "semantic_child_may_still_transition", return_value=True):
            with self.assertRaisesRegex(
                controller.ControllerError, "while child may still transition"
            ):
                controller.authorize_semantic_infrastructure_retry(
                    self.project,
                    "http://127.0.0.1:1",
                    self.execution_id,
                    "should not be accepted",
                )

    def test_semantic_infrastructure_retry_remains_bounded_after_generation_three(self):
        execution_id = controller.execution_action_id(
            "state-a", self.root, self.action, 3
        )
        controller.save_execution_ledger(self.project, {
            "owner": "stage-a-controller",
            "protocol": controller.EXECUTION_LEDGER_PROTOCOL,
            "executions": {
                execution_id: {
                    "execution_id": execution_id,
                    "state_version": "state-a",
                    "root_session": self.root,
                    "action": self.action,
                    "semantic_generation": 3,
                    "created_at_ms": int((time.time() - 30) * 1000),
                    "baseline_child_ids": [],
                }
            },
        })
        child = {
            "id": "ses-validator-generation-three",
            "parentID": self.root,
            "agent": "acceptance-validator",
        }
        with mock.patch.object(controller, "evaluate", return_value=self.result), \
             mock.patch.object(controller, "child_snapshot", return_value=[child]), \
             mock.patch.object(controller, "semantic_child_may_still_transition", return_value=False):
            with self.assertRaisesRegex(
                controller.ControllerError, "retry limit reached"
            ):
                controller.authorize_semantic_infrastructure_retry(
                    self.project,
                    "http://127.0.0.1:1",
                    execution_id,
                    "generation four remains forbidden",
                )

    def test_terminal_acceptance_can_finalize_fresh_current_report(self):
        ctrl = self.project / ".opencode-v2"
        ctrl.mkdir(parents=True, exist_ok=True)
        report = ctrl / "acceptance-report.json"
        report.write_text('{"protocol":"v2-acceptance-report-v1","result":"PASS","checks":[]}\n')
        created_ms = int((time.time() - 1) * 1000)
        intent = {
            "action": self.action,
            "created_at_ms": created_ms,
        }

        def fake_run(*_args, **_kwargs):
            (ctrl / "acceptance-pass.json").write_text(
                '{"protocol":"v2-acceptance-pass-v1","result":"PASS"}\n'
            )
            return subprocess.CompletedProcess([], 0, stdout="ACCEPTANCE_GATE_PASS\n")

        with mock.patch.object(
            controller,"reconcile_terminal_acceptance_with_supervisor",
            return_value="pass",
        ), mock.patch.object(
            controller.subprocess, "run", side_effect=fake_run
        ):
            result = controller.finalize_terminal_acceptance_report(
                self.project,
                "http://127.0.0.1:1",
                intent,
                {"sessions":["ses-validator-pass"]},
            )
        self.assertEqual(result["kind"], "terminal-acceptance-report-finalized")
        self.assertEqual(len(result["report_sha256"]), 64)
        self.assertEqual(len(result["acceptance_pass_sha256"]), 64)

    def test_terminal_acceptance_fail_routes_to_existing_leaf_remediation(self):
        ctrl=self.project/".opencode-v2"
        ctrl.mkdir(parents=True,exist_ok=True)
        report=ctrl/"acceptance-report.json"
        report.write_text(json.dumps({
            "protocol":"v2-acceptance-report-v1",
            "result":"FAIL",
            "checks":[{"id":"A008","status":"FAIL","evidence":"README mismatch"}],
        }))
        intent={
            "action":self.action,
            "created_at_ms":int((time.time()-1)*1000),
        }
        expected={
            "kind":"terminal-acceptance-fail-remediation",
            "failed_acceptance_ids":["A008"],
            "reopened_deliverables":["D008"],
        }
        with mock.patch.object(
            controller,"reconcile_terminal_acceptance_with_supervisor",
            return_value="fail-recovered",
        ), mock.patch.object(
            controller,"finalize_terminal_acceptance_failure_repair",
            return_value=expected,
        ) as repair, mock.patch.object(
            controller.subprocess,"run"
        ) as finalizer:
            result=controller.finalize_terminal_acceptance_report(
                self.project,
                "http://127.0.0.1:1",
                intent,
                {"sessions":["ses-validator-fail"]},
            )
        self.assertEqual(result,expected)
        repair.assert_called_once()
        finalizer.assert_not_called()

    def test_terminal_acceptance_refuses_report_older_than_execution(self):
        ctrl = self.project / ".opencode-v2"
        ctrl.mkdir(parents=True, exist_ok=True)
        report = ctrl / "acceptance-report.json"
        report.write_text('{"protocol":"v2-acceptance-report-v1","result":"PASS","checks":[]}\n')
        old = time.time() - 60
        os.utime(report, (old, old))
        intent = {
            "action": self.action,
            "created_at_ms": int(time.time() * 1000),
        }
        with mock.patch.object(
            controller,"reconcile_terminal_acceptance_with_supervisor",
            return_value="pass",
        ), mock.patch.object(controller.subprocess, "run") as run:
            result = controller.finalize_terminal_acceptance_report(
                self.project,
                "http://127.0.0.1:1",
                intent,
                {"sessions":["ses-validator-old-report"]},
            )
        self.assertIsNone(result)
        run.assert_not_called()


class DecisionStateVersionTests(unittest.TestCase):
    def base_snapshot(self):
        return {
            "state_error":False,
            "resume_phase":"acceptance",
            "acceptance":{"complete":False},
            "reference":{"policy":"internal"},
            "plan":{"complete":False,"blocked":False,"planner_failures":0},
            "scheduler":{"max_concurrent_workers":3,"active_workers":0,"reserved_workers":0,"available_worker_slots":3,"active_deliverables":[],"error":""},
            "leaves":{},
            "execution_blockers":[],
            "tests":{"complete":False},
            "acceptance_validation":{"complete":False},
        }

    def test_acceptance_guard_error_changes_decision_version_with_same_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            (project/".opencode-v2").mkdir()
            snapshot=self.base_snapshot()
            rendered=json.dumps(snapshot,sort_keys=True)
            first,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            (project/".opencode-v2/ACCEPTANCE.guard-errors.txt").write_text("repair me\n")
            second,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            self.assertEqual(first["decision.json"]["acceptance"]["next_action"],"fresh")
            self.assertEqual(second["decision.json"]["acceptance"]["next_action"],"repair")
            self.assertNotEqual(first["decision.json"]["state_version"],second["decision.json"]["state_version"])

    def test_structured_plan_presence_changes_decision_version_with_same_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            ctrl.mkdir()
            snapshot=self.base_snapshot()
            snapshot["resume_phase"]="implementation-plan"
            snapshot["acceptance"]["complete"]=True
            rendered=json.dumps(snapshot,sort_keys=True)
            first,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            (ctrl/"IMPLEMENTATION_PLAN.structured.json").write_text("{}\n")
            second,_=control_query_views.build_query_views(snapshot,{},rendered,project=project)
            self.assertEqual(first["decision.json"]["plan"]["next_action"],"fresh")
            self.assertEqual(second["decision.json"]["plan"]["next_action"],"continue")
            self.assertNotEqual(first["decision.json"]["state_version"],second["decision.json"]["state_version"])

    def test_control_policy_epoch_changes_state_version_with_same_project_state(self):
        snapshot=self.base_snapshot()
        rendered=json.dumps(snapshot,sort_keys=True)
        with mock.patch.object(control_query_views,"CONTROL_POLICY_EPOCH","epoch-a"):
            first,_=control_query_views.build_query_views(snapshot,{},rendered)
        with mock.patch.object(control_query_views,"CONTROL_POLICY_EPOCH","epoch-b"):
            second,_=control_query_views.build_query_views(snapshot,{},rendered)
        self.assertEqual(first["decision.json"]["control_policy_epoch"],"epoch-a")
        self.assertEqual(second["decision.json"]["control_policy_epoch"],"epoch-b")
        self.assertNotEqual(first["decision.json"]["state_version"],second["decision.json"]["state_version"])


class AcceptanceValidatorBudgetTests(unittest.TestCase):
    def test_validator_reserves_budget_for_mandatory_report_write(self):
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/acceptance-validator.md").read_text()
        self.assertIn("steps: 6",role)
        self.assertIn("FIRST tool call MUST create `.opencode-v2/acceptance-report.json`",role)
        self.assertIn("complete immutable evidence surface",role)
        self.assertIn("use later steps only to correct the report",role)

    def test_validator_cannot_invent_executable_report_commands(self):
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/acceptance-validator.md").read_text()
        self.assertIn("NEVER invent, rewrite, simplify, or paraphrase an executable command",role)
        self.assertIn("copy that exact command and its",role)
        self.assertIn("One already-proven canonical command may be reused verbatim",role)

    def test_validator_is_read_only_except_for_the_acceptance_report(self):
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/acceptance-validator.md").read_text()
        for denied in ("read: deny","glob: deny","grep: deny","list: deny","bash: deny"):
            self.assertIn(denied,role)
        self.assertIn("Final acceptance is evidence review only",role)
        self.assertIn("Never call `bash` or any executable",role)
        self.assertIn("Never rerun, replace, or repair executable",role)
        self.assertNotIn("Use live `bash`",role)


class AcceptanceValidatorPacketTests(unittest.TestCase):
    def make_project(self, base: Path) -> Path:
        project=base/"project"
        ctrl=project/".opencode-v2"
        work=ctrl/"work"
        work.mkdir(parents=True)
        (ctrl/"ORIGINAL_TASK.md").write_text("Build the app.\n")
        (ctrl/"ACCEPTANCE.md").write_text(
            "# Acceptance Contract\nReference policy: internal\n"
            "- [ ] A001: app.txt exists and contains hello.\n"
        )
        command="test -f app.txt"
        guard={
            "protocol":"V2.6.9",
            "leaves":{
                "D001":{
                    "id":"D001",
                    "name":"app artifact",
                    "role":"implementer",
                    "outcome":"Create app.txt.",
                    "done_when":"app.txt exists.",
                    "acceptance_ids":["A001"],
                    "owned_artifact_paths":["app.txt"],
                    "verify_command":command,
                }
            },
        }
        (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(guard))
        (project/"app.txt").write_text("hello\n")
        (work/"D001.ready").write_text("status=ready\n")
        evidence={
            "owner":"supervisor",
            "protocol":"v2-supervisor-verify-evidence-v1",
            "latest":{
                "command":command,
                "executed":True,
                "exit_code":0,
                "result":"verified",
                "timestamp":"2026-09-25T00:00:00Z",
                "stdout":"",
                "stderr":"",
            },
        }
        (work/"D001.verify-evidence.json").write_text(json.dumps(evidence))
        report={
            "protocol":"v2-test-report-v1",
            "status":"pass",
            "checks_run":1,
            "checks_passed":1,
            "missing_required_files":[],
            "checks":[{
                "name":"app",
                "command":command,
                "exit_code":0,
                "timed_out":False,
            }],
        }
        (ctrl/"TEST_REPORT.json").write_text(json.dumps(report))
        return project

    def test_packet_binds_exact_must_mapping_evidence_and_artifact_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            project=self.make_project(Path(td))
            packet=controller.build_acceptance_validation_packet(project)
            self.assertEqual(packet["protocol"],controller.ACCEPTANCE_CONTEXT_PROTOCOL)
            self.assertEqual(packet["must_ids"],["A001"])
            self.assertEqual(packet["acceptance_to_leaves"],{"A001":["D001"]})
            self.assertEqual(packet["leaf_evidence"][0]["verify"]["command"],"test -f app.txt")
            self.assertEqual(packet["leaf_evidence"][0]["verify"]["exit_code"],0)
            snapshot=next(x for x in packet["artifact_snapshots"] if x["path"]=="app.txt")
            self.assertEqual(snapshot["content"],"hello\n")
            self.assertEqual(len(packet["packet_sha256"]),64)
            part=controller.build_semantic_subtask(
                {"kind":"launch","agent":"acceptance-validator","mode":"final"},
                project,
            )
            self.assertIn("CANONICAL_ACCEPTANCE_CONTEXT_JSON_BEGIN",part["prompt"])
            self.assertIn(packet["packet_sha256"],part["prompt"])
            self.assertNotIn("command",part)

    def test_packet_rejects_verify_command_provenance_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            project=self.make_project(Path(td))
            path=project/".opencode-v2/work/D001.verify-evidence.json"
            evidence=json.loads(path.read_text())
            evidence["latest"]["command"]="true"
            path.write_text(json.dumps(evidence))
            with self.assertRaisesRegex(controller.ControllerError,"provenance mismatch"):
                controller.build_acceptance_validation_packet(project)


class ValidatorShadowControlTreeTests(unittest.TestCase):
    def test_validator_shadow_keeps_control_inputs_read_only_and_outputs_local(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            project=base/"project"
            shadow=base/"shadow"
            (project/".opencode-v2/query/leaves").mkdir(parents=True)
            (project/".opencode-v2/query/leaves/D001.json").write_text("{}\n")
            (project/".opencode-v2/TEST_REPORT.json").write_text("{}\n")
            (project/".opencode-v2/test-logs").mkdir()
            (project/".opencode-v2/test-logs/old.log").write_text("old\n")
            (project/"README.md").write_text("hello\n")
            worker_sandbox._prepare_verify_shadow(
                project,shadow,"/v2-lower"
            )
            control=shadow/".opencode-v2"
            self.assertTrue(control.is_dir())
            self.assertFalse(control.is_symlink())
            query=control/"query"
            self.assertTrue(query.is_symlink())
            self.assertEqual(os.readlink(query),"/v2-lower/.opencode-v2/query")
            self.assertTrue((control/"test-logs").is_dir())
            self.assertFalse((control/"test-logs").is_symlink())
            self.assertFalse((control/"TEST_REPORT.json").exists())
            self.assertFalse((control/"test-logs/old.log").exists())
            self.assertTrue((shadow/"README.md").is_file())


class ControllerShadowCoherenceTests(unittest.TestCase):
    def test_transient_shadow_mismatch_reloads_decision_until_exact_match(self):
        first={"state_version":"v1","resume_phase":"implementation-plan","actions":[{"kind":"launch","mode":"continue"}]}
        second={"state_version":"v2","resume_phase":"implementation-plan","actions":[{"kind":"launch","mode":"repair"}]}
        with mock.patch.object(controller,"evaluate",side_effect=[first,second]) as evaluate, \
             mock.patch.object(controller,"compare_supervisor_shadow",side_effect=[
                 (False,"action-mismatch transient"),
                 (True,"exact-match"),
             ]) as compare, \
             mock.patch.object(controller.time,"sleep") as sleep:
            result=controller.one_pass(Path("/tmp/project"),True,shadow_attempts=2,shadow_poll_seconds=0.01)
        self.assertEqual(result["state_version"],"v2")
        self.assertTrue(result["shadow_match"])
        self.assertEqual(evaluate.call_count,2)
        self.assertEqual(compare.call_count,2)
        sleep.assert_called_once_with(0.01)

    def test_persistent_shadow_mismatch_still_fails_closed(self):
        result={"state_version":"v1","resume_phase":"implementation-plan","actions":[{"kind":"launch","mode":"continue"}]}
        with mock.patch.object(controller,"evaluate",return_value=result), \
             mock.patch.object(controller,"compare_supervisor_shadow",return_value=(False,"action-mismatch persistent")), \
             mock.patch.object(controller.time,"sleep"):
            with self.assertRaisesRegex(controller.ControllerError,"action-mismatch persistent"):
                controller.one_pass(Path("/tmp/project"),True,shadow_attempts=2,shadow_poll_seconds=0.01)


class TaskSplitterOutputCapTests(unittest.TestCase):
    def test_task_splitter_keeps_its_small_json_cap_while_planner_can_emit_one_plan_write(self):
        config=json.loads((Path(__file__).parents[1] / "xdg/config/opencode/opencode.jsonc").read_text())
        models=config["provider"]["syv"]["models"]
        planner=models["qwen38-implementation-planner-48k"]
        splitter=models["qwen38-task-splitter-nothink"]
        self.assertEqual(splitter["id"],planner["id"])
        self.assertEqual(splitter["limit"]["context"],planner["limit"]["context"])
        self.assertEqual(planner["limit"]["output"],planner["options"]["v2_max_tokens"])
        self.assertEqual(planner["limit"]["output"],7168)
        self.assertEqual(splitter["limit"]["output"],1536)
        self.assertEqual(splitter["options"]["v2_max_tokens"],7168)
        self.assertEqual(
            splitter["options"]["chat_template_kwargs"],
            {"enable_thinking":False,"preserve_thinking":False},
        )
        self.assertNotIn("reasoning_effort",splitter["options"])
        self.assertNotIn("reasoningEffort",splitter["options"])
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/task-splitter.md").read_text()
        self.assertIn("model: syv/qwen38-task-splitter-nothink",role)

    def test_role_ends_with_a_no_analysis_json_only_output_rule(self):
        role=(Path(__file__).parents[1] / "xdg/config/opencode/agents/task-splitter.md").read_text()
        self.assertIn("steps: 4",role)
        self.assertIn("read: deny",role)
        self.assertIn("CANONICAL_SPLIT_REQUEST_JSON_BEGIN",role)
        self.assertIn("## Output-cap execution rule — highest priority",role)
        self.assertIn("Do not narrate analysis",role)
        self.assertIn("must begin with `{`",role)
        self.assertIn("worker-created artifact contents",role)
        self.assertIn("it below 1000 characters",role)
        self.assertLess(len(role), 15_000)

    def test_direct_context_prevents_the_duplicate_read_step_loop_without_salvage(self):
        action={"kind":"launch","agent":"task-splitter","deliverable":"D001","generation":1}
        request={"parent_id":"D001","depth":0,"generation":1,"ownership_items":["a.txt"]}
        prompt=controller.build_task_splitter_subtask(action,request)["prompt"]
        self.assertTrue(prompt.startswith("SPLIT_PARENT: D001\nCANONICAL_SPLIT_REQUEST_JSON_BEGIN\n"))
        self.assertIn('"ownership_items":["a.txt"]',prompt)
        self.assertTrue(prompt.endswith("\nCANONICAL_SPLIT_REQUEST_JSON_END"))
        self.assertEqual(supervisor.parse_split_parent(prompt),"D001")
        stranded='Maximum steps reached. Intended final JSON: {"protocol":"v2-task-split-proposal-v2"}'
        self.assertIsNone(supervisor.parse_splitter_final_json(stranded))


class RecursiveSplitControllerIntegrationTests(unittest.TestCase):
    """Filesystem-only threshold-to-rejoin integration; never creates an LLM child."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.control = self.project / ".opencode-v2"
        (self.control / "work").mkdir(parents=True)
        self.old_root, self.old_project = supervisor.ROOT, supervisor.PROJECT
        supervisor.ROOT, supervisor.PROJECT = self.project, str(self.project)
        self.parent = {
            "id": "D001", "name": "bounded parent", "owned_artifacts": "`a.txt`, `b.txt`, `c.txt`, `d.txt`",
            "launch_deps": [], "contract_deps": [], "verify_command": "test -f a.txt -a -f b.txt -a -f c.txt -a -f d.txt",
            "role": "implementer", "done_when": "all files exist", "acceptance_ids": ["A001"], "parallel": "yes",
        }
        (self.control / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "protocol": "V2.6.9", "project": str(self.project),
            "recursive_split_protocol": control_state.RECURSIVE_SPLIT_PROTOCOL,
            "leaves": {"D001": self.parent},
        }))
        (self.control / "ACCEPTANCE.md").write_text("<!-- ACCEPTANCE_COMPLETE -->\n")
        (self.control / "IMPLEMENTATION_PLAN.md").write_text("<!-- IMPLEMENTATION_PLAN_COMPLETE -->\n")
        self.write_phase_ready("ACCEPTANCE.ready", "ACCEPTANCE.md", "ACCEPTANCE_COMPLETE")
        self.write_phase_ready("IMPLEMENTATION_PLAN.ready", "IMPLEMENTATION_PLAN.md", "IMPLEMENTATION_PLAN_COMPLETE")

    def tearDown(self):
        supervisor.ROOT, supervisor.PROJECT = self.old_root, self.old_project
        self.temp.cleanup()

    def write_phase_ready(self, ready: str, artifact: str, marker: str):
        digest = hashlib.sha256((self.control / artifact).read_bytes()).hexdigest()
        (self.control / ready).write_text(
            f"status=complete\nprotocol={control_state.PHASE_READY_PROTOCOL}\n"
            f"artifact={artifact}\nmarker={marker}\n"
            f"validated={control_state.ACCEPTANCE_READY_VALIDATOR if artifact=='ACCEPTANCE.md' else control_state.PHASE_READY_VALIDATOR}\nartifact_sha256={digest}\n"
        )

    def proposal(self):
        return [
            {"scope": "first", "owned_artifacts": "`a.txt`, `b.txt`", "verify_command": "test -f a.txt -a -f b.txt", "role": "implementer", "depends_on_sibling": "", "done_when": "first files exist", "reads_existing": [], "creates_or_updates": ["a.txt", "b.txt"]},
            {"scope": "second", "owned_artifacts": "`c.txt`, `d.txt`", "verify_command": "test -f c.txt -a -f d.txt", "role": "core-builder", "depends_on_sibling": "", "done_when": "second files exist", "reads_existing": [], "creates_or_updates": ["c.txt", "d.txt"]},
        ]

    def fail_twice(self):
        self.assertEqual(supervisor.claim_attempt("parent-1", "D001"), ("claimed", 1))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify failed"), (True, "genuine-recorded"))
        self.assertEqual(supervisor.claim_attempt("parent-2", "D001"), ("claimed", 2))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify failed"), (True, "split-required"))

    def test_threshold_split_children_reconcile_and_rejoin(self):
        self.fail_twice()
        self.assertTrue(supervisor.split_request_path("D001").is_file())
        state = control_state.snapshot(self.project)
        state["split_required"] = [{
            "deliverable": "D001", "split_state": "split-required",
            "split_generation": 1, "split_claim_count": 0,
        }]
        actions = deterministic_dispatch.select_actions(state)
        splitter = {
            "kind": "launch", "agent": "task-splitter",
            "deliverable": "D001", "generation": 1, "claim": 1,
        }
        self.assertIn(splitter, actions)
        request={"parent_id":"D001","depth":0,"generation":1}
        self.assertIn("CANONICAL_SPLIT_REQUEST_JSON_BEGIN",controller.build_task_splitter_subtask(splitter,request)["prompt"])
        self.assertEqual(supervisor.claim_splitter("D001", "split-session"), (True, "claimed"))
        proposal = {
            "protocol": supervisor.SPLIT_PROPOSAL_PROTOCOL, "parent_id": "D001", "depth": 0,
            "generation": 1, "proposals": self.proposal(),
        }
        self.assertEqual(
            supervisor.complete_splitter("D001", "ses-split-child", "split-session", json.dumps(proposal)),
            (True, "accepted"),
        )
        split_state = control_state.snapshot(self.project)
        self.assertEqual(split_state["leaves"]["D001"]["split_children"], ["D001-A", "D001-B"])
        materialized = supervisor.load_manifest()["leaves"]
        self.assertEqual(materialized["D001-A"]["owned_artifact_paths"], ["a.txt", "b.txt"])
        self.assertEqual(materialized["D001-B"]["owned_artifact_paths"], ["c.txt", "d.txt"])
        for child, files, session in (("D001-A", ("a.txt", "b.txt"), "child-a"), ("D001-B", ("c.txt", "d.txt"), "child-b")):
            self.assertEqual(supervisor.claim_attempt(session, child)[0], "claimed")
            for name in files:
                (self.project / name).write_text(name, encoding="utf-8")
            self.assertEqual(supervisor.supervisor_finalize_ready(child), (True, "finalized"))
        self.assertEqual(supervisor.post_session_finalize("D001"), (True, "finalized"))
        rejoined = control_state.snapshot(self.project)
        self.assertTrue(rejoined["leaves"]["D001"]["complete"])
        self.assertNotEqual(rejoined["resume_phase"], "recursive-split")
        self.assertNotIn(splitter, deterministic_dispatch.select_actions(rejoined))

    def test_false_parent_contract_repair_recreates_exactly_one_claim_slot(self):
        self.fail_twice()
        status={
            "owner":"supervisor","parent_id":"D001","state":"splitter-active",
            "generation":1,"claim_count":6,"proposal_failures":5,
            "recovery_claim_budget":4,"recovery_history":[],
        }
        proposal={
            "protocol":supervisor.SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL,
            "parent_id":"D001","depth":0,"generation":1,
            "field":"prerequisite_artifacts","reason":"missing owned output is not a contract defect",
        }
        supervisor.atomic_write_json(supervisor.split_status_path("D001"),status)
        supervisor.atomic_write_json(supervisor.split_proposal_path("D001"),proposal)
        archived=supervisor._archive_split_state_for_contract_repair("D001")
        supervisor._clear_split_request_state_for_contract_repair("D001")
        data=supervisor.load_attempts()
        entry=data["deliverables"]["D001"]
        entry.pop("split_required")
        for row in entry["failure_history"]:
            row["classification"]="bad-plan"
            row["reclassified_by"]="runtime-parent-contract-repair"
        entry["split_rearm_after_contract_repair"]={
            "generation":1,"field":"prerequisite_artifacts","structured_key":"parent",
        }
        supervisor.save_attempts(data)
        self.assertTrue(archived.is_file())
        original_fingerprint=supervisor.task_splitter_direct_context_fingerprint
        supervisor.task_splitter_direct_context_fingerprint=lambda: "current-direct-context"
        try:
            ok,detail=supervisor.recover_false_parent_contract_repair("D001")
        finally:
            supervisor.task_splitter_direct_context_fingerprint=original_fingerprint
        self.assertEqual((ok,detail),(True,"recovered-one-claim"))
        recovered=supervisor.load_split_status("D001")
        self.assertEqual(recovered["state"],"split-retryable")
        self.assertEqual((recovered["claim_count"],recovered["proposal_failures"]),(6,5))
        self.assertEqual(recovered["recovery_claim_budget"],5)
        self.assertEqual(supervisor.splitter_claim_limit(recovered),7)
        preserved=supervisor.load_attempts()["deliverables"]["D001"]
        self.assertTrue(all(row["classification"]=="bad-plan" for row in preserved["failure_history"]))
        self.assertTrue(supervisor.split_request_path("D001").is_file())
        repair={
            "protocol":"v2-structured-plan-repair-v1",
            "source":"runtime-split-parent-contract","whole_plan":False,
            "affected_keys":["parent"],
            "errors":[{"code":"runtime-parent-prerequisite-artifacts-missing"}],
        }
        repair_path=self.control/"IMPLEMENTATION_PLAN.repair.json"
        supervisor.atomic_write_json(repair_path,repair)
        original_finalizer=supervisor._finalize_current_plan_without_rearm
        supervisor._finalize_current_plan_without_rearm=lambda: True
        try:
            ok,detail=supervisor.resolve_false_parent_contract_repair("D001")
        finally:
            supervisor._finalize_current_plan_without_rearm=original_finalizer
        self.assertEqual((ok,detail),(True,"resolved-current-plan"))
        self.assertFalse(repair_path.exists())
        preserved=supervisor.load_attempts()["deliverables"]["D001"]
        self.assertNotIn("split_rearm_after_contract_repair",preserved)
        self.assertTrue(preserved["false_parent_contract_repair_recovery"]["resolution_archive"])
        self.assertEqual(supervisor.claim_splitter("D001","claim-seven"),(True,"claimed"))


class InlinePythonVerifyProvenanceTests(unittest.TestCase):
    """Reject a helper owned only by a downstream child before any worker runs."""

    @classmethod
    def setUpClass(cls):
        import runpy
        cls.guard=runpy.run_path(
            str(Path(__file__).resolve().with_name("control-guard.py"))
        )

    def test_inline_importlib_and_runpy_paths_are_real_verify_inputs(self):
        bad=(
            'cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && '
            'python3 -c "import importlib.util as u; '
            "s=u.spec_from_file_location('p','tests/_verify_core.py').loader; "
            's.exec_module(u.module_from_spec(s))"'
        )
        self.assertIn(
            "tests/_verify_core.py",
            self.guard["verify_referenced_paths"](bad),
        )
        self.assertIn(
            "tests/_verify_core.py",
            self.guard["verify_referenced_paths"](
                "python3 -c \"import runpy; runpy.run_path('tests/_verify_core.py')\""
            ),
        )

    def test_downstream_owned_verify_helper_is_rejected(self):
        command=(
            'python3 -c "import importlib.util as u; '
            "spec=u.spec_from_file_location('p','tests/_verify_core.py')" + '"'
        )
        with tempfile.TemporaryDirectory() as td:
            leaves={
                "D001":{
                    "verify_command":command,
                    "owned_artifact_paths":["microadd/core.py"],
                    "launch_deps":[],"contract_deps":[],"verify_deps":[],
                },
                "D003":{
                    "verify_command":"python3 -m unittest tests.test_core -v",
                    "owned_artifact_paths":["tests/_verify_core.py"],
                    "launch_deps":["D001"],"contract_deps":[],"verify_deps":[],
                },
            }
            errors=self.guard["verify_path_provenance_errors"](
                Path(td),leaves
            )
        self.assertEqual(len(errors),1,errors)
        self.assertIn("D001",errors[0])
        self.assertIn("tests/_verify_core.py",errors[0])
        self.assertIn("D003",errors[0])
        self.assertIn("not a declared dependency",errors[0])

    def test_loader_is_not_a_modulespec(self):
        bad=(
            'python3 -c "import importlib.util as u; '
            "s=u.spec_from_file_location('p','tests/_verify_core.py').loader; "
            's.exec_module(u.module_from_spec(s))"'
        )
        errors=self.guard["verify_inline_structural_errors"]("D001",bad)
        self.assertEqual(len(errors),1,errors)
        self.assertIn("module_from_spec",errors[0])
        good=(
            'python3 -c "import importlib.util as u; '
            "spec=u.spec_from_file_location('p','tests/_verify_core.py'); "
            'm=u.module_from_spec(spec); spec.loader.exec_module(m)"'
        )
        self.assertEqual(
            self.guard["verify_inline_structural_errors"]("D001",good),[]
        )


class SharedAcceptanceRemediationOwnerTests(unittest.TestCase):
    """A failed shared MUST must select only the exact verified executable owner."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.project=Path(self.tmp.name)
        self.ctrl=self.project/".opencode-v2"
        (self.ctrl/"work").mkdir(parents=True)
        self.command="python3 -m unittest tests.test_report -v"
        self.report=self.ctrl/"acceptance-report.json"
        self.failed={
            "protocol":"v2-acceptance-report-v1","result":"FAIL",
            "checks":[{
                "id":"A012","status":"FAIL","required_executable":True,
                "command":self.command,"exit_code":0,
                "evidence":"Report-formatting test is missing from test_report.py",
            }],
        }
        self.leaves={
            "D006":{"acceptance_ids":["A012"],
                    "owned_artifact_paths":["tests/test_text.py"],
                    "verify_command":"python3 -m unittest tests.test_text -v"},
            "D008":{"acceptance_ids":["A012"],
                    "owned_artifact_paths":["tests/test_report.py"],
                    "verify_command":self.command},
            "D009":{"acceptance_ids":["A012"],
                    "owned_artifact_paths":["tests/test_cli.py"],
                    "verify_command":"python3 -m unittest tests.test_cli -v"},
        }
        (self.ctrl/"ACCEPTANCE.md").write_text("- [ ] A012: Test coverage\\n")
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "leaves":self.leaves,
        }))
        (self.ctrl/"work/attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{
                "D008":{"count":1,"automatic_limit":3},
            },
        }))
        (self.ctrl/"work/D008.ready").write_text("owned READY\\n")
        self._set_evidence(self.command)

    def tearDown(self):
        self.tmp.cleanup()

    def _set_evidence(self,command,result="verified"):
        (self.ctrl/"work/D008.verify-evidence.json").write_text(json.dumps({
            "latest":{
                "command":command,"result":result,"executed":True,
                "exit_code":0,"attempt":1,
            },
        }))

    def _run(self):
        self.report.write_text(json.dumps(self.failed))
        with mock.patch.object(
            controller,"acceptance_must_ids",return_value=["A012"]
        ):
            return controller.finalize_terminal_acceptance_failure_repair(
                self.project,self.report,self.failed
            )

    def test_chooses_one_attested_writer_among_shared_must_owners(self):
        result=self._run()
        self.assertEqual(result["reopened_deliverables"],["D008"])
        self.assertEqual(result["failed_acceptance_ids"],["A012"])
        self.assertFalse((self.ctrl/"work/D008.ready").exists())
        handoff=(self.ctrl/"work/D008.progress.md").read_text()
        self.assertIn("report-formatting",handoff.lower())

    def test_rejects_unattested_report_command_without_reopening(self):
        self.failed["checks"][0]["command"]="python3 -m unittest tests.test_unknown -v"
        with self.assertRaisesRegex(controller.ControllerError,"owner must be unique"):
            self._run()
        self.assertTrue((self.ctrl/"work/D008.ready").exists())

    def test_rejects_failed_supervisor_verify_evidence(self):
        self._set_evidence(self.command,result="verify-failed-1")
        with self.assertRaisesRegex(controller.ControllerError,"owner must be unique"):
            self._run()
        self.assertTrue((self.ctrl/"work/D008.ready").exists())

    def test_rejects_duplicate_exact_verified_writer(self):
        self.leaves["D008-B"]={
            "acceptance_ids":["A012"],
            "owned_artifact_paths":["tests/test_report.py"],
            "verify_command":self.command,
        }
        (self.ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "leaves":self.leaves,
        }))
        (self.ctrl/"work/D008-B.verify-evidence.json").write_text(json.dumps({
            "latest":{
                "command":self.command,"result":"verified",
                "executed":True,"exit_code":0,
            },
        }))
        with self.assertRaisesRegex(controller.ControllerError,"owner must be unique"):
            self._run()
        self.assertTrue((self.ctrl/"work/D008.ready").exists())


if __name__ == "__main__":
    unittest.main()
