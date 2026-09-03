import json


async def main(args):
    params = getattr(args, "params", None)

    if not isinstance(params, dict):
        if isinstance(args, dict):
            params = args.get("params", args)
        else:
            params = {}

    if isinstance(params.get("_input"), dict):
        params = params.get("_input")

    def parse_value(value, max_depth=5):
        current = value

        for _ in range(max_depth):
            if not isinstance(current, str):
                break

            text = current.strip()

            if not text:
                break

            try:
                parsed = json.loads(text)
            except Exception:
                break

            if parsed == current:
                break

            current = parsed

        return current

    def as_list(value):
        value = parse_value(value)
        return value if isinstance(value, list) else []

    def as_dict(value):
        value = parse_value(value)
        return value if isinstance(value, dict) else {}

    def clean_text(value):
        if value is None:
            return ""
        return str(value).strip()

    def to_duration(value):
        try:
            number = float(value)
        except Exception:
            return 0

        if number <= 0:
            return 0

        if number.is_integer():
            return int(number)

        return round(number, 1)

    def get_seed_text(value):
        value = parse_value(value)

        if isinstance(value, dict):
            return clean_text(
                value.get("motion_seed")
                or value.get("seed")
                or value.get("text")
                or value.get("content")
            )

        return clean_text(value)

    def get_image_url(value):
        value = parse_value(value)

        if isinstance(value, dict):
            return clean_text(
                value.get("image_url")
                or value.get("url")
                or value.get("link")
                or value.get("image")
                or value.get("src")
            )

        return clean_text(value)

    def get_llm_shot_count(value):
        group = as_dict(value)

        shots = as_list(group.get("shots"))
        if shots:
            return len(shots)

        return len(as_list(group.get("items")))

    def get_group_durations(value):
        value = parse_value(value)

        if isinstance(value, list):
            return [to_duration(item) for item in value]

        group = as_dict(value)

        for key in [
            "clip_duration",
            "clip_durations",
            "duration_list",
            "durations"
        ]:
            durations = as_list(group.get(key))

            if durations:
                return [to_duration(item) for item in durations]

        return []

    source_items = as_list(params.get("items"))
    llm_list = as_list(params.get("LLM_list"))
    code_list = as_list(params.get("Code_list"))
    flat_motion_seeds = as_list(params.get("motion_seed"))
    flat_image_urls = as_list(params.get("image_url_list"))

    errors = []

    if not source_items:
        errors.append("items 为空或格式不是数组。")

    if not code_list:
        errors.append("Code_list 为空或格式不是数组。")

    if not flat_motion_seeds:
        errors.append("motion_seed 为空或格式不是数组。")

    if not flat_image_urls:
        errors.append("image_url_list 为空或格式不是数组。")

    if source_items and llm_list and len(source_items) != len(llm_list):
        errors.append(
            f"items 与 LLM_list 组数不一致："
            f"{len(source_items)} / {len(llm_list)}。"
        )

    if source_items and code_list and len(source_items) != len(code_list):
        errors.append(
            f"items 与 Code_list 组数不一致："
            f"{len(source_items)} / {len(code_list)}。"
        )

    grouped_motion_seed = []
    grouped_clip_duration = []
    grouped_ref_image = []
    grouped_ref_image_f = []
    grouped_ref_image_e = []

    seed_cursor = 0
    image_cursor = 0

    for group_index, _ in enumerate(source_items):
        llm_group = (
            llm_list[group_index]
            if group_index < len(llm_list)
            else {}
        )

        code_group = (
            code_list[group_index]
            if group_index < len(code_list)
            else {}
        )

        llm_shot_count = get_llm_shot_count(llm_group)
        clip_duration = get_group_durations(code_group)
        duration_shot_count = len(clip_duration)

        if (
            llm_shot_count
            and duration_shot_count
            and llm_shot_count != duration_shot_count
        ):
            errors.append(
                f"第 {group_index + 1} 组 shots 数量与 clip_duration 数量不一致："
                f"{llm_shot_count} / {duration_shot_count}。"
            )

        shot_count = duration_shot_count or llm_shot_count

        if shot_count <= 0:
            errors.append(
                f"第 {group_index + 1} 组未识别到有效 shots 或 clip_duration。"
            )

            grouped_motion_seed.append({"motion_seed": []})
            grouped_clip_duration.append({"clip_duration": []})
            grouped_ref_image.append({"ref_image": []})
            grouped_ref_image_f.append({"ref_image_f": []})
            grouped_ref_image_e.append({"ref_image_e": []})
            continue

        group_seeds = flat_motion_seeds[
            seed_cursor: seed_cursor + shot_count
        ]
        seed_cursor += shot_count

        # 当前镜头组内 N 条分镜头，对应 N 张连续首帧图：
        # 镜头0：图1 → 图2
        # 镜头1：图2 → 图3
        # 最后一条：只有图N，无尾帧
        group_images = flat_image_urls[
            image_cursor: image_cursor + shot_count
        ]
        image_cursor += shot_count

        if len(group_seeds) != shot_count:
            errors.append(
                f"第 {group_index + 1} 组需要 {shot_count} 条 motion_seed，"
                f"实际取得 {len(group_seeds)} 条。"
            )

        if len(group_images) != shot_count:
            errors.append(
                f"第 {group_index + 1} 组需要 {shot_count} 张参考图，"
                f"实际取得 {len(group_images)} 张。"
            )

        # 多模态 LLM 一次接收的整组连续参考图。
        ref_image = [
            get_image_url(group_images[shot_index])
            if shot_index < len(group_images)
            else ""
            for shot_index in range(shot_count)
        ]

        # 每条分镜头自己的首帧，长度 N。
        ref_image_f = list(ref_image)

        # 前 N-1 条分镜头的尾帧，长度 N-1。
        # 第 i 条镜头的尾帧 = 第 i+1 条镜头的首帧。
        ref_image_e = [
            ref_image[shot_index + 1]
            for shot_index in range(max(shot_count - 1, 0))
            if shot_index + 1 < len(ref_image)
        ]

        for shot_index, image_url in enumerate(ref_image):
            if not image_url:
                errors.append(
                    f"第 {group_index + 1} 组第 {shot_index + 1} 条缺少参考图。"
                )

        if len(ref_image_e) != max(shot_count - 1, 0):
            errors.append(
                f"第 {group_index + 1} 组应有 "
                f"{max(shot_count - 1, 0)} 条尾帧，"
                f"实际取得 {len(ref_image_e)} 条。"
            )

        grouped_motion_seed.append(
            {
                "motion_seed": [
                    get_seed_text(group_seeds[shot_index])
                    if shot_index < len(group_seeds)
                    else ""
                    for shot_index in range(shot_count)
                ]
            }
        )

        grouped_clip_duration.append(
            {
                "clip_duration": clip_duration
            }
        )

        grouped_ref_image.append(
            {
                "ref_image": ref_image
            }
        )

        grouped_ref_image_f.append(
            {
                "ref_image_f": ref_image_f
            }
        )

        grouped_ref_image_e.append(
            {
                "ref_image_e": ref_image_e
            }
        )

    if seed_cursor < len(flat_motion_seeds):
        errors.append(
            f"存在 {len(flat_motion_seeds) - seed_cursor} 条未被使用的 motion_seed。"
        )

    if image_cursor < len(flat_image_urls):
        errors.append(
            f"存在 {len(flat_image_urls) - image_cursor} 张未被使用的 image_url。"
        )

    return {
        "items": source_items,
        "motion_seed": grouped_motion_seed,
        "clip_duration": grouped_clip_duration,
        "ref_image": grouped_ref_image,
        "ref_image_f": grouped_ref_image_f,
        "ref_image_e": grouped_ref_image_e,
        "error": "；".join(errors)
    }
