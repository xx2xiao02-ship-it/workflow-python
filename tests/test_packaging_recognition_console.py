from __future__ import annotations

import json
from html import escape
from pathlib import Path

import pytest

from tools import video_production_console as console


def _write_draft(path: Path) -> Path:
    path.mkdir()
    draft = {
        "name": "包装识别样例",
        "duration": 10_000_000,
        "tracks": [
            {
                "name": "AIGC动画",
                "type": "video",
                "segments": [{
                    "material_id": "video-1",
                    "target_timerange": {"start": 0, "duration": 10_000_000},
                }],
            },
            {
                "name": "字幕",
                "type": "text",
                "segments": [{
                    "material_id": "text-1",
                    "target_timerange": {"start": 0, "duration": 10_000_000},
                }],
            },
            {
                "name": "解说",
                "type": "audio",
                "segments": [{
                    "material_id": "audio-tts",
                    "target_timerange": {"start": 0, "duration": 10_000_000},
                }],
            },
            {
                "name": "背景音乐",
                "type": "audio",
                "segments": [{
                    "material_id": "audio-bgm",
                    "target_timerange": {"start": 0, "duration": 10_000_000},
                }],
            },
            {
                "name": "包装特效",
                "type": "effect",
                "segments": [{
                    "effect_id": "effect-1",
                    "target_timerange": {"start": 0, "duration": 10_000_000},
                }],
            },
            {
                "name": "转场音效",
                "type": "audio",
                "segments": [
                    {
                        "material_id": "audio-sfx",
                        "target_timerange": {"start": 4_800_000, "duration": 400_000},
                    },
                    {
                        "material_id": "audio-visual-sfx",
                        "target_timerange": {"start": 5_400_000, "duration": 400_000},
                    },
                ],
            },
        ],
        "materials": {
            "videos": [{"id": "video-1", "name": "镜头素材.mp4"}],
            "texts": [{"id": "text-1", "name": "字幕", "content": "测试字幕"}],
            "audios": [
                {"id": "audio-tts", "name": "解说.wav", "type": "voice"},
                {"id": "audio-bgm", "name": "背景音乐.mp3", "type": "music"},
                {"id": "audio-sfx", "name": "whoosh 转场音效.wav", "type": "sound"},
                {"id": "audio-visual-sfx", "name": "写字音效.wav", "type": "sound"},
            ],
            "video_effects": [{"id": "effect-1", "name": "暗角", "effect_id": "vignette"}],
        },
    }
    content = json.dumps(draft, ensure_ascii=False)
    (path / "draft_content.json").write_text(content, encoding="utf-8")
    return path


