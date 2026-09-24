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
import operator_control


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

    def test_live_beta_location_directory_scopes_the_session(self):
        shape = supervisor.message_shape(
            [message()], {"parentID": "root", "location": {"directory": "/tmp/live-project"}}
        )
        self.assertEqual(shape["directory"], "/tmp/live-project")

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
        self.assertEqual(ledger["protocol"], supervisor.ATTEMPT_LEDGER_PROTOCOL)

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

    def _manifest(self, leaves=None):
        root = Path(self.tmp.name) / ".opencode-v2"
        root.mkdir(exist_ok=True)
        leaves = leaves or {
            "D004": {"role": "implementer", "launch_deps": [], "owned_artifacts": "d004.txt", "verify_command": "test -f d004.txt"},
            "D008": {"role": "test-builder", "launch_deps": [], "owned_artifacts": "d008.txt", "verify_command": "test -f d008.txt"},
        }
        (root / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves": leaves}))
        return root, leaves

    def _exhaust(self, did):
        root, _ = self._manifest()
        root.joinpath("work").mkdir(exist_ok=True)
        root.joinpath("work/attempts.json").write_text(json.dumps({
            "owner": "supervisor", "deliverables": {did: {"count": 3, "sessions": ["one", "two", "three"]}},
        }))
        return root

    def test_operator_grant_permits_truthful_fourth_attempt_without_reset(self):
        root = self._exhaust("D004")
        self.assertEqual(supervisor.claim_attempt("automatic-four", "D004"), ("limit", 3))
        self.assertEqual([did for did, _ in supervisor.grant_operator_retry(["D004"])], ["D004"])
        before = json.loads((root / "work/attempts.json").read_text())["deliverables"]["D004"]
        self.assertEqual(before["count"], 3)
        self.assertEqual(before["sessions"], ["one", "two", "three"])
        self.assertEqual(before["automatic_limit"], 3)
        self.assertEqual(before["operator_retry_grants"], 1)
        self.assertEqual(before["operator_overrides"][0]["source"], "operator-cli")
        self.assertEqual(supervisor.claim_attempt("human-four", "D004"), ("claimed", 4))
        after = json.loads((root / "work/attempts.json").read_text())["deliverables"]["D004"]
        self.assertEqual(after["count"], 4)
        self.assertEqual(after["sessions"][:3], ["one", "two", "three"])
        projected = control_state.attempt_state(after)
        self.assertTrue(projected["operator_authorized_attempt"])
        self.assertEqual(projected["operator_grants_remaining"], 0)
        self.assertEqual(supervisor.claim_attempt("automatic-five", "D004"), ("limit", 4))

    def test_unauthorized_count_four_remains_invalid(self):
        root, _ = self._manifest()
        (root / "work").mkdir(exist_ok=True)
        (root / "work/attempts.json").write_text(json.dumps({
            "owner": "supervisor", "deliverables": {"D004": {"count": 4, "sessions": ["old"]}},
        }))
        self.assertEqual(supervisor.claim_attempt("old", "D004"), ("invalid", 4))

    def test_runtime_contract_repair_releases_only_reclassified_attempts(self):
        root, _ = self._manifest()
        (root / "work").mkdir(exist_ok=True)
        entry={
            "count":3,
            "sessions":["infra","bad-plan-1","bad-plan-2"],
            "automatic_limit":2,
            "infrastructure_retry_grants":1,
            "infrastructure_failures":[{
                "source":"supervisor","kind":"runtime-cancel","grant":1,
                "timestamp":"2026-09-21T00:00:00Z","session":"infra",
                "evidence":"no-owned-artifact-or-progress",
            }],
            "failure_history":[
                {"attempt":1,"classification":"infrastructure"},
                {"attempt":2,"classification":"bad-plan",
                 "reclassified_by":"runtime-parent-contract-repair"},
                {"attempt":3,"classification":"bad-plan",
                 "reclassified_by":"runtime-parent-contract-repair"},
            ],
            "parent_contract_repair_resolution":{
                "structured_key":"fixture_manifest",
                "reclassified_attempts":[2,3],
            },
        }
        (root / "work/attempts.json").write_text(json.dumps({
            "owner":"supervisor","deliverables":{"D004":entry},
        }))
        projected=control_state.attempt_state(entry)
        self.assertTrue(projected["valid"])
        self.assertEqual(projected["bad_plan_retry_grants"],2)
        self.assertEqual(projected["automatic_attempts_consumed"],0)
        self.assertEqual(projected["allowed_attempts"],5)
        self.assertEqual(supervisor.claim_attempt("repaired-one","D004"),("claimed",4))
        saved=json.loads((root / "work/attempts.json").read_text())["deliverables"]["D004"]
        self.assertFalse(saved.get("operator_retry_attempts"))

    def test_plain_bad_plan_does_not_create_a_contract_repair_credit(self):
        entry={
            "count":3,"sessions":["one","two","three"],"automatic_limit":3,
            "failure_history":[{"attempt":3,"classification":"bad-plan"}],
        }
        projected=control_state.attempt_state(entry)
        self.assertTrue(projected["valid"])
        self.assertEqual(projected["bad_plan_retry_grants"],0)
        self.assertEqual(projected["allowed_attempts"],3)

    def test_unresolved_reclassification_does_not_reopen_execution(self):
        entry={
            "count":3,"sessions":["one","two","three"],"automatic_limit":3,
            "failure_history":[{
                "attempt":3,"classification":"bad-plan",
                "reclassified_by":"runtime-parent-contract-repair",
            }],
        }
        projected=control_state.attempt_state(entry)
        self.assertEqual(projected["bad_plan_retry_grants"],0)
        self.assertEqual(projected["allowed_attempts"],3)

    def test_plan_contract_revision_releases_one_replacement_slot(self):
        entry={
            "count":2,"sessions":["one","two"],"automatic_limit":2,
            "plan_contract_revisions":[{
                "attempt":2,"source":"supervisor-plan-contract-revision",
                "current_verify_sha256":"abc",
            }],
        }
        projected=control_state.attempt_state(entry)
        self.assertEqual(projected["plan_contract_retry_grants"],1)
        self.assertEqual(projected["automatic_attempts_consumed"],1)
        self.assertEqual(projected["allowed_attempts"],3)

    def test_plan_contract_revision_validates_its_materialized_replacement(self):
        entry={
            "count":3,"sessions":["one","two","dispatch:three"],"automatic_limit":2,
            "plan_contract_revisions":[{
                "attempt":2,"source":"supervisor-plan-contract-revision",
                "current_verify_sha256":"abc",
            }],
            # Historical buggy projection recorded this replacement as reserved;
            # it must not consume a human grant after the durable credit exists.
            "operator_retry_attempts":[{
                "sequence":3,"session":"dispatch:three","state":"reserved",
                "consumes_operator_grant":False,"source":"supervisor",
            }],
        }
        projected=control_state.attempt_state(entry)
        self.assertTrue(projected["valid"])
        self.assertEqual(projected["plan_contract_retry_grants"],1)
        self.assertEqual(projected["operator_grants_reserved"],0)
        self.assertEqual(projected["allowed_attempts"],3)

    def test_plan_contract_replacement_is_not_operator_authorized(self):
        entry={"count":3,"sessions":["one","two","three"],"automatic_limit":2,
               "plan_contract_revisions":[{"attempt":2,"source":"supervisor-plan-contract-revision","current_verify_sha256":"abc"}],
               "operator_retry_attempts":[{"sequence":3,"session":"three","state":"reserved","consumes_operator_grant":False,"source":"supervisor"}]}
        self.assertFalse(control_state.attempt_state(entry)["operator_authorized_attempt"])

    def test_reconcile_plan_revision_revokes_only_stale_ready(self):
        old_root,old_log,old_csv=supervisor.ROOT,supervisor.LOG,supervisor.CSV
        try:
            supervisor.ROOT=Path(self.tmp.name)
            supervisor.LOG=Path(self.tmp.name)/"supervisor-events.log"
            supervisor.CSV=Path(self.tmp.name)/"supervisor-events.csv"
            root=Path(self.tmp.name)/".opencode-v2"; work=root/"work"
            work.mkdir(parents=True)
            current="test -s revised.txt"; old="test -s old.txt"
            (root/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "leaves":{"D004":{"verify_command":current}},
            }))
            (work/"D004.ready").write_text(
                "status=complete\ndeliverable=D004\nattempt=2\nverified=true\n"
                "owner=supervisor\nprotocol=v2-leaf-ready-v1\n"
            )
            (work/"D004.verify-evidence.json").write_text(json.dumps({
                "owner":"supervisor","protocol":"v2-supervisor-verify-evidence-v1",
                "deliverable":"D004","entries":[],
                "latest":{"command":old,"result":"verified"},
            }))
            (work/"attempts.json").write_text(json.dumps({
                "owner":"supervisor","deliverables":{"D004":{
                    "count":2,"sessions":["one","two"],"automatic_limit":2,
                }},
            }))
            self.assertEqual(supervisor.reconcile_plan_contract_revisions(),["D004"])
            self.assertFalse((work/"D004.ready").exists())
            entry=json.loads((work/"attempts.json").read_text())["deliverables"]["D004"]
            self.assertEqual(control_state.attempt_state(entry)["allowed_attempts"],3)
        finally:
            supervisor.ROOT,supervisor.LOG,supervisor.CSV=old_root,old_log,old_csv

    def test_retry_failed_and_selected_retry_preserve_plan_and_scope(self):
        root, leaves = self._manifest()
        (root / "work").mkdir(exist_ok=True)
        (root / "work/attempts.json").write_text(json.dumps({
            "owner": "supervisor",
            "deliverables": {
                "D004": {"count": 3, "sessions": ["a", "b", "c"]},
                "D008": {"count": 3, "sessions": ["d", "e", "f"]},
            },
        }))
        original = json.loads((root / "IMPLEMENTATION_PLAN.guard.json").read_text())
        self.assertEqual(operator_control.exhausted_incomplete(self.tmp.name), ["D004", "D008"])
        self.assertEqual(operator_control.grant(self.tmp.name, ["D004"]), ["D004"])
        self.assertEqual(operator_control.exhausted_incomplete(self.tmp.name), ["D008"])
        ledger = json.loads((root / "work/attempts.json").read_text())
        self.assertEqual(ledger["deliverables"]["D004"]["operator_retry_grants"], 1)
        self.assertNotIn("operator_retry_grants", ledger["deliverables"]["D008"])
        self.assertEqual(json.loads((root / "IMPLEMENTATION_PLAN.guard.json").read_text()), original)

    def test_second_explicit_grant_permits_attempt_five_only_after_four_fails(self):
        self._exhaust("D004")
        supervisor.grant_operator_retry(["D004"])
        self.assertEqual(supervisor.claim_attempt("four", "D004"), ("claimed", 4))
        self.assertEqual(supervisor.claim_attempt("five", "D004"), ("limit", 4))
        supervisor.grant_operator_retry(["D004"])
        self.assertEqual(supervisor.claim_attempt("five", "D004"), ("claimed", 5))

    def test_operator_reservation_releases_after_proven_preexecution_abort(self):
        root = self._exhaust("D004")
        supervisor.grant_operator_retry(["D004"])
        self.assertEqual(supervisor.claim_attempt("dispatch:four", "D004"), ("claimed", 4))
        self.assertEqual(supervisor.claim_attempt("child-four", "D004"), ("existing", 4))
        self.assertEqual(
            supervisor.release_operator_reservation("child-four", "D004", "zero-token runtime abort"),
            (True, "released"),
        )
        entry = json.loads(root.joinpath("work/attempts.json").read_text())["deliverables"]["D004"]
        self.assertEqual(entry["count"], 4)  # historical sequence never rewrites
        self.assertEqual(entry["operator_retry_attempts"][0]["outcome"], "infrastructure_abort")
        self.assertFalse(entry["operator_retry_attempts"][0]["consumes_operator_grant"])
        state = control_state.attempt_state(entry)
        self.assertEqual(state["operator_grants_remaining"], 1)
        self.assertEqual(state["operator_infrastructure_aborted"], 1)
        self.assertEqual(supervisor.claim_attempt("dispatch:five", "D004"), ("claimed", 5))

    def test_genuine_execution_consumes_reserved_operator_grant(self):
        self._exhaust("D004")
        supervisor.grant_operator_retry(["D004"])
        supervisor.claim_attempt("dispatch:four", "D004")
        supervisor.claim_attempt("child-four", "D004")
        self.assertEqual(
            supervisor.consume_operator_reservation("child-four", "D004", "owned-artifact-or-progress"),
            (True, "consumed"),
        )
        entry = json.loads(Path(self.tmp.name, ".opencode-v2/work/attempts.json").read_text())["deliverables"]["D004"]
        state = control_state.attempt_state(entry)
        self.assertEqual(state["operator_grants_used"], 1)
        self.assertEqual(state["operator_grants_remaining"], 0)
        self.assertEqual(supervisor.claim_attempt("five", "D004"), ("limit", 4))

    def test_repeated_operator_infrastructure_abort_blocks_without_free_loop(self):
        self._exhaust("D004")
        supervisor.grant_operator_retry(["D004"])
        supervisor.claim_attempt("dispatch:four", "D004")
        supervisor.claim_attempt("child-four", "D004")
        self.assertEqual(supervisor.release_operator_reservation("child-four", "D004", "first"), (True, "released"))
        supervisor.claim_attempt("dispatch:five", "D004")
        supervisor.claim_attempt("child-five", "D004")
        self.assertEqual(
            supervisor.release_operator_reservation("child-five", "D004", "second"),
            (False, "infrastructure-retry-limit"),
        )
        entry = json.loads(Path(self.tmp.name, ".opencode-v2/work/attempts.json").read_text())["deliverables"]["D004"]
        state = control_state.attempt_state(entry)
        self.assertTrue(state["valid"])
        self.assertEqual(state["operator_infrastructure_aborted"], 1)
        self.assertEqual(state["operator_infrastructure_blocked"], 1)
        self.assertEqual(state["operator_grants_remaining"], 0)
        self.assertEqual(supervisor.claim_attempt("six", "D004"), ("limit", 5))

    def test_stale_dispatch_placeholders_do_not_reject_operator_child_binding(self):
        root = self._exhaust("D004")
        ledger_path = root / "work/attempts.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["deliverables"]["D004"]["sessions"] = ["dispatch:old-one", "dispatch:old-two", "three"]
        ledger_path.write_text(json.dumps(ledger))
        supervisor.grant_operator_retry(["D004"])
        self.assertEqual(supervisor.claim_attempt("dispatch:new", "D004"), ("claimed", 4))
        self.assertEqual(supervisor.claim_attempt("actual-child", "D004"), ("existing", 4))
        entry = json.loads(ledger_path.read_text())["deliverables"]["D004"]
        self.assertIn("actual-child", entry["sessions"])
        self.assertTrue(control_state.attempt_state(entry)["valid"])

    def test_snapshot_exposes_operator_and_infrastructure_accounting(self):
        root, _ = self._manifest()
        root.joinpath("work").mkdir(exist_ok=True)
        root.joinpath("work/attempts.json").write_text(json.dumps({
            "owner": "supervisor", "deliverables": {"D004": {
                "count": 4, "sessions": ["one", "two", "three", "four"],
                "automatic_limit": 3, "operator_retry_grants": 1,
                "operator_overrides": [{"timestamp":"2026-01-01T00:00:00Z","grant":1,"source":"operator-cli","reason":"test"}],
                "operator_retry_attempts": [{"sequence":4,"session":"four","source":"supervisor","state":"infrastructure_abort","outcome":"infrastructure_abort","consumes_operator_grant":False}],
            }},
        }))
        leaf = control_state.snapshot(self.tmp.name)["leaves"]["D004"]
        self.assertEqual(leaf["total_dispatches"], 4)
        self.assertEqual(leaf["automatic_attempts_consumed"], 3)
        self.assertEqual(leaf["operator_grants_used"], 0)
        self.assertEqual(leaf["operator_grants_remaining"], 1)
        self.assertEqual(leaf["operator_infrastructure_aborted"], 1)
        self.assertTrue(leaf["eligible"])

    def test_zero_token_zero_tool_aborted_session_is_the_only_auto_release_evidence(self):
        class Cursor:
            def __init__(self, rows): self.rows = rows
            def fetchall(self): return self.rows
        class Connection:
            def __init__(self, rows): self.rows = rows
            def execute(self, *_): return Cursor(self.rows)
            def close(self): pass
        old_connect = supervisor.db_connect
        try:
            aborted = json.dumps({"finish":"error","error":{"type":"aborted"},"content":[]})
            supervisor.db_connect = lambda: Connection([("user", "{}"), ("assistant", aborted)])
            self.assertIn("zero-token-zero-tool", supervisor.immediate_runtime_abort("s"))
            tool = json.dumps({"type":"tool","id":"t"})
            supervisor.db_connect = lambda: Connection([("assistant", aborted), ("part", tool)])
            self.assertEqual(supervisor.immediate_runtime_abort("s"), "")
            completed_tool = json.dumps({"content":[{"type":"tool","id":"t","state":{"status":"completed"}}]})
            supervisor.db_connect = lambda: Connection([(completed_tool,)])
            self.assertEqual(supervisor.meaningful_worker_execution("s", "D404"), "completed-worker-tool-action")
        finally:
            supervisor.db_connect = old_connect

    def test_one_pre_artifact_compaction_failure_gets_a_bounded_auditable_slot(self):
        root, _ = self._manifest({
            "D008": {"role": "tester", "launch_deps": [], "owned_artifacts": "`tests/ephemeris.test.mjs`.", "verify_command": "test -f tests/ephemeris.test.mjs"},
        })
        root.joinpath("work").mkdir(exist_ok=True)
        root.joinpath("work/attempts.json").write_text(json.dumps({
            "owner": "supervisor", "deliverables": {"D008": {"count": 1, "sessions": ["compact-fail"]}},
        }))
        self.assertEqual(
            supervisor.record_compaction_infrastructure_failure("compact-fail", "D008"),
            (True, "granted"),
        )
        entry = json.loads(root.joinpath("work/attempts.json").read_text())["deliverables"]["D008"]
        projected = control_state.attempt_state(entry)
        self.assertTrue(projected["valid"])
        self.assertEqual(projected["infrastructure_retry_grants"], 1)
        self.assertEqual(projected["operator_grants_remaining"], 0)
        self.assertEqual(entry["infrastructure_failures"][0]["kind"], "opencode-compaction-template")
        self.assertEqual(operator_control.exhausted_incomplete(self.tmp.name), [])
        self.assertEqual(supervisor.claim_attempt("real-2", "D008"), ("claimed", 2))
        self.assertEqual(supervisor.claim_attempt("real-3", "D008"), ("claimed", 3))
        self.assertEqual(supervisor.claim_attempt("real-4", "D008"), ("claimed", 4))
        self.assertEqual(supervisor.claim_attempt("real-5", "D008"), ("limit", 4))

    def test_compaction_credit_is_rejected_after_any_durable_worker_state_or_second_failure(self):
        root, _ = self._manifest({
            "D008": {"role": "tester", "launch_deps": [], "owned_artifacts": "`artifact.txt`.", "verify_command": "test -f artifact.txt"},
        })
        root.joinpath("work").mkdir(exist_ok=True)
        root.joinpath("work/attempts.json").write_text(json.dumps({
            "owner": "supervisor", "deliverables": {"D008": {"count": 1, "sessions": ["compact-fail"]}},
        }))
        Path(self.tmp.name, "artifact.txt").write_text("partial")
        self.assertEqual(
            supervisor.record_compaction_infrastructure_failure("compact-fail", "D008"),
            (False, "durable-worker-state-present"),
        )
        Path(self.tmp.name, "artifact.txt").unlink()
        self.assertEqual(
            supervisor.record_compaction_infrastructure_failure("compact-fail", "D008"),
            (True, "granted"),
        )
        self.assertEqual(
            supervisor.record_compaction_infrastructure_failure("other", "D008"),
            (False, "session-not-in-ledger"),
        )


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

    def test_gametest2x_raw_beta_placeholder_and_success_shapes_normalize_identically(self):
        # Exact pre-dispatch V2-hook inputs from gametest2x: the first call
        # retained the beta/root's literal Dxxx placeholder; the retry carried
        # D001. Both have the same already-bound deliverable in line one.
        failed_raw = (
            "DELIVERABLE: D001\n"
            "Read your Dxxx section in .opencode-v2/IMPLEMENTATION_PLAN.md.\n"
            "Read .opencode-v2/work/D001.progress.md if present.\n"
            "Inspect your owned project artifacts as they currently exist.\n"
            "Continue from actual filesystem state and execute the deliverable."
        )
        successful_raw = supervisor.implementation_prompt("D001")
        self.assertEqual(
            supervisor.normalize_implementation_prompt(failed_raw), successful_raw
        )
        self.assertEqual(
            supervisor.normalize_implementation_prompt(successful_raw), successful_raw
        )
        self.assertEqual(supervisor.implementation_prompt_violation(failed_raw), "")
        self.assertEqual(supervisor.implementation_prompt_violation(successful_raw), "")

    def test_placeholder_normalization_does_not_allow_wrong_or_appended_handoff(self):
        placeholder = supervisor.implementation_prompt("D001").replace(
            "Read your D001 section", "Read your Dxxx section"
        )
        self.assertEqual(
            supervisor.implementation_prompt_violation(
                placeholder.replace("D001.progress.md", "D002.progress.md")
            ),
            "noncanonical_or_model_derived_handoff",
        )
        self.assertEqual(
            supervisor.implementation_prompt_violation(placeholder + "\nPrior worker said done."),
            "noncanonical_or_model_derived_handoff",
        )

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

    def test_denied_preclaim_keeps_zero_attempts_and_placeholder_claims_once(self):
        bad = supervisor.implementation_prompt("D001") + "\nmodel-derived handoff"
        self.assertEqual(
            supervisor.preclaim_attempt("probe-builder", bad, "bad")[:1], ("denied",)
        )
        self.assertFalse((Path(self.tmp.name) / ".opencode-v2/work/attempts.json").exists())
        beta_placeholder = supervisor.implementation_prompt("D001").replace(
            "Read your D001 section", "Read your Dxxx section"
        )
        self.assertEqual(
            supervisor.preclaim_attempt("probe-builder", beta_placeholder, "beta"),
            ("claimed", "D001", "", 1),
        )

    def test_preclaim_baseline_detects_probe_write_to_d002_owned_package(self):
        self.assertEqual(
            supervisor.preclaim_attempt("probe-builder", supervisor.implementation_prompt("D001"), "own"),
            ("claimed", "D001", "", 1),
        )
        Path(self.tmp.name, "package.json").write_text('{"bad":"D001"}\n')
        self.assertEqual(
            supervisor.ownership_violations("D001"), ["package.json"]
        )


class ProbeProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_project = supervisor.PROJECT
        supervisor.PROJECT = self.tmp.name
        supervisor.worker_progress.clear()
        ctrl = Path(self.tmp.name, ".opencode-v2")
        ctrl.mkdir(); (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "leaves": {"D001": {"owned_artifacts": "`notes/PROBE.md`"}}
        }))

    def tearDown(self):
        supervisor.PROJECT = self.old_project
        supervisor.worker_progress.clear()
        self.tmp.cleanup()

    def test_probe_research_turns_without_durable_progress_recycle_early(self):
        for turn in range(1, supervisor.PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS):
            self.assertEqual(supervisor.probe_loop_reason("probe", "probe-builder", "D001", f"tool-{turn}"), "")
        self.assertIn(
            "probe_research_loop_no_owned_progress",
            supervisor.probe_loop_reason("probe", "probe-builder", "D001", "tool-final"),
        )

    def test_owned_probe_or_progress_write_resets_the_loop_budget(self):
        for turn in range(1, supervisor.PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS):
            supervisor.probe_loop_reason("probe", "probe-builder", "D001", f"tool-{turn}")
        probe = Path(self.tmp.name, "notes/PROBE.md")
        probe.parent.mkdir(); probe.write_text("node=present\n")
        self.assertEqual(supervisor.probe_loop_reason("probe", "probe-builder", "D001", "write-probe"), "")
        self.assertEqual(supervisor.probe_loop_reason("probe", "probe-builder", "D001", "verify"), "")

