from workflow_1256.sound_effect_production import produce_sound_effects
def test_plan_result_order_is_stable():
    rows=[]
    for i in range(1,21): rows.append({"shot_id":f"g01_s{i:02d}","enabled":False,"subject":"","subject_confidence":0,"cue_start_us":0,"cue_end_us":1})
    lock={"shots":[{"shot_id":f"g01_s{i:02d}","timeline":{"start_us":0,"end_us":2}} for i in range(1,21)],"sound_effect_plan":rows}
    out=produce_sound_effects(lock,dry_run=True)
    assert [x["shot_id"] for x in out["shot_results"]] == [x["shot_id"] for x in rows]
