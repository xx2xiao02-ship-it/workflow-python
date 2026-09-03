from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path

from audit.equivalence_batch import compare_batch_outputs
from workflow_1256.batch import (
    BatchItemExecutionError,
    BatchTransportRequired,
    BatchValidationError,
    build_batch_items,
    run_video_generation_batch,
)


def load_sample() -> dict:
    path = Path(__file__).parents[1] / "samples" / "synthetic" / "batch_video_generation_input.json"
    return json.loads(path.read_text(encoding="utf-8"))


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generate_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.lock = threading.Lock()

    def generate(self, params):
        with self.lock:
            self.generate_calls.append(dict(params))
        return {"task_id": f"task-{params['prompt'].split()[-1]}"}

    def query(self, params):
        with self.lock:
            self.query_calls.append(dict(params))
        task_id = params["task_id"]
        return {
            "public_video_url": {
                "task-1": "https://synthetic.example/video-1.mp4",
                "task-2": "",
                "task-3": "https://synthetic.example/video-3.mp4",
            }.get(task_id, "")
        }

    def test_success_preserves_input_order_and_fixed_mapping(self) -> None:
        result = run_video_generation_batch(
            load_sample(), generate_runner=self.generate, query_runner=self.query
        )
        self.assertEqual(
            result,
            {
                "public_video_url_list": [
                    "https://synthetic.example/video-1.mp4",
                    "",
                    "https://synthetic.example/video-3.mp4",
                ]
            },
        )
        self.assertEqual(len(self.generate_calls), 3)
        self.assertEqual(len(self.query_calls), 3)
        self.assertTrue(all(call["generate_audio"] is False for call in self.generate_calls))
        self.assertTrue(all(call["ratio"] == "9:16" for call in self.generate_calls))
        self.assertTrue(all(call["resolution"] == "480p" for call in self.generate_calls))
        self.assertTrue(all(call["watermark"] is False for call in self.generate_calls))
        self.assertTrue(all(call["video_model"] == "seedance_1_5_pro" for call in self.generate_calls))

    def test_optional_video_model_is_forwarded_to_every_video_task(self) -> None:
        params = load_sample() | {"video_model": "Seedance1.0Pro"}
        run_video_generation_batch(params, generate_runner=self.generate, query_runner=self.query)
        self.assertTrue(all(call["video_model"] == "seedance_1_0_pro" for call in self.generate_calls))

    def test_frame_continuity_can_be_disabled_before_video_task(self) -> None:
        params = load_sample() | {"use_frame_continuity": False}
        items = build_batch_items(params)
        self.assertEqual([item["ref_image_e"] for item in items], ["", "", ""])
        run_video_generation_batch(params, generate_runner=self.generate, query_runner=self.query)
        self.assertTrue(all(call["last_frame_url"] == "" for call in self.generate_calls))

    def test_frame_continuity_invalid_option_is_rejected(self) -> None:
        params = load_sample() | {"use_frame_continuity": "maybe"}
        with self.assertRaises(BatchValidationError):
            build_batch_items(params)

    def test_unsupported_video_model_is_rejected_before_batch_execution(self) -> None:
        params = load_sample() | {"video_model": "unknown"}
        with self.assertRaises(BatchValidationError):
            build_batch_items(params)
        self.assertEqual(self.generate_calls, [])

    def test_selected_canvas_is_forwarded_to_every_video_task(self) -> None:
        params = load_sample() | {"canvas": "16:9"}
        run_video_generation_batch(params, generate_runner=self.generate, query_runner=self.query)
        self.assertTrue(all(call["ratio"] == "16:9" for call in self.generate_calls))

    def test_observed_projection_comparison_has_no_field_difference(self) -> None:
        result = run_video_generation_batch(
            load_sample(), generate_runner=self.generate, query_runner=self.query
        )
        observed = [
            {"public_video_url": "https://synthetic.example/video-1.mp4"},
            {"public_video_url": ""},
            {"public_video_url": "https://synthetic.example/video-3.mp4"},
        ]
        self.assertEqual(compare_batch_outputs(observed, result), [])

    def test_empty_arrays_return_empty_array(self) -> None:
        params = {"video_prompt": [], "int_duration": [], "ref_image_f": [], "ref_image_e": []}
        result = run_video_generation_batch(
            params, generate_runner=self.generate, query_runner=self.query
        )
        self.assertEqual(result, {"public_video_url_list": []})
        self.assertEqual(self.generate_calls, [])

    def test_json_string_input_is_accepted(self) -> None:
        result = run_video_generation_batch(
            json.dumps(load_sample(), ensure_ascii=False),
            generate_runner=self.generate,
            query_runner=self.query,
        )
        self.assertEqual(len(result["public_video_url_list"]), 3)

    def test_missing_and_mismatched_fields_are_rejected(self) -> None:
        with self.assertRaises(BatchValidationError):
            build_batch_items({})
        with self.assertRaises(BatchValidationError):
            build_batch_items({"video_prompt": [], "int_duration": [], "ref_image_f": []})
        bad = load_sample()
        bad["ref_image_e"] = [""]
        with self.assertRaises(BatchValidationError):
            build_batch_items(bad)

    def test_invalid_json_and_invalid_item_types_are_rejected(self) -> None:
        with self.assertRaises(BatchValidationError):
            build_batch_items("not-json")
        bad = load_sample()
        bad["int_duration"] = [True, 7, 9]
        with self.assertRaises(BatchValidationError):
            build_batch_items(bad)
        bad = load_sample()
        bad["video_prompt"] = ["ok", 2, "ok"]
        with self.assertRaises(BatchValidationError):
            build_batch_items(bad)

    def test_limit_and_transport_requirements(self) -> None:
        too_many = {field: list(value) * 34 for field, value in load_sample().items()}
        with self.assertRaises(BatchValidationError):
            build_batch_items(too_many)
        with self.assertRaises(BatchTransportRequired):
            run_video_generation_batch(load_sample())

    def test_query_is_called_even_when_generate_has_no_task_id(self) -> None:
        def no_task(_params):
            return {"success": False, "task_id": ""}

        def fallback_query(params):
            self.query_calls.append(dict(params))
            return {
                "public_video_url": "",
                "status": "",
                "success": False,
                "tip": "",
                "file_size_mb": 0,
                "msg": "",
            }

        one = {field: [values[0]] for field, values in load_sample().items()}
        result = run_video_generation_batch(
            one, generate_runner=no_task, query_runner=fallback_query
        )
        self.assertEqual(result, {"public_video_url_list": [""]})
        self.assertEqual(self.query_calls, [{"task_id": ""}])

    def test_child_exception_keeps_batch_item_index(self) -> None:
        def failing_generate(params):
            if params["prompt"] == "synthetic prompt 2":
                raise RuntimeError("synthetic child failure")
            return self.generate(params)

        with self.assertRaises(BatchItemExecutionError) as context:
            run_video_generation_batch(
                load_sample(), generate_runner=failing_generate, query_runner=self.query
            )
        self.assertEqual(context.exception.index, 1)
        self.assertIn("第 2 项", str(context.exception))

    def test_concurrency_does_not_change_output_order(self) -> None:
        def slow_generate(params):
            time.sleep({"synthetic prompt 1": 0.03, "synthetic prompt 2": 0.02, "synthetic prompt 3": 0.01}[params["prompt"]])
            return self.generate(params)

        result = run_video_generation_batch(
            load_sample(), generate_runner=slow_generate, query_runner=self.query
        )
        self.assertEqual(result["public_video_url_list"][0], "https://synthetic.example/video-1.mp4")
        self.assertEqual(result["public_video_url_list"][2], "https://synthetic.example/video-3.mp4")

    def test_observed_17_item_duration_shape_keeps_index_order_under_concurrency(self) -> None:
        # This is the non-sensitive duration/index shape observed from the
        # successful Coze batch.  URLs and prompts are deliberately synthetic.
        durations = [7, 7, 4, 9, 5, 9, 10, 4, 8, 6, 10, 5, 8, 4, 8, 9, 4]
        params = {
            "video_prompt": [f"synthetic observed shot {index}" for index in range(17)],
            "int_duration": durations,
            "ref_image_f": [f"https://synthetic.invalid/first-{index}.png" for index in range(17)],
            "ref_image_e": [f"https://synthetic.invalid/last-{index}.png" for index in range(17)],
        }

        def generate(item):
            index = int(item["prompt"].rsplit(" ", 1)[1])
            # Finish in reverse order to prove result aggregation is index-based.
            time.sleep((16 - index) * 0.001)
            return {"task_id": f"observed-{index}"}

        def query(item):
            return {"public_video_url": f"https://synthetic.example/observed-{item['task_id'].split('-')[-1]}.mp4"}

        result = run_video_generation_batch(
            params,
            generate_runner=generate,
            query_runner=query,
            concurrent_size=8,
        )
        self.assertEqual(len(result["public_video_url_list"]), 17)
        self.assertEqual(
            result["public_video_url_list"],
            [f"https://synthetic.example/observed-{index}.mp4" for index in range(17)],
        )
        self.assertEqual(
            [item["int_duration"] for item in build_batch_items(params)],
            durations,
        )


if __name__ == "__main__":
    unittest.main()
