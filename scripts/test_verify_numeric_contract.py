#!/usr/bin/env python3
import unittest
import leaf_contract


class VerifyNumericContractTests(unittest.TestCase):
    def test_r5_file_minimum_mismatch_is_rejected(self):
        done=(
            "At least 4 fixture files exist (one per Galilean moon) each "
            "containing one or more frozen epochs."
        )
        command=(
            "python3 -c \"import glob; files=glob.glob('*.json'); "
            "assert len(files)>=5; assert files\""
        )
        errors=leaf_contract.validate_verify_adequacy(done,command)
        self.assertTrue(any(
            "Verify minimum file count 5" in error
            and "Done-when minimum 4" in error
            for error in errors
        ),errors)

    def test_matching_file_minimum_is_allowed(self):
        done="At least 4 fixture files exist and are non-empty."
        command=(
            "python3 -c \"import glob; files=glob.glob('*.json'); "
            "assert len(files)>=4; assert files\""
        )
        self.assertEqual(
            leaf_contract._minimum_file_count_contract_errors(done,command),[]
        )

    def test_weaker_verify_file_minimum_is_rejected(self):
        done="At least 5 JSON files exist in the fixture directory."
        command=(
            "python3 -c \"import glob; files=glob.glob('*.json'); "
            "assert len(files)>=4\""
        )
        errors=leaf_contract._minimum_file_count_contract_errors(done,command)
        self.assertTrue(any("weaker than" in error for error in errors),errors)

    def test_unrelated_len_assertion_is_not_interpreted_as_file_contract(self):
        done="At least 4 fixture files exist."
        command="python3 -c \"items=[1,2,3,4,5]; assert len(items)>=5\""
        self.assertEqual(
            leaf_contract._minimum_file_count_contract_errors(done,command),[]
        )

    def test_done_without_explicit_file_minimum_is_unchanged(self):
        done="Fixture files exist for all Galilean moons."
        command=(
            "python3 -c \"import glob; files=glob.glob('*.json'); "
            "assert len(files)>=5\""
        )
        self.assertEqual(
            leaf_contract._minimum_file_count_contract_errors(done,command),[]
        )


if __name__=="__main__":
    unittest.main()
