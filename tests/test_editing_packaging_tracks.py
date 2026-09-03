from __future__ import annotations

import json
from types import SimpleNamespace

from workflow_1256.editing_layer import (
    _apply_packaging_application,
    _apply_primary_image_zoom_keyframes,
    _enum_from_template_item,
    _inject_packaging_audio_effects,
    _inject_packaging_video_effects,
    _inject_packaging_text_templates,
    _resolve_packaging_targets,
)


def test_primary_images_get_full_duration_uniform_zoom_but_reference_and_covered_images_do_not() -> None:
    class _KeyframeList:
        def __init__(self, property_value) -> None:
            self.keyframe_property = property_value
            self.points = []

    class _Segment:
        def __init__(self, start: int, duration: int) -> None:
            self.target_timerange = _FakeTimerange(start, duration)
            self.common_keyframes = []

        def add_keyframe(self, property_value, offset: int, value: float) -> None:
            keyframe_list = next(
                (item for item in self.common_keyframes if item.keyframe_property == property_value),
                None,
            )
            if keyframe_list is None:
                keyframe_list = _KeyframeList(property_value)
                self.common_keyframes.append(keyframe_list)
            keyframe_list.points.append((offset, value))

    class _Properties:
        scale_x = "KFTypeScaleX"
        scale_y = "KFTypeScaleY"
        uniform_scale = "UNIFORM_SCALE"

    covered_image = _Segment(0, 5_000_000)
    top_video = _Segment(0, 5_000_000)
    reference_image = _Segment(5_000_000, 5_000_000)
    primary_image = _Segment(10_000_000, 3_000_000)
    # 模拟包装模板已经写过的缩放列表，自动规则应当替换而不是叠加。
    covered_image.common_keyframes.append(_KeyframeList(_Properties.scale_x))

    result = _apply_primary_image_zoom_keyframes(
        [
            ({"asset_type": "image"}, covered_image),
            ({"asset_type": "video"}, top_video),
            ({"asset_type": "image"}, reference_image),
            ({"asset_type": "image"}, primary_image),
        ],
        segment_track_names={
            id(covered_image): "AIGC动画",
            id(top_video): "AIGC动画",
            id(reference_image): "首帧参考",
            id(primary_image): "AIGC动画",
        },
        keyframe_property=_Properties,
    )

    assert result == {"candidate_count": 1, "applied_count": 1, "keyframe_count": 2}
    assert len(covered_image.common_keyframes) == 1
    assert covered_image.common_keyframes[0].keyframe_property == "KFTypeScaleX"
    assert top_video.common_keyframes == []
    assert reference_image.common_keyframes == []
    assert primary_image.common_keyframes[0].keyframe_property == "UNIFORM_SCALE"
    assert primary_image.common_keyframes[0].points == [(0, 1.0), (3_000_000, 1.2)]


class _FakeTimerange:
    def __init__(self, start: int, duration: int) -> None:
        self.start = start
        self.duration = duration
        self.end = start + duration


class _FakeVisualSegment:
    def __init__(self, start: int, duration: int) -> None:
        self.target_timerange = _FakeTimerange(start, duration)
        self.transitions: list[tuple[object, int]] = []

    def add_transition(self, transition: object, *, duration: int) -> None:
        self.transitions.append((transition, duration))


