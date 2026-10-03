#!/usr/bin/env python3
"""Completion-contract revisions must terminate and replace challenged attempts."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import control_state
import stage_a_controller
import supervisor


class CompletionContractRevisionTests(unittest.TestCase):
    def test_credit_accepts_done_when_only_transition_and_deduplicates(self):
        verify=hashlib.sha256(b"same verify").hexdigest()
        old=hashlib.sha256(b"old contract").hexdigest()
        new=hashlib.sha256(b"new contract").hexdigest()
        row={
            "attempt":2,
            "source":"supervisor-plan-contract-revision",
            "previous_verify_sha256":verify,
            "current_verify_sha256":verify,
            "previous_contract_sha256":old,
            "current_contract_sha256":new,
            "previous_result":"verify-failed-1",
        }
        entry={"plan_contract_revisions":[row,dict(row)]}
        self.assertEqual(
            control_state._plan_contract_revision_credit_count(entry,2),1
        )
        self.assertEqual(
            control_state._plan_contract_revision_terminal_attempts(entry,2),
            {2},
        )
        invalid=dict(row,current_contract_sha256=old)
        self.assertEqual(
            control_state._plan_contract_revision_credit_count(
                {"plan_contract_revisions":[invalid]},2
            ),0,
        )

    def test_legacy_verify_only_transition_remains_supported(self):
        row={
            "attempt":1,
            "source":"supervisor-plan-contract-revision",
            "previous_verify_sha256":"a"*64,
            "current_verify_sha256":"b"*64,
            "previous_result":"verify-failed-1",
        }
        entry={"plan_contract_revisions":[row]}
        self.assertEqual(
            control_state._plan_contract_revision_terminal_attempts(entry,1),
            {1},
        )

    def test_done_when_revision_reconciles_deadlocked_attempt(self):
        with tempfile.TemporaryDirectory(prefix="completion-contract-") as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            work=ctrl/"work"
            work.mkdir(parents=True)
            command="python3 -m unittest spec_tests.test_summary -v"
            old_leaf={
                "id":"D003","role":"implementer",
                "verify_command":command,
                "done_when":"summarize returns counts and names.",
                "owned_artifact_paths":["summary.py"],
                "launch_deps":[],"verify_deps":[],"contract_deps":[],
            }
            new_leaf=dict(
                old_leaf,
                done_when=(
                    "summarize returns counts and lexicographically sorted "
                    "names with duplicates retained."
                ),
            )
            (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "protocol":"V2.6.9",
                "project":str(project),
                "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
                "leaves":{"D003":new_leaf},
            }))
            attempts={
                "owner":"supervisor",
                "protocol":"v2-attempt-ledger-v1",
                "deliverables":{
                    "D003":{
                        "automatic_limit":2,
                        "count":2,
                        "sessions":["ses-first","ses-challenge"],
                        "failure_history":[{
                            "attempt":1,"classification":"genuine",
                            "reason":"verify-failed-1","session":"ses-first",
                            "source":"supervisor",
                        }],
                    }
                },
            }
            (work/"attempts.json").write_text(json.dumps(attempts))
            old_hash=supervisor.completion_contract_sha256(old_leaf)
            evidence={
                "owner":"supervisor",
                "protocol":supervisor.VERIFY_EVIDENCE_PROTOCOL,
                "deliverable":"D003",
                "entries":[],
                "latest":{
                    "attempt":2,"session":"ses-challenge",
                    "command":command,
                    "completion_contract_sha256":old_hash,
                    "executed":True,"exit_code":1,
                    "result":"verify-failed-1",
                    "stdout":"","stderr":"","error":"",
                },
            }
            (work/"D003.verify-evidence.json").write_text(json.dumps(evidence))

            old_project=supervisor.PROJECT
            old_log=supervisor.LOG
            supervisor.PROJECT=str(project)
            supervisor.LOG=project/"events.log"
            try:
                with mock.patch.object(
                    supervisor,"normalize_supervisor_replacement_record",
                    return_value=False,
                ), mock.patch.object(
                    supervisor,"_archive_split_state_for_contract_repair"
                ), mock.patch.object(
                    supervisor,"_clear_split_request_state_for_contract_repair"
                ):
                    changed=supervisor.reconcile_plan_contract_revisions()
                self.assertEqual(changed,["D003"])
                entry=json.loads((work/"attempts.json").read_text())[
                    "deliverables"
                ]["D003"]
                rows=entry["plan_contract_revisions"]
                self.assertEqual(len(rows),1)
                row=rows[0]
                self.assertEqual(row["attempt"],2)
                self.assertEqual(
                    row["previous_verify_sha256"],
                    row["current_verify_sha256"],
                )
                self.assertEqual(
                    row["previous_contract_sha256"],old_hash
                )
                self.assertEqual(
                    row["current_contract_sha256"],
                    supervisor.completion_contract_sha256(new_leaf),
                )
                state=control_state.attempt_state(entry)
                self.assertTrue(state["valid"],state)
                self.assertEqual(state["plan_contract_retry_grants"],1)
                self.assertEqual(state["allowed_attempts"],3)
                self.assertNotIn(
                    "D003",
                    supervisor.unclassified_native_attempt_deliverables(
                        json.loads((work/"attempts.json").read_text())
                    ),
                )
                self.assertTrue(
                    supervisor.plan_contract_reverify_pending("D003",new_leaf)
                )
                self.assertTrue(
                    stage_a_controller.current_attempt_is_terminal(
                        project,"D003"
                    )
                )
                self.assertEqual(
                    stage_a_controller.attempt_failure_generation(
                        project,"D003"
                    ),
                    2,
                )
            finally:
                supervisor.PROJECT=old_project
                supervisor.LOG=old_log

    def test_persisted_verify_evidence_binds_current_done_when(self):
        with tempfile.TemporaryDirectory(prefix="verify-contract-evidence-") as td:
            project=Path(td)
            ctrl=project/".opencode-v2"
            work=ctrl/"work"
            work.mkdir(parents=True)
            sid="ses-evidence"
            leaf={
                "id":"D001","role":"implementer",
                "verify_command":"test -s out.txt",
                "done_when":"out.txt contains the normalized result.",
                "owned_artifact_paths":["out.txt"],
                "launch_deps":[],"verify_deps":[],"contract_deps":[],
            }
            (ctrl/"IMPLEMENTATION_PLAN.guard.json").write_text(json.dumps({
                "protocol":"V2.6.9","project":str(project),
                "recursive_split_protocol":control_state.RECURSIVE_SPLIT_PROTOCOL,
                "leaves":{"D001":leaf},
            }))
            (work/"attempts.json").write_text(json.dumps({
                "owner":"supervisor","protocol":"v2-attempt-ledger-v1",
                "deliverables":{"D001":{
                    "automatic_limit":2,"count":1,"sessions":[sid],
                }},
            }))
            old_project=supervisor.PROJECT
            supervisor.PROJECT=str(project)
            try:
                item=supervisor.persist_supervisor_verify_evidence(
                    "D001",sid,leaf["verify_command"],
                    SimpleNamespace(returncode=1,stdout="",stderr="failed"),
                    "verify-failed-1",
                )
            finally:
                supervisor.PROJECT=old_project
            self.assertEqual(
                item["completion_contract_sha256"],
                supervisor.completion_contract_sha256(leaf),
            )
            self.assertEqual(
                item["done_when_sha256"],
                hashlib.sha256(leaf["done_when"].encode()).hexdigest(),
            )


if __name__=="__main__":
    unittest.main()
