"""Seed 2.1 Pro 语音导演模型 transport，主/备用鉴权按顺序切换。"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]


class VoiceDirectorTransportError(RuntimeError):
    pass


class VoiceDirectorTransportConfigError(VoiceDirectorTransportError):
    pass


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def load_seed21_pro_models_from_auth_document(path: str | Path) -> list[str]:
    """只从用户指定鉴权文档的 Seed 2.1 Pro 行读取模型端点，不读取其它模型。"""
    document = Path(path)
    if not document.is_file():
        raise VoiceDirectorTransportConfigError("未找到内部鉴权文档")
    content = document.read_text(encoding="utf-8")
    lines = content.splitlines()
    values: list[str] = []
    for index, line in enumerate(lines):
        if re.search(r"(?i)Seed\s*2\.1\s*pro", line):
            for candidate_line in lines[index:index + 3]:
                values.extend(re.findall(r"\bep-[A-Za-z0-9-]+\b", candidate_line))
            break
    if not values:
        raise VoiceDirectorTransportConfigError("内部鉴权文档缺少 Seed 2.1 Pro 模型端点")
    return list(dict.fromkeys(values))


def load_seedance_api_keys_from_auth_document(path: str | Path) -> list[str]:
    """只读取指定鉴权文档中 Seedance 标题下的主/备用方舟密钥。"""
    document = Path(path)
    if not document.is_file():
        raise VoiceDirectorTransportConfigError("未找到内部鉴权文档")
    content = document.read_text(encoding="utf-8")
    heading = re.search(r"(?im)^\s*\\?#\s*[^\r\n]*seedance[^\r\n]*$", content)
    if heading is None:
        raise VoiceDirectorTransportConfigError("内部鉴权文档缺少 Seedance 区段")
    following = content[heading.end():]
    next_heading = re.search(r"(?im)^\s*\\?#", following)
    section = following[:next_heading.start()] if next_heading else following
    values = re.findall(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])", section)
    return list(dict.fromkeys(values))


def load_shot_refinement_seed21_pro_auth_from_document(path: str | Path) -> tuple[str, str, str, str]:
    """读取现有镜头导演主/备用方舟 Key 及其对应 Seed 2.1 Pro 端点。"""
    document = Path(path)
    if not document.is_file():
        raise VoiceDirectorTransportConfigError("未找到内部鉴权文档")
    content = document.read_text(encoding="utf-8")
    key_pattern = r"(?im)^\s*\$env:SHOT\\?_REFINEMENT\\?_(PRIMARY|BACKUP)\\?_API\\?_KEY\s*=\s*[\"'](?P<key>[^\"']+)[\"']"
    key_matches = {match.group(1).lower(): match.group("key").strip() for match in re.finditer(key_pattern, content)}
    models = re.findall(r"(?im)^\s*Seed\s*2\.1\s*pro\s+(ep-[A-Za-z0-9-]+)\s*$", content)
    if not key_matches.get("primary") or not key_matches.get("backup") or len(models) < 2:
        raise VoiceDirectorTransportConfigError("镜头导演主/备用鉴权或 Seed 2.1 Pro 端点不完整")
    return key_matches["primary"], key_matches["backup"], models[0], models[1]


def load_shot_refinement_seed21_turbo_auth_from_document(path: str | Path) -> tuple[str, str, str, str]:
    """读取既有镜头导演双鉴权及 Seed 2.1 turbo 的主/辅模型端点。"""

    document = Path(path)
    if not document.is_file():
        raise VoiceDirectorTransportConfigError("未找到内部鉴权文档")
    content = document.read_text(encoding="utf-8")
    key_pattern = r"(?im)^\s*\$env:SHOT\\?_REFINEMENT\\?_(PRIMARY|BACKUP)\\?_API\\?_KEY\s*=\s*[\"'](?P<key>[^\"']+)[\"']"
    key_matches = {match.group(1).lower(): match.group("key").strip() for match in re.finditer(key_pattern, content)}
    models: list[str] = []
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"(?i)Seed\s*2\.1\s*turbo", line):
            for candidate_line in lines[index:index + 3]:
                models.extend(re.findall(r"\bep-[A-Za-z0-9-]+\b", candidate_line))
    models = list(dict.fromkeys(models))
    if not key_matches.get("primary") or not key_matches.get("backup") or not models:
        raise VoiceDirectorTransportConfigError("镜头导演主/备用鉴权或 Seed 2.1 turbo 端点不完整")
    # 鉴权可以主/辅分离，而两套鉴权共用同一个 Turbo endpoint 是官方常见配置。
    return key_matches["primary"], key_matches["backup"], models[0], models[1] if len(models) > 1 else models[0]


def _bearer(value: str) -> str:
    return value if value.lower().startswith("bearer ") else "Bearer " + value


def _parse_json(value: Any) -> Mapping[str, Any]:
    text = str(value or "").replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VoiceDirectorTransportError("Seed 2.1 Pro 未返回合法 JSON") from exc
    if not isinstance(parsed, Mapping):
        raise VoiceDirectorTransportError("Seed 2.1 Pro 返回根节点必须是对象")
    return parsed


def _request(url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise VoiceDirectorTransportError(f"Seed 2.1 Pro HTTP {exc.code}：{exc.read().decode('utf-8', errors='replace')[:500]}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VoiceDirectorTransportError(f"Seed 2.1 Pro 网络请求失败：{type(exc).__name__}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VoiceDirectorTransportError("Seed 2.1 Pro 响应不是 JSON") from exc
    if not isinstance(result, Mapping):
        raise VoiceDirectorTransportError("Seed 2.1 Pro 响应根节点必须是对象")
    return result


class Seed21ProVoiceDirectorTransport:
    def __init__(self, *, primary_api_key: str, primary_model: str, backup_api_key: str = "", backup_model: str = "", api_url: str = DEFAULT_API_URL, timeout: float = 120.0, requester: Requester | None = None) -> None:
        if not primary_api_key.strip() or not primary_model.strip():
            raise VoiceDirectorTransportConfigError("必须配置 VOICE_DIRECTOR_PRIMARY_API_KEY 和 VOICE_DIRECTOR_PRIMARY_MODEL")
        if bool(backup_api_key.strip()) != bool(backup_model.strip()):
            raise VoiceDirectorTransportConfigError("备用鉴权与备用模型必须同时配置")
        self.auths = [(primary_api_key.strip(), primary_model.strip())]
        if backup_api_key.strip():
            self.auths.append((backup_api_key.strip(), backup_model.strip()))
        self.api_url, self.timeout, self.requester = api_url.strip(), float(timeout), requester or _request

    @classmethod
    def from_env(cls) -> "Seed21ProVoiceDirectorTransport":
        return cls(
            primary_api_key=_first_env(
                "VOICE_DIRECTOR_PRIMARY_API_KEY",
                "STORY_WRITER_ARK_API_KEY",
                "DIRECTORS_V2_ARK_API_KEY",
                "ARK_API_KEY",
            ),
            primary_model=_first_env(
                "VOICE_DIRECTOR_PRIMARY_MODEL",
                "STORY_WRITER_ARK_MODEL",
                "DIRECTORS_V2_ARK_MODEL",
            ),
            backup_api_key=_first_env(
                "VOICE_DIRECTOR_BACKUP_API_KEY",
                "STORY_WRITER_ARK_BACKUP_API_KEY",
                "DIRECTORS_V2_ARK_BACKUP_API_KEY",
                "ARK_BACKUP_API_KEY",
            ),
            backup_model=_first_env(
                "VOICE_DIRECTOR_BACKUP_MODEL",
                "STORY_WRITER_ARK_BACKUP_MODEL",
                "DIRECTORS_V2_ARK_BACKUP_MODEL",
                "ARK_BACKUP_MODEL",
            ),
            api_url=os.environ.get("VOICE_DIRECTOR_API_URL", DEFAULT_API_URL),
            timeout=float(os.environ.get("VOICE_DIRECTOR_TIMEOUT", "120")),
        )

    def __call__(self, prompt: Mapping[str, Any]) -> Mapping[str, Any]:
        payload_base = {
            "messages": [
                {"role": "system", "content": "你是语音导演。仅输出合法 JSON，不要解释。逐段保持原文，输出情绪、情绪强度、语速、音量、音调、句尾静音、停顿计划。"},
                {"role": "user", "content": json.dumps(dict(prompt), ensure_ascii=False)},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0.2,
        }
        last_error: Exception | None = None
        for api_key, model in self.auths:
            try:
                response = self.requester(self.api_url, {"Authorization": _bearer(api_key)}, {**payload_base, "model": model}, self.timeout)
                choices = response.get("choices") if isinstance(response, Mapping) else None
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                    raise VoiceDirectorTransportError("Seed 2.1 Pro 响应缺少 choices")
                message = choices[0].get("message")
                if not isinstance(message, Mapping):
                    raise VoiceDirectorTransportError("Seed 2.1 Pro 响应缺少 message")
                return _parse_json(message.get("content"))
            except Exception as exc:
                last_error = exc
        raise VoiceDirectorTransportError("主、备用鉴权均无法完成语音导演请求") from last_error


__all__ = ["Seed21ProVoiceDirectorTransport", "VoiceDirectorTransportConfigError", "VoiceDirectorTransportError", "load_seed21_pro_models_from_auth_document", "load_seedance_api_keys_from_auth_document", "load_shot_refinement_seed21_pro_auth_from_document", "load_shot_refinement_seed21_turbo_auth_from_document"]