def test_boundary_transitions_do_not_fall_back_to_first_visual_segment(tmp_path) -> None:
    def write_template(category: str, transition_id: str) -> str:
        path = tmp_path / f"{category}.json"
        path.write_text(
            json.dumps({
                "duration": 10_000_000,
                "materials": {"transitions": [{"id": transition_id, "name": category}]},
                "tracks": [{
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}],
                }],
            }),
            encoding="utf-8",
        )
        return str(path)

    intro = _FakeVisualSegment(0, 3_000_000)
    middle = _FakeVisualSegment(3_000_000, 4_000_000)
    outro = _FakeVisualSegment(7_000_000, 3_000_000)
    opening_transition = object()
    ending_transition = object()
    ordinary_transition = object()
    transition_enum = [
        type("Transition", (), {"value": type("Meta", (), {"name": "image", "resource_id": "ordinary"})()})(),
        type("Transition", (), {"value": type("Meta", (), {"name": "opening", "resource_id": "opening-id"})()})(),
        type("Transition", (), {"value": type("Meta", (), {"name": "ending", "resource_id": "ending-id"})()})(),
    ]
    transition_enum[0].marker = ordinary_transition
    transition_enum[1].marker = opening_transition
    transition_enum[2].marker = ending_transition

    class _FakeScript:
        tracks = {}

    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "opening_title": "看似解决了，代价谁来付？",
            "categories": {
                "image": {"draft_content_path": write_template("image", "ordinary")},
                "opening": {"draft_content_path": write_template("opening", "opening-id")},
                "ending": {"draft_content_path": write_template("ending", "ending-id")},
            },
            "shot_categories": {"intro": "image", "middle": "image", "outro": "image"},
            "first_shot_id": "intro",
            "last_shot_id": "outro",
        },
        segments_by_shot={"intro": [intro], "middle": [middle], "outro": [outro]},
        ordered_visual_segments=[intro, middle, outro],
        video_scene_effect_type=[],
        transition_type=transition_enum,
        keyframe_property=[],
        script=_FakeScript(),
        timerange_type=_FakeTimerange,
    )

    assert report["applied_transitions"] == 2
    assert report["opening_title"] == "看似解决了，代价谁来付？"
    assert len(intro.transitions) == 1
    assert len(middle.transitions) == 0
    assert len(outro.transitions) == 1
    assert intro.transitions[0][0] is transition_enum[1]
    assert outro.transitions[0][0] is transition_enum[2]


def test_transition_prefers_primary_visual_track_over_first_frame_reference(tmp_path) -> None:
    template_path = tmp_path / "image.json"
    template_path.write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": [{"id": "ordinary", "name": "叠化", "duration": 500_000}]},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }),
        encoding="utf-8",
    )
    first_frame = _FakeVisualSegment(0, 3_000_000)
    primary = _FakeVisualSegment(0, 3_000_000)
    next_primary = _FakeVisualSegment(3_000_000, 4_000_000)
    transition_enum = [
        type("Transition", (), {"value": type("Meta", (), {"name": "叠化", "resource_id": "ordinary"})()})(),
    ]

    class _FakeScript:
        tracks = {}

    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "categories": {"image": {"draft_content_path": str(template_path)}},
            "shot_categories": {"s1": "image", "s2": "image"},
            "transition_points": [{
                "transition_index": 0,
                "at_us": 3_000_000,
                "from_shot_id": "s1",
                "to_shot_id": "s2",
            }],
        },
        segments_by_shot={"s1": [first_frame, primary], "s2": [next_primary]},
        ordered_visual_segments=[first_frame, primary, next_primary],
        segment_track_names={
            id(first_frame): "首帧参考",
            id(primary): "AIGC动画",
            id(next_primary): "AIGC动画",
        },
        video_scene_effect_type=[],
        transition_type=transition_enum,
        keyframe_property=[],
        script=_FakeScript(),
        timerange_type=_FakeTimerange,
    )

    assert len(first_frame.transitions) == 0
    assert len(primary.transitions) == 1
    assert report["transition_points"][0]["target_track_name"] == "AIGC动画"


