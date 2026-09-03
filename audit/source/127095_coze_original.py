import json
import math
import unicodedata


MIN_FULL_TENTHS = 40
MIN_LAST_TENTHS = 1
MAX_SHOT_TENTHS = 100


async def main(args):
    params = getattr(args, "params", None)

    if not isinstance(params, dict):
        if isinstance(args, dict):
            params = args.get("params", args)
        else:
            params = {}

    if isinstance(params.get("_input"), dict):
        params = params["_input"]

    def parse_value(value):
        if isinstance(value, str):
            text = value.strip()

            if text:
                try:
                    return json.loads(text)
                except Exception:
                    return value

        return value

    def as_list(value):
        value = parse_value(value)
        return value if isinstance(value, list) else []

    def as_dict(value):
        value = parse_value(value)
        return value if isinstance(value, dict) else {}

    def clean_text(value):
        if value is None:
            return ""

        return str(value).strip()

    def to_int(value):
        try:
            return int(round(float(value)))
        except Exception:
            return None

    def to_tenths(value):
        try:
            number = float(value)

            if number <= 0:
                return 0

            return int(round(number * 10))
        except Exception:
            return 0

    def get_timeline(value):
        value = parse_value(value)

        if isinstance(value, dict):
            return value

        if isinstance(value, list) and len(value) == 1:
            first = parse_value(value[0])

            if isinstance(first, dict):
                return first

        return {}

    def effective_text_len(text):
        count = 0

        for char in clean_text(text):
            if char.isspace():
                continue

            category = unicodedata.category(char)

            if (
                category.startswith("P")
                or category.startswith("Z")
                or category.startswith("C")
            ):
                continue

            count += 1

        return max(count, 1)

    def get_shot_count_bounds(total_tenths):
        min_count = max(
            1,
            math.ceil(total_tenths / MAX_SHOT_TENTHS)
        )

        max_count = max(
            1,
            math.floor(
                (total_tenths - MIN_LAST_TENTHS) / MIN_FULL_TENTHS
            ) + 1
        )

        return min_count, max_count

    def allocate_durations(total_tenths, source_text_list):
        shot_count = len(source_text_list)

        if shot_count == 0:
            return [], [], []

        base_units = [MIN_FULL_TENTHS] * shot_count
        base_units[-1] = MIN_LAST_TENTHS

        min_total = sum(base_units)
        max_total = shot_count * MAX_SHOT_TENTHS

        if total_tenths < min_total:
            return [], [], []

        if total_tenths > max_total:
            return [], [], []

        duration_units = list(base_units)
        remaining = total_tenths - min_total

        weights = [
            effective_text_len(text)
            for text in source_text_list
        ]

        extras = [0] * shot_count

        max_extra = [
            MAX_SHOT_TENTHS - base
            for base in base_units
        ]

        for _ in range(remaining):
            candidates = [
                index
                for index in range(shot_count)
                if extras[index] < max_extra[index]
            ]

            if not candidates:
                break

            target_index = min(
                candidates,
                key=lambda index: (
                    (extras[index] + 1) / weights[index],
                    -weights[index],
                    index
                )
            )

            extras[target_index] += 1

        for index in range(shot_count):
            duration_units[index] += extras[index]

        clip_duration = [
            round(value / 10, 1)
            for value in duration_units
        ]

        int_duration = [
            max(4, int(math.ceil(value / 10.0)))
            for value in duration_units
        ]

        return duration_units, clip_duration, int_duration

    def build_timelines(start, end, duration_units):
        if not duration_units:
            return []

        total_units = sum(duration_units)
        total_span = end - start

        if total_units <= 0 or total_span <= 0:
            return []

        result = []
        current_start = start
        accumulated_units = 0

        for index, units in enumerate(duration_units):
            accumulated_units += units

            if index == len(duration_units) - 1:
                current_end = end
            else:
                current_end = start + int(
                    round(
                        total_span * accumulated_units / total_units
                    )
                )

            if current_end <= current_start:
                current_end = current_start + 1

            result.append({
                "start": current_start,
                "end": current_end
            })

            current_start = current_end

        return result

    empty_result = {
        "shots": [],
        "clip_duration": [],
        "int_duration": [],
        "timelines": []
    }

    total_tenths = to_tenths(params.get("duration"))
    raw_shots = as_list(params.get("shots"))
    source_timeline = get_timeline(params.get("timelines"))

    timeline_start = to_int(source_timeline.get("start"))
    timeline_end = to_int(source_timeline.get("end"))

    if total_tenths <= 0 or not raw_shots:
        return empty_result

    if timeline_start is None or timeline_end is None:
        return empty_result

    if timeline_end <= timeline_start:
        return empty_result

    min_count, max_count = get_shot_count_bounds(total_tenths)

    if len(raw_shots) < min_count or len(raw_shots) > max_count:
        return empty_result

    source_text_list = []
    clean_shots = []

    for raw_shot in raw_shots:
        shot = as_dict(raw_shot)

        source_text = clean_text(shot.get("source_text"))
        clip_role = clean_text(shot.get("clip_role"))
        story_beat = clean_text(shot.get("story_beat"))

        if not source_text or not clip_role or not story_beat:
            return empty_result

        source_text_list.append(source_text)

        clean_shots.append({
            "source_text": source_text,
            "clip_role": clip_role,
            "story_beat": story_beat
        })

    duration_units, clip_duration, int_duration = allocate_durations(
        total_tenths,
        source_text_list
    )

    if not duration_units:
        return empty_result

    timelines = build_timelines(
        timeline_start,
        timeline_end,
        duration_units
    )

    if not timelines:
        return empty_result

    for index, shot in enumerate(clean_shots):
        shot["clip_duration"] = clip_duration[index]

    return {
        "shots": clean_shots,
        "clip_duration": clip_duration,
        "int_duration": int_duration,
        "timelines": timelines
    }
