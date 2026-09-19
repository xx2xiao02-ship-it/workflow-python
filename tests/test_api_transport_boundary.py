from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from topic_migration.api_transports import UnifiedApiTransport
from topic_migration.config_service import TIKHUB_AUTH_VERIFICATION_CONTRACT, ConfigService
from topic_migration.tikhub_transport import TikHubTransport


class _Response:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def _key(label: str) -> str:
    return "synthetic_" + label + "_transport_key"


def _row(service: ConfigService, channel: str) -> dict:
    return next(item for item in service.public_snapshot()["groups"] if item["channel_id"] == channel)


class _FakeTransportConfig:
    def __init__(self, token: str) -> None:
        self.token = token
        self.results = []

    def get_service_runtime(self, channel: str) -> dict:
        return {
            "channel_id": channel,
            "settings": {"endpoint": "https://api.tikhub.io", "timeout_seconds": 3, "max_retries": 0},
            "secrets": {"primary_api_key": self.token},
            "inherited_secrets": {},
            "credential_ref": "tikhub.primary_api_key",
        }

    def record_connection_result(self, _channel: str, result: dict) -> None:
        self.results.append(dict(result))


def test_target_transport_entrypoints_preserve_real_field_mapping(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.merge_import({
        "digital-human": {
            "settings": {
                "endpoint": "https://runninghub.example/openapi/v2",
                "model_slots": {"RUNNINGHUB_APP_ID": "app-synthetic"},
                "timeout_seconds": 45,
            },
            "secrets": {"primary_api_key": _key("runninghub")},
        },
        "tts": {
            "settings": {"timeout_seconds": 300},
            "secrets": {"primary_api_key": _key("tts")},
        },
        "sound-effect": {"settings": {}, "secrets": {}},
        "bgm-audio": {
            "settings": {"endpoint": "https://music.example/", "region": "cn-beijing"},
            "secrets": {"primary_access_key": _key("ak"), "primary_secret_key": _key("sk")},
        },
        "capcut-mate": {"settings": {"endpoint": "http://127.0.0.1:30000"}, "secrets": {}},
    })
    gateway = UnifiedApiTransport(service)
    runninghub = gateway.contract_snapshot("digital-human")
    assert runninghub["entrypoint"] == "RunningHubInfiniteTalkTransport"
    assert runninghub["model"] == "app-synthetic"
    assert runninghub["max_retries"] == 1
    assert "synthetic_" not in str(runninghub)
    sound_effect = gateway.contract_snapshot("sound-effect")
    assert sound_effect["credential_present"] is True
    assert sound_effect["auth_mode"] == "X-Api-Key"
    assert gateway.contract_snapshot("bgm-audio")["auth_mode"] == "HMAC-SHA256"
    assert gateway.contract_snapshot("capcut-mate")["credential_present"] is True


def test_generation_protocol_without_free_auth_probe_does_not_send_request(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.save({
        "channel_id": "tts",
        "settings": {"timeout_seconds": 3, "max_retries": 1},
        "secrets": {"primary_api_key": _key("tts")},
    })
    gateway = UnifiedApiTransport(service, opener=lambda *_args, **_kwargs: pytest.fail("no external request expected"))
    result = gateway.verify("tts", confirm="safe-readonly-connection")
    assert result["executed"] is True
    assert result["external_request"] is False
    assert result["generation_started"] is False
    assert result["paid_call"] is False
    assert result["connection_status"] == "not_verifiable_without_generation"
    assert result["auth_verified"] is False
    assert _key("tts") not in str(result)
    restarted = ConfigService(tmp_path)
    row = _row(restarted, "tts")
    assert row["connection_status"] == "not_verifiable_without_generation"
    assert row["test_connection_executed"] is True
    assert row["auth_verified"] is False
    assert row["verification_error_type"] == "auth_requires_billable_operation"


@pytest.mark.parametrize("channel", ["story-writing", "director-seed21", "video-generation"])
def test_ark_language_model_auth_uses_readonly_models_endpoint(tmp_path: Path, channel: str) -> None:
    service = ConfigService(tmp_path)
    service.save({
        "channel_id": channel,
        "settings": {"endpoint": "https://ark.cn-beijing.volces.com/api/v3/chat/completions"},
        "secrets": {"primary_api_key": _key(channel)},
    })
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, dict(request.header_items()), timeout))
        return _Response(200, b'{"data": []}')

    result = UnifiedApiTransport(service, opener=opener).verify(
        channel, confirm="controlled-auth-verification"
    )

    assert calls[0][0] == "GET"
    assert calls[0][1] == "https://ark.cn-beijing.volces.com/api/v3/models"
    assert {name.lower(): value for name, value in calls[0][2].items()}["authorization"] == "Bearer " + _key(channel)
    assert result["connection_status"] == "verified"
    assert result["auth_verified"] is True
    assert result["verification_contract"] == "ark-models-list-v1"
    assert result["evidence"] == "GET /api/v3/models -> HTTP 200"
    assert result["generation_started"] is False
    assert result["paid_call"] is False
    assert _key(channel) not in str(result)
    saved = _row(ConfigService(tmp_path), channel)
    assert saved["auth_verified"] is True
    assert saved["verification_contract"] == "ark-models-list-v1"


