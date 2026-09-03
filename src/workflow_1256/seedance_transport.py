"""Seedance video_generate/video_query 的安全配置 transport。

原始插件逻辑保留在 ``video_generate.py`` 和 ``video_query.py``，本模块只负责
从本机环境组装鉴权/TOS 配置，并把请求执行委托给原始逻辑。没有配置密钥时
立即阻断，不读取或打印代码中的脱敏占位值。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any


class SeedanceTransportError(RuntimeError):
    """Seedance transport 配置或执行失败。"""


DEFAULT_SEEDANCE_1_5_MODEL = "doubao-seedance-1-5-pro-251215"
DEFAULT_SEEDANCE_1_0_MODEL = "doubao-seedance-1-0-pro-250528"


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _extra_api_keys_from_env(name: str) -> tuple[str, ...]:
    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _as_bearer_token(api_key: str) -> str:
    """把本机保存的方舟裸 Key 规范为 HTTP Authorization 值。

    方舟控制台导出的 ``ARK_API_KEY`` 是裸值，而内容生成接口要求
    ``Authorization: Bearer <ARK_API_KEY>``。原 Coze 插件可把完整请求头
    值直接写入配置；本地 transport 同时兼容这两种安全保存方式。
    """

    normalized = api_key.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"\"", "'"}
    ):
        normalized = normalized[1:-1].strip()
    if not normalized:
        return ""
    if normalized.lower().startswith("bearer "):
        return normalized
    return f"Bearer {normalized}"


@dataclass(frozen=True)
class SeedanceConfig:
    primary_api_key: str
    # model_ep 是旧版单模型配置，继续保留并作为该账号的 1.0 回退接入点。
    primary_model_ep: str = ""
    backup_api_key: str = ""
    backup_model_ep: str = ""
    third_api_key: str = ""
    third_model_ep: str = ""
    fourth_api_key: str = ""
    fourth_model_ep: str = ""
    extra_api_keys: tuple[str, ...] = ()
    # 1.5 优先使用；1.0 是额度/权限级失败后的降级模型。两者既可传
    # 官方 Model ID，也可传用户项目中的 Endpoint ID。
    model_1_5_ep: str = DEFAULT_SEEDANCE_1_5_MODEL
    model_1_0_ep: str = DEFAULT_SEEDANCE_1_0_MODEL
    api_url: str = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    tos_access_key: str = ""
    tos_secret_key: str = ""
    tos_bucket: str = ""
    tos_endpoint: str = "https://tos-cn-beijing.volces.com"
    tos_region: str = "cn-beijing"
    tos_path_prefix: str = "seedance-input-frames"
    failover_strategy: str = "primary_then_backup"

    @classmethod
    def from_env(cls) -> "SeedanceConfig":
        primary_api_key = os.environ.get("SEEDANCE_PRIMARY_API_KEY", "").strip()
        if not primary_api_key:
            raise SeedanceTransportError(
                "未配置 SEEDANCE_PRIMARY_API_KEY；"
                "不会使用源码中的脱敏占位值"
            )
        return cls(
            primary_api_key=primary_api_key,
            primary_model_ep=os.environ.get("SEEDANCE_PRIMARY_MODEL_EP", "").strip(),
            backup_api_key=os.environ.get("SEEDANCE_BACKUP_API_KEY", "").strip(),
            backup_model_ep=os.environ.get("SEEDANCE_BACKUP_MODEL_EP", "").strip(),
            third_api_key=_first_env(
                "SEEDANCE_THIRD_API_KEY", "SEEDANCE_AUTH3_API_KEY", "SEEDANCE_AUTH_3_API_KEY"
            ),
            third_model_ep=_first_env(
                "SEEDANCE_THIRD_MODEL_EP", "SEEDANCE_AUTH3_MODEL_EP", "SEEDANCE_AUTH_3_MODEL_EP"
            ),
            fourth_api_key=_first_env(
                "SEEDANCE_FOURTH_API_KEY", "SEEDANCE_AUTH4_API_KEY", "SEEDANCE_AUTH_4_API_KEY"
            ),
            fourth_model_ep=_first_env(
                "SEEDANCE_FOURTH_MODEL_EP", "SEEDANCE_AUTH4_MODEL_EP", "SEEDANCE_AUTH_4_MODEL_EP"
            ),
            extra_api_keys=_extra_api_keys_from_env("API_MANAGEMENT_EXTRA_KEYS_VIDEO_GENERATION"),
            model_1_5_ep=_first_env(
                "SEEDANCE_MODEL_1_5_EP",
                "SEEDANCE_1_5_MODEL_EP",
                "SEEDANCE_PRIMARY_MODEL_1_5_EP",
                default=DEFAULT_SEEDANCE_1_5_MODEL,
            ),
            model_1_0_ep=_first_env(
                "SEEDANCE_MODEL_1_0_EP",
                "SEEDANCE_1_0_MODEL_EP",
                default=DEFAULT_SEEDANCE_1_0_MODEL,
            ),
            api_url=os.environ.get(
                "SEEDANCE_API_URL",
                "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
            ).strip(),
            tos_access_key=os.environ.get("SEEDANCE_TOS_ACCESS_KEY", "").strip(),
            tos_secret_key=os.environ.get("SEEDANCE_TOS_SECRET_KEY", "").strip(),
            tos_bucket=os.environ.get("SEEDANCE_TOS_BUCKET", "").strip(),
            tos_endpoint=os.environ.get(
                "SEEDANCE_TOS_ENDPOINT", "https://tos-cn-beijing.volces.com"
            ).strip(),
            tos_region=os.environ.get("SEEDANCE_TOS_REGION", "cn-beijing").strip(),
            tos_path_prefix=os.environ.get(
                "SEEDANCE_TOS_PATH_PREFIX", "seedance-input-frames"
            ).strip(),
            failover_strategy=os.environ.get("SEEDANCE_FAILOVER_STRATEGY", "primary_then_backup").strip(),
        )

    @classmethod
    def from_api_keys(
        cls,
        api_keys: list[str] | tuple[str, ...],
        *,
        legacy_model_eps: list[str] | tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> "SeedanceConfig":
        """把鉴权文档中的有序密钥装配为可连续轮换配置。

        旧项目只把前两套密钥装入配置；这里保留原有主/备用字段，并把
        第三、第四套和后续 Key 显式接入。legacy_model_eps 只用于兼容历史 1.0
        Endpoint，1.5/1.0 的优先级仍由 ``auth_configs`` 统一生成。
        """
        keys = [str(value).strip() for value in api_keys if str(value).strip()]
        if not keys:
            raise SeedanceTransportError("未读取到任何 Seedance API Key")
        endpoints = list(legacy_model_eps or [])
        endpoint = lambda index: endpoints[index].strip() if index < len(endpoints) else ""
        kwargs.setdefault(
            "api_url",
            os.environ.get(
                "SEEDANCE_API_URL",
                "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
            ).strip(),
        )
        kwargs.setdefault(
            "failover_strategy",
            os.environ.get("SEEDANCE_FAILOVER_STRATEGY", "primary_then_backup").strip(),
        )
        kwargs.setdefault("extra_api_keys", tuple(keys[4:]))
        return cls(
            primary_api_key=keys[0],
            primary_model_ep=endpoint(0),
            backup_api_key=keys[1] if len(keys) > 1 else "",
            backup_model_ep=endpoint(1),
            third_api_key=keys[2] if len(keys) > 2 else "",
            third_model_ep=endpoint(2),
            fourth_api_key=keys[3] if len(keys) > 3 else "",
            fourth_model_ep=endpoint(3),
            **kwargs,
        )

    def auth_configs(self) -> list[dict[str, str]]:
        raw_configs = [
            ("主账号", self.primary_api_key, self.primary_model_ep),
            ("备用账号", self.backup_api_key, self.backup_model_ep),
            ("第三账号", self.third_api_key, self.third_model_ep),
            ("第四账号", self.fourth_api_key, self.fourth_model_ep),
        ]
        raw_configs.extend(
            (f"第{index + 5}级账号", api_key, "")
            for index, api_key in enumerate(self.extra_api_keys)
        )
        configs: list[dict[str, str]] = []
        strategy = str(self.failover_strategy or "primary_then_backup").strip().lower()
        if strategy not in {"primary_then_backup", "primary_only"}:
            raise SeedanceTransportError(
                "SEEDANCE_FAILOVER_STRATEGY 只支持 primary_then_backup 或 primary_only"
            )
        for index, (name, api_key, legacy_model_ep) in enumerate(raw_configs):
            if strategy == "primary_only" and index > 0:
                break
            if not api_key and not legacy_model_ep:
                continue
            legacy_model_ep = legacy_model_ep or self.model_1_0_ep
            configs.append({
                "name": name,
                "api_key": _as_bearer_token(api_key),
                "model_ep": legacy_model_ep,
                "model_ep_1_5": self.model_1_5_ep,
                "model_ep_1_0": legacy_model_ep or self.model_1_0_ep,
            })
        return configs

    def tos_configs(self) -> list[dict[str, str]]:
        if not (self.tos_access_key and self.tos_secret_key and self.tos_bucket):
            return []
        return [{
            "name": "TOS",
            "ak": self.tos_access_key,
            "sk": self.tos_secret_key,
            "bucket": self.tos_bucket,
            "endpoint": self.tos_endpoint,
            "region": self.tos_region,
        }]


class SeedanceHTTPTransport:
    """为两个 Seedance 节点提供可注入、可审计的执行入口。"""

    def __init__(self, config: SeedanceConfig, *, requests_module: Any | None = None) -> None:
        self.config = config
        if requests_module is None:
            try:
                import requests as requests_module  # type: ignore
            except ImportError as exc:
                raise SeedanceTransportError("当前 Python 环境缺少 requests") from exc
        self.requests_module = requests_module

    @classmethod
    def from_env(cls) -> "SeedanceHTTPTransport":
        return cls(SeedanceConfig.from_env())

    @contextlib.contextmanager
    def _configured_modules(self) -> Iterator[tuple[Any, Any]]:
        from . import video_generate, video_query

        originals = (
            video_generate.AUTH_CONFIGS,
            video_generate.TOS_CONFIGS,
            video_generate.API_URL,
            video_generate.TOS_PATH_PREFIX,
            video_generate.requests,
            video_query.AUTH_CONFIGS,
            video_query.TOS_CONFIGS,
            video_query.requests,
        )
        video_generate.AUTH_CONFIGS = self.config.auth_configs()
        video_generate.TOS_CONFIGS = self.config.tos_configs()
        video_generate.API_URL = self.config.api_url
        video_generate.TOS_PATH_PREFIX = self.config.tos_path_prefix
        video_generate.requests = self.requests_module
        video_query.AUTH_CONFIGS = self.config.auth_configs()
        video_query.TOS_CONFIGS = self.config.tos_configs()
        video_query.requests = self.requests_module
        try:
            yield video_generate, video_query
        finally:
            (
                video_generate.AUTH_CONFIGS,
                video_generate.TOS_CONFIGS,
                video_generate.API_URL,
                video_generate.TOS_PATH_PREFIX,
                video_generate.requests,
                video_query.AUTH_CONFIGS,
                video_query.TOS_CONFIGS,
                video_query.requests,
            ) = originals

    def generate(self, params: Mapping[str, Any], *, sleep: Any | None = None) -> dict[str, Any]:
        with self._configured_modules() as (video_generate, _):
            return video_generate.run_video_generate(
                params,
                requests_client=self.requests_module,
                sleep=sleep,
            )

    def query(
        self,
        params: Mapping[str, Any],
        *,
        clock: Any | None = None,
        sleep: Any | None = None,
    ) -> dict[str, Any]:
        with self._configured_modules() as (_, video_query):
            # run_video_query 的默认 clock/sleep 仅用于离线确定性测试；真实任务
            # 必须使用真实时间和等待，否则会把轮询时长错误写成时间戳并提前误判。
            return video_query.run_video_query(
                params,
                requests_client=self.requests_module,
                clock=clock or time,
                sleep=sleep or time.sleep,
            )


__all__ = [
    "DEFAULT_SEEDANCE_1_0_MODEL",
    "DEFAULT_SEEDANCE_1_5_MODEL",
    "SeedanceConfig",
    "SeedanceHTTPTransport",
    "SeedanceTransportError",
]
