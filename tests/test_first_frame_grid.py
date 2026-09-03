from __future__ import annotations

from pathlib import Path

import pytest

from workflow_1256.end_frame_extension import run_end_frame_extension
from workflow_1256.first_frame_grid import (
    FirstFrameGridBatchError,
    FirstFrameGridCropError,
    FirstFrameGridValidationError,
    build_first_frame_grid_plan,
    crop_equal_grid_image,
    run_first_frame_grid_batch,
)


def test_grid_layout_validator_rejects_wrong_grid_canvas(tmp_path):
    from PIL import Image
    from workflow_1256.first_frame_grid import _validate_grid_image_layout

    wrong_grid = tmp_path / "wrong.png"
    Image.new("RGB", (900, 2400), "black").save(wrong_grid)
    batch = {"grid_id": "grid_001", "grid_layout": "2x2", "layout": {"total_width": 900, "total_height": 1600}}
    with pytest.raises(FirstFrameGridBatchError, match="宫格不符"):
        _validate_grid_image_layout(wrong_grid, batch)


def test_grid_batch_passes_creation_provider_context_to_query(tmp_path):
    from PIL import Image

    source = tmp_path / "grid.png"
    Image.new("RGB", (200, 200), "black").save(source)
    queried: list[dict] = []

    def create(_request):
        return {
            "task_id": "provider-task-1",
            "channel_id": "image2-backup",
            "key_index": 2,
            "endpoint": "https://image2-backup.example/v1/",
            "model_id": "gpt-image-2-all",
        }

    def query(request):
        queried.append(dict(request))
        return {"task_id": request["task_id"], "status": "completed", "image_url": str(source)}

    result = run_first_frame_grid_batch(
        [{"shot_id": "s1", "prompt": "画面"}],
        create_runner=create,
        query_runner=query,
        materialize_runner=lambda image_url, _batch, _output_dir: Path(image_url),
        output_dir=tmp_path / "out",
        grid_layout="2x2",
        aspect_ratio="1:1",
        max_workers=1,
    )

    assert result["status"] == "completed"
    assert queried == [{
        "task_id": "provider-task-1",
        "channel_id": "image2-backup",
        "key_index": 2,
        "endpoint": "https://image2-backup.example/v1/",
        "model_id": "gpt-image-2-all",
    }]


def test_grid_batch_passes_checkpoint_provider_context_to_query(tmp_path):
    from PIL import Image
    import json

    source = tmp_path / "grid.png"
    Image.new("RGB", (200, 200), "black").save(source)
    checkpoint = tmp_path / "first_frame_checkpoint.json"
    checkpoint.write_text(json.dumps({
        "version": 1,
        "batches": {
            "first_frame_grid_001": {
                "task_id": "provider-task-2",
                "status": "submitted",
                "channel_id": "image2-backup",
                "key_index": 2,
                "endpoint": "https://image2-backup.example/v1/",
                "model_id": "gpt-image-2-all",
            },
        },
    }), encoding="utf-8")
    queried: list[dict] = []

    def query(request):
        queried.append(dict(request))
        return {"task_id": request["task_id"], "status": "completed", "image_url": str(source)}

    result = run_first_frame_grid_batch(
        [{"shot_id": "s1", "prompt": "画面"}],
        create_runner=lambda _request: pytest.fail("恢复断点时不得创建新任务"),
        query_runner=query,
        materialize_runner=lambda image_url, _batch, _output_dir: Path(image_url),
        output_dir=tmp_path / "out",
        grid_layout="2x2",
        aspect_ratio="1:1",
        checkpoint_path=checkpoint,
        resume=True,
        max_workers=1,
    )

    assert result["status"] == "completed"
    assert queried == [{
        "task_id": "provider-task-2",
        "channel_id": "image2-backup",
        "key_index": 2,
        "endpoint": "https://image2-backup.example/v1/",
        "model_id": "gpt-image-2-all",
    }]
from workflow_1256.prompt_generation import run_prompt_generation


