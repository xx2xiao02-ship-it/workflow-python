from __future__ import annotations

import json
from pathlib import Path
import unittest

from workflow_1256.story_writer import (
    CINEMATIC_SYSTEM_PROMPT,
    StoryWriterTransportRequired,
    StoryWriterValidationError,
    build_cinematic_story_prompt,
    build_story_prompt,
    normalize_cinematic_story,
    run_cinematic_story_writer,
    run_story_writer,
)


def _story() -> dict:
    return {
        "story_spine": "从看见 Token 爆发，到理解它为何成为 AI 时代的计价器，最后留下能源代价的悬念。",
        "emotional_arc": "震撼→好奇→理解→期待",
        "acts": [
            {"phase": "起", "source_anchor": "Token 日消耗量暴增", "state_before": "观众不了解规模", "trigger": "数据冲击出现", "state_after": "观众感到震撼", "next_tension": "Token 到底是什么"},
            {"phase": "承", "source_anchor": "Token 像乐高积木", "state_before": "概念陌生", "trigger": "用提问拆分举例", "state_after": "观众理解基本单位", "next_tension": "为什么它能计价"},
            {"phase": "转", "source_anchor": "AI 替代大量脑力处理", "state_before": "Token 只是技术名词", "trigger": "与钢铁和电力计量类比", "state_after": "观众看见智力生产被计量", "next_tension": "这种智力为何要消耗巨大能源"},
            {"phase": "合", "source_anchor": "大佬争抢核电站", "state_before": "理解 Token 价值", "trigger": "抛出疯狂电费战", "state_after": "观众期待下一集", "next_tension": "AI 背后的能源争夺"},
        ],
    }


def _cinematic_story() -> dict:
    value = {
        "opening_title": "效率越高，代价越大？",
        "movie_outline": {
            "protagonist": "城市职场新人",
            "protagonist_goal": "在 AI 冲击中完成关键交付",
            "central_conflict": "任务压力与工具带来的新竞争同时逼近",
            "character_arc": "从措手不及到理解并主动使用工具",
            "ending_hook": "成功之后看见更大的系统能源代价",
        },
        "source_semantic_anchor": {"core_thesis":"智力生产进入计量治理","old_system":"经验救火","new_capability":"流程化协作","governance_choice":"是否重构组织","systemic_cost":"能源与成本压力"},
        "story_anchor": {"premise":"新人在期限前重组失控任务","protagonist_want":"完成交付","opposing_force":"任务洪流","stakes_if_fail":"失去证明机会","point_of_no_return":"交出最后资料","decisive_choice":"主动重组任务","transformation":"从慌乱到主动","final_image_hook":"机房灯光不灭"},
        "supporting_characters": [
            {"name":"李叔","role":"期限压力","own_goal":"按时清场","relationship_to_protagonist":"项目管理员","conflict_or_alliance":"要求她停止整理","decisive_interaction":"抽走她面前的资料箱"},
            {"name":"周岚","role":"失败代价","own_goal":"保住团队成果","relationship_to_protagonist":"同事","conflict_or_alliance":"先质疑后协助","decisive_interaction":"把最后一箱资料递回给她"},
        ],
        "conflict_chain": {"external":"资料和期限同时压迫","relationship":"李叔清场与周岚观望","internal":"林夏在保守与冒险间选择","escalation":"资料箱被抽走后她改用新方法"},
        "silent_story_text": "深夜办公室里，资料堆压向桌面。李叔抽走林夏面前的资料箱，周岚站在门边迟疑。林夏先被涌来的任务逼退，随后把散乱纸页归整为可执行的工作流；周岚把最后一箱资料递回。清晨的玻璃幕墙外，远处机房的灯光仍持续亮着。",
        "scene_groups": [
            {
                "scene_id": "scene_01", "group_ids": ["g01"], "scene_purpose": "建立规模压力",
                "state_before": "主角独自面对堆积任务", "visible_conflict": "资料和时限同时压近",
                "turn": "她发现可被拆分的工作单位", "state_after": "混乱开始出现秩序",
            },
            {
                "scene_id": "scene_02", "group_ids": ["g02"], "scene_purpose": "把工具转化为行动",
                "state_before": "主角还不理解工具的代价", "visible_conflict": "交付速度与资源消耗同时增加",
                "turn": "她完成交付并望向持续运转的机房", "state_after": "新的问题被留下",
            },
        ],
        "narration_mappings": [
            {
                "group_id": "g01", "scene_id": "scene_01", "semantic_mapping": "Token 规模增长映射为任务潮水般压来",
                "silent_action": "纸页从桌面边缘滑落，主角按类别重新归拢", "metaphor": "散页是可拆分的信息单元",
                "state_before": "被规模吓住", "state_after": "发现秩序", "bridge_to_next": "归拢后的资料引向新的交付动作",
            },
            {
                "group_id": "g02", "scene_id": "scene_02", "semantic_mapping": "智力计价与能耗问题映射为完成交付后的远景机房",
                "silent_action": "主角合上交付文件，隔窗看见机房灯光延伸到天际", "metaphor": "持续亮起的机房是计算代价",
                "state_before": "理解效率", "state_after": "意识到代价", "bridge_to_next": "把能源问题留给下一集",
            },
        ],
    }
    for mapping in value["narration_mappings"]:
        mapping.update({
            "source_claim": "原文观点", "story_range": "完整故事中的连续事件",
            "trailer_highlight": "主角面对一次可见变化", "conflict": "正在升级的阻力",
            "choice_or_action": "主角采取可见行动", "consequence": "行动改变当前状态",
            "trailer_hook": "把新的风险留给下一段",
        })
    return value


