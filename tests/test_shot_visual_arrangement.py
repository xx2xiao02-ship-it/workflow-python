from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from audit.coze_adapter_shot_visual_arrangement import (
    run_original_and_migrated,
    run_migrated_with_model_responses,
)
from audit.equivalence_shot_visual_arrangement import compare_results, output_shape
from workflow_1256.shot_visual_arrangement import (
    FEATURE_BATCH_SIZE,
    FEATURE_EVALUATOR_SYSTEM_PROMPT,
    GLOBAL_PLANNER_MODEL,
    GPT_SYSTEM_PROMPT,
    ShotVisualArrangementTransportRequired,
    apply_visual_director_lock,
    build_sequence,
    gpt_plan,
    repair_plan_rhythm,
    run_classification_feature_batches,
    run_shot_visual_arrangement,
    validate_plan,
)
from workflow_1256.governance.media_route_scoring_v3 import CLASSIFICATION_FEATURE_KEYS


class ShotVisualArrangementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1]
        cls.synthetic_dir = root / "samples" / "synthetic"
        cls.real_dir = root / "samples" / "real"
        cls.synthetic_input = json.loads(
            (cls.synthetic_dir / "shot_visual_arrangement_input.json").read_text(encoding="utf-8")
        )
        cls.synthetic_models = json.loads(
            (cls.synthetic_dir / "shot_visual_arrangement_model_responses.json").read_text(encoding="utf-8")
        )
        cls.real_input = json.loads(
            (cls.real_dir / "run-7668196429877870619-116616-shot-visual-arrangement-input.json")
            .read_text(encoding="utf-8")
        )
        cls.real_models = json.loads(
            (cls.real_dir / "run-7668196429877870619-116616-shot-visual-arrangement-model-responses.json")
            .read_text(encoding="utf-8")
        )

    @classmethod
    def run_pair(cls, input_value, models=None, **kwargs):
        models = models or cls.synthetic_models
        return run_original_and_migrated(
            input_value,
            models["gpt_raw"],
            models["mini_raw_by_group"],
            **kwargs,
        )

    @staticmethod
    def multi_shot_input(count: int) -> dict:
        code_list = []
        segments = []
        refs = []
        end = 0
        for index in range(count):
            start = end
            end = start + 8000000
            group_index = index + 1
            code_list.append({
                "clip_duration": [8],
                "int_duration": [8],
                "shots": [{
                    "clip_duration": 8,
                    "clip_role": "状态建立" if group_index == 1 else "结果落点",
                    "source_text": f"镜头 {group_index} 的语义文本",
                    "story_beat": f"第 {group_index} 条镜头承接状态",
                }],
                "timelines": [{"start": start, "end": end}],
            })
            segments.append(f"第 {group_index} 段")
            refs.append(f"ref-{group_index}")
        return {
            "Code_list": code_list,
            "segments": segments,
            "ref_image": refs,
            "debug": False,
        }

    @staticmethod
    def mini_materials(count: int) -> dict:
        materials = {}
        for index in range(1, count + 1):
            shot_id = f"g{index:02d}_s01"
            materials[f"g{index:02d}"] = {"materials": [{
                "shot_id": shot_id,
                "subject_action": "人物保持当前动作",
                "space_layers": "前景物件，中景人物，后景空间",
                "key_objects": "桌面文件",
                "lighting_mood": "环境自然光",
                "motion_seed": "动作轻微延续后停住",
            }]}
        return materials

    def assert_pair_equal(self, original, migrated) -> None:
        mismatches = compare_results(original, migrated)
        self.assertEqual(mismatches, [])

    def test_synthetic_full_output_and_order(self) -> None:
        original, migrated = self.run_pair(self.synthetic_input)
        self.assert_pair_equal(original, migrated)
        self.assertEqual(
            list(migrated),
            ["prompt", "ref_image", "motion_seed", "timelines", "int_duration", "error", "debug"],
        )
        self.assertEqual(output_shape(migrated)["prompt_count"], 2)
        self.assertEqual(output_shape(migrated)["timeline_start_end"], [[0, 8000000], [8000000, 18000000]])
        self.assertEqual(migrated["int_duration"], [8, 10])
        self.assertEqual([item["ref_image"] for item in migrated["ref_image"]], [
            self.synthetic_input["ref_image"], self.synthetic_input["ref_image"]
        ])

    def test_real_success_input_shape_and_same_code_path(self) -> None:
        original, migrated = self.run_pair(self.real_input, self.real_models)
        self.assert_pair_equal(original, migrated)
        self.assertEqual(len(migrated["prompt"]), 17)
        self.assertEqual(len(migrated["motion_seed"]), 17)
        self.assertEqual(len(migrated["timelines"]), 17)
        self.assertEqual(len(migrated["int_duration"]), 17)
        self.assertEqual(migrated["timelines"][0], {"start": 0, "end": 6906732})
        self.assertEqual(migrated["timelines"][-1], {"start": 109416000, "end": 111672000})
        self.assertEqual(migrated["int_duration"], [7, 7, 4, 10, 6, 9, 10, 5, 8, 6, 10, 5, 8, 4, 9, 9, 4])

    def test_debug_fields_types_and_counts_match(self) -> None:
        input_value = copy.deepcopy(self.synthetic_input)
        input_value["debug"] = True
        original, migrated = self.run_pair(input_value)
        mismatches = compare_results(original, migrated)
        self.assertEqual(
            [item for item in mismatches if item["path"] not in {"$.debug.gpt_system_chars", "$.debug.gpt_input_chars"}],
            [],
        )
        self.assertEqual(migrated["debug"]["gpt_system_chars"], len(GPT_SYSTEM_PROMPT))
        self.assertNotEqual(
            original["debug"]["gpt_system_chars"],
            migrated["debug"]["gpt_system_chars"],
        )
        self.assertGreater(
            migrated["debug"]["gpt_input_chars"],
            original["debug"]["gpt_input_chars"],
        )
        self.assertEqual(
            list(migrated["debug"]),
            [
                "build_ms", "gpt_ms", "gpt_input_chars", "gpt_system_chars",
                "gpt_output_chars", "gpt_group_count", "gpt_shot_count",
                "plan_validate_ms", "mini_ms", "assemble_ms", "mini_batches",
                "mini_success_batches", "mini_fallback_batches",
                "mini_parallel_workers", "prompt_count", "motion_seed_count",
                "total_ms",
            ],
        )
        self.assertEqual(migrated["debug"]["gpt_group_count"], 2)
        self.assertEqual(migrated["debug"]["gpt_shot_count"], 2)
        self.assertEqual(migrated["debug"]["prompt_count"], 2)

    def test_empty_arrays_match_without_model_call(self) -> None:
        empty = {"Code_list": [], "ref_image": [], "segments": [], "debug": False}
        original, migrated = self.run_pair(empty)
        self.assert_pair_equal(original, migrated)
        self.assertEqual(migrated["prompt"], [])
        self.assertNotEqual(migrated["error"], "")

    def test_invalid_timeline_matches(self) -> None:
        broken = copy.deepcopy(self.synthetic_input)
        broken["Code_list"][0]["timelines"][0] = {"start": 8000000, "end": 8000000}
        original, migrated = self.run_pair(broken)
        self.assert_pair_equal(original, migrated)
        self.assertEqual(migrated["prompt"], [])
        self.assertIn("时间线非法", migrated["error"])

    def test_missing_field_matches(self) -> None:
        broken = copy.deepcopy(self.synthetic_input)
        del broken["Code_list"][0]["shots"][0]["story_beat"]
        original, migrated = self.run_pair(broken)
        self.assert_pair_equal(original, migrated)
        self.assertIn("缺少语义字段", migrated["error"])

    def test_json_string_input_matches_original_behavior(self) -> None:
        encoded = json.dumps(self.synthetic_input, ensure_ascii=False)
        original, migrated = self.run_pair(encoded)
        self.assert_pair_equal(original, migrated)
        self.assertEqual(migrated["prompt"], [])
        self.assertNotEqual(migrated["error"], "")

    def test_malformed_model_response_matches(self) -> None:
        models = {
            "gpt_raw": "{bad json",
            "mini_raw_by_group": self.synthetic_models["mini_raw_by_group"],
        }
        original, migrated = self.run_pair(self.synthetic_input, models)
        mismatches = compare_results(original, migrated)
        self.assertEqual([item for item in mismatches if item["path"] != "$.error"], [])
        self.assertEqual(migrated["prompt"], [])
        self.assertIn("全局视觉规划模型校验失败", migrated["error"])
        self.assertNotIn("DeepSeek", migrated["error"])

    def test_global_plan_count_error_reports_expected_and_actual_counts(self) -> None:
        records = [{"shot_id": "g01_s01"}, {"shot_id": "g01_s02"}]
        plans, error = validate_plan({"p": []}, records)
        self.assertIsNone(plans)
        self.assertEqual(error, "全局视觉规划模型输出镜头数不一致：期望 2，实际 0。")

    def test_global_plan_payload_declares_ordered_ids_and_keeps_features_out(self) -> None:
        sequence, error = build_sequence(self.synthetic_input)
        self.assertEqual(error, "")
        captured: dict[str, object] = {}

        def transport(payload):
            captured["payload"] = payload
            return self.synthetic_models["gpt_raw"]

        _data, error, _stats = gpt_plan(sequence["gpt_groups"], transport)
        self.assertEqual(error, "")
        payload = captured["payload"]
        request = json.loads(payload["messages"][1]["content"])
        self.assertEqual(request["contract"]["expected_shot_count"], 2)
        self.assertEqual(request["contract"]["expected_shot_ids"], ["g01_s01", "g02_s01"])
        self.assertNotIn("motion_need、temporal_dependency、static_completeness", GPT_SYSTEM_PROMPT)
        self.assertIn("expected_shot_ids", GPT_SYSTEM_PROMPT)

    def test_feature_batches_split_large_group_and_preserve_order(self) -> None:
        shots = []
        timelines = []
        durations = []
        for index in range(7):
            start = index * 8_000_000
            shots.append({
                "clip_duration": 8,
                "clip_role": "状态建立" if index == 0 else "结果落点",
                "source_text": f"第 {index + 1} 条镜头文本",
                "story_beat": f"第 {index + 1} 条镜头承接状态",
            })
            timelines.append({"start": start, "end": start + 8_000_000})
            durations.append(8)
        input_value = {
            "Code_list": [{"clip_duration": durations, "int_duration": durations, "shots": shots, "timelines": timelines}],
            "segments": ["这是一个需要拆成两个特征批次的段落"],
            "ref_image": [],
            "debug": False,
        }
        calls: list[list[str]] = []

        def transport(payload):
            request = json.loads(payload["messages"][1]["content"])
            shot_ids = request["contract"]["expected_shot_ids"]
            calls.append(shot_ids)
            return {
                "classification_features": [
                    {
                        "shot_id": shot_id,
                        **{key: 50 for key in CLASSIFICATION_FEATURE_KEYS},
                        "evidence": {key: "固定测试中文证据" for key in CLASSIFICATION_FEATURE_KEYS},
                    }
                    for shot_id in shot_ids
                ]
            }

        features, stats = run_classification_feature_batches(
            input_value,
            transport=transport,
            batch_size=FEATURE_BATCH_SIZE,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], [f"g01_s{index:02d}" for index in range(1, 7)])
        self.assertEqual(calls[1], ["g01_s07"])
        self.assertEqual([item["shot_id"] for item in features], [f"g01_s{index:02d}" for index in range(1, 8)])
        self.assertEqual(stats["feature_batch_count"], 2)
        self.assertIn("classification_features", FEATURE_EVALUATOR_SYSTEM_PROMPT)

    def test_mini_exception_falls_back_in_both_versions(self) -> None:
        original, migrated = self.run_pair(
            self.synthetic_input,
            mini_failure_groups={"g02"},
        )
        self.assert_pair_equal(original, migrated)
        self.assertEqual(migrated["debug"], {})
        self.assertEqual(len(migrated["prompt"]), 2)
        self.assertEqual(migrated["motion_seed"][1].startswith("人物保持首帧姿势"), True)

    def test_new_implementation_requires_explicit_transports(self) -> None:
        with self.assertRaises(ShotVisualArrangementTransportRequired):
            run_shot_visual_arrangement(self.synthetic_input)

    def test_v31_features_are_required_and_preserved_from_segment_batches(self) -> None:
        model = copy.deepcopy(self.synthetic_models["gpt_raw"])
        feature_calls: list[list[str]] = []

        def feature_response(payload, *, broken=False):
            request = json.loads(payload["messages"][1]["content"])
            shot_ids = request["contract"]["expected_shot_ids"]
            feature_calls.append(shot_ids)
            rows = []
            for shot_id in shot_ids:
                evidence = {key: "固定测试中文证据" for key in CLASSIFICATION_FEATURE_KEYS}
                if broken and shot_id == "g02_s01":
                    del evidence["motion_need"]
                rows.append({
                    "shot_id": shot_id,
                    **{key: 50 for key in CLASSIFICATION_FEATURE_KEYS},
                    "evidence": evidence,
                })
            return {"classification_features": rows}

        def gpt_transport(_payload):
            return model

        def mini_transport(payload):
            sequence = json.loads(payload["input"][1]["content"][0]["text"])["sequence"]
            group_id = sequence[0]["shot_id"].split("_", 1)[0]
            return self.synthetic_models["mini_raw_by_group"][group_id]

        result = run_shot_visual_arrangement(
            self.synthetic_input,
            gpt_transport=gpt_transport,
            mini_transport=mini_transport,
            feature_transport=lambda payload: feature_response(payload),
            require_classification_features=True,
        )
        self.assertEqual(result["error"], "")
        self.assertEqual([item["shot_id"] for item in result["classification_features"]], ["g01_s01", "g02_s01"])
        self.assertEqual(feature_calls, [["g01_s01"], ["g02_s01"]])

        failed = run_shot_visual_arrangement(
            self.synthetic_input,
            gpt_transport=lambda _payload: model,
            mini_transport=mini_transport,
            feature_transport=lambda payload: feature_response(payload, broken=True),
            require_classification_features=True,
        )
        self.assertIn("FEATURE_CONTRACT_INVALID", failed["error"])

    def test_global_planner_uses_original_seed_transport_label(self) -> None:
        self.assertEqual(GLOBAL_PLANNER_MODEL, "seed-2.1-turbo")
        source = Path(__file__).parents[1] / "src" / "workflow_1256" / "shot_visual_arrangement.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("deepseek-v4-pro", text)
        self.assertNotIn("DeepSeek V4", text)

    def test_visual_director_requires_wide_mid_close_coverage_for_long_sequences(self) -> None:
        self.assertIn("必须同时覆盖远/全类", GPT_SYSTEM_PROMPT)
        records = [
            {"shot_id": f"g01_s{index:02d}"}
            for index in range(1, 6)
        ]
        plans = {
            "p": [
                [record["shot_id"], "办公区", "全景", "完整人物", "平视", "斜向纵深", "人物与桌面"]
                if index % 2 == 0 else
                [record["shot_id"], "办公区", "近景", "人物局部", "侧视", "局部焦点与留白", "手部动作"]
                for index, record in enumerate(records)
            ]
        }
        _, error = validate_plan(plans, records)
        self.assertEqual(error, "五条及以上镜头缺少中景。")

    def test_visual_director_receives_story_role_and_established_state(self) -> None:
        sequence, error = build_sequence(self.synthetic_input)
        self.assertEqual(error, "")
        first = sequence["gpt_groups"][0]["s"][0]
        source = self.synthetic_input["Code_list"][0]["shots"][0]
        self.assertEqual(first[0], "g01_s01")
        self.assertEqual(first[2], source["clip_role"])
        self.assertEqual(first[3], source["story_beat"])
        self.assertIn("故事连续性", GPT_SYSTEM_PROMPT)
        self.assertIn("不得逐句把逻辑词做成示意画面", GPT_SYSTEM_PROMPT)


    def test_approved_story_context_reaches_first_frame_prompt(self) -> None:
        input_value = copy.deepcopy(self.synthetic_input)
        input_value["story_context"] = {
            "scene_groups": [{
                "scene_id": "scene_01",
                "group_ids": ["g01"],
                "scene_purpose": "UNIQUE_FIRST_FRAME_SCENE",
                "visible_conflict": "UNIQUE_FIRST_FRAME_CONFLICT",
                "turn": "UNIQUE_FIRST_FRAME_TURN",
                "state_before": "UNIQUE_FIRST_FRAME_BEFORE",
                "state_after": "UNIQUE_FIRST_FRAME_AFTER",
            }],
            "narration_mappings": [{
                "group_id": "g01",
                "scene_id": "scene_01",
                "semantic_mapping": "UNIQUE_FIRST_FRAME_MAPPING",
                "silent_action": "UNIQUE_FIRST_FRAME_ACTION",
                "metaphor": "UNIQUE_FIRST_FRAME_METAPHOR",
                "state_before": "UNIQUE_FIRST_FRAME_BEFORE",
                "state_after": "UNIQUE_FIRST_FRAME_AFTER",
                "bridge_to_next": "UNIQUE_FIRST_FRAME_BRIDGE",
            }],
        }
        migrated = run_migrated_with_model_responses(
            input_value,
            self.synthetic_models["gpt_raw"],
            self.synthetic_models["mini_raw_by_group"],
        )
        self.assertEqual(migrated["error"], "")
        self.assertEqual(len(migrated["prompt"]), len(migrated["story_context"]))
        self.assertIn("UNIQUE_FIRST_FRAME_SCENE", migrated["prompt"][0])
        self.assertIn("UNIQUE_FIRST_FRAME_ACTION", migrated["prompt"][0])
        self.assertIn("UNIQUE_FIRST_FRAME_METAPHOR", migrated["prompt"][0])

    def test_visual_director_lock_overrides_material_camera_plan(self) -> None:
        plans = [{
            "shot_id": "g01_s01", "scene_context": "办公室", "shot_size": "中景",
            "carrier_mode": "完整人物", "viewpoint": "平视",
            "composition": "斜向纵深", "visual_focus": "文件夹",
        }]
        records = [{"story_context": {"visual_direction": {
            "shot_size": "特写", "camera_angle": "低机位", "composition": "边缘压力切入",
            "staging": "手指压住文件边缘", "lighting": "冷白顶光", "visual_motif": "褶皱文件",
        }}}]
        locked = apply_visual_director_lock(plans, records)
        self.assertEqual(locked[0]["shot_size"], "特写")
        self.assertEqual(locked[0]["viewpoint"], "低机位")
        self.assertEqual(locked[0]["composition"], "边缘压力切入")

    def test_empty_shot_accepts_consciousness_and_ultra_wide_aliases(self) -> None:
        plans, error = validate_plan({
            "p": [[
                "g01_s05", "空铺外的街角空间", "超大全景", "意识流", "高机位",
                "斜向纵深", "空铺与街道之间的冷清距离",
            ]]
        }, [{
            "shot_id": "g01_s05",
            "story_context": {"shot_design_type": "空间空镜"},
        }])
        self.assertEqual(error, "")
        self.assertEqual(plans[0]["shot_size"], "超远景")
        self.assertEqual(plans[0]["carrier_mode"], "空间")

    def test_object_empty_shot_repairs_known_conflicting_carrier(self) -> None:
        plans, error = validate_plan({
            "p": [[
                "g03_s01", "水果店经营场景", "全景", "完整人物", "平视",
                "多人层级", "水果店门口与货物之间的关系",
            ]]
        }, [{
            "shot_id": "g03_s01",
            "story_context": {"shot_design_type": "物件空镜"},
        }])
        self.assertEqual(error, "")
        self.assertEqual(plans[0]["carrier_mode"], "物件")

    def test_rhythm_validation_returns_parsed_plan_for_repair(self) -> None:
        records = [{"shot_id": f"g01_s{index:02d}"} for index in range(1, 5)]
        raw_plans = {
            "p": [
                [record["shot_id"], "办公区", size, carrier, "平视", "斜向纵深", "人物与桌面"]
                for record, size, carrier in zip(
                    records,
                    ["中景", "中景", "中景", "近景"],
                    ["完整人物", "完整人物", "人物局部", "完整人物"],
                    strict=True,
                )
            ]
        }
        parsed, error = validate_plan(raw_plans, records)
        self.assertEqual(error, "存在连续三条相同景别。")
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed), 4)

    def test_repair_consecutive_identical_shot_sizes(self) -> None:
        gpt_raw = {"p": [
            ["g01_s01", "办公区", "中景", "完整人物", "平视", "斜向纵深", "人物与桌面"],
            ["g02_s01", "办公区", "中景", "完整人物", "平视", "斜向纵深", "人物与桌面"],
            ["g03_s01", "办公区", "中景", "完整人物", "平视", "斜向纵深", "人物与桌面"],
        ]}
        migrated = run_migrated_with_model_responses(
            self.multi_shot_input(3), gpt_raw, self.mini_materials(3)
        )
        self.assertEqual(migrated["error"], "")
        self.assertEqual(len(migrated["prompt"]), 3)
        repaired, repair_error = repair_plan_rhythm(
            [
                {"shot_id": f"g{index:02d}_s01", "shot_size": "中景", "carrier_mode": "完整人物"}
                for index in range(1, 4)
            ],
            [],
        )
        self.assertEqual(repair_error, "")
        self.assertEqual(len({shot["shot_size"] for shot in repaired}), 2)

    def test_repair_missing_mid_shot_size_for_long_sequence(self) -> None:
        sizes = ["全景", "近景", "全景", "近景", "全景"]
        gpt_raw = {"p": [
            [f"g{index:02d}_s01", "办公区", sizes[index - 1],
             "人物局部" if index % 2 == 0 else "完整人物", "平视", "斜向纵深", "人物与桌面"]
            for index in range(1, 6)
        ]}
        migrated = run_migrated_with_model_responses(
            self.multi_shot_input(5), gpt_raw, self.mini_materials(5)
        )
        self.assertEqual(migrated["error"], "")
        self.assertTrue(any("中景景别" in prompt for prompt in migrated["prompt"]))


if __name__ == "__main__":
    unittest.main()
