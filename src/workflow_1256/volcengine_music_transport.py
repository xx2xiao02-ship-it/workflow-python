"""火山引擎 AI 音乐生成 transport（AK/SK 独立鉴权）。

这里实现的是 Volcengine ``imagination`` 服务的 HMAC-SHA256 签名接口，
不是 TTS、TOS，也不读取任何 API Key 或历史鉴权文档。对上层保留已有
``gen_bgm`` 的 ``data.SongDetail.AudioUrl`` 输出契约，避免改动 8364 的
任务组装、时间线和融合字段。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from typing import Any


DEFAULT_VOLCENGINE_MUSIC_URL = "https://open.volcengineapi.com/"
DEFAULT_VOLCENGINE_MUSIC_REGION = "cn-beijing"
DEFAULT_VOLCENGINE_MUSIC_SERVICE = "imagination"
DEFAULT_VOLCENGINE_MUSIC_API_VERSION = "2024-08-12"
DEFAULT_VOLCENGINE_MUSIC_MODEL_VERSION = "v5.0"
MIN_BGM_DURATION = 30
MAX_BGM_DURATION = 120


class VolcengineMusicTransportError(RuntimeError):
    """音乐生成请求、轮询或响应映射失败。"""


class VolcengineMusicConfigError(VolcengineMusicTransportError):
    """音乐生成 AK/SK 配置不完整。"""


# url, method, headers, body, timeout -> response mapping
Requester = Callable[[str, str, Mapping[str, str], bytes | None, float], Any]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise VolcengineMusicConfigError(f"{name} 必须是数字") from exc
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise VolcengineMusicConfigError(f"{name} 必须是整数") from exc
    return value


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class VolcengineMusicConfig:
    """独立 BGM 凭据及请求参数；密钥只存在运行时对象，不参与序列化。"""

    access_key: str = ""
    secret_key: str = ""
    backup_access_key: str = ""
    backup_secret_key: str = ""
    api_url: str = DEFAULT_VOLCENGINE_MUSIC_URL
    region: str = DEFAULT_VOLCENGINE_MUSIC_REGION
    service: str = DEFAULT_VOLCENGINE_MUSIC_SERVICE
    api_version: str = DEFAULT_VOLCENGINE_MUSIC_API_VERSION
    model_version: str = DEFAULT_VOLCENGINE_MUSIC_MODEL_VERSION
    failover_strategy: str = "primary_then_backup"
    timeout: float = 300.0
    poll_interval: float = 2.0
    max_wait: float = 300.0

    @classmethod
    def from_env(cls) -> "VolcengineMusicConfig":
        # 只接受 BGM 专用变量。不会从 TTS、TOS、故事模型或历史鉴权文档取值。
        return cls(
            access_key=_first_env("ARK_BGM_ACCESS_KEY"),
            secret_key=_first_env("ARK_BGM_SECRET_KEY"),
            backup_access_key=_first_env("ARK_BGM_BACKUP_ACCESS_KEY"),
            backup_secret_key=_first_env("ARK_BGM_BACKUP_SECRET_KEY"),
            api_url=_first_env("ARK_BGM_API_URL") or DEFAULT_VOLCENGINE_MUSIC_URL,
            region=_first_env("ARK_BGM_REGION") or DEFAULT_VOLCENGINE_MUSIC_REGION,
            service=_first_env("ARK_BGM_SERVICE") or DEFAULT_VOLCENGINE_MUSIC_SERVICE,
            api_version=_first_env("ARK_BGM_API_VERSION") or DEFAULT_VOLCENGINE_MUSIC_API_VERSION,
            model_version=(
                _first_env("ARK_BGM_MODEL")
                if _first_env("ARK_BGM_MODEL").lower().startswith("v")
                else DEFAULT_VOLCENGINE_MUSIC_MODEL_VERSION
            ),
            failover_strategy=_first_env("ARK_BGM_FAILOVER_STRATEGY") or "primary_then_backup",
            timeout=_env_float("ARK_BGM_TIMEOUT", 300.0),
            poll_interval=_env_float("ARK_BGM_POLL_INTERVAL", 2.0),
            max_wait=_env_float("ARK_BGM_MAX_WAIT", _env_float("ARK_BGM_TIMEOUT", 300.0)),
        )

    def credential_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if self.access_key or self.secret_key:
            pairs.append((self.access_key.strip(), self.secret_key.strip()))
        if self.backup_access_key or self.backup_secret_key:
            pairs.append((self.backup_access_key.strip(), self.backup_secret_key.strip()))
        return pairs


def _sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _hmac_hex(key: bytes, value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def _hmac_bytes(key: bytes, value: bytes | str) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key, raw, hashlib.sha256).digest()


def _canonical_query(params: Mapping[str, object] | list[tuple[str, object]]) -> str:
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    encoded = [
        (quote(str(key), safe="-_.~"), quote(str(value), safe="-_.~"))
        for key, value in items
    ]
    encoded.sort()
    return "&".join(f"{key}={value}" for key, value in encoded)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    normalized = {
        str(key).lower().strip(): " ".join(str(value).strip().split())
        for key, value in headers.items()
    }
    signed_names = ";".join(sorted(normalized))
    canonical = "".join(f"{name}:{normalized[name]}\n" for name in sorted(normalized))
    return canonical, signed_names


def _build_signed_headers(
    *,
    access_key: str,
    secret_key: str,
    method: str,
    url: str,
    query: Mapping[str, object] | list[tuple[str, object]],
    body: bytes,
    region: str,
    service: str,
    timestamp: datetime,
) -> dict[str, str]:
    parts = urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    query_string = _canonical_query(query)
    x_date = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload_hash = _sha256_hex(body)
    signing_headers = {
        "content-type": "application/json; charset=utf-8",
        "host": host,
        "x-content-sha256": payload_hash,
        "x-date": x_date,
    }
    canonical_header_text, signed_names = _canonical_headers(signing_headers)
    canonical_request = "\n".join(
        [method.upper(), path, query_string, canonical_header_text, signed_names, payload_hash]
    )
    date = x_date[:8]
    scope = f"{date}/{region}/{service}/request"
    string_to_sign = "\n".join(
        ["HMAC-SHA256", x_date, scope, _sha256_hex(canonical_request)]
    )
    # Volcengine OpenAPI HMAC-SHA256 V4 derives the date key directly from
    # SecretAccessKey; unlike AWS-style variants, no provider prefix is added.
    date_key = _hmac_bytes(secret_key.encode("utf-8"), date)
    region_key = _hmac_bytes(date_key, region)
    service_key = _hmac_bytes(region_key, service)
    signing_key = _hmac_bytes(service_key, "request")
    signature = _hmac_hex(signing_key, string_to_sign)
    return {
        "Content-Type": signing_headers["content-type"],
        "Host": host,
        "X-Content-Sha256": payload_hash,
        "X-Date": x_date,
        "Authorization": (
            f"HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        ),
    }


def _default_requester(
    url: str,
    method: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> dict[str, object]:
    request = Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
            response_headers = {str(key): str(value) for key, value in response.headers.items()}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        response_headers = {str(key): str(value) for key, value in exc.headers.items()}
    except (URLError, TimeoutError, OSError) as exc:
        raise VolcengineMusicTransportError(
            f"BGM 音乐接口网络请求失败：{type(exc).__name__}"
        ) from exc
    try:
        body_value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VolcengineMusicTransportError("BGM 音乐接口响应不是合法 JSON") from exc
    if not isinstance(body_value, Mapping):
        raise VolcengineMusicTransportError("BGM 音乐接口响应根节点必须是对象")
    return {"status_code": status, "headers": response_headers, "body": dict(body_value)}


def _response_parts(response: Any) -> tuple[int, Mapping[str, Any]]:
    if isinstance(response, Mapping) and isinstance(response.get("body"), Mapping):
        return int(response.get("status_code", 200)), response["body"]
    if isinstance(response, Mapping):
        return 200, response
    raise VolcengineMusicTransportError("BGM 音乐 transport 响应必须是对象")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _result(body: Mapping[str, Any]) -> Mapping[str, Any]:
    value = body.get("Result")
    if isinstance(value, Mapping):
        return value
    value = body.get("result")
    return value if isinstance(value, Mapping) else {}


def _provider_error(body: Mapping[str, Any]) -> tuple[str, str]:
    metadata = body.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        metadata = body.get("response_metadata")
    error = metadata.get("Error") if isinstance(metadata, Mapping) else None
    if not isinstance(error, Mapping):
        error = body.get("Error")
    if not isinstance(error, Mapping):
        return "", ""
    return _text(error.get("Code") or error.get("code")), _text(error.get("Message") or error.get("message"))


def _task_id(body: Mapping[str, Any]) -> str:
    result = _result(body)
    return _text(result.get("TaskID") or result.get("TaskId") or body.get("TaskID") or body.get("task_id"))


def _song_detail(body: Mapping[str, Any]) -> Mapping[str, Any]:
    result = _result(body)
    for candidate in (result.get("SongDetail"), result.get("song_detail"), body.get("SongDetail")):
        if isinstance(candidate, Mapping):
            return candidate
    return result


def _audio_url(body: Mapping[str, Any]) -> str:
    detail = _song_detail(body)
    return _text(
        detail.get("AudioUrl")
        or detail.get("AudioURL")
        or detail.get("AudioUrl")
        or detail.get("Url")
        or detail.get("URL")
        or body.get("AudioUrl")
        or body.get("audio_url")
    )


class VolcengineMusicHTTPTransport:
    """调用 GenBGMForTime 并轮询 QuerySong，返回既有 gen_bgm 输出契约。"""

    def __init__(
        self,
        config: VolcengineMusicConfig,
        *,
        requester: Requester | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        timestamp_factory: Callable[[], datetime] | None = None,
        task_created_reporter: Callable[[Mapping[str, Any]], None] | None = None,
        task_query_reporter: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        parts = urlsplit(config.api_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise VolcengineMusicConfigError("ARK_BGM_API_URL 必须是 http/https 地址")
        if not config.region.strip() or not config.service.strip() or not config.api_version.strip():
            raise VolcengineMusicConfigError("BGM AK/SK 请求的区域、服务名和版本不能为空")
        pairs = config.credential_pairs()
        if not pairs or not any(access and secret for access, secret in pairs):
            raise VolcengineMusicConfigError(
                "未配置 BGM 独立 AK/SK：请填写 API 管理中的 Access Key 和 Secret Key"
            )
        if config.timeout <= 0 or config.max_wait <= 0:
            raise VolcengineMusicConfigError("BGM 请求超时必须大于 0")
        if config.poll_interval < 0:
            raise VolcengineMusicConfigError("BGM 轮询间隔不能为负数")
        self.config = config
        self.requester = requester or _default_requester
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc))
        self.task_created_reporter = task_created_reporter
        self.task_query_reporter = task_query_reporter

    @classmethod
    def from_env(cls, **kwargs: Any) -> "VolcengineMusicHTTPTransport":
        return cls(VolcengineMusicConfig.from_env(), **kwargs)

    def _url(self, action: str, *, task_id: str = "") -> tuple[str, list[tuple[str, str]]]:
        parts = urlsplit(self.config.api_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"Action", "Version", "TaskID"}
        ]
        query.extend((("Action", action), ("Version", self.config.api_version)))
        if task_id:
            query.append(("TaskID", task_id))
        query_string = urlencode(query)
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query_string, "")), query

    def _call(
        self,
        action: str,
        *,
        access_key: str,
        secret_key: str,
        method: str,
        task_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body = (
            json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if method.upper() != "GET"
            else b""
        )
        url, query = self._url(action, task_id=task_id)
        headers = _build_signed_headers(
            access_key=access_key,
            secret_key=secret_key,
            method=method,
            url=url,
            query=query,
            body=body,
            region=self.config.region,
            service=self.config.service,
            timestamp=self.timestamp_factory(),
        )
        try:
            raw_response = self.requester(url, method.upper(), headers, body if method.upper() != "GET" else None, self.config.timeout)
        except VolcengineMusicTransportError:
            raise
        except Exception as exc:
            raise VolcengineMusicTransportError(
                f"BGM 音乐接口请求失败：{type(exc).__name__}"
            ) from exc
        status, body_value = _response_parts(raw_response)
        error_code, error_message = _provider_error(body_value)
        if status >= 400:
            detail = f" code={error_code}" if error_code else ""
            if error_message:
                detail += f" message={error_message[:240]}"
            raise VolcengineMusicTransportError(f"BGM 音乐接口 HTTP {status}{detail}")
        if error_code or error_message:
            detail = f" code={error_code}" if error_code else ""
            raise VolcengineMusicTransportError(
                f"BGM 音乐服务拒绝请求{detail}：{error_message[:240] or '未提供原因'}"
            )
        return body_value

    @staticmethod
    def _payload(request: Mapping[str, Any], model_version: str) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise VolcengineMusicTransportError("gen_bgm 输入必须是对象")
        duration = request.get("Duration")
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise VolcengineMusicTransportError("gen_bgm.Duration 必须是整数秒")
        if not MIN_BGM_DURATION <= duration <= MAX_BGM_DURATION:
            raise VolcengineMusicTransportError(
                f"gen_bgm.Duration 必须在 {MIN_BGM_DURATION}–{MAX_BGM_DURATION} 秒内"
            )
        text = _text(request.get("Text"))
        if not text:
            raise VolcengineMusicTransportError("gen_bgm.Text 不能为空")
        payload: dict[str, Any] = {
            "Text": text,
            "Version": _text(request.get("Version")) or model_version or DEFAULT_VOLCENGINE_MUSIC_MODEL_VERSION,
            "EnableInputRewrite": bool(request.get("EnableInputRewrite", True)),
            "Duration": duration,
        }
        segments = request.get("Segments")
        if isinstance(segments, list) and segments:
            payload["Segments"] = segments
        return payload

    def _generate_once(
        self,
        request: Mapping[str, Any],
        *,
        access_key: str,
        secret_key: str,
    ) -> dict[str, Any]:
        payload = self._payload(request, self.config.model_version)
        created = self._call(
            "GenBGMForTime",
            access_key=access_key,
            secret_key=secret_key,
            method="POST",
            payload=payload,
        )
        task_id = _task_id(created)
        created_url = _audio_url(created)
        if created_url:
            if self.task_created_reporter is not None and task_id:
                self.task_created_reporter({
                    "task_id": task_id,
                    "request": dict(request),
                    "payload": dict(payload),
                    "status": "succeeded",
                })
            detail = _song_detail(created)
            return self._legacy_output(created, detail, created_url, task_id, payload)
        if not task_id:
            raise VolcengineMusicTransportError("BGM 创建成功响应缺少 Result.TaskID")
        if self.task_created_reporter is not None:
            self.task_created_reporter({
                "task_id": task_id,
                "request": dict(request),
                "payload": dict(payload),
                "status": "submitted",
            })
        return self._query_once(
            request,
            task_id=task_id,
            access_key=access_key,
            secret_key=secret_key,
            payload=payload,
        )

    def _query_once(
        self,
        request: Mapping[str, Any],
        *,
        task_id: str,
        access_key: str,
        secret_key: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(payload or self._payload(request, self.config.model_version))
        deadline = self.clock() + self.config.max_wait
        while True:
            queried = self._call(
                "QuerySong",
                access_key=access_key,
                secret_key=secret_key,
                method="GET",
                task_id=task_id,
            )
            if self.task_query_reporter is not None:
                self.task_query_reporter({
                    "task_id": task_id,
                    "request": dict(request),
                    "status": _text(_result(queried).get("Status", queried.get("Status"))),
                })
            status_value = _result(queried).get("Status", queried.get("Status"))
            status_text = _text(status_value).lower()
            status = int(status_value) if isinstance(status_value, (int, float)) and not isinstance(status_value, bool) else None
            if status_text in {"success", "succeeded", "completed"}:
                status = 2
            if status == 3 or status_text in {"failed", "error"}:
                reason = _text(
                    _result(queried).get("FailReason")
                    or _result(queried).get("ErrorMsg")
                    or queried.get("Message")
                )
                raise VolcengineMusicTransportError(
                    f"BGM 任务失败：{reason[:240] or '服务返回失败状态'}"
                )
            audio_url = _audio_url(queried)
            if status == 2 or audio_url:
                if not audio_url:
                    raise VolcengineMusicTransportError("BGM 任务完成但未返回 AudioUrl")
                return self._legacy_output(queried, _song_detail(queried), audio_url, task_id, payload)
            if self.clock() >= deadline:
                raise VolcengineMusicTransportError(
                    f"BGM 任务轮询超时（{int(self.config.max_wait)} 秒）"
                )
            self.sleep(self.config.poll_interval)

    def query_bgm(self, request: Mapping[str, Any], task_id: str) -> dict[str, Any]:
        """Resume a previously-created BGM task without creating a new task."""

        task_id = _text(task_id)
        if not task_id:
            raise VolcengineMusicTransportError("BGM 续查缺少 TaskID")
        payload = self._payload(request, self.config.model_version)
        pairs = self.config.credential_pairs()
        strategy = self.config.failover_strategy.strip().lower()
        candidates = pairs[:1] if strategy == "primary_only" else pairs
        errors: list[Exception] = []
        for access_key, secret_key in candidates:
            if not access_key or not secret_key:
                errors.append(VolcengineMusicConfigError("BGM AK/SK 必须成对填写"))
                continue
            try:
                return self._query_once(
                    request,
                    task_id=task_id,
                    access_key=access_key,
                    secret_key=secret_key,
                    payload=payload,
                )
            except VolcengineMusicTransportError as exc:
                errors.append(exc)
        if errors:
            raise VolcengineMusicTransportError(
                f"BGM 续查失败：已尝试 {len(errors)} 套独立 AK/SK；{str(errors[-1])[:300]}"
            ) from errors[-1]
        raise VolcengineMusicConfigError("未配置 BGM 独立 AK/SK")

    @staticmethod
    def _legacy_output(
        body: Mapping[str, Any],
        detail: Mapping[str, Any],
        audio_url: str,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not audio_url.startswith(("http://", "https://")):
            raise VolcengineMusicTransportError("BGM 结果 AudioUrl 必须是 http(s) URL")
        duration = _number(detail.get("Duration"), _number(payload.get("Duration"), 0.0))
        return {
            "code": 200,
            "data": {
                "SongDetail": {
                    "AudioUrl": audio_url,
                    "Duration": duration,
                    "Prompt": _text(payload.get("Text")),
                    "Version": _text(payload.get("Version")),
                    "Genre": "",
                    "Instrument": "",
                    "Mood": "",
                    "Captions": "",
                    "Lyrics": "",
                },
                "Progress": 1.0,
                "Status": 2,
                "TaskID": task_id,
            },
            "msg": "ok",
            "errorBody": {"errorCode": "", "errorMessage": ""},
            "isSuccess": True,
            "provider_response": {
                "request_id": _text(
                    (body.get("ResponseMetadata") or {}).get("RequestId")
                    if isinstance(body.get("ResponseMetadata"), Mapping)
                    else ""
                )
            },
        }

    def generate_bgm(self, request: Mapping[str, Any]) -> dict[str, Any]:
        pairs = self.config.credential_pairs()
        strategy = self.config.failover_strategy.strip().lower()
        candidates = pairs[:1] if strategy == "primary_only" else pairs
        errors: list[Exception] = []
        for access_key, secret_key in candidates:
            if not access_key or not secret_key:
                errors.append(VolcengineMusicConfigError("BGM AK/SK 必须成对填写"))
                continue
            try:
                return self._generate_once(
                    request,
                    access_key=access_key,
                    secret_key=secret_key,
                )
            except VolcengineMusicTransportError as exc:
                errors.append(exc)
        if errors:
            raise VolcengineMusicTransportError(
                f"BGM 生成失败：已尝试 {len(errors)} 套独立 AK/SK；{str(errors[-1])[:300]}"
            ) from errors[-1]
        raise VolcengineMusicConfigError("未配置 BGM 独立 AK/SK")


__all__ = [
    "DEFAULT_VOLCENGINE_MUSIC_URL",
    "VolcengineMusicConfig",
    "VolcengineMusicConfigError",
    "VolcengineMusicHTTPTransport",
    "VolcengineMusicTransportError",
]
