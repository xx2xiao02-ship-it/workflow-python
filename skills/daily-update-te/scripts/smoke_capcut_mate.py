"""对本机 CapCut Mate 执行一次最小真实草稿链路。

默认链路为 create_draft -> add_captions -> save_draft；提供真实媒体 URL 或 JSON
文件时，可扩展为 create_draft -> add_videos/add_audios -> add_captions -> save_draft。
默认只输出脱敏字段和数量信息，不打印草稿 URL；脚本会创建新的测试草稿。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from project_root import find_project_root


PROJECT_ROOT = find_project_root()
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from workflow_1256.capcut_mate_transport import CapCutMateClient
from workflow_1256.create_draft import run_create_draft
from workflow_1256.save_draft import run_save_draft


def _redact(value: Any) -> str:
    text = str(value)
    return re.sub(r"https?://[^\s'\"}]+", "<redacted-url>", text)


def _summary(name: str, response: dict[str, Any], previous_url: str = "") -> dict[str, Any]:
    current_url = response.get("draft_url", "")
    return {
        "step": name,
        "ok": isinstance(current_url, str) and bool(current_url),
        "keys": list(response),
        "draft_url_present": bool(current_url),
        "draft_url_changed": bool(previous_url and current_url and current_url != previous_url),
        "array_counts": {
            key: len(value)
            for key, value in response.items()
            if isinstance(value, list)
        },
    }


def _field_summary(name: str, response: dict[str, Any], field: str) -> dict[str, Any]:
    value = response.get(field)
    item_count = None
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            item_count = len(parsed)
    return {
        "step": name,
        "ok": isinstance(value, str) and bool(value),
        "keys": list(response),
        "field": field,
        "item_count": item_count,
    }


def _array_summary(name: str, response: dict[str, Any], *fields: str) -> dict[str, Any]:
    counts = {
        field: len(response[field])
        for field in fields
        if isinstance(response.get(field), list)
    }
    return {
        "step": name,
        "ok": len(counts) == len(fields) and all(counts[field] > 0 for field in fields),
        "keys": list(response),
        "array_counts": counts,
    }


def _read_json_text(path: Path, field: str) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError as exc:
        raise ValueError(f"找不到 {field} JSON 文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} JSON 文件无效：{path}: {exc}") from exc
    if isinstance(value, str):
        json.loads(value)
        return value
    return json.dumps(value, ensure_ascii=False)


def _single_media_infos(url: str, *, kind: str) -> str:
    value = url.strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{kind}_url 必须是 CapCut Mate 可访问的 http/https URL")
    if kind == "video":
        item = {
            "video_url": value,
            "start": 0,
            "end": 1_000_000,
            "duration": 1_000_000,
            "volume": 0,
        }
    elif kind == "audio":
        item = {
            "audio_url": value,
            "start": 0,
            "end": 1_000_000,
            "duration": 1_000_000,
            "volume": 0.8,
        }
    else:
        raise ValueError(f"不支持的媒体类型：{kind}")
    return json.dumps([item], ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="CapCut Mate 最小真实草稿链路")
    parser.add_argument("--base-url", help="CapCut Mate 地址；也可使用 CAPCUT_MATE_BASE_URL")
    video_source = parser.add_mutually_exclusive_group()
    video_source.add_argument("--video-infos-file", type=Path, help="真实 add_videos.video_infos JSON 文件")
    video_source.add_argument("--video-url", help="CapCut Mate 可访问的小 MP4 URL；默认截取前 1 秒")
    audio_source = parser.add_mutually_exclusive_group()
    audio_source.add_argument("--audio-infos-file", type=Path, help="真实 add_audios.audio_infos JSON 文件")
    audio_source.add_argument("--audio-url", help="CapCut Mate 可访问的小 MP3 URL；默认截取前 1 秒")
    parser.add_argument(
        "--with-keyframes-effects",
        action="store_true",
        help="在视频写入后继续验证 8364 的关键帧，以及暗角/蓝色丝印特效",
    )
    args = parser.parse_args()

    try:
        client = CapCutMateClient(args.base_url) if args.base_url else CapCutMateClient.from_env()
        created = run_create_draft(
            {"height": 1920, "width": 1080},
            transport=client.create_draft,
        )
        draft_url = created["draft_url"]
        current_url = draft_url
        results = [_summary("create_draft", created)]
        video_result: dict[str, Any] | None = None
        one_second_timeline = [{"start": 0, "end": 1_000_000}]

        if args.audio_url:
            timeline_result = client.call("audio_timelines", {"links": [args.audio_url]})
            results.append(
                _array_summary(
                    "audio_timelines",
                    timeline_result,
                    "timelines",
                    "all_timelines",
                )
            )

        if args.video_infos_file or args.video_url:
            if args.video_infos_file:
                video_infos = _read_json_text(args.video_infos_file, "video_infos")
            else:
                video_info_result = client.call(
                    "video_infos",
                    {
                        "video_urls": [args.video_url],
                        "timelines": one_second_timeline,
                        "transition": "漫画撕纸",
                        "transition_duration": 1_000_000,
                        "volume": 0,
                    },
                )
                results.append(_field_summary("video_infos", video_info_result, "infos"))
                video_infos = video_info_result.get("infos")
                if not isinstance(video_infos, str) or not video_infos:
                    raise ValueError("video_infos 未返回有效 infos")
                host_video_info_result = client.call(
                    "video_infos",
                    {
                        "video_urls": [args.video_url],
                        "timelines": one_second_timeline,
                        "transition": "胶片定格",
                        "transition_duration": 1_000_000,
                        "volume": 0,
                    },
                )
                results.append(
                    _field_summary(
                        "video_infos:HOST",
                        host_video_info_result,
                        "infos",
                    )
                )
            video_result = client.add_videos(current_url, video_infos)
            results.append(_summary("add_videos", video_result, current_url))
            current_url = video_result.get("draft_url", "")
            if not current_url:
                raise ValueError("add_videos 未返回有效 draft_url")

        if args.with_keyframes_effects:
            if not video_result:
                raise ValueError("--with-keyframes-effects 必须同时提供视频 URL 或 video_infos")
            segment_infos = video_result.get("segment_infos")
            if not isinstance(segment_infos, list) or not segment_infos:
                raise ValueError("add_videos 未返回可用于关键帧的 segment_infos")
            keyframes_info_result = client.call(
                "keyframes_infos",
                {
                    "ctype": "UNIFORM_SCALE",
                    "offsets": "0|100",
                    "segment_infos": segment_infos,
                    "values": "1|1.15",
                },
            )
            results.append(
                _field_summary(
                    "keyframes_infos",
                    keyframes_info_result,
                    "keyframes_infos",
                )
            )
            keyframes = keyframes_info_result.get("keyframes_infos")
            if not isinstance(keyframes, str) or not keyframes:
                raise ValueError("keyframes_infos 未返回有效 keyframes_infos")
            keyframe_result = client.add_keyframes(current_url, keyframes)
            results.append(_summary("add_keyframes", keyframe_result, current_url))
            current_url = keyframe_result.get("draft_url", "")
            if not current_url:
                raise ValueError("add_keyframes 未返回有效 draft_url")

            for effect_name in ("暗角", "蓝色丝印"):
                effect_info_result = client.call(
                    "effect_infos",
                    {
                        "effects": [effect_name],
                        "timelines": one_second_timeline,
                    },
                )
                results.append(
                    _field_summary(
                        f"effect_infos:{effect_name}",
                        effect_info_result,
                        "infos",
                    )
                )
                effect_infos = effect_info_result.get("infos")
                if not isinstance(effect_infos, str) or not effect_infos:
                    raise ValueError(f"effect_infos 未返回 {effect_name} 的有效 infos")
                effect_result = client.add_effects(current_url, effect_infos)
                results.append(
                    _summary(f"add_effects:{effect_name}", effect_result, current_url)
                )
                current_url = effect_result.get("draft_url", "")
                if not current_url:
                    raise ValueError(f"add_effects 未返回 {effect_name} 的有效 draft_url")

        if args.audio_infos_file or args.audio_url:
            if args.audio_infos_file:
                audio_infos = _read_json_text(args.audio_infos_file, "audio_infos")
            else:
                audio_info_result = client.call(
                    "audio_infos",
                    {
                        "mp3_urls": [args.audio_url],
                        "timelines": one_second_timeline,
                        "audio_effect": "人声增强",
                        "volume": 1.2,
                    },
                )
                results.append(_field_summary("audio_infos", audio_info_result, "infos"))
                audio_infos = audio_info_result.get("infos")
                if not isinstance(audio_infos, str) or not audio_infos:
                    raise ValueError("audio_infos 未返回有效 infos")
                bgm_info_result = client.call(
                    "audio_infos",
                    {
                        "mp3_urls": [args.audio_url],
                        "timelines": one_second_timeline,
                        "volume": 0.4,
                    },
                )
                results.append(
                    _field_summary("audio_infos:BGM", bgm_info_result, "infos")
                )
            audio_result = client.add_audios(current_url, audio_infos)
            results.append(_summary("add_audios", audio_result, current_url))
            current_url = audio_result.get("draft_url", "")
            if not current_url:
                raise ValueError("add_audios 未返回有效 draft_url")

        caption_info_result = client.call(
            "caption_infos",
            {
                "texts": ["CapCut Mate smoke test"],
                "timelines": one_second_timeline,
            },
        )
        results.append(_field_summary("caption_infos", caption_info_result, "infos"))
        captions = caption_info_result.get("infos")
        if not isinstance(captions, str) or not captions:
            raise ValueError("caption_infos 未返回有效 infos")
        caption_result = client.add_captions(current_url, captions)
        caption_url = caption_result.get("draft_url", "")
        saved = run_save_draft(
            {"draft_url": caption_url or current_url},
            transport=client.save_draft,
        )
        draft_files = client.get_draft(saved["draft_url"])
    except Exception as exc:
        print(json.dumps({"ok": False, "error": _redact(exc)}, ensure_ascii=False))
        return 1

    results.extend([
        _summary("add_captions", caption_result, current_url),
        _summary("save_draft", saved, caption_url or current_url),
        {
            "step": "get_draft",
            "ok": bool(draft_files.get("files")),
            "file_count": len(draft_files.get("files", [])),
            "files_present": bool(draft_files.get("files")),
        },
    ])
    all_ok = all(item["ok"] for item in results)
    print(json.dumps({"ok": all_ok, "steps": results}, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
