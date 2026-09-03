"""``directors_v2`` 的真实双模型 HTTP transport。

该模块只负责把用户提供的模型服务接到已经审计过的源码逻辑：

1. Ark Responses 接口执行导演路由；
2. Chat Completions 接口执行导演策略和分段；
3. ``directors_v2_logic.run_directors_v2_logic`` 负责清洗、补全和契约检查。

密钥只能从显式环境变量读取，不能写入代码、fixture 或日志。测试时可注入
``requester``，因此不会因为导入模块而访问网络。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .directors_v2_logic import run_directors_v2_logic


DEFAULT_ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
DEFAULT_ARK_MODEL = "ep-20260609123759-6sv2j"
DEFAULT_GPT_URL = "https://api.aibh.site/v1/chat/completions"
DEFAULT_GPT_MODEL = "gpt-5.5"
DEFAULT_ARK_TIMEOUT = 35.0
DEFAULT_GPT_TIMEOUT = 100.0


class DirectorsV2TransportError(RuntimeError):
    """模型请求或响应结构不符合原插件预期。"""


class DirectorsV2TransportConfigError(DirectorsV2TransportError):
    """模型 transport 的环境变量配置不完整。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _bearer(value: str) -> str:
    return value if value.lower().startswith("bearer ") else "Bearer " + value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(item for item in (_text(part) for part in value) if item).strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "content", "output_text"):
            result = _text(value.get(key))
            if result:
                return result
    return ""


def _response_body(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise DirectorsV2TransportError("模型响应根节点必须是对象")
    if payload.get("error"):
        error = payload["error"]
        if isinstance(error, Mapping):
            message = error.get("message") or error.get("detail") or str(dict(error))
        else:
            message = str(error)
        raise DirectorsV2TransportError("模型服务返回业务错误：" + str(message)[:800])
    return payload


def _extract_ark_text(payload: Any) -> str:
    data = _response_body(payload)
    direct = _text(data.get("output_text"))
    if direct:
        return direct
    parts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []):
            value = _text(content)
            if value:
                parts.append(value)
    result = "\n".join(parts).strip()
    if not result:
        raise DirectorsV2TransportError("Ark Responses 成功但没有返回文本")
    return result


def _extract_gpt_text(payload: Any) -> str:
    data = _response_body(payload)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise DirectorsV2TransportError("Chat Completions 未返回 choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise DirectorsV2TransportError("Chat Completions 的 message 不是对象")
    result = _text(message.get("content"))
    if result:
        return result
    refusal = _text(message.get("refusal"))
    if refusal:
        raise DirectorsV2TransportError("GPT 拒绝返回正文：" + refusal[:800])
    raise DirectorsV2TransportError("Chat Completions 成功但没有返回正文")


def _default_requester(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Any:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise DirectorsV2TransportError(
            f"模型 HTTP 请求失败：{exc.code} {raw[:800]}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DirectorsV2TransportError(f"模型网络请求失败：{type(exc).__name__}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DirectorsV2TransportError("模型响应不是合法 JSON") from exc


class DirectorsV2HTTPTransport:
    """按 8364 的 ``directors_v2`` 源码执行双模型调用。"""

    def __init__(
        self,
        *,
        ark_api_key: str,
        gpt_api_key: str,
        ark_url: str = DEFAULT_ARK_URL,
        ark_model: str = DEFAULT_ARK_MODEL,
        gpt_url: str = DEFAULT_GPT_URL,
        gpt_model: str = DEFAULT_GPT_MODEL,
        ark_timeout: float = DEFAULT_ARK_TIMEOUT,
        gpt_timeout: float = DEFAULT_GPT_TIMEOUT,
        requester: Requester | None = None,
    ) -> None:
        if not ark_api_key.strip() or not gpt_api_key.strip():
            raise DirectorsV2TransportConfigError(
                "需要 DIRECTORS_V2_ARK_API_KEY 和 DIRECTORS_V2_GPT_API_KEY；密钥只从本机环境变量读取"
            )
        self.ark_api_key = ark_api_key.strip()
        self.gpt_api_key = gpt_api_key.strip()
        self.ark_url = ark_url.strip()
        self.ark_model = ark_model.strip()
        self.gpt_url = gpt_url.strip()
        self.gpt_model = gpt_model.strip()
        self.ark_timeout = float(ark_timeout)
        self.gpt_timeout = float(gpt_timeout)
        self.requester = requester or _default_requester

    @classmethod
    def from_env(cls) -> "DirectorsV2HTTPTransport":
        ark_key = _first_env("DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY")
        gpt_key = _first_env("DIRECTORS_V2_GPT_API_KEY", "GPT_API_KEY")
        if not ark_key or not gpt_key:
            raise DirectorsV2TransportConfigError(
                "未配置 directors_v2 模型密钥；请设置 DIRECTORS_V2_ARK_API_KEY 和 DIRECTORS_V2_GPT_API_KEY"
            )
        return cls(
            ark_api_key=ark_key,
            gpt_api_key=gpt_key,
            ark_url=os.environ.get("DIRECTORS_V2_ARK_URL", DEFAULT_ARK_URL),
            ark_model=os.environ.get("DIRECTORS_V2_ARK_MODEL", DEFAULT_ARK_MODEL),
            gpt_url=os.environ.get("DIRECTORS_V2_GPT_URL", DEFAULT_GPT_URL),
            gpt_model=os.environ.get("DIRECTORS_V2_GPT_MODEL", DEFAULT_GPT_MODEL),
            ark_timeout=float(os.environ.get("DIRECTORS_V2_ARK_TIMEOUT", DEFAULT_ARK_TIMEOUT)),
            gpt_timeout=float(os.environ.get("DIRECTORS_V2_GPT_TIMEOUT", DEFAULT_GPT_TIMEOUT)),
        )

    def _call_ark(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.ark_model,
            "instructions": system_prompt,
            "input": user_prompt,
            "temperature": 0.1,
            "max_output_tokens": 380,
            "thinking": {"type": "disabled"},
        }
        response = self.requester(
            self.ark_url,
            {"Authorization": _bearer(self.ark_api_key)},
            payload,
            self.ark_timeout,
        )
        return _extract_ark_text(response)

    def _call_gpt(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.gpt_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = self.requester(
            self.gpt_url,
            {"Authorization": _bearer(self.gpt_api_key), "User-Agent": "Mozilla/5.0"},
            payload,
            self.gpt_timeout,
        )
        return _extract_gpt_text(response)

    def __call__(self, request: Any) -> dict[str, Any]:
        text = getattr(request, "text", None)
        if not isinstance(text, str):
            raise DirectorsV2TransportError("directors_v2 请求缺少字符串 text")
        return run_directors_v2_logic(
            {"text": text},
            pick_model=self._call_ark,
            director_model=self._call_gpt,
        )


__all__ = [
    "DEFAULT_ARK_MODEL",
    "DEFAULT_ARK_URL",
    "DEFAULT_GPT_MODEL",
    "DEFAULT_GPT_URL",
    "DirectorsV2HTTPTransport",
    "DirectorsV2TransportConfigError",
    "DirectorsV2TransportError",
]
