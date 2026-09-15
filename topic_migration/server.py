"""F0/F1 独立 HTTP 服务。

使用 Python 标准库提供服务，目标项目无需导入旧控制台或安装其依赖。
"""

from __future__ import annotations

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from .errors import MigrationError, NotFoundError, ValidationError
from .service_governance import ServiceGovernance, ServiceLock
from .topic_service import TopicCenterService

MAX_REQUEST_BYTES = 4 * 1024 * 1024
STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"


def _first(query: Mapping[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key) or []
    return str(values[0] if values else default)


def _page(name: str) -> bytes:
    path = STATIC_ROOT / name
    try:
        return path.read_bytes()
    except OSError:
        return "<!doctype html><meta charset=\"utf-8\"><title>页面不可用</title><h1>页面资源缺失</h1>".encode("utf-8")


class TopicMigrationHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: TopicCenterService, governance: ServiceGovernance) -> None:
        self.service = service
        self.governance = governance
        super().__init__(address, TopicMigrationHandler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.governance.lock.release()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    service: TopicCenterService | None = None,
    lock_path: Path | str | None = None,
) -> TopicMigrationHTTPServer:
    instance = service or TopicCenterService()
    lock = ServiceLock(Path(lock_path) if lock_path is not None else instance.root / "service.lock")
    governance = ServiceGovernance(lock)
    governance.lock.acquire()
    try:
        return TopicMigrationHTTPServer((host, int(port)), instance, governance)
    except Exception:
        governance.lock.release()
        raise


