"""方舟电影故事编写器的真实 HTTP transport。

仅用于编导层文字生成：不调用 TTS、素材、视频或剪映服务。鉴权只从
运行时环境变量读取，调用结果交给 ``story_writer`` 的 JSON 契约校验。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ARK_STORY_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
# 该默认值与现有 directors_v2 的方舟端点保持一致；可由环境变量覆盖。
DEFAULT_ARK_STORY_MODEL = "ep-20260609123759-6sv2j"
DEFAULT_AUTH_DOCUMENT = Path.home() / "Documents" / "鉴权信息 .md"


class ArkStoryTransportError(RuntimeError):
    """方舟故事模型请求或响应不可用。"""


class ArkStoryConfigError(ArkStoryTransportError):
    """方舟故事模型的运行时配置不完整。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _brief_transport_error(exc: ArkStoryTransportError) -> str:
    """Expose a retryable failure category without leaking provider response text."""

    message = str(exc)
    status = re.search(r"\bHTTP\s+(\d{3})\b", message, flags=re.IGNORECASE)
    if status:
        return f"HTTP {status.group(1)}"
    if "网络请求失败" in message:
        return "网络请求失败"
    if "响应不是合法 JSON" in message:
        return "响应不是合法 JSON"
    if "未找到 output_text" in message:
        return "响应缺少 output_text"
    if "返回业务错误" in message:
        return "模型返回业务错误"
    return type(exc).__name__


def _extra_api_keys_from_env(name: str) -> list[str]:
    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _model_fallbacks_from_env(name: str) -> list[str]:
    """Read the ordered, non-secret model access-point list exported by API 管理."""

    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _api_group_order_from_env() -> list[str]:
    """Read the API1/API2 order exported by the API management runtime."""

    try:
        value = json.loads(os.environ.get("API_MANAGEMENT_LANGUAGE_MODEL_ORDER", ""))
    except json.JSONDecodeError:
        value = []
    if not isinstance(value, list):
        return ["story-writing", "director-seed21"]
    order = list(dict.fromkeys(
        str(item).strip() for item in value if isinstance(item, str) and item.strip()
    ))
    return order or ["story-writing", "director-seed21"]


def _is_chat_completions_url(url: str) -> bool:
    return "/chat/completions" in str(url or "").lower()


def load_story_auth_from_document(path: str | Path) -> list[tuple[str, str]]:
    """Read existing primary/backup Ark credentials without persisting or logging them."""
    document = Path(path)
    if not document.is_file():
        return []
    content = document.read_text(encoding="utf-8")
    key_pattern = r"(?im)^\s*(?:export\s+)?ARK\\?_API\\?_KEY\s*=\s*[\"'](?P<key>[^\"']+)[\"']"
    keys = [match.group("key").strip() for match in re.finditer(key_pattern, content) if match.group("key").strip()]
    models = re.findall(r"(?im)^\s*Seed\s*2\.1\s*pro\s+(ep-[A-Za-z0-9-]+)\s*$", content)
    if not keys:
        return []
    if not models:
        models = [DEFAULT_ARK_STORY_MODEL] * len(keys)
    return list(dict.fromkeys(zip(keys, models)))


def _text(value: Any) -> str:
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


def _extract_text(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        raise ArkStoryTransportError("方舟故事模型响应根节点必须是对象")
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, Mapping) else str(error)
        raise ArkStoryTransportError(f"方舟故事模型返回业务错误：{str(message)[:500]}")
    direct = _text(payload.get("output_text"))
    if direct:
        return direct
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        result = _text(message.get("content") if isinstance(message, Mapping) else message)
        if result:
            return result
    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []):
            result = _text(content)
            if result:
                parts.append(result)
    result = "\n".join(parts).strip()
    if not result:
        raise ArkStoryTransportError("方舟故事模型成功响应中未找到 output_text")
    return result


