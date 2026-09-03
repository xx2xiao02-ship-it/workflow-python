from __future__ import annotations

import json
import unittest

from workflow_1256.global_opening_frame import (
    GLOBAL_OPENING_FRAME_SYSTEM_PROMPT,
    GlobalOpeningFrameTransportRequired,
    build_global_opening_frame_prompt,
    normalize_global_opening_frame,
    run_global_opening_frame_writer,
)


def _result() -> dict:
    return {
        "visual_thesis": "普通人的脑力劳动被Token洪流重新计量",
        "dramatic_conflict": "林夏站在纸山与算力奇观之间，效率突破同时逼近能源代价",
        "spectacle_anchor": "发光Token从办公桌汇成城市上空的知识建筑",
        "composition": "林夏位于前景，纸山和电脑连接中景光流，数据中心与冷却塔压在远景",
        "lighting_mood": "压迫的午夜蓝中出现暖金突破光，远处保留红色警示灯",
        "must_include": ["林夏", "纸质报表山", "Token光流", "数据中心冷却塔"],
        "avoid": ["分屏", "字幕", "水印", "第二位主角"],
        "image_prompt": "9:16 电影海报式单一连续场景，林夏站在纸质报表山与Token光流之间，远处数据中心和冷却塔形成能源悬念，无文字无拼贴。",
    }


class GlobalOpeningFrameTests(unittest.TestCase):
    def test_prompt_contains_full_copy_story_and_locked_anchor(self) -> None:
        prompt = build_global_opening_frame_prompt(
            "第一段全文\n第二段全文",
            {"movie_outline": {"protagonist": "林夏"}},
            aspect_ratio="16:9",
            protagonist_name="林夏",
        )
        self.assertIn("第一段全文", prompt)
        self.assertIn("movie_outline", prompt)
        self.assertIn("16:9", prompt)
        self.assertIn("固定主角锚点：林夏", prompt)

    def test_real_writer_uses_injected_llm_and_normalizes_json(self) -> None:
        received = []

        def transport(system, user):
            received.append((system, user))
            return json.dumps(_result(), ensure_ascii=False)

        result = run_global_opening_frame_writer(
            "Token 全文",
            {"movie_outline": {"protagonist": "林夏"}},
            transport=transport,
        )
        self.assertEqual(result["protagonist_name"], "林夏")
        self.assertEqual(result["aspect_ratio"], "9:16")
        self.assertEqual(received[0][0], GLOBAL_OPENING_FRAME_SYSTEM_PROMPT)
        self.assertIn("Token 全文", received[0][1])

    def test_requires_real_transport(self) -> None:
        with self.assertRaises(GlobalOpeningFrameTransportRequired):
            run_global_opening_frame_writer("全文", {"story": "故事"})

    def test_rejects_missing_image_prompt(self) -> None:
        value = _result()
        del value["image_prompt"]
        with self.assertRaises(ValueError):
            normalize_global_opening_frame(value)


if __name__ == "__main__":
    unittest.main()
