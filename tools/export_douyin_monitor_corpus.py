"""把本地 douyin-monitor 账号记录导出为账号知识库可导入的 Markdown 语料。

该工具只读取 SQLite，不访问网络、不调用模型，也不直接写入 Obsidian Vault。
导出的文件保留作品链接、发布时间、标签和本地转录来源，供
``run_account_knowledge_lab.py import-corpus`` 进行显式导入。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "services" / "douyin-monitor" / "data" / "douyin_monitor.db"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _published_at(value: Any) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _hashtags(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        text = _clean(item)
        if text and text not in result:
            result.append(text)
    return result


def _short_title(description: str, video_id: str) -> str:
    first = next((_clean(line) for line in description.splitlines() if _clean(line)), "")
    return first[:120] or f"抖音作品 {video_id}"


def _resolve_creator(connection: sqlite3.Connection, creator_id: str) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    key = _clean(creator_id)
    row = connection.execute(
        """
        SELECT id, name, nickname, platform, sec_uid, bio
        FROM creators
        WHERE CAST(id AS TEXT) = ? OR name = ? OR nickname = ? OR sec_uid = ?
        ORDER BY id
        LIMIT 1
        """,
        (key, key, key, key),
    ).fetchone()
    if row is None:
        raise ValueError(f"找不到 creator：{key}")
    return row


def _frontmatter(metadata: Mapping[str, Any]) -> str:
    # JSON 对象也是合法 YAML；这样在没有 PyYAML 时仍可由项目导入器回退解析。
    return "---\n" + json.dumps(dict(metadata), ensure_ascii=False, indent=2) + "\n---\n"


def _body(description: str, transcript: str) -> str:
    parts = ["## 作品描述", "", description]
    if transcript:
        parts.extend(["", "## 本地口播转录", "", transcript])
    else:
        parts.extend(["", "## 本地口播转录", "", "（本地监测库尚未采集转录）"])
    return "\n".join(parts).strip() + "\n"


def export_creator_corpus(
    *,
    db_path: Path | str,
    creator_id: str,
    output: Path | str,
    min_transcript_chars: int = 20,
    overwrite: bool = False,
) -> dict[str, Any]:
    """导出一个账号的作品描述/本地转录，返回可审计报告。"""

    if min_transcript_chars < 1:
        raise ValueError("min_transcript_chars 必须是正整数")
    database = Path(db_path).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"douyin-monitor 数据库不存在：{database}")
    target.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        creator = _resolve_creator(connection, creator_id)
        rows = connection.execute(
            """
            SELECT v.id, v.video_id, v.title, v.duration_ms, v.create_time, v.hashtags,
                   t.full_text
            FROM videos v
            LEFT JOIN transcripts t ON t.video_id = v.id
            WHERE v.creator_id = ?
            ORDER BY v.create_time DESC, v.id DESC
            """,
            (creator["id"],),
        ).fetchall()

    report: dict[str, Any] = {
        "status": "ready",
        "mode": "local_sqlite_export",
        "db_path": str(database),
        "output": str(target),
        "creator": {
            "id": creator["id"],
            "name": _clean(creator["name"]),
            "nickname": _clean(creator["nickname"]),
            "platform": _clean(creator["platform"]),
            "bio": _clean(creator["bio"]),
        },
        "discovered": len(rows),
        "exported": 0,
        "skipped": 0,
        "failed": 0,
        "with_local_transcript": 0,
        "items": [],
    }
    for row in rows:
        video_id = _clean(row["video_id"])
        description = _clean(row["title"])
        if not video_id or not description:
            report["failed"] += 1
            report["items"].append({"video_id": video_id, "status": "failed", "message": "缺少 video_id 或作品描述"})
            continue
        transcript = _clean(row["full_text"])
        has_transcript = len(transcript) >= min_transcript_chars
        if not has_transcript:
            transcript = ""
        tags = _hashtags(row["hashtags"])
        source_url = f"https://www.douyin.com/video/{video_id}"
        metadata = {
            "title": _short_title(description, video_id),
            "source_url": source_url,
            "published_at": _published_at(row["create_time"]),
            "date": _published_at(row["create_time"]),
            "platform": _clean(creator["platform"]) or "douyin",
            "author": _clean(creator["nickname"]) or _clean(creator["name"]),
            "video_id": video_id,
            "source_origin": "douyin-monitor.sqlite.videos",
            "source_record_id": int(row["id"]),
            "source_quality": "description_plus_local_transcript" if has_transcript else "description_only",
            "transcript_status": "local_whisper" if has_transcript else "not_collected",
            "duration_seconds": round(float(row["duration_ms"] or 0) / 1000.0, 3),
            "topic_tags": tags,
            "tags": tags,
            "source_types": ["抖音公开描述"] + (["本地口播转录"] if has_transcript else []),
        }
        path = target / f"{video_id}.md"
        if has_transcript:
            # Count source coverage independently of whether this invocation
            # writes a new file, so an idempotent rerun remains informative.
            report["with_local_transcript"] += 1
        if path.exists() and not overwrite:
            report["skipped"] += 1
            report["items"].append({"video_id": video_id, "status": "skipped", "path": str(path)})
            continue
        try:
            path.write_text(_frontmatter(metadata) + "\n" + _body(description, transcript), encoding="utf-8", newline="\n")
        except OSError as exc:
            report["failed"] += 1
            report["items"].append({"video_id": video_id, "status": "failed", "message": f"{type(exc).__name__}: {exc}"})
            continue
        report["exported"] += 1
        report["items"].append(
            {
                "video_id": video_id,
                "status": "exported",
                "path": str(path),
                "source_quality": metadata["source_quality"],
            }
        )

    manifest = {
        "schema_version": 1,
        "mode": "local_sqlite_export",
        "creator": report["creator"],
        "source_db": str(database),
        "discovered": report["discovered"],
        "exported": report["exported"],
        "with_local_transcript": report["with_local_transcript"],
        "note": "导出内容仍需确认原文完整性、授权和人工审核，不能直接视为已批准知识卡。",
    }
    (target / "_export_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 douyin-monitor 本地账号语料（不调用模型）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="douyin_monitor.db 路径")
    parser.add_argument("--creator-id", required=True, help="creator 表的 id、name、nickname 或 sec_uid")
    parser.add_argument("--output", required=True, help="导出的 Markdown 目录")
    parser.add_argument("--min-transcript-chars", type=int, default=20)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = export_creator_corpus(
            db_path=args.db,
            creator_id=args.creator_id,
            output=args.output,
            min_transcript_chars=args.min_transcript_chars,
            overwrite=args.overwrite,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failed"] == 0 else 2
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
