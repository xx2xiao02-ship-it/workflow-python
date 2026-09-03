"""火山方舟 Seed 2.1 turbo 的镜头精细化导演 transport。

该模块只负责把 197742 的单组输入送入官方 Chat API 并解析 JSON；
批处理、字段校验和顺序保持由 ``shot_refinement.py`` 负责。
密钥只从环境变量读取，默认不会创建网络请求。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .shot_refinement import ShotRefinementItem


DEFAULT_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_MODEL = "doubao-seed-2-1-turbo-260628"
DEFAULT_TIMEOUT = 180.0
PREFERRED_SHOT_MIN_SECONDS = 2.0
PREFERRED_SHOT_MAX_SECONDS = 5.0
PREFERRED_SHOT_SECONDS = 4.5


class ShotRefinementTransportError(RuntimeError):
    """方舟请求或响应不符合镜头精细化节点契约。"""


class ShotRefinementTransportConfigError(ShotRefinementTransportError):
    """方舟 transport 缺少必要配置。"""


Requester = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Any]


def preferred_shot_count(duration_seconds: float) -> tuple[int, int, int]:
    """返回镜头数的软约束：优先让每个镜头落在 2--5 秒。

    小于 2 秒的完整大镜头不可再切分；低于 3 秒的镜头由素材层改走静态图片，
    不进入 AIGC 视频；语义完整性优先于机械凑时长。
    返回值依次为最少数、最多数、建议数，供提示词约束使用。
    """

    if duration_seconds <= 0:
        raise ValueError("duration_seconds 必须大于 0")
    if duration_seconds < PREFERRED_SHOT_MIN_SECONDS:
        return (1, 1, 1)
    minimum = max(1, math.ceil(duration_seconds / PREFERRED_SHOT_MAX_SECONDS))
    maximum = max(1, math.floor(duration_seconds / PREFERRED_SHOT_MIN_SECONDS))
    recommended = max(1, math.floor(duration_seconds / PREFERRED_SHOT_SECONDS + 0.5))
    return (minimum, maximum, min(maximum, max(minimum, recommended)))


FALLBACK_SYSTEM_PROMPT = """你是视频内容规划中的镜头精细化导演。
你的唯一职责是将当前镜头组拆为连续的叙事镜头单元，只处理事件如何拆开并连续推进，不设计画面。
严格只输出合法 JSON：
{"shots":[{"source_text":"中文语义锚点","clip_role":"叙事作用","story_beat":"已经成立的事件状态"}]}"""


def load_system_prompt() -> str:
    prompt_path = Path(__file__).with_name("shot_refinement_system_prompt.txt")
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        prompt = ""
    return prompt or FALLBACK_SYSTEM_PROMPT


def _bearer(value: str) -> str:
    return value if value.lower().startswith("bearer ") else "Bearer " + value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _narration_key(value: str) -> str:
    """比较 STT 字幕与原文时忽略空白和标点，但不忽略任何文字或数字。

    剪映 STT 输出的语义文本通常不带原文逗号、句号等标点；时间线与字幕
    仍然来自同一条旁白音频，不能因此把有效坑位误判为不完整覆盖。
    """

    return "".join(char for char in value if char.isalnum())


def _extra_api_keys_from_env(name: str) -> tuple[str, ...]:
    try:
        value = json.loads(os.environ.get(name, ""))
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip()))


def _parse_json(value: Any) -> Mapping[str, Any]:
    text = _text(value).replace("```json", "").replace("```", "").strip()
    if not text:
        raise ShotRefinementTransportError("方舟响应为空")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ShotRefinementTransportError("方舟响应不是合法 JSON")
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ShotRefinementTransportError("方舟响应不是合法 JSON") from exc
    if not isinstance(parsed, Mapping):
        raise ShotRefinementTransportError("方舟响应根节点必须是对象")
    return parsed


def _default_requester(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Any:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ShotRefinementTransportError(f"方舟 HTTP 请求失败：{exc.code} {detail[:500]}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ShotRefinementTransportError(f"方舟网络请求失败：{type(exc).__name__}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ShotRefinementTransportError("方舟响应不是合法 JSON") from exc


class Seed21TurboShotRefinementTransport:
    """使用官方 Chat API 的 Seed 2.1 turbo 单组执行器。

    鉴权顺序保持项目现有约定：主账号优先，主账号失败后切备用账号；
    每个账号最多尝试 ``max_attempts_per_auth`` 次。
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        backup_api_key: str = "",
        api_url: str = DEFAULT_API_URL,
        model: str = DEFAULT_MODEL,
        fallback_models: tuple[str, ...] = (),
        system_prompt: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts_per_auth: int = 3,
        extra_api_keys: tuple[str, ...] = (),
        requester: Requester | None = None,
    ) -> None:
        if not api_key.strip():
            raise ShotRefinementTransportConfigError(
                "未配置 SHOT_REFINEMENT_PRIMARY_API_KEY；密钥只从本机环境变量读取"
            )
        self.auth_configs = [
            {"name": "主账号", "api_key": api_key.strip()},
        ]
        if backup_api_key.strip():
            self.auth_configs.append({"name": "备用账号", "api_key": backup_api_key.strip()})
        for index, extra_api_key in enumerate(extra_api_keys, start=3):
            if extra_api_key.strip():
                self.auth_configs.append({"name": f"第{index}级账号", "api_key": extra_api_key.strip()})
        self.api_url = api_url.strip()
        self.models = list(
            dict.fromkeys(
                value.strip()
                for value in (model, *fallback_models)
                if isinstance(value, str) and value.strip()
            )
        )
        if not self.models:
            raise ShotRefinementTransportConfigError("未配置镜头精细化模型")
        # 保留该属性，兼容已有调用方和测试；实际请求会按 self.models 顺序跌落。
        self.model = self.models[0]
        self.system_prompt = (system_prompt or "").strip() or load_system_prompt()
        self.timeout = float(timeout)
        self.max_attempts_per_auth = max(1, int(max_attempts_per_auth))
        self.requester = requester or _default_requester

    @classmethod
    def from_env(cls) -> "Seed21TurboShotRefinementTransport":
        primary_api_key = os.environ.get(
            "SHOT_REFINEMENT_PRIMARY_API_KEY",
            os.environ.get("SHOT_REFINEMENT_API_KEY", ""),
        ).strip()
        return cls(
            api_key=primary_api_key,
            backup_api_key=os.environ.get("SHOT_REFINEMENT_BACKUP_API_KEY", ""),
            api_url=os.environ.get("SHOT_REFINEMENT_API_URL", DEFAULT_API_URL),
            model=os.environ.get("SHOT_REFINEMENT_MODEL", DEFAULT_MODEL),
            system_prompt=os.environ.get("SHOT_REFINEMENT_SYSTEM_PROMPT") or None,
            timeout=float(os.environ.get("SHOT_REFINEMENT_TIMEOUT", DEFAULT_TIMEOUT)),
            max_attempts_per_auth=int(os.environ.get("SHOT_REFINEMENT_MAX_ATTEMPTS", "3")),
            extra_api_keys=_extra_api_keys_from_env("API_MANAGEMENT_EXTRA_KEYS_DIRECTOR_SEED21"),
        )

    @staticmethod
    def _user_prompt(item: ShotRefinementItem) -> str:
        min_count, max_count, required_count = preferred_shot_count(float(item.duration))
        override = item.item.get("required_shot_count")
        if override is not None:
            if isinstance(override, bool) or not isinstance(override, int):
                raise ShotRefinementTransportError("required_shot_count 必须是整数")
            if override < min_count or override > max_count:
                raise ShotRefinementTransportError(
                    f"required_shot_count={override} 超出当前大分段允许范围 {min_count}..{max_count}"
                )
            required_count = override
        locked_slots = item.item.get("locked_shot_slots")
        if locked_slots is not None:
            if not isinstance(locked_slots, list) or not locked_slots:
                raise ShotRefinementTransportError("locked_shot_slots 必须是非空数组")
            if len(locked_slots) != required_count:
                raise ShotRefinementTransportError("locked_shot_slots 数量必须等于 required_shot_count")
            locked_narration: list[str] = []
            for index, slot in enumerate(locked_slots):
                if not isinstance(slot, Mapping) or not isinstance(slot.get("narration_text"), str):
                    raise ShotRefinementTransportError(f"locked_shot_slots[{index}].narration_text 必须是字符串")
                locked_narration.append(slot["narration_text"])
            if _narration_key("".join(locked_narration)) != _narration_key(item.segment):
                raise ShotRefinementTransportError(
                    "locked_shot_slots 去除标点和空白后仍未完整覆盖原始 segments"
                )
            return "\n".join(
                [
                    "duration=" + json.dumps(item.duration, ensure_ascii=False) + " seconds",
                    "segments=" + json.dumps(item.segment, ensure_ascii=False),
                    "items=" + json.dumps(dict(item.item), ensure_ascii=False),
                    "LOCKED_SLOT_PLAN: " + json.dumps(locked_slots, ensure_ascii=False),
                    "EXECUTION_ORDER: the CapCut STT timeline and semantic narration boundaries have already been frozen before your call.",
                    "OUTPUT_CONSTRAINT: output exactly " + str(required_count) + " shots, one for each LOCKED_SLOT_PLAN item in order.",
                    "NARRATION_AUTHORITY: LOCKED_SLOT_PLAN owns narration_text, timeline and duration. narration_text is not an output field and must not be copied, rewritten or used to change a slot.",
                    "YOUR_ONLY_JOB: based on each already-locked narration slot, generate only source_text, clip_role and story_beat as a continuous visual narrative. You are designing the content of an existing shot, not deciding where the shot begins or ends.",
                ]
            )
        return "\n".join(
            [
                "duration=" + json.dumps(item.duration, ensure_ascii=False) + " seconds",
                "segments=" + json.dumps(item.segment, ensure_ascii=False),
                "items=" + json.dumps(dict(item.item), ensure_ascii=False),
                "PREFERRED_SHOT_DURATION: 2..5 seconds. Shots under 3 seconds must use a static image and skip AIGC video. The exact count below was allocated from the whole video's 4.5-second average budget and narrative rhythm; it overrides any per-group calculation.",
                (
                    "OUTPUT_CONSTRAINT: output exactly "
                    + str(required_count)
                    + " shots; legal range is "
                    + str(min_count)
                    + ".."
                    + str(max_count)
                    + ". Do not output any other shot count."
                ),
                "NARRATION_BOUNDARY: every shot must include narration_text copied verbatim from segments. Concatenate all narration_text values in order and it must equal segments exactly (except surrounding whitespace). Do not omit, rewrite, reorder, or add any character. Split at meaningful pauses, clauses, questions, turns, or actions whenever possible.",
            ]
        )

    def _call_once(
        self, item: ShotRefinementItem, api_key: str, model: str, user_prompt: str
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
        }
        response = self.requester(
            self.api_url,
            {"Authorization": _bearer(api_key)},
            payload,
            self.timeout,
        )
        if not isinstance(response, Mapping):
            raise ShotRefinementTransportError("方舟响应根节点必须是对象")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ShotRefinementTransportError("方舟响应缺少 choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ShotRefinementTransportError("方舟响应缺少 choices[0].message")
        parsed = _parse_json(message.get("content"))
        shots = parsed.get("shots")
        if not isinstance(shots, list):
            raise ShotRefinementTransportError("方舟响应缺少 shots 数组")
        return {"shots": list(shots), "reasoning_content": _text(message.get("reasoning_content"))}

    @staticmethod
    def _is_retryable_failure(exc: Exception) -> bool:
        """只重试可能短暂恢复的网络、限流和服务端错误。

        响应结构不合格、模型输出不合格等问题，继续重试同一候选没有意义，
        应立即跌落到下一个模型或账号。
        """

        message = str(exc)
        if "方舟网络请求失败" in message:
            return True
        matched = re.search(r"方舟 HTTP 请求失败：\s*(\d{3})", message)
        if not matched:
            return False
        status = int(matched.group(1))
        return status == 408 or status == 429 or 500 <= status <= 599

    @staticmethod
    def _failure_summary(exc: Exception) -> str:
        """给任务页保留可行动但不泄露响应正文的失败类别。"""

        message = str(exc)
        matched = re.search(r"方舟 HTTP 请求失败：\s*(\d{3})", message)
        if matched:
            return "HTTP " + matched.group(1)
        if "方舟网络请求失败" in message:
            return "网络或超时"
        if "locked_shot_slots" in message:
            return "字幕坑位未完整覆盖原文"
        if "方舟响应" in message or "JSON" in message:
            return "响应契约不合格"
        return type(exc).__name__

    def __call__(self, item: ShotRefinementItem) -> dict[str, Any]:
        # 先做本地坑位校验。若这一步失败，绝不尝试模型或鉴权候选，也不会
        # 产生 API 调用费用。
        user_prompt = self._user_prompt(item)
        last_error: Exception | None = None
        failures: list[str] = []
        # 先完整使用当前模型的主/备用账号；若是不可恢复的响应契约错误，则不
        # 浪费同一候选的重复请求，直接按 API 管理页顺序跌落到下一个模型。
        for model in self.models:
            for auth in self.auth_configs:
                api_key = auth["api_key"]
                for attempt in range(self.max_attempts_per_auth):
                    try:
                        return self._call_once(item, api_key, model, user_prompt)
                    except Exception as exc:
                        last_error = exc
                        failure = f"{model}/{auth['name']}：{self._failure_summary(exc)}"
                        if failure not in failures:
                            failures.append(failure)
                        if attempt + 1 >= self.max_attempts_per_auth or not self._is_retryable_failure(exc):
                            break
        if last_error is not None:
            raise ShotRefinementTransportError(
                "所有镜头精细化模型/鉴权候选均失败：" + "；".join(failures[-12:])
            ) from last_error
        raise ShotRefinementTransportError("没有可用的鉴权配置")


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "PREFERRED_SHOT_MIN_SECONDS",
    "PREFERRED_SHOT_MAX_SECONDS",
    "PREFERRED_SHOT_SECONDS",
    "Seed21TurboShotRefinementTransport",
    "ShotRefinementTransportConfigError",
    "ShotRefinementTransportError",
    "preferred_shot_count",
]
