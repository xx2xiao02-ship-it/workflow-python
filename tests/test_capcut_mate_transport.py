from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import workflow_1256.capcut_mate_transport as capcut_transport
from workflow_1256.capcut_mate_transport import CapCutMateClient
from workflow_1256.create_draft import CreateDraftRequest, run_create_draft
from workflow_1256.save_draft import SaveDraftRequest, run_save_draft


class CapCutMateTransportTests(unittest.TestCase):
    def test_local_default_requester_bypasses_system_proxy(self) -> None:
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        opener = Mock()
        opener.open.return_value = Response()
        with patch.object(capcut_transport, "build_opener", return_value=opener) as build_opener:
            with patch.object(capcut_transport, "urlopen", side_effect=AssertionError("local request used proxy")):
                result = capcut_transport._default_requester(
                    "http://127.0.0.1:30000/openapi/capcut-mate/v1/create_draft",
                    {"height": 1920, "width": 1080},
                    1,
                )

        self.assertEqual(result, {"ok": True})
        build_opener.assert_called_once()
        opener.open.assert_called_once()

    def test_draft_url_and_payload_are_forwarded_without_reshaping(self) -> None:
        calls: list[tuple[str, dict, float]] = []
        responses = [
            {"draft_url": "http://draft/one", "tip_url": "tip"},
            {"draft_url": "http://draft/one", "message": "saved"},
        ]

        def requester(url, payload, timeout):
            calls.append((url, dict(payload), timeout))
            return responses.pop(0)

        client = CapCutMateClient("http://127.0.0.1:30000", requester=requester)
        created = run_create_draft(
            {"height": 1920, "width": 1080},
            transport=client.create_draft,
        )
        saved = run_save_draft(
            {"draft_url": created["draft_url"]},
            transport=client.save_draft,
        )

        self.assertEqual(created["draft_url"], saved["draft_url"])
        self.assertEqual(calls[0][0], "http://127.0.0.1:30000/openapi/capcut-mate/v1/create_draft")
        self.assertEqual(calls[0][1], {"height": 1920, "width": 1080})
        self.assertEqual(calls[1][1], {"draft_url": "http://draft/one"})

    def test_add_audios_keeps_original_json_string(self) -> None:
        calls: list[dict] = []

        def requester(_url, payload, _timeout):
            calls.append(dict(payload))
            return {"draft_url": "http://draft/two", "track_id": "track", "audio_ids": ["a1"]}

        client = CapCutMateClient("http://127.0.0.1:30000", requester=requester)
        raw = json.dumps([{"audio_url": "https://example.com/a.mp3", "start": 0, "end": 1}])
        result = client.add_audios("http://draft/one", raw, [])
        self.assertEqual(result["draft_url"], "http://draft/two")
        self.assertEqual(calls[0], {"draft_url": "http://draft/one", "audio_infos": raw})

    def test_add_videos_forwards_only_explicit_optional_fields(self) -> None:
        calls: list[dict] = []

        def requester(_url, payload, _timeout):
            calls.append(dict(payload))
            return {"draft_url": "http://draft/two", "track_id": "track", "segment_ids": [], "segment_infos": [], "video_ids": []}

        client = CapCutMateClient("http://127.0.0.1:30000", requester=requester)
        raw = json.dumps([{"video_url": "https://example.com/a.mp4", "start": 0, "end": 1}])
        client.add_videos("http://draft/one", raw, alpha=0.5, scene_timelines=[])
        self.assertEqual(calls[0], {"draft_url": "http://draft/one", "video_infos": raw, "alpha": 0.5, "scene_timelines": []})

    def test_get_draft_reads_file_list_without_post_side_effect(self) -> None:
        get_calls = []

        def get_requester(url, params, timeout):
            get_calls.append((url, dict(params), timeout))
            return {"files": ["draft.json", "timeline.json"]}

        client = CapCutMateClient("http://127.0.0.1:30000", get_requester=get_requester)
        result = client.get_draft("http://mate/openapi/capcut-mate/v1/get_draft?draft_id=2026073102205453e3ee3f")
        self.assertEqual(result, {"files": ["draft.json", "timeline.json"]})
        self.assertEqual(get_calls[0][1], {"draft_id": "2026073102205453e3ee3f"})


if __name__ == "__main__":
    unittest.main()
