"""Seed 2.1 Pro 的受限音色候选选择 transport。"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]


class VoiceSelectorTransportError(RuntimeError):
    pass


def _first_env(*names: str) -> str:
    return next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")


def _bearer(value: str) -> str:
    return value if value.lower().startswith("bearer ") else "Bearer " + value


def _request(url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise VoiceSelectorTransportError(f"音色选择模型 HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VoiceSelectorTransportError(f"音色选择模型网络请求失败：{type(exc).__name__}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VoiceSelectorTransportError("音色选择模型响应不是 JSON") from exc
    if not isinstance(result, Mapping):
        raise VoiceSelectorTransportError("音色选择模型响应根节点必须是对象")
    return result


class Seed21ProVoiceSelectorTransport:
    def __init__(self, *, primary_api_key: str, primary_model: str, backup_api_key: str = "", backup_model: str = "", api_url: str = DEFAULT_API_URL, timeout: float = 120.0, requester: Requester | None = None) -> None:
        if not primary_api_key.strip() or not primary_model.strip():
            raise VoiceSelectorTransportError("必须配置音色选择模型的主鉴权和 Seed 2.1 Pro 端点")
        if bool(backup_api_key.strip()) != bool(backup_model.strip()):
            raise VoiceSelectorTransportError("音色选择模型备用鉴权与模型端点必须同时配置")
        self.auths = [(primary_api_key.strip(), primary_model.strip())]
        if backup_api_key.strip():
            self.auths.append((backup_api_key.strip(), backup_model.strip()))
        self.api_url, self.timeout, self.requester = api_url.strip(), float(timeout), requester or _request

    @classmethod
    def from_env(cls) -> "Seed21ProVoiceSelectorTransport":
        return cls(
            primary_api_key=_first_env("VOICE_SELECTOR_PRIMARY_API_KEY", "VOICE_DIRECTOR_PRIMARY_API_KEY", "STORY_WRITER_ARK_API_KEY", "ARK_API_KEY"),
            primary_model=_first_env("VOICE_SELECTOR_PRIMARY_MODEL", "VOICE_DIRECTOR_PRIMARY_MODEL", "STORY_WRITER_ARK_MODEL"),
            backup_api_key=_first_env("VOICE_SELECTOR_BACKUP_API_KEY", "VOICE_DIRECTOR_BACKUP_API_KEY", "STORY_WRITER_ARK_BACKUP_API_KEY", "ARK_BACKUP_API_KEY"),
            backup_model=_first_env("VOICE_SELECTOR_BACKUP_MODEL", "VOICE_DIRECTOR_BACKUP_MODEL", "STORY_WRITER_ARK_BACKUP_MODEL", "ARK_BACKUP_MODEL"),
            api_url=os.environ.get("VOICE_SELECTOR_API_URL", DEFAULT_API_URL), timeout=float(os.environ.get("VOICE_SELECTOR_TIMEOUT", "120")),
        )

    def __call__(self, prompt: Mapping[str, Any]) -> Mapping[str, Any]:
        payload_base = {
            "messages": [
                {"role": "system", "content": "你是受控音色选择器。仅输出合法 JSON。你只能从用户提供的候选 voice_key 中选择 candidate_key，绝不输出、猜测或编造 speaker_id；性别必须遵从 policy。"},
                {"role": "user", "content": json.dumps(dict(prompt), ensure_ascii=False)},
            ], "thinking": {"type": "disabled"}, "temperature": 0.2,
        }
        last_error: Exception | None = None
        for api_key, model in self.auths:
            try:
                response = self.requester(self.api_url, {"Authorization": _bearer(api_key)}, {**payload_base, "model": model}, self.timeout)
                choices = response.get("choices") if isinstance(response, Mapping) else None
                message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
                if not isinstance(message, Mapping):
                    raise VoiceSelectorTransportError("音色选择模型响应缺少 message")
                content = str(message.get("content", "")).replace("```json", "").replace("```", "").strip()
                parsed = json.loads(content)
                if not isinstance(parsed, Mapping):
                    raise VoiceSelectorTransportError("音色选择模型返回根节点必须是对象")
                return parsed
            except Exception as exc:
                last_error = exc
        raise VoiceSelectorTransportError("音色选择模型主、备用鉴权均无法完成请求") from last_error


__all__ = ["Seed21ProVoiceSelectorTransport", "VoiceSelectorTransportError"]
