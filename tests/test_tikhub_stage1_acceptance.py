from __future__ import annotations

import functools
import hashlib
import http.server
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

from workflow_1256.account_knowledge.obsidian_repository import ObsidianRepository
from workflow_1256.account_knowledge.production_bridge import sync_collection_to_obsidian
from workflow_1256.douyin_style_collector import collect_creator_tikhub
from workflow_1256.douyin_tikhub_adapter import (
    DOUYIN_USER_POST_VIDEOS_PATH,
    TikHubDouyinAdapter,
)


def _long_text(index: int) -> str:
    return f"这是第 {index} 条完整的本地转写样本，用于验证证据集边界、作品卡落盘、断点恢复和来源追踪。" * 3


def _evidence_report(count: int) -> dict[str, object]:
    return {
        "collection_status": "completed",
        "platform": "douyin",
        "creator_id": "sec_boundary_demo",
        "creator_url": "https://www.douyin.com/user/sec_boundary_demo",
        "creator_name": "边界测试账号",
        "target_valid_works": 50,
        "candidate_count": count,
        "knowledge_source_ids": ["source-boundary-demo"],
        "knowledge_source_versions": {"source-boundary-demo": "source-v1"},
        "items": [
            {
                "aweme_id": f"boundary-{index}",
                "video_id": f"boundary-{index}",
                "title": f"边界作品 {index}",
                "source_url": f"https://www.douyin.com/video/boundary-{index}",
                "published_at": "2026-09-02T00:00:00+00:00",
                "transcript_status": "local_whisper",
                "transcript": _long_text(index),
            }
            for index in range(count)
        ],
    }


@pytest.mark.parametrize(
    ("count", "expected_status", "expected_reached"),
    [(49, "WAITING_FOR_EVIDENCE", False), (50, "READY_TO_DISTILL", True)],
)
def test_evidence_set_49_50_boundary(tmp_path: Path, count: int, expected_status: str, expected_reached: bool):
    repository = ObsidianRepository(tmp_path / "vault")
    result = sync_collection_to_obsidian(
        _evidence_report(count),
        repository=repository,
        requested_account_id=f"boundary-{count}",
        distill_assets=False,
        task_id=f"job-boundary-{count}",
        candidate_count=count,
    )

    assert result["eligible_work_count"] == count
    assert result["target_valid_works"] == 50
    assert result["target_reached"] is expected_reached
    assert result["evidence_status"] == expected_status
    evidence = repository.load_evidence_set(result["account_id"], result["evidence_set_id"])
    job = repository.load_distillation_job(f"job-boundary-{count}")
    assert evidence.status == expected_status
    assert evidence.eligible_work_count == count
    assert job.status == ("READY_TO_DISTILL" if expected_reached else "WAITING_FOR_EVIDENCE")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        return


class _LocalMediaServer:
    def __init__(self, root: Path):
        handler = functools.partial(_QuietHandler, directory=str(root))
        self.server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/sample.mp4"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_local_media_download_ffmpeg_obsidian_evidence_loop(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.fail("本地闭环验收需要 ffmpeg 和 ffprobe，当前环境未找到")

    media_root = tmp_path / "media"
    media_root.mkdir()
    media_file = media_root / "sample.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(media_file),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr[-1000:]
    assert media_file.stat().st_size > 1024

    with _LocalMediaServer(media_root) as media_server:
        calls: list[tuple[str, str, dict[str, object]]] = []

        def requester(method, endpoint, *, params):
            calls.append((method, endpoint, dict(params)))
            assert endpoint == DOUYIN_USER_POST_VIDEOS_PATH
            return {
                "data": {
                    "aweme_list": [
                        {
                            "aweme_id": "local-media-1",
                            "desc": "本地媒体闭环作品",
                            "create_time": 1788307200,
                            "author": {"nickname": "本地闭环账号"},
                            "video": {
                                "duration": 1000,
                                "play_addr": {"url_list": [media_server.url]},
                                "cover": {"url_list": ["https://cdn.invalid/cover.jpg"]},
                            },
                        }
                    ],
                    "max_cursor": "0",
                    "has_more": False,
                }
            }

        adapter = TikHubDouyinAdapter(requester=requester)
        repository = ObsidianRepository(tmp_path / "vault")
        task_id = "job-local-media"
        source_id = "source-local-media"
        evidence_set_id = "evidence-local-media"
        item_reports: list[dict[str, object]] = []

        def extract_audio(video_path: str, audio_path: str):
            completed = subprocess.run(
                [ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audio_path],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr[-1000:]
            return audio_path

        def transcribe(audio_path: str):
            assert Path(audio_path).is_file()
            assert Path(audio_path).stat().st_size > 100
            return [{"text": _long_text(1)}]

        def persist_item(item: dict[str, object]):
            item_reports.append(dict(item))
            sync_collection_to_obsidian(
                {
                    "collection_status": "partial",
                    "platform": "douyin",
                    "creator_id": "sec_local_media",
                    "creator_url": "https://www.douyin.com/user/sec_local_media",
                    "creator_name": "本地闭环账号",
                    "target_valid_works": 50,
                    "candidate_count": 1,
                    "knowledge_source_ids": [source_id],
                    "items": [dict(item)],
                },
                repository=repository,
                requested_account_id="local-media-account",
                distill_assets=False,
                task_id=task_id,
                evidence_set_id=evidence_set_id,
                checkpoint_state="item_saved",
                candidate_count=1,
            )

        report = collect_creator_tikhub(
            "https://www.douyin.com/user/sec_local_media",
            limit=1,
            target_samples=1,
            adapter=adapter,
            transcriber_runtime={"extract_audio": extract_audio, "transcribe": transcribe, "ffprobe_path": ffprobe},
            item_reporter=persist_item,
            account_id="local-media-account",
            source_id=source_id,
            evidence_set_id=evidence_set_id,
            checkpoint_reporter=lambda _checkpoint: None,
        )

        assert report["collection_status"] == "completed"
        assert report["transcript_ready_count"] == 1
        assert len(item_reports) == 1
        assert calls and calls[0][1] == DOUYIN_USER_POST_VIDEOS_PATH

        final = sync_collection_to_obsidian(
            {
                **report,
                "target_valid_works": 50,
                "knowledge_source_ids": [source_id],
                "knowledge_source_versions": {source_id: "source-v1"},
            },
            repository=repository,
            requested_account_id="local-media-account",
            distill_assets=False,
            task_id=task_id,
            evidence_set_id=evidence_set_id,
            checkpoint_state="collected",
            candidate_count=report["candidate_count"],
        )

    account_id = "local-media-account"
    cards = repository.list_work_cards(account_id)
    raw_records = repository.list_raw_source_records(account_id)
    evidence = repository.load_evidence_set(account_id, evidence_set_id)
    job = repository.load_distillation_job(task_id)
    assert final["evidence_status"] == "WAITING_FOR_EVIDENCE"
    assert len(cards) == 1
    assert len(raw_records) == 1
    assert cards[0].work_id == raw_records[0].work_id
    assert raw_records[0].source_id == source_id
    assert evidence.source_ids == [source_id]
    assert evidence.eligible_work_count == 1
    assert job.evidence_set_id == evidence_set_id
    assert job.status == "WAITING_FOR_EVIDENCE"
    assert hashlib.sha256(media_file.read_bytes()).hexdigest()
