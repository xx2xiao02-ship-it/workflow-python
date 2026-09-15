from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from topic_migration.config_service import ConfigService
from topic_migration.errors import NotFoundError, PersistenceError, ValidationError
from topic_migration.governance_store import TopicGovernanceStore
from topic_migration.server import create_server
from topic_migration.tikhub_transport import TikHubTransport, _normalize_item, filter_short_videos
from topic_migration.topic_service import TopicCenterService
from topic_migration.trendradar_adapter import TrendRadarAdapter


class FakeTikHub:
    def __init__(self) -> None:
        self.search_calls = 0

    def health(self) -> dict[str, Any]:
        return {
            "provider": "TikHub",
            "service": "tikhub",
            "status": "configured_not_verified",
            "auth_configured": True,
            "connection_status": "not_tested",
        }

    def search_related(self, keyword: str, *, platforms: list[str], limit: int, wechat_ghid: str = "") -> dict[str, Any]:
        self.search_calls += 1
        return {
            "status": "ready",
            "keyword": keyword,
            "contents": [
                {
                    "content_id": "fake-article-1",
                    "provider": "TikHub",
                    "platform": platforms[0] if platforms else "zhihu",
                    "content_type": "article",
                    "title": "synthetic related content",
                    "url": "https://example.com/synthetic-related",
                    "content_preview": "synthetic preview",
                }
            ],
            "queries": [{"platform": platforms[0] if platforms else "zhihu", "status": "ready"}],
        }

    def fetch_zhihu_detail(self, source_url: str) -> dict[str, Any]:
        raise AssertionError("知乎预览不应触发详情 Transport")


class _Response:
    status = 200

    def __init__(self, value: Any, url: str = "https://configured.example") -> None:
        self.value = value
        self.url = url

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, *_args: Any) -> bytes:
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")

    def geturl(self) -> str:
        return self.url


