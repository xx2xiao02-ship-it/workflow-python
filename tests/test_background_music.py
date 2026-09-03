from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.coze_adapter_background_music import load_sample, run_migrated
from audit.equivalence_background_music import compare_background_music_outputs, run_audit, summarize
from workflow_1256.background_music import AddAudiosTransportRequired, AudioInfoValidationError, normalize_audio_infos, run_background_music


class BackgroundMusicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample = json.loads((root / "background_music_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "background_music_expected.json").read_text(encoding="utf-8"))

    def test_synthetic_output_contract_and_old_new_comparison(self) -> None:
        actual = run_migrated(self.sample)
        self.assertEqual(compare_background_music_outputs(self.expected, actual), [])
        self.assertEqual(list(actual), ["audio_ids", "draft_url", "track_id"])

    def test_real_like_timeline_is_normalized(self) -> None:
        value = normalize_audio_infos("[{\"audio_url\":\"https://redacted.example/bgm.mp3\",\"start\":0,\"end\":111672000,\"volume\":0.4}]")
        self.assertEqual(value[0]["start"], 0)
        self.assertEqual(value[0]["end"], 111672000)
        self.assertEqual(value[0]["volume"], 0.4)
        self.assertIsNone(value[0]["audio_effect"])

    def test_boundary_audit(self) -> None:
        summary = summarize(run_audit())
        self.assertEqual(summary["total"], 8)
        self.assertTrue(summary["contract_equivalent"])
        self.assertFalse(summary["plugin_behavior_equivalent"])

    def test_empty_array_is_valid(self) -> None:
        self.assertEqual(normalize_audio_infos("[]"), [])

    def test_json_and_schema_errors(self) -> None:
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos("{bad json")
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos("{}")
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos('[{"audio_url":"synthetic://bgm.mp3","start":0,"end":1}]')

    def test_missing_fields_and_time_range_errors(self) -> None:
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos('[{"audio_url":"https://redacted.example/bgm.mp3","start":0}]')
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos('[{"audio_url":"https://redacted.example/bgm.mp3","start":2,"end":1}]')

    def test_volume_fallback_and_integer_times(self) -> None:
        value = normalize_audio_infos('[{"audio_url":"https://redacted.example/bgm.mp3","start":0.9,"end":10.9,"volume":9}]')
        self.assertEqual(value[0]["start"], 0)
        self.assertEqual(value[0]["end"], 10)
        self.assertEqual(value[0]["volume"], 1.0)

    def test_no_executor_does_not_call_external_service(self) -> None:
        with self.assertRaises(AddAudiosTransportRequired):
            run_background_music(load_sample())


if __name__ == "__main__":
    unittest.main()
