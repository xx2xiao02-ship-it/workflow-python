"""F0 control-plane API configuration for the independent topic center.

Only non-sensitive settings are written to api_management.json. API
credentials are kept in an external runtime secret file outside the source
tree and protected by the host file permissions. Topic-center transports
receive their runtime configuration through this module rather than reading
another module's storage or the old console configuration.
"""

from __future__ import annotations

import copy
import ctypes
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
DEFAULT_TIKHUB_ENDPOINT = "https://api.tikhub.io"
DEFAULT_TIKHUB_TIMEOUT_SECONDS = 25.0
DEFAULT_TIKHUB_MAX_RETRIES = 1
MIN_TIMEOUT_SECONDS = 3.0
MAX_TIMEOUT_SECONDS = 120.0
SECRET_FILE_NAME = "api_credentials.json"

SERVICE_CATALOG = {
    "tikhub": ("TikHub", "Bearer"),
    "story-writing": ("方舟文案", "Bearer"),
    "director-seed21": ("方舟编导", "Bearer"),
    "visual-guidance": ("编导视觉约束", "Bearer"),
    "image-generation": ("Image 2 图像", "Bearer"),
    "video-generation": ("Seedance 视频", "Bearer"),
    "digital-human": ("RunningHub / InfiniteTalk", "Bearer"),
    "tts": ("方舟 TTS", "X-Api-Key"),
    "sound-effect": ("Seed-Audio", "X-Api-Key"),
    "bgm-audio": ("火山音乐", "HMAC-SHA256"),
    "audio-storage": ("TOS 对象存储", "AK/SK"),
    "capcut-mate": ("剪映助手", "not_applicable"),
}
COMMON_SETTINGS = frozenset({
    "endpoint", "region", "primary_model", "backup_model", "strategy",
    "max_attempts", "max_retries", "timeout_seconds", "model_slots", "model_names", "model_order",
    "quality", "api_format", "name", "enabled", "resource_id", "app_id", "service", "api_version", "model_version",
})
KEY_FIELDS = frozenset({"primary_api_key", "backup_api_key", "extra_api_keys"})
PAIR_FIELDS = frozenset({"primary_access_key", "primary_secret_key", "backup_access_key", "backup_secret_key"})
FIXED_SETTINGS = {
    "tts": {"endpoint": "https://openspeech.bytedance.com/api/v3/tts/unidirectional", "primary_model": "seed-tts-2.0-standard"},
    "sound-effect": {"endpoint": "https://openspeech.bytedance.com/api/v3/tts/create", "primary_model": "seed-audio-1.0"},
}
CONNECTION_STATUS_LABELS = {
    "not_tested": "未验证",
    "verified": "已验证连接",
    "reachable_not_auth_verified": "网络可达，鉴权未验证",
    "auth_failed": "鉴权验证失败",
    "forbidden": "供应商拒绝 Token/接口权限",
    "billing_blocked": "余额或计费受限",
    "request_failed": "连接失败",
    "blocked_missing_credential": "凭据缺失，未发送请求",
    "not_verifiable_without_generation": "协议要求生成请求，未验证鉴权",
}
TIKHUB_AUTH_VERIFICATION_CONTRACT = "tikhub-account-info-v1"
BUSINESS_ENTRYPOINTS = {
    "tikhub": "TikHubTransport",
    "story-writing": "ArkStoryTransport",
    "director-seed21": "Seed21DirectorTransport",
    "visual-guidance": "VisualGuidanceTransport",
    "image-generation": "Image2Transport",
    "video-generation": "SeedanceTransport",
    "digital-human": "RunningHubInfiniteTalkTransport",
    "tts": "ArkTTSHTTPTransport",
    "sound-effect": "SeedAudioHTTPTransport",
    "bgm-audio": "VolcengineMusicTransport",
    "audio-storage": "TOSAudioStorageTransport",
    "capcut-mate": "CapCutMateClient",
}


def _base_channel(channel: str) -> str:
    return channel.split("__custom_", 1)[0]


def _secret_fields(channel: str) -> frozenset[str]:
    base = _base_channel(channel)
    if base == "capcut-mate":
        return frozenset()
    if base in {"bgm-audio", "audio-storage"}:
        return PAIR_FIELDS
    if base == "tikhub":
        return frozenset({"primary_api_key"})
    return KEY_FIELDS


