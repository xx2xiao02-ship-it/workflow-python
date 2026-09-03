from __future__ import annotations

from pathlib import Path

from workflow_1256.first_frame_grid import build_first_frame_grid_plan
from workflow_1256.first_frame_visual_guidance_expander import (
    ExistingModelGuidanceExpanderAdapter,
    LightweightVisualGuidanceExpander,
    VisualGuidanceExpanderConfig,
)


class _FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def expand(self, payload):
        self.calls.append(payload)
        return self.result


def _config(tmp_path: Path) -> VisualGuidanceExpanderConfig:
    return VisualGuidanceExpanderConfig(
        enabled=True,
        mode="ON",
        provider="test-provider",
        model="small-test-model",
        model_version="test-v1",
        cache_path=tmp_path / "guidance-cache.json",
    )


def test_expander_preserves_original_and_returns_traceable_directives(tmp_path: Path):
    runner = _FakeRunner({
        "status": "EXPANDED",
        "directives": [
            {
                "source_phrase": "微缩场景",
                "instruction": "所有宫格均以独立、比例明确的微缩模型舞台呈现，主体、关键道具与局部环境必须在本格内闭合。",
            },
            {
                "source_phrase": "白色背景",
                "instruction": "每格外部保持干净纯白背景，仅保留该格必要的小型底座，不得用连续实景背景铺满画面。",
            },
        ],
    })
    original = "每格都是微缩场景，白色背景"

    audit = LightweightVisualGuidanceExpander(_config(tmp_path), runner).expand(
        original,
        context={"grid_layout": "3x3"},
    )

    assert audit["original_user_guidance"] == original
    assert audit["guidance_expansion_status"] == "EXPANDED"
    assert audit["enhanced_directive"].startswith("所有宫格均以独立")
    assert [item["source_phrase"] for item in audit["guidance_expansion_directives"]] == [
        "微缩场景", "白色背景"
    ]
    assert runner.calls[0]["config"]["model"] == "small-test-model"
    assert "陈岭" not in runner.calls[0]["input"]["original_user_guidance"]


def test_expander_cache_prevents_repeat_model_call(tmp_path: Path):
    runner = _FakeRunner({
        "status": "EXPANDED",
        "directives": [{"source_phrase": "白色背景", "instruction": "所有宫格外部保持纯白背景。"}],
    })
    expander = LightweightVisualGuidanceExpander(_config(tmp_path), runner)

    first = expander.expand("白色背景", context={"grid_layout": "3x3"})
    second = expander.expand("白色背景", context={"grid_layout": "3x3"})

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert len(runner.calls) == 1
    assert (tmp_path / "guidance-cache.json").is_file()


def test_expander_rejects_model_added_story_facts_and_falls_back(tmp_path: Path):
    runner = _FakeRunner({
        "status": "EXPANDED",
        "directives": [
            {"source_phrase": "白色背景", "instruction": "让陈岭站在医院走廊，并保持白色背景。"},
        ],
    })

    audit = LightweightVisualGuidanceExpander(_config(tmp_path), runner).expand("白色背景")

    assert audit["guidance_expansion_status"] == "ERROR"
    assert audit["enhanced_directive"] == ""
    assert "新增未在用户原文声明的故事事实" in audit["guidance_expansion_error"]


def test_expander_off_never_calls_runner(tmp_path: Path):
    runner = _FakeRunner({"status": "EXPANDED", "directives": []})
    config = VisualGuidanceExpanderConfig(
        enabled=False,
        mode="OFF",
        provider="test-provider",
        model="small-test-model",
        cache_path=tmp_path / "guidance-cache.json",
    )

    audit = LightweightVisualGuidanceExpander(config, runner).expand("白色背景")

    assert audit["guidance_expansion_status"] == "SKIPPED"
    assert audit["enhanced_directive"] == ""
    assert runner.calls == []


def test_grid_prompt_keeps_user_text_before_validated_expanded_guidance():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "s1",
            "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒",
            "visual_guidance": {
                "source": "explicit_user",
                "user_prompt": "每格都是微缩场景，白色背景",
                "guidance_expansion_status": "EXPANDED",
                "guidance_expansion_directives": [
                    {
                        "source_phrase": "微缩场景",
                        "instruction": "每格必须呈现为独立闭合的微缩模型舞台。",
                    },
                ],
            },
        },
    ], grid_layout="2x2")

    lines = plan["batches"][0]["prompt"].splitlines()
    assert lines[1] == "每格都是微缩场景，白色背景"
    assert lines[2] == "每格必须呈现为独立闭合的微缩模型舞台。"