def _write_pure_variable_draft(path: Path) -> Path:
    path.mkdir()
    draft = {
        "name": "纯变量草稿",
        "duration": 20_000_000,
        "tracks": [
            {"name": "内容画面", "type": "video", "segments": [{
                "material_id": "video-1", "target_timerange": {"start": 0, "duration": 20_000_000},
            }]},
            {"name": "字幕", "type": "text", "segments": [{
                "material_id": "text-1", "target_timerange": {"start": 0, "duration": 20_000_000},
            }]},
            {"name": "解说", "type": "audio", "segments": [{
                "material_id": "audio-tts", "target_timerange": {"start": 0, "duration": 20_000_000},
            }]},
            {"name": "BGM", "type": "audio", "segments": [{
                "material_id": "audio-bgm", "target_timerange": {"start": 0, "duration": 20_000_000},
            }]},
        ],
        "materials": {
            "videos": [{"id": "video-1", "name": "内容镜头.mp4"}],
            "texts": [{"id": "text-1", "name": "字幕", "content": "普通字幕"}],
            "audios": [
                {"id": "audio-tts", "name": "解说.wav", "type": "voice"},
                {"id": "audio-bgm", "name": "背景音乐.mp3", "type": "music"},
            ],
        },
    }
    (path / "draft_content.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return path


def test_inspect_packaging_recognition_draft_reads_without_mutating_source(tmp_path: Path) -> None:
    draft = _write_draft(tmp_path / "包装识别样例")
    before = (draft / "draft_content.json").read_text(encoding="utf-8")

    result = console._inspect_packaging_recognition_draft({"draft_path": str(draft)})

    assert result["status"] == "ready"
    assert result["mode"] == "read_only_no_draft_write"
    assert result["source"]["duration_us"] == 10_000_000
    assert result["summary"]["track_count"] == 6
    assert {lane["track_name"] for lane in result["catalog"]["track_lanes"]} == {
        "AIGC动画", "字幕", "解说", "背景音乐", "包装特效", "转场音效"
    }
    segments_by_track = {
        lane["track_name"]: lane["segments"][0]
        for lane in result["catalog"]["track_lanes"]
    }
    assert result["summary"]["global_element_threshold"] == 0.5
    assert result["summary"]["global_element_count"] == 5
    package_groups = result["catalog"]["packaging_groups"]["groups"]
    assert result["summary"]["packaging_group_count"] >= 1
    global_group = next(group for group in package_groups if group["category"] == "global")
    assert global_group["members"]
    assert segments_by_track["AIGC动画"]["is_global_element"] is True
    assert segments_by_track["AIGC动画"]["duration_ratio"] == 1.0
    assert segments_by_track["转场音效"]["is_global_element"] is False
    sfx_segments = next(
        lane["segments"] for lane in result["catalog"]["track_lanes"]
        if lane["track_name"] == "转场音效"
    )
    visual_sfx = next(segment for segment in sfx_segments if segment["audio_role"] == "visual_enhancement_sfx")
    assert visual_sfx["is_packaging_evidence"] is True
    assert visual_sfx["category"] == "unclassified"
    assert (draft / "draft_content.json").read_text(encoding="utf-8") == before


def test_pure_variable_draft_does_not_create_a_packaging_layer(tmp_path: Path) -> None:
    draft = _write_pure_variable_draft(tmp_path / "纯变量草稿")

    result = console._inspect_packaging_recognition_draft({"draft_path": str(draft)})

    groups = result["catalog"]["packaging_groups"]
    assert result["summary"]["packaging_group_count"] == 0
    assert groups["groups"] == []
    assert groups["representative_groups"] == []
    assert "未发现包装层证据" in result["message"]
    assert not any(
        segment["is_packaging_evidence"]
        for lane in result["catalog"]["track_lanes"]
        for segment in lane["segments"]
    )


def test_inspect_packaging_recognition_draft_requires_draft_content(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="draft_content.json"):
        console._inspect_packaging_recognition_draft({"draft_path": str(tmp_path)})


def test_packaging_group_candidates_list_components_and_preserve_missing_items() -> None:
    catalog = {
        "categories": {"image": {"label": "图片·单图/少图包装", "segment_count": 1}},
        "track_lanes": [
            {"raw_track_index": 0, "track_type": "video", "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 4_000_000,
                "category": "image", "material_name": "镜头图片.png", "is_global_element": False,
            }]},
            {"raw_track_index": 1, "track_type": "effect", "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 4_000_000,
                "category": "image", "material_name": "暗角", "is_global_element": False,
                "keyframes": {"point_count": 3},
            }]},
            {"raw_track_index": 2, "track_type": "filter", "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 4_000_000,
                "category": "image", "material_name": "胶片滤镜", "is_global_element": False,
            }]},
            {"raw_track_index": 3, "track_type": "sticker", "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 4_000_000,
                "category": "image", "material_name": "角标", "is_global_element": False,
            }]},
            {"raw_track_index": 4, "track_type": "audio", "audio_role": "sfx", "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 4_000_000,
                "category": "image", "material_name": "转场音效", "is_global_element": False,
            }]},
            {"raw_track_index": 5, "track_type": "text", "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 4_000_000,
                "category": "image", "material_name": "旅行标题", "material_type": "text_template",
                "parameter_refs": [{"collection": "text_templates"}], "is_global_element": False,
            }]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    assert groups["group_count"] == 1
    group = groups["groups"][0]
    components = {item["key"]: item["count"] for item in group["components"]}
    assert components == {
        "variables": 1,
        "effects": 1,
        "filters": 1,
        "stickers": 1,
        "audio_tracks": 1,
        "text_templates": 1,
        "keyframes": 3,
    }
    assert "text" in group["missing_component_keys"]
    assert {member["component_key"] for member in group["members"]} >= {
        "variables", "effects", "filters", "stickers", "audio_tracks", "text_templates"
    }
    assert "未出现的组件保留为空，不视为错误" in groups["component_policy"]