def _write_synthetic_trendradar(root: Path) -> Path:
    """Create an isolated synthetic TrendRadar snapshot for offline tests."""

    news_dir = root / "output" / "news"
    news_dir.mkdir(parents=True)
    (root / "version").write_text("synthetic-test-snapshot\n", encoding="utf-8")
    db_path = news_dir / "2026-09-15-synthetic.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE platforms (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY,
                title TEXT,
                platform_id TEXT,
                rank INTEGER,
                url TEXT,
                mobile_url TEXT,
                first_crawl_time TEXT,
                last_crawl_time TEXT,
                crawl_count INTEGER
            );
            CREATE TABLE crawl_records (
                id INTEGER PRIMARY KEY,
                crawl_time TEXT,
                created_at TEXT,
                total_items INTEGER
            );
            INSERT INTO platforms VALUES ('zhihu', '知乎');
            INSERT INTO news_items VALUES (
                1, 'synthetic AI topic', 'zhihu', 1,
                'https://www.zhihu.com/question/123456',
                'https://www.zhihu.com/question/123456',
                '2026-09-15T00:00:00+08:00',
                '2026-09-15T00:00:00+08:00', 1
            );
            INSERT INTO crawl_records VALUES (1, '2026-09-15T00:00:00+08:00', '2026-09-15T00:00:00+08:00', 1);
            """
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


def _synthetic_secret(label: str) -> str:
    """Build a clearly synthetic token without a static secret-like literal."""

    return "_".join(("synthetic", label, "test", "value"))


def _json_request(base: str, path: str, *, method: str = "GET", body: Any = None) -> tuple[int, dict[str, Any], str]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw), raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return int(exc.code), json.loads(raw), raw


def test_config_save_restart_mask_and_empty_secret_preservation(tmp_path: Path) -> None:
    config = ConfigService(tmp_path / "control-plane")
    config_secret = _synthetic_secret("config")
    saved = config.save(
        {
            "group_id": "tikhub",
            "channel_id": "tikhub",
            "settings": {"endpoint": "https://configured.example/api", "timeout_seconds": 12.5, "max_retries": 0},
            "secrets": {"primary_api_key": config_secret},
        }
    )
    serialized = json.dumps(saved, ensure_ascii=False)
    assert config_secret not in serialized
    assert saved["groups"][0]["credential_masked"] == "********"
    restarted = ConfigService(tmp_path / "control-plane")
    snapshot = restarted.public_snapshot()
    group = snapshot["groups"][0]
    assert group["settings"]["endpoint"] == "https://configured.example/api"
    assert group["settings"]["timeout_seconds"] == 12.5
    assert restarted.get_tikhub_transport_config().token == config_secret
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hashlib, sys; from topic_migration.config_service import ConfigService; token=ConfigService(sys.argv[1]).get_tikhub_transport_config().token; print(hashlib.sha256(token.encode('utf-8')).hexdigest())",
            str(config.root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == hashlib.sha256(config_secret.encode("utf-8")).hexdigest()
    restarted.save(
        {
            "group_id": "tikhub",
            "channel_id": "tikhub",
            "settings": {},
            "secrets": {"primary_api_key": ""},
        }
    )
    assert ConfigService(tmp_path / "control-plane").get_tikhub_transport_config().token == config_secret


def test_config_rejects_illegal_values(tmp_path: Path) -> None:
    config = ConfigService(tmp_path / "control-plane")
    with pytest.raises(ValidationError):
        config.save({"group_id": "other", "channel_id": "other", "settings": {}, "secrets": {}})
    with pytest.raises(ValidationError):
        config.save({"group_id": "tikhub", "channel_id": "tikhub", "settings": {"endpoint": "https://u:p@example.com"}, "secrets": {}})
    with pytest.raises(ValidationError):
        config.save({"group_id": "tikhub", "channel_id": "tikhub", "settings": {"timeout_seconds": "nan"}, "secrets": {}})
    with pytest.raises(ValidationError):
        config.save({"group_id": "tikhub", "channel_id": "tikhub", "settings": {"max_retries": 0.5}, "secrets": {}})
    with pytest.raises(ValidationError):
        config.save({"group_id": "tikhub", "channel_id": "tikhub", "settings": {"unknown": 1}, "secrets": {}})


def test_topic_runtime_root_inside_source_is_rejected() -> None:
    source_root = Path(__file__).resolve().parents[1]
    with pytest.raises(PersistenceError) as error:
        TopicCenterService(source_root / "runtime-boundary-probe")
    assert error.value.code == "runtime_root_inside_source"


def test_transport_uses_registered_config_and_does_not_leak_token(tmp_path: Path) -> None:
    config = ConfigService(tmp_path / "control-plane")
    transport_secret = _synthetic_secret("transport")
    config.save(
        {
            "group_id": "tikhub",
            "channel_id": "tikhub",
            "settings": {"endpoint": "https://configured.example/api", "timeout_seconds": 9, "max_retries": 0},
            "secrets": {"primary_api_key": transport_secret},
        }
    )
    calls: list[tuple[str, str, float, str]] = []

    def opener(request: Request, timeout: float) -> _Response:
        calls.append((request.method, request.full_url, timeout, request.get_header("Authorization") or ""))
        return _Response({"data": [{"id": "1", "title": "long video", "url": "https://example.com/v", "duration_ms": 61000, "type": "video"}]})

    transport = TikHubTransport(config, opener=opener)
    result = transport.search_related("synthetic", platforms=["douyin"], limit=3)
    assert result["contents"][0]["duration_seconds"] == 61.0
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://configured.example/api/api/v1/douyin/search/fetch_video_search_v2"
    assert calls[0][2] == 9.0
    assert calls[0][3] == "Bearer " + transport_secret
    assert transport_secret not in json.dumps(result, ensure_ascii=False)


def test_duration_units_and_invalid_duration_are_auditable() -> None:
    common = {"id": "1", "title": "synthetic video", "url": "https://example.com/video"}
    assert _normalize_item({**common, "duration": "01:02"}, platform="douyin", endpoint="/x", keyword="synthetic")["duration_seconds"] == 62.0
    assert _normalize_item({**common, "duration_seconds": 61}, platform="douyin", endpoint="/x", keyword="synthetic")["duration_seconds"] == 61.0
    invalid = _normalize_item({**common, "duration": "not-a-duration"}, platform="douyin", endpoint="/x", keyword="synthetic")
    assert invalid["duration_seconds"] is None
    kept, removed = filter_short_videos([invalid, {**common, "duration_seconds": 60, "content_type": "video"}])
    assert len(kept) == 1
    assert removed == 1


def test_trendradar_snapshot_is_read_only_and_validates_source_url(tmp_path: Path) -> None:
    db_path = _write_synthetic_trendradar(tmp_path / "trendradar")
    before = db_path.stat().st_mtime_ns
    adapter = TrendRadarAdapter(tmp_path / "trendradar")
    result = adapter.candidates(intent="AI", limit=10)
    assert result["status"] == "ready"
    assert result["candidates"][0]["candidate_id"].startswith("trendradar-")
    assert result["candidates"][0]["url"].startswith("https://")
    assert db_path.stat().st_mtime_ns == before


def test_topic_store_content_ready_gate_hash_and_id_pairing(tmp_path: Path) -> None:
    store = TopicGovernanceStore(tmp_path / "topic" / "governance.json")
    content_items = [{"content_id": "content-1", "title": "synthetic article", "url": "https://example.com/source"}]
    selection = store.save_selection({"candidate_id": "candidate-1", "title": "synthetic topic", "source_url": "https://example.com/source", "content_items": content_items, "primary_content_id": "content-1"})
    assert selection["selection_id"] == "topic-candidate-1"
    long_text = "synthetic content " * 8
    source = store.save_extraction_result(selection["selection_id"], {"status": "ready", "content": long_text, "source_content_id": "source-1", "final_url": "https://example.com/source"})
    assert source["status"] == "CONTENT_READY"
    assert source["content_hash"]
    handoff = store.writing_input("source-1", selection["selection_id"], project_id="topic-center", run_id="synthetic-run")
    assert handoff["topic_content_manifest"]["selection_id"] == selection["selection_id"]
    assert handoff["topic_content_manifest"]["source_content_id"] == "source-1"
    assert handoff["topic_content_manifest"]["content_sha256"] == source["content_hash"]
    with pytest.raises(NotFoundError):
        store.writing_input("source-1", "topic-other")
    short = store.save_extraction_result(selection["selection_id"], {"status": "ready", "content": "too short", "source_content_id": "source-short", "final_url": "https://example.com/source"})
    assert short["status"] == "CONTENT_MISSING"
    with pytest.raises(ValidationError):
        store.writing_input("source-short", selection["selection_id"])


def test_zhihu_preview_is_deferred_without_network(tmp_path: Path) -> None:
    _write_synthetic_trendradar(tmp_path / "trendradar")
    called = False

    def forbidden_opener(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("preview opener must not be called")

    service = TopicCenterService(tmp_path / "runtime", trendradar_root=tmp_path / "trendradar", tikhub=FakeTikHub(), preview_opener=forbidden_opener)
    result = service.preview(service.topics()["candidates"][0]["candidate_id"])
    assert result["status"] == "deferred"
    assert called is False


def test_http_boundary_get_enrich_reads_cache_only_and_pages_are_independent(tmp_path: Path) -> None:
    _write_synthetic_trendradar(tmp_path / "trendradar")
    fake = FakeTikHub()
    service = TopicCenterService(tmp_path / "runtime", trendradar_root=tmp_path / "trendradar", tikhub=fake)
    server = create_server("127.0.0.1", 0, service=service, lock_path=tmp_path / "runtime" / "service.lock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for page, title in (("/topic-center", "选题中心"), ("/topic-writing-governance", "正文治理"), ("/api-management", "API 配置"), ("/writing", "尚未迁移")):
            response = urlopen(base + page, timeout=5)
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert title in body
        status, health, _ = _json_request(base, "/api/executor/health")
        assert status == 200
        assert health["scope"].startswith("F0公共基础")
        topic_id = service.topics()["candidates"][0]["candidate_id"]
        status, before, _ = _json_request(base, f"/api/topic-center/topics/{topic_id}/enrich?platforms=zhihu&limit=20")
        assert status == 200
        assert before["status"] == "not_enriched"
        assert fake.search_calls == 0
        status, enriched, _ = _json_request(base, f"/api/topic-center/topics/{topic_id}/enrich", method="POST", body={"platforms": ["zhihu"], "limit": 20})
        assert status == 200
        assert enriched["contents"]
        assert fake.search_calls == 1
        status, cached, _ = _json_request(base, f"/api/topic-center/topics/{topic_id}/enrich?platforms=zhihu&limit=20")
        assert status == 200
        assert cached["cached"] is True
        assert fake.search_calls == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_config_save_and_deferred_test_are_separate(tmp_path: Path) -> None:
    service = TopicCenterService(tmp_path / "runtime", tikhub=FakeTikHub())
    server = create_server("127.0.0.1", 0, service=service, lock_path=tmp_path / "runtime" / "service.lock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, saved, raw = _json_request(base, "/api/api-management/config", method="POST", body={"group_id": "tikhub", "channel_id": "tikhub", "settings": {"endpoint": "https://configured.example", "timeout_seconds": 11, "max_retries": 0}, "secrets": {"primary_api_key": _synthetic_secret("http")}})
        assert status == 200
        assert _synthetic_secret("http") not in raw
        status, tested, _ = _json_request(base, "/api/api-management/auth-verify", method="POST", body={"confirm": "controlled-auth-verification", "channels": ["tikhub"]})
        assert status == 200
        assert tested["status"] == "deferred"
        assert tested["executed"] is False
        status, bad, _ = _json_request(base, "/api/api-management/config", method="POST", body={"group_id": "tikhub", "channel_id": "tikhub", "settings": {"endpoint": "file:///secret"}, "secrets": {}})
        assert status == 422
        assert bad["code"] == "invalid_input"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_writing_governance_delete_requires_confirmation_and_preserves_store_boundary(tmp_path: Path) -> None:
    service = TopicCenterService(tmp_path / "runtime", tikhub=FakeTikHub())
    service.store.save_selection(
        {
            "selection_id": "selection-delete-http",
            "candidate_id": "candidate-delete-http",
            "title": "synthetic delete candidate",
            "source_url": "https://example.com/delete-source",
        }
    )
    server = create_server("127.0.0.1", 0, service=service, lock_path=tmp_path / "runtime" / "service.lock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, rejected, _ = _json_request(
            base,
            "/api/topic-writing-governance/selections/selection-delete-http/delete",
            method="POST",
            body={},
        )
        assert status == 422
        assert rejected["code"] == "invalid_input"
        status, deleted, _ = _json_request(
            base,
            "/api/topic-writing-governance/selections/selection-delete-http/delete",
            method="POST",
            body={"confirmed": True},
        )
        assert status == 200
        assert deleted["status"] == "deleted"
        assert deleted["assets_preserved"] is True
        assert service.store.list_selections() == []
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