class TopicMigrationHandler(BaseHTTPRequestHandler):
    server: TopicMigrationHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # 不把 URL 查询值或请求体写入日志，避免凭据和正文意外落盘。
        return

    @property
    def service(self) -> TopicCenterService:
        return self.server.service

    def _trace_id(self) -> str:
        value = self.headers.get("X-Trace-Id", "").strip()
        return value[:100] if value else uuid.uuid4().hex

    def _send_json(self, status: int, value: Any) -> None:
        trace_id = self._trace_id()
        if isinstance(value, Mapping):
            body_value = dict(value)
            body_value.setdefault("trace_id", trace_id)
        else:
            body_value = {"data": value, "trace_id": trace_id}
        body = json.dumps(body_value, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Trace-Id", trace_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes, *, status: int = 200) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        body = f'<a href="{location}">继续</a>'.encode("utf-8")
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length or 0)
        except ValueError as exc:
            raise ValidationError("Content-Length 无效") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValidationError("请求体超过允许大小")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("请求体必须是 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是对象")
        return value

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, MigrationError):
            self._send_json(exc.http_status, {"status": "error", "code": exc.code, "message": str(exc), "retryable": exc.retryable})
            return
        if isinstance(exc, ValueError):
            self._send_json(422, {"status": "error", "code": "invalid_input", "message": str(exc), "retryable": False})
            return
        self._send_json(500, {"status": "error", "code": "internal_error", "message": "服务内部错误，未返回异常细节", "retryable": False})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/":
                self._redirect("/topic-center")
                return
            pages = {
                "/topic-center": "topic-center.html",
                "/topic-writing-governance": "topic-writing-governance.html",
                "/api-management": "api-management.html",
            }
            if path in pages:
                self._send_html(_page(pages[path]))
                return
            if path in {"/writing", "/writing-governance"}:
                self._send_html(_page("not-migrated.html"))
                return
            if path == "/topic-center-new":
                self._redirect("/topic-center")
                return
            if path == "/api/executor/health":
                self._send_json(200, {"status": "ready", "service": "topic-center-migration", "governance": self.server.governance.snapshot(), "scope": "F0公共基础 + F0 control-plane API配置 + F1选题中心", "external_requests": False})
                return
            if path == "/healthz":
                self._send_json(200, {"status": "ready", "service": "topic-center-migration"})
                return
            if path == "/api/api-management":
                self._send_json(200, self.service.config_service.public_snapshot())
                return
            if path == "/api/topic-center/health":
                self._send_json(200, self.service.health())
                return
            if path == "/api/topic-writing-governance/health":
                result = self.service.health()
                trend = result.get("providers", {}).get("trendradar", {}) if isinstance(result.get("providers"), Mapping) else {}
                self._send_json(200, {**result, "database_date": trend.get("database_date", ""), "latest_crawl": trend.get("latest_crawl", ""), "item_count": trend.get("item_count", 0)})
                return
            if path == "/api/topic-center/refresh-schedule":
                self._send_json(200, self.service.get_refresh_schedule())
                return
            if path == "/api/topic-center/topics" or path in {"/api/topic-center/candidates", "/api/topic-writing-governance/candidates"}:
                result = self.service.topics(intent=_first(query, "intent"), tags=query.get("tags", []), sources=query.get("sources", []), limit=int(_first(query, "limit", "50") or 50))
                self._send_json(200 if result.get("status") in {"ready", "empty"} else 503, result)
                return
            if path == "/api/topic-center/accounts":
                self._send_json(200, self.service.list_accounts())
                return
            if path == "/api/topic-center/selections" or path == "/api/topic-writing-governance/selections":
                self._send_json(200, {"status": "ready", "schema_version": "topic-writing-governance-v2", "selections": self.service.store.list_selections()})
                return
            if path == "/api/topic-center/sources" or path == "/api/topic-writing-governance/sources":
                self._send_json(200, {"status": "ready", "schema_version": "topic-writing-governance-v2", "sources": self.service.store.list_sources()})
                return
            if path == "/api/topic-center/writing-input":
                result = self.service.writing_input(_first(query, "source_content_id"), _first(query, "selection_id"), project_id=_first(query, "project_id", "topic-center"), run_id=_first(query, "run_id", "local"))
                self._send_json(200, result)
                return
            if path == "/api/topic-center/queue":
                self._send_json(410, self.service.queue_archived())
                return
            if path == "/api/topic-center/douyin":
                self._send_json(501, {"status": "not_migrated", "message": "抖音登录/热点专用服务尚未迁移；本批不启动采集服务。"})
                return
            if path.startswith("/api/topic-center/topics/"):
                parts = [unquote(part) for part in path.split("/") if part]
                if len(parts) == 5 and parts[-1] in {"contents", "preview"}:
                    candidate_id = parts[-2]
                    if parts[-1] == "contents":
                        platforms = query.get("platforms") or None
                        limit = int(_first(query, "limit", "20") or 20)
                        ghid = _first(query, "wechat_ghid")
                        result = self.service.cached_contents(candidate_id, platforms=platforms or tuple(), limit=limit, wechat_ghid=ghid)
                        self._send_json(200, result)
                    else:
                        self._send_json(200, self.service.preview(candidate_id))
                    return
            if path.startswith("/api/topic-center/topics/") and path.endswith("/enrich"):
                candidate_id = unquote(path.removeprefix("/api/topic-center/topics/").removesuffix("/enrich").strip("/"))
                platforms = query.get("platforms") or None
                limit = int(_first(query, "limit", "20") or 20)
                ghid = _first(query, "wechat_ghid")
                self._send_json(200, self.service.cached_contents(candidate_id, platforms=platforms or tuple(), limit=limit, wechat_ghid=ghid))
                return
            if path.startswith("/api/topic-center/selections/") or path.startswith("/api/topic-writing-governance/selections/"):
                prefix = "/api/topic-writing-governance/selections/" if path.startswith("/api/topic-writing-governance/") else "/api/topic-center/selections/"
                remainder = path.removeprefix(prefix).strip("/")
                if remainder and "/" not in remainder:
                    snapshot = self.service.store.snapshot(unquote(remainder))
                    self._send_json(200, {"status": "ready", **snapshot})
                    return
            if path.startswith("/api/topic-center/sources/") or path.startswith("/api/topic-writing-governance/sources/"):
                prefix = "/api/topic-writing-governance/sources/" if path.startswith("/api/topic-writing-governance/") else "/api/topic-center/sources/"
                source_id = unquote(path.removeprefix(prefix).strip("/"))
                source = self.service.store.get_source(source_id)
                if source is None:
                    raise NotFoundError("未找到 source_content_id 对应的素材")
                self._send_json(200, {"status": "ready", "source": source})
                return
            if path.startswith("/api/topic-center/queue/") and path.endswith("/writing-input"):
                self._send_json(410, self.service.queue_archived())
                return
            self._send_json(404, {"status": "not_found", "message": "接口或页面不存在"})
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        try:
            payload = self._read_json()
            if path == "/api/api-management/config":
                self._send_json(200, self.service.config_service.save(payload))
                return
            if path in {"/api/api-management/test-connection", "/api/api-management/auth-verify"}:
                self._send_json(200, self.service.config_service.connection_test_deferred(payload))
                return
            if path == "/api/topic-center/refresh-schedule":
                self._send_json(202, self.service.save_refresh_schedule(payload))
                return
            if path.startswith("/api/topic-center/topics/") and path.endswith("/enrich"):
                candidate_id = unquote(path.removeprefix("/api/topic-center/topics/").removesuffix("/enrich").strip("/"))
                self._send_json(200, self.service.enrich(candidate_id, payload))
                return
            if path == "/api/topic-center/accounts":
                self._send_json(200, self.service.save_accounts(payload))
                return
            if path == "/api/topic-center/selections":
                self._send_json(201, self.service.create_selection(payload))
                return
            if path == "/api/topic-center/professional-media":
                self._send_json(200, self.service.professional_media(payload))
                return
            if path == "/api/topic-center/decisions":
                self._send_json(200, self.service.save_decision(payload))
                return
            if path.startswith("/api/topic-center/selections/"):
                remainder = path.removeprefix("/api/topic-center/selections/").strip("/")
                if remainder.endswith("/delete"):
                    if payload.get("confirmed") is not True:
                        raise ValidationError("删除选题需要明确确认")
                    selection_id = unquote(remainder.removesuffix("/delete").strip("/"))
                    self._send_json(200, self.service.store.delete_selection(selection_id))
                    return
                for suffix in ("/collect", "/retry"):
                    if remainder.endswith(suffix):
                        selection_id = unquote(remainder[: -len(suffix)].strip("/"))
                        self._send_json(200, self.service.collect(selection_id, payload))
                        return
            if path.startswith("/api/topic-writing-governance/selections/"):
                remainder = path.removeprefix("/api/topic-writing-governance/selections/").strip("/")
                if remainder.endswith("/delete"):
                    if payload.get("confirmed") is not True:
                        raise ValidationError("删除选题需要明确确认")
                    selection_id = unquote(remainder.removesuffix("/delete").strip("/"))
                    self._send_json(200, self.service.store.delete_selection(selection_id))
                    return
            if path.startswith("/api/topic-center/sources/") and path.endswith("/archive"):
                source_id = unquote(path.removeprefix("/api/topic-center/sources/").removesuffix("/archive").strip("/"))
                self._send_json(200, self.service.archive_source(source_id, payload))
                return
            if path == "/api/topic-writing-governance/selections":
                self._send_json(201, self.service.accept_governance(payload))
                return
            if path == "/api/topic-writing-governance/sources/manual":
                source = self.service.store.save_manual_source(payload.get("source_text") if payload.get("source_text") is not None else payload.get("text"), title=payload.get("title"), source_name=payload.get("source_name"), selection_id=payload.get("selection_id"), source_url=payload.get("source_url"))
                selection_id = str(payload.get("selection_id") or "").strip()
                selection = self.service.store.get_selection(selection_id) if selection_id else None
                self._send_json(201, {"status": "CONTENT_READY", "source": source, "selection": selection, "message": "纯文案已保存，可进入文案创作。"})
                return
            if path.startswith("/api/topic-writing-governance/selections/"):
                remainder = path.removeprefix("/api/topic-writing-governance/selections/").strip("/")
                for suffix in ("/extract/retry", "/extract", "/retry"):
                    if remainder.endswith(suffix):
                        selection_id = unquote(remainder[: -len(suffix)].strip("/"))
                        self._send_json(200, self.service.collect(selection_id, payload))
                        return
            if path.startswith("/api/topic-writing-governance/sources/") and path.endswith("/archive"):
                source_id = unquote(path.removeprefix("/api/topic-writing-governance/sources/").removesuffix("/archive").strip("/"))
                self._send_json(200, self.service.archive_source(source_id, payload))
                return
            self._send_json(404, {"status": "not_found", "message": "接口不存在"})
        except Exception as exc:
            self._error(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="独立选题中心迁移服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--trendradar-root", default=None)
    args = parser.parse_args()
    service = TopicCenterService(args.runtime_root, trendradar_root=args.trendradar_root)
    server = create_server(args.host, args.port, service=service)
    print(f"topic-center-migration listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TopicMigrationHTTPServer", "TopicMigrationHandler", "create_server", "main"]