def _request(
    url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float
) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise ArkStoryTransportError(f"方舟故事模型 HTTP {exc.code}：{raw[:500]}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        # Python 3.14 的 urllib 在当前 Windows 环境出现过 TLS EOF；只有发生
        # 连接级故障时才回退 requests，不改变正常响应、HTTP 错误或业务错误语义。
        try:
            import requests

            response = requests.post(
                url,
                json=dict(payload),
                headers={"Accept": "application/json", **headers},
                timeout=timeout,
            )
        except Exception as fallback_exc:
            raise ArkStoryTransportError(
                f"方舟故事模型网络请求失败：{type(exc).__name__}；requests 回退失败：{type(fallback_exc).__name__}"
            ) from fallback_exc
        raw = response.text
        if response.status_code >= 400:
            raise ArkStoryTransportError(f"方舟故事模型 HTTP {response.status_code}：{raw[:500]}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArkStoryTransportError("方舟故事模型响应不是合法 JSON") from exc
    if not isinstance(result, Mapping):
        raise ArkStoryTransportError("方舟故事模型响应根节点必须是对象")
    return result


@dataclass(frozen=True)
class ArkStoryConfig:
    api_key: str
    model: str = DEFAULT_ARK_STORY_MODEL
    api_url: str = DEFAULT_ARK_STORY_URL
    timeout: float = 90.0

    @classmethod
    def from_env(cls) -> "ArkStoryConfig":
        api_key = _first_env("STORY_WRITER_ARK_API_KEY", "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY")
        if not api_key:
            raise ArkStoryConfigError(
                "未配置故事编写器方舟鉴权；请设置 STORY_WRITER_ARK_API_KEY，"
                "或复用 DIRECTORS_V2_ARK_API_KEY/ARK_API_KEY"
            )
        return cls(
            api_key=api_key,
            model=_first_env("STORY_WRITER_ARK_MODEL", "DIRECTORS_V2_ARK_MODEL") or DEFAULT_ARK_STORY_MODEL,
            api_url=_first_env("STORY_WRITER_ARK_URL", "DIRECTORS_V2_ARK_URL") or DEFAULT_ARK_STORY_URL,
            timeout=float(_first_env("STORY_WRITER_ARK_TIMEOUT") or "90"),
        )


class ArkStoryHTTPTransport:
    """将电影故事编写器的 system/user prompt 调用到方舟 Responses API。"""

    def __init__(self, config: ArkStoryConfig, *, requester: Requester | None = None) -> None:
        if not config.api_key.strip():
            raise ArkStoryConfigError("STORY_WRITER_ARK_API_KEY 不能为空")
        if not config.model.strip() or not config.api_url.strip():
            raise ArkStoryConfigError("故事模型 model 和 api_url 不能为空")
        self.config = config
        self.requester = requester or _request

    @classmethod
    def from_env(cls) -> "ArkStoryHTTPTransport":
        return cls(ArkStoryConfig.from_env())

    def run_text(
        self,
        *,
        system_prompt: str,
        prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """调用既有 Ark 文本通道，并允许受控的小任务覆盖生成参数。

        覆盖只在当前请求内生效，不修改共享 transport 的模型或鉴权配置。
        """
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ArkStoryTransportError("system_prompt 必须是非空字符串")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ArkStoryTransportError("prompt 必须是非空字符串")
        resolved_model = str(model or self.config.model).strip()
        if not resolved_model:
            raise ArkStoryTransportError("model 必须是非空字符串")
        resolved_temperature = 0.35 if temperature is None else float(temperature)
        if not 0 <= resolved_temperature <= 2:
            raise ArkStoryTransportError("temperature 必须在 0 到 2 之间")
        resolved_max_tokens = 3200 if max_tokens is None else int(max_tokens)
        if resolved_max_tokens <= 0:
            raise ArkStoryTransportError("max_tokens 必须为正整数")
        resolved_timeout = self.config.timeout if timeout is None else float(timeout)
        if resolved_timeout <= 0:
            raise ArkStoryTransportError("timeout 必须为正数")
        if _is_chat_completions_url(self.config.api_url):
            payload = {
                "model": resolved_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": resolved_temperature,
                "max_tokens": resolved_max_tokens,
                "thinking": {"type": "disabled"},
            }
        else:
            payload = {
                "model": resolved_model,
                "instructions": system_prompt,
                "input": prompt,
                "temperature": resolved_temperature,
                "max_output_tokens": resolved_max_tokens,
                "thinking": {"type": "disabled"},
            }
        response = self.requester(
            self.config.api_url,
            {"Authorization": "Bearer " + self.config.api_key},
            payload,
            resolved_timeout,
        )
        return _extract_text(response)

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        return self.run_text(system_prompt=system_prompt, prompt=user_prompt)


class ArkStoryFailoverHTTPTransport:
    """Use primary then backup Ark credentials for story and style work."""

    def __init__(self, transports: list[ArkStoryHTTPTransport]) -> None:
        if not transports:
            raise ArkStoryConfigError("未配置故事编写器方舟鉴权")
        self.transports = transports
        # Sanitized runtime diagnostics for live verification.  Never retain
        # credentials or provider response bodies here.
        self.last_success: dict[str, Any] | None = None
        self.last_failures: list[str] = []

    @classmethod
    def _director_seed21_api_fallbacks(cls) -> list[ArkStoryHTTPTransport]:
        """Build the API2 channel from the page-managed runtime credentials.

        API1/API2 fallback is a different layer from the internal backup Key
        of one channel.  The API management page exports both director keys
        and model order into this process; no legacy auth document is read.
        """

        primary = _first_env("VOICE_DIRECTOR_PRIMARY_API_KEY", "SHOT_REFINEMENT_PRIMARY_API_KEY")
        if not primary:
            return []
        backup = _first_env("VOICE_DIRECTOR_BACKUP_API_KEY", "SHOT_REFINEMENT_BACKUP_API_KEY")
        api_url = _first_env("VOICE_DIRECTOR_API_URL", "SHOT_REFINEMENT_API_URL") or DEFAULT_ARK_STORY_URL
        try:
            timeout = float(
                _first_env("VOICE_DIRECTOR_TIMEOUT", "SHOT_REFINEMENT_TIMEOUT") or "120"
            )
        except ValueError as exc:
            raise ArkStoryConfigError("VOICE_DIRECTOR_TIMEOUT 必须是数字") from exc
        strategy = (
            _first_env("VOICE_DIRECTOR_FAILOVER_STRATEGY", "SHOT_REFINEMENT_FAILOVER_STRATEGY")
            or "primary_then_backup"
        ).strip().lower()
        if strategy not in {"primary_then_backup", "primary_only"}:
            raise ArkStoryConfigError(
                "VOICE_DIRECTOR_FAILOVER_STRATEGY 只支持 primary_then_backup 或 primary_only"
            )
        primary_model = _first_env("VOICE_DIRECTOR_PRIMARY_MODEL", "SHOT_REFINEMENT_MODEL")
        backup_model = _first_env("VOICE_DIRECTOR_BACKUP_MODEL") or primary_model
        model_fallbacks = _model_fallbacks_from_env("API_MANAGEMENT_MODEL_FALLBACKS_DIRECTOR_SEED21")
        models = (
            [primary_model]
            if strategy == "primary_only"
            else list(dict.fromkeys([primary_model, *model_fallbacks, backup_model]))
        )
        models = [model for model in models if model]
        if not models:
            raise ArkStoryConfigError("director-seed21 未配置模型接入点")
        configs = [(primary, model) for model in models]
        if strategy == "primary_then_backup" and backup and backup != primary:
            configs.extend((backup, model) for model in models)
        existing_pairs = set(configs)
        for key in _extra_api_keys_from_env("API_MANAGEMENT_EXTRA_KEYS_DIRECTOR_SEED21"):
            for model in models:
                pair = (key, model)
                if pair not in existing_pairs:
                    configs.append(pair)
                    existing_pairs.add(pair)
        return [
            ArkStoryHTTPTransport(
                ArkStoryConfig(api_key=key, model=model, api_url=api_url, timeout=timeout)
            )
            for key, model in configs
        ]

    @classmethod
    def _with_language_model_api_fallback(
        cls, transport: "ArkStoryFailoverHTTPTransport"
    ) -> "ArkStoryFailoverHTTPTransport":
        order = _api_group_order_from_env()
        if "director-seed21" not in order:
            return transport
        api2 = cls._director_seed21_api_fallbacks()
        if not api2:
            return transport
        # The base role transport normally represents API1.  Respect an
        # explicit API management order if the operator intentionally moves
        # API2 ahead of API1; each channel still keeps its own model and
        # internal-key order intact.
        try:
            api1_index = order.index("story-writing")
            api2_index = order.index("director-seed21")
        except ValueError:
            api1_index, api2_index = 0, 1
        ordered = (
            [*api2, *transport.transports]
            if api2_index < api1_index
            else [*transport.transports, *api2]
        )
        return cls(ordered)

    @classmethod
    def from_runtime_config(
        cls, *, include_language_model_api_fallback: bool = False
    ) -> "ArkStoryFailoverHTTPTransport":
        primary = _first_env("STORY_WRITER_ARK_API_KEY", "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY")
        backup = _first_env("STORY_WRITER_ARK_BACKUP_API_KEY", "DIRECTORS_V2_ARK_BACKUP_API_KEY", "ARK_BACKUP_API_KEY")
        api_url = _first_env("STORY_WRITER_ARK_URL", "DIRECTORS_V2_ARK_URL") or DEFAULT_ARK_STORY_URL
        try:
            timeout = float(_first_env("STORY_WRITER_ARK_TIMEOUT") or "90")
        except ValueError as exc:
            raise ArkStoryConfigError("STORY_WRITER_ARK_TIMEOUT 必须是数字") from exc
        strategy = (_first_env("STORY_WRITER_FAILOVER_STRATEGY") or "primary_then_backup").strip().lower()
        if strategy not in {"primary_then_backup", "primary_only"}:
            raise ArkStoryConfigError("STORY_WRITER_FAILOVER_STRATEGY 只支持 primary_then_backup 或 primary_only")
        model_fallbacks = _model_fallbacks_from_env("API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING")
        if primary:
            primary_model = _first_env("STORY_WRITER_ARK_MODEL", "DIRECTORS_V2_ARK_MODEL") or DEFAULT_ARK_STORY_MODEL
            backup_model = _first_env("STORY_WRITER_ARK_BACKUP_MODEL", "DIRECTORS_V2_ARK_BACKUP_MODEL") or primary_model
            models = (
                [primary_model]
                if strategy == "primary_only"
                else list(dict.fromkeys([primary_model, *model_fallbacks, backup_model]))
            )
            configs = [(primary, model) for model in models]
            if strategy == "primary_then_backup" and backup:
                configs.extend((backup, model) for model in models)
        else:
            raw_auth_document = os.environ.get("STORY_WRITER_AUTH_DOCUMENT")
            auth_configs = [] if raw_auth_document == "" else load_story_auth_from_document(
                raw_auth_document or str(DEFAULT_AUTH_DOCUMENT)
            )
            # API 管理页可能只提供 STORY_WRITER_ARK_MODEL，而共享鉴权文档
            # 提供 API Key。模型槽位必须覆盖文档中的旧模型，否则配置页的
            # 故事/审核模型选择不会真正生效。
            configured_model = _first_env("STORY_WRITER_ARK_MODEL", "DIRECTORS_V2_ARK_MODEL")
            configured_backup_model = _first_env("STORY_WRITER_ARK_BACKUP_MODEL", "DIRECTORS_V2_ARK_BACKUP_MODEL")
            if configured_model or configured_backup_model or model_fallbacks:
                primary_model = configured_model or DEFAULT_ARK_STORY_MODEL
                backup_model = configured_backup_model or primary_model
                models = (
                    [primary_model]
                    if strategy == "primary_only"
                    else list(dict.fromkeys([primary_model, *model_fallbacks, backup_model]))
                )
                keys = [key for key, _model in auth_configs]
                configs = [(key, model) for key in keys for model in models]
                if strategy == "primary_only":
                    configs = configs[:1]
            else:
                configs = auth_configs
                if strategy == "primary_only":
                    configs = configs[:1]
        if strategy == "primary_then_backup":
            fallback_models = list(dict.fromkeys(
                model for _key, model in configs if model
            )) or [DEFAULT_ARK_STORY_MODEL]
            existing_pairs = set(configs)
            for key in _extra_api_keys_from_env("API_MANAGEMENT_EXTRA_KEYS_STORY_WRITING"):
                for model in fallback_models:
                    pair = (key, model)
                    if pair not in existing_pairs:
                        configs.append(pair)
                        existing_pairs.add(pair)
        transport = cls([
            ArkStoryHTTPTransport(
                ArkStoryConfig(api_key=key, model=model, api_url=api_url, timeout=timeout)
            )
            for key, model in configs
        ])
        return (
            cls._with_language_model_api_fallback(transport)
            if include_language_model_api_fallback
            else transport
        )

    @classmethod
    def _from_optional_role_runtime_config(cls, role_prefix: str) -> "ArkStoryFailoverHTTPTransport":
        """Use an optional role-specific model, falling back to story-writing config."""

        role_names = {
            "key": f"{role_prefix}_ARK_API_KEY",
            "backup_key": f"{role_prefix}_ARK_BACKUP_API_KEY",
            "model": f"{role_prefix}_ARK_MODEL",
            "backup_model": f"{role_prefix}_ARK_BACKUP_MODEL",
            "url": f"{role_prefix}_ARK_URL",
            "timeout": f"{role_prefix}_ARK_TIMEOUT",
        }
        overrides = {name: _first_env(env_name) for name, env_name in role_names.items()}
        if not any(overrides.values()):
            return cls.from_runtime_config()

        primary = overrides["key"] or _first_env("STORY_WRITER_ARK_API_KEY", "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY")
        backup = overrides["backup_key"] or _first_env("STORY_WRITER_ARK_BACKUP_API_KEY", "DIRECTORS_V2_ARK_BACKUP_API_KEY", "ARK_BACKUP_API_KEY")
        api_url = overrides["url"] or _first_env("STORY_WRITER_ARK_URL", "DIRECTORS_V2_ARK_URL") or DEFAULT_ARK_STORY_URL
        try:
            timeout = float(overrides["timeout"] or _first_env("STORY_WRITER_ARK_TIMEOUT") or "90")
        except ValueError as exc:
            raise ArkStoryConfigError(f"{role_prefix}_ARK_TIMEOUT 必须是数字") from exc
        strategy = (_first_env("STORY_WRITER_FAILOVER_STRATEGY") or "primary_then_backup").strip().lower()
        if strategy not in {"primary_then_backup", "primary_only"}:
            raise ArkStoryConfigError("STORY_WRITER_FAILOVER_STRATEGY 只支持 primary_then_backup 或 primary_only")

        model_fallbacks = _model_fallbacks_from_env("API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING")
        if primary:
            primary_model = overrides["model"] or _first_env("STORY_WRITER_ARK_MODEL", "DIRECTORS_V2_ARK_MODEL") or DEFAULT_ARK_STORY_MODEL
            backup_model = overrides["backup_model"] or _first_env("STORY_WRITER_ARK_BACKUP_MODEL") or primary_model
            # 角色专用模型仍作为首选；API 管理页导出的其余接入点按顺序
            # 作为同一 API 内的模型级回退，不改变 API 凭据层次。
            models = (
                [primary_model]
                if strategy == "primary_only"
                else list(dict.fromkeys([primary_model, *model_fallbacks, backup_model]))
            )
            configs = [(primary, model) for model in models]
            if strategy == "primary_then_backup" and backup and backup != primary:
                configs.extend((backup, model) for model in models)
        else:
            raw_auth_document = os.environ.get("STORY_WRITER_AUTH_DOCUMENT")
            auth_configs = [] if raw_auth_document == "" else load_story_auth_from_document(
                raw_auth_document or str(DEFAULT_AUTH_DOCUMENT)
            )
            # API 管理页通常只保存角色模型槽位，鉴权仍复用共享故事鉴权
            # 文档。角色模型覆盖不能因为没有 CONTENT_ANALYZER_ARK_API_KEY /
            # STYLE_ADAPTER_ARK_API_KEY 就被忽略；把角色首选模型放到共享
            # 鉴权的每个可用凭据前面，再保留 API 管理导出的模型级回退顺序。
            if overrides["model"] or overrides["backup_model"]:
                primary_model = overrides["model"] or _first_env("STORY_WRITER_ARK_MODEL", "DIRECTORS_V2_ARK_MODEL") or DEFAULT_ARK_STORY_MODEL
                backup_model = overrides["backup_model"] or _first_env("STORY_WRITER_ARK_BACKUP_MODEL") or primary_model
                models = (
                    [primary_model]
                    if strategy == "primary_only"
                    else list(dict.fromkeys([primary_model, *model_fallbacks, backup_model]))
                )
                keys = [key for key, _model in auth_configs]
                configs = [(key, model) for key in keys for model in models]
                if strategy == "primary_only":
                    configs = configs[:1]
            else:
                configs = auth_configs
                if strategy == "primary_only":
                    configs = configs[:1]
        if not configs:
            raise ArkStoryConfigError(
                f"未配置{role_prefix}角色模型鉴权；请设置 {role_prefix}_ARK_API_KEY，或复用故事模型鉴权"
            )
        if strategy == "primary_then_backup":
            fallback_models = list(dict.fromkeys(
                model for _key, model in configs if model
            )) or [DEFAULT_ARK_STORY_MODEL]
            existing_pairs = set(configs)
            for key in _extra_api_keys_from_env("API_MANAGEMENT_EXTRA_KEYS_STORY_WRITING"):
                for model in fallback_models:
                    pair = (key, model)
                    if pair not in existing_pairs:
                        configs.append(pair)
                        existing_pairs.add(pair)
        return cls([
            ArkStoryHTTPTransport(
                ArkStoryConfig(api_key=key, model=model, api_url=api_url, timeout=timeout)
            )
            for key, model in configs
        ])

    @classmethod
    def from_content_runtime_config(
        cls, *, include_language_model_api_fallback: bool | None = None
    ) -> "ArkStoryFailoverHTTPTransport":
        """Build the optional neutral content-analysis transport."""

        transport = cls._from_optional_role_runtime_config("CONTENT_ANALYZER")
        if include_language_model_api_fallback is None:
            include_language_model_api_fallback = bool(
                os.environ.get("API_MANAGEMENT_LANGUAGE_MODEL_ORDER", "").strip()
            )
        return (
            cls._with_language_model_api_fallback(transport)
            if include_language_model_api_fallback
            else transport
        )

    @classmethod
    def from_style_runtime_config(
        cls, *, include_language_model_api_fallback: bool | None = None
    ) -> "ArkStoryFailoverHTTPTransport":
        """Build the optional style-adaptation transport."""

        transport = cls._from_optional_role_runtime_config("STYLE_ADAPTER")
        if include_language_model_api_fallback is None:
            include_language_model_api_fallback = bool(
                os.environ.get("API_MANAGEMENT_LANGUAGE_MODEL_ORDER", "").strip()
            )
        return (
            cls._with_language_model_api_fallback(transport)
            if include_language_model_api_fallback
            else transport
        )

    @classmethod
    def from_review_runtime_config(
        cls, *, include_language_model_api_fallback: bool | None = None
    ) -> "ArkStoryFailoverHTTPTransport":
        """Build the optional copy-review transport.

        With no COPY_REVIEW_* override, reuse the normal story transport. This
        keeps existing deployments unchanged while allowing review to use a
        different endpoint/model when the user explicitly configures one.
        """

        review_key = _first_env("COPY_REVIEW_ARK_API_KEY")
        review_backup = _first_env("COPY_REVIEW_ARK_BACKUP_API_KEY")
        review_model = _first_env("COPY_REVIEW_ARK_MODEL")
        review_backup_model = _first_env("COPY_REVIEW_ARK_BACKUP_MODEL")
        review_url = _first_env("COPY_REVIEW_ARK_URL")
        review_timeout = _first_env("COPY_REVIEW_ARK_TIMEOUT")
        if not any((review_key, review_backup, review_model, review_backup_model, review_url, review_timeout)):
            transport = cls.from_runtime_config()
            if include_language_model_api_fallback is None:
                include_language_model_api_fallback = bool(
                    os.environ.get("API_MANAGEMENT_LANGUAGE_MODEL_ORDER", "").strip()
                )
            return (
                cls._with_language_model_api_fallback(transport)
                if include_language_model_api_fallback
                else transport
            )

        primary = review_key or _first_env("STORY_WRITER_ARK_API_KEY", "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY")
        if not primary:
            raise ArkStoryConfigError(
                "已配置 COPY_REVIEW_* 审核模型覆盖，但没有审核模型鉴权；请设置 COPY_REVIEW_ARK_API_KEY"
            )
        model = review_model or DEFAULT_ARK_STORY_MODEL
        url = review_url or DEFAULT_ARK_STORY_URL
        timeout = float(review_timeout or "90")
        configs = [(primary, model)]
        if review_backup:
            configs.append((review_backup, review_backup_model or model))
        transport = cls([
            ArkStoryHTTPTransport(
                ArkStoryConfig(api_key=key, model=selected_model, api_url=url, timeout=timeout)
            )
            for key, selected_model in configs
        ])
        if include_language_model_api_fallback is None:
            include_language_model_api_fallback = bool(
                os.environ.get("API_MANAGEMENT_LANGUAGE_MODEL_ORDER", "").strip()
            )
        return (
            cls._with_language_model_api_fallback(transport)
            if include_language_model_api_fallback
            else transport
        )

    def call_from(self, start_index: int, system_prompt: str, user_prompt: str) -> str:
        """Call the failover chain from a sanitized transport index.

        This is used only when a provider returns a syntactically valid but
        semantically wrong JSON shape.  It lets the caller bypass a channel
        that produced a false-success response without exposing credentials.
        """

        try:
            start = max(0, int(start_index))
        except (TypeError, ValueError):
            start = 0
        last_error: ArkStoryTransportError | None = None
        failures: list[str] = []
        self.last_success = None
        self.last_failures = []
        for index, transport in enumerate(self.transports[start:], start=start):
            try:
                result = transport(system_prompt, user_prompt)
                self.last_success = {
                    "attempt": index + 1,
                    "api_url": transport.config.api_url,
                    "model": transport.config.model,
                }
                return result
            except ArkStoryTransportError as exc:
                last_error = exc
                label = "主" if index == 0 else f"备用{index}"
                failures.append(f"{label}：{_brief_transport_error(exc)}")
        self.last_failures = failures
        detail = "；".join(failures) or "无可用请求通道"
        raise ArkStoryTransportError(
            f"主、备用方舟模型请求均未完成（{detail}）"
        ) from last_error

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        return self.call_from(0, system_prompt, user_prompt)


__all__ = [
    "ArkStoryConfig",
    "ArkStoryConfigError",
    "ArkStoryFailoverHTTPTransport",
    "ArkStoryHTTPTransport",
    "ArkStoryTransportError",
    "DEFAULT_ARK_STORY_MODEL",
    "DEFAULT_ARK_STORY_URL",
    "load_story_auth_from_document",
]
