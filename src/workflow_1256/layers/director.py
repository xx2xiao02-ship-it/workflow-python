"""编导层适配器：把现有导演节点输出固化为 DirectorPlan。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .contracts import DirectorPlan, LayerContractError
from ..governance import DirectorLockedManifest, build_director_locked_manifest
from ..story_writer import (
    DirectorStoryDraft,
    STORY_REVIEW_APPROVED,
    STORY_REVIEW_PENDING,
    StoryTransport,
    run_cinematic_story_writer,
)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LayerContractError(f"{field} JSON 无效") from exc
    if not isinstance(value, Mapping):
        raise LayerContractError(f"{field} 必须是对象")
    return value


def build_director_plan(output: Any, *, text: str | None = None) -> DirectorPlan:
    """接收具体画面导演或兼容导演节点的输出，不调用模型。"""

    value = _mapping(output, "director_output")
    segments = value.get("segments")
    beats = value.get("segment_beats")
    plan = value.get("director_plan")
    if not isinstance(segments, list) or not all(isinstance(item, str) for item in segments):
        raise LayerContractError("director_output.segments 必须是字符串数组")
    if not isinstance(beats, list):
        raise LayerContractError("director_output.segment_beats 必须是数组")
    if not isinstance(plan, Mapping):
        raise LayerContractError("director_output.director_plan 必须是对象")
    source_text = text if text is not None else value.get("text")
    if not isinstance(source_text, str) or not source_text.strip():
        source_text = "\n".join(segments)
    return DirectorPlan(
        text=source_text,
        director_plan=dict(plan),
        segment_beats=[dict(item) for item in beats],
        segments=list(segments),
    )


def lock_director_output(**artifacts: Any) -> DirectorLockedManifest:
    """编导层运行时收口入口；原“具体画面导演”输出保持只读。"""

    if artifacts.get("cinematic_story") is not None and artifacts.get("story_review_status") != STORY_REVIEW_APPROVED:
        raise LayerContractError("故事尚未获得用户同意，不能直接锁定并交给素材层")
    return build_director_locked_manifest(**artifacts)


def run_cinematic_director_lock(
    *,
    story_transport: StoryTransport | None = None,
    text: str | None = None,
    review_feedback: str | None = None,
    revision: int = 1,
    **artifacts: Any,
) -> DirectorStoryDraft:
    """编导层正式收口入口。

    先使用已冻结的 TTS 大分段生成连续默片故事，再把逐段隐喻映射随
    ``DirectorLockedManifest`` 交给镜头与素材层。未注入模型 transport 时会
    明确阻断，不会把 synthetic 结果伪装成真实故事生成。
    """

    output = _mapping(artifacts.get("director_output"), "director_output")
    segments = output.get("segments")
    if not isinstance(segments, list) or not all(isinstance(item, str) for item in segments):
        raise LayerContractError("director_output.segments 必须是字符串数组")
    source_text = text if text is not None else output.get("text")
    if not isinstance(source_text, str) or not source_text.strip():
        source_text = "\n".join(segments)
    timelines = artifacts.get("tts_group_timelines")
    if not isinstance(timelines, list):
        raise LayerContractError("tts_group_timelines 必须是数组")
    story = run_cinematic_story_writer(
        source_text,
        segments,
        timelines=timelines,
        transport=story_transport,
        review_feedback=review_feedback,
    )
    if "cinematic_story" in artifacts:
        raise LayerContractError("正式编导收口不接受外部 cinematic_story 覆盖；请通过 story_transport 生成")
    return DirectorStoryDraft(
        project_id=str(artifacts.get("project_id", "")),
        run_id=str(artifacts.get("run_id", "")),
        plan_version=str(artifacts.get("plan_version", "")),
        source_text=source_text,
        segments=list(segments),
        timelines=[dict(item) for item in timelines],
        story=story,
        revision=revision,
        review_status=STORY_REVIEW_PENDING,
    )


def approve_cinematic_story(
    draft: DirectorStoryDraft,
    **artifacts: Any,
) -> DirectorLockedManifest:
    """用户同意故事后，才把同一版故事升级成跨层锁定清单。"""

    if not isinstance(draft, DirectorStoryDraft):
        raise LayerContractError("approve_cinematic_story 只接受 DirectorStoryDraft")
    if draft.review_status != STORY_REVIEW_PENDING:
        raise LayerContractError("只有待审核故事草案可以被同意")
    for key in ("project_id", "run_id", "plan_version"):
        if key in artifacts and str(artifacts[key]) != getattr(draft, key):
            raise LayerContractError(f"{key} 与故事草案不一致，禁止跨运行串接")
    if artifacts.get("cinematic_story") is not None:
        raise LayerContractError("审批入口不接受外部 cinematic_story 覆盖")
    locked_artifacts = dict(artifacts)
    locked_artifacts.update(
        {
            "cinematic_story": draft.story,
            "story_review_status": STORY_REVIEW_APPROVED,
            "story_revision": draft.revision,
        }
    )
    return lock_director_output(**locked_artifacts)


__all__ = [
    "approve_cinematic_story",
    "build_director_plan",
    "lock_director_output",
    "run_cinematic_director_lock",
]