def test_grid_plan_keeps_original_indices_and_skips_digital_human():
    shots = [
        {"shot_id": "s1", "prompt": "办公室清晨"},
        {"shot_id": "s2", "prompt": "数字人播报", "selected_route": "host"},
        {"shot_id": "s3", "prompt": "地铁人流"},
        {"shot_id": "s4", "prompt": "电脑屏幕"},
        {"shot_id": "s5", "prompt": "夜色街道"},
    ]

    result = build_first_frame_grid_plan(
        shots,
        cells_per_grid=4,
        cell_width=100,
        cell_height=160,
    )

    assert result["active_shot_indices"] == [0, 2, 3, 4]
    assert result["skipped_digital_human_shot_indices"] == [1]
    assert result["separation_px"] == 0
    assert len(result["batches"]) == 1
    batch = result["batches"][0]
    assert batch["layout"] == {
        "rows": 2,
        "columns": 2,
        "cell_width": 100,
        "cell_height": 160,
        "separation_px": 0,
        "total_width": 200,
        "total_height": 320,
    }
    assert batch["active_shot_indices"] == [0, 2, 3, 4]
    assert all(len(cell["box"]) == 4 for cell in batch["cells"])
    assert "零像素间隙" in batch["prompt"]
    assert "分割线" in batch["prompt"]


def test_user_can_select_fixed_2x2_or_3x3_layout():
    shots = [{"shot_id": f"s{i}", "prompt": f"镜头{i}"} for i in range(9)]

    plan_2x2 = build_first_frame_grid_plan(shots[:5], grid_layout="2x2")
    assert plan_2x2["grid_layout"] == "2x2"
    assert plan_2x2["cells_per_grid"] == 4
    assert [batch["grid_layout"] for batch in plan_2x2["batches"]] == ["2x2", "2x2"]
    assert [batch["layout"]["rows"] for batch in plan_2x2["batches"]] == [2, 2]
    assert [batch["layout"]["columns"] for batch in plan_2x2["batches"]] == [2, 2]
    assert "9:16" in plan_2x2["batches"][0]["prompt"]
    assert plan_2x2["aspect_ratio"] == "9:16"
    assert "深蓝抽象纹理占位画面" in plan_2x2["batches"][1]["prompt"]

    plan_3x3 = build_first_frame_grid_plan(shots, grid_layout="3x3")
    assert plan_3x3["grid_layout"] == "3x3"
    assert plan_3x3["cells_per_grid"] == 9
    assert len(plan_3x3["batches"]) == 1
    assert plan_3x3["batches"][0]["layout"]["rows"] == 3
    assert plan_3x3["batches"][0]["layout"]["columns"] == 3
    assert plan_3x3["batches"][0]["unused_cell_count"] == 0

    with pytest.raises(FirstFrameGridValidationError, match="2x2 或 3x3"):
        build_first_frame_grid_plan(shots, grid_layout="4x4")


def test_grid_batch_deduplicates_shared_reference_images_in_first_seen_order():
    shared = ["https://example.test/style-a.png", "https://example.test/style-b.png"]
    shots = [
        {"shot_id": f"s{i}", "prompt": f"镜头{i}", "ref_images": shared}
        for i in range(9)
    ]

    plan = build_first_frame_grid_plan(shots, grid_layout="3x3")

    assert plan["batches"][0]["reference_images"] == shared


