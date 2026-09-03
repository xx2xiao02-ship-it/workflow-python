"""受控音色目录与音色选择解析。

新版 V3 声音复刻上传音频是训练动作。首次预付费/免费额度创建可留空
``speaker_id``，由接口分配并返回真实 ``S_...``；更新已有音色时传真实
``speaker_id``。后付费自定义音色请求时必须把
``speaker_id`` 固定为 ``custom_speaker_id``，用户自己的唯一名称放在
``custom_speaker_id``。目录同时保存请求绑定与实际可用代号，TTS 合成阶段不会
把协议固定值误传成声音。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
from typing import Any


class VoiceCatalogError(ValueError):
    """音色目录、用户偏好或模型选择不符合受控契约。"""


VoiceSelectorTransport = Callable[[Mapping[str, Any]], Mapping[str, Any]]
_GENDERS = {"male", "female", "neutral", "auto"}
_BILLING_MODES = {"official", "prepaid_slot", "postpaid_custom"}
_INTERNAL_TTS_CONFIG_KEYS = {"voice_catalog", "voice_selection", "voice_director", "performance_plan", "overall_speed_ratio", "voice_style", "voice_gender", "voice_key"}
DEFAULT_VOICE_PROVIDER = "volcengine"
DEFAULT_VOICE_TYPE = "official"
DEFAULT_VOICE_MODEL = "seed-tts-2.0-standard"
DEFAULT_VOICE_RESOURCE_ID = "seed-tts-2.0"
# 资源 ID 与音色类型必须成对出现。官方音色使用 TTS 2.0 字符版，
# 定制/复刻音色默认使用 ICL 2.0；旧版已登记音色可能由官方状态响应绑定
# ICL 1.0，因此允许两种官方 ICL 资源，但不能让页面静默跨版本替换。
CUSTOM_VOICE_RESOURCE_IDS = frozenset({"seed-icl-1.0", "seed-icl-2.0"})
CUSTOM_SPEAKER_PROTOCOL_ID = "custom_speaker_id"
_CUSTOM_SPEAKER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*[A-Za-z0-9]$")
VOICE_RESOURCE_BY_TYPE = {
    "official": "seed-tts-2.0",
    "custom": "seed-icl-2.0",
}

# 内置项来自火山引擎豆包语音合成模型 2.0 官方音色列表；是否能被当前账号调用，
# 仍由真实 TTS 请求验收，不把“官方列出”误报为“本账号已试听”。
DEFAULT_VOICE_CATALOG: dict[str, Any] = {
    "version": "2026-08-18",
    "defaults": {"male": "qingcang_2", "female": "vivi_2", "neutral": ""},
    "voices": [
        {
            "voice_key": "qingcang_2", "speaker_id": "zh_male_qingcang_uranus_bigtts",
            "display_name": "擎苍 2.0", "gender": "male",
            "style_tags": ["成熟", "稳重", "清晰", "科普", "叙事"], "languages": ["zh-CN"],
            "enabled": True, "verified": True, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "youyoujunzi_2", "speaker_id": "zh_male_youyoujunzi_uranus_bigtts",
            "display_name": "悠悠君子 2.0", "gender": "male",
            "style_tags": ["中文", "通用", "男声"], "languages": ["zh-CN"],
            "enabled": True, "verified": False, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "kailangxuezhang_2", "speaker_id": "zh_male_kailangxuezhang_uranus_bigtts",
            "display_name": "开朗学长 2.0", "gender": "male",
            "style_tags": ["中文", "通用", "男声"], "languages": ["zh-CN"],
            "enabled": True, "verified": False, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "shenyeboke_2", "speaker_id": "zh_male_shenyeboke_uranus_bigtts",
            "display_name": "深夜播客 2.0", "gender": "male",
            "style_tags": ["中文", "通用", "男声"], "languages": ["zh-CN"],
            "enabled": True, "verified": False, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "liufei_2", "speaker_id": "zh_male_liufei_uranus_bigtts",
            "display_name": "刘飞 2.0", "gender": "male",
            "style_tags": ["中文", "通用", "男声"], "languages": ["zh-CN"],
            "enabled": True, "verified": False, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "dayi_2", "speaker_id": "zh_male_dayi_uranus_bigtts",
            "display_name": "大益 2.0", "gender": "male",
            "style_tags": ["中文", "通用", "男声"], "languages": ["zh-CN"],
            "enabled": True, "verified": False, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "m191_2", "speaker_id": "zh_male_m191_uranus_bigtts",
            "display_name": "M191 2.0", "gender": "male",
            "style_tags": ["中文", "通用", "男声"], "languages": ["zh-CN"],
            "enabled": True, "verified": True, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "vivi_2", "speaker_id": "zh_female_vv_uranus_bigtts",
            "display_name": "Vivi 2.0", "gender": "female",
            "style_tags": ["通用", "多语种", "方言", "指令遵循"], "languages": ["zh-CN", "ja", "id", "es-MX"],
            "enabled": True, "verified": True, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "xiaohe_2", "speaker_id": "zh_female_xiaohe_uranus_bigtts",
            "display_name": "小何 2.0", "gender": "female",
            "style_tags": ["通用", "中文", "指令遵循"], "languages": ["zh-CN"],
            "enabled": True, "verified": True, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "qingxin_2", "speaker_id": "zh_female_qingxinnvsheng_uranus_bigtts",
            "display_name": "清新女声 2.0", "gender": "female",
            "style_tags": ["通用", "清新", "中文", "指令遵循"], "languages": ["zh-CN"],
            "enabled": True, "verified": True, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
        {
            "voice_key": "sophie_2", "speaker_id": "zh_female_sophie_uranus_bigtts",
            "display_name": "魅力苏菲 2.0", "gender": "female",
            "style_tags": ["通用", "魅力", "中文", "指令遵循"], "languages": ["zh-CN"],
            "enabled": True, "verified": True, "provider": DEFAULT_VOICE_PROVIDER,
            "voice_type": DEFAULT_VOICE_TYPE, "model": DEFAULT_VOICE_MODEL, "resource_id": DEFAULT_VOICE_RESOURCE_ID,
            "training_status": "ready", "authorization_status": "verified",
        },
    ],
}

# 兼容旧模块对默认值常量的导入，但实际值只从本目录派生，禁止再维护第二份
# speaker_id 映射。新增或调整默认音色时只修改 DEFAULT_VOICE_CATALOG。
DEFAULT_TTS_SPEAKER_ID = next(
    item["speaker_id"]
    for item in DEFAULT_VOICE_CATALOG["voices"]
    if item["voice_key"] == DEFAULT_VOICE_CATALOG["defaults"]["male"]
)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VoiceCatalogError(f"{field} 必须是非空字符串")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise VoiceCatalogError(f"{field} 必须是字符串数组")
    return list(dict.fromkeys(item.strip() for item in value))


def normalize_custom_speaker_id(value: Any, field: str = "custom_speaker_id") -> str:
    """按新版 V3 文档校验后付费自定义音色代号。"""

    if not isinstance(value, str) or not value.strip():
        raise VoiceCatalogError(f"{field} 必须是非空字符串")
    candidate = value.strip()
    folded = candidate.casefold()
    if folded == CUSTOM_SPEAKER_PROTOCOL_ID:
        raise VoiceCatalogError(f"{field} 不能使用接口固定值 {CUSTOM_SPEAKER_PROTOCOL_ID}")
    if not 8 <= len(candidate) <= 256:
        raise VoiceCatalogError(f"{field} 长度必须在 8~256 个字符之间")
    if not _CUSTOM_SPEAKER_ID_RE.fullmatch(candidate):
        raise VoiceCatalogError(
            f"{field} 必须以英文字母开头、以字母或数字结尾，且只能包含字母、数字、中划线和下划线"
        )
    return candidate


def effective_speaker_id(item: Mapping[str, Any]) -> str:
    """返回 TTS 合成应使用的实际音色代号。"""

    custom = str(item.get("custom_speaker_id") or "").strip()
    return custom or str(item.get("speaker_id") or "").strip()


def normalize_voice_catalog(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = DEFAULT_VOICE_CATALOG if value is None else value
    if not isinstance(raw, Mapping):
        raise VoiceCatalogError("voice_catalog 必须是对象")
    voices = raw.get("voices")
    if not isinstance(voices, list) or not voices:
        raise VoiceCatalogError("voice_catalog.voices 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_identities: set[str] = set()
    for index, item in enumerate(voices):
        if not isinstance(item, Mapping):
            raise VoiceCatalogError(f"voices[{index}] 必须是对象")
        voice_key = _string(item.get("voice_key"), f"voices[{index}].voice_key")
        speaker_id = _string(item.get("speaker_id"), f"voices[{index}].speaker_id")
        voice_type = _string(item.get("voice_type", DEFAULT_VOICE_TYPE), f"voices[{index}].voice_type").lower()
        if voice_type not in {"official", "custom"}:
            raise VoiceCatalogError(f"voices[{index}].voice_type 仅支持 official、custom")
        raw_custom_speaker_id = str(item.get("custom_speaker_id") or "").strip()
        if voice_type == "custom" and speaker_id == CUSTOM_SPEAKER_PROTOCOL_ID:
            custom_speaker_id = normalize_custom_speaker_id(raw_custom_speaker_id, f"voices[{index}].custom_speaker_id")
        elif raw_custom_speaker_id:
            raise VoiceCatalogError(
                f"voices[{index}].custom_speaker_id 只有 speaker_id={CUSTOM_SPEAKER_PROTOCOL_ID} 时才能填写"
            )
        else:
            custom_speaker_id = ""
        if voice_type != "custom" and speaker_id == CUSTOM_SPEAKER_PROTOCOL_ID:
            raise VoiceCatalogError(f"voices[{index}].speaker_id 固定值只能用于 custom 音色")
        raw_billing_mode = str(item.get("billing_mode") or "").strip().lower()
        if not raw_billing_mode:
            # 历史目录没有 billing_mode：官方音色按 official 处理；普通定制
            # speaker_id 是已有槽位绑定，只有固定协议值才是后付费自定义。
            raw_billing_mode = (
                "official"
                if voice_type == "official"
                else ("postpaid_custom" if speaker_id == CUSTOM_SPEAKER_PROTOCOL_ID else "prepaid_slot")
            )
        if raw_billing_mode not in _BILLING_MODES:
            raise VoiceCatalogError(
                f"voices[{index}].billing_mode 仅支持 official、prepaid_slot、postpaid_custom"
            )
        if voice_type == "official" and raw_billing_mode != "official":
            raise VoiceCatalogError(f"voices[{index}].billing_mode 与 official 音色不匹配")
        if speaker_id == CUSTOM_SPEAKER_PROTOCOL_ID and raw_billing_mode != "postpaid_custom":
            raise VoiceCatalogError(
                f"voices[{index}].billing_mode 与 speaker_id={CUSTOM_SPEAKER_PROTOCOL_ID} 不匹配"
            )
        if speaker_id != CUSTOM_SPEAKER_PROTOCOL_ID and raw_billing_mode == "postpaid_custom":
            raise VoiceCatalogError("postpaid_custom 音色必须使用 speaker_id=custom_speaker_id")
        slot_source = str(item.get("slot_source") or "").strip()
        if raw_billing_mode == "prepaid_slot" and not slot_source:
            slot_source = "console_prepaid"
        # 声音复刻状态接口不保证返回性别；目录缺省按 neutral 登记，避免
        # 只读同步或人工登记因缺少非关键展示字段而阻断 TTS 解析。
        gender = _string(item.get("gender", "neutral"), f"voices[{index}].gender").lower()
        if gender not in _GENDERS - {"auto"}:
            raise VoiceCatalogError(f"voices[{index}].gender 仅支持 male、female、neutral")
        identity = custom_speaker_id or speaker_id
        if voice_key in seen_keys or identity in seen_identities:
            raise VoiceCatalogError("voice_key 与实际音色代号必须在目录中唯一")
        seen_keys.add(voice_key); seen_identities.add(identity)
        expected_resource_id = VOICE_RESOURCE_BY_TYPE[voice_type]
        # 兼容旧目录中缺少 resource_id 的条目；保存时会把迁移后的配对资源
        # 固化下来。显式写错的资源仍然必须阻断，不能静默纠正用户配置。
        raw_resource_id = item.get("resource_id")
        if raw_resource_id is None:
            raw_resource_id = expected_resource_id
        resource_id = _string(raw_resource_id, f"voices[{index}].resource_id")
        resource_allowed = (
            resource_id == expected_resource_id
            if voice_type == "official"
            else resource_id in CUSTOM_VOICE_RESOURCE_IDS
        )
        if not resource_allowed:
            expected = expected_resource_id
            if voice_type == "custom":
                expected = "seed-icl-1.0 或 seed-icl-2.0"
            raise VoiceCatalogError(
                f"voices[{index}].resource_id 与 voice_type 不匹配；{voice_type} 必须使用 {expected}"
            )
        model = _string(item.get("model", DEFAULT_VOICE_MODEL), f"voices[{index}].model")
        normalized.append({
            "voice_key": voice_key,
            "speaker_id": speaker_id,
            "custom_speaker_id": custom_speaker_id,
            "display_name": _string(item.get("display_name", voice_key), f"voices[{index}].display_name"),
            "gender": gender,
            "style_tags": _string_list(item.get("style_tags", []), f"voices[{index}].style_tags"),
            "languages": _string_list(item.get("languages", ["zh-CN"]), f"voices[{index}].languages"),
            "enabled": bool(item.get("enabled", True)),
            "verified": bool(item.get("verified", False)),
            "provider": _string(item.get("provider", DEFAULT_VOICE_PROVIDER), f"voices[{index}].provider"),
            "voice_type": voice_type,
            "billing_mode": raw_billing_mode,
            "slot_source": slot_source,
            "model": model,
            "resource_id": resource_id,
            "training_status": _string(item.get("training_status", "ready"), f"voices[{index}].training_status"),
            "authorization_status": _string(item.get("authorization_status", "unknown"), f"voices[{index}].authorization_status"),
            "preview_url": str(item.get("preview_url", "") or "").strip(),
            "catalog_version": str(item.get("catalog_version", raw.get("version", "local")) or "local").strip(),
            "created_at": str(item.get("created_at", "") or "").strip(),
            "updated_at": str(item.get("updated_at", "") or "").strip(),
        })
    defaults_raw = raw.get("defaults", {})
    if not isinstance(defaults_raw, Mapping):
        raise VoiceCatalogError("voice_catalog.defaults 必须是对象")
    defaults = {gender: str(defaults_raw.get(gender, "")).strip() for gender in ("male", "female", "neutral")}
    available = {item["voice_key"] for item in normalized if item["enabled"]}
    for gender, voice_key in defaults.items():
        if voice_key and voice_key not in available:
            raise VoiceCatalogError(f"voice_catalog.defaults.{gender} 指向不存在或未启用的音色")
    return {"version": str(raw.get("version", "local")), "defaults": defaults, "voices": normalized}


def _official_tts_catalog(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the production catalog after removing all custom voices.

    Historical snapshots may still contain custom/clone rows.  They remain
    parseable for migration and audit, but no resolver or TTS request is
    allowed to consume them.  Keeping this gate here makes direct callers
    (outside the web console) obey the same official-only policy.
    """

    voices = [
        dict(item)
        for item in value.get("voices", ())
        if str(item.get("voice_type") or DEFAULT_VOICE_TYPE).strip().lower() == "official"
        and str(item.get("resource_id") or DEFAULT_VOICE_RESOURCE_ID).strip() == DEFAULT_VOICE_RESOURCE_ID
        and not str(item.get("custom_speaker_id") or "").strip()
        and str(item.get("speaker_id") or "").strip() != CUSTOM_SPEAKER_PROTOCOL_ID
    ]
    if not voices:
        raise VoiceCatalogError("官方 TTS 音色目录为空；定制音色/声音复刻已停用")
    keys = {str(item.get("voice_key") or "").strip() for item in voices}
    raw_defaults = value.get("defaults") if isinstance(value.get("defaults"), Mapping) else {}
    defaults = {
        gender: str(raw_defaults.get(gender) or "").strip()
        if str(raw_defaults.get(gender) or "").strip() in keys
        else ""
        for gender in ("male", "female", "neutral")
    }
    for gender in ("male", "female", "neutral"):
        if not defaults[gender]:
            defaults[gender] = next(
                (
                    str(item.get("voice_key") or "")
                    for item in voices
                    if bool(item.get("enabled", True))
                    and str(item.get("gender") or "neutral") == gender
                ),
                "",
            )
    return {"version": str(value.get("version", "local")), "defaults": defaults, "voices": voices}


