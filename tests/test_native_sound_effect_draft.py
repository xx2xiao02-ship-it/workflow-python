from pathlib import Path
from workflow_1256.editing_layer import EditingLayerPlan, write_windows_native_draft
from workflow_1256.governance.contracts import TimeWindow

def test_native_writer_emits_sound_effect_track(tmp_path):
    """使用本地剪映小助手依赖验证“音效”轨真实写入。"""
    capcut = Path(r"C:\Users\Administrator\Documents\capcut-mate")
    if not (capcut / "src").is_dir():
        return
    source = next(Path("tmp/sfx-http").glob("*.mp3"), None)
    if source is None:
        return
    plan = EditingLayerPlan(
        project_id="p", run_id="r", source_plan_version="3.2", source_asset_version="a",
        edit_version="e", width=1920, height=1080, fps=30, platform_os="windows",
        draft_method="native", total_timeline=TimeWindow(0, 1_500_000),
        tracks=[{"track_name":"音效", "track_type":"audio", "track_role":"sound_effect", "item_count":1}],
        native_items=[{"asset_id":"sfx-1", "requirement_id":"req-sfx", "asset_type":"audio", "role":"sound_effect", "track_name":"音效", "source_node":"sound_effect_production", "source_index":0, "start_us":0, "end_us":1_500_000, "duration_us":1_500_000, "asset_duration_us":1_500_000, "native_path":str(source), "remote_url":"", "metadata":{}, "shot_ids":["g01_s01"]}],
        captions=[], capcut_payloads={}, bindings=[], validation={},
    )
    result = write_windows_native_draft(plan, capcut_mate_root=capcut, draft_root=tmp_path, draft_name="v32_sfx_verify")
    content = Path(result.draft_path) / "draft_content.json"
    data = __import__("json").loads(content.read_text(encoding="utf-8"))
    assert any(track.get("name") == "音效" and track.get("type") == "audio" for track in data.get("tracks", []))

def test_native_writer_emits_twenty_sound_effect_segments(tmp_path):
    capcut = Path(r"C:\Users\Administrator\Documents\capcut-mate")
    source = next(Path("tmp/sfx-http").glob("*.mp3"), None)
    if not (capcut / "src").is_dir() or source is None:
        return
    items=[]
    for i in range(20):
        start=i*3_000_000
        items.append({"asset_id":f"sfx-{i:02d}","requirement_id":"req-sfx","asset_type":"audio","role":"sound_effect","track_name":"音效","source_node":"sound_effect_production","source_index":i,"start_us":start,"end_us":start+1_000_000,"duration_us":1_000_000,"asset_duration_us":1_000_000,"native_path":str(source),"remote_url":"","metadata":{},"shot_ids":[f"g{i+1:02d}_s01"]})
    plan=EditingLayerPlan(project_id="p",run_id="r20",source_plan_version="3.2",source_asset_version="a",edit_version="e",width=1920,height=1080,fps=30,platform_os="windows",draft_method="native",total_timeline=TimeWindow(0,60_000_000),tracks=[{"track_name":"音效","track_type":"audio","track_role":"sound_effect","item_count":20}],native_items=items,captions=[],capcut_payloads={},bindings=[],validation={})
    result=write_windows_native_draft(plan,capcut_mate_root=capcut,draft_root=tmp_path,draft_name="v32_sfx_20_verify")
    data=__import__('json').loads((Path(result.draft_path)/"draft_content.json").read_text(encoding='utf-8'))
    tracks=[t for t in data.get('tracks',[]) if t.get('name')=='音效']
    assert len(tracks)==1 and len(tracks[0].get('segments',[]))==20
