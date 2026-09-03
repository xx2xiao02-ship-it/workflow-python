"""统一知识源登记、自动识别与队列追踪。

这个模块是文案创作入口和 Obsidian Vault 之间的唯一知识源边界。它只负责：

* 识别链接所属平台和资料类型；
* 为 URL/本地文件生成稳定的 ``source_id``；
* 保存来源、用途、状态、Vault 路径和版本；
* 记录任务/资产绑定关系。

它不抓取社交平台，也不调用模型。外部采集和蒸馏必须由上层在人工确认后
显式触发，因此无法识别的链接永远停留在 ``PENDING_CONFIRMATION``。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .account_knowledge.obsidian_repository import ObsidianRepository, ObsidianRepositoryError


SCHEMA_VERSION = "knowledge-source-v1"
SOURCE_VERSION = "source-v1"

SOURCE_STATUSES = frozenset(
    {
        "PENDING_IDENTIFICATION",
        "PENDING_CONFIRMATION",
        "PENDING_PROCESSING",
        "PROCESSING",
        "PENDING_REVIEW",
        "INGESTED",
        "FAILED",
    }
)
SOURCE_STATUS_LABELS = {
    "PENDING_IDENTIFICATION": "待识别",
    "PENDING_CONFIRMATION": "待人工确认",
    "PENDING_PROCESSING": "待处理",
    "PROCESSING": "处理中",
    "PENDING_REVIEW": "待审核",
    "INGESTED": "已入库",
    "FAILED": "失败",
}

SOURCE_KINDS = frozenset({"video", "profile", "article", "document", "unknown"})
PLATFORMS = frozenset(
    {
        "douyin",
        "bilibili",
        "kuaishou",
        "weixin_video",
        "xiaohongshu",
        "wechat_article",
        "zhihu",
        "weibo",
        "local",
        "other",
        "unknown",
    }
)
USAGE_SCOPES = frozenset({"current_copy", "knowledge_base", "distillation"})
USAGE_SCOPE_LABELS = {
    "current_copy": "仅供本次文案引用",
    "knowledge_base": "登记到待审核知识库",
    # ``distillation`` 保留为后端兼容字段；产品界面不再把“先入库”与
    # “后续整理思维/风格资产”拆成两个互斥的用户选择。
    "distillation": "纳入知识资产中枢",
}

_URL_RE = re.compile(r"https?://[^\s，。；、）》)]+", flags=re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class KnowledgeSourceError(ValueError):
    """知识源不符合登记或状态迁移契约。"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(value: Any, *, limit: int = 400) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


def _safe_id(value: Any, field: str = "source_id") -> str:
    result = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(result):
        raise KnowledgeSourceError(f"{field} 不合法")
    return result