@pytest.mark.parametrize(
    ("http_status", "connection_status", "error_type"),
    [
        (401, "auth_failed", "http_401"),
        (403, "forbidden", "http_403_permission_or_account"),
        (404, "request_failed", "http_404_auth_probe_not_found"),
    ],
)
def test_ark_models_auth_failure_is_not_misreported_as_network_only(
    tmp_path: Path, http_status: int, connection_status: str, error_type: str
) -> None:
    service = ConfigService(tmp_path)
    service.save({
        "channel_id": "story-writing",
        "settings": {"endpoint": "https://ark.cn-beijing.volces.com/api/v3/responses"},
        "secrets": {"primary_api_key": _key("ark-failure")},
    })

    def opener(_request, timeout):
        return _Response(http_status)

    result = UnifiedApiTransport(service, opener=opener).verify(
        "story-writing", confirm="controlled-auth-verification"
    )
    assert result["connection_status"] == connection_status
    assert result["auth_verified"] is False
    assert result["http_status"] == http_status
    assert result["error_type"] == error_type
    assert result["generation_started"] is False
    assert result["paid_call"] is False


def test_non_ark_language_model_endpoint_is_not_claimed_as_auth_verified(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.save({
        "channel_id": "story-writing",
        "settings": {"endpoint": "https://api.example.test/v1/chat/completions"},
        "secrets": {"primary_api_key": _key("custom-ark-compatible")},
    })
    result = UnifiedApiTransport(service, opener=lambda *_args, **_kwargs: pytest.fail("no generic probe expected")).verify(
        "story-writing", confirm="controlled-auth-verification"
    )
    assert result["connection_status"] == "not_verifiable_without_generation"
    assert result["external_request"] is False
    assert result["auth_verified"] is False
    assert result["error_type"] == "auth_requires_billable_operation"


def test_api_models_uses_authenticated_balance_endpoint(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.save({
        "channel_id": "image-generation",
        "settings": {"endpoint": "https://api.apimodels.app/v1"},
        "secrets": {"primary_api_key": _key("apimodels")},
    })
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, dict(request.header_items()), timeout))
        return _Response(200, b'{"balance": 1}')

    result = UnifiedApiTransport(service, opener=opener).verify(
        "image-generation", confirm="controlled-auth-verification"
    )
    headers = {name.lower(): value for name, value in calls[0][2].items()}
    assert calls[0][0] == "GET"
    assert calls[0][1] == "https://api.apimodels.app/v1/balance"
    assert headers["authorization"] == "Bearer " + _key("apimodels")
    assert result["connection_status"] == "verified"
    assert result["auth_verified"] is True
    assert result["verification_contract"] == "apimodels-balance-v1"
    assert result["evidence"] == "GET /v1/balance -> HTTP 200"
    assert result["generation_started"] is False
    assert result["paid_call"] is False
    assert _key("apimodels") not in str(result)


