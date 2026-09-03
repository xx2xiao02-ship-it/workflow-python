import json

from workflow_1256.voice_director_transport import (
    Seed21ProVoiceDirectorTransport,
    load_seedance_api_keys_from_auth_document,
    load_shot_refinement_seed21_pro_auth_from_document,
    load_shot_refinement_seed21_turbo_auth_from_document,
)


def test_seed_21_pro_uses_disabled_thinking_and_falls_back_to_backup():
    calls = []
    def requester(_url, headers, payload, _timeout):
        calls.append((headers["Authorization"], payload))
        if len(calls) == 1:
            raise RuntimeError("primary unavailable")
        return {"choices": [{"message": {"content": json.dumps({"segments": []})}}]}
    transport = Seed21ProVoiceDirectorTransport(primary_api_key="primary", primary_model="pro-primary", backup_api_key="backup", backup_model="pro-backup", requester=requester)
    assert transport({"segments": []}) == {"segments": []}
    assert [call[0] for call in calls] == ["Bearer primary", "Bearer backup"]
    assert calls[0][1]["thinking"] == {"type": "disabled"}
    assert calls[1][1]["model"] == "pro-backup"


def test_seed_21_pro_reuses_existing_shared_ark_configuration(monkeypatch):
    for env_name in (
        "VOICE_DIRECTOR_PRIMARY_MODEL",
        "VOICE_DIRECTOR_BACKUP_MODEL",
        "DIRECTORS_V2_ARK_MODEL",
        "VOICE_DIRECTOR_BACKUP_API_KEY",
        "STORY_WRITER_ARK_BACKUP_API_KEY",
        "DIRECTORS_V2_ARK_BACKUP_API_KEY",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("STORY_WRITER_ARK_API_KEY", "shared-primary")
    monkeypatch.setenv("STORY_WRITER_ARK_MODEL", "shared-model")
    transport = Seed21ProVoiceDirectorTransport.from_env()
    assert transport.auths == [("shared-primary", "shared-model")]


def test_load_seedance_keys_only_from_seedance_section(tmp_path):
    auth = tmp_path / "auth.md"
    auth.write_text("\\# TTS\ntts-key-012345678901234567890123\n\\# seedance 1.5pro\nseed-primary-012345678901234567890123\nseed-backup-012345678901234567890123\n", encoding="utf-8")
    assert load_seedance_api_keys_from_auth_document(auth) == [
        "seed-primary-012345678901234567890123",
        "seed-backup-012345678901234567890123",
    ]


def test_load_shot_refinement_primary_backup_and_matching_models(tmp_path):
    auth = tmp_path / "auth.md"
    auth.write_text("$env:SHOT\\_REFINEMENT\\_PRIMARY\\_API\\_KEY = \"primary-012345678901234567890123\"\n$env:SHOT\\_REFINEMENT\\_BACKUP\\_API\\_KEY = \"backup-012345678901234567890123\"\nSeed 2.1 pro  ep-primary\nSeed 2.1 pro  ep-backup\n", encoding="utf-8")
    assert load_shot_refinement_seed21_pro_auth_from_document(auth) == (
        "primary-012345678901234567890123", "backup-012345678901234567890123", "ep-primary", "ep-backup"
    )


def test_load_shot_refinement_turbo_reuses_one_endpoint_for_primary_and_backup(tmp_path):
    auth = tmp_path / "auth.md"
    auth.write_text(
        "$env:SHOT\\_REFINEMENT\\_PRIMARY\\_API\\_KEY = \"primary-012345678901234567890123\"\n"
        "$env:SHOT\\_REFINEMENT\\_BACKUP\\_API\\_KEY = \"backup-012345678901234567890123\"\n"
        "Seed 2.1 turbo  ep-shared\n",
        encoding="utf-8",
    )
    assert load_shot_refinement_seed21_turbo_auth_from_document(auth) == (
        "primary-012345678901234567890123",
        "backup-012345678901234567890123",
        "ep-shared",
        "ep-shared",
    )