def _effective_verification(provider: Mapping[str, Any], channel: str) -> dict[str, Any]:
    contract = str(provider.get("verification_contract") or "")
    result = {
        "connection_status": str(provider.get("connection_status") or "not_tested"),
        "connection_status_label": CONNECTION_STATUS_LABELS.get(
            str(provider.get("connection_status") or "not_tested"),
            str(provider.get("connection_status_label") or ""),
        ),
        "auth_verified": bool(provider.get("auth_verified")),
        "test_connection_executed": bool(provider.get("test_connection_executed")),
        "verification_http_status": provider.get("verification_http_status"),
        "verification_attempts": int(provider.get("verification_attempts") or 0),
        "verification_error_type": str(provider.get("verification_error_type") or ""),
        "verification_evidence": str(provider.get("verification_evidence") or ""),
        "verification_contract": contract,
        "verification_message": str(provider.get("verification_message") or ""),
    }
    if (
        _base_channel(channel) == "tikhub"
        and (result["connection_status"] == "verified" or result["auth_verified"])
        and contract != TIKHUB_AUTH_VERIFICATION_CONTRACT
    ):
        result.update({
            "connection_status": "not_tested",
            "connection_status_label": CONNECTION_STATUS_LABELS["not_tested"],
            "auth_verified": False,
            "test_connection_executed": False,
            "verification_http_status": None,
            "verification_attempts": 0,
            "verification_error_type": "stale_verification_contract",
            "verification_evidence": "旧验证结果未使用当前 TikHub 账户信息接口，请重新验证",
            "verification_message": "旧验证结果未使用当前 TikHub 账户信息接口，请重新验证。",
        })
    if not result["connection_status_label"]:
        result["connection_status_label"] = CONNECTION_STATUS_LABELS.get(result["connection_status"], "未验证")
    return result


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
        temporary.touch(mode=0o600)
        _harden_secret_file(temporary)
        temporary.write_bytes(payload)
        os.replace(temporary, path)
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


