"""8364 内部节点 159953 ``speech_synthesis`` 的契约适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .voice_catalog import DEFAULT_TTS_SPEAKER_ID


class SpeechSynthesisValidationError(ValueError):
    """TTS 输入或响应不符合 8364 YAML 契约。"""


class SpeechSynthesisTransportRequired(RuntimeError):
    """未注入真实 TTS transport。"""


DEFAULT_SPEED_RATIO = 1.1
# 兼容 8364 旧字段名；实际默认值由唯一音色目录派生。
DEFAULT_VOICE_ID = DEFAULT_TTS_SPEAKER_ID


@dataclass(frozen=True)
class SpeechSynthesisRequest:
    text: str
    speed_ratio: float
    voice_id: str
    emotion: str | None = None
    emotion_scale: float | None = None
    language: str | None = None
    speaker_id: str | None = None
    # 后付费自定义音色在声音复刻接口使用固定 speaker_id；这里保留
    # 实际自定义代号，供 TTS 绑定与审计使用。
    custom_speaker_id: str | None = None
    loudness_rate: int = 0
    pitch: int = 0
    silence_duration: int = 0
    dialect: str | None = None
    resource_id: str | None = None
    model: str | None = None


class SpeechSynthesisTransport(Protocol):
    def __call__(self, request: SpeechSynthesisRequest) -> Mapping[str, Any]: ...


def _parse(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith("{"):
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpeechSynthesisValidationError("speech_synthesis 输入 JSON 无效") from exc


def _resolve(params: Any) -> Mapping[str, Any]:
    value = _parse(params)
    if not isinstance(value, Mapping):
        raise SpeechSynthesisValidationError("speech_synthesis 输入必须是对象")
    for key in ("params", "_input"):
        if isinstance(value.get(key), Mapping):
            value = value[key]
    return value


def build_request(params: Any) -> SpeechSynthesisRequest:
    value = _resolve(params)
    text = value.get("text")
    speed_ratio = value.get("speed_ratio")
    voice_id = value.get("voice_id")
    if not isinstance(text, str) or not text.strip():
        raise SpeechSynthesisValidationError("text 必须是非空字符串")
    if isinstance(speed_ratio, bool) or not isinstance(speed_ratio, (int, float)):
        raise SpeechSynthesisValidationError("speed_ratio 必须是数字")
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise SpeechSynthesisValidationError("voice_id 必须是非空字符串")
    emotion = value.get("emotion")
    if emotion is not None and not isinstance(emotion, str):
        raise SpeechSynthesisValidationError("emotion 必须是字符串")
    emotion_scale = value.get("emotion_scale")
    if emotion_scale is not None and (
        isinstance(emotion_scale, bool)
        or not isinstance(emotion_scale, (int, float))
    ):
        raise SpeechSynthesisValidationError("emotion_scale 必须是数字")
    language = value.get("language")
    if language is not None and not isinstance(language, str):
        raise SpeechSynthesisValidationError("language 必须是字符串")
    speaker_id = value.get("speaker_id")
    if speaker_id is not None and not isinstance(speaker_id, str):
        raise SpeechSynthesisValidationError("speaker_id 必须是字符串")
    custom_speaker_id = value.get("custom_speaker_id")
    if custom_speaker_id is not None and (
        not isinstance(custom_speaker_id, str) or not custom_speaker_id.strip()
    ):
        raise SpeechSynthesisValidationError("custom_speaker_id 必须是非空字符串")
    loudness_rate = value.get("loudness_rate", 0)
    pitch = value.get("pitch", 0)
    silence_duration = value.get("silence_duration", value.get("tail_silence_ms", 0))
    for field, raw, minimum, maximum in (("loudness_rate", loudness_rate, -50, 100), ("pitch", pitch, -12, 12), ("silence_duration", silence_duration, 0, 30000)):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw or raw < minimum or raw > maximum:
            raise SpeechSynthesisValidationError(f"{field} 超出支持范围")
    dialect = value.get("dialect")
    if dialect is not None and not isinstance(dialect, str):
        raise SpeechSynthesisValidationError("dialect 必须是字符串")
    resource_id = value.get("resource_id")
    if resource_id is not None and (not isinstance(resource_id, str) or not resource_id.strip()):
        raise SpeechSynthesisValidationError("resource_id 必须是非空字符串")
    model = value.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise SpeechSynthesisValidationError("model 必须是非空字符串")
    return SpeechSynthesisRequest(
        text=text.strip(),
        speed_ratio=float(speed_ratio),
        voice_id=voice_id.strip(),
        emotion=emotion.strip() if isinstance(emotion, str) and emotion.strip() else None,
        emotion_scale=float(emotion_scale) if emotion_scale is not None else None,
        language=language.strip() if isinstance(language, str) and language.strip() else None,
        speaker_id=speaker_id.strip() if isinstance(speaker_id, str) and speaker_id.strip() else None,
        custom_speaker_id=(
            custom_speaker_id.strip()
            if isinstance(custom_speaker_id, str) and custom_speaker_id.strip()
            else None
        ),
        loudness_rate=int(loudness_rate), pitch=int(pitch), silence_duration=int(silence_duration),
        dialect=dialect.strip() if isinstance(dialect, str) and dialect.strip() else None,
        resource_id=resource_id.strip() if isinstance(resource_id, str) and resource_id.strip() else None,
        model=model.strip() if isinstance(model, str) and model.strip() else None,
    )


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise SpeechSynthesisValidationError(f"{path} 必须是字符串")
    return value


def normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise SpeechSynthesisValidationError("speech_synthesis 响应必须是对象")
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise SpeechSynthesisValidationError("data 必须是对象")
    duration = data.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise SpeechSynthesisValidationError("data.duration 必须是数字秒")
    if duration < 0:
        raise SpeechSynthesisValidationError("data.duration 不能为负数")
    link = _string(data.get("link"), "data.link")
    if not link:
        raise SpeechSynthesisValidationError("data.link 不能为空")
    normalized: dict[str, Any] = {}
    if "code" in response:
        code = response.get("code")
        if isinstance(code, bool) or not isinstance(code, (int, float)):
            raise SpeechSynthesisValidationError("code 必须是数字")
        normalized["code"] = code
    normalized.update({
        "data": {"duration": float(duration), "link": link},
        "log_id": _string(response.get("log_id"), "log_id"),
        "msg": _string(response.get("msg"), "msg"),
    })
    return normalized


def run_speech_synthesis(
    params: Any,
    *,
    transport: SpeechSynthesisTransport
    | Callable[[SpeechSynthesisRequest], Mapping[str, Any]]
    | None = None,
) -> dict[str, Any]:
    request = build_request(params)
    if transport is None:
        raise SpeechSynthesisTransportRequired(
            "未配置 159953 speech_synthesis 真实 TTS transport"
        )
    return normalize_response(transport(request))


__all__ = [
    "DEFAULT_SPEED_RATIO",
    "DEFAULT_VOICE_ID",
    "SpeechSynthesisRequest",
    "SpeechSynthesisTransportRequired",
    "SpeechSynthesisValidationError",
    "build_request",
    "normalize_response",
    "run_speech_synthesis",
]
