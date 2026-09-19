from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from topic_migration.config_service import FIXED_SETTINGS, SERVICE_CATALOG, ConfigService
from topic_migration.errors import PersistenceError, ValidationError


def key(label):
    return "synthetic_" + label + "_test_credential"


def test_all_services_save_restart_and_secret_masking(tmp_path):
    service = ConfigService(tmp_path)
    channels = {}
    for channel in SERVICE_CATALOG:
        secrets = {} if channel == "capcut-mate" else {"primary_api_key": key(channel)}
        if channel in {"bgm-audio", "audio-storage"}:
            secrets = {"primary_access_key": key(channel + "ak"), "primary_secret_key": key(channel + "sk")}
        channels[channel] = {"settings": {"endpoint": FIXED_SETTINGS.get(channel, {}).get("endpoint", "https://example.test")}, "secrets": secrets}
    result = service.merge_import(channels)
    assert result["conflicts"] == []
    for group in result["snapshot"]["groups"]:
        assert group["config_status"] == "imported"
        assert group["connection_status"] == "not_tested"
        assert group["auth_verified"] is False
        assert group["business_status"] == "migrated"
        assert group["credential_status"] == ("not_applicable" if group["channel_id"] == "capcut-mate" else "configured")
    serialized = json.dumps(result)
    assert "synthetic_" not in serialized
    assert "synthetic_" not in service.config_path.read_text(encoding="utf-8")
    if os.name == "nt":
        assert service.secret_path.read_bytes().startswith(b"U1:")
        assert b"synthetic_" not in service.secret_path.read_bytes()
    probe = subprocess.run([sys.executable, "-c", "import json,sys;from topic_migration.config_service import ConfigService;s=ConfigService(sys.argv[1]);print(json.dumps(s.public_snapshot()))", str(tmp_path)], capture_output=True, text=True, check=True)
    restarted = json.loads(probe.stdout)
    assert [(g["settings"], g["credential_status"]) for g in restarted["groups"]] == [(g["settings"], g["credential_status"]) for g in result["snapshot"]["groups"]]


def test_merge_conflicts_empty_values_order_and_seed_fallback(tmp_path):
    service = ConfigService(tmp_path)
    service.save({"channel_id": "digital-human", "settings": {"endpoint": "https://target.test", "model_slots": {"RUNNINGHUB_APP_ID": "existing"}, "model_order": ["second", "first"]}, "secrets": {"primary_api_key": key("existing")}})
    incoming = {"digital-human": {"settings": {"endpoint": "https://source.test", "model_slots": {"RUNNINGHUB_APP_ID": "incoming"}, "model_order": ["first", "second"]}, "secrets": {"primary_api_key": key("incoming")}}, "tts": {"secrets": {"primary_api_key": key("tts")}}}
    merged = service.merge_import(incoming)
    assert len(merged["conflicts"]) == 4
    assert key("incoming") not in json.dumps(merged)
    rows = {g["channel_id"]: g for g in merged["snapshot"]["groups"]}
    assert rows["digital-human"]["settings"]["model_order"] == ["second", "first"]
    assert rows["sound-effect"]["credential_status"] == "configured"
    assert rows["sound-effect"]["credential_source"] == "tts.primary_api_key"
    service.save({"channel_id": "digital-human", "secrets": {"primary_api_key": ""}})
    assert service._read_secrets()["digital-human"]["primary_api_key"] == key("existing")
    result = service.connection_test_deferred({"channels": ["digital-human"], "confirm": "controlled-auth-verification"})
    assert result["executed"] is False and result["auth_verified"] is False


def test_invalid_credentials_and_partial_pairs(tmp_path):
    service = ConfigService(tmp_path)
    for value in ('"quoted"', "two words", "********", "line\nbreak"):
        with pytest.raises(ValidationError):
            service.save({"channel_id": "tts", "secrets": {"primary_api_key": value}})
    service.save({"channel_id": "bgm-audio", "secrets": {"primary_access_key": key("ak")}})
    row = next(g for g in service.public_snapshot()["groups"] if g["channel_id"] == "bgm-audio")
    assert row["credential_status"] == "incomplete"
    with pytest.raises(ValidationError):
        service.save({"channel_id": "capcut-mate", "secrets": {"primary_api_key": key("wrong")}})
    with pytest.raises(ValidationError):
        service.save({"channel_id": "tts", "settings": {"endpoint": "https://old.test/v1"}})


def test_save_rollback_and_corrupt_config_protection(tmp_path, monkeypatch):
    service = ConfigService(tmp_path)
    service.save({"channel_id": "tts", "secrets": {"primary_api_key": key("old")}})
    original = service.config_path.read_bytes()
    def fail(_config):
        raise PersistenceError("synthetic failure")
    monkeypatch.setattr(service, "_write_config", fail)
    with pytest.raises(PersistenceError):
        service.save({"channel_id": "tts", "secrets": {"primary_api_key": key("new")}})
    assert service._read_secrets()["tts"]["primary_api_key"] == key("old")
    assert service.config_path.read_bytes() == original
    service.config_path.write_text("invalid json", encoding="utf-8")
    with pytest.raises(PersistenceError):
        ConfigService(tmp_path).save({"channel_id": "tts"})


def test_model_id_mapping_and_custom_channel_are_preserved(tmp_path):
    service = ConfigService(tmp_path)
    custom = "image-generation__custom_test"
    settings = {"endpoint": "https://api.test", "model_slots": {"IMAGE2_MODEL": "custom-model"}, "model_order": ["IMAGE2_MODEL"]}
    service.merge_import({custom: {"settings": settings, "secrets": {"primary_api_key": key("custom"), "extra_api_keys": [key("extra")]}}})
    restarted = ConfigService(Path(tmp_path))
    group = next(g for g in restarted.public_snapshot()["groups"] if g["channel_id"] == custom)
    assert group["settings"] == settings
    assert restarted._read_secrets()[custom]["extra_api_keys"] == [key("extra")]
