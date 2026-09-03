"""火山方舟官方 HTTP Chunked 单向流式 TTS transport。

官方接口只返回 Base64 音频分片；1256 的下游契约要求可访问的
``data.link``，因此必须注入 ``audio_publisher`` 才允许真实请求。
本模块不保存鉴权值，也不把 Base64 伪装成本地或公网 URL。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .speech_synthesis import SpeechSynthesisRequest
from .voice_catalog import CUSTOM_SPEAKER_PROTOCOL_ID, DEFAULT_TTS_SPEAKER_ID, normalize_custom_speaker_id


DEFAULT_ARK_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_TTS_RESOURCE_ID = "seed-tts-2.0"
# 该值是本项目目录里的兼容元数据别名，不是 V3 HTTP Chunked 的默认
# provider model。官方文档的默认调用不传 model；只有用户显式指定其它
# 版本（例如文档列出的 seed-tts-1.1）时才把 model 放进 req_params。
DEFAULT_TTS_MODEL = "seed-tts-2.0-standard"
_INTERNAL_DEFAULT_MODEL_ALIASES = frozenset({DEFAULT_TTS_MODEL})
DEFAULT_SAMPLE_RATE = 48_000
# 生产链路只保留火山官方 TTS 2.0；ICL 资源属于已停用的定制音色/声音
# 复刻链路，不能再被配置或请求带入。
SUPPORTED_RESOURCE_IDS = {"seed-tts-2.0"}
SUPPORTED_SAMPLE_RATES = {8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000}
# 方舟单向流式接口的成功响应可能使用 0 或 20000000。
SUCCESS_CODES = {0, 20_000_000}


class ArkTTSTransportError(RuntimeError):
    """官方 TTS 请求、响应或音频发布失败。"""


class ArkTTSConfigError(ArkTTSTransportError):
    """官方 TTS 配置不完整或不受支持。"""


class ArkTTSAudioPublisherRequired(ArkTTSTransportError):
    """官方返回了音频，但没有安全的 URL 发布器。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]
AudioPublisher = Callable[[bytes, str, Mapping[str, Any]], Any]
DurationProbe = Callable[[bytes, str], float]


def load_tts_api_keys_from_auth_document(path: str | Path) -> list[str]:
    """读取历史鉴权文档中的 TTS 双鉴权（仅迁移审计使用）。

    新版生产 TTS、声音复刻训练、状态查询和试听不会调用此函数；它保留
    只是为了让历史迁移脚本能够显式读取旧快照，且不打印、不持久化密钥。

    文档内必须存在 ``# TTS`` 区段；仅在该区段读取长度足够的 token，避免误取
    Seedance 等其它服务凭据。返回顺序即主、备用切换顺序。
    """
    document = Path(path)
    if not document.is_file():
        raise ArkTTSConfigError("未找到内部 TTS 鉴权文档")
    content = document.read_text(encoding="utf-8")
    heading = re.search(r"(?im)^\s*\\?#\s*[^\r\n]*TTS[^\r\n]*$", content)
    if heading is None:
        raise ArkTTSConfigError("内部鉴权文档缺少 TTS 区段")
    following = content[heading.end():]
    next_heading = re.search(r"(?im)^\s*\\?#", following)
    section = following[:next_heading.start()] if next_heading else following
    values = re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])", section)
    return list(dict.fromkeys(values))


def _extra_api_keys_from_env(name: str) -> tuple[str, ...]:
    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


