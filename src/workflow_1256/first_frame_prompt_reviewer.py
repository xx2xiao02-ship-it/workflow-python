"""首帧图片 Prompt 的轻量语义审核层。

Task B 只审查 Task A 已经编译完成的单格 Prompt。它不修改 Director 数据、
不决定宫格布局，也不直接调用图片模型。模型调用通过 ``runner`` 注入，默认
使用确定性的风险门控；没有配置 provider 时，风险单元会记录 ERROR 并回退
到编译结果，因而不会阻断首帧生产。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


REVIEW_MODES = ("OFF", "RISK_ONLY", "ALL")
REVIEW_STATUSES = ("PASS", "REPAIRED", "NEEDS_REVIEW", "ERROR", "SKIPPED")
ALLOWED_ISSUE_CODES = {
    "SCENE_CONFLICT",
    "SUBJECT_CONFLICT",
    "PROP_CONFLICT",
    "CHARACTER_RELATION_CONFLICT",
    "MULTI_ACTION_SEMANTIC_CONFLICT",
    "TEMPORAL_SEQUENCE_RESIDUE",
    "SHOT_SEMANTIC_CONFLICT",
    "COMPOSITION_SEMANTIC_CONFLICT",
    "INTERNAL_LOGIC_CONFLICT",
    "SYSTEM_PROMPT_BUILD_ERROR",
    "IMMUTABLE_FACT_VIOLATION",
}

TASK_A_REGRESSION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bscene_\d+\b", "最终首帧仍泄漏内部 scene 引用"),
    (r"自行车\s*/\s*街道场景|同一自行车|自行车/街道", "最终首帧仍包含旧自行车案例"),
    (r"构图\s*[=:：].*【", "结构字段吞并了后续字段"),
    (r"【镜头动势】.*【镜头动势】", "镜头动势字段重复"),
    (r"缓慢横摇镜|缓慢拉远|继续向前跑几步|逐渐放缓|随后|最终停住|跟拍指尖移动轨迹", "视频时间过程残留到静态首帧"),
)

_INDOOR_MARKERS = ("室内", "室内场景", "餐桌旁", "桌旁", "客厅", "房间", "门内", "半开的门框")
_OUTDOOR_MARKERS = ("室外", "路边", "菜场", "街道", "户外", "摊位前", "居民楼外")
_COMMON_PERSON_NAMES = frozenset(
    {
        "陈岭", "周姐", "父亲", "母亲", "女儿", "儿子", "林夏", "阿豆", "小陈", "老陈",
        "老板", "老板娘", "医生", "护士", "警察", "老师",
    }
)
_COMMON_PROP_NAMES = frozenset(
    {
        "铁盒", "零钱", "舞鞋", "现金信封", "信封", "药盒", "舞鞋盒", "招牌", "租金牌",
        "自行车", "纸币", "钱币", "钞票", "桌面",
    }
)
_TEMPORAL_RESIDUE = ("刚完成", "正准备", "之后将", "同时已经", "正在", "随后", "最终", "接着")
_ACTION_CONNECTORS = ("同时", "然后", "随后", "接着", "又", "再", "并且", "转身", "站起", "走向", "退一步")
_FINE_DETAIL_MARKERS = ("指甲", "指尖", "纸币纤维", "毫米级", "微小纹理", "细小纹理")

REVIEWER_SYSTEM_PROMPT = """你是首帧生图 Prompt 审核器，不是导演，也不是创作者。
你的任务是判断当前 Prompt 是否忠实于给定结构化导演数据。
正常 Prompt 必须返回 PASS，禁止为了语言更优美主动改写。
只有存在明确语义冲突时才允许最小范围修复；不得改变剧情、场景、人物身份、人物关系、关键道具、景别、机位、视角、构图核心意图、光线或宫格位置。
如果无法从结构化数据唯一确定正确修复方式，返回 NEEDS_REVIEW，不得猜测。
只返回 JSON：status、risk_score、issues、repaired_prompt。"""


class ExistingModelReviewerAdapter:
    """把既有 Provider Adapter 的 ``run_plugin`` 包装成 Reviewer runner。

    适配器本身不创建 transport、不读取密钥；调用方必须显式注入已经配置好的
    transport。这样可以复用现有模型路由，同时保证 Task B 离线模式不会产生外部
    请求或费用。
    """

    def __init__(self, transport: Any) -> None:
        if transport is None or not hasattr(transport, "run_plugin"):
            raise TypeError("ExistingModelReviewerAdapter 需要提供带 run_plugin 的现有 Provider Adapter")
        self.transport = transport

    def review(self, payload: Mapping[str, Any]) -> Any:
        review_input = payload.get("input") if isinstance(payload, Mapping) else None
        result = self.transport.run_plugin(
            system_prompt=_text(payload.get("system_prompt")) or REVIEWER_SYSTEM_PROMPT,
            prompt=json.dumps(review_input if isinstance(review_input, Mapping) else {}, ensure_ascii=False),
            image_urls=[],
        )
        if isinstance(result, Mapping):
            for key in ("output_5_5", "content", "text", "output"):
                if result.get(key) not in (None, ""):
                    return result[key]
        return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        item_text = _text(item)
        if item_text and item_text not in result:
            result.append(item_text)
    return result


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return _text(value).lower() in {"1", "true", "yes", "on", "是", "开启"}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _severity_for(code: str) -> str:
    if code in {"SYSTEM_PROMPT_BUILD_ERROR", "IMMUTABLE_FACT_VIOLATION", "INTERNAL_LOGIC_CONFLICT"}:
        return "HIGH"
    if code in {"SCENE_CONFLICT", "SUBJECT_CONFLICT", "PROP_CONFLICT", "CHARACTER_RELATION_CONFLICT"}:
        return "HIGH"
    return "MEDIUM"


def _issue(code: str, reason: str, *, source: str = "TASK_B") -> dict[str, str]:
    return {"code": code, "severity": _severity_for(code), "reason": reason, "source": source}


@dataclass(frozen=True)
class ImagePromptReviewerConfig:
    """Reviewer 配置；provider/model 仅从配置读取，不在业务代码中固化。"""

    enabled: bool = True
    provider: str = ""
    model: str = ""
    model_version: str = "task-b-v1"
    temperature: float = 0.0
    max_tokens: int = 500
    timeout: float = 20.0
    mode: str = "RISK_ONLY"
    max_changed_ratio: float = 0.20
    fallback_to_compiled: bool = True
    cache_path: Path | None = None

    def __post_init__(self) -> None:
        mode = str(self.mode or "RISK_ONLY").upper()
        if mode not in REVIEW_MODES:
            raise ValueError(f"image_prompt_reviewer.mode 不受支持：{self.mode!r}")
        object.__setattr__(self, "mode", mode)
        if not 0 <= float(self.temperature) <= 2:
            raise ValueError("image_prompt_reviewer.temperature 必须在 0 到 2 之间")
        if int(self.max_tokens) <= 0 or float(self.timeout) <= 0:
            raise ValueError("image_prompt_reviewer.max_tokens/timeout 必须为正数")
        if not 0 <= float(self.max_changed_ratio) <= 1:
            raise ValueError("image_prompt_reviewer.max_changed_ratio 必须在 0 到 1 之间")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "ImagePromptReviewerConfig":
        if not isinstance(mapping, Mapping):
            return cls()
        values: Mapping[str, Any] = mapping.get("image_prompt_reviewer", mapping)
        if not isinstance(values, Mapping):
            return cls()
        cache = values.get("cache_path")
        return cls(
            enabled=_truthy(values.get("enabled"), True),
            provider=_text(values.get("provider")),
            model=_text(values.get("model")),
            model_version=_text(values.get("model_version")) or "task-b-v1",
            temperature=float(values.get("temperature", 0.0)),
            max_tokens=int(values.get("max_tokens", 500)),
            timeout=float(values.get("timeout", 20.0)),
            mode=(_text(values.get("mode")) or "RISK_ONLY").upper(),
            max_changed_ratio=float(values.get("max_changed_ratio", 0.20)),
            fallback_to_compiled=_truthy(values.get("fallback_to_compiled"), True),
            cache_path=Path(str(cache)) if cache else None,
        )

    @classmethod
    def from_env(cls, prefix: str = "IMAGE_PROMPT_REVIEWER_") -> "ImagePromptReviewerConfig":
        def value(name: str, default: Any = None) -> Any:
            return os.getenv(prefix + name, default)

        return cls(
            enabled=_truthy(value("ENABLED"), True),
            provider=_text(value("PROVIDER")),
            model=_text(value("MODEL")),
            model_version=_text(value("MODEL_VERSION")) or "task-b-v1",
            temperature=float(value("TEMPERATURE", 0.0)),
            max_tokens=int(value("MAX_TOKENS", 500)),
            timeout=float(value("TIMEOUT", 20.0)),
            mode=(_text(value("MODE")) or "RISK_ONLY").upper(),
            max_changed_ratio=float(value("MAX_CHANGED_RATIO", 0.20)),
            fallback_to_compiled=_truthy(value("FALLBACK_TO_COMPILED"), True),
            cache_path=Path(value("CACHE_PATH")) if value("CACHE_PATH") else None,
        )


def build_reviewer_input(cell: Mapping[str, Any], compiled_prompt: str | None = None) -> dict[str, Any]:
    """只从结构字段组装 Reviewer 输入；不从自然语言猜测 ground truth。"""

    prompt = _text(compiled_prompt) or _text(
        cell.get("compiled_first_frame_prompt") or cell.get("compiled_cell_prompt") or cell.get("final_first_frame_prompt")
    )
    characters = _string_list(cell.get("characters") or cell.get("character_names"))
    key_props = _string_list(cell.get("key_props") or cell.get("key_prop"))
    character_count = cell.get("character_count")
    if character_count in (None, ""):
        character_count = len(characters)
    try:
        character_count = int(character_count)
    except (TypeError, ValueError):
        character_count = len(characters)
    return {
        "position": _text(cell.get("position")),
        "scene": _text(cell.get("scene") or cell.get("semantic_anchor")),
        "characters": characters,
        "character_count": max(0, character_count),
        "key_props": key_props,
        "shot_size": _text(cell.get("shot_size")),
        "camera_angle": _text(cell.get("camera_angle") or cell.get("viewpoint") or cell.get("camera_view")),
        "composition": _text(cell.get("composition")),
        "frame_intent": _text(cell.get("frame_intent") or cell.get("semantic_anchor")),
        "dominant_action": _text(cell.get("dominant_action") or cell.get("action") or cell.get("staging")),
        "lighting": _text(cell.get("lighting")),
        "character_relation": _text(cell.get("character_relation") or cell.get("relation")),
        "compiled_first_frame_prompt": prompt,
    }


def detect_deterministic_risks(review_input: Mapping[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """返回风险分数和风险提示；该函数不承担 Task A 的修复职责。"""

    prompt = _text(review_input.get("compiled_first_frame_prompt"))
    issues: list[dict[str, str]] = []
    for pattern, reason in TASK_A_REGRESSION_PATTERNS:
        if re.search(pattern, prompt, flags=re.S):
            issues.append(_issue("SYSTEM_PROMPT_BUILD_ERROR", reason, source="TASK_A_REGRESSION"))
    if len(re.findall(r"【镜头动势】", prompt)) > 1:
        issues.append(_issue("SYSTEM_PROMPT_BUILD_ERROR", "镜头动势标签重复，疑似字段拼接回归", source="TASK_A_REGRESSION"))

    scene = _text(review_input.get("scene"))
    if scene:
        is_indoor = any(marker in scene for marker in _INDOOR_MARKERS)
        is_outdoor = any(marker in scene for marker in _OUTDOOR_MARKERS)
        prompt_indoor = any(marker in prompt for marker in _INDOOR_MARKERS)
        prompt_outdoor = any(marker in prompt for marker in _OUTDOOR_MARKERS)
        if (is_outdoor and prompt_indoor and not is_indoor) or (is_indoor and prompt_outdoor and not is_outdoor):
            issues.append(_issue("SCENE_CONFLICT", f"结构化场景“{scene}”与 Prompt 空间语义冲突"))

    characters = set(_string_list(review_input.get("characters")))
    if characters:
        unexpected = sorted(name for name in _COMMON_PERSON_NAMES if name not in characters and name in prompt)
        if unexpected:
            issues.append(_issue("SUBJECT_CONFLICT", f"Prompt 出现未定义主体：{'、'.join(unexpected)}"))

    key_props = set(_string_list(review_input.get("key_props")))
    if key_props:
        unexpected_props = sorted(prop for prop in _COMMON_PROP_NAMES if prop not in key_props and prop in prompt)
        if unexpected_props:
            issues.append(_issue("PROP_CONFLICT", f"Prompt 出现未声明关键道具：{'、'.join(unexpected_props)}"))

    relation = _text(review_input.get("character_relation") or review_input.get("frame_intent"))
    if ("看向" in relation or "面对" in relation) and re.search(r"背对|离开|走开", prompt):
        issues.append(_issue("CHARACTER_RELATION_CONFLICT", "人物关系方向与结构化关系不一致"))

    action_text = "；".join(
        value for value in (_text(review_input.get("dominant_action")), prompt) if value
    )
    connector_count = sum(action_text.count(token) for token in _ACTION_CONNECTORS)
    if connector_count >= 2 or len(re.findall(r"(?:，|；|。)", _text(review_input.get("dominant_action")))) >= 2:
        issues.append(_issue("MULTI_ACTION_SEMANTIC_CONFLICT", "同一静态首帧残留两个以上连续动作"))

    if any(token in prompt for token in _TEMPORAL_RESIDUE):
        issues.append(_issue("TEMPORAL_SEQUENCE_RESIDUE", "Prompt 仍要求同时表达多个时间状态"))

    shot_size = _text(review_input.get("shot_size"))
    if any(token in shot_size for token in ("极远景", "远景")) and any(token in prompt for token in _FINE_DETAIL_MARKERS):
        issues.append(_issue("SHOT_SEMANTIC_CONFLICT", "当前景别无法合理承载过细的微观动作"))

    try:
        character_count = int(review_input.get("character_count") or 0)
    except (TypeError, ValueError):
        character_count = 0
    # 0 表示上游没有提供人数 ground truth，不能把“未知”误当作单人。
    if 0 < character_count <= 1 and re.search(r"多人层级|两组人物|双人层级", prompt):
        issues.append(_issue("COMPOSITION_SEMANTIC_CONFLICT", "单人结构数据与多人层级构图冲突"))

    if ("没有移动" in prompt or "保持静止" in prompt) and re.search(r"脚步|冲出画面|快速移动|奔跑", prompt):
        issues.append(_issue("INTERNAL_LOGIC_CONFLICT", "静止状态与移动状态同时出现"))

    if not issues:
        return 0.0, []
    risk = 0.0
    for item in issues:
        risk = max(risk, {"HIGH": 0.90, "MEDIUM": 0.65, "LOW": 0.30}.get(item["severity"], 0.50))
    return round(risk, 3), issues


def _parse_model_output(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    text = _text(raw)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, Mapping):
        raise ValueError("Reviewer 输出必须是 JSON 对象")
    return parsed


def _validate_issue_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("issues 必须是数组")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("issues 的每一项必须是对象")
        code = _text(item.get("code"))
        if code not in ALLOWED_ISSUE_CODES:
            raise ValueError(f"不支持的 issue code：{code!r}")
        severity = _text(item.get("severity")) or _severity_for(code)
        if severity not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError(f"issue severity 不受支持：{severity!r}")
        result.append({
            "code": code,
            "severity": severity,
            "reason": _text(item.get("reason")),
            **({"source": _text(item.get("source"))} if _text(item.get("source")) else {}),
        })
    return result


def _validate_model_result(raw: Any) -> dict[str, Any]:
    data = _parse_model_output(raw)
    status = _text(data.get("status")).upper()
    if status not in {"PASS", "REPAIRED", "NEEDS_REVIEW"}:
        raise ValueError("status 必须是 PASS、REPAIRED 或 NEEDS_REVIEW")
    try:
        risk_score = float(data.get("risk_score"))
    except (TypeError, ValueError):
        raise ValueError("risk_score 必须是 0 到 1 的数字") from None
    if not 0 <= risk_score <= 1:
        raise ValueError("risk_score 必须在 0 到 1 之间")
    issues = _validate_issue_list(data.get("issues"))
    repaired_prompt = data.get("repaired_prompt")
    if repaired_prompt is not None and not isinstance(repaired_prompt, str):
        raise ValueError("repaired_prompt 必须是字符串或 null")
    if status == "PASS" and repaired_prompt not in (None, ""):
        raise ValueError("PASS 不允许携带 repaired_prompt")
    if status == "PASS" and issues:
        raise ValueError("PASS 的 issues 必须为空数组")
    if status == "REPAIRED" and not _text(repaired_prompt):
        raise ValueError("REPAIRED 必须携带 repaired_prompt")
    return {
        "status": status,
        "risk_score": round(risk_score, 3),
        "issues": issues,
        "repaired_prompt": repaired_prompt,
    }


def _immutable_facts(review_input: Mapping[str, Any]) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    for label, value in (
        ("scene", review_input.get("scene")),
        ("shot_size", review_input.get("shot_size")),
        ("camera_angle", review_input.get("camera_angle")),
        ("composition", review_input.get("composition")),
        ("lighting", review_input.get("lighting")),
    ):
        text = _text(value)
        if text:
            facts.append((label, text))
    for label, values in (("character", review_input.get("characters")), ("key_prop", review_input.get("key_props"))):
        for value in _string_list(values):
            facts.append((label, value))
    return facts


def _immutable_fact_violations(review_input: Mapping[str, Any], before: str, after: str) -> list[str]:
    violations: list[str] = []
    for label, fact in _immutable_facts(review_input):
        # If an upstream prompt did not contain a declared fact, this guard
        # cannot infer a repair; the deterministic source remains authoritative.
        if fact in before and fact not in after:
            violations.append(f"{label}={fact}")
    expected_position = _text(review_input.get("position"))
    if expected_position and any(
        position in after and position != expected_position
        for position in ("左上", "上中", "右上", "左中", "正中", "右中", "左下", "下中", "右下")
    ):
        violations.append(f"position={expected_position}")
    return violations


def _changed_ratio(before: str, after: str) -> float:
    if before == after:
        return 0.0
    return round(1 - SequenceMatcher(None, before, after).ratio(), 4)


@dataclass
class LightweightImagePromptReviewer:
    config: ImagePromptReviewerConfig = field(default_factory=ImagePromptReviewerConfig)
    runner: Callable[[Mapping[str, Any]], Any] | Any | None = None
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.config.cache_path:
            self._load_cache(self.config.cache_path)

    @property
    def system_prompt(self) -> str:
        return REVIEWER_SYSTEM_PROMPT

    def _load_cache(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, Mapping):
            self._cache.update({str(key): dict(value) for key, value in data.items() if isinstance(value, Mapping)})

    def _save_cache(self) -> None:
        path = self.config.cache_path
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # Cache is an optimization and must never block production.
            return

    def _input_hash(self, review_input: Mapping[str, Any]) -> str:
        payload = {
            "compiled_first_frame_prompt": _text(review_input.get("compiled_first_frame_prompt")),
            "ground_truth": {key: value for key, value in review_input.items() if key != "compiled_first_frame_prompt"},
            "model_version": self.config.model_version,
            "provider": self.config.provider,
            "model": self.config.model,
        }
        return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    def _base_audit(self, review_input: Mapping[str, Any], input_hash: str, compiled: str) -> dict[str, Any]:
        return {
            "compiled_first_frame_prompt": compiled,
            "review_mode": self.config.mode,
            "review_requested": False,
            "review_status": "SKIPPED",
            "review_risk_score": 0.0,
            "review_model": self.config.model,
            "review_model_version": self.config.model_version,
            "review_issues_json": "[]",
            "reviewed_first_frame_prompt": None,
            "review_input_hash": input_hash,
            "review_latency_ms": 0,
            "review_error": "",
            "final_first_frame_prompt": compiled,
            "cache_hit": False,
        }

    def _finish(self, audit: dict[str, Any], *, issues: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        normalized = [dict(item) for item in issues]
        audit["review_issues_json"] = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        return audit

    def review_cell(self, cell: Mapping[str, Any], compiled_prompt: str | None = None) -> dict[str, Any]:
        review_input = build_reviewer_input(cell, compiled_prompt)
        compiled = _text(review_input.get("compiled_first_frame_prompt"))
        input_hash = self._input_hash(review_input)
        cached = self._cache.get(input_hash)
        if isinstance(cached, Mapping):
            result = dict(cached)
            result["cache_hit"] = True
            return result
        audit = self._base_audit(review_input, input_hash, compiled)
        if not compiled:
            audit.update({"review_status": "ERROR", "review_error": "compiled_first_frame_prompt 为空"})
            return self._finish(audit)
        if not self.config.enabled or self.config.mode == "OFF":
            audit["review_status"] = "SKIPPED"
            self._cache[input_hash] = dict(audit)
            self._save_cache()
            return self._finish(audit)

        risk_score, deterministic_issues = detect_deterministic_risks(review_input)
        audit["review_risk_score"] = risk_score
        if any(item.get("code") == "SYSTEM_PROMPT_BUILD_ERROR" for item in deterministic_issues):
            audit["review_status"] = "NEEDS_REVIEW"
            audit["review_error"] = "检测到 Task A 回归，Reviewer 不越权修复"
            result = self._finish(audit, issues=deterministic_issues)
            self._cache[input_hash] = dict(result)
            self._save_cache()
            return result
        if self.config.mode == "RISK_ONLY" and risk_score <= 0:
            audit["review_status"] = "PASS"
            result = self._finish(audit)
            self._cache[input_hash] = dict(result)
            self._save_cache()
            return result

        audit["review_requested"] = True
        if self.runner is None:
            audit.update({
                "review_status": "ERROR",
                "review_error": "未配置 Reviewer provider adapter，已回退 compiled_first_frame_prompt",
            })
            result = self._finish(audit, issues=deterministic_issues)
            self._cache[input_hash] = dict(result)
            self._save_cache()
            return result

        started = time.perf_counter()
        try:
            payload = {
                "system_prompt": self.system_prompt,
                "input": review_input,
                "config": {
                    "provider": self.config.provider,
                    "model": self.config.model,
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens,
                    "timeout": self.config.timeout,
                },
            }
            if hasattr(self.runner, "review"):
                raw_result = self.runner.review(payload)
            else:
                raw_result = self.runner(payload)
            model_result = _validate_model_result(raw_result)
            audit["review_latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            audit["review_status"] = model_result["status"]
            audit["review_risk_score"] = max(risk_score, float(model_result["risk_score"]))
            issues = [*deterministic_issues, *model_result["issues"]]
            repaired_prompt = _text(model_result.get("repaired_prompt"))
            if model_result["status"] == "REPAIRED":
                ratio = _changed_ratio(compiled, repaired_prompt)
                violations = _immutable_fact_violations(review_input, compiled, repaired_prompt)
                if violations:
                    issues.append(_issue("IMMUTABLE_FACT_VIOLATION", "; ".join(violations)))
                if ratio > float(self.config.max_changed_ratio):
                    issues.append(_issue("INTERNAL_LOGIC_CONFLICT", f"Reviewer 改写比例 {ratio:.2%} 超过预算 {self.config.max_changed_ratio:.2%}"))
                    audit["review_status"] = "NEEDS_REVIEW"
                    audit["review_error"] = "change_budget_exceeded"
                elif violations:
                    audit["review_status"] = "NEEDS_REVIEW"
                    audit["review_error"] = "immutable_facts_changed"
                else:
                    audit["reviewed_first_frame_prompt"] = repaired_prompt
                    audit["final_first_frame_prompt"] = repaired_prompt
            if audit["review_status"] in {"PASS", "NEEDS_REVIEW", "ERROR"}:
                audit["final_first_frame_prompt"] = compiled
            result = self._finish(audit, issues=issues)
        except Exception as exc:  # provider/JSON failures never block Task A output
            audit["review_latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            audit.update({"review_status": "ERROR", "review_error": f"{type(exc).__name__}: {exc}"})
            result = self._finish(audit, issues=deterministic_issues)
        self._cache[input_hash] = dict(result)
        self._save_cache()
        return result


def summarize_review_audits(audits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [dict(item) for item in audits]
    counts = {status.lower() + "_count": sum(item.get("review_status") == status for item in values) for status in REVIEW_STATUSES}
    requested = [item for item in values if item.get("review_requested")]
    input_chars = sum(len(_text(item.get("compiled_first_frame_prompt"))) for item in requested)
    output_chars = sum(len(_text(item.get("reviewed_first_frame_prompt"))) for item in requested)
    latencies = [int(item.get("review_latency_ms") or 0) for item in requested]
    return {
        "total_cells": len(values),
        "review_requested": len(requested),
        "pass_count": counts.get("pass_count", 0),
        "repaired_count": counts.get("repaired_count", 0),
        "needs_review_count": counts.get("needs_review_count", 0),
        "error_count": counts.get("error_count", 0),
        "skipped_count": counts.get("skipped_count", 0),
        "false_positive_count": sum(1 for item in values if item.get("review_status") == "REPAIRED" and not item.get("review_issues_json")),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
        "average_input_tokens": round(input_chars / 4 / len(requested), 2) if requested else 0.0,
        "average_output_tokens": round(output_chars / 4 / len(requested), 2) if requested else 0.0,
        "estimated_cost": 0.0,
        "cache_hit_count": sum(bool(item.get("cache_hit")) for item in values),
    }


__all__ = [
    "ALLOWED_ISSUE_CODES",
    "ExistingModelReviewerAdapter",
    "ImagePromptReviewerConfig",
    "LightweightImagePromptReviewer",
    "REVIEWER_SYSTEM_PROMPT",
    "REVIEW_MODES",
    "REVIEW_STATUSES",
    "build_reviewer_input",
    "detect_deterministic_risks",
    "summarize_review_audits",
]
