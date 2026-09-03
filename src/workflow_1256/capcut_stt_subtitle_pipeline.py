"""用真实 TTS 音频生成字幕，并保持 1256 字幕输出契约。

字幕时间戳只来自剪映 STT；Doubao-Seed-2.0-mini 仅允许校正文本文字。
旧 ``subtitle_data`` 的按字符比例切分不参与当前生产链；历史实现只允许
从归档包召回，不能作为运行时兜底。
"""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .capcut_stt_transport import CapCutSTTError, CapCutSTTTransport
from .subtitle_correction import SubtitleCorrectionTransport


MAX_AUDIO_DOWNLOAD_BYTES = 200 * 1024 * 1024


def _int_us(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CapCutSTTError(f"{field} 必须是整数微秒")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CapCutSTTError(f"{field} 必须是整数微秒") from exc
    if result < 0:
        raise CapCutSTTError(f"{field} 不能为负数")
    return result


def _window(value: Any, field: str) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        raise CapCutSTTError(f"{field} 必须是对象")
    start = _int_us(value.get("start", value.get("start_us")), f"{field}.start")
    end = _int_us(value.get("end", value.get("end_us")), f"{field}.end")
    if end <= start:
        raise CapCutSTTError(f"{field} 必须满足 end > start")
    return start, end


def _download_audio(source: str, target: Path) -> Path:
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        raise CapCutSTTError(f"TTS 音频不是本地文件或 http(s) 链接：{source}")
    request = Request(source, headers={"User-Agent": "workflow-1256-capcut-stt/1.0"})
    size = 0
    try:
        with urlopen(request, timeout=120) as response, target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_AUDIO_DOWNLOAD_BYTES:
                    raise CapCutSTTError("TTS 音频下载超过 200 MB，拒绝送入 STT")
                handle.write(chunk)
    except CapCutSTTError:
        raise
    except Exception as exc:
        raise CapCutSTTError(f"TTS 音频下载失败：{type(exc).__name__}") from exc
    if size <= 0:
        raise CapCutSTTError("TTS 音频下载结果为空")
    return target


def _materialize_audio_sources(sources: Sequence[Any]) -> tuple[list[str], tempfile.TemporaryDirectory | None]:
    local_paths: list[str] = []
    temporary: tempfile.TemporaryDirectory | None = None
    for index, raw_source in enumerate(sources, start=1):
        source = str(raw_source or "").strip()
        if not source:
            raise CapCutSTTError(f"TTS 音频[{index}] 为空")
        path = Path(source).expanduser()
        if path.is_file():
            local_paths.append(str(path.resolve()))
            continue
        if urlparse(source).scheme not in {"http", "https"}:
            raise CapCutSTTError(f"TTS 音频文件不存在：{path}")
        if temporary is None:
            temporary = tempfile.TemporaryDirectory(prefix="workflow-1256-stt-")
        suffix = Path(urlparse(source).path).suffix or ".audio"
        target = Path(temporary.name) / f"group_{index:02d}{suffix}"
        local_paths.append(str(_download_audio(source, target)))
    return local_paths, temporary


def _bgm_duration(total_timeline: Mapping[str, Any]) -> int:
    start, end = _window(total_timeline, "capcut_stt.total_timeline")
    total_seconds = math.ceil((end - start) / 1_000_000)
    return max(30, min(120, total_seconds))


def _validate_caption_timeline(
    captions: Sequence[Mapping[str, Any]],
    *,
    group_timelines: Sequence[Mapping[str, int]],
    group_ids: Sequence[str] | None = None,
    total_timeline: Mapping[str, int],
    field: str,
) -> list[tuple[str, int, int]]:
    """校验 STT/校对字幕的范围、分组顺序和重叠。

    时间轴只能来自剪映 STT。校对模型只允许修改 ``text``，因此这份校验
    同时作为 STT 候选和校对结果的边界闸门：字幕可以有停顿空洞，但不能
    越过所属音频、跨组乱序或互相覆盖。
    """

    total_start, total_end = _window(total_timeline, f"{field}.total_timeline")
    if not group_timelines:
        raise CapCutSTTError(f"{field} 缺少分段时间线")
    normalized_group_ids = [
        str(item).strip() for item in (group_ids or [f"g{index + 1:02d}" for index in range(len(group_timelines))])
    ]
    if len(normalized_group_ids) != len(group_timelines) or any(not item for item in normalized_group_ids):
        raise CapCutSTTError(f"{field} 分段 group_id 不完整")
    if len(set(normalized_group_ids)) != len(normalized_group_ids):
        raise CapCutSTTError(f"{field} 分段 group_id 重复")
    group_windows = {
        group_id: _window(item, f"{field}.group_timelines[{index}]")
        for index, (group_id, item) in enumerate(zip(normalized_group_ids, group_timelines))
    }
    group_order = {group_id: index for index, group_id in enumerate(normalized_group_ids)}
    previous_group_index = -1
    previous_end_by_group: dict[str, int] = {}
    seen_groups: set[str] = set()
    seen_ids: set[str] = set()
    result: list[tuple[str, int, int]] = []
    for index, raw in enumerate(captions):
        if not isinstance(raw, Mapping):
            raise CapCutSTTError(f"{field}[{index}] 必须是对象")
        caption_id = str(raw.get("caption_id") or "").strip()
        group_id = str(raw.get("group_id") or "").strip()
        if not caption_id or not group_id:
            raise CapCutSTTError(f"{field}[{index}] 缺少 caption_id/group_id")
        if caption_id in seen_ids:
            raise CapCutSTTError(f"{field}[{index}] caption_id 重复：{caption_id}")
        seen_ids.add(caption_id)
        if group_id not in group_windows:
            raise CapCutSTTError(f"{field}[{index}] group_id 不在 STT 分段时间线内：{group_id}")
        group_index = group_order[group_id]
        start, end = _window(raw, f"{field}[{index}]")
        if start < total_start or end > total_end:
            raise CapCutSTTError(f"{field}[{index}] 超出 STT 总时间线范围")
        group_start, group_end = group_windows[group_id]
        if start < group_start or end > group_end:
            raise CapCutSTTError(f"{field}[{index}] 超出 {group_id} STT 分段时间线")
        if group_index < previous_group_index:
            raise CapCutSTTError(f"{field} 分组顺序错误")
        previous_group_index = group_index
        previous_end = previous_end_by_group.get(group_id)
        if previous_end is not None and start < previous_end:
            raise CapCutSTTError(f"{field}[{index}] 与上一条字幕重叠或时间顺序错误")
        previous_end_by_group[group_id] = end
        seen_groups.add(group_id)
        result.append((caption_id, start, end))
    if not result:
        raise CapCutSTTError(f"{field} 未返回有效字幕")
    missing_groups = [group_id for group_id in normalized_group_ids if group_id not in seen_groups]
    if missing_groups:
        raise CapCutSTTError(f"{field} 未覆盖 STT 分段：{', '.join(missing_groups)}")
    return result


def run_capcut_stt_subtitle_pipeline(
    *,
    segment_text: Sequence[Any],
    audio_sources: Sequence[Any],
    stt_transport: CapCutSTTTransport | Any | None = None,
    correction_transport: SubtitleCorrectionTransport | Any | None = None,
) -> dict[str, Any]:
    """把 TTS 音频转成 ``new_segments/new_timelines``。

    ``audio_sources`` 可以是本地 TTS 路径，也可以是 TTS 返回的 http(s) 链接。
    远程链接仅下载到临时目录，STT 完成后自动清理，不写入业务交接稿。
    """

    if len(segment_text) != len(audio_sources):
        raise CapCutSTTError("TTS 文案和音频必须一一对应")
    if not audio_sources:
        raise CapCutSTTError("至少需要一个 TTS 分段")
    normalized_text = [str(item or "").strip() for item in segment_text]
    if any(not item for item in normalized_text):
        raise CapCutSTTError("TTS 文案不能包含空分段")

    started_at = time.perf_counter()
    materialize_started_at = time.perf_counter()
    local_paths, temporary = _materialize_audio_sources(audio_sources)
    materialize_elapsed_ms = round((time.perf_counter() - materialize_started_at) * 1000, 1)
    try:
        stt = stt_transport or CapCutSTTTransport.from_env()
        groups = [
            {
                "group_id": f"g{index:02d}",
                "audio_path": local_paths[index - 1],
                "reference_text": normalized_text[index - 1],
            }
            for index in range(1, len(local_paths) + 1)
        ]
        stt_started_at = time.perf_counter()
        candidates, stt_report = stt.transcribe_groups(groups)
        stt_elapsed_ms = round((time.perf_counter() - stt_started_at) * 1000, 1)
        if not candidates:
            raise CapCutSTTError("剪映 STT 未生成字幕候选")
        raw_group_timelines = stt_report.get("group_timelines")
        raw_total_timeline = stt_report.get("total_timeline")
        if not isinstance(raw_group_timelines, list) or len(raw_group_timelines) != len(groups):
            raise CapCutSTTError("剪映 STT 未返回与音频一一对应的分段时间线")
        group_timelines = [
            {"start": start, "end": end}
            for index, item in enumerate(raw_group_timelines)
            for start, end in [_window(item, f"capcut_stt.group_timelines[{index}]")]
        ]
        total_start, total_end = _window(raw_total_timeline, "capcut_stt.total_timeline")
        total_timeline = {"start": total_start, "end": total_end}
        if group_timelines[0]["start"] != total_start or group_timelines[-1]["end"] != total_end:
            raise CapCutSTTError("剪映 STT 分段时间线未完整覆盖总时间线")
        if any(
            group_timelines[index]["end"] != group_timelines[index + 1]["start"]
            for index in range(len(group_timelines) - 1)
        ):
            raise CapCutSTTError("剪映 STT 分段时间线存在空洞或重叠")
        candidate_windows = _validate_caption_timeline(
            candidates,
            group_timelines=group_timelines,
            group_ids=[str(group["group_id"]) for group in groups],
            total_timeline=total_timeline,
            field="capcut_stt.candidates",
        )
        correction = correction_transport or SubtitleCorrectionTransport.from_api_management()
        correction_started_at = time.perf_counter()
        corrected, correction_report = correction.correct(candidates)
        correction_elapsed_ms = round((time.perf_counter() - correction_started_at) * 1000, 1)
    finally:
        if temporary is not None:
            temporary.cleanup()

    if len(corrected) != len(candidates):
        raise CapCutSTTError("字幕校对改变了字幕数量")
    output_timelines: list[dict[str, int]] = []
    output_segments: list[str] = []
    corrected_windows: list[tuple[str, int, int]] = []
    for index, item in enumerate(corrected):
        if not isinstance(item, Mapping):
            raise CapCutSTTError(f"字幕校对结果[{index}] 必须是对象")
        text = str(item.get("text") or "").strip()
        if not text:
            raise CapCutSTTError(f"字幕校对结果[{index}] 文本为空")
        start, end = _window(
            {"start_us": item.get("start_us"), "end_us": item.get("end_us")},
            f"corrected[{index}]",
        )
        original_item = candidates[index]
        original_id = str(original_item.get("caption_id") or "").strip()
        original_group = str(original_item.get("group_id") or "").strip()
        returned_id = str(item.get("caption_id") or "").strip()
        returned_group = str(item.get("group_id") or "").strip()
        if returned_id != original_id or returned_group != original_group:
            raise CapCutSTTError("字幕校对改变了字幕 ID 或分组顺序")
        original_start = _int_us(original_item.get("start_us"), f"candidates[{index}].start_us")
        original_end = _int_us(original_item.get("end_us"), f"candidates[{index}].end_us")
        if (start, end) != (original_start, original_end):
            raise CapCutSTTError("字幕校对不得修改剪映 STT 时间线")
        output_segments.append(text)
        output_timelines.append({"start": start, "end": end})
        corrected_windows.append((returned_id, start, end))

    _validate_caption_timeline(
        corrected,
        group_timelines=group_timelines,
        group_ids=[str(group["group_id"]) for group in groups],
        total_timeline=total_timeline,
        field="corrected",
    )
    if [item[0] for item in corrected_windows] != [item[0] for item in candidate_windows]:
        raise CapCutSTTError("字幕校对改变了字幕 ID 顺序")

    # 不把临时下载路径写进交接数据；保留每个 STT 任务的可追踪摘要。
    pipeline = {
        "status": "succeeded",
        "source": "capcut_stt_doubao_mini",
        "timeline_source": "capcut_stt_upload_duration_and_utterances",
        "timeline_unit": "microseconds",
        "stt": dict(stt_report),
        "correction": dict(correction_report),
        "caption_count": len(output_segments),
        "performance": {
            "audio_materialize_elapsed_ms": materialize_elapsed_ms,
            "stt_elapsed_ms": stt_elapsed_ms,
            "correction_elapsed_ms": correction_elapsed_ms,
            "total_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        },
    }
    for task in pipeline["stt"].get("tasks", []):
        if isinstance(task, dict):
            task.pop("file_path", None)

    return {
        "captions": [dict(item) for item in corrected],
        "new_segments": output_segments,
        "new_timelines": output_timelines,
        "group_timelines": group_timelines,
        "total_timeline": total_timeline,
        "duration": [round((item["end"] - item["start"]) / 1_000_000, 1) for item in group_timelines],
        "BGM_duration": _bgm_duration(total_timeline),
        "subtitle_source": "capcut_stt_doubao_mini",
        "pipeline": pipeline,
    }


__all__ = ["run_capcut_stt_subtitle_pipeline"]