def _gender(value: Any) -> str:
    gender = "auto" if value is None else _string(value, "voice_gender").lower()
    if gender not in _GENDERS:
        raise VoiceCatalogError("voice_gender 仅支持 auto、male、female、neutral")
    return gender


def _voice_is_selectable(item: Mapping[str, Any]) -> bool:
    """官方音色登记且启用后即可直接进入普通 TTS。"""

    # ``verified`` 只是目录审计标记，不再作为官方 TTS 的使用前置条件；
    # 否则官方目录中尚未做本地试听标记的音色会被错误拦截。
    return bool(item.get("enabled"))


def build_voice_candidates(catalog: Mapping[str, Any], *, voice_gender: str = "auto", style_tags: Sequence[str] = (), language: str = "zh-CN", limit: int = 5) -> list[dict[str, Any]]:
    normalized = _official_tts_catalog(normalize_voice_catalog(catalog))
    if voice_gender not in _GENDERS:
        raise VoiceCatalogError("voice_gender 不合法")
    desired = {item.strip() for item in style_tags if isinstance(item, str) and item.strip()}
    # 目录同步结果可能返回 ``zh-cn``，而历史配置和调用默认值使用
    # ``zh-CN``。语言标签大小写不应让已启用的明确音色在缓存恢复阶段
    # 变成“没有候选”，因此只在比较时做大小写折叠，保留原始目录值。
    requested_language = str(language or "").strip().casefold()
    candidates = [
        item for item in normalized["voices"]
        if _voice_is_selectable(item)
        and (voice_gender == "auto" or item["gender"] == voice_gender)
        and (
            not requested_language
            or any(str(candidate).strip().casefold() == requested_language for candidate in item["languages"])
        )
    ]
    if desired:
        tagged = [item for item in candidates if desired.intersection(item["style_tags"])]
        if tagged:
            candidates = tagged
    if not candidates:
        raise VoiceCatalogError(
            "目录中没有符合性别、语言和启用状态的官方 TTS 音色"
        )
    return [{key: item[key] for key in ("voice_key", "display_name", "gender", "style_tags", "languages")} for item in candidates[:limit]]