class BoundedChildResultTests(unittest.TestCase):
    PLUGIN = Path(__file__).parents[1] / "xdg/config/opencode/plugins/v2-bounded-subagent.js"

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

    def test_internal_policy_prohibits_external_astronomy_probe_requirements(self):
        acceptance = (self.AGENTS / "acceptance-planner.md").read_text()
        planner = (self.AGENTS / "implementation-planner.md").read_text()
        probe = (self.AGENTS / "probe-builder.md").read_text()
        for text in (acceptance, planner, probe):
            self.assertIn("Skyfield", text)
        for text in (acceptance, planner):
            self.assertIn("as seen from Earth", text)
        self.assertIn('"package.json": deny', probe)
        self.assertIn("After at most a handful of probe tool turns", probe)

    def test_workers_are_told_to_follow_the_beta_compaction_template_without_tags(self):
        global_rules = (self.AGENTS.parent / "AGENTS.md").read_text()
        self.assertIn("every exact heading supplied by that request", global_rules)
        self.assertIn("do not include\n  its literal `<template>` tags", global_rules)
        self.assertIn("project files, not the summary prose", global_rules)
        beta = Path(__file__).parents[1] / "runtime/opencode2/lib/node_modules/@opencode-ai/cli/node_modules/@opencode-ai/cli-linux-x64/bin/opencode2"
        text = beta.read_bytes().decode("utf-8", "replace")
        self.assertIn("Compaction summary did not match the required template", text)
        self.assertIn("Do not include the <template> tags in your response.", text)

    def test_bounded_child_plugin_uses_supported_global_plugin_directory(self):
        config = (self.AGENTS.parent / "opencode.jsonc").read_text()
        plugin = self.AGENTS.parent / "plugins/v2-bounded-subagent.js"
        self.assertTrue(plugin.is_file())
        # This beta auto-loads local .js/.ts modules from plugins/. A file URL
        # to the module is rejected, and it does not scan .mjs modules.
        self.assertNotIn('"plugin"', config)
        self.assertFalse((self.AGENTS.parent / "plugins/v2-bounded-subagent.mjs").exists())

    def test_orchestrator_does_not_instruct_unavailable_list_or_execute_tools(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertIn("`list` and `execute` are unavailable", root)
        self.assertNotIn("use `list`", root)

    def test_operator_retry_is_human_cli_only_and_never_a_model_planner_escape(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        config = (self.AGENTS.parent / "opencode.jsonc").read_text()
        self.assertIn("operator-control.py", root)
        self.assertIn("Never infer a retry grant", root)
        self.assertIn("Never invoke implementation-planner merely", root)
        self.assertIn('"resource": "*operator-control.py*"', config)
        self.assertIn('"resource": "*operator_control.py*"', config)
        self.assertIn('"effect": "deny"', config)

    def test_dispatch_plugin_preclaims_before_child_and_root_forbids_salvage(self):
        plugin = (self.AGENTS.parent / "plugins/v2-bounded-subagent.js").read_text()
        self.assertIn('api.tool.hook("execute.before"', plugin)
        self.assertIn("--claim-dispatch", plugin)
        self.assertIn("event.input", plugin)
        self.assertIn("before OpenCode materializes", plugin)
        self.assertIn("event.result.content", plugin)
        self.assertIn('id: "v2-bounded-subagent"', plugin)
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

    def test_root_has_only_the_exact_readonly_status_shell_capability(self):
        root = (self.AGENTS / "orchestrator.md").read_text()
        self.assertIn("sole shell authority", root)
        self.assertIn("Do not delegate it", root)
        self.assertIn("immediately re-run `control-status`", root)
        self.assertIn('".opencode-v2/bin/control-status": allow', root)
        self.assertIn('"*": deny', root)
        config = json.loads((self.AGENTS.parent / "opencode.jsonc").read_text())
        self.assertEqual(
            config["providers"]["syv"]["models"]["qwen38-orchestrator"]["body"]["chat_template_kwargs"],
            {"enable_thinking": False, "preserve_thinking": False},
        )

    def test_task_splitter_is_single_write_and_completion_hook_is_authoritative(self):
        splitter = (self.AGENTS / "task-splitter.md").read_text()
        plugin = (self.AGENTS.parent / "plugins/v2-bounded-subagent.js").read_text()
        self.assertIn("steps: 3", splitter)
        self.assertIn('"protocol": "v2-task-split-proposal-v1"', splitter)
        self.assertIn("read the request, then\nwrite the proposal", splitter)
        self.assertIn("--claim-splitter", plugin)
        self.assertIn("--complete-splitter", plugin)
        self.assertIn("splitter for the same generation", plugin)
        self.assertIn("materializeSplitterProposal", plugin)
        self.assertIn("proposalFromOutput", plugin)
        self.assertIn("never derive children, scopes, IDs", plugin)

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

    def test_internal_policy_rejects_jupiter_external_reference_probe(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"; ctrl.mkdir()
            (ctrl / "ACCEPTANCE.md").write_text(
                "# Acceptance Contract\nReference policy: internal\n"
                "- [ ] A001: local model\n<!-- ACCEPTANCE_COMPLETE -->\n"
            )
            plan = self.plan(40).replace(
                "### D001 — Tests", "### D001 — JPL Skyfield feasibility probe"
            ).replace(
                "- Role: tester", "- Role: probe-builder"
            ).replace(
                "- Verify command: `.opencode-v2/bin/run-checks`",
                "- Verify command: `python3 -c \"import skyfield\"`",
            )
            (ctrl / "IMPLEMENTATION_PLAN.md").write_text(plan)
            rejected = self.validate(td)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("internal Reference policy forbids", (ctrl / "IMPLEMENTATION_PLAN.guard-errors.txt").read_text())

    def test_correct_earth_view_wording_allows_internal_but_named_external_truth_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            ctrl = Path(td) / ".opencode-v2"; ctrl.mkdir()
            acceptance = ctrl / "ACCEPTANCE.md"
            acceptance.write_text(
                "# Acceptance Contract\nReference policy: internal\n"
                "- [ ] A001: positions are correct as seen from Earth.\n"
                "<!-- ACCEPTANCE_COMPLETE -->\n"
            )
            allowed = subprocess.run(
                [sys.executable, str(self.GUARD), "--project", td, "--finalize-acceptance"],
                text=True, capture_output=True,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            acceptance.write_text(acceptance.read_text().replace(
                "positions are correct", "Skyfield/JPL verifies positions are correct"
            ))
            rejected = subprocess.run(
                [sys.executable, str(self.GUARD), "--project", td, "--check-acceptance"],
                text=True, capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("internal Reference policy cannot require", rejected.stdout)


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
            self.assertIn("v2-attempt-ledger-v1", contract)
            self.assertIn("historical ledger label", contract)
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

    def test_leaf_complete_rejects_unowned_file_created_after_preclaim_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td); self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (project / "probe.txt").write_text("ok")
            (project / "package.json").write_text("unowned")
            (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "leaves": {"D001": {"verify_command": "test -s probe.txt", "owned_artifacts": "probe.txt"}}
            }))
            (ctrl / "work").mkdir()
            (ctrl / "work/attempts.json").write_text(json.dumps({"owner": "supervisor", "deliverables": {"D001": {"count": 1, "sessions": ["s"]}}}))
            (ctrl / "work/D001.ownership-baseline.json").write_text(json.dumps({
                "owner": "supervisor", "deliverable": "D001", "files": {}
            }))
            leaf = subprocess.run([str(ctrl / "bin/leaf-complete"), "D001"], cwd=project, text=True, capture_output=True)
            self.assertNotEqual(leaf.returncode, 0)
            self.assertIn("package.json", leaf.stderr)
            self.assertFalse((ctrl / "work/D001.ready").exists())

    def test_leaf_complete_accepts_operator_authorized_attempt_four(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td); self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (project / "artifact.txt").write_text("ok")
            (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "leaves": {"D004": {"verify_command": "test -s artifact.txt", "owned_artifacts": "artifact.txt"}},
            }))
            (ctrl / "work").mkdir()
            (ctrl / "work/attempts.json").write_text(json.dumps({
                "owner": "supervisor", "deliverables": {"D004": {
                    "count": 4, "sessions": ["one", "two", "three", "four"],
                    "automatic_limit": 3, "operator_retry_grants": 1,
                    "operator_overrides": [{"timestamp": "2026-01-01T00:00:00Z", "grant": 1,
                                             "source": "operator-cli", "reason": "explicit operator retry command"}],
                }},
            }))
            leaf = subprocess.run([str(ctrl / "bin/leaf-complete"), "D004"], cwd=project, text=True, capture_output=True)
            self.assertEqual(leaf.returncode, 0, leaf.stderr)
            self.assertIn("attempt=4", (ctrl / "work/D004.ready").read_text())

    def test_leaf_complete_accepts_one_valid_infrastructure_recovery_attempt_four(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td); self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (project / "artifact.txt").write_text("ok")
            (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "leaves": {"D008": {"verify_command": "test -s artifact.txt", "owned_artifacts": "`artifact.txt`."}},
            }))
            (ctrl / "work").mkdir()
            (ctrl / "work/attempts.json").write_text(json.dumps({
                "owner": "supervisor", "deliverables": {"D008": {
                    "count": 4, "sessions": ["one", "two", "three", "four"],
                    "automatic_limit": 3, "infrastructure_retry_grants": 1,
                    "infrastructure_failures": [{
                        "timestamp": "2026-01-01T00:00:00Z", "grant": 1,
                        "source": "supervisor", "kind": "opencode-compaction-template",
                        "session": "one", "evidence": "no-owned-artifact-or-progress",
                    }],
                }},
            }))
            leaf = subprocess.run([str(ctrl / "bin/leaf-complete"), "D008"], cwd=project, text=True, capture_output=True)
            self.assertEqual(leaf.returncode, 0, leaf.stderr)
            self.assertIn("attempt=4", (ctrl / "work/D008.ready").read_text())

    def test_leaf_complete_accepts_historical_attempt_five_after_released_operator_reservation(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td); self.run_runner(project, "--bootstrap-control-contract")
            ctrl = project / ".opencode-v2"
            (project / "artifact.txt").write_text("ok")
            (ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "leaves": {"D004": {"verify_command": "test -s artifact.txt", "owned_artifacts": "artifact.txt"}},
            }))
            (ctrl / "work").mkdir()
            override={"timestamp":"2026-01-01T00:00:00Z","grant":1,"source":"operator-cli","reason":"retry"}
            (ctrl / "work/attempts.json").write_text(json.dumps({
                "owner":"supervisor", "deliverables":{"D004":{
                    "count":5,"sessions":["one","two","three","four","five"],
                    "automatic_limit":3,"operator_retry_grants":1,"operator_overrides":[override],
                    "operator_retry_attempts":[
                        {"sequence":4,"session":"four","source":"supervisor","state":"infrastructure_abort","outcome":"infrastructure_abort","consumes_operator_grant":False},
                        {"sequence":5,"session":"five","source":"supervisor","state":"consumed","outcome":"meaningful_execution","consumes_operator_grant":True},
                    ],
                }},
            }))
            leaf = subprocess.run([str(ctrl / "bin/leaf-complete"), "D004"], cwd=project, text=True, capture_output=True)
            self.assertEqual(leaf.returncode, 0, leaf.stderr)
            self.assertIn("attempt=5", (ctrl / "work/D004.ready").read_text())


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

    def test_operator_grant_reopens_execution_without_replanning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".opencode-v2"
            (root / "work").mkdir(parents=True)
            (root / "ACCEPTANCE.ready").write_text("status=complete\nartifact=ACCEPTANCE.md\nmarker=ACCEPTANCE_COMPLETE\nvalidated=deterministic-test\n")
            (root / "IMPLEMENTATION_PLAN.ready").write_text("status=complete\nartifact=IMPLEMENTATION_PLAN.md\nmarker=IMPLEMENTATION_PLAN_COMPLETE\nvalidated=deterministic-test\n")
            (root / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({"leaves": {"D004": {"launch_deps": []}}}))
            ledger = {"owner": "supervisor", "deliverables": {"D004": {"count": 3, "sessions": ["a", "b", "c"]}}}
            (root / "work/attempts.json").write_text(json.dumps(ledger))
            blocked = control_state.snapshot(td)
            self.assertEqual(blocked["resume_phase"], "execution-blocked")
            self.assertEqual(blocked["execution_blockers"], [{
                "deliverable": "D004", "reason": "attempt_limit_reached",
                "attempts": 3, "allowed_attempts": 3,
            }])
            ledger["deliverables"]["D004"].update({
                "automatic_limit": 3, "operator_retry_grants": 1,
                "operator_overrides": [{"timestamp": "2026-01-01T00:00:00Z", "grant": 1,
                                        "source": "operator-cli", "reason": "explicit operator retry command"}],
            })
            (root / "work/attempts.json").write_text(json.dumps(ledger))
            reopened = control_state.snapshot(td)["leaves"]["D004"]
            self.assertTrue(reopened["eligible"])
            self.assertFalse(reopened["attempt_limit_reached"])
            self.assertEqual(reopened["operator_grants_remaining"], 1)
            self.assertEqual(control_state.snapshot(td)["resume_phase"], "execution")


