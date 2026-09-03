from __future__ import annotations

import sqlite3

from workflow_1256.trendradar_adapter import TrendRadarAdapter


def _make_snapshot(tmp_path):
    root = tmp_path / "TrendRadar"
    db_dir = root / "output" / "news"
    db_dir.mkdir(parents=True)
    (root / "version").write_text("6.10.0\n", encoding="utf-8")
    db_path = db_dir / "2026-08-26.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE platforms (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE news_items (
            id INTEGER PRIMARY KEY, title TEXT, platform_id TEXT, rank INTEGER,
            url TEXT, mobile_url TEXT, first_crawl_time TEXT,
            last_crawl_time TEXT, crawl_count INTEGER
        );
        CREATE TABLE crawl_records (id INTEGER PRIMARY KEY, crawl_time TEXT, total_items INTEGER, created_at TEXT);
        INSERT INTO platforms VALUES ('zhihu', '知乎');
        INSERT INTO platforms VALUES ('toutiao', '今日头条');
        INSERT INTO news_items VALUES (1, '人工智能工具进入职场', 'zhihu', 1, 'https://example.com/ai', '', '15-00', '15-30', 2);
        INSERT INTO news_items VALUES (2, '普通消费市场观察', 'toutiao', 2, 'https://example.com/market', '', '15-00', '15-30', 1);
        INSERT INTO crawl_records VALUES (1, '15-30', 2, '2026-08-26 15:30:00');
        """
    )
    connection.commit()
    connection.close()
    return root


def test_trendradar_health_and_candidates_are_read_only(tmp_path):
    root = _make_snapshot(tmp_path)
    adapter = TrendRadarAdapter(root)

    health = adapter.health()
    assert health["status"] == "ready"
    assert health["provider"] == "TrendRadar"
    assert health["item_count"] == 2

    result = adapter.candidates(intent="人工智能")
    assert result["status"] == "ready"
    assert result["provider"] == "TrendRadar"
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["source_type"] == "trendradar_hot"
    assert candidate["platform"] == "zhihu"
    assert candidate["source_metadata"]["provider"] == "TrendRadar"


def test_trendradar_source_filter_and_missing_snapshot(tmp_path):
    root = _make_snapshot(tmp_path)
    adapter = TrendRadarAdapter(root)
    result = adapter.candidates(sources=["toutiao_hot"])
    assert [item["platform"] for item in result["candidates"]] == ["toutiao"]

    missing = TrendRadarAdapter(tmp_path / "missing").health()
    assert missing["status"] == "blocked"
    assert "output/news" in missing["message"]