def test_normal_transitions_follow_upstream_transition_points(tmp_path) -> None:
    template_path = tmp_path / "image.json"
    template_path.write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": [{"id": "ordinary", "name": "叠化", "duration": 500_000}]},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }),
        encoding="utf-8",
    )
    first = _FakeVisualSegment(0, 3_000_000)
    second = _FakeVisualSegment(3_000_000, 4_000_000)
    third = _FakeVisualSegment(7_000_000, 3_000_000)
    transition_enum = [
        type("Transition", (), {"value": type("Meta", (), {"name": "叠化", "resource_id": "ordinary"})()})(),
    ]

    class _FakeScript:
        tracks = {}

    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "categories": {"image": {"draft_content_path": str(template_path)}},
            "shot_categories": {"s1": "image", "s2": "image", "s3": "image"},
            "first_shot_id": "s1",
            "last_shot_id": "s3",
            "transition_timeline_source": "upstream.transition_points",
            "transition_points": [
                {"transition_index": 0, "at_us": 3_000_000, "from_shot_id": "s1", "to_shot_id": "s2"},
                {"transition_index": 1, "at_us": 7_000_000, "from_shot_id": "s2", "to_shot_id": "s3"},
            ],
        },
        segments_by_shot={"s1": [first], "s2": [second], "s3": [third]},
        ordered_visual_segments=[first, second, third],
        video_scene_effect_type=[],
        transition_type=transition_enum,
        keyframe_property=[],
        script=_FakeScript(),
        timerange_type=_FakeTimerange,
    )

    assert report["transition_source"] == "upstream.transition_points"
    assert report["applied_transitions"] == 2
    assert len(first.transitions) == 1
    assert len(second.transitions) == 1
    assert len(third.transitions) == 0
    assert [item["at_us"] for item in report["transition_points"]] == [3_000_000, 7_000_000]
    assert [item["from_shot_id"] for item in report["transition_points"]] == ["s1", "s2"]
    assert [item["boundary_status"] for item in report["transition_points"]] == ["exact", "exact"]


