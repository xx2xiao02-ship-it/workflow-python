"""治理对象的版本失效范围。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any


INVALIDATION_RULES: dict[str, list[str]] = {
    "source_text_changed": ["director", "tts", "timeline", "captions", "assets", "edit"],
    # TTS 是时间权威：音色、资源、模型或表演参数变化后，必须显式重跑
    # STT、字幕、镜头时间轴及其数字人/素材绑定，不能只标记一个笼统的
    # ``timeline`` 再让下游自行猜测影响范围。
    "tts_config_changed": [
        "tts", "stt", "timeline", "captions", "shot_timeline",
        "assets", "digital_human", "edit",
    ],
    "tts_result_changed": [
        "stt", "timeline", "captions", "shot_timeline",
        "assets", "digital_human", "edit",
    ],
    "shot_order_changed": ["shot_timeline", "requirements", "assets", "edit"],
    "shot_prompt_changed": ["target_requirement", "target_asset", "target_edit_binding"],
    "asset_rerender_same_contract": ["target_asset", "target_edit_binding"],
    "subtitle_style_changed": ["caption_edit_track"],
    "audio_mix_changed": ["audio_edit_track"],
    "keyframe_changed": ["target_edit_segment"],
    "effect_changed": ["effect_edit_track", "draft_validation"],
}


def invalidation_scope(change_type: str, *, group_id: str = "", shot_id: str = "", requirement_id: str = "") -> dict[str, Any]:
    """返回变更的影响范围，不直接修改任何 Manifest。"""

    if change_type not in INVALIDATION_RULES:
        raise ValueError(f"未知变更类型：{change_type}")
    return {
        "change_type": change_type,
        "scope": list(INVALIDATION_RULES[change_type]),
        "group_id": group_id,
        "shot_id": shot_id,
        "requirement_id": requirement_id,
    }


def build_tts_fingerprint(
    *,
    text: str,
    voice_key: str,
    speaker_id: str,
    resource_id: str,
    model: str,
    speed_ratio: float | int = 1.0,
    performance: dict[str, Any] | None = None,
    audio_sha256: list[str] | tuple[str, ...] = (),
    actual_durations: list[float | int] | tuple[float | int, ...] = (),
) -> str:
    """构造可审计的 TTS 指纹；音频自身观测值不参与时间线推导。"""

    payload = {
        "text": str(text),
        "voice_key": str(voice_key),
        "speaker_id": str(speaker_id),
        "resource_id": str(resource_id),
        "model": str(model),
        "speed_ratio": float(speed_ratio),
        "performance": dict(performance or {}),
        "audio_sha256": [str(item) for item in audio_sha256],
        "actual_durations": [float(item) for item in actual_durations],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def transactional_tts_rebuild(
    current_state: dict[str, Any],
    *,
    new_fingerprint: str,
    rebuild: Any,
    event_sink: Any | None = None,
    group_id: str = "",
    defer_commit: bool = False,
    pending_version: str = "",
) -> dict[str, Any]:
    """执行 TTS 变更的失效与重建事务，失败时保留旧 active 版本。

    ``defer_commit`` 用于异步重建：候选版本创建后先保持旧
    ``active_version``，待下游完整验收后再以默认模式提交。
    """

    if not isinstance(current_state, dict):
        raise ValueError("current_state 必须是对象")
    if not str(new_fingerprint or "").strip():
        raise ValueError("new_fingerprint 不能为空")
    old_state = copy.deepcopy(current_state)
    scope = invalidation_scope("tts_config_changed", group_id=group_id)
    event = {
        "event_id": "tts-invalidation-" + hashlib.sha256(
            f"{datetime.now(timezone.utc).isoformat()}:{new_fingerprint}".encode("utf-8")
        ).hexdigest()[:16],
        "event_type": "tts_config_changed",
        "reason": "TTS 音色/资源/模型或表演参数变更",
        "old_fingerprint": str(old_state.get("tts_fingerprint") or ""),
        "new_fingerprint": str(new_fingerprint),
        "affected": list(scope["scope"]),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    pending_id = str(pending_version or "").strip()
    if pending_id:
        event["pending_version"] = pending_id
    try:
        candidate = rebuild(copy.deepcopy(old_state), scope)
        if not isinstance(candidate, dict):
            raise ValueError("rebuild 必须返回对象状态")
        candidate = copy.deepcopy(candidate)
        if defer_commit:
            # 异步候选尚未完成真实 TTS/STT/素材链路，旧版本仍是唯一 active。
            old_fingerprint = str(
                old_state.get("tts_fingerprint")
                or old_state.get("active_version")
                or ""
            )
            candidate["tts_fingerprint"] = old_fingerprint
            candidate["active_version"] = str(
                old_state.get("active_version") or old_fingerprint
            )
            candidate["pending_version"] = pending_id or str(
                candidate.get("pending_version") or new_fingerprint
            )
            candidate["invalidation"] = {
                **scope,
                "status": "replacement_pending",
                "requested_fingerprint": str(new_fingerprint),
                "pending_version": candidate["pending_version"],
            }
            event["status"] = "replacement_pending"
            if event_sink is not None:
                event_sink(dict(event))
            return {
                "status": "replacement_pending",
                "state": candidate,
                "event": event,
            }
        candidate["tts_fingerprint"] = str(new_fingerprint)
        # 候选状态即使错误地带入旧 active_version，也不能阻止事务切换到
        # 新版本；只有完整 rebuild 成功后才在这里原子更新 active 指针。
        candidate["active_version"] = str(new_fingerprint)
        candidate["invalidation"] = {**scope, "status": "committed", "new_fingerprint": str(new_fingerprint)}
        event["status"] = "committed"
        if event_sink is not None:
            event_sink(dict(event))
        return {"status": "committed", "state": candidate, "event": event}
    except Exception as exc:
        event["status"] = "failed_preserved"
        event["error"] = str(exc)
        if event_sink is not None:
            event_sink(dict(event))
        preserved = copy.deepcopy(old_state)
        preserved["invalidation"] = {
            **scope,
            "status": "failed_preserved",
            "requested_fingerprint": str(new_fingerprint),
            "error": str(exc),
        }
        return {"status": "failed_preserved", "state": preserved, "event": event}


__all__ = [
    "INVALIDATION_RULES",
    "build_tts_fingerprint",
    "invalidation_scope",
    "transactional_tts_rebuild",
]