def test_grid_prompt_preserves_distinct_per_cell_direction_and_reference_boundary():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "arrival",
            "prompt": "【场景】修车铺门口【主体与动作】阿豆推着掉链子的自行车进入店门【镜头语言】远景，高机位，斜向纵深【光线与氛围】午后侧光",
            "semantic_anchor": "即时麻烦",
            "motion_seed": "推车前行，链条轻晃",
            "camera_motion": [{"camera_motion": "缓慢后移", "action": "让出店门空间"}],
            "duration_s": 3.5,
            "visual_guidance": {"status": "blocked", "user_prompt": "保留人物周边的白色撕纸描边"},
        },
        {
            "shot_id": "leave",
            "prompt": "【场景】修车铺内【主体与动作】林夏举起的手停在半空，阿豆骑车离开【镜头语言】近景，越肩，边缘压力【光线与氛围】冷暖对比侧光",
            "semantic_anchor": "解释被打断",
            "motion_seed": "阿豆加速离开，林夏停住",
            "camera_motion": [{"camera_motion": "轻微摇镜", "action": "跟随阿豆离开"}],
            "duration_s": 4.2,
        },
    ], grid_layout="2x2")

    batch = plan["batches"][0]
    prompt = batch["prompt"]
    assert "严禁复制参考图中的具体人物数量" in prompt
    assert "相邻格必须使用不同的动作瞬间" in prompt
    assert "【左上】画面安排：修车铺门口" in prompt
    assert "【右上】画面安排：修车铺内" in prompt
    assert "阿豆推着掉链子的自行车进入店门" in prompt
    assert "林夏举起的手停在半空，阿豆骑车离开" in prompt
    assert "画面采用远景，以高机位视角拍摄，构图突出斜向纵深" in prompt
    assert "画面采用近景，以越肩视角拍摄，构图突出边缘压力" in prompt
    assert "缓慢后移" not in prompt
    assert "轻微摇镜" not in prompt
    assert "画面定格在主体与关键物件形成清晰关系的瞬间" in prompt
    assert "保留人物周边的白色撕纸描边" not in prompt
    assert "镜头编号" not in prompt
    assert "镜头时长" not in prompt
    assert "景别=" not in prompt


def test_grid_cells_keep_explicit_director_camera_fields_for_each_shot():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "g01_s01",
            "prompt": "【场景】办公室【主体与动作】人物抬头",
            "shot_size": "远景",
            "viewpoint": "高机位",
            "composition": "边缘压力切入",
            "staging": "人物从门口走到桌边",
            "duration_s": 2.8,
        },
    ], grid_layout="2x2")

    cell = plan["batches"][0]["cells"][0]
    assert cell["shot_size"] == "远景"
    assert cell["viewpoint"] == "高机位"
    assert cell["composition"] == "边缘压力切入"
    assert cell["staging"] == "人物从门口走到桌边"
    prompt = plan["batches"][0]["prompt"]
    assert "g01_s01" not in prompt
    assert "2.8秒" not in prompt
    assert "画面采用远景，以高机位视角拍摄，构图突出边缘压力切入" in prompt
    assert "人物从门口走到桌边" in prompt
    assert "景别=" not in prompt
    assert "构图=" not in prompt
    assert "调度=" not in prompt


def test_grid_prompt_strips_field_prefixes_from_cell_values():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "prefixed",
            "prompt": "【场景】室内【主体与动作】人物停下",
            "shot_size": "景别=近景",
            "viewpoint": "视角=平视",
            "composition": "构图=局部焦点",
            "staging": "调度=人物与物件保持静止",
        },
    ], grid_layout="2x2")

    prompt = plan["batches"][0]["prompt"]
    assert "画面采用近景，以平视视角拍摄，构图突出局部焦点" in prompt
    assert "景别=" not in prompt
    assert "视角=" not in prompt
    assert "构图=" not in prompt
    assert "调度=" not in prompt


def test_grid_prompt_does_not_inject_reference_torn_paper_outline_template():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "outlined",
            "prompt": "【场景】修车铺内【主体与动作】林夏举起扳手【镜头语言】中景，平视，主体明确",
            "ref_images": ["https://example.test/character-outline.png"],
        },
    ], grid_layout="2x2")

    prompt = plan["batches"][0]["prompt"]
    # 参考图只决定画风/角色连续性；撕纸描边不是默认模板约束，
    # 只有用户把它写入注意力引导时才应出现在 prompt。
    assert "白色撕纸描边" not in prompt
    assert "这里的白色描边不是宫格分割线" not in prompt
    assert "严禁分割线、白线、黑线" in prompt