def _url_from_input(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = _URL_RE.search(text)
    return match.group(0).rstrip(".,;!?，。；！？") if match else text


def _platform_for_host(host: str) -> str:
    host = str(host or "").lower().removeprefix("www.")
    if host in {"douyin.com", "v.douyin.com", "iesdouyin.com"} or host.endswith(".douyin.com"):
        return "douyin"
    if host in {"bilibili.com", "b23.tv"} or host.endswith(".bilibili.com"):
        return "bilibili"
    if host == "kuaishou.com" or host.endswith(".kuaishou.com"):
        return "kuaishou"
    if host in {"channels.weixin.qq.com", "channels.weixin.qq.com.cn"}:
        return "weixin_video"
    if host in {"xiaohongshu.com", "xhslink.com"} or host.endswith(".xiaohongshu.com"):
        return "xiaohongshu"
    if host in {"mp.weixin.qq.com", "weixin.qq.com"}:
        return "wechat_article"
    if host in {"zhihu.com", "zhuanlan.zhihu.com"} or host.endswith(".zhihu.com"):
        return "zhihu"
    if host in {"weibo.com", "weibo.cn"} or host.endswith(".weibo.com"):
        return "weibo"
    return "unknown"


def _looks_like_douyin_profile_share(value: str, *, hostname: str = "") -> bool:
    """识别抖音分享文案里的“查看主页/更多作品”语义。

    ``v.douyin.com`` 短链本身只代表一个分享跳转，不能在不发起外部请求
    的情况下判断它最终指向作品还是博主主页。抖音的主页分享文案通常会
    带有“查看 TA 的更多作品”等稳定提示；只在存在这类提示时把短链识别
    为 profile，避免把主页误判成 video。
    """

    host = str(hostname or "").lower().removeprefix("www.")
    if host not in {"v.douyin.com", "douyin.com", "iesdouyin.com"}:
        return False
    normalized = re.sub(r"\s+", "", str(value or "")).lower()
    return any(
        token in normalized
        for token in (
            "查看ta的更多作品",
            "查看他的更多作品",
            "查看她的更多作品",
            "打开抖音搜索查看ta的更多作品",
            "达人主页",
            "博主主页",
            "个人主页",
        )
    )


def detect_source(
    value: Any = "",
    *,
    file_name: Any = "",
    mime_type: Any = "",
) -> dict[str, Any]:
    """只根据输入形式和 URL 路径做保守识别，不臆测未知平台。"""

    raw = str(value or "").strip()
    filename = _clean(file_name, limit=160)
    mime = _clean(mime_type, limit=120)
    if filename or mime:
        return {
            "platform": "local",
            "source_kind": "document",
            "material_type": "document",
            "detection_status": "detected",
            "detection_confidence": 1.0,
            "detection_reason": "本地文件由文件选择器提供",
        }

    url = _url_from_input(raw)
    if not url:
        return {
            "platform": "unknown",
            "source_kind": "unknown",
            "material_type": "unknown",
            "detection_status": "pending_identification",
            "detection_confidence": 0.0,
            "detection_reason": "尚未提供链接或本地文件",
        }
    parsed = urlparse(url)
    try:
        # ``hostname`` removes credentials and ports.  Using ``netloc`` here
        # made otherwise valid links such as ``www.douyin.com:443`` fall
        # through to ``unknown`` and unnecessarily enter manual confirmation.
        hostname = str(parsed.hostname or "").lower()
    except ValueError:
        hostname = ""
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return {
            "platform": "unknown",
            "source_kind": "unknown",
            "material_type": "unknown",
            "detection_status": "pending_confirmation",
            "detection_confidence": 0.0,
            "detection_reason": "链接不是带域名的 HTTP(S) 地址",
        }

    platform = _platform_for_host(hostname)
    path = parsed.path.lower().rstrip("/")
    kind = "unknown"
    reason = "未匹配到已支持的平台路径"
    if platform == "douyin":
        if _looks_like_douyin_profile_share(raw, hostname=hostname):
            kind, reason = "profile", "抖音博主主页分享文案"
        elif "/user/" in path or "/search/" in path and "user" in path:
            kind, reason = "profile", "抖音用户主页路径"
        elif "/video/" in path:
            kind, reason = "video", "抖音作品路径"
        elif hostname.startswith("v."):
            # 短链需要展开后才能知道是作品还是主页；不允许把它臆测成
            # 单条视频，否则主页分享会落入视频采集器并显示误导性错误。
            kind, reason = "unknown", "抖音分享短链未能确认是作品还是博主主页"
    elif platform == "bilibili":
        if hostname.startswith("space.") or "/space/" in path:
            kind, reason = "profile", "B站空间主页路径"
        elif "/video/" in path or hostname == "b23.tv":
            kind, reason = "video", "B站视频路径或短链"
    elif platform == "kuaishou":
        if "/profile/" in path or "/user/" in path:
            kind, reason = "profile", "快手用户主页路径"
        elif "/short-video/" in path or "/video/" in path:
            kind, reason = "video", "快手作品路径"
    elif platform == "weixin_video":
        kind, reason = "video", "视频号域名"
    elif platform == "xiaohongshu":
        if "/user/" in path or "/profile/" in path:
            kind, reason = "profile", "小红书用户主页路径"
        elif "/explore/" in path or "/discovery/" in path:
            kind, reason = "article", "小红书笔记路径"
    elif platform == "wechat_article":
        kind, reason = "article", "微信公众号文章域名"
    elif platform == "zhihu":
        if "/people/" in path or "/org/" in path:
            kind, reason = "profile", "知乎用户/机构主页路径"
        elif "/p/" in path or "/question/" in path:
            kind, reason = "article", "知乎文章或问题路径"
    elif platform == "weibo":
        if "/u/" in path or "/profile/" in path:
            kind, reason = "profile", "微博用户主页路径"
        elif "/status/" in path or "/detail/" in path:
            kind, reason = "article", "微博内容路径"

    detected = kind != "unknown" and platform != "unknown"
    return {
        "platform": platform,
        "source_kind": kind,
        "material_type": kind,
        "detection_status": "detected" if detected else "pending_confirmation",
        "detection_confidence": 0.95 if detected else 0.0,
        "detection_reason": reason,
    }


def _normalize_usage(value: Any) -> tuple[str, list[str]]:
    aliases = {
        "current_copy": "current_copy",
        "current": "current_copy",
        "once": "current_copy",
        "本次": "current_copy",
        "仅供本次文案引用": "current_copy",
        "knowledge_base": "knowledge_base",
        "knowledge": "knowledge_base",
        "登记到待审核知识库": "knowledge_base",
        "待审核知识库": "knowledge_base",
        "distillation": "distillation",
        "distill": "distillation",
        "进入风格包、思维包蒸馏": "distillation",
        "风格包": "distillation",
    }
    values = value if isinstance(value, (list, tuple, set)) else [value]
    normalized: list[str] = []
    for item in values:
        key = str(item or "").strip().lower()
        canonical = aliases.get(key) or aliases.get(str(item or "").strip())
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        normalized = ["current_copy"]
    return normalized[0], normalized


def _extract_text_from_bytes(source_bytes: bytes | None, suffix: str) -> str:
    if not source_bytes:
        return ""
    suffix = str(suffix or "").lower()
    if suffix in {".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv"}:
        try:
            return source_bytes.decode("utf-8", errors="replace").strip()[:200_000]
        except Exception:
            return ""
    return ""


def _file_payload(payload: Mapping[str, Any]) -> tuple[str, str, bytes | None]:
    file_value = payload.get("file")
    if not isinstance(file_value, Mapping):
        return "", "", None
    name = _clean(file_value.get("name"), limit=160)
    mime = _clean(file_value.get("mime_type") or file_value.get("type"), limit=120)
    encoded = str(file_value.get("base64") or "").strip()
    if not encoded:
        return name, mime, None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise KnowledgeSourceError("本地资料编码无效") from exc
    if len(raw) > 24 * 1024 * 1024:
        raise KnowledgeSourceError("本地资料超过 24MB")
    return name, mime, raw


class KnowledgeSourceRegistry:
    """线程安全的本地知识源索引；内容正文仍保存在 Obsidian。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "sources": {}}

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if not isinstance(value, Mapping):
            return self._empty()
        raw_sources = value.get("sources") if isinstance(value.get("sources"), Mapping) else {}
        return {
            "schema_version": str(value.get("schema_version") or SCHEMA_VERSION),
            "updated_at": str(value.get("updated_at") or ""),
            "sources": {str(key): dict(item) for key, item in raw_sources.items() if isinstance(item, Mapping)},
        }

    def _write(self, document: Mapping[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now(),
            "sources": document.get("sources") if isinstance(document.get("sources"), Mapping) else {},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _append_history(record: dict[str, Any], status: str) -> None:
        history = record.get("status_history") if isinstance(record.get("status_history"), list) else []
        if not history or str(history[-1].get("status") or "") != status:
            history.append({"status": status, "label": SOURCE_STATUS_LABELS.get(status, status), "at": utc_now()})
        record["status_history"] = history[-30:]

    def get(self, source_id: str) -> dict[str, Any] | None:
        key = _safe_id(source_id)
        with self._lock:
            record = self._read()["sources"].get(key)
            return dict(record) if isinstance(record, Mapping) else None

    def list(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        requested = str(status or "").strip().upper()
        if requested and requested not in SOURCE_STATUSES:
            raise KnowledgeSourceError(f"不支持的知识源状态：{requested}")
        with self._lock:
            records = [dict(item) for item in self._read()["sources"].values() if isinstance(item, Mapping)]
        if requested:
            records = [item for item in records if str(item.get("status") or "").upper() == requested]
        records.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return records[: max(1, min(int(limit), 2000))]

    def register(
        self,
        payload: Mapping[str, Any],
        *,
        repository: ObsidianRepository | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise KnowledgeSourceError("知识源请求必须是对象")
        raw_input = _clean(payload.get("raw_url") or payload.get("source_url") or payload.get("url"), limit=1_000)
        raw_url = _url_from_input(raw_input)
        file_name, file_mime, file_bytes = _file_payload(payload)
        content_text = _clean(payload.get("content_text") or payload.get("text"), limit=200_000)
        if file_bytes is not None and not content_text:
            content_text = _extract_text_from_bytes(file_bytes, Path(file_name).suffix)
        if not raw_url and file_bytes is None and not content_text:
            raise KnowledgeSourceError("请提供链接、正文或选择本地资料")
        # 保留完整分享文案给识别器；仅把 canonical URL 写入 raw_url。否则
        # “查看 TA 的更多作品 + v.douyin.com 短链”会在登记时丢掉主页语义，
        # 再次被错误识别成单条视频。
        detection = detect_source(raw_input or raw_url, file_name=file_name, mime_type=file_mime)
        usage_scope, usage_scopes = _normalize_usage(payload.get("usage_scope") or payload.get("usage"))
        # 博主主页不是可直接引用的单篇资料。识别成功后必须进入可追踪的
        # 账号作品采集与蒸馏链，避免用户先理解“使用范围”这一内部概念才
        # 能看到采集数量弹窗。未知链接仍保持原来的人工确认边界。
        if str(detection.get("material_type") or "").lower() == "profile":
            usage_scope, usage_scopes = "distillation", ["distillation"]
        account_id = _clean(payload.get("account_id"), limit=80)
        author = _clean(payload.get("author") or payload.get("source_name"), limit=200)
        title = _clean(payload.get("title"), limit=300)
        digest_input = "\x1f".join(
            [
                raw_url,
                file_name,
                hashlib.sha256(file_bytes).hexdigest() if file_bytes is not None else "",
                hashlib.sha256(content_text.encode("utf-8")).hexdigest() if content_text else "",
            ]
        )
        source_id = "source-" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:20]
        now = utc_now()
        detected_status = str(detection["detection_status"]).lower()
        status = "PENDING_CONFIRMATION" if detected_status != "detected" else "PENDING_PROCESSING"
        record = {
            "schema_version": SCHEMA_VERSION,
            "source_version": SOURCE_VERSION,
            "source_id": source_id,
            "input_type": "file" if file_bytes is not None else ("text" if not raw_url else "url"),
            "raw_url": raw_url,
            "original_input": raw_input,
            "file_name": file_name,
            "file_mime_type": file_mime or (mimetypes.guess_type(file_name)[0] if file_name else ""),
            "file_size": len(file_bytes) if file_bytes is not None else 0,
            "content_hash": hashlib.sha256(content_text.encode("utf-8")).hexdigest() if content_text else "",
            "content": content_text,
            "platform": detection["platform"],
            "source_kind": detection["source_kind"],
            "material_type": detection["material_type"],
            "detection_status": str(detection["detection_status"]),
            "detection_confidence": detection["detection_confidence"],
            "detection_reason": detection["detection_reason"],
            "author": author,
            "title": title,
            "account_id": account_id,
            "usage_scope": usage_scope,
            "usage_scopes": usage_scopes,
            "status": status,
            "status_label": SOURCE_STATUS_LABELS[status],
            "obsidian_path": "",
            "vault_path": "",
            "bound_task_ids": [],
            "bound_asset_ids": [],
            "error_code": "",
            "error_message": "",
            "created_at": now,
            "updated_at": now,
            "content_available": bool(content_text),
            "status_history": [],
        }
        self._append_history(record, status)
        reclassified_existing = False
        with self._lock:
            document = self._read()
            previous = document["sources"].get(source_id)
            if isinstance(previous, Mapping):
                # 同一 URL/文件重复登记时保留历史绑定与最初创建时间，仅补充新用途/作者。
                # 但短链是一个例外：旧版本可能只拿到了 ``v.douyin.com``，
                # 将其错误保存为单条视频。后来用户粘贴包含“查看 TA 的更多
                # 作品”的完整分享文案时，识别结果已经拥有明确的主页语义；
                # 不能因为 source_id 仍相同而继续返回旧的 video/FAILED
                # 记录，否则页面既不会弹出采集数量窗口，也无法修复该资料。
                existing = dict(previous)
                existing["usage_scope"] = usage_scope
                existing["usage_scopes"] = sorted(set(existing.get("usage_scopes") or []) | set(usage_scopes))
                for key in ("author", "title", "account_id"):
                    if record.get(key) and not existing.get(key):
                        existing[key] = record[key]
                existing_kind = str(existing.get("material_type") or existing.get("source_kind") or "").lower()
                incoming_kind = str(record.get("material_type") or record.get("source_kind") or "").lower()
                if incoming_kind == "profile" and existing_kind != "profile":
                    for key in (
                        "original_input",
                        "platform",
                        "source_kind",
                        "material_type",
                        "detection_status",
                        "detection_confidence",
                        "detection_reason",
                        "usage_scope",
                        "usage_scopes",
                    ):
                        existing[key] = record[key]
                    # 仅重置尚未形成可用内容的旧误识别记录。已在运行或已审核的
                    # 任务不在登记动作中被擅自改写，仍须人工在控制台处理。
                    if str(existing.get("status") or "").upper() in {
                        "FAILED",
                        "PENDING_CONFIRMATION",
                        "PENDING_IDENTIFICATION",
                        "PENDING_PROCESSING",
                    }:
                        existing["status"] = "PENDING_PROCESSING"
                        existing["status_label"] = SOURCE_STATUS_LABELS["PENDING_PROCESSING"]
                        existing["error_code"] = ""
                        existing["error_message"] = ""
                        self._append_history(existing, "PENDING_PROCESSING")
                    reclassified_existing = True
                existing["updated_at"] = now
                record = existing
            else:
                document["sources"][source_id] = record
            if isinstance(previous, Mapping):
                document["sources"][source_id] = record
            self._write(document)

        if reclassified_existing and repository is not None and str(record.get("obsidian_path") or "").strip():
            self._sync_vault_record(record, repository)

        # 自动识别和写入 Vault 是同一次登记动作；没有 Vault 时上层会把记录
        # 标为 FAILED，不会向页面返回虚假的“已入库”。
        if repository is not None and not record.get("obsidian_path"):
            body_lines = [
                f"# {title or file_name or raw_url or source_id}",
                "",
                f"- source_id：{source_id}",
                f"- 来源 URL：{raw_url or '未提供'}",
                f"- 平台：{record['platform']}",
                f"- 资料类型：{record['material_type']}",
                f"- 作者/账号：{author or '待补充'}",
                f"- 使用范围：{USAGE_SCOPE_LABELS[usage_scope]}",
                f"- 识别状态：{record['detection_status']}",
                "",
                "## 原始内容",
                content_text or "（尚未提取正文；待人工确认/后续处理）",
            ]
            try:
                paths = repository.save_professional_material(
                    material_id=source_id,
                    metadata={
                        "source_id": source_id,
                        "source_version": SOURCE_VERSION,
                        "raw_url": raw_url,
                        "platform": record["platform"],
                        "material_type": record["material_type"],
                        "author": author,
                        "title": title or file_name or raw_url or source_id,
                        "usage_scope": usage_scope,
                        "usage_scopes": usage_scopes,
                        "status": status,
                        "detection_status": record["detection_status"],
                        "created_at": now,
                    },
                    body="\n".join(body_lines) + "\n",
                    source_bytes=file_bytes,
                    source_suffix=Path(file_name).suffix.lower() if file_name else "",
                )
            except (ObsidianRepositoryError, OSError) as exc:
                self.transition(source_id, "FAILED", error_code="vault_write_failed", error_message=str(exc))
                raise
            record["obsidian_path"] = str(paths.get("material_path") or "")
            record["obsidian_attachment_path"] = str(paths.get("attachment_path") or "")
            record["vault_path"] = str(repository.vault_path)
            with self._lock:
                document = self._read()
                current = dict(document["sources"].get(source_id) or record)
                current["obsidian_path"] = record["obsidian_path"]
                current["obsidian_attachment_path"] = record.get("obsidian_attachment_path", "")
                current["vault_path"] = record["vault_path"]
                current["updated_at"] = utc_now()
                document["sources"][source_id] = current
                self._write(document)
                record = current
        return dict(record)

    @staticmethod
    def _vault_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
        """把注册表字段投影到 Obsidian 资料 frontmatter，避免写入正文。"""

        return {
            "source_id": str(record.get("source_id") or ""),
            "source_version": str(record.get("source_version") or SOURCE_VERSION),
            "raw_url": str(record.get("raw_url") or ""),
            "platform": str(record.get("platform") or "unknown"),
            "material_type": str(record.get("material_type") or "unknown"),
            "source_kind": str(record.get("source_kind") or record.get("material_type") or "unknown"),
            "author": str(record.get("author") or ""),
            "title": str(record.get("title") or record.get("file_name") or record.get("source_id") or ""),
            "usage_scope": str(record.get("usage_scope") or "current_copy"),
            "usage_scopes": list(record.get("usage_scopes") or []),
            "detection_status": str(record.get("detection_status") or ""),
            "detection_confidence": record.get("detection_confidence", 0.0),
            "detection_reason": str(record.get("detection_reason") or ""),
            "status": str(record.get("status") or ""),
            "status_label": str(record.get("status_label") or ""),
            "content_available": bool(record.get("content_available")),
            "content_hash": str(record.get("content_hash") or ""),
            "content_type": str(record.get("content_type") or ""),
            "transcript_status": str(record.get("transcript_status") or ""),
            "final_url": str(record.get("final_url") or ""),
            "processing_route": str(record.get("processing_route") or ""),
            "processing_task_id": str(record.get("processing_task_id") or ""),
            "processing_result": record.get("processing_result") if isinstance(record.get("processing_result"), Mapping) else {},
            "last_processed_at": str(record.get("last_processed_at") or ""),
            "error_code": str(record.get("error_code") or ""),
            "error_message": str(record.get("error_message") or ""),
        }

    @classmethod
    def _sync_vault_record(
        cls,
        record: Mapping[str, Any],
        repository: ObsidianRepository | None,
    ) -> None:
        if repository is None or not str(record.get("obsidian_path") or "").strip():
            return
        material_id = str(record.get("source_id") or record.get("material_id") or "").strip()
        if not material_id:
            return
        repository.update_professional_material(material_id, cls._vault_metadata(record))

    def transition(
        self,
        source_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
        repository: ObsidianRepository | None = None,
    ) -> dict[str, Any]:
        key = _safe_id(source_id)
        target = str(status or "").strip().upper()
        if target not in SOURCE_STATUSES:
            raise KnowledgeSourceError(f"不支持的知识源状态：{target}")
        with self._lock:
            document = self._read()
            record = document["sources"].get(key)
            if not isinstance(record, Mapping):
                raise KnowledgeSourceError("未找到知识源")
            updated = dict(record)
            updated["status"] = target
            updated["status_label"] = SOURCE_STATUS_LABELS[target]
            updated["error_code"] = str(error_code or "").strip()
            updated["error_message"] = _clean(error_message, limit=800)
            updated["updated_at"] = utc_now()
            self._append_history(updated, target)
            self._sync_vault_record(updated, repository)
            document["sources"][key] = updated
            self._write(document)
            return dict(updated)

    def update_processing(
        self,
        source_id: str,
        *,
        status: str,
        content: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        repository: ObsidianRepository | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """持久化一次知识源处理结果，并同步 Vault 笔记。

        ``register`` 只负责登记原始输入；正文提取、转写或账号采集完成后，
        由此方法原子地更新正文、处理元数据和状态。正文为空时不会被标记为
        可用，调用方应使用 ``FAILED`` 或其它明确的未完成状态。该方法保留
        小型 ``processing_result`` 摘要，避免把完整采集报告写入注册表。
        """

        key = _safe_id(source_id)
        target = str(status or "").strip().upper()
        if target not in SOURCE_STATUSES:
            raise KnowledgeSourceError(f"不支持的知识源状态：{target}")
        clean_content: str | None = None
        if content is not None:
            clean_content = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:200_000]
        allowed_metadata = {
            "title",
            "author",
            "platform",
            "source_kind",
            "material_type",
            "raw_url",
            "final_url",
            "content_type",
            "transcript_status",
            "processing_route",
            "processing_task_id",
            "processing_result",
            "last_processed_at",
            "source_metadata",
            "detection_status",
            "detection_confidence",
            "detection_reason",
        }
        metadata_value = dict(metadata or {})
        with self._lock:
            document = self._read()
            record = document["sources"].get(key)
            if not isinstance(record, Mapping):
                raise KnowledgeSourceError("未找到知识源")
            updated = dict(record)
            for name, value in metadata_value.items():
                if name not in allowed_metadata:
                    continue
                if name in {"title", "author", "platform", "source_kind", "material_type", "raw_url", "final_url", "content_type", "transcript_status", "processing_route", "processing_task_id", "last_processed_at", "detection_status", "detection_reason"}:
                    updated[name] = _clean(value, limit=2_000 if name in {"title", "detection_reason"} else 400)
                elif name == "detection_confidence":
                    try:
                        updated[name] = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        updated[name] = 0.0
                else:
                    updated[name] = value
            if clean_content is not None:
                updated["content"] = clean_content
                updated["content_hash"] = hashlib.sha256(clean_content.encode("utf-8")).hexdigest() if clean_content else ""
                updated["content_available"] = bool(clean_content)
            elif "content_available" not in updated:
                updated["content_available"] = bool(str(updated.get("content") or "").strip())
            updated["status"] = target
            updated["status_label"] = SOURCE_STATUS_LABELS[target]
            updated["error_code"] = str(error_code or "").strip()
            updated["error_message"] = _clean(error_message, limit=800)
            updated["updated_at"] = utc_now()
            self._append_history(updated, target)

            # When new content was extracted, refresh the note body as well as
            # frontmatter so Obsidian and the registry cannot diverge.
            vault_body = body
            if clean_content is not None and vault_body is None:
                title = str(updated.get("title") or updated.get("file_name") or updated.get("raw_url") or key).strip()
                vault_body = "\n".join(
                    [
                        f"# {title}",
                        "",
                        f"- source_id：{key}",
                        f"- 来源 URL：{str(updated.get('raw_url') or '未提供')}",
                        f"- 平台：{str(updated.get('platform') or 'unknown')}",
                        f"- 资料类型：{str(updated.get('material_type') or updated.get('source_kind') or 'unknown')}",
                        f"- 作者/账号：{str(updated.get('author') or '待补充')}",
                        f"- 使用范围：{str(updated.get('usage_scope') or 'current_copy')}",
                        "",
                        "## 原始内容",
                        clean_content or "（正文为空，等待人工补充）",
                        "",
                    ]
                )
            self._sync_vault_record(updated, repository)
            if repository is not None and str(updated.get("obsidian_path") or "").strip() and vault_body is not None:
                repository.update_professional_material(
                    key,
                    self._vault_metadata(updated),
                    body=vault_body,
                )
            document["sources"][key] = updated
            self._write(document)
            return dict(updated)

    def update_content(
        self,
        source_id: str,
        content: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        repository: ObsidianRepository | None = None,
        status: str = "PENDING_REVIEW",
    ) -> dict[str, Any]:
        """写入已提取正文的便捷入口；默认进入待人工审核。"""

        if not str(content or "").strip():
            raise KnowledgeSourceError("处理结果正文为空，不能标记为可用")
        return self.update_processing(
            source_id,
            status=status,
            content=content,
            metadata=metadata,
            repository=repository,
        )

    def confirm(
        self,
        source_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        repository: ObsidianRepository | None = None,
    ) -> dict[str, Any]:
        key = _safe_id(source_id)
        values = dict(payload or {})
        with self._lock:
            document = self._read()
            record = document["sources"].get(key)
            if not isinstance(record, Mapping):
                raise KnowledgeSourceError("未找到知识源")
            updated = dict(record)
            platform = str(values.get("platform") or values.get("confirmed_platform") or updated.get("platform") or "unknown").strip().lower()
            kind = str(values.get("source_kind") or values.get("material_type") or updated.get("source_kind") or "unknown").strip().lower()
            if platform not in PLATFORMS:
                raise KnowledgeSourceError("确认的平台不在支持列表中")
            if kind not in SOURCE_KINDS:
                raise KnowledgeSourceError("确认的资料类型不在支持列表中")
            usage_scope, usage_scopes = _normalize_usage(values.get("usage_scope") or updated.get("usage_scope"))
            updated.update(
                {
                    "platform": platform,
                    "source_kind": kind,
                    "material_type": kind,
                    "author": _clean(values.get("author") or updated.get("author"), limit=200),
                    "usage_scope": usage_scope,
                    "usage_scopes": sorted(set(updated.get("usage_scopes") or []) | set(usage_scopes)),
                    "detection_status": "confirmed",
                    "detection_confidence": 1.0,
                    "detection_reason": "人工确认",
                    "updated_at": utc_now(),
                }
            )
            target = "PENDING_PROCESSING"
            updated["status"] = target
            updated["status_label"] = SOURCE_STATUS_LABELS[target]
            self._append_history(updated, target)
            self._sync_vault_record(updated, repository)
            document["sources"][key] = updated
            self._write(document)
            return dict(updated)

    def bind_task(self, source_id: str, task_id: str) -> dict[str, Any]:
        key = _safe_id(source_id)
        task = _clean(task_id, limit=160)
        if not task or "/" in task or "\\" in task:
            raise KnowledgeSourceError("task_id 不合法")
        with self._lock:
            document = self._read()
            record = document["sources"].get(key)
            if not isinstance(record, Mapping):
                raise KnowledgeSourceError("未找到知识源")
            updated = dict(record)
            tasks = [str(item) for item in (updated.get("bound_task_ids") or []) if str(item).strip()]
            if task not in tasks:
                tasks.append(task)
            updated["bound_task_ids"] = tasks[-50:]
            updated["updated_at"] = utc_now()
            document["sources"][key] = updated
            self._write(document)
            return dict(updated)

    def bind_asset(self, source_id: str, asset_id: str) -> dict[str, Any]:
        key = _safe_id(source_id)
        asset = _clean(asset_id, limit=160)
        if not asset or "/" in asset or "\\" in asset:
            raise KnowledgeSourceError("asset_id 不合法")
        with self._lock:
            document = self._read()
            record = document["sources"].get(key)
            if not isinstance(record, Mapping):
                raise KnowledgeSourceError("未找到知识源")
            updated = dict(record)
            assets = [str(item) for item in (updated.get("bound_asset_ids") or []) if str(item).strip()]
            if asset not in assets:
                assets.append(asset)
            updated["bound_asset_ids"] = assets[-50:]
            updated["updated_at"] = utc_now()
            document["sources"][key] = updated
            self._write(document)
            return dict(updated)


__all__ = [
    "KnowledgeSourceError",
    "KnowledgeSourceRegistry",
    "PLATFORMS",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "SOURCE_STATUSES",
    "SOURCE_STATUS_LABELS",
    "SOURCE_VERSION",
    "USAGE_SCOPES",
    "USAGE_SCOPE_LABELS",
    "detect_source",
    "utc_now",
]