class RecursiveSplitTests(unittest.TestCase):
    """Filesystem-only regression coverage for the bounded recursive tree."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root, self.old_project = supervisor.ROOT, supervisor.PROJECT
        supervisor.ROOT, supervisor.PROJECT = Path(self.tmp.name), self.tmp.name
        self.ctrl = Path(self.tmp.name) / ".opencode-v2"; (self.ctrl / "work").mkdir(parents=True)
        self.parent = {"id":"D001", "name":"remaining work", "owned_artifacts":"a.txt, b.txt, c.txt, d.txt",
                       "launch_deps":[], "contract_deps":[], "verify_command":"test -f a.txt -a -f b.txt -a -f c.txt -a -f d.txt",
                       "role":"implementer", "done_when":"both files exist", "acceptance_ids":["A001"], "parallel":"yes"}
        self.write_manifest({"D001": dict(self.parent)})

    def tearDown(self):
        supervisor.ROOT, supervisor.PROJECT = self.old_root, self.old_project
        self.tmp.cleanup()

    def write_manifest(self, leaves):
        (self.ctrl / "IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
            "recursive_split_protocol": control_state.RECURSIVE_SPLIT_PROTOCOL, "leaves": leaves
        }))

    def proposal(self, first="a.txt, b.txt", second="c.txt, d.txt", sequential=False):
        return [{"scope":"finish first scope", "owned_artifacts":first, "verify_command":"test -f a.txt",
                 "role":"implementer", "depends_on_sibling":"", "done_when":"a exists"},
                 {"scope":"finish second scope", "owned_artifacts":second, "verify_command":"test -f b.txt",
                  "role":"tester", "depends_on_sibling":"first" if sequential else "", "done_when":"b exists"}]

    def proposal_payload(self, did="D001"):
        return {"protocol": supervisor.SPLIT_PROPOSAL_PROTOCOL, "parent_id": did,
                "depth": control_state.split_depth(did), "generation": 1,
                "proposals": self.proposal()}

    def fail_twice(self, did="D001"):
        self.assertEqual(supervisor.claim_attempt("one", did), ("claimed", 1))
        self.assertEqual(supervisor.record_leaf_failure(did, "verify-failed-1"), (True, "genuine-recorded"))
        self.assertEqual(supervisor.claim_attempt("two", did), ("claimed", 2))
        self.assertEqual(supervisor.record_leaf_failure(did, "verify-failed-1"), (True, "split-required"))

    def test_first_genuine_failure_retries_same_leaf_then_root_splits(self):
        self.assertEqual(supervisor.claim_attempt("one", "D001"), ("claimed", 1))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify-failed-1"), (True, "genuine-recorded"))
        self.assertFalse(supervisor.split_request_path("D001").exists())
        self.assertEqual(supervisor.claim_attempt("two", "D001"), ("claimed", 2))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify-failed-1"), (True, "split-required"))
        self.assertTrue(supervisor.split_request_path("D001").exists())

    def test_depth_one_splits_and_depth_two_has_three_attempt_limit(self):
        self.fail_twice(); self.assertEqual(supervisor.persist_split("D001", self.proposal()), ["D001-A", "D001-B"])
        self.fail_twice("D001-A")
        self.assertEqual(supervisor.persist_split("D001-A", self.proposal("a.txt", "b.txt")), ["D001-A1", "D001-A2"])
        for sid in ("a", "b", "c"):
            self.assertEqual(supervisor.claim_attempt(sid, "D001-A1")[0], "claimed")
            self.assertEqual(supervisor.record_leaf_failure("D001-A1", "verify-failed-1"), (True, "genuine-recorded"))
        self.assertEqual(supervisor.claim_attempt("d", "D001-A1"), ("limit", 3))
        self.assertFalse(supervisor.split_request_path("D001-A1").exists())

    def test_infrastructure_and_bad_plan_do_not_trigger_split(self):
        self.assertEqual(supervisor.claim_attempt("one", "D001"), ("claimed", 1))
        self.assertEqual(supervisor.record_leaf_failure("D001", "runtime", "infrastructure"), (True, "infrastructure"))
        self.assertEqual(supervisor.claim_attempt("two", "D001"), ("claimed", 2))
        self.assertEqual(supervisor.record_leaf_failure("D001", "bad ownership", "bad-plan"), (True, "bad-plan"))
        self.assertFalse(supervisor.split_request_path("D001").exists())

    def test_runtime_abort_preserves_dispatch_history_but_not_genuine_budget(self):
        self.assertEqual(supervisor.claim_attempt("cancelled", "D001"), ("claimed", 1))
        self.assertEqual(
            supervisor.record_infrastructure_abort("cancelled", "D001", "runtime cancelled"),
            (True, "granted"),
        )
        self.assertEqual(supervisor.record_leaf_failure("D001", "runtime cancelled", "infrastructure"), (True, "infrastructure"))
        entry = supervisor.load_attempts()["deliverables"]["D001"]
        self.assertEqual(entry["count"], 1)  # historical dispatch remains true
        self.assertEqual(control_state.attempt_state(entry)["automatic_attempts_consumed"], 0)
        self.assertEqual(supervisor.claim_attempt("real-one", "D001"), ("claimed", 2))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify-failed"), (True, "genuine-recorded"))
        self.assertEqual(supervisor.claim_attempt("real-two", "D001"), ("claimed", 3))
        self.assertEqual(supervisor.record_leaf_failure("D001", "verify-failed"), (True, "split-required"))

    def test_splitter_completion_persists_children_once_without_root_cycle(self):
        self.fail_twice()
        request = supervisor.split_request_path("D001")
        self.assertTrue(request.exists())
        self.assertEqual(supervisor.claim_splitter("D001", "launch-1"), (True, "claimed"))
        self.assertEqual(supervisor.claim_splitter("D001", "launch-2"), (False, "splitter-active"))
        supervisor.split_proposal_path("D001").write_text(json.dumps(self.proposal_payload()))
        self.assertEqual(supervisor.complete_splitter("D001", "split-session"), (True, "accepted"))
        self.assertFalse(request.exists())
        state = control_state.snapshot(self.tmp.name)
        self.assertEqual(state["leaves"]["D001"]["split_children"], ["D001-A", "D001-B"])
        self.assertTrue(state["leaves"]["D001-A"]["eligible"])
        self.assertTrue(state["leaves"]["D001-B"]["eligible"])
        self.assertEqual(supervisor.claim_splitter("D001", "restart"), (False, "split-request-missing"))

    def test_splitter_missing_or_invalid_proposal_is_a_finite_explicit_failure(self):
        self.fail_twice()
        self.assertEqual(supervisor.claim_splitter("D001", "launch"), (True, "claimed"))
        self.assertEqual(supervisor.complete_splitter("D001", "split-session"),
                         (False, "splitter-completed-without-durable-proposal"))
        leaf = control_state.snapshot(self.tmp.name)["leaves"]["D001"]
        self.assertEqual(leaf["split_state"], "splitter-failed")
        state = {"acceptance":{"complete":True}, "plan":{"complete":True},
                 "leaves":{"D001":leaf}, "tests":{"complete":False},
                 "acceptance_validation":{"complete":False}}
        self.assertEqual(control_state.resume_phase(state), "execution-blocked")

    def test_validation_enforces_ownership_and_acyclic_order(self):
        self.fail_twice()
        bad = self.proposal(); bad[1]["owned_artifacts"] = "a.txt"
        with self.assertRaisesRegex(ValueError, "disjoint"):
            supervisor.persist_split("D001", bad)
        cyclic = self.proposal(); cyclic[0]["depends_on_sibling"] = "first"
        with self.assertRaisesRegex(ValueError, "only second"):
            supervisor.persist_split("D001", cyclic)

    def test_parent_verification_is_preserved_and_requires_children(self):
        self.fail_twice(); original = self.parent["verify_command"]
        supervisor.persist_split("D001", self.proposal())
        manifest = supervisor.load_manifest()
        self.assertEqual(manifest["leaves"]["D001"]["verify_command"], original)
        for child in ("D001-A", "D001-B"):
            (self.ctrl / "work" / f"{child}.ready").write_text(f"status=complete\ndeliverable={child}\nverified=true\n")
        self.assertFalse(control_state.ready_info(self.tmp.name, "D001"))
        (self.ctrl / "work" / "attempts.json").write_text(json.dumps({"owner":"supervisor","deliverables":{"D001":{"count":2,"sessions":["one","two"],"automatic_limit":2}}}))
        self.assertNotEqual(subprocess.run([str(Path(__file__).with_name("leaf-complete.sh")), "D001"], cwd=self.tmp.name).returncode, 0)
        for name in ("a.txt", "b.txt", "c.txt", "d.txt"): Path(self.tmp.name, name).write_text(name)
        self.assertEqual(subprocess.run([str(Path(__file__).with_name("leaf-complete.sh")), "D001"], cwd=self.tmp.name).returncode, 0)
        self.assertTrue(control_state.ready_info(self.tmp.name, "D001"))

    def test_independent_children_are_concurrent_restartable_and_operator_retry_is_leaf_only(self):
        self.fail_twice(); supervisor.persist_split("D001", self.proposal())
        state = control_state.snapshot(self.tmp.name)
        self.assertIn("Owned artifacts: a.txt, b.txt", (self.ctrl / "work/D001-A.scope.md").read_text())
        self.assertTrue(state["leaves"]["D001-A"]["eligible"])
        self.assertTrue(state["leaves"]["D001-B"]["eligible"])
        self.assertFalse(state["leaves"]["D001"]["eligible"])
        # A fresh process-equivalent snapshot reconstructs the complete tree.
        self.assertEqual(set(control_state.snapshot(self.tmp.name)["leaves"]), {"D001", "D001-A", "D001-B"})
        with self.assertRaisesRegex(ValueError, "split parent"):
            supervisor.grant_operator_retry(["D001"])
        ledger = json.loads((self.ctrl / "work" / "attempts.json").read_text())
        ledger["deliverables"]["D001-A"] = {"count":2,"sessions":["x","y"],"automatic_limit":2}
        (self.ctrl / "work" / "attempts.json").write_text(json.dumps(ledger))
        self.assertEqual([x for x, _ in supervisor.grant_operator_retry(["D001-A"])], ["D001-A"])


class SplitterPluginMaterializationTests(unittest.TestCase):
    def test_complete_structured_splitter_output_becomes_one_durable_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / ".opencode-v2/work"; work.mkdir(parents=True)
            request = {"parent_id": "D001", "depth": 0, "generation": 1}
            (work / "D001.split-request.json").write_text(json.dumps(request))
            proposal = {
                "protocol": supervisor.SPLIT_PROPOSAL_PROTOCOL, "parent_id": "D001",
                "depth": 0, "generation": 1, "proposals": [{"one": 1}, {"two": 2}],
            }
            output = "summary\n```json\n" + json.dumps(proposal) + "\n```\n"
            plugin = Path(__file__).parents[1] / "xdg/config/opencode/plugins/v2-bounded-subagent.js"
            module = Path(td) / "plugin.mjs"
            module.write_text(plugin.read_text())
            code = (
                f"import {{materializeSplitterProposal}} from {json.dumps(str(module))};"
                f"if (!materializeSplitterProposal({json.dumps(td)}, 'D001', {json.dumps(output)})) process.exit(2);"
            )
            result = subprocess.run(
                ["node", "--input-type=module", "-e", code],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((work / "D001.split-proposal.json").read_text()), proposal)


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

    def test_step_limit_with_durable_progress_is_classified_for_filesystem_continuation(self):
        self.manifest()
        progress = Path(self.tmp.name) / ".opencode-v2/work/D003.progress.md"
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text("partial measured facts\n")
        ok, detail = supervisor.post_session_finalize("D003")
        self.assertFalse(ok)
        self.assertEqual(detail, "durable-progress-incomplete")
        self.assertFalse((Path(self.tmp.name) / ".opencode-v2/work/D003.ready").exists())

    def test_markdown_punctuation_in_guard_owned_artifacts_is_not_a_path_suffix(self):
        self.manifest()
        manifest = supervisor.load_manifest()
        manifest["leaves"]["D003"]["owned_artifacts"] = "`artifact.txt`."
        ctrl = Path(self.tmp.name) / ".opencode-v2"
        ctrl.joinpath("IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps(manifest))
        (Path(self.tmp.name) / "artifact.txt").write_text("done\n")
        ok, detail = supervisor.post_session_finalize("D003")
        self.assertTrue(ok, detail)


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
        self.assertEqual(fake.calls[1][2], {"text": "retrospective", "delivery": "steer"})


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
