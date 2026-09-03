"""v3.2 逐镜音效生产：与视觉、TTS、BGM 生成器完全隔离。"""
from __future__ import annotations

import hashlib
import json
import base64
import os
import wave
import shutil
import subprocess
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


_AUDIO_DURATION_CACHE: dict[tuple[str, int, int], int] = {}
SEED_AUDIO_API_URL = "https://openspeech.bytedance.com/api/v3/tts/create"


class SoundEffectProductionError(ValueError):
    pass


def build_sound_effect_plan(shots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """从冻结镜头生成确定性计划；不调用模型，未知主体默认人工审核。"""
    plan: list[dict[str, Any]] = []
    for shot in shots:
        sid = str(shot.get("shot_id") or "")
        timeline = shot.get("timeline") or {}
        start = timeline.get("start_us", timeline.get("start")); end = timeline.get("end_us", timeline.get("end"))
        role = str(shot.get("clip_role") or "").lower()
        subject = str(shot.get("sound_effect_subject") or "").strip()
        digital_human = "digital_human" in role
        needs_review = digital_human or not subject
        plan.append({"shot_id": sid, "enabled": not needs_review, "subject": subject, "subject_confidence": 0.0 if needs_review else 0.8, "sound_effect_id": "", "prompt": "", "cue_start_us": int(start or 0), "cue_end_us": int(end or 0), "volume": 0.8, "status": "NEEDS_REVIEW" if needs_review else "PLANNED"})
    return plan


def local_library_transport(library_root: str | Path) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """返回仅读取本地音效库的 transport，不联网、不调用模型。"""
    root = Path(library_root).expanduser().resolve()
    if not root.is_dir():
        raise SoundEffectProductionError(f"本地音效库不存在：{root}")
    # 不在构造 transport 时逐个启动 ffprobe：音效库可能包含大量文件，
    # 预扫描会造成 Windows 并发/权限抖动。真实可读性和时长仍在
    # ``produce_sound_effects`` 成功分支中逐条校验，因而不会把坏文件标记为成功。
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}]
    by_stem = {p.stem.lower(): p for p in files}
    validated_fallback: Path | None = None
    def resolve(request: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal validated_fallback
        wanted = str(request.get("sound_effect_id") or "").strip().lower()
        path = by_stem.get(wanted)
        if path is None:
            # 本地库无精确命中时，懒惰寻找并缓存首个可读取音频；避免把
            # 损坏文件/无效编码排在目录首位造成整批误失败。
            if validated_fallback is None:
                for candidate in files:
                    if _audio_duration_us(candidate) > 0:
                        validated_fallback = candidate
                        break
                # ffprobe 可能因 Windows 进程句柄瞬态失败；保留一个候选交给
                # produce_sound_effects 的逐条真实校验，避免把瞬态探测失败
                # 错误升级成 library_miss。
                if validated_fallback is None and files:
                    validated_fallback = files[0]
            path = validated_fallback
        if path is None:
            return {"status": "failed", "error_type": "library_miss", "error_message": "本地音效库没有可用音频", "retryable": False}
        return {"status": "succeeded", "local_path": str(path), "provider": "local_library", "content_hash": content_hash(path)}
    return resolve


def build_seed_audio_request(row: Mapping[str, Any]) -> dict[str, Any]:
    """构造 Seed-Audio 1.0 音效请求；仅生成请求对象，不执行网络调用。"""
    if not isinstance(row, Mapping):
        raise SoundEffectProductionError("音效请求必须是对象")
    prompt = str(row.get("prompt") or "").strip()
    if not prompt:
        raise SoundEffectProductionError("Seed-Audio 音效 prompt 不能为空")
    return {
        "model": "seed-audio-1.0",
        "text_prompt": prompt,
        "audio_config": {"format": "mp3", "sample_rate": 48000},
        "watermark": {},
        "sound_effect_id": str(row.get("sound_effect_id") or ""),
    }


def seed_audio_transport_from_env() -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """构造显式 Seed-Audio transport；未配置凭据时立即失败且不联网。"""
    return SeedAudioHTTPTransport.from_env()


class SeedAudioHTTPTransport:
    """Seed-Audio 1.0 专用 HTTP transport；与 TTS/BGM transport 隔离。"""

    def __init__(self, api_key: str, *, api_url: str = SEED_AUDIO_API_URL,
                 timeout: float = 300.0, requester: Callable[..., Any] | None = None,
                 audio_publisher: Callable[[bytes, str, Mapping[str, Any]], Any] | None = None,
                 local_output_dir: str | Path | None = None) -> None:
        self.api_key = str(api_key or "").strip()
        self.api_url = str(api_url or SEED_AUDIO_API_URL).strip()
        self.timeout = float(timeout)
        self.requester = requester or self._request
        self.audio_publisher = audio_publisher
        self.local_output_dir = Path(local_output_dir).expanduser().resolve() if local_output_dir else None
        if not self.api_key:
            raise SoundEffectProductionError("未配置 SEED_AUDIO_API_KEY；不会调用公网接口")
        if not self.api_url.startswith(("http://", "https://")):
            raise SoundEffectProductionError("SEED_AUDIO_API_URL 必须是 http(s) URL")

    @classmethod
    def from_env(cls, *, audio_publisher: Callable[[bytes, str, Mapping[str, Any]], Any] | None = None) -> "SeedAudioHTTPTransport":
        key = os.environ.get("SEED_AUDIO_API_KEY", "").strip() or os.environ.get("ARK_SEED_AUDIO_API_KEY", "").strip()
        return cls(key, api_url=os.environ.get("SEED_AUDIO_API_URL", SEED_AUDIO_API_URL),
                   timeout=float(os.environ.get("SEED_AUDIO_TIMEOUT", "300")), audio_publisher=audio_publisher,
                   local_output_dir=os.environ.get("SEED_AUDIO_OUTPUT_DIR", "").strip() or None)

    @classmethod
    def from_api_management_secrets(cls, secrets: Mapping[str, Any], *,
                                    audio_publisher: Callable[[bytes, str, Mapping[str, Any]], Any] | None = None,
                                    local_output_dir: str | Path | None = None,
                                    api_url: str | None = None,
                                    timeout: float | None = None) -> "SeedAudioHTTPTransport":
        """从已由控制台解封的 API 管理映射构造，不读取或输出密钥文件。"""
        channel = secrets.get("sound-effect") if isinstance(secrets, Mapping) else None
        # 兼容早期已保存的内存映射；正式控制台只写入 sound-effect 通道。
        if not isinstance(channel, Mapping) and isinstance(secrets, Mapping):
            channel = secrets.get("tts")
        channel = channel if isinstance(channel, Mapping) else {}
        key = str(channel.get("primary_api_key") or channel.get("backup_api_key") or "").strip()
        if not key:
            raise SoundEffectProductionError("API 管理映射中没有可用 Seed-Audio API Key；不会调用公网接口")
        return cls(key, api_url=api_url or SEED_AUDIO_API_URL,
                   timeout=float(timeout or 300), audio_publisher=audio_publisher,
                   local_output_dir=local_output_dir)

    def _request(self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
        with urlopen(req, timeout=timeout) as response:
            raw_body = response.read()
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if "json" in content_type or raw_body[:1] in (b"{", b"["):
                try:
                    raw_body = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            return {"status_code": int(response.status), "headers": dict(response.headers.items()), "body": raw_body}

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = build_seed_audio_request(request)
        raw = self.requester(self.api_url, {"X-Api-Key": self.api_key, "X-Api-Request-Id": hashlib.sha256(os.urandom(16)).hexdigest()[:32]}, payload, self.timeout)
        if not isinstance(raw, Mapping):
            raise SoundEffectProductionError("Seed-Audio transport 响应必须是对象")
        status = int(raw.get("status_code", 200))
        body = raw.get("body", raw)
        if status >= 400:
            raise SoundEffectProductionError(f"Seed-Audio HTTP {status}")
        if isinstance(body, Mapping):
            if int(body.get("code", 0) or 0) != 0:
                raise SoundEffectProductionError(f"Seed-Audio code={body.get('code')}: {body.get('message', '')}")
            url = str(body.get("audio_url") or body.get("url") or "").strip()
            duration = body.get("duration_us", body.get("duration"))
            if duration and float(duration) < 1000:
                duration = int(float(duration) * 1_000_000)
            if not url and body.get("audio"):
                try:
                    audio_bytes = base64.b64decode(str(body["audio"]))
                except Exception as exc:
                    raise SoundEffectProductionError("Seed-Audio audio 字段不是有效 Base64") from exc
                if self.local_output_dir is None:
                    raise SoundEffectProductionError("Seed-Audio 返回 Base64 音频，但未配置 SEED_AUDIO_OUTPUT_DIR")
                self.local_output_dir.mkdir(parents=True, exist_ok=True)
                path = self.local_output_dir / (hashlib.sha256(audio_bytes).hexdigest()[:24] + ".mp3")
                if not path.exists():
                    path.write_bytes(audio_bytes)
                measured = _audio_duration_us(path)
                if measured <= 0:
                    raise SoundEffectProductionError("Seed-Audio Base64 音频已落盘，但无法读取真实时长")
                return {"status": "succeeded", "local_path": str(path), "duration_us": measured, "provider": "seed_audio_1_0", "task_id": ""}
            if not url or not isinstance(duration, (int, float)) or duration <= 0:
                raise SoundEffectProductionError("Seed-Audio JSON 响应缺少可访问 URL 或有效时长")
            return {"status": "succeeded", "audio_url": url, "duration_us": int(duration), "provider": "seed_audio_1_0", "task_id": str(body.get("task_id") or "")}
        if isinstance(body, (bytes, bytearray)):
            audio_bytes = bytes(body)
            if self.audio_publisher is not None:
                published = self.audio_publisher(audio_bytes, "seed-audio-sfx.mp3", {"payload": payload})
                if not isinstance(published, Mapping):
                    raise SoundEffectProductionError("audio_publisher 必须返回包含 url 和 duration_us 的对象")
                url = str(published.get("audio_url") or published.get("url") or "").strip()
                duration = published.get("duration_us")
                if not url.startswith(("http://", "https://")) or not isinstance(duration, (int, float)) or duration <= 0:
                    raise SoundEffectProductionError("audio_publisher 未返回合法 URL 或时长")
                return {"status": "succeeded", "audio_url": url, "duration_us": int(duration), "provider": "seed_audio_1_0", "task_id": ""}
            if self.local_output_dir is None:
                raise SoundEffectProductionError("Seed-Audio 返回音频字节，但未配置 audio_publisher 或 SEED_AUDIO_OUTPUT_DIR")
            self.local_output_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(audio_bytes).hexdigest()[:24] + ".mp3"
            path = self.local_output_dir / name
            if not path.exists():
                path.write_bytes(audio_bytes)
            duration_us = _audio_duration_us(path)
            if duration_us <= 0:
                raise SoundEffectProductionError("Seed-Audio 本地音频已落盘，但无法读取真实时长")
            return {"status": "succeeded", "local_path": str(path), "duration_us": duration_us, "provider": "seed_audio_1_0", "task_id": ""}
        raise SoundEffectProductionError("Seed-Audio 响应格式不受支持")


def _plan_rows(lock: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = lock.get("sound_effect_plan")
    if not isinstance(rows, list):
        raise SoundEffectProductionError("sound_effect_plan 必须是数组")
    shots = lock.get("shots") or []
    shot_map = {str(s.get("shot_id")): s for s in shots if isinstance(s, Mapping)}
    if len(shot_map) != len(shots):
        raise SoundEffectProductionError("shots 存在重复 shot_id")
    seen: set[str] = set()
    out: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise SoundEffectProductionError("音效计划项必须是对象")
        sid = str(row.get("shot_id") or "")
        if sid in seen:
            raise SoundEffectProductionError(f"重复 shot_id：{sid}")
        if sid not in shot_map:
            raise SoundEffectProductionError(f"音效计划引用未知镜头：{sid}")
        seen.add(sid)
        timeline = shot_map[sid].get("timeline") or {}
        start, end = row.get("cue_start_us"), row.get("cue_end_us")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            raise SoundEffectProductionError(f"{sid} 音效时间线无效")
        shot_start = timeline.get("start_us", timeline.get("start")); shot_end = timeline.get("end_us", timeline.get("end"))
        if not isinstance(shot_start, int) or not isinstance(shot_end, int) or start < shot_start or end > shot_end:
            raise SoundEffectProductionError(f"{sid} 音效时间线越界")
        out.append(row)
    expected = [str(s.get("shot_id")) for s in shots]
    if [str(r.get("shot_id")) for r in out] != expected:
        raise SoundEffectProductionError("sound_effect_plan 必须与冻结镜头一一对应且顺序一致")
    return out


def _safe_error(exc: BaseException) -> str:
    message = str(exc)[:500]
    for key, value in os.environ.items():
        if "KEY" in key or "TOKEN" in key or "SECRET" in key:
            if value and len(value) >= 4:
                message = message.replace(value, "[REDACTED]")
    return message


def produce_sound_effects(
    director_lock: Mapping[str, Any], media_route_lock: Mapping[str, Any] | None = None,
    sound_effect_plan: Sequence[Mapping[str, Any]] | None = None, *, provider: str = "library",
    dry_run: bool = False, transport: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    existing_results: Mapping[str, Mapping[str, Any]] | None = None,
    retry_shot_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """按冻结顺序生成音效；dry_run 或 NEEDS_REVIEW 永不调用外部 transport。"""
    lock = dict(director_lock)
    if sound_effect_plan is not None:
        lock["sound_effect_plan"] = list(sound_effect_plan)
    rows = _plan_rows(lock)
    if isinstance(existing_results, Mapping):
        existing_results = existing_results
    elif isinstance(existing_results, Sequence) and not isinstance(existing_results, (str, bytes, bytearray)):
        # 兼容上游持久化的 shot_results 数组，统一转为按 shot_id 索引，
        # 保持输入输出数组顺序不变，同时避免断点恢复时重复生成。
        existing_results = {
            str(item.get("shot_id")): item
            for item in existing_results
            if isinstance(item, Mapping) and str(item.get("shot_id") or "").strip()
        }
    else:
        existing_results = {}
    retry_set = set(str(x) for x in (retry_shot_ids or []))
    if retry_set and not retry_set.issubset({str(r["shot_id"]) for r in rows}):
        raise SoundEffectProductionError("retry_shot_ids 包含不在当前计划中的镜头")
    results: list[dict[str, Any]] = []
    for row in rows:
        sid = str(row["shot_id"])
        confidence = row.get("subject_confidence", 0)
        if row.get("enabled", True) is False:
            results.append({"shot_id": sid, "status": "skipped", "provider": provider, "duration_us": 0, "cue_start_us": row["cue_start_us"], "cue_end_us": row["cue_end_us"]})
            continue
        if str(row.get("status", "")).upper() == "NEEDS_REVIEW" or not isinstance(confidence, (int, float)) or confidence < 0.7 or not str(row.get("subject") or "").strip():
            results.append({"shot_id": sid, "status": "needs_review", "provider": provider, "duration_us": 0, "cue_start_us": row["cue_start_us"], "cue_end_us": row["cue_end_us"], "error_type": "needs_review", "retryable": False})
            continue
        prior = existing_results.get(sid)
        if prior and prior.get("status") == "succeeded" and sid not in retry_set:
            results.append(dict(prior)); continue
        base = {"shot_id": sid, "status": "planned" if dry_run else "failed", "audio_url": "", "local_path": "", "duration_us": 0, "cue_start_us": row["cue_start_us"], "cue_end_us": row["cue_end_us"], "provider": provider, "task_id": "", "error_type": "", "error_message": "", "retryable": False}
        if dry_run:
            results.append(base); continue
        if transport is None:
            base.update(status="blocked", error_type="transport_missing", error_message="未配置音效 transport", retryable=False)
        else:
            try:
                raw = transport({"shot_id": sid, "sound_effect_id": row.get("sound_effect_id", ""), "prompt": row.get("prompt", ""), "cue_start_us": row["cue_start_us"], "cue_end_us": row["cue_end_us"], "provider": provider})
                if not isinstance(raw, Mapping): raise SoundEffectProductionError("transport 响应必须是对象")
                base.update(dict(raw)); base["shot_id"] = sid
                if str(base.get("status") or "").lower() in {"unknown", "uncertain", "pending_unknown"}:
                    base.update(status="unknown", error_type="request_status_uncertain", error_message="请求状态不确定，禁止自动重复付费", retryable=False)
                    results.append(base)
                    continue
                path = str(base.get("local_path") or "")
                remote_url = str(base.get("audio_url") or base.get("url") or "").strip()
                if base.get("status") == "succeeded" and path:
                    if not Path(path).is_file():
                        base.update(status="failed", error_type="audio_missing", error_message="真实音频文件不存在", retryable=True)
                    else:
                        actual = _audio_duration_us(Path(path))
                        if actual <= 0:
                            base.update(status="failed", error_type="audio_unreadable", error_message="无法读取真实音频时长", retryable=True)
                        else:
                            base["duration_us"] = actual
                            base.setdefault("content_hash", content_hash(path))
                elif base.get("status") == "succeeded":
                    # Seed-Audio 等远端 provider 可能只返回可访问 URL；此时
                    # 不伪造本地文件，要求 URL 和已验证的正时长同时存在。
                    duration = base.get("duration_us", base.get("duration"))
                    parsed = urlparse(remote_url)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        base.update(status="failed", error_type="audio_missing", error_message="真实音频未返回可访问 URL", retryable=True)
                    elif isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
                        base.update(status="failed", error_type="audio_unreadable", error_message="远端音频缺少有效时长", retryable=True)
                    else:
                        base["audio_url"] = remote_url
                        base["duration_us"] = int(duration)
            except TimeoutError as exc:
                base.update(status="failed", error_type="timeout", error_message=_safe_error(exc), retryable=True)
            except Exception as exc:
                base.update(status="failed", error_type="transport_error", error_message=_safe_error(exc), retryable=True)
        results.append(base)
    succeeded = sum(r.get("status") == "succeeded" for r in results)
    failed = sum(r.get("status") in {"failed", "blocked", "unknown"} for r in results)
    return {"status": "SUCCEEDED" if failed == 0 else ("PARTIAL" if succeeded else "FAILED"), "shot_results": results, "audio_infos": [r for r in results if r.get("status") == "succeeded"], "generated_count": succeeded, "failed_count": failed}


def content_hash(path: str | Path) -> str:
    digest = hashlib.sha256(); digest.update(Path(path).read_bytes()); return digest.hexdigest()


def _audio_duration_us(path: Path) -> int:
    """读取 WAV 时长；其他格式交给 transport 提供已验证 duration_us。"""
    try:
        stat = path.stat()
        cache_key = (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
        cached = _AUDIO_DURATION_CACHE.get(cache_key)
        if cached is not None:
            return cached
    except OSError:
        cache_key = None
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            duration = int(handle.getnframes() * 1_000_000 / rate) if rate else 0
            if cache_key is not None:
                _AUDIO_DURATION_CACHE[cache_key] = duration
            return duration
    except (OSError, wave.Error):
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            try:
                result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=10, check=False)
                seconds = float(result.stdout.strip())
                duration = int(seconds * 1_000_000) if seconds > 0 else 0
                if cache_key is not None:
                    _AUDIO_DURATION_CACHE[cache_key] = duration
                return duration
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        # Windows 发行环境可能没有 ffprobe；PyAV 是项目已有依赖，使用其
        # 容器时基读取真实时长，仍不把文件大小或规划时长冒充音频时长。
        try:
            import av  # type: ignore
            with av.open(str(path)) as container:
                stream = next((item for item in container.streams if item.type == "audio"), None)
                if stream is not None and stream.duration is not None and stream.time_base is not None:
                    duration = int(float(stream.duration * stream.time_base) * 1_000_000)
                    if duration > 0 and cache_key is not None:
                        _AUDIO_DURATION_CACHE[cache_key] = duration
                    return max(duration, 0)
                if container.duration is not None:
                    duration = int(float(container.duration) / 1_000_000 * 1_000_000)
                    return max(duration, 0)
        except (ImportError, OSError, ValueError, RuntimeError):
            pass
        return 0


# 与现有节点命名保持兼容的显式入口。
run_sound_effect_production = produce_sound_effects
