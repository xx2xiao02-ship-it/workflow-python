"""统一 API 服务入口、重试边界和安全连接预检。

本模块只依赖目标项目的 ConfigService。它不读取旧项目、不把凭据放入
返回值，也不把生成、合成或任务创建操作放进连接验证路径。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config_service import TIKHUB_AUTH_VERIFICATION_CONTRACT, ConfigService
from .errors import ValidationError

RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
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

_TIKHUB_AUTH_PATH = "/api/v1/tikhub/user/get_user_info"
_ARK_MODELS_AUTH_PATH = "/api/v3/models"
_ARK_MODELS_AUTH_HOST = "ark.cn-beijing.volces.com"
_ARK_MODELS_AUTH_VERIFICATION_CONTRACT = "ark-models-list-v1"
_APIMODELS_BALANCE_PATH = "/v1/balance"
_APIMODELS_AUTH_HOSTS = frozenset({"api.apimodels.app", "apimodels.app"})
_APIMODELS_AUTH_VERIFICATION_CONTRACT = "apimodels-balance-v1"
_TOS_AUTH_VERIFICATION_CONTRACT = "tos-list-buckets-v1"
_NO_SAFE_AUTH_VERIFICATION_CONTRACT = "no-safe-auth-probe-v1"


@dataclass(frozen=True)
class ServiceTransportSpec:
    """目标侧公开的服务入口描述；不包含凭据值。"""

    entrypoint: str
    auth_mode: str
    default_endpoint: str
    operations: tuple[str, ...]
    verification: str
    billable_operations: tuple[str, ...] = ()


SERVICE_TRANSPORT_SPECS: dict[str, ServiceTransportSpec] = {
    "tikhub": ServiceTransportSpec(
        "TikHubTransport", "Bearer", "https://api.tikhub.io", ("search_related",),
        "使用 GET /api/v1/tikhub/user/get_user_info 验证账户鉴权；只读，不执行搜索。",
        ("search_related",),
    ),
    "story-writing": ServiceTransportSpec(
        "ArkStoryTransport", "Bearer", "https://ark.cn-beijing.volces.com/api/v3/responses",
        ("call",), "对官方方舟 endpoint 使用 GET /api/v3/models 只读验证 Bearer 鉴权；不执行 Responses 生成。", ("call",),
    ),
    "director-seed21": ServiceTransportSpec(
        "Seed21DirectorTransport", "Bearer", "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        ("call",), "对官方方舟 endpoint 使用 GET /api/v3/models 只读验证 Bearer 鉴权；不执行 Chat 生成。", ("call",),
    ),
    "visual-guidance": ServiceTransportSpec(
        "VisualGuidanceTransport", "Bearer", "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        ("call",), "历史视觉约束通道复用方舟文本协议；仅官方方舟 endpoint 支持模型列表鉴权验证。", ("call",),
    ),
    "image-generation": ServiceTransportSpec(
        "Image2Transport", "Bearer", "https://api.apimodels.app/v1/",
        ("create_image",), "API Models 使用 GET /v1/balance 只读验证 Bearer 鉴权；不执行图像生成。", ("create_image",),
    ),
    "video-generation": ServiceTransportSpec(
        "SeedanceTransport", "Bearer", "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
        ("create_video", "query_video", "retry_video"),
        "视频创建会产生任务；验证只做协议预检。", ("create_video", "retry_video"),
    ),
    "digital-human": ServiceTransportSpec(
        "RunningHubInfiniteTalkTransport", "Bearer", "https://www.runninghub.cn/openapi/v2",
        ("create", "query"), "RunningHub 查询需要已有 taskId；验证不创建任务。", ("create",),
    ),
    "tts": ServiceTransportSpec(
        "ArkTTSHTTPTransport", "X-Api-Key", "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        ("synthesize",), "TTS V3 认证与合成请求同接口；验证不发送音频合成。", ("synthesize",),
    ),
    "sound-effect": ServiceTransportSpec(
        "SeedAudioHTTPTransport", "X-Api-Key", "https://openspeech.bytedance.com/api/v3/tts/create",
        ("create_effect",), "Seed-Audio 认证与音效生成请求同接口；验证不生成音效。", ("create_effect",),
    ),
    "bgm-audio": ServiceTransportSpec(
        "VolcengineMusicTransport", "HMAC-SHA256", "https://open.volcengineapi.com/",
        ("generate_bgm", "query_bgm"), "GenBGM 会创建任务；验证不创建或续查任务。", ("generate_bgm",),
    ),
    "audio-storage": ServiceTransportSpec(
        "TOSAudioStorageTransport", "AK/SK", "https://tos-cn-beijing.volces.com",
        ("publish_audio", "publish_reference"), "使用 TOS ListBuckets 只读验证 AK/SK；不上传、不删除对象。", (),
    ),
    "capcut-mate": ServiceTransportSpec(
        "CapCutMateClient", "not_applicable", "http://127.0.0.1:30000",
        ("get_draft", "create_draft", "save_draft"),
        "只读 GET /healthz 预检；不修改草稿。",
        ("create_draft", "save_draft"),
    ),
}


def _base_channel(channel: str) -> str:
    return channel.split("__custom_", 1)[0]


def _text(value: object) -> str:
    return str(value or "").strip()


def _bearer_header(value: object) -> str:
    token = _text(value)
    return token if token.lower().startswith("bearer ") else "Bearer " + token


def _effective_api_key(channel: str, secrets: Mapping[str, object], inherited: Mapping[str, object]) -> str:
    values = secrets
    if channel == "sound-effect" and not values.get("primary_api_key") and not values.get("backup_api_key"):
        values = inherited
    for name in ("primary_api_key", "backup_api_key"):
        value = _text(values.get(name))
        if value:
            return value
    extra = values.get("extra_api_keys")
    if isinstance(extra, list):
        for value in extra:
            if _text(value):
                return _text(value)
    return ""


def _model(settings: Mapping[str, object], *names: str) -> str:
    slots = settings.get("model_slots")
    if isinstance(slots, Mapping):
        for name in names:
            value = _text(slots.get(name))
            if value:
                return value
    for name in names:
        value = _text(settings.get(name))
        if value:
            return value
    return ""


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _ark_models_auth_url(channel: str, endpoint: str) -> str | None:
    if _base_channel(channel) not in {"story-writing", "director-seed21", "visual-guidance", "video-generation"}:
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != _ARK_MODELS_AUTH_HOST:
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if not (parsed.path == "/api/v3" or parsed.path.startswith("/api/v3/")):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, _ARK_MODELS_AUTH_PATH, "", ""))


def _apimodels_balance_url(channel: str, endpoint: str) -> str | None:
    """Return the documented free account endpoint for API Models.

    ``GET /models`` is public and therefore cannot prove a key.  The provider
    documents ``GET /api/v1/balance`` as a free authenticated request, while
    the direct host uses ``/v1/balance``.  Only official hosts are accepted.
    """

    if _base_channel(channel) != "image-generation":
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _APIMODELS_AUTH_HOSTS:
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    path = parsed.path.rstrip("/")
    if path not in {"/v1", "/api/v1"}:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/balance", "", ""))


def _tos_list_buckets_url(channel: str, endpoint: str) -> str | None:
    """Return the read-only TOS ListBuckets endpoint for official hosts."""

    if _base_channel(channel) != "audio-storage":
        return None
    parsed = urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host.startswith("tos-") and host.endswith(".volces.com")):
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


class ServiceTransportError(RuntimeError):
    """目标服务入口或安全预检失败。"""


class UnifiedApiTransport:
    """从统一配置中心构造目标服务入口，并提供只读连接预检。"""

    def __init__(
        self,
        config_service: ConfigService,
        *,
        opener: Callable[..., object] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config_service = config_service
        self.opener = opener
        self.sleeper = sleeper

    @staticmethod
    def _spec(channel: str) -> ServiceTransportSpec:
        base = _base_channel(channel)
        try:
            return SERVICE_TRANSPORT_SPECS[base]
        except KeyError as exc:
            raise ValidationError("服务通道未登记") from exc

    def _runtime(self, channel: str) -> dict[str, object]:
        return self.config_service.get_service_runtime(channel)

    def _endpoint(self, channel: str, settings: Mapping[str, object]) -> str:
        spec = self._spec(channel)
        endpoint = _text(settings.get("endpoint")) or spec.default_endpoint
        return endpoint.rstrip("/")

    def descriptor(self, channel: str) -> dict[str, object]:
        runtime = self._runtime(channel)
        settings = runtime["settings"] if isinstance(runtime["settings"], Mapping) else {}
        secrets = runtime["secrets"] if isinstance(runtime["secrets"], Mapping) else {}
        inherited = runtime["inherited_secrets"] if isinstance(runtime["inherited_secrets"], Mapping) else {}
        spec = self._spec(channel)
        key_present = bool(_effective_api_key(channel, secrets, inherited))
        pair_present = any(
            all(_text(secrets.get(field)) for field in pair)
            for pair in (
                ("primary_access_key", "primary_secret_key"),
                ("backup_access_key", "backup_secret_key"),
            )
        )
        if spec.auth_mode in {"HMAC-SHA256", "AK/SK"}:
            credentials_present = pair_present
        elif spec.auth_mode == "not_applicable":
            credentials_present = True
        else:
            credentials_present = key_present
        timeout = settings.get("timeout_seconds") or 60
        retries = settings.get("max_retries", settings.get("max_attempts", 1))
        try:
            timeout = max(3.0, min(float(timeout), 3600.0))
        except (TypeError, ValueError):
            timeout = 60.0
        try:
            retries = max(0, min(int(retries), 10))
        except (TypeError, ValueError):
            retries = 1
        model = _model(settings, "primary_model", "RUNNINGHUB_APP_ID", "IMAGE2_MODEL", "SEEDANCE_PRIMARY_MODEL_EP")
        return {
            "channel_id": channel,
            "base_channel_id": _base_channel(channel),
            "entrypoint": spec.entrypoint,
            "endpoint": self._endpoint(channel, settings),
            "auth_mode": spec.auth_mode,
            "credential_present": credentials_present,
            "credential_ref": _text(runtime.get("credential_ref")),
            "model": model,
            "timeout_seconds": timeout,
            "max_retries": retries,
            "operations": list(spec.operations),
            "billable_operations": list(spec.billable_operations),
            "verification": spec.verification,
            "generation_started": False,
        }

    def _headers(
        self,
        channel: str,
        endpoint: str,
        method: str,
        settings: Mapping[str, object],
        secrets: Mapping[str, object],
        inherited: Mapping[str, object],
    ) -> dict[str, str]:
        spec = self._spec(channel)
        headers = {"Accept": "*/*"}
        if _base_channel(channel) == "tikhub":
            # Keep the read-only auth probe aligned with the business transport.
            # TikHub's edge can reject the generic urllib client signature.
            headers.update({"Accept": "application/json", "User-Agent": "TopicCenterMigration/1.0"})
        key = _effective_api_key(channel, secrets, inherited)
        if spec.auth_mode == "Bearer" and key:
            headers["Authorization"] = _bearer_header(key)
        elif spec.auth_mode == "X-Api-Key" and key:
            headers["X-Api-Key"] = key
        elif spec.auth_mode == "HMAC-SHA256":
            # The preflight is deliberately an empty HEAD request. It signs the
            # request with the same field family as the source transport but
            # never sends a GenBGM payload or creates a task.
            access = _text(secrets.get("primary_access_key")) or _text(secrets.get("backup_access_key"))
            secret = _text(secrets.get("primary_secret_key")) or _text(secrets.get("backup_secret_key"))
            if access and secret:
                headers.update(self._volcengine_headers(
                    endpoint,
                    method,
                    access,
                    secret,
                    region=_text(settings.get("region")) or "cn-beijing",
                    service=_text(settings.get("service")) or "imagination",
                    api_version=_text(settings.get("api_version")) or "2024-08-12",
                ))
        elif spec.auth_mode == "AK/SK":
            access = _text(secrets.get("primary_access_key")) or _text(secrets.get("backup_access_key"))
            secret = _text(secrets.get("primary_secret_key")) or _text(secrets.get("backup_secret_key"))
            if access and secret and _base_channel(channel) == "audio-storage":
                headers.update(self._tos_headers(
                    endpoint,
                    method,
                    access,
                    secret,
                    region=_text(settings.get("region")) or "cn-beijing",
                ))
        return headers

    @staticmethod
    def _volcengine_headers(
        endpoint: str,
        method: str,
        access: str,
        secret: str,
        *,
        region: str,
        service: str,
        api_version: str,
    ) -> dict[str, str]:
        # Keep the signing surface minimal; the actual generation transport is
        # still a separate operation and is never reached by verify().
        import hashlib
        import hmac
        from urllib.parse import urlsplit

        now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        date = now[:8]
        host = urlsplit(endpoint).netloc
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\nx-content-sha256:{payload_hash}\nx-date:{now}\n"
        signed = "content-type;host;x-content-sha256;x-date"
        canonical_request = "\n".join([method.upper(), urlsplit(endpoint).path or "/", "", canonical_headers, signed, payload_hash])
        def digest(key: bytes, value: str) -> bytes:
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()
        scope = f"{date}/{region}/{service}/request"
        string_to_sign = "\n".join(["HMAC-SHA256", now, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
        date_key = digest(secret.encode("utf-8"), date)
        region_key = digest(date_key, region)
        service_key = digest(region_key, service)
        signature = hmac.new(digest(service_key, "request"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-Content-Sha256": payload_hash,
            "X-Date": now,
            "X-Api-Version": api_version,
            "Authorization": f"HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}",
        }

    @staticmethod
    def _tos_headers(
        endpoint: str,
        method: str,
        access: str,
        secret: str,
        *,
        region: str,
    ) -> dict[str, str]:
        """Build the standard read-only TOS4 signature without an SDK."""

        parsed = urlsplit(endpoint)
        host = parsed.netloc
        now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        date = now[:8]
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_headers = f"host:{host}\nx-tos-content-sha256:{payload_hash}\nx-tos-date:{now}\n"
        signed = "host;x-tos-content-sha256;x-tos-date"
        canonical_request = "\n".join([
            method.upper(),
            parsed.path or "/",
            "",
            canonical_headers,
            signed,
            payload_hash,
        ])

        def digest(key: bytes, value: str) -> bytes:
            return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

        scope = f"{date}/{region}/tos/request"
        string_to_sign = "\n".join([
            "TOS4-HMAC-SHA256",
            now,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        date_key = digest(secret.encode("utf-8"), date)
        region_key = digest(date_key, region)
        service_key = digest(region_key, "tos")
        signing_key = digest(service_key, "request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "Accept": "application/json",
            "Host": host,
            "X-Tos-Content-Sha256": payload_hash,
            "X-Tos-Date": now,
            "Authorization": f"TOS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}",
        }

    @staticmethod
    def _probe_request(channel: str, endpoint: str) -> tuple[str, str] | None:
        base = _base_channel(channel)
        if base == "tikhub":
            return "GET", endpoint + _TIKHUB_AUTH_PATH
        if base == "capcut-mate":
            return "GET", _origin(endpoint) + "/healthz"
        apimodels_auth_url = _apimodels_balance_url(channel, endpoint)
        if apimodels_auth_url:
            return "GET", apimodels_auth_url
        ark_auth_url = _ark_models_auth_url(channel, endpoint)
        if ark_auth_url:
            return "GET", ark_auth_url
        tos_auth_url = _tos_list_buckets_url(channel, endpoint)
        if tos_auth_url:
            return "GET", tos_auth_url
        return None

    @staticmethod
    def _no_safe_auth_probe_result(spec: ServiceTransportSpec) -> dict[str, object]:
        message = (
            f"{spec.entrypoint}没有独立免费鉴权端点；为避免启动生成、合成或创建任务，"
            "本次未发送供应商请求。请在实际业务调用前单独授权并验证。"
        )
        return {
            "connection_status": "not_verifiable_without_generation",
            "connection_status_label": CONNECTION_STATUS_LABELS["not_verifiable_without_generation"],
            "executed": True,
            "external_request": False,
            "auth_verified": False,
            "http_status": None,
            "attempts": 0,
            "error_type": "auth_requires_billable_operation",
            "evidence": "未发送外部请求：当前协议没有独立免费鉴权端点",
            "verification_contract": _NO_SAFE_AUTH_VERIFICATION_CONTRACT,
            "generation_started": False,
            "paid_call": False,
            "message": message,
        }

    def _preflight(self, channel: str, descriptor: Mapping[str, object], runtime: Mapping[str, object]) -> dict[str, object]:
        spec = self._spec(channel)
        settings = runtime["settings"] if isinstance(runtime.get("settings"), Mapping) else {}
        secrets = runtime["secrets"] if isinstance(runtime.get("secrets"), Mapping) else {}
        inherited = runtime["inherited_secrets"] if isinstance(runtime.get("inherited_secrets"), Mapping) else {}
        endpoint = _text(descriptor.get("endpoint"))
        base = _base_channel(channel)
        apimodels_auth_probe = _apimodels_balance_url(channel, endpoint)
        ark_auth_probe = _ark_models_auth_url(channel, endpoint)
        tos_auth_probe = _tos_list_buckets_url(channel, endpoint)
        probe = self._probe_request(channel, endpoint)
        if probe is None:
            return self._no_safe_auth_probe_result(spec)
        method, url = probe
        # A verification probe must not inherit a 300-second generation
        # timeout; keep the control-plane button bounded and independent of
        # production task timeouts.
        timeout = min(float(descriptor.get("timeout_seconds") or 60), 15.0)
        retries = int(descriptor.get("max_retries") or 0)
        attempts = 0
        last_status: int | None = None
        last_error = ""
        auth_verified = False
        message = CONNECTION_STATUS_LABELS["request_failed"]
        for attempt in range(retries + 1):
            attempts = attempt + 1
            current_status: int | None = None
            current_error = ""
            request = Request(url, method=method, headers=self._headers(channel, url, method, settings, secrets, inherited))
            try:
                with self.opener(request, timeout=timeout) as response:
                    current_status = int(getattr(response, "status", 200))
            except HTTPError as exc:
                current_status = int(exc.code)
                current_error = "http_error"
            except (URLError, TimeoutError, OSError) as exc:
                current_error = type(exc).__name__
            last_status = current_status
            last_error = current_error
            if current_status not in RETRYABLE_HTTP_STATUS and not (current_status is None and current_error):
                break
            if attempt < retries:
                self.sleeper(0.05 * (attempt + 1))
        if last_status is None:
            status = "request_failed"
        else:
            if base == "tikhub":
                if 200 <= last_status < 300:
                    status = "verified"
                    auth_verified = True
                    last_error = ""
                    message = "TikHub 账户鉴权验证通过。"
                elif last_status == 401:
                    status = "auth_failed"
                    last_error = "http_401"
                    message = "TikHub 鉴权失败：Token 无效、缺失或已过期。"
                elif last_status == 403:
                    status = "forbidden"
                    last_error = "http_403_permission_or_account"
                    message = "TikHub 返回 403：供应商拒绝当前 Token 或接口权限；请检查 Token 所属供应商账户状态、邮箱验证和 Token/接口权限。"
                elif last_status == 402:
                    status = "billing_blocked"
                    last_error = "http_402_balance_or_billing"
                    message = "TikHub 返回 402：该接口需要余额或可用额度。"
                elif last_status == 429:
                    status = "request_failed"
                    last_error = "http_429_rate_limited"
                    message = "TikHub 请求过快，触发限流。"
                else:
                    status = "request_failed"
                    last_error = f"http_{last_status}_client_error" if 400 <= last_status < 500 else f"http_{last_status}_server_error"
                    message = f"TikHub 请求失败（HTTP {last_status}）。"
            elif apimodels_auth_probe:
                if 200 <= last_status < 300:
                    status = "verified"
                    auth_verified = True
                    last_error = ""
                    message = f"API Models 账户余额只读鉴权验证通过（GET {_APIMODELS_BALANCE_PATH}，HTTP {last_status}）；未启动生成任务。"
                elif last_status == 401:
                    status = "auth_failed"
                    last_error = "http_401"
                    message = "API Models 返回 HTTP 401：API Key 无效、缺失或已过期；未启动生成任务。"
                elif last_status == 403:
                    status = "forbidden"
                    last_error = "http_403_permission_or_account"
                    message = "API Models 返回 HTTP 403：当前 API Key 或账户没有账户接口权限；未启动生成任务。"
                elif last_status == 402:
                    status = "billing_blocked"
                    last_error = "http_402_balance_or_billing"
                    message = "API Models 返回 HTTP 402：余额或账户状态受限；未启动生成任务。"
                elif last_status == 404:
                    status = "request_failed"
                    last_error = "http_404_auth_probe_not_found"
                    message = "API Models 余额验证路径返回 HTTP 404；请检查 endpoint 是否为官方 /v1 根地址。"
                else:
                    status = "request_failed"
                    last_error = f"http_{last_status}_server_error" if last_status >= 500 else f"http_{last_status}_client_error"
                    message = f"API Models 余额鉴权验证失败（HTTP {last_status}）；未启动生成任务。"
            elif ark_auth_probe:
                if 200 <= last_status < 300:
                    status = "verified"
                    auth_verified = True
                    last_error = ""
                    message = f"方舟 API Key 鉴权验证通过（GET {_ARK_MODELS_AUTH_PATH}，HTTP {last_status}）；未启动生成任务。"
                elif last_status == 401:
                    status = "auth_failed"
                    last_error = "http_401"
                    message = "方舟返回 HTTP 401：当前 API Key 未通过鉴权；未启动生成任务。"
                elif last_status == 403:
                    status = "forbidden"
                    last_error = "http_403_permission_or_account"
                    message = "方舟返回 HTTP 403：当前 API Key 或账户没有访问模型列表的权限；未启动生成任务。"
                elif last_status == 402:
                    status = "billing_blocked"
                    last_error = "http_402_balance_or_billing"
                    message = "方舟返回 HTTP 402：账户计费或额度受限；未启动生成任务。"
                elif last_status == 404:
                    status = "request_failed"
                    last_error = "http_404_auth_probe_not_found"
                    message = "方舟模型列表验证路径返回 HTTP 404；未判定 API Key 无效，请检查 endpoint 或代理路径。"
                else:
                    status = "request_failed"
                    last_error = f"http_{last_status}_server_error" if last_status >= 500 else f"http_{last_status}_client_error"
                    message = f"方舟模型列表鉴权验证失败（HTTP {last_status}）；未启动生成任务。"
            elif tos_auth_probe:
                if 200 <= last_status < 300:
                    status = "verified"
                    auth_verified = True
                    last_error = ""
                    message = f"TOS ListBuckets 只读鉴权验证通过（GET /，HTTP {last_status}）；未执行上传或删除。"
                elif last_status == 401:
                    status = "auth_failed"
                    last_error = "http_401"
                    message = "TOS 返回 HTTP 401：AK/SK 无效或签名不匹配；未执行写入。"
                elif last_status == 403:
                    status = "forbidden"
                    last_error = "http_403_permission_or_account"
                    message = "TOS 返回 HTTP 403：AK/SK 已到达供应商，但缺少 ListBuckets 权限；未执行写入。"
                elif last_status == 404:
                    status = "request_failed"
                    last_error = "http_404_auth_probe_not_found"
                    message = "TOS 服务地址返回 HTTP 404；请检查地域 endpoint。"
                else:
                    status = "request_failed"
                    last_error = f"http_{last_status}_server_error" if last_status >= 500 else f"http_{last_status}_client_error"
                    message = f"TOS 只读鉴权验证失败（HTTP {last_status}）；未执行写入。"
            elif spec.auth_mode == "not_applicable" and 200 <= last_status < 300:
                status = "verified"
                message = CONNECTION_STATUS_LABELS[status]
            elif 200 <= last_status < 500:
                # Generic probes only prove that the host answered. They do
                # not prove supplier authentication.
                status = "reachable_not_auth_verified"
                message = CONNECTION_STATUS_LABELS[status]
            else:
                status = "request_failed"
                message = CONNECTION_STATUS_LABELS[status]
        if base == "tikhub":
            evidence = f"GET {_TIKHUB_AUTH_PATH} -> HTTP {last_status}" if last_status is not None else f"GET {_TIKHUB_AUTH_PATH} 未收到 HTTP 响应"
        elif apimodels_auth_probe:
            evidence = f"GET {_APIMODELS_BALANCE_PATH} -> HTTP {last_status}" if last_status is not None else f"GET {_APIMODELS_BALANCE_PATH} 未收到 HTTP 响应"
        elif ark_auth_probe:
            evidence = f"GET {_ARK_MODELS_AUTH_PATH} -> HTTP {last_status}" if last_status is not None else f"GET {_ARK_MODELS_AUTH_PATH} 未收到 HTTP 响应"
        elif tos_auth_probe:
            evidence = f"GET / -> HTTP {last_status}" if last_status is not None else "GET / 未收到 HTTP 响应"
        else:
            evidence = ""
        return {
            "connection_status": status,
            "connection_status_label": CONNECTION_STATUS_LABELS[status],
            "executed": True,
            "external_request": True,
            # Only the documented TikHub account endpoint can set this true;
            # generic health/preflight responses remain connection-only.
            "auth_verified": auth_verified,
            "http_status": last_status,
            "attempts": attempts,
            "error_type": last_error,
            "evidence": evidence,
            "verification_contract": (
                TIKHUB_AUTH_VERIFICATION_CONTRACT if base == "tikhub" else
                _APIMODELS_AUTH_VERIFICATION_CONTRACT if apimodels_auth_probe else
                _ARK_MODELS_AUTH_VERIFICATION_CONTRACT if ark_auth_probe else
                _TOS_AUTH_VERIFICATION_CONTRACT if tos_auth_probe else ""
            ),
            "generation_started": False,
            "paid_call": False,
            "message": message,
        }

    def verify(self, channel: str, *, confirm: str) -> dict[str, object]:
        if confirm not in {"safe-readonly-connection", "controlled-auth-verification", "deferred-connection-check"}:
            raise ValidationError("连接验证需要有效的 confirm 标记")
        descriptor = self.descriptor(channel)
        runtime = self._runtime(channel)
        if not bool(descriptor["credential_present"]):
            result = {
                "connection_status": "blocked_missing_credential",
                "connection_status_label": CONNECTION_STATUS_LABELS["blocked_missing_credential"],
                "executed": False,
                "external_request": False,
                "auth_verified": False,
                "http_status": None,
                "attempts": 0,
                "error_type": "missing_credential",
                "verification_contract": TIKHUB_AUTH_VERIFICATION_CONTRACT if _base_channel(channel) == "tikhub" else "",
                "generation_started": False,
                "paid_call": False,
                "message": CONNECTION_STATUS_LABELS["blocked_missing_credential"],
            }
        else:
            result = self._preflight(channel, descriptor, runtime)
        self.config_service.record_connection_result(channel, result)
        return {"provider": channel, **descriptor, **result}

    def contract_snapshot(self, channel: str) -> dict[str, object]:
        """返回正式页面/API 使用的脱敏入口和重试契约。"""

        return self.descriptor(channel)


__all__ = [
    "CONNECTION_STATUS_LABELS",
    "RETRYABLE_HTTP_STATUS",
    "SERVICE_TRANSPORT_SPECS",
    "ServiceTransportError",
    "ServiceTransportSpec",
    "UnifiedApiTransport",
]
