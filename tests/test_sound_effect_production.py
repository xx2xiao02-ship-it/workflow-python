import pytest
import wave
from pathlib import Path
from workflow_1256.sound_effect_production import produce_sound_effects, SoundEffectProductionError, build_seed_audio_request, local_library_transport, build_sound_effect_plan, seed_audio_transport_from_env, SeedAudioHTTPTransport
from workflow_1256.ark_audio_transport import ArkAudioConfig, ArkAudioHTTPTransport

def lock(rows):
    return {"shots":[{"shot_id":f"g01_s0{i}","timeline":{"start_us":0,"end_us":3_000_000}} for i in range(1,len(rows)+1)],"sound_effect_plan":rows}
def row(i=1, **kw):
    x={"shot_id":f"g01_s0{i}","enabled":True,"subject":"物件","subject_confidence":.9,"sound_effect_id":"sfx","prompt":"短音","cue_start_us":1000,"cue_end_us":2000,"volume":.8,"status":"PLANNED"}; x.update(kw); return x
def test_dry_run_no_transport_call():
    called=[]; out=produce_sound_effects(lock([row()]), dry_run=True, transport=lambda x: called.append(x))
    assert not called and out["shot_results"][0]["status"] == "planned"
def test_duplicate_and_out_of_bounds_rejected():
    with pytest.raises(SoundEffectProductionError): produce_sound_effects(lock([row(), row(2,shot_id="g01_s01")]))
    with pytest.raises(SoundEffectProductionError): produce_sound_effects(lock([row(cue_end_us=4_000_000)]))
def test_low_confidence_needs_review_without_transport():
    called=[]; out=produce_sound_effects(lock([row(subject_confidence=.2)]), transport=lambda x: called.append(x))
    assert out["shot_results"][0]["status"] == "needs_review" and not called

def test_success_reads_real_wav_duration_and_reuses_hash(tmp_path):
    audio = tmp_path / "coin.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(1000); handle.writeframes(b"\0\0" * 1500)
    calls=[]
    transport=lambda payload: calls.append(payload) or {"status":"succeeded","local_path":str(audio),"provider":"library"}
    first=produce_sound_effects(lock([row()]), transport=transport)
    second=produce_sound_effects(lock([row()]), transport=transport, existing_results={"g01_s01":first["shot_results"][0]})
    assert first["shot_results"][0]["duration_us"] == 1_500_000
    assert first["shot_results"][0]["content_hash"] == second["shot_results"][0]["content_hash"]
    assert len(calls) == 1

def test_seed_audio_request_builder_is_side_effect_free():
    request = build_seed_audio_request(row(prompt="单一清脆硬币声"))
    assert request["model"] == "seed-audio-1.0"
    assert request["audio_config"]["format"] == "mp3"

def test_seed_audio_transport_maps_verified_remote_result_without_network():
    calls = []
    def requester(url, headers, payload, timeout):
        calls.append((url, payload))
        return {"status_code": 200, "headers": {"X-Tt-Logid": "test-log"}, "body": {"code": 0, "url": "https://audio.example/sfx.mp3", "duration": 1.25}}
    client = ArkAudioHTTPTransport(ArkAudioConfig(api_key="test-only"), requester=requester)
    result = client.generate_sound_effect(build_seed_audio_request(row(prompt="短促硬币声")))
    assert result["status"] == "succeeded" and result["duration_us"] == 1_250_000
    assert calls and calls[0][1]["model"] == "seed-audio-1.0"

def test_seed_audio_transport_from_env_requires_local_credentials(monkeypatch):
    for name in ("ARK_AUDIO_API_KEY", "ARK_BGM_API_KEY", "ARK_TTS_API_KEY", "ARK_API_KEY", "VOLCENGINE_AUDIO_API_KEY", "ARK_AUDIO_APP_ID", "ARK_AUDIO_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(Exception, match="未配置"):
        seed_audio_transport_from_env()

def test_seed_audio_http_transport_uses_v3_prompt_contract_without_network():
    seen = []
    def requester(url, headers, payload, timeout):
        seen.append((url, headers, payload))
        return {"status_code": 200, "body": {"url": "https://audio.example/sfx.mp3", "duration": 1.5}}
    transport = SeedAudioHTTPTransport("test-only", requester=requester)
    result = transport({"prompt": "清脆硬币声", "sound_effect_id": "coin_drop_clean"})
    assert result["status"] == "succeeded" and result["duration_us"] == 1_500_000
    assert seen[0][0].endswith("/api/v3/tts/create")
    assert seen[0][1]["X-Api-Key"] == "test-only"
    assert seen[0][2]["text_prompt"] == "清脆硬币声"

def test_seed_audio_http_transport_can_persist_binary_audio_locally(tmp_path):
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(1000); handle.writeframes(b"\0\0" * 1500)
    transport = SeedAudioHTTPTransport("test-only", requester=lambda *args: {"status_code": 200, "body": buf.getvalue()}, local_output_dir=tmp_path)
    result = transport({"prompt": "短促硬币声"})
    assert result["status"] == "succeeded" and result["duration_us"] == 1_500_000
    assert Path(result["local_path"]).is_file()

def test_seed_audio_transport_can_use_api_management_secret_mapping():
    transport = SeedAudioHTTPTransport.from_api_management_secrets({"tts": {"primary_api_key": "stored-key"}})
    assert transport.api_key == "stored-key"

def test_remote_audio_result_requires_url_and_duration():
    remote = lambda payload: {"status": "succeeded", "audio_url": "https://audio.example/sfx.mp3", "duration_us": 900000}
    result = produce_sound_effects(lock([row()]), provider="seed_audio_1_0", transport=remote)
    assert result["status"] == "SUCCEEDED"
    assert result["shot_results"][0]["audio_url"].startswith("https://")
    bad = lambda payload: {"status": "succeeded", "audio_url": "https://audio.example/sfx.mp3"}
    failed = produce_sound_effects(lock([row()]), provider="seed_audio_1_0", transport=bad)
    assert failed["status"] == "FAILED"
    assert failed["shot_results"][0]["error_type"] == "audio_unreadable"

def test_local_library_can_process_twenty_real_files_in_order():
    root = __import__('pathlib').Path('outputs')
    if not root.is_dir() or not list(root.rglob('*.mp3')):
        pytest.skip('本地音效库为空')
    rows=[row(1, shot_id=f"g{i:02d}_s01", cue_start_us=(i-1)*3_000_000, cue_end_us=(i-1)*3_000_000+1_000_000, sound_effect_id='not-found') for i in range(1,21)]
    lock={"shots":[{"shot_id":f"g{i:02d}_s01","timeline":{"start_us":(i-1)*3_000_000,"end_us":i*3_000_000}} for i in range(1,21)],"sound_effect_plan":rows}
    result=produce_sound_effects(lock, provider='library', transport=local_library_transport(root))
    assert result['status']=='SUCCEEDED' and result['generated_count']==20
    assert [x['shot_id'] for x in result['shot_results']]==[x['shot_id'] for x in rows]

def test_plan_builder_keeps_shots_and_reviews_digital_humans():
    plan = build_sound_effect_plan([
        {"shot_id":"g01_s01","clip_role":"primary_visual","sound_effect_subject":"金币","timeline":{"start_us":0,"end_us":2_000_000}},
        {"shot_id":"g01_s02","clip_role":"digital_human","timeline":{"start_us":2_000_000,"end_us":4_000_000}},
    ])
    assert [item["shot_id"] for item in plan] == ["g01_s01", "g01_s02"]
    assert plan[1]["status"] == "NEEDS_REVIEW" and plan[1]["enabled"] is False