def test_grid_prompt_uses_explicit_guidance_and_removes_transport_metadata():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "s1",
            "prompt": "【场景】（1920×1080）清晨菜场摊位前【主体与动作】陈岭把零钱放进铁盒",
            "visual_guidance": {
                "source": "explicit_user",
                "user_prompt": "纯白色背景，微缩场景画风",
                "positive_prompt": "模型自动扩写的画面风格",
                "negative_prompt": "模型自动追加的负面约束",
            },
        },
    ], grid_layout="2x2")

    prompt = plan["batches"][0]["prompt"]
    assert "纯白色背景，微缩场景画风" in prompt
    assert "注意力引导：" not in prompt
    assert "1920×1080" not in prompt
    assert "镜头编号" not in prompt
    assert "镜头时长" not in prompt
    assert "场景=" not in prompt


def test_grid_prompt_places_reference_style_first_and_attention_second():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "s1",
            "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒",
            "ref_images": ["https://example.test/style.png"],
            "visual_guidance": {
                "source": "explicit_user",
                "user_prompt": "纯白色背景，微缩场景画风",
            },
        },
    ], grid_layout="2x2")

    lines = plan["batches"][0]["prompt"].splitlines()
    assert lines[0] == "根据参考图的画风、笔触、色调与角色基础连续性，生成一张2行2列的无缝多宫格首帧图。"
    assert lines[1] == "纯白色背景，微缩场景画风"
    assert "整组画面统一保持以下视觉风格" not in plan["batches"][0]["prompt"]
    assert "模型自动扩写的画面风格" not in plan["batches"][0]["prompt"]
    assert "模型自动追加的负面约束" not in plan["batches"][0]["prompt"]


def test_explicit_miniature_guidance_is_strengthened_globally_and_per_cell():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "s1",
            "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒",
            "visual_guidance": {
                "source": "explicit_user",
                "user_prompt": "纯白色背景，微缩场景画风",
            },
        },
        {
            "shot_id": "s2",
            "prompt": "【场景】居民楼路边【主体与动作】陈岭扶住父亲",
            "visual_guidance": {
                "source": "explicit_user",
                "user_prompt": "纯白色背景，微缩场景画风",
            },
        },
    ], grid_layout="2x2")

    prompt = plan["batches"][0]["prompt"]
    lines = prompt.splitlines()
    assert lines[1] == "纯白色背景，微缩场景画风"
    assert "必须先分别构思每一格，再排成九宫格" in lines[2]
    assert "不得把九格当作一张连续大场景后再裁切" in lines[2]
    assert "每格画布的外部背景必须保持连续纯白" in lines[2]
    assert "不得将任何一格绘制成真人尺度的电影实景" in lines[2]
    assert prompt.count("这一格必须是独立、完整的微缩模型小舞台") == 2
    assert prompt.count("即使本镜是近景或特写，也必须表现为该微缩舞台中被放大的局部") == 2
    assert prompt.count("本格外部必须是纯白背景") == 2
    assert "相邻格的叙事空间不得相互延伸" in prompt


def test_miniature_strengthening_does_not_apply_without_explicit_guidance():
    prompt = build_first_frame_grid_plan([
        {"shot_id": "s1", "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒"},
    ], grid_layout="2x2")["batches"][0]["prompt"]

    assert "必须先分别构思每一格，再排成九宫格" not in prompt
    assert "这一格必须是独立、完整的微缩模型小舞台" not in prompt


def test_grid_prompt_does_not_apply_unmarked_guidance():
    plan = build_first_frame_grid_plan([
        {
            "shot_id": "s1",
            "prompt": "【场景】菜场【主体与动作】陈岭整理铁盒",
            "visual_guidance": {"user_prompt": "未标记来源的旧引导"},
        },
    ], grid_layout="2x2")

    assert "未标记来源的旧引导" not in plan["batches"][0]["prompt"]


def test_3x3_selection_downgrades_short_trailing_batch_to_2x2():
    shots = [{"shot_id": f"s{i}", "prompt": f"镜头{i}"} for i in range(12)]

    plan = build_first_frame_grid_plan(shots, grid_layout="3x3")

    assert plan["grid_layout"] == "3x3"
    assert plan["requested_grid_layout"] == "3x3"
    assert plan["layout_policy"] == "max_capacity_auto_downgrade"
    assert [batch["grid_layout"] for batch in plan["batches"]] == ["3x3", "2x2"]
    assert plan["batches"][0]["unused_cell_count"] == 0
    assert plan["batches"][1]["unused_cell_count"] == 1
    assert len(plan["batches"][1]["cells"]) == 3
    assert plan["batches"][1]["layout"]["rows"] == 2
    assert plan["batches"][1]["layout"]["columns"] == 2


