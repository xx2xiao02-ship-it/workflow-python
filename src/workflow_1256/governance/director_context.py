"""1256 编导层内部数据中心。

``DirectorContext`` 只存在于 Python 编排层内部，用四类账本保存已经规范化的
编导数据，再通过节点适配视图生成原 Coze 节点所需的字段。它不替换节点契约，
也不把最终模型提示词写入编导账本。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .contracts import DirectorLockedManifest, GovernanceContractError
from ..canvas import resolve_canvas
from ..speech_synthesis import DEFAULT_VOICE_ID


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GovernanceContractError(f"{field} 必须是对象")
    return dict(value)


def _mapping_tuple(value: Any, field: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise GovernanceContractError(f"{field} 必须是数组")
    return tuple(_mapping(item, f"{field}[{index}]") for index, item in enumerate(value))


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GovernanceContractError(f"{field} 必须是字符串数组")
    return tuple(value)


def _any_tuple(value: Any, field: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise GovernanceContractError(f"{field} 必须是数组")
    return tuple(value)


def _lists(values: tuple[Any, ...]) -> list[Any]:
    return list(values)


@dataclass(frozen=True)
class NarrativeLedger:
    """叙事与全局意图账本。"""

    text: str
    director_plan: dict[str, Any]
    segments: tuple[str, ...]
    segment_beats: tuple[dict[str, Any], ...]
    visual_intent_items: tuple[dict[str, Any], ...] = ()
    music_cues: tuple[dict[str, Any], ...] = ()
    reasoning_content: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise GovernanceContractError("NarrativeLedger.text 必须是非空字符串")
        if not isinstance(self.director_plan, dict):
            raise GovernanceContractError("NarrativeLedger.director_plan 必须是对象")
        if not self.segments or not all(isinstance(item, str) and item for item in self.segments):
            raise GovernanceContractError("NarrativeLedger.segments 必须是非空字符串数组")
        if len(self.segment_beats) != len(self.segments):
            raise GovernanceContractError("segment_beats 与 segments 数量必须一致")

    @classmethod
    def from_director_output(cls, output: Mapping[str, Any], *, text: str | None = None) -> "NarrativeLedger":
        segments = _string_tuple(output.get("segments"), "director_output.segments")
        source_text = text or output.get("text") or "\n".join(segments)
        return cls(
            text=source_text,
            director_plan=_mapping(output.get("director_plan"), "director_output.director_plan"),
            segments=segments,
            segment_beats=_mapping_tuple(output.get("segment_beats"), "director_output.segment_beats"),
        )

    def visual_intent_input(self) -> dict[str, Any]:
        return {
            "segments": _lists(self.segments),
            "director_plan": dict(self.director_plan),
        }


@dataclass(frozen=True)
class TimingLedger:
    """TTS、组时间线、字幕时间线账本。"""

    link_list: tuple[str, ...]
    group_timelines: tuple[dict[str, Any], ...]
    all_timelines: tuple[dict[str, Any], ...]
    caption_segments: tuple[str, ...] = ()
    caption_timelines: tuple[dict[str, Any], ...] = ()
    caption_durations: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if len(self.link_list) != len(self.group_timelines):
            raise GovernanceContractError("link_list 与 group_timelines 数量必须一致")
        if self.caption_segments and len(self.caption_segments) != len(self.caption_timelines):
            raise GovernanceContractError("caption_segments 与 caption_timelines 数量必须一致")
        if self.caption_durations and len(self.caption_durations) != len(self.caption_segments):
            raise GovernanceContractError("caption_durations 与字幕数组数量必须一致")

    def timeline_input(self) -> dict[str, Any]:
        return {"links": _lists(self.link_list)}

    def subtitle_input(self, segments: tuple[str, ...]) -> dict[str, Any]:
        if len(self.group_timelines) != len(segments):
            raise GovernanceContractError("字幕输入的 group_timelines 与 segments 数量不一致")
        return {
            "timelines": [dict(item) for item in self.group_timelines],
            "segment_text": _lists(segments),
            "all_timelines": [dict(item) for item in self.all_timelines],
        }

    def scene_timing_view(self) -> dict[str, Any]:
        return {
            "new_segments": _lists(self.caption_segments),
            "new_timelines": [dict(item) for item in self.caption_timelines],
            "timelines": [dict(item) for item in self.group_timelines],
            "link_list": _lists(self.link_list),
        }


@dataclass(frozen=True)
class ShotLedger:
    """小镜头规划和时间线账本。"""

    code_list: tuple[dict[str, Any], ...]
    llm_list: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.code_list:
            raise GovernanceContractError("ShotLedger.code_list 不能为空")

    def shot_visual_input(
        self,
        *,
        ref_images: list[str],
        segments: tuple[str, ...],
        story_context: Mapping[str, Any] | None = None,
        aspect_ratio: str = "9:16",
    ) -> dict[str, Any]:
        return {
            "Code_list": [dict(item) for item in self.code_list],
            "ref_image": list(ref_images),
            "segments": _lists(segments),
            "story_context": dict(story_context or {}),
            "aspect_ratio": aspect_ratio,
            "debug": False,
        }


@dataclass(frozen=True)
class VisualRouteLedger:
    """视觉规格与路线账本；只保存语义规格，不保存最终提示词。"""

    visual_spec: tuple[dict[str, Any], ...] = ()
    route_output: dict[str, Any] | None = None
    host_idxs: tuple[Any, ...] = ()
    host_task_output: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for item in self.visual_spec:
            if not isinstance(item, dict):
                raise GovernanceContractError("VisualRouteLedger.visual_spec 必须是对象数组")
            if any(key in item for key in ("prompt", "video_prompt", "image_prompt", "model", "provider")):
                raise GovernanceContractError("最终提示词或模型参数不得进入 VisualRouteLedger")

    @classmethod
    def from_arrangement(cls, output: Mapping[str, Any]) -> "VisualRouteLedger":
        allowed = ("ref_image", "motion_seed", "timelines", "int_duration", "error", "debug")
        projected = (
            {key: output[key] for key in allowed if key in output},
        )
        # 116616 的数组字段代表按镜头排列的并行列；这里保留为一条投影视图，避免复制 prompt。
        return cls(visual_spec=projected)

    def with_route(self, route_output: Mapping[str, Any]) -> "VisualRouteLedger":
        return replace(self, route_output=dict(route_output))

    def with_host(self, host_idxs: list[Any], host_task_output: Mapping[str, Any]) -> "VisualRouteLedger":
        return replace(self, host_idxs=tuple(host_idxs), host_task_output=dict(host_task_output))


@dataclass(frozen=True)
class DirectorContext:
    """编导层内部数据中心；节点适配器从这里生成原始字段视图。"""

    narrative: NarrativeLedger
    aspect_ratio: str = "9:16"
    speech_links: tuple[str, ...] = ()
    timing: TimingLedger | None = None
    shots: ShotLedger | None = None
    visual_route: VisualRouteLedger | None = None
    bgm_generation: dict[str, Any] | None = None
    bgm_merge: dict[str, Any] | None = None
    locked_manifest: DirectorLockedManifest | None = None

    @classmethod
    def from_director_output(
        cls,
        output: Mapping[str, Any],
        *,
        text: str | None = None,
        aspect_ratio: Any = None,
    ) -> "DirectorContext":
        canvas = resolve_canvas(aspect_ratio)
        return cls(
            narrative=NarrativeLedger.from_director_output(output, text=text),
            aspect_ratio=canvas.aspect_ratio,
        )

    def with_visual_intent(self, output: Mapping[str, Any]) -> "DirectorContext":
        visual_items = _mapping_tuple(output.get("items"), "102833.items")
        if len(visual_items) != len(self.narrative.segments):
            raise GovernanceContractError("102833.items 与 segments 数量必须一致")
        narrative = replace(
            self.narrative,
            visual_intent_items=visual_items,
            music_cues=_mapping_tuple(output.get("music_cues"), "102833.music_cues"),
            reasoning_content=str(output.get("reasoning_content", "")),
        )
        return replace(self, narrative=narrative)

    def with_timing(
        self,
        *,
        link_list: list[str],
        group_timelines: list[dict[str, Any]],
        all_timelines: list[dict[str, Any]],
        subtitle_output: Mapping[str, Any] | None = None,
    ) -> "DirectorContext":
        if len(link_list) != len(self.narrative.segments):
            raise GovernanceContractError("link_list 与 segments 数量必须一致")
        captions = subtitle_output or {}
        timing = TimingLedger(
            link_list=_string_tuple(link_list, "178142.link_list"),
            group_timelines=_mapping_tuple(group_timelines, "165901.timelines"),
            all_timelines=_mapping_tuple(all_timelines, "165901.all_timelines"),
            caption_segments=_string_tuple(captions.get("new_segments", []), "105880.new_segments"),
            caption_timelines=_mapping_tuple(captions.get("new_timelines", []), "105880.new_timelines"),
            caption_durations=_any_tuple(captions.get("duration", []), "105880.duration"),
        )
        return replace(self, speech_links=tuple(link_list), timing=timing)

    def with_speech_links(self, link_list: list[str]) -> "DirectorContext":
        links = _string_tuple(link_list, "178142.link_list")
        if len(links) != len(self.narrative.segments):
            raise GovernanceContractError("link_list 与 segments 数量必须一致")
        return replace(self, speech_links=links)

    def bgm_input(self) -> dict[str, Any]:
        if self.timing is None:
            raise GovernanceContractError("BGM 任务组装需要 TimingLedger")
        return {
            "timelines": [dict(item) for item in self.timing.group_timelines],
            "music_cues": [dict(item) for item in self.narrative.music_cues],
        }

    def with_bgm_generation(self, output: Mapping[str, Any]) -> "DirectorContext":
        return replace(self, bgm_generation=dict(output))

    def with_bgm_merge(self, output: Mapping[str, Any]) -> "DirectorContext":
        return replace(self, bgm_merge=dict(output))

    def with_shots(self, output: Mapping[str, Any]) -> "DirectorContext":
        code_list = _mapping_tuple(output.get("Code_list"), "103964.Code_list")
        llm_value = output.get("LLM_list", [])
        llm_list = _mapping_tuple(llm_value, "103964.LLM_list") if llm_value else ()
        return replace(self, shots=ShotLedger(code_list=code_list, llm_list=llm_list))

    def with_director_lock(self, manifest: DirectorLockedManifest) -> "DirectorContext":
        """把正式编导收口挂回数据中心，后续模块只能从该上下文继续。"""

        if manifest.status != "DIRECTOR_LOCKED":
            raise GovernanceContractError("DirectorContext 只接受 DIRECTOR_LOCKED manifest")
        return replace(self, locked_manifest=manifest)

    def require_director_lock(self, consumer: str) -> DirectorLockedManifest:
        """阻止素材/路线模块绕过正式编导收口。"""

        if self.locked_manifest is None:
            raise GovernanceContractError(f"{consumer} 必须先经过 DirectorLockedManifest 收口")
        return self.locked_manifest

    def with_visual_arrangement(self, output: Mapping[str, Any]) -> "DirectorContext":
        return replace(self, visual_route=VisualRouteLedger.from_arrangement(output))

    def with_scene_route(self, output: Mapping[str, Any]) -> "DirectorContext":
        current = self.visual_route or VisualRouteLedger()
        return replace(self, visual_route=current.with_route(output))

    def with_host_route(self, host_idxs: list[Any], output: Mapping[str, Any]) -> "DirectorContext":
        current = self.visual_route or VisualRouteLedger()
        return replace(self, visual_route=current.with_host(host_idxs, output))

    def visual_intent_input(self) -> dict[str, Any]:
        return self.narrative.visual_intent_input()

    def speech_input(self, speech_config: Mapping[str, Any], index: int, performance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if index < 0 or index >= len(self.narrative.segments):
            raise GovernanceContractError("speech_input index 超出 segments 范围")
        request = {
            "speed_ratio": 1.1,
            "voice_id": DEFAULT_VOICE_ID,
            **dict(speech_config),
        }
        overall_speed = speech_config.get("overall_speed_ratio")
        if overall_speed is not None:
            request["speed_ratio"] = overall_speed
        if performance is not None:
            allowed = {"emotion", "emotion_scale", "speed_ratio", "loudness_rate", "pitch", "tail_silence_ms", "dialect"}
            request.update({key: performance[key] for key in allowed if key in performance})
        request["text"] = self.narrative.segments[index]
        return request

    def timeline_input(self) -> dict[str, Any]:
        if self.timing is not None:
            return self.timing.timeline_input()
        if not self.speech_links:
            raise GovernanceContractError("TTS link_list 尚未建立")
        return {"links": _lists(self.speech_links)}

    def subtitle_input(self) -> dict[str, Any]:
        if self.timing is None:
            raise GovernanceContractError("TimingLedger 尚未建立")
        return self.timing.subtitle_input(self.narrative.segments)

    def shot_visual_arrangement_input(self, *, ref_images: list[str]) -> dict[str, Any]:
        self.require_director_lock("Shot_Visual_Arrangement")
        if self.shots is None:
            raise GovernanceContractError("Shot_Visual_Arrangement 需要 ShotLedger")
        story_context = {}
        if self.locked_manifest is not None and self.locked_manifest.cinematic_story:
            story_context = dict(self.locked_manifest.cinematic_story)
        return self.shots.shot_visual_input(
            ref_images=ref_images,
            segments=self.narrative.segments,
            story_context=story_context,
            aspect_ratio=self.aspect_ratio,
        )

    def scene_type_input(self) -> dict[str, Any]:
        self.require_director_lock("画面类型识别")
        if self.timing is None or self.shots is None:
            raise GovernanceContractError("画面类型识别需要 TimingLedger 和 ShotLedger")
        timing_view = self.timing.scene_timing_view()
        return {
            "director_plan": dict(self.narrative.director_plan),
            "Code_list": [dict(item) for item in self.shots.code_list],
            "new_segments": timing_view["new_segments"],
            "new_timelines": timing_view["new_timelines"],
            "link_list": timing_view["link_list"],
            "timelines": timing_view["timelines"],
            "aspect_ratio": self.aspect_ratio,
        }

    def host_task_input(self, host_idxs: list[Any]) -> dict[str, Any]:
        self.require_director_lock("Host 任务组装")
        if self.visual_route is None or self.visual_route.route_output is None:
            raise GovernanceContractError("Host 任务组装需要路线账本")
        host_map = self.visual_route.route_output.get("host_map")
        if not isinstance(host_map, Mapping):
            raise GovernanceContractError("画面类型识别输出缺少 host_map")
        return {"host_map": dict(host_map), "host_idxs": list(host_idxs)}

    def snapshot(self) -> dict[str, Any]:
        """输出轻量审计快照，不包含最终 prompt 和完整原始响应。"""
        return {
            "segments_count": len(self.narrative.segments),
            "group_timeline_count": len(self.timing.group_timelines) if self.timing else 0,
            "caption_count": len(self.timing.caption_segments) if self.timing else 0,
            "shot_group_count": len(self.shots.code_list) if self.shots else 0,
            "visual_intent_count": len(self.narrative.visual_intent_items),
            "visual_route_ready": self.visual_route is not None,
            "director_lock_status": self.locked_manifest.status if self.locked_manifest else "UNLOCKED",
            "contains_final_prompt": False,
        }


__all__ = [
    "DirectorContext",
    "NarrativeLedger",
    "TimingLedger",
    "ShotLedger",
    "VisualRouteLedger",
]
