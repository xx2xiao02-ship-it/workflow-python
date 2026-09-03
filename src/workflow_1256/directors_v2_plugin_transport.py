"""当前用户提供的 253 行 ``directors_v2`` 插件源码的 HTTP transport。

该版本与旧审计源码不是同一契约：

* 输入：``system_prompt``、``prompt``、``image_urls``；
* 内部：router、4o 视觉模型、5.5 文本模型；
* 输出：``output_5_5``。

本模块按该源码实现请求、重试和多模态分支，然后只在
``output_5_5`` 能解析为 8364 节点声明的四字段对象时，才映射为
``director_plan/ok/segment_beats/segments``。普通文本会明确报契约不匹配。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .directors_v2 import normalize_response


DEFAULT_API_URL = "https://api.aibh.site/v1/chat/completions"
DEFAULT_MODEL_ROUTER = "gpt-4o-mini"
DEFAULT_MODEL_VISION = "gpt-4o"
DEFAULT_MODEL_TEXT = "gpt-5.5"
DEFAULT_TIMEOUT_ROUTER = 15.0
DEFAULT_TIMEOUT_VISION = 45.0
DEFAULT_TIMEOUT_TEXT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_DELAY = 2.0


class DirectorsV2PluginTransportError(RuntimeError):
    """当前 directors_v2 插件请求或输出适配失败。"""


class DirectorsV2PluginConfigError(DirectorsV2PluginTransportError):
    """当前插件 transport 缺少必要配置。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(item for item in (_text(part) for part in value) if item).strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "content", "output_text"):
            current = _text(value.get(key))
            if current:
                return current
    return ""