def test_transition_uses_from_shot_category_and_only_level_two_gets_audio_companion(tmp_path) -> None:
    class _FakeAudioMaterial:
        duration = 2_000_000

        def __init__(self, path: str) -> None:
            self.path = path

    class _FakeAudioSegment:
        def __init__(self, material, timerange, *, source_timerange, volume) -> None:
            self.material = material
            self.target_timerange = timerange
            self.source_timerange = source_timerange
            self.volume = volume
            self.fades: list[tuple[int, int]] = []

        def add_fade(self, fade_in: int, fade_out: int) -> None:
            self.fades.append((fade_in, fade_out))

    class _FakeTrack:
        def __init__(self) -> None:
            self.segments: list[object] = []

    class _FakeScript:
        def __init__(self) -> None:
            self.tracks: dict[str, _FakeTrack] = {}

        def add_track(self, _track_type, *, track_name: str) -> None:
            self.tracks[track_name] = _FakeTrack()

        def add_segment(self, segment, *, track_name: str) -> None:
            self.tracks.setdefault(track_name, _FakeTrack()).segments.append(segment)

    def write_template(category: str, transition_id: str, audio_name: str) -> str:
        root = tmp_path / category / "raw_snapshot"
        audio_root = root / "assets" / "external_audio"
        audio_root.mkdir(parents=True, exist_ok=True)
        (audio_root / f"{category}.mp3").write_bytes(b"audio")
        path = root / "draft_content.json"
        path.write_text(
            json.dumps(
                {
                    "duration": 10_000_000,
                    "materials": {
                        "transitions": [{
                            "id": transition_id,
                            "resource_id": transition_id,
                            "name": f"{category}转场",
                            "duration": 500_000,
                        }],
                        "audios": [{
                            "id": f"{category}-audio",
                            "name": audio_name,
                            "path": f"{category}.mp3",
                        }],
                        "audio_fades": [],
                    },
                    "tracks": [
                        {"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]},
                        {
                            "type": "audio",
                            "name": "",
                            "segments": [{
                                "material_id": f"{category}-audio",
                                "target_timerange": {"start": 9_000_000, "duration": 1_000_000},
                            }],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return str(path)

    first = _FakeVisualSegment(0, 3_000_000)
    second = _FakeVisualSegment(3_000_000, 4_000_000)
    third = _FakeVisualSegment(7_000_000, 3_000_000)
    script = _FakeScript()
    transition_type = [
        type("Transition", (), {"value": type("Meta", (), {"name": "图片转场", "resource_id": "image-transition"})()})(),
        type("Transition", (), {"value": type("Meta", (), {"name": "AIGC转场", "resource_id": "aigc-transition"})()})(),
    ]
    image_template = write_template("image", "image-transition", "图片转场音效")
    aigc_template = write_template("aigc", "aigc-transition", "AIGC转场音效")

    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "categories": {
                "image": {"draft_content_path": image_template},
                "aigc": {"draft_content_path": aigc_template},
            },
            "shot_categories": {"s1": "image", "s2": "aigc", "s3": "aigc"},
            "transition_points": [
                {
                    "transition_index": 0,
                    "at_us": 3_000_000,
                    "from_shot_id": "s1",
                    "to_shot_id": "s2",
                    "from_group_id": "g01",
                    "to_group_id": "g02",
                    "classification_level": "level_2",
                    "default_enabled": True,
                },
                {
                    "transition_index": 1,
                    "at_us": 7_000_000,
                    "from_shot_id": "s2",
                    "to_shot_id": "s3",
                    "from_group_id": "g02",
                    "to_group_id": "g02",
                    "classification_level": "level_3",
                    "default_enabled": False,
                },
            ],
            "transition_timeline_source": "upstream.transition_points",
        },
        segments_by_shot={"s1": [first], "s2": [second], "s3": [third]},
        ordered_visual_segments=[first, second, third],
        video_scene_effect_type=[],
        transition_type=transition_type,
        keyframe_property=[],
        script=script,
        audio_track_type=object(),
        audio_material_type=_FakeAudioMaterial,
        audio_segment_type=_FakeAudioSegment,
        timerange_type=_FakeTimerange,
        draft_dir=tmp_path / "target",
    )

    assert report["applied_transitions"] == 1
    assert report["transition_points"][0]["category"] == "image"
    assert len(first.transitions) == 1
    assert not second.transitions
    assert not third.transitions
    assert len(report["transition_audio_companions"]) == 1
    assert report["transition_audio_companions"][0]["category"] == "image"
    assert report["transition_audio_companions"][0]["transition_index"] == 0
    assert len(script.tracks["包装-图片音频-图片转场音效"].segments) == 1


def test_transition_point_tolerance_keeps_shot_pair_authoritative(tmp_path) -> None:
    template_path = tmp_path / "image.json"
    template_path.write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": [{"id": "ordinary", "name": "叠化", "duration": 500_000}]},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }),
        encoding="utf-8",
    )
    first = _FakeVisualSegment(0, 3_000_000)
    second = _FakeVisualSegment(3_000_000, 4_000_000)
    transition_enum = [
        type("Transition", (), {"value": type("Meta", (), {"name": "叠化", "resource_id": "ordinary"})()})(),
    ]

    class _FakeScript:
        tracks = {}

    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "categories": {"image": {"draft_content_path": str(template_path)}},
            "shot_categories": {"s1": "image", "s2": "image"},
            "first_shot_id": "s1",
            "last_shot_id": "s2",
            "transition_tolerance_us": 50_000,
            "transition_points": [{
                "transition_index": 0,
                "at_us": 3_100_000,
                "from_shot_id": "s1",
                "to_shot_id": "s2",
            }],
        },
        segments_by_shot={"s1": [first], "s2": [second]},
        ordered_visual_segments=[first, second],
        video_scene_effect_type=[],
        transition_type=transition_enum,
        keyframe_property=[],
        script=_FakeScript(),
        timerange_type=_FakeTimerange,
    )

    assert report["applied_transitions"] == 1
    assert report["transition_points"][0]["boundary_status"] == "shot_pair_authoritative"
    assert report["transition_points"][0]["boundary_delta_us"] == 100_000
    assert len(report["transition_warnings"]) == 1


def test_level_three_transition_is_disabled_by_default_but_can_be_explicitly_enabled(tmp_path) -> None:
    template_path = tmp_path / "image.json"
    template_path.write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": [{"id": "ordinary", "name": "叠化", "duration": 500_000}]},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }),
        encoding="utf-8",
    )
    transition_enum = [
        type("Transition", (), {"value": type("Meta", (), {"name": "叠化", "resource_id": "ordinary"})()})(),
    ]

    class _FakeScript:
        tracks = {}

    def apply(point):
        first = _FakeVisualSegment(0, 3_000_000)
        second = _FakeVisualSegment(3_000_000, 4_000_000)
        report = _apply_packaging_application(
            {
                "bundle_name": "测试",
                "version": "1.0",
                "categories": {"image": {"draft_content_path": str(template_path)}},
                "shot_categories": {"s1": "image", "s2": "image"},
                "transition_points": [point],
            },
            segments_by_shot={"s1": [first], "s2": [second]},
            ordered_visual_segments=[first, second],
            video_scene_effect_type=[],
            transition_type=transition_enum,
            keyframe_property=[],
            script=_FakeScript(),
            timerange_type=_FakeTimerange,
        )
        return report, first, second

    disabled, first, second = apply({
        "transition_index": 0,
        "at_us": 3_000_000,
        "from_shot_id": "s1",
        "to_shot_id": "s2",
        "from_group_id": "g01",
        "to_group_id": "g01",
    })
    assert disabled["applied_transitions"] == 0
    assert not first.transitions and not second.transitions
    assert "三级镜头分类接缝 0 默认无转场" in disabled["skipped"]

    enabled, first, second = apply({
        "transition_index": 0,
        "at_us": 3_000_000,
        "from_shot_id": "s1",
        "to_shot_id": "s2",
        "from_group_id": "g01",
        "to_group_id": "g01",
        "enabled": True,
    })
    assert enabled["applied_transitions"] == 1
    assert len(first.transitions) == 1


