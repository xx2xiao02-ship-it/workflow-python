import json
import math
import re


async def main(args):
    params = getattr(args, "params", None)

    if params is None:
        params = getattr(args, "input", None)

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

    def clean_sentence(value):
        return clean_text(value).rstrip("。；;，, ")

    def to_int(value, default=None):
        try:
            return int(math.ceil(float(value)))
        except Exception:
            return default

    def normalize_duration(value, group_index, shot_index, errors):
        duration = to_int(value)
        if duration is None or duration <= 0:
            errors.append(f"第 {group_index + 1} 组第 {shot_index + 1} 条缺少有效时长")
            return 0
        if duration < 4:
            errors.append(f"第 {group_index + 1} 组第 {shot_index + 1} 条时长 {duration}s，已自动调整为 4s")
            return 4
        if duration > 12:
            errors.append(f"第 {group_index + 1} 组第 {shot_index + 1} 条时长 {duration}s，已自动调整为 12s")
            return 12
        return duration

    def is_plan_like(value):
        value = as_dict(value)
        return bool(set(value.keys()).intersection({"shot_index", "duration", "stages"}))

    def extract_plans(value, depth=0):
        if depth > 6:
            return []
        value = parse_value(value)
        if isinstance(value, list):
            if value and all(is_plan_like(item) for item in value):
                return value
            return []
        if not isinstance(value, dict):
            return []

        plans = as_list(value.get("plans"))
        if plans:
            return plans

        plan_out = as_dict(value.get("plan_out"))
        plans = as_list(plan_out.get("plans"))
        if plans:
            return plans

        for key in ["output", "result", "data", "content", "response", "message", "answer"]:
            if key in value:
                found = extract_plans(value.get(key), depth + 1)
                if found:
                    return found
        return []

    def get_plan_groups(value):
        value = parse_value(value)
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return []
        for key in ["plan_out_list", "LLM_list", "llm_list", "results", "output"]:
            groups = as_list(value.get(key))
            if groups:
                return groups
        if extract_plans(value):
            return [value]
        return []

    def get_image_url(value):
        value = parse_value(value)
        if isinstance(value, dict):
            return clean_text(
                value.get("url")
                or value.get("link")
                or value.get("image_url")
                or value.get("image")
                or value.get("src")
            )
        return clean_text(value)

    def get_group_images(value):
        value = parse_value(value)
        if isinstance(value, dict):
            value = value.get("ref_image", [])
        return [get_image_url(item) for item in as_list(value)]

    def get_group_durations(value):
        value = parse_value(value)
        if isinstance(value, dict):
            value = value.get("clip_duration", [])
        return as_list(value)

    def get_visual_lock(value):
        # 统一压缩视觉锁定，避免 LLM 输出的 visual_lock 过长、重复或和后续规则冲突。
        # 这里不再原样拼接上游长文本，而是使用稳定短约定。
        return (
            "保持首帧主体、服装、场景、构图、色彩、光影、材质与原画风一致，"
            "主体身份不漂移，不新增无关人物、道具、场景或剧情"
        )

    def normalize_stage(value):
        value = parse_value(value)
        if isinstance(value, str):
            return {
                "time_range": "",
                "action": clean_sentence(value),
                "camera_motion": "固定机位"
            }
        stage = as_dict(value)
        return {
            "time_range": clean_sentence(stage.get("time_range")),
            "action": clean_sentence(stage.get("action")),
            "camera_motion": clean_sentence(stage.get("camera_motion")) or "固定机位"
        }

    def parse_time_range(value):
        text = clean_text(value).lower()
        text = text.replace("秒", "").replace("s", "")
        text = text.replace("—", "-").replace("–", "-").replace("~", "-")
        numbers = re.findall(r"\d+(?:\.\d+)?", text)
        if len(numbers) < 2:
            return None
        try:
            start = float(numbers[0])
            end = float(numbers[1])
        except Exception:
            return None
        if end <= start:
            return None
        return start, end

    def format_time(value):
        value = round(float(value), 1)
        if value.is_integer():
            return f"{int(value)}.0"
        return f"{value:.1f}"

    def build_timeline(raw_stages, duration):
        stages = [
            normalize_stage(stage)
            for stage in raw_stages
            if normalize_stage(stage).get("action")
        ]

        # 兜底：无动作阶段时生成默认单阶段
        if not stages:
            stages = [{
                "time_range": "",
                "action": "主体保持首帧姿态和外观，在当前空间内完成自然低幅度的连续动态",
                "camera_motion": "固定机位"
            }]

        # 限制最多3阶段，匹配模型语义承载能力
        if len(stages) > 3:
            stages = stages[:3]

        # 计算各阶段时长占比
        raw_lengths = []
        for stage in stages:
            time_pair = parse_time_range(stage.get("time_range"))
            if time_pair:
                raw_lengths.append(time_pair[1] - time_pair[0])
            else:
                raw_lengths.append(0)

        # 无有效时间占比时自动分配
        if sum(raw_lengths) <= 0:
            if len(stages) == 1:
                raw_lengths = [1]
            elif len(stages) == 2:
                raw_lengths = [0.45, 0.55]
            else:
                raw_lengths = [0.25, 0.50, 0.25]

        total_weight = sum(raw_lengths)
        cursor = 0.0
        normalized = []

        for index, stage in enumerate(stages):
            # 最后一阶段强制对齐总时长，避免浮点误差
            if index == len(stages) - 1:
                end = float(duration)
            else:
                segment = duration * raw_lengths[index] / total_weight
                end = cursor + segment

            action = clean_sentence(stage.get("action"))

            # 非首阶段强制动作承接，保证语义连续渐变
            if index > 0 and action and not action.startswith(
                ("在前一", "延续", "保持前一", "随后", "接着", "在此基础上")
            ):
                action = "在前一阶段状态基础上，" + action

            normalized.append({
                "time_range": f"{format_time(cursor)}-{format_time(end)}秒",
                "action": action,
                "camera_motion": clean_sentence(stage.get("camera_motion")) or "固定机位"
            })
            cursor = end

        return normalized

    def is_fixed_motion(value):
        text = clean_text(value)
        moving_words = [
            "推近", "拉远", "横移", "摇镜", "跟随",
            "跟拍", "环绕", "旋转", "移动", "升", "降", "变焦"
        ]
        return not any(word in text for word in moving_words)

    def infer_camera_fixed(stages):
        motions = [
            clean_text(stage.get("camera_motion"))
            for stage in stages
            if clean_text(stage.get("camera_motion"))
        ]
        if not motions:
            return True
        return all(is_fixed_motion(motion) for motion in motions)

    def stage_to_prompt(stage):
        action = clean_sentence(stage.get("action"))
        motion = clean_sentence(stage.get("camera_motion"))
        motion_text = "镜头固定" if is_fixed_motion(motion) else f"镜头{motion}"
        return f"{stage['time_range']}：{action}；{motion_text}。"

    def build_video_prompt(visual_lock, stages, has_tail_frame):
        timeline_text = "".join(stage_to_prompt(stage) for stage in stages)

        if has_tail_frame:
            tail_rule = "结尾自然衔接下一帧状态，姿态、构图与光影过渡顺滑"
        else:
            tail_rule = "结尾自然收束，保持稳定停顿，不突兀"

        return (
            f"视觉锁定：{visual_lock}。"
            "镜头规则：以首帧为起点，按时序完成自然连续运动；除非动作明确要求，不主动切镜、转场或新增场景。"
            "文字控制：不额外添加字幕、标题、logo、水印、聊天气泡、弹窗或UI；"
            "首帧已有文字、屏幕或文件内容仅作为画面元素保留，避免生成新的可读信息。"
            f"时序动作：{timeline_text}"
            f"收束规则：{tail_rule}。"
        )

    # ========== 主逻辑入口 ==========
    raw_plan_groups = get_plan_groups(params.get("plan_out_list"))
    raw_ref_groups = as_list(params.get("ref_image"))
    raw_duration_groups = as_list(params.get("clip_duration"))

    video_prompt = []
    int_duration = []
    ref_image_f = []
    ref_image_e = []
    camera_fixed = []
    errors = []

    group_count = max(
        len(raw_plan_groups),
        len(raw_ref_groups),
        len(raw_duration_groups)
    )

    if group_count <= 0:
        return {
            "video_prompt": [],
            "int_duration": [],
            "ref_image_f": [],
            "ref_image_e": [],
            "camera_fixed": [],
            "error": "plan_out_list、ref_image、clip_duration 均为空或格式不正确"
        }

    for group_index in range(group_count):
        raw_plan_group = raw_plan_groups[group_index] if group_index < len(raw_plan_groups) else {}
        raw_ref_group = raw_ref_groups[group_index] if group_index < len(raw_ref_groups) else {}
        raw_duration_group = raw_duration_groups[group_index] if group_index < len(raw_duration_groups) else {}

        plans = extract_plans(raw_plan_group)
        group_images = get_group_images(raw_ref_group)
        fallback_durations = get_group_durations(raw_duration_group)
        visual_lock = get_visual_lock(raw_plan_group)

        if not plans:
            errors.append(f"第 {group_index + 1} 组未提取到有效 plans")
            continue
        if not group_images:
            errors.append(f"第 {group_index + 1} 组未提取到有效参考图")
            continue
        if len(plans) != len(group_images):
            errors.append(
                f"第 {group_index + 1} 组 plans 数量与参考图数量不一致："
                f"{len(plans)} / {len(group_images)}"
            )

        # 按 shot_index 排序，保证镜头顺序正确
        ordered_plans = []
        used_indexes = set()
        for position, raw_plan in enumerate(plans):
            plan = as_dict(raw_plan)
            shot_index = to_int(plan.get("shot_index"), position)
            if (
                shot_index is None
                or shot_index < 0
                or shot_index >= len(group_images)
                or shot_index in used_indexes
            ):
                shot_index = position
            used_indexes.add(shot_index)
            ordered_plans.append({"shot_index": shot_index, "plan": plan})
        ordered_plans.sort(key=lambda item: item["shot_index"])

        # 逐镜头生成提示词
        for item in ordered_plans:
            shot_index = item["shot_index"]
            plan = item["plan"]

            first_frame_url = group_images[shot_index] if shot_index < len(group_images) else ""
            last_frame_url = group_images[shot_index + 1] if shot_index + 1 < len(group_images) else ""

            if not first_frame_url:
                errors.append(f"第 {group_index + 1} 组第 {shot_index + 1} 条缺少首帧图")
                continue

            # 时长归一化
            # 关键修复：LLM 有时会在 plans 里返回 duration=0 / null / 空字符串。
            # 不能用 plan.get("duration", fallback)，因为 duration 字段一旦存在，哪怕是 0，
            # 也不会自动回退到外层 clip_duration，最终会导致该镜头被 continue 吞掉。
            # 正确优先级：有效 plan.duration > 有效 clip_duration[shot_index] > 兜底 4s。
            plan_duration = plan.get("duration", None)
            fallback_duration = fallback_durations[shot_index] if shot_index < len(fallback_durations) else None

            plan_duration_int = to_int(plan_duration)
            fallback_duration_int = to_int(fallback_duration)

            if plan_duration_int is not None and plan_duration_int > 0:
                duration_source = plan_duration
            elif fallback_duration_int is not None and fallback_duration_int > 0:
                duration_source = fallback_duration
                errors.append(
                    f"第 {group_index + 1} 组第 {shot_index + 1} 条 plan.duration 无效，"
                    f"已使用 clip_duration={fallback_duration}"
                )
            else:
                duration_source = 4
                errors.append(
                    f"第 {group_index + 1} 组第 {shot_index + 1} 条 plan.duration 与 clip_duration 均无效，"
                    "已兜底为 4s"
                )

            duration_value = normalize_duration(duration_source, group_index, shot_index, errors)
            if duration_value <= 0:
                # 保底：任何情况下都不因为时长异常静默丢镜头
                duration_value = 4
                errors.append(f"第 {group_index + 1} 组第 {shot_index + 1} 条时长异常，已强制兜底为 4s")

            # 动作阶段归一化
            normalized_stages = build_timeline(as_list(plan.get("stages")), duration_value)

            # 组装最终提示词
            prompt = build_video_prompt(
                visual_lock=visual_lock,
                stages=normalized_stages,
                has_tail_frame=bool(last_frame_url)
            )

            video_prompt.append(prompt)
            int_duration.append(duration_value)
            ref_image_f.append(first_frame_url)
            ref_image_e.append(last_frame_url)
            camera_fixed.append(infer_camera_fixed(normalized_stages))

    return {
        "video_prompt": video_prompt,
        "int_duration": int_duration,
        "ref_image_f": ref_image_f,
        "ref_image_e": ref_image_e,
        "camera_fixed": camera_fixed,
        "error": "；".join(errors)
    }