def _parse_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    text = _text(value)
    text = re.sub(r"^```json\s*|```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _default_requester(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            **headers,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status_code = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status_code = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return {"status_code": 500, "text": f"{type(exc).__name__}: {exc}", "json": None}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    return {"status_code": status_code, "text": raw, "json": data}


def _bearer(value: str) -> str:
    return value if value.lower().startswith("bearer ") else "Bearer " + value


class DirectorsV2PluginHTTPTransport:
    """执行当前附件源码的 router/vision/text HTTP 分支。"""

    def __init__(
        self,
        *,
        api_key: str,
        system_prompt: str = "",
        api_url: str = DEFAULT_API_URL,
        model_router: str = DEFAULT_MODEL_ROUTER,
        model_vision: str = DEFAULT_MODEL_VISION,
        model_text: str = DEFAULT_MODEL_TEXT,
        timeout_router: float = DEFAULT_TIMEOUT_ROUTER,
        timeout_vision: float = DEFAULT_TIMEOUT_VISION,
        timeout_text: float = DEFAULT_TIMEOUT_TEXT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        failover_strategy: str = "retry_same",
        initial_delay: float = DEFAULT_INITIAL_DELAY,
        requester: Requester | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise DirectorsV2PluginConfigError(
                "需要 DIRECTORS_V2_API_KEY；密钥只从本机环境变量读取"
            )
        self.api_key = api_key.strip()
        self.system_prompt = system_prompt
        self.api_url = api_url.strip()
        self.model_router = model_router.strip()
        self.model_vision = model_vision.strip()
        self.model_text = model_text.strip()
        self.timeout_router = float(timeout_router)
        self.timeout_vision = float(timeout_vision)
        self.timeout_text = float(timeout_text)
        self.max_retries = int(max_retries)
        if self.max_retries < 1 or self.max_retries > 5:
            raise DirectorsV2PluginConfigError("DIRECTORS_V2_MAX_RETRIES 必须在 1 到 5 之间")
        self.failover_strategy = str(failover_strategy or "retry_same").strip().lower()
        if self.failover_strategy not in {"retry_same", "stop_on_error"}:
            raise DirectorsV2PluginConfigError(
                "DIRECTORS_V2_FAILOVER_STRATEGY 只支持 retry_same 或 stop_on_error"
            )
        self.initial_delay = float(initial_delay)
        self.requester = requester or _default_requester
        self.sleeper = sleeper

    @classmethod
    def from_env(cls) -> "DirectorsV2PluginHTTPTransport":
        api_key = os.environ.get("DIRECTORS_V2_API_KEY", "").strip()
        if not api_key:
            raise DirectorsV2PluginConfigError(
                "未配置 DIRECTORS_V2_API_KEY；当前附件版本使用单一 Chat Completions API"
            )
        try:
            max_retries = int(os.environ.get("DIRECTORS_V2_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)))
        except ValueError as exc:
            raise DirectorsV2PluginConfigError("DIRECTORS_V2_MAX_RETRIES 必须是整数") from exc
        return cls(
            api_key=api_key,
            system_prompt=os.environ.get("DIRECTORS_V2_SYSTEM_PROMPT", ""),
            api_url=os.environ.get("DIRECTORS_V2_API_URL", DEFAULT_API_URL),
            model_router=os.environ.get("DIRECTORS_V2_MODEL_ROUTER", DEFAULT_MODEL_ROUTER),
            model_vision=os.environ.get("DIRECTORS_V2_MODEL_VISION", DEFAULT_MODEL_VISION),
            model_text=os.environ.get("DIRECTORS_V2_MODEL_TEXT", DEFAULT_MODEL_TEXT),
            max_retries=max_retries,
            failover_strategy=os.environ.get("DIRECTORS_V2_FAILOVER_STRATEGY", "retry_same"),
        )

    def _post_chat(self, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        try:
            response = self.requester(
                self.api_url,
                {
                    "Authorization": _bearer(self.api_key),
                    "User-Agent": "Mozilla/5.0",
                },
                payload,
                timeout,
            )
        except Exception as exc:
            return {"ok": False, "status_code": 500, "error": f"请求异常: {exc}"}
        status_code = int(response.get("status_code", 500))
        if status_code != 200:
            return {
                "ok": False,
                "status_code": status_code,
                "error": _text(response.get("text"))[:800],
            }
        data = response.get("json")
        if not isinstance(data, Mapping):
            return {"ok": False, "status_code": 500, "error": "响应解析失败"}
        try:
            content = data["choices"][0]["message"].get("content", "")
        except (KeyError, IndexError, TypeError, AttributeError):
            return {"ok": False, "status_code": 500, "error": "响应缺少 choices[0].message.content"}
        return {"ok": True, "status_code": 200, "content": _text(content), "raw": dict(data)}

    def _retry_post_chat(self, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        delay = self.initial_delay
        last: dict[str, Any] = {"ok": False, "status_code": 500, "error": "未执行请求"}
        retry_limit = 1 if self.failover_strategy == "stop_on_error" else self.max_retries
        for attempt in range(retry_limit):
            last = self._post_chat(payload, timeout)
            if last["ok"]:
                return last
            if last.get("status_code") not in {408, 500, 502, 503, 504}:
                return last
            if attempt < retry_limit - 1:
                self.sleeper(delay)
                delay *= 2
        return last

    @staticmethod
    def _vision_content(text: str, image_urls: list[str]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)
        return content

    def _call_text(self, system_prompt: str, user_prompt: str, timeout: float | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return self._retry_post_chat(
            {"model": self.model_text, "messages": messages},
            timeout or self.timeout_text,
        )

    def run_text(
        self,
        *,
        system_prompt: str,
        prompt: str,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, str]:
        """通过同一 Provider 执行纯文本任务，可按调用方配置覆盖模型。

        这用于低成本、短文本的增强或审核步骤。调用方显式传入模型；没有
        覆盖时才沿用 ``DIRECTORS_V2_MODEL_TEXT``，不会改动已有编导链路。
        """
        messages: list[dict[str, Any]] = []
        normalized_system = str(system_prompt or "").strip()
        normalized_prompt = str(prompt or "").strip()
        if normalized_system:
            messages.append({"role": "system", "content": normalized_system})
        messages.append({"role": "user", "content": normalized_prompt})
        selected_model = str(model or "").strip() or self.model_text
        payload: dict[str, Any] = {"model": selected_model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens is not None:
            normalized_max_tokens = int(max_tokens)
            if normalized_max_tokens <= 0:
                raise ValueError("max_tokens 必须为正整数")
            payload["max_tokens"] = normalized_max_tokens
        result = self._retry_post_chat(
            payload,
            timeout or self.timeout_text,
        )
        if not result["ok"]:
            return {
                "output_5_5": (
                    f"文本生成失败! 状态码:{result['status_code']}, 错误:{result.get('error', '')}"
                )
            }
        return {"output_5_5": _text(result.get("content"))}

    def _call_vision(self, system_prompt: str, user_prompt: str, image_urls: list[str], timeout: float | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": self._vision_content(user_prompt, image_urls)})
        return self._retry_post_chat(
            {"model": self.model_vision, "messages": messages},
            timeout or self.timeout_vision,
        )

    def run_plugin(
        self,
        *,
        system_prompt: str,
        prompt: str,
        image_urls: list[str] | None = None,
    ) -> dict[str, str]:
        system_prompt = str(system_prompt or "").strip()
        prompt = str(prompt or "").strip()
        urls = [str(item).strip() for item in (image_urls or []) if str(item).strip()]
        if not system_prompt:
            system_prompt = "你是一个多模态任务处理工具，负责根据文本和图片完成识别、分析、创作等任务。"
        if not prompt and not urls:
            return {"output_5_5": "系统提示: 未收到任何有效的文本或图片输入。"}

        if not urls:
            return self.run_text(system_prompt=system_prompt, prompt=prompt)

        router_system_prompt = (
            "你是多模态任务路由器，判断任务是否复杂：\n"
            "- is_complex=false：只是客观识图\n"
            "- is_complex=true：需要创作、提示词生成、风格分析、文案创作等\n"
            "输出 JSON，不要多余文字：\n"
            '{"is_complex": true/false, "reason": "判断原因", "vision_prompt": "给视觉模型的指令", "text_prompt": "给文字模型的指令"}'
        )
        router_payload = {
            "model": self.model_router,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": router_system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"tool_system_prompt": system_prompt, "user_prompt": prompt, "image_count": len(urls)},
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        router_result = self._retry_post_chat(router_payload, self.timeout_router)
        if not router_result["ok"]:
            task_plan = {"is_complex": True, "reason": "Router 调用失败默认复杂", "vision_prompt": "", "text_prompt": ""}
        else:
            task_plan = _parse_json(router_result.get("content")) or {
                "is_complex": True,
                "reason": "Router JSON解析失败默认复杂",
                "vision_prompt": "",
                "text_prompt": "",
            }

        if task_plan.get("is_complex", True) is False:
            result = self._call_vision(
                "你是简洁准确的视觉问答助手，只根据图片回答问题，不做创作。",
                prompt or "请客观描述图片内容。",
                urls,
            )
            if not result["ok"]:
                return {"output_5_5": f"4o识图失败! 状态码:{result['status_code']}, 错误:{result.get('error', '')}"}
            return {"output_5_5": _text(result.get("content"))}

        vision_result = self._call_vision(
            "你是专业视觉信息提取助手，只提取图片中有用信息，不做创作。",
            _text(task_plan.get("vision_prompt")) or "请分析图片细节供文字模型使用",
            urls,
        )
        if not vision_result["ok"]:
            return {"output_5_5": f"4o素材提炼失败! 状态码:{vision_result['status_code']}, 错误:{vision_result.get('error', '')}"}
        final_prompt = (
            "【工具系统提示词】\n" + system_prompt + "\n\n"
            "【用户输入】\n" + prompt + "\n\n"
            "【视觉模型提取图片信息】\n" + _text(vision_result.get("content")) + "\n\n"
            "【Router文字处理指令】\n" + (_text(task_plan.get("text_prompt")) or "请根据系统提示词和视觉信息生成最终输出") + "\n\n"
            "请严格按系统提示词完成最终输出，直接输出结果，不要解释流程。"
        )
        text_result = self._call_text(system_prompt, final_prompt)
        if not text_result["ok"]:
            return {"output_5_5": f"5.5深度创作失败! 状态码:{text_result['status_code']}, 错误:{text_result.get('error', '')}"}
        return {"output_5_5": _text(text_result.get("content"))}

    def __call__(self, request: Any) -> dict[str, Any]:
        prompt = getattr(request, "text", None)
        if not isinstance(prompt, str):
            raise DirectorsV2PluginTransportError("8364 directors_v2 请求缺少 text")
        plugin_result = self.run_plugin(
            system_prompt=self.system_prompt,
            prompt=prompt,
            image_urls=[],
        )
        parsed = _parse_json(plugin_result.get("output_5_5"))
        if not isinstance(parsed, Mapping):
            raise DirectorsV2PluginTransportError(
                "当前 directors_v2 的 output_5_5 不是 JSON 对象，无法映射为 8364 四字段输出"
            )
        try:
            return normalize_response(parsed)
        except Exception as exc:
            raise DirectorsV2PluginTransportError(
                "当前 directors_v2 的 output_5_5 JSON 不符合 8364 的 director_plan/ok/segment_beats/segments 契约"
            ) from exc


__all__ = [
    "DirectorsV2PluginConfigError",
    "DirectorsV2PluginHTTPTransport",
    "DirectorsV2PluginTransportError",
]
