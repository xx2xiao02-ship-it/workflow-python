"""Daily_Update_Te 8364 的可审计、可注入编排入口。

编排器先执行真实的 ``create_draft -> directors_v2``，再按 8364 的分支顺序
执行已经显式注入的全文导演、TTS 循环、时间线、字幕和镜头精细化节点。外部
节点通过 ``node_runners`` 注入；缺少任一 runner 时返回结构化 ``blocked``，
不会用空数组、伪造 URL 或假任务 ID 越过阻断点。
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .capcut_mate_transport import CapCutMateClient, CapCutMateTransportError
from .capcut_node_adapters import CapCutNodeAdapter
from .capcut_stt_subtitle_pipeline import run_capcut_stt_subtitle_pipeline
from .volcengine_music_transport import VolcengineMusicHTTPTransport, VolcengineMusicTransportError
from .ark_tts_transport import ArkTTSTransportError, ArkTTSHTTPTransport
from .tts_audio_publisher import (
    TOSAudioPublisher,
    TTSAudioPublisherConfigError,
    TTSAudioPublisherError,
)
from .bgm_generation import run_bgm_generation
from .canvas import resolve_canvas
from .bgm_task_assembly import run_bgm_task_assembly
from .create_draft import run_create_draft
from .directors_v2 import run_directors_v2
from .directors_v2_plugin_transport import (
    DirectorsV2PluginHTTPTransport,
    DirectorsV2PluginTransportError,
)
from .ffmpeg_bgm_merge_transport import (
    FfmpegBgmMergeTransport,
    FfmpegBgmMergeTransportError,
)
from .governance.director_context import DirectorContext
from .governance import build_director_locked_manifest
from .host_shot_recognition import run_host_shot_recognition
from .host_task_assembly import run_host_task_assembly
from .merge_bgm_timeline import run_bgm_merge
from .scene_type_recognition import run_scene_type_recognition
from .shot_refinement import run_shot_refinement
from .shot_slot_planning import plan_stt_caption_shot_slots
from .speech_synthesis import normalize_response as normalize_speech_response
from .timeline_planning import run_timeline_planning
from .visual_intent import normalize_response as normalize_visual_intent_response
from .voice_director import normalize_voice_performance_plan, run_voice_director
from .voice_director_transport import Seed21ProVoiceDirectorTransport, VoiceDirectorTransportError
from .voice_catalog import VoiceCatalogError, apply_voice_resolution_to_tts_request, build_voice_candidates, resolve_voice_selection, run_voice_selector
from .voice_selector_transport import Seed21ProVoiceSelectorTransport, VoiceSelectorTransportError
from .layers.director import approve_cinematic_story, run_cinematic_director_lock
from .material_models import normalize_material_model_selection
from .story_writer import DirectorStoryDraft, normalize_cinematic_story


@dataclass
class WorkflowRunResult:
    status: str
    completed_nodes: list[str] = field(default_factory=list)
    blocked_nodes: list[dict[str, str]] = field(default_factory=list)
    failed_node: str = ""
    error: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "completed_nodes": list(self.completed_nodes),
            "blocked_nodes": list(self.blocked_nodes),
            "failed_node": self.failed_node,
            "error": self.error,
            "outputs": dict(self.outputs),
        }


def _resolve_start(params: Any) -> Mapping[str, Any]:
    value = params
    if isinstance(value, Mapping) and isinstance(value.get("params"), Mapping):
        value = value["params"]
    if isinstance(value, Mapping) and isinstance(value.get("_input"), Mapping):
        value = value["_input"]
    if not isinstance(value, Mapping):
        raise ValueError("Daily_Update_Te 起始输入必须是对象")
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Daily_Update_Te.text 必须是非空字符串")
    return value


def _blocked(*nodes: tuple[str, str]) -> list[dict[str, str]]:
    return [{"node_id": node_id, "reason": reason} for node_id, reason in nodes]


def _as_mapping(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} JSON 无效") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ValueError(f"{field} 必须是对象")


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是数组")
    return list(value)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _first_string(value: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _director_runtime_identity(
    start: Mapping[str, Any],
    *,
    draft_url: str,
    director_output: Mapping[str, Any],
    timeline_output: Mapping[str, Any],
    shot_output: Mapping[str, Any],
    speech_links: list[str],
    voice_resolution: Mapping[str, Any] | None = None,
    speech_outputs: list[Mapping[str, Any]] | None = None,
) -> tuple[str, str, str, str]:
    """为一次运行生成可追踪的 project/run/plan/TTS 身份。"""

    run_id = _first_string(start, ("run_id", "execution_id", "execute_id"))
    if run_id is None:
        run_id = f"run-{_sha256_json({'draft_url': draft_url, 'text': start['text']})[:16]}"
    project_id = _first_string(start, ("project_id", "project"))
    if project_id is None:
        project_id = f"daily-update-te-{_sha256_json(start['text'])[:16]}"
    plan_version = _first_string(start, ("plan_version",))
    if plan_version is None:
        plan_version = f"director-{_sha256_json({'director': director_output, 'timelines': timeline_output, 'shots': shot_output})[:16]}"
    tts_fingerprint = _sha256_json({
        "text": start["text"],
        "speech_links": speech_links,
        "voice": {
            key: (voice_resolution or {}).get(key, "")
            for key in ("voice_key", "speaker_id", "resource_id", "model")
        },
        "performance": start.get("speech_synthesis", start.get("tts", {})),
        "audio_observations": [
            {
                "duration_s": item.get("data", {}).get("duration") if isinstance(item.get("data"), Mapping) else None,
                "audio_sha256": item.get("audio_sha256", ""),
            }
            for item in (speech_outputs or [])
            if isinstance(item, Mapping)
        ],
        "timelines": timeline_output,
    })
    return project_id, run_id, plan_version, tts_fingerprint


def _run_info_node(
    runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    params: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    output = _as_mapping(runner(params), field)
    output["infos"] = _require_string(output.get("infos"), f"{field}.infos")
    return output


def _run_audio_write(
    runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    params: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    output = _as_mapping(runner(params), field)
    audio_ids = _require_list(output.get("audio_ids"), f"{field}.audio_ids")
    if not all(isinstance(item, str) for item in audio_ids):
        raise ValueError(f"{field}.audio_ids 必须是字符串数组")
    output["audio_ids"] = audio_ids
    output["draft_url"] = _require_string(output.get("draft_url"), f"{field}.draft_url")
    output["track_id"] = _require_string(output.get("track_id"), f"{field}.track_id")
    return output


def _run_caption_write(
    runner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    params: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    output = _as_mapping(runner(params), field)
    output["draft_url"] = _require_string(output.get("draft_url"), f"{field}.draft_url")
    output["track_id"] = _require_string(output.get("track_id"), f"{field}.track_id")
    for key in ("segment_ids", "text_ids"):
        values = output.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"{field}.{key} 必须是字符串数组")
        output[key] = list(values)
    return output


def _draft_from_review_payload(
    review: Mapping[str, Any],
    *,
    project_id: str,
    run_id: str,
    plan_version: str,
    source_text: str,
    segments: list[str],
    timelines: list[Mapping[str, Any]],
) -> DirectorStoryDraft:
    """把用户审核页回传的草案重新校验成不可伪造的同版草案。"""

    payload = review.get("draft")
    if not isinstance(payload, Mapping):
        raise ValueError("story_review.draft 必须是上一轮输出的故事草案对象")
    if str(payload.get("project_id", "")) != project_id or str(payload.get("run_id", "")) != run_id:
        raise ValueError("story_review.draft 与当前项目或运行不一致")
    if str(payload.get("plan_version", "")) != plan_version:
        raise ValueError("story_review.draft 与当前编导版本不一致")
    if payload.get("source_text") != source_text or payload.get("segments") != segments:
        raise ValueError("story_review.draft 与当前原文或分段不一致")
    if payload.get("timelines") != timelines:
        raise ValueError("story_review.draft 与当前 TTS 时间线不一致")
    story = normalize_cinematic_story(payload.get("story"), segments)
    return DirectorStoryDraft(
        project_id=project_id,
        run_id=run_id,
        plan_version=plan_version,
        source_text=source_text,
        segments=list(segments),
        timelines=[dict(item) for item in timelines],
        story=story,
        revision=int(payload.get("revision", 1)),
    )


def run_daily_update_te(
    params: Any,
    *,
    capcut_client: CapCutMateClient | Any | None = None,
    directors_transport: Callable[[Any], Mapping[str, Any]] | None = None,
    story_transport: Callable[[str, str], Any] | None = None,
    node_runners: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
) -> WorkflowRunResult:
    """执行已接入的工作流分支，并在未接入节点处可审计地停止。

    ``capcut_client`` 和 ``directors_transport`` 都可显式注入测试替身；不注入
    时才尝试从本机环境读取对应配置。``node_runners`` 的 key 使用 8364
    节点 ID，例如 ``"102833"``、``"159953"``、``"165901"``、
    ``"197742"``、``"116616"`` 和 ``"139488"``；这样外部 transport
    可以逐个接入而不改动编排主线。
    """

    try:
        start = _resolve_start(params)
        model_selection = normalize_material_model_selection({
            "image_model": start.get("image_model", "auto"),
            "video_model": start.get("video_model", "auto"),
        })
    except Exception as exc:
        return WorkflowRunResult(status="failed", failed_node="100001", error=str(exc))

    result = WorkflowRunResult(status="running")
    result.outputs["material_model_selection"] = model_selection
    runners = {str(key): value for key, value in (node_runners or {}).items()}

    if capcut_client is None:
        try:
            capcut_client = CapCutMateClient.from_env()
        except CapCutMateTransportError as exc:
            result.status = "blocked"
            result.blocked_nodes = _blocked(("182422", str(exc)))
            return result

    try:
        canvas = resolve_canvas(start.get("canvas", start.get("aspect_ratio", None)))
        draft_output = run_create_draft(
            {
                "height": canvas.height,
                "width": canvas.width,
            },
            transport=capcut_client.create_draft,
        )
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "182422"
        result.error = str(exc)
        return result

    result.completed_nodes.append("182422")
    result.outputs["182422"] = draft_output
    result.outputs["canvas"] = canvas.to_dict()
    try:
        current_draft_url = _require_string(draft_output.get("draft_url"), "182422.draft_url")
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "182422"
        result.error = str(exc)
        return result

    if directors_transport is None:
        try:
            directors_transport = DirectorsV2PluginHTTPTransport.from_env()
        except DirectorsV2PluginTransportError as exc:
            result.status = "blocked"
            result.blocked_nodes = _blocked(("129109", str(exc)))
            return result

    try:
        director_output = run_directors_v2(
            {"text": start["text"]},
            transport=directors_transport,
        )
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "129109"
        result.error = str(exc)
        return result

    result.completed_nodes.append("129109")
    result.outputs["129109"] = director_output

    try:
        context = DirectorContext.from_director_output(
            director_output,
            text=start["text"],
            aspect_ratio=canvas.aspect_ratio,
        )
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "129109"
        result.error = str(exc)
        return result

    # 8364 在这里分叉到全文视觉意图导演和语音合成循环；两者都必须有真实
    # transport 后才能生成时间线。不能拿已有 synthetic fixture 越过分支。
    missing = []
    tts_transport: ArkTTSHTTPTransport | None = None
    if "102833" not in runners:
        missing.append(("102833", "全文视觉意图导演尚未注入 LLM transport"))
    if "159953" not in runners:
        try:
            tts_publisher = TOSAudioPublisher.from_env()
            tts_transport = ArkTTSHTTPTransport.from_env(
                audio_publisher=tts_publisher,
            )
            runners["159953"] = tts_transport.synthesize
        except (
            ArkTTSTransportError,
            TTSAudioPublisherConfigError,
            TTSAudioPublisherError,
        ) as exc:
            missing.append(("159953", str(exc)))
    if missing:
        result.status = "blocked"
        result.blocked_nodes = _blocked(*missing)
        return result

    try:
        visual_output = normalize_visual_intent_response(
            _as_mapping(
                runners["102833"](context.visual_intent_input()),
                "102833 输出",
            ),
            segment_count=len(context.narrative.segments),
        )
        visual_items = visual_output["items"]
        music_cues = visual_output["music_cues"]
        reasoning_content = visual_output["reasoning_content"]
        result.completed_nodes.append("102833")
        result.outputs["102833"] = {
            "items": visual_items,
            "music_cues": music_cues,
            "reasoning_content": reasoning_content,
        }
        context = context.with_visual_intent(result.outputs["102833"])
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "102833"
        result.error = str(exc)
        return result

    speech_outputs: list[dict[str, Any]] = []
    speech_config = start.get("speech_synthesis", start.get("tts", {}))
    if speech_config is None:
        speech_config = {}
    if not isinstance(speech_config, Mapping):
        result.status = "failed"
        result.failed_node = "159953"
        result.error = "speech_synthesis/tts 配置必须是对象"
        return result
    voice_plan = None
    voice_resolution = None
    voice_director_config = speech_config.get("voice_director", {})
    if voice_director_config is None:
        voice_director_config = {}
    if not isinstance(voice_director_config, Mapping):
        result.status = "failed"; result.failed_node = "voice_director"; result.error = "tts.voice_director 必须是对象"
        return result
    try:
        voice_profile = dict(voice_director_config.get("profile", {}))
        if speech_config.get("overall_speed_ratio") is not None:
            voice_profile["speed_ratio"] = speech_config["overall_speed_ratio"]
        voice_catalog = speech_config.get("voice_catalog")
        voice_selection_config = speech_config.get("voice_selection", {})
        if voice_selection_config is None:
            voice_selection_config = {}
        if not isinstance(voice_selection_config, Mapping):
            raise VoiceCatalogError("tts.voice_selection 必须是对象")
        model_selection = None
        selection_error = ""
        # 只有在没有用户精确指定音色时才调用模型；模型只看到压缩后的候选元数据。
        exact_voice = speech_config.get("speaker_id") or speech_config.get("voice_id") or voice_selection_config.get("voice_key") or speech_config.get("voice_key")
        if voice_selection_config.get("enabled", True) and not exact_voice:
            requested_gender = voice_selection_config.get("voice_gender", speech_config.get("voice_gender", "auto"))
            candidates = build_voice_candidates(
                voice_catalog, voice_gender=str(requested_gender),
                style_tags=voice_selection_config.get("style_tags", speech_config.get("voice_style", [])),
                language=str(voice_selection_config.get("language", speech_config.get("language", "zh-CN"))),
            )
            # 仅有一个合法候选时没有模型决策空间，直接使用目录默认，避免无意义付费调用。
            if len(candidates) > 1:
                selector_runner = runners.get("voice_selector")
                if selector_runner is None:
                    try:
                        selector_runner = Seed21ProVoiceSelectorTransport.from_env()
                    except VoiceSelectorTransportError as exc:
                        selection_error = str(exc)
                if selector_runner is not None:
                    try:
                        model_selection = run_voice_selector(context.narrative.segments, voice_selection_config, candidates, transport=selector_runner)
                    except Exception as exc:
                        selection_error = str(exc)
        voice_resolution = resolve_voice_selection(speech_config, catalog=voice_catalog, model_selection=model_selection, model_error=selection_error)
        voice_profile["voice_profile"] = voice_resolution["display_name"] + "：" + "、".join(voice_resolution["style_tags"])
        result.outputs["voice_resolution"] = voice_resolution
        if model_selection is not None:
            result.completed_nodes.append("voice_selector")
            result.outputs["voice_selector"] = model_selection
        if isinstance(speech_config.get("performance_plan"), Mapping):
            voice_plan = normalize_voice_performance_plan(speech_config["performance_plan"], context.narrative.segments, profile=voice_profile)
        elif voice_director_config.get("enabled"):
            director_runner = runners.get("voice_director")
            if director_runner is None:
                try:
                    director_runner = Seed21ProVoiceDirectorTransport.from_env()
                except VoiceDirectorTransportError as exc:
                    result.status = "blocked"; result.blocked_nodes = _blocked(("voice_director", str(exc)))
                    return result
            voice_plan = run_voice_director(context.narrative.segments, voice_profile, transport=director_runner)
        if voice_plan is not None:
            result.completed_nodes.append("voice_director"); result.outputs["voice_director"] = voice_plan
    except Exception as exc:
        result.status = "failed"; result.failed_node = "voice_director"; result.error = str(exc)
        return result
    try:
        for index, _segment in enumerate(context.narrative.segments):
            performance = voice_plan["segments"][index] if voice_plan is not None else None
            request = apply_voice_resolution_to_tts_request(
                context.speech_input(speech_config, index, performance=performance), voice_resolution
            )
            raw = normalize_speech_response(
                _as_mapping(runners["159953"](request), f"159953[{index}] 输出")
            )
            data = raw["data"]
            link = data.get("link")
            duration = data.get("duration")
            if not isinstance(link, str) or not link.strip():
                raise ValueError(f"159953[{index}].data.link 必须是非空字符串")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise ValueError(f"159953[{index}].data.duration 必须是数字秒")
            speech_outputs.append(raw)
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "159953"
        result.error = str(exc)
        return result

    result.completed_nodes.extend(["159953", "178142"])
    result.outputs["159953"] = speech_outputs
    result.outputs["178142"] = {
        "link_list": [
            _as_mapping(item["data"], "speech_synthesis.data")["link"]
            for item in speech_outputs
        ]
    }
    context = context.with_speech_links(result.outputs["178142"]["link_list"])

    try:
        if "105880" in runners:
            raise ValueError(
                "105880 旧字幕生成 runner 已废弃；当前唯一入口是 TTS -> CapCut STT -> Mini"
            )
        subtitle_output = run_capcut_stt_subtitle_pipeline(
            segment_text=list(context.narrative.segments),
            audio_sources=list(context.speech_links),
            stt_transport=runners.get("capcut_stt"),
            correction_transport=runners.get("subtitle_correction"),
        )
        if not isinstance(subtitle_output.get("captions"), list):
            raise ValueError("新 STT 字幕链路没有返回 captions，禁止进入编导节点")
        timelines = _require_list(subtitle_output.get("group_timelines"), "CapCut STT.group_timelines")
        total_timeline = _as_mapping(subtitle_output.get("total_timeline"), "CapCut STT.total_timeline")
        all_timelines = [total_timeline]
        if len(timelines) != len(context.narrative.segments):
            raise ValueError("CapCut STT.group_timelines 与 segments 数量不一致")
        subtitle_output["shot_slot_plan"] = plan_stt_caption_shot_slots(
            subtitle_output["captions"], timelines, min_seconds=2.5, max_seconds=5.0
        )
        # 保留 165901 输出键以维持后续节点字段契约，但其值已完全由 CapCut STT
        # 独立项目生成，不再调用旧音频时长节点。
        result.completed_nodes.extend(["165901", "105880"])
        result.outputs["165901"] = {
            "timelines": timelines,
            "all_timelines": all_timelines,
            "source": "capcut_stt_upload_duration_and_utterances",
        }
        result.outputs["105880"] = subtitle_output
        context = context.with_timing(
            link_list=list(context.speech_links),
            group_timelines=timelines,
            all_timelines=all_timelines,
            subtitle_output=subtitle_output,
        )
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "105880"
        result.error = str(exc)
        return result

    # BGM 分支必须先完成任务、生成和融合，之后才允许创建背景音乐 infos
    # 或写入草稿，避免融合器缺失时留下半成品草稿。
    try:
        bgm_assembly = run_bgm_task_assembly(context.bgm_input())
        result.completed_nodes.append("192311")
        result.outputs["192311"] = bgm_assembly
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "192311"
        result.error = str(exc)
        return result

    bgm_tasks = _require_list(bgm_assembly.get("bgm_tasks"), "192311.bgm_tasks")
    bgm_generation: dict[str, Any] = {
        "bgm_tasks": list(bgm_tasks),
        "bgm_timelines": _require_list(
            bgm_assembly.get("bgm_timelines"), "192311.bgm_timelines"
        ),
        "transition_schemes": _require_list(
            bgm_assembly.get("transition_schemes"), "192311.transition_schemes"
        ),
        "AudioUrl_list": [],
        "gen_bgm_outputs": [],
    }
    result.completed_nodes.append("141288")
    result.outputs["141288"] = {"AudioUrl_list": []}

    if bgm_tasks:
        if "186546" not in runners:
            try:
                bgm_transport = VolcengineMusicHTTPTransport.from_env()
                runners["186546"] = bgm_transport.generate_bgm
            except VolcengineMusicTransportError as exc:
                result.status = "blocked"
                result.blocked_nodes = _blocked(
                    ("186546", f"gen_bgm transport 未配置：{exc}")
                )
                return result
        try:
            bgm_generation = run_bgm_generation(
                context.bgm_input(),
                transport=runners["186546"],
            )
            result.completed_nodes.append("186546")
            result.outputs["186546"] = list(bgm_generation["gen_bgm_outputs"])
            result.outputs["141288"] = {
                "AudioUrl_list": list(bgm_generation["AudioUrl_list"]),
            }
            context = context.with_bgm_generation(bgm_generation)
        except Exception as exc:
            result.status = "failed"
            result.failed_node = "186546"
            result.error = str(exc)
            return result

        merge_runner = runners.get("165818")
        if merge_runner is None:
            try:
                merge_transport = FfmpegBgmMergeTransport.from_env()
                merge_runner = merge_transport
                runners["165818"] = merge_runner
            except FfmpegBgmMergeTransportError as exc:
                result.status = "blocked"
                result.blocked_nodes = _blocked(
                    ("165818", f"merge_bgm_timeline transport 未配置：{exc}")
                )
                return result
        try:
            bgm_merge = run_bgm_merge(
                {
                    "audio_urls": bgm_generation["AudioUrl_list"],
                    "timelines": bgm_generation["bgm_timelines"],
                    "transition_schemes": bgm_generation["transition_schemes"],
                },
                transport=merge_runner,
            )
            result.completed_nodes.append("165818")
            result.outputs["165818"] = bgm_merge
            context = context.with_bgm_merge(bgm_merge)
        except Exception as exc:
            result.status = "failed"
            result.failed_node = "165818"
            result.error = str(exc)
            return result

    capcut_adapter = (
        CapCutNodeAdapter(capcut_client)
        if callable(getattr(capcut_client, "call", None))
        else None
    )
    narration_info_runner = runners.get("191683")
    if narration_info_runner is None and capcut_adapter is not None:
        narration_info_runner = capcut_adapter.audio_infos
    caption_info_runner = runners.get("111882")
    if caption_info_runner is None and capcut_adapter is not None:
        caption_info_runner = capcut_adapter.caption_infos
    if narration_info_runner is None or caption_info_runner is None:
        missing_info_nodes = []
        if narration_info_runner is None:
            missing_info_nodes.append(("191683", "解说 audio_infos transport 尚未注入"))
        if caption_info_runner is None:
            missing_info_nodes.append(("111882", "字幕 caption_infos transport 尚未注入"))
        result.status = "blocked"
        result.blocked_nodes = _blocked(*missing_info_nodes)
        return result

    try:
        narration_info = _run_info_node(
            narration_info_runner,
            {
                "mp3_urls": list(context.speech_links),
                "timelines": list(timelines),
                # CapCut Mate currently serializes the Chinese audio-effect name
                # into malformed draft JSON. Keep narration unprocessed until the
                # upstream effect writer is independently validated.
                "audio_effect": None,
                "volume": 1.2,
            },
            "191683",
        )
        caption_info = _run_info_node(
            caption_info_runner,
            {
                "texts": list(context.timing.caption_segments if context.timing else ()),
                "timelines": list(context.timing.caption_timelines if context.timing else ()),
            },
            "111882",
        )
        result.completed_nodes.extend(["191683", "111882"])
        result.outputs["191683"] = narration_info
        result.outputs["111882"] = caption_info
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "191683"
        result.error = str(exc)
        return result

    bgm_info: dict[str, Any] | None = None
    if bgm_tasks:
        bgm_info_runner = runners.get("116592")
        if bgm_info_runner is None and capcut_adapter is not None:
            bgm_info_runner = capcut_adapter.audio_infos
        if bgm_info_runner is None:
            result.status = "blocked"
            result.blocked_nodes = _blocked(
                ("116592", "背景音乐 audio_infos transport 尚未注入")
            )
            return result
        try:
            bgm_info = _run_info_node(
                bgm_info_runner,
                {
                    "mp3_urls": list(bgm_merge["audio_url_list"]),
                    "timelines": list(all_timelines),
                    "volume": 0.4,
                },
                "116592",
            )
            result.completed_nodes.append("116592")
            result.outputs["116592"] = bgm_info
        except Exception as exc:
            result.status = "failed"
            result.failed_node = "116592"
            result.error = str(exc)
            return result

    narration_write_runner = runners.get("135313")
    if narration_write_runner is None and capcut_adapter is not None:
        narration_write_runner = capcut_adapter.add_audios
    caption_write_runner = runners.get("179989")
    if caption_write_runner is None and capcut_adapter is not None:
        caption_write_runner = capcut_adapter.add_captions
    bgm_write_runner = runners.get("110576")
    if bgm_write_runner is None and capcut_adapter is not None:
        bgm_write_runner = capcut_adapter.add_audios
    missing_write_nodes = []
    if narration_write_runner is None:
        missing_write_nodes.append(("135313", "解说* add_audios transport 尚未注入"))
    if caption_write_runner is None:
        missing_write_nodes.append(("179989", "字幕* add_captions transport 尚未注入"))
    if bgm_tasks and bgm_write_runner is None:
        missing_write_nodes.append(("110576", "背景音乐* add_audios transport 尚未注入"))
    if missing_write_nodes:
        result.status = "blocked"
        result.blocked_nodes = _blocked(*missing_write_nodes)
        return result

    try:
        narration_write = _run_audio_write(
            narration_write_runner,
            {"draft_url": current_draft_url, "audio_infos": narration_info["infos"]},
            "135313",
        )
        current_draft_url = narration_write["draft_url"]
        result.completed_nodes.append("135313")
        result.outputs["135313"] = narration_write

        if bgm_tasks and bgm_info is not None:
            bgm_write = _run_audio_write(
                bgm_write_runner,
                {"draft_url": current_draft_url, "audio_infos": bgm_info["infos"]},
                "110576",
            )
            current_draft_url = bgm_write["draft_url"]
            result.completed_nodes.append("110576")
            result.outputs["110576"] = bgm_write

        caption_write = _run_caption_write(
            caption_write_runner,
            {
                "draft_url": current_draft_url,
                "captions": caption_info["infos"],
                "font": "风雅宋",
                "font_size": 12,
                "has_shadow": True,
                "transform_y": -1000,
            },
            "179989",
        )
        current_draft_url = caption_write["draft_url"]
        result.completed_nodes.append("179989")
        result.outputs["179989"] = caption_write
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "135313"
        result.error = str(exc)
        return result

    if "197742" not in runners:
        result.status = "blocked"
        result.blocked_nodes = _blocked(
            ("197742", "镜头精细化内部 LLM transport 尚未注入"),
            ("103964", "镜头精细化批处理等待内部 197742 transport"),
        )
        return result

    try:
        def llm_runner(item: Any) -> Mapping[str, Any]:
            return runners["197742"]({
                "duration": item.duration,
                "items": dict(item.item),
                "segments": item.segment,
                "timelines": dict(item.timeline),
            })

        def code_runner(item: Any, llm_result: Mapping[str, Any]) -> Mapping[str, Any]:
            return run_timeline_planning({
                "duration": item.duration,
                "shots": llm_result.get("shots", []),
                "segments": [item.segment],
                "timelines": item.timeline,
            })

        subtitle_plan = result.outputs["105880"].get("shot_slot_plan")
        if isinstance(subtitle_plan, Mapping):
            shot_input = {
                "duration": [
                    (item["end"] - item["start"]) / 1_000_000
                    for item in timelines
                ],
                "items": [
                    {
                        **dict(visual),
                        "source_text": segment,
                        "visual_story": beat.get("segment_goal", ""),
                        "narrative_role": beat.get("rhythm", ""),
                        "required_shot_count": group["required_shot_count"],
                        "locked_shot_slots": group["slots"],
                        "pacing_policy": {
                            "source": "capcut_stt_captions",
                            "merge_below_seconds": 2.5,
                            "split_above_seconds": 5.0,
                        },
                    }
                    for segment, beat, visual, group in zip(
                        context.narrative.segments,
                        context.narrative.segment_beats,
                        context.narrative.visual_intent_items,
                        subtitle_plan["groups"],
                        strict=True,
                    )
                ],
                "segments": list(context.narrative.segments),
                "timelines": [dict(item) for item in timelines],
            }
        else:
            raise ValueError(
                "新字幕链路缺少 shot_slot_plan；旧 shot_refinement_input 兜底已废弃"
            )
        shot_output = run_shot_refinement(
            shot_input,
            llm_transport=llm_runner,
            code_transport=code_runner,
        )
        result.completed_nodes.extend(["197742", "127095", "103964"])
        result.outputs["103964"] = shot_output
        context = context.with_shots(shot_output)

        if context.timing is None or context.shots is None:
            raise ValueError("编导锁定需要 TimingLedger 和 ShotLedger")
        subtitle_segments = list(context.timing.caption_segments)
        subtitle_timelines = [dict(item) for item in context.timing.caption_timelines]
        if len(context.timing.all_timelines) != 1:
            raise ValueError("165901.all_timelines 必须只有一个总时间线")
        project_id, run_id, plan_version, tts_fingerprint = _director_runtime_identity(
            start,
            draft_url=current_draft_url,
            director_output=director_output,
            timeline_output={
                "timelines": timelines,
                "all_timelines": all_timelines,
            },
            shot_output=shot_output,
            speech_links=list(context.speech_links),
            voice_resolution=voice_resolution,
            speech_outputs=speech_outputs,
        )
        lock_artifacts = {
            "director_output": director_output,
            "tts_group_timelines": [dict(item) for item in context.timing.group_timelines],
            "total_timeline": dict(context.timing.all_timelines[0]),
            "shot_groups": [dict(item) for item in context.shots.code_list],
            "caption_segments": subtitle_segments,
            "caption_timelines": subtitle_timelines,
            "project_id": project_id,
            "run_id": run_id,
            "plan_version": plan_version,
            "tts_fingerprint": tts_fingerprint,
            "narration_assets": [
                {
                    "asset_id": f"g{index + 1:02d}.audio.narration",
                    "requirement_id": f"g{index + 1:02d}.audio.narration",
                    "group_id": f"g{index + 1:02d}",
                    "timeline": dict(timeline),
                    "remote_url": link,
                    "source_node": "159953",
                    "duration_us": int(float(speech_outputs[index]["data"]["duration"]) * 1_000_000),
                    "voice_key": (voice_resolution or {}).get("voice_key", ""),
                    "speaker_id": (voice_resolution or {}).get("speaker_id", ""),
                    "resource_id": (voice_resolution or {}).get("resource_id", ""),
                    "model": (voice_resolution or {}).get("model", ""),
                }
                for index, (link, timeline) in enumerate(
                    zip(context.speech_links, context.timing.group_timelines, strict=True)
                )
            ],
            "subtitle_pipeline": dict(subtitle_output["pipeline"]),
        }
        story_review = start.get("story_review")
        if isinstance(story_review, Mapping) and str(story_review.get("status", "")).lower() == "approved":
            draft = _draft_from_review_payload(
                story_review,
                project_id=project_id,
                run_id=run_id,
                plan_version=plan_version,
                source_text=start["text"],
                segments=list(context.narrative.segments),
                timelines=[dict(item) for item in context.timing.group_timelines],
            )
            director_manifest = approve_cinematic_story(draft, **lock_artifacts)
            result.outputs["story_review"] = {
                "status": "APPROVED",
                "revision": draft.revision,
            }
        else:
            feedback = story_review.get("feedback") if isinstance(story_review, Mapping) else None
            if story_transport is None:
                result.status = "blocked"
                result.outputs["story_review"] = {
                    "status": "PENDING_USER_REVIEW",
                    "reason": "编导故事草案尚未生成；需要注入 story_transport 后重试",
                }
                result.blocked_nodes = _blocked(
                    ("STORY_REVIEW", "编导层必须先输出连续故事草案，等待用户审核")
                )
                return result
            draft = run_cinematic_director_lock(
                **lock_artifacts,
                text=start["text"],
                story_transport=story_transport,
                review_feedback=feedback if isinstance(feedback, str) else None,
                revision=int(story_review.get("revision", 1)) if isinstance(story_review, Mapping) else 1,
            )
            result.outputs["story_draft"] = draft.to_dict()
            result.status = "blocked"
            result.blocked_nodes = _blocked(
                ("STORY_REVIEW", "故事草案已生成，用户同意后把 story_draft 原样回传并标记 approved")
            )
            return result
        context = context.with_director_lock(director_manifest)
        result.outputs["director_lock"] = director_manifest.to_dict()
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "DIRECTOR_LOCK"
        result.error = str(exc)
        return result

    if "116616" not in runners:
        result.status = "blocked"
        result.blocked_nodes = _blocked(
            ("116616", "Shot_Visual_Arrangement 的全局规划和首帧资料 transport 尚未注入")
        )
        return result

    try:
        raw_shot_visual = _as_mapping(
            runners["116616"](
                context.shot_visual_arrangement_input(
                    ref_images=list(start.get("ref_images") or [])
                )
            ),
            "116616 输出",
        )
        shot_visual_output = {
            "prompt": _require_list(raw_shot_visual.get("prompt"), "116616.prompt"),
            "ref_image": _require_list(
                raw_shot_visual.get("ref_image"), "116616.ref_image"
            ),
            "motion_seed": _require_list(
                raw_shot_visual.get("motion_seed"), "116616.motion_seed"
            ),
            "timelines": _require_list(
                raw_shot_visual.get("timelines"), "116616.timelines"
            ),
            "int_duration": _require_list(
                raw_shot_visual.get("int_duration"), "116616.int_duration"
            ),
            "error": raw_shot_visual.get("error"),
            "debug": raw_shot_visual.get("debug"),
        }
        if "story_context" in raw_shot_visual:
            story_context = _require_list(raw_shot_visual.get("story_context"), "116616.story_context")
            if len(story_context) != len(raw_shot_visual.get("prompt", [])) or not all(isinstance(item, Mapping) for item in story_context):
                raise ValueError("116616.story_context 必须与镜头提示词一一对应")
            shot_visual_output["story_context"] = [dict(item) for item in story_context]
        if not isinstance(shot_visual_output["error"], str):
            raise ValueError("116616.error 必须是字符串")
        if not isinstance(shot_visual_output["debug"], Mapping):
            raise ValueError("116616.debug 必须是对象")
        lengths = {
            len(shot_visual_output[field])
            for field in ("prompt", "ref_image", "motion_seed", "timelines", "int_duration")
        }
        if len(lengths) != 1:
            raise ValueError("116616 五个镜头数组数量必须一致")
        if shot_visual_output["error"]:
            raise ValueError(f"116616 返回错误：{shot_visual_output['error']}")
        shot_visual_output["debug"] = dict(shot_visual_output["debug"])
        result.completed_nodes.append("116616")
        result.outputs["116616"] = shot_visual_output
        context = context.with_visual_arrangement(shot_visual_output)
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "116616"
        result.error = str(exc)
        return result

    try:
        scene_output = run_scene_type_recognition(context.scene_type_input())
        result.completed_nodes.append("156199")
        result.outputs["156199"] = scene_output
        context = context.with_scene_route(scene_output)
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "156199"
        result.error = str(exc)
        return result

    if "139488" not in runners:
        result.status = "blocked"
        result.blocked_nodes = _blocked(
            ("139488", "host 镜头识别 LLM transport 尚未注入")
        )
        return result

    try:
        host_output = run_host_shot_recognition(
            {"host_llm_input": scene_output["host_llm_input"]},
            transport=lambda request: runners["139488"]({
                "host_llm_input": dict(request.host_llm_input)
            }),
        )
        result.completed_nodes.append("139488")
        result.outputs["139488"] = host_output
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "139488"
        result.error = str(exc)
        return result

    try:
        host_task_output = run_host_task_assembly(
            context.host_task_input(host_output["host_idxs"])
        )
        result.completed_nodes.append("117861")
        result.outputs["117861"] = host_task_output
        context = context.with_host_route(host_output["host_idxs"], host_task_output)
    except Exception as exc:
        result.status = "failed"
        result.failed_node = "117861"
        result.error = str(exc)
        return result

    result.status = "blocked"
    result.blocked_nodes = _blocked(
        ("175652", "host_audio_split 真实插件 transport 尚未接入")
    )
    return result


__all__ = ["WorkflowRunResult", "run_daily_update_te"]
