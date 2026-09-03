import json

from workflow_1256.voice_selector_transport import Seed21ProVoiceSelectorTransport


def test_voice_selector_uses_candidate_only_prompt_and_falls_back_to_backup():
    calls = []
    def requester(_url, headers, payload, _timeout):
        calls.append((headers["Authorization"], payload))
        if len(calls) == 1:
            raise RuntimeError("primary unavailable")
        return {"choices": [{"message": {"content": json.dumps({"candidate_key": "voice_a"})}}]}
    transport = Seed21ProVoiceSelectorTransport(primary_api_key="primary", primary_model="model-primary", backup_api_key="backup", backup_model="model-backup", requester=requester)
    assert transport({"candidates": [{"voice_key": "voice_a"}]}) == {"candidate_key": "voice_a"}
    assert [item[0] for item in calls] == ["Bearer primary", "Bearer backup"]
    assert calls[0][1]["thinking"] == {"type": "disabled"}
    assert "speaker_id" not in calls[0][1]["messages"][1]["content"]