class StoryWriterTests(unittest.TestCase):
    def test_runs_as_a_standalone_preview_with_injected_transport(self) -> None:
        received = []

        def transport(system, user):
            received.append((system, user))
            return json.dumps(_story(), ensure_ascii=False)

        result = run_story_writer("一篇测试文案", transport=transport)
        self.assertEqual([item["phase"] for item in result["acts"]], ["起", "承", "转", "合"])
        self.assertIn("一篇测试文案", received[0][1])
        self.assertIn("不得虚构", received[0][0])

    def test_requires_explicit_transport(self) -> None:
        with self.assertRaises(StoryWriterTransportRequired):
            run_story_writer("一篇测试文案")

    def test_rejects_missing_story_state(self) -> None:
        value = _story()
        del value["acts"][1]["trigger"]
        with self.assertRaises(StoryWriterValidationError):
            run_story_writer("一篇测试文案", transport=lambda *_: value)

    def test_rejects_wrong_act_order(self) -> None:
        value = _story()
        value["acts"][2]["phase"] = "合"
        with self.assertRaises(StoryWriterValidationError):
            run_story_writer("一篇测试文案", transport=lambda *_: value)

    def test_prompt_rejects_blank_text(self) -> None:
        with self.assertRaises(StoryWriterValidationError):
            build_story_prompt(" ")

    def test_cinematic_story_keeps_group_order_and_timeline_binding(self) -> None:
        received = []

        def transport(system, user):
            received.append((system, user))
            return _cinematic_story()

        result = run_cinematic_story_writer(
            "完整文案", ["第一大段", "第二大段"],
            timelines=[{"start": 0, "end": 3_000_000}, {"start": 3_000_000, "end": 6_000_000}],
            transport=transport,
        )
        self.assertEqual([item["group_id"] for item in result["narration_mappings"]], ["g01", "g02"])
        self.assertEqual(result["scene_groups"][0]["group_ids"], ["g01"])
        self.assertEqual(result["opening_title"], "效率越高，代价越大？")
        self.assertLessEqual(len(result["opening_title"]), 12)
        self.assertEqual(received[0][0], CINEMATIC_SYSTEM_PROMPT)
        self.assertIn("无对白", received[0][0])
        self.assertIn("opening_title", received[0][0])
        self.assertIn('"start": 3000000', received[0][1])

    def test_cinematic_story_prompt_prioritizes_named_person_and_manual_profile(self) -> None:
        received = []
        value = _cinematic_story()
        value["movie_outline"]["protagonist"] = "褚时健，男性，老年企业家"

        result = run_cinematic_story_writer(
            "褚时健在企业改革中承担代价。",
            ["第一大段", "第二大段"],
            transport=lambda system, prompt: received.append((system, prompt)) or value,
            protagonist_name="褚时健",
            protagonist_profile={
                "mode": "manual",
                "name": "褚时健",
                "gender": "male",
                "age_group": "elderly",
                "identity": "企业家",
            },
        )

        assert result["movie_outline"]["protagonist"] == "褚时健，男性，老年企业家"
        assert '"name": "褚时健"' in received[0][1]
        assert '"gender": "male"' in received[0][1]
        assert "不得替换成虚构的年轻女性" in received[0][1]

    def test_cinematic_story_rejects_reordered_group_mapping(self) -> None:
        value = _cinematic_story()
        value["narration_mappings"][1]["group_id"] = "g01"
        with self.assertRaises(StoryWriterValidationError):
            normalize_cinematic_story(value, ["第一大段", "第二大段"])

    def test_cinematic_story_replaces_generic_opening_title_with_suspense_fallback(self) -> None:
        value = _cinematic_story()
        value["opening_title"] = "真正改变一切的是什么？"
        result = normalize_cinematic_story(value, ["第一大段", "第二大段"])
        assert result["opening_title"] != "真正改变一切的是什么？"
        assert len(result["opening_title"]) <= 12
        assert "谁来买单" in result["opening_title"]

    def test_cinematic_story_strict_review_rejects_wrong_lead_or_readable_interface(self) -> None:
        value = _cinematic_story()
        value["movie_outline"]["protagonist"] = "林夏，城市整理师"
        result = run_cinematic_story_writer(
            "完整文案", ["第一大段", "第二大段"],
            transport=lambda *_: value,
            protagonist_name="林夏",
            strict_silent_film=True,
        )
        self.assertEqual(result["movie_outline"]["protagonist"], "林夏，城市整理师")
        value["silent_story_text"] = "林夏面对屏幕，停下了脚步。"
        with self.assertRaisesRegex(StoryWriterValidationError, "禁止"):
            run_cinematic_story_writer(
                "完整文案", ["第一大段", "第二大段"],
                transport=lambda *_: value,
                protagonist_name="林夏",
                strict_silent_film=True,
            )

    def test_cinematic_story_retries_once_after_silent_film_quality_gate(self) -> None:
        invalid = _cinematic_story()
        invalid["silent_story_text"] = "林夏把封条贴在柜门上。"
        valid = _cinematic_story()
        calls = []

        def transport(_system, prompt):
            calls.append(prompt)
            return invalid if len(calls) == 1 else valid

        result = run_cinematic_story_writer(
            "完整文案", ["第一大段", "第二大段"],
            transport=transport,
            strict_silent_film=True,
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("上一版生成未通过自动质量门", calls[1])
        self.assertIn("每位配角的名字", calls[1])
        self.assertIn("默片故事含有禁止", calls[1])
        self.assertEqual(result["silent_story_text"], valid["silent_story_text"])

    def test_cinematic_story_rejects_one_scene_per_group_for_long_copy(self) -> None:
        value = _cinematic_story()
        value["scene_groups"] = [
            {
                "scene_id": f"scene_{index:02d}", "group_ids": [f"g{index:02d}"], "scene_purpose": "场景目的",
                "state_before": "之前", "visible_conflict": "冲突", "turn": "变化", "state_after": "之后",
            }
            for index in range(1, 7)
        ]
        value["narration_mappings"] = [
            {
                "group_id": f"g{index:02d}", "scene_id": f"scene_{index:02d}", "semantic_mapping": "映射",
                "silent_action": "动作", "metaphor": "隐喻", "state_before": "之前", "state_after": "之后", "bridge_to_next": "承接",
            }
            for index in range(1, 7)
        ]
        with self.assertRaisesRegex(StoryWriterValidationError, "scene_groups"):
            normalize_cinematic_story(value, [f"第 {index} 段" for index in range(1, 7)])

    def test_cinematic_prompt_rejects_timeline_count_drift(self) -> None:
        with self.assertRaises(StoryWriterValidationError):
            build_cinematic_story_prompt("完整文案", ["第一段", "第二段"], timelines=[{"start": 0, "end": 1}])

    def test_token_copy_fixture_enters_cinematic_story_contract(self) -> None:
        fixture_path = Path(__file__).resolve().parents[1] / "samples" / "synthetic" / "token_cinematic_story_input.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        received = []
        group_ids = [f"g{index:02d}" for index in range(1, len(fixture["segments"]) + 1)]

        def transport(system, user):
            received.append((system, user))
            return {
                "movie_outline": {
                    "protagonist": "城市职场新人", "protagonist_goal": "完成关键交付",
                    "central_conflict": "任务压力与 AI 变化同时逼近", "character_arc": "从慌乱到主动使用工具",
                    "ending_hook": "机房灯光把能源问题留到下一集",
                },
                "silent_story_text": "主角在深夜办公室处理涌来的资料，学会归类和调度，最后隔窗看见远处机房整夜不灭的灯光。",
                "scene_groups": [
                    {"scene_id": "scene_01", "group_ids": group_ids[:3], "scene_purpose": "压力与理解", "state_before": "任务失控", "visible_conflict": "资料涌入", "turn": "发现可拆分的单位", "state_after": "建立秩序"},
                    {"scene_id": "scene_02", "group_ids": group_ids[3:6], "scene_purpose": "效率与成长", "state_before": "掌握工具", "visible_conflict": "交付要求升级", "turn": "完成高强度分析", "state_after": "看见能力放大"},
                    {"scene_id": "scene_03", "group_ids": group_ids[6:], "scene_purpose": "代价与悬念", "state_before": "完成交付", "visible_conflict": "能源消耗持续扩大", "turn": "远望机房", "state_after": "留下下一集问题"},
                ],
                "narration_mappings": [
                    {"group_id": group_id, "scene_id": "scene_01" if index < 3 else "scene_02" if index < 6 else "scene_03", "semantic_mapping": f"第 {index + 1} 段观点映射为主角状态变化", "silent_action": "整理、移动或完成可见任务", "metaphor": "工作对象承担抽象观点", "state_before": "上一状态", "state_after": "下一状态", "bridge_to_next": "以连续动作承接下一段"}
                    for index, group_id in enumerate(group_ids)
                ],
            }

        result = run_cinematic_story_writer(
            fixture["text"], fixture["segments"], timelines=fixture["timelines"], transport=transport,
            strict_trailer_contract=False,
        )
        self.assertEqual(fixture["evidence_level"], "synthetic")
        self.assertEqual([item["group_id"] for item in result["narration_mappings"]], group_ids)
        self.assertEqual(result["scene_groups"][0]["group_ids"], group_ids[:3])
        self.assertIn("140 万亿", received[0][1])
        self.assertIn('"end": 39000000', received[0][1])


if __name__ == "__main__":
    unittest.main()
