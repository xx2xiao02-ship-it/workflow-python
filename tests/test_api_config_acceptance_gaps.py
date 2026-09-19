"""Acceptance regressions found during the API-only governance audit."""

from topic_migration.config_service import ConfigService


def _group(snapshot, channel):
    return next(row for row in snapshot["groups"] if row["channel_id"] == channel)


def test_empty_import_is_not_reported_as_imported(tmp_path):
    service = ConfigService(tmp_path)
    result = service.merge_import({"visual-guidance": {"settings": {}, "secrets": {}}})
    row = _group(result["snapshot"], "visual-guidance")
    assert row["config_status"] != "imported", "Empty source must not claim successful migration"


def test_unresolved_conflict_is_visible_after_restart(tmp_path):
    service = ConfigService(tmp_path)
    service.save({"channel_id": "digital-human", "settings": {"endpoint": "https://target.example"}})
    result = service.merge_import({"digital-human": {"settings": {"endpoint": "https://source.example"}}})
    assert result["conflicts"]
    row = _group(ConfigService(tmp_path).public_snapshot(), "digital-human")
    assert row["settings"]["endpoint"] == "https://target.example"
    assert row["config_status"] != "imported", "Unresolved conflict must not appear fully imported"
    assert row["migration_conflicts"][0]["field"] == "settings.endpoint"
    service.save({"channel_id": "digital-human", "settings": {"enabled": False}})
    assert _group(service.public_snapshot(), "digital-human")["migration_conflicts"]
    service.merge_import({"digital-human": {"settings": {"primary_model": "application"}}})
    assert _group(service.public_snapshot(), "digital-human")["config_status"] == "import_conflict"
    service.merge_import({"digital-human": {"settings": {"endpoint": "https://target.example"}}})
    assert _group(service.public_snapshot(), "digital-human")["migration_conflicts"] == []


def test_empty_audio_import_does_not_count_injected_defaults(tmp_path):
    result = ConfigService(tmp_path).merge_import({"tts": {}})
    assert _group(result["snapshot"], "tts")["config_status"] == "not_imported"