def build_voice_selection_prompt(segments: Sequence[str], policy: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "task": "voice_selection",
        "role": "音色选择导演",
        "rules": [
            "你只能从 candidates 中选择 candidate_key，不得输出或猜测 speaker_id。",
            "性别不是由文案推断的事实；必须遵从 policy.voice_gender，auto 时仅按叙事风格选择。",
            "输出 JSON：candidate_key、voice_profile、reason；voice_profile 仅描述声音风格。",
            "不得改写、删减或复述原文。",
        ],
        "policy": dict(policy),
        "candidates": [dict(item) for item in candidates],
        "segments": [{"segment_id": f"g{index + 1:02d}", "text": text} for index, text in enumerate(segments)],
    }


def normalize_voice_selection(value: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VoiceCatalogError("音色选择模型输出必须是对象")
    keys = {item.get("voice_key") for item in candidates}
    selected = _string(value.get("candidate_key"), "candidate_key")
    if selected not in keys:
        raise VoiceCatalogError("模型选择了候选列表以外的音色")
    profile = value.get("voice_profile", "")
    if not isinstance(profile, str):
        raise VoiceCatalogError("voice_profile 必须是字符串")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise VoiceCatalogError("reason 必须是字符串")
    return {"candidate_key": selected, "voice_profile": profile.strip(), "reason": reason.strip()}


def run_voice_selector(segments: Sequence[str], policy: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], *, transport: VoiceSelectorTransport | None = None) -> dict[str, Any]:
    if transport is None:
        raise RuntimeError("未注入音色选择模型 transport")
    return normalize_voice_selection(transport(build_voice_selection_prompt(segments, policy, candidates)), candidates)


