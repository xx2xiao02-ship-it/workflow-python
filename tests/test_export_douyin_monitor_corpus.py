from __future__ import annotations

import json
import sqlite3

from tools.export_douyin_monitor_corpus import export_creator_corpus


def _db(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE creators (
            id INTEGER PRIMARY KEY,
            name TEXT,
            nickname TEXT,
            platform TEXT,
            sec_uid TEXT,
            bio TEXT
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            creator_id INTEGER,
            video_id TEXT,
            title TEXT,
            duration_ms INTEGER,
            create_time INTEGER,
            hashtags TEXT
        );
        CREATE TABLE transcripts (
            video_id INTEGER,
            full_text TEXT
        );
        INSERT INTO creators VALUES (1, '分享链接博主', '理性避难所', 'douyin', 'sec-1', '用机制解释人性');
        INSERT INTO videos VALUES (10, 1, '123456', '一个完整的作品描述\n第二行', 5050, 1785331011, '["商业思维"]');
        INSERT INTO videos VALUES (11, 1, '654321', '只有描述的作品', 0, 0, '[]');
        INSERT INTO transcripts VALUES (10, '这是本地采集到的口播转录，长度足够进入导出文件。');
        """
    )
    connection.commit()
    connection.close()


def test_export_creator_corpus_preserves_source_metadata_and_transcript(tmp_path) -> None:
    database = tmp_path / "douyin_monitor.db"
    output = tmp_path / "corpus"
    _db(database)

    report = export_creator_corpus(db_path=database, creator_id="1", output=output)

    assert report["discovered"] == 2
    assert report["exported"] == 2
    assert report["with_local_transcript"] == 1
    exported = output / "123456.md"
    assert exported.is_file()
    text = exported.read_text(encoding="utf-8")
    assert "https://www.douyin.com/video/123456" in text
    assert "本地采集到的口播转录" in text
    metadata = json.loads(text.split("---\n", 2)[1])
    assert metadata["source_origin"] == "douyin-monitor.sqlite.videos"
    assert metadata["source_quality"] == "description_plus_local_transcript"
    assert json.loads((output / "_export_manifest.json").read_text(encoding="utf-8"))["exported"] == 2


def test_export_creator_corpus_is_idempotent_without_overwrite(tmp_path) -> None:
    database = tmp_path / "douyin_monitor.db"
    output = tmp_path / "corpus"
    _db(database)

    first = export_creator_corpus(db_path=database, creator_id="理性避难所", output=output)
    second = export_creator_corpus(db_path=database, creator_id="理性避难所", output=output)

    assert first["exported"] == 2
    assert first["with_local_transcript"] == 1
    assert second["exported"] == 0
    assert second["skipped"] == 2
    assert second["with_local_transcript"] == 1