def test_packaging_group_candidates_merge_adjacent_same_category_on_same_track() -> None:
    catalog = {
        "track_lanes": [{
            "raw_track_index": 4,
            "track_type": "video",
            "segments": [
                {"segment_index": 0, "start_us": 0, "end_us": 1_000_000, "category": "image"},
                {"segment_index": 1, "start_us": 1_150_000, "end_us": 2_000_000, "category": "image"},
                {"segment_index": 2, "start_us": 2_600_000, "end_us": 3_000_000, "category": "image"},
            ],
        }, {
            "raw_track_index": 5,
            "track_type": "effect",
            "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 3_000_000, "category": "image",
            }],
        }],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    assert groups["group_count"] == 2
    assert [group["anchor_count"] for group in groups["groups"]] == [2, 1]
    assert groups["groups"][0]["time_range_us"] == {"start_us": 0, "end_us": 2_000_000}
    assert groups["representative_group_count"] == 1
    assert groups["representative_groups"][0]["anchor_count"] == 2


def test_aigc_video_category_does_not_create_an_automatic_packaging_group() -> None:
    catalog = {
        "categories": {"aigc": {"label": "AI视频", "segment_count": 1}},
        "track_lanes": [
            {"raw_track_index": 1, "track_type": "video", "segments": [{
                "segment_index": 0,
                "start_us": 0,
                "end_us": 3_000_000,
                "category": "aigc",
                "material_name": "aigc-video.mp4",
                "is_global_element": False,
            }]},
            {"raw_track_index": 2, "track_type": "effect", "segments": [{
                "segment_index": 0,
                "start_us": 0,
                "end_us": 3_000_000,
                "category": "aigc",
                "material_name": "局部特效",
                "is_global_element": False,
            }]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    assert not any(group["category"] == "aigc" for group in groups["groups"])
    assert groups["groups"] == []


def test_variable_category_does_not_create_an_automatic_packaging_group() -> None:
    catalog = {
        "categories": {"variable": {"label": "变量内容", "segment_count": 1}},
        "track_lanes": [
            {"raw_track_index": 1, "track_type": "video", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 3_000_000,
                "category": "variable", "material_name": "内容镜头.mp4", "is_global_element": False,
            }]},
            {"raw_track_index": 2, "track_type": "effect", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 3_000_000,
                "category": "variable", "material_name": "局部特效", "is_global_element": False,
            }]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    assert not any(group["category"] == "variable" for group in groups["groups"])
    assert groups["groups"] == []


def test_representative_group_requires_direct_packaging_evidence() -> None:
    catalog = {
        "track_lanes": [
            {"raw_track_index": 1, "track_type": "video", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 3_000_000,
                "category": "image", "material_name": "内容图片.png", "is_global_element": False,
            }]},
            {"raw_track_index": 2, "track_type": "effect", "segments": [{
                "segment_index": 0, "start_us": 3_200_000, "end_us": 4_000_000,
                "category": "image", "material_name": "相邻特效", "is_global_element": False,
            }]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    assert groups["groups"] == []
    assert groups["representative_groups"] == []


def test_packaging_group_uses_cleaner_typical_fragment_and_marks_context() -> None:
    catalog = {
        "image_packaging_patterns": {
            "sequences": [{
                "sequence_id": "sequence_1",
                "time_range_us": {"start_us": 0, "end_us": 6_000_000},
                "members": [
                    {"raw_track_index": 1, "segment_index": 0},
                    {"raw_track_index": 1, "segment_index": 1},
                ],
            }],
        },
        "track_lanes": [
            {"raw_track_index": 1, "track_type": "video", "segments": [
                {"segment_index": 0, "start_us": 0, "end_us": 3_000_000, "category": "image_sequence"},
                {"segment_index": 1, "start_us": 3_000_000, "end_us": 6_000_000, "category": "image_sequence"},
            ]},
            {"raw_track_index": 2, "track_type": "effect", "segments": [{
                "segment_index": 0, "start_us": 3_000_000, "end_us": 6_000_000, "category": "image_sequence",
            }]},
            {"raw_track_index": 3, "track_type": "text", "segments": [
                {"segment_index": 0, "start_us": 0, "end_us": 3_000_000, "category": "variable"},
                {"segment_index": 1, "start_us": 0, "end_us": 3_000_000, "category": "variable"},
            ]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    group = groups["representative_groups"][0]
    assert group["category"] == "image_sequence"
    # 连续图片必须在原始轨道中完整呈现，不能只截取其中一张作为“典型片段”。
    assert group["representative_range_us"] == {"start_us": 0, "end_us": 6_000_000}
    member_matches = {member["raw_track_index"]: member["matches_group_category"] for member in group["members"]}
    assert member_matches[1] is True
    assert member_matches[2] is True
    assert member_matches[3] is False


def test_typical_fragment_uses_exact_main_timeline_range_and_keeps_overlapping_audio() -> None:
    catalog = {
        "track_lanes": [
            {"raw_track_index": 1, "track_type": "video", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 5_000_000,
                "category": "image", "material_name": "主体图片.png", "is_global_element": False,
            }]},
            {"raw_track_index": 2, "track_type": "effect", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 5_000_000,
                "category": "image", "material_name": "边框特效", "is_global_element": False,
            }]},
            {"raw_track_index": -3, "track_type": "audio", "audio_role": "visual_enhancement_sfx", "segments": [{
                "segment_index": 0, "start_us": 4_800_000, "end_us": 7_000_000,
                "category": "unclassified", "material_name": "翻页音效", "is_global_element": False,
            }]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    group = groups["representative_groups"][0]
    assert group["category"] == "image"
    # 典型范围严格等于主图片片段，不因片段结束后的音效继续向后扩张。
    assert group["representative_range_us"] == {"start_us": 0, "end_us": 5_000_000}
    assert group["main_timeline_range_us"] == {"start_us": 0, "end_us": 5_000_000}
    audio_member = next(member for member in group["members"] if member["track_type"] == "audio")
    assert audio_member["matches_group_category"] is True
    assert any(item["key"] == "audio_tracks" for item in group["components"])


def test_global_audio_is_merged_into_global_packaging() -> None:
    catalog = {
        "categories": {"global_audio": {"label": "全局音频", "segment_count": 1}},
        "track_lanes": [{
            "raw_track_index": -1,
            "track_type": "audio",
            "audio_role": "sfx",
            "segments": [{
                "segment_index": 0, "start_us": 2_000_000, "end_us": 5_000_000,
                "category": "global_audio", "material_name": "贯穿音效", "is_global_element": True,
            }],
        }],
    }

    groups = console._build_packaging_recognition_groups(catalog)

    assert groups["group_count"] == 1
    assert groups["representative_group_count"] == 1
    assert groups["representative_groups"][0]["category"] == "global"
    assert groups["representative_groups"][0]["label"] == "全局包装"


def test_boundary_packaging_group_can_overlap_content_category() -> None:
    catalog = {
        "track_lanes": [
            {"raw_track_index": 0, "track_type": "video", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 3_000_000,
                "category": "image", "material_name": "首帧图片.png", "is_global_element": False,
                "boundary_category": "opening",
            }]},
            {"raw_track_index": 1, "track_type": "effect", "segments": [{
                "segment_index": 0, "start_us": 0, "end_us": 3_000_000,
                "category": "unclassified", "material_name": "片头闪白", "is_global_element": False,
                "boundary_category": "opening",
            }]},
        ],
    }

    groups = console._build_packaging_recognition_groups(catalog, duration_us=20_000_000)

    assert {group["category"] for group in groups["groups"]} == {"opening", "image"}
    opening = next(group for group in groups["groups"] if group["category"] == "opening")
    assert opening["time_range_us"] == {"start_us": 0, "end_us": 3_000_000}
    assert opening["representative_range_us"] == {"start_us": 0, "end_us": 3_000_000}
    opening_matches = {member["raw_track_index"]: member["matches_group_category"] for member in opening["members"]}
    assert opening_matches[1] is True


def test_packaging_recognition_page_and_route_are_available() -> None:
    page = console._packaging_recognition_page().decode("utf-8")

    assert 'id="recognitionInspect"' in page
    assert 'id="recognitionDraftList"' in page
    assert "按最近修改时间从近到远排列" in page
    assert "点击草稿在新页面开始识别" in page
    assert "data-draft-path" in page
    assert "inspect.click()" in page
    assert "window.open(nextUrl,'_blank')" in page
    assert "auto_inspect:'1'" in page
    assert "from_packaging" in page
    assert "window.opener.postMessage" in page
    assert "Number(right.modified_at||0)-Number(left.modified_at||0)" in page
    assert "#recognitionInspect[hidden]{display:none!important}" in page
    assert 'id="recognitionLanes"' in page
    assert "/api/editing/packaging-recognition/inspect" in page
    assert f'value="{escape(str(console.JIANYING_DRAFT_ROOT), quote=True)}"' in page
    assert "目录可访问，但其中没有可识别的剪映草稿" in page
    assert ".shell{max-width:none;width:100%" in page
    assert ".recognition-main{grid-template-columns:1fr}" in page
    assert 'id="recognitionInspectorDialog"' in page
    assert 'id="recognitionInspectorClose"' in page
    assert ".recognition-inspector-dialog" in page
    assert "inspectorDialog.showModal()" in page
    assert "inspectorDialog.addEventListener('close'" in page
    assert ".recognition-lanes{max-height:310px}" in page
    assert "全片贯穿（全局元素）" in page
    assert 'id="recognitionLegend"' in page
    assert "const CATEGORY_PALETTE=" in page
    assert "跨轨道的同类别元素永远使用同一种颜色" in page
    assert "button.dataset.packagingCategory=category.key" in page
    assert "renderRuler();renderLegend();syncManualGroupHint();draw();" in page
    assert 'id="recognitionGroupPanel"' in page
    assert 'id="recognitionConfirmProduction"' in page
    assert "确认该包装包" in page
    assert "确认当前草稿已完成该包装包校对" in page
    assert "正在确认包装包…" in page
    assert "location.assign('/production?draft_path='+encodeURIComponent(productionDraftPath))" in page
    assert "不会改写原草稿，也不会伪造视频制作任务。" in page
    assert "params.get('embedded')!=='1'" in page
    assert "完成识别并返回包装设置" in page
    assert "packaging-recognition-complete" in page
    assert "window.parent.postMessage" in page
    assert "'.top,.side,.footer,.recognition-head'" in page
    assert "if(recognitionHead)recognitionHead.hidden=true" in page
    assert "recognition-embedded .top,.recognition-embedded .side" in page
    assert "recognition-embedded .shell{max-width:none!important" in page
    assert 'id="recognitionDraftRootField"' in page
    assert 'id="recognitionDraftPathField"' in page
    assert "root.dispatchEvent(new Event('change'))" in page
    assert "包装分组候选" in page
    assert "包装层按轨道自动" in page
    assert "const packagingKindFor=" in page
    assert "trackIndexFor(lane)<0?'audio':'visual'" in page
    assert "const syncGroupKind=" in page
    assert "const syncInspectorKind=" in page
    assert "groupKind.disabled=true" in page
    assert "kind.disabled=true" in page
    assert "correction.kind=packagingKindFor(groupSelected.lane,groupSelected.segment)" in page
    assert "correction.kind=packagingKindFor(selected.lane,selected.segment)" in page
    assert "renderPackagingGroups(packagingGroupData)" in page
    assert 'id="recognitionManualGroupCategory"' in page
    assert 'id="recognitionManualGroupAdd"' in page
    assert 'id="recognitionSelectedGroupAction"' in page
    assert 'id="recognitionSelectedGroupCategory"' in page
    assert 'id="recognitionSelectedGroupAdd"' in page
    assert "选择识别范围" in page
    assert "const makeManualGroup=" in page
    assert "const manualCategoryNeedsSelection=" in page
    assert "局部分类需要先从下方时间线定位点击一个片段" in page
    assert "先在下方时间线定位点击一个片段，再添加局部包装分组" in page
    assert "const addManualGroup=" in page
    assert "当前时间片内的包装层会同步治理" in page
    assert "const GROUP_ALIGNMENT_TOLERANCE_US=250000" in page
    assert "const overlapsWithTolerance=" in page
    assert "const packagingStyleSignature=" in page
    assert "const synchroniseSimilarPackaging=" in page
    assert "资源标识一致" in page
    assert "时间对齐容差 ±0.25 秒" in page
    assert 'if DEV_READ_ONLY and path != "/api/editing/packaging-recognition/inspect":' in Path(console.__file__).read_text(encoding="utf-8")
    assert "const groupHasPackagingLayer=" in page
    assert "没有包装层的时间片不会进入候选" in page
    assert "当前时间片没有包装层，不能进入包装分组" in page
    assert "包装分组" in page
    assert 'id="recognitionGroupDialog"' in page
    assert "openPackagingGroup(group)" in page
    assert "groupList.contains(button)" in page
    assert "groupDialog.setAttribute('open','')" in page
    assert "recognition-group-segment" in page
    assert "校正此片段" in page
    assert "已校正此片段" in page
    assert "请先点击下方时间线中的具体片段，再进行校正。" in page
    assert "recognition-group-apply-confirmed" in page
    assert ".recognition-track,.recognition-group-track{min-height:26px}" in page
    assert ".recognition-lane-label,.recognition-group-lane-label" in page
    assert ".recognition-lanes{max-height:none;overflow:visible;padding-right:0}" in page
    assert "变量层与包装层标识" in page
    assert "const layerMarker=" in page
    assert "button.dataset.recognitionLayer=layerInfo.className" in page
    assert ".recognition-segment.packaging-visual" in page
    assert "包装层·标题变量" in page
    assert "kind==='variable'?'packaging-variable'" in page
    assert "const laneDisplayLabel=lane=>" in page
    assert "esc(laneDisplayLabel(lane))" in page
    assert "esc(laneDisplayLabel(item.lane))" in page
    assert "grid-template-columns:180px minmax(420px,1fr)" in page
    assert "未识别到包装层" in page
    assert "packagingEvidence=segment.is_packaging_evidence===true" in page
    assert "visual_enhancement_sfx" in page
    assert "linear-gradient(90deg,#3f91c6" not in page
    assert ".recognition-segment.variable,.recognition-group-segment.variable{border-style:solid;background:var(--segment-bg)}" in page
    assert "aigc:['AI视频','#6b4bb4','#c7adff']" in page
    assert "const contentMediaLabel=segment=>" in page
    assert "素材形态：'+contentMediaLabel(segment)" in page
    assert "image_sequence:['连续图片','#2778a5','#82c9f2']" in page
    assert "label.title=laneTitle(lane)" in page
    assert "label.title=laneTitle(item.lane)" in page
    assert "button.setAttribute('aria-label',button.title)" in page
    assert ".recognition-segment,.recognition-group-segment{font-size:0}" in page
    assert "representative_groups" in page
    assert "每个包装分类仅展示一个主时间轴片段" in page
    assert ".recognition-group-list{max-height:none;overflow:visible;padding-right:0}" in page
    assert "category=categoryFor({category:group?.category})" in page
    assert "--group-bg:'+category.background+'26;--group-border:'+category.border" in page
    assert "border:1px solid var(--group-border,#294e70)" in page
    assert "color:var(--group-accent,#e8f6ff)" in page
    assert "representative_range_us||openedGroup.time_range_us" in page
    assert "const expandGroupTimelineMembers=sourceGroup=>" in page
    assert "filter(segment=>Number(segment.end_us||0)>start&&Number(segment.start_us||0)<end)" in page
    assert "非本分类上下文（灰色显示）" in page
    assert "recognition-group-lane.non-category" in page
    assert "groupCategoryField=groupCategory?.closest('div')" in page
    assert "layerMarker(valueFor(item.lane,segment)).className.startsWith('packaging-')" in page
    assert "button.dataset.groupRelevance=isCategoryMember?'category':'context'" in page
    assert "已校对 · 原候选标签已清除" in page
    assert "已校对：本组原始候选统计已清除。" in page
    assert "groupApplyButton?.addEventListener('click', () => queueMicrotask(clearCandidateTags))" in page
    assert "groupCategoryField.hidden=!isPackaging" in page
    assert "包装变量" not in page
    assert "director_category_pending" not in page
    assert "待绑定编导镜头类型" not in page
    assert "非包装层不需要包装分类。" in page
    assert "inspectorCategoryField.hidden=!isPackaging" in page
    assert "if(!isPackaging){inspectorCategory.value=''" in page
    assert "const nonPackagingLabel=correction.layer==='variable'?'变量层':'暂不确定'" in page
    assert "内容变量层（无包装分类）" in page
    assert "normaliseVariableGroupSegments" not in page
    assert "isPackaging=value.layer==='packaging'" in page
    assert "待确认（无包装分类）" in page
    assert 'id="recognitionGroupCategory"' in page
    assert 'id="recognitionCategory"' in page
    assert 'id="recognitionCategoryField"' in page
    assert "包装分类" in page
    assert "图片+3（连续三图以上）包装" in page
    assert "const packagingCategoryOptions=" in page
    assert "const packagingScopeForCategory=category=>" in page
    assert "key==='opening'?'opening':key==='ending'?'ending':key==='global'?'global':'current_range'" in page
    assert "groupScope.disabled=isPackaging" in page
    assert "scope.disabled=isPackaging" in page
    assert "groupCategory.addEventListener('change',syncGroupScope)" in page
    assert "inspectorCategory.addEventListener('change',syncInspectorScope)" in page
    assert "correction.scope=packagingScopeForCategory(correction.category)" in page
    assert "const refreshInspectorCategory=" in page
    assert "refreshInspectorCategory(corrected.category||segment.category)" in page
    assert "inspectorCategoryOptions().some" in page
    assert "if(correction.layer==='packaging'){correction.kind=packagingKindFor(groupSelected.lane,groupSelected.segment);const allowed=groupCategoryOptions().some" in page
    assert "renderSequenceEvidence" not in page
    assert "focusSequence" not in page
    assert console.PAGES["/packaging-recognition"] is console._packaging_recognition_page
