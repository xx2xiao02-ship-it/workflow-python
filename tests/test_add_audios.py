from __future__ import annotations

import json
import unittest
from pathlib import Path

from audit.coze_adapter_add_audios import load_sample, run_migrated
from audit.equivalence_add_audios import run_audit, summarize
from workflow_1256.add_audios import (
    AddAudiosTransportRequired,
    AudioInfoValidationError,
    normalize_audio_infos,
    run_add_audios,
)


class AddAudiosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1] / "samples" / "synthetic"
        cls.sample = json.loads((root / "add_audios_input.json").read_text(encoding="utf-8"))
        cls.expected = json.loads((root / "add_audios_expected.json").read_text(encoding="utf-8"))

    def test_synthetic_output_contract(self) -> None:
        result = run_migrated(self.sample)
        self.assertEqual(result, self.expected)
        self.assertEqual(list(result), ["audio_ids", "draft_url", "track_id"])

    def test_normalization_preserves_order_and_defaults(self) -> None:
        items = normalize_audio_infos(self.sample["audio_infos"])
        self.assertEqual(len(items), 2)
        self.assertEqual(list(items[0]), ["audio_url", "duration", "start", "end", "volume", "audio_effect"])
        self.assertEqual(items[0]["start"], 0)
        self.assertEqual(items[0]["end"], 5000000)
        self.assertIsNone(items[0]["audio_effect"])
        self.assertEqual(items[1]["volume"], 1.0)
        self.assertEqual(items[1]["audio_effect"], "reverb")

    def test_boundary_audit_covers_required_cases(self) -> None:
        results = run_audit()
        summary = summarize(results)
        self.assertEqual(summary["total"], 8)
        self.assertTrue(summary["contract_tests_passed"])
        self.assertFalse(summary["code_level_equivalent"])

    def test_empty_array_is_valid(self) -> None:
        self.assertEqual(normalize_audio_infos("[]"), [])

    def test_json_and_schema_errors(self) -> None:
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos("{bad json")
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos("{}")
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos('[{"audio_url":"synthetic://a.mp3","start":0,"end":1}]')

    def test_missing_fields_and_time_range_errors(self) -> None:
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos('[{"audio_url":"https://synthetic.example/a.mp3","start":0}]')
        with self.assertRaises(AudioInfoValidationError):
            normalize_audio_infos('[{"audio_url":"https://synthetic.example/a.mp3","start":2,"end":1}]')

    def test_out_of_range_volume_falls_back_and_times_are_ints(self) -> None:
        result = normalize_audio_infos('[{"audio_url":"https://synthetic.example/a.mp3","start":0.9,"end":2000000.9,"volume":9}]')
        self.assertEqual(result[0]["start"], 0)
        self.assertEqual(result[0]["end"], 2000000)
        self.assertEqual(result[0]["volume"], 1.0)

    def test_no_executor_does_not_call_external_service(self) -> None:
        with self.assertRaises(AddAudiosTransportRequired):
            run_add_audios(load_sample())

    def test_real_coze_fixture_shape_is_recorded_without_signed_values(self) -> None:
        path = Path(__file__).parents[1] / "samples" / "real" / "run-7668196429877870619-135313-add-audios-real-values-redacted.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(fixture["synthetic"])
        self.assertTrue(fixture["redacted"])
        audio = fixture["input"]["audio_infos"]
        self.assertEqual(audio["type"], "String(JSON)")
        self.assertEqual(audio["time_unit"], "microseconds")
        self.assertEqual(audio["item_count"], 9)
        items = audio["items"]
        self.assertEqual([item["index"] for item in items], list(range(9)))
        self.assertEqual(items[0]["start"], 0)
        self.assertEqual(items[-1]["end"], 111672000)
        for index, item in enumerate(items):
            self.assertEqual(item["duration"], item["end"] - item["start"])
            self.assertTrue(item["audio_url"]["present"])
            self.assertEqual(item["audio_url"]["protocol"], "https")
            self.assertEqual(item["audio_effect"], "人声增强")
            self.assertEqual(item["volume"], 1.2)
            if index:
                self.assertEqual(item["start"], items[index - 1]["end"])
        safe_real_items = [
            {
                "audio_url": f"https://redacted.example/audio-{item['index']}.mp3",
                "start": item["start"],
                "end": item["end"],
                "audio_effect": item["audio_effect"],
                "volume": item["volume"],
            }
            for item in items
        ]
        normalized = normalize_audio_infos(json.dumps(safe_real_items, ensure_ascii=False))
        self.assertEqual(len(normalized), 9)
        self.assertEqual(normalized[0]["start"], 0)
        self.assertEqual(normalized[-1]["end"], 111672000)
        self.assertEqual(fixture["output"]["audio_ids"]["type"], "Array<String>")
        self.assertEqual(fixture["output"]["audio_ids"]["count"], 9)
        self.assertTrue(fixture["output"]["audio_ids"]["values_redacted"])
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn("https://", raw)
        self.assertNotIn("x-signature", raw)


if __name__ == "__main__":
    unittest.main()
