"""从原始文案运行冻结的 directors_v2 逻辑，生成可进入 TTS 的完整覆盖分段。

本工具不创建素材或草稿。它明确复用 ``directors_v2_logic`` 的 Unit 切分、
原文覆盖与 8--12 段校验；模型只承担该逻辑要求的两次文本调用。输出随后必须
进入真实 TTS，不能直接被当作镜头数。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import requests

from workflow_1256.directors_v2 import normalize_response
from workflow_1256.directors_v2_logic import DirectorsV2LogicError, run_directors_v2_logic
from workflow_1256.governance.director_runtime_lock import build_director_locked_manifest
from workflow_1256.governance.contracts import MaterialRequirement
from workflow_1256.sound_effect_production import build_sound_effect_plan
from workflow_1256.governance.director_media_route_lock import build_director_media_route_lock
from workflow_1256.governance.media_route_scoring_v3 import (
    apply_v3_route_lock_to_governance,
    build_director_media_route_lock_v3,
)
from workflow_1256.governance.consecutive_static_pacing import (
    apply_consecutive_static_video_pacing as apply_static_pacing_policy,
)
from workflow_1256.cinematic_storyboard_governance import (
    build_cinematic_storyboard_fallback,
    merge_governance_into_story_context,
    run_cinematic_storyboard_governance,
)
from workflow_1256.shot_refinement import run_shot_refinement
from workflow_1256.shot_refinement_transport import Seed21TurboShotRefinementTransport
from workflow_1256.shot_slot_planning import (
    merge_visual_design_into_locked_slots,
    plan_stt_caption_shot_slots,
)
from workflow_1256.shot_visual_arrangement import (
    FEATURE_BATCH_SIZE,
    run_classification_feature_batches,
    run_shot_visual_arrangement,
)
from workflow_1256.story_writer import run_cinematic_story_writer
from workflow_1256.capcut_stt_subtitle_pipeline import run_capcut_stt_subtitle_pipeline
from workflow_1256.voice_director_transport import (
    load_shot_refinement_seed21_turbo_auth_from_document,
)


def _runtime_extra_api_keys(name: str) -> list[str]:
    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _runtime_model_fallbacks(name: str) -> list[str]:
    """读取 API 管理页已配置的模型顺序，不读取或记录任何鉴权信息。"""

    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _is_v31_material_model_failure(error: object) -> bool:
    """Return whether a material-planning error can safely try the next model.

    V3.1 must never fall back to the old classification rules.  A different
    configured model is still a valid model-level failover, but local input or
    assembly errors must stop immediately instead of causing another paid call.
    """

    message = str(error or "")
    return any(
        marker in message
        for marker in (
            "全局视觉规划模型",
            "FEATURE_CONTRACT_INVALID",
            "Seed 2.1 turbo 主/辅鉴权均无法完成",
            "供应商限流",
            "请求超时",
            "网络请求失败",
            "响应格式异常",
            "HTTP ",
        )
    )


def _v31_material_failure_reason(error: str) -> str:
    """压缩模型失败原因，不把供应商正文或鉴权信息写入任务快照。"""

    message = str(error or "").strip()
    if "FEATURE_CONTRACT_INVALID" in message:
        return "FEATURE_CONTRACT_INVALID"
    if "HTTP 429" in message or "供应商限流" in message:
        return "供应商限流（HTTP 429，未判定鉴权无效）"
    if "输出镜头数不一致" in message:
        match = re.search(r"期望\s*\d+\s*[，,]\s*实际\s*\d+", message)
        return "模型输出镜头数不一致" + (f"（{match.group(0)}）" if match else "")
    if "紧凑输出字段数" in message:
        return "模型紧凑输出字段数不符合契约"
    if "未返回 JSON" in message or "响应格式异常" in message:
        return "模型响应格式不符合契约"
    if "请求超时" in message:
        return "请求超时"
    if "网络请求失败" in message:
        return "网络请求失败"
    if "全局视觉规划模型" in message:
        return "模型输出契约失败"
    return message[:240] or "模型调用失败"


def _run_v31_material_planning_with_failover(
    candidate_transport: "DualSeed21TurboTextTransport",
    auth_document: Path,
    runner: Callable[["DualSeed21TurboTextTransport"], dict[str, Any]],
) -> dict[str, Any]:
    """Run one 116616 V3.1 attempt per configured model candidate.

    ``DualSeed21TurboTextTransport`` already fails over transport errors inside
    one call.  This wrapper adds the missing boundary for a provider that
    returns HTTP 200 but violates the global-plan or V3.1 feature contract.
    It never invokes a legacy planner or rewrites the locked shot sequence.
    """

    attempts: list[dict[str, str]] = []
    feature_contract_failed = False
    credential_keys = list(dict.fromkeys(
        str(item[0]).strip() for item in candidate_transport.auths if str(item[0]).strip()
    ))
    for index, candidate in enumerate(candidate_transport.auths):
        role = "主" if index == 0 else f"降级{index}"
        key_index = credential_keys.index(candidate[0]) if candidate[0] in credential_keys else 0
        credential_role = "主鉴权" if key_index == 0 else ("副鉴权" if key_index == 1 else f"备用鉴权{key_index}")
        candidate_label = f"{credential_role} / {candidate[1]}"
        transport = DualSeed21TurboTextTransport(
            auth_document,
            max_tokens=candidate_transport.max_tokens,
            timeout=candidate_transport.timeout,
            json_mode=candidate_transport.json_mode,
            auths=[candidate],
        )
        try:
            result = runner(transport)
        except Exception as exc:  # keep the next candidate available
            result = {"error": f"插件执行异常：{exc}"}
        if not isinstance(result, dict):
            result = {"error": "116616 模型未返回可解析结果"}
        error = str(result.get("error") or "").strip()
        if not error:
            result["model_failover"] = {
                "selected_role": role,
                "attempts": [
                    *attempts,
                    {"role": role, "candidate": candidate_label, "status": "succeeded"},
                ],
            }
            return result
        if "FEATURE_CONTRACT_INVALID" in error:
            feature_contract_failed = True
        reason = _v31_material_failure_reason(error)
        attempts.append({"role": role, "candidate": candidate_label, "status": "failed", "reason": reason})
        if not _is_v31_material_model_failure(error):
            raise RuntimeError(error)

    if attempts:
        detail = "；".join(
            f"{item['role']}（{item['candidate']}）：{item['reason']}"
            for item in attempts
        )
        prefix = "FEATURE_CONTRACT_INVALID：" if feature_contract_failed else ""
        raise RuntimeError(f"{prefix}116616 V3.1 模型依次降级后仍未产出合规结果（{detail}）")
    raise RuntimeError("116616 V3.1 未配置可用模型候选")


def _ordered_feature_auths(auths: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Prefer the configured secondary/fallback models for small feature batches."""

    models = list(dict.fromkeys(model for _key, model in auths if model))
    if len(models) > 1:
        models = models[1:] + models[:1]
    preferred: list[tuple[str, str]] = []
    remaining: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for model in models:
        model_candidates = [candidate for candidate in auths if candidate[1] == model]
        if model_candidates:
            candidate = model_candidates[0]
            preferred.append(candidate)
            seen.add(candidate)
    for model in models:
        for candidate in auths:
            if candidate[1] == model and candidate not in seen:
                remaining.append(candidate)
                seen.add(candidate)
    return preferred + remaining