def test_grid_prompt_rejects_untraceable_or_unreviewed_expanded_text():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "s1",
            "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒",
            "visual_guidance": {
                "source": "explicit_user",
                "user_prompt": "白色背景",
                "guidance_expansion_status": "EXPANDED",
                "enhanced_directive": "不可信字段",
                "guidance_expansion_directives": [
                    {"source_phrase": "不存在的原文", "instruction": "不可信约束"},
                ],
            },
        },
    ], grid_layout="2x2")

    prompt = plan["batches"][0]["prompt"]
    assert "不可信字段" not in prompt
    assert "不可信约束" not in prompt


def test_console_helper_uses_injected_small_model_runner_without_network(tmp_path: Path):
    import tools.video_production_console as console

    runner = _FakeRunner({
        "status": "EXPANDED",
        "directives": [{"source_phrase": "白色背景", "instruction": "宫格外部一律保持纯白背景。"}],
    })
    audit = console._run_first_frame_guidance_expander(
        "白色背景",
        grid_layout="3x3",
        aspect_ratio="9:16",
        reference_image_count=4,
        config=_config(tmp_path),
        runner=runner,
    )

    assert audit["guidance_expansion_status"] == "EXPANDED"
    assert audit["enhanced_directive"] == "宫格外部一律保持纯白背景。"
    assert runner.calls[0]["input"]["context"] == {
        "grid_layout": "3x3", "aspect_ratio": "9:16", "reference_image_count": 4,
    }


def test_existing_adapter_passes_small_task_limits_to_run_text():
    received = {}

    class _RunTextTransport:
        def run_text(self, **kwargs):
            received.update(kwargs)
            return '{"status":"PASS","directives":[]}'

    result = ExistingModelGuidanceExpanderAdapter(_RunTextTransport()).expand({
        "system_prompt": "系统提示",
        "input": {"original_user_guidance": "白色背景"},
        "config": {
            "model": "doubao-seed-2.0-mini",
            "temperature": 0.0,
            "max_tokens": 400,
            "timeout": 20,
        },
    })

    assert result == '{"status":"PASS","directives":[]}'
    assert received["model"] == "doubao-seed-2.0-mini"
    assert received["temperature"] == 0.0
    assert received["max_tokens"] == 400
    assert received["timeout"] == 20.0


def test_api_management_runtime_config_enables_ark_small_model(monkeypatch):
    import tools.video_production_console as console

    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    for name in (
        "FIRST_FRAME_GUIDANCE_EXPANDER_ENABLED",
        "FIRST_FRAME_GUIDANCE_EXPANDER_PROVIDER",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MODEL",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MODEL_VERSION",
        "FIRST_FRAME_GUIDANCE_EXPANDER_TEMPERATURE",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MAX_TOKENS",
        "FIRST_FRAME_GUIDANCE_EXPANDER_TIMEOUT",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    console._api_management_apply_runtime_config({
        "groups": {},
        "first_frame_guidance_expander": {
            "enabled": True,
            "provider": "ark-story-writing",
            "model": "ep-20260823144829-knnfs",
            "model_version": "Doubao-Seed-2.0-mini",
            "temperature": 0.0,
            "max_tokens": 400,
            "timeout": 20,
            "mode": "ON",
        },
    })

    loaded = VisualGuidanceExpanderConfig.from_env()
    assert loaded.enabled is True
    assert loaded.mode == "ON"
    assert loaded.provider == "ark-story-writing"
    assert loaded.model == "ep-20260823144829-knnfs"
    assert loaded.model_version == "Doubao-Seed-2.0-mini"
    assert loaded.temperature == 0.0
    assert loaded.max_tokens == 400
    assert loaded.timeout == 20.0


def test_api_management_loader_preserves_guidance_expander_runtime_settings(monkeypatch, tmp_path: Path):
    import json
    import tools.video_production_console as console

    config_path = tmp_path / "api_management.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "groups": {},
        "first_frame_guidance_expander": {
            "enabled": True,
            "provider": "ark-story-writing",
            "model": "ep-20260823144829-knnfs",
            "model_version": "Doubao-Seed-2.0-mini",
            "temperature": 0.0,
            "max_tokens": 400,
            "timeout": 20,
            "mode": "ON",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(console, "API_MANAGEMENT_CONFIG_PATH", config_path)
    monkeypatch.setattr(console, "_api_management_apply_secret_runtime", lambda: None)
    for name in (
        "FIRST_FRAME_GUIDANCE_EXPANDER_ENABLED",
        "FIRST_FRAME_GUIDANCE_EXPANDER_PROVIDER",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MODEL",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MODEL_VERSION",
        "FIRST_FRAME_GUIDANCE_EXPANDER_TEMPERATURE",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MAX_TOKENS",
        "FIRST_FRAME_GUIDANCE_EXPANDER_TIMEOUT",
        "FIRST_FRAME_GUIDANCE_EXPANDER_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    console._api_management_apply_runtime_config()
    loaded = VisualGuidanceExpanderConfig.from_env()

    assert loaded.enabled is True
    assert loaded.mode == "ON"
    assert loaded.provider == "ark-story-writing"
    assert loaded.model == "ep-20260823144829-knnfs"
