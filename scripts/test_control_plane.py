#!/usr/bin/env python3
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import control_state
import supervisor
import agent_config_audit


def message(parts=None, completed=None):
    info = {
        "id": "msg-1",
        "role": "assistant",
        "time": {"created": 1},
    }
    if completed is not None:
        info["time"]["completed"] = completed
    item = {"info": info}
    if parts is not None:
        item["parts"] = parts
    return item


class WatchdogShapeTests(unittest.TestCase):
    def setUp(self):
        supervisor.watch.clear()

    def test_actual_message_shape_counts_live_reasoning(self):
        shape = supervisor.message_shape(
            [message([{"type": "reasoning", "text": "x" * 8001}])],
            {"parentID": "root", "directory": "/tmp/project"},
        )
        self.assertTrue(shape["observable"])
        self.assertEqual(shape["reasoning"], 8001)
        self.assertFalse(shape["assistant_completed"])

    def test_missing_parts_waits_instead_of_aging(self):
        shape = supervisor.message_shape(
            [message()], {"parentID": "root", "directory": "/tmp/project"}
        )
        self.assertFalse(shape["observable"])
        age, _ = supervisor.watchdog_age("s", ("msg-1", ""), False, now=0)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", ""), False, now=200)
        self.assertEqual(age, 0)

    def test_tool_resets_then_later_reasoning_gets_new_window(self):
        age, _ = supervisor.watchdog_age("s", ("msg-1", ""), True, now=10)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", "tool-1"), False, now=90)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", "tool-1"), True, now=100)
        self.assertEqual(age, 0)
        age, _ = supervisor.watchdog_age("s", ("msg-1", "tool-1"), True, now=110)
        self.assertEqual(age, 10)

    def test_completed_assistant_is_never_watchable(self):
        shape = supervisor.message_shape(
            [message([{"type": "reasoning", "text": "x" * 9000}], completed=2)],
            {"parentID": "root", "directory": "/tmp/project"},
        )
        self.assertTrue(shape["assistant_completed"])

    def test_planner_has_reasoning_watchdog_and_input_context_ceiling(self):
        self.assertEqual(
            supervisor.watchdog_limits("implementation-planner"),
            (300, 20000, 20000),
        )
        ceiling = supervisor.PLANNER_CONTEXT_INPUT_CEILING
        self.assertEqual(ceiling, 45000)
        self.assertEqual(
            supervisor.planner_context_reason(
                "implementation-planner", ceiling - 1
            ),
            "",
        )
        self.assertIn(
            "planner_context_input=45000",
            supervisor.planner_context_reason(
                "implementation-planner", ceiling
            ),
        )
        self.assertEqual(
            supervisor.planner_context_reason(
                "implementation-planner", ceiling, tool_running=True
            ),
            "",
        )
        self.assertEqual(
            supervisor.planner_context_reason("implementer", ceiling), ""
        )


class AttemptLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = supervisor.ROOT
        self.old_project = supervisor.PROJECT
        supervisor.ROOT = Path(self.tmp.name)
        supervisor.PROJECT = self.tmp.name
        supervisor.session_task.clear()

    def tearDown(self):
        supervisor.ROOT = self.old_root
        supervisor.PROJECT = self.old_project
        self.tmp.cleanup()

    def test_live_and_persisted_claims_cannot_create_attempt_four(self):
        self.assertEqual(supervisor.claim_attempt("live-1", "D005"), ("claimed", 1))
        self.assertEqual(supervisor.claim_attempt("db-2", "D005"), ("claimed", 2))
        self.assertEqual(supervisor.claim_attempt("live-3", "D005"), ("claimed", 3))
        self.assertEqual(supervisor.claim_attempt("db-4", "D005"), ("limit", 3))
        self.assertEqual(supervisor.claim_attempt("live-3", "D005"), ("existing", 3))
        ledger = json.loads(
            (Path(self.tmp.name) / ".opencode-v2/work/attempts.json").read_text()
        )
        self.assertEqual(ledger["owner"], "supervisor")
        self.assertEqual(ledger["deliverables"]["D005"]["count"], 3)
        self.assertEqual(len(ledger["deliverables"]["D005"]["sessions"]), 3)

    def test_existing_session_with_count_four_is_invalid_and_not_repaired(self):
        ledger_path = Path(self.tmp.name) / ".opencode-v2/work/attempts.json"
        ledger_path.parent.mkdir(parents=True)
        original = {
            "owner": "supervisor",
            "deliverables": {"D005": {"count": 4, "sessions": ["old-session"]}},
        }
        ledger_path.write_text(json.dumps(original))
        self.assertEqual(supervisor.claim_attempt("old-session", "D005"), ("invalid", 4))
        self.assertNotIn("old-session", supervisor.session_task)
        self.assertEqual(json.loads(ledger_path.read_text()), original)

    def test_existing_count_four_is_blocked_by_dispatch(self):
        ledger_path = Path(self.tmp.name) / ".opencode-v2/work/attempts.json"
        ledger_path.parent.mkdir(parents=True)
        ledger_path.write_text(json.dumps({
            "owner": "supervisor",
            "deliverables": {"D005": {"count": 4, "sessions": ["old-session"]}},
        }))
        old_plan_ready = supervisor.plan_ready
        old_load_manifest = supervisor.load_manifest
        old_ready_info = supervisor.ready_info
        old_abort = supervisor.abort_session
        aborts = []
        try:
            supervisor.plan_ready = lambda: True
            supervisor.load_manifest = lambda: {"leaves": {"D005": {"role": "implementer", "launch_deps": []}}}
            supervisor.ready_info = lambda did: {}
            supervisor.abort_session = lambda sid, reason, agent="": aborts.append((sid, reason, agent))
            supervisor.dispatch_seen.clear()
            supervisor.enforce_assignment("old-session", "implementer", supervisor.implementation_prompt("D005"))
        finally:
            supervisor.plan_ready = old_plan_ready
            supervisor.load_manifest = old_load_manifest
            supervisor.ready_info = old_ready_info
            supervisor.abort_session = old_abort
            supervisor.dispatch_seen.clear()
        self.assertEqual(len(aborts), 1)
        self.assertIn("attempt_ledger_invalid", aborts[0][1])


class DispatchPromptProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = supervisor.ROOT
        self.old_project = supervisor.PROJECT
        self.old_abort = supervisor.abort_session
        supervisor.ROOT = Path(self.tmp.name)
        supervisor.PROJECT = self.tmp.name
        supervisor.dispatch_seen.clear()
        self.aborts = []
        supervisor.abort_session = lambda sid, reason, agent="": self.aborts.append((sid, reason, agent))

    def tearDown(self):
        supervisor.ROOT = self.old_root
        supervisor.PROJECT = self.old_project
        supervisor.abort_session = self.old_abort
        supervisor.dispatch_seen.clear()
        self.tmp.cleanup()

    def test_short_exact_prompt_is_valid(self):
        prompt = supervisor.implementation_prompt("D005")
        self.assertEqual(supervisor.implementation_prompt_violation(prompt), "")
        self.assertEqual(supervisor.parse_deliverable(prompt), "D005")

    def test_actual_beta_single_newline_subagent_wrapper_is_valid(self):
        wrapped = "You are a subagent spawned by another session.\n" + supervisor.implementation_prompt("D005")
        self.assertEqual(supervisor.implementation_prompt_violation(wrapped), "")

    def test_model_handoff_claim_is_rejected_even_when_short(self):
        prompt = supervisor.implementation_prompt("D006") + "\nAttempt 1 created public/app.js."
        self.assertEqual(
            supervisor.implementation_prompt_violation(prompt),
            "noncanonical_or_model_derived_handoff",
        )

    def test_oversized_prompt_is_rejected_before_dispatch(self):
        prompt = "DELIVERABLE: D005\n" + ("x" * supervisor.MAX_IMPLEMENTATION_PROMPT_CHARS)
        supervisor.enforce_assignment("oversized", "implementer", prompt)
        self.assertEqual(len(self.aborts), 1)
        self.assertIn("oversized_first_user_prompt", self.aborts[0][1])
        self.assertIn("oversized", supervisor.dispatch_seen)


class PreDispatchClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root, self.old_project = supervisor.ROOT, supervisor.PROJECT
        self.old_ready, self.old_manifest, self.old_info = supervisor.plan_ready, supervisor.load_manifest, supervisor.ready_info
        supervisor.ROOT = Path(self.tmp.name); supervisor.PROJECT = self.tmp.name
        supervisor.plan_ready = lambda: True
        supervisor.load_manifest = lambda: {"leaves": {"D001": {"role": "probe-builder", "launch_deps": []}, "D002": {"role": "implementer", "launch_deps": ["D001"]}}}
        supervisor.ready_info = lambda did: {}
        supervisor.session_task.clear()

    def tearDown(self):
        supervisor.ROOT, supervisor.PROJECT = self.old_root, self.old_project
        supervisor.plan_ready, supervisor.load_manifest, supervisor.ready_info = self.old_ready, self.old_manifest, self.old_info
        supervisor.session_task.clear(); self.tmp.cleanup()

    def test_first_canonical_dispatch_creates_ledger_before_session_and_binds_without_increment(self):
        status, did, reason, count = supervisor.preclaim_attempt("probe-builder", supervisor.implementation_prompt("D001"), "call-1")
        self.assertEqual((status, did, reason, count), ("claimed", "D001", "", 1))
        ledger = json.loads((Path(self.tmp.name) / ".opencode-v2/work/attempts.json").read_text())
        self.assertEqual(ledger["owner"], "supervisor")
        self.assertEqual(ledger["deliverables"]["D001"], {"sessions": ["dispatch:call-1"], "count": 1})
        self.assertEqual(supervisor.claim_attempt("child-1", "D001"), ("existing", 1))
        ledger = json.loads((Path(self.tmp.name) / ".opencode-v2/work/attempts.json").read_text())
        self.assertEqual(ledger["deliverables"]["D001"]["sessions"], ["child-1"])

    def test_failed_claim_never_reserves_or_allows_general_substitution(self):
        status, did, reason, count = supervisor.preclaim_attempt("general", supervisor.implementation_prompt("D001"), "call-2")
        self.assertEqual((status, did, count), ("denied", "D001", 0))
        self.assertIn("role_mismatch expected=probe-builder actual=general", reason)
        self.assertFalse((Path(self.tmp.name) / ".opencode-v2/work/attempts.json").exists())

