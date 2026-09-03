from __future__ import annotations

import unittest

from workflow_1256.video_data_aggregation import (
    VideoDataAggregationValidationError,
    run_video_data_aggregation,
)


class VideoDataAggregationTests(unittest.TestCase):
    def test_overlay_sort_and_merge_preserve_sources(self) -> None:
        result = run_video_data_aggregation(
            {
                "url_list": ["new-1"],
                "new_timelines": [{"start": 5, "end": 15}],
                "public_video_url_list": ["public-0", "public-1"],
                "timelines": [{"start": 0, "end": 10}, {"start": 10, "end": 20}],
            }
        )
        self.assertEqual(result["super_timelines"], [{"start": 0, "end": 5}, {"start": 5, "end": 15}, {"start": 15, "end": 20}])
        self.assertEqual(result["super_url_list"], ["public-0", "new-1", "public-1"])
        self.assertEqual([item["source"] for item in result["super_segments"]], ["public", "new", "public"])
        self.assertEqual([item["source_index"] for item in result["super_segments"]], [0, 0, 1])

    def test_adjacent_same_url_keeps_distinct_source_indexes(self) -> None:
        result = run_video_data_aggregation(
            {
                "url_list": [],
                "new_timelines": [],
                "public_video_url_list": ["same", "same"],
                "timelines": [{"start": 0, "end": 5}, {"start": 5, "end": 10}],
            }
        )
        self.assertEqual(result["super_timelines"], [{"start": 0, "end": 5}, {"start": 5, "end": 10}])
        self.assertEqual([item["source_index"] for item in result["super_segments"]], [0, 1])

    def test_array_alignment_and_timeline_errors_are_rejected(self) -> None:
        with self.assertRaises(VideoDataAggregationValidationError):
            run_video_data_aggregation(
                {"url_list": [], "new_timelines": [{"start": 0, "end": 1}], "public_video_url_list": [], "timelines": []}
            )
        with self.assertRaises(VideoDataAggregationValidationError):
            run_video_data_aggregation(
                {"url_list": [], "new_timelines": [], "public_video_url_list": ["u"], "timelines": [{"start": 1, "end": 1}]}
            )


if __name__ == "__main__":
    unittest.main()