def resolve_voice_selection(speech_config: Mapping[str, Any], *, catalog: Mapping[str, Any] | None = None, model_selection: Mapping[str, Any] | None = None, model_error: str = "") -> dict[str, Any]:
    """按用户精确指定 > 用户性别 > 模型候选 > 同性别默认 的优先级解析真实音色。"""
    if not isinstance(speech_config, Mapping):
        raise VoiceCatalogError("tts 配置必须是对象")
    normalized = _official_tts_catalog(normalize_voice_catalog(catalog))
    selector = speech_config.get("voice_selection", {})
    if selector is None:
        selector = {}
    if not isinstance(selector, Mapping):
        raise VoiceCatalogError("tts.voice_selection 必须是对象")
    requested_gender = _gender(selector.get("voice_gender", speech_config.get("voice_gender")))
    style_tags = _string_list(selector.get("style_tags", speech_config.get("voice_style", [])), "voice_style")
    language = str(selector.get("language", speech_config.get("language", "zh-CN"))).strip() or "zh-CN"
    all_voices = {item["voice_key"]: item for item in normalized["voices"]}
    voices = {key: item for key, item in all_voices.items() if _voice_is_selectable(item)}
    all_by_speaker: dict[str, Mapping[str, Any]] = {}
    by_speaker: dict[str, Mapping[str, Any]] = {}
    for item in all_voices.values():
        effective = effective_speaker_id(item)
        if effective:
            all_by_speaker[effective] = item
        provider_speaker = str(item.get("speaker_id") or "").strip()
        if provider_speaker and provider_speaker != CUSTOM_SPEAKER_PROTOCOL_ID:
            all_by_speaker.setdefault(provider_speaker, item)
    for item in voices.values():
        effective = effective_speaker_id(item)
        if effective:
            by_speaker[effective] = item
        provider_speaker = str(item.get("speaker_id") or "").strip()
        if provider_speaker and provider_speaker != CUSTOM_SPEAKER_PROTOCOL_ID:
            by_speaker.setdefault(provider_speaker, item)

    explicit_custom = speech_config.get("custom_speaker_id")
    explicit_speaker = speech_config.get("speaker_id") or speech_config.get("voice_id")
    explicit_key = selector.get("voice_key", speech_config.get("voice_key"))
    source = ""
    chosen: Mapping[str, Any] | None = None
    if explicit_custom is not None and str(explicit_custom).strip():
        raise VoiceCatalogError("官方 TTS 不支持 custom_speaker_id；声音复刻/定制音色已停用")
    elif explicit_speaker is not None:
        speaker_id = _string(explicit_speaker, "speaker_id/voice_id")
        if speaker_id == CUSTOM_SPEAKER_PROTOCOL_ID:
            raise VoiceCatalogError("TTS 选择 custom 音色时必须提供实际 custom_speaker_id")
        chosen = by_speaker.get(speaker_id)
        if chosen is None:
            registered = all_by_speaker.get(speaker_id)
            if registered is None:
                raise VoiceCatalogError("用户指定的 speaker_id/voice_id 未登记，拒绝直接传给 TTS")
            if not registered["enabled"]:
                raise VoiceCatalogError("用户指定的 speaker_id/voice_id 已停用，拒绝直接传给 TTS")
            chosen = registered
        source = "user_speaker_id"
    elif explicit_key is not None:
        voice_key = _string(explicit_key, "voice_key")
        chosen = voices.get(voice_key)
        if chosen is None:
            registered = all_voices.get(voice_key)
            if registered is None:
                raise VoiceCatalogError("用户指定的 voice_key 未登记")
            if not registered["enabled"]:
                raise VoiceCatalogError("用户指定的 voice_key 已停用")
            chosen = registered
        source = "user_voice_key"
    else:
        candidates = build_voice_candidates(normalized, voice_gender=requested_gender, style_tags=style_tags, language=language)
        if model_selection is not None:
            selected = normalize_voice_selection(model_selection, candidates)
            chosen = voices[selected["candidate_key"]]
            source = "llm_resolved"
        else:
            default_key = normalized["defaults"].get(requested_gender, "") if requested_gender != "auto" else ""
            if not default_key:
                default_key = normalized["defaults"].get("male", "") or normalized["defaults"].get("neutral", "") or normalized["defaults"].get("female", "")
            chosen = voices.get(default_key) if default_key else None
            if chosen is None:
                chosen = voices[candidates[0]["voice_key"]]
            source = "default_fallback" if model_error else "catalog_default"
    if chosen is None:
        raise VoiceCatalogError("无法解析可用音色")
    if requested_gender != "auto" and chosen["gender"] != requested_gender:
        raise VoiceCatalogError("解析出的音色与用户指定性别不一致")
    return {
        "voice_key": chosen["voice_key"],
        # 保持历史 TTS 契约的 speaker_id 为实际可合成的代号；同时保留
        # voice_clone/get_voice 所需的固定协议值和自定义代号。
        "speaker_id": effective_speaker_id(chosen), "voice_id": effective_speaker_id(chosen),
        "provider_speaker_id": chosen["speaker_id"],
        "custom_speaker_id": chosen.get("custom_speaker_id", ""),
        "display_name": chosen["display_name"], "gender": chosen["gender"], "style_tags": list(chosen["style_tags"]),
        "provider": chosen["provider"], "voice_type": chosen["voice_type"], "model": chosen["model"],
        "resource_id": chosen["resource_id"], "training_status": chosen["training_status"],
        "authorization_status": chosen["authorization_status"], "preview_url": chosen["preview_url"],
        "catalog_version": chosen["catalog_version"],
        "source": source, "requested_gender": requested_gender, "model_error": model_error,
    }


