#!/usr/bin/env python3
import copy
import unittest
import structured_plan

class AcceptanceIdPruningTests(unittest.TestCase):
    def test_two_overloaded_leaves_keep_global_coverage(self):
        ids=["A006","A007","A008","A009","A014"]
        raw={"leaves":[
            {"key":"ui","complexity":"M","acceptance_ids":list(ids)},
            {"key":"tests_controls","complexity":"M","acceptance_ids":list(ids)},
        ]}
        out,changes=structured_plan.prune_redundant_acceptance_ids(raw)
        leaves=out["leaves"]
        self.assertLessEqual(len(leaves[0]["acceptance_ids"]),4)
        self.assertLessEqual(len(leaves[1]["acceptance_ids"]),4)
        covered=set(leaves[0]["acceptance_ids"])|set(leaves[1]["acceptance_ids"])
        self.assertEqual(covered,set(ids))
        self.assertEqual(len(changes),2)
        self.assertTrue(all(x["remaining_global_owners"]>=1 for x in changes))

    def test_sole_coverage_is_never_removed(self):
        ids=["A001","A002","A003","A004","A005"]
        raw={"leaves":[
            {"key":"only","complexity":"M","acceptance_ids":list(ids)},
        ]}
        before=copy.deepcopy(raw)
        out,changes=structured_plan.prune_redundant_acceptance_ids(raw)
        self.assertEqual(out,before)
        self.assertEqual(changes,[])

    def test_within_limit_is_unchanged(self):
        raw={"leaves":[
            {"key":"a","complexity":"S","acceptance_ids":["A001","A002"]},
            {"key":"b","complexity":"M","acceptance_ids":["A001","A003","A004","A005"]},
        ]}
        before=copy.deepcopy(raw)
        out,changes=structured_plan.prune_redundant_acceptance_ids(raw)
        self.assertEqual(out,before)
        self.assertEqual(changes,[])

    def test_invalid_or_duplicate_lists_are_left_for_fail_closed_validation(self):
        cases=[
            ["A001","A001","A002"],
            ["A001","S002","A003"],
            ["A001",3,"A003"],
        ]
        for ids in cases:
            raw={"leaves":[
                {"key":"bad","complexity":"S","acceptance_ids":list(ids)},
                {"key":"peer","complexity":"M","acceptance_ids":["A001","A002","A003"]},
            ]}
            before=copy.deepcopy(raw)
            out,changes=structured_plan.prune_redundant_acceptance_ids(raw)
            self.assertEqual(out["leaves"][0],before["leaves"][0])
            self.assertFalse(any(x["key"]=="bad" for x in changes))

if __name__=="__main__":
    unittest.main()
