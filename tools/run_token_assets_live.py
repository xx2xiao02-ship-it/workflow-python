"""为 Token 镜头清单准备真实首帧、Seedance 视频和火山 TTS 音频。

新版生产 TTS 只从 API 管理中心注入的 TTS API Key 读取鉴权，绝不回退到历史
《鉴权信息 .md》；其它尚未迁移的素材通道仍按各自的既有治理规则读取。输出只保存
任务结果、素材路径和必要的短期 URL，不打印、不写入密钥。数字人按当前验收要求跳过，
使用固定主角占位图。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

import requests

from workflow_1256.aishuch_transport import AishuchHTTPClient
from workflow_1256.image2_transport import (
    DEFAULT_IMAGE2_ALL_BASE_URL,
    DEFAULT_IMAGE2_ALL_MODEL,
    Image2AllHTTPClient,
    Image2HTTPError,
    Image2RequestUncertainError,
    Image2ResponseError,
    decode_image_base64,
    extract_image_result,
)
from workflow_1256.create_image_task import CreateImageTaskRequest
from workflow_1256.get_task_result import GetTaskResultRequest
from workflow_1256.ark_tts_transport import (
    DEFAULT_ARK_TTS_URL,
    DEFAULT_TTS_MODEL,
    ArkTTSConfig,
    ArkTTSHTTPTransport,
)
from workflow_1256.infinitetalk_transport import load_infinite_talk_auth_from_document
from workflow_1256.seedance_transport import (
    DEFAULT_SEEDANCE_1_0_MODEL,
    DEFAULT_SEEDANCE_1_5_MODEL,
    SeedanceConfig,
    SeedanceHTTPTransport,
)
from workflow_1256.video_generate import BACKUP_MODEL_EP, PRIMARY_MODEL_EP
from workflow_1256.voice_director_transport import load_seedance_api_keys_from_auth_document
from workflow_1256.speech_synthesis import SpeechSynthesisRequest
from workflow_1256.voice_catalog import DEFAULT_VOICE_CATALOG, VoiceCatalogError, resolve_voice_selection
from workflow_1256.voice_catalog_store import VoiceCatalogStore, VoiceCatalogStoreError
from workflow_1256.tts_audio_publisher import TOSAudioPublisher
from workflow_1256.first_frame_grid import (
    build_first_frame_grid_plan,
    persist_grid_checkpoint_handle,
    run_first_frame_grid_batch,
)


AIGC_MIN_DURATION_MS = 3_000
AIGC_MAX_DURATION_MS = 5_500
# 素材层默认按 2～5 秒锁定镜头；连续静态纠偏允许同语义视觉槽位合并到
# 最多 5.5 秒。服务商的
# duration 入参使用整数秒，因此 3.497 秒这类合法坑位必须先向上取整为 4 秒，
# 并在剪辑层按冻结时间线裁回，不得把原始 STT 坑位改成整数秒。
AIGC_PROVIDER_MIN_DURATION_SECONDS = 4
AIGC_PROVIDER_MAX_DURATION_SECONDS = 6
IMAGE2_RUNTIME_TARGETS_ENV = "API_MANAGEMENT_IMAGE2_TARGETS"
TTS_PARALLEL_WORKERS = 4


def _normalize_image2_model(value: object) -> str:
    model = str(value or "").strip()
    return DEFAULT_IMAGE2_ALL_MODEL if model in {"", "image2", "gpt-image-2"} else model


def _is_full_video_first_shot(shot: Mapping[str, Any]) -> bool:
    """只把全片锁定的 g01_s01 视为首镜，避免子集重试误放宽短镜规则。"""

    return bool(shot.get("is_first_shot")) or str(shot.get("shot_id") or "").strip() == "g01_s01"


def _provider_duration_seconds(duration_ms: int, *, allow_short: bool = False) -> int:
    """将冻结坑位向上取整为 Seedance 支持的整数秒。"""

    duration_ms = int(duration_ms)
    if (duration_ms < AIGC_MIN_DURATION_MS and not allow_short) or duration_ms > AIGC_MAX_DURATION_MS or duration_ms <= 0:
        raise ValueError(
            f"AIGC 镜头时长必须在 {AIGC_MIN_DURATION_MS / 1000:g}～"
            f"{AIGC_MAX_DURATION_MS / 1000:g} 秒窗口内，实际为 {duration_ms / 1000:g} 秒"
        )
    # 3.0～4.0 秒的镜头统一补到 4 秒；4.0～5.5 秒按向上取整处理，
    # 合并槽位最高请求 6 秒。剪辑层仍使用原始 start_us/end_us 裁回真实坑位，
    # 因此不会改变 CapCut STT 时间线。
    return min(
        AIGC_PROVIDER_MAX_DURATION_SECONDS,
        max(AIGC_PROVIDER_MIN_DURATION_SECONDS, math.ceil(duration_ms / 1000)),
    )


def _validate_aigc_shots(shots: Any) -> None:
    """阻止旧清单绕过媒体路由，把非首镜的 3 秒以下镜头送入 AIGC。"""

    if not isinstance(shots, list):
        raise ValueError("AIGC 视频清单 shots 必须是数组")
    invalid: list[str] = []
    for index, shot in enumerate(shots):
        if not isinstance(shot, Mapping):
            invalid.append(f"#{index + 1}(镜头记录不是对象)")
            continue
        shot_id = str(shot.get("shot_id") or f"#{index + 1}")
        try:
            duration_ms = int(shot.get("duration_ms"))
        except (TypeError, ValueError):
            invalid.append(f"{shot_id}(无有效时长)")
            continue
        if duration_ms < AIGC_MIN_DURATION_MS and not _is_full_video_first_shot(shot):
            invalid.append(f"{shot_id}={duration_ms}ms(<3秒，必须使用图片)")
        elif duration_ms > AIGC_MAX_DURATION_MS:
            invalid.append(f"{shot_id}={duration_ms}ms(超过5秒)")
    if invalid:
        raise ValueError(
            "AIGC 视频输入不符合 3～5.5 秒窗口：" + ", ".join(invalid)
            + "；低于 3 秒的镜头必须在媒体路由层改为 static_image，不得调用 Seedance。"
        )


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 根节点必须是对象")
    return value


def _save(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2), encoding="utf-8")


def _probe_written_audio_duration(path: Path) -> float:
    """读取已落盘音频文件的真实时长，作为时间轴契约的权威值。"""

    ffprobe = os.environ.get("FFPROBE_PATH", "").strip() or shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("TTS 音频已落盘，但本机未找到 ffprobe，无法确认实际时长")
    try:
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
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("ffprobe 无法读取已落盘 TTS 音频时长") from exc
    if result.returncode != 0:
        raise RuntimeError("ffprobe 无法解析已落盘 TTS 音频")
    try:
        duration = float(result.stdout.decode("ascii", errors="strict").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("ffprobe 未返回有效的已落盘 TTS 音频时长") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("已落盘 TTS 音频时长必须是大于 0 的有限秒数")
    return duration


def _first_runtime_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _runtime_extra_api_keys(name: str) -> list[str]:
    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _load_runtime_key_pair(
    auth_document: Path,
    loader: Callable[[Path], list[str]] | None,
    primary_names: tuple[str, ...],
    backup_names: tuple[str, ...],
    extra_env_name: str = "",
    *,
    allow_document_fallback: bool = True,
) -> list[str]:
    primary = _first_runtime_env(*primary_names)
    backup_keys = [
        value for name in backup_names
        if (value := os.environ.get(name, "").strip())
    ]
    keys = list(dict.fromkeys(
        ([primary] + backup_keys)
        if primary
        else (
            loader(auth_document)
            if allow_document_fallback and loader is not None
            else []
        )
    ))
    if extra_env_name:
        keys.extend(_runtime_extra_api_keys(extra_env_name))
    return list(dict.fromkeys(key for key in keys if key))


def _find_character(manifest: Mapping[str, Any]) -> Path:
    direct = Path(str(manifest.get("character_asset") or ""))
    if direct.is_file():
        return direct
    candidates = list(Path("outputs").rglob("protagonist_placeholder.*"))
    candidates.extend(Path(r"D:\jianying\JianYingPro Drafts").rglob("protagonist_placeholder.*"))
    for item in sorted(candidates):
        if item.is_file():
            return item
    raise FileNotFoundError("未找到主角占位图 protagonist_placeholder")


def _load_image2_key(auth_document: Path) -> str:
    configured = _first_runtime_env("IMAGE2_API_KEY", "AISHUCH_API_KEY")
    if configured:
        return configured
    raw = auth_document.read_text(encoding="utf-8")
    section_match = re.search(r"(?ims)^\s*\\?#\s*image2\s*$([\s\S]*)", raw)
    section = section_match.group(1) if section_match else raw
    match = re.search(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])", section)
    if not match:
        raise RuntimeError("鉴权文档缺少 image2 图像服务密钥")
    return match.group(0)


def _load_image2_runtime_targets(auth_document: Path) -> list[dict[str, Any]]:
    raw = os.environ.get(IMAGE2_RUNTIME_TARGETS_ENV, "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("API 管理页下发的 Image2 接入点配置不是有效 JSON") from exc
        if not isinstance(decoded, list) or not decoded:
            raise RuntimeError("API 管理页没有可用的 Image2 接入点")
        targets: list[dict[str, Any]] = []
        for index, item in enumerate(decoded, start=1):
            if not isinstance(item, Mapping):
                raise RuntimeError(f"Image2 接入点 #{index} 配置不是对象")
            channel_id = str(item.get("channel_id") or f"image-generation-{index}").strip()
            endpoint = str(item.get("endpoint") or DEFAULT_IMAGE2_ALL_BASE_URL).strip()
            keys = [str(value).strip() for value in item.get("api_keys", ()) if str(value).strip()] if isinstance(item.get("api_keys"), (list, tuple)) else []
            for field in ("primary_api_key", "backup_api_key"):
                value = str(item.get(field) or "").strip()
                if value and value not in keys: keys.append(value)
            if not endpoint or not keys:
                raise RuntimeError(f"Image2 接入点 {channel_id} 缺少 endpoint 或 API Key")
            targets.append({
                "channel_id": channel_id,
                "endpoint": endpoint,
                "model_id": _normalize_image2_model(item.get("model_id")),
                "quality": str(item.get("quality") or "low").strip().lower() or "low",
                "timeout_seconds": max(90, int(item.get("timeout_seconds") or 180)),
                "api_keys": list(dict.fromkeys(keys)),
            })
        return targets
    key = _load_image2_key(auth_document)
    return [{
        "channel_id": "legacy-image2",
        "endpoint": _first_runtime_env("IMAGE2_BASE_URL", "AISHUCH_BASE_URL") or DEFAULT_IMAGE2_ALL_BASE_URL,
        "model_id": _normalize_image2_model(_first_runtime_env("IMAGE2_MODEL") or DEFAULT_IMAGE2_ALL_MODEL),
        "quality": _first_runtime_env("IMAGE2_QUALITY") or "low",
        "timeout_seconds": 180,
        "api_keys": [key],
    }]


def _publish_reference(path: Path, auth_document: Path) -> str:
    # 服务进程启动时已经把 API 管理页的 TOS 主/备用桶注入环境；只有
    # 独立运行这个素材脚本且运行时没有完整配置时，才从历史鉴权文档补齐。
    # 不能无条件覆盖环境，否则页面刚保存的主/备用桶会被旧文档劫持。
    tos_env = {
        "ARK_TTS_TOS_ACCESS_KEY": "",
        "ARK_TTS_TOS_SECRET_KEY": "",
        "ARK_TTS_TOS_BUCKET": "",
        "ARK_TTS_TOS_ENDPOINT": "",
        "ARK_TTS_TOS_REGION": "",
    }
    if not all(os.environ.get(name, "").strip() for name in tos_env):
        tos = load_infinite_talk_auth_from_document(auth_document)
        tos_env.update({
            "ARK_TTS_TOS_ACCESS_KEY": tos["tos_access_key"],
            "ARK_TTS_TOS_SECRET_KEY": tos["tos_secret_key"],
            "ARK_TTS_TOS_BUCKET": tos["tos_bucket"],
            "ARK_TTS_TOS_ENDPOINT": tos["tos_endpoint"] or "https://tos-cn-beijing.volces.com",
            "ARK_TTS_TOS_REGION": tos["tos_region"] or "cn-beijing",
        })
        for name, value in tos_env.items():
            if value and not os.environ.get(name, "").strip():
                os.environ[name] = value
    publisher = TOSAudioPublisher.from_env()
    publisher.prefix = "1256/token-reference"
    result = publisher(
        path.read_bytes(),
        f"linxia-reference-{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}{path.suffix.lower()}",
        {"mime_type": mimetypes.guess_type(path.name)[0] or "image/jpeg", "source_name": path.name},
    )
    url = str(result.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("主角参考图发布后没有得到有效 URL")
    return url


def _image_result(response: Mapping[str, Any]) -> tuple[str, str]:
    data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
    status = str(data.get("status") or "").strip().lower()
    result = data.get("result") if isinstance(data.get("result"), Mapping) else {}
    images = result.get("images") if isinstance(result.get("images"), list) else []
    url = ""
    if images and isinstance(images[0], Mapping):
        value = images[0].get("url")
        if isinstance(value, list) and value:
            url = str(value[0])
        elif isinstance(value, str):
            url = value
    return status, url


def _image_failure_detail(response: Mapping[str, Any]) -> str:
    """提取服务商任务失败原因，避免页面只显示无法诊断的 task_id。"""
    data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
    error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
    message = str(error.get("message") or "").strip()
    code = str(error.get("code") or "").strip()
    if message and code:
        return f"{message}（{code}）"
    return message or code or "服务商未返回具体失败原因"


def _download(url: str, path: Path, minimum: int) -> None:
    response = requests.get(url, timeout=120, headers={"Accept": "*/*", "Accept-Encoding": "identity"})
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    if path.stat().st_size < minimum:
        raise RuntimeError(f"下载文件过小：{path.name}")


def _image2_sync_size(aspect_ratio: str) -> str:
    # APIMODELS 将 WxH 直接作为同步模式的尺寸；16:9 不能误发成
    # 1536x1024（3:2），否则服务商会按错误画幅生成首帧。
    return {
        "1:1": "1024x1024",
        "16:9": "1280x720",
        "9:16": "720x1280",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
    }.get(str(aspect_ratio or "9:16"), "720x1280")


def _image2_http_status_label(status_code: int) -> str:
    return {
        401: "鉴权失败",
        402: "余额不足或额度不足",
        403: "权限/来源被拒绝",
        429: "服务商限流",
    }.get(int(status_code or 0), "服务商 HTTP 错误" if int(status_code or 0) >= 400 else "")


def _image2_quality_escalation(start_quality: object) -> tuple[str, ...]:
    """返回一次创建请求允许使用的质量档位顺序。

    Image2 的质量降级/升档只发生在创建接口明确失败后。异步任务一旦拿到
    task_id，查询链路不会再次调用本函数，也不会重新创建任务。用户约定的
    自动升档顺序是 low -> auto -> high；medium 仍作为兼容的单档配置保留，
    不擅自插入该顺序。
    """

    quality = str(start_quality or "low").strip().lower() or "low"
    if quality == "low":
        return ("low", "auto", "high")
    if quality == "auto":
        return ("auto", "high")
    if quality == "high":
        return ("high",)
    if quality == "medium":
        return ("medium",)
    raise RuntimeError("Image2 quality 必须是 low、medium、auto 或 high")


def _image2_create_with_failover(request: Mapping[str, Any], targets: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    quality_attempts: list[str] = []
    requested_mode = str(request.get("image2_mode") or "sync").strip().lower()
    async_mode = requested_mode in {"async", "asynchronous", "task", "native_async"}
    for target in targets:
        channel_id = str(target.get("channel_id") or "未命名接入点")
        endpoint = str(target.get("endpoint") or DEFAULT_IMAGE2_ALL_BASE_URL).strip()
        model_id = _normalize_image2_model(target.get("model_id"))
        # 请求级质量（若存在）优先；否则沿用 API 管理页该接入点的配置。
        # 这样默认 low 会自动升档，同时保留 auto/high/medium 的人工选择。
        start_quality = str(request.get("quality") or target.get("quality") or "low").strip().lower() or "low"
        quality_plan = _image2_quality_escalation(start_quality)
        # GPT Image 2 All 的同步兼容网关可能在已返回 200 头后继续保持连接，
        # 文档要求客户端至少等待 180 秒；异步提交同样不能用过短的读取窗口，
        # 否则会在 taskId 尚未返回前把一次已提交请求误判为未知状态。
        timeout_seconds = max(180, int(target.get("timeout_seconds") or 180))
        raw_keys = target.get("api_keys")
        keys = [str(value).strip() for value in raw_keys if str(value).strip()] if isinstance(raw_keys, (list, tuple)) else []
        for quality in quality_plan:
            quality_attempts.append(quality)
            for key_index, key in enumerate(keys, start=1):
                try:
                    client = Image2AllHTTPClient(key, base_url=endpoint, model_id=model_id, timeout=timeout_seconds)
                    response = client.create_image(
                        prompt=str(request.get("prompt") or ""),
                        # 文档规定：带 size 是同步；不带 size、改传
                        # aspect_ratio + resolution 才会返回可持久化 taskId。
                        size="" if async_mode else str(request.get("size") or _image2_sync_size(str(request.get("aspect_ratio") or "9:16"))),
                        quality=quality,
                        aspect_ratio=str(request.get("aspect_ratio") or "") or None,
                        resolution=str(request.get("resolution") or "1K"),
                        n=int(request.get("n") or 1),
                        image_urls=[str(value) for value in request.get("image_urls", ()) if str(value).strip()],
                    )
                    result = extract_image_result(response)
                    if result.get("error") and result.get("task_id"):
                        # APIMODELS 文档明确说明：同步请求可能已经发送 200
                        # 响应头，随后在响应体中报告失败并附 task_id。此时请求
                        # 已有明确的服务商句柄，不能切换 Key 或质量再 POST，避免重复计费。
                        raise Image2ResponseError(
                            f"{channel_id}/key{key_index}: Image2 响应体报告失败："
                            f"{result.get('error')}（task_id={result.get('task_id')}）",
                            task_id=str(result.get("task_id") or ""),
                        )
                    if result.get("task_id") and str(result.get("state") or "").lower() in {"failed", "error", "cancelled", "canceled"}:
                        # 已经拿到供应商任务句柄时，即使响应直接标记为终态失败，
                        # 也不再创建另一个质量任务；否则一次任务可能被重复计费。
                        raise Image2ResponseError(
                            f"{channel_id}/key{key_index}: Image2 任务创建后立即失败："
                            f"{result.get('error') or result.get('state')}（task_id={result.get('task_id')}）",
                            task_id=str(result.get("task_id") or ""),
                        )
                    if (
                        result["image_urls"]
                        or result["image_b64"]
                        or (
                            result.get("task_id")
                            and not result.get("error")
                            and str(result.get("state") or "").lower()
                            not in {"failed", "error", "cancelled", "canceled"}
                        )
                    ):
                        # Preserve the provider response and safe response
                        # headers for immediate durable auditing.  Secrets are
                        # excluded by the header allow-list at persistence.
                        result["response_body"] = dict(response)
                        raw_headers = getattr(response, "response_headers", {})
                        if isinstance(raw_headers, Mapping):
                            result["response_headers"] = dict(raw_headers)
                        result.update(
                            channel_id=channel_id,
                            model_id=model_id,
                            quality=quality,
                            quality_attempts=list(quality_attempts),
                            endpoint=endpoint,
                            key_index=key_index,
                            image_task_mode="asynchronous" if async_mode and not (result["image_urls"] or result["image_b64"]) else "synchronous",
                            query_context={
                                "channel_id": channel_id,
                                "endpoint": endpoint,
                                "model_id": model_id,
                                "key_index": key_index,
                            },
                        )
                        return result
                    failures.append(f"{channel_id}/key{key_index}/quality={quality}: {result.get('state') or 'unknown'} {result.get('error') or '响应未返回图片'}".strip())
                except Image2RequestUncertainError as exc:
                    # 连接在服务商已返回响应头后断开时，task_id 可以继续查询；
                    # 没有 task_id 也不能切换下一个 Key 或质量，因为 POST 可能已经扣费。
                    if exc.task_id:
                        return {
                            "task_id": exc.task_id,
                            "state": "submitted",
                            "status": "submitted",
                            "image_urls": [],
                            "image_b64": [],
                            "error": str(exc),
                            "channel_id": channel_id,
                            "model_id": model_id,
                            "quality": quality,
                            "quality_attempts": list(quality_attempts),
                            "endpoint": endpoint,
                            "key_index": key_index,
                            "image_task_mode": "asynchronous" if async_mode else "synchronous_recoverable",
                            "query_context": {
                                "channel_id": channel_id,
                                "endpoint": endpoint,
                                "model_id": model_id,
                                "key_index": key_index,
                            },
                            "response_headers": dict(exc.response_headers or {}),
                        }
                    raise RuntimeError(
                        f"{channel_id}/key{key_index}: Image2 请求状态不确定，已停止切换鉴权和质量，避免重复付费"
                    ) from exc
                except Image2ResponseError:
                    # 响应体已有明确失败与 task_id；禁止落入 failover。
                    raise
                except Image2HTTPError as exc:
                    if getattr(exc, "task_id", ""):
                        # HTTP 错误体若同时给出了 task_id，同样属于已提交请求，
                        # 禁止切换质量或鉴权重发。
                        raise Image2ResponseError(
                            f"{channel_id}/key{key_index}: Image2 HTTP 响应包含任务句柄："
                            f"HTTP {int(getattr(exc, 'status_code', 0) or 0)}（task_id={exc.task_id}）",
                            task_id=str(exc.task_id),
                        ) from exc
                    status_code = int(getattr(exc, "status_code", getattr(exc, "code", 0)) or 0)
                    failures.append(
                        f"{channel_id}/key{key_index}/quality={quality}: HTTP {status_code}（{_image2_http_status_label(status_code)}）"
                    )
                except Exception as exc:
                    failures.append(f"{channel_id}/key{key_index}/quality={quality}: {type(exc).__name__}: {str(exc)[:240]}")
    mode_label = "异步创建" if async_mode else "同步调用"
    raise RuntimeError("GPT Image 2 All " + mode_label + "失败：" + ("；".join(failures) or "没有有效的 Image2 接入点"))


def _image2_query_with_failover(
    request: Mapping[str, Any],
    targets: list[Mapping[str, Any]],
    *,
    deadline_seconds: int = 360,
) -> dict[str, Any]:
    """查询 APIMODELS 原生异步任务，绝不创建新任务。"""

    task_id = str(request.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("Image2 查询缺少 task_id")
    preferred_channel = str(request.get("channel_id") or "").strip()
    preferred_key_index = str(request.get("key_index") or "").strip()
    ordered_targets = list(targets)
    if preferred_channel:
        ordered_targets.sort(
            key=lambda item: 0 if str(item.get("channel_id") or "").strip() == preferred_channel else 1
        )
    failures: list[str] = []
    for target in ordered_targets:
        channel_id = str(target.get("channel_id") or "未命名接入点")
        endpoint = str(target.get("endpoint") or DEFAULT_IMAGE2_ALL_BASE_URL).strip()
        model_id = _normalize_image2_model(target.get("model_id"))
        quality = str(target.get("quality") or "low").strip().lower() or "low"
        # 查询只复用原 taskId，不创建新任务；给供应商网关足够读取时间，
        # 网络抖动时由下方同一 taskId 的退避轮询继续处理。
        timeout_seconds = max(180, int(target.get("timeout_seconds") or 180))
        raw_keys = target.get("api_keys")
        keys = [str(value).strip() for value in raw_keys if str(value).strip()] if isinstance(raw_keys, (list, tuple)) else []
        if preferred_channel == channel_id and preferred_key_index.isdigit():
            index = int(preferred_key_index) - 1
            if 0 <= index < len(keys):
                keys = [keys[index]] + [value for pos, value in enumerate(keys) if pos != index]
        for key_index, key in enumerate(keys, start=1):
            try:
                client = Image2AllHTTPClient(
                    key,
                    base_url=endpoint,
                    model_id=model_id,
                    timeout=timeout_seconds,
                )
                deadline = time.monotonic() + max(60, int(deadline_seconds))
                uncertain_attempts = 0
                while True:
                    try:
                        result = extract_image_result(client.get_task(task_id))
                    except (Image2RequestUncertainError, ConnectionError, TimeoutError) as exc:
                        # 查询请求本身不产生费用。网络抖动时保留同一 task_id
                        # 等待后重查，避免供应商已完成而本地误报失败；连续两次
                        # 仍无法确认才升级为阻断，绝不重新 POST 创建任务。
                        uncertain_attempts += 1
                        remaining = deadline - time.monotonic()
                        if uncertain_attempts < 2 and remaining > 0:
                            time.sleep(min(15.0, remaining))
                            continue
                        raise Image2RequestUncertainError(str(exc)) from exc
                    uncertain_attempts = 0
                    if result["image_urls"] or result["image_b64"]:
                        result.update(
                            channel_id=channel_id,
                            model_id=model_id,
                            quality=quality,
                            endpoint=endpoint,
                            key_index=key_index,
                            image_task_mode="asynchronous",
                        )
                        return result
                    state = str(result.get("state") or result.get("status") or "").strip().lower()
                    if state in {"failed", "error", "cancelled", "canceled"} or result.get("error"):
                        raise RuntimeError(
                            f"Image2 任务 {task_id} 失败：{result.get('error') or state or '服务商未返回原因'}"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Image2 任务查询超时：{task_id}")
                    # 原生异步协议的标准轮询节奏：创建后每 10 秒只查询同一
                    # task_id，直到返回 URL/b64 或明确失败；绝不在查询环节重建。
                    time.sleep(min(10.0, remaining))
            except Image2HTTPError as exc:
                failures.append(
                    f"{channel_id}/key{key_index}: HTTP {int(getattr(exc, 'status_code', getattr(exc, 'code', 0)) or 0)}（{_image2_http_status_label(int(getattr(exc, 'status_code', getattr(exc, 'code', 0)) or 0))}）"
                )
            except Image2RequestUncertainError as exc:
                # GET 查询本身不应触发付费重试；直接保留明确诊断。
                raise RuntimeError(f"Image2 任务查询状态不确定：{task_id}；{exc}") from exc
            except RuntimeError:
                raise
            except Exception as exc:
                failures.append(f"{channel_id}/key{key_index}: {type(exc).__name__}: {str(exc)[:240]}")
    raise RuntimeError(
        f"Image2 任务 {task_id} 查询失败：" + ("；".join(failures) or "没有有效的 Image2 查询接入点")
    )


def _write_image2_result(result: Mapping[str, Any], path: Path) -> str:
    urls = result.get("image_urls")
    if isinstance(urls, list) and urls and str(urls[0]).startswith(("http://", "https://")):
        _download(str(urls[0]), path, 5_000)
        return str(urls[0])
    values = result.get("image_b64")
    if isinstance(values, list) and values:
        data = decode_image_base64(str(values[0]))
        if not data: raise RuntimeError("GPT Image 2 All 返回空图片")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"inline://{path.name}"
    raise RuntimeError("GPT Image 2 All 返回中没有可保存的图片 URL 或 b64_json")


def _generate_images_sync(manifest: Mapping[str, Any], auth_document: Path, out_dir: Path, targets: list[dict[str, Any]], *, upload_reference: bool = False, progress_reporter: Callable[[Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    reference_url = ""
    if upload_reference:
        reference_url = _publish_reference(_find_character(manifest), auth_document)
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots: raise ValueError("manifest 缺少 shots")
    def report(stage: str, submitted: int, completed: int, message: str) -> None:
        if progress_reporter is not None: progress_reporter({"stage": stage, "total": len(shots), "submitted": submitted, "completed": completed, "message": message})
    def create(index: int) -> tuple[int, dict[str, Any]]:
        shot = shots[index]
        return index, _image2_create_with_failover({"prompt": str(shot["first_frame_prompt"]), "size": _image2_sync_size(str(manifest.get("aspect_ratio") or "9:16")), "aspect_ratio": str(manifest.get("aspect_ratio") or "9:16"), "resolution": "1K", "n": 1, "image_urls": [reference_url] if reference_url else []}, targets)
    results: list[dict[str, Any] | None] = [None] * len(shots)
    with ThreadPoolExecutor(max_workers=min(4, len(shots))) as executor:
        futures = [executor.submit(create, index) for index in range(len(shots))]
        submitted = 0
        for future in as_completed(futures):
            index, value = future.result(); results[index] = value; submitted += 1
            report("submitted", submitted, submitted, f"GPT Image 2 All 已同步完成 {submitted}/{len(shots)} 张")
    urls = [""] * len(shots); task_ids = [""] * len(shots); access_points = [""] * len(shots); local_paths: list[str] = []
    for index, value in enumerate(results, start=1):
        if value is None: raise RuntimeError(f"第 {index} 个首帧没有同步结果")
        if (
            not value.get("image_urls")
            and not value.get("image_b64")
            and str(value.get("task_id") or "").strip()
        ):
            # 同步连接在响应体读取阶段断开但保留了服务商 taskId 时，
            # 直接走文档规定的 GET 查询，不能重新 POST。
            value = _image2_query_with_failover(value, targets)
        path = out_dir / "images" / f"first_frame_{index:02d}.jpg"; urls[index - 1] = _write_image2_result(value, path)
        task_ids[index - 1] = str(value.get("task_id") or "") or f"sync-image2-all-{index:02d}"
        access_points[index - 1] = str(value.get("channel_id") or ""); local_paths.append(str(path.resolve())); report("completed", index, index, f"首帧已落盘 {index}/{len(shots)}")
    return {"image_task_ids": task_ids, "image_urls": urls, "image_paths": local_paths, "reference_url": reference_url, "image_task_mode": "synchronous", "image_model": DEFAULT_IMAGE2_ALL_MODEL, "image_access_points": access_points}


def _generate_images_legacy(
    manifest: Mapping[str, Any],
    auth_document: Path,
    out_dir: Path,
    *,
    upload_reference: bool = False,
    progress_reporter: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """并行创建并轮询首帧任务，按原始 shots 顺序返回结果。

    ``progress_reporter`` 仅报告运行态，不改变返回契约；调用方可用它显示
    已提交、轮询中和已完成数量，避免整批任务静默等待。
    """
    key = _load_image2_key(auth_document)
    client = AishuchHTTPClient(key, timeout=30)
    reference_url = ""
    if upload_reference:
        reference = _find_character(manifest)
        reference_url = _publish_reference(reference, auth_document)
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("manifest 缺少 shots")

    def report(stage: str, *, submitted: int, completed: int, message: str) -> None:
        if progress_reporter is not None:
            progress_reporter({
                "stage": stage,
                "total": len(shots),
                "submitted": submitted,
                "completed": completed,
                "message": message,
            })

    def create(index: int) -> tuple[int, str]:
        shot = shots[index]
        response = client.create_image(CreateImageTaskRequest(
            api_key="",
            prompt=str(shot["first_frame_prompt"]),
            n=1,
            size="9:16",
            resolution="1k",
            image_urls=[reference_url] if reference_url else [],
            official_fallback=False,
        ))
        data = response.get("data") if isinstance(response, Mapping) else None
        task_id = ""
        if isinstance(data, list) and data and isinstance(data[0], Mapping):
            task_id = str(data[0].get("task_id") or "")
        if not task_id:
            raise RuntimeError(f"{shot['shot_id']} 首帧任务没有返回 task_id")
        return index, task_id

    task_ids = [""] * len(shots)
    with ThreadPoolExecutor(max_workers=min(4, len(shots))) as executor:
        futures = [executor.submit(create, index) for index in range(len(shots))]
        submitted = 0
        for future in as_completed(futures):
            index, task_id = future.result()
            task_ids[index] = task_id
            submitted += 1
            report("submitted", submitted=submitted, completed=0, message=f"已并行提交 {submitted}/{len(shots)} 个 Image 2 任务")

    urls = [""] * len(shots)
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline and not all(urls):
        pending = [index for index, task_id in enumerate(task_ids) if task_id and not urls[index]]
        report("polling", submitted=len(shots), completed=sum(bool(url) for url in urls), message=f"正在并行轮询 {len(pending)} 个 Image 2 任务")

        def query(index: int) -> tuple[int, str, str]:
            response = client.get_task(GetTaskResultRequest(task_id=task_ids[index], api_key=""))
            status, url = _image_result(response)
            return index, status, url

        # 创建与查询均限制在 4 路，避免瞬时请求过多，同时不会逐镜阻塞。
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
            futures = [executor.submit(query, index) for index in pending]
            for future in as_completed(futures):
                index, status, url = future.result()
                if status in {"completed", "success", "succeeded"} and url.startswith(("http://", "https://")):
                    urls[index] = url
                    completed = sum(bool(item) for item in urls)
                    report("completed", submitted=len(shots), completed=completed, message=f"首帧已完成 {completed}/{len(shots)}，其余任务继续并行生成")
                elif status in {"failed", "error", "cancelled", "canceled"}:
                    raise RuntimeError(f"{shots[index]['shot_id']} 首帧生成失败")
        if not all(urls):
            time.sleep(15)
    if not all(urls):
        raise TimeoutError("首帧任务未在 360 秒内全部完成")

    local_paths: list[str] = []
    for index, url in enumerate(urls, start=1):
        path = out_dir / "images" / f"first_frame_{index:02d}.jpg"
        _download(url, path, 5_000)
        local_paths.append(str(path.resolve()))
    return {"image_task_ids": task_ids, "image_urls": urls, "image_paths": local_paths, "reference_url": reference_url}


def generate_images(
    manifest: Mapping[str, Any],
    auth_document: Path,
    out_dir: Path,
    *,
    upload_reference: bool = False,
    progress_reporter: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    # Image2 All 的创建、task_id 查询和结果下载是唯一生产入口。
    # API 管理页配置优先；独立脚本没有注入运行时配置时，才从明确的
    # Image2 鉴权文档读取同一接入点，绝不静默切换到旧 /tasks 链路。
    return _generate_images_sync(
        manifest,
        auth_document,
        out_dir,
        _load_image2_runtime_targets(auth_document),
        upload_reference=upload_reference,
        progress_reporter=progress_reporter,
    )


def _generate_images_as_grids_sync(manifest: Mapping[str, Any], auth_document: Path, out_dir: Path, targets: list[dict[str, Any]], *, grid_layout: str = "3x3", progress_reporter: Callable[[Mapping[str, Any]], None] | None = None, checkpoint_path: str | Path | None = None, resume: bool = False, publish: bool = True) -> dict[str, Any]:
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots: raise ValueError("manifest 缺少 shots")
    plan = build_first_frame_grid_plan(shots, cells_per_grid=9 if grid_layout == "3x3" else 4, grid_layout=grid_layout, aspect_ratio=str(manifest.get("aspect_ratio") or "9:16"))
    total = len(plan["batches"]); counters = {"submitted": 0, "completed": 0}; lock = threading.Lock(); response_store: dict[str, dict[str, Any]] = {}; result_counter = 0
    def persist_handle(request: Mapping[str, Any], value: Mapping[str, Any], *, status: str, image_url: str = "") -> None:
        if checkpoint_path is None:
            return
        grid_id = str(request.get("grid_id") or "").strip()
        if not grid_id:
            return
        record: dict[str, Any] = {"grid_id": grid_id, "status": status}
        task_id = str(value.get("task_id") or "").strip()
        if task_id:
            record["task_id"] = task_id
        if image_url:
            record["image_url"] = image_url
        grid_record = value.get("grid_record")
        if isinstance(grid_record, Mapping):
            record["task_record"] = dict(grid_record)
        for key in ("channel_id", "endpoint", "model_id", "key_index"):
            if value.get(key) not in (None, ""):
                record[key] = value.get(key)
        if isinstance(value.get("response_body"), Mapping):
            record["response_body"] = dict(value["response_body"])
        if isinstance(value.get("response_headers"), Mapping):
            record["response_headers"] = {
                str(k).lower(): str(v)
                for k, v in value["response_headers"].items()
                if str(k).lower() in {"x-apimodels-task-id", "content-type", "x-request-id"}
            }
        persist_grid_checkpoint_handle(checkpoint_path, grid_id, record)
    def report(stage: str, message: str) -> None:
        if progress_reporter is not None:
            # 调用方可能正在持有 lock 更新计数；这里不能再次获取同一把
            # 非可重入锁，否则创建接口返回 task_id 后会死锁，句柄无法
            # 写入 checkpoint，后续也就永远不会进入查询链路。
            progress_reporter({"stage": stage, "total": total, "submitted": counters["submitted"], "completed": counters["completed"], "message": message})
    def create(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal result_counter
        value = _image2_create_with_failover(
            {
                "prompt": str(request["prompt"]),
                # 首帧宫格使用文档定义的原生异步模式：不发送 size，
                # 仅发送 aspect_ratio + resolution，先把 taskId 写入断点。
                "size": "",
                "image2_mode": "async",
                "aspect_ratio": str(manifest.get("aspect_ratio") or "9:16"),
                "resolution": "1K",
                "n": 1,
                "image_urls": [str(item) for item in request.get("image_urls", ()) if str(item).strip()],
            },
            targets,
        )
        # 服务商有时会在异步入口直接返回图片；仍支持该合法结果，
        # 但只把真正的 task_id 交给查询链路，不能制造本地伪句柄。
        with lock:
            result_counter += 1
            result_handle = f"sync-image2-result-{result_counter:03d}"
            response_store[result_handle] = value
        urls = value.get("image_urls")
        if isinstance(urls, list) and urls and str(urls[0]).startswith(("http://", "https://")):
            image_ref = str(urls[0])
        elif isinstance(value.get("image_b64"), list) and value["image_b64"]:
            # 原生异步端点偶尔会直接返回 b64。先落盘再写断点，重启后可以
            # 直接裁切已完成结果，无需再次调用付费创建接口。
            provider_path = out_dir / "grids" / ".provider" / f"{request.get('grid_id') or result_handle}.jpg"
            provider_path.parent.mkdir(parents=True, exist_ok=True)
            provider_path.write_bytes(decode_image_base64(str(value["image_b64"][0])))
            image_ref = provider_path.resolve().as_uri()
        elif str(value.get("task_id") or "").strip():
            task_id = str(value["task_id"]).strip()
            with lock:
                counters["submitted"] += 1
                report("submitted", f"GPT Image 2 All 已提交 {counters['submitted']}/{total} 个可恢复宫格任务")
            persist_handle(request, value, status="submitted")
            return {
                "task_id": task_id,
                "status": str(value.get("state") or "submitted"),
                "channel_id": value.get("channel_id"),
                "endpoint": value.get("endpoint"),
                "model_id": value.get("model_id"),
                "key_index": value.get("key_index"),
                "grid_record": {
                    "grid_id": str(request.get("grid_id") or ""),
                    "task_id": task_id,
                    "status": "submitted",
                    "mode": "asynchronous",
                    "channel_id": str(value.get("channel_id") or ""),
                    "key_index": int(value.get("key_index") or 0),
                    "endpoint": str(value.get("endpoint") or ""),
                },
            }
        else:
            raise RuntimeError(f"{request.get('grid_id') or '宫格'} Image2 响应没有图片或可恢复 task_id")
        with lock:
            counters["submitted"] += 1; report("submitted", f"GPT Image 2 All 已同步完成 {counters['submitted']}/{total} 个宫格")
        persist_handle(request, value, status="completed", image_url=image_ref)
        return {"image_url": image_ref, "status": "completed", "channel_id": value.get("channel_id"), "grid_record": {"grid_id": str(request.get("grid_id") or ""), "status": "completed", "mode": "synchronous", "channel_id": str(value.get("channel_id") or "")}}

    def query(request: Mapping[str, Any]) -> Mapping[str, Any]:
        value = _image2_query_with_failover(request, targets)
        image_urls = value.get("image_urls")
        image_b64 = value.get("image_b64")
        if isinstance(image_urls, list) and image_urls and str(image_urls[0]).startswith(("http://", "https://")):
            image_url = str(image_urls[0])
        elif isinstance(image_b64, list) and image_b64:
            # 查询结果若以 b64 返回，也先落盘为稳定的本地断点；不把
            # 大段 b64 写入任务 JSON。
            provider_path = out_dir / "grids" / ".provider" / f"{str(request.get('task_id') or 'image2-task')}.jpg"
            provider_path.parent.mkdir(parents=True, exist_ok=True)
            provider_path.write_bytes(decode_image_base64(str(image_b64[0])))
            image_url = provider_path.resolve().as_uri()
        else:
            raise RuntimeError(f"Image2 任务 {request.get('task_id') or ''} 已完成但没有图片结果")
        with lock:
            counters["completed"] += 1
            report("completed", f"已查询完成 {counters['completed']}/{total} 个宫格，正在裁切回填镜头")
        return {
            "status": str(value.get("state") or value.get("status") or "completed"),
            "image_url": image_url,
            "task_id": str(value.get("task_id") or request.get("task_id") or ""),
            "channel_id": value.get("channel_id"),
        }
    def materialize(image_url: str, batch: Mapping[str, Any], output_dir: str | Path) -> Path:
        path = Path(output_dir) / "grids" / f"{batch['grid_id']}.jpg"
        if image_url.startswith("file://"):
            local_text = unquote(urlparse(image_url).path or "")
            if os.name == "nt" and local_text.startswith("/") and len(local_text) > 2 and local_text[2] == ":":
                local_text = local_text[1:]
            local_source = Path(local_text)
            if not local_source.is_file():
                raise RuntimeError(f"同步 Image2 本地结果不存在：{local_source}")
            path.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(local_source, path)
        elif image_url.startswith("inline://"):
            result_handle = image_url[len("inline://"):]
            value = response_store.get(result_handle) or {}; values = value.get("image_b64")
            if not isinstance(values, list) or not values: raise RuntimeError(f"同步 Image2 b64 结果不存在：{result_handle}")
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(decode_image_base64(str(values[0])))
        else: _download(image_url, path, 5_000)
        with lock: counters["completed"] += 1; report("completed", f"宫格已落盘，正在裁切 {counters['completed']}/{total}")
        return path
    result = run_first_frame_grid_batch(shots, create_runner=create, query_runner=query, materialize_runner=materialize, output_dir=out_dir, cells_per_grid=9 if grid_layout == "3x3" else 4, grid_layout=grid_layout, aspect_ratio=str(manifest.get("aspect_ratio") or "9:16"), max_workers=4, checkpoint_path=checkpoint_path, resume=resume)
    paths = [str(item) for item in result["image_url_list"] if item]
    # 显式关闭发布时到此结束：不把裁切后的宫格产物转交 TOS，也不触发
    # BGM/音频发布分支。画风参考图是否需要发布由素材层在创建前单独处理。
    # 完整素材模式仍需把裁切结果发布成公网 URL 供 Seedance 等后续分支使用。
    urls = publish_existing_grid_images(
        paths,
        auth_document,
        out_dir / "published_grid_images.json",
        progress_reporter=progress_reporter,
    ) if publish else []
    return {"image_paths": paths, "image_urls": urls, "grid_batches": result["grid_batches"], "review_summary": result.get("review_summary", {}), "grid_task_records": result["task_records"], "grid_config": result["grid_config"], "image_task_mode": "asynchronous", "image_model": DEFAULT_IMAGE2_ALL_MODEL, "image_access_points": [str(item.get("channel_id") or "") for item in response_store.values() if str(item.get("channel_id") or "")]}


def generate_images_as_grids(manifest: Mapping[str, Any], auth_document: Path, out_dir: Path, *, grid_layout: str = "3x3", progress_reporter: Callable[[Mapping[str, Any]], None] | None = None, checkpoint_path: str | Path | None = None, resume: bool = False, publish: bool = True) -> dict[str, Any]:
    # 宫格首帧同样必须使用原生异步 Image2 契约：POST 创建返回 task_id，
    # 随后由同一 task_id 的 GET 查询直到返回图片。旧 Aishuch /tasks
    # 实现仅作为历史代码保留，不再由生产入口自动调用。
    return _generate_images_as_grids_sync(
        manifest,
        auth_document,
        out_dir,
        _load_image2_runtime_targets(auth_document),
        grid_layout=grid_layout,
        progress_reporter=progress_reporter,
        checkpoint_path=checkpoint_path,
        resume=resume,
        publish=publish,
    )


def _generate_images_as_grids_legacy(
    manifest: Mapping[str, Any],
    auth_document: Path,
    out_dir: Path,
    *,
    grid_layout: str = "3x3",
    progress_reporter: Callable[[Mapping[str, Any]], None] | None = None,
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """用项目既有宫格编排生成首帧并裁切为逐镜图片，数字人镜头由计划器跳过。"""
    key = _load_image2_key(auth_document)
    client = AishuchHTTPClient(key, timeout=30)
    shots = manifest.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("manifest 缺少 shots")
    plan = build_first_frame_grid_plan(
        shots,
        cells_per_grid=9 if grid_layout == "3x3" else 4,
        grid_layout=grid_layout,
        aspect_ratio=str(manifest.get("aspect_ratio") or "9:16"),
    )
    total = len(plan["batches"])
    lock = threading.Lock()
    submitted = 0
    completed = 0

    def report(stage: str, message: str) -> None:
        if progress_reporter is not None:
            progress_reporter({
                "stage": stage,
                "total": total,
                "submitted": submitted,
                "completed": completed,
                "message": message,
            })

    def persist_handle(request: Mapping[str, Any], *, status: str, task_id: str = "", image_url: str = "", task_record: Mapping[str, Any] | None = None) -> None:
        if checkpoint_path is None:
            return
        grid_id = str(request.get("grid_id") or "").strip()
        if not grid_id:
            return
        record: dict[str, Any] = {"grid_id": grid_id, "status": status}
        if task_id:
            record["task_id"] = task_id
        if image_url:
            record["image_url"] = image_url
        if isinstance(task_record, Mapping):
            record["task_record"] = dict(task_record)
        persist_grid_checkpoint_handle(checkpoint_path, grid_id, record)

    def create(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal submitted
        response = client.create_image(CreateImageTaskRequest(api_key="", prompt=str(request["prompt"]), n=1, size=str(request["size"]), resolution="1k", image_urls=[str(v) for v in request.get("image_urls", [])], official_fallback=False))
        data = response.get("data") if isinstance(response, Mapping) else None
        task_id = str(data[0].get("task_id") or "") if isinstance(data, list) and data and isinstance(data[0], Mapping) else ""
        if not task_id:
            raise RuntimeError("宫格首帧任务没有返回 task_id")
        with lock:
            submitted += 1
            report("submitted", f"已并行提交 {submitted} 个宫格首帧任务")
        persist_handle(request, status="submitted", task_id=task_id)
        return {"task_id": task_id}

    def query(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal completed
        deadline = time.monotonic() + 360
        task_id = str(request["task_id"])
        while time.monotonic() < deadline:
            response = client.get_task(GetTaskResultRequest(task_id=task_id, api_key=""))
            status, url = _image_result(response)
            if status in {"completed", "success", "succeeded"} and url.startswith(("http://", "https://")):
                with lock:
                    completed += 1
                    report("completed", f"已完成 {completed} 个宫格首帧任务，正在裁切回填镜头")
                return {"status": status, "image_url": url}
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(
                    f"宫格首帧任务失败：{task_id}；服务商原因：{_image_failure_detail(response)}"
                )
            with lock:
                report("polling", f"正在并行轮询宫格首帧任务，已完成 {completed}/{submitted}")
            time.sleep(8)
        raise TimeoutError(f"宫格首帧任务超时：{task_id}")

    def materialize(image_url: str, batch: Mapping[str, Any], output_dir: str | Path) -> Path:
        path = Path(output_dir) / "grids" / f"{batch['grid_id']}.jpg"
        _download(image_url, path, 5_000)
        return path

    result = run_first_frame_grid_batch(
        shots,
        create_runner=create,
        query_runner=query,
        materialize_runner=materialize,
        output_dir=out_dir,
        cells_per_grid=9 if grid_layout == "3x3" else 4,
        grid_layout=grid_layout,
        aspect_ratio=str(manifest.get("aspect_ratio") or "9:16"),
        max_workers=4,
        checkpoint_path=checkpoint_path,
        resume=resume,
    )
    paths = [str(item) for item in result["image_url_list"] if item]
    # 宫格裁切结果必须重新发布，才能作为 Seedance 的逐镜首帧 URL。
    urls = publish_existing_grid_images(
        paths,
        auth_document,
        out_dir / "published_grid_images.json",
        progress_reporter=progress_reporter,
    )
    return {"image_paths": paths, "image_urls": urls, "grid_batches": result["grid_batches"], "grid_task_records": result["task_records"], "grid_config": result["grid_config"]}


def publish_existing_grid_images(
    image_paths: list[str | Path],
    auth_document: Path,
    state_path: Path,
    *,
    force_refresh: bool = False,
    progress_reporter: Callable[[Mapping[str, Any]], None] | None = None,
) -> list[str]:
    """将已裁切的首帧断点续传到 TOS，避免超时后重复调用 Image2。"""
    paths = [Path(value).resolve() for value in image_paths]
    if not paths or not all(path.is_file() for path in paths):
        raise ValueError("待发布的宫格裁切首帧不存在")
    state = _load(state_path) if state_path.is_file() else {}
    previous_paths = state.get("image_paths")
    urls = state.get("image_urls")
    if previous_paths != [str(path) for path in paths] or not isinstance(urls, list) or len(urls) != len(paths):
        urls = [""] * len(paths)
    for index, path in enumerate(paths):
        # TOS 临时 URL 会过期；参考图再次执行素材任务时必须重新发布，
        # 否则 Image2/Seedance 可能收到旧 URL 并返回 404/410。
        if force_refresh or not str(urls[index]).startswith(("http://", "https://")):
            urls[index] = _publish_reference(path, auth_document)
            _save(state_path, {"image_paths": [str(item) for item in paths], "image_urls": urls})
        if progress_reporter is not None:
            progress_reporter({
                "stage": "publishing",
                "total": len(paths),
                "completed": sum(bool(item) for item in urls),
                "message": f"已发布 {sum(bool(item) for item in urls)}/{len(paths)} 张裁切首帧",
            })
    return [str(item) for item in urls]


def generate_videos(
    manifest: Mapping[str, Any],
    state: Mapping[str, Any],
    auth_document: Path,
    out_dir: Path,
    *,
    progress_reporter: Callable[[Mapping[str, Any]], None] | None = None,
    task_created_reporter: Callable[[Mapping[str, Any]], None] | None = None,
    existing_records: list[Mapping[str, Any]] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """并行生成视频，并按镜头保留成功、失败与查询证据。

    ``resume=True`` 时只查询已有的 provider task_id，或复用已经落盘的
    视频；没有断点记录的镜头才会创建新任务。这样执行器重启不会把已付费
    的 Seedance 任务再次提交。
    """
    shots = manifest.get("shots")
    _validate_aigc_shots(shots)
    keys = _load_runtime_key_pair(
        auth_document,
        load_seedance_api_keys_from_auth_document,
        ("SEEDANCE_PRIMARY_API_KEY",),
        ("SEEDANCE_BACKUP_API_KEY", "SEEDANCE_THIRD_API_KEY", "SEEDANCE_FOURTH_API_KEY"),
        "API_MANAGEMENT_EXTRA_KEYS_VIDEO_GENERATION",
    )
    if not keys:
        raise RuntimeError("Seedance 鉴权配置为空")
    tos = load_infinite_talk_auth_from_document(auth_document)
    legacy_eps = [
        PRIMARY_MODEL_EP,
        BACKUP_MODEL_EP,
        os.environ.get("SEEDANCE_THIRD_MODEL_EP", ""),
        os.environ.get("SEEDANCE_FOURTH_MODEL_EP", ""),
    ]

    def make_transport(*, prefer_index: int = 0) -> SeedanceHTTPTransport:
        """装配四套鉴权；外层重试时只改变首选账号，内部仍执行模型降级。"""
        start = max(0, min(int(prefer_index), len(keys) - 1))
        order = list(range(start, len(keys))) + list(range(0, start))
        ordered_keys = [keys[index] for index in order]
        ordered_eps = [legacy_eps[index] if index < len(legacy_eps) else "" for index in order]
        config = SeedanceConfig.from_api_keys(
            ordered_keys,
            legacy_model_eps=ordered_eps,
            model_1_5_ep=os.environ.get("SEEDANCE_MODEL_1_5_EP", DEFAULT_SEEDANCE_1_5_MODEL),
            model_1_0_ep=os.environ.get("SEEDANCE_MODEL_1_0_EP", DEFAULT_SEEDANCE_1_0_MODEL),
            tos_access_key=tos["tos_access_key"], tos_secret_key=tos["tos_secret_key"],
            tos_bucket=tos["tos_bucket"], tos_endpoint=tos["tos_endpoint"] or "https://tos-cn-beijing.volces.com",
            tos_region=tos["tos_region"] or "cn-beijing",
        )
        return SeedanceHTTPTransport(config)

    primary_transport = make_transport()
    backup_transport = make_transport(prefer_index=1) if len(keys) > 1 else primary_transport
    failover_strategy = os.environ.get("SEEDANCE_FAILOVER_STRATEGY", "primary_then_backup").strip().lower()
    try:
        attempt_limit = max(1, min(5, int(os.environ.get("SEEDANCE_MAX_ATTEMPTS", "2"))))
    except ValueError:
        attempt_limit = 2
    urls = state.get("image_urls")
    if not isinstance(urls, list) or len(urls) != len(shots):
        raise ValueError("首帧 URL 数量与 shots 不一致")

    def provider_fields(response: Mapping[str, Any] | None) -> dict[str, Any]:
        """只保留服务商可展示的诊断字段，绝不持久化鉴权或签名 URL。"""
        if not isinstance(response, Mapping):
            return {}
        return {
            key: response.get(key)
            for key in (
                "success", "status", "msg", "message", "tip", "error", "errorBody",
                "raw_error", "error_code", "error_field", "code", "waited_seconds",
            )
            if response.get(key) not in (None, "")
        }

    def failure_detail(diagnostic: Mapping[str, Any]) -> str:
        return str(
            diagnostic.get("msg") or diagnostic.get("message") or diagnostic.get("errorBody")
            or diagnostic.get("raw_error") or diagnostic.get("error") or diagnostic.get("tip")
            or diagnostic.get("status") or "服务端未返回可用视频 URL"
        )

    def should_retry(diagnostic: Mapping[str, Any]) -> bool:
        """只对服务端未解释的临时失败补一次，避免安全/参数错误重复计费。"""
        detail = failure_detail(diagnostic).lower()
        hard_failures = (
            "safety", "content policy", "安全审核", "图片", "image", "duration", "参数",
            "invalid", "permission", "鉴权", "quota", "额度", "余额", "unsupported", "不支持",
        )
        return not any(marker in detail for marker in hard_failures)

    def should_switch_to_backup(diagnostic: Mapping[str, Any]) -> bool:
        if failover_strategy == "primary_only":
            return False
        detail = failure_detail(diagnostic).lower()
        return any(marker in detail for marker in (
            "setlimitexceeded", "inference limit", "safe experience mode", "安全体验模式",
            "quota", "额度", "余额", "rate limit", "too many requests", "429",
        ))

    existing_by_shot: dict[str, dict[str, Any]] = {}
    if resume and isinstance(existing_records, list):
        for item in existing_records:
            if not isinstance(item, Mapping):
                continue
            shot_id = str(item.get("shot_id") or "").strip()
            if shot_id:
                existing_by_shot[shot_id] = dict(item)

    def run(index: int) -> dict[str, Any]:
        shot = shots[index]
        shot_id = str(shot.get("shot_id") or f"shot_{index + 1:03d}")
        previous = existing_by_shot.get(shot_id)
        if previous is not None:
            previous["index"] = index
            previous_status = str(previous.get("status") or "").strip().lower()
            previous_path = str(previous.get("video_path") or "").strip()
            if previous_status == "succeeded" and previous_path:
                try:
                    if Path(previous_path).is_file():
                        # 本地文件已经是最强断点；不查询、不下载、不创建。
                        return previous
                except OSError:
                    pass
            previous_task_id = str(previous.get("task_id") or "").strip()
            if previous_status == "succeeded" and str(previous.get("video_url") or "").startswith(("http://", "https://")):
                # URL 仍在快照中但本地下载未完成，交给下方统一下载逻辑。
                return previous
            if previous_status in {"failed", "error", "cancelled", "canceled"} and not previous_task_id:
                # 自动恢复不把明确失败变成新的付费提交；由页面“重试”显式触发。
                return previous
            if previous_task_id:
                try:
                    queried = primary_transport.query({"task_id": previous_task_id, "max_wait": 175, "check_interval": 10, "enable_tos_upload": False})
                    query_diagnostic = provider_fields(queried)
                    video_url = str(queried.get("public_video_url") or "")
                    if queried.get("success") and video_url.startswith(("http://", "https://")):
                        resumed = dict(previous)
                        resumed.update({
                            "index": index,
                            "shot_id": shot_id,
                            "task_id": previous_task_id,
                            "status": "succeeded",
                            "video_url": video_url,
                            "provider_diagnostic": {
                                **(dict(previous.get("provider_diagnostic") or {}) if isinstance(previous.get("provider_diagnostic"), Mapping) else {}),
                                "query": query_diagnostic,
                            },
                            "resumed_from_checkpoint": True,
                        })
                        return resumed
                    resumed = dict(previous)
                    resumed.update({
                        "index": index,
                        "shot_id": shot_id,
                        "status": "failed",
                        "error": f"视频查询未成功：{failure_detail(query_diagnostic)}",
                        "provider_diagnostic": {
                            **(dict(previous.get("provider_diagnostic") or {}) if isinstance(previous.get("provider_diagnostic"), Mapping) else {}),
                            "query": query_diagnostic,
                        },
                        "resumed_from_checkpoint": True,
                    })
                    return resumed
                except Exception as exc:
                    resumed = dict(previous)
                    resumed.update({
                        "index": index,
                        "shot_id": shot_id,
                        "status": "failed",
                        "error": f"恢复视频任务查询失败：{str(exc)[:320]}",
                        "resumed_from_checkpoint": True,
                    })
                    return resumed
            if resume:
                # 有快照但没有 task_id/URL，不能猜测供应商状态并重复扣费。
                resumed = dict(previous)
                resumed.update({
                    "index": index,
                    "shot_id": shot_id,
                    "status": "failed",
                    "error": "恢复记录缺少 provider task_id 或视频 URL；请从页面显式重试该镜头",
                    "resumed_from_checkpoint": True,
                })
                return resumed
        task_id = ""
        create_diagnostic: dict[str, Any] = {}
        query_diagnostic: dict[str, Any] = {}
        attempts: list[dict[str, Any]] = []
        use_backup = False
        for attempt in range(1, attempt_limit + 1):
            try:
                # 只有已由媒体路由锁定为 AIGC 的镜头才会进入这里；除全片首镜外，低于
                # 3 秒的镜头在上游已改走图片。首镜即使短于 3 秒，也按服务商最小
                # 整数秒提交，剪辑层再按冻结微秒时间线裁回。
                # 例如 3.5 秒提交 4 秒；剪辑层仍按原始时间线裁回镜头坑位。
                requested_duration_ms = int(shot["duration_ms"])
                duration = _provider_duration_seconds(
                    requested_duration_ms,
                    allow_short=_is_full_video_first_shot(shot),
                )
                transport = backup_transport if use_backup else primary_transport
                created = transport.generate({
                    "prompt": shot["prompt"], "duration": duration,
                    "first_frame_url": urls[index], "last_frame_url": "",
                    "generate_audio": False, "watermark": False,
                    "camera_fixed": bool(shot.get("camera_fixed", True)),
                    "ratio": "adaptive", "resolution": "720p",
                    "video_model": str(shot.get("video_model") or manifest.get("video_model") or "auto"),
                })
                create_diagnostic = provider_fields(created)
                task_id = str(created.get("task_id") or "")
                if not task_id:
                    raise RuntimeError(f"视频任务没有返回 task_id：{failure_detail(create_diagnostic)}")
                # 创建成功即交给编排层持久化。查询、下载和后续进程退出都不能
                # 再让已付费的 provider task_id 消失。
                if task_created_reporter is not None:
                    task_created_reporter({
                        "index": index,
                        "shot_id": shot_id,
                        "task_id": task_id,
                        "status": "submitted",
                        "attempt": attempt,
                        "provider_diagnostic": {"create": create_diagnostic},
                    })
                queried = transport.query({"task_id": task_id, "max_wait": 175, "check_interval": 10, "enable_tos_upload": False})
                query_diagnostic = provider_fields(queried)
                video_url = str(queried.get("public_video_url") or "")
                if queried.get("success") and video_url.startswith(("http://", "https://")):
                    return {"index": index, "shot_id": shot_id, "task_id": task_id, "status": "succeeded", "video_url": video_url, "requested_duration_ms": requested_duration_ms, "provider_duration_seconds": duration, "attempt_count": attempt, "attempts": attempts, "provider_diagnostic": {"create": create_diagnostic, "query": query_diagnostic}}
                raise RuntimeError(f"视频查询未成功：{failure_detail(query_diagnostic)}")
            except Exception as exc:
                failure = str(exc).replace("\r", " ").replace("\n", " ").strip()[:320]
                diagnostic = query_diagnostic or create_diagnostic
                attempts.append({"attempt": attempt, "task_id": task_id, "error": failure, "provider_diagnostic": {"create": create_diagnostic, "query": query_diagnostic}})
                if attempt == 1 and should_retry(diagnostic):
                    use_backup = should_switch_to_backup(diagnostic)
                    time.sleep(2)
                    task_id = ""
                    continue
                return {"index": index, "shot_id": shot_id, "task_id": task_id, "status": "failed", "error": failure, "requested_duration_ms": int(shot.get("duration_ms") or 0), "provider_duration_seconds": _provider_duration_seconds(int(shot["duration_ms"]), allow_short=_is_full_video_first_shot(shot)), "attempt_count": attempt, "attempts": attempts, "provider_diagnostic": {"create": create_diagnostic, "query": query_diagnostic}}

    results: list[dict[str, Any] | None] = [None] * len(shots)
    with ThreadPoolExecutor(max_workers=min(3, len(shots))) as executor:
        futures = [executor.submit(run, index) for index in range(len(shots))]
        for future in as_completed(futures):
            record = future.result()
            index = int(record["index"])
            if record["status"] == "succeeded":
                try:
                    existing_path = str(record.get("video_path") or "").strip()
                    if existing_path and Path(existing_path).is_file():
                        record["video_path"] = str(Path(existing_path).resolve())
                    else:
                        path = out_dir / "videos" / f"video_{index + 1:02d}.mp4"
                        video_url = str(record.get("video_url") or "")
                        if not video_url.startswith(("http://", "https://")):
                            raise RuntimeError("成功记录缺少可下载的视频 URL")
                        _download(video_url, path, 50_000)
                        record["video_path"] = str(path.resolve())
                except Exception as exc:
                    record = {**record, "status": "failed", "error": f"视频下载失败：{str(exc)[:260]}"}
            results[index] = record
            if progress_reporter is not None:
                completed = sum(item is not None for item in results)
                succeeded = sum(isinstance(item, Mapping) and item.get("status") == "succeeded" for item in results)
                progress_reporter({"total": len(shots), "completed": completed, "succeeded": succeeded, "record": dict(record)})
    records = [item for item in results if isinstance(item, Mapping)]
    if len(records) != len(shots):
        raise RuntimeError("视频结果数量不完整")
    return {"video_task_ids": [str(item.get("task_id") or "") for item in records], "video_paths": [str(item.get("video_path") or "") for item in records], "shot_results": records, "succeeded_count": sum(item.get("status") == "succeeded" for item in records), "failed_count": sum(item.get("status") == "failed" for item in records)}


def generate_video_one(manifest: Mapping[str, Any], state: Mapping[str, Any], auth_document: Path, out_dir: Path, index: int) -> dict[str, Any]:
    """只补跑指定缺失镜头，绝不重新提交已有镜头。"""
    shots = manifest.get("shots")
    _validate_aigc_shots(shots)
    keys = _load_runtime_key_pair(
        auth_document,
        load_seedance_api_keys_from_auth_document,
        ("SEEDANCE_PRIMARY_API_KEY",),
        ("SEEDANCE_BACKUP_API_KEY", "SEEDANCE_THIRD_API_KEY", "SEEDANCE_FOURTH_API_KEY"),
        "API_MANAGEMENT_EXTRA_KEYS_VIDEO_GENERATION",
    )
    if not keys:
        raise RuntimeError("Seedance 鉴权配置为空")
    tos = load_infinite_talk_auth_from_document(auth_document)
    transport = SeedanceHTTPTransport(SeedanceConfig.from_api_keys(
        keys,
        legacy_model_eps=[
            PRIMARY_MODEL_EP,
            BACKUP_MODEL_EP,
            os.environ.get("SEEDANCE_THIRD_MODEL_EP", ""),
            os.environ.get("SEEDANCE_FOURTH_MODEL_EP", ""),
        ],
        model_1_5_ep=os.environ.get("SEEDANCE_MODEL_1_5_EP", DEFAULT_SEEDANCE_1_5_MODEL),
        model_1_0_ep=os.environ.get("SEEDANCE_MODEL_1_0_EP", DEFAULT_SEEDANCE_1_0_MODEL),
        tos_access_key=tos["tos_access_key"], tos_secret_key=tos["tos_secret_key"],
        tos_bucket=tos["tos_bucket"], tos_endpoint=tos["tos_endpoint"] or "https://tos-cn-beijing.volces.com",
        tos_region=tos["tos_region"] or "cn-beijing",
    ))
    urls = state.get("image_urls")
    if not isinstance(urls, list) or len(urls) != len(shots):
        raise ValueError("首帧 URL 数量与 shots 不一致")
    shot = shots[index]
    requested_duration_ms = int(shot["duration_ms"])
    duration = _provider_duration_seconds(
        requested_duration_ms,
        allow_short=_is_full_video_first_shot(shot),
    )
    created = transport.generate({
        "prompt": shot["prompt"], "duration": duration,
        "first_frame_url": urls[index], "last_frame_url": "",
        "generate_audio": False, "watermark": False,
        "ratio": "adaptive", "resolution": "720p",
        "video_model": str(shot.get("video_model") or manifest.get("video_model") or "auto"),
    })
    task_id = str(created.get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"{shot['shot_id']} 补跑视频没有返回 task_id")
    result = transport.query({"task_id": task_id, "max_wait": 175, "check_interval": 10, "enable_tos_upload": False})
    video_url = str(result.get("public_video_url") or "")
    if not result.get("success") or not video_url.startswith(("http://", "https://")):
        raise RuntimeError(f"{shot['shot_id']} 补跑视频查询未成功")
    path = out_dir / "videos" / f"video_{index + 1:02d}.mp4"
    _download(video_url, path, 50_000)
    return {"video_one_index": index, "video_one_task_id": task_id, "video_one_path": str(path.resolve()), "requested_duration_ms": requested_duration_ms, "provider_duration_seconds": duration}


def _resolve_manifest_tts_voice(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """把任务中的 voice_key 解析成目录登记的真实 speaker_id。"""

    if "tts_speaker_id" in manifest:
        raise RuntimeError("已废弃的 tts_speaker_id 不再受支持；请传入 voice_key")
    voice_key = str(manifest.get("voice_key") or "").strip()
    catalog_path = str(os.environ.get("VOICE_CATALOG_PATH") or "").strip()
    catalog = DEFAULT_VOICE_CATALOG
    if catalog_path and Path(catalog_path).is_file():
        try:
            catalog = VoiceCatalogStore(catalog_path).snapshot()
        except VoiceCatalogStoreError as exc:
            raise RuntimeError(f"TTS 音色目录读取失败：{exc}") from exc
    try:
        return resolve_voice_selection(
            {"voice_key": voice_key} if voice_key else {},
            catalog=catalog,
        )
    except VoiceCatalogError as exc:
        raise RuntimeError(f"TTS 音色解析失败：{exc}") from exc


def _build_tts_contract(
    voice: Mapping[str, Any],
    *,
    paths: list[str],
    audio_hashes: list[str],
    durations: list[float],
    auth_slots: list[str],
    voice_bindings: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """构建新任务使用的版本化 TTS 音色/音频契约。

    旧任务的 ``tts_*`` 平铺字段只允许由编排层的迁移适配器读取；本函数
    不再把它们写入新任务，这样 voice_key、speaker_id、resource_id 和实际
    音频证据始终在同一个可校验版本中。
    """

    voice_type = str(voice.get("voice_type") or "official").strip().lower()
    if voice_type != "official":
        raise RuntimeError("素材 TTS 仅允许官方音色；定制音色/声音复刻已停用")
    if str(voice.get("custom_speaker_id") or "").strip():
        raise RuntimeError("素材 TTS 不允许 custom_speaker_id")
    if str(voice.get("resource_id") or "").strip() != "seed-tts-2.0":
        raise RuntimeError("素材 TTS 仅支持 resource_id=seed-tts-2.0")
    voice_data: dict[str, Any] = {
        "voice_key": str(voice.get("voice_key") or ""),
        "speaker_id": str(voice.get("speaker_id") or ""),
        "resource_id": str(voice.get("resource_id") or ""),
        "model": str(voice.get("model") or ""),
        "voice_type": voice_type,
        "authorization_status": str(voice.get("authorization_status") or "unknown"),
        "training_status": str(voice.get("training_status") or "ready"),
    }
    return {
        "schema_version": "tts-voice-binding-v1",
        "voice": voice_data,
        "audio": {
            "paths": list(paths),
            "sha256": list(audio_hashes),
            "actual_durations_s": [float(item) for item in durations],
        },
        "auth_slots": list(auth_slots),
        "voice_bindings": [dict(item) for item in voice_bindings],
        "timing_authority": "capcut_stt_required",
    }


def _resolve_tts_workers(value: int | None = None) -> int:
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("TTS 并行 worker 数必须是正整数")
        return value
    raw = os.environ.get("DIRECTOR_TTS_PARALLEL_WORKERS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = TTS_PARALLEL_WORKERS
        if parsed > 0:
            return parsed
    return TTS_PARALLEL_WORKERS


def generate_tts(
    manifest: Mapping[str, Any],
    auth_document: Path,
    out_dir: Path,
    parallel_workers: int | None = None,
    *,
    voice_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # ``auth_document`` 仍保留在函数签名中是因为素材编排器对各供应商
    # 使用统一参数；新版 TTS 不得读取它。主/副 Key 必须来自 API 管理页
    # 注入的运行时环境，避免旧文档把错误账号带入声音复刻或时间轴链路。
    keys = _load_runtime_key_pair(
        auth_document,
        None,
        ("ARK_TTS_API_KEY", "ARK_AUDIO_API_KEY", "VOLCENGINE_AUDIO_API_KEY"),
        ("ARK_TTS_BACKUP_API_KEY", "ARK_AUDIO_BACKUP_API_KEY", "VOLCENGINE_AUDIO_BACKUP_API_KEY"),
        "API_MANAGEMENT_EXTRA_KEYS_TTS",
        allow_document_fallback=False,
    )
    if not keys:
        raise RuntimeError("TTS 主鉴权缺失")
    # ``voice_override`` 仅保留给历史调用方的兼容参数；即使传入覆盖值，
    # 也必须通过官方音色硬门禁，不能绕过目录解析使用定制音色。
    if voice_override is None:
        voice = _resolve_manifest_tts_voice(manifest)
    elif isinstance(voice_override, Mapping):
        voice = dict(voice_override)
        if not str(voice.get("voice_key") or manifest.get("voice_key") or "").strip():
            raise RuntimeError("受控 TTS 音色覆盖缺少 voice_key")
        voice.setdefault("voice_key", str(manifest.get("voice_key") or ""))
        if not str(voice.get("speaker_id") or "").strip():
            raise RuntimeError("受控 TTS 音色覆盖缺少 speaker_id")
        voice.setdefault("resource_id", "seed-tts-2.0")
        voice.setdefault("model", DEFAULT_TTS_MODEL)
    else:
        raise RuntimeError("受控 TTS 音色覆盖必须是对象")
    voice_type = str(voice.get("voice_type") or "official").strip().lower()
    custom_speaker_id = str(voice.get("custom_speaker_id") or "").strip()
    provider_speaker_id = str(voice.get("provider_speaker_id") or "").strip()
    if voice_type != "official" or custom_speaker_id:
        raise RuntimeError("素材 TTS 仅允许官方音色；定制音色/声音复刻已停用")
    if str(voice.get("resource_id") or "").strip() != "seed-tts-2.0":
        raise RuntimeError("素材 TTS 仅支持 resource_id=seed-tts-2.0")
    transport = ArkTTSHTTPTransport(ArkTTSConfig(
        api_key=keys[0],
        backup_api_key=keys[1] if len(keys) > 1 else "",
        speaker_id="", audio_format="mp3",
        sample_rate=48_000, enable_subtitle=True,
        # V3 endpoint 固定；model 只能来自已经通过音色目录校验的绑定，
        # 不再允许历史 ARK_TTS_API_URL/ARK_TTS_MODEL 覆盖生产请求。
        api_url=DEFAULT_ARK_TTS_URL,
        model=str(voice.get("model") or DEFAULT_TTS_MODEL).strip(),
        # 资源由官方音色目录决定；环境变量不能覆盖固定资源。
        resource_id=voice["resource_id"],
        failover_strategy=os.environ.get("ARK_TTS_FAILOVER_STRATEGY", "primary_then_backup"),
        timeout=float(os.environ.get("ARK_TTS_TIMEOUT", "300")),
        extra_api_keys=tuple(keys[2:]),
    ))
    shots = list(manifest["shots"])
    started_at = time.perf_counter()
    if not shots:
        worker_count = 1
        indexed_results: dict[int, tuple[dict[str, Any], Path, float]] = {}
    else:
        worker_count = min(_resolve_tts_workers(parallel_workers), len(shots))
        indexed_results: dict[int, tuple[dict[str, Any], Path, float]] = {}
        errors: list[tuple[int, Exception]] = []
        start_events = [threading.Event() for _ in shots]

        def synthesize_one(index: int, shot: Mapping[str, Any]) -> tuple[int, dict[str, Any], Path, float]:
            # 仅保证请求进入 transport 的顺序；上一请求仍在网络等待时，
            # 下一 worker 可继续发起请求，从而兼顾确定性审计和吞吐。
            if index:
                start_events[index - 1].wait()
            start_events[index].set()
            result = transport.synthesize_local(SpeechSynthesisRequest(
                text=str(shot["narration_text"]), speed_ratio=1.1,
                voice_id=voice["speaker_id"], speaker_id=voice["speaker_id"],
                loudness_rate=0, pitch=0, silence_duration=0,
                resource_id=voice.get("resource_id"), model=voice.get("model"),
            ))
            path = out_dir / "audios" / f"voice_{index + 1:02d}.mp3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result["audio"])
            # 供应商返回的 timestamps 只作诊断；时间线契约必须绑定实际落盘文件。
            actual_duration = _probe_written_audio_duration(path)
            return index, result, path, actual_duration

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tts") as executor:
            futures = {
                executor.submit(synthesize_one, index, shot): index
                for index, shot in enumerate(shots)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result_index, result, path, actual_duration = future.result()
                    indexed_results[result_index] = (result, path, actual_duration)
                except Exception as error:
                    errors.append((index, error))
        if errors:
            _, error = min(errors, key=lambda item: item[0])
            raise error

    durations: list[float] = []
    audio_hashes: list[str] = []
    paths: list[str] = []
    auth_slots: list[str] = []
    voice_bindings: list[dict[str, Any]] = []
    for index in range(len(shots)):
        result, path, actual_duration = indexed_results[index]
        metadata = result.get("metadata") if isinstance(result, Mapping) else {}
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        metadata["provider_duration"] = float(result.get("duration") or 0)
        metadata["actual_duration"] = float(actual_duration)
        result["metadata"] = metadata
        durations.append(float(actual_duration))
        audio_hashes.append(hashlib.sha256(result["audio"]).hexdigest())
        paths.append(str(path.resolve()))
        auth_slots.append(str(metadata.get("auth_slot") or "primary") if isinstance(metadata, Mapping) else "primary")
        binding = metadata.get("voice_binding") if isinstance(metadata, Mapping) else {}
        if isinstance(binding, Mapping):
            voice_bindings.append(dict(binding))
        else:
            fallback_binding: dict[str, Any] = {
                "speaker_id": voice["speaker_id"],
                "resource_id": voice.get("resource_id", ""),
                "model": voice.get("model", ""),
            }
            voice_bindings.append(fallback_binding)
    contract = _build_tts_contract(
        voice,
        paths=paths,
        audio_hashes=audio_hashes,
        durations=durations,
        auth_slots=auth_slots,
        voice_bindings=voice_bindings,
    )
    return {
        "tts_contract": contract,
        "performance": {
            "tts_parallel_workers": worker_count,
            "tts_completed": len(shots),
            "tts_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("images", "videos", "video-one", "tts"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--auth-document", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--upload-reference", action="store_true", help="明确授权后才把本地主角图临时上传给首帧服务")
    parser.add_argument("--index", type=int, help="video-one 的 0-based 镜头索引")
    args = parser.parse_args()
    manifest = _load(args.manifest)
    state = _load(args.state) if args.state.is_file() else {}
    if args.action == "images":
        value = generate_images(manifest, args.auth_document, args.out_dir, upload_reference=args.upload_reference)
    elif args.action == "videos":
        value = generate_videos(manifest, state, args.auth_document, args.out_dir)
    elif args.action == "video-one":
        if args.index is None or args.index < 0 or args.index >= len(manifest.get("shots", [])):
            raise ValueError("video-one 必须提供合法的 --index")
        value = generate_video_one(manifest, state, args.auth_document, args.out_dir, args.index)
    else:
        value = generate_tts(manifest, args.auth_document, args.out_dir)
    state.update(value)
    state["last_action"] = args.action
    state["evidence_level"] = "live_external_service"
    _save(args.state, state)
    tts_paths = (
        value.get("tts_contract", {}).get("audio", {}).get("paths", [])
        if isinstance(value.get("tts_contract"), Mapping)
        else value.get("tts_paths", [])
    )
    count = len(value.get("image_paths") or value.get("video_paths") or tts_paths or [])
    print(json.dumps({"status": "ok", "action": args.action, "state": str(args.state), "count": count}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