def test_opening_and_ending_templates_override_media_categories_only_at_boundaries() -> None:
    segments = {
        "intro": ["intro-segment"],
        "middle": ["middle-segment"],
        "outro": ["outro-segment"],
        "stray": ["stray-segment"],
    }
    application = {
        "categories": {"image": {}, "aigc": {}, "opening": {}, "ending": {}},
        "shot_categories": {
            "intro": "image",
            "middle": "aigc",
            "outro": "image",
            "stray": "opening",
        },
        "first_shot_id": "intro",
        "last_shot_id": "outro",
    }

    targets, overrides = _resolve_packaging_targets(application, segments_by_shot=segments)

    assert [(shot_id, category) for shot_id, category, _ in targets] == [
        ("middle", "aigc"),
        ("intro", "opening"),
        ("outro", "ending"),
    ]
    assert overrides == [
        {"shot_id": "intro", "from_category": "image", "to_category": "opening"},
        {"shot_id": "outro", "from_category": "image", "to_category": "ending"},
        {"shot_id": "stray", "from_category": "opening", "to_category": ""},
    ]


def test_template_effect_matching_prefers_resource_id_over_duplicate_name() -> None:
    first = SimpleNamespace(value=SimpleNamespace(name="纸质抽帧", resource_id="wrong-resource", effect_id="wrong-effect"))
    source = SimpleNamespace(value=SimpleNamespace(name="纸质抽帧", resource_id="7046650052286091812", effect_id="source-effect"))

    assert _enum_from_template_item(
        [first, source],
        {"name": "纸质抽帧", "resource_id": "7046650052286091812"},
    ) is source


