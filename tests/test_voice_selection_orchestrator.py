from workflow_1256.orchestrator import run_daily_update_te
from tests.test_orchestrator import FakeCapCut, fake_directors, fake_speech, fake_timelines, fake_visual_intent


def test_tts_resolves_model_candidate_to_catalogued_speaker_only():
    requests = []
    catalog = {
        "defaults": {"male": "male_explain", "female": "female_story", "neutral": ""},
        "voices": [
            {"voice_key": "male_explain", "speaker_id": "speaker-m", "display_name": "男声", "gender": "male", "style_tags": ["科普"], "languages": ["zh-CN"], "verified": True},
            {"voice_key": "female_story", "speaker_id": "speaker-f", "display_name": "女声", "gender": "female", "style_tags": ["叙事"], "languages": ["zh-CN"], "verified": True},
        ],
    }
    def speech(params):
        requests.append(dict(params))
        return fake_speech(params)
    result = run_daily_update_te(
        {"text": "text", "tts": {"voice_catalog": catalog, "voice_selection": {"enabled": True, "voice_gender": "auto"}}},
        capcut_client=FakeCapCut(), directors_transport=fake_directors,
        node_runners={"102833": fake_visual_intent, "159953": speech, "165901": fake_timelines, "voice_selector": lambda _prompt: {"candidate_key": "female_story", "voice_profile": "温暖叙事", "reason": "匹配"}},
    )
    assert result.outputs["voice_resolution"]["speaker_id"] == "speaker-f"
    assert result.outputs["voice_resolution"]["source"] == "llm_resolved"
    assert "voice_selector" in result.completed_nodes
    assert requests[0]["speaker_id"] == requests[0]["voice_id"] == "speaker-f"
    assert "voice_catalog" not in requests[0] and "voice_selection" not in requests[0]
