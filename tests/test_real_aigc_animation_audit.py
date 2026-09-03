from __future__ import annotations

import unittest

from audit.real_fixture_aigc_animation import audit_real_shape, load_fixture
from workflow_1256.aigc_animation import normalize_video_infos


class RealAIGCAnimationAuditTests(unittest.TestCase):
    def test_redacted_real_execution_shape_and_timeline(self) -> None:
        fixture = load_fixture()
        result = audit_real_shape()
        self.assertEqual(fixture["status"], "运行成功")
        self.assertEqual(result["input_count"], 17)
        self.assertEqual(result["output_segment_count"], 17)
        self.assertEqual(result["output_video_id_count"], 17)
        self.assertEqual(result["output_segment_id_count"], 17)
        self.assertTrue(result["timeline_equal"])
        self.assertEqual(result["derived_timeline"][0], {"index": 0, "start": 0, "end": 6906732, "duration": 6906732})
        self.assertEqual(result["derived_timeline"][-1]["end"], 111672000)
        self.assertEqual(normalize_video_infos(fixture["input"]["video_infos"])[0]["width"], 1024)


if __name__ == "__main__":
    unittest.main()