def test_tos_uses_signed_list_buckets_readonly_endpoint(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.save({
        "channel_id": "audio-storage",
        "settings": {"endpoint": "https://tos-cn-beijing.volces.com", "region": "cn-beijing"},
        "secrets": {
            "primary_access_key": _key("tos-ak"),
            "primary_secret_key": _key("tos-sk"),
        },
    })
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, dict(request.header_items()), timeout))
        return _Response(200, b"<ListAllMyBucketsResult />")

    result = UnifiedApiTransport(service, opener=opener).verify(
        "audio-storage", confirm="controlled-auth-verification"
    )
    headers = {name.lower(): value for name, value in calls[0][2].items()}
    assert calls[0][0] == "GET"
    assert calls[0][1] == "https://tos-cn-beijing.volces.com/"
    assert headers["authorization"].startswith("TOS4-HMAC-SHA256 Credential=")
    assert headers["x-tos-date"]
    assert result["connection_status"] == "verified"
    assert result["auth_verified"] is True
    assert result["verification_contract"] == "tos-list-buckets-v1"
    assert result["evidence"] == "GET / -> HTTP 200"
    assert _key("tos-ak") not in str(result)
    assert _key("tos-sk") not in str(result)


@pytest.mark.parametrize("channel", ["digital-human", "sound-effect", "bgm-audio"])
def test_generation_only_channels_share_explicit_no_probe_result(tmp_path: Path, channel: str) -> None:
    service = ConfigService(tmp_path)
    if channel == "digital-human":
        service.save({
            "channel_id": channel,
            "settings": {"endpoint": "https://www.runninghub.cn/openapi/v2"},
            "secrets": {"primary_api_key": _key(channel)},
        })
    elif channel == "sound-effect":
        service.save({"channel_id": "tts", "secrets": {"primary_api_key": _key("shared-tts")}})
        service.save({"channel_id": channel, "settings": {}, "secrets": {}})
    else:
        service.save({
            "channel_id": channel,
            "settings": {"endpoint": "https://open.volcengineapi.com", "region": "cn-beijing"},
            "secrets": {
                "primary_access_key": _key("bgm-ak"),
                "primary_secret_key": _key("bgm-sk"),
            },
        })
    result = UnifiedApiTransport(service, opener=lambda *_args, **_kwargs: pytest.fail("no external request expected")).verify(
        channel, confirm="safe-readonly-connection"
    )
    assert result["connection_status"] == "not_verifiable_without_generation"
    assert result["external_request"] is False
    assert result["error_type"] == "auth_requires_billable_operation"
    assert result["verification_contract"] == "no-safe-auth-probe-v1"
    assert result["generation_started"] is False
    assert result["paid_call"] is False


def test_tikhub_auth_verification_uses_documented_account_endpoint() -> None:
    token = _key("tikhub")
    service = _FakeTransportConfig(token)
    calls = []

    def opener(request, timeout):
        headers = {key.lower(): value for key, value in request.header_items()}
        calls.append((request.get_method(), request.full_url, timeout, headers))
        return _Response(200)

    result = UnifiedApiTransport(service, opener=opener).verify("tikhub", confirm="controlled-auth-verification")
    assert result["connection_status"] == "verified"
    assert result["auth_verified"] is True
    assert result["generation_started"] is False
    assert result["paid_call"] is False
    assert calls == [
        (
            "GET",
            "https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
            3.0,
            {
                "accept": "application/json",
                "authorization": "Bearer " + token,
                "user-agent": "TopicCenterMigration/1.0",
            },
        )
    ]
    assert service.results[-1]["connection_status"] == "verified"
    assert service.results[-1]["auth_verified"] is True
    assert service.results[-1]["evidence"] == "GET /api/v1/tikhub/user/get_user_info -> HTTP 200"
    assert service.results[-1]["verification_contract"] == TIKHUB_AUTH_VERIFICATION_CONTRACT
    assert token not in str(result)