def _dpapi(payload: bytes, *, protect: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise PersistenceError("Windows 凭据加解密失败", code="secret_crypto_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


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
        or parsed.query
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
        except FileNotFoundError:
            return self._default_config()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PersistenceError("配置文件无法读取，停止保存以保留原文件", code="config_read_failed") from exc
        if not isinstance(raw, Mapping):
            raise PersistenceError("配置文件格式无效", code="config_read_failed")
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
        raw_providers = raw.get("providers", {})
        if not isinstance(raw_providers, Mapping):
            raise PersistenceError("配置通道格式无效", code="config_read_failed")
        for channel, value in raw_providers.items():
            if _base_channel(channel) in SERVICE_CATALOG and isinstance(value, Mapping):
                if channel == "tikhub":
                    result["providers"][channel].update({k: copy.deepcopy(v) for k, v in value.items() if k != "settings"})
                else:
                    result["providers"][channel] = copy.deepcopy(value)
        result["routing"] = copy.deepcopy(raw.get("routing", {}))
        result["migration_conflicts"] = copy.deepcopy(raw.get("migration_conflicts", []))
        return result

    def _read_secrets(self) -> dict[str, dict[str, str]]:
        try:
            payload = self.secret_path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise PersistenceError("无法读取凭据文件", code="secret_read_failed") from exc
        try:
            if payload.startswith(b"U1:"):
                payload = _dpapi(payload[3:], protect=False)
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise PersistenceError("凭据文件内容无效", code="secret_format_invalid") from exc
        if not isinstance(raw, Mapping):
            raise PersistenceError("凭据文件内容不是对象", code="secret_format_invalid")
        return {
            str(provider): {str(key): copy.deepcopy(value) for key, value in values.items()}
            for provider, values in raw.items()
            if isinstance(values, Mapping)
        }

    def _write_secrets(self, secrets: Mapping[str, Mapping[str, str]]) -> None:
        if not secrets:
            self._delete_secrets()
            return
        payload = json.dumps(secrets, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if os.name == "nt":
            payload = b"U1:" + _dpapi(payload, protect=True)
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
        if _base_channel(channel_id) not in SERVICE_CATALOG:
            raise ValidationError("服务通道未登记")
        if channel_id != "tikhub":
            return self._save_channel(channel_id, payload)
        if group_id != "tikhub":
            raise ValidationError("配置组与通道不匹配")
        self._validated_channel(channel_id, payload)
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
            provider["auth_verified"] = False
            provider["test_connection_executed"] = False
            provider["verification_contract"] = ""
            provider["verification_evidence"] = ""
            provider["verification_message"] = ""
            provider["updated_at"] = now
            config["selected_provider"] = "tikhub"
            config["updated_at"] = now
            self._persist(config, secrets)
            return self.public_snapshot()

    def _validated_channel(self, channel: str, payload: Mapping[str, Any]) -> tuple[dict, dict]:
        base = _base_channel(channel)
        if base not in SERVICE_CATALOG:
            raise ValidationError("服务通道未登记")
        settings = payload.get("settings") or {}
        secrets = payload.get("secrets") or {}
        if not isinstance(settings, Mapping) or not isinstance(secrets, Mapping):
            raise ValidationError("配置与凭据必须是对象")
        allowed = self.SUPPORTED_SETTINGS if base == "tikhub" else COMMON_SETTINGS
        if set(settings) - allowed or set(secrets) - _secret_fields(channel):
            raise ValidationError("配置包含未登记字段")
        clean = {}
        for field, value in settings.items():
            if value in (None, ""):
                continue
            if field == "endpoint":
                value = _normalize_endpoint(value)
            elif field in {"model_slots", "model_names"}:
                if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                    raise ValidationError("模型字段必须为字符串映射")
                value = dict(value)
            elif field == "model_order":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValidationError("模型顺序必须为字符串数组")
            elif field == "enabled":
                if not isinstance(value, bool):
                    raise ValidationError("启用状态必须为布尔值")
            elif field == "timeout_seconds":
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("超时必须为数字秒数") from exc
                if not math.isfinite(value) or not 3 <= value <= 3600:
                    raise ValidationError("超时必须在 3 至 3600 秒之间")
            elif field in {"max_attempts", "max_retries"}:
                try:
                    numeric = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("次数必须为整数") from exc
                if not math.isfinite(numeric) or not numeric.is_integer() or not 0 <= numeric <= 10:
                    raise ValidationError("次数超出范围")
                value = int(numeric)
            else:
                value = _non_empty_text(value, field, max_chars=2000)
            clean[field] = value
        for field, fixed in FIXED_SETTINGS.get(base, {}).items():
            if field in clean and clean[field] != fixed:
                raise ValidationError("音频服务使用固定 V3 协议字段")
            clean[field] = fixed
        keys = {}
        for field, raw in secrets.items():
            if raw in (None, "", []):
                continue
            values = raw if field == "extra_api_keys" else [raw]
            if not isinstance(values, list) or len(values) > 32:
                raise ValidationError("凭据列表格式无效")
            normalized = []
            for value in values:
                if not isinstance(value, str):
                    raise ValidationError("凭据必须为字符串")
                value = value.strip()
                if not value or len(value) > 4096 or any(c.isspace() for c in value) or any(c in value for c in '\"\'') or not value.isascii():
                    raise ValidationError("凭据包含空白、引号或无效字符")
                if "://" in value:
                    raise ValidationError("服务地址不能作为凭据保存")
                if set(value) == {"*"}:
                    raise ValidationError("脱敏占位符不能作为凭据保存")
                normalized.append(value)
            keys[field] = normalized if field == "extra_api_keys" else normalized[0]
        return clean, keys

    def _persist(self, config: dict, secrets: dict) -> None:
        serialized = json.dumps(config, ensure_ascii=False)
        for values in secrets.values():
            for value in values.values():
                for key in value if isinstance(value, list) else [value]:
                    if key and str(key) in serialized:
                        raise ValidationError("普通配置字段中检测到凭据，未保存")
        previous = self._read_secrets()
        try:
            self._write_secrets(secrets)
            self._write_config(config)
        except Exception:
            self._write_secrets(previous)
            raise

    def _save_channel(self, channel: str, payload: Mapping[str, Any]) -> dict:
        settings, keys = self._validated_channel(channel, payload)
        with self._lock:
            config, secrets = self._read_config(), self._read_secrets()
            provider = config["providers"].setdefault(channel, {"settings": {}})
            provider["settings"].update(settings)
            secrets.setdefault(channel, {}).update(keys)
            provider.update({
                "updated_at": utc_now(),
                "connection_status": "not_tested",
                "auth_verified": False,
                "test_connection_executed": False,
                "verification_contract": "",
                "verification_http_status": None,
                "verification_attempts": 0,
                "verification_error_type": "",
                "verification_evidence": "",
                "verification_message": "",
                "config_status": "saved",
            })
            config.update({"updated_at": utc_now(), "selected_provider": channel})
            self._persist(config, secrets)
            return self.public_snapshot()

    def merge_import(self, channels: Mapping[str, Mapping[str, Any]], *, routing: Mapping[str, Any] | None = None) -> dict:
        """Merge audited inputs without replacing nonempty target values or contacting providers."""
        validated = {channel: self._validated_channel(channel, payload) for channel, payload in channels.items()}
        conflicts = []
        touched = set()

        def merge(target: dict, source: Mapping, channel: str, prefix: str) -> None:
            for field, value in source.items():
                if value in (None, "", [], {}):
                    continue
                path = prefix + field
                old = target.get(field)
                if isinstance(value, Mapping) and isinstance(old, dict):
                    merge(old, value, channel, path + ".")
                else:
                    touched.add((channel, path))
                    if old in (None, "", [], {}):
                        target[field] = copy.deepcopy(value)
                    elif old != value:
                        conflicts.append({"channel": channel, "field": path, "target": "********", "source": "********", "action": "kept_target"})

        with self._lock:
            config, secrets = self._read_config(), self._read_secrets()
            for channel, (settings, keys) in validated.items():
                incoming = channels[channel]
                if not any(value not in (None, "", [], {}) for section in ("settings", "secrets") for value in (incoming.get(section) or {}).values()):
                    continue
                provider = config["providers"].setdefault(channel, {"settings": {}})
                # Defaults are not saved target values until a user has persisted them.
                if channel == "tikhub" and not provider.get("updated_at"):
                    provider["settings"] = {}
                merge(provider["settings"], settings, channel, "settings.")
                merge(secrets.setdefault(channel, {}), keys, channel, "secrets.")
                provider.update({
                    "updated_at": utc_now(),
                    "imported_at": utc_now(),
                    "connection_status": "not_tested",
                    "auth_verified": False,
                    "test_connection_executed": False,
                    "verification_contract": "",
                    "verification_http_status": None,
                    "verification_attempts": 0,
                    "verification_error_type": "",
                    "verification_evidence": "",
                    "verification_message": "",
                    "config_status": "imported",
                })
            if routing:
                merge(config.setdefault("routing", {}), routing, "routing", "")
            # Keep unresolved conflicts across saves/restarts and partial reimports.
            remaining = [item for item in config.get("migration_conflicts", []) if (item.get("channel"), item.get("field")) not in touched]
            config["migration_conflicts"] = remaining + conflicts
            config["updated_at"] = utc_now()
            self._persist(config, secrets)
            return {"conflicts": conflicts, "snapshot": self.public_snapshot(), "external_requests": False}

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

    def get_service_runtime(self, channel: str) -> dict[str, Any]:
        """为目标 Transport 提供一次性运行时配置；调用方不得序列化 secrets。"""

        channel = str(channel or "").strip()
        base = _base_channel(channel)
        if base not in SERVICE_CATALOG:
            raise ValidationError("服务通道未登记")
        with self._lock:
            config = self._read_config()
            secrets = self._read_secrets()
            provider = config.get("providers", {}).get(channel, {})
            if not isinstance(provider, Mapping):
                provider = {}
            channel_secrets = secrets.get(channel, {})
            if not isinstance(channel_secrets, Mapping):
                channel_secrets = {}
            inherited = secrets.get("tts", {}) if base == "sound-effect" else {}
            if not isinstance(inherited, Mapping):
                inherited = {}
            return {
                "channel_id": channel,
                "base_channel_id": base,
                "settings": copy.deepcopy(provider.get("settings", {})) if isinstance(provider.get("settings"), Mapping) else {},
                "secrets": copy.deepcopy(dict(channel_secrets)),
                "inherited_secrets": copy.deepcopy(dict(inherited)),
                "credential_ref": str(provider.get("credential_ref") or f"{channel}.primary_api_key"),
                "revision": str(config.get("updated_at") or "initial"),
            }

    def record_connection_result(self, channel: str, result: Mapping[str, Any]) -> None:
        """只保存不含凭据的连接验证摘要，供页面重启后读取。"""

        channel = str(channel or "").strip()
        if _base_channel(channel) not in SERVICE_CATALOG:
            raise ValidationError("服务通道未登记")
        status = str(result.get("connection_status") or "not_tested")
        if status not in CONNECTION_STATUS_LABELS:
            raise ValidationError("连接验证状态未登记")
        with self._lock:
            config = self._read_config()
            provider = config["providers"].setdefault(channel, {"settings": {}})
            provider.update({
                "connection_status": status,
                "connection_status_label": CONNECTION_STATUS_LABELS[status],
                "auth_verified": bool(result.get("auth_verified")),
                "test_connection_executed": bool(result.get("executed")),
                "verification_contract": str(result.get("verification_contract") or ""),
                "verification_checked_at": utc_now(),
                "verification_http_status": result.get("http_status") if isinstance(result.get("http_status"), int) else None,
                "verification_attempts": int(result.get("attempts") or 0),
                "verification_error_type": str(result.get("error_type") or "")[:80],
                "verification_evidence": str(result.get("evidence") or "")[:160],
                "verification_message": str(result.get("message") or "")[:320],
                "verification_generation_started": False,
            })
            config["updated_at"] = utc_now()
            self._persist(config, self._read_secrets())

    def public_snapshot(self) -> dict[str, Any]:
        """Return a frontend-safe snapshot with no credential value."""

        with self._lock:
            config = self._read_config()
            provider = config["providers"]["tikhub"]
            settings = dict(provider["settings"])
            token = self._current_token()
            credential_status = "configured" if token else "missing"
            provider_status = "configured_not_verified" if token else "not_configured"
            connection_status = str(provider.get("connection_status") or "not_tested")
            groups = [{
                "group_id": "tikhub",
                "channel_id": "tikhub",
                "provider": "TikHub",
                "status": provider_status,
                "status_label": "已配置，未验证" if token else "未配置",
                "connection_status": connection_status,
                "connection_status_label": CONNECTION_STATUS_LABELS.get(
                    connection_status,
                    str(provider.get("connection_status_label") or "未验证"),
                ),
                "settings": settings,
                "credential_ref": "tikhub.primary_api_key",
                "credential_status": credential_status,
                "credential_masked": _mask_secret(token),
                "save_triggers_external_request": False,
                "test_connection_executed": bool(provider.get("test_connection_executed")),
                "auth_verified": bool(provider.get("auth_verified")),
                "verification_message": str(provider.get("verification_message") or ""),
                "note": "保存配置不会触发采集、生成或付费请求。",
            }]
            all_secrets = self._read_secrets()
            channels = list(dict.fromkeys([*SERVICE_CATALOG, *config["providers"]]))
            for channel in channels:
                base = _base_channel(channel)
                saved = config["providers"].get(channel, {})
                values = all_secrets.get(channel, {})
                auth = SERVICE_CATALOG[base][1]
                present = bool(values.get("primary_api_key") or values.get("backup_api_key") or values.get("extra_api_keys"))
                credential_source = channel
                if base == "sound-effect" and not present:
                    present = bool(all_secrets.get("tts", {}).get("primary_api_key"))
                    credential_source = "tts.primary_api_key" if present else channel
                if auth in {"AK/SK", "HMAC-SHA256"}:
                    present = any(values.get(prefix + "_access_key") and values.get(prefix + "_secret_key") for prefix in ("primary", "backup"))
                credential = "not_applicable" if auth == "not_applicable" else ("configured" if present else ("incomplete" if any(values.values()) else "missing"))
                item = groups[0] if channel == "tikhub" else {}
                pending = [item for item in config.get("migration_conflicts", []) if item.get("channel") in {channel, "routing"}]
                has_data = any(value not in (None, "", [], {}) for value in saved.get("settings", {}).values()) or any(values.values())
                migration_status = "import_conflict" if pending else ("imported" if saved.get("imported_at") and has_data else ("saved" if saved.get("updated_at") and has_data else "not_imported"))
                verification = _effective_verification(saved, channel)
                item.update({
                    "group_id": channel, "channel_id": channel,
                    "provider": SERVICE_CATALOG[base][0] + (" / API2 " + channel.split("__custom_", 1)[1] if "__custom_" in channel else ""),
                    "settings": copy.deepcopy(saved.get("settings", {})),
                    "auth_mode": auth,
                    "config_status": migration_status,
                    "migration_conflicts": [{"channel": entry["channel"], "field": entry["field"], "target": "********", "source": "********", "action": "kept_target"} for entry in pending],
                    "credential_status": credential,
                    "credential_source": credential_source,
                    "credential_masked": "********" if present else "",
                    "credential_fields": [{"field": field, "present": bool(values.get(field)), "masked": "********" if values.get(field) else ""} for field in sorted(_secret_fields(channel))],
                    "business_status": "migrated" if base in BUSINESS_ENTRYPOINTS else "not_migrated",
                    "business_entrypoint": BUSINESS_ENTRYPOINTS.get(base, ""),
                    "connection_status": verification["connection_status"],
                    "connection_status_label": verification["connection_status_label"],
                    "auth_verified": verification["auth_verified"],
                    "test_connection_executed": verification["test_connection_executed"],
                    "verification_http_status": verification["verification_http_status"],
                    "verification_attempts": verification["verification_attempts"],
                    "verification_error_type": verification["verification_error_type"],
                    "verification_evidence": verification["verification_evidence"],
                    "verification_contract": verification["verification_contract"],
                    "verification_message": verification["verification_message"],
                    "verification_generation_started": False,
                    "save_triggers_external_request": False,
                    "status_label": "已配置，未验证" if present or credential == "not_applicable" else "凭据待配置",
                })
                if channel != "tikhub":
                    groups.append(item)
            unmigrated = [{"provider_id": g["channel_id"], "provider": g["provider"], "status": "not_migrated"} for g in groups if g["business_status"] == "not_migrated"]
            return {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "selected_provider": config.get("selected_provider", "tikhub"),
                "generated_at": utc_now(),
                "runtime": {
                    "config_path": str(self.config_path),
                    "credential_store": "外部运行时凭据文件（Windows DPAPI + ACL）" if os.name == "nt" else "外部运行时凭据文件（POSIX 600）",
                    "storage_policy": "配置文件与密钥均在源项目之外；凭据文件限制为当前运行用户可读，不进入 Git、日志或快照。",
                },
                "summary": {
                    "total": len(groups),
                    "configured": sum(g["credential_status"] == "configured" for g in groups),
                    "verified": sum(g["connection_status"] == "verified" for g in groups),
                    "not_migrated": len(unmigrated),
                },
                "groups": groups,
                "unmigrated_providers": unmigrated,
            }

    def connection_test_deferred(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """兼容旧路由；实际验证由 server 注入的 UnifiedApiTransport 执行。"""

        if payload is not None and not isinstance(payload, Mapping):
            raise ValidationError("连接测试请求必须是对象")
        if payload and str(payload.get("confirm") or "").strip() not in {
            "controlled-auth-verification",
            "deferred-connection-check",
        }:
            raise ValidationError("连接测试需要有效的 confirm 标记")
        channel = str((payload or {}).get("channel_id") or "tikhub")
        channels = (payload or {}).get("channels")
        if isinstance(channels, list) and channels:
            channel = str(channels[0])
        group = next((g for g in self.public_snapshot()["groups"] if g["channel_id"] == channel), None)
        if group is None:
            raise ValidationError("服务通道未登记")
        return {
            "status": "deferred",
            "provider": channel,
            "executed": False,
            "auth_verified": False,
            "paid_call": "unknown",
            "credential_configured": group["credential_status"] == "configured",
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
