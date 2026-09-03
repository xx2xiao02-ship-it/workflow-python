from workflow_1256.asset_generation_contracts import (
    MODEL_PROFILES,
    build_first_frame_request,
    build_video_request,
    compile_first_frame_prompt,
    normalize_aspect_ratio,
)


def test_first_frame_prompt_inherits_requested_aspect_ratio_not_legacy_text():
    shot = {
        "first_frame_prompt": "首帧画面：9:16竖版，白天的修车铺。硬性画面禁令：无文字。",
        "shot_content": "林夏举起扳手检查自行车",
    }

    prompt = compile_first_frame_prompt(shot, aspect_ratio="16:9")

    assert "【画幅】16:9 横版" in prompt
    assert "9:16竖版" not in prompt
    assert "【主体与动作】林夏举起扳手检查自行车" in prompt


def test_asset_requests_keep_locked_script_fields_provider_neutral():
    shot = {
        "shot_content": "林夏推着自行车穿过雨后的街道",
        "motion_seed": "镜头缓慢跟拍，人物向前走",
        "camera_motion": ["跟拍", "轻微推进"],
        "duration_s": 4.5,
        "first_frame_prompt": "首帧画面：雨后街道。",
        "first_frame_reference": {"urls": ["https://example.test/style.jpg"]},
    }

    image_request = build_first_frame_request(shot, aspect_ratio="9:16")
    video_request = build_video_request(shot)

    assert image_request["model"] == "image2"
    assert image_request["reference_urls"] == ["https://example.test/style.jpg"]
    assert video_request == {
        "model": "seedance_1_5_pro",
        "prompt": "林夏推着自行车穿过雨后的街道；镜头缓慢跟拍，人物向前走；跟拍、轻微推进",
        "duration_ms": 4500,
        "camera_fixed": False,
    }


def test_model_profiles_and_ratio_normalization_are_explicit():
    assert MODEL_PROFILES["image2"].supports_grid is True
    assert MODEL_PROFILES["seedance_1_5_pro"].supports_reference_images is True
    assert MODEL_PROFILES["seedance_1_0_pro"].supports_reference_images is True
    assert normalize_aspect_ratio("1:1") == "1:1"
    assert normalize_aspect_ratio("not-a-ratio") == "9:16"


def test_first_frame_prompt_does_not_apply_guidance_without_explicit_source():
    shot = {
        "first_frame_prompt": "首帧画面：菜场摊位前。硬性画面禁令：无文字。",
        "visual_guidance": {
            "status": "blocked",
            "user_prompt": "历史任务注意力引导",
            "positive_prompt": "微缩场景画风",
            "negative_prompt": "纯白背景",
        },
    }

    prompt = compile_first_frame_prompt(shot, aspect_ratio="16:9")

    assert "视觉大模型增强" not in prompt
    assert "视觉大模型负面约束" not in prompt


def test_first_frame_prompt_applies_guidance_when_explicit_user_source():
    shot = {
        "first_frame_prompt": "首帧画面：菜场摊位前。硬性画面禁令：无文字。",
        "visual_guidance": {
            "source": "explicit_user",
            "reference_usage": "保持角色连续",
            "positive_prompt": "微缩场景画风",
            "negative_prompt": "不要改变色调",
        },
    }

    prompt = compile_first_frame_prompt(shot, aspect_ratio="16:9")

    assert "【参考图增强约束】保持角色连续" in prompt
    assert "【视觉大模型增强】微缩场景画风" in prompt
    assert "【视觉大模型负面约束】不要改变色调" in prompt
