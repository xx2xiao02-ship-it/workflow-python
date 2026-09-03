"""版本化音色目录存储。

目录是业务元数据，不保存任何 API 密钥。首次读取时从内置官方目录种子化，
后续使用原子替换写入，避免页面保存失败破坏上一版可用目录。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .voice_catalog import (
    DEFAULT_VOICE_CATALOG,
    VoiceCatalogError,
    effective_speaker_id,
    normalize_voice_catalog,
)


class VoiceCatalogStoreError(RuntimeError):
    """音色目录无法读取、写入或违反唯一性约束。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VoiceCatalogStore:
    """单文件音色目录；同一进程内串行化读写。"""

    schema_version = "voice-catalog-v1"

    def __init__(self, path: str | Path, *, seed: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.seed = dict(seed or DEFAULT_VOICE_CATALOG)
        self._lock = threading.RLock()

    def _seeded(self) -> dict[str, Any]:
        value = normalize_voice_catalog(self.seed)
        timestamp = _now()
        for voice in value["voices"]:
            if not str(voice.get("created_at") or "").strip():
                voice["created_at"] = timestamp
            if not str(voice.get("updated_at") or "").strip():
                voice["updated_at"] = timestamp
            voice.setdefault("catalog_version", value["version"])
        value["schema_version"] = self.schema_version
        return value

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.is_file():
                return self._seeded()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise VoiceCatalogStoreError(f"音色目录读取失败：{self.path}") from exc
            try:
                value = normalize_voice_catalog(raw)
            except VoiceCatalogError as exc:
                raise VoiceCatalogStoreError(f"音色目录契约无效：{exc}") from exc
            value["schema_version"] = str(raw.get("schema_version") or self.schema_version)
            return value

    def save(self, catalog: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                value = normalize_voice_catalog(catalog)
            except VoiceCatalogError as exc:
                raise VoiceCatalogStoreError(f"音色目录契约无效：{exc}") from exc
            value["schema_version"] = self.schema_version
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                Path(temp_name).replace(self.path)
            except OSError as exc:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
                raise VoiceCatalogStoreError(f"音色目录写入失败，旧目录仍保留：{self.path}") from exc
            return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = self.load()
            if not self.path.is_file():
                value = self.save(value)
            return value

    def register(self, voice: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.load()
            voice_key = str(voice.get("voice_key") or "").strip()
            speaker_id = str(voice.get("speaker_id") or "").strip()
            if not voice_key or not speaker_id:
                raise VoiceCatalogStoreError("登记音色必须提供 voice_key 和 speaker_id")
            if any(item["voice_key"] == voice_key for item in current["voices"]):
                raise VoiceCatalogStoreError("voice_key 已存在；稳定标识不能覆盖")
            candidate_identity = effective_speaker_id(voice)
            if not candidate_identity:
                raise VoiceCatalogStoreError("登记音色必须提供实际音色代号")
            if any(effective_speaker_id(item) == candidate_identity for item in current["voices"]):
                raise VoiceCatalogStoreError("实际音色代号已存在；不能重复登记")
            timestamp = _now()
            item = dict(voice)
            if not str(item.get("created_at") or "").strip():
                item["created_at"] = timestamp
            if not str(item.get("updated_at") or "").strip():
                item["updated_at"] = timestamp
            item.setdefault("catalog_version", current["version"])
            current["voices"].append(item)
            return self.save(current)

    def update(self, voice_key: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.load()
            target = next((item for item in current["voices"] if item["voice_key"] == voice_key), None)
            if target is None:
                raise VoiceCatalogStoreError("未找到指定 voice_key")
            if "voice_key" in patch and str(patch.get("voice_key") or "").strip() != voice_key:
                raise VoiceCatalogStoreError("voice_key 创建后不可修改")
            if "speaker_id" in patch or "custom_speaker_id" in patch:
                candidate = dict(target)
                candidate.update({key: value for key, value in patch.items() if key in {"speaker_id", "custom_speaker_id"}})
                speaker_id = str(candidate.get("speaker_id") or "").strip()
                identity = effective_speaker_id(candidate)
                if not speaker_id or not identity or any(
                    item is not target and effective_speaker_id(item) == identity
                    for item in current["voices"]
                ):
                    raise VoiceCatalogStoreError("speaker_id/custom_speaker_id 为空或已被其它音色使用")
            for key, value in patch.items():
                if key != "voice_key":
                    target[key] = value
            if patch.get("enabled") is False:
                defaults = current.get("defaults") if isinstance(current.get("defaults"), dict) else {}
                for gender, default_key in list(defaults.items()):
                    if default_key == voice_key:
                        defaults[gender] = ""
            target["updated_at"] = _now()
            return self.save(current)


__all__ = ["VoiceCatalogStore", "VoiceCatalogStoreError"]