def test_packaging_audio_effect_resource_uses_runtime_segment_id(tmp_path) -> None:
    template_path = tmp_path / "opening.json"
    template_path.write_text(
        json.dumps(
            {
                "materials": {
                    "audio_effects": [
                        {
                            "id": "source-audio-effect",
                            "name": "人声增强3",
                            "resource_id": "7425556785693463090",
                            "type": "audio_effect",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    content = {"materials": {"audio_effects": []}}

    added = _inject_packaging_audio_effects(
        content,
        [
            {
                "effect_id": "runtime-audio-effect",
                "name": "人声增强3",
                "template_path": str(template_path),
            }
        ],
    )

    assert added == 1
    assert content["materials"]["audio_effects"][0]["id"] == "runtime-audio-effect"


def test_packaging_video_effect_resource_replaces_same_name_runtime_fallback(tmp_path) -> None:
    template_path = tmp_path / "image.json"
    template_path.write_text(
        json.dumps(
            {
                "materials": {
                    "video_effects": [
                        {
                            "id": "source-video-effect",
                            "name": "纸质抽帧",
                            "effect_id": "1534320",
                            "resource_id": "7046650052286091812",
                            "adjust_params": [{"name": "effects_adjust_speed", "value": 0.33}],
                            "path": "C:/cache/effect/1534320/resource",
                            "type": "video_effect",
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    content = {
        "materials": {
            "video_effects": [
                {
                    "id": "runtime-video-effect",
                    "name": "纸质抽帧",
                    "effect_id": "83272772",
                    "resource_id": "7416333380603613737",
                }
            ]
        }
    }

    replaced = _inject_packaging_video_effects(
        content,
        [
            {
                "runtime_material_id": "runtime-video-effect",
                "template_material_id": "source-video-effect",
                "template_path": str(template_path),
            }
        ],
    )

    assert replaced == 1
    material = content["materials"]["video_effects"][0]
    assert material["id"] == "runtime-video-effect"
    assert material["effect_id"] == "1534320"
    assert material["resource_id"] == "7046650052286091812"
    assert material["path"].endswith("effect/1534320/resource")


def test_packaging_text_template_preserves_template_resources_and_replaces_title(tmp_path) -> None:
    template_path = tmp_path / "opening.json"
    template_path.write_text(
        json.dumps(
            {
                "materials": {
                    "text_templates": [
                        {
                            "id": "source-template",
                            "name": "片头标题模板",
                            "resource_id": "template-resource",
                            "text_info_resources": [
                                {"text_material_id": "source-text"}
                            ],
                        }
                    ],
                    "texts": [
                        {
                            "id": "source-text",
                            "content": json.dumps(
                                {"text": "原始文案", "styles": [{"range": [0, 8]}]},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    content = {
        "materials": {"text_templates": [], "texts": []},
        "tracks": [
            {
                "name": "包装-片头文字模板",
                "type": "text",
                "segments": [{"material_id": "placeholder", "extra_material_refs": []}],
            }
        ],
    }

    result = _inject_packaging_text_templates(
        content,
        [{"template_path": str(template_path)}],
        "新的片头标题",
    )

    assert result == {"templates": 1, "segments": 1}
    template = content["materials"]["text_templates"][0]
    text_material_id = template["text_info_resources"][0]["text_material_id"]
    text_material = next(item for item in content["materials"]["texts"] if item["id"] == text_material_id)
    assert json.loads(text_material["content"])["text"] == "新的片头标题"
    assert content["tracks"][0]["segments"][0]["template_id"] == template["id"]


def test_full_draft_audio_effects_are_applied_to_matching_audio_track(tmp_path) -> None:
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    (full_dir / "draft_content.json").write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {
                "audios": [{"id": "voice", "name": "voice.mp3", "path": "voice.mp3"}],
                "audio_effects": [{
                    "id": "source-effect",
                    "name": "人声增强3",
                    "resource_id": "audio-resource",
                    "type": "audio_effect",
                }],
                "transitions": [],
            },
            "tracks": [{
                "type": "audio",
                "name": "解说",
                "segments": [{
                    "material_id": "voice",
                    "target_timerange": {"start": 0, "duration": 10_000_000},
                    "extra_material_refs": ["source-effect"],
                }],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    category_path = tmp_path / "image.json"
    category_path.write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": []},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }),
        encoding="utf-8",
    )

    class _AudioEffectSegment:
        def __init__(self) -> None:
            self.target_timerange = _FakeTimerange(0, 10_000_000)
            self.extra_material_refs: list[str] = []
            self.effects: list[object] = []

        def add_effect(self, effect: object) -> None:
            self.effects.append(effect)
            self.extra_material_refs.append(str(getattr(effect, "effect_id", "")))

    class _Script:
        def __init__(self) -> None:
            self.tracks = {}
            self.materials = type("Materials", (), {"audio_effects": []})()

    audio_effect = type(
        "AudioEffect",
        (),
        {
            "effect_id": "runtime-audio-effect",
            "value": type("Meta", (), {"name": "人声增强3", "resource_id": "audio-resource"})(),
        },
    )()
    target_audio = _AudioEffectSegment()
    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "categories": {"image": {"draft_content_path": str(category_path)}},
            "shot_categories": {"s1": "image"},
            "full_draft_source": str(full_dir),
            "full_draft_style": {
                "source": {"path": str(full_dir), "duration_us": 10_000_000},
                "visual": {},
                "subtitles": {},
                "transitions": {"applied": []},
            },
        },
        segments_by_shot={"s1": [_FakeVisualSegment(0, 10_000_000)]},
        ordered_visual_segments=[_FakeVisualSegment(0, 10_000_000)],
        video_scene_effect_type=[],
        transition_type=[],
        keyframe_property=[],
        script=_Script(),
        audio_segments=[("解说", target_audio)],
        audio_effect_types=[[audio_effect]],
        timerange_type=_FakeTimerange,
    )

    assert len(target_audio.effects) == 1
    assert report["applied_audio_effects"][0]["name"] == "人声增强3"


def test_full_draft_fallback_keeps_all_transition_points_and_microsecond_duration(tmp_path) -> None:
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    (full_dir / "draft_content.json").write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": [{
                "id": "source-transition",
                "name": "漫画撕纸",
                "resource_id": "transition-resource",
                "duration": 1_000_000,
            }]},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    category_path = tmp_path / "image.json"
    category_path.write_text(
        json.dumps({
            "duration": 10_000_000,
            "materials": {"transitions": []},
            "tracks": [{"type": "video", "segments": [{"target_timerange": {"start": 0, "duration": 10_000_000}}]}],
        }),
        encoding="utf-8",
    )
    first = _FakeVisualSegment(0, 3_000_000)
    second = _FakeVisualSegment(3_000_000, 4_000_000)
    third = _FakeVisualSegment(7_000_000, 3_000_000)
    transition = type(
        "Transition",
        (),
        {"value": type("Meta", (), {"name": "漫画撕纸", "resource_id": "transition-resource"})()},
    )()
    report = _apply_packaging_application(
        {
            "bundle_name": "测试",
            "version": "1.0",
            "categories": {"image": {"draft_content_path": str(category_path)}},
            "shot_categories": {"s1": "image", "s2": "image", "s3": "image"},
            "full_draft_source": str(full_dir),
            "full_draft_style": {
                "source": {"path": str(full_dir), "duration_us": 10_000_000},
                "visual": {},
                "subtitles": {},
                "transitions": {"applied": [
                    {"transition_at_us": 3_000_000, "transition_duration_us": 1_000_000},
                    {"transition_at_us": 7_000_000, "transition_duration_us": 1_000_000},
                ]},
            },
        },
        segments_by_shot={"s1": [first], "s2": [second], "s3": [third]},
        ordered_visual_segments=[first, second, third],
        segment_track_names={id(first): "AIGC动画", id(second): "AIGC动画", id(third): "AIGC动画"},
        video_scene_effect_type=[],
        transition_type=[transition],
        keyframe_property=[],
        script=type("Script", (), {"tracks": {}})(),
        timerange_type=_FakeTimerange,
    )

    assert report["applied_transitions"] == 2
    assert [item[1] for item in first.transitions + second.transitions] == [1_000_000, 1_000_000]
