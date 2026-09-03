from __future__ import annotations

import json
import unittest

from workflow_1256.capcut_mate_transport import CapCutMateClient
from workflow_1256.capcut_node_adapters import CapCutNodeAdapter, CapCutNodeContractError


class CapCutNodeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []

        def requester(url, payload, _timeout):
            self.calls.append((url, dict(payload)))
            if url.endswith("/add_captions"):
                return {
                    "draft_url": "draft://new",
                    "track_id": "track-1",
                    "text_ids": ["text-1"],
                    "segment_ids": ["segment-1"],
                    "segment_infos": [{"id": "segment-1", "start": 0, "end": 1}],
                }
            if url.endswith("/add_audios"):
                return {"draft_url": "draft://audio", "track_id": "track-a", "audio_ids": ["audio-1"]}
            if url.endswith("/add_videos"):
                return {
                    "draft_url": "draft://video",
                    "track_id": "track-v",
                    "video_ids": ["video-1"],
                    "segment_ids": ["segment-v"],
                    "segment_infos": [{"id": "segment-v", "start": 0, "end": 100}],
                }
            if url.endswith("/video_infos"):
                return {"infos": "[]"}
            if url.endswith("/audio_timelines"):
                timeline = [{"start": 0, "end": 1_000_000}]
                return {"timelines": timeline, "all_timelines": timeline}
            return {"draft_url": "draft://new", "track_id": "track", "effect_ids": [], "segment_ids": [], "segment_infos": [], "video_ids": []}

        self.adapter = CapCutNodeAdapter(CapCutMateClient("http://mate", requester=requester))

    def test_caption_mapping_preserves_latest_draft_url_and_arrays(self) -> None:
        result = self.adapter.add_captions(
            {"draft_url": "draft://old", "captions": [{"text": "测试", "start": 0, "end": 1}]}
        )
        self.assertEqual(result["draft_url"], "draft://new")
        self.assertEqual(result["text_ids"], ["text-1"])
        self.assertEqual(result["segment_infos"][0]["id"], "segment-1")
        self.assertEqual(json.loads(self.calls[0][1]["captions"])[0]["text"], "测试")

    def test_audio_mapping_uses_original_json_string_and_projected_fields(self) -> None:
        raw = json.dumps([{"audio_url": "https://example.com/a.mp3", "start": 0, "end": 1}])
        result = self.adapter.add_audios({"draft_url": "draft://old", "audio_infos": raw})
        self.assertEqual(result, {"audio_ids": ["audio-1"], "draft_url": "draft://audio", "track_id": "track-a"})
        self.assertEqual(self.calls[0][1]["audio_infos"], raw)

    def test_info_mapping_requires_infos_json_string(self) -> None:
        self.assertEqual(self.adapter.infos("video_infos", {"video_urls": [], "timelines": []}), {"infos": "[]"})

        broken = CapCutNodeAdapter(CapCutMateClient("http://mate", requester=lambda *_: {"infos": []}))
        with self.assertRaises(CapCutNodeContractError):
            broken.infos("video_infos", {})

    def test_aigc_video_mapping_projects_strict_add_videos_contract(self) -> None:
        result = self.adapter.add_videos({
            "draft_url": "draft://old",
            "video_infos": json.dumps([{
                "video_url": "https://example.com/a.mp4",
                "start": 0,
                "end": 100,
            }]),
        })
        self.assertEqual(result["draft_url"], "draft://video")
        self.assertEqual(result["video_ids"], ["video-1"])
        self.assertEqual(self.calls[0][1]["draft_url"], "draft://old")

    def test_video_mapping_forwards_json_string_and_optional_fields(self) -> None:
        raw = json.dumps([{"video_url": "https://example.com/a.mp4", "start": 0, "end": 10}])
        result = self.adapter.add_videos(
            {
                "draft_url": "draft://old",
                "video_infos": raw,
                "alpha": 0.5,
                "scale_x": 1.2,
                "scene_timelines": [{"start": 0, "end": 5}],
            }
        )
        self.assertEqual(list(result), ["draft_url", "segment_ids", "segment_infos", "track_id", "video_ids"])
        self.assertEqual(self.calls[0][1]["video_infos"], raw)
        self.assertEqual(self.calls[0][1]["alpha"], 0.5)
        self.assertEqual(self.calls[0][1]["scale_x"], 1.2)
        self.assertEqual(self.calls[0][1]["scene_timelines"], [{"start": 0, "end": 5}])

    def test_digital_human_uses_independent_node_contract_and_same_transport(self) -> None:
        raw = json.dumps(
            [
                {
                    "video_url": "https://example.com/host.mp4",
                    "start": 0,
                    "end": 100,
                    "transition": "胶片定格",
                    "volume": 0,
                }
            ],
            ensure_ascii=False,
        )
        result = self.adapter.add_digital_human(
            {"draft_url": "draft://host-old", "video_infos": raw}
        )
        self.assertEqual(
            list(result),
            ["draft_url", "segment_ids", "segment_infos", "track_id", "video_ids"],
        )
        self.assertEqual(result["segment_infos"], [{"end": 100, "id": "segment-v", "start": 0}])
        self.assertEqual(self.calls[0][1], {"draft_url": "draft://host-old", "video_infos": raw})

    def test_audio_timelines_uses_8364_links_route(self) -> None:
        result = self.adapter.audio_timelines({"links": ["https://example.com/a.mp3"]})

        self.assertEqual(result, {
            "all_timelines": [{"start": 0, "end": 1_000_000}],
            "timelines": [{"start": 0, "end": 1_000_000}],
        })
        self.assertTrue(self.calls[0][0].endswith("/audio_timelines"))
        self.assertEqual(self.calls[0][1], {"links": ["https://example.com/a.mp3"]})

    def test_bgm_merge_is_not_misreported_as_capcut_audio_timelines(self) -> None:
        from workflow_1256.capcut_node_adapters import CAPCUT_NODE_ENDPOINTS

        self.assertEqual(CAPCUT_NODE_ENDPOINTS["165901"], "audio_timelines")
        self.assertEqual(CAPCUT_NODE_ENDPOINTS["152553"], "video_infos")
        self.assertNotIn("165818", CAPCUT_NODE_ENDPOINTS)
        self.assertNotIn("191914", CAPCUT_NODE_ENDPOINTS)


if __name__ == "__main__":
    unittest.main()
