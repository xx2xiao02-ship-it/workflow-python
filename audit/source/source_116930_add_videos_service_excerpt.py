"""116930“数字人*”对应 capcut-mate add_videos 的只读源码摘录。

来源：Hommy-master/capcut-mate
提交：f2581bd989190ee61719f2bf50e118ab035dcc7f
原文件：src/service/add_videos.py

本文件只保存可脱离草稿缓存、素材下载和剪映运行时执行的 JSON
解析/归一化函数，用于旧新代码同输入对照；不调用任何外部服务。
"""

import json
from typing import Any


class OriginalAddVideosError(ValueError):
    """原源码 CustomException 的离线替身，仅用于异常边界比较。"""


def parse_video_data(json_str: str) -> list[dict[str, Any]]:
    """按原 add_videos.py 的 parse_video_data 规则解析视频 JSON。"""

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise OriginalAddVideosError(f"JSON parse error: {exc.msg}") from exc

    if not isinstance(data, list):
        raise OriginalAddVideosError("video_infos should be a list")

    result: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise OriginalAddVideosError(f"the {index}th item should be a dict")

        required_fields = ["video_url", "start", "end"]
        missing_fields = [field for field in required_fields if field not in item]
        if missing_fields:
            raise OriginalAddVideosError(
                f"the {index}th item is missing required fields: {', '.join(missing_fields)}"
            )

        if not isinstance(item["start"], (int, float)) or item["start"] < 0:
            raise OriginalAddVideosError(f"the {index}th item has invalid start time")
        if not isinstance(item["end"], (int, float)) or item["end"] <= item["start"]:
            raise OriginalAddVideosError(f"the {index}th item has invalid end time")

        start = int(item["start"])
        end = int(item["end"])
        if end <= start:
            raise OriginalAddVideosError(f"the {index}th item has invalid end time")

        if "duration" in item:
            duration = item["duration"]
            if not isinstance(duration, (int, float)) or duration <= 0:
                raise OriginalAddVideosError(f"the {index}th item has invalid duration")
            duration = int(duration)
        else:
            duration = end - start

        processed_item = {
            "video_url": item["video_url"],
            "width": item.get("width"),
            "height": item.get("height"),
            "start": start,
            "end": end,
            "duration": duration,
            "mask": item.get("mask", None),
            "transition": item.get("transition", None),
            "transition_duration": item.get("transition_duration", None),
            "volume": 1.0 if item.get("volume") is None else item.get("volume"),
        }

        if processed_item["volume"] < 0 or processed_item["volume"] > 10:
            processed_item["volume"] = 1.0

        result.append(processed_item)

    return result

