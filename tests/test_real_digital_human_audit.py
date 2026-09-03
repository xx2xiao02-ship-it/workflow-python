from __future__ import annotations

import unittest

from audit.real_fixture_digital_human import audit_real_shape, load_fixture
from workflow_1256.digital_human import normalize_video_infos


class RealDigitalHumanAuditTests(unittest.TestCase):
    def test_redacted_real_execution_shape_types_order_and_timeline(self) -> None:
        fixture = load_fixture()
        result = audit_real_shape()
        self.assertEqual(fixture["status"], "运行成功")
        self.assertEqual(result["input_count"], 18)
        self.assertEqual(result["output_segment_count"], 18)
        self.assertEqual(result["output_video_id_count"], 18)
        self.assertEqual(result["output_segment_id_count"], 18)
        self.assertTrue(result["timeline_equal"])
        self.assertTrue(result["input_output_timeline_equal"])
        self.assertEqual(
            result["yaml_output_field_order"],
            ["draft_url", "segment_ids", "segment_infos", "track_id", "video_ids"],
        )
        self.assertEqual(
            result["derived_timeline"][0],
            {"index": 0, "start": 0, "end": 6906732, "duration": 6906732},
        )
        self.assertEqual(result["derived_timeline"][10]["end"], 75521228)
        self.assertEqual(result["derived_timeline"][-1]["end"], 111672000)
        self.assertTrue(
            all(row["id_type"] == "String" for row in fixture["output"]["segment_infos"])
        )

    def test_real_input_keeps_host_video_parameters_and_source_counts(self) -> None:
        fixture = load_fixture()
        normalized = normalize_video_infos(fixture["input"]["video_infos"])
        self.assertTrue(all(item["width"] == 1024 for item in normalized))
        self.assertTrue(all(item["height"] == 1024 for item in normalized))
        self.assertTrue(all(item["transition"] == "胶片定格" for item in normalized))
        self.assertTrue(all(item["transition_duration"] == 1000000 for item in normalized))
        self.assertTrue(all(item["volume"] == 0 for item in normalized))
        self.assertEqual(
            fixture["input"]["video_url_source_categories"],
            {"seedance": 15, "host_segment": 3},
        )


if __name__ == "__main__":
    unittest.main()

