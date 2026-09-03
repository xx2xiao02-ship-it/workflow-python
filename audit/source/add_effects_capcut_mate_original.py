import json


def parse_effects_data(json_str):
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse error: {e.msg}")
    if not isinstance(data, list):
        raise ValueError("effect_infos should be a list")
    result = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"the {i}th item should be a dict")
        required_fields = ["effect_title", "start", "end"]
        missing_fields = [field for field in required_fields if field not in item]
        if missing_fields:
            raise ValueError(f"the {i}th item is missing required fields: {', '.join(missing_fields)}")
        processed_item = {"effect_title": str(item["effect_title"]), "start": item["start"], "end": item["end"]}
        if not isinstance(processed_item["start"], (int, float)) or processed_item["start"] < 0:
            raise ValueError(f"the {i}th item has invalid start time")
        if not isinstance(processed_item["end"], (int, float)) or processed_item["end"] <= processed_item["start"]:
            raise ValueError(f"the {i}th item has invalid end time")
        if len(processed_item["effect_title"].strip()) == 0:
            raise ValueError(f"the {i}th item has invalid effect_title")
        processed_item["start"] = int(processed_item["start"])
        processed_item["end"] = int(processed_item["end"])
        result.append(processed_item)
    return result
