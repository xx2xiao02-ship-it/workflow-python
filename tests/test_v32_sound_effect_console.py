from tools import video_production_console as console


def test_assets_page_exposes_v32_sound_effect_status_and_dry_run_entry():
    page = console._assets_page().decode("utf-8")
    assert 'id="soundEffectBox" class="material-box sound-effect-box" hidden' in page
    assert 'id="soundEffectDryRun"' in page
    assert 'id="soundEffectStatus"' in page
    assert "查看 dry-run" in page
    for label in ("未启用", "待审核", "待生成", "生成中", "生成成功", "部分失败", "需要人工审核"):
        assert label in page
    assert "不产生外部请求" in page


def test_assets_page_only_shows_sound_effect_box_for_v32_tasks():
    page = console._assets_page().decode("utf-8")
    assert "function directorPackageFrom(source)" in page
    assert "box.hidden=packageId!=='v3_2'" in page
    assert "currentDirectorPackage=directorPackageFrom(currentStory);syncSoundEffectVisibility(currentStory)" in page
    assert "syncSoundEffectVisibility(scriptTask)" in page
    assert "task=x;taskId=x?x.task_id:'';syncSoundEffectVisibility(x)" in page
    assert "source?.director_package||result.director_package||input.director_package" in page
