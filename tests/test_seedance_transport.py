from __future__ import annotations

import pytest

from workflow_1256.seedance_transport import (
    SeedanceConfig,
    SeedanceHTTPTransport,
    SeedanceTransportError,
)


def test_seedance_config_requires_local_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEEDANCE_PRIMARY_API_KEY", raising=False)
    monkeypatch.delenv("SEEDANCE_PRIMARY_MODEL_EP", raising=False)

    with pytest.raises(SeedanceTransportError):
        SeedanceConfig.from_env()


def test_seedance_config_reads_env_without_logging_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEEDANCE_PRIMARY_API_KEY", "key-local-only")
    monkeypatch.setenv("SEEDANCE_PRIMARY_MODEL_EP", "ep-local-only")
    monkeypatch.setenv("SEEDANCE_TOS_ACCESS_KEY", "ak-local-only")
    monkeypatch.setenv("SEEDANCE_TOS_SECRET_KEY", "sk-local-only")
    monkeypatch.setenv("SEEDANCE_TOS_BUCKET", "bucket-local-only")

    config = SeedanceConfig.from_env()

    assert config.auth_configs()[0]["api_key"] == "Bearer key-local-only"
    assert config.auth_configs()[0]["model_ep"] == "ep-local-only"
    assert config.tos_configs()[0]["bucket"] == "bucket-local-only"


def test_seedance_config_keeps_existing_bearer_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEEDANCE_PRIMARY_API_KEY", "Bearer key-local-only")
    monkeypatch.setenv("SEEDANCE_PRIMARY_MODEL_EP", "ep-local-only")

    config = SeedanceConfig.from_env()

    assert config.auth_configs()[0]["api_key"] == "Bearer key-local-only"


def test_seedance_config_strips_shell_quotes_before_adding_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEEDANCE_PRIMARY_API_KEY", '"key-local-only"')
    monkeypatch.setenv("SEEDANCE_PRIMARY_MODEL_EP", "ep-local-only")

    config = SeedanceConfig.from_env()

    assert config.auth_configs()[0]["api_key"] == "Bearer key-local-only"


def test_seedance_config_maps_four_auth_sets_in_order() -> None:
    config = SeedanceConfig.from_api_keys(
        ["key-1", "key-2", "key-3", "key-4"],
        legacy_model_eps=["ep-1", "ep-2", "ep-3", "ep-4"],
    )

    configs = config.auth_configs()
    assert [item["name"] for item in configs] == ["主账号", "备用账号", "第三账号", "第四账号"]
    assert [item["api_key"] for item in configs] == [
        "Bearer key-1", "Bearer key-2", "Bearer key-3", "Bearer key-4"
    ]
    assert [item["model_ep_1_0"] for item in configs] == ["ep-1", "ep-2", "ep-3", "ep-4"]
    assert all(item["model_ep_1_5"] == "doubao-seedance-1-5-pro-251215" for item in configs)


def test_transport_restores_module_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEEDANCE_PRIMARY_API_KEY", "key-local-only")
    monkeypatch.setenv("SEEDANCE_PRIMARY_MODEL_EP", "ep-local-only")
    transport = SeedanceHTTPTransport.from_env()

    from workflow_1256 import video_generate, video_query

    original_generate_configs = video_generate.AUTH_CONFIGS
    original_query_configs = video_query.AUTH_CONFIGS
    with transport._configured_modules() as (generate_module, query_module):
        assert generate_module.AUTH_CONFIGS[0]["api_key"] == "Bearer key-local-only"
        assert query_module.AUTH_CONFIGS[0]["model_ep"] == "ep-local-only"

    assert video_generate.AUTH_CONFIGS is original_generate_configs
    assert video_query.AUTH_CONFIGS is original_query_configs


def test_transport_query_uses_real_clock_and_sleep_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SeedanceConfig(primary_api_key="key", primary_model_ep="ep")
    transport = SeedanceHTTPTransport(config)
    from workflow_1256 import video_query

    captured = {}

    def fake_run(_params, **kwargs):
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(video_query, "run_video_query", fake_run)
    assert transport.query({"task_id": "task"}) == {"success": True}
    assert captured["clock"].time() > 1_000_000_000
    assert captured["sleep"] is not None
