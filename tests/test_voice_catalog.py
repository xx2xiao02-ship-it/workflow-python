import pytest

from workflow_1256.voice_catalog import (
    DEFAULT_VOICE_CATALOG,
    VoiceCatalogError,
    build_voice_candidates,
    build_voice_selection_prompt,
    resolve_voice_selection,
)


CATALOG = {
    "defaults": {"male": "male_explain", "female": "female_story", "neutral": ""},
    "voices": [
        {"voice_key": "male_explain", "speaker_id": "speaker-m", "display_name": "男声", "gender": "male", "style_tags": ["成熟", "科普"], "languages": ["zh-CN"], "verified": True},
        {"voice_key": "female_story", "speaker_id": "speaker-f", "display_name": "女声", "gender": "female", "style_tags": ["温暖", "叙事"], "languages": ["zh-CN"], "verified": True},
    ],
}


def test_model_only_sees_compact_candidates_and_never_speaker_id():
    candidates = build_voice_candidates(CATALOG, voice_gender="female")
    prompt = build_voice_selection_prompt(["原文"], {"voice_gender": "female"}, candidates)
    assert prompt["candidates"][0]["voice_key"] == "female_story"
    assert "speaker_id" not in prompt["candidates"][0]


def test_user_exact_speaker_wins_over_model_selection():
    result = resolve_voice_selection({"speaker_id": "speaker-f"}, catalog=CATALOG, model_selection={"candidate_key": "male_explain"})
    assert result["speaker_id"] == "speaker-f"
    assert result["source"] == "user_speaker_id"


def test_model_must_choose_from_filtered_candidates():
    result = resolve_voice_selection({"voice_selection": {"voice_gender": "female"}}, catalog=CATALOG, model_selection={"candidate_key": "female_story", "voice_profile": "温暖叙事", "reason": "文案"})
    assert result["speaker_id"] == "speaker-f"
    assert result["source"] == "llm_resolved"
    with pytest.raises(VoiceCatalogError, match="候选列表以外"):
        resolve_voice_selection({"voice_selection": {"voice_gender": "female"}}, catalog=CATALOG, model_selection={"candidate_key": "male_explain"})


def test_gender_default_and_unknown_explicit_id_are_deterministic():
    result = resolve_voice_selection({"voice_gender": "female"}, catalog=CATALOG)
    assert result["speaker_id"] == "speaker-f"
    assert result["source"] == "catalog_default"
    with pytest.raises(VoiceCatalogError, match="未登记"):
        resolve_voice_selection({"speaker_id": "made-up"}, catalog=CATALOG)


def test_enabled_official_voice_is_a_normal_tts_candidate_without_verification():
    catalog = {
        "defaults": {"male": "pending", "female": "", "neutral": ""},
        "voices": [{
            "voice_key": "pending", "speaker_id": "speaker-pending", "display_name": "待验收",
            "gender": "male", "languages": ["zh-CN"], "enabled": True, "verified": False,
        }],
    }
    result = resolve_voice_selection({"speaker_id": "speaker-pending"}, catalog=catalog)
    assert result["speaker_id"] == "speaker-pending"
    assert build_voice_candidates(catalog, voice_gender="male")[0]["voice_key"] == "pending"


def test_builtin_catalog_has_official_female_default_without_user_setup():
    result = resolve_voice_selection({"voice_gender": "female"}, catalog=DEFAULT_VOICE_CATALOG)
    assert result["voice_key"] == "vivi_2"
    assert result["speaker_id"] == "zh_female_vv_uranus_bigtts"


def test_builtin_catalog_contains_new_user_requested_male_voices():
    voices = {item["speaker_id"]: item for item in DEFAULT_VOICE_CATALOG["voices"]}
    requested = (
        "zh_male_youyoujunzi_uranus_bigtts",
        "zh_male_kailangxuezhang_uranus_bigtts",
        "zh_male_shenyeboke_uranus_bigtts",
        "zh_male_liufei_uranus_bigtts",
        "zh_male_dayi_uranus_bigtts",
        "zh_male_m191_uranus_bigtts",
    )
    for speaker_id in requested:
        assert speaker_id in voices
        assert voices[speaker_id]["gender"] == "male"
        assert voices[speaker_id]["enabled"] is True
        assert voices[speaker_id]["languages"] == ["zh-CN"]
        resolved = resolve_voice_selection({"speaker_id": speaker_id}, catalog=DEFAULT_VOICE_CATALOG)
        assert resolved["speaker_id"] == speaker_id


def test_language_matching_is_case_insensitive_for_synced_catalogs():
    catalog = {
        "defaults": {"male": "male_lower", "female": "", "neutral": ""},
        "voices": [{
            "voice_key": "male_lower",
            "speaker_id": "speaker-lower",
            "display_name": "同步男声",
            "gender": "male",
            "languages": ["zh-cn"],
            "enabled": True,
            "verified": True,
        }],
    }
    result = resolve_voice_selection({"voice_gender": "male"}, catalog=catalog)
    assert result["voice_key"] == "male_lower"