def apply_voice_resolution_to_tts_request(request: Mapping[str, Any], resolution: Mapping[str, Any]) -> dict[str, Any]:
    """移除内部治理字段，并将已校验 speaker 写入 1256 原有 voice_id 契约。"""
    if not isinstance(request, Mapping):
        raise VoiceCatalogError("TTS 请求必须是对象")
    if str(resolution.get("voice_type") or "official").strip().lower() != "official":
        raise VoiceCatalogError("TTS 仅允许官方音色；定制音色/声音复刻已停用")
    if str(resolution.get("custom_speaker_id") or "").strip():
        raise VoiceCatalogError("官方 TTS 不接受 custom_speaker_id")
    resource_id = _string(resolution.get("resource_id", DEFAULT_VOICE_RESOURCE_ID), "voice_resolution.resource_id")
    if resource_id != DEFAULT_VOICE_RESOURCE_ID:
        raise VoiceCatalogError("官方 TTS 仅支持 resource_id=seed-tts-2.0")
    speaker_id = _string(resolution.get("speaker_id"), "voice_resolution.speaker_id")
    result = {key: value for key, value in request.items() if key not in _INTERNAL_TTS_CONFIG_KEYS}
    result["voice_id"] = speaker_id
    result["speaker_id"] = speaker_id
    model = _string(resolution.get("model", DEFAULT_VOICE_MODEL), "voice_resolution.model")
    result["resource_id"] = resource_id
    result["model"] = model
    return result


__all__ = ["CUSTOM_SPEAKER_PROTOCOL_ID", "CUSTOM_VOICE_RESOURCE_IDS", "DEFAULT_TTS_SPEAKER_ID", "DEFAULT_VOICE_CATALOG", "DEFAULT_VOICE_MODEL", "DEFAULT_VOICE_RESOURCE_ID", "VOICE_RESOURCE_BY_TYPE", "VoiceCatalogError", "apply_voice_resolution_to_tts_request", "build_voice_candidates", "build_voice_selection_prompt", "effective_speaker_id", "normalize_custom_speaker_id", "normalize_voice_catalog", "normalize_voice_selection", "resolve_voice_selection", "run_voice_selector"]
