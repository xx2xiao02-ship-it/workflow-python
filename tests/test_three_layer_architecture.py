from __future__ import annotations

import json
import unittest

from workflow_1256.layers import (
    LayerContractError,
    build_asset_manifest,
    build_director_plan,
    build_draft_plan,
    run_three_layer_pipeline,
)


DIRECTOR_OUTPUT = {
    "director_plan": {"core": "清晰表达", "tone": "克制"},
    "segment_beats": [
        {"segment_index": 0, "segment_goal": "开场"},
        {"segment_index": 1, "segment_goal": "收束"},
    ],
    "segments": ["第一段文案。", "第二段文案。"],
}


ASSET_INPUTS = {
    "aigc_video_infos": '[{"video_url":"synthetic://video/1","start":0,"end":4000000}]',
    "digital_human_video_infos": [
        {"video_url": "synthetic://host/1", "start": 4000000, "end": 8000000}
    ],
    "narration_audio_infos": [
        {"audio_url": "synthetic://audio/narration", "start": 0, "end": 8000000}
    ],
    "captions": [
        {"text": "第一段文案。", "start": 0, "end": 4000000},
        {"text": "第二段文案。", "start": 4000000, "end": 8000000},
    ],
}


class ThreeLayerArchitectureTests(unittest.TestCase):
    def test_director_layer_preserves_segment_order(self) -> None:
        plan = build_director_plan(DIRECTOR_OUTPUT, text="原始文案")
        self.assertEqual(plan.segments, ["第一段文案。", "第二段文案。"])
        self.assertEqual(plan.segment_beats[1]["segment_index"], 1)
        self.assertEqual(plan.to_dict()["source_node"], "具体画面导演")

    def test_asset_layer_accepts_json_string_and_keeps_timeline(self) -> None:
        manifest = build_asset_manifest(ASSET_INPUTS, director_segments=DIRECTOR_OUTPUT["segments"])
        self.assertEqual(len(manifest.assets), 5)
        self.assertEqual([asset.kind for asset in manifest.assets], ["video", "video", "audio", "caption", "caption"])
        self.assertEqual(manifest.duration_us, 8000000)
        self.assertEqual(manifest.assets[1].start_us, 4000000)

    def test_execution_layer_is_no_effect_by_default_and_orders_tracks(self) -> None:
        manifest = build_asset_manifest(ASSET_INPUTS)
        draft = build_draft_plan(manifest)
        self.assertFalse(draft.effects_enabled)
        self.assertEqual([track.kind for track in draft.tracks], ["video", "video", "audio", "caption"])
        self.assertEqual(draft.duration_us, 8000000)

    def test_three_layers_run_end_to_end_without_external_services(self) -> None:
        result = run_three_layer_pipeline({
            "text": "原始文案",
            "director_output": DIRECTOR_OUTPUT,
            "asset_inputs": ASSET_INPUTS,
            "draft_url": "synthetic://draft/three-layer",
        })
        self.assertEqual(result["director_plan"]["segments"], DIRECTOR_OUTPUT["segments"])
        self.assertEqual(result["asset_manifest"]["duration_us"], 8000000)
        self.assertFalse(result["draft_plan"]["effects_enabled"])
        self.assertEqual(result["draft_plan"]["draft_url"], "synthetic://draft/three-layer")

    def test_invalid_timeline_is_rejected_at_asset_boundary(self) -> None:
        with self.assertRaises(LayerContractError):
            build_asset_manifest({
                "aigc_video_infos": [{"video_url": "synthetic://bad", "start": 4, "end": 4}]
            })

    def test_empty_asset_arrays_are_valid_but_produce_no_tracks(self) -> None:
        manifest = build_asset_manifest({
            "aigc_video_infos": [],
            "digital_human_video_infos": "[]",
            "narration_audio_infos": [],
            "captions": [],
        })
        draft = build_draft_plan(manifest)
        self.assertEqual(manifest.duration_us, 0)
        self.assertEqual(draft.tracks, [])

    def test_director_count_mismatch_is_rejected_before_materials(self) -> None:
        invalid = dict(DIRECTOR_OUTPUT)
        invalid["segment_beats"] = []
        with self.assertRaises(LayerContractError):
            build_director_plan(json.dumps(invalid), text="原始文案")


if __name__ == "__main__":
    unittest.main()
