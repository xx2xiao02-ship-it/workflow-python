"""TTS 三级参数治理：用户音色、项目基线、LLM 分段演绎计划。"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any


class VoiceDirectorValidationError(ValueError):
    pass


VoiceDirectorTransport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def build_voice_director_prompt(segments: Sequence[str], profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": "语音导演",
        "rules": [
            "逐段覆盖输入文本且不得改写原文", "停顿仅放语义边界", "单次停顿 150-800ms",
            "语速 0.5-2.0，音调 -12 到 12，音量 -50 到 100，句尾静音 0-30000ms",
            "输出 JSON：segments[]，每项含 segment_id、emotion、emotion_scale、speed_ratio、loudness_rate、pitch、tail_silence_ms、pause_plan",
        ],
        "profile": dict(profile),
        "segments": [{"segment_id": f"g{index + 1:02d}", "text": text} for index, text in enumerate(segments)],
    }


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VoiceDirectorValidationError(f"{field} 必须是数字")
    number = float(value)
    if number < minimum or number > maximum:
        raise VoiceDirectorValidationError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return number


def normalize_voice_performance_plan(
    value: Mapping[str, Any], segments: Sequence[str], *, profile: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    raw = value.get("segments") if isinstance(value, Mapping) else None
    if not isinstance(raw, list) or len(raw) != len(segments):
        raise VoiceDirectorValidationError("语音导演 segments 必须与大配音分段一一对应")
    base = dict(profile or {})
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or item.get("segment_id") != f"g{index + 1:02d}":
            raise VoiceDirectorValidationError("语音导演 segment_id 必须按 g01、g02 顺序覆盖")
        pauses = item.get("pause_plan", [])
        if not isinstance(pauses, list):
            pauses = []
        clean_pauses: list[dict[str, Any]] = []
        for pause in pauses:
            if not isinstance(pause, Mapping) or not isinstance(pause.get("after"), str):
                continue
            if pause["after"] not in segments[index]:
                continue
            try:
                duration_ms = int(_number(pause.get("duration_ms"), "pause_plan.duration_ms", 150, 800))
            except VoiceDirectorValidationError:
                continue
            clean_pauses.append({"after": pause["after"], "duration_ms": duration_ms})
        emotion = item.get("emotion", base.get("emotion", "自然"))
        if not isinstance(emotion, str) or not emotion.strip():
            raise VoiceDirectorValidationError("emotion 必须是非空字符串")
        normalized.append({
            # LLM 只负责演绎参数，文案绝不信任模型回显；始终使用输入原文。
            "segment_id": f"g{index + 1:02d}", "text": segments[index], "emotion": emotion.strip(),
            "emotion_scale": _number(item.get("emotion_scale", base.get("emotion_scale", 0.5)), "emotion_scale", 0, 1),
            "speed_ratio": _number(item.get("speed_ratio", base.get("speed_ratio", 1.0)), "speed_ratio", 0.5, 2.0),
            "loudness_rate": int(_number(item.get("loudness_rate", base.get("loudness_rate", 0)), "loudness_rate", -50, 100)),
            "pitch": int(_number(item.get("pitch", base.get("pitch", 0)), "pitch", -12, 12)),
            "tail_silence_ms": int(_number(item.get("tail_silence_ms", base.get("tail_silence_ms", 300)), "tail_silence_ms", 0, 30000)),
            "pause_plan": clean_pauses,
            # 当前 HTTP Chunked adapter 已确认全局静音参数；精确 SSML 由专用 transport 启用前保持审计状态。
            "ssml_status": "planned_transport_pending" if clean_pauses else "not_needed",
        })
    return {"segments": normalized, "ssml_transport_status": "pending_official_contract_confirmation"}


def run_voice_director(segments: Sequence[str], profile: Mapping[str, Any], *, transport: VoiceDirectorTransport | None = None) -> dict[str, Any]:
    if transport is None:
        raise RuntimeError("未注入语音导演模型 transport")
    return normalize_voice_performance_plan(transport(build_voice_director_prompt(segments, profile)), segments, profile=profile)


__all__ = ["VoiceDirectorValidationError", "build_voice_director_prompt", "normalize_voice_performance_plan", "run_voice_director"]
