"""剪映 CapCut STT 适配层。

该模块只负责把本地 TTS 音频送到 ``capcut-tts-api``，并把剪映返回的
毫秒时间戳转换为本项目统一的微秒时间线。第三方 SDK 的高层
``transcribe_file`` 只识别 ``success``，而当前接口也会返回 ``succeed``；
这里使用 SDK 的低层接口自行轮询，兼容两种成功状态。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SUCCESS_STATUSES = {"success", "succeed", "completed", "complete", "done"}
FAILED_STATUSES = {"failed", "failure", "error", "canceled", "cancelled", "timeout"}
STT_PARALLEL_WORKERS = 3


class CapCutSTTError(RuntimeError):
    """剪映 STT 请求、轮询或字幕契约错误。"""


class CapCutSTTDependencyError(CapCutSTTError):
    """未安装 GitHub SDK。"""


@dataclass(frozen=True)
class CapCutSTTConfig:
    language: str = "zh-CN"
    translation_language: str = "zh-CN"
    use_translation: bool = False
    timeout_seconds: float = 180.0
    poll_interval_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "CapCutSTTConfig":
        def number(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError as exc:
                raise CapCutSTTError(f"{name} 必须是数字") from exc
            if value <= 0:
                raise CapCutSTTError(f"{name} 必须大于 0")
            return value

        return cls(
            language=os.environ.get("CAPCUT_STT_LANGUAGE", "zh-CN").strip() or "zh-CN",
            translation_language=os.environ.get("CAPCUT_STT_TRANSLATION_LANGUAGE", "zh-CN").strip() or "zh-CN",
            use_translation=os.environ.get("CAPCUT_STT_USE_TRANSLATION", "false").strip().lower() in {"1", "true", "yes"},
            timeout_seconds=number("CAPCUT_STT_TIMEOUT", 180.0),
            poll_interval_seconds=number("CAPCUT_STT_POLL_INTERVAL", 2.0),
        )


@dataclass(frozen=True)
class STTUtterance:
    """剪映返回的单条字幕，时间单位固定为毫秒。"""

    text: str
    start_ms: int
    end_ms: int
    words: tuple[dict[str, Any], ...] = ()


RequesterSleep = Callable[[float], None]


def _load_client() -> Any:
    try:
        from capcut_tts_api import CapCutClient
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - depends on deployment environment
        raise CapCutSTTDependencyError(
            "未安装 capcut-tts-api；请在当前 Python 环境执行："
            "python -m pip install capcut-tts-api"
        ) from exc
    # SDK 默认 requests.Session 不对连接级瞬时失败重试；在编导任务中这会把
    # 一次短暂的 TLS/连接抖动直接暴露为 ConnectTimeout。只允许一次连接/读
    # 重试，不创建新的业务任务，也不改变任务级最多重试一次的治理规则。
    session = requests.Session()
    # requests 不会读取 Windows WinHTTP 代理；本机的剪映上传链路可能依赖
    # 系统已配置的代理。显式环境变量优先，否则只读取 WinHTTP 的代理地址，
    # 不凭空切换业务接入点，也不影响未配置代理的环境。
    proxy = (
        os.environ.get("CAPCUT_STT_PROXY", "").strip()
        or os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
    )
    if not proxy and os.name == "nt":
        try:
            probe = subprocess.run(
                ["netsh", "winhttp", "show", "proxy"],
                capture_output=True,
                text=True,
                encoding="mbcs",
                errors="replace",
                timeout=3,
                check=False,
            )
            match = re.search(r"Proxy Server(?:\(s\)|s)?\s*:\s*(https?://)?([^\s]+)", probe.stdout or "", re.I)
            if match and match.group(2).lower() not in {"direct", "(none)"}:
                proxy = (match.group(1) or "http://") + match.group(2)
        except (OSError, subprocess.SubprocessError):
            proxy = ""
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    retry = Retry(
        total=1,
        connect=1,
        read=1,
        status=0,
        redirect=0,
        backoff_factor=0.2,
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return CapCutClient(session=session)


def _tasks(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    raw = data.get("tasks")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _task_id_token(task: Mapping[str, Any]) -> tuple[str, str]:
    task_id = str(task.get("id") or task.get("task_id") or "").strip()
    token = str(task.get("token") or "").strip()
    if not task_id or not token:
        raise CapCutSTTError("剪映 STT 任务响应缺少 id/token")
    return task_id, token


def _status(task: Mapping[str, Any]) -> str:
    return str(task.get("status") or task.get("state") or "").strip().lower()


def _int_ms(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CapCutSTTError(f"剪映 STT {field} 不能是布尔值")
    try:
        result = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise CapCutSTTError(f"剪映 STT {field} 必须是数字毫秒") from exc
    if result < 0:
        raise CapCutSTTError(f"剪映 STT {field} 不能为负数")
    return result


def _normalize_subtitles(client: Any, response: Mapping[str, Any]) -> list[STTUtterance]:
    try:
        parsed = client.extract_subtitles(dict(response))
    except Exception as exc:
        raise CapCutSTTError("剪映 STT 字幕解析失败") from exc
    raw_utterances = getattr(parsed, "utterances", None)
    if not isinstance(raw_utterances, list):
        raise CapCutSTTError("剪映 STT 响应缺少 utterances")
    result: list[STTUtterance] = []
    previous_end_ms: int | None = None
    for index, raw in enumerate(raw_utterances):
        text = str(getattr(raw, "text", "") or "").strip()
        if not text:
            continue
        start_ms = _int_ms(getattr(raw, "start_time", 0), f"utterances[{index}].start_time")
        end_ms = _int_ms(getattr(raw, "end_time", 0), f"utterances[{index}].end_time")
        if end_ms <= start_ms:
            raise CapCutSTTError(f"剪映 STT utterances[{index}] 时间窗口无效")
        if previous_end_ms is not None and start_ms < previous_end_ms:
            raise CapCutSTTError("剪映 STT utterances 时间顺序错误或窗口重叠")
        previous_end_ms = end_ms
        words_value = getattr(raw, "words", ())
        words: list[dict[str, Any]] = []
        if isinstance(words_value, list):
            for word in words_value:
                if isinstance(word, Mapping):
                    words.append(dict(word))
                else:
                    words.append({
                        "text": str(getattr(word, "text", "") or ""),
                        "start_ms": _int_ms(getattr(word, "start_time", 0), "word.start_time"),
                        "end_ms": _int_ms(getattr(word, "end_time", 0), "word.end_time"),
                    })
        result.append(STTUtterance(text=text, start_ms=start_ms, end_ms=end_ms, words=tuple(words)))
    return result


class CapCutSTTTransport:
    """使用剪映 STT 为每个本地旁白音频生成字幕候选。"""

    def __init__(
        self,
        *,
        client: Any | None = None,
        config: CapCutSTTConfig | None = None,
        sleep: RequesterSleep = time.sleep,
    ) -> None:
        self.client = client or _load_client()
        self.config = config or CapCutSTTConfig.from_env()
        self.sleep = sleep

    @classmethod
    def from_env(cls) -> "CapCutSTTTransport":
        return cls(config=CapCutSTTConfig.from_env())

    def transcribe_file(self, file_path: str | Path) -> tuple[list[STTUtterance], dict[str, Any]]:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size <= 0:
            raise CapCutSTTError(f"剪映 STT 音频文件不存在或为空：{path}")
        try:
            upload = self.client.upload_audio(path)
            vid = str(getattr(upload, "vid", "") or "").strip()
            md5 = str(getattr(upload, "md5", "") or "").strip()
            duration_ms = _int_ms(getattr(upload, "duration_ms", 0), "upload.duration_ms")
            if duration_ms <= 0:
                raise CapCutSTTError("剪映 VOD 上传响应缺少有效音频时长")
            if not vid or not md5:
                raise CapCutSTTError("剪映 VOD 上传响应缺少 vid/md5")
            created = self.client.create_stt_task(
                audio_vid=vid,
                audio_md5=md5,
                duration_ms=duration_ms,
                language=self.config.language,
                translation_language=self.config.translation_language,
                use_translation=self.config.use_translation,
            )
            tasks = _tasks(created)
            if not tasks:
                raise CapCutSTTError("剪映 STT 创建任务未返回 tasks")
            task_id, token = _task_id_token(tasks[0])
            deadline = time.monotonic() + self.config.timeout_seconds
            latest: Mapping[str, Any] = created
            while time.monotonic() < deadline:
                latest = self.client.query_stt_task(task_id, token)
                query_tasks = _tasks(latest)
                if query_tasks:
                    state = _status(query_tasks[0])
                    if state in SUCCESS_STATUSES:
                        return _normalize_subtitles(self.client, latest), {
                            "task_id": task_id,
                            "status": state,
                            "duration_ms": duration_ms,
                            "file_path": str(path),
                        }
                    if state in FAILED_STATUSES:
                        raise CapCutSTTError(f"剪映 STT 任务失败：{state}")
                self.sleep(self.config.poll_interval_seconds)
            raise CapCutSTTError(
                f"剪映 STT 任务超时：{task_id}（{self.config.timeout_seconds:g} 秒）"
            )
        except CapCutSTTError:
            raise
        except Exception as exc:
            raise CapCutSTTError(f"剪映 STT 请求失败：{type(exc).__name__}") from exc

    def transcribe_groups(
        self,
        groups: Sequence[Mapping[str, Any]],
        parallel_workers: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """按大分段顺序转录，输出可直接进入 DirectorLockedManifest 的字幕候选。"""

        started_at = time.perf_counter()
        normalized_groups: list[tuple[int, str, str, Mapping[str, Any]]] = []
        for group_index, group in enumerate(groups):
            group_id = str(group.get("group_id") or "").strip()
            audio_path = str(group.get("audio_path") or "").strip()
            if not group_id or not audio_path:
                raise CapCutSTTError(f"STT groups[{group_index}] 缺少 group_id/audio_path")
            normalized_groups.append((group_index, group_id, audio_path, group))

        if parallel_workers is not None:
            if isinstance(parallel_workers, bool) or not isinstance(parallel_workers, int) or parallel_workers < 1:
                raise CapCutSTTError("STT 并行 worker 数必须是正整数")
            configured_workers = parallel_workers
        else:
            raw_workers = (
                os.environ.get("CAPCUT_STT_PARALLEL_WORKERS", "").strip()
                or os.environ.get("DIRECTOR_STT_PARALLEL_WORKERS", "").strip()
            )
            try:
                configured_workers = int(raw_workers) if raw_workers else STT_PARALLEL_WORKERS
            except ValueError:
                configured_workers = STT_PARALLEL_WORKERS
            if configured_workers < 1:
                configured_workers = STT_PARALLEL_WORKERS

        worker_count = min(configured_workers, len(normalized_groups)) if normalized_groups else 1
        transcription_results: dict[int, tuple[list[STTUtterance], dict[str, Any]]] = {}
        errors: list[tuple[int, Exception]] = []
        if normalized_groups:
            start_events = [threading.Event() for _ in normalized_groups]

            def transcribe_one(
                local_index: int,
                group_id: str,
                audio_path: str,
            ) -> tuple[int, list[STTUtterance], dict[str, Any]]:
                # 让请求按输入顺序进入 transport，随后仍允许网络轮询并行。
                if local_index:
                    start_events[local_index - 1].wait()
                start_events[local_index].set()
                utterances, report = self.transcribe_file(audio_path)
                return local_index, utterances, report

            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="capcut-stt") as executor:
                futures = {
                    executor.submit(transcribe_one, local_index, group_id, audio_path): local_index
                    for local_index, (_group_index, group_id, audio_path, _group) in enumerate(normalized_groups)
                }
                for future in as_completed(futures):
                    local_index = futures[future]
                    try:
                        result_index, utterances, report = future.result()
                        transcription_results[result_index] = (utterances, report)
                    except Exception as error:
                        errors.append((local_index, error))
        if errors:
            _, error = min(errors, key=lambda item: item[0])
            raise error

        captions: list[dict[str, Any]] = []
        task_reports: list[dict[str, Any]] = []
        group_timelines: list[dict[str, int]] = []
        cursor_us = 0
        for local_index, (group_index, group_id, _audio_path, group) in enumerate(normalized_groups):
            utterances, report = transcription_results[local_index]
            duration_ms = _int_ms(report.get("duration_ms"), f"STT {group_id}.duration_ms")
            if duration_ms <= 0:
                raise CapCutSTTError(f"STT {group_id} 缺少有效剪映音频时长")
            start_us = cursor_us
            end_us = start_us + duration_ms * 1_000
            cursor_us = end_us
            group_timeline = {"start": start_us, "end": end_us}
            group_timelines.append(group_timeline)
            task_reports.append({
                "group_id": group_id,
                **report,
                "utterance_count": len(utterances),
                "timeline": group_timeline,
            })
            for utterance_index, utterance in enumerate(utterances):
                relative_start_us = utterance.start_ms * 1_000
                relative_end_us = utterance.end_ms * 1_000
                start = start_us + relative_start_us
                end = start_us + relative_end_us
                # 剪映端字幕可比其上传音频时长多出少量尾部静音；允许小误差并
                # 截回剪映自身的音频边界，超过 100ms 则认为是时间线契约错误。
                if start < start_us or end > end_us:
                    if start < start_us - 100_000 or end > end_us + 100_000:
                        raise CapCutSTTError(f"剪映 STT {group_id} 字幕超出锁定时间线")
                    start = max(start_us, start)
                    end = min(end_us, end)
                if end <= start:
                    continue
                normalized_words: list[dict[str, Any]] = []
                for raw_word in utterance.words:
                    if not isinstance(raw_word, Mapping):
                        continue
                    raw_start = raw_word.get("start_ms", raw_word.get("start_time"))
                    raw_end = raw_word.get("end_ms", raw_word.get("end_time"))
                    if raw_start is None or raw_end is None:
                        continue
                    try:
                        word_start = start_us + _int_ms(raw_start, "word.start_time") * 1_000
                        word_end = start_us + _int_ms(raw_end, "word.end_time") * 1_000
                    except CapCutSTTError:
                        continue
                    if word_end <= word_start:
                        continue
                    word = dict(raw_word)
                    word["start_us"] = max(start, word_start)
                    word["end_us"] = min(end, word_end)
                    if word["end_us"] > word["start_us"]:
                        normalized_words.append(word)
                captions.append({
                    "caption_id": f"{group_id}.stt.{utterance_index + 1:03d}",
                    "group_id": group_id,
                    "text": utterance.text,
                    "start_us": start,
                    "end_us": end,
                    "reference_text": str(group.get("reference_text") or "").strip(),
                    "source_node": "capcut_stt",
                    "words": normalized_words,
                })
        if not captions:
            raise CapCutSTTError("剪映 STT 未返回有效字幕")
        return captions, {
            "status": "succeeded",
            "provider": "capcut_stt",
            "timeline_source": "capcut_stt_upload_duration_and_utterances",
            "group_count": len(groups),
            "caption_count": len(captions),
            "tasks": task_reports,
            "group_timelines": group_timelines,
            "total_timeline": {"start": 0, "end": cursor_us},
            "timeline_unit": "microseconds",
            "performance": {
                "stt_parallel_workers": worker_count,
                "stt_completed_groups": len(normalized_groups),
                "stt_elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            },
        }


__all__ = [
    "CapCutSTTConfig",
    "CapCutSTTDependencyError",
    "CapCutSTTError",
    "CapCutSTTTransport",
    "STT_PARALLEL_WORKERS",
    "STTUtterance",
]