def test_3x3_selection_keeps_five_to_eight_shots_as_3x3():
    shots = [{"shot_id": f"s{i}", "prompt": f"镜头{i}"} for i in range(17)]

    plan = build_first_frame_grid_plan(shots, grid_layout="3x3")

    assert [batch["grid_layout"] for batch in plan["batches"]] == ["3x3", "3x3"]
    assert plan["batches"][0]["unused_cell_count"] == 0
    assert plan["batches"][1]["unused_cell_count"] == 1
    assert len(plan["batches"][1]["cells"]) == 8
    assert plan["batches"][1]["layout"]["rows"] == 3
    assert plan["batches"][1]["layout"]["columns"] == 3


def test_tail_placeholder_cells_are_explicit_and_all_real_shots_have_first_frames():
    plan = build_first_frame_grid_plan(
        [{"shot_id": "s1", "prompt": "镜头1"}],
        grid_layout="3x3",
    )

    batch = plan["batches"][0]
    assert batch["grid_layout"] == "2x2"
    assert batch["generation_mode"] == "fixed_grid"
    assert batch["real_shot_count"] == 1
    assert batch["grid_capacity"] == 4
    assert batch["placeholder_cell_indices"] == [1, 2, 3]
    assert batch["discarded_placeholder_cell_indices"] == [1, 2, 3]
    assert batch["placeholder_policy"] == "background_only_discard_after_crop"
    assert "剩余3个未使用宫格" in batch["prompt"]
    assert plan["placeholder_cell_count"] == 3
    assert plan["discarded_placeholder_cell_count"] == 3
    assert plan["first_frame_coverage"] == {
        "ordinary_shot_count": 1,
        "planned_first_frame_count": 1,
        "complete": True,
    }


def test_three_by_three_tail_keeps_five_to_eight_shots_as_3x3():
    plan = build_first_frame_grid_plan(
        [{"shot_id": f"s{i}", "prompt": f"镜头{i}"} for i in range(6)],
        grid_layout="3x3",
    )

    assert [batch["grid_layout"] for batch in plan["batches"]] == ["3x3"]
    assert [len(batch["cells"]) for batch in plan["batches"]] == [6]
    assert plan["batches"][0]["placeholder_cell_indices"] == [6, 7, 8]
    assert plan["first_frame_coverage"]["complete"] is True


