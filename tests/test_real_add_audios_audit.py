from __future__ import annotations

import unittest

from audit.real_fixture_add_audios import run_real_shape_audit


class RealAddAudiosAuditTests(unittest.TestCase):
    def test_real_coze_shape_matches_local_contract(self) -> None:
        result = run_real_shape_audit()
        self.assertTrue(result["shape_equivalent"], result["differences"])
        self.assertEqual(result["normalized_item_count"], 9)
        self.assertEqual(result["normalized_timeline"], {"start": 0, "end": 111672000, "unit": "microseconds"})
        self.assertTrue(result["real_output_values_redacted"])
        self.assertFalse(result["plugin_behavior_equivalent"])


if __name__ == "__main__":
    unittest.main()
