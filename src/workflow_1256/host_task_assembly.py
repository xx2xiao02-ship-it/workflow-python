# 8364 节点 117861 Host 任务组装；代码逐字来源于工作流导出。
# 未对业务代码做重构；仅增加可测试的 Python 调用入口。
import asyncio
from types import SimpleNamespace

import json


async def main(args):
    params = getattr(args, "params", None)

    if not isinstance(params, dict):
        if isinstance(args, dict):
            params = args.get("params", args)
        else:
            params = {}

    if isinstance(params.get("_input"), dict):
        params = params.get("_input")

    def to_plain(value):
        if value is None:
            return None

        if isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return {
                str(key): to_plain(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple, set)):
            return [to_plain(item) for item in value]

        if hasattr(value, "__dict__"):
            return {
                str(key): to_plain(item)
                for key, item in vars(value).items()
            }

        return str(value)

    def parse_value(value):
        value = to_plain(value)

        if isinstance(value, str):
            text = value.strip()

            if text:
                try:
                    return json.loads(text)
                except Exception:
                    return value

        return value

    def as_dict(value):
        value = parse_value(value)
        return value if isinstance(value, dict) else {}

    def as_list(value):
        value = parse_value(value)
        return value if isinstance(value, list) else []

    def clean_text(value):
        if value is None:
            return ""

        return str(value).strip()

    def to_int(value, default=0):
        try:
            return int(float(value))
        except Exception:
            return default

    def get_param(key, default=None):
        source = to_plain(params)

        if not isinstance(source, dict):
            return default

        if key in source:
            return source.get(key)

        for wrap_key in [
            "input",
            "inputs",
            "params",
            "body",
            "data",
            "output",
            "result"
        ]:
            wrapped = source.get(wrap_key)

            if isinstance(wrapped, dict) and key in wrapped:
                return wrapped.get(key)

        return default

    def norm_time(value):
        item = as_dict(value)

        start = to_int(item.get("start"), 0)
        end = to_int(item.get("end"), 0)

        if end < start:
            end = start

        return {
            "start": start,
            "end": end
        }

    raw_idxs = as_list(
        get_param("host_idxs", [])
    )

    host_map = as_dict(
        get_param("host_map", {})
    )

    raw_units = as_list(
        host_map.get("units")
    )

    errors = []
    idx_set = set()

    for item in raw_idxs:
        idx = to_int(item, -1)

        if idx >= 0:
            idx_set.add(idx)

    if not idx_set:
        return {
            "audio_url": [],
            "audio_time": [],
            "new_timelines": [],
            "error": ""
        }

    selected = []

    for raw_unit in raw_units:
        unit = as_dict(raw_unit)
        idx = to_int(unit.get("idx"), -1)

        if idx not in idx_set:
            continue

        url = clean_text(
            unit.get("audio_url")
        )

        audio_time = norm_time(
            unit.get("audio_time")
        )

        new_time = norm_time(
            unit.get("new_timelines")
        )

        if not url:
            errors.append(
                "idx " + str(idx) + " 缺少 audio_url"
            )
            continue

        if audio_time["end"] <= audio_time["start"]:
            errors.append(
                "idx " + str(idx) + " audio_time 无效"
            )
            continue

        if new_time["end"] <= new_time["start"]:
            errors.append(
                "idx " + str(idx) + " new_timelines 无效"
            )
            continue

        selected.append({
            "idx": idx,
            "audio_url": url,
            "audio_time": audio_time,
            "new_timelines": new_time
        })

    found_idxs = {
        item["idx"]
        for item in selected
    }

    for idx in sorted(idx_set - found_idxs):
        errors.append(
            "host_map 未找到 idx " + str(idx)
        )

    selected.sort(
        key=lambda item: (
            item["new_timelines"]["start"],
            item["new_timelines"]["end"]
        )
    )

    tasks = []

    for item in selected:
        if not tasks:
            tasks.append({
                "audio_url": item["audio_url"],
                "audio_time": item["audio_time"],
                "new_timelines": item["new_timelines"]
            })
            continue

        last = tasks[-1]

        can_merge = (
            last["audio_url"] == item["audio_url"]
            and last["audio_time"]["end"]
            == item["audio_time"]["start"]
            and last["new_timelines"]["end"]
            == item["new_timelines"]["start"]
        )

        if can_merge:
            last["audio_time"]["end"] = (
                item["audio_time"]["end"]
            )

            last["new_timelines"]["end"] = (
                item["new_timelines"]["end"]
            )
        else:
            tasks.append({
                "audio_url": item["audio_url"],
                "audio_time": item["audio_time"],
                "new_timelines": item["new_timelines"]
            })

    audio_url = [
        item["audio_url"]
        for item in tasks
    ]

    audio_time = [
        item["audio_time"]
        for item in tasks
    ]

    new_timelines = [
        item["new_timelines"]
        for item in tasks
    ]

    return {
        "audio_url": audio_url,
        "audio_time": audio_time,
        "new_timelines": new_timelines,
        "error": "；".join(errors)
    }

async def run_host_task_assembly_async(params):
    return await main(SimpleNamespace(params=params))

def run_host_task_assembly(params):
    return asyncio.run(run_host_task_assembly_async(params))

__all__ = ["run_host_task_assembly", "run_host_task_assembly_async"]

