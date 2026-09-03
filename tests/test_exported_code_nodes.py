from __future__ import annotations

import unittest

from workflow_1256.bgm_task_assembly import run_bgm_task_assembly
from workflow_1256.host_task_assembly import run_host_task_assembly
from workflow_1256.scene_type_recognition import run_scene_type_recognition


class ExportedCodeNodeTests(unittest.TestCase):
    def test_scene_type_recognition_keeps_host_map_shape_and_microseconds(self) -> None:
        result = run_scene_type_recognition(
            {
                "director_plan": {"core": "核心", "spine": "推进"},
                "Code_list": [
                    {
                        "shots": [{"source_text": "测试内容", "clip_role": "状态", "story_beat": "建立"}],
                        "timelines": [{"start": 0, "end": 2_000_000}],
                    }
                ],
                "new_segments": ["测试内容。"],
                "new_timelines": [{"start": 0, "end": 2_000_000}],
                "link_list": ["https://example.com/audio.mp3"],
                "timelines": [{"start": 0, "end": 2_000_000}],
            }
        )
        self.assertEqual(list(result), ["host_llm_input", "host_map", "error"])
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["host_map"]["units"]), 1)

    def test_host_task_assembly_preserves_selected_index_order(self) -> None:
        result = run_host_task_assembly(
            {
                "host_map": {
                    "units": [
                        {
                            "idx": 1,
                            "audio_url": "https://example.com/b.mp3",
                            "audio_time": {"start": 1_000_000, "end": 2_000_000},
                            "new_timelines": {"start": 3_000_000, "end": 4_000_000},
                        },
                        {
                            "idx": 0,
                            "audio_url": "https://example.com/a.mp3",
                            "audio_time": {"start": 0, "end": 1_000_000},
                            "new_timelines": {"start": 0, "end": 1_000_000},
                        },
                    ]
                },
                "host_idxs": [0, 1],
            }
        )
        self.assertEqual(result["error"], "")
        self.assertEqual(result["audio_url"], ["https://example.com/a.mp3", "https://example.com/b.mp3"])
        self.assertEqual(result["new_timelines"][1]["start"], 3_000_000)

    def test_bgm_task_assembly_returns_parallel_arrays(self) -> None:
        result = run_bgm_task_assembly(
            {
                "timelines": [
                    {"start": 0, "end": 5_000_000},
                    {"start": 5_000_000, "end": 10_000_000},
                ],
                "music_cues": [
                    {"music_role": "推进", "emotion": "稳定", "energy": 3, "music_direction": "持续推进", "transition_to_next": "crossfade"},
                    {"music_role": "收束", "emotion": "平静", "energy": 2, "music_direction": "平稳收束", "transition_to_next": "fade_out"},
                ],
            }
        )
        self.assertEqual(list(result), ["bgm_tasks", "bgm_timelines", "transition_schemes"])
        self.assertEqual(len(result["bgm_tasks"]), 2)
        self.assertEqual(len(result["bgm_timelines"]), 2)
        self.assertEqual(len(result["transition_schemes"]), 1)
        self.assertEqual(result["bgm_timelines"][0]["start"], 0)


if __name__ == "__main__":
    unittest.main()