class BoundedChildResultTests(unittest.TestCase):
    PLUGIN = Path(__file__).parents[1] / "xdg/config/opencode/plugins/v2-bounded-subagent.mjs"

    def invoke(self, original, agent="implementer"):
        script = """
import { boundedChildResult } from %s;
const value = boundedChildResult({directory: process.argv[1], args: {agent: %s, prompt: %s}, metadata: {sessionID: 'child-1', status: 'completed'}, original: %s});
process.stdout.write(value);
""" % (json.dumps(self.PLUGIN.as_uri()), json.dumps(agent), json.dumps(supervisor.implementation_prompt("D005")), json.dumps(original))
        with tempfile.TemporaryDirectory() as td:
            return subprocess.run(["node", "--input-type=module", "-e", script, td], text=True, capture_output=True, check=True).stdout

    def test_synthetic_50k_child_output_becomes_small_receipt(self):
        receipt = self.invoke("x" * 50000)
        self.assertLessEqual(len(receipt), 1500)
        self.assertIn("DELIVERABLE: D005", receipt)
        self.assertNotIn("x" * 100, receipt)

    def test_acceptance_success_stays_exact_bare_token(self):
        receipt = self.invoke('<subagent sessionID="x">\nACCEPTANCE_PASS\n</subagent>', "acceptance-validator")
        self.assertEqual(receipt, "ACCEPTANCE_PASS")


class LiveEventWatchdogTests(unittest.TestCase):
    def test_sse_deltas_cross_worker_bound_and_tool_success_resets(self):
        state = {"reasoning": 0, "text": 0, "tool_running": False}
        supervisor.reduce_live_event(state, {"type": "session.next.reasoning.delta", "data": {"delta": "x" * 8001}})
        self.assertIn("sse_reasoning_chars", supervisor.event_watchdog_reason("implementer", state))
        supervisor.reduce_live_event(state, {"type": "session.next.tool.success", "data": {}})
        self.assertEqual(state["reasoning"], 0)
        self.assertEqual(supervisor.event_watchdog_reason("implementer", state), "")

    def test_invisible_stream_has_conservative_hard_fallback(self):
        old_project = supervisor.PROJECT
        supervisor.event_watch.clear()
        with tempfile.TemporaryDirectory() as td:
            supervisor.PROJECT = td
            try:
                self.assertEqual(supervisor.fallback_no_progress_reason("s", "implementer", "", False, now=0), "")
                self.assertIn("300s", supervisor.fallback_no_progress_reason("s", "implementer", "", False, now=300))
            finally:
                supervisor.PROJECT = old_project
                supervisor.event_watch.clear()

    def test_generic_invisible_stream_fallback_never_preempts_planner_policy(self):
        old_project = supervisor.PROJECT
        supervisor.event_watch.clear()
        with tempfile.TemporaryDirectory() as td:
            supervisor.PROJECT = td
            try:
                self.assertEqual(
                    supervisor.effective_fallback_reason(
                        "planner", "implementation-planner", "", False, now=0
                    ), "",
                )
                self.assertEqual(
                    supervisor.effective_fallback_reason(
                        "planner", "implementation-planner", "", False, now=300
                    ), "",
                )
                self.assertEqual(
                    supervisor.effective_fallback_reason(
                        "worker", "implementer", "", False, now=0
                    ), "",
                )
                self.assertIn(
                    "300s",
                    supervisor.effective_fallback_reason(
                        "worker", "implementer", "", False, now=300
                    ),
                )
            finally:
                supervisor.PROJECT = old_project
                supervisor.event_watch.clear()

    def test_inactive_sse_watches_are_stopped_without_breaking_the_poll_loop(self):
        supervisor.event_watch.clear()
        stale_stop = supervisor.threading.Event()
        live_stop = supervisor.threading.Event()
        supervisor.event_watch.update({
            "stale": {"stop": stale_stop},
            "live": {"stop": live_stop},
        })
        supervisor.stop_inactive_event_watches({"live"})
        self.assertTrue(stale_stop.is_set())
        self.assertFalse(live_stop.is_set())
        self.assertNotIn("stale", supervisor.event_watch)
        supervisor.event_watch.clear()


