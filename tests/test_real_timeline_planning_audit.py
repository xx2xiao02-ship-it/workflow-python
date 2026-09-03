from __future__ import annotations

import unittest

from audit.real_fixture_127095 import ITEMS, run_real_batch_fixture_audit, run_real_item_fixture_audit


class RealTimelinePlanningAuditTests(unittest.TestCase):
    def test_real_coze_items_match_original_and_migrated(self) -> None:
        expected = {
            "1": ([6.9, 6.7, 2.8], [7, 7, 4], {"start": 13613268, "end": 16416000}),
            "2": ([9.2, 5.2], [10, 6], {"start": 25631333, "end": 30840000}),
            "3": ([9, 9.7, 4.1], [9, 10, 5], {"start": 49500632, "end": 53592000}),
            "4": ([7.8, 5.6], [8, 6], {"start": 61373373, "end": 66960000}),
            "5": ([10, 4.2], [10, 5], {"start": 76965634, "end": 81168000}),
            "6": ([7.8, 3.1], [8, 4], {"start": 88999486, "end": 92112000}),
            "7": ([8.5], [9], {"start": 92112000, "end": 100656000}),
            "8": ([8.8], [9], {"start": 100656000, "end": 109416000}),
            "9": ([2.3], [4], {"start": 109416000, "end": 111672000}),
        }
        self.assertEqual(tuple(expected), ITEMS)
        for item, (expected_clip, expected_int, expected_last) in expected.items():
            result = run_real_item_fixture_audit(item)

            self.assertEqual(result["original_vs_migrated"], [])
            self.assertEqual(result["coze_vs_original"], [])
            self.assertEqual(result["coze_vs_migrated"], [])

            output = result["coze_output"]
            self.assertEqual(len(output["shots"]), len(expected_clip))
            self.assertEqual(output["clip_duration"], expected_clip)
            self.assertEqual(output["int_duration"], expected_int)
            self.assertEqual(output["timelines"][-1], expected_last)

    def test_real_batch_audit_covers_all_nine_items(self) -> None:
        results = run_real_batch_fixture_audit()
        self.assertEqual(tuple(results), ITEMS)
        for result in results.values():
            self.assertEqual(result["original_vs_migrated"], [])
            self.assertEqual(result["coze_vs_original"], [])
            self.assertEqual(result["coze_vs_migrated"], [])


if __name__ == "__main__":
    unittest.main()
