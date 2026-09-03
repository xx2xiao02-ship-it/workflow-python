"""首帧用户注意力引导的轻量强化层。

该层只把用户自己的简短视觉意图扩展为更可执行的全局约束；它不是导演、
不是逐格 Prompt Reviewer，也不拥有图片生成权限。原始用户文本始终保留，
模型不可用或输出不安全时直接回退，不阻断首帧任务。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


GUIDANCE_EXPANDER_MODES = ("OFF", "ON")
GUIDANCE_EXPANDER_STATUSES = ("SKIPPED", "EXPANDED", "PASS", "ERROR")

# 这些名称只用于拒绝“原文没有、模型却自行加进来”的故事事实。原文已经
# 明示的名称不受影响；用户原文永远会原样保留在最终 Prompt 中。
_KNOWN_STORY_ENTITY_PATTERN = re.compile(
    r"陈岭|周姐|老陈|父亲|母亲|女儿|儿子|医生|护士|老板娘?|铁盒|舞鞋|药盒|"
    r"现金信封|信封|租金牌|招牌|菜场|医院|居民楼|零工市场|舞蹈教室"
)
_STRUCTURAL_LEAK_PATTERN = re.compile(r"【[^】]{0,40}】|\bscene_\d+\b|镜头编号|shot_id", re.IGNORECASE)


GUIDANCE_EXPANDER_SYSTEM_PROMPT = """你是首帧多宫格的用户注意力引导强化器，不是导演，也不是画面创作者。
你的唯一工作是把用户自己的简短视觉要求，扩展为简洁、强制、可执行的全局生图约束。
不要推断或新增任何故事、人物、人物关系、地点、道具、动作、镜头编号、景别、机位、构图或时间线。
不要改写用户原文，不要输出标题、Markdown、九宫格位置或单格剧情。
每一条扩展约束必须只服务于原文中明确出现的 source_phrase，source_phrase 必须逐字摘自用户原文。
正常且无需强化时返回 PASS。只返回严格 JSON：
{"status":"EXPANDED 或 PASS","directives":[{"source_phrase":"用户原文中的短语","instruction":"一条自然语言的强制约束"}]}。
"""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return _text(value).lower() in {"1", "true", "yes", "on", "是", "开启"}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _extract_json(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raw = _text(value)
    raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.IGNORECASE).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise ValueError("模型未返回 JSON 对象")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, Mapping):
        raise ValueError("模型返回根节点不是 JSON 对象")
    return parsed


@dataclass(frozen=True)
class VisualGuidanceExpanderConfig:
    """强化器配置。默认关闭，避免未确认的模型费用。"""

    enabled: bool = False
    provider: str = ""
    model: str = ""
    model_version: str = "guidance-expander-v1"
    temperature: float = 0.0
    max_tokens: int = 400
    timeout: float = 20.0
    mode: str = "OFF"
    cache_path: Path | None = None

    def __post_init__(self) -> None:
        mode = _text(self.mode or "OFF").upper()
        if mode not in GUIDANCE_EXPANDER_MODES:
            raise ValueError(f"first_frame_guidance_expander.mode 不受支持：{self.mode!r}")
        object.__setattr__(self, "mode", mode)
        if not 0 <= float(self.temperature) <= 2:
            raise ValueError("first_frame_guidance_expander.temperature 必须在 0 到 2 之间")
        if int(self.max_tokens) <= 0 or float(self.timeout) <= 0:
            raise ValueError("first_frame_guidance_expander.max_tokens/timeout 必须为正数")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "VisualGuidanceExpanderConfig":
        if not isinstance(mapping, Mapping):
            return cls()
        values = mapping.get("first_frame_guidance_expander", mapping)
        if not isinstance(values, Mapping):
            return cls()
        cache_path = values.get("cache_path")
        return cls(
            enabled=_truthy(values.get("enabled"), False),
            provider=_text(values.get("provider")),
            model=_text(values.get("model")),
            model_version=_text(values.get("model_version")) or "guidance-expander-v1",
            temperature=float(values.get("temperature", 0.0)),
            max_tokens=int(values.get("max_tokens", 400)),
            timeout=float(values.get("timeout", 20.0)),
            mode=(_text(values.get("mode")) or "OFF").upper(),
            cache_path=Path(str(cache_path)) if cache_path else None,
        )

    @classmethod
    def from_env(cls, prefix: str = "FIRST_FRAME_GUIDANCE_EXPANDER_") -> "VisualGuidanceExpanderConfig":
        def value(name: str, default: Any = None) -> Any:
            return os.getenv(prefix + name, default)

        enabled = _truthy(value("ENABLED"), False)
        return cls(
            enabled=enabled,
            provider=_text(value("PROVIDER")),
            model=_text(value("MODEL")),
            model_version=_text(value("MODEL_VERSION")) or "guidance-expander-v1",
            temperature=float(value("TEMPERATURE", 0.0)),
            max_tokens=int(value("MAX_TOKENS", 400)),
            timeout=float(value("TIMEOUT", 20.0)),
            mode=(_text(value("MODE")) or ("ON" if enabled else "OFF")).upper(),
            cache_path=Path(value("CACHE_PATH")) if value("CACHE_PATH") else None,
        )


class ExistingModelGuidanceExpanderAdapter:
    """复用既有文本 Provider；不读取密钥，也不固化任何模型名称。"""

    def __init__(self, transport: Any) -> None:
        if transport is None or not (
            hasattr(transport, "run_text") or hasattr(transport, "run_plugin") or callable(transport)
        ):
            raise TypeError("ExistingModelGuidanceExpanderAdapter 需要既有文本 Provider Adapter")
        self.transport = transport

    def expand(self, payload: Mapping[str, Any]) -> Any:
        config = payload.get("config") if isinstance(payload, Mapping) else {}
        config = config if isinstance(config, Mapping) else {}
        user_payload = json.dumps(payload.get("input") or {}, ensure_ascii=False)
        system_prompt = _text(payload.get("system_prompt")) or GUIDANCE_EXPANDER_SYSTEM_PROMPT
        if hasattr(self.transport, "run_text"):
            result = self.transport.run_text(
                system_prompt=system_prompt,
                prompt=user_payload,
                model=_text(config.get("model")),
                temperature=float(config.get("temperature") or 0.0),
                max_tokens=int(config.get("max_tokens") or 400),
                timeout=float(config.get("timeout") or 20.0),
            )
        elif hasattr(self.transport, "run_plugin"):
            result = self.transport.run_plugin(
                system_prompt=system_prompt,
                prompt=user_payload,
                image_urls=[],
            )
        else:
            result = self.transport(system_prompt, user_payload)
        if isinstance(result, Mapping):
            for key in ("output_5_5", "content", "text", "output"):
                if result.get(key) not in (None, ""):
                    return result[key]
        return result


def _validate_directives(raw: Any, original: str) -> tuple[str, list[dict[str, str]]]:
    parsed = _extract_json(raw)
    status = _text(parsed.get("status")).upper()
    if status not in {"EXPANDED", "PASS"}:
        raise ValueError("模型 status 只支持 EXPANDED 或 PASS")
    if status == "PASS":
        return status, []
    raw_directives = parsed.get("directives")
    if not isinstance(raw_directives, Sequence) or isinstance(raw_directives, (str, bytes)):
        raise ValueError("EXPANDED 必须返回 directives 数组")
    directives: list[dict[str, str]] = []
    for index, item in enumerate(raw_directives):
        if not isinstance(item, Mapping):
            raise ValueError(f"directives[{index}] 必须是对象")
        source_phrase = _text(item.get("source_phrase"))
        instruction = re.sub(r"\s+", " ", _text(item.get("instruction")))
        if not source_phrase or source_phrase not in original:
            raise ValueError(f"directives[{index}].source_phrase 不在用户原文中")
        if not instruction or len(instruction) > 480:
            raise ValueError(f"directives[{index}].instruction 为空或过长")
        if _STRUCTURAL_LEAK_PATTERN.search(instruction):
            raise ValueError(f"directives[{index}] 泄漏内部结构字段")
        unknown_entities = {
            match.group(0)
            for match in _KNOWN_STORY_ENTITY_PATTERN.finditer(instruction)
            if match.group(0) not in original
        }
        if unknown_entities:
            raise ValueError(
                f"directives[{index}] 新增未在用户原文声明的故事事实：{'、'.join(sorted(unknown_entities))}"
            )
        normalized = {"source_phrase": source_phrase, "instruction": instruction}
        if normalized not in directives:
            directives.append(normalized)
    if not directives:
        raise ValueError("EXPANDED 至少需要一条有效 directive")
    return status, directives


@dataclass
class LightweightVisualGuidanceExpander:
    config: VisualGuidanceExpanderConfig = field(default_factory=VisualGuidanceExpanderConfig)
    runner: Callable[[Mapping[str, Any]], Any] | Any | None = None
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.config.cache_path:
            self._load_cache(self.config.cache_path)

    def _load_cache(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, Mapping):
            self._cache.update({str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)})

    def _save_cache(self) -> None:
        path = self.config.cache_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # 缓存仅用于节流，不能影响生图主链。
            return

    def _input_hash(self, original: str, context: Mapping[str, Any]) -> str:
        payload = {
            "original_user_guidance": original,
            "context": dict(context),
            "provider": self.config.provider,
            "model": self.config.model,
            "model_version": self.config.model_version,
        }
        return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    def _base_audit(self, original: str, input_hash: str) -> dict[str, Any]:
        return {
            "original_user_guidance": original,
            "guidance_expansion_mode": self.config.mode,
            "guidance_expansion_requested": False,
            "guidance_expansion_status": "SKIPPED",
            "guidance_expansion_provider": self.config.provider,
            "guidance_expansion_model": self.config.model,
            "guidance_expansion_model_version": self.config.model_version,
            "guidance_expansion_directives": [],
            "enhanced_directive": "",
            "guidance_expansion_input_hash": input_hash,
            "guidance_expansion_latency_ms": 0,
            "guidance_expansion_error": "",
            "cache_hit": False,
        }

    def expand(
        self,
        user_guidance: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        original = _text(user_guidance)
        safe_context = dict(context or {})
        input_hash = self._input_hash(original, safe_context)
        if not original:
            audit = self._base_audit(original, input_hash)
            audit["guidance_expansion_error"] = "用户未填写注意力引导"
            return audit
        cached = self._cache.get(input_hash)
        if isinstance(cached, Mapping):
            result = dict(cached)
            result["cache_hit"] = True
            return result
        audit = self._base_audit(original, input_hash)
        if not self.config.enabled or self.config.mode == "OFF":
            audit["guidance_expansion_error"] = "强化器未启用，已使用用户原文与确定性规则"
            self._cache[input_hash] = dict(audit)
            self._save_cache()
            return audit
        if not self.config.provider or not self.config.model:
            audit.update(
                guidance_expansion_status="ERROR",
                guidance_expansion_error="强化器缺少 provider 或 model 配置，已回退用户原文",
            )
            self._cache[input_hash] = dict(audit)
            self._save_cache()
            return audit
        audit["guidance_expansion_requested"] = True
        if self.runner is None:
            audit.update(
                guidance_expansion_status="ERROR",
                guidance_expansion_error="未配置强化器 Provider Adapter，已回退用户原文",
            )
            self._cache[input_hash] = dict(audit)
            self._save_cache()
            return audit
        payload = {
            "system_prompt": GUIDANCE_EXPANDER_SYSTEM_PROMPT,
            "input": {
                "original_user_guidance": original,
                "scope": "first_frame_grid_global_constraints",
                "context": safe_context,
            },
            "config": {
                "provider": self.config.provider,
                "model": self.config.model,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "timeout": self.config.timeout,
            },
        }
        started = time.perf_counter()
        try:
            raw = self.runner.expand(payload) if hasattr(self.runner, "expand") else self.runner(payload)
            status, directives = _validate_directives(raw, original)
            audit["guidance_expansion_latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            audit["guidance_expansion_status"] = status
            audit["guidance_expansion_directives"] = directives
            audit["enhanced_directive"] = "；".join(item["instruction"] for item in directives)
        except Exception as exc:  # 外部模型是增强层，异常绝不能阻塞首帧创建。
            audit["guidance_expansion_latency_ms"] = int(round((time.perf_counter() - started) * 1000))
            audit.update(
                guidance_expansion_status="ERROR",
                guidance_expansion_error=f"{type(exc).__name__}: {exc}",
            )
        self._cache[input_hash] = dict(audit)
        self._save_cache()
        return audit


__all__ = [
    "ExistingModelGuidanceExpanderAdapter",
    "GUIDANCE_EXPANDER_MODES",
    "GUIDANCE_EXPANDER_STATUSES",
    "GUIDANCE_EXPANDER_SYSTEM_PROMPT",
    "LightweightVisualGuidanceExpander",
    "VisualGuidanceExpanderConfig",
]
