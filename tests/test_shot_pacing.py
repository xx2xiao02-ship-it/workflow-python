from __future__ import annotations

import unittest

from workflow_1256.shot_pacing import (
    PACING_TOLERANCE_RATIO,
    ShotPacingValidationError,
    is_planned_average_within_tolerance,
    pacing_average_bounds,
    plan_global_shot_pacing,
)


def _timeline(start: float, duration: float) -> dict[str, int]:
    start_us = int(start * 1_000_000)
    return {"start": start_us, "end": start_us + int(duration * 1_000_000)}


class ShotPacingTests(unittest.TestCase):
    def test_global_budget_targets_four_point_five_seconds_and_prioritizes_fast_rhythm(self) -> None:
        durations = [5.448, 14.016, 11.112, 12.216, 8.568, 15.480, 13.296, 9.312]
        rhythms = ["hook", "why", "proof", "fact", "turn", "wrong", "method", "end"]
        timelines = []
        start = 0.0
        for duration in durations:
            timelines.append(_timeline(start, duration))
            start += duration

        result = plan_global_shot_pacing(timelines, [{"rhythm": rhythm} for rhythm in rhythms])

        self.assertEqual(result["required_shot_count"], 22)
        self.assertAlmostEqual(result["planned_average_seconds"], 4.0658, places=3)
        self.assertEqual(
            [group["required_shot_count"] for group in result["groups"]],
            [2, 3, 3, 3, 2, 4, 3, 2],
        )
        # “wrong” 是高压反转段：在相同总镜头预算下，优先从 3 镜提高到 4 镜。
        self.assertEqual(result["groups"][5]["required_shot_count"], 4)

    def test_mismatched_timeline_and_beats_are_rejected(self) -> None:
        with self.assertRaises(ShotPacingValidationError):
            plan_global_shot_pacing([_timeline(0, 5)], [])

    def test_budget_is_clamped_by_the_two_to_five_second_count_bounds(self) -> None:
        result = plan_global_shot_pacing(
            [_timeline(0, 2.2), _timeline(2.2, 2.1)],
            [{"rhythm": "hook"}, {"rhythm": "turn"}],
        )
        self.assertEqual(result["required_shot_count"], 2)
        self.assertEqual([item["required_shot_count"] for item in result["groups"]], [1, 1])

    def test_fast_rhythm_gets_the_added_budget_when_average_is_four_point_two_seconds(self) -> None:
        durations = [20.0, 20.0, 20.0]
        timelines = []
        start = 0.0
        for duration in durations:
            timelines.append(_timeline(start, duration))
            start += duration

        result = plan_global_shot_pacing(
            timelines,
            [{"rhythm": "hook"}, {"rhythm": "why"}, {"rhythm": "proof"}],
        )

        self.assertEqual(result["required_shot_count"], 14)
        self.assertEqual([item["required_shot_count"] for item in result["groups"]], [6, 4, 4])
        self.assertAlmostEqual(result["planned_average_seconds"], 60 / 14, places=6)

    def test_gate_uses_ten_percent_tolerance_for_real_short_average(self) -> None:
        minimum, maximum = pacing_average_bounds()
        self.assertAlmostEqual(minimum, 3.78, places=6)
        self.assertAlmostEqual(maximum, 4.62, places=6)
        self.assertEqual(PACING_TOLERANCE_RATIO, 0.10)
        # 64.704 秒总时长在 17 个受 2--5 秒分组坑位下为 3.806118 秒，
        # 这正是此前被 4.0 秒下限误拦截的真实形态。
        self.assertTrue(is_planned_average_within_tolerance(3.806118))
        self.assertFalse(is_planned_average_within_tolerance(3.70))

    def test_pacing_plan_persists_average_tolerance_policy(self) -> None:
        result = plan_global_shot_pacing(
            [_timeline(0, 3.288), _timeline(3.288, 16.704), _timeline(19.992, 8.208),
             _timeline(28.2, 11.88), _timeline(40.08, 3.504), _timeline(43.584, 7.632),
             _timeline(51.216, 6.552), _timeline(57.768, 6.936)],
            [{"rhythm": rhythm} for rhythm in ("hook", "fact", "proof", "proof", "turn", "fact", "why", "end")],
        )
        self.assertEqual(result["required_shot_count"], 17)
        self.assertAlmostEqual(result["planned_average_seconds"], 3.806118, places=6)
        self.assertEqual(result["average_tolerance_ratio"], 0.10)
        self.assertEqual(result["average_min_seconds"], 3.78)
        self.assertEqual(result["average_max_seconds"], 4.62)


if __name__ == "__main__":
    unittest.main()