@dataclass(frozen=True)
class ArkTTSConfig:
    api_key: str = ""
    backup_api_key: str = ""
    resource_id: str = DEFAULT_TTS_RESOURCE_ID
    model: str = DEFAULT_TTS_MODEL
    speaker_id: str = ""
    api_url: str = DEFAULT_ARK_TTS_URL
    audio_format: str = "mp3"
    sample_rate: int = DEFAULT_SAMPLE_RATE
    enable_subtitle: bool = True
    explicit_language: str = ""
    explicit_dialect: str = ""
    timeout: float = 300.0
    failover_strategy: str = "primary_then_backup"
    extra_api_keys: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, allow_legacy_document: bool = False) -> "ArkTTSConfig":
        """从运行时环境构造 TTS 配置。

        新版生产链路只允许读取 API 管理中心注入的 TTS API Key，默认不再
        读取历史《鉴权信息 .md》。``allow_legacy_document=True`` 只保留给
        明确标记为历史的离线迁移脚本；任何新版 TTS/声音复刻入口都不得打开
        该开关，避免旧凭据在页面外被静默重新启用。
        """
        api_key = (
            os.environ.get("ARK_TTS_API_KEY", "").strip()
            or os.environ.get("ARK_AUDIO_API_KEY", "").strip()
            or os.environ.get("VOLCENGINE_AUDIO_API_KEY", "").strip()
        )
        backup_api_key = (
            os.environ.get("ARK_TTS_BACKUP_API_KEY", "").strip()
            or os.environ.get("ARK_AUDIO_BACKUP_API_KEY", "").strip()
            or os.environ.get("VOLCENGINE_AUDIO_BACKUP_API_KEY", "").strip()
        )

        # 只有显式的历史迁移调用才允许读取文档；默认值为 False，避免
        # 运行环境中残留的 ARK_TTS_AUTH_DOCUMENT 重新启用旧鉴权链路。
        if allow_legacy_document:
            raw_auth_document = os.environ.get("ARK_TTS_AUTH_DOCUMENT")
            auth_document = (
                raw_auth_document.strip()
                if raw_auth_document is not None
                else str(Path.home() / "Documents" / "鉴权信息 .md")
            )
            if auth_document and Path(auth_document).is_file():
                try:
                    document_keys = load_tts_api_keys_from_auth_document(auth_document)
                except ArkTTSConfigError:
                    document_keys = []
                if not api_key and document_keys:
                    api_key = document_keys[0]
                if not backup_api_key and len(document_keys) > 1:
                    backup_api_key = document_keys[1]

        if not api_key:
            raise ArkTTSConfigError(
                "未配置 ARK_TTS_API_KEY 或 ARK_AUDIO_API_KEY；不会调用公网接口"
            )

        # 新版 V3 的 endpoint、默认 resource_id 和默认 model 属于协议常量，
        # 不能再被历史环境变量覆盖。定制音色需要的 seed-icl resource_id
        # 由编导音色目录随请求显式传入，不从进程环境推断。
        resource_id = DEFAULT_TTS_RESOURCE_ID
        model = DEFAULT_TTS_MODEL
        # 新版生产链路的 speaker 必须来自编导音色目录/请求。历史环境变量
        # 即使存在也不能作为默认音色，否则会在调用方漏传 speaker 时静默
        # 覆盖用户选择；直接阻断并提示清理，而不是继续走旧绑定。
        speaker_id = os.environ.get("ARK_TTS_SPEAKER_ID", "").strip()
        if speaker_id:
            raise ArkTTSConfigError(
                "ARK_TTS_SPEAKER_ID 已停用；请在编导音色目录中选择 voice_key，"
                "并由请求显式携带 speaker_id"
            )
        return cls(
            api_key=api_key,
            backup_api_key=backup_api_key,
            resource_id=resource_id,
            model=model,
            speaker_id=speaker_id,
            api_url=DEFAULT_ARK_TTS_URL,
            audio_format=os.environ.get("ARK_TTS_FORMAT", "mp3").strip(),
            sample_rate=int(
                os.environ.get("ARK_TTS_SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE))
            ),
            enable_subtitle=os.environ.get("ARK_TTS_ENABLE_SUBTITLE", "true").lower()
            in {"1", "true", "yes"},
            explicit_language=os.environ.get("ARK_TTS_LANGUAGE", "").strip(),
            explicit_dialect=os.environ.get("ARK_TTS_DIALECT", "").strip(),
            timeout=float(os.environ.get("ARK_TTS_TIMEOUT", "300")),
            failover_strategy=os.environ.get("ARK_TTS_FAILOVER_STRATEGY", "primary_then_backup").strip(),
            extra_api_keys=_extra_api_keys_from_env("API_MANAGEMENT_EXTRA_KEYS_TTS"),
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return _text(value)
    return ""


def _speech_rate(speed_ratio: float) -> int:
    value = int(round((float(speed_ratio) - 1.0) * 100))
    return max(-50, min(100, value))


def _language(value: str | None) -> str:
    aliases = {
        "中文": "zh-cn",
        "汉语": "zh-cn",
        "英语": "en",
        "英文": "en",
        "日语": "ja",
        "西语": "es-mx",
    }
    normalized = _text(value)
    return aliases.get(normalized, normalized)


def _json_objects(raw: bytes) -> list[Mapping[str, Any]]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ArkTTSTransportError("火山 TTS 响应为空")
    decoder = json.JSONDecoder()
    values: list[Mapping[str, Any]] = []
    position = 0
    try:
        while position < len(text):
            while position < len(text) and text[position].isspace():
                position += 1
            if position >= len(text):
                break
            item, position = decoder.raw_decode(text, position)
            if isinstance(item, Mapping):
                values.append(item)
            elif isinstance(item, list) and all(isinstance(value, Mapping) for value in item):
                values.extend(item)
            else:
                raise ArkTTSTransportError("TTS JSON chunk must be an object")
    except json.JSONDecodeError as exc:
        raise ArkTTSTransportError("TTS Chunked response is not valid JSON") from exc
    if not values:
        raise ArkTTSTransportError("TTS response contains no JSON object")
    return values

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        values: list[Mapping[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ArkTTSTransportError(
                    "火山 TTS Chunked 响应不是合法 JSON"
                ) from exc
            if isinstance(item, Mapping):
                values.append(item)
        if not values:
            raise ArkTTSTransportError("火山 TTS 响应不是 JSON 对象")
        return values
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return list(value)
    raise ArkTTSTransportError("火山 TTS 响应根节点必须是对象")


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
            raw = response.read()
            status = int(response.status)
            response_headers = {
                str(key): str(value) for key, value in response.headers.items()
            }
    except HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
        response_headers = {
            str(key): str(value) for key, value in exc.headers.items()
        }
    except (URLError, TimeoutError, OSError) as exc:
        raise ArkTTSTransportError(
            f"火山 TTS 网络请求失败：{type(exc).__name__}"
        ) from exc
    chunks = _json_objects(raw)
    return {
        "status_code": status,
        "headers": response_headers,
        "body": dict(chunks[-1]),
        "chunks": [dict(item) for item in chunks],
    }


def _parts(response: Any) -> tuple[int, Mapping[str, Any], list[Mapping[str, Any]], Mapping[str, str]]:
    if not isinstance(response, Mapping):
        raise ArkTTSTransportError("火山 TTS transport 响应必须是对象")
    body = response.get("body")
    if not isinstance(body, Mapping):
        body = response
    chunks = response.get("chunks")
    if not isinstance(chunks, list) or not all(isinstance(item, Mapping) for item in chunks):
        chunks = [body]
    headers = response.get("headers", {})
    if not isinstance(headers, Mapping):
        headers = {}
    return int(response.get("status_code", 200)), body, list(chunks), headers


def _decode_audio(chunks: list[Mapping[str, Any]]) -> bytes:
    parts: list[bytes] = []
    for item in chunks:
        value = item.get("data")
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parts.append(base64.b64decode(value, validate=False))
        except (ValueError, binascii.Error) as exc:
            raise ArkTTSTransportError("火山 TTS data 不是合法 Base64 音频") from exc
    audio = b"".join(parts)
    if not audio:
        raise ArkTTSTransportError("火山 TTS 响应没有 Base64 音频 data")
    return audio


def _max_timestamp(value: Any) -> float | None:
    candidates: list[float] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"endTime", "end_time", "end"} and isinstance(item, (int, float)):
                candidates.append(float(item))
            else:
                nested = _max_timestamp(item)
                if nested is not None:
                    candidates.append(nested)
    elif isinstance(value, list):
        for item in value:
            nested = _max_timestamp(item)
            if nested is not None:
                candidates.append(nested)
    return max(candidates) if candidates else None


def _probe_duration(audio: bytes, audio_format: str) -> float:
    """Use local ffprobe for official responses that contain no duration field."""
    ffprobe = os.environ.get("FFPROBE_PATH", "").strip() or shutil.which("ffprobe")
    if not ffprobe:
        raise ArkTTSTransportError(
            "方舟 TTS 响应未提供 duration，且本机未找到 ffprobe"
        )
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=f".{audio_format}", prefix="workflow-tts-", delete=False
        ) as temp_file:
            temp_file.write(audio)
            temp_path = temp_file.name
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "-i",
                temp_path,
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArkTTSTransportError("ffprobe 无法读取 TTS 音频时长") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    if result.returncode != 0:
        raise ArkTTSTransportError("ffprobe 无法解析 TTS 音频")
    try:
        duration = float(result.stdout.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArkTTSTransportError("ffprobe 未返回有效 TTS duration") from exc
    if duration < 0:
        raise ArkTTSTransportError("TTS duration 不能为负数")
    return duration


class ArkTTSHTTPTransport:
    """把官方 Chunked TTS 响应发布为 1256 的 ``data.link`` 契约。"""

    def __init__(
        self,
        config: ArkTTSConfig,
        *,
        audio_publisher: AudioPublisher | None = None,
        requester: Requester | None = None,
        duration_probe: DurationProbe | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not config.api_key.strip():
            raise ArkTTSConfigError("火山 TTS 缺少 X-Api-Key")
        if config.resource_id not in SUPPORTED_RESOURCE_IDS:
            raise ArkTTSConfigError(
                f"ARK_TTS_RESOURCE_ID 不受官方接口支持：{config.resource_id}"
            )
        normalized_api_url = str(config.api_url or "").strip().rstrip("/")
        if not normalized_api_url:
            raise ArkTTSConfigError("新版 V3 TTS endpoint 不能为空")
        if normalized_api_url != DEFAULT_ARK_TTS_URL:
            raise ArkTTSConfigError(
                "新版 V3 TTS endpoint 固定为 "
                f"{DEFAULT_ARK_TTS_URL}；旧版或自定义地址已停用"
            )
        if config.audio_format not in {"mp3", "pcm", "ogg_opus", "wav"}:
            raise ArkTTSConfigError("ARK_TTS_FORMAT 不是官方支持值")
        if config.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ArkTTSConfigError("ARK_TTS_SAMPLE_RATE 不是官方支持值")
        self.config = config
        strategy = str(config.failover_strategy or "primary_then_backup").strip().lower()
        if strategy not in {"primary_then_backup", "primary_only"}:
            raise ArkTTSConfigError("ARK_TTS_FAILOVER_STRATEGY 只支持 primary_then_backup 或 primary_only")
        auth_keys = [config.api_key.strip()]
        if strategy == "primary_then_backup":
            auth_keys.append(config.backup_api_key.strip())
            auth_keys.extend(config.extra_api_keys)
        self._auth_keys = tuple(dict.fromkeys(key for key in auth_keys if key))
        if not self._auth_keys:
            raise ArkTTSConfigError("火山 TTS 缺少主鉴权 API Key")
        self._active_auth_index = 0
        self.audio_publisher = audio_publisher
        self.requester = requester or _default_requester
        self.duration_probe = duration_probe or _probe_duration
        self.request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))

    @classmethod
    def from_env(
        cls,
        *,
        audio_publisher: AudioPublisher | None = None,
    ) -> "ArkTTSHTTPTransport":
        return cls(ArkTTSConfig.from_env(), audio_publisher=audio_publisher)

    def _headers(self, request_id: str, api_key: str, resource_id: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": request_id,
        }

    def _request_resource_id(self, request: SpeechSynthesisRequest) -> str:
        resource_id = _text(request.resource_id) or _text(self.config.resource_id)
        if resource_id not in SUPPORTED_RESOURCE_IDS:
            raise ArkTTSConfigError(f"TTS resource_id 不受官方接口支持：{resource_id}")
        return resource_id

    def _payload(self, request: SpeechSynthesisRequest) -> dict[str, Any]:
        configured_speaker = _text(self.config.speaker_id)
        requested_speaker = _text(request.speaker_id)
        requested_custom = _text(request.custom_speaker_id)
        if requested_custom:
            raise ArkTTSConfigError("官方 TTS 不支持 custom_speaker_id；定制音色/声音复刻已停用")
        if requested_speaker == CUSTOM_SPEAKER_PROTOCOL_ID or configured_speaker == CUSTOM_SPEAKER_PROTOCOL_ID:
            raise ArkTTSConfigError("官方 TTS 不支持 speaker_id=custom_speaker_id")
        if configured_speaker and requested_speaker and configured_speaker != requested_speaker:
            raise ArkTTSConfigError(
                "火山 TTS 配置 speaker_id 与请求 speaker_id 不一致；拒绝覆盖用户选择"
            )
        speaker = requested_speaker or configured_speaker
        if not speaker.strip():
            raise ArkTTSConfigError("火山 TTS 缺少 req_params.speaker")
        audio_params: dict[str, Any] = {
            "format": self.config.audio_format,
            "sample_rate": self.config.sample_rate,
            "speech_rate": _speech_rate(request.speed_ratio),
            "loudness_rate": request.loudness_rate,
            "enable_subtitle": self.config.enable_subtitle,
        }
        if request.pitch:
            audio_params["pitch"] = request.pitch
        if request.silence_duration:
            audio_params["silence_duration"] = request.silence_duration
        language = _language(request.language or self.config.explicit_language)
        if language:
            audio_params["explicit_language"] = language
        dialect = _text(request.dialect or self.config.explicit_dialect)
        if dialect:
            audio_params["explicit_dialect"] = dialect
        params: dict[str, Any] = {
            "text": request.text,
            "speaker": speaker.strip(),
            "audio_params": audio_params,
        }
        # Resource ID 已经决定 TTS/ICL 服务版本。V3 官方示例在默认场景
        # 不传 model；历史代码中的 seed-tts-2.0-standard 只是本地元数据
        # 别名，直接发送会把未被官方确认的值带入真实请求。显式填写的
        # provider model（如 seed-tts-1.1）仍原样透传，便于按官方文档升级。
        model = _text(request.model) or _text(self.config.model)
        if model and model not in _INTERNAL_DEFAULT_MODEL_ALIASES:
            params["model"] = model
        if request.emotion:
            detail = f"请使用{request.emotion}的语气朗读，情绪强度为{request.emotion_scale if request.emotion_scale is not None else 0.5}。"
            params["context_texts"] = [detail]
        return {"req_params": params}

    def synthesize(self, request: SpeechSynthesisRequest) -> dict[str, Any]:
        if self.audio_publisher is None:
            raise ArkTTSAudioPublisherRequired(
                "官方 TTS 返回 Base64 音频；必须先配置安全 audio_publisher 才能生成 data.link"
            )
        local = self.synthesize_local(request)
        audio = local["audio"]
        metadata = local["metadata"]
        duration = local["duration"]
        request_id = local["request_id"]
        filename = f"tts-{request_id}.{self.config.audio_format}"
        published = self.audio_publisher(audio, filename, metadata)
        if isinstance(published, str):
            link = published.strip()
            published_duration = None
        elif isinstance(published, Mapping):
            link = _text(published.get("url") or published.get("link"))
            published_duration = published.get("duration")
        else:
            raise ArkTTSTransportError("audio_publisher 必须返回 URL 或对象")
        if not link.startswith(("http://", "https://")):
            raise ArkTTSTransportError("audio_publisher 未返回可访问 http(s) URL")
        if published_duration is not None:
            duration = published_duration
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ArkTTSTransportError(
                "无法得到 TTS duration；publisher 或 enable_subtitle 必须提供时长"
            )
        if duration < 0:
            raise ArkTTSTransportError("TTS duration 不能为负数")
        return {
            "code": local["code"],
            "data": {"duration": float(duration), "link": link},
            "log_id": local["log_id"],
            "msg": local["msg"],
        }

    @staticmethod
    def _should_failover(error: Exception) -> bool:
        """识别鉴权、额度、限流和资源绑定错误，允许切换备用鉴权。"""
        text = str(error).lower()
        markers = (
            "http 401", "http 403", "http 429", "unauthorized", "forbidden",
            "authentication", "permission", "鉴权", "权限", "quota", "limit",
            "rate limit", "too many requests", "insufficient", "balance",
            "resource id is mismatched", "resource mismatch", "额度", "余额",
        )
        return any(marker in text for marker in markers)

    def _synthesize_local_once(
        self,
        request: SpeechSynthesisRequest,
        api_key: str,
    ) -> dict[str, Any]:
        request_id = self.request_id_factory()
        resource_id = self._request_resource_id(request)
        response = self.requester(
            self.config.api_url,
            self._headers(request_id, api_key, resource_id),
            self._payload(request),
            self.config.timeout,
        )
        status, body, chunks, headers = _parts(response)
        if status >= 400:
            raise ArkTTSTransportError(f"火山 TTS HTTP {status}")
        code = body.get("code", 0)
        if isinstance(code, bool) or not isinstance(code, (int, float)):
            raise ArkTTSTransportError("火山 TTS code 必须是数字")
        if int(code) not in SUCCESS_CODES:
            message = _text(body.get("message")) or "火山 TTS 返回失败"
            raise ArkTTSTransportError(f"火山 TTS code={int(code)}：{message[:300]}")
        audio = _decode_audio(chunks)
        metadata = dict(body)
        metadata["request_id"] = request_id
        metadata["response_headers"] = dict(headers)
        # 记录本次请求实际采用的绑定参数，供跨账号降级审计使用；不记录
        # API Key，主/副账号切换不得改变音色绑定。普通音色不写空的
        # custom_speaker_id，避免旧快照和普通音色契约被无意义字段污染。
        metadata["voice_binding"] = {
            "speaker_id": _text(request.speaker_id) or _text(self.config.speaker_id),
            "resource_id": resource_id,
            "model": _text(request.model) or _text(self.config.model),
        }
        duration = _max_timestamp(body)
        if duration is None:
            duration = self.duration_probe(audio, self.config.audio_format)
        metadata["duration"] = duration
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ArkTTSTransportError(
                "无法得到 TTS duration；服务响应或本机 ffprobe 必须提供时长"
            )
        if duration < 0:
            raise ArkTTSTransportError("TTS duration 不能为负数")
        return {
            "code": int(code),
            "audio": audio,
            "duration": float(duration),
            "metadata": metadata,
            "request_id": request_id,
            "log_id": _header_value(headers, "X-Tt-Logid") or _text(body.get("log_id")),
            "msg": _text(body.get("message")) or "ok",
        }

    def synthesize_local(self, request: SpeechSynthesisRequest) -> dict[str, Any]:
        """真实请求并返回音频字节；主鉴权失败时自动切换备用鉴权。"""
        order = [self._active_auth_index]
        order.extend(index for index in range(len(self._auth_keys)) if index not in order)
        errors: list[str] = []
        for index in order:
            api_key = self._auth_keys[index]
            try:
                result = self._synthesize_local_once(request, api_key)
                self._active_auth_index = index
                metadata = dict(result.get("metadata") or {})
                metadata["auth_slot"] = "primary" if index == 0 else "backup"
                result["metadata"] = metadata
                return result
            except ArkTTSTransportError as exc:
                errors.append(f"{('主' if index == 0 else '备用')}鉴权：{exc}")
                if not self._should_failover(exc):
                    raise
        raise ArkTTSTransportError("；".join(errors) or "主、备用鉴权均失败")


__all__ = [
    "ArkTTSConfig",
    "ArkTTSConfigError",
    "ArkTTSAudioPublisherRequired",
    "ArkTTSTransportError",
    "ArkTTSHTTPTransport",
    "DEFAULT_ARK_TTS_URL",
    "DEFAULT_TTS_MODEL",
    "DEFAULT_TTS_RESOURCE_ID",
    "DEFAULT_TTS_SPEAKER_ID",
    "load_tts_api_keys_from_auth_document",
]
