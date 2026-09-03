import json

import pytest

from workflow_1256.voice_catalog_store import VoiceCatalogStore, VoiceCatalogStoreError


def test_store_seeds_and_atomically_persists_extended_voice_fields(tmp_path):
    path = tmp_path / "voice_catalog.json"
    store = VoiceCatalogStore(path, seed={
        "version": "test",
        "defaults": {"male": "custom", "female": "", "neutral": ""},
        "voices": [{
            "voice_key": "custom", "speaker_id": "S_CUSTOM", "display_name": "定制音色",
            "gender": "male", "languages": ["zh-CN"], "style_tags": [],
            "voice_type": "custom", "resource_id": "seed-icl-2.0", "model": "seed-tts-2.0-standard",
            "enabled": True, "verified": False,
        }],
    })
    snapshot = store.snapshot()
    assert path.is_file()
    assert snapshot["schema_version"] == "voice-catalog-v1"
    assert snapshot["voices"][0]["resource_id"] == "seed-icl-2.0"
    assert json.loads(path.read_text(encoding="utf-8"))["voices"][0]["voice_type"] == "custom"


def test_store_register_rejects_duplicate_and_update_cannot_change_stable_key(tmp_path):
    store = VoiceCatalogStore(tmp_path / "voice_catalog.json")
    store.snapshot()
    with pytest.raises(VoiceCatalogStoreError, match="voice_key 已存在"):
        store.register({"voice_key": "qingcang_2", "speaker_id": "S_NEW", "display_name": "重复", "gender": "male"})
    with pytest.raises(VoiceCatalogStoreError, match="不可修改"):
        store.update("qingcang_2", {"voice_key": "other"})


def test_store_update_supports_soft_disable(tmp_path):
    store = VoiceCatalogStore(tmp_path / "voice_catalog.json")
    store.snapshot()
    value = store.update("qingcang_2", {"enabled": False})
    item = next(item for item in value["voices"] if item["voice_key"] == "qingcang_2")
    assert item["enabled"] is False


def test_custom_voice_resource_rejects_official_resource(tmp_path):
    store = VoiceCatalogStore(tmp_path / "voice_catalog.json")
    with pytest.raises(VoiceCatalogStoreError, match="resource_id 与 voice_type 不匹配"):
        store.register({
            "voice_key": "custom_bad", "speaker_id": "S_BAD", "display_name": "错误资源",
            "gender": "neutral", "voice_type": "custom", "resource_id": "seed-tts-2.0",
        })


def test_custom_voice_resource_icl_1_is_preserved(tmp_path):
    store = VoiceCatalogStore(tmp_path / "voice_catalog.json")
    snapshot = store.register({
        "voice_key": "custom_icl1", "speaker_id": "S_ICL1", "display_name": "旧版定制",
        "gender": "neutral", "voice_type": "custom", "resource_id": "seed-icl-1.0",
    })
    assert snapshot["voices"][-1]["resource_id"] == "seed-icl-1.0"


def test_custom_voice_without_resource_is_migrated_to_icl_2(tmp_path):
    store = VoiceCatalogStore(tmp_path / "voice_catalog.json", seed={
        "version": "v0", "defaults": {"male": "", "female": "", "neutral": "custom"},
        "voices": [{
            "voice_key": "custom", "speaker_id": "S_CUSTOM", "display_name": "旧定制",
            "gender": "neutral", "voice_type": "custom", "model": "seed-tts-2.0-standard",
        }],
    })
    item = store.snapshot()["voices"][0]
    assert item["resource_id"] == "seed-icl-2.0"
