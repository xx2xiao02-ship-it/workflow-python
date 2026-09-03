"""RunningHub transport for the 8364 InfiniteTalk nodes.

This module implements the behavior observed in the supplied create/query
plugins. Credentials are runtime inputs only; no attachment credentials are
stored here. Network calls are injectable so contract tests never contact
RunningHub or create billable tasks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any


class InfiniteTalkTransportError(RuntimeError):
    """Configuration, HTTP, or task-state error from RunningHub."""


class InfiniteTalkValidationError(ValueError):
    """Invalid 8364 InfiniteTalk input or response."""


RUN_APP_ID = "2006270540499128322"
NODE_IMAGE = "284"
NODE_MAX_SIDE = "312"
NODE_AUDIO = "125"
NODE_PROMPT = "314"
NODE_INTENSITY = "369"
NODE_SCALE = "370"


@dataclass(frozen=True)
class InfiniteTalkConfig:
    api_key: str
    app_id: str = RUN_APP_ID
    base_url: str = "https://www.runninghub.cn/openapi/v2"
    download_timeout: float = 60.0
    upload_timeout: float = 60.0
    create_timeout: float = 45.0
    query_timeout: float = 30.0
    health_check_seconds: float = 10.0
    max_wait_seconds: float | None = None
    poll_interval_seconds: float = 10.0
    max_network_errors: int = 3

    @classmethod
    def from_env(cls) -> "InfiniteTalkConfig":
        key = os.environ.get("INFINITETALK_API_KEY", "").strip()
        if not key:
            raise InfiniteTalkTransportError(
                "未配置 INFINITETALK_API_KEY；也可以在节点输入中显式传入 api_key"
            )
        return cls(
            api_key=key,
            app_id=os.environ.get("RUNNINGHUB_APP_ID", RUN_APP_ID).strip(),
            base_url=os.environ.get(
                "RUNNINGHUB_API_BASE", "https://www.runninghub.cn/openapi/v2"
            ).rstrip("/"),
            health_check_seconds=float(os.environ.get("INFINITETALK_HEALTH_CHECK_SECONDS", "10")),
            max_wait_seconds=(float(os.environ["INFINITETALK_MAX_WAIT_SECONDS"]) if os.environ.get("INFINITETALK_MAX_WAIT_SECONDS", "").strip() else None),
            poll_interval_seconds=float(os.environ.get("INFINITETALK_POLL_INTERVAL_SECONDS", "10")),
        )

    @property
    def run_url(self) -> str:
        return f"{self.base_url}/run/ai-app/{self.app_id}"

    @property
    def upload_url(self) -> str:
        return f"{self.base_url}/media/upload/binary"

    @property
    def query_url(self) -> str:
        return f"{self.base_url}/query"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _plain(vars(value))
    return value


def _resolve(value: Any) -> dict[str, Any]:
    value = _plain(value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InfiniteTalkValidationError("InfiniteTalk 输入 JSON 无效") from exc
    while isinstance(value, Mapping):
        for key in ("_input", "input", "params", "body", "data", "payload"):
            inner = value.get(key)
            if isinstance(inner, Mapping) or isinstance(inner, str):
                value = _plain(inner)
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError as exc:
                        raise InfiniteTalkValidationError("InfiniteTalk 输入 JSON 无效") from exc
                break
        else:
            return dict(value)
    raise InfiniteTalkValidationError("InfiniteTalk 输入必须是对象")


def normalize_api_key(value: Any) -> str:
    key = str(value or "").strip()
    key = re.sub(r"^Bearer\s+", "", key, flags=re.IGNORECASE).strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"":
        key = key[1:-1].strip()
    if len(key) != 32 or any(c.isspace() or not c.isprintable() for c in key):
        raise InfiniteTalkValidationError("api_key 必须是 RunningHub 的 32 位密钥")
    return key


def api_key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def load_infinite_talk_auth_from_document(path: str | Path) -> dict[str, str]:
    """Read the user-owned credential document without logging any secret.

    The existing document keeps the RunningHub key under ``## RUNNINGHUB`` and
    stores temporary TOS settings as JSON-like ``ak/sk/bucket/endpoint/region``
    fields.  This helper is deliberately opt-in: callers decide when a real
    task is authorized, while the returned values remain process memory only.
    """

    document = Path(path)
    raw = document.read_text(encoding="utf-8") if document.is_file() else ""
    section = re.search(r"(?ims)^##\s*RUNNINGHUB\s*$([\s\S]*?)(?=^##\s|\Z)", raw)
    key_source = section.group(1) if section else raw
    key_match = re.search(r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", key_source)
    configured_key = os.environ.get("INFINITETALK_API_KEY", "").strip()
    if not key_match and not configured_key:
        raise InfiniteTalkTransportError("鉴权文档未找到有效的 RunningHub 32 位密钥")

    def field(name: str) -> str:
        pattern = rf'''(?im)["']?{re.escape(name)}["']?\s*[:=]\s*["']?([^\s,"'}}]+)'''
        match = re.search(pattern, raw)
        return match.group(1).strip() if match else ""

    def runtime_field(*names: str, fallback: str = "") -> str:
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return fallback

    values = {
        "api_key": normalize_api_key(configured_key or key_match.group(0)),
        "tos_access_key": runtime_field("ARK_TTS_TOS_ACCESS_KEY", "INFINITETALK_TOS_ACCESS_KEY", fallback=field("ak")),
        "tos_secret_key": runtime_field("ARK_TTS_TOS_SECRET_KEY", "INFINITETALK_TOS_SECRET_KEY", fallback=field("sk")),
        "tos_bucket": runtime_field("ARK_TTS_TOS_BUCKET", "INFINITETALK_TOS_BUCKET", fallback=field("bucket")),
        "tos_endpoint": runtime_field("ARK_TTS_TOS_ENDPOINT", "INFINITETALK_TOS_ENDPOINT", fallback=field("endpoint")),
        "tos_region": runtime_field("ARK_TTS_TOS_REGION", "INFINITETALK_TOS_REGION", fallback=field("region")),
    }
    missing = [name for name in ("tos_access_key", "tos_secret_key", "tos_bucket") if not values[name]]
    if missing:
        raise InfiniteTalkTransportError("鉴权文档缺少数字人临时上传所需的 TOS 配置")
    return values


def _response_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return {"non_json_response": str(getattr(response, "text", "") or "")[:1000]}


def _require_http(response: Any, stage: str) -> Any:
    data = _response_json(response)
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        raise InfiniteTalkTransportError(f"RunningHub {stage} 请求失败 HTTP {status}")
    if isinstance(data, Mapping) and data.get("code") not in (None, 0, "0"):
        raise InfiniteTalkTransportError(f"RunningHub {stage} 业务请求失败")
    return data


def _download(session: Any, url: str, timeout: float, label: str) -> tuple[bytes, str]:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise InfiniteTalkValidationError(f"{label} 必须是 http/https URL")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    url_lower = url.lower()
    if any(marker in url_lower for marker in ("byteimg.com", "bytecdn", "bytedance", "p26-bot-workflow-sign")):
        headers["Referer"] = "https://www.coze.cn/"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            data = getattr(response, "content", b"")
            status = int(getattr(response, "status_code", 0) or 0)
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
            if status == 404:
                raise InfiniteTalkTransportError(f"{label} 链接已失效或文件不存在")
            if status in (401, 403):
                raise InfiniteTalkTransportError(f"{label} 链接无访问权限")
            if 200 <= status < 300 and data and "text/html" not in content_type.lower():
                return bytes(data), content_type
            last_error = InfiniteTalkTransportError(f"{label} 下载失败")
        except InfiniteTalkTransportError as exc:
            if "已失效" in str(exc) or "无访问权限" in str(exc):
                raise
            last_error = exc
        except Exception as exc:  # requests/network failure: match the original retry behavior.
            last_error = exc
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise InfiniteTalkTransportError(f"{label} 下载失败") from last_error


def _image_png(content: bytes) -> bytes:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return content
    try:
        from PIL import Image
        image = Image.open(BytesIO(content))
        if image.mode != "RGB":
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception as exc:
        raise InfiniteTalkValidationError("人物图片无法转换为 PNG") from exc


def _audio_mime(content: bytes, url: str, content_type: str) -> tuple[str, str]:
    lower_url = url.lower().split("?", 1)[0]
    lower_type = content_type.lower()
    if content.startswith(b"ID3") or content[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio.mp3", "audio/mpeg"
    if len(content) > 12 and b"ftyp" in content[:32]:
        return "audio.m4a", "audio/mp4"
    if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
        return "audio.wav", "audio/wav"
    if lower_url.endswith(".wav") or "wav" in lower_type:
        return "audio.wav", "audio/wav"
    if lower_url.endswith(".m4a") or "mp4" in lower_type:
        return "audio.m4a", "audio/mp4"
    return "audio.mp3", "audio/mpeg"


def _normalize_positive_integer(value: Any, default: str = "1080") -> str:
    try:
        number = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return default
    return str(number) if number > 0 else default


def _normalize_number_text(value: Any, default: str) -> str:
    text = str(value or "").strip()
    try:
        float(text)
    except (TypeError, ValueError):
        return default
    return text


def _normalize_instance_type(value: Any) -> str:
    instance_type = str(value or "plus").strip().lower()
    return instance_type if instance_type in {"default", "plus"} else "plus"


def build_payload(*, image_file: str, audio_file: str, max_side: str = "1080", prompt: str = "", intensity: str = "0", scale: str = "1.05", instance_type: str = "plus") -> dict[str, Any]:
    return {
        "instanceType": instance_type if instance_type in {"default", "plus"} else "plus",
        "usePersonalQueue": False,
        "nodeInfoList": [
            {"nodeId": NODE_IMAGE, "fieldName": "image", "fieldValue": image_file},
            {"nodeId": NODE_MAX_SIDE, "fieldName": "value", "fieldValue": str(max_side)},
            {"nodeId": NODE_AUDIO, "fieldName": "audio", "fieldValue": audio_file},
            {"nodeId": NODE_PROMPT, "fieldName": "prompt", "fieldValue": prompt},
            {"nodeId": NODE_INTENSITY, "fieldName": "value", "fieldValue": str(intensity)},
            {"nodeId": NODE_SCALE, "fieldName": "value", "fieldValue": str(scale)},
        ],
    }


def _output_file(data: Mapping[str, Any]) -> str:
    nested = data.get("data") if isinstance(data.get("data"), Mapping) else {}
    value = nested.get("fileName") or nested.get("download_url") or data.get("fileName")
    if not value:
        raise InfiniteTalkTransportError("RunningHub 上传未返回文件标识")
    return str(value)


def _status(data: Mapping[str, Any]) -> str:
    raw = str(data.get("status") or "").strip().upper().replace("-", "_").replace(" ", "_")
    if raw in {"SUCCEEDED", "COMPLETED", "COMPLETE"}: return "SUCCESS"
    if raw in {"FAIL", "ERROR"}: return "FAILED"
    if raw in {"PENDING", "WAITING"}: return "QUEUED"
    if raw in {"PROCESSING", "IN_PROGRESS"}: return "RUNNING"
    if raw in {"CANCELLED", "CANCELED", "STOPPED", "TERMINATED", "ABORTED"}: return "CANCELED"
    # The supplied query plugin treats an otherwise status-less response with
    # errorCode/errorMessage as terminal failure (for example RunningHub 1004,
    # an expired task).  Do not turn that response into a 170-second timeout.
    if str(data.get("errorCode") or "").strip() or str(data.get("errorMessage") or data.get("message") or "").strip():
        return "FAILED"
    return raw


def _result(task_id: str = "", status: str = "FAILED", msg: str = "", output_url: str = "", data: Mapping[str, Any] | None = None) -> dict[str, str]:
    usage = (data or {}).get("usage") or {}
    return {
        "task_ID": str(task_id or ""), "status": status or "FAILED", "msg": msg or "",
        "output_url": output_url or "",
        "cost_coins": str(usage.get("consumeCoins") or "0"),
        "cost_time": str(usage.get("taskCostTime") or "0"),
    }


def _find_task_id(value: Any) -> str:
    """Find only explicitly named task identifiers in evolving API envelopes."""

    if isinstance(value, Mapping):
        for name in ("taskId", "task_id", "taskID"):
            candidate = str(value.get(name) or "").strip()
            if candidate:
                return candidate
        for child in value.values():
            found = _find_task_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_task_id(child)
            if found:
                return found
    return ""


def _response_shape(value: Any, depth: int = 0) -> Any:
    """Return key/type metadata only, suitable for safe API-shape diagnostics."""

    if depth >= 2:
        return type(value).__name__
    if isinstance(value, Mapping):
        return {str(key): _response_shape(child, depth + 1) for key, child in value.items()}
    if isinstance(value, list):
        return [_response_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _safe_service_message(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", "<redacted-key>", text)
    text = re.sub(r"https?://\S+", "<redacted-url>", text)
    return text[:300]


class RunningHubInfiniteTalkTransport:
    """Create and query transport for nodes 173596/137386/1178381/1693143."""

    def __init__(self, config: InfiniteTalkConfig, *, requests_module: Any | None = None, sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None) -> None:
        self.config = config
        if requests_module is None:
            import requests as requests_module  # type: ignore
        self.http = requests_module
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic

    @classmethod
    def from_env(cls) -> "RunningHubInfiniteTalkTransport":
        return cls(InfiniteTalkConfig.from_env())

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {normalize_api_key(api_key)}", "Content-Type": "application/json"}

    def _upload(self, content: bytes, filename: str, mime: str, key: str, stage: str) -> str:
        response = self.http.post(
            self.config.upload_url,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, content, mime)},
            timeout=self.config.upload_timeout,
        )
        return _output_file(_require_http(response, stage))

    def create(self, params: Any) -> dict[str, str]:
        value = _resolve(params)
        key = normalize_api_key(value.get("api_key") or self.config.api_key)
        image = str(value.get("image") or "").strip()
        audio = str(value.get("audio") or "").strip()
        if not image or not audio:
            raise InfiniteTalkValidationError("InfiniteTalk 必须提供 image 和 audio")
        image_bytes, _ = _download(self.http, image, self.config.download_timeout, "image")
        audio_bytes, audio_type = _download(self.http, audio, self.config.download_timeout, "audio")
        image_file = self._upload(_image_png(image_bytes), "image.png", "image/png", key, "upload_image")
        audio_name, audio_mime = _audio_mime(audio_bytes, audio, audio_type)
        audio_file = self._upload(audio_bytes, audio_name, audio_mime, key, "upload_audio")
        payload = build_payload(
            image_file=image_file, audio_file=audio_file,
            max_side=_normalize_positive_integer(value.get("Max_side_length"), "1080"),
            prompt=str(value.get("prompt") or "人物自然说话，身体保持稳定，配合轻微自然手势"),
            intensity=_normalize_number_text(value.get("intensity"), "0"),
            scale=_normalize_number_text(value.get("Scale"), "1.05"),
            instance_type=_normalize_instance_type(value.get("instanceType")),
        )
        response = self.http.post(self.config.run_url, headers=self._headers(key), json=payload, timeout=self.config.create_timeout)
        data = _require_http(response, "create")
        if _status(data) == "FAILED":
            code = _safe_service_message(data.get("errorCode"))
            message = _safe_service_message(data.get("errorMessage") or data.get("message"))
            raise InfiniteTalkTransportError(
                "RunningHub 创建任务失败"
                + (f"（错误码 {code}）" if code else "")
                + (f"：{message}" if message else "")
            )
        nested = data.get("data") if isinstance(data.get("data"), Mapping) else {}
        # RunningHub has returned both camelCase and snake_case task fields
        # across app versions.  Keep the original contract while accepting the
        # current API response shape.
        task_id = _find_task_id(data)
        if not task_id:
            raise InfiniteTalkTransportError(
                "RunningHub 创建响应未返回 task_ID；响应结构="
                + json.dumps(_response_shape(data), ensure_ascii=False, separators=(",", ":"))
            )
        if self.config.health_check_seconds > 0:
            self.sleep(self.config.health_check_seconds)
            health = self.query_once(task_id, key)
            state = _status(health)
            if state == "FAILED":
                return _result(task_id, "FAILED", "InfiniteTalk 任务启动失败")
            return _result(task_id, state or "RUNNING", "任务已创建，交由查询节点继续处理")
        return _result(task_id, "QUEUED", "任务已创建，交由查询节点继续处理")

    def query_once(self, task_id: str, api_key: str) -> dict[str, Any]:
        response = self.http.post(self.config.query_url, headers=self._headers(api_key), json={"taskId": task_id}, timeout=self.config.query_timeout)
        return _require_http(response, "query")

    @staticmethod
    def _pick_url(data: Mapping[str, Any]) -> str:
        results = data.get("results") or []
        for item in results:
            if isinstance(item, Mapping) and str(item.get("outputType") or "").lower() in {"mp4", "mov", "webm", "avi", "mkv"} and item.get("url"):
                return str(item["url"])
        for item in results:
            if isinstance(item, Mapping) and item.get("url"):
                return str(item["url"])
        return ""

    def query(self, params: Any) -> dict[str, str]:
        value = _resolve(params)
        key = normalize_api_key(value.get("api_key") or self.config.api_key)
        task_id = str(value.get("task_ID") or value.get("taskId") or value.get("task_id") or "").strip()
        if not task_id:
            raise InfiniteTalkValidationError("查询节点必须提供 task_ID/taskId/task_id")
        started = self.clock()
        last_state = "RUNNING"
        network_errors = 0
        while self.config.max_wait_seconds is None or self.clock() - started < self.config.max_wait_seconds:
            try:
                data = self.query_once(task_id, key)
                network_errors = 0
            except Exception as exc:
                network_errors += 1
                # 查询失败不是任务失败；只要服务没有返回明确 FAILED/CANCELED，
                # 就继续按阶段节奏轮询，避免后台已完成而本地提前放弃。
                self.sleep(self._poll_delay(self.clock() - started))
                continue
            state = _status(data)
            if state == "SUCCESS":
                return _result(task_id, state, "任务执行成功", self._pick_url(data), data)
            if state in {"FAILED", "CANCELED"}:
                message = "任务已被取消" if state == "CANCELED" else "InfiniteTalk 任务执行失败，请检查人物图片、音频和参数后重试。"
                return _result(task_id, state, message, data=data)
            if state in {"RUNNING", "QUEUED"}:
                last_state = state
            self.sleep(self._poll_delay(self.clock() - started))
        raise InfiniteTalkTransportError(f"任务在 {int(self.config.max_wait_seconds or 0)} 秒后仍为 {last_state}")

    @staticmethod
    def _poll_delay(elapsed_seconds: float) -> float:
        """按长任务阶段轮询：前3分钟60秒，3-5分钟30秒，之后10秒。"""
        if elapsed_seconds < 180:
            return 60.0
        if elapsed_seconds < 300:
            return 30.0
        return 10.0


__all__ = [
    "InfiniteTalkConfig", "InfiniteTalkTransportError", "InfiniteTalkValidationError",
    "RunningHubInfiniteTalkTransport", "api_key_fingerprint", "build_payload", "load_infinite_talk_auth_from_document", "normalize_api_key",
]