def test_tikhub_old_verification_record_is_not_reported_as_current(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    config = service._default_config()
    config["providers"]["tikhub"].update({
        "connection_status": "verified",
        "auth_verified": True,
        "test_connection_executed": True,
        "verification_http_status": 200,
        "verification_attempts": 1,
        "verification_evidence": "authorized_read_only_query_http_200",
        "verification_contract": "legacy-root-head-v0",
    })
    service.config_path.parent.mkdir(parents=True, exist_ok=True)
    service.config_path.write_text(json.dumps(config), encoding="utf-8")

    row = _row(service, "tikhub")
    assert row["connection_status"] == "not_tested"
    assert row["auth_verified"] is False
    assert row["test_connection_executed"] is False
    assert row["verification_http_status"] is None
    assert row["verification_error_type"] == "stale_verification_contract"


def test_tikhub_current_verification_contract_is_persisted(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.record_connection_result("tikhub", {
        "connection_status": "verified",
        "executed": True,
        "auth_verified": True,
        "http_status": 200,
        "attempts": 1,
        "evidence": "GET /api/v1/tikhub/user/get_user_info -> HTTP 200",
        "verification_contract": TIKHUB_AUTH_VERIFICATION_CONTRACT,
    })

    row = _row(service, "tikhub")
    assert row["connection_status"] == "verified"
    assert row["auth_verified"] is True
    assert row["verification_contract"] == TIKHUB_AUTH_VERIFICATION_CONTRACT


@pytest.mark.parametrize(
    ("http_status", "connection_status", "error_type"),
    [
        (401, "auth_failed", "http_401"),
        (403, "forbidden", "http_403_permission_or_account"),
        (402, "billing_blocked", "http_402_balance_or_billing"),
    ],
)
def test_tikhub_auth_verification_exposes_documented_failure_class(
    tmp_path: Path, http_status: int, connection_status: str, error_type: str
) -> None:
    service = _FakeTransportConfig(_key("tikhub-status"))

    def opener(_request, timeout):
        return _Response(http_status)

    result = UnifiedApiTransport(service, opener=opener).verify("tikhub", confirm="controlled-auth-verification")
    assert result["connection_status"] == connection_status
    assert result["auth_verified"] is False
    assert result["http_status"] == http_status
    assert result["error_type"] == error_type
    if http_status == 403:
        assert result["connection_status_label"] == "供应商拒绝 Token/接口权限"
        assert "Token 所属供应商账户" in result["message"]
    assert result["generation_started"] is False
    assert result["paid_call"] is False


def test_tikhub_business_request_does_not_duplicate_bearer_prefix(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    token = "Bearer " + _key("already-prefixed")
    service.get_tikhub_transport_config = lambda: SimpleNamespace(
        endpoint="https://api.tikhub.io",
        token=token,
        timeout_seconds=3.0,
        max_retries=0,
    )
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, timeout, request.get_header("Authorization")))
        return _Response(200, b"{}")

    result = TikHubTransport(service, opener=opener).request_json("GET", "/api/v1/tikhub/user/get_user_info")
    assert result == {}
    assert calls[0][3] == token


def test_missing_credential_does_not_send_request(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    calls = []
    gateway = UnifiedApiTransport(service, opener=lambda *_args, **_kwargs: calls.append(True))
    result = gateway.verify("visual-guidance", confirm="safe-readonly-connection")
    assert result["connection_status"] == "blocked_missing_credential"
    assert result["executed"] is False
    assert calls == []
    assert _row(service, "visual-guidance")["auth_verified"] is False


def test_capcut_health_probe_is_non_auth_and_marks_connection_only(tmp_path: Path) -> None:
    service = ConfigService(tmp_path)
    service.save({"channel_id": "capcut-mate", "settings": {"endpoint": "http://127.0.0.1:30000"}})
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, timeout))
        return _Response(200)

    result = UnifiedApiTransport(service, opener=opener).verify("capcut-mate", confirm="safe-readonly-connection")
    assert result["connection_status"] == "verified"
    assert result["auth_verified"] is False
    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/healthz")