class PlannerDurableProgressTests(unittest.TestCase):
    def setUp(self): supervisor.planner_checkpoints.clear()

    def test_untouched_bootstrap_scaffold_is_not_retired_before_observed_initial_write_latency(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "IMPLEMENTATION_PLAN.md"
            path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
            self.assertEqual(supervisor.planner_progress_reason("p", 0, path), "")
            self.assertEqual(supervisor.planner_progress_reason("p", 150, path), "")
            self.assertEqual(supervisor.planner_progress_reason("p", 240, path), "")
            self.assertEqual(
                supervisor.planner_progress_reason(
                    "p", 359, path
                ), "",
            )

    def test_durable_model_edit_is_detected_and_resets_stall_timer(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "IMPLEMENTATION_PLAN.md"
            path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
            self.assertEqual(supervisor.planner_progress_reason("p", 0, path), "")
            path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD + "### D001 — First leaf\n")
            self.assertEqual(supervisor.planner_progress_reason("p", 356, path), "")
            state = supervisor.planner_checkpoints["p"]
            self.assertTrue(state["model_progress"])
            self.assertEqual(
                supervisor.planner_progress_reason(
                    "p", 356 + supervisor.PLANNER_PROGRESS_STALL_SECONDS - 1, path
                ), "",
            )

    def test_truly_stalled_planner_is_eventually_retired(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "IMPLEMENTATION_PLAN.md"
            path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
            self.assertEqual(supervisor.planner_progress_reason("p", 0, path), "")
            reason = supervisor.planner_progress_reason(
                "p", supervisor.PLANNER_INITIAL_PROGRESS_GRACE_SECONDS + 1, path
            )
            self.assertIn("planner_no_meaningful_plan_progress", reason)
            self.assertIn("limit=420s", reason)

    def test_checkpoint_engagement_never_resets_deadline_without_dxxx_structure(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "IMPLEMENTATION_PLAN.md"
            path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
            self.assertEqual(supervisor.planner_progress_reason("p", 0, path), "")
            path.write_text(path.read_text().replace("Status: BOOTSTRAP", "Status: PLANNING"))
            self.assertEqual(supervisor.planner_progress_reason("p", 300, path), "")
            state = supervisor.planner_checkpoints["p"]
            self.assertTrue(state["engagement"])
            self.assertFalse(state["model_progress"])
            reason = supervisor.planner_progress_reason("p", 421, path)
            self.assertIn("planner_no_meaningful_plan_progress", reason)
            self.assertIn("engagement=true", reason)

    def test_checkpoint_status_change_cannot_extend_post_edit_stall_window(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "IMPLEMENTATION_PLAN.md"
            path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
            self.assertEqual(supervisor.planner_progress_reason("p", 0, path), "")
            path.write_text(path.read_text() + "### D001 — First leaf\n")
            self.assertEqual(supervisor.planner_progress_reason("p", 356, path), "")
            path.write_text(path.read_text().replace("Status: BOOTSTRAP", "Status: PLANNING"))
            reason = supervisor.planner_progress_reason(
                "p", 356 + supervisor.PLANNER_PROGRESS_STALL_SECONDS, path
            )
            self.assertIn("planner_plan_progress_stalled", reason)

    def test_partial_plan_survives_fresh_planner_baseline_and_retry_is_reference_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "IMPLEMENTATION_PLAN.md"
            partial = control_state.IMPLEMENTATION_PLAN_SCAFFOLD + "### D001 — Durable partial\n"
            path.write_text(partial)
            self.assertEqual(supervisor.planner_progress_reason("old", 0, path), "")
            self.assertEqual(supervisor.planner_progress_reason("fresh", 0, path), "")
            self.assertEqual(supervisor.planner_progress_reason("fresh", 150, path), "")
            self.assertEqual(path.read_text(), partial)
            self.assertEqual(
                supervisor.PLANNER_CONTINUATION_PROMPT,
                "Continue implementation planning for this project.\n"
                "Read .opencode-v2/ACCEPTANCE.md.\n"
                "Read .opencode-v2/CONTROL_CONTRACT.md.\n"
                "Read .opencode-v2/IMPLEMENTATION_PLAN.md.\n"
                "Continue from durable file state using your progressive planner protocol.",
            )

    def test_three_supervisor_recorded_failures_block_a_fourth_planner(self):
        old_project = supervisor.PROJECT
        with tempfile.TemporaryDirectory() as td:
            supervisor.PROJECT = td
            try:
                path = Path(td) / ".opencode-v2/IMPLEMENTATION_PLAN.md"
                path.parent.mkdir(parents=True)
                path.write_text(control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
                for number in range(3):
                    supervisor.record_planner_restart(f"p{number}", "genuine-failure")
                self.assertEqual(supervisor.planner_restart_count(), 3)
                self.assertEqual(
                    supervisor.planner_retirement_reason("fourth", 0, path),
                    "planner_restart_limit=3",
                )
            finally:
                supervisor.PROJECT = old_project
                supervisor.planner_checkpoints.clear()


class RootResumeStateTests(unittest.TestCase):
    def state(self, acceptance=True, plan=False, leaves=None, tests=False, validation=False):
        return {"acceptance":{"complete":acceptance}, "plan":{"complete":plan},
                "leaves":leaves or {}, "tests":{"complete":tests},
                "acceptance_validation":{"complete":validation}}

    def test_representative_resume_phases(self):
        self.assertEqual(control_state.resume_phase(self.state(plan=False)), "implementation-plan")
        self.assertEqual(
            control_state.resume_phase(self.state(plan=False) | {"plan": {"complete": False, "blocked": True}}),
            "implementation-blocked",
        )
        self.assertEqual(control_state.resume_phase(self.state(plan=True, leaves={"D001":{"complete":False}})), "execution")
        self.assertEqual(control_state.resume_phase(self.state(plan=True, leaves={"D001":{"complete":True}}, tests=False)), "final-tests")
        self.assertEqual(control_state.resume_phase(self.state(plan=True, leaves={"D001":{"complete":True}}, tests=True)), "acceptance-validation")

    def test_continuation_prompt_is_short_and_reference_only(self):
        prompt = supervisor.ROOT_CONTINUATION_PROMPT
        self.assertLess(len(prompt), 1000)
        self.assertIn("control-status", prompt)
        self.assertNotIn("transcript", prompt.lower())


class AgentConfigurationAndPromptAuditTests(unittest.TestCase):
    AGENTS = Path(__file__).parents[1] / "xdg/config/opencode/agents"

    def test_todowrite_ui_mirroring_is_intentionally_disabled(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertNotRegex(root, r"(?m)^  todowrite: allow$")
        self.assertIn("TodoWrite UI mirroring is intentionally disabled", root)

    def test_resolved_permission_audit_requires_root_allow_and_worker_denies(self):
        editor_agents = {
            *agent_config_audit.WRITER_AGENTS,
            *agent_config_audit.PLANNER_EDIT_TARGETS,
        }
        payload = {"data": [
            *[
                {"name": name, "permissions": [
                    {"action": "edit", "resource": agent_config_audit.PLANNER_EDIT_TARGETS.get(name, "*"), "effect": "allow"},
                    *([{"action": "edit", "resource": ".opencode-v2/bin/*", "effect": "deny"}]
                      if name in agent_config_audit.CONTROL_WRAPPER_DENY_AGENTS else []),
                ]}
                for name in sorted(editor_agents)
            ],
        ]}
        self.assertEqual(agent_config_audit.audit_agents(payload), [])
        next(item for item in payload["data"] if item["name"] == "acceptance-planner")["permissions"][0]["resource"] = "*"
        self.assertTrue(agent_config_audit.audit_agents(payload))

    def test_planners_use_available_file_tools_for_contract_artifacts(self):
        expected = {
            "acceptance-planner.md": ".opencode-v2/ACCEPTANCE.md",
            "implementation-planner.md": ".opencode-v2/IMPLEMENTATION_PLAN.md",
        }
        for name, target in expected.items():
            text = (self.AGENTS / name).read_text()
            self.assertIn(f'    "{target}": allow', text, name)
            self.assertIn("`write` or `edit`", text, name)
            self.assertNotIn("`apply_patch`", text, name)

    def test_phase_ready_sentinels_are_guard_only(self):
        sentinels = ("ACCEPTANCE.ready", "IMPLEMENTATION_PLAN.ready")
        for path in self.AGENTS.glob("*.md"):
            for line in path.read_text().splitlines():
                if not any(sentinel in line for sentinel in sentinels):
                    continue
                if any(word in line.lower() for word in ("write", "create", "modify", "request")):
                    self.assertRegex(line, r"(?i)\b(?:never|do not)\b", f"{path.name}: {line}")
        guard = (Path(__file__).with_name("control-guard.py")).read_text()
        self.assertIn('ready = ctrl / "ACCEPTANCE.ready"', guard)
        self.assertIn('ready = ctrl / "IMPLEMENTATION_PLAN.ready"', guard)

    def test_success_output_protocol_is_unconditional_and_bare(self):
        required = (
            "On success, your entire final response\n"
            "MUST be the exact bare text ACCEPTANCE_PASS, with no Markdown, emoji, heading,"
        )
        for name in ("orchestrator.md", "acceptance-validator.md"):
            text = (self.AGENTS / name).read_text()
            self.assertIn(required, text, name)
            self.assertNotIn("When success is required", text, name)
            self.assertNotIn("**ACCEPTANCE_PASS**", text, name)

    def test_control_protocol_is_project_local_for_planners_and_workers(self):
        names = (
            "implementation-planner.md", "probe-builder.md", "implementer.md",
            "core-builder.md", "feature-builder.md", "reasoning-builder.md",
            "integrator.md", "test-builder.md", "tester.md",
        )
        for name in names:
            text = (self.AGENTS / name).read_text()
            self.assertIn(".opencode-v2/CONTROL_CONTRACT.md", text, name)
            self.assertRegex(
                text, r"(?is)(?:never|do not).*?(?:read|inspect).*?harness", name
            )
            self.assertNotRegex(
                text,
                r"(?im)^(?!.*(?:never|do not)).*(?:read|inspect).*run-checks\.py",
                name,
            )
        planner = (self.AGENTS / "implementation-planner.md").read_text()
        self.assertIn("Do not add a\nDxxx probe merely to discover control protocol", planner)
        self.assertIn("Never guess or use a fallback\nmanifest schema", planner)

    def test_planner_progressively_externalizes_and_retries_by_reference(self):
        planner = (self.AGENTS / "implementation-planner.md").read_text()
        self.assertIn("Progressive externalization — mandatory", planner)
        self.assertIn("bootstrapper has already created", planner)
        self.assertIn("explicitly incomplete scaffold", planner)
        self.assertIn("never delete or recreate it", planner)
        self.assertIn("Before optional lessons, project\n   inspection, broad design, or long reasoning", planner)
        self.assertIn("status from `BOOTSTRAP` to `PLANNING`", planner)
        self.assertIn("it is not plan\n   progress", planner)
        self.assertIn("Use bounded `edit` calls", planner)
        self.assertIn("150-300 lines preferred", planner)
        self.assertIn("400 physical lines is the hard protocol maximum", planner)
        self.assertIn("do not wait\nto write the complete file atomically", planner)
        self.assertNotIn("Within 150 seconds", planner)
        self.assertNotIn("planner_checkpoint_missing", Path(supervisor.__file__).read_text())

        root = (self.AGENTS / "orchestrator.md").read_text()
        retry_lines = (
            "`Continue implementation planning for this project.`",
            "`Read .opencode-v2/ACCEPTANCE.md.`",
            "`Read .opencode-v2/CONTROL_CONTRACT.md.`",
            "`Read .opencode-v2/IMPLEMENTATION_PLAN.md.`",
            "`Continue from durable file state using your progressive planner protocol.`",
        )
        positions = [root.index(line) for line in retry_lines]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("do not include the original request", root)
        self.assertIn("never request a shorter self-contained retry", root)
        self.assertIn("IMPLEMENTATION_BLOCKED", root)
        self.assertRegex(root, r"(?s)orchestrator.*?edit:\s*deny")

    def test_retry_prompt_is_exact_filesystem_only_protocol(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        for line in supervisor.implementation_prompt("Dxxx").splitlines():
            self.assertIn(f"`{line}`", root)
        self.assertIn("Use exactly those five lines", root)
        self.assertIn("model\nhandoff", root)

    def test_workers_use_local_completion_and_protect_control_wrappers(self):
        for name in sorted(supervisor.IMPLEMENTATION_AGENTS):
            text = (self.AGENTS / f"{name}.md").read_text()
            self.assertIn(".opencode-v2/bin/leaf-complete Dxxx", text)
            self.assertIn('".opencode-v2/bin/*": deny', text)
            self.assertIn("meaningful owned artifact early", text)

    def test_bounded_child_plugin_is_resolved_in_config(self):
        config = (self.AGENTS.parent / "opencode.jsonc").read_text()
        self.assertIn("v2-bounded-subagent.mjs", config)

    def test_dispatch_plugin_preclaims_before_child_and_root_forbids_salvage(self):
        plugin = (self.AGENTS.parent / "plugins/v2-bounded-subagent.mjs").read_text()
        self.assertIn('"tool.execute.before"', plugin)
        self.assertIn("--claim-dispatch", plugin)
        self.assertIn("input.args", plugin)
        self.assertIn("before OpenCode materializes", plugin)
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertIn("Never substitute `general`", root)
        self.assertIn("Never create application/source/test/configuration artifacts yourself", root)
        self.assertIn("Missing ownership is a plan defect", root)

    def test_planner_uses_the_bounded_nonthinking_qwen_profile(self):
        config = json.loads((self.AGENTS.parent / "opencode.jsonc").read_text())
        model = config["providers"]["syv"]["models"][
            "qwen38-implementation-planner-48k"
        ]
        self.assertEqual(model["limit"]["output"], 1536)
        self.assertEqual(
            model["body"]["chat_template_kwargs"],
            {"enable_thinking": False, "preserve_thinking": False},
        )
        self.assertNotIn("settings", model)
        planner = (self.AGENTS / "implementation-planner.md").read_text()
        self.assertIn("steps: 24", planner)

    def test_root_never_resets_or_relaunches_a_blocked_planner_ledger(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertIn("planner-restarts.json", root)
        self.assertIn("does NOT reset when a root or supervisor session is", root)
        self.assertIn("If its count is already 3", root)
        self.assertIn("launch no planner and output", root)
        self.assertNotIn("attempt ledger starts fresh", root.lower())


class ImplementationPlanSizeTests(unittest.TestCase):
    GUARD = Path(__file__).with_name("control-guard.py")

    def plan(self, lines):
        prefix = """# Plan
## Deliverables
### D001 — Tests
- Outcome: tests
- Owned artifacts: .opencode-v2/TEST_CHECKS.json
- Launch deps: (none)
- Contract deps: (none)
- Verify deps: (none)
- Acceptance IDs: A001
- Complexity: S
- Deep reasoning: no
- Role: tester
- Parallel-safe with: (none)
- Verify command: `.opencode-v2/bin/run-checks`
- Done when: report passes
## Execution Waves
- Wave 1: D001
""".splitlines()
        filler = ["<!-- compact plan padding -->"] * (lines - len(prefix) - 1)
        return "\n".join(prefix + filler + ["<!-- IMPLEMENTATION_PLAN_COMPLETE -->"]) + "\n"

    def validate(self, project):
        return subprocess.run(
            [sys.executable, str(self.GUARD), "--project", str(project), "--finalize-plan"],
            text=True,
            capture_output=True,
        )

    def test_400_lines_is_accepted_and_401_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"
            ctrl.mkdir()
            plan = ctrl / "IMPLEMENTATION_PLAN.md"
            plan.write_text(self.plan(400))
            accepted = self.validate(td)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertTrue((ctrl / "IMPLEMENTATION_PLAN.ready").exists())

            (ctrl / "IMPLEMENTATION_PLAN.ready").unlink()
            plan.write_text(self.plan(401))
            rejected = self.validate(td)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "hard maximum is 400",
                (ctrl / "IMPLEMENTATION_PLAN.guard-errors.txt").read_text(),
            )
            self.assertFalse((ctrl / "IMPLEMENTATION_PLAN.ready").exists())

    def test_test_manifest_leaf_requires_exact_project_local_runner(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"
            ctrl.mkdir()
            text = self.plan(40).replace(
                ".opencode-v2/bin/run-checks",
                "python3 run-checks.py --project .",
            )
            (ctrl / "IMPLEMENTATION_PLAN.md").write_text(text)
            rejected = self.validate(td)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("must be exact .opencode-v2/bin/run-checks", (ctrl / "IMPLEMENTATION_PLAN.guard-errors.txt").read_text())


class TestChecksControlContractTests(unittest.TestCase):
    RUNNER = Path(__file__).with_name("run-checks.py")

    def run_runner(self, project, *args):
        return subprocess.run(
            [sys.executable, str(self.RUNNER), "--project", str(project), *args],
            text=True,
            capture_output=True,
        )

    def test_bootstrap_exposes_exact_current_schema_and_full_control_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            bootstrapped = self.run_runner(project, "--bootstrap-control-contract")
            self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stderr)
            contract = (project / ".opencode-v2/CONTROL_CONTRACT.md").read_text()
            scaffold = project / ".opencode-v2/IMPLEMENTATION_PLAN.md"
            self.assertEqual(scaffold.read_text(), control_state.IMPLEMENTATION_PLAN_SCAFFOLD)
            self.assertNotIn("IMPLEMENTATION_PLAN_COMPLETE", scaffold.read_text())
            self.assertIn("## Planner checkpoint\nStatus: BOOTSTRAP", scaffold.read_text())
            match = re.search(r"```json\n(.*?)\n```", contract, re.DOTALL)
            self.assertIsNotNone(match)
            exposed_schema = json.loads(match.group(1))
            printed = subprocess.run(
                [sys.executable, str(self.RUNNER), "--print-test-checks-schema"],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(exposed_schema, json.loads(printed.stdout))
            for required in (
                "ACCEPTANCE.ready", "IMPLEMENTATION_PLAN.ready", "Dxxx.ready",
                "attempts.json", "Dxxx.progress.md", "TEST_REPORT.json",
                "ACCEPTANCE_PASS",
            ):
                self.assertIn(required, contract)
            self.assertIn(".opencode-v2/bin/run-checks", contract)
            self.assertIn(".opencode-v2/bin/leaf-complete Dxxx", contract)
            self.assertIn("explicitly\n  incomplete scaffold", contract)
            for command in ("run-checks", "leaf-complete", "control-status"):
                path = project / ".opencode-v2/bin" / command
                self.assertTrue(path.exists())
                self.assertTrue(path.stat().st_mode & 0o111)
            launch = (Path(__file__).parents[1] / "run.sh").read_text()
            self.assertIn("--bootstrap-control-contract", launch)

    def test_bootstrap_preserves_a_partial_plan_on_retry(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            self.run_runner(project, "--bootstrap-control-contract")
            plan = project / ".opencode-v2/IMPLEMENTATION_PLAN.md"
            partial = plan.read_text() + "### D001 — Preserve me\n"
            plan.write_text(partial)
            again = self.run_runner(project, "--bootstrap-control-contract")
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(plan.read_text(), partial)

    def test_untouched_scaffold_never_creates_plan_ready(self):
        guard = Path(__file__).with_name("control-guard.py")
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            self.run_runner(project, "--bootstrap-control-contract")
            result = subprocess.run(
                [sys.executable, str(guard), "--project", str(project), "--finalize-plan"],
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / ".opencode-v2/IMPLEMENTATION_PLAN.ready").exists())

    def test_manifest_conforming_to_exposed_schema_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (ctrl / "TEST_CHECKS.json").write_text(json.dumps({
                "required_files": [".opencode-v2/CONTROL_CONTRACT.md"],
                "checks": [{
                    "name": "project-local-contract",
                    "command": "test -f .opencode-v2/CONTROL_CONTRACT.md",
                    "timeout_seconds": 5,
                }],
            }))
            result = self.run_runner(project)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((ctrl / "TEST_REPORT.json").read_text())
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["checks_run"], 1)

    def test_malformed_manifest_is_rejected_before_commands_run(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            ctrl = project / ".opencode-v2"
            ctrl.mkdir()
            (ctrl / "TEST_CHECKS.json").write_text(json.dumps({
                "checks": [{"name": "bad", "command": 7}],
            }))
            result = self.run_runner(project)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid TEST_CHECKS.json", result.stderr)
            self.assertFalse((ctrl / "TEST_REPORT.json").exists())

    def test_project_local_runner_and_leaf_complete_execute(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (project / "artifact.txt").write_text("done\n")
            (ctrl / "TEST_CHECKS.json").write_text(json.dumps({
                "checks": [{"name": "artifact", "command": "test -s artifact.txt"}]
            }))
            run = subprocess.run([str(ctrl / "bin/run-checks")], cwd=project, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "leaves": {"D001": {"verify_command": "test -s artifact.txt", "owned_artifacts": "artifact.txt"}}
            }))
            (ctrl / "work").mkdir(exist_ok=True)
            (ctrl / "work/attempts.json").write_text(json.dumps({"owner": "supervisor", "deliverables": {"D001": {"count": 1, "sessions": ["s"]}}}))
            leaf = subprocess.run([str(ctrl / "bin/leaf-complete"), "D001"], cwd=project, text=True, capture_output=True)
            self.assertEqual(leaf.returncode, 0, leaf.stderr)
            self.assertTrue((ctrl / "work/D001.ready").exists())

    def test_leaf_complete_rejects_count_zero_or_non_supervisor_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td); self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (project / "artifact.txt").write_text("ok")
            (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves": {"D003": {"verify_command": "test -s artifact.txt", "owned_artifacts": "artifact.txt"}}}))
            (ctrl / "work").mkdir()
            (ctrl / "work/attempts.json").write_text(json.dumps({"owner": "model", "deliverables": {"D003": {"count": 0, "sessions": []}}}))
            leaf = subprocess.run([str(ctrl / "bin/leaf-complete"), "D003"], cwd=project, text=True, capture_output=True)
            self.assertNotEqual(leaf.returncode, 0)
            self.assertIn("attempt ledger owner is not supervisor", leaf.stderr)


class StatusTests(unittest.TestCase):
    def test_snapshot_is_derived_from_authoritative_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".opencode-v2"
            (root / "work").mkdir(parents=True)
            (root / "ACCEPTANCE.ready").write_text("status=complete\nartifact=ACCEPTANCE.md\nmarker=ACCEPTANCE_COMPLETE\nvalidated=deterministic-test\n")
            (root / "IMPLEMENTATION_PLAN.ready").write_text("status=complete\nartifact=IMPLEMENTATION_PLAN.md\nmarker=IMPLEMENTATION_PLAN_COMPLETE\nvalidated=deterministic-test\n")
            (root / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves": {"D001": {"launch_deps": []}, "D002": {"launch_deps": ["D001"]}}}))
            (root / "work/D001.ready").write_text("status=complete\ndeliverable=D001\nverified=true\n")
            (root / "work/attempts.json").write_text(json.dumps({"deliverables": {"D002": {"count": 2}}}))
            (root / "TEST_REPORT.json").write_text(json.dumps({"status": "pass", "checks_run": 2}))
            state = control_state.snapshot(td)
            self.assertTrue(state["acceptance"]["complete"])
            self.assertTrue(state["leaves"]["D001"]["complete"])
            self.assertTrue(state["leaves"]["D002"]["eligible"])
            self.assertTrue(state["tests"]["complete"])

    def test_three_planner_failures_are_exposed_as_implementation_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".opencode-v2"
            (root / "work").mkdir(parents=True)
            (root / "ACCEPTANCE.ready").write_text(
                "status=complete\nartifact=ACCEPTANCE.md\nmarker=ACCEPTANCE_COMPLETE\nvalidated=deterministic-test\n"
            )
            (root / "work/planner-restarts.json").write_text(json.dumps({"count": 3}))
            state = control_state.snapshot(td)
            self.assertTrue(state["plan"]["blocked"])
            self.assertEqual(state["resume_phase"], "implementation-blocked")


class PostSessionFinalizationTests(unittest.TestCase):
    RUNNER = Path(__file__).with_name("run-checks.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_project = supervisor.PROJECT
        supervisor.PROJECT = self.tmp.name
        project = Path(self.tmp.name)
        subprocess.run([sys.executable, str(self.RUNNER), "--project", self.tmp.name, "--bootstrap-control-contract"], check=True, capture_output=True)
        ctrl = project / ".opencode-v2"
        (ctrl / "work").mkdir(exist_ok=True)
        (ctrl / "work/attempts.json").write_text(json.dumps({"owner": "supervisor", "deliverables": {"D003": {"count": 2, "sessions": ["s"]}}}))

    def tearDown(self):
        supervisor.PROJECT = self.old_project
        self.tmp.cleanup()

    def manifest(self, verify="test -s artifact.txt"):
        ctrl = Path(self.tmp.name) / ".opencode-v2"
        (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves": {"D003": {"owned_artifacts": "artifact.txt", "verify_command": verify}}}))

    def test_step_limit_with_valid_artifacts_is_mechanically_finalized(self):
        self.manifest()
        (Path(self.tmp.name) / "artifact.txt").write_text("done\n")
        ok, detail = supervisor.post_session_finalize("D003")
        self.assertTrue(ok, detail)
        self.assertTrue((Path(self.tmp.name) / ".opencode-v2/work/D003.ready").exists())

    def test_step_limit_with_missing_or_invalid_artifacts_stays_incomplete(self):
        self.manifest()
        ok, detail = supervisor.post_session_finalize("D003")
        self.assertFalse(ok)
        self.assertEqual(detail, "owned-artifacts-missing")
        self.assertFalse((Path(self.tmp.name) / ".opencode-v2/work/D003.ready").exists())


class LessonsApiTests(unittest.TestCase):
    def test_lessons_uses_current_session_create_then_prompt_routes(self):
        class Fake(supervisor.OpenCodeHTTP):
            def __init__(self):
                self.calls = []
            def ensure(self):
                return True
            def request(self, method, path, payload=None, timeout=3):
                self.calls.append((method, path, payload, timeout))
                return {"data": {"id": "lessons-1"}} if path == "/api/session" else {}

        fake = Fake()
        old_project = supervisor.PROJECT
        supervisor.PROJECT = "/tmp/project"
        try:
            self.assertEqual(fake.start_lessons_session("retrospective"), (True, "lessons-1"))
        finally:
            supervisor.PROJECT = old_project
        self.assertEqual(fake.calls[0][0:2], ("POST", "/api/session"))
        self.assertEqual(fake.calls[0][2]["agent"], "lessons-learner")
        self.assertEqual(fake.calls[0][2]["location"], {"directory": "/tmp/project"})
        self.assertNotIn("title", fake.calls[0][2])
        self.assertEqual(fake.calls[1][0:2], ("POST", "/api/session/lessons-1/prompt"))
        self.assertEqual(fake.calls[1][2], {"prompt": {"text": "retrospective"}, "delivery": "steer"})


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_nonzero_required_executable_can_never_finalize_as_pass(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"
            ctrl.mkdir()
            (ctrl / "ACCEPTANCE.md").write_text(
                "# Acceptance Contract\n- [ ] A001: command-backed requirement\n"
            )
            report = {
                "result": "PASS",
                "checks": [{
                    "id": "A001", "status": "PASS", "evidence": "validator prose",
                    "required_executable": True, "command": "false", "exit_code": 1,
                }],
            }
            (ctrl / "acceptance-report.json").write_text(json.dumps(report))
            finalizer = Path(__file__).with_name("finalize-acceptance.py")
            failed = subprocess.run([sys.executable, str(finalizer), td], text=True, capture_output=True)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("A001=executable-exit-1", failed.stderr)
            self.assertFalse((ctrl / "acceptance-pass.json").exists())
            report["checks"][0]["exit_code"] = 0
            (ctrl / "acceptance-report.json").write_text(json.dumps(report))
            passed = subprocess.run([sys.executable, str(finalizer), td], text=True, capture_output=True)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertTrue((ctrl / "acceptance-pass.json").exists())


if __name__ == "__main__":
    unittest.main()
