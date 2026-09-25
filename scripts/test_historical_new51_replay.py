#!/usr/bin/env python3
import json
import unittest
from pathlib import Path

from deterministic_dispatch import select_actions

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "fixtures" / "historical-new51-decisions.json"

class HistoricalNew51ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(CORPUS.read_text())
        cls.cases = cls.data["cases"]

    def test_corpus_shape_and_coverage(self):
        self.assertEqual(self.data["protocol"], "v2-historical-orchestrator-replay-v1")
        self.assertEqual(self.data["case_count"], 121)
        self.assertEqual(len(self.cases), 121)
        self.assertEqual(
            set(self.data["source_runs"]),
            {"New51p2","New51p3","New51r","New51s","New51t","New51u","New51v","New51w","New51y","New51z"},
        )

    def test_all_archived_decisions_keep_the_current_deterministic_result(self):
        for case in self.cases:
            with self.subTest(run=case["run"], session=case["session"], ordinal=case["ordinal"]):
                self.assertEqual(
                    select_actions(case["decision"]),
                    case["expected_current_actions"],
                )

    def find_case(self, predicate):
        matches = [case for case in self.cases if predicate(case)]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def test_old_duplicate_retry_is_now_wait_while_same_leaf_is_active(self):
        case = self.find_case(lambda c:
            c["run"] == "New51t"
            and c["decision"].get("resume_phase") == "execution"
            and c["decision"].get("eligible") == []
            and (c["decision"].get("scheduler") or {}).get("active_deliverables") == ["D001"]
            and c["observed_original_actions"] == [{"kind":"launch","agent":"probe-builder","deliverable":"D001"}]
        )
        self.assertEqual(case["expected_current_actions"], [{"kind":"wait"}])

    def test_old_premature_global_block_is_now_wait_while_unrelated_child_is_active(self):
        case = self.find_case(lambda c:
            c["run"] == "New51y"
            and c["decision"].get("resume_phase") == "execution"
            and (c["decision"].get("scheduler") or {}).get("active_deliverables") == ["D002-B"]
            and c["observed_original_actions"] == [{"kind":"blocked","deliverable":"D003"}]
        )
        self.assertEqual(case["expected_current_actions"], [{"kind":"wait"}])

    def test_duplicate_same_leaf_launch_collapses_to_one_deterministic_launch(self):
        case = self.find_case(lambda c:
            c["run"] == "New51p2"
            and c["decision"].get("eligible") == ["D013-A2"]
            and len(c["observed_original_actions"]) == 2
            and all(a.get("deliverable") == "D013-A2" for a in c["observed_original_actions"])
        )
        self.assertEqual(
            case["expected_current_actions"],
            [{"kind":"launch","agent":"test-builder","deliverable":"D013-A2"}],
        )

    def test_parallel_capacity_uses_authoritative_eligible_order(self):
        case = self.find_case(lambda c:
            c["run"] == "New51p2"
            and c["decision"].get("eligible") == ["D001","D008","D015","D016"]
        )
        self.assertEqual(
            case["expected_current_actions"],
            [
                {"kind":"launch","agent":"probe-builder","deliverable":"D001"},
                {"kind":"launch","agent":"implementer","deliverable":"D008"},
                {"kind":"launch","agent":"test-builder","deliverable":"D015"},
            ],
        )

if __name__ == "__main__":
    unittest.main(verbosity=2)
