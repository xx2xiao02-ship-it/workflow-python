"""字幕文本纠错：读取 API 中心的 Doubao-Seed-2.0-mini 主/备用通道。

剪映 STT 只负责听写和时间戳；本模块只允许模型修改 ``text``，字幕 ID、
分组、时间线和数组顺序由本地校验器掌握。API 顺序读取
``api_management.json -> api_groups.language-model.order``，因此页面配置的
“主 API 用完切副 API”会真正作用于字幕纠错，而不是停留在页面预览。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .ark_story_transport import DEFAULT_AUTH_DOCUMENT, load_story_auth_from_document


DEFAULT_API_MANAGEMENT_CONFIG = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    / "VideoProductionConsole"
    / "api_management.json"
)
MINI_NAME_PATTERN = re.compile(r"doubao.*seed.*2\.?0.*mini|seed.*2\.?0.*mini", re.IGNORECASE)


class SubtitleCorrectionError(RuntimeError):
    """字幕纠错请求、解析或数组契约错误。"""


class SubtitleCorrectionConfigError(SubtitleCorrectionError):
    """API 中心配置或凭据不完整。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]


@dataclass(frozen=True)
class SubtitleCorrectionChannel:
    channel_id: str
    endpoint: str
    model_id: str
    model_name: str
    api_key: str
    timeout_seconds: float = 120.0


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SubtitleCorrectionConfigError(f"未找到 API 中心配置：{path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleCorrectionConfigError("API 中心配置不是合法 JSON") from exc
    if not isinstance(value, Mapping):
        raise SubtitleCorrectionConfigError("API 中心配置根节点必须是对象")
    return value


def _channel_order(config: Mapping[str, Any]) -> list[str]:
    groups = config.get("api_groups")
    language_model = groups.get("language-model") if isinstance(groups, Mapping) else None
    order = language_model.get("order") if isinstance(language_model, Mapping) else None
    if isinstance(order, list):
        values = [str(item).strip() for item in order if str(item).strip()]
        if values:
            return values
    return ["story-writing", "director-seed21"]


def _mini_slot(group: Mapping[str, Any]) -> tuple[str, str, str]:
    slots = group.get("model_slots")
    names = group.get("model_names")
    order = group.get("model_order")
    if not isinstance(slots, Mapping) or not isinstance(names, Mapping):
        raise SubtitleCorrectionConfigError("API 中心语言模型通道缺少 model_slots/model_names")
    candidates = [str(item).strip() for item in order] if isinstance(order, list) else list(slots)
    for slot in candidates:
        name = str(names.get(slot) or "").strip()
        model_id = str(slots.get(slot) or "").strip()
        if model_id and MINI_NAME_PATTERN.search(name):
            return slot, model_id, name
    # 页面旧版本可能只保存了自定义槽位 ID，没有保存模型名称；仅在名称完全
    # 缺失时允许 custom_model 槽位作为最后回退，避免误用 Seed 2.1 主模型。
    for slot in candidates:
        model_id = str(slots.get(slot) or "").strip()
        name = str(names.get(slot) or "").strip()
        if model_id and str(slot).lower().startswith("custom_model_") and not name:
            return slot, model_id, "custom model"
    raise SubtitleCorrectionConfigError("API 中心通道没有配置 Doubao-Seed-2.0-mini 模型")


def _story_key(auth_document: Path) -> str:
    key = _first_env("STORY_WRITER_ARK_API_KEY", "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY")
    if key:
        return key
    values = load_story_auth_from_document(auth_document)
    return values[0][0] if values else ""


def _director_key(auth_document: Path) -> str:
    key = _first_env("VOICE_DIRECTOR_PRIMARY_API_KEY", "SHOT_REFINEMENT_PRIMARY_API_KEY")
    if key:
        return key
    try:
        from .voice_director_transport import load_shot_refinement_seed21_turbo_auth_from_document

        return load_shot_refinement_seed21_turbo_auth_from_document(auth_document)[0]
    except Exception:
        return ""


def load_subtitle_correction_channels(
    *,
    config_path: str | Path | None = None,
    auth_document: str | Path | None = None,
) -> list[SubtitleCorrectionChannel]:
    """从 API 中心的语言模型顺序构造 Mini 主/备用通道。"""

    path = Path(config_path or os.environ.get("API_MANAGEMENT_CONFIG_PATH", "") or DEFAULT_API_MANAGEMENT_CONFIG).expanduser()
    config = _read_json(path)
    groups = config.get("groups")
    if not isinstance(groups, Mapping):
        raise SubtitleCorrectionConfigError("API 中心配置缺少 groups")
    document = Path(auth_document or os.environ.get("STORY_WRITER_AUTH_DOCUMENT", "") or DEFAULT_AUTH_DOCUMENT).expanduser()
    result: list[SubtitleCorrectionChannel] = []
    for channel_id in _channel_order(config):
        raw = groups.get(channel_id)
        if not isinstance(raw, Mapping):
            continue
        endpoint = str(raw.get("endpoint") or "").strip()
        if not endpoint:
            continue
        _slot, model_id, model_name = _mini_slot(raw)
        api_key = _story_key(document) if channel_id == "story-writing" else _director_key(document)
        if not api_key:
            raise SubtitleCorrectionConfigError(f"{channel_id} 未找到 API Key")
        try:
            timeout = float(raw.get("timeout_seconds") or 120)
        except (TypeError, ValueError) as exc:
            raise SubtitleCorrectionConfigError(f"{channel_id}.timeout_seconds 必须是数字") from exc
        result.append(SubtitleCorrectionChannel(channel_id, endpoint, model_id, model_name, api_key, timeout))
        if str(raw.get("strategy") or "primary_then_backup").strip().lower() == "primary_only":
            break
    if not result:
        raise SubtitleCorrectionConfigError("API 中心未找到可用的字幕纠错主/备用通道")
    return result


def _default_requester(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        # 只保留状态码；响应正文可能包含请求上下文，不应进入任务日志。
        raise SubtitleCorrectionError(f"字幕纠错 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        try:
            import requests

            response = requests.post(url, json=dict(payload), headers=dict(headers), timeout=timeout)
            if response.status_code >= 400:
                raise SubtitleCorrectionError(f"字幕纠错 HTTP {response.status_code}")
            raw = response.text
        except SubtitleCorrectionError:
            raise
        except Exception as fallback_exc:
            raise SubtitleCorrectionError(f"字幕纠错网络请求失败：{type(fallback_exc).__name__}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SubtitleCorrectionError("字幕纠错响应不是合法 JSON") from exc
    if not isinstance(value, Mapping):
        raise SubtitleCorrectionError("字幕纠错响应根节点必须是对象")
    if value.get("error"):
        raise SubtitleCorrectionError("字幕纠错模型返回业务错误")
    return value


def _response_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "content", "output_text"):
            text = _response_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        return "\n".join(text for text in (_response_text(item) for item in value) if text).strip()
    return ""


def _extract_model_text(response: Mapping[str, Any]) -> str:
    direct = _response_text(response.get("output_text"))
    if direct:
        return direct
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        text = _response_text(message.get("content") if isinstance(message, Mapping) else message)
        if text:
            return text
    output = response.get("output")
    text = _response_text(output)
    if text:
        return text
    raise SubtitleCorrectionError("字幕纠错响应缺少文本内容")


def _parse_json_text(text: str) -> Mapping[str, Any]:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise SubtitleCorrectionError("字幕纠错模型未返回合法 JSON")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SubtitleCorrectionError("字幕纠错模型未返回合法 JSON") from exc
    if not isinstance(value, Mapping):
        raise SubtitleCorrectionError("字幕纠错 JSON 根节点必须是对象")
    return value


def _brief_error(exc: Exception) -> str:
    message = str(exc)
    match = re.search(r"HTTP\s+(\d{3})", message, flags=re.IGNORECASE)
    if match:
        return f"HTTP {match.group(1)}"
    if "网络请求失败" in message:
        return "网络请求失败"
    if "JSON" in message or "json" in message:
        return "JSON 契约错误"
    return type(exc).__name__


SYSTEM_PROMPT = """你是中文短视频字幕校对器。只做听写结果的错别字、同音字、标点和明显断词纠正。
不要改写语气，不要增删事实，不要合并或拆分字幕，不要改变字幕 ID、顺序或时间。
必须只输出 JSON：{"items":[{"caption_id":"原ID","text":"纠正后的文本"}]}。
如果听写文本与参考文案一致，原样返回。参考文案只用于确认专有名词，不得把参考文案整句替换进字幕。"""


def _validate_items(
    original: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_items = response.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(original):
        raise SubtitleCorrectionError("字幕纠错返回数量与 STT 字幕不一致")
    expected_ids = [str(item.get("caption_id") or "") for item in original]
    returned_ids: list[str] = []
    by_id: dict[str, str] = {}
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise SubtitleCorrectionError(f"字幕纠错 items[{index}] 不是对象")
        caption_id = str(item.get("caption_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not caption_id or not text:
            raise SubtitleCorrectionError(f"字幕纠错 items[{index}] 缺少 caption_id/text")
        returned_ids.append(caption_id)
        by_id[caption_id] = text
    if returned_ids != expected_ids:
        raise SubtitleCorrectionError("字幕纠错改变了字幕 ID 顺序")
    result: list[dict[str, Any]] = []
    for original_item in original:
        item = dict(original_item)
        item["text"] = by_id[str(original_item["caption_id"])]
        result.append(item)
    return result


class SubtitleCorrectionTransport:
    """Mini 主/备用纠错 transport。"""

    def __init__(
        self,
        channels: Sequence[SubtitleCorrectionChannel],
        *,
        requester: Requester | None = None,
    ) -> None:
        if not channels:
            raise SubtitleCorrectionConfigError("字幕纠错没有可用 API 通道")
        self.channels = list(channels)
        self.requester = requester or _default_requester

    @classmethod
    def from_api_management(
        cls,
        *,
        config_path: str | Path | None = None,
        auth_document: str | Path | None = None,
        requester: Requester | None = None,
    ) -> "SubtitleCorrectionTransport":
        return cls(
            load_subtitle_correction_channels(config_path=config_path, auth_document=auth_document),
            requester=requester,
        )

    @staticmethod
    def _payload(channel: SubtitleCorrectionChannel, user_prompt: str) -> dict[str, Any]:
        if channel.endpoint.rstrip("/").endswith("/responses"):
            return {
                "model": channel.model_id,
                "instructions": SYSTEM_PROMPT,
                "input": user_prompt,
                "temperature": 0.1,
                "max_output_tokens": 2400,
                "thinking": {"type": "disabled"},
            }
        return {
            "model": channel.model_id,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2400,
            "thinking": {"type": "disabled"},
        }

    def correct(self, captions: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not captions:
            raise SubtitleCorrectionError("字幕纠错输入不能为空")
        original = [dict(item) for item in captions]
        for index, item in enumerate(original):
            if not str(item.get("caption_id") or "").strip() or not str(item.get("text") or "").strip():
                raise SubtitleCorrectionError(f"字幕纠错输入 items[{index}] 缺少 caption_id/text")
        prompt_items = [
            {
                "caption_id": str(item["caption_id"]),
                "text": str(item["text"]),
                "reference_text": str(item.get("reference_text") or ""),
            }
            for item in original
        ]
        user_prompt = json.dumps({"items": prompt_items}, ensure_ascii=False)
        failures: list[dict[str, str]] = []
        for index, channel in enumerate(self.channels):
            try:
                response = self.requester(
                    channel.endpoint,
                    {"Authorization": "Bearer " + channel.api_key},
                    self._payload(channel, user_prompt),
                    channel.timeout_seconds,
                )
                if not isinstance(response, Mapping):
                    raise SubtitleCorrectionError("字幕纠错响应根节点必须是对象")
                parsed = _parse_json_text(_extract_model_text(response))
                corrected = _validate_items(original, parsed)
                return corrected, {
                    "status": "succeeded",
                    "provider": channel.channel_id,
                    "model_name": channel.model_name,
                    "model_id": channel.model_id,
                    "attempt": index + 1,
                    "failover": index > 0,
                    "failed_attempts": failures,
                    "caption_count": len(corrected),
                }
            except Exception as exc:
                failures.append({
                    "provider": channel.channel_id,
                    "model_name": channel.model_name,
                    "reason": _brief_error(exc),
                })
        raise SubtitleCorrectionError(
            "Mini 字幕纠错主/备用 API 均未完成："
            + "；".join(f"{item['provider']} {item['reason']}" for item in failures)
        )

    def __call__(self, captions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return self.correct(captions)[0]


__all__ = [
    "SubtitleCorrectionChannel",
    "SubtitleCorrectionConfigError",
    "SubtitleCorrectionError",
    "SubtitleCorrectionTransport",
    "load_subtitle_correction_channels",
]
