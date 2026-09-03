"""本地风格包及二创审核产物仓库。"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class StylePackageStoreError(RuntimeError):
    pass


class StylePackageApprovalConflict(StylePackageStoreError):
    """The profile is valid but cannot be promoted to a formal asset yet."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


PENDING_PROFILE_OVERVIEW = "基于已采集公开样本提炼的可复用表达规则，待人工审核。"
APPROVED_PROFILE_OVERVIEW = "基于已采集公开样本提炼的可复用表达规则，可用于文案创作。"


class StylePackageStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.profiles_dir = root / "profiles"
        self.profile_versions_dir = root / "profile_versions"
        self.archived_profiles_dir = root / "archived_profiles"
        self.collections_dir = root / "collections"
        self.rewrites_dir = root / "rewrites"
        self.archived_rewrites_dir = root / "archived_rewrites"

    @staticmethod
    def _safe_profile_id(style_profile_id: str) -> str:
        profile_id = str(style_profile_id or "").strip()
        if not profile_id or Path(profile_id).name != profile_id or ".." in profile_id or "/" in profile_id or "\\" in profile_id:
            raise StylePackageStoreError("风格包编号不合法")
        return profile_id

    def _profile_path(self, style_profile_id: str) -> Path:
        return self.profiles_dir / f"{self._safe_profile_id(style_profile_id)}.json"

    def _profile_version_path(self, style_profile_id: str, version_id: str) -> Path:
        profile_id = self._safe_profile_id(style_profile_id)
        version = str(version_id or "").strip()
        if not version or Path(version).name != version or ".." in version or "/" in version or "\\" in version:
            raise StylePackageStoreError("风格包版本编号不合法")
        return self.profile_versions_dir / profile_id / f"{version}.json"

    @staticmethod
    def _profile_enabled(record: dict[str, Any]) -> bool:
        value = record.get("enabled")
        if value is None:
            return str(record.get("review_status") or "").upper() == "APPROVED"
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}
        return bool(value)

    def _ensure_profile_history(self, record: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        """为早期单文件风格包补建版本索引，保留旧字段契约。"""

        profile_id = self._safe_profile_id(str(record.get("style_profile_id") or ""))
        history = record.get("version_history") if isinstance(record.get("version_history"), list) else []
        current = str(record.get("current_version") or record.get("style_profile_version") or "").strip()
        if history and current:
            return record
        if not current:
            current = f"style-v1-{profile_id}"
            record["style_profile_version"] = current
        record["current_version"] = current
        record["enabled"] = self._profile_enabled(record)
        record["version_history"] = history
        record["version_count"] = len(history)
        if persist:
            self._record_profile_version(record, reason="legacy_import", version_id=current)
        return record

    def _record_profile_version(
        self,
        record: dict[str, Any],
        *,
        reason: str,
        reviewer: str = "",
        notes: str = "",
        version_id: str = "",
    ) -> dict[str, Any]:
        profile_id = self._safe_profile_id(str(record.get("style_profile_id") or ""))
        history = record.get("version_history") if isinstance(record.get("version_history"), list) else []
        existing_ids = {str(item.get("version_id") or "") for item in history if isinstance(item, dict)}
        if not version_id:
            version_number = max(
                [int(item.get("version_number") or 0) for item in history if isinstance(item, dict)] or [0]
            ) + 1
            version_id = f"style-v{version_number}-{uuid4().hex[:12]}"
        else:
            version_number = max(
                [int(item.get("version_number") or 0) for item in history if isinstance(item, dict)] or [0]
            ) + (0 if version_id in existing_ids else 1)
            if not history:
                version_number = 1
        snapshot = dict(record)
        snapshot.pop("version_history", None)
        snapshot.pop("version_count", None)
        snapshot["style_profile_version"] = version_id
        snapshot["current_version"] = version_id
        snapshot["enabled"] = self._profile_enabled(record)
        self._write(self._profile_version_path(profile_id, version_id), snapshot)
        if version_id not in existing_ids:
            history.append({
                "version_id": version_id,
                "version_number": version_number,
                "created_at": _now(),
                "reason": str(reason or "update"),
                "reviewer": str(reviewer or "").strip(),
                "notes": str(notes or "").strip(),
                "review_status": str(record.get("review_status") or ""),
                "enabled": self._profile_enabled(record),
                "sample_count": int(record.get("sample_count") or 0),
                "creator_name": str(record.get("creator_name") or ""),
                "rollback_of": str(record.get("rollback_of") or ""),
            })
        record["style_profile_version"] = version_id
        record["current_version"] = version_id
        record["version_history"] = history[-50:]
        record["version_count"] = len(record["version_history"])
        self._write(self._profile_path(profile_id), record)
        return record

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StylePackageStoreError(f"无法读取本地风格包：{path.name}") from exc
        if not isinstance(data, dict):
            raise StylePackageStoreError("本地风格包格式错误")
        return data

    def save_collection(self, report: dict[str, Any]) -> str:
        collection_id = "collection_" + uuid4().hex[:12]
        self._write(self.collections_dir / f"{collection_id}.json", {"collection_id": collection_id, "created_at": _now(), **report})
        return collection_id

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        collection_key = str(collection_id or "").strip()
        if not collection_key or Path(collection_key).name != collection_key or ".." in collection_key:
            raise StylePackageStoreError("采集记录编号不合法")
        path = self.collections_dir / f"{collection_key}.json"
        if not path.is_file():
            raise StylePackageStoreError("找不到对应的采集记录")
        return self._read(path)

    def save_profile(
        self,
        *,
        profile: dict[str, Any],
        creator_url: str,
        collection_id: str,
        sample_count: int,
        creator_name: str = "",
    ) -> dict[str, Any]:
        style_profile_id = "style_" + uuid4().hex[:12]
        name = creator_name.strip()
        stored_profile = dict(profile)
        # The package is an asset of a creator, so its visible name must be
        # stable and human-recognizable instead of a model-generated label.
        if name:
            stored_profile["profile_name"] = name
        style_version = f"style-v1-{style_profile_id}"
        record = {
            "style_profile_id": style_profile_id,
            # A stable asset version is persisted with every profile so a
            # rewrite can prove exactly which approved package it consumed.
            "style_profile_version": style_version,
            "current_version": style_version,
            "version_history": [],
            "version_count": 0,
            # 新生成资产默认不可用于生产工作台，必须经过后台审核并通过
            # Writing-DNA 样本门禁后才能启用。
            "enabled": False,
            "created_at": _now(),
            "review_status": "PENDING_USER_REVIEW",
            "creator_url": creator_url,
            "creator_name": name,
            "collection_id": collection_id,
            "sample_count": sample_count,
            "style_profile": stored_profile,
        }
        self._write(self.profiles_dir / f"{style_profile_id}.json", record)
        return self._record_profile_version(record, reason="created", version_id=style_version)

    def list_profiles(self) -> list[dict[str, Any]]:
        if not self.profiles_dir.exists():
            return []
        records = [self.get_profile(path.stem) for path in self.profiles_dir.glob("style_*.json")]
        legacy_usage = self._legacy_profile_usage()
        for record in records:
            profile_id = str(record.get("style_profile_id") or "").strip()
            stored_usage = str(record.get("last_used_at") or "").strip()
            historical_usage = legacy_usage.get(profile_id, "")
            if historical_usage and historical_usage > stored_usage:
                # Keep old rewrite history visible in ordering without
                # rewriting every historical profile file on page load.
                record["last_used_at"] = historical_usage
        return sorted(
            records,
            key=lambda item: (
                str(item.get("last_used_at", "")),
                str(item.get("created_at", "")),
            ),
            reverse=True,
        )

    def _legacy_profile_usage(self) -> dict[str, str]:
        """Read usage timestamps from active and archived rewrite records."""

        paths: list[Path] = []
        if self.rewrites_dir.is_dir():
            paths.extend(self.rewrites_dir.glob("rewrite_*.json"))
        if self.archived_rewrites_dir.is_dir():
            paths.extend(self.archived_rewrites_dir.rglob("rewrite_*.json"))
        latest: dict[str, str] = {}
        for path in paths:
            try:
                record = self._read(path)
            except StylePackageStoreError:
                continue
            profile_id = str(record.get("style_profile_id") or "").strip()
            created_at = str(record.get("created_at") or "").strip()
            if profile_id and created_at > latest.get(profile_id, ""):
                latest[profile_id] = created_at
        return latest

    def get_profile(self, style_profile_id: str) -> dict[str, Any]:
        path = self._profile_path(style_profile_id)
        if not path.exists():
            raise StylePackageStoreError("找不到指定的风格包")
        record = self._read(path)
        profile = record.get("style_profile")
        # 兼容早期已审核风格包：旧版本只更新 review_status，留下了待审核概览。
        if (
            record.get("review_status") == "APPROVED"
            and isinstance(profile, dict)
            and str(profile.get("overview") or "").strip() == PENDING_PROFILE_OVERVIEW
        ):
            record = dict(record)
            profile = dict(profile)
            profile["overview"] = APPROVED_PROFILE_OVERVIEW
            record["style_profile"] = profile
        return self._ensure_profile_history(record)

    def approve_profile(self, style_profile_id: str, *, creator_name: str = "") -> dict[str, Any]:
        record = self.get_profile(style_profile_id)
        # Only records created by the production Writing-DNA path carry this
        # explicit gate.  Keep legacy fixtures/records without a gate
        # backwards compatible, while refusing to promote an experimental
        # (<30 complete works) package to a formal production asset.
        profile = record.get("style_profile")
        gate = profile.get("writing_dna_gate") if isinstance(profile, dict) else None
        if isinstance(gate, dict):
            try:
                sample_count = int(gate.get("sample_count", record.get("sample_count") or 0))
            except (TypeError, ValueError):
                sample_count = int(record.get("sample_count") or 0)
            formal = gate.get("formal") is True and str(gate.get("status") or "").upper() != "EXPERIMENTAL"
            if sample_count < 30 or not formal:
                raise StylePackageApprovalConflict(
                    "Writing-DNA 样本不足 30 篇，只能保留实验版，不能批准为正式风格包"
                )
        name = creator_name.strip() or str(record.get("creator_name") or "").strip()
        if name:
            record["creator_name"] = name
            profile = record.get("style_profile")
            if isinstance(profile, dict):
                profile["profile_name"] = name
        profile = record.get("style_profile")
        if isinstance(profile, dict) and str(profile.get("overview") or "").strip() == PENDING_PROFILE_OVERVIEW:
            profile["overview"] = APPROVED_PROFILE_OVERVIEW
        record["review_status"] = "APPROVED"
        record["approved_at"] = _now()
        record["enabled"] = True
        return self._record_profile_version(record, reason="approve")

    def rename_profile(self, style_profile_id: str, *, creator_name: str) -> dict[str, Any]:
        """Apply the creator's verified display name to an existing package."""
        name = creator_name.strip()
        if not name:
            raise ValueError("博主名称不能为空")
        record = self.get_profile(style_profile_id)
        record["creator_name"] = name
        profile = record.get("style_profile")
        if isinstance(profile, dict):
            profile["profile_name"] = name
        return self._record_profile_version(record, reason="rename")

    def mark_profile_used(self, style_profile_id: str, *, used_at: str = "") -> bool:
        """Record a successful or attempted use without failing rewrite persistence."""

        profile_id = str(style_profile_id or "").strip()
        if not profile_id or Path(profile_id).name != profile_id or ".." in profile_id:
            return False
        path = self.profiles_dir / f"{profile_id}.json"
        if not path.is_file():
            return False
        record = self._read(path)
        timestamp = str(used_at or _now()).strip()
        previous = str(record.get("last_used_at") or "").strip()
        record["last_used_at"] = max(previous, timestamp)
        try:
            record["usage_count"] = int(record.get("usage_count") or 0) + 1
        except (TypeError, ValueError):
            record["usage_count"] = 1
        self._write(path, record)
        return True

    def update_expression_habit_library(
        self,
        style_profile_id: str,
        *,
        library: dict[str, Any],
        warning: str = "",
    ) -> dict[str, Any]:
        """Persist a user-triggered, non-destructive habit-library backfill."""

        record = self.get_profile(style_profile_id)
        profile = record.get("style_profile")
        if not isinstance(profile, dict):
            raise StylePackageStoreError("风格包缺少 style_profile 对象")
        profile["expression_habit_library"] = dict(library)
        warnings = profile.get("normalization_warnings")
        normalized_warnings = [str(item).strip() for item in warnings if str(item).strip()] if isinstance(warnings, list) else []
        normalized_warnings = [
            item for item in normalized_warnings
            if item != "expression_habit_library: 未返回可用表达习惯，需补采样或人工补充"
        ]
        if warning and warning not in normalized_warnings:
            normalized_warnings.append(warning)
        if normalized_warnings:
            profile["normalization_warnings"] = normalized_warnings
        else:
            profile.pop("normalization_warnings", None)
        record["style_profile"] = profile
        record["expression_habits_backfilled_at"] = _now()
        return self._record_profile_version(record, reason="backfill_habits")

    def list_profile_versions(self, style_profile_id: str) -> list[dict[str, Any]]:
        """列出风格包版本摘要；正文规则只在详情接口按需读取。"""

        record = self.get_profile(style_profile_id)
        history = record.get("version_history") if isinstance(record.get("version_history"), list) else []
        current = str(record.get("current_version") or record.get("style_profile_version") or "")
        result: list[dict[str, Any]] = []
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            result.append({
                key: item.get(key)
                for key in (
                    "version_id", "version_number", "created_at", "reason", "reviewer", "notes",
                    "review_status", "enabled", "sample_count", "creator_name", "rollback_of",
                )
            } | {"current": str(item.get("version_id") or "") == current})
        return result

    def get_profile_version(self, style_profile_id: str, version_id: str) -> dict[str, Any]:
        """读取一份风格包历史快照，供回滚前展示/审计。"""

        path = self._profile_version_path(style_profile_id, version_id)
        if not path.is_file():
            raise StylePackageStoreError("找不到风格包历史版本")
        return self._read(path)

    def set_profile_enabled(
        self,
        style_profile_id: str,
        *,
        enabled: bool,
        reviewer: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """启用/停用已审核风格包，并保留可回滚版本。"""

        reviewer_text = str(reviewer or "").strip()
        if not reviewer_text:
            raise StylePackageStoreError("启用或停用风格包必须记录 reviewer")
        record = self.get_profile(style_profile_id)
        if str(record.get("review_status") or "").upper() != "APPROVED":
            raise StylePackageStoreError("只有已审核风格包才能启用或停用")
        record["enabled"] = bool(enabled)
        record["enabled_by"] = reviewer_text
        record["enabled_at"] = _now()
        return self._record_profile_version(record, reason="enable" if enabled else "disable", reviewer=reviewer_text, notes=notes)

    def rollback_profile(
        self,
        style_profile_id: str,
        version_id: str,
        *,
        reviewer: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """恢复历史风格包快照，并生成新的回滚版本而不删除历史。"""

        reviewer_text = str(reviewer or "").strip()
        if not reviewer_text:
            raise StylePackageStoreError("回滚风格包必须记录 reviewer")
        record = self.get_profile(style_profile_id)
        requested = str(version_id or "").strip()
        target = self.get_profile_version(style_profile_id, requested)
        target_profile = target.get("style_profile")
        if not isinstance(target_profile, dict):
            raise StylePackageStoreError("历史风格包版本缺少 style_profile")
        for key in ("creator_url", "creator_name", "collection_id", "sample_count", "review_status", "approved_at"):
            if key in target:
                record[key] = target[key]
        record["style_profile"] = dict(target_profile)
        record["enabled"] = bool(target.get("enabled")) and str(target.get("review_status") or "").upper() == "APPROVED"
        record["rollback_of"] = requested
        record["rolled_back_by"] = reviewer_text
        record["rolled_back_at"] = _now()
        return self._record_profile_version(record, reason=f"rollback:{requested}", reviewer=reviewer_text, notes=notes)

    def archive_profile(self, style_profile_id: str) -> dict[str, Any]:
        """Remove a profile from the active list without destroying its history."""

        profile_id = self._safe_profile_id(style_profile_id)
        source = self.profiles_dir / f"{profile_id}.json"
        if not source.is_file():
            raise StylePackageStoreError("找不到指定的风格包")
        record = self._read(source)
        archive_id = f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        archive_dir = self.archived_profiles_dir / archive_id
        archive_dir.mkdir(parents=True, exist_ok=False)
        destination = archive_dir / source.name
        try:
            shutil.move(str(source), str(destination))
            versions_source = self.profile_versions_dir / profile_id
            if versions_source.is_dir():
                shutil.move(str(versions_source), str(archive_dir / "versions"))
            self._write(archive_dir / "archive_manifest.json", {
                "schema_version": "style_profile_archive_v1",
                "archive_id": archive_id,
                "archived_at": _now(),
                "style_profile_id": profile_id,
                "review_status": record.get("review_status", ""),
            })
        except OSError as exc:
            if destination.is_file() and not source.exists():
                try:
                    shutil.move(str(destination), str(source))
                except OSError:
                    pass
            archived_versions = archive_dir / "versions"
            if archived_versions.is_dir() and not (self.profile_versions_dir / profile_id).exists():
                try:
                    shutil.move(str(archived_versions), str(self.profile_versions_dir / profile_id))
                except OSError:
                    pass
            raise StylePackageStoreError("风格包归档失败，原风格包仍保留") from exc
        profile = record.get("style_profile") if isinstance(record.get("style_profile"), dict) else {}
        return {
            "style_profile_id": profile_id,
            "profile_name": record.get("creator_name") or profile.get("profile_name") or "未命名风格包",
            "archive_id": archive_id,
            "review_status": record.get("review_status", ""),
        }

    def delete_profile_cascade(self, style_profile_id: str) -> dict[str, Any]:
        """删除风格包及其本地派生记录；调用方负责删除账号 Vault。"""
        profile_id = self._safe_profile_id(style_profile_id)
        source = self.profiles_dir / f"{profile_id}.json"
        if not source.is_file():
            raise StylePackageStoreError("找不到指定的风格包")
        record = self._read(source)
        collection_id = str(record.get("collection_id") or "").strip()
        deleted = 0
        for path in (source, self.profile_versions_dir / profile_id):
            if path.is_dir():
                shutil.rmtree(path)
                deleted += 1
            elif path.is_file():
                path.unlink()
                deleted += 1
        if collection_id:
            collection_path = self.collections_dir / f"{collection_id}.json"
            if collection_path.is_file():
                collection_path.unlink()
                deleted += 1
        for path in self.rewrites_dir.glob("rewrite_*.json"):
            try:
                item = self._read(path)
            except StylePackageStoreError:
                continue
            if str(item.get("style_profile_id") or "") == profile_id:
                path.unlink()
                deleted += 1
        return {"style_profile_id": profile_id, "account_id": str((record.get("style_profile") or {}).get("account_id") or ""), "deleted_records": deleted}

    def save_rewrite(self, *, style_profile_id: str, case_item: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        rewrite_id = "rewrite_" + uuid4().hex[:12]
        record = {
            "rewrite_id": rewrite_id,
            "created_at": _now(),
            "review_status": "PENDING_USER_REVIEW",
            "style_profile_id": style_profile_id,
            "case_source": case_item,
            "result": result,
        }
        self._write(self.rewrites_dir / f"{rewrite_id}.json", record)
        self.mark_profile_used(style_profile_id, used_at=record["created_at"])
        return record

    def archive_existing_rewrites(self) -> dict[str, Any]:
        """Move previous rewrite drafts out of the active queue before a new one is saved.

        Archived drafts remain recoverable on disk but are deliberately outside
        ``rewrites_dir`` so the writing/director runtime can only see the newest
        active draft.  This prevents an older approved copy from being selected
        after a new rewrite run starts.
        """
        if not self.rewrites_dir.exists():
            return {"archive_id": "", "moved": [], "errors": []}
        files = sorted(self.rewrites_dir.glob("rewrite_*.json"))
        if not files:
            return {"archive_id": "", "moved": [], "errors": []}
        archive_id = f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        archive_root = self.archived_rewrites_dir / archive_id
        archive_root.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        errors: list[str] = []
        for source in files:
            destination = archive_root / source.name
            try:
                shutil.move(str(source), str(destination))
                moved.append(source.name)
            except OSError as exc:
                errors.append(f"{source.name}：{type(exc).__name__}")
        manifest = {
            "schema_version": "rewrite_archive_v1",
            "archive_id": archive_id,
            "archived_at": _now(),
            "moved": moved,
            "errors": errors,
        }
        self._write(archive_root / "archive_manifest.json", manifest)
        return {"archive_id": archive_id, "moved": moved, "errors": errors}

    def list_rewrites(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return newest persisted rewrite review records first.

        Rewrites are user-review artifacts, not transient executor state.  Keeping
        them readable after a browser refresh is required so a completed rewrite
        cannot appear to have disappeared.
        """
        if limit <= 0 or not self.rewrites_dir.exists():
            return []
        records = [self._read(path) for path in self.rewrites_dir.glob("rewrite_*.json")]
        return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)[:limit]

    def get_rewrite(self, rewrite_id: str) -> dict[str, Any]:
        path = self.rewrites_dir / f"{rewrite_id}.json"
        if not path.exists():
            raise StylePackageStoreError("找不到指定的二创草案")
        return self._read(path)

    def approve_rewrite(self, rewrite_id: str) -> dict[str, Any]:
        """Lock a reviewed rewrite as the only copy allowed into the director layer."""
        record = self.get_rewrite(rewrite_id)
        result = record.get("result")
        approved_copy = str(result.get("rewritten_copy") or "").strip() if isinstance(result, dict) else ""
        if not approved_copy:
            raise StylePackageStoreError("二创文案为空，不能确认")
        record["review_status"] = "APPROVED"
        record["approved_at"] = _now()
        record["approved_copy"] = approved_copy
        record["approved_payload"] = {
            "approved_copy": approved_copy,
            "style_profile_id": record.get("style_profile_id", ""),
        }
        self._write(self.rewrites_dir / f"{rewrite_id}.json", record)
        return record


__all__ = ["StylePackageApprovalConflict", "StylePackageStore", "StylePackageStoreError"]