def _run_v31_feature_evaluation_with_failover(
    candidate_transport: "DualSeed21TurboTextTransport",
    auth_document: Path,
    runner: Callable[["DualSeed21TurboTextTransport"], dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate feature batches with a separate, smaller model-level failover."""

    attempts: list[dict[str, str]] = []
    credential_keys = list(dict.fromkeys(
        str(item[0]).strip() for item in candidate_transport.auths if str(item[0]).strip()
    ))
    for index, candidate in enumerate(candidate_transport.auths):
        key_index = credential_keys.index(candidate[0]) if candidate[0] in credential_keys else 0
        credential_role = "主鉴权" if key_index == 0 else ("副鉴权" if key_index == 1 else f"备用鉴权{key_index}")
        role = "特征主" if index == 0 else f"特征降级{index}"
        candidate_label = f"{credential_role} / {candidate[1]}"
        transport = DualSeed21TurboTextTransport(
            auth_document,
            max_tokens=candidate_transport.max_tokens,
            timeout=candidate_transport.timeout,
            json_mode=candidate_transport.json_mode,
            auths=[candidate],
        )
        try:
            result = runner(transport)
        except Exception as exc:
            result = {"error": f"插件执行异常：{exc}"}
        if not isinstance(result, dict):
            result = {"error": "V3.1 分段特征模型未返回可解析结果"}
        error = str(result.get("error") or "").strip()
        if not error:
            result["feature_model_failover"] = {
                "selected_role": role,
                "attempts": [
                    *attempts,
                    {"role": role, "candidate": candidate_label, "status": "succeeded"},
                ],
            }
            return result
        reason = _v31_material_failure_reason(error)
        attempts.append({"role": role, "candidate": candidate_label, "status": "failed", "reason": reason})
        if not _is_v31_material_model_failure(error):
            raise RuntimeError(error)

    if attempts:
        detail = "；".join(
            f"{item['role']}（{item['candidate']}）：{item['reason']}"
            for item in attempts
        )
        raise RuntimeError(
            f"FEATURE_CONTRACT_INVALID：116616 V3.1 分段特征模型依次降级后仍未产出合规结果（{detail}）"
        )
    raise RuntimeError("FEATURE_CONTRACT_INVALID：116616 未配置可用分段特征模型候选")


class DualSeed21TurboTextTransport:
    """全文导演专用的 Seed 2.1 turbo 主/辅文本 transport。"""

    api_url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

    def __init__(
        self,
        auth_document: Path,
        *,
        max_tokens: int = 5000,
        timeout: float = 180,
        json_mode: bool = True,
        auths: list[tuple[str, str]] | None = None,
    ) -> None:
        env_primary_key = os.environ.get("VOICE_DIRECTOR_PRIMARY_API_KEY", "").strip() or os.environ.get("SHOT_REFINEMENT_PRIMARY_API_KEY", "").strip()
        env_backup_key = os.environ.get("VOICE_DIRECTOR_BACKUP_API_KEY", "").strip() or os.environ.get("SHOT_REFINEMENT_BACKUP_API_KEY", "").strip()
        page_credentials_only = os.environ.get("DIRECTOR_AUTH_DOCUMENT") == ""
        if page_credentials_only:
            # 8768 的 API 管理页显式置空该变量时，不能因为默认文档恰好存在
            # 又补入其备用 Key；否则页面凭据模式会产生不可追踪的旧鉴权回退。
            document_primary = document_backup = document_model = document_backup_model = ""
        else:
            try:
                document_primary, document_backup, document_model, document_backup_model = load_shot_refinement_seed21_turbo_auth_from_document(auth_document)
            except Exception:
                if not env_primary_key:
                    raise
                document_primary = env_primary_key
                document_backup = env_backup_key
                document_model = ""
                document_backup_model = ""
        primary_key = env_primary_key or document_primary
        backup_key = env_backup_key or document_backup
        primary_model = document_model
        backup_model = document_backup_model
        primary_model = os.environ.get("VOICE_DIRECTOR_PRIMARY_MODEL", "").strip() or primary_model
        backup_model = os.environ.get("VOICE_DIRECTOR_BACKUP_MODEL", "").strip() or backup_model
        if not primary_key:
            raise RuntimeError("Seed 2.1 turbo 未配置主 API Key；请补充 VOICE_DIRECTOR_PRIMARY_API_KEY 或现有鉴权文档")
        if not primary_model:
            raise RuntimeError("Seed 2.1 turbo 未配置主模型；请补充 VOICE_DIRECTOR_PRIMARY_MODEL 或现有鉴权文档")
        strategy = os.environ.get("VOICE_DIRECTOR_FAILOVER_STRATEGY", "primary_then_backup").strip().lower()
        self.auths: list[tuple[str, str]] = []
        seen_auths: set[tuple[str, str]] = set()

        def add_auth(api_key: str, model: str) -> None:
            pair = (api_key.strip(), model.strip())
            if pair[0] and pair[1] and pair not in seen_auths:
                self.auths.append(pair)
                seen_auths.add(pair)

        add_auth(primary_key, primary_model)
        if strategy != "primary_only":
            key_candidates = [primary_key]
            if backup_key and backup_model:
                add_auth(backup_key, backup_model)
                key_candidates.append(backup_key)
            for extra_key in _runtime_extra_api_keys("API_MANAGEMENT_EXTRA_KEYS_DIRECTOR_SEED21"):
                add_auth(extra_key, primary_model)
                key_candidates.append(extra_key)
            model_candidates = [primary_model, backup_model]
            model_candidates.extend(
                _runtime_model_fallbacks("API_MANAGEMENT_MODEL_FALLBACKS_DIRECTOR_SEED21")
            )
            for model in dict.fromkeys(value for value in model_candidates if value):
                for api_key in dict.fromkeys(value for value in key_candidates if value):
                    add_auth(api_key, model)
        if auths is not None:
            # 全文导演的两次调用必须由同一个模型完成。调用方在结构校验失败时
            # 才切到下一个模型，避免“路由模型与分段模型混用”造成不可追踪结果。
            self.auths = []
            seen_auths.clear()
            for api_key, model in auths:
                add_auth(api_key, model)
            if not self.auths:
                raise RuntimeError("全文导演未提供可用的模型候选")
        self.api_url = os.environ.get("VOICE_DIRECTOR_API_URL", self.api_url).strip()
        self.max_tokens = max_tokens
        self.timeout = float(timeout)
        self.json_mode = bool(json_mode)

    @staticmethod
    def _failure_reason(error: BaseException) -> str:
        """Return a non-secret failure category for task snapshots and UI diagnostics."""

        message = str(error or "")
        status = re.search(r"\bHTTP\s+(\d{3})\b", message, flags=re.IGNORECASE)
        if status:
            return f"HTTP {status.group(1)}"
        if isinstance(error, requests.exceptions.Timeout) or "timeout" in message.lower():
            return "请求超时"
        if isinstance(error, requests.exceptions.RequestException):
            return "网络请求失败"
        lowered = message.lower()
        if "response" in lowered or "json" in lowered or "content" in lowered:
            return "响应格式异常"
        return type(error).__name__

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        last_error: Exception | None = None
        failure_reasons: list[str] = []
        for index, (api_key, model) in enumerate(self.auths):
            role = "主" if index == 0 else f"备用{index}"
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "thinking": {"type": "disabled"},
                    "temperature": 0.2,
                    "max_tokens": self.max_tokens,
                }
                if self.json_mode:
                    payload["response_format"] = {"type": "json_object"}
                response = requests.post(
                    self.api_url,
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    # 保留服务端可公开返回的诊断摘要，便于判断是模型端点、鉴权、
                    # 参数还是账户状态问题；绝不记录请求头中的密钥。
                    detail = response.text.replace("\r", " ").replace("\n", " ").strip()[:500]
                    raise RuntimeError(f"HTTP {response.status_code}: {detail or '无响应正文'}")
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("响应缺少 message.content")
                return content
            except Exception as exc:  # 主失败后继续使用备用鉴权。
                last_error = exc
                failure_reasons.append(f"{role}：{self._failure_reason(exc)}")
        detail = "；".join(failure_reasons) or "无可用调用详情"
        statuses = [
            int(match.group(1))
            for reason in failure_reasons
            if (match := re.search(r"\bHTTP\s+(\d{3})\b", reason, flags=re.IGNORECASE))
        ]
        if statuses and all(status == 429 for status in statuses):
            prefix = "Seed 2.1 turbo 供应商限流，未判定鉴权无效"
        elif statuses and all(status in {401, 403} for status in statuses):
            prefix = "Seed 2.1 turbo 鉴权失败"
        elif 429 in statuses:
            prefix = "Seed 2.1 turbo 调用失败（含供应商限流，未判定鉴权无效）"
        else:
            prefix = "Seed 2.1 turbo 主/备候选均无法完成全文导演"
        raise RuntimeError(f"{prefix}（{detail}）") from last_error


def _load_text(path: Path) -> str:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise ValueError("输入 JSON 必须包含非空 text")
    text = value["text"].strip()
    if not text:
        raise ValueError("输入 text 不能为空")
    return text


def run_live_director_text(input_path: Path, auth_document: Path) -> dict[str, Any]:
    """只使用冻结的 directors_v2 原文覆盖逻辑，返回其已校验输出。"""

    text = _load_text(input_path)
    candidate_transport = DualSeed21TurboTextTransport(auth_document.resolve())
    last_error: Exception | None = None
    failure_reasons: list[str] = []
    normalized: dict[str, Any] | None = None
    for index, candidate in enumerate(candidate_transport.auths):
        transport = DualSeed21TurboTextTransport(
            auth_document.resolve(),
            max_tokens=candidate_transport.max_tokens,
            timeout=candidate_transport.timeout,
            json_mode=candidate_transport.json_mode,
            auths=[candidate],
        )
        try:
            # pick 与全文分段是一组原子操作：任一调用不可用，或返回结果未通过
            # 8--12 段/原文覆盖校验，都要让出当前模型并尝试下一个候选。
            result = run_directors_v2_logic(
                {"text": text},
                pick_model=transport,
                director_model=transport,
            )
            parsed = normalize_response(result)
            segments = parsed["segments"]
            if not 8 <= len(segments) <= 12:
                raise DirectorsV2LogicError("冻结 directors_v2 逻辑未产出 8--12 个全文分段")
            normalized = parsed
            break
        except (DirectorsV2LogicError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            last_error = exc
            role = "主" if index == 0 else f"降级{index}"
            if isinstance(exc, DirectorsV2LogicError):
                reason = "结构校验失败"
            else:
                reason = transport._failure_reason(exc)
            failure_reasons.append(f"{role}：{reason}")
    if normalized is None:
        detail = "；".join(failure_reasons) or "无可用模型候选"
        raise RuntimeError(f"全文导演模型依次降级后仍未产出合规分段（{detail}）") from last_error
    return {
        "evidence_level": "live_seed_directors_v2_full_text",
        "source_input": str(input_path.resolve()),
        "source_text_chars": len(text),
        "director_output": normalized,
    }


def run_live_tts(
    director_result: dict[str, Any],
    auth_document: Path,
    output_dir: Path,
    *,
    voice_key: str = "",
) -> dict[str, Any]:
    """对全文覆盖的导演分段执行真实 TTS；TTS 只生成音频，不生成时间线。"""

    from run_token_assets_live import generate_tts

    segments = director_result["director_output"]["segments"]
    manifest = {
        "voice_key": voice_key,
        "shots": [
            {"shot_id": f"g{index:02d}", "narration_text": text}
            for index, text in enumerate(segments, start=1)
        ],
    }
    state = generate_tts(manifest, auth_document.resolve(), output_dir.resolve())
    contract = _tts_contract_view(state)
    paths = contract["audio"].get("paths")
    if not isinstance(paths, list) or len(paths) != len(segments):
        raise RuntimeError("真实 TTS 未返回与全文分段一一对应的音频路径")
    # 不能把火山方舟返回的 duration 当成字幕或镜头的时间来源。音频生成完成后，
    # 由剪映 STT 独立项目重新读取音频并冻结唯一时间线。
    return _tts_contract_only(state)


def _tts_contract_view(tts: Mapping[str, Any]) -> dict[str, Any]:
    """读取版本化 TTS 契约；旧快照只能通过这里的迁移适配器读取。"""

    raw_contract = tts.get("tts_contract")
    if isinstance(raw_contract, Mapping):
        voice = raw_contract.get("voice") if isinstance(raw_contract.get("voice"), Mapping) else {}
        audio = raw_contract.get("audio") if isinstance(raw_contract.get("audio"), Mapping) else {}
        voice_data = dict(voice)
        return {
            "schema_version": str(raw_contract.get("schema_version") or "tts-voice-binding-v1"),
            "voice": voice_data,
            "audio": {
                "paths": list(audio.get("paths") or []),
                "sha256": list(audio.get("sha256") or []),
                "actual_durations_s": [float(item) for item in (audio.get("actual_durations_s") or [])],
            },
            "auth_slots": list(raw_contract.get("auth_slots") or []),
            "voice_bindings": [dict(item) for item in (raw_contract.get("voice_bindings") or []) if isinstance(item, Mapping)],
            "timing_authority": str(raw_contract.get("timing_authority") or "capcut_stt_required"),
        }

    # 迁移适配器：仅为历史任务读取旧平铺字段，新任务不会从这里写入。
    resolution = tts.get("tts_voice_resolution") if isinstance(tts.get("tts_voice_resolution"), Mapping) else {}
    voice_data: dict[str, Any] = {
        "voice_key": str(tts.get("tts_voice_key") or resolution.get("voice_key") or ""),
        "speaker_id": str(tts.get("tts_speaker_id") or resolution.get("speaker_id") or ""),
        "resource_id": str(tts.get("tts_resource_id") or resolution.get("resource_id") or ""),
        "model": str(tts.get("tts_model") or resolution.get("model") or ""),
        "voice_type": str(tts.get("tts_voice_type") or resolution.get("voice_type") or "official"),
        "authorization_status": str(tts.get("tts_authorization_status") or resolution.get("authorization_status") or "unknown"),
        "training_status": str(tts.get("tts_training_status") or resolution.get("training_status") or "ready"),
    }
    # 历史快照允许被读取用于审计，但锁定稿不再传播定制音色字段。
    if voice_data["voice_type"].strip().lower() != "official" or str(voice_data["resource_id"]).strip() != "seed-tts-2.0":
        raise RuntimeError("TTS 契约包含已停用的定制音色或 ICL 资源；当前仅保留官方 TTS")
    return {
        "schema_version": "tts-voice-binding-v1",
        "voice": voice_data,
        "audio": {
            "paths": list(tts.get("tts_paths") or []),
            "sha256": list(tts.get("tts_audio_sha256") or []),
            "actual_durations_s": [float(item) for item in (tts.get("tts_actual_durations") or tts.get("tts_durations") or [])],
        },
        "auth_slots": list(tts.get("tts_auth_slots") or []),
        "voice_bindings": [dict(item) for item in (tts.get("tts_voice_bindings") or []) if isinstance(item, Mapping)],
        "timing_authority": str(tts.get("timing_authority") or "capcut_stt_required"),
    }


def _tts_contract_only(tts: Mapping[str, Any]) -> dict[str, Any]:
    """把 TTS 结果收口为新任务契约，保留缓存等非契约元数据。"""

    output: dict[str, Any] = {
        "tts_contract": _tts_contract_view(tts),
        "timing_authority": "capcut_stt_required",
    }
    for key in ("cache", "evidence_level", "performance"):
        if key in tts and tts.get(key) not in (None, "", [], {}):
            output[key] = tts[key]
    return output


def _require_capcut_stt_timelines(director_result: dict[str, Any]) -> tuple[list[dict[str, int]], dict[str, int]]:
    """读取唯一的剪映 STT 时间线，拒绝回退到 TTS 或旧时间线。"""

    subtitle = director_result.get("subtitle")
    if not isinstance(subtitle, dict):
        raise RuntimeError("必须先由剪映 STT 独立项目生成字幕时间线")
    raw_groups = subtitle.get("group_timelines")
    raw_total = subtitle.get("total_timeline")
    if not isinstance(raw_groups, list) or not isinstance(raw_total, dict):
        raise RuntimeError("剪映 STT 未返回完整分段/总时间线")
    groups: list[dict[str, int]] = []
    previous_end = 0
    for index, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"剪映 STT 分段时间线 g{index:02d} 不是对象")
        start = raw.get("start")
        end = raw.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or end <= start:
            raise RuntimeError(f"剪映 STT 分段时间线 g{index:02d} 无效")
        if start != previous_end:
            raise RuntimeError("剪映 STT 分段时间线存在空洞或重叠")
        groups.append({"start": start, "end": end})
        previous_end = end
    total_start = raw_total.get("start")
    total_end = raw_total.get("end")
    if total_start != 0 or not isinstance(total_end, int) or total_end != previous_end:
        raise RuntimeError("剪映 STT 总时间线未完整覆盖全部分段")
    return groups, {"start": 0, "end": total_end}


def run_live_subtitle_pipeline(director_result: dict[str, Any]) -> dict[str, Any]:
    """在镜头细化前完成一次 TTS -> STT，并冻结新的镜头坑位基础数据。"""

    tts = director_result.get("tts")
    if not isinstance(tts, dict):
        raise RuntimeError("必须先完成真实 TTS 才能生成 STT 字幕和镜头坑位")
    output = director_result.get("director_output")
    if not isinstance(output, dict):
        raise RuntimeError("字幕生成缺少 director_output")
    tts_paths = _tts_contract_view(tts)["audio"].get("paths")
    segments = output.get("segments") or []
    if not isinstance(tts_paths, list) or len(tts_paths) != len(segments):
        raise RuntimeError("真实 TTS 音频路径与全文分段必须一一对应")
    subtitle = run_capcut_stt_subtitle_pipeline(
        segment_text=segments,
        audio_sources=tts_paths,
    )
    timelines, _total_timeline = _require_capcut_stt_timelines({"subtitle": subtitle})
    subtitle["shot_slot_plan"] = plan_stt_caption_shot_slots(
        subtitle["captions"], timelines, min_seconds=2.5, max_seconds=5.0
    )
    return subtitle


def run_live_shot_refinement(director_result: dict[str, Any], auth_document: Path) -> dict[str, Any]:
    """以 STT 字幕坑位驱动 197742/127095，逐段保留顺序。"""

    started_at = time.perf_counter()
    output = director_result["director_output"]
    segments = output["segments"]
    beats = output["segment_beats"]
    timelines, _total_timeline = _require_capcut_stt_timelines(director_result)
    if not (len(segments) == len(beats) == len(timelines)):
        raise RuntimeError("导演分段、节拍与剪映 STT 时间线数量不一致")
    subtitle = director_result.get("subtitle")
    if not isinstance(subtitle, dict) or not isinstance(subtitle.get("shot_slot_plan"), dict):
        raise RuntimeError("必须先完成 TTS -> STT 字幕和 2.5–5 秒镜头坑位规划")
    env_primary = os.environ.get("SHOT_REFINEMENT_PRIMARY_API_KEY", "").strip() or os.environ.get("VOICE_DIRECTOR_PRIMARY_API_KEY", "").strip()
    env_backup = os.environ.get("SHOT_REFINEMENT_BACKUP_API_KEY", "").strip() or os.environ.get("VOICE_DIRECTOR_BACKUP_API_KEY", "").strip()
    page_credentials_only = os.environ.get("DIRECTOR_AUTH_DOCUMENT") == ""
    if page_credentials_only:
        document_primary = document_backup = document_model = ""
    else:
        try:
            document_primary, document_backup, document_model, _ = load_shot_refinement_seed21_turbo_auth_from_document(auth_document)
        except Exception:
            if not env_primary:
                raise
            document_primary, document_backup, document_model = env_primary, env_backup, ""
    primary = env_primary or document_primary
    backup = env_backup or document_backup
    # API 管理页将编导通道的第一个模型保存为
    # VOICE_DIRECTOR_PRIMARY_MODEL；镜头精细化仍属于同一通道，未单独
    # 配置 SHOT_REFINEMENT_MODEL 时必须继承该主模型，不能把已配置模型
    # 误判为缺失。后续模型级回退仍由 API_MANAGEMENT_MODEL_FALLBACKS_*
    # 保持原有顺序处理。
    model = (
        os.environ.get("SHOT_REFINEMENT_MODEL", "").strip()
        or os.environ.get("VOICE_DIRECTOR_PRIMARY_MODEL", "").strip()
        or document_model
    )
    if not model:
        raise RuntimeError("镜头精细化未配置 Seed 2.1 turbo 模型")
    failover_strategy = os.environ.get("SHOT_REFINEMENT_FAILOVER_STRATEGY", "primary_then_backup").strip().lower()
    transport = Seed21TurboShotRefinementTransport(
        api_key=primary,
        backup_api_key="" if failover_strategy == "primary_only" else backup,
        api_url=os.environ.get("SHOT_REFINEMENT_API_URL", "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
        model=model,
        fallback_models=tuple(
            candidate
            for candidate in _runtime_model_fallbacks("API_MANAGEMENT_MODEL_FALLBACKS_DIRECTOR_SEED21")
            if candidate != model
        ),
        max_attempts_per_auth=int(os.environ.get("SHOT_REFINEMENT_MAX_ATTEMPTS", "3")),
        extra_api_keys=() if failover_strategy == "primary_only" else tuple(_runtime_extra_api_keys("API_MANAGEMENT_EXTRA_KEYS_DIRECTOR_SEED21")),
    )
    slot_plan = subtitle["shot_slot_plan"]
    slot_groups = slot_plan["groups"]
    if len(slot_groups) != len(segments):
        raise RuntimeError("STT 镜头坑位分组数量与导演大分段数量不一致")
    items = [
        {
            "source_text": segment,
            "visual_story": beat["segment_goal"],
            "visual_core": output["director_plan"]["core"],
            "narrative_role": beat["rhythm"],
            "required_shot_count": slot_group["required_shot_count"],
            "locked_shot_slots": slot_group["slots"],
            "pacing_policy": {
                "source": "capcut_stt_captions",
                "merge_below_seconds": 2.5,
                "split_above_seconds": 5.0,
                "rhythm_target": beat["rhythm"],
            },
        }
        for segment, beat, slot_group in zip(
            segments, beats, slot_groups, strict=True
        )
    ]
    refinement_input = {
        "duration": [(item["end"] - item["start"]) / 1_000_000 for item in timelines],
        "items": items,
        "segments": segments,
        "timelines": timelines,
    }

    def code_transport(item: Any, llm_result: dict[str, Any]) -> dict[str, Any]:
        return merge_visual_design_into_locked_slots(item.item["locked_shot_slots"], llm_result)

    result = run_shot_refinement(
        refinement_input,
        llm_transport=transport,
        code_transport=code_transport,
    )
    small_shot_count = sum(len(group["shots"]) for group in result["Code_list"])
    expected_shot_count = sum(int(group["required_shot_count"]) for group in slot_groups)
    if small_shot_count != expected_shot_count:
        raise RuntimeError("镜头精细化输出数量与 STT 冻结坑位数量不一致")
    over_limit = []
    under_limit = []
    narration_errors = []
    for group_index, group in enumerate(result["Code_list"], start=1):
        source_segment = "".join(slot["narration_text"] for slot in slot_groups[group_index - 1]["slots"])
        narration_parts = [shot.get("narration_text") for shot in group["shots"]]
        if not all(isinstance(part, str) and part.strip() for part in narration_parts):
            narration_errors.append(f"g{group_index:02d}: 缺少 narration_text")
        elif "".join(narration_parts).strip() != source_segment.strip():
            narration_errors.append(f"g{group_index:02d}: narration_text 未完整连续覆盖原旁白")
        for shot_index, timeline in enumerate(group["timelines"], start=1):
            duration_us = int(timeline["end"]) - int(timeline["start"])
            if duration_us > 5_000_000:
                over_limit.append(f"g{group_index:02d}_s{shot_index:02d}:{duration_us / 1_000_000:.3f}s")
            if duration_us < 2_500_000 and not any(
                slot.get("allow_short") and slot["timeline"]["start"] == timeline["start"] and slot["timeline"]["end"] == timeline["end"]
                for slot in slot_groups[group_index - 1]["slots"]
            ):
                under_limit.append(f"g{group_index:02d}_s{shot_index:02d}:{duration_us / 1_000_000:.3f}s")
    if over_limit:
        raise RuntimeError("镜头精细化仍存在超过 5 秒的镜头：" + "、".join(over_limit))
    if under_limit:
        raise RuntimeError("镜头精细化存在低于 2.5 秒的非语义切点：" + "、".join(under_limit))
    if narration_errors:
        raise RuntimeError("镜头精细化旁白切分不合格：" + "、".join(narration_errors))
    raw_parallel_workers = os.environ.get("DIRECTOR_SHOT_REFINEMENT_PARALLEL_WORKERS", "").strip()
    try:
        parsed_parallel_workers = int(raw_parallel_workers) if raw_parallel_workers else 4
    except ValueError:
        parsed_parallel_workers = 4
    refinement_parallel_workers = parsed_parallel_workers if parsed_parallel_workers > 0 else 4
    return {
        "evidence_level": "live_seed21_turbo_shot_refinement",
        "output": result,
        "small_shot_count": small_shot_count,
        "pacing_plan": slot_plan["pacing_plan"],
        "slot_plan": slot_plan,
        "performance": {
            "shot_refinement_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "shot_refinement_parallel_workers": refinement_parallel_workers,
        },
    }


def run_live_story_draft(
    director_result: dict[str, Any],
    input_path: Path,
    auth_document: Path,
    *,
    protagonist_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """为当前全文分段生成待用户审核的连续默片故事，不擅自锁定。"""

    timelines, _total_timeline = _require_capcut_stt_timelines(director_result)
    transport = DualSeed21TurboTextTransport(auth_document.resolve())
    profile = dict(protagonist_profile) if isinstance(protagonist_profile, Mapping) else {"mode": "auto"}
    profile.setdefault("mode", "auto")
    profile.setdefault("name", "")
    profile.setdefault("gender", "auto")
    profile.setdefault("age_group", "auto")
    profile.setdefault("identity", "")
    protagonist_name = str(profile.get("name") or "").strip()
    hard_feedback = (
        "硬性修正：必须遵守输入的主角身份规则；原文有具名核心人物时不得换成虚构角色，"
        "也不得沿用林夏或任何历史默认人物。全片无对白默片。禁止出现可读文字、"
        "字幕、对话框、聊天窗口、弹窗、通知、进度条、输入框、UI、品牌标识或任何"
        "要求观众阅读的屏幕内容。不要让角色用积木逐字拼句子；只用动作、物件、空间、"
        "光影和人物反应表达概念。"
    )
    banned = ("对话", "弹窗", "进度条", "输入框", "UI", "可读", "字样", "聊天窗口", "通知")
    last_error: Exception | None = None
    for attempt in range(2):
        story = run_cinematic_story_writer(
            _load_text(input_path),
            director_result["director_output"]["segments"],
            timelines=timelines,
            transport=transport,
            review_feedback=hard_feedback + (" 上一版仍违反硬约束，必须完全重写。" if attempt else ""),
            protagonist_name=protagonist_name,
            strict_silent_film=True,
            protagonist_profile=profile,
        )
        rendered = json.dumps(story, ensure_ascii=False)
        if (not protagonist_name or protagonist_name in story["movie_outline"]["protagonist"]) and not any(token in rendered for token in banned):
            break
        last_error = RuntimeError("故事违反主角身份或无文字默片硬约束")
    else:
        raise RuntimeError("连续两次故事生成都违反主角身份/无文字默片硬约束") from last_error
    story["protagonist_profile"] = {
        **profile,
        "resolution_source": "user_setting" if profile.get("mode") == "manual" else "auto_from_approved_copy",
        "resolved_anchor": story["movie_outline"]["protagonist"],
    }
    return {
        "evidence_level": "live_seed21_turbo_cinematic_story",
        "review_status": "PENDING_USER_REVIEW",
        "story": story,
    }


def _narration_voice_binding(
    voice_bindings: list[Mapping[str, Any]],
    index: int,
    voice: Mapping[str, Any],
) -> dict[str, Any]:
    """生成不泄露凭据的旁白音色绑定；协议字段仅在有值时写入。"""

    source = voice_bindings[index - 1] if index - 1 < len(voice_bindings) else {}
    source = source if isinstance(source, Mapping) else {}
    binding: dict[str, Any] = {
        "speaker_id": str(source.get("speaker_id") or voice.get("speaker_id") or "").strip(),
        "resource_id": str(source.get("resource_id") or voice.get("resource_id") or "").strip(),
        "model": str(source.get("model") or voice.get("model") or "").strip(),
    }
    if binding["resource_id"] != "seed-tts-2.0":
        raise RuntimeError("旁白音色绑定不是官方 TTS 2.0 资源")
    return binding


def build_approved_director_lock(director_result: dict[str, Any], input_path: Path, *, include_sound_effect_plan: bool = False) -> dict[str, Any]:
    """把已审核的故事与真实 TTS/小镜头收口成不可变编导交接稿。"""

    tts = director_result.get("tts")
    refinement = director_result.get("shot_refinement")
    story_draft = director_result.get("story_draft")
    if not isinstance(tts, dict) or not isinstance(refinement, dict) or not isinstance(story_draft, dict):
        raise RuntimeError("锁定前必须已有真实 TTS、镜头精细化和故事草案")
    story = story_draft.get("story")
    if not isinstance(story, dict):
        raise RuntimeError("故事草案缺少可审核 story")
    director_output = director_result["director_output"]
    subtitle = director_result.get("subtitle")
    if not isinstance(subtitle, dict) or not isinstance(subtitle.get("pipeline"), dict):
        raise RuntimeError("锁定前必须先完成 TTS -> STT 字幕和镜头坑位规划")
    timelines, total_timeline = _require_capcut_stt_timelines(director_result)
    tts_contract = _tts_contract_view(tts)
    tts_voice = tts_contract["voice"]
    if str(tts_voice.get("voice_type") or "official").strip().lower() != "official" or str(tts_voice.get("resource_id") or "").strip() != "seed-tts-2.0":
        raise RuntimeError("锁定稿只允许官方 TTS 音色；定制音色/声音复刻已停用")
    tts_audio = tts_contract["audio"]
    tts_paths = tts_audio.get("paths")
    if not isinstance(tts_paths, list) or len(tts_paths) != len(timelines):
        raise RuntimeError("真实 TTS 缺少与分段一一对应的本地音频路径")
    source_text = _load_text(input_path)
    fingerprint_payload = {
        "segments": director_output.get("segments", []),
        "voice_key": tts_voice.get("voice_key", ""),
        "speaker_id": tts_voice.get("speaker_id", ""),
        "resource_id": tts_voice.get("resource_id", ""),
        "model": tts_voice.get("model", ""),
        "speed_ratio": 1.1,
        "performance": {"loudness_rate": 0, "pitch": 0, "silence_duration": 0},
        "audio_sha256": tts_audio.get("sha256", []),
        "actual_durations": tts_audio.get("actual_durations_s", []),
        "stt_timelines": timelines,
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    raw_auth_slots = tts_contract.get("auth_slots")
    auth_slots = raw_auth_slots if isinstance(raw_auth_slots, list) else []
    raw_voice_bindings = tts_contract.get("voice_bindings")
    voice_bindings = raw_voice_bindings if isinstance(raw_voice_bindings, list) else []
    narration_assets = [
        {
            "asset_id": f"g{index:02d}.audio.narration",
            "requirement_id": f"g{index:02d}.audio.narration",
            "group_id": f"g{index:02d}",
            "timeline": timeline,
            "local_path": tts_paths[index - 1],
            "source_node": "159953",
            "voice_key": tts_voice.get("voice_key", ""),
            "speaker_id": tts_voice.get("speaker_id", ""),
            "resource_id": tts_voice.get("resource_id", ""),
            "model": tts_voice.get("model", ""),
            "audio_sha256": (tts_audio.get("sha256") or [""])[index - 1],
            "actual_duration_s": (tts_audio.get("actual_durations_s") or [0])[index - 1],
            # 只保留鉴权槽位和音色绑定的非敏感审计字段；绝不把 API Key
            # 或完整响应写入锁定稿。槽位切换不参与时间线推导，时间线仍来自 STT。
            "auth_slot": (
                str(auth_slots[index - 1]).strip()
                if index - 1 < len(auth_slots)
                and str(auth_slots[index - 1]).strip() in {"primary", "backup"}
                else "primary"
            ),
            "voice_binding": _narration_voice_binding(voice_bindings, index, tts_voice),
        }
        for index, timeline in enumerate(timelines, start=1)
    ]
    manifest = build_director_locked_manifest(
        director_output=director_output,
        tts_group_timelines=timelines,
        total_timeline=total_timeline,
        shot_groups=refinement["output"]["Code_list"],
        caption_segments=subtitle["new_segments"],
        caption_timelines=subtitle["new_timelines"],
        project_id="token-" + hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:12],
        run_id="locked-token-20260810",
        plan_version="director-locked-live-v1",
        tts_fingerprint=fingerprint,
        cinematic_story=story,
        story_review_status="APPROVED",
        story_revision=1,
        narration_assets=narration_assets,
        subtitle_pipeline=subtitle["pipeline"],
        sound_effect_plan=sound_effect_plan,
    )
    if include_sound_effect_plan:
        plan_input = [
            {
                "shot_id": shot.shot_id,
                "clip_role": shot.clip_role,
                "timeline": {"start_us": shot.timeline.start_us, "end_us": shot.timeline.end_us},
                "sound_effect_subject": shot.production_spec.get("sound_effect_subject", ""),
            }
            for shot in manifest.shots
        ]
        # v3.2 仅在锁定稿阶段增加计划；它不改变任何视觉路由或时间线。
        manifest.sound_effect_plan = build_sound_effect_plan(plan_input)
        for row in manifest.sound_effect_plan:
            shot_id = str(row.get("shot_id") or "")
            shot = next(item for item in manifest.shots if item.shot_id == shot_id)
            manifest.requirements.append(MaterialRequirement(
                f"{shot_id}.audio.sound_effect", "shot", "audio", "sound_effect",
                shot.group_id, [shot_id], bool(row.get("enabled", False)),
                "sound_effect_production", "fit_to_shot", "sound_effect_production",
                shot.timeline, {"cue_start_us": row.get("cue_start_us"), "cue_end_us": row.get("cue_end_us"), "status": row.get("status", "")},
            ))
    result = manifest.to_dict()
    if len(result["shots"]) != refinement["small_shot_count"]:
        raise RuntimeError("DirectorLockedManifest 小镜头数量与精细化输出不一致")
    return result


def apply_consecutive_static_video_pacing(
    director_result: dict[str, Any], input_path: Path,
) -> dict[str, Any]:
    """在进入 116616 前，以连续静态候选修正视觉槽位。

    先由纯程序治理骨架识别当前媒体路线；若命中连续 4 张图片，则只合并
    同一大分段内且不超过 5.5 秒的视觉槽位，或提升一个原生时长的视频候选。
    剪映 STT 字幕、其时间线与全文导演分段均不改写。
    """

    refinement = director_result.get("shot_refinement")
    lock = director_result.get("director_lock")
    if not isinstance(refinement, dict) or not isinstance(lock, dict):
        raise RuntimeError("连续静态纠偏需要镜头精细化结果与 DirectorLockedManifest")
    output = refinement.get("output")
    code_list = output.get("Code_list") if isinstance(output, dict) else None
    if not isinstance(code_list, list):
        raise RuntimeError("连续静态纠偏缺少镜头精细化 Code_list")
    preliminary_governance = build_cinematic_storyboard_fallback(
        lock,
        reason="先识别连续静态候选，再执行不改字幕的视觉槽位纠偏",
    )
    preliminary_route_lock = build_director_media_route_lock(lock, preliminary_governance)
    pacing = apply_static_pacing_policy(code_list, lock, preliminary_route_lock)
    policy = pacing["policy"]
    if int(policy["result_visual_slot_count"]) == int(policy["original_visual_slot_count"]):
        refinement["media_pacing"] = policy
        return policy
    updated_output = dict(output)
    updated_output["Code_list"] = pacing["code_list"]
    refinement["output"] = updated_output
    refinement["small_shot_count"] = int(policy["result_visual_slot_count"])
    refinement["media_pacing"] = policy
    # 重新锁定视觉槽位，字幕仍然从同一份 CapCut STT pipeline 读取。
    director_result["director_lock"] = build_approved_director_lock(
        director_result,
        input_path,
        include_sound_effect_plan=str(director_result.get("director_package") or "").strip().lower() == "v3_2",
    )
    return policy


def run_live_material_planning(
    director_result: dict[str, Any], auth_document: Path, *, aspect_ratio: str = "9:16",
    director_package: str = "v2", dynamic_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """对锁定小镜头运行 116616，生成首帧提示词；不改变锁定时间线。"""

    started_at = time.perf_counter()
    lock = director_result.get("director_lock")
    refinement = director_result.get("shot_refinement")
    if not isinstance(lock, dict) or lock.get("status") != "DIRECTOR_LOCKED":
        raise RuntimeError("素材提示词只接受已批准的 DirectorLockedManifest")
    if not isinstance(refinement, dict):
        raise RuntimeError("缺少镜头精细化结果")
    model = DualSeed21TurboTextTransport(auth_document.resolve())

    governance = director_result.get("director_governance")
    if not isinstance(governance, dict):
        # 素材生产路径采用“先锁镜头语言骨架，再由 116616 增润画面”的顺序。
        # 电影化治理模型仍可由 --director-governance 单独调用和审核，但不再让
        # 其结构化输出成为素材链的单点故障或长时间重试源。
        governance = build_cinematic_storyboard_fallback(
            lock,
            reason="素材路径使用程序锁定的镜头语言骨架；画面细节交由 116616 方舟增润",
        )
    package_id = str(director_package or "v2").strip().lower()
    if package_id == "v3":
        media_route_lock = None
    else:
        media_route_lock = build_director_media_route_lock(lock, governance)
    story_context = merge_governance_into_story_context(lock["cinematic_story"], governance)
    arrangement_input = {
        "Code_list": refinement["output"]["Code_list"],
        "segments": director_result["director_output"]["segments"],
        "story_context": story_context,
        "aspect_ratio": aspect_ratio,
        "debug": True,
    }

    def run_arrangement(active_model: DualSeed21TurboTextTransport) -> dict[str, Any]:
        def transport(payload: dict[str, Any]) -> str:
            messages = payload.get("messages")
            if isinstance(messages, list) and len(messages) >= 2:
                return active_model(str(messages[0].get("content") or ""), str(messages[1].get("content") or ""))
            inputs = payload.get("input")
            if isinstance(inputs, list) and len(inputs) >= 2:
                system = inputs[0]["content"][0]["text"]
                user = inputs[1]["content"][0]["text"]
                return active_model(str(system), str(user))
            raise RuntimeError("116616 模型请求结构无法识别")

        return run_shot_visual_arrangement(
            arrangement_input,
            gpt_transport=transport,
            mini_transport=transport,
            require_classification_features=False,
        )

    if package_id == "v3":
        # 全局规划与逐段特征是两个独立契约：全局调用只生成精简 p，
        # 特征评估复用已有 segments/Code_list 分段，避免一条响应同时承载
        # 全片规划、8维分数和证据。
        result = _run_v31_material_planning_with_failover(
            model,
            auth_document.resolve(),
            lambda active_model: run_arrangement(active_model),
        )
        feature_auths = _ordered_feature_auths(model.auths)
        feature_max_tokens = min(model.max_tokens, 2400)
        feature_model = DualSeed21TurboTextTransport(
            auth_document.resolve(),
            max_tokens=feature_max_tokens,
            timeout=model.timeout,
            json_mode=model.json_mode,
            auths=feature_auths,
        )

        def run_feature_batches(active_model: DualSeed21TurboTextTransport) -> dict[str, Any]:
            def transport(payload: dict[str, Any]) -> str:
                messages = payload.get("messages")
                if isinstance(messages, list) and len(messages) >= 2:
                    return active_model(str(messages[0].get("content") or ""), str(messages[1].get("content") or ""))
                inputs = payload.get("input")
                if isinstance(inputs, list) and len(inputs) >= 2:
                    system = inputs[0]["content"][0]["text"]
                    user = inputs[1]["content"][0]["text"]
                    return active_model(str(system), str(user))
                raise RuntimeError("116616 分段特征模型请求结构无法识别")

            try:
                features, feature_debug = run_classification_feature_batches(
                    arrangement_input,
                    transport=transport,
                    batch_size=FEATURE_BATCH_SIZE,
                )
            except Exception as exc:
                return {"error": str(exc)}
            return {
                "classification_features": features,
                "feature_debug": feature_debug,
            }

        feature_result = _run_v31_feature_evaluation_with_failover(
            feature_model,
            auth_document.resolve(),
            run_feature_batches,
        )
        raw_features = feature_result.get("classification_features")
        if not isinstance(raw_features, list) or len(raw_features) != len(lock["shots"]):
            raise RuntimeError("FEATURE_CONTRACT_INVALID: 116616 分段特征未返回与冻结镜头一一对应的审计")
        if isinstance(result.get("debug"), dict) and isinstance(feature_result.get("feature_debug"), dict):
            result["debug"].update(feature_result["feature_debug"])
        result["feature_model_failover"] = feature_result.get("feature_model_failover", {})
        result["classification_features"] = raw_features
    else:
        result = run_arrangement(model)
    if result.get("error"):
        prefix = "116616 V3.1 素材提示词失败：" if package_id == "v3" else "116616 素材提示词失败："
        raise RuntimeError(prefix + str(result["error"]))
    if package_id == "v3":
        raw_features = result.get("classification_features")
        if not isinstance(raw_features, list) or len(raw_features) != len(lock["shots"]):
            raise RuntimeError("FEATURE_CONTRACT_INVALID: 116616 未返回与冻结镜头一一对应的特征审计")
        feature_by_id = {str(item.get("shot_id") or ""): item for item in raw_features if isinstance(item, Mapping)}
        updated_governance = copy.deepcopy(governance)
        updated_contexts = updated_governance.get("shot_contexts")
        if not isinstance(updated_contexts, list) or len(updated_contexts) != len(lock["shots"]):
            raise RuntimeError("FEATURE_CONTRACT_INVALID: 治理上下文与冻结镜头数量不一致")
        for context, shot in zip(updated_contexts, lock["shots"], strict=True):
            shot_id = str(shot.get("shot_id") or "")
            feature = feature_by_id.get(shot_id)
            if not isinstance(context, dict) or not isinstance(feature, dict):
                raise RuntimeError(f"FEATURE_CONTRACT_INVALID: {shot_id} 缺少特征审计")
            context["classification_features"] = copy.deepcopy(feature)
        # build_director_media_route_lock_v3 会再次严格校验值域、证据、ID、
        # 顺序和微秒时间线；任何不合规都明确失败，不使用旧规则回退。
        media_route_lock = build_director_media_route_lock_v3(lock, updated_governance, dynamic_params)
        governance = apply_v3_route_lock_to_governance(updated_governance, media_route_lock)
    if len(result.get("prompt") or []) != len(lock["shots"]):
        raise RuntimeError("116616 提示词数量与锁定小镜头数量不一致")
    if result.get("timelines") != [{"start": shot["timeline"]["start_us"], "end": shot["timeline"]["end_us"]} for shot in lock["shots"]]:
        raise RuntimeError("116616 输出时间线与 DirectorLockedManifest 不一致")
    output = {
        "evidence_level": "live_seed21_turbo_shot_visual_arrangement",
        "director_governance": governance,
        "media_route_lock": media_route_lock,
        "director_package": package_id,
        "director_dynamic_params": dict(dynamic_params or {}) if package_id == "v3" else {},
        **result,
    }
    debug = output.get("debug") if isinstance(output.get("debug"), Mapping) else {}
    output["performance"] = {
        "material_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
        "material_debug_total_ms": debug.get("total_ms"),
        "feature_elapsed_ms": debug.get("feature_elapsed_ms"),
        "feature_parallel_workers": debug.get("feature_parallel_workers"),
    }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--auth-document", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--director-output", type=Path, help="复用同一原文已校验的导演结果，避免重复模型调用")
    parser.add_argument("--tts-output-dir", type=Path)
    parser.add_argument(
        "--voice-key",
        default="",
        help="TTS 使用版本化音色目录中的 voice_key；未填写时沿用目录默认音色",
    )
    parser.add_argument("--refine-shots", action="store_true")
    parser.add_argument("--generate-story", action="store_true")
    parser.add_argument("--approve-story", action="store_true", help="仅在用户审核同意当前故事后使用")
    parser.add_argument("--director-governance", action="store_true", help="生成导演书、连续性圣经、节拍序列和逐镜头导演复审")
    parser.add_argument("--director-governance-input", type=Path, help="复用已通过复审的导演治理 JSON")
    parser.add_argument("--material-plan", action="store_true")
    parser.add_argument("--export-grid-input-dir", type=Path)
    args = parser.parse_args()
    result = (
        json.loads(args.director_output.read_text(encoding="utf-8"))
        if args.director_output is not None
        else run_live_director_text(args.input, args.auth_document)
    )
    if not isinstance(result, dict) or not isinstance(result.get("director_output"), dict):
        raise ValueError("--director-output 必须是本工具生成的导演结果 JSON")
    if args.tts_output_dir is not None:
        result["tts"] = run_live_tts(
            result,
            args.auth_document,
            args.tts_output_dir,
            voice_key=args.voice_key,
        )
    if (args.refine_shots or args.approve_story) and isinstance(result.get("tts"), dict) and "subtitle" not in result:
        result["subtitle"] = run_live_subtitle_pipeline(result)
    if args.refine_shots:
        result["shot_refinement"] = run_live_shot_refinement(result, args.auth_document)
    if args.generate_story:
        result["story_draft"] = run_live_story_draft(result, args.input, args.auth_document)
    if args.approve_story:
        result["director_lock"] = build_approved_director_lock(result, args.input)
    if args.director_governance:
        lock = result.get("director_lock")
        if not isinstance(lock, dict):
            raise RuntimeError("生成导演治理层前必须已有 DirectorLockedManifest")
        result["director_governance"] = run_cinematic_storyboard_governance(
            lock, transport=DualSeed21TurboTextTransport(args.auth_document.resolve())
        )
    if args.director_governance_input is not None:
        governance = json.loads(args.director_governance_input.read_text(encoding="utf-8"))
        if not isinstance(governance, dict) or governance.get("review", {}).get("status") != "PASS":
            raise RuntimeError("--director-governance-input 必须是 review.status=PASS 的治理 JSON")
        result["director_governance"] = governance
    if args.material_plan:
        result["material_output"] = run_live_material_planning(result, args.auth_document, aspect_ratio="9:16")
    if args.export_grid_input_dir is not None:
        lock = result.get("director_lock")
        material = result.get("material_output")
        if not isinstance(lock, dict) or not isinstance(material, dict):
            raise RuntimeError("导出多宫格输入前必须已有 DirectorLockedManifest 和素材层输出")
        args.export_grid_input_dir.mkdir(parents=True, exist_ok=True)
        (args.export_grid_input_dir / "director_lock.json").write_text(
            json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        grid_material = {**material, "aspect_ratio": "9:16"}
        (args.export_grid_input_dir / "material_output.json").write_text(
            json.dumps(grid_material, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "evidence_level": result["evidence_level"],
        "source_text_chars": result["source_text_chars"],
        "full_text_segment_count": len(result["director_output"]["segments"]),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
