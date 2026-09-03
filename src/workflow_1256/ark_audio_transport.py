"""火山引擎豆包语音/音频生成 HTTP transport。

官方接口：``POST https://openspeech.bytedance.com/api/v3/tts/create``。
本模块只负责真实 HTTP 请求和响应映射，不把密钥写入代码、fixture 或日志。

官方文档说明：新版控制台使用 ``X-Api-Key``；旧版控制台使用
``X-Api-App-Id`` + ``X-Api-Access-Key``。两种方式都保留，优先使用新版单头。
TTS 的 ``speed_ratio`` 以百分比映射到官方 ``audio_config.speech_rate``，例如
1.1 -> 10；这个映射是本地适配层的明确规则，不修改 Coze 原字段。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .speech_synthesis import SpeechSynthesisRequest


DEFAULT_ARK_AUDIO_URL = "https://openspeech.bytedance.com/api/v3/tts/create"
DEFAULT_MODEL = "seed-audio-1.0-multilingual"
MAX_AUDIO_SECONDS = 120
SUCCESS_CODES = {0, 20_000_000}


class ArkAudioTransportError(RuntimeError):
    """火山音频请求失败、未配置或响应无法映射。"""


class ArkAudioConfigError(ArkAudioTransportError):
    """火山音频鉴权配置不完整。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]