def test_director_aspect_ratio_controls_grid_cell_and_request_canvas(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    grid_path = tmp_path / "landscape-grid.png"
    image.new("RGB", (256, 144), (10, 20, 30)).save(grid_path)
    create_calls = []

    def create_runner(request):
        create_calls.append(request)
        return {"task_id": "landscape-grid-task"}

    def query_runner(_request):
        return {"status": "completed", "image_url": "landscape-grid"}

    def materialize_runner(_image_url, _batch, _output_dir):
        return grid_path

    result = run_first_frame_grid_batch(
        [{"shot_id": "s1", "prompt": "城市天际线"}],
        aspect_ratio="16:9",
        grid_layout="2x2",
        create_runner=create_runner,
        query_runner=query_runner,
        materialize_runner=materialize_runner,
        output_dir=tmp_path / "out",
    )

    assert create_calls[0]["size"] == "1280x720"
    assert create_calls[0]["grid_config"]["aspect_ratio"] == "16:9"
    assert create_calls[0]["grid_config"]["cell_aspect_ratio"] == "16:9"
    assert create_calls[0]["grid_config"]["canvas_aspect_ratio"] == "16:9"
    assert create_calls[0]["grid_config"]["cell_width"] == 1280
    assert create_calls[0]["grid_config"]["cell_height"] == 720
    assert result["grid_config"]["aspect_ratio"] == "16:9"
    assert result["grid_batches"][0]["aspect_ratio"] == "16:9"

    three_by_three = build_first_frame_grid_plan(
        [{"shot_id": "s1", "prompt": "城市天际线"}],
        aspect_ratio="16:9",
        grid_layout="3x3",
    )
    assert three_by_three["batches"][0]["layout"]["cell_width"] == 1280
    assert three_by_three["batches"][0]["layout"]["cell_height"] == 720
    assert "16:9" in three_by_three["batches"][0]["prompt"]


@pytest.mark.parametrize(
    ("aspect_ratio", "image_size", "expected_dimensions"),
    [
        ("3:2", (300, 200), (1536, 1024)),
        ("2:3", (200, 300), (1024, 1536)),
    ],
)
def test_grid_supports_three_to_two_and_two_to_three_aspects(
    tmp_path: Path,
    aspect_ratio: str,
    image_size: tuple[int, int],
    expected_dimensions: tuple[int, int],
):
    image = pytest.importorskip("PIL.Image")
    grid_path = tmp_path / f"{aspect_ratio.replace(':', '-')}-grid.png"
    image.new("RGB", image_size, (10, 20, 30)).save(grid_path)
    create_calls = []

    def create_runner(request):
        create_calls.append(request)
        return {"task_id": "aspect-grid-task"}

    def query_runner(_request):
        return {"status": "completed", "image_url": "aspect-grid"}

    def materialize_runner(_image_url, _batch, _output_dir):
        return grid_path

    result = run_first_frame_grid_batch(
        [{"shot_id": "s1", "prompt": "城市天际线"}],
        aspect_ratio=aspect_ratio,
        grid_layout="2x2",
        create_runner=create_runner,
        query_runner=query_runner,
        materialize_runner=materialize_runner,
        output_dir=tmp_path / "out",
    )

    assert create_calls[0]["size"] == f"{expected_dimensions[0]}x{expected_dimensions[1]}"
    assert create_calls[0]["grid_config"]["aspect_ratio"] == aspect_ratio
    assert (
        create_calls[0]["grid_config"]["cell_width"],
        create_calls[0]["grid_config"]["cell_height"],
    ) == expected_dimensions
    assert result["grid_config"]["aspect_ratio"] == aspect_ratio


def test_grid_aspect_ratio_rejects_conflicting_legacy_dimensions():
    with pytest.raises(FirstFrameGridValidationError, match="冲突"):
        build_first_frame_grid_plan(
            [{"prompt": "镜头"}],
            aspect_ratio="16:9",
            cell_width=720,
            cell_height=1280,
        )


def test_crop_equal_grid_image_crops_exact_equal_cells(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "grid.png"
    canvas = image.new("RGB", (200, 200))
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    for cell_index, color in enumerate(colors):
        left = (cell_index % 2) * 100
        top = (cell_index // 2) * 100
        for x in range(left, left + 100):
            for y in range(top, top + 100):
                canvas.putpixel((x, y), color)
    canvas.save(source)

    plan = build_first_frame_grid_plan(
        [{"shot_id": f"s{i}", "prompt": f"镜头{i}"} for i in range(4)],
        cells_per_grid=4,
        cell_width=100,
        cell_height=100,
    )
    result = crop_equal_grid_image(
        source,
        tmp_path / "cropped",
        plan["batches"][0],
        crop_inset_px=0,
    )

    assert [item["shot_index"] for item in result] == [0, 1, 2, 3]
    assert {(item["width"], item["height"]) for item in result} == {(100, 100)}
    assert all(Path(item["path"]).is_file() for item in result)
    for item, expected_color in zip(result, colors):
        with image.open(item["path"]) as cropped:
            assert cropped.size == (100, 100)
            assert cropped.getpixel((50, 50)) == expected_color


def test_crop_removes_center_white_separator_and_keeps_equal_sizes(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "grid-with-separators.png"
    canvas = image.new("RGB", (200, 200), (0, 0, 0))
    colors = [(220, 40, 40), (40, 220, 40), (40, 40, 220), (220, 220, 40)]
    for cell_index, color in enumerate(colors):
        left = (cell_index % 2) * 100
        top = (cell_index // 2) * 100
        for x in range(left, left + 100):
            for y in range(top, top + 100):
                canvas.putpixel((x, y), color)
    for coordinate in (99, 100):
        for position in range(200):
            canvas.putpixel((coordinate, position), (255, 255, 255))
            canvas.putpixel((position, coordinate), (255, 255, 255))
    canvas.save(source)

    plan = build_first_frame_grid_plan(
        [{"shot_id": f"s{i}", "prompt": f"镜头{i}"} for i in range(4)],
        cell_width=100,
        cell_height=100,
    )
    result = crop_equal_grid_image(
        source,
        tmp_path / "cropped",
        plan["batches"][0],
        crop_inset_px=0,
    )

    assert {(item["width"], item["height"]) for item in result} == {(98, 98)}
    assert all(item["total_inset_px"] == 1 for item in result)
    for item in result:
        with image.open(item["path"]) as cropped:
            assert cropped.size == (98, 98)
            assert (255, 255, 255) not in cropped.getdata()


def test_crop_supports_three_pixel_uniform_inset(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "grid.png"
    image.new("RGB", (200, 200), (20, 30, 40)).save(source)
    plan = build_first_frame_grid_plan(
        [{"prompt": "镜头1"}, {"prompt": "镜头2"}],
        cell_width=100,
        cell_height=100,
        grid_layout="2x2",
    )

    result = crop_equal_grid_image(
        source,
        tmp_path / "cropped",
        plan["batches"][0],
        crop_inset_px=3,
        auto_trim_separators=False,
    )

    assert {(item["width"], item["height"]) for item in result} == {(94, 94)}
    assert all(item["total_inset_px"] == 3 for item in result)


def _legacy_test_crop_rejects_non_divisible_source(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "bad-grid.png"
    image.new("RGB", (201, 200)).save(source)
    plan = build_first_frame_grid_plan(
        [{"prompt": "镜头1"}, {"prompt": "镜头2"}],
        cells_per_grid=2,
        cell_width=100,
        cell_height=100,
    )

    with pytest.raises(FirstFrameGridCropError, match="无法被.*等分"):
        crop_equal_grid_image(source, tmp_path / "cropped", plan["batches"][0])


def test_crop_handles_non_divisible_source_by_centering_usable_grid(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    source = tmp_path / "odd-grid.png"
    image.new("RGB", (201, 200), (20, 30, 40)).save(source)
    plan = build_first_frame_grid_plan(
        [{"prompt": "镜头1"}, {"prompt": "镜头2"}],
        cells_per_grid=2,
        cell_width=100,
        cell_height=100,
        grid_layout="2x2",
    )

    result = crop_equal_grid_image(
        source,
        tmp_path / "cropped",
        plan["batches"][0],
        crop_inset_px=0,
        auto_trim_separators=False,
    )

    assert {(item["width"], item["height"]) for item in result} == {(100, 100)}
    assert {item["grid_offset_px"]["x"] for item in result} == {0}


def test_prompt_generation_skips_digital_human_and_breaks_tail_frame():
    result = run_prompt_generation(
        {
            "plan_out_list": [
                {
                    "plans": [
                        {
                            "shot_index": 0,
                            "duration": 4,
                            "stages": [{"time_range": "0-4s", "action": "推门"}],
                        },
                        {
                            "shot_index": 1,
                            "duration": 4,
                            "stages": [{"time_range": "0-4s", "action": "播报"}],
                        },
                        {
                            "shot_index": 2,
                            "duration": 4,
                            "stages": [{"time_range": "0-4s", "action": "奔跑"}],
                        },
                    ]
                }
            ],
            "ref_image": [["frame-1", "", "frame-3"]],
            "clip_duration": [[4, 4, 4]],
            "digital_human_shot_indices": [1],
            "shot_metadata": [{}, {"selected_route": "host"}, {}],
            "story_context": [
                {"semantic_mapping": "a"},
                {"semantic_mapping": "b"},
                {"semantic_mapping": "c"},
            ],
        }
    )

    assert len(result["video_prompt"]) == 2
    assert result["ref_image_f"] == ["frame-1", "frame-3"]
    assert result["ref_image_e"] == ["", ""]
    assert result["int_duration"] == [4, 4]
    assert result["skipped_digital_human_shot_indices"] == [1]
    assert "语义映射=a" in result["video_prompt"][0]
    assert "语义映射=c" in result["video_prompt"][1]


def test_end_frame_extension_allows_aligned_empty_digital_human_frame():
    result = run_end_frame_extension(
        {
            "items": [{"group": "g1"}],
            "LLM_list": [{"shots": [{}, {"selected_route": "host"}, {}]}],
            "Code_list": [{"clip_duration": [4, 4, 4]}],
            "motion_seed": ["a", "b", "c"],
            "image_url_list": ["frame-1", "", "frame-3"],
            "digital_human_shot_indices": [1],
            "shot_metadata": [{}, {"selected_route": "host"}, {}],
        }
    )

    assert result["error"] == ""
    assert result["ref_image_f"] == [{"ref_image_f": ["frame-1", "", "frame-3"]}]
    assert result["ref_image_e"] == [{"ref_image_e": ["", ""]}]
    assert result["skipped_digital_human_shot_indices"] == [1]


def test_end_frame_extension_accepts_compact_active_frame_array():
    result = run_end_frame_extension(
        {
            "items": [{"group": "g1"}],
            "LLM_list": [{"shots": [{}, {"selected_route": "host"}, {}]}],
            "Code_list": [{"clip_duration": [4, 4, 4]}],
            "motion_seed": ["a", "b", "c"],
            "image_url_list": ["frame-1", "frame-3"],
            "digital_human_shot_indices": [1],
        }
    )

    assert result["error"] == ""
    assert result["ref_image_f"] == [{"ref_image_f": ["frame-1", "", "frame-3"]}]
    assert result["skipped_digital_human_shot_indices"] == [1]


def test_grid_batch_executes_one_task_and_returns_aligned_cropped_paths(tmp_path: Path):
    image = pytest.importorskip("PIL.Image")
    grid_path = tmp_path / "remote-grid.png"
    image.new("RGB", (200, 356), (10, 20, 30)).save(grid_path)
    create_calls = []
    query_calls = []

    def create_runner(request):
        create_calls.append(request)
        return {"task_id": "grid-task-1", "status": "processing"}

    def query_runner(request):
        query_calls.append(request)
        return {"status": "completed", "image_url": "remote-grid"}

    def materialize_runner(image_url, _batch, _output_dir):
        assert image_url == "remote-grid"
        return grid_path

    result = run_first_frame_grid_batch(
        [
            {"shot_id": "s1", "prompt": "一"},
            {"shot_id": "s2", "prompt": "数字人", "selected_route": "host"},
            {"shot_id": "s3", "prompt": "三"},
        ],
        create_runner=create_runner,
        query_runner=query_runner,
        materialize_runner=materialize_runner,
        output_dir=tmp_path / "out",
        cell_width=720,
        cell_height=1280,
        cells_per_grid=4,
    )

    assert len(create_calls) == 1
    assert len(query_calls) == 1
    assert create_calls[0]["size"] == "720x1280"
    assert create_calls[0]["grid_config"]["separation_px"] == 0
    assert create_calls[0]["grid_config"]["cell_aspect_ratio"] == "9:16"
    assert create_calls[0]["grid_config"]["canvas_aspect_ratio"] == "9:16"
    assert create_calls[0]["grid_config"]["crop_inset_px"] == 3
    assert create_calls[0]["grid_layout"] == "2x2"
    assert len(result["image_url_list"]) == 3
    assert result["image_url_list"][1] == ""
    assert Path(result["image_url_list"][0]).is_file()
    assert Path(result["image_url_list"][2]).is_file()
    assert result["skipped_digital_human_shot_indices"] == [1]


def test_grid_batch_requires_explicit_external_runners():
    with pytest.raises(FirstFrameGridBatchError, match="未注入"):
        run_first_frame_grid_batch([{"prompt": "镜头"}])
