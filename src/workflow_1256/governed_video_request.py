"""将导演治理、连续首帧和冻结镜头绑定为视频生成请求。

本层只读取锁定契约，运行期媒体 URL 不回写 DirectorLockedManifest。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class GovernedVideoRequestError(ValueError):
    pass


_VIDEO_CONTINUITY_GUARD = (
    "视频连续性最高优先级：整段必须从所给首帧的同一主角、同一灰色卫衣、同一深夜书桌"
    "与同一蓝色屏幕光自然发展；主角从头到尾清晰可见，不得变成空景。禁止地点切换、"
    "图书馆、书架、书本、纸张、印刷物、可读文字、UI、图表、品牌或水印。若表达信息"
    "洪流，只能让蓝色光、无字半透明几何片和粒子从电脑屏幕附近自然扩散，镜头始终留在"
    "原书桌空间；动作必须是从疲惫伏桌逐渐被蓝光惊醒并抬眼。"
)


def build_governed_video_request(
    *,
    director_lock: Mapping[str, Any],
    material_output: Mapping[str, Any],
    sequential_records: Sequence[Mapping[str, Any]],
    shot_id: str,
) -> dict[str, Any]:
    shots = director_lock.get("shots")
    prompts = material_output.get("prompt")
    if not isinstance(shots, list) or not isinstance(prompts, list) or len(shots) != len(prompts):
        raise GovernedVideoRequestError("锁定镜头与素材提示词必须一一对应")
    index = next((i for i, shot in enumerate(shots) if isinstance(shot, Mapping) and shot.get("shot_id") == shot_id), -1)
    if index < 0:
        raise GovernedVideoRequestError(f"不存在锁定镜头：{shot_id}")
    shot = shots[index]
    timeline = shot.get("timeline") if isinstance(shot.get("timeline"), Mapping) else {}
    start_us = timeline.get("start_us")
    end_us = timeline.get("end_us")
    if not isinstance(start_us, int) or not isinstance(end_us, int) or end_us <= start_us:
        raise GovernedVideoRequestError(f"{shot_id} 缺少有效微秒时间线")
    record = next((item for item in sequential_records if item.get("shot_id") == shot_id), None)
    image_url = str(record.get("image_url") or "") if isinstance(record, Mapping) else ""
    if not image_url.startswith(("http://", "https://")):
        raise GovernedVideoRequestError(f"{shot_id} 缺少连续首帧 URL")
    duration_us = end_us - start_us
    if duration_us < 3_000_000:
        raise GovernedVideoRequestError(f"{shot_id} 时长低于 3 秒，必须走静态图片，禁止生成 AIGC 视频")
    if duration_us > 5_000_000:
        raise GovernedVideoRequestError(f"{shot_id} 时长超过 5 秒，不符合当前 2～5 秒镜头窗口")
    duration = round(duration_us / 1_000_000)
    prompt = str(prompts[index] or "").strip()
    if "逐镜头导演约束" not in prompt:
        raise GovernedVideoRequestError(f"{shot_id} 的视频提示词未消费导演逐镜上下文")
    return {
        "shot_id": shot_id,
        "duration": duration,
        "prompt": prompt + "\n" + _VIDEO_CONTINUITY_GUARD,
        "first_frame_url": image_url,
        "last_frame_url": "",
        "generate_audio": False,
        "watermark": False,
        "ratio": "adaptive",
        "resolution": "720p",
        "timeline": {"start_us": start_us, "end_us": end_us},
        "lock_mutated": False,
    }