AudioPublisher = Callable[[bytes, str, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class ArkAudioConfig:
    api_key: str = ""
    app_id: str = ""
    access_key: str = ""
    api_url: str = DEFAULT_ARK_AUDIO_URL
    tts_model: str = DEFAULT_MODEL
    bgm_model: str = DEFAULT_MODEL
    tts_speaker_id: str = ""
    sample_rate: int = 48_000
    timeout: float = 300.0

    @classmethod
    def from_env(cls) -> "ArkAudioConfig":
        api_key = (
            os.environ.get("ARK_AUDIO_API_KEY", "").strip()
            or os.environ.get("ARK_BGM_API_KEY", "").strip()
            or os.environ.get("ARK_TTS_API_KEY", "").strip()
            or os.environ.get("ARK_API_KEY", "").strip()
            or os.environ.get("VOLCENGINE_AUDIO_API_KEY", "").strip()
        )
        app_id = (
            os.environ.get("ARK_AUDIO_APP_ID", "").strip()
            or os.environ.get("ARK_BGM_APP_ID", "").strip()
            or os.environ.get("ARK_TTS_APP_ID", "").strip()
            or os.environ.get("VOLCENGINE_AUDIO_APP_ID", "").strip()
        )
        access_key = (
            os.environ.get("ARK_AUDIO_ACCESS_KEY", "").strip()
            or os.environ.get("ARK_BGM_ACCESS_KEY", "").strip()
            or os.environ.get("ARK_TTS_ACCESS_KEY", "").strip()
            or os.environ.get("VOLCENGINE_AUDIO_ACCESS_KEY", "").strip()
        )
        if not api_key and not (app_id and access_key):
            raise ArkAudioConfigError(
                "未配置 ARK_AUDIO_API_KEY，或旧版双鉴权的 "
                "ARK_AUDIO_APP_ID/ARK_AUDIO_ACCESS_KEY；不会调用公网接口"
            )
        return cls(
            api_key=api_key,
            app_id=app_id,
            access_key=access_key,
            api_url=os.environ.get("ARK_AUDIO_API_URL", DEFAULT_ARK_AUDIO_URL).strip(),
            tts_model=os.environ.get("ARK_TTS_MODEL", DEFAULT_MODEL).strip(),
            bgm_model=os.environ.get("ARK_BGM_MODEL", DEFAULT_MODEL).strip(),
            tts_speaker_id=os.environ.get("ARK_TTS_SPEAKER_ID", "").strip(),
            sample_rate=int(os.environ.get("ARK_AUDIO_SAMPLE_RATE", "48000")),
            timeout=float(os.environ.get("ARK_AUDIO_TIMEOUT", "300")),
        )


def _default_requester(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Accept": "application/json", **headers},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = int(response.status)
            response_headers = {
                str(key): str(value) for key, value in response.headers.items()
            }
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        response_headers = {
            str(key): str(value) for key, value in exc.headers.items()
        }
    except (URLError, TimeoutError, OSError) as exc:
        raise ArkAudioTransportError(
            f"火山音频网络请求失败：{type(exc).__name__}"
        ) from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArkAudioTransportError("火山音频响应不是合法 JSON") from exc
    if not isinstance(body, Mapping):
        raise ArkAudioTransportError("火山音频响应根节点必须是对象")
    return {
        "status_code": status,
        "headers": response_headers,
        "body": dict(body),
    }


def _response_parts(response: Any) -> tuple[int, Mapping[str, Any], Mapping[str, str]]:
    if not isinstance(response, Mapping):
        raise ArkAudioTransportError("火山音频 transport 响应必须是对象")
    if isinstance(response.get("body"), Mapping):
        status = int(response.get("status_code", 200))
        return status, response["body"], response.get("headers", {})
    return 200, response, {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return _text(value)
    return ""


def _numeric(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArkAudioTransportError(f"火山音频响应缺少数字字段：{field}")
    return float(value)


def _extract_audio(body: Mapping[str, Any]) -> tuple[int, str, float, str]:
    code = body.get("code", 0)
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        raise ArkAudioTransportError("火山音频响应 code 必须是数字")
    message = _text(body.get("message"))
    url = _text(body.get("url"))
    duration_value = body.get("duration", body.get("original_duration"))
    if not url and body.get("audio"):
        duration = _numeric(duration_value, "duration/original_duration")
        if duration < 0:
            raise ArkAudioTransportError("鐏北闊抽 duration 涓嶈兘涓鸿礋鏁?")
        return int(code), "", duration, message
    if not url:
        # 官方接口也可能只返回 Base64 audio；当前工作流需要公网 URL，不能
        # 把 Base64 当成剪映可访问地址，也不在这里伪造上传结果。
        if body.get("audio"):
            raise ArkAudioTransportError(
                "火山音频只返回 Base64 audio，当前未配置安全的对象存储上传器"
            )
        raise ArkAudioTransportError("火山音频响应未返回可访问的 url")
    duration = _numeric(duration_value, "duration/original_duration")
    if duration < 0:
        raise ArkAudioTransportError("火山音频 duration 不能为负数")
    return int(code), url, duration, message


class ArkAudioHTTPTransport:
    """将官方火山音频接口映射到 TTS 与 BGM 两个工作流节点。"""

    def __init__(
        self,
        config: ArkAudioConfig,
        *,
        requester: Requester | None = None,
        audio_publisher: AudioPublisher | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not config.api_url.strip():
            raise ArkAudioConfigError("ARK_AUDIO_API_URL 不能为空")
        if not config.api_key and not (config.app_id and config.access_key):
            raise ArkAudioConfigError("火山音频鉴权配置不完整")
        if config.sample_rate not in {8000, 16000, 24000, 32000, 44100, 48000}:
            raise ArkAudioConfigError("ARK_AUDIO_SAMPLE_RATE 不是官方支持值")
        self.config = config
        self.requester = requester or _default_requester
        self.audio_publisher = audio_publisher
        self.request_id_factory = request_id_factory or (lambda: uuid.uuid4().hex)

    @classmethod
    def from_env(
        cls,
        *,
        audio_publisher: AudioPublisher | None = None,
    ) -> "ArkAudioHTTPTransport":
        return cls(ArkAudioConfig.from_env(), audio_publisher=audio_publisher)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-Request-Id": self.request_id_factory(),
        }
        if self.config.api_key:
            headers["X-Api-Key"] = self.config.api_key
        else:
            headers["X-Api-App-Id"] = self.config.app_id
            headers["X-Api-Access-Key"] = self.config.access_key
        return headers

    def _call(self, payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, str]]:
        response = self.requester(
            self.config.api_url,
            self._headers(),
            payload,
            self.config.timeout,
        )
        status, body, headers = _response_parts(response)
        if status >= 400:
            code = body.get("code")
            message = _text(body.get("message"))
            detail = f" code={code}" if code is not None else ""
            if message:
                detail += f" message={message[:200]}"
            raise ArkAudioTransportError(f"Ark audio HTTP {status}{detail}")
            raise ArkAudioTransportError(f"火山音频 HTTP {status}")
        code = body.get("code", 0)
        if (
            isinstance(code, (int, float))
            and not isinstance(code, bool)
            and int(code) not in SUCCESS_CODES
        ):
            message = _text(body.get("message")) or "火山音频接口返回失败"
            raise ArkAudioTransportError(f"火山音频 code={int(code)}：{message[:300]}")
        return body, headers

    def _audio_config(self, *, speech_rate: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "format": "mp3",
            "sample_rate": self.config.sample_rate,
            "pitch_rate": 0,
            "loudness_rate": 0,
        }
        if speech_rate is not None:
            result["speech_rate"] = speech_rate
        return result

    def synthesize(self, request: SpeechSynthesisRequest) -> dict[str, Any]:
        prompt_parts: list[str] = []
        if request.language:
            prompt_parts.append(f"使用{request.language}语音")
        if request.emotion:
            prompt_parts.append(f"情绪为{request.emotion}")
        if request.emotion_scale is not None:
            prompt_parts.append(f"情绪强度为{request.emotion_scale}")
        if prompt_parts:
            text_prompt = "；".join(prompt_parts) + "。文本：" + request.text
        else:
            text_prompt = request.text
        speaker = self.config.tts_speaker_id or request.speaker_id or request.voice_id
        payload: dict[str, Any] = {
            "model": self.config.tts_model,
            "text_prompt": text_prompt,
            "references": [{"speaker": speaker}],
            "audio_config": self._audio_config(
                speech_rate=int(round((request.speed_ratio - 1.0) * 100))
            ),
            "watermark": {},
        }
        body, headers = self._call(payload)
        code, url, duration, message = _extract_audio(body)
        if not url:
            raise ArkAudioTransportError(
                "TTS 响应仅返回 Base64 audio；请使用 Chunked TTS transport"
            )
        return {
            "code": code,
            "data": {"duration": duration, "link": url},
            "log_id": _header_value(headers, "X-Tt-Logid") or _text(body.get("log_id")),
            "msg": message or "ok",
        }

    @staticmethod
    def _bgm_prompt(request: Mapping[str, Any]) -> str:
        duration = int(request["Duration"])
        text = _text(request.get("Text"))
        genre = ", ".join(str(item) for item in request.get("Genre", []))
        instrument = ", ".join(str(item) for item in request.get("Instrument", []))
        mood = ", ".join(str(item) for item in request.get("Mood", []))
        theme = _text(request.get("Theme"))
        parts = [
            f"生成一段约{duration}秒的纯音乐背景音乐",
            "不要人声，不要歌词，不要语音，不要突然停止",
        ]
        if genre:
            parts.append(f"曲风：{genre}")
        if instrument:
            parts.append(f"乐器：{instrument}")
        if mood:
            parts.append(f"情绪：{mood}")
        if theme:
            parts.append(f"主题：{theme}")
        if text:
            parts.append(f"编导要求：{text}")
        return "；".join(parts) + "。"

    def generate_bgm(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise ArkAudioTransportError("gen_bgm 输入必须是对象")
        duration = request.get("Duration")
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ArkAudioTransportError("gen_bgm.Duration 必须是整数秒")
        if not 1 <= duration <= MAX_AUDIO_SECONDS:
            raise ArkAudioTransportError("gen_bgm.Duration 必须在 1–120 秒内")
        for field in ("Text", "Genre", "Instrument", "Mood"):
            if field not in request:
                raise ArkAudioTransportError(f"gen_bgm 缺少字段：{field}")
        payload = {
            "model": self.config.bgm_model,
            "text_prompt": self._bgm_prompt(request),
            "audio_config": self._audio_config(),
            "watermark": {},
        }
        body, headers = self._call(payload)
        code, url, actual_duration, message = _extract_audio(body)
        if not url:
            encoded = body.get("audio")
            if not isinstance(encoded, str) or not encoded.strip():
                raise ArkAudioTransportError("BGM 响应未返回 url 或 Base64 audio")
            if self.audio_publisher is None:
                raise ArkAudioTransportError(
                    "BGM 响应仅返回 Base64 audio；请配置 audio_publisher"
                )
            try:
                audio = base64.b64decode(encoded, validate=False)
            except (ValueError, binascii.Error) as exc:
                raise ArkAudioTransportError("BGM audio 不是合法 Base64") from exc
            if not audio:
                raise ArkAudioTransportError("BGM Base64 audio 为空")
            published = self.audio_publisher(
                audio,
                f"bgm-{self.request_id_factory()}.mp3",
                {"duration": actual_duration, "response": dict(body)},
            )
            if isinstance(published, str):
                url = published.strip()
            elif isinstance(published, Mapping):
                url = _text(published.get("url") or published.get("link"))
                published_duration = published.get("duration")
                if isinstance(published_duration, (int, float)) and not isinstance(
                    published_duration, bool
                ):
                    actual_duration = float(published_duration)
            if not url.startswith(("http://", "https://")):
                raise ArkAudioTransportError(
                    "BGM audio_publisher 未返回可访问的 http(s) URL"
                )
        song_detail = {
            "AudioUrl": url,
            "Duration": actual_duration,
            "Genre": ", ".join(str(item) for item in request.get("Genre", [])),
            "Instrument": ", ".join(str(item) for item in request.get("Instrument", [])),
            "Mood": ", ".join(str(item) for item in request.get("Mood", [])),
            "Prompt": _text(request.get("Text")),
            "Theme": _text(request.get("Theme")),
            "Captions": "",
            "Lyrics": "",
        }
        return {
            "code": code,
            "data": {
                "SongDetail": song_detail,
                "Progress": 1.0,
                "Status": 1,
                "TaskID": "",
            },
            "log_id": _header_value(headers, "X-Tt-Logid") or _text(body.get("log_id")),
            "msg": message or "ok",
            "errorBody": {"errorCode": "", "errorMessage": ""},
            "isSuccess": True,
        }


    def generate_sound_effect(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """调用官方音频接口生成单条音效；不会改变 TTS/BGM 契约。"""
        if not isinstance(request, Mapping):
            raise ArkAudioTransportError("音效请求必须是对象")
        if not _text(request.get("text_prompt")):
            raise ArkAudioTransportError("音效请求缺少 text_prompt")
        body, headers = self._call(dict(request))
        code, url, duration, message = _extract_audio(body)
        if not url:
            raise ArkAudioTransportError("音效响应仅返回 Base64 audio；请配置安全的 audio_publisher")
        return {
            "status": "succeeded",
            "code": code,
            "message": message or "ok",
            "log_id": _header_value(headers, "X-Tt-Logid") or _text(body.get("log_id")),
            "audio_url": url,
            "duration_us": int(duration * 1_000_000),
            "provider": "seed_audio_1_0",
            "task_id": _text(body.get("task_id") or body.get("taskID")),
        }


__all__ = [
    "ArkAudioConfig",
    "ArkAudioConfigError",
    "ArkAudioHTTPTransport",
    "ArkAudioTransportError",
    "DEFAULT_ARK_AUDIO_URL",
]
