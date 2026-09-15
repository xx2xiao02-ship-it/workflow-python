"""F0 control-plane API configuration for the independent topic center.

Only non-sensitive settings are written to api_management.json. API
credentials are kept in an external runtime secret file outside the source
tree and protected by the host file permissions. Topic-center transports
receive their runtime configuration through this module rather than reading
another module's storage or the old console configuration.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import PersistenceError, ValidationError

CONFIG_SCHEMA_VERSION = "control-plane-api-config-v1"
DEFAULT_TIKHUB_ENDPOINT = "https://api.tikhub.dev"
DEFAULT_TIKHUB_TIMEOUT_SECONDS = 25.0
DEFAULT_TIKHUB_MAX_RETRIES = 1
MIN_TIMEOUT_SECONDS = 3.0
MAX_TIMEOUT_SECONDS = 120.0
SECRET_FILE_NAME = "api_credentials.json"

_UNMIGRATED_PROVIDERS = (
    ("runninghub", "RunningHub / InfiniteTalk"),
    ("ark", "方舟文案与编导"),
    ("seed-audio", "Seed-Audio 音效"),
    ("volcengine-music", "火山音乐"),
    ("capcut-mate", "剪映小助手"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_runtime_root() -> Path:
    """Return an external runtime root; never default to the source tree."""

    configured = str(os.environ.get("TOPIC_CENTER_CONFIG_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "TopicCenterMigration"
    return Path.home() / ".topic-center-migration"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise PersistenceError(f"无法保存配置文件：{type(exc).__name__}", code="config_write_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _harden_secret_file(path: Path) -> None:
    """Restrict the external secret file to the current runtime user."""

    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise PersistenceError("无法限制凭据文件权限", code="secret_permissions_failed") from exc
        return
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    try:
        username = str(os.getlogin() or "").strip()
    except OSError:
        username = ""
    username = username or str(os.environ.get("USERNAME") or "").strip()
    identity = f"{domain}\\{username}" if domain and username else username
    if not identity:
        raise PersistenceError("无法确定当前 Windows 用户，未写入凭据", code="secret_permissions_failed")
    icacls = Path(os.environ.get("SystemRoot") or "C:\\Windows") / "System32" / "icacls.exe"
    try:
        result = subprocess.run(
            [str(icacls), str(path), "/inheritance:r", "/grant:r", f"{identity}:F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PersistenceError("无法设置凭据文件权限", code="secret_permissions_failed") from exc
    if result.returncode != 0:
        raise PersistenceError("无法设置凭据文件权限", code="secret_permissions_failed")


def _atomic_write_secret(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        try:
            _harden_secret_file(path)
        except PersistenceError:
            try:
                path.unlink()
            except OSError:
                pass
            raise
    except PersistenceError:
        raise
    except OSError as exc:
        raise PersistenceError("无法保存凭据文件", code="secret_write_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    return "*" * 8


def _non_empty_text(value: Any, field: str, *, max_chars: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > max_chars:
        raise ValidationError(f"{field} 超过允许长度")
    return text


def _normalize_endpoint(value: Any) -> str:
    text = _non_empty_text(value, "settings.endpoint", max_chars=2_000).rstrip("/")
    parsed = urlparse(text)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValidationError("settings.endpoint 必须是无凭据的 HTTP(S) 地址")
    return text


def _normalize_timeout(value: Any) -> float:
    if isinstance(value, bool):
        raise ValidationError("settings.timeout_seconds 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("settings.timeout_seconds 必须是数字") from exc
    if not math.isfinite(number):
        raise ValidationError("settings.timeout_seconds 必须是有限数字")
    if number < MIN_TIMEOUT_SECONDS or number > MAX_TIMEOUT_SECONDS:
        raise ValidationError(
            f"settings.timeout_seconds 必须在 {MIN_TIMEOUT_SECONDS:g} 到 {MAX_TIMEOUT_SECONDS:g} 秒之间"
        )
    return round(number, 3)


def _normalize_retries(value: Any) -> int:
    if isinstance(value, bool):
        raise ValidationError("settings.max_retries 必须是 0 或 1")
    if isinstance(value, float) and not value.is_integer():
        raise ValidationError("settings.max_retries 必须是 0 或 1")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("settings.max_retries 必须是 0 或 1") from exc
    if number not in {0, 1}:
        raise ValidationError("settings.max_retries 必须是 0 或 1")
    return number


@dataclass(frozen=True)
class TikHubTransportConfig:
    endpoint: str
    token: str
    timeout_seconds: float
    max_retries: int
    credential_ref: str
    revision: str


class ConfigService:
    """Registered control-plane configuration boundary for this migration."""

    SUPPORTED_GROUP_ID = "tikhub"
    SUPPORTED_CHANNEL_ID = "tikhub"
    SUPPORTED_SETTINGS = frozenset({"endpoint", "timeout_seconds", "max_retries"})
    SUPPORTED_SECRETS = frozenset({"primary_api_key"})

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else default_runtime_root()
        source_root = Path(__file__).resolve().parent.parent
        try:
            self.root.resolve().relative_to(source_root)
        except ValueError:
            pass
        else:
            raise PersistenceError("配置运行目录不能位于源码仓库内", code="runtime_root_inside_source")
        self.config_path = self.root / "api_management.json"
        self.secret_path = self.root / SECRET_FILE_NAME
        self._lock = threading.RLock()

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "version": 1,
            "selected_provider": "tikhub",
            "providers": {
                "tikhub": {
                    "group_id": "tikhub",
                    "channel_id": "tikhub",
                    "settings": {
                        "endpoint": DEFAULT_TIKHUB_ENDPOINT,
                        "timeout_seconds": DEFAULT_TIKHUB_TIMEOUT_SECONDS,
                        "max_retries": DEFAULT_TIKHUB_MAX_RETRIES,
                    },
                    "credential_ref": "tikhub.primary_api_key",
                    "connection_status": "not_tested",
                    "updated_at": "",
                }
            },
            "updated_at": "",
        }

    def _read_config(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._default_config()
        if not isinstance(raw, Mapping):
            return self._default_config()
        result = self._default_config()
        provider = raw.get("providers", {}).get("tikhub") if isinstance(raw.get("providers"), Mapping) else None
        if isinstance(provider, Mapping):
            settings = provider.get("settings") if isinstance(provider.get("settings"), Mapping) else {}
            current = result["providers"]["tikhub"]
            current["settings"].update({
                "endpoint": str(settings.get("endpoint") or current["settings"]["endpoint"]).strip().rstrip("/"),
                "timeout_seconds": settings.get("timeout_seconds", current["settings"]["timeout_seconds"]),
                "max_retries": settings.get("max_retries", current["settings"]["max_retries"]),
            })
            current["connection_status"] = str(provider.get("connection_status") or "not_tested")
            current["updated_at"] = str(provider.get("updated_at") or "")
        result["selected_provider"] = str(raw.get("selected_provider") or "tikhub")
        result["updated_at"] = str(raw.get("updated_at") or "")
        return result

    def _read_secrets(self) -> dict[str, dict[str, str]]:
        try:
            payload = self.secret_path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise PersistenceError("无法读取凭据文件", code="secret_read_failed") from exc
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise PersistenceError("凭据文件内容无效", code="secret_format_invalid") from exc
        if not isinstance(raw, Mapping):
            raise PersistenceError("凭据文件内容不是对象", code="secret_format_invalid")
        return {
            str(provider): {str(key): str(value) for key, value in values.items()}
            for provider, values in raw.items()
            if isinstance(values, Mapping)
        }

    def _write_secrets(self, secrets: Mapping[str, Mapping[str, str]]) -> None:
        if not secrets:
            self._delete_secrets()
            return
        payload = json.dumps(secrets, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        _atomic_write_secret(self.secret_path, payload)

    def _delete_secrets(self) -> None:
        try:
            self.secret_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PersistenceError("无法删除凭据文件", code="secret_delete_failed") from exc

    def _write_config(self, config: Mapping[str, Any]) -> None:
        _atomic_write_text(self.config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")

    def _current_token(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> str:
        values = secrets if secrets is not None else self._read_secrets()
        provider = values.get("tikhub") if isinstance(values.get("tikhub"), Mapping) else {}
        return str(provider.get("primary_api_key") or "").strip()

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Save settings and optional credentials without performing any request."""

        if not isinstance(payload, Mapping):
            raise ValidationError("配置请求必须是对象")
        group_id = str(payload.get("group_id") or self.SUPPORTED_GROUP_ID).strip()
        channel_id = str(payload.get("channel_id") or group_id).strip()
        if group_id != self.SUPPORTED_GROUP_ID or channel_id != self.SUPPORTED_CHANNEL_ID:
            raise ValidationError("本批只迁移 tikhub 配置；其他供应商尚未迁移")
        raw_settings = payload.get("settings")
        raw_settings = {} if raw_settings is None else raw_settings
        if not isinstance(raw_settings, Mapping):
            raise ValidationError("settings 必须是对象")
        unknown_settings = sorted(str(key) for key in raw_settings if str(key) not in self.SUPPORTED_SETTINGS)
        if unknown_settings:
            raise ValidationError("settings 包含未登记字段：" + ", ".join(unknown_settings))
        raw_secrets = payload.get("secrets")
        raw_secrets = {} if raw_secrets is None else raw_secrets
        if not isinstance(raw_secrets, Mapping):
            raise ValidationError("secrets 必须是对象")
        unknown_secrets = sorted(str(key) for key in raw_secrets if str(key) not in self.SUPPORTED_SECRETS)
        if unknown_secrets:
            raise ValidationError("secrets 包含未登记字段：" + ", ".join(unknown_secrets))

        with self._lock:
            config = self._read_config()
            provider = config["providers"]["tikhub"]
            settings = dict(provider["settings"])
            if "endpoint" in raw_settings and str(raw_settings.get("endpoint") or "").strip():
                settings["endpoint"] = _normalize_endpoint(raw_settings.get("endpoint"))
            if "timeout_seconds" in raw_settings and raw_settings.get("timeout_seconds") not in (None, ""):
                settings["timeout_seconds"] = _normalize_timeout(raw_settings.get("timeout_seconds"))
            if "max_retries" in raw_settings and raw_settings.get("max_retries") not in (None, ""):
                settings["max_retries"] = _normalize_retries(raw_settings.get("max_retries"))

            secrets = self._read_secrets()
            current_provider_secrets = dict(secrets.get("tikhub") or {})
            submitted_key = raw_secrets.get("primary_api_key")
            if submitted_key not in (None, ""):
                if not isinstance(submitted_key, str):
                    raise ValidationError("secrets.primary_api_key 必须是字符串")
                key = _non_empty_text(submitted_key, "secrets.primary_api_key", max_chars=1024)
                if any(char in key for char in "\r\n"):
                    raise ValidationError("secrets.primary_api_key 不能包含换行")
                current_provider_secrets["primary_api_key"] = key
            # An empty secret is intentionally ignored: it cannot erase a saved key.
            if current_provider_secrets:
                secrets["tikhub"] = current_provider_secrets
            elif "tikhub" in secrets:
                secrets.pop("tikhub", None)

            now = utc_now()
            provider["settings"] = settings
            provider["connection_status"] = "not_tested"
            provider["updated_at"] = now
            config["selected_provider"] = "tikhub"
            config["updated_at"] = now
            old_secrets = self._read_secrets()
            try:
                self._write_secrets(secrets)
                self._write_config(config)
            except Exception:
                try:
                    if old_secrets:
                        self._write_secrets(old_secrets)
                    else:
                        self._delete_secrets()
                except Exception:
                    pass
                raise
            return self.public_snapshot()

    def get_tikhub_transport_config(self) -> TikHubTransportConfig:
        """Return the only registered runtime credential boundary for TikHub."""

        with self._lock:
            config = self._read_config()
            settings = config["providers"]["tikhub"]["settings"]
            endpoint = _normalize_endpoint(settings.get("endpoint") or DEFAULT_TIKHUB_ENDPOINT)
            timeout = _normalize_timeout(settings.get("timeout_seconds", DEFAULT_TIKHUB_TIMEOUT_SECONDS))
            retries = _normalize_retries(settings.get("max_retries", DEFAULT_TIKHUB_MAX_RETRIES))
            token = self._current_token()
            return TikHubTransportConfig(
                endpoint=endpoint,
                token=token,
                timeout_seconds=timeout,
                max_retries=retries,
                credential_ref="tikhub.primary_api_key",
                revision=str(config.get("updated_at") or "initial"),
            )

    def public_snapshot(self) -> dict[str, Any]:
        """Return a frontend-safe snapshot with no credential value."""

        with self._lock:
            config = self._read_config()
            provider = config["providers"]["tikhub"]
            settings = dict(provider["settings"])
            token = self._current_token()
            credential_status = "configured" if token else "missing"
            provider_status = "configured_not_verified" if token else "not_configured"
            groups = [{
                "group_id": "tikhub",
                "channel_id": "tikhub",
                "provider": "TikHub",
                "status": provider_status,
                "status_label": "已配置，未验证" if token else "未配置",
                "connection_status": str(provider.get("connection_status") or "not_tested"),
                "connection_status_label": "未执行供应商连接验证",
                "settings": settings,
                "credential_ref": "tikhub.primary_api_key",
                "credential_status": credential_status,
                "credential_masked": _mask_secret(token),
                "save_triggers_external_request": False,
                "test_connection_executed": False,
                "note": "保存配置不会触发采集、生成或付费请求。",
            }]
            unmigrated = [
                {
                    "provider_id": provider_id,
                    "provider": label,
                    "status": "not_migrated",
                    "status_label": "尚未迁移",
                    "connection_status": "not_available",
                    "credential_status": "not_imported",
                    "note": "本批未迁移，不展示已连接。",
                }
                for provider_id, label in _UNMIGRATED_PROVIDERS
            ]
            return {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "selected_provider": "tikhub",
                "generated_at": utc_now(),
                "runtime": {
                    "config_path": str(self.config_path),
                    "credential_store": "外部运行时凭据文件（Windows ACL）" if os.name == "nt" else "外部运行时凭据文件（POSIX 600）",
                    "storage_policy": "配置文件与密钥均在源项目之外；凭据文件限制为当前运行用户可读，不进入 Git、日志或快照。",
                },
                "summary": {
                    "total": len(groups) + len(unmigrated),
                    "configured": 1 if token else 0,
                    "verified": 0,
                    "not_migrated": len(unmigrated),
                },
                "groups": groups,
                "unmigrated_providers": unmigrated,
            }

    def connection_test_deferred(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Keep the separate test endpoint explicit without making an unknown-cost call."""

        if payload is not None and not isinstance(payload, Mapping):
            raise ValidationError("连接测试请求必须是对象")
        if payload and str(payload.get("confirm") or "").strip() not in {
            "controlled-auth-verification",
            "deferred-connection-check",
        }:
            raise ValidationError("连接测试需要有效的 confirm 标记")
        token = self._current_token()
        return {
            "status": "deferred",
            "provider": "tikhub",
            "executed": False,
            "auth_verified": False,
            "paid_call": "unknown",
            "credential_configured": bool(token),
            "message": "连接测试暂未执行：无法确认本次供应商请求免费，未发送外部请求。",
        }


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "ConfigService",
    "DEFAULT_TIKHUB_ENDPOINT",
    "DEFAULT_TIKHUB_MAX_RETRIES",
    "DEFAULT_TIKHUB_TIMEOUT_SECONDS",
    "TikHubTransportConfig",
    "default_runtime_root",
]
