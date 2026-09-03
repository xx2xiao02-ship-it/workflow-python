from src.utils.logger import logger
import json


def effect_infos(effects, timelines):
    logger.info(f"effect_infos called with {len(effects)} effects and {len(timelines)} timelines")
    if len(effects) != len(timelines):
        min_len = min(len(effects), len(timelines))
        effects = effects[:min_len]
        timelines = timelines[:min_len]
    infos = []
    for effect, timeline in zip(effects, timelines):
        infos.append({
            "effect_title": effect,
            "start": timeline["start"],
            "end": timeline["end"],
        })
    return json.dumps(infos, ensure_ascii=False)
