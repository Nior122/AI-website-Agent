import unittest

from research.identity import compare_branch_location


class BranchIdentityTests(unittest.TestCase):
    def test_exact_recorded_branch_matches(self):
        result = compare_branch_location(
            ["27 Oluwaleyimu Street, Ikeja, Lagos", "No. 1 Patricia Akpan Street, Ajah, Lagos"],
            "27 Oluwaleyimu Street, Ikeja, Lagos",
        )
        self.assertEqual(result, "MATCHED")

    def test_similarly_named_different_branch_is_a_conflict(self):
        result = compare_branch_location(
            ["27 Oluwaleyimu Street, Ikeja, Lagos", "No. 1 Patricia Akpan Street, Ajah, Lagos"],
            "22 Molade Okoya Thomas Street, Victoria Island, Lagos",
        )
        self.assertEqual(result, "CONFLICT")

    def test_missing_location_remains_unknown(self):
        self.assertEqual(compare_branch_location(["27 Oluwaleyimu Street, Ikeja, Lagos"], None), "UNKNOWN")
